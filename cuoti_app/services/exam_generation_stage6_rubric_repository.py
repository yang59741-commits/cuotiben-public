"""Additive persistence seam for candidate-base rubric receipts.

This module deliberately does not alter v44 tables or migrate legacy score
records.  Deployments may create the two immutable tables in a later schema
revision; until then callers can use the pure validation helpers and remain
blocked closed.  The rows are keyed by candidate-base identity (never slot or
window) and graph utility rows bind the active graph, policy and rejection
ledger head.
"""
from __future__ import annotations

import hashlib, json, re
from typing import Any, Mapping, Sequence
from .exam_generation_stage6_rubric import (
    RUBRIC_CATALOG_VERSION, FACTOR_CATALOG_HASH, FACTOR_APPLICABILITY_VERSION,
    RUBRIC_RESPONSE_VERSION, UTILITY_POLICY_VERSION, UTILITY_RECEIPT_VERSION,
    build_rubric_request, parse_rubric_response,
    validate_candidate_base_consistency, candidate_base_key as rubric_candidate_base_key,
)

RUBRIC_RECEIPTS = "exam_generation_stage6_candidate_base_rubric_receipts"
UTILITY_RECEIPTS = "exam_generation_stage6_graph_utility_receipts"
RUBRIC_REPOSITORY_VERSION = "past-v3-stage6-rubric-repository-v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")

class Stage6RubricRepositoryError(RuntimeError):
    pass

