from types import SimpleNamespace
from unittest.mock import patch

from cuoti_app.services.exam_generation_contracts import ExamGenerationResult
from cuoti_app.services.exam_generation_d2_publication import D2PublicationError
from tests import test_job_integration as job_fixtures


def test_environment_failure_can_retry_same_job_without_losing_failed_attempt():
    fixture = job_fixtures.JobBusinessIntegrationTests(methodName="runTest")
    fixture.setUp()
    try:
        coordinator = fixture.coordinator()
        with patch(
            "cuoti_app.services.jobs.freeze_exam_generation_profile_in_transaction",
            side_effect=lambda _db, payload: dict(payload),
        ):
            job, _ = coordinator.submit(
                "exam-generation",
                {"source": "past", "subject_id": 1, "question_count": 1},
            )
        preflight = SimpleNamespace(completed=False, request=object())
        pipeline_calls = []

        def run_pipeline(*_args, **_kwargs):
            pipeline_calls.append(job["id"])
            if len(pipeline_calls) == 1:
                raise D2PublicationError(
                    "quality_environment_requires_repair",
                    "模板或编译环境需要修复，未将题目判错",
                )
            return object()

        with (
            patch(
                "cuoti_app.services.job_handlers_generation.prepare_generation_preflight",
                return_value=preflight,
            ),
            patch(
                "cuoti_app.services.job_handlers_generation.run_injected_past_v3_pipeline",
                side_effect=run_pipeline,
            ),
            patch(
                "cuoti_app.services.job_handlers_generation.pipeline_result_to_generation_result",
                return_value=ExamGenerationResult({"exams": []}, 200),
            ),
        ):
            failed = coordinator.run_once()
            assert failed["id"] == job["id"]
            assert failed["status"] == "failed"
            assert failed["error"]["code"] == "quality_environment_requires_repair"
            assert failed["error"]["category"] == "dependency"
            assert failed["error"]["retryable"] is False
            assert failed["error"]["actions"] == [
                "repair_template_or_environment",
                "retry_the_same_job",
            ]

            retried = coordinator.retry(job["id"])
            assert retried["id"] == job["id"]
            assert retried["status"] == "queued"
            assert retried["manual_retry_count"] == 1

            succeeded = coordinator.run_once()
            assert succeeded["id"] == job["id"]
            assert succeeded["status"] == "succeeded"
            assert pipeline_calls == [job["id"], job["id"]]

        attempts = coordinator.list_attempts(job["id"])
        assert [attempt["status"] for attempt in attempts] == ["failed", "succeeded"]
        assert attempts[0]["error_code"] == "quality_environment_requires_repair"
    finally:
        fixture.tearDown()
