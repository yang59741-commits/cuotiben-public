"""Generation job environment, native-past binding and outcome adaptation."""

from __future__ import annotations
from collections.abc import Mapping
from ..database import get_db
from .jobs import DeferredJobError, JobOutcome, PermanentJobError, RetryableJobError
from .job_progress import use_job_progress
from .tex import compile_exam, use_compile_runner
from .exam_generation import generate_exam
from .exam_generation_attempt_binding import persist_profile_resolved_binding
from .exam_generation_contracts import ExamGenerationResult
from .exam_generation_d2_publication import D2PublicationError
from .exam_generation_preflight import prepare_generation_preflight
from .exam_generation_past_v3_integration import (
    make_published_recovery_dispatcher,
    pipeline_result_to_generation_result,
    run_injected_past_v3_pipeline,
)
from .exam_generation_profile_worker import (
    resolve_persisted_past_profile_for_attempt,
)
from .exam_generation_stage3_contract import (
    persist_stage3_generation_contract,
)
from .exam_generation_stage4_worker import (
    freeze_and_persist_stage4_candidate_facts,
)
from .exam_publish import ExamPublicationFence, compensate_exam_publication
from .exam_runtime import build_exam_generation_runtime
from .semantic_index_refresh import load_app_semantic_source_bundle
from .ai import DeepSeekConfigurationError, DeepSeekServiceError, use_ai_attempt_guard
from .material_understanding import MaterialUnderstandingError
from .job_handlers_ai import make_job_ai_call

def prepare_native_past_attempt(app, context, request_payload, job_ai_call):
    """Recover publication first, then bind the frozen native attempt and facts."""
    def strict_invoke(stage, callback, *args, **kwargs):
        context.assert_lease()
        context.raise_if_cancelled(stage=stage)
        return callback(*args, **kwargs)

    def resolve_profile(*args, **kwargs):
        return strict_invoke(
            "past_profile_resolve", resolve_persisted_past_profile_for_attempt,
            *args, **kwargs,
        )

    def bind_profile(db, job_id, profile_context):
        return strict_invoke(
            "past_profile_bind", persist_profile_resolved_binding, db,
            job_id=job_id, profile_context=profile_context,
            worker_id=context.coordinator.worker_id, lease_attempt=context.lease_attempt,
        )

    def bind_contract(db, job_id, profile_context):
        return strict_invoke(
            "stage3_contract_bind", persist_stage3_generation_contract, db,
            job_id=job_id, profile_context=profile_context,
            worker_id=context.coordinator.worker_id, lease_attempt=context.lease_attempt,
        )

    def freeze_candidates(db, job_id, profile_context):
        return strict_invoke(
            "stage4_candidate_facts_bind", freeze_and_persist_stage4_candidate_facts, db,
            job_id=job_id, payload=request_payload, profile_context=profile_context,
            source_bundle_loader=lambda: strict_invoke(
                "stage4_source_bundle_load", load_app_semantic_source_bundle, app,
            ),
            projection_algorithm_version=str(app.config.get("SEMANTIC_RECALL_ALGORITHM_VERSION") or ""),
            worker_id=context.coordinator.worker_id, lease_attempt=context.lease_attempt,
        )

    return prepare_generation_preflight(
        request_payload, db=get_db(), config=app.config, logger=app.logger,
        request_id=context.job_id, require_ai_config=lambda: None, progress=lambda *_args: None,
        past_attempt_resolver=resolve_profile, past_attempt_binder=bind_profile,
        past_stage3_contract_binder=bind_contract, past_stage4_fact_binder=freeze_candidates,
        published_recovery_dispatcher=make_published_recovery_dispatcher(
            worker_id=context.coordinator.worker_id, lease_attempt=int(context.lease_attempt),
            app=app, context=context, request=request_payload, job_ai_call=job_ai_call,
        ),
    )


