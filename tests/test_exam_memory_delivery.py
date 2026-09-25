import json
import sqlite3

import pytest

from cuoti_app.migrations.v54_exam_generation_memory_delivery import (
    MEMORY_EVENT_DELIVERY_HEADS,
    MEMORY_EVENT_OUTBOX,
    migrate_v53_to_v54,
    schema_issues,
)
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.services.exam_generation_memory_delivery import (
    GenerationMemoryDeliveryError,
    discard_pending_generation_memory_delivery,
    dispatch_generation_memory_delivery,
    recover_generation_memory_delivery,
    stage_generation_memory_delivery,
)


def _db(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute(
        "CREATE TABLE exams(id INTEGER PRIMARY KEY,artifact_status TEXT NOT NULL,"
        "generation_job_id TEXT)"
    )
    migrate_v53_to_v54(db)
    db.commit()
    assert schema_issues(db) == []
    return db


def _events(operation_id="job-1", exam_id=1, mode="free"):
    return [
        {
            "event_id": f"{operation_id}:request",
            "event_type": "request",
            "operation_id": operation_id,
            "request": {"difficulty": "中等", "question_count": 1},
        },
        {
            "event_id": f"{operation_id}:result:1",
            "event_type": "result",
            "operation_id": operation_id,
            "paper_index": 1,
            "result": {"exam_id": exam_id, "source": mode},
            "questions": [{"position": 1, "fingerprint": "question-1"}],
        },
    ]


def _stage(db, *, mode="free", actor="free_exam", operation_id="job-1", exam_id=1):
    db.execute(
        "INSERT INTO exams(id,artifact_status,generation_job_id) VALUES(?,'published',?)",
        (exam_id, operation_id),
    )
    return stage_generation_memory_delivery(
        db,
        operation_id=operation_id,
        exam_id=exam_id,
        mode=mode,
        actor=actor,
        event_day="2026-09-06",
        published_at="2026-09-06T12:00:00+00:00",
        events=_events(operation_id, exam_id, mode),
    )


def test_stage_joins_caller_transaction_and_rolls_back_atomically():
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    assert db.execute(f"SELECT COUNT(*) FROM {MEMORY_EVENT_OUTBOX}").fetchone()[0] == 1
    db.rollback()
    assert db.execute(f"SELECT COUNT(*) FROM {MEMORY_EVENT_OUTBOX}").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0


def test_stage_accepts_structurally_equal_noncanonical_json():
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO exams(id,artifact_status,generation_job_id) "
        "VALUES(1,'published','job-1')"
    )
    events = _events()
    for event in events:
        event["occurred_at"] = "2026-09-06T12:00:00+00:00"
    db.execute(
        f"INSERT INTO {MEMORY_EVENT_OUTBOX}("
        "operation_id,exam_id,mode,actor,event_day,published_at,events_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (
            "job-1", 1, "free", "free_exam", "2026-09-06",
            "2026-09-06T12:00:00+00:00",
            json.dumps(events, ensure_ascii=False, indent=2),
            "2026-09-06T12:00:00+00:00",
        ),
    )
    db.execute(
        f"INSERT INTO {MEMORY_EVENT_DELIVERY_HEADS} "
        "VALUES('job-1','pending',1,NULL,'2026-09-06T12:00:00+00:00')"
    )

    assert stage_generation_memory_delivery(
        db, operation_id="job-1", exam_id=1, mode="free", actor="free_exam",
        event_day="2026-09-06", published_at="2026-09-06T12:00:00+00:00",
        events=_events(),
    ) == "job-1"
    assert recover_generation_memory_delivery(db, operation_id="job-1").state == "pending"
    db.rollback()


@pytest.mark.parametrize(
    "mode,actor",
    [("free", "free_exam"), ("mistakes", "mistake_exam"), ("past", "past_exam")],
)
def test_dispatch_writes_only_the_mode_namespace(tmp_path, mode, actor):
    db = _db()
    operation_id = f"{mode}-job"
    db.execute("BEGIN IMMEDIATE")
    _stage(db, mode=mode, actor=actor, operation_id=operation_id)
    db.commit()
    memory = AIMemory(tmp_path / "memory")

    outcome = dispatch_generation_memory_delivery(
        db, operation_id=operation_id, memory=memory
    )

    assert outcome["state"] == "delivered"
    target = tmp_path / "memory" / mode / "events" / "daily" / "2026-09-06.jsonl"
    stored = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in stored] == ["request", "result"]
    assert all(event["actor"] == actor for event in stored)
    assert not [path for path in (tmp_path / "memory").rglob("*.jsonl") if path != target]


def test_stage_rejects_wrong_actor_and_unpublished_exam():
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO exams(id,artifact_status,generation_job_id) VALUES(1,'publishing','job-1')"
    )
    with pytest.raises(GenerationMemoryDeliveryError) as route_error:
        stage_generation_memory_delivery(
            db, operation_id="job-1", exam_id=1, mode="free", actor="past_exam",
            event_day="2026-09-06", published_at="2026-09-06T12:00:00+00:00",
            events=_events(),
        )
    assert route_error.value.reason == "memory_delivery_route_invalid"
    with pytest.raises(GenerationMemoryDeliveryError) as publish_error:
        stage_generation_memory_delivery(
            db, operation_id="job-1", exam_id=1, mode="free", actor="free_exam",
            event_day="2026-09-06", published_at="2026-09-06T12:00:00+00:00",
            events=_events(),
        )
    assert publish_error.value.reason == "memory_delivery_exam_not_published"
    db.rollback()


