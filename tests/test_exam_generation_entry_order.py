from dataclasses import MISSING, fields
from unittest.mock import Mock

import pytest

from cuoti_app.services.exam_generation import ExamGenerationRuntime, generate_exam
from cuoti_app.services import exam_generation_preflight as preflight
from cuoti_app.services.exam_generation_recovery import ExamRecoveryResponse
from cuoti_app.services.exams import generation
from cuoti_app.services.exams import free, mistakes


def runtime():
    required = {
        field.name: Mock(name=field.name)
        for field in fields(ExamGenerationRuntime)
        if field.default is MISSING and field.default_factory is MISSING
    }
    required.update(config={}, request_id="a" * 32)
    return ExamGenerationRuntime(**required)


def payload(source):
    return {
        "_job_id": "a" * 32, "source": source, "subject_id": 1,
        "chapters": [1], "mistake_ids": [1], "question_count": 1,
        "types": ["计算题"], "difficulty": "中等",
    }


@pytest.mark.parametrize("changes,status", [
    ({"paper_count": 2}, 400),
    ({"difficulty": "invalid"}, 400),
    ({"chapters": [1, 2], "question_count": 1}, 422),
])
def test_invalid_request_is_rejected_before_published_recovery(monkeypatch, changes, status):
    context = runtime()
    recovery = Mock(side_effect=AssertionError("recovery must not run"))
    monkeypatch.setattr(preflight, "recover_published_exam_job", recovery)
    result = generate_exam({**payload("free"), **changes}, context)
    assert result.status == status
    recovery.assert_not_called()
    context.require_deepseek_config.assert_not_called()
    context.ai_call.assert_not_called()
    context.compile_exam.assert_not_called()
    context.progress.assert_not_called()


@pytest.mark.parametrize("source", ["free", "mistakes"])
@pytest.mark.parametrize("status,body", [
    (200, {"id": 7, "idempotency_replayed": True}),
    (503, {"code": "memory_delivery_pending", "retryable": True}),
])
def test_published_recovery_returns_before_freeze_or_source_work(monkeypatch, source, status, body):
    context = runtime()
    recovery = Mock(return_value=ExamRecoveryResponse(body, status))
    freeze = Mock(side_effect=AssertionError("must not freeze a new request"))
    source_work = Mock(side_effect=AssertionError("must not prepare sources"))
    monkeypatch.setattr(preflight, "recover_published_exam_job", recovery)
    monkeypatch.setattr(preflight, "freeze_generation_request", freeze)
    monkeypatch.setattr(free if source == "free" else mistakes, f"prepare_{source}_source_context", source_work)
    result = generate_exam(payload(source), context)
    assert (result.body, result.status) == (body, status)
    recovery.assert_called_once()
    freeze.assert_not_called()
    source_work.assert_not_called()
    context.require_deepseek_config.assert_not_called()
    context.ai_call.assert_not_called()
    context.compile_exam.assert_not_called()
    context.progress.assert_not_called()


@pytest.mark.parametrize("source", ["free", "mistakes"])
def test_unpublished_request_freezes_then_checks_configuration_before_source(monkeypatch, source):
    context = runtime()
    events = []
    original_freeze = preflight.freeze_generation_request

    def recover(*args, **kwargs):
        events.append("recover")
        return None

    def freeze(*args, **kwargs):
        events.append("freeze")
        return original_freeze(*args, **kwargs)

    def check_config():
        events.append("config")
        return {"error": "fixture configuration unavailable"}, 503

    context.require_deepseek_config.side_effect = check_config
    monkeypatch.setattr(preflight, "recover_published_exam_job", recover)
    monkeypatch.setattr(preflight, "freeze_generation_request", freeze)
    source_work = Mock(side_effect=AssertionError("source work before valid config"))
    monkeypatch.setattr(free if source == "free" else mistakes, f"prepare_{source}_source_context", source_work)
    result = generate_exam(payload(source), context)
    assert result.status == 503
    assert events == ["recover", "freeze", "config"]
    source_work.assert_not_called()
    context.progress.assert_not_called()
