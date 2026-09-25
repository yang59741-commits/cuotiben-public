from unittest.mock import patch

from cuoti_app.services.exam_generation_contracts import ExamGenerationResult
from cuoti_app.services.exam_generation_memory_delivery import dispatch_generation_memory_delivery
from cuoti_app.services.job_serialization import publication_head_state
from cuoti_app.services.ai_memory import AIMemory
from tests.test_exam_memory_delivery import _db, _stage
from tests import test_job_integration as job_fixtures


def test_memory_delivery_is_committed_but_not_completed_until_delivered(tmp_path):
    db = _db()
    _stage(db)
    db.commit()
    assert publication_head_state(db, "job-1") == "published_pending_receipt"
    db.execute("CREATE TABLE exam_generation_d2_quality_publications(job_id TEXT,state TEXT)")
    db.execute("INSERT INTO exam_generation_d2_quality_publications VALUES('job-1','completed')")
    db.commit()
    assert publication_head_state(db, "job-1") == "published_pending_receipt"
    dispatch_generation_memory_delivery(
        db, operation_id="job-1", memory=AIMemory(tmp_path / "memory")
    )
    assert publication_head_state(db, "job-1") == "completed"
    db.execute("DELETE FROM exam_generation_d2_quality_publications")
    db.commit()
    assert publication_head_state(db, "job-1") == "completed"
    db.close()


def test_memory_pending_response_defers_same_job_without_provider_retry():
    fixture = job_fixtures.JobBusinessIntegrationTests(methodName="runTest")
    fixture.setUp()
    try:
        coordinator = fixture.coordinator()
        job, _ = coordinator.submit("exam-generation", {
            "source": "free", "subject_id": 1, "chapters": [1],
            "difficulty": "中等", "question_count": 1, "types": ["计算题"], "note": "",
        })
        with patch("cuoti_app.services.job_handlers_generation.generate_exam", return_value=ExamGenerationResult(
            {"error": "memory paused", "code": "memory_delivery_pending", "reason": "memory_paused"}, 503
        )) as generate:
            outcome = coordinator.run_once()
        assert outcome["id"] == job["id"]
        assert outcome["status"] == "queued"
        assert outcome["error"]["code"] == "memory_delivery_pending"
        assert generate.call_count == 1
        assert coordinator.run_once() is None
    finally:
        fixture.tearDown()
