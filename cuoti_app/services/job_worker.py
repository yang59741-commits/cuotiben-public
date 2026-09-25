"""Worker execution loop for durable background jobs.

The coordinator remains the compatibility facade.  This module owns handler
execution, lease-heartbeat lifetime, outcome mapping, and the long-running
poll loop without knowing how job state is stored.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import replace
from typing import Any, Callable, Protocol

from .job_contracts import (
    TERMINAL_STATUSES,
    DeferredJobError,
    JobCancelled,
    JobError,
    JobLeaseLost,
    JobOutcome,
    PermanentJobError,
    RetryableJobError,
    WorkerStopRequested,
)
from .exam_time_policy import ExamDeadlineExceeded


# Preserve the long-standing diagnostic namespace exposed by the jobs facade.
logger = logging.getLogger("cuoti_app.services.jobs")


class WorkerCoordinator(Protocol):
    worker_id: str
    lease_seconds: float
    lane: str | None
    handlers: dict[str, Callable[..., Any]]

    def claim_next(self) -> dict[str, Any] | None: ...
    def heartbeat(self, job_id: str, *, lease_attempt: int | None = None) -> bool: ...
    def get_input(self, job_id: str) -> dict[str, Any]: ...
    def get_job(self, job_id: str) -> dict[str, Any]: ...
    def cleanup_staged_input(self, job_id: str) -> int: ...
    def cleanup_retention(self) -> dict[str, int]: ...
    def run_once(self) -> dict[str, Any] | None: ...
    def _notify_terminal(self, job: dict[str, Any]) -> None: ...
    def _finish_cancelled(
        self,
        job_id: str,
        *,
        lease_attempt: int | None = None,
        timing: Any = None,
    ) -> None: ...
    def _finish_deferred(self, job_id: str, **kwargs: Any) -> None: ...
    def _finish_failure(self, job_id: str, **kwargs: Any) -> None: ...
    def _finish_success(
        self,
        job_id: str,
        outcome: JobOutcome,
        *,
        lease_attempt: int | None = None,
    ) -> None: ...


def execute_next(
    coordinator: WorkerCoordinator,
    context_factory: Callable[[WorkerCoordinator, str, int, bool], Any],
) -> dict[str, Any] | None:
    job = coordinator.claim_next()
    if job is None:
        return None
    job_id = job["id"]
    lease_attempt = int(job["attempts"])
    handler = coordinator.handlers.get(job["kind"])
    if handler is None:
        coordinator._finish_failure(
            job_id,
            code="handler_missing",
            message="当前进程未注册该任务处理器",
            category="configuration",
        )
        finished = coordinator.get_job(job_id)
        if finished["status"] in TERMINAL_STATUSES:
            coordinator._notify_terminal(finished)
            coordinator.cleanup_staged_input(job_id)
        return finished
    receipt_only = job["status"] == "published_pending_receipt"
    context = context_factory(coordinator, job_id, lease_attempt, receipt_only)
    heartbeat_stop = threading.Event()

    def maintain_lease() -> None:
        interval = max(0.25, coordinator.lease_seconds / 3)
        while not heartbeat_stop.wait(interval):
            try:
                coordinator.heartbeat(job_id, lease_attempt=lease_attempt)
            except JobError:
                return
            except sqlite3.OperationalError:
                # A short-lived business transaction may temporarily own
                # SQLite's single writer slot; retry on the next heartbeat.
                continue

    heartbeat_thread = threading.Thread(target=maintain_lease, daemon=True)
    heartbeat_thread.start()

    def exam_attempt_timing():
        if job["kind"] != "exam-generation":
            return None
        finish = getattr(context, "finish_exam_timing", None)
        return finish() if callable(finish) else None

    outcome: JobOutcome | None = None
    try:
        if receipt_only:
            # Native publication is already an irreversible business fact.
            # Receipt recovery must still honor worker shutdown and attempt
            # ownership, but a stale pre-publication cancel/deadline fence can
            # no longer prevent the receipt-only handler from repairing it.
            context.raise_if_worker_stopping()
            context.assert_lease()
        else:
            context.raise_if_cancelled()
        raw_outcome = handler(context, coordinator.get_input(job_id))
        outcome = (
            raw_outcome
            if isinstance(raw_outcome, JobOutcome)
            else JobOutcome(result=raw_outcome)
        )
        if job["kind"] == "exam-generation":
            outcome = replace(outcome, timing=exam_attempt_timing())
        if not receipt_only:
            context.raise_if_worker_stopping()
        if job["kind"] == "exam-generation" and not outcome.side_effect_committed:
            context.raise_if_deadline_exceeded(stage="receipt_and_finalization")
        if not outcome.side_effect_committed:
            context.raise_if_cancelled()
        coordinator._finish_success(
            job_id, outcome, lease_attempt=lease_attempt
        )
    except JobLeaseLost:
        logger.warning(
            "Background job lease lost job_id=%s attempt=%s worker_id=%s",
            job_id,
            lease_attempt,
            coordinator.worker_id,
        )
    except WorkerStopRequested:
        logger.info(
            "Background job interrupted by worker stop job_id=%s attempt=%s "
            "worker_id=%s",
            job_id,
            lease_attempt,
            coordinator.worker_id,
        )
    except JobCancelled:
        try:
            coordinator._finish_cancelled(
                job_id,
                lease_attempt=lease_attempt,
                timing=exam_attempt_timing(),
            )
        except JobLeaseLost:
            logger.warning(
                "Background job cancellation lost lease job_id=%s attempt=%s",
                job_id,
                lease_attempt,
            )
    except DeferredJobError as exc:
        try:
            coordinator._finish_deferred(
                job_id,
                code=exc.code,
                message=str(exc),
                delay_seconds=exc.delay_seconds,
                details=exc.details,
                timing=exam_attempt_timing(),
                lease_attempt=lease_attempt,
            )
        except JobLeaseLost:
            logger.warning(
                "Background job dependency defer lost lease job_id=%s attempt=%s",
                job_id,
                lease_attempt,
            )
    except RetryableJobError as exc:
        try:
            coordinator._finish_failure(
                job_id,
                code=exc.code,
                message=str(exc),
                retry_delay=exc.delay_seconds,
                category="temporary",
                retryable=True,
                details=exc.details,
                timing=exam_attempt_timing(),
                lease_attempt=lease_attempt,
            )
        except JobLeaseLost:
            logger.warning(
                "Background job retry lost lease job_id=%s attempt=%s",
                job_id,
                lease_attempt,
            )
    except PermanentJobError as exc:
        if (
            isinstance(exc, ExamDeadlineExceeded)
            and outcome is not None
            and callable(outcome.deadline_compensation)
        ):
            try:
                outcome.deadline_compensation()
            except Exception:
                logger.exception(
                    "Deadline compensation failed job_id=%s attempt=%s",
                    job_id,
                    lease_attempt,
                )
        try:
            coordinator._finish_failure(
                job_id,
                code=exc.code,
                message=str(exc),
                category=exc.category,
                details=exc.details,
                timing=exam_attempt_timing(),
                lease_attempt=lease_attempt,
            )
        except JobLeaseLost:
            logger.warning(
                "Background job rejection lost lease job_id=%s attempt=%s",
                job_id,
                lease_attempt,
            )
    except Exception:
        correlation_id = uuid.uuid4().hex
        logger.exception(
            "Unhandled background job failure job_id=%s correlation_id=%s",
            job_id,
            correlation_id,
        )
        try:
            coordinator._finish_failure(
                job_id,
                code="handler_error",
                message="任务执行失败",
                category="internal",
                correlation_id=correlation_id,
                timing=exam_attempt_timing(),
                lease_attempt=lease_attempt,
            )
        except JobLeaseLost:
            logger.warning(
                "Background job failure lost lease job_id=%s attempt=%s",
                job_id,
                lease_attempt,
            )
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=1)
    finished = coordinator.get_job(job_id)
    if finished["status"] in TERMINAL_STATUSES:
        coordinator._notify_terminal(finished)
        coordinator.cleanup_staged_input(job_id)
    return finished


def run_worker_loop(
    coordinator: WorkerCoordinator,
    stop_event: threading.Event,
    *,
    poll_interval: float = 0.25,
    retention_interval: float = 3600,
) -> None:
    next_retention = 0.0
    while not stop_event.is_set():
        monotonic_now = time.monotonic()
        if monotonic_now >= next_retention:
            try:
                coordinator.cleanup_retention()
            except Exception:
                logger.exception(
                    "Background job retention cleanup failed worker_id=%s lane=%s",
                    coordinator.worker_id,
                    coordinator.lane,
                )
            next_retention = monotonic_now + max(60.0, retention_interval)
        try:
            result = coordinator.run_once()
        except Exception:
            correlation_id = uuid.uuid4().hex
            logger.exception(
                "Background worker iteration failed worker_id=%s lane=%s "
                "correlation_id=%s",
                coordinator.worker_id,
                coordinator.lane,
                correlation_id,
            )
            stop_event.wait(max(0.05, min(5.0, poll_interval * 4)))
            continue
        if result is None:
            stop_event.wait(max(0.01, poll_interval))
__all__ = [
    "WorkerCoordinator",
    "WorkerStopRequested",
    "execute_next",
    "run_worker_loop",
]
