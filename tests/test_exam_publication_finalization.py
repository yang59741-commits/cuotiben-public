from unittest.mock import Mock

import pytest

from cuoti_app.services import exam_generation_publish as publication
from cuoti_app.services.exam_publish import PublishedExamBatch, PublishedExamResult
from cuoti_app.services.job_contracts import JobCancelled, WorkerStopRequested


@pytest.fixture
def completion(monkeypatch):
    receipt = Mock()
    # Keep the real receipt error mapping, replacing only its external write.
    monkeypatch.setattr(publication, "confirm_exam_publication_receipt", receipt)
    dispatch = Mock(return_value={"state": "delivered"})
    monkeypatch.setattr(publication, "dispatch_generation_memory_delivery", dispatch)
    published = PublishedExamBatch((PublishedExamResult(
        id=1, title="试卷", content="题目", pdf_url="/pdfs/exam_1.pdf",
        created_at="2026-09-23 12:00",
    ),))
    args = dict(
        published=published, generation_job_id="job-1", db=object(),
        database_path="fixture.db", source="free", operation_id="job-1",
        response={"id": 1, "count": 1, "warnings": [{"code": "existing"}]},
        receipt_writer=Mock(), bank_index=Mock(),
        candidate_questions=({"candidate_id": "ordinary-1"},),
        candidate_pool=(),
        past_selections=(({"candidate_id": "ordinary-1"},),),
        generated_question_items=({"reference_ids": ["ordinary-1"]},),
        memory=Mock(), memory_type="free", memory_actor="free_exam",
        memory_events=({"event_type": "request"}, {"event_type": "result"}),
        logger=Mock(),
    )
    return args, receipt, dispatch


@pytest.mark.parametrize("source", ["free", "mistakes", "past"])
def test_success_preserves_response_and_receipt_usage_memory_order(completion, source):
    args, receipt, dispatch = completion
    args.update(source=source, memory_type=source, memory_actor=f"{source}_exam")
    order = []
    receipt.side_effect = lambda *a, **kw: order.append("receipt")
    args["bank_index"].record_usage.side_effect = lambda *a: order.append("usage")
    dispatch.side_effect = lambda *a, **kw: (order.append("memory") or {"state": "delivered"})
    args["memory"].append_events.side_effect = lambda *a: order.append("memory")

    result = publication.finalize_generation_publication(**args)

    assert result.error_body is None
    expected = {"id": 1, "count": 1, "warnings": [{"code": "existing"}]}
    if source != "past":
        expected["memory_delivery"] = "delivered"
        args["memory"].append_events.assert_not_called()
        dispatch.assert_called_once_with(
            args["db"], operation_id="job-1", memory=args["memory"], logger=args["logger"],
        )
    else:
        dispatch.assert_not_called()
        args["memory"].append_events.assert_called_once_with(
            "past", "past_exam", args["memory_events"],
        )
    assert result.results == expected
    assert order == (["receipt", "memory"] if source == "mistakes" else ["receipt", "usage", "memory"])
    if source != "mistakes":
        args["bank_index"].record_usage.assert_called_once_with(["ordinary-1"], ["ordinary-1"])


def test_receipt_failure_defers_usage_and_memory(completion):
    args, receipt, dispatch = completion
    receipt.side_effect = OSError("receipt unavailable")

    result = publication.finalize_generation_publication(**args)

    assert result.error_status == 503
    assert result.error_body["code"] == "receipt_recovery_pending"
    args["bank_index"].record_usage.assert_not_called()
    dispatch.assert_not_called()
    args["memory"].append_events.assert_not_called()


@pytest.mark.parametrize("state", ["paused", "exception", "job_error"])
def test_memory_delivery_remains_retryable_after_publication(completion, state):
    args, _, dispatch = completion
    if state == "exception":
        dispatch.side_effect = OSError("memory unavailable")
    elif state == "job_error":
        dispatch.side_effect = JobCancelled("cancelled during delivery")
    else:
        dispatch.return_value = {"state": "pending"}

    result = publication.finalize_generation_publication(**args)

    assert result.error_status == 503
    assert result.error_body == {
        "error": "试卷已发布，生成记忆仍待交付，请稍后重试",
        "code": "memory_delivery_pending", "memory_delivery": "pending", "retryable": True,
    }
    assert "memory_delivery" not in args["response"]


@pytest.mark.parametrize("stage", ["receipt", "usage", "past_memory"])
def test_cancellation_is_preserved_at_existing_control_boundaries(completion, stage):
    args, receipt, dispatch = completion
    if stage == "receipt":
        receipt.side_effect = JobCancelled("cancelled")
    elif stage == "usage":
        args["bank_index"].record_usage.side_effect = JobCancelled("cancelled")
    else:
        args["source"] = "past"
        args["memory"].append_events.side_effect = JobCancelled("cancelled")

    with pytest.raises(JobCancelled):
        publication.finalize_generation_publication(**args)
    dispatch.assert_not_called()


@pytest.mark.parametrize("stage", ["usage", "past_memory"])
def test_optional_audit_failure_does_not_fail_publication(completion, stage):
    args, _, _ = completion
    if stage == "usage":
        args["bank_index"].record_usage.side_effect = OSError("audit unavailable")
    else:
        args["source"] = "past"
        args["memory"].append_events.side_effect = OSError("memory unavailable")
    args["logger"].exception.side_effect = OSError("logger unavailable")

    result = publication.finalize_generation_publication(**args)

    assert result.error_body is None
    assert result.results["id"] == 1


def test_worker_stop_is_not_converted_to_memory_retry(completion):
    args, _, dispatch = completion
    dispatch.side_effect = WorkerStopRequested()
    with pytest.raises(WorkerStopRequested):
        publication.finalize_generation_publication(**args)
