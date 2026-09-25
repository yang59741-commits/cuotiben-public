from __future__ import annotations

import json
from types import SimpleNamespace

from cuoti_app.migrations.v54_exam_generation_memory_delivery import (
    MEMORY_EVENT_DELIVERY_HEADS,
    migrate_v53_to_v54,
)
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.services.exam_generation_d2_publication import (
    D2_TABLE,
    recover_d2_publication_outcome,
    run_d2_quality_and_publish,
)
from cuoti_app.services.exam_generation_memory import pending_request_event
from cuoti_app.services.exam_generation_past_v3_dependencies import (
    PastV3ProductionAuthorities,
    build_past_v3_production_dependencies,
)
from cuoti_app.services.exam_generation_past_v3_integration import (
    PAST_V3_PUBLICATION_RECEIPT_EXTENSION,
    make_published_recovery_dispatcher,
)
from cuoti_app.services.exam_generation_past_v3_production import (
    acknowledge_publication_receipt,
)
from tests.test_exam_d2_publication import _artifact, _db, _fixture, _quality


def _install_delivery(db) -> None:
    migrate_v53_to_v54(db)
    db.commit()


def _memory_arguments(tmp_path, memory=None):
    return {
        "memory": memory or AIMemory(tmp_path / "memory"),
        "pending_memory_request": pending_request_event(
            {
                "source": "past",
                "subject_id": 1,
                "difficulty": "中等",
                "question_count": 1,
            },
            "job",
        ),
        "memory_subject": "数学",
    }


def _publish(db, tmp_path, *, memory, artifact_calls, receipt_callback=lambda _: None):
    state, facts, contract = _fixture()
    return run_d2_quality_and_publish(
        db,
        state=state,
        facts=facts,
        stage3_contract=contract,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="D2",
        artifact_dir=str(tmp_path),
        artifact_callback=_artifact(artifact_calls),
        quality_callback=_quality(),
        receipt_callback=receipt_callback,
        **_memory_arguments(tmp_path, memory),
    )


