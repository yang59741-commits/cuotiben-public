from __future__ import annotations

import json
import sqlite3

import pytest

from cuoti_app.migrations.v54_exam_generation_memory_delivery import (
    MEMORY_EVENT_DELIVERY_HEADS,
    MEMORY_EVENT_OUTBOX,
    migrate_v53_to_v54,
)
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.services.exam_generation_memory_delivery import (
    dispatch_generation_memory_delivery,
    recover_generation_memory_delivery,
    stage_generation_memory_delivery,
)
from cuoti_app.services.exam_generation_recovery import recover_published_exam_job
from cuoti_app.services.exam_generation_success import (
    SuccessMemoryContext,
    build_success_memory_events,
)
from cuoti_app.services.exam_publish import (
    ExamPaperDraft,
    compensate_exam_publication,
    publish_exam_batch_result,
)
from cuoti_app.services.exam_repetition_policy import question_content_fingerprint


def _db(path=":memory:"):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE subjects(id INTEGER PRIMARY KEY,name TEXT)")
    db.execute("INSERT INTO subjects VALUES(1,'数学')")
    db.execute(
        "CREATE TABLE exam_generation_manifests("
        "job_id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,"
        "manifest_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE exams("
        "id INTEGER PRIMARY KEY,subject_id INTEGER,title TEXT,config TEXT,"
        "content TEXT,created_at TEXT,generation_job_id TEXT,batch_index INTEGER,"
        "artifact_status TEXT,latex_source TEXT,latex_sha256 TEXT,"
        "artifact_revision INTEGER,recompile_available INTEGER,scoring_mode TEXT,"
        "total_points REAL,scoring_evidence_hash TEXT,scoring_projection_hash TEXT,"
        "pdf_sha256 TEXT)"
    )
    migrate_v53_to_v54(db)
    db.commit()
    return db


def _events(operation_id: str, exam_id: int, *, mode="free"):
    request = {
        "event_id": f"{operation_id}:request",
        "event_type": "request",
        "operation_id": operation_id,
        "request": {"difficulty": "中等", "question_count": 1},
    }
    results = build_success_memory_events(
        SuccessMemoryContext(
            source=mode,
            operation_id=operation_id,
            subject="数学",
            chapters=("函数",),
            request={"difficulty": "中等", "question_count": 1},
            results=({"id": exam_id, "title": "试卷", "content": "题面"},),
            generated_question_items=({"position": 1, "content": "1+1=?"},),
        ),
        fingerprint=question_content_fingerprint,
    )
    return (request, *results)


def _insert_published(db, *, operation_id="job-1", exam_id=1):
    db.execute(
        "INSERT INTO exams(id,subject_id,title,config,content,created_at,"
        "generation_job_id,batch_index,artifact_status,latex_source,"
        "artifact_revision,recompile_available,scoring_mode) "
        "VALUES(?,1,'试卷','{\"batch_size\":1}','题面','2026-09-06 12:00',"
        "?,1,'published','tex',1,1,'unscored')",
        (exam_id, operation_id),
    )


def _stage(db, *, operation_id="job-1", exam_id=1):
    return stage_generation_memory_delivery(
        db,
        operation_id=operation_id,
        exam_id=exam_id,
        mode="free",
        actor="free_exam",
        event_day="2026-09-06",
        published_at="2026-09-06 12:00",
        events=_events(operation_id, exam_id),
    )


def test_legacy_publisher_stages_fixed_events_in_final_publication_transaction(tmp_path):
    db = _db()

    def stage(db_in_transaction, result):
        assert db_in_transaction.in_transaction
        stage_generation_memory_delivery(
            db_in_transaction,
            operation_id="request-1",
            exam_id=result.id,
            mode="free",
            actor="free_exam",
            event_day="2026-09-06",
            published_at="2026-09-06 12:00",
            events=_events("request-1", result.id),
        )

    published = publish_exam_batch_result(
        db,
        subject_id=1,
        source="free",
        generation_job_id=None,
        drafts=(ExamPaperDraft(
            title="试卷",
            body=r"\begin{problem}1+1=?\end{problem}",
            config={"batch_size": 1},
            latex_source="tex",
        ),),
        reference_mistakes=(),
        pdf_dir=str(tmp_path),
        xelatex_command="xelatex",
        xelatex_timeout=1,
        testing=True,
        compile_exam=lambda *_args, **_kwargs: None,
        store_exam_items=lambda *_args, **_kwargs: None,
        progress=lambda *_args: None,
        created_at="2026-09-06 12:00",
        stage_published=stage,
        memory_delivery_operation_id="request-1",
    )

    delivery = recover_generation_memory_delivery(
        db, operation_id="request-1"
    )
    assert published.exam_ids == (1,)
    assert delivery.exam_id == 1
    assert delivery.state == "pending"
    assert [event["event_type"] for event in delivery.events] == ["request", "result"]
    assert all(event["occurred_at"] == "2026-09-06 12:00" for event in delivery.events)


