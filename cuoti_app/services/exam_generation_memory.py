"""Success-only AI-memory contracts for exam generation."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .job_contracts import JobError


def question_fingerprint(content: object) -> str:
    """Return the stable whitespace-insensitive exam-memory fingerprint."""

    normalized = re.sub(r"\s+", "", str(content or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def pending_request_event(
    payload: Mapping[str, Any], operation_id: str
) -> dict[str, Any]:
    return {
        "event_id": f"{operation_id}:request",
        "event_type": "request",
        "operation_id": operation_id,
        "request": {
            key: value
            for key, value in payload.items()
            if key not in {"_job_id", "content"}
        },
    }


def append_success_events(
    memory,
    memory_type: str,
    actor: str,
    events: Sequence[Mapping[str, Any]],
    *,
    logger,
) -> None:
    """Best-effort memory persistence after every exam output is published."""

    try:
        memory.append_events(memory_type, actor, events)
    except JobError:
        raise
    except Exception:
        try:
            logger.exception("failed to write exam AI memory")
        except Exception:
            pass


__all__ = ["append_success_events", "pending_request_event", "question_fingerprint"]