def run_native_past_generation(app, context, request_payload, job_ai_call):
    past_preflight = prepare_native_past_attempt(app, context, request_payload, job_ai_call)
    if past_preflight.completed:
        result = ExamGenerationResult(
            body=dict(past_preflight.response_body or {}),
            status=int(
                past_preflight.response_status or 200
            ),
        )
    else:
        try:
            pipeline_result = run_injected_past_v3_pipeline(
                get_db(),
                app=app,
                context=context,
                request=past_preflight.request,
                preflight=past_preflight,
                job_ai_call=job_ai_call,
            )
        except D2PublicationError as exc:
            if exc.reason != "quality_environment_requires_repair":
                raise
            raise PermanentJobError(
                "试卷渲染模板或编译环境需要修复，修复后可重试同一任务",
                code=exc.reason,
                category="dependency",
                details={
                    "reason": exc.reason,
                    "actions": [
                        "repair_template_or_environment",
                        "retry_the_same_job",
                    ],
                },
            ) from exc
        result = pipeline_result_to_generation_result(
            pipeline_result
        )

    return result


def run_service_generation(app, context, request_payload, job_ai_call):
    context.update_progress(0, 7, "正在校验输入")
    with use_ai_attempt_guard(context.raise_if_cancelled):
        with use_job_progress(context.update_progress):
            with use_compile_runner(
                context.run_cancellable_process
            ):
                result = generate_exam(
                    request_payload,
                    build_exam_generation_runtime(
                        app,
                        get_db(),
                        ai_call=job_ai_call,
                        compile_exam=compile_exam,
                        progress=context.update_progress,
                        request_id=context.job_id,
                        publication_fence=(
                            ExamPublicationFence(
                                job_id=context.job_id,
                                worker_id=(
                                    context.coordinator.worker_id
                                ),
                                lease_attempt=int(
                                    context.lease_attempt
                                ),
                            )
                        ),
                        observe_metric=lambda name, value: (
                            context.add_exam_metric(
                                name,
                                int(value),
                                maximum=name
                                == "candidate_count",
                            )
                        ),
                    ),
                )
    return result


