"""Strict, dependency-injected coordinator for one native past-exam v3 paper.

This module intentionally knows no provider, legacy checkpoint, or publication
implementation.  It only orders already-closed stages and makes recovery
precedence explicit.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


Callback = Callable[..., Any]


_CONTROL_FLOW_ERROR_NAMES = frozenset({
    "CancelledError",
    "ExamDeadlineExceeded",
    "JobCancelled",
    "JobLeaseLost",
})

# Not accepted by request parsing or production authority loading.  This
# private protocol only keeps the retired legacy engine testable against
# disposable provider-free fixtures.
_LEGACY_SCORE_FIXTURE_PROTOCOL = "_provider_free_legacy_score_fixture_v2"

_CONSTRAINT_CALLBACK_NAMES = frozenset({
    "recovery",
    "stage7_head_load",
    "stage6_state_load",
    "stage5",
    "stage6",
    "stage7_difficulty_load",
    "stage7_global_evidence_load",
    "stage7",
    "assembly",
    "publication",
    "d2",
})


class _PipelineConstraintRejected(Exception):
    """Carry one explicit callback constraint to the public result boundary."""

    def __init__(
        self,
        callback_name: str,
        error: Exception,
        *,
        stages: dict[str, str],
        elapsed: dict[str, int],
        calls: dict[str, int],
        retries: dict[str, int],
        hashes: dict[str, str],
    ) -> None:
        super().__init__(callback_name)
        self.callback_name = callback_name
        self.error = error
        # These mappings intentionally remain live until the inner coordinator
        # unwinds, so the callback's elapsed measurement is retained.
        self.stages = stages
        self.elapsed = elapsed
        self.calls = calls
        self.retries = retries
        self.hashes = hashes


@dataclass(frozen=True, slots=True)
class PipelineDependencies:
    publication_recovery: Callback | None
    stage7_head_loader: Callback | None
    stage6_state_loader: Callback | None
    stage5_runner: Callback | None
    stage6_runner: Callback | None
    stage7_runner: Callback | None
    assembler: Callback | None
    native_publisher: Callback | None
    stage3_contract_loader: Callback | None
    policy_fact_loader: Callback | None
    score_receipt_loader: Callback | None
    classification_binding: Mapping[str, Any] | None
    verification_binding: Mapping[str, Any] | None
    classify_callback: Callback | None
    difficulty_verify_callback: Callback | None
    score_callback: Callback | None
    score_prompt_version: str | None
    score_model_id: str | None
    stage7_difficulty_loader: Callback | None
    stage7_global_evidence_loader: Callback | None
    quality_callback: Callback | None
    adaptation_callback: Callback | None
    verification_callback: Callback | None
    stage7_budget: Any
    score_provider_capability: Any = None
    score_transport_retry_limit: int = 2
    usage_loader: Callback | None = None
    clock: Callback = time.monotonic
    fault_hook: Callable[[str], None] | None = None
    lifecycle_guard: Callback | None = None
    # Explicit Stage-6 protocol seam.  The historical default is retained for
    # recovery of pre-rubric tasks only; a new request must opt into the
    # candidate-base protocol and cannot fall back implicitly.
    scoring_protocol: str = "legacy_score_read_only_v2"
    rubric_callback: Callback | None = None
    rubric_prompt_version: str | None = None
    rubric_model_id: str | None = None
    utility_policy: Mapping[str, Any] | None = None
    d2_runner: Callback | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    status: str
    code: str
    reason: str
    status_code: int
    exam: Mapping[str, Any] | None
    stages: Mapping[str, str]
    hashes: Mapping[str, str]
    stage_elapsed_ms: Mapping[str, int]
    call_counts: Mapping[str, int]
    retry_counts: Mapping[str, int]
    witness: Mapping[str, Any] | None = None
    actions: tuple[str, ...] = field(default_factory=tuple)
    warnings: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _invoke(callback: Callback, **kwargs: Any) -> Any:
    return callback(**kwargs)


def _finite_ms(start: float, end: float) -> int:
    elapsed = (end - start) * 1000
    if not math.isfinite(elapsed):
        return 0
    return max(0, min(int(round(elapsed)), 2_147_483_647))


def _safe(value: Any, depth: int = 0) -> Any:
    """Bound an infeasibility witness without reflecting opaque adapter data."""

    if depth > 4:
        return "truncated"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:240]
    if isinstance(value, Mapping):
        allowed = {
            "version", "kind", "reason", "reason_code", "constraint_hash", "graph_hash",
            "callback_name", "error_code", "error_reason",
            "witness_hash", "deficient_slot_ids", "slot_ids", "candidate_ids",
            "reachable_candidate_ids", "reachable_content_hashes", "resource_kind",
            "deficiency", "required", "available", "shortfall", "constraint_ids",
            "constraints", "counts", "actions", "slots", "candidates", "content_hashes",
            "source_targets", "source_capacity", "chapter_targets", "chapter_capacity",
            "coverage_minima", "coverage_capacity",
        }
        projected: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name not in allowed:
                continue
            projected[name] = _safe_counts(item, depth + 1) if name == "counts" else _safe(item, depth + 1)
        return projected
    if isinstance(value, (list, tuple)):
        return [_safe(item, depth + 1) for item in value[:50]]
    return str(value)[:240]


def _safe_counts(value: Any, depth: int) -> Any:
    """Keep only bounded numeric aggregate trees from a witness counts field."""

    if depth > 5:
        return {}
    if isinstance(value, Mapping):
        return {
            str(key)[:120]: _safe_counts(item, depth + 1)
            for key, item in list(value.items())[:50]
            if isinstance(item, Mapping) or (isinstance(item, (int, float)) and not isinstance(item, bool))
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return {}


def _hashes(*values: Any) -> dict[str, str]:
    names = (
        "constraint_hash", "graph_hash", "score_set_hash", "selection_hash",
        "context_hash", "result_hash", "checkpoint_hash", "pre_adaptation_hash",
        "final_exam_hash", "publication_hash", "receipt_hash",
    )
    result: dict[str, str] = {}
    for value in values:
        for name in names:
            item = _value(value, name)
            if isinstance(item, str) and item:
                result[name] = item
    return result


def _blocked(
    *, reason: str, stages: Mapping[str, str], elapsed: Mapping[str, int],
    calls: Mapping[str, int], retries: Mapping[str, int], hashes: Mapping[str, str],
) -> PipelineResult:
    return PipelineResult(
        "blocked", "exam_past_v3_dependency_unavailable", reason, 422, None,
        dict(stages), dict(hashes), dict(elapsed), dict(calls), dict(retries),
        actions=("configure_the_named_native_dependency", "retry_the_same_job"),
    )


def _infeasible(
    result: Any, *, stages: Mapping[str, str], elapsed: Mapping[str, int],
    calls: Mapping[str, int], retries: Mapping[str, int], hashes: Mapping[str, str],
) -> PipelineResult:
    witness = _safe(_value(result, "witness") or {})
    kind = str(_value(witness, "kind", _value(result, "reason", "")))
    if kind == "hall_min_cut" or "exact_slot" in kind:
        code = "exam_exact_slot_infeasible"
        reason = "exact_slot_infeasible"
    else:
        code = "exam_global_constraint_infeasible"
        reason = "global_constraint_infeasible"
    witness_actions = _value(witness, "actions", ())
    actions = tuple(str(item)[:120] for item in witness_actions) if isinstance(witness_actions, list) else ()
    if not actions:
        actions = ("inspect_the_constraint_witness", "adjust_input_constraints_and_submit_a_new_job")
    return PipelineResult(
        "infeasible", code, reason, 422,
        None, dict(stages), dict(hashes), dict(elapsed), dict(calls), dict(retries),
        witness=witness if isinstance(witness, Mapping) else {},
        actions=actions,
    )


def _is_control_flow_error(exc: BaseException) -> bool:
    return any(
        cls.__name__ in _CONTROL_FLOW_ERROR_NAMES
        for cls in type(exc).__mro__
    )


def _is_constraint_error(exc: BaseException) -> bool:
    return (
        isinstance(exc, Exception)
        and not _is_control_flow_error(exc)
        and getattr(exc, "status_code", None) == 422
    )


def _is_bounded_stage_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    return (
        isinstance(exc, Exception)
        and not _is_control_flow_error(exc)
        and type(status_code) is int
        and 400 <= status_code <= 599
    )


def _constraint_stage(callback_name: str) -> str:
    if callback_name == "d2":
        return "d2"
    if callback_name.startswith("stage5"):
        return "stage5"
    if callback_name.startswith("stage6"):
        return "stage6"
    if callback_name.startswith("stage7"):
        return "stage7"
    if callback_name in {"assembly", "publication", "recovery"}:
        return callback_name
    return "recovery"


def _constraint_rejected(rejected: _PipelineConstraintRejected) -> PipelineResult:
    stage = _constraint_stage(rejected.callback_name)
    rejected.stages[stage] = "constraint_rejected"
    raw_evidence = (
        getattr(rejected.error, "details", None)
        or getattr(rejected.error, "evidence", None)
        or getattr(rejected.error, "witness", None)
        or {}
    )
    safe_error = {
        "callback_name": rejected.callback_name,
        "error_code": str(getattr(rejected.error, "code", "constraint_rejected"))[:120],
        "error_reason": str(getattr(rejected.error, "reason", "constraint_rejected"))[:120],
    }
    if isinstance(raw_evidence, Mapping):
        safe_error.update(raw_evidence)
    evidence = _safe(safe_error)
    return PipelineResult(
        "constraint_rejected",
        "exam_past_v3_constraint_rejected",
        f"{stage}_constraint_rejected",
        422,
        None,
        dict(rejected.stages),
        dict(rejected.hashes),
        dict(rejected.elapsed),
        dict(rejected.calls),
        dict(rejected.retries),
        witness=evidence if isinstance(evidence, Mapping) and evidence else None,
        actions=(
            "inspect_the_constraint_evidence",
            "correct_the_rejected_constraint_and_submit_a_new_job",
        ),
    )


def _stage_failed(rejected: _PipelineConstraintRejected) -> PipelineResult:
    stage = _constraint_stage(rejected.callback_name)
    rejected.stages[stage] = "failed"
    error_code = str(getattr(rejected.error, "code", ""))[:120]
    error_reason = str(getattr(rejected.error, "reason", ""))[:120]
    raw_evidence = (
        getattr(rejected.error, "details", None)
        or getattr(rejected.error, "evidence", None)
        or {}
    )
    safe_error = {
        "callback_name": rejected.callback_name,
        "error_code": error_code or "stage_failed",
        "error_reason": error_reason or "stage_failed",
    }
    if isinstance(raw_evidence, Mapping):
        safe_error.update(raw_evidence)
    evidence = _safe(safe_error)
    scoring_schema_failure = (
        rejected.callback_name == "stage6"
        and error_code == "exam_stage6_scoring_failed"
    )
    return PipelineResult(
        "failed",
        "scoring_schema_invalid" if scoring_schema_failure else "exam_past_v3_stage_failed",
        error_reason or f"{stage}_failed",
        int(getattr(rejected.error, "status_code", 500)),
        None,
        dict(rejected.stages),
        dict(rejected.hashes),
        dict(rejected.elapsed),
        dict(rejected.calls),
        dict(rejected.retries),
        witness=evidence if isinstance(evidence, Mapping) and evidence else None,
        actions=(
            ("inspect_provider_response_contract", "submit_a_new_generation_job")
            if scoring_schema_failure
            else ("inspect_the_failed_stage", "retry_if_the_stage_contract_allows")
        ),
    )


def _published_exam(value: Any) -> Mapping[str, Any] | None:
    status = _value(value, "status")
    if status not in {"completed", "recovered"}:
        return None
    exam = _value(value, "exam")
    return exam if isinstance(exam, Mapping) else None


def _pending_receipt_exam(value: Any) -> Mapping[str, Any] | None:
    if _value(value, "status") != "published_pending_receipt":
        return None
    exam = _value(value, "exam")
    return exam if isinstance(exam, Mapping) else None


def _receipt_pending(
    exam: Mapping[str, Any], *, stages: Mapping[str, str], elapsed: Mapping[str, int],
    calls: Mapping[str, int], retries: Mapping[str, int], hashes: Mapping[str, str],
) -> PipelineResult:
    return PipelineResult(
        "published_pending_receipt", "receipt_pending", "publication_receipt_pending",
        202, dict(exam), dict(stages), dict(hashes), dict(elapsed), dict(calls),
        dict(retries),
        actions=("complete_publication_receipt", "retry_the_same_job"),
    )


def _is_infeasible_error(exc: BaseException) -> bool:
    return (
        getattr(exc, "status_code", None) == 422
        and isinstance(getattr(exc, "witness", None), Mapping)
        and "infeasible" in str(getattr(exc, "code", "")).lower()
    )


def _is_stage6_handoff(result: Any) -> bool:
    return (
        _value(result, "status") == "adapter_pending"
        and _value(result, "reason") in {"stage6_adapter_pending", "stage6_scoring_adapter_pending"}
        and bool(_value(result, "selection_hash") or _value(result, "graph_hash"))
    )


def _counts(value: Any) -> tuple[dict[str, int], dict[str, int]]:
    calls = _value(value, "call_counts", {})
    retries = _value(value, "retry_counts", {})
    def clean(source: Any) -> dict[str, int]:
        if not isinstance(source, Mapping):
            return {}
        return {
            str(key): int(item) for key, item in source.items()
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0
        }
    return clean(calls), clean(retries)


def _run_past_v3_pipeline(
    db: Any,
    *,
    job_id: str,
    worker_id: str,
    lease_attempt: int,
    dependencies: PipelineDependencies,
) -> PipelineResult:
    """Recover or advance one strict v3 paper, highest durable stage first."""

    stages = {name: "not_started" for name in ("recovery", "stage5", "stage6", "stage7", "assembly", "publication")}
    elapsed: dict[str, int] = {}
    calls: dict[str, int] = {}
    retries: dict[str, int] = {}
    hashes: dict[str, str] = {}
    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}

    def run(name: str, callback: Callback, **kwargs: Any) -> Any:
        started = dependencies.clock()
        calls[name] = calls.get(name, 0) + 1
        # Recovery is receipt-only and therefore already follows a committed
        # publication.  Usage is best-effort telemetry.  Every callback in an
        # unpublished business chain is fenced immediately before invocation.
        if dependencies.lifecycle_guard is not None and name not in {"recovery", "usage_load"}:
            _invoke(dependencies.lifecycle_guard, stage=name)
        if dependencies.fault_hook is not None:
            dependencies.fault_hook(f"before_{name}")
        try:
            try:
                value = _invoke(callback, **kwargs)
            except Exception as exc:
                if name not in _CONSTRAINT_CALLBACK_NAMES or not _is_bounded_stage_error(exc):
                    raise
                raise _PipelineConstraintRejected(
                    name,
                    exc,
                    stages=stages,
                    elapsed=elapsed,
                    calls=calls,
                    retries=retries,
                    hashes=hashes,
                ) from exc
        finally:
            elapsed[name] = _finite_ms(started, dependencies.clock())
        if dependencies.fault_hook is not None:
            dependencies.fault_hook(f"after_{name}")
        return value

    def guarded_callback(callback: Callback | None, stage: str) -> Callback | None:
        if callback is None or dependencies.lifecycle_guard is None:
            return callback

        def guarded(*args: Any, **kwargs: Any) -> Any:
            _invoke(dependencies.lifecycle_guard, stage=stage)
            return callback(*args, **kwargs)

        correction = getattr(callback, "correct_schema", None)
        if callable(correction):
            def correct_schema(*args: Any, **kwargs: Any) -> Any:
                _invoke(dependencies.lifecycle_guard, stage=stage)
                return correction(*args, **kwargs)

            setattr(guarded, "correct_schema", correct_schema)

        return guarded

    if dependencies.publication_recovery is None:
        return _blocked(reason="native_publication_recovery_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    recovered = run("recovery", dependencies.publication_recovery, db=db, **actor)
    stages["recovery"] = "completed"
    hashes.update(_hashes(recovered))
    recovery_pending_exam = _pending_receipt_exam(recovered)
    if recovery_pending_exam is not None:
        stages["publication"] = "published_pending_receipt"
        return _receipt_pending(
            recovery_pending_exam, stages=stages, elapsed=elapsed, calls=calls,
            retries=retries, hashes=hashes,
        )
    recovered_exam = _published_exam(recovered)
    if recovered_exam is not None:
        stages["publication"] = "recovered"
        return PipelineResult("completed", "exam_past_v3_completed", "publication_recovered", 200, dict(recovered_exam), stages, hashes, elapsed, calls, retries)

    if dependencies.scoring_protocol == "past-v3-stage6-candidate-base-d2-v1":
        if dependencies.d2_runner is None:
            return _blocked(reason="d2_runner_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
        return run("d2", dependencies.d2_runner, db=db, lifecycle_guard=dependencies.lifecycle_guard, **actor)

    # User-frozen 2026-08-30 rule: legacy score/penalty may only support
    # already-completed historical publication recovery.  Any unpublished
    # legacy task stops here, before Stage 5, Stage 6, or a Provider callback.
    if dependencies.scoring_protocol == "legacy_score_read_only_v2":
        stages["stage6"] = "blocked"
        return _blocked(
            reason="stage6_legacy_score_write_disabled",
            stages=stages,
            elapsed=elapsed,
            calls=calls,
            retries=retries,
            hashes=hashes,
        )

    if dependencies.stage7_head_loader is None:
        return _blocked(reason="stage7_head_loader_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    stage7_head = run("stage7_head_load", dependencies.stage7_head_loader, db=db, **actor)
    hashes.update(_hashes(stage7_head))

    stage6_result = None
    if stage7_head is None:
        if dependencies.stage6_state_loader is None:
            return _blocked(reason="stage6_state_loader_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
        stage6_state = run("stage6_state_load", dependencies.stage6_state_loader, db=db, **actor)
        hashes.update(_hashes(stage6_state))
        stage6_closed = _value(stage6_state, "status") in {"completed", "closed", "stage6_scored"} or bool(_value(stage6_state, "checkpoint_hash"))
        stage6_started = stage6_state is not None

        if not stage6_closed and not stage6_started:
            required_stage5 = (
                dependencies.stage5_runner, dependencies.stage3_contract_loader,
                dependencies.policy_fact_loader,
            )
            if any(value is None for value in required_stage5):
                return _blocked(reason="stage5_native_adapter_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            try:
                stage5 = run(
                    "stage5", dependencies.stage5_runner, db=db, **actor,
                    stage3_contract_loader=guarded_callback(dependencies.stage3_contract_loader, "stage3_contract_load"),
                    policy_fact_loader=guarded_callback(dependencies.policy_fact_loader, "stage5_policy_fact_load"),
                    score_receipt_loader=guarded_callback(dependencies.score_receipt_loader, "stage5_score_receipt_load"),
                    classification_binding=dependencies.classification_binding,
                    verification_binding=dependencies.verification_binding,
                    classify=guarded_callback(dependencies.classify_callback, "stage5_classify"),
                    verify=guarded_callback(dependencies.difficulty_verify_callback, "stage5_difficulty_verify"),
                )
            except _PipelineConstraintRejected as rejected:
                exc = rejected.error
                if not _is_infeasible_error(exc):
                    raise
                stages["stage5"] = "infeasible"
                return _infeasible(exc, stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            hashes.update(_hashes(stage5))
            if _value(stage5, "status") == "infeasible":
                stages["stage5"] = "infeasible"
                return _infeasible(stage5, stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            # A selection-bearing adapter_pending is the worker's private handoff
            # to Stage 6, not a public pipeline result.
            if _value(stage5, "status") == "adapter_pending" and not _is_stage6_handoff(stage5):
                stages["stage5"] = "blocked"
                return _blocked(reason="stage5_native_adapter_unavailable", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            if _value(stage5, "status") not in {"completed", "closed"} and not _is_stage6_handoff(stage5):
                stages["stage5"] = "blocked"
                return _blocked(reason="stage5_not_closed", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            stages["stage5"] = "completed"

        if not stage6_closed:
            protocol = dependencies.scoring_protocol
            if protocol == "candidate_base_rubric_v1":
                if (
                    dependencies.stage6_runner is None
                    or dependencies.rubric_callback is None
                    or not isinstance(dependencies.rubric_prompt_version, str)
                    or not dependencies.rubric_prompt_version.strip()
                    or not isinstance(dependencies.rubric_model_id, str)
                    or not dependencies.rubric_model_id.strip()
                    or dependencies.utility_policy is None
                ):
                    return _blocked(reason="stage6_candidate_base_policy_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            elif protocol == "legacy_score_read_only_v2":
                return _blocked(reason="stage6_legacy_score_write_disabled", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            elif protocol == _LEGACY_SCORE_FIXTURE_PROTOCOL:
                if (
                    dependencies.stage6_runner is None
                    or dependencies.score_callback is None
                    or not isinstance(dependencies.score_prompt_version, str)
                    or not dependencies.score_prompt_version.strip()
                    or not isinstance(dependencies.score_model_id, str)
                    or not dependencies.score_model_id.strip()
                ):
                    return _blocked(reason="stage6_scoring_adapter_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            else:
                return _blocked(reason="stage6_scoring_protocol_unknown", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            if (
                type(dependencies.score_transport_retry_limit) is not int
                or not 0 <= dependencies.score_transport_retry_limit <= 2
            ):
                return _blocked(reason="stage6_transport_retry_limit_invalid", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            try:
                stage6_kwargs = {
                    "db": db,
                    **actor,
                    "provider_capability": dependencies.score_provider_capability,
                    "transport_retry_limit": dependencies.score_transport_retry_limit,
                    "scoring_protocol": protocol,
                }
                if protocol == "candidate_base_rubric_v1":
                    stage6_kwargs.update({
                        "score_callback": None,
                        "rubric_callback": guarded_callback(dependencies.rubric_callback, "stage6_rubric"),
                        "prompt_version": dependencies.rubric_prompt_version.strip(),
                        "model_id": dependencies.rubric_model_id.strip(),
                        "utility_policy": dependencies.utility_policy,
                    })
                else:
                    stage6_kwargs.update({
                        "score_callback": guarded_callback(dependencies.score_callback, "stage6_score"),
                        "prompt_version": dependencies.score_prompt_version.strip(),
                        "model_id": dependencies.score_model_id.strip(),
                    })
                stage6_result = run("stage6", dependencies.stage6_runner, **stage6_kwargs)
            except _PipelineConstraintRejected as rejected:
                exc = rejected.error
                if not _is_infeasible_error(exc):
                    raise
                stages["stage6"] = "infeasible"
                return _infeasible(exc, stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            hashes.update(_hashes(stage6_result))
            if _value(stage6_result, "status") == "infeasible":
                stages["stage6"] = "infeasible"
                return _infeasible(stage6_result, stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            if _value(stage6_result, "status") not in {"completed", "closed", "stage6_scored"}:
                stages["stage6"] = "blocked"
                return _blocked(reason="stage6_not_closed", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
            stages["stage6"] = "completed"
        else:
            stage6_result = stage6_state
            stages["stage6"] = "recovered"

    stage7_was_complete = _value(stage7_head, "next_action") == "complete"
    if stage7_was_complete:
        stage7 = stage7_head
    else:
        callback_pair = (dependencies.adaptation_callback, dependencies.verification_callback)
        if (callback_pair[0] is None) != (callback_pair[1] is None):
            return _blocked(reason="stage7_adaptation_verification_pair_incomplete", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
        required_stage7 = [dependencies.stage7_runner]
        if stage7_head is None:
            required_stage7.extend((dependencies.stage7_difficulty_loader, dependencies.stage7_global_evidence_loader, dependencies.quality_callback))
        if any(value is None for value in required_stage7):
            return _blocked(reason="stage7_native_adapter_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
        difficulties = None
        global_evidence = None
        if stage7_head is None:
            difficulties = run("stage7_difficulty_load", dependencies.stage7_difficulty_loader, db=db, **actor)
            global_evidence = run("stage7_global_evidence_load", dependencies.stage7_global_evidence_loader, db=db, **actor)
        stage7 = run(
            "stage7", dependencies.stage7_runner, db=db, **actor,
            difficulty_resolution_results=difficulties,
            global_constraint_evidence=global_evidence,
            quality_callback=guarded_callback(dependencies.quality_callback, "stage7_quality"),
            adaptation_callback=guarded_callback(dependencies.adaptation_callback, "stage7_adaptation"),
            verification_callback=guarded_callback(dependencies.verification_callback, "stage7_verification"),
            budget=dependencies.stage7_budget,
        )
    hashes.update(_hashes(stage7))
    if not stage7_was_complete and _value(stage7, "status") != "completed":
        stages["stage7"] = "blocked"
        return _blocked(reason="stage7_not_finalized", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    stages["stage7"] = "recovered" if stage7_head is not None else "completed"

    if dependencies.assembler is None or dependencies.native_publisher is None:
        return _blocked(reason="native_assembly_or_publisher_missing", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    assembled = run("assembly", dependencies.assembler, db=db, stage7_result=stage7, **actor)
    if not isinstance(assembled, Mapping) or isinstance(assembled.get("exams"), (list, tuple)):
        return _blocked(reason="assembler_single_exam_contract_invalid", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    stages["assembly"] = "completed"
    published = run("publication", dependencies.native_publisher, db=db, exam=assembled, **actor)
    hashes.update(_hashes(published))
    pending_exam = _pending_receipt_exam(published)
    if pending_exam is not None:
        stages["publication"] = "published_pending_receipt"
        return _receipt_pending(
            pending_exam, stages=stages, elapsed=elapsed, calls=calls,
            retries=retries, hashes=hashes,
        )
    exam = _published_exam(published)
    if exam is None:
        return _blocked(reason="native_publication_not_closed", stages=stages, elapsed=elapsed, calls=calls, retries=retries, hashes=hashes)
    stages["publication"] = "completed"

    warnings: tuple[Mapping[str, Any], ...] = ()
    if dependencies.usage_loader is not None:
        try:
            usage = run("usage_load", dependencies.usage_loader, db=db, **actor)
        except Exception:
            # Publication is authoritative and irreversible; telemetry cannot
            # reverse it into a failure/cancellation/deadline result.
            warnings = ({"code": "usage_missing", "reason": "usage_loader_failed"},)
        else:
            external_calls, external_retries = _counts(usage)
            calls.update(external_calls)
            retries.update(external_retries)
    return PipelineResult("completed", "exam_past_v3_completed", "native_publication_completed", 200, dict(exam), stages, hashes, elapsed, calls, retries, warnings=warnings)


def run_past_v3_pipeline(
    db: Any,
    *,
    job_id: str,
    worker_id: str,
    lease_attempt: int,
    dependencies: PipelineDependencies,
) -> PipelineResult:
    """Run the strict pipeline and safely expose explicit hard constraints."""

    try:
        return _run_past_v3_pipeline(
            db,
            job_id=job_id,
            worker_id=worker_id,
            lease_attempt=lease_attempt,
            dependencies=dependencies,
        )
    except _PipelineConstraintRejected as rejected:
        if _is_infeasible_error(rejected.error):
            stage = _constraint_stage(rejected.callback_name)
            rejected.stages[stage] = "infeasible"
            return _infeasible(
                rejected.error,
                stages=rejected.stages,
                elapsed=rejected.elapsed,
                calls=rejected.calls,
                retries=rejected.retries,
                hashes=rejected.hashes,
            )
        if _is_constraint_error(rejected.error):
            return _constraint_rejected(rejected)
        return _stage_failed(rejected)


__all__ = ["PipelineDependencies", "PipelineResult", "run_past_v3_pipeline"]