def test_publication_failure_explicitly_discards_pending_envelope(tmp_path):
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _insert_published(db)
    _stage(db)
    db.commit()

    compensate_exam_publication(
        db,
        (1,),
        pdf_dir=str(tmp_path),
        memory_delivery_operation_id="job-1",
    )

    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0
    assert db.execute(f"SELECT COUNT(*) FROM {MEMORY_EVENT_OUTBOX}").fetchone()[0] == 0


def test_delivered_history_cannot_be_removed_by_publication_compensation(tmp_path):
    db = _db()
    db.execute("BEGIN IMMEDIATE")
    _insert_published(db)
    _stage(db)
    db.commit()
    dispatch_generation_memory_delivery(
        db, operation_id="job-1", memory=AIMemory(tmp_path / "memory")
    )

    compensate_exam_publication(
        db,
        (1,),
        pdf_dir=str(tmp_path),
        memory_delivery_operation_id="job-1",
    )

    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
    assert db.execute(f"SELECT state FROM {MEMORY_EVENT_DELIVERY_HEADS}").fetchone()[0] == "delivered"


def test_recovery_delivers_same_operation_without_ai_or_compile(tmp_path):
    db = _db(tmp_path / "db.sqlite3")
    _insert_published(db)
    _stage(db)
    db.commit()
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "exam_1.pdf").write_bytes(b"%PDF-1.7\n")
    memory = AIMemory(tmp_path / "memory")
    memory.set_paused("free", True)
    config = {
        "DATABASE": str(tmp_path / "db.sqlite3"),
        "PDF_DIR": str(pdf_dir),
        "AI_MEMORY_DIR": str(tmp_path / "memory"),
    }

    pending = recover_published_exam_job(
        db, config, "job-1", logger=None, memory=memory
    )
    assert pending.status == 503
    assert pending.body["code"] == "memory_delivery_pending"
    assert pending.body["memory_delivery"] == "pending"
    assert db.execute("SELECT artifact_status FROM exams").fetchone()[0] == "published"

    memory.set_paused("free", False)
    recovered = recover_published_exam_job(
        db, config, "job-1", logger=None, memory=memory
    )
    assert recovered.status is None
    assert recovered.body["idempotency_replayed"] is True
    assert recovered.body["memory_delivery"] == "delivered"
    assert recover_generation_memory_delivery(db, operation_id="job-1").state == "delivered"
    path = tmp_path / "memory" / "free" / "events" / "daily" / "2026-09-06.jsonl"
    assert [json.loads(line)["event_type"] for line in path.read_text(encoding="utf-8").splitlines()] == ["request", "result"]

    recover_published_exam_job(db, config, "job-1", logger=None, memory=memory)
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_legacy_history_without_outbox_replays_without_backfill(tmp_path):
    db = _db(tmp_path / "legacy.sqlite3")
    db.execute(f"DROP TABLE {MEMORY_EVENT_DELIVERY_HEADS}")
    db.execute(f"DROP TABLE {MEMORY_EVENT_OUTBOX}")
    _insert_published(db, operation_id="old-job")
    db.commit()
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "exam_1.pdf").write_bytes(b"%PDF-1.7\n")
    config = {
        "DATABASE": str(tmp_path / "legacy.sqlite3"),
        "PDF_DIR": str(pdf_dir),
        "AI_MEMORY_DIR": str(tmp_path / "memory"),
    }

    recovered = recover_published_exam_job(db, config, "old-job", logger=None)

    assert recovered.body["idempotency_replayed"] is True
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (MEMORY_EVENT_OUTBOX,)
    ).fetchone() is None
    assert not (tmp_path / "memory").exists()


def test_recovery_write_failure_is_explicit_and_keeps_published_exam(tmp_path):
    db = _db(tmp_path / "db.sqlite3")
    _insert_published(db)
    _stage(db)
    db.commit()
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "exam_1.pdf").write_bytes(b"%PDF-1.7\n")

    class BrokenMemory:
        def append_events(self, *_args, **_kwargs):
            raise LookupError("simulated memory write failure")

    result = recover_published_exam_job(
        db,
        {
            "DATABASE": str(tmp_path / "db.sqlite3"),
            "PDF_DIR": str(pdf_dir),
            "AI_MEMORY_DIR": str(tmp_path / "memory"),
        },
        "job-1",
        logger=None,
        memory=BrokenMemory(),
    )

    assert result.status == 503
    assert result.body["code"] == "memory_delivery_pending"
    assert result.body["memory_delivery"] == "pending"
    assert db.execute("SELECT artifact_status FROM exams").fetchone()[0] == "published"
    assert recover_generation_memory_delivery(db, operation_id="job-1").state == "pending"