def generation_outcome(app, context, result, *, strict_past_v3, job_error_from_response):
    body = result.body if isinstance(result.body, Mapping) else {}
    result_code = str(body.get("code") or "").upper()
    if result_code == "MEMORY_DELIVERY_PENDING":
        raise DeferredJobError(
            "试卷已发布，等待补齐本模式记忆",
            code="memory_delivery_pending",
            delay_seconds=30,
            details={"reason": str(body.get("reason") or "memory_delivery_pending")},
        )
    if strict_past_v3 and (
        result.status == 202 or result_code == "RECEIPT_PENDING"
    ):
        raise DeferredJobError(
            "试卷已经原生发布，正在完成不可变回执",
            code="receipt_pending",
            delay_seconds=0.5,
            details={
                "reason": str(
                    body.get("reason")
                    or "publication_receipt_pending"
                ),
                "actions": list(body.get("actions") or [
                    "complete_publication_receipt",
                    "retry_the_same_job",
                ]),
            },
        )
    if result.status >= 400 or result_code == "QUESTION_BANK_BUILDING":
        code = result_code
        if code == "QUESTION_BANK_BUILDING":
            raw_details = body.get("details")
            details = dict(raw_details) if isinstance(raw_details, Mapping) else {}
            raw_job = body.get("job")
            dependency_id = str(
                body.get("job_id")
                or details.get("job_id")
                or (raw_job.get("id") if isinstance(raw_job, Mapping) else "")
                or ""
            )
            dependency = None
            if dependency_id:
                try:
                    dependency = context.coordinator.get_job(dependency_id)
                except Exception:
                    dependency = None
            if not dependency_id:
                raise PermanentJobError(
                    "题库索引依赖任务信息缺失，请重建题库后再试",
                    code="QUESTION_BANK_DEPENDENCY_MISSING",
                    category="dependency",
                    details={
                        "actions": ["rebuild_question_bank"],
                    },
                )
            dependency_status = str((dependency or {}).get("status") or "")
            if dependency is None or dependency_status in {
                "queued", "running", "cancel_requested"
            }:
                if dependency_id:
                    details["job_id"] = dependency_id
                raise DeferredJobError(
                    "正在等待题库索引构建完成",
                    code="question_bank_building",
                    delay_seconds=1.5,
                    details=details,
                )
            if dependency_status == "succeeded":
                raise DeferredJobError(
                    "题库索引刚刚完成，正在重新校验",
                    code="question_bank_building",
                    delay_seconds=0.5,
                    details={"job_id": dependency_id},
                )
            raise PermanentJobError(
                "题库索引构建失败，请重建题库后再试",
                code="QUESTION_BANK_BUILD_FAILED",
                category="dependency",
                details={
                    "job_id": dependency_id,
                    "actions": ["rebuild_question_bank"],
                    "dependency_error": dependency.get("error")
                    if isinstance(dependency, Mapping)
                    else {},
                },
            )
        raise job_error_from_response(result.body, result.status)
    context.update_progress(7, 7, "已完成")
    body = result.body
    exams = body.get("exams") if isinstance(body.get("exams"), list) else []
    exam_ids = tuple(
        int(item["id"])
        for item in exams
        if isinstance(item, dict) and item.get("id")
    )
    if not exam_ids and body.get("id"):
        exam_ids = (int(body["id"]),)

    def compensate_deadline():
        if strict_past_v3 or not exam_ids:
            return
        with app.app_context():
            compensate_exam_publication(
                get_db(),
                exam_ids,
                pdf_dir=app.config["PDF_DIR"],
                warn=app.logger.warning,
            )

    return JobOutcome(
        result=body,
        exam_ids=exam_ids,
        side_effect_committed=True,
        deadline_compensation=(
            None if strict_past_v3 or body.get("memory_delivery") == "delivered"
            else compensate_deadline
        ),
    )


def make_exam_generation_handler(app, *, call_ai, job_error_from_response):
    def handler(context, payload):
        operation = str(payload.get("operation") or "generate")
        job_ai_call = make_job_ai_call(context, call_ai, exam_attempt=operation == "generate")
        if operation != "generate":
            raise PermanentJobError("任务操作类型无效", code="invalid_operation", category="input")
        request_payload = {
            key: value for key, value in payload.items() if key != "operation"
        }
        request_payload["_job_id"] = context.job_id
        strict_past_v3 = (
            str(request_payload.get("source") or "free") == "past"
        )
        try:
            with app.app_context():
                result = None
                if strict_past_v3:
                    result = run_native_past_generation(app, context, request_payload, job_ai_call)
                if result is None:
                    result = run_service_generation(app, context, request_payload, job_ai_call)
        except DeepSeekConfigurationError as exc:
            raise PermanentJobError(
                str(exc),code="configuration",category="configuration"
            ) from exc
        except DeepSeekServiceError as exc:
            if exc.code.casefold() == "context_safety_exceeded":
                raise PermanentJobError(
                    str(exc),
                    code="context_safety_exceeded",
                    category="input",
                    details={"details": dict(exc.details)},
                ) from exc
            if exc.retryable:
                raise RetryableJobError(
                    "AI 服务暂时不可用，请稍后重试",
                    code="ai_provider_response",
                    delay_seconds=2,
                ) from exc
            raise PermanentJobError(
                "AI 服务拒绝了组卷请求，请检查 Provider 配置或输入",
                code="ai_provider_response",
                category="provider",
            ) from exc
        except MaterialUnderstandingError as exc:
            raise RetryableJobError(
                str(exc),code="material_understanding_incomplete",delay_seconds=2
            ) from exc
        return generation_outcome(
            app, context, result, strict_past_v3=strict_past_v3,
            job_error_from_response=job_error_from_response,
        )
    return handler
