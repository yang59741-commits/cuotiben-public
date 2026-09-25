"""Application boundary for the strict formal-past v3 pipeline.

Production policy, answer evidence and model adapters are deliberately supplied
by the Flask application.  The handler never derives those authorities from
runtime configuration.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .exam_generation_contracts import ExamGenerationResult
from .exam_generation_past_v3_pipeline import (
    PipelineDependencies,
    PipelineResult,
    run_past_v3_pipeline,
)
from .exam_generation_recovery import ExamRecoveryResponse, recover_published_exam_job
from .exam_generation_d2_publication import (
    D2PublicationError,
    recover_d2_publication_outcome,
)
from .exam_generation_v3_publish import (
    ExamGenerationV3PublishError,
    NativeV3PublishOutcome,
    recover_v3_publication_receipt,
)
from .exam_time_policy import ExamDeadlineExceeded
from .job_contracts import JobCancelled, JobLeaseLost
from .ai_memory import memory_from_app
from .exam_generation_memory_delivery import (
    GenerationMemoryDeliveryError,
    recover_generation_memory_delivery,
)


# The extension value must be a callable accepting the documented keyword-only
# context used by ``resolve_pipeline_dependencies`` below.
PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION = (
    "past_v3_pipeline_dependencies_factory"
)
PAST_V3_PUBLICATION_RECEIPT_EXTENSION = "past_v3_publication_receipt_callback"


def _dependency_lock(reason: str) -> PipelineResult:
    return PipelineResult(
        status="blocked",
        code="exam_past_v3_dependency_unavailable",
        reason=reason,
        status_code=422,
        exam=None,
        stages={},
        hashes={},
        stage_elapsed_ms={},
        call_counts={},
        retry_counts={},
        actions=("configure_the_named_native_dependency", "retry_the_same_job"),
    )


def resolve_pipeline_dependencies(
    app: Any,
    *,
    context: Any,
    request: Any,
    preflight: Any,
    job_ai_call: Callable[..., Any],
) -> PipelineDependencies | PipelineResult:
    """Resolve explicitly registered native authorities, failing closed."""

    factory = app.extensions.get(PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION)
    if not callable(factory):
        return _dependency_lock("past_v3_pipeline_dependencies_factory_missing")
    try:
        dependencies = factory(
            app=app,
            context=context,
            request=request,
            preflight=preflight,
            job_ai_call=job_ai_call,
        )
    except (
        JobCancelled,
        JobLeaseLost,
        ExamDeadlineExceeded,
        asyncio.CancelledError,
        concurrent.futures.CancelledError,
        KeyboardInterrupt,
        SystemExit,
    ):
        raise
    except Exception:
        app.logger.exception("formal past v3 dependency factory failed")
        return _dependency_lock("past_v3_pipeline_dependencies_factory_failed")
    if not isinstance(dependencies, PipelineDependencies):
        return _dependency_lock("past_v3_pipeline_dependencies_factory_type_invalid")
    return dependencies


def run_injected_past_v3_pipeline(
    db: Any,
    *,
    app: Any,
    context: Any,
    request: Any,
    preflight: Any,
    job_ai_call: Callable[..., Any],
) -> PipelineResult:
    dependencies = resolve_pipeline_dependencies(
        app,
        context=context,
        request=request,
        preflight=preflight,
        job_ai_call=job_ai_call,
    )
    if isinstance(dependencies, PipelineResult):
        return dependencies
    inherited_guard = dependencies.lifecycle_guard

    def lifecycle_guard(*, stage: str) -> None:
        context.assert_lease()
        context.raise_if_cancelled(stage=stage)
        if inherited_guard is not None:
            inherited_guard(stage=stage)

    dependencies = replace(dependencies, lifecycle_guard=lifecycle_guard)
    result = run_past_v3_pipeline(
        db,
        job_id=context.job_id,
        worker_id=context.coordinator.worker_id,
        lease_attempt=int(context.lease_attempt),
        dependencies=dependencies,
    )
    return enrich_native_pipeline_exam(
        db,
        config=app.config,
        job_id=context.job_id,
        result=result,
    )


def _native_exam_view(
    db: Any,
    *,
    config: Mapping[str, Any],
    job_id: str,
    exam_id: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    if isinstance(exam_id, bool):
        return None, "native_exam_id_invalid"
    try:
        normalized_id = int(exam_id)
    except (TypeError, ValueError, OverflowError):
        return None, "native_exam_id_invalid"
    if normalized_id <= 0:
        return None, "native_exam_id_invalid"
    rows = db.execute(
        "SELECT id,title,content,created_at,artifact_status,generation_job_id "
        "FROM exams WHERE generation_job_id=?",
        (job_id,),
    ).fetchall()
    if len(rows) != 1:
        return None, "native_job_single_exam_binding_invalid"
    row = rows[0]
    if (
        int(row["id"]) != normalized_id
        or str(row["generation_job_id"] or "") != job_id
        or str(row["artifact_status"] or "") != "published"
    ):
        return None, "native_published_exam_binding_invalid"
    pdf_root = config.get("PDF_DIR")
    if not isinstance(pdf_root, (str, Path)) or not Path(
        pdf_root, f"exam_{normalized_id}.pdf"
    ).is_file():
        return None, "native_published_exam_artifact_missing"
    return {
        "id": normalized_id,
        "title": str(row["title"] or ""),
        "content": str(row["content"] or ""),
        "pdf_url": f"/pdfs/exam_{normalized_id}.pdf",
        "created_at": row["created_at"],
    }, None


def enrich_native_pipeline_exam(
    db: Any,
    *,
    config: Mapping[str, Any],
    job_id: str,
    result: PipelineResult,
) -> PipelineResult:
    """Bind a completed native result to exactly one readable published row."""

    if result.status not in {"completed", "published_pending_receipt"}:
        return result
    exam_id = result.exam.get("id") if isinstance(result.exam, Mapping) else None
    exam, reason = _native_exam_view(
        db, config=config, job_id=job_id, exam_id=exam_id
    )
    if reason is not None:
        return PipelineResult(
            status="blocked",
            code="exam_past_v3_publication_view_conflict",
            reason=reason,
            status_code=422,
            exam=None,
            stages=dict(result.stages),
            hashes=dict(result.hashes),
            stage_elapsed_ms=dict(result.stage_elapsed_ms),
            call_counts=dict(result.call_counts),
            retry_counts=dict(result.retry_counts),
            actions=("inspect_native_publication_authority", "retry_the_same_job"),
        )
    return replace(result, exam=exam)


def pipeline_result_to_generation_result(result: PipelineResult) -> ExamGenerationResult:
    body: dict[str, Any] = {
        "code": result.code,
        "reason": result.reason,
        "stages": dict(result.stages),
        "stage_elapsed_ms": dict(result.stage_elapsed_ms),
        "calls": dict(result.call_counts),
        "retries": dict(result.retry_counts),
        "hashes": dict(result.hashes),
    }
    if result.exam is not None:
        exam = dict(result.exam)
        body.update({"exam": exam, "count": 1})
        if exam.get("id") is not None:
            body["id"] = exam["id"]
    if result.witness is not None:
        body["witness"] = dict(result.witness)
        body["details"] = {"witness": dict(result.witness)}
    if result.actions:
        body["actions"] = list(result.actions)
    if result.warnings:
        body["warnings"] = [dict(item) for item in result.warnings]
    if result.status not in {"completed", "published_pending_receipt"}:
        quality_messages = {
            "quality_provider_authentication": "AI 服务认证失败，请在 AI 设置中检查当前配置。此任务已结束，没有发布试卷。",
            "quality_callback_failed": "AI 质量检查未完成，此任务已结束，没有发布试卷。",
        }
        quality_failure = result.reason in quality_messages
        body["error"] = {
            "code": result.code,
            "message": quality_messages.get(result.reason, "正式真题原生生成未满足不可变权威约束"),
            "reason": result.reason,
            "category": (
                "dependency" if result.status == "blocked"
                else "provider" if result.code == "scoring_schema_invalid" or quality_failure
                else "input"
            ),
            "actions": list(result.actions),
        }
        if result.code == "scoring_schema_invalid" or quality_failure:
            body["error"]["retryable"] = False
    return ExamGenerationResult(body=body, status=int(result.status_code))


def make_published_recovery_dispatcher(
    *, worker_id: str, lease_attempt: int, app: Any | None = None,
    context: Any | None = None, request: Any | None = None,
    job_ai_call: Callable[..., Any] | None = None,
) -> Callable[..., ExamRecoveryResponse | None]:
    """Dispatch legacy, D2-native, and historical v46 publication recovery."""

    def response_from_outcome(db, config, generation_job_id, outcome):
        exam_id = (outcome.exam or {}).get("id")
        exam, view_reason = _native_exam_view(
            db, config=config, job_id=generation_job_id, exam_id=exam_id,
        )
        if view_reason is not None:
            return ExamRecoveryResponse(
                {
                    "error": "正式真题原生发布视图与持久化权威不一致",
                    "code": "exam_past_v3_publication_view_conflict",
                    "reason": view_reason,
                    "category": "dependency",
                    "actions": ["inspect_native_publication_authority"],
                },
                422,
            )
        memory_pending = False
        if outcome.status == "published_pending_receipt":
            try:
                memory_pending = recover_generation_memory_delivery(
                    db, operation_id=generation_job_id
                ).state == "pending"
            except GenerationMemoryDeliveryError as exc:
                if exc.reason != "memory_delivery_missing":
                    raise D2PublicationError(
                        exc.reason, "D2 记忆投递状态无法恢复", retryable=True
                    ) from exc
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc).casefold():
                    raise
        body = {
            "code": (
                "memory_delivery_pending" if memory_pending
                else "receipt_pending" if outcome.status == "published_pending_receipt"
                else "exam_past_v3_completed"
            ),
            "reason": (
                "memory_delivery_pending" if memory_pending
                else "publication_receipt_pending" if outcome.status == "published_pending_receipt"
                else "native_publication_recovered"
            ),
            "exam": exam,
            "count": 1,
            "idempotency_replayed": True,
            "hashes": {
                "request_hash": outcome.request_hash,
                "publication_hash": outcome.publication_hash,
                "receipt_hash": outcome.receipt_hash,
            },
        }
        if exam.get("id") is not None:
            body["id"] = exam["id"]
        return ExamRecoveryResponse(
            body, 202 if outcome.status == "published_pending_receipt" else 200
        )

    def dispatch(db, config, generation_job_id: str, *, logger):
        binding = db.execute(
            "SELECT 1 FROM exam_generation_attempt_bindings WHERE job_id=?",
            (generation_job_id,),
        ).fetchone()
        if binding is None:
            return recover_published_exam_job(
                db, config, generation_job_id, logger=logger
            )

        # D2 owns an independent publication namespace.  Probe its committed
        # state before resolving production dependencies so recovery cannot
        # re-enter profile/source loading, local solve, quality, or artifact IO.
        try:
            receipt_callback = (
                app.extensions.get(PAST_V3_PUBLICATION_RECEIPT_EXTENSION)
                if app is not None else None
            )
            d2_outcome = recover_d2_publication_outcome(
                db,
                job_id=generation_job_id,
                worker_id=worker_id,
                lease_attempt=int(lease_attempt),
                receipt_callback=receipt_callback if callable(receipt_callback) else None,
                memory=(
                    memory_from_app(app)
                    if app is not None
                    and isinstance(getattr(app, "config", None), Mapping)
                    and app.config.get("AI_MEMORY_DIR")
                    else None
                ),
                memory_logger=logger,
            )
        except D2PublicationError as exc:
            logger.exception("D2 native publication recovery failed")
            return ExamRecoveryResponse(
                {
                    "error": "正式真题 D2 原生发布恢复失败",
                    "code": exc.code,
                    "reason": exc.reason,
                    "category": "dependency",
                    "actions": ["inspect_native_publication_authority"],
                },
                422,
            )
        if d2_outcome is not None:
            return response_from_outcome(db, config, generation_job_id, d2_outcome)

        # A v39-bound attempt must never be interpreted through the legacy
        # manifest.  No native head means there is simply nothing to recover;
        # the strict pipeline will resume from its highest durable stage.
        try:
            head = db.execute(
                "SELECT state FROM exam_generation_v3_publish_heads WHERE job_id=?",
                (generation_job_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return ExamRecoveryResponse(
                {
                    "error": "正式真题原生发布依赖尚未就绪",
                    "code": "exam_past_v3_dependency_unavailable",
                    "reason": "native_publication_schema_missing",
                    "category": "dependency",
                    "actions": ["complete_native_schema_upgrade", "retry_the_same_job"],
                },
                422,
            )
        if head is None or str(head["state"] or "") == "request_frozen":
            return None
        try:
            outcome: NativeV3PublishOutcome
            recovery_callback = None
            if app is not None and context is not None and job_ai_call is not None:
                dependencies = resolve_pipeline_dependencies(
                    app,
                    context=context,
                    request=request,
                    preflight=None,
                    job_ai_call=job_ai_call,
                )
                if isinstance(dependencies, PipelineDependencies):
                    recovery_callback = dependencies.publication_recovery
            if callable(recovery_callback):
                recovered = recovery_callback(
                    db=db,
                    job_id=generation_job_id,
                    worker_id=worker_id,
                    lease_attempt=int(lease_attempt),
                )
                if not isinstance(recovered, Mapping):
                    raise ExamGenerationV3PublishError(
                        "exam_v3_publish_conflict",
                        "authoritative_recovery_result_invalid",
                        "生产回执恢复结果无效",
                    )
                exam_value = recovered.get("exam")
                exam_id_value = exam_value.get("id") if isinstance(exam_value, Mapping) else None
                outcome = NativeV3PublishOutcome(
                    generation_job_id,
                    str(recovered.get("state") or ("published_pending_receipt" if recovered.get("status") == "published_pending_receipt" else "completed")),
                    str(recovered.get("request_hash") or ""),
                    int(exam_id_value) if exam_id_value is not None else None,
                    recovered.get("publication_hash"),
                    recovered.get("receipt_hash"),
                    True,
                )
            elif str(head["state"] or "") == "completed":
                outcome = recover_v3_publication_receipt(
                    db,
                    job_id=generation_job_id,
                    worker_id=worker_id,
                    lease_attempt=int(lease_attempt),
                    receipt_callback=None,
                )
            else:
                # Missing production receipt authority must never be treated
                # as a successful no-op callback.  Expose the durable pending
                # publication without advancing its head.
                row = db.execute(
                    "SELECT h.request_hash,h.publication_hash,p.exam_id "
                    "FROM exam_generation_v3_publish_heads h "
                    "JOIN exam_generation_v3_publications p ON p.job_id=h.job_id "
                    "WHERE h.job_id=? AND h.state='published_pending_receipt'",
                    (generation_job_id,),
                ).fetchone()
                if row is None:
                    raise ExamGenerationV3PublishError(
                        "exam_v3_publish_conflict",
                        "pending_publication_fact_missing",
                        "待回执发布事实缺失",
                    )
                outcome = NativeV3PublishOutcome(
                    generation_job_id, "published_pending_receipt",
                    str(row["request_hash"]), int(row["exam_id"]),
                    str(row["publication_hash"]), None, True,
                )
        except ExamGenerationV3PublishError as exc:
            logger.exception("native v3 publication recovery failed")
            return ExamRecoveryResponse(
                {
                    "error": "正式真题原生发布恢复失败",
                    "code": exc.code,
                    "reason": exc.reason,
                    "category": "dependency",
                    "actions": ["inspect_native_publication_authority"],
                },
                422,
            )
        return response_from_outcome(db, config, generation_job_id, outcome)

    return dispatch


__all__ = [
    "PAST_V3_PIPELINE_DEPENDENCIES_FACTORY_EXTENSION",
    "make_published_recovery_dispatcher",
    "enrich_native_pipeline_exam",
    "pipeline_result_to_generation_result",
    "resolve_pipeline_dependencies",
    "run_injected_past_v3_pipeline",
]
