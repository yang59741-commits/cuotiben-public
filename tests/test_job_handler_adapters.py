from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cuoti_app.services.job_handlers_ai import make_job_ai_call
from cuoti_app.services import job_handlers_generation as generation
from cuoti_app.services.job_contracts import JobCancelled, JobLeaseLost
from cuoti_app.services.exam_generation_contracts import ExamGenerationResult
from cuoti_app.services.jobs import DeferredJobError


def context():
    return SimpleNamespace(
        job_id="fixture-job", lease_attempt=3,
        coordinator=SimpleNamespace(worker_id="fixture-worker"),
        assert_lease=Mock(), raise_if_cancelled=Mock(return_value=None),
        is_cancel_requested=Mock(return_value=False),
        frozen_time_policy=Mock(return_value={"final_reserve_ms": 1500}),
        bounded_timeout=Mock(return_value=12), add_exam_metric=Mock(),
        update_progress=Mock(),
    )


def test_generation_ai_call_preserves_budget_cancellation_and_usage_observer():
    ctx = context()
    provider = Mock(return_value="fixture answer")
    upstream = Mock()
    call_ai = make_job_ai_call(ctx, provider, exam_attempt=True)
    assert call_ai([], usage_observer=upstream, task_policy="quality") == "fixture answer"
    kwargs = provider.call_args.kwargs
    assert kwargs["freeze_id"] == ctx.job_id
    assert kwargs["attempt_guard"] is ctx.raise_if_cancelled
    assert kwargs["cancellation"].is_cancelled() is False
    ctx.raise_if_cancelled.assert_called_once_with()
    ctx.bounded_timeout.assert_called_once_with(300, stage="quality", reserve_seconds=1.5)
    assert kwargs["timeout"] == 12
    fields = {"provider_calls": 2, "attempts": 3, "prompt_tokens": 10,
              "completion_tokens": 4, "total_tokens": 14, "usage_missing": True}
    kwargs["usage_observer"](fields)
    upstream.assert_called_once_with(fields)
    metrics = {call.args[0]: call.args[1] for call in ctx.add_exam_metric.call_args_list}
    assert metrics == {
        "model_calls": 1, "provider_calls": 2, "model_retry_count": 2,
        "prompt_tokens": 10, "completion_tokens": 4, "reasoning_tokens": 0,
        "cached_tokens": 0, "total_tokens": 14, "usage_missing_calls": 1,
    }


def test_non_generation_ai_keeps_its_original_timeout_and_polling_contract():
    ctx = context()
    ctx.is_cancel_requested.return_value = True
    provider = Mock()
    call_ai = make_job_ai_call(ctx, provider, exam_attempt=False)
    call_ai([], freeze_id="caller-freeze", timeout=90)
    kwargs = provider.call_args.kwargs
    assert kwargs["freeze_id"] == "caller-freeze"
    assert kwargs["timeout"] == 90
    assert "attempt_guard" not in kwargs
    assert kwargs["cancellation"].is_cancelled() is True
    ctx.raise_if_cancelled.assert_not_called()
    ctx.bounded_timeout.assert_not_called()
    ctx.add_exam_metric.assert_not_called()


@pytest.mark.parametrize("key,dependency", [
    ("past_attempt_resolver", "resolve_persisted_past_profile_for_attempt"),
    ("past_attempt_binder", "persist_profile_resolved_binding"),
    ("past_stage3_contract_binder", "persist_stage3_generation_contract"),
    ("past_stage4_fact_binder", "freeze_and_persist_stage4_candidate_facts"),
])
@pytest.mark.parametrize("interrupt", [JobLeaseLost, JobCancelled])
def test_native_bindings_stop_before_side_effects(monkeypatch, key, dependency, interrupt):
    ctx = context()
    app = SimpleNamespace(config={}, logger=Mock())
    preflight = Mock()
    effect = Mock()
    monkeypatch.setattr(generation, "get_db", lambda: "fixture-db")
    monkeypatch.setattr(generation, "prepare_generation_preflight", preflight)
    monkeypatch.setattr(generation, "make_published_recovery_dispatcher", Mock())
    monkeypatch.setattr(generation, dependency, effect)
    generation.prepare_native_past_attempt(app, ctx, {"source": "past"}, Mock())
    callback = preflight.call_args.kwargs[key]
    if interrupt is JobLeaseLost:
        ctx.assert_lease.side_effect = interrupt("fixture lease lost")
    else:
        ctx.raise_if_cancelled.side_effect = interrupt("fixture cancelled")
    args = ("db", {}) if key == "past_attempt_resolver" else ("db", ctx.job_id, object())
    with pytest.raises(interrupt):
        callback(*args)
    effect.assert_not_called()