def test_stage_rejects_result_source_from_another_mode():
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    db.execute(
        "INSERT INTO exams(id,artifact_status,generation_job_id) "
        "VALUES(1,'published','job-1')"
    )
    with pytest.raises(GenerationMemoryDeliveryError) as error:
        stage_generation_memory_delivery(
            db, operation_id="job-1", exam_id=1, mode="mistakes",
            actor="mistake_exam", event_day="2026-09-06",
            published_at="2026-09-06T12:00:00+00:00",
            events=_events(mode="free"),
        )
    assert error.value.reason == "memory_delivery_result_mode_invalid"
    db.rollback()


def test_fsync_then_cas_failure_replays_on_the_fixed_day_without_duplicates(tmp_path):
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    db.commit()
    memory = AIMemory(tmp_path / "memory")

    def fail(point):
        assert point == "after_memory_fsync_before_delivery_cas"
        raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError, match="simulated crash"):
        dispatch_generation_memory_delivery(
            db, operation_id="job-1", memory=memory, fault_injector=fail
        )
    assert recover_generation_memory_delivery(db, operation_id="job-1").state == "pending"

    outcome = dispatch_generation_memory_delivery(
        db, operation_id="job-1", memory=memory
    )
    path = tmp_path / "memory" / "free" / "events" / "daily" / "2026-09-06.jsonl"
    assert outcome["state"] == "delivered"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    assert list((tmp_path / "memory" / "free" / "events" / "daily").glob("*.jsonl")) == [path]


def test_concurrent_cas_loser_accepts_an_already_delivered_head(tmp_path):
    database = tmp_path / "delivery.db"
    db = _db(database)
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    db.commit()
    memory = AIMemory(tmp_path / "memory")

    def win_elsewhere(point):
        assert point == "after_memory_fsync_before_delivery_cas"
        other = sqlite3.connect(database)
        try:
            other.execute(
                f"UPDATE {MEMORY_EVENT_DELIVERY_HEADS} SET "
                "state='delivered',revision=2,delivered_at=?,updated_at=? "
                "WHERE operation_id='job-1' AND state='pending' AND revision=1",
                ("2026-09-06T12:01:00+00:00", "2026-09-06T12:01:00+00:00"),
            )
            other.commit()
        finally:
            other.close()

    outcome = dispatch_generation_memory_delivery(
        db,
        operation_id="job-1",
        memory=memory,
        fault_injector=win_elsewhere,
    )
    assert outcome == {"state": "delivered", "revision": 2, "idempotent_replay": True}
    path = tmp_path / "memory" / "free" / "events" / "daily" / "2026-09-06.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_paused_memory_stays_pending_until_it_can_be_delivered(tmp_path):
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    db.commit()
    memory = AIMemory(tmp_path / "memory")
    memory.set_paused("free", True)

    paused = dispatch_generation_memory_delivery(
        db, operation_id="job-1", memory=memory
    )
    assert paused == {"state": "pending", "revision": 1, "reason": "memory_paused"}
    head = db.execute(
        f"SELECT state,revision FROM {MEMORY_EVENT_DELIVERY_HEADS}"
    ).fetchone()
    assert tuple(head) == ("pending", 1)
    assert list((tmp_path / "memory").rglob("*.jsonl")) == []

    memory.set_paused("free", False)
    assert dispatch_generation_memory_delivery(
        db, operation_id="job-1", memory=memory
    )["state"] == "delivered"


def test_append_failure_is_logged_and_never_acknowledged():
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    db.commit()

    class BrokenMemory:
        def append_events(self, *_args, **_kwargs):
            raise OSError("disk unavailable")

    class Logger:
        def __init__(self):
            self.messages = []

        def exception(self, message):
            self.messages.append(message)

    logger = Logger()
    with pytest.raises(OSError, match="disk unavailable"):
        dispatch_generation_memory_delivery(
            db, operation_id="job-1", memory=BrokenMemory(), logger=logger
        )
    assert logger.messages == ["failed to deliver published exam AI memory"]
    assert recover_generation_memory_delivery(db, operation_id="job-1").state == "pending"


def test_only_explicit_compensation_discards_pending_delivery(tmp_path):
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _stage(db)
    db.commit()
    db.execute("DELETE FROM exams WHERE id=1")
    db.commit()
    assert db.execute(f"SELECT COUNT(*) FROM {MEMORY_EVENT_OUTBOX}").fetchone()[0] == 1
    db.execute("BEGIN IMMEDIATE")
    assert discard_pending_generation_memory_delivery(db, operation_id="job-1") is True
    db.commit()
    assert db.execute(f"SELECT COUNT(*) FROM {MEMORY_EVENT_OUTBOX}").fetchone()[0] == 0

    db.execute("BEGIN IMMEDIATE")
    _stage(db, operation_id="job-2", exam_id=2)
    db.commit()
    dispatch_generation_memory_delivery(
        db, operation_id="job-2", memory=AIMemory(tmp_path / "memory")
    )
    db.execute("BEGIN IMMEDIATE")
    with pytest.raises(GenerationMemoryDeliveryError) as error:
        discard_pending_generation_memory_delivery(db, operation_id="job-2")
    assert error.value.reason == "memory_delivery_discard_delivered_forbidden"
    db.rollback()