def _canonical(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def _digest(v: Any) -> str:
    return hashlib.sha256(_canonical(v).encode()).hexdigest()

def _hash(v: Any, field: str) -> str:
    if not isinstance(v, str) or _HASH.fullmatch(v) is None:
        raise Stage6RubricRepositoryError(f"{field}_invalid")
    return v


def _text(v: Any, field: str) -> str:
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > 512:
        raise Stage6RubricRepositoryError(f"{field}_invalid")
    return v.strip()

def _payload(value: Any, field: str) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise Stage6RubricRepositoryError(f"{field}_invalid")
    return dict(value)

RUBRIC_RECEIPT_VERSION = "candidate-base-rubric-receipt-v1"
RUBRIC_RECEIPT_V2_VERSION = "candidate-base-rubric-receipt-v2"
RUBRIC_MEMBER_RECEIPT_VERSION = "candidate-base-rubric-member-receipt-v1"
RUBRIC_SET_RECEIPT_VERSION = "candidate-base-rubric-set-receipt-v1"
GRAPH_UTILITY_RECEIPT_V2_VERSION = "candidate-base-graph-utility-receipt-v2"

_CANONICAL_MEMBER_RUBRIC_FIELDS = {
    "candidate_id", "candidate_fact_hash", "content_hash",
    "content_snapshot_hash", "material_identity", "catalog_version",
    "catalog_hash", "factor_applicability_version", "factors",
}


def _canonical_member_rubric(value: Any) -> dict[str, Any]:
    """Validate then remove transport-only response identity from a rubric."""

    rubric = _payload(value, "rubric_member")
    request = build_rubric_request(rubric)
    if set(rubric) == _CANONICAL_MEMBER_RUBRIC_FIELDS:
        response = {
            "version": RUBRIC_RESPONSE_VERSION,
            "request_hash": request["request_hash"],
            **rubric,
        }
    elif set(rubric) == _CANONICAL_MEMBER_RUBRIC_FIELDS | {
        "version", "request_hash"
    }:
        response = rubric
    else:
        raise Stage6RubricRepositoryError("rubric_member_invalid")
    try:
        parsed = parse_rubric_response(response, request=request)
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_member_invalid") from exc
    return {field: parsed[field] for field in _CANONICAL_MEMBER_RUBRIC_FIELDS}

def build_candidate_base_rubric_receipt(rubrics: Any, *, visibility_hash: str) -> dict[str, Any]:
    """Build an additive v2 receipt bound to the complete visibility sidecar."""
    if not isinstance(rubrics, (list, tuple)) or not rubrics:
        raise Stage6RubricRepositoryError("rubric_receipt_candidate_invalid")
    _hash(visibility_hash, "visibility_hash")
    # Reuse the canonical parser/consistency checks before wrapping.
    validate_candidate_base_consistency(rubrics)
    body = {
        "version": RUBRIC_RECEIPT_V2_VERSION,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "visibility_hash": visibility_hash,
        "rubrics": [dict(item) for item in rubrics],
    }
    return {**body, "receipt_hash": _digest(body)}

def validate_rubric_receipt(receipt: Any) -> dict[str, Any]:
    value = _payload(receipt, "rubric_receipt")
    required = {"version", "catalog_version", "catalog_hash", "factor_applicability_version", "rubrics", "receipt_hash"}
    version = value.get("version")
    required = required | ({"visibility_hash"} if version == RUBRIC_RECEIPT_V2_VERSION else set())
    if set(value) != required or version not in (RUBRIC_RECEIPT_VERSION, RUBRIC_RECEIPT_V2_VERSION) or not isinstance(value.get("rubrics"), list) or not value["rubrics"]:
        raise Stage6RubricRepositoryError("rubric_receipt_shape_invalid")
    if version == RUBRIC_RECEIPT_V2_VERSION:
        _hash(value.get("visibility_hash"), "visibility_hash")
    if value["catalog_version"] != RUBRIC_CATALOG_VERSION or value["catalog_hash"] != FACTOR_CATALOG_HASH or value["factor_applicability_version"] != FACTOR_APPLICABILITY_VERSION:
        raise Stage6RubricRepositoryError("rubric_receipt_catalog_invalid")
    _hash(value["catalog_hash"], "catalog_hash"); _hash(value["receipt_hash"], "receipt_hash")
    if _digest({k: value[k] for k in value if k != "receipt_hash"}) != value["receipt_hash"]:
        raise Stage6RubricRepositoryError("rubric_receipt_hash_mismatch")
    try:
        consistent = validate_candidate_base_consistency(value["rubrics"])
        if len(consistent) != len(value["rubrics"]):
            raise Stage6RubricRepositoryError("rubric_duplicate_candidate")
        for rubric in value["rubrics"]:
            request = build_rubric_request(rubric)
            parse_rubric_response(rubric, request=request)
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_receipt_candidate_invalid") from exc
    return value


def build_candidate_base_rubric_member_receipt(
    rubric: Mapping[str, Any], *, visibility_hash: str
) -> dict[str, Any]:
    """Build one immutable row-level rubric receipt for a future FK target."""

    _hash(visibility_hash, "visibility_hash")
    value = _canonical_member_rubric(rubric)
    try:
        base = list(rubric_candidate_base_key(value))
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_member_invalid") from exc
    body = {
        "version": RUBRIC_MEMBER_RECEIPT_VERSION,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "visibility_hash": visibility_hash,
        "candidate_base": base,
        "rubric": value,
    }
    return {**body, "receipt_hash": _digest(body)}


def validate_candidate_base_rubric_member_receipt(receipt: Any) -> dict[str, Any]:
    value = _payload(receipt, "rubric_member_receipt")
    expected = {
        "version", "catalog_version", "catalog_hash",
        "factor_applicability_version", "visibility_hash", "candidate_base",
        "rubric", "receipt_hash",
    }
    if (
        set(value) != expected
        or value.get("version") != RUBRIC_MEMBER_RECEIPT_VERSION
        or value.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or value.get("catalog_hash") != FACTOR_CATALOG_HASH
        or value.get("factor_applicability_version")
        != FACTOR_APPLICABILITY_VERSION
    ):
        raise Stage6RubricRepositoryError("rubric_member_shape_invalid")
    _hash(value.get("visibility_hash"), "visibility_hash")
    _hash(value.get("receipt_hash"), "receipt_hash")
    if _digest({key: value[key] for key in value if key != "receipt_hash"}) != value["receipt_hash"]:
        raise Stage6RubricRepositoryError("rubric_member_hash_mismatch")
    try:
        rubric = _canonical_member_rubric(value.get("rubric"))
        base = list(rubric_candidate_base_key(rubric))
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_member_invalid") from exc
    if value.get("candidate_base") != base or value.get("rubric") != rubric:
        raise Stage6RubricRepositoryError("rubric_member_identity_mismatch")
    return value


def build_candidate_base_rubric_set_receipt(
    member_receipts: Sequence[Mapping[str, Any]], *, visibility_hash: str
) -> dict[str, Any]:
    """Build the aggregate receipt that graph utility v2 can reference by FK."""

    _hash(visibility_hash, "visibility_hash")
    if not isinstance(member_receipts, Sequence) or isinstance(member_receipts, (str, bytes)) or not member_receipts:
        raise Stage6RubricRepositoryError("rubric_set_members_invalid")
    members: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for raw in member_receipts:
        member = validate_candidate_base_rubric_member_receipt(raw)
        if member["visibility_hash"] != visibility_hash:
            raise Stage6RubricRepositoryError("rubric_set_visibility_mismatch")
        base = tuple(member["candidate_base"])
        if base in seen:
            raise Stage6RubricRepositoryError("rubric_set_member_duplicate")
        seen.add(base)
        members.append({
            "candidate_base": list(base),
            "member_receipt_hash": member["receipt_hash"],
        })
    members.sort(key=lambda item: tuple(item["candidate_base"]))
    body = {
        "version": RUBRIC_SET_RECEIPT_VERSION,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "visibility_hash": visibility_hash,
        "member_count": len(members),
        "members": members,
    }
    return {**body, "receipt_hash": _digest(body)}


def validate_candidate_base_rubric_set_receipt(
    receipt: Any, *, member_receipts: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    value = _payload(receipt, "rubric_set_receipt")
    expected = {
        "version", "catalog_version", "catalog_hash",
        "factor_applicability_version", "visibility_hash", "member_count",
        "members", "receipt_hash",
    }
    if (
        set(value) != expected
        or value.get("version") != RUBRIC_SET_RECEIPT_VERSION
        or value.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or value.get("catalog_hash") != FACTOR_CATALOG_HASH
        or value.get("factor_applicability_version")
        != FACTOR_APPLICABILITY_VERSION
        or type(value.get("member_count")) is not int
        or value["member_count"] <= 0
        or not isinstance(value.get("members"), list)
        or len(value["members"]) != value["member_count"]
    ):
        raise Stage6RubricRepositoryError("rubric_set_shape_invalid")
    _hash(value.get("visibility_hash"), "visibility_hash")
    _hash(value.get("receipt_hash"), "receipt_hash")
    if _digest({key: value[key] for key in value if key != "receipt_hash"}) != value["receipt_hash"]:
        raise Stage6RubricRepositoryError("rubric_set_hash_mismatch")
    rebuilt = build_candidate_base_rubric_set_receipt(
        member_receipts, visibility_hash=value["visibility_hash"]
    )
    if _canonical(rebuilt) != _canonical(value):
        raise Stage6RubricRepositoryError("rubric_set_member_binding_mismatch")
    return value

def validate_graph_utility_receipt(receipt: Any, *, active_graph_hash: str, rejection_ledger_head_hash: str, policy_hash: str) -> dict[str, Any]:
    value = _payload(receipt, "utility_receipt")
    expected = {
        "version", "graph_hash", "constraint_hash", "candidate_facts_hash",
        "projection_identity_hash", "catalog_version", "catalog_hash",
        "factor_applicability_version", "parameter_set_hash", "policy_version",
        "policy_hash", "diversity_policy_hash", "rejection_ledger_head_hash",
        "rejected_edges", "rubric_receipt_hash", "utilities", "receipt_hash",
    }
    if (
        set(value) != expected
        or value.get("version") != UTILITY_RECEIPT_VERSION
        or value.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or value.get("catalog_hash") != FACTOR_CATALOG_HASH
        or value.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION
        or value.get("policy_version") != UTILITY_POLICY_VERSION
        or not isinstance(value.get("utilities"), list)
        or not isinstance(value.get("rejected_edges"), list)
    ):
        raise Stage6RubricRepositoryError("utility_receipt_shape_invalid")

    def forbidden(v: Any) -> bool:
        if isinstance(v, Mapping):
            return any(str(k).casefold() in {"score", "penalty"} or forbidden(item) for k, item in v.items())
        return any(forbidden(item) for item in v) if isinstance(v, list) else False

    if forbidden(value):
        raise Stage6RubricRepositoryError("utility_forbidden_legacy_field")

    # Every identity/hash field is part of the authenticated receipt body.
    for field in (
        "graph_hash", "constraint_hash", "candidate_facts_hash",
        "projection_identity_hash", "parameter_set_hash", "policy_hash",
        "rejection_ledger_head_hash", "rubric_receipt_hash", "receipt_hash",
    ):
        _hash(value.get(field), field)
    if value.get("diversity_policy_hash") is not None:
        _hash(value.get("diversity_policy_hash"), "diversity_policy_hash")
    _hash(active_graph_hash, "active_graph_hash")
    _hash(rejection_ledger_head_hash, "rejection_ledger_head_hash")
    _hash(policy_hash, "policy_hash")
    if (
        value["graph_hash"] != active_graph_hash
        or value["rejection_ledger_head_hash"] != rejection_ledger_head_hash
        or value["policy_hash"] != policy_hash
    ):
        raise Stage6RubricRepositoryError("utility_receipt_binding_mismatch")

    rejected: set[tuple[str, str]] = set()
    for item in value["rejected_edges"]:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id"}:
            raise Stage6RubricRepositoryError("utility_rejected_edge_invalid")
        pair = (_text(item["slot_id"], "rejected_slot_id"), _text(item["candidate_id"], "rejected_candidate_id"))
        if pair in rejected:
            raise Stage6RubricRepositoryError("utility_rejected_edge_duplicate")
        rejected.add(pair)
    zero_hash = "0" * 64
    if (rejected and value["rejection_ledger_head_hash"] == zero_hash) or (not rejected and value["rejection_ledger_head_hash"] != zero_hash):
        raise Stage6RubricRepositoryError("utility_rejection_ledger_invalid")

    seen: set[tuple[str, str]] = set()
    for item in value["utilities"]:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"slot_id", "candidate_id", "candidate_base", "utility"}
            or type(item.get("utility")) is not int
        ):
            raise Stage6RubricRepositoryError("utility_entry_invalid")
        slot_id = _text(item["slot_id"], "utility_slot_id")
        candidate_id = _text(item["candidate_id"], "utility_candidate_id")
        pair = (slot_id, candidate_id)
        if pair in rejected:
            raise Stage6RubricRepositoryError("utility_entry_rejected")
        if pair in seen:
            raise Stage6RubricRepositoryError("utility_duplicate_entry")
        seen.add(pair)

        base = item["candidate_base"]
        if not isinstance(base, list) or len(base) != 8:
            raise Stage6RubricRepositoryError("utility_candidate_base_invalid")
        try:
            normalized_base = rubric_candidate_base_key({
                "candidate_id": base[0],
                "candidate_fact_hash": base[1],
                "content_hash": base[2],
                "content_snapshot_hash": base[3] or None,
                "material_identity": base[4] or None,
                "catalog_version": base[5],
                "catalog_hash": base[6],
                "factor_applicability_version": base[7],
            })
        except Exception as exc:
            raise Stage6RubricRepositoryError("utility_candidate_base_invalid") from exc
        if list(normalized_base) != base or base[0] != candidate_id:
            raise Stage6RubricRepositoryError("utility_candidate_base_invalid")

    if _digest({k: value[k] for k in value if k != "receipt_hash"}) != value["receipt_hash"]:
        raise Stage6RubricRepositoryError("utility_receipt_hash_mismatch")
    return value