def test_native_commit_stages_real_past_events_then_delivers_after_commit(tmp_path):
    db = _db()
    _install_delivery(db)
    memory = AIMemory(tmp_path / "memory")

    outcome = _publish(db, tmp_path, memory=memory, artifact_calls=[])

    assert outcome.state == "completed"
    head = db.execute(
        f"SELECT state,revision FROM {MEMORY_EVENT_DELIVERY_HEADS} WHERE operation_id='job'"
    ).fetchone()
    assert tuple(head) == ("delivered", 2)
    publication = json.loads(
        db.execute(f"SELECT publication_json FROM {D2_TABLE} WHERE job_id='job'").fetchone()[0]
    )
    path = tmp_path / "memory" / "past" / "events" / "daily" / publication["published_at"][:10]
    events = [
        json.loads(line)
        for line in path.with_suffix(".jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["event_id"] for event in events] == ["job:request", "job:result:1"]
    assert all(event["occurred_at"] == publication["published_at"] for event in events)
    assert events[1]["result"] == {
        "exam_id": str(outcome.exam_id), "title": "D2", "source": "past"
    }
    assert events[1]["questions"] == [
        {
            "position": 1,
            "candidate_id": "candidate-1",
            "chapter": "代数",
            "difficulty": "中等",
            "adapted": False,
            "fingerprint": events[1]["questions"][0]["fingerprint"],
        }
    ]


def test_paused_delivery_keeps_published_paper_and_resume_does_no_work_twice(tmp_path):
    db = _db()
    _install_delivery(db)
    memory = AIMemory(tmp_path / "memory")
    memory.set_paused("past", True)
    artifact_calls = []

    first = _publish(db, tmp_path, memory=memory, artifact_calls=artifact_calls)

    assert first.state == "published_pending_receipt"
    assert len(artifact_calls) == 1
    assert db.execute(f"SELECT state FROM {D2_TABLE}").fetchone()[0] == "published_pending_receipt"
    assert db.execute("SELECT artifact_status FROM exams").fetchone()[0] == "published"

    class Logger:
        def exception(self, *_args, **_kwargs):
            raise AssertionError("recovery unexpectedly failed")

    app = SimpleNamespace(
        config={"AI_MEMORY_DIR": str(tmp_path / "memory")},
        extensions={
            PAST_V3_PUBLICATION_RECEIPT_EXTENSION: acknowledge_publication_receipt
        },
    )
    response = make_published_recovery_dispatcher(
        worker_id="worker", lease_attempt=1, app=app
    )(db, {"PDF_DIR": str(tmp_path)}, "job", logger=Logger())
    assert response.status == 202
    assert response.body["code"] == "memory_delivery_pending"

    memory.set_paused("past", False)
    second = run_d2_quality_and_publish(
        db,
        state=None,
        facts=None,
        stage3_contract=None,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="ignored",
        artifact_dir=str(tmp_path),
        artifact_callback=lambda _: (_ for _ in ()).throw(AssertionError("artifact repeated")),
        quality_callback=lambda _: (_ for _ in ()).throw(AssertionError("AI repeated")),
        receipt_callback=lambda _: None,
        memory=memory,
    )
    assert second.state == "completed"
    assert second.exam_id == first.exam_id
    assert len(artifact_calls) == 1


def test_failed_delivery_is_recovered_without_republishing(tmp_path):
    class BrokenMemory:
        def append_events(self, *_args, **_kwargs):
            raise OSError("memory unavailable")

    db = _db()
    _install_delivery(db)
    artifact_calls = []
    first = _publish(db, tmp_path, memory=BrokenMemory(), artifact_calls=artifact_calls)
    assert first.state == "published_pending_receipt"
    assert db.execute(f"SELECT state FROM {MEMORY_EVENT_DELIVERY_HEADS}").fetchone()[0] == "pending"

    recovered = recover_d2_publication_outcome(
        db,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        receipt_callback=lambda _: None,
        memory=AIMemory(tmp_path / "memory"),
    )
    assert recovered is not None and recovered.state == "completed"
    assert recovered.exam_id == first.exam_id
    assert len(artifact_calls) == 1


def test_old_published_d2_row_without_outbox_is_not_backfilled(tmp_path):
    db = _db()
    artifact_calls = []
    state, facts, contract = _fixture()
    first = run_d2_quality_and_publish(
        db,
        state=state,
        facts=facts,
        stage3_contract=contract,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="D2",
        artifact_dir=str(tmp_path),
        artifact_callback=_artifact(artifact_calls),
        quality_callback=_quality(),
        receipt_callback=None,
    )
    assert first.state == "published_pending_receipt"
    _install_delivery(db)

    recovered = recover_d2_publication_outcome(
        db,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        receipt_callback=lambda _: None,
        memory=AIMemory(tmp_path / "memory"),
    )
    assert recovered is not None and recovered.state == "completed"
    assert db.execute(
        f"SELECT COUNT(*) FROM {MEMORY_EVENT_DELIVERY_HEADS}"
    ).fetchone()[0] == 0
    assert list((tmp_path / "memory").rglob("*.jsonl")) == []


def test_production_dependencies_forward_the_real_ai_memory_instance(monkeypatch, tmp_path):
    from cuoti_app.services import exam_generation_d2_runtime as runtime
    from cuoti_app.services import exam_generation_past_v3_dependencies as dependencies

    memory = AIMemory(tmp_path / "memory")
    request_event = pending_request_event(
        {"source": "past", "subject_id": 1, "question_count": 1}, "job"
    )
    contract = object()
    monkeypatch.setattr(
        dependencies,
        "persist_stage3_generation_contract",
        lambda *_args, **_kwargs: SimpleNamespace(contract=contract),
    )
    captured = {}

    def fake_run(_db, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(runtime, "run_d2_generation", fake_run)
    built = build_past_v3_production_dependencies(
        PastV3ProductionAuthorities(
            profile_context=object(),
            authority_registry_loader=lambda **_kwargs: {},
            recent_usage_snapshot_loader=lambda **_kwargs: {},
            score_callback=None,
            score_prompt_version=None,
            score_model_id=None,
            quality_callback=lambda *_args, **_kwargs: None,
            subject_id=1,
            title="D2",
            artifact_dir=tmp_path,
            artifact_callback=lambda *_args, **_kwargs: None,
            memory=memory,
            pending_memory_request=request_event,
            memory_subject="数学",
        )
    )

    assert built.d2_runner is not None
    assert built.d2_runner(
        object(), job_id="job", worker_id="worker", lease_attempt=1
    ) == "done"
    assert captured["memory"] is memory
    assert captured["pending_memory_request"] == request_event
    assert captured["memory_subject"] == "数学"
