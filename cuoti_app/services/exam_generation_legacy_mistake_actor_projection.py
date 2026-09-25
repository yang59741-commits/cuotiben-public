"""Read-only classification and explicit actor projection for legacy mistake events."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .exam_generation_mode_memory_authority import MemoryAuthorityError


LEGACY_MISTAKE_ACTOR_PROJECTION_VERSION = "legacy-mistake-actor-projection-v1"
LEGACY_MISTAKE_ACTOR_CLASSIFIER_VERSION = "legacy-mistake-actor-classifier-v1"
_DAILY_OPERATION = re.compile(r"daily-(\d{4}-\d{2}-\d{2})\Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _classification(event: Mapping[str, Any]) -> tuple[str, str | None]:
    actor = str(event.get("actor") or "").strip()
    if actor:
        return "actor_already_present", None
    kind = str(event.get("event_kind") or event.get("event_type") or "").strip()
    result = event.get("observation") or event.get("result") or {}
    operation = str(event.get("operation_id") or "").strip()
    date_scope = str(event.get("date_scope") or "").strip()
    daily_match = _DAILY_OPERATION.fullmatch(operation)
    questions = event.get("questions")
    if (
        kind == "analysis"
        and daily_match is not None
        and (not date_scope or daily_match.group(1) == date_scope)
        and isinstance(result, Mapping)
        and (event.get("source") == "daily_analysis" or result.get("source") == "daily_analysis")
        and (
            result.get("question_count") is None
            or (
                type(result.get("question_count")) is int
                and result["question_count"] >= 0
                and isinstance(questions, list)
                and result["question_count"] == len(questions)
            )
        )
        and (
            questions is None
            or (
                isinstance(questions, list)
                and all(isinstance(item, Mapping) and str(item.get("question_id") or "") for item in questions)
            )
        )
    ):
        return "daily_analysis_signature_v1", "mistake_analysis"
    if kind == "result" and isinstance(result, Mapping):
        exam_id = result.get("exam_id")
        paper_index = event.get("paper_index")
        event_id = str(event.get("event_id") or "")
        source = event.get("source") or result.get("source")
        business_result = event.get("business_result")
        if (
            operation
            and type(paper_index) is int
            and paper_index > 0
            and event_id == f"{operation}:result:{paper_index}"
            and type(exam_id) is int
            and exam_id > 0
            and source == "mistakes"
            and business_result in {None, "", "published"}
        ):
            return "mistake_exam_published_result_signature_v1", "mistake_exam"
    return "insufficient_source_evidence", None


def build_legacy_mistake_actor_projection(
    *, events: Sequence[Mapping[str, Any]], projection_authority_ref: str
) -> dict[str, Any]:
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise MemoryAuthorityError("legacy_actor_projection_events_invalid")
    rows = []
    seen = set()
    event_values = [dict(raw) if isinstance(raw, Mapping) else raw for raw in events]
    for raw in event_values:
        if not isinstance(raw, Mapping):
            raise MemoryAuthorityError("legacy_actor_projection_event_invalid")
        event = dict(raw)
        event_id = str(event.get("event_id") or "").strip()
        if not event_id or event_id in seen:
            raise MemoryAuthorityError("legacy_actor_projection_event_identity_invalid")
        seen.add(event_id)
        rule, projected = _classification(event)
        rows.append({
            "event_id": event_id,
            "source_event_hash": _digest(event),
            "classification_rule": rule,
            "projected_actor": projected,
            "preference_eligible": projected == "mistake_analysis",
            "operation_id": str(event.get("operation_id") or ""),
            "source_schema_version": event.get("schema_version"),
            "classification_status": "proven" if projected else "audit_only",
        })
    proven_result_operations = {
        str(event.get("operation_id") or "")
        for event, row in zip(event_values, rows)
        if isinstance(event, Mapping)
        and row["classification_rule"] == "mistake_exam_published_result_signature_v1"
    }
    for event, row in zip(event_values, rows):
        if not isinstance(event, Mapping) or row["classification_rule"] != "insufficient_source_evidence":
            continue
        operation = str(event.get("operation_id") or "")
        event_id = str(event.get("event_id") or "")
        kind = str(event.get("event_kind") or event.get("event_type") or "")
        if kind == "request" and operation in proven_result_operations and event_id == f"{operation}:request":
            row["classification_rule"] = "mistake_exam_request_with_proven_result_v1"
            row["projected_actor"] = "mistake_exam"
            row["classification_status"] = "proven"
    rows.sort(key=lambda item: item["event_id"])
    body = {
        "version": LEGACY_MISTAKE_ACTOR_PROJECTION_VERSION,
        "namespace": "mistakes",
        "projection_kind": "read_only_actor_compatibility",
        "classifier_version": LEGACY_MISTAKE_ACTOR_CLASSIFIER_VERSION,
        "source_event_catalog_hash": _digest({"events": sorted((dict(item) for item in event_values), key=lambda item: str(item.get("event_id") or ""))}),
        "event_count": len(rows),
        "projected_preference_count": sum(item["preference_eligible"] for item in rows),
        "projected_exam_audit_count": sum(item["projected_actor"] == "mistake_exam" for item in rows),
        "audit_only_count": sum(item["projected_actor"] is None for item in rows),
        "classifications": rows,
        "source_events_mutated": False,
        "production_memory_write_allowed": False,
        "projection_authority_ref": str(projection_authority_ref or "").strip(),
    }
    if not body["projection_authority_ref"]:
        raise MemoryAuthorityError("legacy_actor_projection_authority_ref_invalid")
    return {**body, "projection_receipt_hash": _digest(body)}


def validate_legacy_mistake_actor_projection(value: Any, **sources: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryAuthorityError("legacy_actor_projection_invalid")
    expected = build_legacy_mistake_actor_projection(**sources)
    if _canonical(value) != _canonical(expected):
        raise MemoryAuthorityError("legacy_actor_projection_invalid")
    return dict(value)


def projected_actor_map(value: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(item["event_id"]): str(item["projected_actor"])
        for item in value.get("classifications") or []
        if isinstance(item, Mapping) and item.get("projected_actor")
    }


__all__ = [
    "LEGACY_MISTAKE_ACTOR_CLASSIFIER_VERSION", "LEGACY_MISTAKE_ACTOR_PROJECTION_VERSION", "build_legacy_mistake_actor_projection",
    "projected_actor_map", "validate_legacy_mistake_actor_projection",
]
