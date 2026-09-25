"""Published-receipt recovery boundary for idempotent exam jobs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .exam_manifests import ExamManifestError
from .ai_memory import AIMemory
from .exam_generation_memory_delivery import (
    dispatch_generation_memory_delivery,
)
from .exam_publication import ExamReceiptRecoveryPending, replay_published_job


@dataclass(frozen=True, slots=True)
class ExamRecoveryResponse:
    body: Any
    status: int | None = None


def _memory_delivery_pending_response() -> ExamRecoveryResponse:
    return ExamRecoveryResponse(
        {
            "error": "试卷已发布，生成记忆仍待交付，请稍后重试",
            "code": "memory_delivery_pending",
            "memory_delivery": "pending",
            "retryable": True,
        },
        503,
    )


def _has_memory_delivery(db, operation_id: str) -> bool:
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='exam_generation_memory_event_outbox_v1'"
    ).fetchone()
    if table is None:
        return False
    return db.execute(
        "SELECT 1 FROM exam_generation_memory_event_outbox_v1 "
        "WHERE operation_id=?",
        (operation_id,),
    ).fetchone() is not None


def _memory_from_config(config) -> AIMemory:
    return AIMemory(
        config["AI_MEMORY_DIR"],
        daily_days=config.get("AI_MEMORY_DAILY_DAYS", 30),
        max_event_chars=config.get("AI_MEMORY_MAX_EVENT_CHARS", 20_000),
        max_context_chars=config.get("AI_MEMORY_MAX_CONTEXT_CHARS", 12_000),
    )


def recover_published_exam_job(
    db,
    config,
    generation_job_id: str,
    *,
    logger,
    memory=None,
) -> ExamRecoveryResponse | None:
    try:
        replayed = replay_published_job(db, config, generation_job_id)
    except ExamReceiptRecoveryPending:
        logger.exception("published exam receipt recovery is pending")
        return ExamRecoveryResponse(
            {
                "error": "试卷已发布，正在确认幂等收据，请稍后重试",
                "code": "receipt_recovery_pending",
            },
            503,
        )
    except ExamManifestError as manifest_error:
        logger.exception("failed to reconcile exam manifest receipt")
        return ExamRecoveryResponse(
            {"error": str(manifest_error), "code": manifest_error.code},
            409,
        )
    if replayed is None:
        return None
    # Historical publications predate the durable outbox.  They replay as-is;
    # only a publication carrying its own envelope participates in recovery.
    if not _has_memory_delivery(db, generation_job_id):
        return ExamRecoveryResponse(replayed)
    try:
        delivery = dispatch_generation_memory_delivery(
            db,
            operation_id=generation_job_id,
            memory=(memory if memory is not None else _memory_from_config(config)),
            logger=logger,
        )
    except Exception:
        try:
            logger.exception("published exam memory delivery is pending")
        except Exception:
            pass
        return _memory_delivery_pending_response()
    if delivery.get("state") != "delivered":
        return _memory_delivery_pending_response()
    completed = dict(replayed)
    completed["memory_delivery"] = "delivered"
    return ExamRecoveryResponse(completed)


__all__ = ["ExamRecoveryResponse", "recover_published_exam_job"]
