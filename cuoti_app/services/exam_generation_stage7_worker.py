"""Crash-safe Stage-7 orchestration over the closed repository protocol.

The worker owns no provider configuration and performs no network calls.  All
external work is injected as callbacks.  Each callback receives the exact
request that was persisted first and its canonical SHA-256 idempotency key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage4_difficulty import DifficultyResolutionResult
from .exam_generation_stage7_quality import (
    Stage7Budget,
    Stage7Cancellation,
    Stage7DeadlineExceeded,
    Stage7LeaseLost,
    Stage7Result,
)
from .exam_generation_stage7_repository import (
    Stage7RepositoryError,
    expected_stage7_request,
    persist_stage7_context,
    persist_stage7_disposition,
    persist_stage7_finalized_checkpoint,
    persist_stage7_request,
    persist_stage7_result,
    recover_stage7_state,
    replay_stage7_result,
)
from .exam_time_policy import ExamDeadlineExceeded
from .job_contracts import JobCancelled, JobLeaseLost


Stage7Callback = Callable[[Mapping[str, Any], str], str | Mapping[str, Any]]
Stage7StepHook = Callable[[str], None]
_CALL_KINDS = ("quality", "adaptation", "verification")


class Stage7WorkerError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = False


@dataclass(frozen=True, slots=True)
class Stage7WorkerResult:
    status: str
    code: str
    reason: str
    status_code: int
    context_hash: str
    result_hash: str
    checkpoint_hash: str
    pre_adaptation_hash: str
    final_exam_hash: str
    adapted_position: int | None
    single_item_fallback: bool
    result: Stage7Result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _request_hash(request: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(request).encode("utf-8")).hexdigest()


def _step(hook: Stage7StepHook | None, name: str) -> None:
    if hook is not None:
        hook(name)


def _is_control_flow(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            JobCancelled,
            JobLeaseLost,
            Stage7Cancellation,
            Stage7LeaseLost,
            Stage7DeadlineExceeded,
            ExamDeadlineExceeded,
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ),
    )


def _callback_response(callback: Stage7Callback, request: Mapping[str, Any]) -> str:
    value = callback(request, _request_hash(request))
    if isinstance(value, Mapping):
        return _canonical(value)
    if isinstance(value, str) and value:
        return value
    raise Stage7WorkerError(
        "exam_stage7_callback_failed",
        "callback_result_invalid",
        "阶段 7 callback 必须返回非空 JSON 文本或对象",
        status_code=502,
    )


def _persist_callback_disposition(
    db,
    *,
    state,
    call_kind: str,
    callback: Stage7Callback,
    actor: Mapping[str, Any],
    stage_hook: Stage7StepHook | None,
) -> None:
    request = state.requests[call_kind]
    try:
        response_text = _callback_response(callback, request)
    except BaseException as exc:
        if _is_control_flow(exc) or not isinstance(exc, Exception):
            raise
        outcome = "timeout" if isinstance(exc, TimeoutError) else "error"
        _step(stage_hook, f"{call_kind}_callback")
        persist_stage7_disposition(
            db,
            call_kind=call_kind,
            outcome=outcome,
            expected_revision=state.head.revision,
            **actor,
        )
    else:
        _step(stage_hook, f"{call_kind}_callback")
        persist_stage7_disposition(
            db,
            call_kind=call_kind,
            outcome="received",
            response_text=response_text,
            expected_revision=state.head.revision,
            **actor,
        )
    _step(stage_hook, f"{call_kind}_disposition")


def _completed(state) -> Stage7WorkerResult:
    result = state.result
    if result is None or state.head.result_hash is None or state.head.checkpoint_hash is None:
        raise Stage7WorkerError(
            "exam_stage7_incomplete",
            "completed_state_incomplete",
            "阶段 7 完成态缺少结果或 checkpoint",
        )
    return Stage7WorkerResult(
        "completed",
        "exam_stage7_completed",
        "stage7_finalized",
        200,
        state.head.context_hash,
        state.head.result_hash,
        state.head.checkpoint_hash,
        result.pre_adaptation_hash,
        result.final_exam_hash,
        result.adapted_position,
        result.single_item_fallback,
        result,
    )


def run_stage7_worker(
    db,
    *,
    job_id: str,
    worker_id: str,
    lease_attempt: int,
    difficulty_resolution_results: Sequence[DifficultyResolutionResult] | None = None,
    global_constraint_evidence: Mapping[str, Any] | None = None,
    quality_callback: Stage7Callback | None = None,
    adaptation_callback: Stage7Callback | None = None,
    verification_callback: Stage7Callback | None = None,
    budget: Stage7Budget = Stage7Budget(),
    stage_hook: Stage7StepHook | None = None,
) -> Stage7WorkerResult:
    """Recover or close Stage 7 for one fenced worker attempt; never publish."""

    if quality_callback is not None and not callable(quality_callback):
        raise Stage7WorkerError("exam_stage7_contract_invalid", "quality_callback_invalid", "阶段 7 质量审查 callback 无效", status_code=422)
    if adaptation_callback is not None and not callable(adaptation_callback):
        raise Stage7WorkerError("exam_stage7_contract_invalid", "adaptation_callback_invalid", "阶段 7 改编 callback 无效", status_code=422)
    if verification_callback is not None and not callable(verification_callback):
        raise Stage7WorkerError("exam_stage7_contract_invalid", "verification_callback_invalid", "阶段 7 核验 callback 无效", status_code=422)
    if not isinstance(budget, Stage7Budget):
        raise Stage7WorkerError("exam_stage7_contract_invalid", "budget_type_invalid", "阶段 7 预算合同无效", status_code=422)
    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}

    try:
        state = recover_stage7_state(db, **actor)
    except Stage7RepositoryError as exc:
        if exc.reason != "stage7_head_missing":
            raise
        if not callable(quality_callback):
            raise Stage7WorkerError("exam_stage7_contract_invalid", "quality_callback_missing", "新建阶段 7 必须显式注入质量审查 callback", status_code=422)
        if not isinstance(global_constraint_evidence, Mapping):
            raise Stage7WorkerError("exam_stage7_contract_invalid", "global_constraint_evidence_missing", "新建阶段 7 必须显式接收全卷约束证据", status_code=422)
        if (
            isinstance(difficulty_resolution_results, (str, bytes))
            or not isinstance(difficulty_resolution_results, Sequence)
            or any(not isinstance(value, DifficultyResolutionResult) for value in difficulty_resolution_results)
        ):
            raise Stage7WorkerError("exam_stage7_contract_invalid", "difficulty_results_missing", "新建阶段 7 必须显式接收完整 DifficultyResolutionResult 集合", status_code=422)
        persist_stage7_context(
            db,
            difficulty_resolution_results=tuple(difficulty_resolution_results),
            global_constraint_evidence=global_constraint_evidence,
            budget=budget,
            adaptation_enabled=adaptation_callback is not None,
            verification_enabled=verification_callback is not None,
            **actor,
        )
        _step(stage_hook, "context")
    else:
        # A finalized Stage 7 has advanced the shared Stage-6 head, so calling
        # context persistence again would no longer have a stage6_scored root.
        # Compare every explicit authority input against the recovered freeze.
        if (
            (global_constraint_evidence is not None and _canonical(state.global_constraint_evidence) != _canonical(global_constraint_evidence))
            or (difficulty_resolution_results is not None and [value.to_dict() for value in state.difficulty_results] != [value.to_dict() for value in difficulty_resolution_results])
        ):
            raise Stage7WorkerError(
                "exam_stage7_contract_conflict",
                "context_argument_drift",
                "阶段 7 显式权威输入与已冻结上下文不一致",
            )
        if state.next_action == "complete":
            return _completed(state)

    callbacks = {
        "quality": quality_callback,
        "adaptation": adaptation_callback,
        "verification": verification_callback,
    }
    while True:
        state = recover_stage7_state(db, **actor)
        action = state.next_action
        if action == "complete":
            return _completed(state)
        if action == "checkpoint":
            row = db.execute(
                "SELECT checkpoint_hash,revision FROM exam_generation_stage6_heads WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise Stage7WorkerError("exam_stage7_incomplete", "stage6_head_missing", "阶段 7 checkpoint 缺少阶段 6 head")
            persist_stage7_finalized_checkpoint(
                db,
                expected_revision=state.head.revision,
                expected_stage6_revision=int(row["revision"]),
                **actor,
            )
            _step(stage_hook, "checkpoint")
            continue
        if action == "final":
            result = replay_stage7_result(db, **actor)
            persist_stage7_result(db, result=result, expected_revision=state.head.revision, **actor)
            _step(stage_hook, "result")
            continue

        if action.endswith("_request"):
            call_kind = action.removesuffix("_request")
            if call_kind not in _CALL_KINDS:
                raise Stage7WorkerError("exam_stage7_incomplete", "next_action_invalid", "阶段 7 恢复动作无效")
            request = expected_stage7_request(db, call_kind=call_kind, **actor)
            if request is None:
                raise Stage7WorkerError("exam_stage7_incomplete", "expected_request_missing", "阶段 7 预期请求缺失")
            persist_stage7_request(
                db,
                call_kind=call_kind,
                request=request,
                expected_revision=state.head.revision,
                **actor,
            )
            _step(stage_hook, f"{call_kind}_request")
            continue

        if action.endswith("_response"):
            call_kind = action.removesuffix("_response")
            callback = callbacks.get(call_kind)
            if call_kind not in _CALL_KINDS or callback is None:
                raise Stage7WorkerError("exam_stage7_incomplete", "pending_callback_missing", "阶段 7 冻结请求缺少对应 callback")
            _persist_callback_disposition(
                db,
                state=state,
                call_kind=call_kind,
                callback=callback,
                actor=actor,
                stage_hook=stage_hook,
            )
            continue

        raise Stage7WorkerError("exam_stage7_incomplete", "next_action_invalid", "阶段 7 恢复动作无效")


__all__ = ["Stage7Callback", "Stage7StepHook", "Stage7WorkerError", "Stage7WorkerResult", "run_stage7_worker"]
