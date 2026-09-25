"""Request validation, replay recovery and immutable identity preflight."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_contracts import validate_generation_job_id
from .exam_generation_attempt_binding import (
    ExamGenerationAttemptBindingError,
)
from .exam_generation_profile_worker import (
    ExamGenerationProfileWorkerError,
    PastFrozenProfileContext,
)
from .exam_generation_stage3_contract import (
    ExamGenerationStage3ContractError,
)
from .exam_generation_stage4_worker import Stage4WorkerError
from .exam_generation_recovery import recover_published_exam_job
from .exam_generation_request import FrozenGenerationRequest, freeze_generation_request


def _validation_error_body(exc: ValueError) -> dict[str, Any]:
    body: dict[str, Any] = {"error": str(exc)}
    code = str(getattr(exc, "code", "") or "").strip()
    if code:
        body.update({"code": code, "retryable": False})
    return body


@dataclass(frozen=True, slots=True)
class GenerationPreflightOutcome:
    """Exactly one of ``request`` or ``response_body`` is populated."""

    generation_job_id: str | None = None
    request: FrozenGenerationRequest | None = None
    past_profile_context: PastFrozenProfileContext | None = None
    response_body: Mapping[str, Any] | None = None
    response_status: int | None = None

    @property
    def completed(self) -> bool:
        return self.response_body is not None


def prepare_generation_preflight(
    payload: Mapping[str, Any],
    *,
    db: Any,
    config: Mapping[str, Any],
    logger: Any,
    request_id: str | None,
    require_ai_config: Callable[[], Any],
    progress: Callable[[int, int, str], None],
    past_attempt_resolver: Callable[
        [Any, Mapping[str, Any]], PastFrozenProfileContext | None
    ]
    | None = None,
    past_attempt_binder: Callable[
        [Any, str, PastFrozenProfileContext], Any
    ]
    | None = None,
    past_stage3_contract_binder: Callable[
        [Any, str, PastFrozenProfileContext], Any
    ]
    | None = None,
    past_stage4_fact_binder: Callable[
        [Any, str, PastFrozenProfileContext], Any
    ]
    | None = None,
    allow_legacy_past_characterization: bool = False,
    published_recovery_dispatcher: Callable[..., Any] | None = None,
) -> GenerationPreflightOutcome:
    """Recover published work, then freeze and validate attempt authority."""

    recover_published = (
        published_recovery_dispatcher or recover_published_exam_job
    )

    try:
        generation_job_id = validate_generation_job_id(payload.get("_job_id"))
    except ValueError as exc:
        return GenerationPreflightOutcome(
            response_body=_validation_error_body(exc), response_status=400
        )

    if generation_job_id:
        recovery = recover_published(
            db,
            config,
            generation_job_id,
            logger=logger,
        )
        if recovery is not None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                response_body=dict(recovery.body),
                response_status=recovery.status,
            )

    try:
        frozen_request = freeze_generation_request(
            payload,
            generation_job_id=generation_job_id,
            request_id=request_id,
        )
    except ValueError as exc:
        return GenerationPreflightOutcome(
            generation_job_id=generation_job_id,
            response_body=_validation_error_body(exc),
            response_status=400,
        )

    past_profile_context = None
    if frozen_request.source == "past" and allow_legacy_past_characterization:
        if not bool(config.get("TESTING")):
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                response_body={
                    "error": "真题 worker 门禁配置无效，已停止生成",
                    "code": "past_profile_worker_gate_unconfigured",
                    "reason": "legacy_past_characterization_outside_testing",
                    "retryable": False,
                    "category": "configuration",
                },
                response_status=500,
            )
    elif frozen_request.source == "past":
        if past_attempt_resolver is None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                response_body={
                    "error": "真题 worker 门禁尚未配置，已停止生成",
                    "code": "past_profile_worker_gate_unconfigured",
                    "reason": "past_attempt_resolver_missing",
                    "retryable": False,
                    "category": "configuration",
                },
                response_status=500,
            )
        try:
            past_profile_context = past_attempt_resolver(db, payload)
        except ExamGenerationProfileWorkerError as exc:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                response_body={
                    "error": str(exc),
                    "code": exc.code,
                    "reason": exc.reason,
                    "retryable": exc.retryable,
                    "actions": list(exc.actions),
                    "category": exc.category,
                },
                response_status=exc.status_code,
            )
        if past_profile_context is None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                response_body={
                    "error": "真题任务未返回冻结画像上下文，请重新提交",
                    "code": "past_profile_freeze_missing",
                    "reason": "past_profile_context_missing",
                    "retryable": False,
                    "actions": ["resubmit_past_exam"],
                    "category": "contract",
                },
                response_status=409,
            )
        if generation_job_id is None:
            return GenerationPreflightOutcome(
                response_body={
                    "error": "真题 worker 缺少任务身份，已停止生成",
                    "code": "past_profile_worker_gate_unconfigured",
                    "reason": "generation_job_id_missing_for_binding",
                    "retryable": False,
                    "category": "configuration",
                },
                response_status=500,
            )
        if past_attempt_binder is None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": "真题 worker 回执门禁尚未配置，已停止生成",
                    "code": "past_profile_worker_gate_unconfigured",
                    "reason": "past_attempt_binder_missing",
                    "retryable": False,
                    "category": "configuration",
                },
                response_status=500,
            )
        try:
            past_attempt_binder(
                db,
                generation_job_id,
                past_profile_context,
            )
        except ExamGenerationAttemptBindingError as exc:
            if exc.code == "published_recovery_required":
                recovery = recover_published(
                    db,
                    config,
                    generation_job_id,
                    logger=logger,
                )
                if recovery is not None:
                    return GenerationPreflightOutcome(
                        generation_job_id=generation_job_id,
                        response_body=dict(recovery.body),
                        response_status=recovery.status,
                    )
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": str(exc),
                    "code": exc.code,
                    "reason": exc.reason,
                    "retryable": exc.retryable,
                    "actions": list(exc.actions),
                    "category": exc.category,
                },
                response_status=exc.status_code,
            )
        recovery = recover_published(
            db,
            config,
            generation_job_id,
            logger=logger,
        )
        if recovery is not None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body=dict(recovery.body),
                response_status=recovery.status,
            )
        if past_stage3_contract_binder is None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": "真题逐槽生成合同门禁尚未配置，已停止生成",
                    "code": "past_profile_worker_gate_unconfigured",
                    "reason": "past_stage3_contract_binder_missing",
                    "retryable": False,
                    "category": "configuration",
                },
                response_status=500,
            )
        try:
            past_stage3_contract_binder(
                db,
                generation_job_id,
                past_profile_context,
            )
        except ExamGenerationStage3ContractError as exc:
            if exc.code == "published_recovery_required":
                recovery = recover_published(
                    db,
                    config,
                    generation_job_id,
                    logger=logger,
                )
                if recovery is not None:
                    return GenerationPreflightOutcome(
                        generation_job_id=generation_job_id,
                        past_profile_context=past_profile_context,
                        response_body=dict(recovery.body),
                        response_status=recovery.status,
                    )
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": str(exc),
                    "code": exc.code,
                    "reason": exc.reason,
                    "retryable": exc.retryable,
                    "actions": list(exc.actions),
                    "category": exc.category,
                },
                response_status=exc.status_code,
            )
        recovery = recover_published(
            db,
            config,
            generation_job_id,
            logger=logger,
        )
        if recovery is not None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body=dict(recovery.body),
                response_status=recovery.status,
            )
        if past_stage4_fact_binder is None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": "真题严格语义投影门禁尚未配置，已停止生成",
                    "code": "semantic_projection_unavailable",
                    "reason": "past_stage4_fact_binder_missing",
                    "retryable": False,
                    "actions": ["resubmit_after_upgrade"],
                    "category": "dependency",
                },
                response_status=409,
            )
        try:
            past_stage4_fact_binder(
                db,
                generation_job_id,
                past_profile_context,
            )
        except Stage4WorkerError as exc:
            if exc.code == "published_recovery_required":
                recovery = recover_published(
                    db,
                    config,
                    generation_job_id,
                    logger=logger,
                )
                if recovery is not None:
                    return GenerationPreflightOutcome(
                        generation_job_id=generation_job_id,
                        past_profile_context=past_profile_context,
                        response_body=dict(recovery.body),
                        response_status=recovery.status,
                    )
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body={
                    "error": str(exc),
                    "code": exc.code,
                    "reason": exc.reason,
                    "retryable": exc.retryable,
                    "actions": list(exc.actions),
                    "category": exc.category,
                },
                response_status=exc.status_code,
            )
        recovery = recover_published(
            db,
            config,
            generation_job_id,
            logger=logger,
        )
        if recovery is not None:
            return GenerationPreflightOutcome(
                generation_job_id=generation_job_id,
                past_profile_context=past_profile_context,
                response_body=dict(recovery.body),
                response_status=recovery.status,
            )
        return GenerationPreflightOutcome(
            generation_job_id=generation_job_id,
            request=frozen_request,
            past_profile_context=past_profile_context,
        )

    config_error = require_ai_config()
    if config_error:
        body, status = config_error
        return GenerationPreflightOutcome(
            generation_job_id=generation_job_id,
            response_body=dict(body),
            response_status=int(status),
        )

    progress(1, 7, "输入校验完成")
    progress(2, 7, "正在冻结题源")
    return GenerationPreflightOutcome(
        generation_job_id=generation_job_id,
        request=frozen_request,
        past_profile_context=past_profile_context,
    )


__all__ = ["GenerationPreflightOutcome", "prepare_generation_preflight"]
