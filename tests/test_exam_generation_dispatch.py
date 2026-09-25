from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cuoti_app.services.exam_generation_contracts import ExamGenerationResult
from tests import test_job_integration as job_fixtures


@pytest.mark.parametrize("source", ["free", "mistakes", "past"])
def test_http_job_dispatches_to_current_generation_entry(source):
    fixture = job_fixtures.JobBusinessIntegrationTests(methodName="runTest")
    fixture.setUp()
    try:
        payload = {
            "source": source, "subject_id": 1, "chapters": [1],
            "difficulty": "中等", "question_count": 1, "types": ["计算题"],
        }
        if source == "mistakes":
            payload["mistake_ids"] = [1]
        # This test isolates dispatch; profile contents and generated papers
        # are covered by the existing native-pipeline and service fixtures.
        with patch(
            "cuoti_app.services.jobs.freeze_exam_generation_profile_in_transaction",
            side_effect=lambda _db, value: dict(value),
        ):
            response = fixture.client.post("/api/v2/jobs/exam-generation", json=payload)
        assert response.status_code == 202, response.get_json()
        result = ExamGenerationResult({"exams": []}, 200)
        with (
            patch("cuoti_app.services.job_handlers_generation.generate_exam", return_value=result) as service,
            patch(
                "cuoti_app.services.job_handlers_generation.prepare_generation_preflight",
                return_value=SimpleNamespace(completed=False, request=object()),
            ),
            patch("cuoti_app.services.job_handlers_generation.run_injected_past_v3_pipeline") as native,
            patch("cuoti_app.services.job_handlers_generation.pipeline_result_to_generation_result", return_value=result),
        ):
            outcome = fixture.coordinator().run_once()
        assert outcome["status"] == "succeeded", outcome
        if source == "past":
            native.assert_called_once()
            service.assert_not_called()
        else:
            native.assert_not_called()
            service.assert_called_once()
            actual_payload, runtime = service.call_args.args
            assert actual_payload["source"] == source
            assert actual_payload["_job_id"] == outcome["id"]
            assert runtime.request_id == outcome["id"]
    finally:
        fixture.tearDown()
