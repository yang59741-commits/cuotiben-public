import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from cuoti_app.database import get_db
from cuoti_app.services.exam_generation import generate_exam
from cuoti_app.services.exam_runtime import build_exam_generation_runtime
from cuoti_app.services.exam_tex import RawDraftValidationError
from cuoti_app.services.tex import TexCompileError
from cuoti_app.services.exam_quality_review import ExamQualityError
from cuoti_app.services.exams import free
from cuoti_app.services import exam_generation_publish as publication
from cuoti_app.services.exam_generation_memory_delivery import dispatch_generation_memory_delivery
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.services.jobs import JobCoordinator
from cuoti_app.services.exam_publish import ExamPublicationFence
from tests import test_exam_runtime as runtime_fixtures


@pytest.fixture
def free_case():
    case = runtime_fixtures.ExamRouteReliabilityTestCase(methodName="runTest")
    case.setUp()
    try:
        with case.app.app_context():
            yield case
    finally:
        case.tearDown()


def make_runtime(case, *, raw_failures=0, compile_failure=False):
    calls = []

    def indexed(_system, _prompt, count, _quota, _subject, candidates, types, chapters):
        calls.append("raw")
        if calls.count("raw") <= raw_failures:
            raise RawDraftValidationError("fixture invalid draft")
        content = r"计算 $1+1$。"
        return r"\begin{problem}" + content + r"\end{problem}", [{
            "position": 1, "chapter": chapters[0], "type": types[0],
            "question_type": types[0], "difficulty": "中等", "latex": content,
            "content": content, "reference_ids": [candidates[0]],
        }], ["中等"]

    def compile_fixture(directory, command, exam_id, source, **kwargs):
        calls.append("compile")
        if compile_failure:
            raise TexCompileError("fixture compile failed")
        Path(directory).mkdir(exist_ok=True)
        (Path(directory) / f"exam_{exam_id}.pdf").write_bytes(b"%PDF-fixture")
        (Path(directory) / f"exam_{exam_id}.tex").write_text(source, encoding="utf-8")

    runtime = build_exam_generation_runtime(
        case.app, get_db(), ai_call=Mock(side_effect=AssertionError("unexpected provider")),
        compile_exam=compile_fixture, progress=lambda *args: None,
    )
    return replace(
        runtime, generate_indexed_free_exam=indexed,
        require_deepseek_config=lambda: None,
    ), calls


@pytest.mark.parametrize("raw_failures,compile_failure,status,raw_count,exam_count", [
    (0, False, 200, 1, 1),
    (1, False, 200, 2, 1),
    (2, False, 422, 2, 0),
    (0, True, 500, 1, 0),
])
def test_free_service_generation_retry_and_compensation(
    free_case, raw_failures, compile_failure, status, raw_count, exam_count,
):
    runtime, calls = make_runtime(free_case, raw_failures=raw_failures, compile_failure=compile_failure)
    response = generate_exam(free_case._free_payload(), runtime)
    assert response.status == status, response.body
    assert calls.count("raw") == raw_count
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == exam_count
    if exam_count:
        row = get_db().execute("SELECT * FROM exams").fetchone()
        assert row["artifact_status"] == "published"
        config = json.loads(row["config"])
        assert config["draft_attempt"] == raw_count
        assert response.body["memory_delivery"] == "delivered"
        assert (free_case.pdf_dir / f"exam_{row['id']}.pdf").is_file()
    else:
        assert not list(free_case.pdf_dir.glob("exam_*.pdf"))


def test_free_quality_rejection_does_not_compile(free_case, monkeypatch):
    runtime, calls = make_runtime(free_case)
    gate = Mock(side_effect=ExamQualityError("QUALITY_DRAFT_REJECTED", "fixture rejected"))
    monkeypatch.setattr(free, "enforce_exam_quality", gate)
    response = generate_exam(free_case._free_payload(), runtime)
    assert response.status == 422, response.body
    assert calls == ["raw", "raw"]
    assert gate.call_count == 2
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0


def test_free_tail_adaptation_failure_keeps_the_reviewed_paper(free_case):
    runtime, calls = make_runtime(free_case)
    ai = Mock(return_value="{}")
    runtime = replace(runtime, ai_call=ai)
    result = generate_exam({**free_case._free_payload(), "allow_adaptation": True}, runtime)
    assert result.status == 200, result.body
    assert result.body["adaptation"]["requested"] is True
    assert result.body["adaptation"]["completed"] is False
    assert any(item["code"] == "ADAPTATION_NOT_COMPLETED" for item in result.body["warnings"])
    assert calls == ["raw", "compile"]
    assert ai.call_count == 1


def test_free_memory_failure_preserves_paper_and_delivery_is_idempotent(free_case, monkeypatch):
    runtime, calls = make_runtime(free_case)
    monkeypatch.setattr(publication, "dispatch_generation_memory_delivery", Mock(side_effect=OSError("fixture memory unavailable")))
    coordinator = JobCoordinator(free_case.database)
    job, _ = coordinator.submit("exam-generation", free_case._free_payload())
    claimed = coordinator.claim_next()
    assert claimed["id"] == job["id"]
    runtime = replace(runtime, publication_fence=ExamPublicationFence(
        job_id=job["id"], worker_id=coordinator.worker_id, lease_attempt=claimed["attempts"],
    ))
    payload = {**free_case._free_payload(), "_job_id": job["id"]}
    result = generate_exam(payload, runtime)
    assert result.status == 503
    assert result.body["code"] == "memory_delivery_pending"
    row = get_db().execute("SELECT id,artifact_status FROM exams").fetchone()
    assert row["artifact_status"] == "published"
    assert (free_case.pdf_dir / f"exam_{row['id']}.pdf").is_file()
    from cuoti_app.migrations.v54_exam_generation_memory_delivery import MEMORY_EVENT_DELIVERY_HEADS
    head = get_db().execute(f"SELECT operation_id,state FROM {MEMORY_EVENT_DELIVERY_HEADS}").fetchone()
    assert head["state"] == "pending"
    memory = AIMemory(free_case.root / "memory")
    for _ in range(2):
        delivered = dispatch_generation_memory_delivery(get_db(), operation_id=head["operation_id"], memory=memory)
        assert delivered["state"] == "delivered"
    assert calls == ["raw", "compile"]
    assert get_db().execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
    events = [json.loads(line) for file in (free_case.root / "memory/free/events/daily").glob("*.jsonl")
              for line in file.read_text(encoding="utf-8").splitlines()]
    assert [event["event_type"] for event in events] == ["request", "result"]
    monkeypatch.undo()
    recovered = generate_exam(payload, runtime)
    assert recovered.status == 200, recovered.body
    assert recovered.body["id"] == row["id"]
    assert recovered.body["idempotency_replayed"] is True
    assert calls == ["raw", "compile"]