def _validated_rubric_member_index(
    member_receipts: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, ...], dict[str, Any]]:
    if not isinstance(member_receipts, Sequence) or isinstance(member_receipts, (str, bytes)) or not member_receipts:
        raise Stage6RubricRepositoryError("rubric_set_members_invalid")
    members: dict[tuple[str, ...], dict[str, Any]] = {}
    for raw in member_receipts:
        member = validate_candidate_base_rubric_member_receipt(raw)
        base = tuple(member["candidate_base"])
        if base in members:
            raise Stage6RubricRepositoryError("rubric_set_member_duplicate")
        members[base] = member
    return members


def _legacy_rubric_aggregate_hash(
    members: Mapping[tuple[str, ...], Mapping[str, Any]],
) -> str:
    try:
        consistent = validate_candidate_base_consistency(
            [member["rubric"] for member in members.values()]
        )
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_set_member_invalid") from exc
    return _digest({
        "rubrics": [consistent[key] for key in sorted(consistent)]
    })


def build_graph_utility_receipt_v2(
    candidate_utility_receipt: Any,
    *,
    rubric_set_receipt: Any,
    member_receipts: Sequence[Mapping[str, Any]],
    active_graph_hash: str,
    rejection_ledger_head_hash: str,
    policy_hash: str,
) -> dict[str, Any]:
    """Replace the non-row v1 rubric hash with an actual rubric-set FK target."""

    utility = validate_graph_utility_receipt(
        candidate_utility_receipt,
        active_graph_hash=active_graph_hash,
        rejection_ledger_head_hash=rejection_ledger_head_hash,
        policy_hash=policy_hash,
    )
    members = _validated_rubric_member_index(member_receipts)
    rubric_set = validate_candidate_base_rubric_set_receipt(
        rubric_set_receipt, member_receipts=list(members.values())
    )
    if utility["rubric_receipt_hash"] != _legacy_rubric_aggregate_hash(members):
        raise Stage6RubricRepositoryError("utility_rubric_set_source_mismatch")
    utility_bases = {tuple(item["candidate_base"]) for item in utility["utilities"]}
    if utility_bases != set(members):
        raise Stage6RubricRepositoryError("utility_rubric_set_coverage_mismatch")
    body = {
        key: value
        for key, value in utility.items()
        if key not in {"version", "rubric_receipt_hash", "receipt_hash"}
    }
    body["version"] = GRAPH_UTILITY_RECEIPT_V2_VERSION
    body["rubric_set_receipt_hash"] = rubric_set["receipt_hash"]
    return validate_graph_utility_receipt_v2(
        {**body, "receipt_hash": _digest(body)},
        rubric_set_receipt=rubric_set,
        member_receipts=list(members.values()),
        active_graph_hash=active_graph_hash,
        rejection_ledger_head_hash=rejection_ledger_head_hash,
        policy_hash=policy_hash,
    )


