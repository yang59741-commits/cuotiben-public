"""Stable data and error contracts for durable background jobs.

This module has no database, Flask, or worker dependencies.  Keeping these
types at the boundary lets handlers and callers depend on a small contract
without importing the coordinator implementation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


SUPPORTED_JOB_KINDS = frozenset(
    {
        "analysis",
        "classification",
        "ocr",
        "batch-mistakes",
        "exam-generation",
        "exam-recompile",
        "data-import",
        "mistake-export",
        "question-bank-rebuild",
        "semantic-index-refresh",
        "memory-maintenance",
    }
)
TERMINAL_STATUSES = frozenset({"cancelled", "succeeded", "failed"})
ACTIVE_STATUSES = frozenset(
    {"queued", "checkpointed", "running", "cancel_requested", "published_pending_receipt"}
)
JOB_METADATA_RETENTION_DAYS = 30
OCR_RESULT_RETENTION_DAYS = 7
STAGED_INPUT_FIELD = "_staged_input_path"
JOB_LANES = frozenset({"fast", "heavy"})
JOB_KIND_POLICIES = {
    "classification": ("fast", 80),
    "analysis": ("fast", 70),
    "batch-mistakes": ("fast", 60),
    "ocr": ("heavy", 30),
    "data-import": ("heavy", 20),
    "mistake-export": ("heavy", 20),
    "question-bank-rebuild": ("heavy", 15),
    "exam-generation": ("heavy", 10),
    "exam-recompile": ("heavy", 10),
    "semantic-index-refresh": ("heavy", 5),
    "memory-maintenance": ("heavy", 0),
}


class JobError(RuntimeError):
    """Base class for expected coordination failures."""


class UnknownJobKind(JobError):
    pass


class JobNotFound(JobError):
    pass


class JobConflict(JobError):
    pass


class InvalidJobState(JobError):
    pass


class JobLeaseLost(InvalidJobState):
    """The worker attempt no longer owns the job lease."""


class JobCancelled(JobError):
    pass


class WorkerStopRequested(BaseException):
    """Stop the current attempt without broad handler catches hiding it."""


def normalize_error_details(details: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded JSON-safe metadata attached to an expected error."""

    if not isinstance(details, Mapping):
        return {}
    try:
        encoded = json.dumps(details, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 16_000:
            return {}
        normalized = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return normalized if isinstance(normalized, dict) else {}


class DeferredJobError(JobError):
    """Keep a parent job queued while an external durable dependency runs."""

    def __init__(
        self,
        message: str = "任务正在等待依赖完成",
        *,
        code: str = "dependency_pending",
        delay_seconds: float = 1.0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code or "dependency_pending")
        self.delay_seconds = max(0.1, float(delay_seconds))
        self.details = normalize_error_details(details)


class RetryableJobError(JobError):
    def __init__(
        self,
        message: str = "任务暂时失败，将自动重试",
        *,
        code: str = "temporary_failure",
        delay_seconds: float = 0,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.details = normalize_error_details(details)


class PermanentJobError(JobError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "job_rejected",
        category: str = "business",
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.category = category
        self.details = normalize_error_details(details)


@dataclass(frozen=True, slots=True)
class JobOutcome:
    result: Any = None
    exam_ids: tuple[int, ...] = ()
    side_effect_committed: bool = False
    timing: Mapping[str, Any] | None = None
    deadline_compensation: Callable[[], None] | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def canonical_payload(payload: Mapping[str, Any]) -> tuple[str, str]:
    try:
        raw = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except (TypeError, UnicodeError, ValueError) as exc:
        raise ValueError("任务输入必须是可序列化的 JSON 对象") from exc
    return raw, digest


__all__ = [
    "DeferredJobError",
    "ACTIVE_STATUSES",
    "InvalidJobState",
    "JOB_KIND_POLICIES",
    "JOB_LANES",
    "JOB_METADATA_RETENTION_DAYS",
    "JobCancelled",
    "JobConflict",
    "JobError",
    "JobLeaseLost",
    "JobNotFound",
    "JobOutcome",
    "OCR_RESULT_RETENTION_DAYS",
    "PermanentJobError",
    "RetryableJobError",
    "STAGED_INPUT_FIELD",
    "SUPPORTED_JOB_KINDS",
    "TERMINAL_STATUSES",
    "UnknownJobKind",
    "WorkerStopRequested",
    "canonical_payload",
    "normalize_error_details",
    "timestamp",
    "utcnow",
]
