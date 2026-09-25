"""Defensive three-mode memory read/snapshot authority with writes blocked."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence


MEMORY_ACCESS_AUTHORITY_VERSION = "exam-generation-memory-access-authority-v1"
MEMORY_SNAPSHOT_VERSION = "exam-generation-memory-snapshot-v1"
MEMORY_WRITE_DECISION_VERSION = "exam-generation-memory-write-decision-v1"
MEMORY_AUTHORITY_LINEAGE = "ADDITIVE_UNREGISTERED/NON_WRITING"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_READ_MATRIX = {
    "free": ("free", "mistake"),
    "mistakes": ("mistake",),
    "past": ("mistake", "past"),
}
_PAST_FACT_TYPES = {"publication_history_use_fact", "mistake_priority_consumed_fact"}
_FORBIDDEN = {"score", "penalty", "score_identity_hash"}


class MemoryAuthorityError(RuntimeError):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise MemoryAuthorityError("memory_authority_non_canonical_json") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise MemoryAuthorityError(f"memory_authority_{field}_invalid")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or len(value) > 512:
        raise MemoryAuthorityError(f"memory_authority_{field}_invalid")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryAuthorityError(f"memory_authority_{field}_invalid")
    return dict(value)


def _sequence(value: Any, field: str) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MemoryAuthorityError(f"memory_authority_{field}_invalid")
    return list(value)


def _reject_legacy(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(str(key).casefold() in _FORBIDDEN for key in value):
            raise MemoryAuthorityError("memory_authority_legacy_field_forbidden")
        for item in value.values():
            _reject_legacy(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_legacy(item)


def build_memory_access_authority(
    *,
    mode: str,
    subject_id: str,
    profile_identity_hash: str | None,
    execution_kind: str,
) -> dict[str, Any]:
    if mode not in _READ_MATRIX:
        raise MemoryAuthorityError("memory_authority_mode_invalid")
    if execution_kind not in {"production_candidate", "r2_calibration"}:
        raise MemoryAuthorityError("memory_authority_execution_kind_invalid")
    if mode == "past":
        profile = _hash(profile_identity_hash, "profile_identity_hash")
    elif profile_identity_hash is not None:
        raise MemoryAuthorityError("memory_authority_profile_forbidden")
    else:
        profile = None
    body = {
        "version": MEMORY_ACCESS_AUTHORITY_VERSION,
        "lineage": MEMORY_AUTHORITY_LINEAGE,
        "production_activation_allowed": False,
        "mode": mode,
        "subject_id": _text(subject_id, "subject_id"),
        "profile_identity_hash": profile,
        "execution_kind": execution_kind,
        "allowed_read_namespaces": list(_READ_MATRIX[mode]),
        "authoritative_profile_is_memory": False,
        "memory_may_change_hard_graph": False,
        "memory_may_override_explicit_user_settings": False,
        "memory_write_allowed": False,
        "unavailable_or_corrupt_behavior": "fail_closed_policy_unapproved",
    }
    return {**body, "access_authority_hash": _digest(body)}


def validate_memory_access_authority(value: Any, **sources: Any) -> dict[str, Any]:
    authority = _mapping(value, "access_authority")
    expected = build_memory_access_authority(**sources)
    if _canonical(authority) != _canonical(expected):
        raise MemoryAuthorityError("memory_authority_access_invalid")
    return authority


def build_memory_snapshot(
    *,
    access_authority: Mapping[str, Any],
    access_sources: Mapping[str, Any],
    cutoff: Mapping[str, Any],
    memory_events: Sequence[Mapping[str, Any]],
    snapshot_authority_ref: str,
) -> dict[str, Any]:
    access = validate_memory_access_authority(access_authority, **dict(access_sources))
    cutoff_value = _mapping(cutoff, "cutoff")
    if set(cutoff_value) != {"kind", "watermark", "authority_hash"} or cutoff_value["kind"] != "event_watermark":
        raise MemoryAuthorityError("memory_authority_cutoff_shape_invalid")
    if type(cutoff_value["watermark"]) is not int or cutoff_value["watermark"] < 0:
        raise MemoryAuthorityError("memory_authority_cutoff_watermark_invalid")
    normalized_cutoff = {
        "kind": "event_watermark",
        "watermark": cutoff_value["watermark"],
        "authority_hash": _hash(cutoff_value["authority_hash"], "cutoff_authority_hash"),
    }
    allowed = set(access["allowed_read_namespaces"])
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in _sequence(memory_events, "memory_events"):
        item = _mapping(raw, "memory_event")
        if set(item) != {"namespace", "event_id", "event_hash", "event_version", "fact_type", "watermark"}:
            raise MemoryAuthorityError("memory_authority_event_shape_invalid")
        namespace = _text(item["namespace"], "namespace")
        event_id = _text(item["event_id"], "event_id")
        key = (namespace, event_id)
        watermark = item["watermark"]
        if namespace not in allowed or key in seen:
            raise MemoryAuthorityError("memory_authority_event_namespace_invalid")
        if type(watermark) is not int or watermark < 0 or watermark > normalized_cutoff["watermark"]:
            raise MemoryAuthorityError("memory_authority_event_watermark_invalid")
        seen.add(key)
        events.append({
            "namespace": namespace,
            "event_id": event_id,
            "event_hash": _hash(item["event_hash"], "event_hash"),
            "event_version": _text(item["event_version"], "event_version"),
            "fact_type": _text(item["fact_type"], "fact_type"),
            "watermark": watermark,
        })
    events.sort(key=lambda item: (item["namespace"], item["watermark"], item["event_id"]))
    body = {
        "version": MEMORY_SNAPSHOT_VERSION,
        "lineage": MEMORY_AUTHORITY_LINEAGE,
        "production_activation_allowed": False,
        "access_authority_hash": access["access_authority_hash"],
        "mode": access["mode"],
        "subject_id": access["subject_id"],
        "profile_identity_hash": access["profile_identity_hash"],
        "execution_kind": access["execution_kind"],
        "allowed_read_namespaces": access["allowed_read_namespaces"],
        "cutoff": normalized_cutoff,
        "memory_events": events,
        "event_count": len(events),
        "snapshot_authority_ref": _text(snapshot_authority_ref, "snapshot_authority_ref"),
        "snapshot_mutable": False,
    }
    _reject_legacy(body)
    return {**body, "memory_snapshot_hash": _digest(body)}


def validate_memory_snapshot(value: Any, **sources: Any) -> dict[str, Any]:
    snapshot = _mapping(value, "memory_snapshot")
    expected = build_memory_snapshot(**sources)
    if _canonical(snapshot) != _canonical(expected):
        raise MemoryAuthorityError("memory_authority_snapshot_invalid")
    return snapshot


def build_memory_write_decision(
    *,
    memory_snapshot: Mapping[str, Any],
    snapshot_sources: Mapping[str, Any],
    requested_fact_types: Sequence[str],
    trigger: str,
    publication_authority: Mapping[str, Any] | None,
    decision_authority_ref: str,
) -> dict[str, Any]:
    snapshot = validate_memory_snapshot(memory_snapshot, **dict(snapshot_sources))
    facts = sorted({_text(value, "fact_type") for value in _sequence(requested_fact_types, "requested_fact_types")})
    if len(facts) != len(requested_fact_types):
        raise MemoryAuthorityError("memory_authority_fact_type_duplicate")
    execution_kind, mode = snapshot["execution_kind"], snapshot["mode"]
    publication = None
    blockers: list[str]
    if execution_kind == "r2_calibration":
        if facts or publication_authority is not None or trigger != "calibration_run":
            raise MemoryAuthorityError("memory_authority_calibration_write_request_forbidden")
        blockers = ["calibration_memory_write_forbidden"]
        status = "calibration_zero_write"
    elif mode in {"free", "mistakes"}:
        if publication_authority is not None:
            raise MemoryAuthorityError("memory_authority_publication_authority_forbidden")
        blockers = ["memory_namespace_write_policy_unapproved"]
        status = "blocked_write_policy_unapproved" if facts else "no_write_requested"
    else:
        if any(fact not in _PAST_FACT_TYPES for fact in facts):
            raise MemoryAuthorityError("memory_authority_past_fact_type_invalid")
        if facts:
            if trigger != "native_publication_transaction_committed":
                raise MemoryAuthorityError("memory_authority_past_trigger_invalid")
            publication_value = _mapping(publication_authority, "publication_authority")
            if set(publication_value) != {"job_id", "exam_id", "publication_hash", "commit_authority_hash"}:
                raise MemoryAuthorityError("memory_authority_publication_shape_invalid")
            if type(publication_value["exam_id"]) is not int or publication_value["exam_id"] <= 0:
                raise MemoryAuthorityError("memory_authority_exam_id_invalid")
            publication = {
                "job_id": _text(publication_value["job_id"], "job_id"),
                "exam_id": publication_value["exam_id"],
                "publication_hash": _hash(publication_value["publication_hash"], "publication_hash"),
                "commit_authority_hash": _hash(publication_value["commit_authority_hash"], "commit_authority_hash"),
            }
            blockers = ["past_memory_fact_schema_unapproved", "m_once_identity_and_cas_unapproved"]
            status = "blocked_fact_schema_unapproved"
        else:
            if publication_authority is not None:
                raise MemoryAuthorityError("memory_authority_unused_publication_authority")
            blockers = []
            status = "no_write_requested"
    body = {
        "version": MEMORY_WRITE_DECISION_VERSION,
        "lineage": MEMORY_AUTHORITY_LINEAGE,
        "production_activation_allowed": False,
        "memory_snapshot_hash": snapshot["memory_snapshot_hash"],
        "mode": mode,
        "execution_kind": execution_kind,
        "trigger": _text(trigger, "trigger"),
        "requested_fact_types": facts,
        "publication_authority": publication,
        "decision_status": status,
        "blocker_codes": blockers,
        "memory_write_allowed": False,
        "history_use_write_allowed": False,
        "priority_consumed_write_allowed": False,
        "m_once_state_change_allowed": False,
        "decision_authority_ref": _text(decision_authority_ref, "decision_authority_ref"),
    }
    _reject_legacy(body)
    return {**body, "write_decision_hash": _digest(body)}


def validate_memory_write_decision(value: Any, **sources: Any) -> dict[str, Any]:
    decision = _mapping(value, "write_decision")
    expected = build_memory_write_decision(**sources)
    if _canonical(decision) != _canonical(expected):
        raise MemoryAuthorityError("memory_authority_write_decision_invalid")
    return decision


def reject_memory_authority_for_production_write(value: Any) -> None:
    artifact = _mapping(value, "memory_artifact")
    if artifact.get("lineage") == MEMORY_AUTHORITY_LINEAGE or artifact.get("memory_write_allowed") is False:
        raise MemoryAuthorityError("memory_authority_non_writing")
    raise MemoryAuthorityError("memory_authority_artifact_invalid")


__all__ = [
    "MEMORY_ACCESS_AUTHORITY_VERSION", "MEMORY_AUTHORITY_LINEAGE", "MEMORY_SNAPSHOT_VERSION",
    "MEMORY_WRITE_DECISION_VERSION", "MemoryAuthorityError", "build_memory_access_authority",
    "build_memory_snapshot", "build_memory_write_decision", "reject_memory_authority_for_production_write",
    "validate_memory_access_authority", "validate_memory_snapshot", "validate_memory_write_decision",
]