def validate_graph_utility_receipt_v2(
    receipt: Any,
    *,
    rubric_set_receipt: Any,
    member_receipts: Sequence[Mapping[str, Any]],
    active_graph_hash: str,
    rejection_ledger_head_hash: str,
    policy_hash: str,
) -> dict[str, Any]:
    value = _payload(receipt, "utility_receipt_v2")
    expected = {
        "version", "graph_hash", "constraint_hash", "candidate_facts_hash",
        "projection_identity_hash", "catalog_version", "catalog_hash",
        "factor_applicability_version", "parameter_set_hash", "policy_version",
        "policy_hash", "diversity_policy_hash", "rejection_ledger_head_hash",
        "rejected_edges", "rubric_set_receipt_hash", "utilities", "receipt_hash",
    }
    if set(value) != expected or value.get("version") != GRAPH_UTILITY_RECEIPT_V2_VERSION:
        raise Stage6RubricRepositoryError("utility_receipt_v2_shape_invalid")
    _hash(value.get("rubric_set_receipt_hash"), "rubric_set_receipt_hash")
    _hash(value.get("receipt_hash"), "receipt_hash")
    if _digest({key: value[key] for key in value if key != "receipt_hash"}) != value["receipt_hash"]:
        raise Stage6RubricRepositoryError("utility_receipt_v2_hash_mismatch")
    members = _validated_rubric_member_index(member_receipts)
    rubric_set = validate_candidate_base_rubric_set_receipt(
        rubric_set_receipt, member_receipts=list(members.values())
    )
    if value["rubric_set_receipt_hash"] != rubric_set["receipt_hash"]:
        raise Stage6RubricRepositoryError("utility_rubric_set_binding_mismatch")
    legacy_hash = _legacy_rubric_aggregate_hash(members)
    legacy_body = {
        key: item
        for key, item in value.items()
        if key not in {
            "version", "rubric_set_receipt_hash", "receipt_hash"
        }
    }
    legacy_body["version"] = UTILITY_RECEIPT_VERSION
    legacy_body["rubric_receipt_hash"] = legacy_hash
    validate_graph_utility_receipt(
        {**legacy_body, "receipt_hash": _digest(legacy_body)},
        active_graph_hash=active_graph_hash,
        rejection_ledger_head_hash=rejection_ledger_head_hash,
        policy_hash=policy_hash,
    )
    utility_bases = {tuple(item["candidate_base"]) for item in value["utilities"]}
    if utility_bases != set(members):
        raise Stage6RubricRepositoryError("utility_rubric_set_coverage_mismatch")
    return value

