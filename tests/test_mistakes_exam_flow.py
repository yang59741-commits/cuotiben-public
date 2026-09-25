import json
import uuid
from dataclasses import replace
from unittest.mock import Mock

import pytest

from tests.test_free_exam_flow import free_case, make_runtime
from cuoti_app.database import get_db
from cuoti_app.services.exam_generation import generate_exam
from cuoti_app.services.exams import mistakes
from cuoti_app.services import exam_generation_publish as publication
from cuoti_app.services.jobs import JobCoordinator
from cuoti_app.services.exam_publish import ExamPublicationFence
from cuoti_app.services.job_contracts import JobCancelled
from cuoti_app.services.exam_generation_memory_delivery import dispatch_generation_memory_delivery
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.migrations.v54_exam_generation_memory_delivery import MEMORY_EVENT_DELIVERY_HEADS


@pytest.fixture
def mistake_request(free_case, monkeypatch):
    public_id = str(uuid.uuid4())
    cursor = get_db().execute(
        "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added,updated_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (public_id, free_case.subject_id, free_case.chapter_id, "1", "计算一个极限。",
         "2026-09-24", "2026-09-24T10:00:00+00:00"),
    )
    get_db().commit()
    monkeypatch.setattr(mistakes, "audit_question_difficulties", Mock(return_value=["中等"]))
    return {
        "source": "mistakes", "mistake_ids": [cursor.lastrowid],
        "difficulty": "中等", "question_count": 1, "types": ["计算题"],
        "target": "日常练习", "training_target": "transfer", "note": "",
    }, public_id


@pytest.mark.parametrize("raw_failures,compile_failure,status,raw_count,exam_count", [
    (0, False, 200, 1, 1), (1, False, 200, 2, 1),
    (2, False, 422, 2, 0), (0, True, 500, 1, 0),
])
def test_mistake_lineage_retry_and_compensation(
    free_case, mistake_request, raw_failures, compile_failure, status, raw_count, exam_count,
):
    payload, public_id = mistake_request
    runtime, calls = make_runtime(free_case, raw_failures=raw_failures, compile_failure=compile_failure)
    response = generate_exam(payload, runtime)
    assert response.status == status, response.body
    assert calls.count("raw") == raw_count
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == exam_count
    if exam_count:
        row = get_db().execute("SELECT id,artifact_status,config FROM exams").fetchone()
        assert row["artifact_status"] == "published"
        config = json.loads(row["config"])
        assert config["training_target"] == "transfer"
        assert config["draft_attempt"] == raw_count
        assert get_db().execute(
            "SELECT mistake_public_id FROM exam_item_mistake_lineage"
        ).fetchone()[0] == public_id
        assert response.body["memory_delivery"] == "delivered"
    else:
        assert not list(free_case.pdf_dir.glob("exam_*.pdf"))


def test_wrong_mistake_identity_never_reaches_compile(free_case, mistake_request):
    runtime, calls = make_runtime(free_case)
    indexed = runtime.generate_indexed_free_exam

    def wrong_identity(*args):
        body, items, labels = indexed(*args)
        items[0]["reference_ids"] = [str(uuid.uuid4())]
        return body, items, labels

    result = generate_exam(mistake_request[0], replace(runtime, generate_indexed_free_exam=wrong_identity))
    assert result.status == 500, result.body
    assert calls == ["raw"]
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0


def test_same_mistake_job_recovers_without_republishing(free_case, mistake_request, monkeypatch):
    payload, _ = mistake_request
    runtime, calls = make_runtime(free_case)
    coordinator = JobCoordinator(free_case.database)
    job, _ = coordinator.submit("exam-generation", payload)
    claimed = coordinator.claim_next()
    runtime = replace(runtime, publication_fence=ExamPublicationFence(
        job_id=job["id"], worker_id=coordinator.worker_id, lease_attempt=claimed["attempts"],
    ))
    payload = {**payload, "_job_id": job["id"]}
    with monkeypatch.context() as patch:
        patch.setattr(publication, "dispatch_generation_memory_delivery", Mock(side_effect=OSError("fixture")))
        result = generate_exam(payload, runtime)
    assert result.status == 503
    assert result.body["code"] == "memory_delivery_pending"
    row = get_db().execute("SELECT id,artifact_status FROM exams").fetchone()
    assert row["artifact_status"] == "published"
    assert (free_case.pdf_dir / f"exam_{row['id']}.pdf").is_file()
    head = get_db().execute(f"SELECT operation_id,state FROM {MEMORY_EVENT_DELIVERY_HEADS}").fetchone()
    assert head["state"] == "pending"
    recovered = generate_exam(payload, runtime)
    assert recovered.status == 200, recovered.body
    assert recovered.body["id"] == row["id"]
    assert recovered.body["idempotency_replayed"] is True
    again = dispatch_generation_memory_delivery(
        get_db(), operation_id=head["operation_id"], memory=AIMemory(free_case.root / "memory"),
    )
    assert again["state"] == "delivered"
    assert calls == ["raw", "compile"]
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
    events = [json.loads(line) for file in (free_case.root / "memory/mistakes/events/daily").glob("*.jsonl")
              for line in file.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["request", "result"]


@pytest.mark.parametrize("after_publication", [False, True])
def test_cancellation_keeps_original_compensation_boundary(
    free_case, mistake_request, monkeypatch, after_publication,
):
    runtime, _calls = make_runtime(free_case)
    stop = Mock(side_effect=JobCancelled("fixture cancelled"))
    if after_publication:
        monkeypatch.setattr(publication, "confirm_generation_receipt", stop)
    else:
        runtime = replace(runtime, compile_exam=stop)
    with pytest.raises(JobCancelled):
        generate_exam(mistake_request[0], runtime)
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == int(after_publication)
    assert bool(list(free_case.pdf_dir.glob("exam_*.pdf"))) == after_publication