def test_native_source_bundle_load_has_its_own_cancellation_boundary(monkeypatch):
    ctx = context()
    app = SimpleNamespace(config={}, logger=Mock())
    preflight = Mock()
    bundle = Mock()
    monkeypatch.setattr(generation, "get_db", lambda: "db")
    monkeypatch.setattr(generation, "prepare_generation_preflight", preflight)
    monkeypatch.setattr(generation, "make_published_recovery_dispatcher", Mock())
    monkeypatch.setattr(generation, "load_app_semantic_source_bundle", bundle)
    monkeypatch.setattr(generation, "freeze_and_persist_stage4_candidate_facts",
                        lambda *args, **kwargs: kwargs["source_bundle_loader"]())
    generation.prepare_native_past_attempt(app, ctx, {"source": "past"}, Mock())

    def cancel(*, stage):
        if stage == "stage4_source_bundle_load":
            raise JobCancelled("fixture cancelled")

    ctx.raise_if_cancelled.side_effect = cancel
    with pytest.raises(JobCancelled):
        preflight.call_args.kwargs["past_stage4_fact_binder"]("db", ctx.job_id, object())
    bundle.assert_not_called()


def test_native_bindings_forward_the_same_frozen_actor_and_request(monkeypatch):
    ctx = context()
    app = SimpleNamespace(config={"SEMANTIC_RECALL_ALGORITHM_VERSION": "fixture-v2"}, logger=Mock())
    payload, profile = {"source": "past"}, object()
    preflight, recovery, bundle = Mock(), Mock(), Mock(return_value="fixture-bundle")
    monkeypatch.setattr(generation, "get_db", lambda: "fixture-db")
    monkeypatch.setattr(generation, "prepare_generation_preflight", preflight)
    monkeypatch.setattr(generation, "make_published_recovery_dispatcher", recovery)
    monkeypatch.setattr(generation, "load_app_semantic_source_bundle", bundle)
    effects = {}
    for key, symbol in (
        ("past_attempt_binder", "persist_profile_resolved_binding"),
        ("past_stage3_contract_binder", "persist_stage3_generation_contract"),
        ("past_stage4_fact_binder", "freeze_and_persist_stage4_candidate_facts"),
    ):
        effects[key] = Mock()
        monkeypatch.setattr(generation, symbol, effects[key])
    ai = Mock()
    generation.prepare_native_past_attempt(app, ctx, payload, ai)
    options = preflight.call_args.kwargs
    assert preflight.call_args.args == (payload,)
    assert options["db"] == "fixture-db"
    assert options["published_recovery_dispatcher"] is recovery.return_value
    assert recovery.call_args.kwargs["request"] is payload
    for key, effect in effects.items():
        options[key]("binding-db", "bound-job", profile)
        assert effect.call_args.args == ("binding-db",)
        assert effect.call_args.kwargs["job_id"] == "bound-job"
        assert effect.call_args.kwargs["profile_context"] is profile
        assert effect.call_args.kwargs["worker_id"] == ctx.coordinator.worker_id
        assert effect.call_args.kwargs["lease_attempt"] == ctx.lease_attempt
    facts = effects["past_stage4_fact_binder"].call_args.kwargs
    assert facts["payload"] is payload
    assert facts["projection_algorithm_version"] == "fixture-v2"
    assert facts["source_bundle_loader"]() == "fixture-bundle"
    bundle.assert_called_once_with(app)


def test_recovered_native_publication_does_not_run_pipeline(monkeypatch):
    preflight = SimpleNamespace(completed=True, response_body={"id": 27}, response_status=200)
    monkeypatch.setattr(generation, "prepare_native_past_attempt", Mock(return_value=preflight))
    pipeline = Mock()
    monkeypatch.setattr(generation, "run_injected_past_v3_pipeline", pipeline)
    result = generation.run_native_past_generation(object(), context(), {"source": "past"}, Mock())
    assert (result.body, result.status) == ({"id": 27}, 200)
    pipeline.assert_not_called()


@pytest.mark.parametrize("body,status,native,code", [
    ({"code": "memory_delivery_pending"}, 503, False, "memory_delivery_pending"),
    ({"code": "receipt_pending"}, 202, True, "receipt_pending"),
])
def test_committed_pending_results_defer_the_same_job(body, status, native, code):
    with pytest.raises(DeferredJobError) as caught:
        generation.generation_outcome(
            object(), context(), ExamGenerationResult(body, status),
            strict_past_v3=native, job_error_from_response=Mock(),
        )
    assert caught.value.code == code


@pytest.mark.parametrize("native,delivered,compensates", [
    (True, False, False), (False, True, False), (False, False, True),
])
def test_deadline_compensation_respects_committed_delivery(native, delivered, compensates):
    body = {"id": 27, **({"memory_delivery": "delivered"} if delivered else {})}
    result = generation.generation_outcome(
        object(), context(), ExamGenerationResult(body),
        strict_past_v3=native, job_error_from_response=Mock(),
    )
    assert result.exam_ids == (27,)
    assert result.side_effect_committed is True
    assert callable(result.deadline_compensation) == compensates