def persist_candidate_base_rubric_receipt(db, *, job_id: str, candidate_base_key: tuple[str, ...], receipt: Any, created_at: str) -> str:
    """Insert an immutable candidate-base receipt; absent schema fails closed."""
    value = validate_rubric_receipt(receipt)
    if not isinstance(job_id, str) or not job_id or not isinstance(candidate_base_key, tuple) or not candidate_base_key:
        raise Stage6RubricRepositoryError("candidate_base_identity_invalid")
    actual_keys = {rubric_candidate_base_key(r) for r in value["rubrics"]}
    if actual_keys != {candidate_base_key}:
        raise Stage6RubricRepositoryError("candidate_base_identity_mismatch")
    try:
        db.execute(f"INSERT INTO {RUBRIC_RECEIPTS}(job_id,candidate_base_key,receipt_hash,receipt_version,payload_json,created_at) VALUES(?,?,?,?,?,?)", (job_id, _canonical(list(candidate_base_key)), value["receipt_hash"], value["version"], _canonical(value), created_at))
    except Exception as exc:
        raise Stage6RubricRepositoryError("rubric_persistence_unavailable") from exc
    return value["receipt_hash"]

def persist_graph_utility_receipt(db, *, job_id: str, receipt: Any, active_graph_hash: str, rejection_ledger_head_hash: str, policy_hash: str, created_at: str) -> str:
    value = validate_graph_utility_receipt(receipt, active_graph_hash=active_graph_hash, rejection_ledger_head_hash=rejection_ledger_head_hash, policy_hash=policy_hash)
    try:
        db.execute(f"INSERT INTO {UTILITY_RECEIPTS}(job_id,receipt_hash,graph_hash,policy_hash,rejection_ledger_head_hash,receipt_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, value["receipt_hash"], active_graph_hash, policy_hash, rejection_ledger_head_hash, value["version"], _canonical(value), created_at))
    except Exception as exc:
        raise Stage6RubricRepositoryError("utility_persistence_unavailable") from exc
    return value["receipt_hash"]

def recover_candidate_base_rubric_receipt(db, *, job_id: str, receipt_hash: str) -> dict[str, Any]:
    _hash(receipt_hash, "receipt_hash")
    row = db.execute(f"SELECT payload_json,receipt_hash FROM {RUBRIC_RECEIPTS} WHERE job_id=? AND receipt_hash=?", (job_id, receipt_hash)).fetchone()
    if row is None:
        raise Stage6RubricRepositoryError("rubric_receipt_missing")
    try:
        value = json.loads(row[0])
        if _canonical(value) != row[0]:
            raise Stage6RubricRepositoryError("rubric_receipt_noncanonical_json")
    except (TypeError, ValueError) as exc:
        raise Stage6RubricRepositoryError("rubric_receipt_json_invalid") from exc
    validated = validate_rubric_receipt(value)
    if validated["receipt_hash"] != row[1]:
        raise Stage6RubricRepositoryError("rubric_receipt_hash_mismatch")
    return validated

def recover_graph_utility_receipt(db, *, job_id: str, receipt_hash: str, active_graph_hash: str, rejection_ledger_head_hash: str, policy_hash: str) -> dict[str, Any]:
    _hash(receipt_hash, "receipt_hash")
    row = db.execute(f"SELECT payload_json,receipt_hash FROM {UTILITY_RECEIPTS} WHERE job_id=? AND receipt_hash=?", (job_id, receipt_hash)).fetchone()
    if row is None:
        raise Stage6RubricRepositoryError("utility_receipt_missing")
    try:
        value = json.loads(row[0])
        if _canonical(value) != row[0]:
            raise Stage6RubricRepositoryError("utility_receipt_noncanonical_json")
    except (TypeError, ValueError) as exc:
        raise Stage6RubricRepositoryError("utility_receipt_json_invalid") from exc
    validated = validate_graph_utility_receipt(value, active_graph_hash=active_graph_hash, rejection_ledger_head_hash=rejection_ledger_head_hash, policy_hash=policy_hash)
    if validated["receipt_hash"] != row[1]:
        raise Stage6RubricRepositoryError("utility_receipt_hash_mismatch")
    return validated

def validate_checkpoint_rubric_bindings(bindings: Mapping[str, Any]) -> dict[str, Any]:
    """Checkpoint may only reference receipt hashes; no embedded AI scores."""
    if not isinstance(bindings, Mapping):
        raise Stage6RubricRepositoryError("checkpoint_bindings_invalid")
    allowed = {"rubric_receipt_hash", "utility_receipt_hash"}
    if not bindings or set(bindings) - allowed:
        raise Stage6RubricRepositoryError("checkpoint_embedded_rubric_forbidden")
    for key, value in bindings.items():
        _hash(value, key)
    return dict(bindings)

__all__ = [
    "GRAPH_UTILITY_RECEIPT_V2_VERSION",
    "RUBRIC_MEMBER_RECEIPT_VERSION",
    "RUBRIC_RECEIPTS",
    "RUBRIC_REPOSITORY_VERSION",
    "RUBRIC_RECEIPT_V2_VERSION",
    "RUBRIC_SET_RECEIPT_VERSION",
    "Stage6RubricRepositoryError",
    "UTILITY_RECEIPTS",
    "build_candidate_base_rubric_member_receipt",
    "build_candidate_base_rubric_receipt",
    "build_candidate_base_rubric_set_receipt",
    "build_graph_utility_receipt_v2",
    "persist_candidate_base_rubric_receipt",
    "persist_graph_utility_receipt",
    "recover_candidate_base_rubric_receipt",
    "recover_graph_utility_receipt",
    "validate_candidate_base_rubric_member_receipt",
    "validate_candidate_base_rubric_set_receipt",
    "validate_checkpoint_rubric_bindings",
    "validate_graph_utility_receipt",
    "validate_graph_utility_receipt_v2",
    "validate_rubric_receipt",
]
