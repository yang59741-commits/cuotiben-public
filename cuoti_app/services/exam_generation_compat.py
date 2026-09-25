"""Stage 5/6-R0 compatibility boundaries.

This module is deliberately small: it records identities for the new pipeline,
offers read-only access to historical records, and provides fail-closed fences.
It does not translate legacy scores or selections into new authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

REQUEST_VERSION = "request-v1"
RUBRIC_VERSION = "rubric-v1-candidate-base"
UTILITY_VERSION = "utility-v1-candidate-base"
GRAPH_VERSION = "graph-v1-hard-feasible"
SELECTION_VERSION = "selection-v1-lexicographic"
STAGE7_VERSION = "stage7-v1-candidate"
MEMORY_VERSION = "memory-v1-snapshot"

LEGACY_READ_VERSIONS = frozenset({"legacy", "score-v2", "local-score-v2", "selection-v1"})


@dataclass(frozen=True, slots=True)
class PipelineIdentity:
    request_version: str = REQUEST_VERSION
    rubric_version: str = RUBRIC_VERSION
    utility_version: str = UTILITY_VERSION
    graph_version: str = GRAPH_VERSION
    selection_version: str = SELECTION_VERSION
    stage7_version: str = STAGE7_VERSION
    memory_version: str = MEMORY_VERSION

    def as_dict(self) -> dict[str, str]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def freeze_request(payload: Mapping[str, Any], *, operation_id: str) -> dict[str, Any]:
    """Freeze a request identity without copying mutable content into authority."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("operation_id is required")
    source = payload.get("source", "free")
    if source not in {"free", "mistakes", "past"}:
        raise ValueError("unsupported source")
    if source == "past" and "question_count" in payload:
        raise ValueError("past requests must not provide question_count")
    if source in {"free", "mistakes"}:
        count = payload.get("question_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("free/mistakes requests require explicit question_count")
    return {
        "operation_id": operation_id,
        "source": source,
        "identity": PipelineIdentity().as_dict(),
        "payload": dict(payload),
    }


def read_legacy(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a marked, read-only view of an old record; never re-sign it."""
    result = dict(record)
    result["compatibility"] = "legacy_read_only"
    result["publishable"] = False
    result["converted"] = False
    return result


def unfinished_disposition(record: Mapping[str, Any]) -> str:
    """Classify incomplete work. Published work may only receive receipt recovery."""
    if record.get("publication_state") in {"published", "published_pending_receipt"}:
        return "receipt_only_recovery"
    return "discard_and_requeue"


class PublicationFence:
    """Fail-closed publication guard for candidate/non-publishable pipelines."""

    def __init__(self, *, publishable: bool = False) -> None:
        self.publishable = bool(publishable)

    def assert_publishable(self, candidate: Mapping[str, Any]) -> None:
        if not self.publishable or candidate.get("compatibility") == "legacy_read_only":
            raise RuntimeError("candidate publication is fenced")
        if candidate.get("stage7_version") != STAGE7_VERSION:
            raise RuntimeError("stage7 identity mismatch")


def legacy_past_entry_allowed(*, production: bool = True) -> bool:
    """Legacy past production entry is permanently unreachable in R0."""
    return False


__all__ = [
    "PipelineIdentity", "PublicationFence", "freeze_request", "read_legacy",
    "unfinished_disposition", "legacy_past_entry_allowed",
    "REQUEST_VERSION", "RUBRIC_VERSION", "UTILITY_VERSION", "GRAPH_VERSION",
    "SELECTION_VERSION", "STAGE7_VERSION", "MEMORY_VERSION",
]
