"""Independent candidate-base rubric and local-utility contracts for Stage 6.

The model can only return finite, candidate-local rubric facts.  Numeric
utility is derived by local code and only when a complete, explicitly supplied
policy is hash-bound; this module never invents weights or numeric defaults.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

RUBRIC_CATALOG_VERSION = "candidate-base-rubric-catalog-v1"
RUBRIC_REQUEST_VERSION = "past-v3-stage6-candidate-base-rubric-request-v1"
RUBRIC_RESPONSE_VERSION = "past-v3-stage6-candidate-base-rubric-response-v1"
WINDOW_RUBRIC_REQUEST_VERSION = "past-v3-stage6-candidate-base-rubric-window-request-v1"
WINDOW_RUBRIC_RESPONSE_VERSION = "past-v3-stage6-candidate-base-rubric-window-response-v1"
UTILITY_POLICY_VERSION = "past-v3-stage6-candidate-base-utility-policy-v1"
UTILITY_RECEIPT_VERSION = "candidate-base-utility-receipt-v1"
FACTOR_APPLICABILITY_VERSION = "candidate-base-factor-applicability-v1"
CANDIDATE_MATERIAL_VISIBILITY_VERSION = "candidate-base-material-visibility-v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN = {"score", "penalty", "utility", "selection", "selected", "difficulty", "answer", "answers", "solution", "reasoning", "chain_of_thought", "解析", "答案", "推理", "难度"}


class CandidateBaseRubricError(ValueError):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CandidateBaseRubricError("rubric_non_canonical_json") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise CandidateBaseRubricError(f"{field}_invalid")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise CandidateBaseRubricError(f"{field}_invalid")
    return value.strip()


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CandidateBaseRubricError("duplicate_json_key")
        result[key] = value
    return result


def build_candidate_material_visibility_receipt(stage4: Any, material_receipt: Any) -> dict[str, Any]:
    """Build a hash-bound visibility sidecar from complete Stage-4 snapshots.

    This is deliberately separate from the legacy material preheat receipt.  It
    never classifies a question; it only records whether the bounded prompt
    projection would hide any content/list/item from the model.  Missing or
    malformed Stage-4 evidence fails closed.
    """
    # ``FrozenCandidateFacts`` keeps its identity hash as an object attribute,
    # while the persisted ``facts_payload_json`` intentionally contains only
    # the canonical payload.  Recover the latter's hash from that exact
    # payload so the database-backed path remains compatible with Stage 4.
    stage_identity_hash = getattr(stage4, "candidate_facts_hash", None)
    stage = _receipt_dict(stage4, "stage4_candidate_facts")
    mat = _receipt_dict(material_receipt, "rubric_material")
    candidates = stage.get("candidates")
    materials = mat.get("materials")
    if not isinstance(candidates, list) or not isinstance(materials, list):
        raise CandidateBaseRubricError("candidate_material_visibility_missing")
    material_hash = getattr(material_receipt, "material_hash", None) or mat.get("material_hash") or _digest(mat)
    if not isinstance(material_hash, str) or _HASH.fullmatch(material_hash) is None or _digest(mat) != material_hash:
        raise CandidateBaseRubricError("candidate_material_visibility_binding_invalid")
    expected_stage_hash = stage_identity_hash or stage.get("candidate_facts_hash")
    if expected_stage_hash is None:
        # The normal serialized Stage-4 payload has no duplicated hash field;
        # its canonical digest is the authoritative candidate_facts_hash.  Do
        # not derive a hash from a partial adapter payload, because that would
        # silently bind a sidecar to an incomplete candidate set.
        complete_stage_fields = {
            "contract_version", "projection_identity", "projection_identity_hash",
            "target_institution", "candidates", "unresolved_candidate_ids",
        }
        if (
            isinstance(stage, Mapping)
            and stage.get("contract_version") == "past-v3-stage4-candidate-facts-v1"
            and complete_stage_fields.issubset(stage)
        ):
            expected_stage_hash = _digest(stage)
        else:
            raise CandidateBaseRubricError("candidate_material_visibility_stage_identity_missing")
    if not isinstance(expected_stage_hash, str) or _HASH.fullmatch(expected_stage_hash) is None or mat.get("candidate_facts_hash") != expected_stage_hash:
        raise CandidateBaseRubricError("candidate_material_visibility_identity_drift")
    by_id = {row.get("candidate_id"): row for row in materials if isinstance(row, Mapping)}
    if len(by_id) != len(materials):
        raise CandidateBaseRubricError("candidate_material_visibility_material_duplicate")
    stage_by_id: dict[str, Mapping[str, Any]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("candidate_id"), str):
            raise CandidateBaseRubricError("candidate_material_visibility_missing")
        cid = candidate["candidate_id"]
        if cid in stage_by_id:
            raise CandidateBaseRubricError("candidate_material_visibility_candidate_duplicate")
        stage_by_id[cid] = candidate

    # Stage 4 may contain a much larger candidate pool than the bounded
    # Stage-6 material receipt.  Visibility is required only for the material
    # rows actually entering this rubric window; requiring every Stage-4 row
    # would make the strict path unusable for normal 4N/6N graphs.
    entries: list[dict[str, Any]] = []
    for row in sorted(
        materials,
        key=lambda item: str(item.get("candidate_id")) if isinstance(item, Mapping) else "",
    ):
        if not isinstance(row, Mapping):
            raise CandidateBaseRubricError("candidate_material_visibility_missing")
        cid = row.get("candidate_id")
        candidate = stage_by_id.get(cid)
        if not isinstance(cid, str) or candidate is None:
            raise CandidateBaseRubricError("candidate_material_visibility_missing")
        snapshot = candidate.get("content_snapshot")
        snapshot_hash = candidate.get("content_snapshot_hash")
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise CandidateBaseRubricError("candidate_material_visibility_missing")
        expected_fact_hash = _digest(candidate)
        if (
            row.get("candidate_fact_hash") != expected_fact_hash
            or row.get("content_snapshot_hash") != snapshot_hash
            or row.get("content_hash") != candidate.get("content_hash")
        ):
            raise CandidateBaseRubricError("candidate_material_visibility_identity_drift")
        if not isinstance(snapshot_hash, str) or _HASH.fullmatch(snapshot_hash) is None or _digest(snapshot) != snapshot_hash:
            raise CandidateBaseRubricError("candidate_material_visibility_snapshot_hash_mismatch")
        try:
            payload = json.loads(snapshot, object_pairs_hook=_pairs)
        except (TypeError, ValueError, json.JSONDecodeError, CandidateBaseRubricError) as exc:
            raise CandidateBaseRubricError("candidate_material_visibility_snapshot_invalid") from exc
        if not isinstance(payload, Mapping) or set(payload) != {"schema_version", "content", "figure_references", "options", "conditions"} or payload.get("schema_version") != "semantic-candidate-content-snapshot-v1":
            raise CandidateBaseRubricError("candidate_material_visibility_snapshot_invalid")
        limits = {"content": 1200, "figure_references": 256, "options": 512, "conditions": 512}
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip() or any(not isinstance(payload.get(f), list) for f in limits if f != "content"):
            raise CandidateBaseRubricError("candidate_material_visibility_snapshot_invalid")
        normalized = {"content": " ".join(content.split())}
        for field in ("figure_references", "options", "conditions"):
            normalized[field] = [" ".join(v.split()) for v in payload[field] if isinstance(v, str)]
            if len(normalized[field]) != len(payload[field]):
                raise CandidateBaseRubricError("candidate_material_visibility_snapshot_invalid")
        fields: dict[str, Any] = {}
        complete = True
        for field, limit in limits.items():
            value = normalized[field]
            if field == "content":
                fields[field] = {"status": "truncated" if len(value) > limit else "complete", "source_length": len(value), "visible_length": min(len(value), limit)}
                complete = complete and len(value) <= limit
            else:
                item_rows = [{"index": i, "status": "truncated" if len(item) > limit else "complete", "source_length": len(item), "visible_length": min(len(item), limit)} for i, item in enumerate(value)]
                list_truncated = len(value) > 16
                fields[field] = {"status": "truncated" if list_truncated else "complete", "source_count": len(value), "visible_count": min(len(value), 16), "items": item_rows}
                complete = complete and not list_truncated and all(item["status"] == "complete" for item in item_rows)
        entries.append({"candidate_id": cid, "candidate_fact_hash": row.get("candidate_fact_hash"), "content_hash": row.get("content_hash"), "content_snapshot_hash": snapshot_hash, "status": "complete" if complete else "truncated", "fields": fields})
    entries.sort(key=lambda x: x["candidate_id"])
    body = {"version": CANDIDATE_MATERIAL_VISIBILITY_VERSION, "material_hash": material_hash, "candidate_facts_hash": mat.get("candidate_facts_hash"), "candidates": entries}
    return {**body, "visibility_hash": _digest(body)}


def validate_candidate_material_visibility(receipt: Any, *, material_receipt: Any) -> dict[str, Any]:
    value = _receipt_dict(receipt, "candidate_material_visibility")
    if set(value) != {"version", "material_hash", "candidate_facts_hash", "candidates", "visibility_hash"} or value.get("version") != CANDIDATE_MATERIAL_VISIBILITY_VERSION or value.get("visibility_hash") != _digest({k: value[k] for k in value if k != "visibility_hash"}):
        raise CandidateBaseRubricError("candidate_material_visibility_invalid")
    _hash(value.get("material_hash"), "visibility_material_hash")
    _hash(value.get("candidate_facts_hash"), "visibility_candidate_facts_hash")
    _hash(value.get("visibility_hash"), "visibility_hash")
    mat = _receipt_dict(material_receipt, "rubric_material")
    material_hash = getattr(material_receipt, "material_hash", None) or mat.get("material_hash") or _digest(mat)
    if (
        value.get("material_hash") != material_hash
        or value.get("candidate_facts_hash") != mat.get("candidate_facts_hash")
        or not isinstance(value.get("candidates"), list)
        or _digest(mat) != material_hash
    ):
        raise CandidateBaseRubricError("candidate_material_visibility_binding_invalid")
    material_rows = mat.get("materials")
    if not isinstance(material_rows, list) or not material_rows:
        raise CandidateBaseRubricError("candidate_material_visibility_coverage_invalid")
    expected_rows: list[tuple[str, str, str, str]] = []
    try:
        for row in material_rows:
            if not isinstance(row, Mapping):
                raise CandidateBaseRubricError("candidate_material_visibility_invalid")
            cid = _text(row.get("candidate_id"), "visibility_material_candidate_id")
            hashes = tuple(
                _hash(row.get(field), f"visibility_material_{field}")
                for field in ("candidate_fact_hash", "content_hash", "content_snapshot_hash")
            )
            if row.get("candidate_id") != cid:
                raise CandidateBaseRubricError("candidate_material_visibility_invalid")
            expected_rows.append((cid, *hashes))
    except CandidateBaseRubricError:
        raise
    if len(set(expected_rows)) != len(expected_rows):
        raise CandidateBaseRubricError("candidate_material_visibility_material_duplicate")
    actual_rows = value["candidates"]
    actual_ids: list[tuple[str, str, str, str]] = []
    for row in actual_rows:
        if not isinstance(row, Mapping):
            raise CandidateBaseRubricError("candidate_material_visibility_invalid")
        try:
            cid = _text(row.get("candidate_id"), "visibility_candidate_id")
            hashes = tuple(
                _hash(row.get(field), f"visibility_{field}")
                for field in ("candidate_fact_hash", "content_hash", "content_snapshot_hash")
            )
        except CandidateBaseRubricError:
            raise
        if row.get("candidate_id") != cid:
            raise CandidateBaseRubricError("candidate_material_visibility_invalid")
        actual_ids.append((cid, *hashes))
    if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != set(expected_rows):
        raise CandidateBaseRubricError("candidate_material_visibility_coverage_invalid")
    for row in value["candidates"]:
        if not isinstance(row, Mapping) or set(row) != {"candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "status", "fields"}:
            raise CandidateBaseRubricError("candidate_material_visibility_invalid")
        _text(row.get("candidate_id"), "visibility_candidate_id")
        for field in ("candidate_fact_hash", "content_hash", "content_snapshot_hash"):
            _hash(row.get(field), f"visibility_{field}")
        if row.get("status") != "complete":
            raise CandidateBaseRubricError("candidate_material_visibility_incomplete")
        fields = row.get("fields")
        if not isinstance(fields, Mapping) or set(fields) != {"content", "figure_references", "options", "conditions"}:
            raise CandidateBaseRubricError("candidate_material_visibility_invalid")
        for field, limit in {"content": 1200, "figure_references": 256, "options": 512, "conditions": 512}.items():
            item = fields.get(field)
            if not isinstance(item, Mapping) or item.get("status") != "complete":
                raise CandidateBaseRubricError("candidate_material_visibility_incomplete")
            if field == "content":
                if set(item) != {"status", "source_length", "visible_length"} or type(item["source_length"]) is not int or type(item["visible_length"]) is not int or item["source_length"] > limit or item["visible_length"] != item["source_length"]:
                    raise CandidateBaseRubricError("candidate_material_visibility_invalid")
            else:
                if set(item) != {"status", "source_count", "visible_count", "items"} or type(item["source_count"]) is not int or type(item["visible_count"]) is not int or item["source_count"] > 16 or item["visible_count"] != item["source_count"] or not isinstance(item["items"], list) or len(item["items"]) != item["source_count"]:
                    raise CandidateBaseRubricError("candidate_material_visibility_invalid")
                for index, part in enumerate(item["items"]):
                    if not isinstance(part, Mapping) or set(part) != {"index", "status", "source_length", "visible_length"} or part.get("index") != index or part.get("status") != "complete" or type(part.get("source_length")) is not int or type(part.get("visible_length")) is not int or part["source_length"] > limit or part["visible_length"] != part["source_length"]:
                        raise CandidateBaseRubricError("candidate_material_visibility_invalid")
    return value


# Business-neutral catalog: only candidate-local observables are permitted.
FACTOR_CATALOG: dict[str, Any] = {
    "version": RUBRIC_CATALOG_VERSION,
    "factors": {
        "content_completeness": {
            "definition": "题面是否包含作答所需的材料、条件、选项或图表引用与问题目标。",
            "levels": ["insufficient", "adequate", "strong"],
            "level_explanations": {"insufficient": "存在关键材料缺失或无法独立作答", "adequate": "材料基本齐全，仅有不影响作答的轻微缺口", "strong": "材料、引用和目标完整且可独立作答"},
            "positive_evidence_codes": ["content_all_components_present"],
            "negative_evidence_codes": ["content_component_missing", "content_minor_gap", "content_truncated"],
            "reason_codes": ["content_insufficient", "content_adequate", "content_strong"],
            "level_reason_code": {"insufficient": "content_insufficient", "adequate": "content_adequate", "strong": "content_strong"},
            "level_evidence_codes": {
                "insufficient": ["content_component_missing", "content_truncated"],
                "adequate": ["content_component_missing", "content_minor_gap"],
                "strong": ["content_all_components_present"],
            },
        },
        "statement_clarity": {
            "definition": "题面陈述、条件和指令是否清晰、无实质歧义且可按单一合理方式理解。",
            "levels": ["insufficient", "adequate", "strong"],
            "level_explanations": {"insufficient": "存在会改变答案或解法的歧义/矛盾", "adequate": "总体可理解，但有局部表述不够精确", "strong": "陈述、条件和指令均明确且无实质歧义"},
            "positive_evidence_codes": ["statement_unambiguous"],
            "negative_evidence_codes": ["statement_ambiguous", "statement_local_imprecision", "statement_unreadable"],
            "reason_codes": ["statement_insufficient", "statement_adequate", "statement_strong"],
            "level_reason_code": {"insufficient": "statement_insufficient", "adequate": "statement_adequate", "strong": "statement_strong"},
            "level_evidence_codes": {
                "insufficient": ["statement_ambiguous", "statement_unreadable"],
                "adequate": ["statement_ambiguous", "statement_local_imprecision"],
                "strong": ["statement_unambiguous"],
            },
        },
    },
}
FACTOR_CATALOG_HASH = _digest(FACTOR_CATALOG)

# Additive, calibration-only catalog.  This deliberately does not alter the
# v1 catalog or its utility policy: callers must opt into every v2 field.
RUBRIC_CATALOG_V2_VERSION = "candidate-base-rubric-catalog-v2"
RUBRIC_REQUEST_V2_VERSION = "past-v3-stage6-candidate-base-rubric-request-v2"
RUBRIC_RESPONSE_V2_VERSION = "past-v3-stage6-candidate-base-rubric-response-v2"
FACTOR_APPLICABILITY_V2_VERSION = "candidate-base-factor-applicability-v2"
FACTOR_CATALOG_V2: dict[str, Any] = {
    "version": RUBRIC_CATALOG_V2_VERSION,
    "factors": {
        **FACTOR_CATALOG["factors"],
        "internal_coherence": {
            "definition": "题面内部结构关系是否自洽：仅检查显式引用、符号绑定及条件与选项之间的结构矛盾；不判断答案、解法、正确性或可解性。",
            "levels": ["insufficient", "adequate", "strong"],
            "level_explanations": {
                "insufficient": "存在悬空图表引用、未绑定符号或条件/选项结构矛盾",
                "adequate": "结构关系基本完整，但有局部可定位的不一致风险",
                "strong": "显式结构引用、符号绑定及条件/选项关系一致",
            },
            "positive_evidence_codes": ["internal_relation_consistent"],
            "negative_evidence_codes": ["dangling_figure_reference", "unbound_symbol", "condition_option_conflict", "internal_relation_local_gap"],
            "reason_codes": ["coherence_insufficient", "coherence_adequate", "coherence_strong"],
            "level_reason_code": {"insufficient": "coherence_insufficient", "adequate": "coherence_adequate", "strong": "coherence_strong"},
            "level_evidence_codes": {
                "insufficient": ["dangling_figure_reference", "unbound_symbol", "condition_option_conflict"],
                "adequate": ["internal_relation_local_gap"],
                "strong": ["internal_relation_consistent"],
            },
        },
    },
}
FACTOR_CATALOG_V2_HASH = _digest(FACTOR_CATALOG_V2)


def _ensure_catalog_integrity() -> None:
    if _digest(FACTOR_CATALOG) != FACTOR_CATALOG_HASH:
        raise CandidateBaseRubricError("rubric_catalog_hash_mismatch")


def factor_catalog() -> dict[str, Any]:
    _ensure_catalog_integrity()
    return json.loads(_canonical(FACTOR_CATALOG))


def factor_catalog_v2() -> dict[str, Any]:
    """Return the opt-in v2 catalog; no production utility uses it yet."""
    if _digest(FACTOR_CATALOG_V2) != FACTOR_CATALOG_V2_HASH:
        raise CandidateBaseRubricError("rubric_catalog_v2_hash_mismatch")
    return json.loads(_canonical(FACTOR_CATALOG_V2))


def extend_factor_catalog(base: Mapping[str, Any], name: str, specification: Mapping[str, Any]) -> dict[str, Any]:
    """Explicit future-extension seam; unknown factors are never implicit."""
    if not isinstance(base, Mapping) or base.get("version") != RUBRIC_CATALOG_VERSION:
        raise CandidateBaseRubricError("rubric_catalog_invalid")
    _text(name, "factor_name")
    if name in FACTOR_CATALOG["factors"] or not isinstance(specification, Mapping):
        raise CandidateBaseRubricError("rubric_factor_registration_invalid")
    required = {
        "definition", "levels", "level_explanations", "positive_evidence_codes",
        "negative_evidence_codes", "reason_codes", "level_reason_code",
        "level_evidence_codes",
    }
    if set(specification) != required:
        raise CandidateBaseRubricError("rubric_factor_registration_invalid")
    if (
        not isinstance(specification["definition"], str)
        or not isinstance(specification["levels"], list)
        or not specification["levels"]
        or len(set(specification["levels"])) != len(specification["levels"])
        or any(not isinstance(level, str) or not level for level in specification["levels"])
        or not isinstance(specification["level_explanations"], Mapping)
        or set(specification["level_explanations"]) != set(specification["levels"])
        or any(not isinstance(value, str) or not value for value in specification["level_explanations"].values())
        or not isinstance(specification["positive_evidence_codes"], list)
        or not isinstance(specification["negative_evidence_codes"], list)
        or not isinstance(specification["reason_codes"], list)
        or not isinstance(specification["level_reason_code"], Mapping)
        or set(specification["level_reason_code"]) != set(specification["levels"])
        or not isinstance(specification["level_evidence_codes"], Mapping)
        or set(specification["level_evidence_codes"]) != set(specification["levels"])
    ):
        raise CandidateBaseRubricError("rubric_factor_registration_invalid")
    result = json.loads(_canonical(base)); result.setdefault("factors", {})[name] = json.loads(_canonical(specification))
    return result


def candidate_base_key(value: Mapping[str, Any]) -> tuple[str, ...]:
    """Return identity independent of slot, window, bucket, or call order."""
    _ensure_catalog_integrity()
    if not isinstance(value, Mapping):
        raise CandidateBaseRubricError("candidate_base_identity_invalid")
    candidate_id = _text(value.get("candidate_id"), "candidate_id")
    fact = _hash(value.get("candidate_fact_hash"), "candidate_fact_hash")
    content = _hash(value.get("content_hash"), "content_hash")
    snapshot = value.get("content_snapshot_hash")
    material = value.get("material_identity")
    if snapshot is None and material is None:
        raise CandidateBaseRubricError("candidate_material_identity_missing")
    if snapshot is not None:
        snapshot = _hash(snapshot, "content_snapshot_hash")
    if material is not None:
        material = _hash(material, "material_identity")
    catalog_version = value.get("catalog_version", RUBRIC_CATALOG_VERSION)
    catalog_hash = value.get("catalog_hash", FACTOR_CATALOG_HASH)
    applicability_version = value.get(
        "factor_applicability_version", FACTOR_APPLICABILITY_VERSION
    )
    if catalog_version != RUBRIC_CATALOG_VERSION:
        raise CandidateBaseRubricError("rubric_catalog_version_invalid")
    _hash(catalog_hash, "catalog_hash")
    if catalog_hash != FACTOR_CATALOG_HASH:
        raise CandidateBaseRubricError("rubric_catalog_hash_invalid")
    if applicability_version != FACTOR_APPLICABILITY_VERSION:
        raise CandidateBaseRubricError("factor_applicability_version_invalid")
    return (
        candidate_id, fact, content, snapshot or "", material or "",
        catalog_version, catalog_hash, applicability_version,
    )


def candidate_base_key_v2(value: Mapping[str, Any]) -> tuple[str, ...]:
    """v2 identity helper; transport context remains excluded."""
    candidate_id = _text(value.get("candidate_id"), "candidate_id")
    fact = _hash(value.get("candidate_fact_hash"), "candidate_fact_hash")
    content = _hash(value.get("content_hash"), "content_hash")
    snapshot = value.get("content_snapshot_hash"); material = value.get("material_identity")
    if snapshot is None and material is None:
        raise CandidateBaseRubricError("candidate_material_identity_missing")
    if snapshot is not None: snapshot = _hash(snapshot, "content_snapshot_hash")
    if material is not None: material = _hash(material, "material_identity")
    if value.get("catalog_version") != RUBRIC_CATALOG_V2_VERSION or value.get("catalog_hash") != FACTOR_CATALOG_V2_HASH or value.get("factor_applicability_version") != FACTOR_APPLICABILITY_V2_VERSION:
        raise CandidateBaseRubricError("rubric_catalog_v2_identity_invalid")
    return (candidate_id, fact, content, snapshot or "", material or "", RUBRIC_CATALOG_V2_VERSION, FACTOR_CATALOG_V2_HASH, FACTOR_APPLICABILITY_V2_VERSION)


def build_rubric_request(candidate: Mapping[str, Any], *, material_identity: str | None = None) -> dict[str, Any]:
    key = candidate_base_key({**candidate, **({"material_identity": material_identity} if material_identity else {})})
    body = {"version": RUBRIC_REQUEST_VERSION, "catalog_version": RUBRIC_CATALOG_VERSION,
            "catalog_hash": FACTOR_CATALOG_HASH, "candidate_id": key[0],
            "candidate_fact_hash": key[1], "content_hash": key[2],
            "content_snapshot_hash": key[3] or None, "material_identity": key[4] or None,
            "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
            "candidate_material_trust": "untrusted_data"}
    return {**body, "request_hash": _digest(body)}


def build_rubric_request_v2(candidate: Mapping[str, Any], *, material_identity: str | None = None) -> dict[str, Any]:
    """Build an explicitly opt-in v2 request including internal_coherence."""
    # Candidate facts may carry stale catalog metadata from an upstream
    # adapter.  Rebind the complete identity to the opt-in v2 catalog instead
    # of accidentally constructing a v2 envelope around a v1 key.
    identity_input = {
        k: v for k, v in candidate.items()
        if k not in {"catalog_version", "catalog_hash", "factor_applicability_version"}
    }
    identity_input.update({
        "catalog_version": RUBRIC_CATALOG_V2_VERSION,
        "catalog_hash": FACTOR_CATALOG_V2_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_V2_VERSION,
    })
    if material_identity:
        identity_input["material_identity"] = material_identity
    key = candidate_base_key_v2(identity_input)
    body = {"version": RUBRIC_REQUEST_V2_VERSION, "catalog_version": RUBRIC_CATALOG_V2_VERSION,
            "catalog_hash": FACTOR_CATALOG_V2_HASH, "candidate_id": key[0],
            "candidate_fact_hash": key[1], "content_hash": key[2],
            "content_snapshot_hash": key[3] or None, "material_identity": key[4] or None,
            "factor_applicability_version": FACTOR_APPLICABILITY_V2_VERSION,
            "candidate_material_trust": "untrusted_data"}
    return {**body, "request_hash": _digest(body)}


def _scan_forbidden(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN:
                raise CandidateBaseRubricError("forbidden_decision_field")
            _scan_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _scan_forbidden(item)


def parse_rubric_response(raw: str | Mapping[str, Any], *, request: Mapping[str, Any]) -> dict[str, Any]:
    _ensure_catalog_integrity()
    try:
        value = json.loads(raw, object_pairs_hook=_pairs) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateBaseRubricError("rubric_response_invalid_json") from exc
    _scan_forbidden(value)
    if (
        set(request) != {"version", "catalog_version", "catalog_hash", "candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity", "factor_applicability_version", "candidate_material_trust", "request_hash"}
        or request.get("version") != RUBRIC_REQUEST_VERSION
        or request.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or request.get("catalog_hash") != FACTOR_CATALOG_HASH
        or request.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION
        or request.get("candidate_material_trust") != "untrusted_data"
        or request.get("request_hash") != _digest({k: request[k] for k in request if k != "request_hash"})
    ):
        raise CandidateBaseRubricError("rubric_request_invalid")
    fields = {"version", "request_hash", "candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity", "catalog_version", "catalog_hash", "factor_applicability_version", "factors"}
    if set(value) != fields or value.get("version") != RUBRIC_RESPONSE_VERSION or value.get("request_hash") != request["request_hash"]:
        raise CandidateBaseRubricError("rubric_response_shape_invalid")
    if value["catalog_version"] != RUBRIC_CATALOG_VERSION or value["factor_applicability_version"] != FACTOR_APPLICABILITY_VERSION or value["candidate_id"] != request["candidate_id"] or value["candidate_fact_hash"] != request["candidate_fact_hash"] or value["content_hash"] != request["content_hash"] or value["content_snapshot_hash"] != request["content_snapshot_hash"] or value["material_identity"] != request["material_identity"] or value["catalog_hash"] != FACTOR_CATALOG_HASH:
        raise CandidateBaseRubricError("rubric_response_identity_invalid")
    factors = value["factors"]
    if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_CATALOG["factors"]):
        raise CandidateBaseRubricError("rubric_factor_set_invalid")
    for name, item in factors.items():
        if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes", "reason_codes"}:
            raise CandidateBaseRubricError("rubric_factor_shape_invalid")
        spec = FACTOR_CATALOG["factors"][name]
        if item["level"] not in spec["levels"]:
            raise CandidateBaseRubricError("rubric_factor_value_invalid")
        codes = item["evidence_codes"]
        allowed = set(spec["positive_evidence_codes"] + spec["negative_evidence_codes"])
        if not isinstance(codes, list) or not codes or len(set(codes)) != len(codes) or any(code not in allowed for code in codes):
            raise CandidateBaseRubricError("rubric_evidence_code_invalid")
        reasons = item["reason_codes"]
        if not isinstance(reasons, list) or not reasons or len(set(reasons)) != len(reasons) or any(code not in spec["reason_codes"] for code in reasons):
            raise CandidateBaseRubricError("rubric_reason_code_invalid")
        expected_reason = spec["level_reason_code"][item["level"]]
        allowed_evidence = set(spec["level_evidence_codes"][item["level"]])
        if reasons != [expected_reason] or not set(codes).issubset(allowed_evidence):
            raise CandidateBaseRubricError("rubric_level_evidence_mismatch")
    return value


def parse_rubric_response_v2(raw: str | Mapping[str, Any], *, request: Mapping[str, Any]) -> dict[str, Any]:
    """Strict parser for the additive v2 catalog.

    v2 is calibration-only.  In particular, this parser does not compute
    utility and does not infer answer/correctness/solvability from coherence.
    """
    if _digest(FACTOR_CATALOG_V2) != FACTOR_CATALOG_V2_HASH:
        raise CandidateBaseRubricError("rubric_catalog_v2_hash_mismatch")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateBaseRubricError("rubric_response_invalid_json") from exc
    _scan_forbidden(value)
    request_fields = {"version", "catalog_version", "catalog_hash", "candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity", "factor_applicability_version", "candidate_material_trust", "request_hash"}
    if (set(request) != request_fields or request.get("version") != RUBRIC_REQUEST_V2_VERSION
        or request.get("catalog_version") != RUBRIC_CATALOG_V2_VERSION or request.get("catalog_hash") != FACTOR_CATALOG_V2_HASH
        or request.get("factor_applicability_version") != FACTOR_APPLICABILITY_V2_VERSION
        or request.get("candidate_material_trust") != "untrusted_data"
        or request.get("request_hash") != _digest({k: request[k] for k in request if k != "request_hash"})):
        raise CandidateBaseRubricError("rubric_request_invalid")
    fields = {"version", "request_hash", "candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity", "catalog_version", "catalog_hash", "factor_applicability_version", "factors"}
    if set(value) != fields or value.get("version") != RUBRIC_RESPONSE_V2_VERSION or value.get("request_hash") != request["request_hash"]:
        raise CandidateBaseRubricError("rubric_response_shape_invalid")
    try:
        request_key = candidate_base_key_v2(request)
        value_key = candidate_base_key_v2(value)
    except CandidateBaseRubricError as exc:
        raise CandidateBaseRubricError("rubric_catalog_v2_identity_invalid") from exc
    if request_key != value_key:
        raise CandidateBaseRubricError("rubric_response_identity_invalid")
    for name in ("catalog_version", "catalog_hash", "factor_applicability_version", "candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity"):
        expected = {"catalog_version": RUBRIC_CATALOG_V2_VERSION, "catalog_hash": FACTOR_CATALOG_V2_HASH, "factor_applicability_version": FACTOR_APPLICABILITY_V2_VERSION}.get(name, request[name])
        if value.get(name) != expected:
            raise CandidateBaseRubricError("rubric_response_identity_invalid")
    factors = value["factors"]
    if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_CATALOG_V2["factors"]):
        raise CandidateBaseRubricError("rubric_factor_set_invalid")
    for name, item in factors.items():
        spec = FACTOR_CATALOG_V2["factors"][name]
        if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes", "reason_codes"}:
            raise CandidateBaseRubricError("rubric_factor_shape_invalid")
        level, codes, reasons = item["level"], item["evidence_codes"], item["reason_codes"]
        allowed = set(spec["positive_evidence_codes"] + spec["negative_evidence_codes"])
        if level not in spec["levels"] or not isinstance(codes, list) or not codes or len(set(codes)) != len(codes) or any(code not in allowed for code in codes):
            raise CandidateBaseRubricError("rubric_factor_value_invalid")
        if not isinstance(reasons, list) or reasons != [spec["level_reason_code"][level]] or not set(codes).issubset(set(spec["level_evidence_codes"][level])):
            raise CandidateBaseRubricError("rubric_level_evidence_mismatch")
    return value


def validate_candidate_base_consistency(values: Sequence[Mapping[str, Any]]) -> dict[tuple[str, ...], Mapping[str, Any]]:
    registry: dict[tuple[str, ...], Mapping[str, Any]] = {}
    for value in values:
        key = candidate_base_key(value)
        comparable = {k: value[k] for k in ("candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "material_identity", "catalog_version", "catalog_hash", "factor_applicability_version", "factors") if k in value}
        previous = registry.get(key)
        if previous is not None and _canonical(previous) != _canonical(comparable):
            raise CandidateBaseRubricError("candidate_base_consistency_invalid")
        registry[key] = comparable
    return registry


def validate_utility_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    _ensure_catalog_integrity()
    fields = {
        "version", "catalog_version", "catalog_hash",
        "factor_applicability_version", "parameter_set_hash", "aggregation",
        "weights", "level_mapping", "rounding", "capping",
        "unknown_blocked", "policy_hash",
    }
    if not isinstance(policy, Mapping) or set(policy) != fields:
        raise CandidateBaseRubricError("utility_policy_missing_or_invalid")
    # Authenticate the supplied parameter payload before reporting any
    # structural/numeric defect; stale hashes remain a distinct contract error.
    body = {k: policy[k] for k in fields - {"policy_hash"}}
    if not isinstance(policy.get("policy_hash"), str) or policy.get("policy_hash") != _digest(body):
        raise CandidateBaseRubricError("utility_policy_hash_mismatch")
    if policy.get("version") != UTILITY_POLICY_VERSION or policy.get("unknown_blocked") is not False or type(policy.get("unknown_blocked")) is not bool:
        raise CandidateBaseRubricError("utility_policy_unknown_blocked")
    if (
        policy.get("catalog_version") != RUBRIC_CATALOG_VERSION
        or policy.get("catalog_hash") != FACTOR_CATALOG_HASH
        or policy.get("factor_applicability_version")
        != FACTOR_APPLICABILITY_VERSION
        or policy.get("aggregation") != "weighted_sum_integer_v1"
    ):
        raise CandidateBaseRubricError("utility_policy_identity_invalid")
    _hash(policy.get("parameter_set_hash"), "parameter_set_hash")
    weights, mappings = policy["weights"], policy["level_mapping"]
    factor_ids = set(FACTOR_CATALOG["factors"])
    if not isinstance(weights, Mapping) or not isinstance(mappings, Mapping) or set(weights) != factor_ids or set(mappings) != factor_ids:
        raise CandidateBaseRubricError("utility_policy_missing_or_invalid")
    for factor_id, spec in FACTOR_CATALOG["factors"].items():
        if type(weights[factor_id]) is not int:
            raise CandidateBaseRubricError("utility_policy_numeric_invalid")
        mapping = mappings[factor_id]
        if not isinstance(mapping, Mapping) or set(mapping) != set(spec["levels"]):
            raise CandidateBaseRubricError("utility_policy_level_mapping_invalid")
        if any(type(mapping[level]) is not int for level in spec["levels"]):
            raise CandidateBaseRubricError("utility_policy_numeric_invalid")
    if policy.get("rounding") != {"mode": "exact_integer"}:
        raise CandidateBaseRubricError("utility_policy_rounding_invalid")
    capping = policy.get("capping")
    if (
        not isinstance(capping, Mapping)
        or set(capping) != {"minimum", "maximum"}
        or any(value is not None and type(value) is not int for value in capping.values())
        or capping["minimum"] is not None
        and capping["maximum"] is not None
        and capping["minimum"] > capping["maximum"]
    ):
        raise CandidateBaseRubricError("utility_policy_capping_invalid")
    return dict(policy)


def build_utility_policy(
    *,
    parameter_set_hash: str,
    aggregation: str,
    weights: Mapping[str, Any],
    level_mapping: Mapping[str, Any],
    rounding: Mapping[str, Any],
    capping: Mapping[str, Any],
    unknown_blocked: bool,
) -> dict[str, Any]:
    """Construct a policy only from explicitly supplied calibration values.

    There are intentionally no defaults in this helper.  Callers must provide
    every value whose choice can change selection; validation then produces the
    immutable policy hash used by utility and selection receipts.
    """

    body = {
        "version": UTILITY_POLICY_VERSION,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "parameter_set_hash": parameter_set_hash,
        "aggregation": aggregation,
        "weights": weights,
        "level_mapping": level_mapping,
        "rounding": rounding,
        "capping": capping,
        "unknown_blocked": unknown_blocked,
    }
    policy = {**body, "policy_hash": _digest(body)}
    return validate_utility_policy(policy)


def compute_candidate_utility(rubric: Mapping[str, Any], policy: Mapping[str, Any]) -> int:
    policy = validate_utility_policy(policy)
    if not isinstance(rubric, Mapping) or rubric.get("catalog_hash") != FACTOR_CATALOG_HASH:
        raise CandidateBaseRubricError("utility_rubric_identity_mismatch")
    factors = rubric.get("factors")
    if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_CATALOG["factors"]):
        raise CandidateBaseRubricError("utility_rubric_factor_mismatch")
    weights, mappings = policy["weights"], policy["level_mapping"]
    if set(weights) != set(factors) or set(mappings) != set(factors):
        raise CandidateBaseRubricError("utility_policy_factor_mismatch")
    total = 0
    for name, item in factors.items():
        if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes", "reason_codes"}:
            raise CandidateBaseRubricError("utility_rubric_factor_shape_invalid")
        level = item.get("level")
        specification = FACTOR_CATALOG["factors"][name]
        if level not in specification["levels"]:
            raise CandidateBaseRubricError("utility_rubric_level_invalid")
        evidence_codes = item.get("evidence_codes")
        reason_codes = item.get("reason_codes")
        if (
            not isinstance(evidence_codes, list)
            or not evidence_codes
            or len(set(evidence_codes)) != len(evidence_codes)
            or any(code not in set(specification["positive_evidence_codes"]) | set(specification["negative_evidence_codes"]) for code in evidence_codes)
            or reason_codes != [specification["level_reason_code"][level]]
            or not set(evidence_codes).issubset(set(specification["level_evidence_codes"][level]))
        ):
            raise CandidateBaseRubricError("utility_rubric_evidence_invalid")
        mapping = mappings.get(name)
        if not isinstance(mapping, Mapping) or set(mapping) != set(FACTOR_CATALOG["factors"][name]["levels"]) or level not in mapping:
            raise CandidateBaseRubricError("utility_policy_level_mapping_invalid")
        weight = weights.get(name)
        if type(weight) is not int or type(mapping[level]) is not int:
            raise CandidateBaseRubricError("utility_policy_numeric_invalid")
        total += weight * mapping[level]
    minimum = policy["capping"]["minimum"]
    maximum = policy["capping"]["maximum"]
    if minimum is not None:
        total = max(minimum, total)
    if maximum is not None:
        total = min(maximum, total)
    return total


def explain_candidate_utility(
    rubric: Mapping[str, Any], policy: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a finite, re-computable explanation for one utility value.

    The payload intentionally contains levels, codes and local contributions
    only.  It has no open-ended model rationale and no legacy score/penalty
    fields, so it can be safely attached to a selection audit receipt.
    """

    validated = validate_utility_policy(policy)
    if not isinstance(rubric, Mapping) or rubric.get("catalog_hash") != FACTOR_CATALOG_HASH:
        raise CandidateBaseRubricError("utility_rubric_identity_mismatch")
    factors = rubric.get("factors")
    if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_CATALOG["factors"]):
        raise CandidateBaseRubricError("utility_rubric_factor_mismatch")
    contributions: list[dict[str, Any]] = []
    for factor_id in sorted(FACTOR_CATALOG["factors"]):
        item = factors[factor_id]
        if not isinstance(item, Mapping):
            raise CandidateBaseRubricError("utility_rubric_factor_mismatch")
        level = item.get("level")
        mapping = validated["level_mapping"].get(factor_id)
        weight = validated["weights"].get(factor_id)
        if not isinstance(mapping, Mapping) or level not in mapping or type(weight) is not int or type(mapping[level]) is not int:
            raise CandidateBaseRubricError("utility_policy_level_mapping_invalid")
        contributions.append({
            "factor_id": factor_id,
            "level": level,
            "reason_codes": list(item["reason_codes"]),
            "evidence_codes": list(item["evidence_codes"]),
            "weight": weight,
            "level_value": mapping[level],
            "contribution": weight * mapping[level],
        })
    utility = compute_candidate_utility(rubric, validated)
    body = {
        "version": "candidate-base-utility-explanation-v1",
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "policy_version": validated["version"],
        "policy_hash": validated["policy_hash"],
        "parameter_set_hash": validated["parameter_set_hash"],
        "utility": utility,
        "contributions": contributions,
    }
    return {**body, "explanation_hash": _digest(body)}


def _receipt_dict(value: Any, field: str) -> Mapping[str, Any]:
    """Accept either frozen receipt objects or their serialized dictionaries."""
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise CandidateBaseRubricError(f"{field}_invalid")
    return value


def build_window_rubric_request(window: Any, material_receipt: Any) -> dict[str, Any]:
    """Build one window-level request, deduplicating candidates by candidate-base identity.

    Slot/position/bucket fields are deliberately omitted from each candidate entry;
    graph/window binding remains at the request level for replay and audit.
    """
    win = _receipt_dict(window, "rubric_window")
    mat = _receipt_dict(material_receipt, "rubric_material")
    if not isinstance(win.get("edges"), list) or not isinstance(mat.get("materials"), list):
        raise CandidateBaseRubricError("rubric_window_shape_invalid")
    window_hash = getattr(window, "window_hash", None) or win.get("window_hash")
    material_hash = getattr(material_receipt, "material_hash", None) or mat.get("material_hash")
    if window_hash is None:
        window_hash = _digest(win)
    if material_hash is None:
        material_hash = _digest(mat)
    if (not isinstance(window_hash, str) or not isinstance(material_hash, str)
            or _HASH.fullmatch(window_hash) is None or _HASH.fullmatch(material_hash) is None):
        raise CandidateBaseRubricError("rubric_window_binding_invalid")
    # A caller-supplied receipt hash is not trusted merely because it has the
    # right shape.  Recompute both frozen payload digests before deriving a
    # request identity; otherwise a forged window/material object could be
    # carried into a later persistence layer with an internally consistent but
    # false binding.
    if _digest(win) != window_hash or _digest(mat) != material_hash:
        raise CandidateBaseRubricError("rubric_window_binding_hash_mismatch")
    if win.get("graph_hash") != mat.get("graph_hash"):
        raise CandidateBaseRubricError("rubric_window_binding_invalid")
    materials: dict[str, Mapping[str, Any]] = {}
    for row in mat["materials"]:
        if not isinstance(row, Mapping) or not isinstance(row.get("candidate_id"), str):
            raise CandidateBaseRubricError("rubric_material_entry_invalid")
        if row["candidate_id"] in materials:
            raise CandidateBaseRubricError("rubric_material_candidate_duplicate")
        materials[row["candidate_id"]] = row
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for edge in win["edges"]:
        if not isinstance(edge, Mapping) or not isinstance(edge.get("candidate_id"), str):
            raise CandidateBaseRubricError("rubric_window_edge_invalid")
        row = materials.get(edge["candidate_id"])
        if row is None:
            raise CandidateBaseRubricError("rubric_material_missing")
        if edge.get("candidate_fact_hash") != row.get("candidate_fact_hash") or edge.get("content_hash") != row.get("content_hash"):
            raise CandidateBaseRubricError("rubric_candidate_identity_drift")
        candidate = {k: row.get(k) for k in ("candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash")}
        key = candidate_base_key(candidate)
        entry = build_rubric_request(candidate)
        # Local evidence is useful to the model but is not part of identity.
        if "local_summary" in row:
            entry["local_summary"] = row["local_summary"]
        previous = unique.get(key)
        if previous is not None and _canonical(previous) != _canonical(entry):
            raise CandidateBaseRubricError("candidate_base_consistency_invalid")
        unique[key] = entry
    candidates = sorted(unique.values(), key=lambda x: (x["candidate_id"], x["candidate_fact_hash"], x["content_hash"]))
    body = {
        "version": WINDOW_RUBRIC_REQUEST_VERSION,
        "graph_hash": win.get("graph_hash"),
        "window_hash": window_hash,
        "window_index": win.get("window_index"),
        "phase": win.get("phase"),
        "slot_ids": list(win.get("slot_ids", [])),
        "material_hash": material_hash,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "candidates": candidates,
    }
    return {**body, "request_hash": _digest(body)}


def build_candidate_base_rubric_request(window: Any, material_receipt: Any, visibility_receipt: Any | None = None) -> dict[str, Any]:
    """Strict candidate-base request constructor requiring complete visibility."""
    if visibility_receipt is None:
        raise CandidateBaseRubricError("candidate_material_visibility_missing")
    visibility = validate_candidate_material_visibility(visibility_receipt, material_receipt=material_receipt)
    request = build_window_rubric_request(window, material_receipt)
    body = {**request, "visibility_hash": visibility["visibility_hash"]}
    return {**body, "request_hash": _digest({k: body[k] for k in body if k != "request_hash"})}


def parse_window_rubric_response(raw: str | Mapping[str, Any], *, request: Mapping[str, Any]) -> dict[str, Any]:
    """Strictly parse a window response and require exactly one rubric per candidate."""
    try:
        value = json.loads(raw, object_pairs_hook=_pairs) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CandidateBaseRubricError("rubric_response_invalid_json") from exc
    _scan_forbidden(value)
    required = {"version", "request_hash", "graph_hash", "window_hash", "material_hash", "catalog_version", "catalog_hash", "factor_applicability_version", "candidates"}
    if set(value) != required or value.get("version") != WINDOW_RUBRIC_RESPONSE_VERSION:
        raise CandidateBaseRubricError("rubric_window_response_shape_invalid")
    request_fields = {"version", "graph_hash", "window_hash", "window_index", "phase", "slot_ids", "material_hash", "catalog_version", "catalog_hash", "factor_applicability_version", "candidates", "request_hash"}
    if isinstance(request, Mapping) and "visibility_hash" in request:
        request_fields.add("visibility_hash")
    if (not isinstance(request, Mapping) or set(request) != request_fields
            or request.get("version") != WINDOW_RUBRIC_REQUEST_VERSION
            or request.get("catalog_version") != RUBRIC_CATALOG_VERSION
            or request.get("catalog_hash") != FACTOR_CATALOG_HASH
            or request.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION
            or request.get("request_hash") != _digest({k: request[k] for k in request if k != "request_hash"})):
        raise CandidateBaseRubricError("rubric_request_invalid")
    if "visibility_hash" in request and (_HASH.fullmatch(request.get("visibility_hash", "")) is None):
        raise CandidateBaseRubricError("candidate_material_visibility_binding_invalid")
    if value.get("request_hash") != request["request_hash"]:
        raise CandidateBaseRubricError("rubric_window_response_identity_invalid")
    if (value.get("catalog_version") != RUBRIC_CATALOG_VERSION
            or value.get("catalog_hash") != FACTOR_CATALOG_HASH
            or value.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION):
        raise CandidateBaseRubricError("rubric_window_response_identity_invalid")
    for field in ("graph_hash", "window_hash", "material_hash"):
        if value.get(field) != request.get(field):
            raise CandidateBaseRubricError("rubric_window_response_identity_invalid")
    expected = request.get("candidates")
    rows = value.get("candidates")
    if not isinstance(expected, list) or not isinstance(rows, list) or len(rows) != len(expected):
        raise CandidateBaseRubricError("rubric_window_candidate_coverage_invalid")
    by_key = {candidate_base_key(item): item for item in expected}
    if len(by_key) != len(expected):
        raise CandidateBaseRubricError("rubric_request_invalid")
    parsed: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"rubric"}:
            raise CandidateBaseRubricError("rubric_window_candidate_shape_invalid")
        rubric = row["rubric"]
        key = candidate_base_key(rubric)
        if key not in by_key or key in seen:
            raise CandidateBaseRubricError("rubric_window_candidate_coverage_invalid")
        request_candidate = {k: v for k, v in by_key[key].items() if k != "local_summary"}
        parsed.append(parse_rubric_response(rubric, request=request_candidate))
        seen.add(key)
    if seen != set(by_key):
        raise CandidateBaseRubricError("rubric_window_candidate_coverage_invalid")
    return {**value, "candidates": parsed}


def build_utility_receipt(rubrics: Sequence[Mapping[str, Any]], *, policy: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a deterministic utility receipt; no implicit weights or policy exist."""
    if policy is None:
        raise CandidateBaseRubricError("utility_policy_missing_or_invalid")
    validated = validate_utility_policy(policy)
    consistent = validate_candidate_base_consistency(rubrics)
    utilities = [
        {
            "candidate_base": list(key),
            "utility": compute_candidate_utility(consistent[key], validated),
        }
        for key in sorted(consistent)
    ]
    body = {
        "version": UTILITY_RECEIPT_VERSION,
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "parameter_set_hash": validated["parameter_set_hash"],
        "policy_version": validated["version"],
        "policy_hash": validated["policy_hash"],
        "utilities": utilities,
    }
    return {**body, "receipt_hash": _digest(body)}


def build_graph_utility_receipt(
    graph: Any,
    rubrics: Sequence[Mapping[str, Any]],
    *,
    policy: Mapping[str, Any] | None = None,
    rejected_edges: Sequence[Mapping[str, str]] = (),
    rejection_ledger_head_hash: str = "0" * 64,
    diversity_policy_hash: str | None = None,
) -> dict[str, Any]:
    """Bind candidate-local utilities to a frozen graph without slot scoring.

    The rubric is evaluated once per candidate-base identity.  An edge only
    carries the resulting local utility; it never carries an AI-generated
    score/penalty.  This receipt is intentionally separate from the legacy
    ``local-score-v2`` shape so old records cannot be silently converted.
    """

    graph_payload = _receipt_dict(graph, "utility_graph")
    if not isinstance(graph_payload.get("edges"), list):
        raise CandidateBaseRubricError("utility_graph_shape_invalid")
    graph_hash = getattr(graph, "graph_hash", None) or graph_payload.get("graph_hash")
    if not isinstance(graph_hash, str) or _HASH.fullmatch(graph_hash) is None:
        raise CandidateBaseRubricError("utility_graph_hash_invalid")
    if _digest(graph_payload) != graph_hash:
        raise CandidateBaseRubricError("utility_graph_hash_mismatch")
    for field in ("constraint_hash", "candidate_facts_hash", "projection_identity_hash"):
        _hash(graph_payload.get(field), field)
    if diversity_policy_hash is not None:
        _hash(diversity_policy_hash, "diversity_policy_hash")
    validated = validate_utility_policy(policy) if policy is not None else None
    if validated is None:
        raise CandidateBaseRubricError("utility_policy_missing_or_invalid")
    consistent = validate_candidate_base_consistency(rubrics)
    by_candidate: dict[str, tuple[tuple[str, ...], int]] = {}
    for key, rubric in consistent.items():
        if key[0] in by_candidate and by_candidate[key[0]][0] != key:
            raise CandidateBaseRubricError("candidate_base_ambiguity")
        by_candidate[key[0]] = (key, compute_candidate_utility(rubric, validated))
    rejected: list[dict[str, str]] = []
    for item in rejected_edges:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id"}:
            raise CandidateBaseRubricError("utility_rejected_edge_invalid")
        rejected.append({"slot_id": _text(item["slot_id"], "rejected_slot_id"), "candidate_id": _text(item["candidate_id"], "rejected_candidate_id")})
    if len({(x["slot_id"], x["candidate_id"]) for x in rejected}) != len(rejected):
        raise CandidateBaseRubricError("utility_rejected_edge_duplicate")
    ledger = _hash(rejection_ledger_head_hash, "rejection_ledger_head_hash")
    if (rejected and ledger == "0" * 64) or (not rejected and ledger != "0" * 64):
        raise CandidateBaseRubricError("utility_rejection_ledger_invalid")
    utilities: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    rejected_keys = {(x["slot_id"], x["candidate_id"]) for x in rejected}
    graph_keys = {
        (_text(edge.get("slot_id"), "utility_slot_id"), _text(edge.get("candidate_id"), "utility_candidate_id"))
        for edge in graph_payload["edges"] if isinstance(edge, Mapping)
    }
    if not rejected_keys.issubset(graph_keys):
        raise CandidateBaseRubricError("utility_rejected_edge_unknown")
    for edge in graph_payload["edges"]:
        if not isinstance(edge, Mapping):
            raise CandidateBaseRubricError("utility_graph_edge_invalid")
        slot_id = _text(edge.get("slot_id"), "utility_slot_id")
        candidate_id = _text(edge.get("candidate_id"), "utility_candidate_id")
        key = (slot_id, candidate_id)
        if key in seen_edges:
            raise CandidateBaseRubricError("utility_graph_edge_duplicate")
        seen_edges.add(key)
        if key in rejected_keys:
            continue
        candidate_key = (
            candidate_id,
            _hash(edge.get("candidate_fact_hash"), "candidate_fact_hash"),
            _hash(edge.get("content_hash"), "content_hash"),
        )
        match = next((entry for entry in by_candidate.values() if entry[0][:3] == candidate_key), None)
        if match is None:
            raise CandidateBaseRubricError("utility_rubric_missing")
        utilities.append({
            "slot_id": slot_id,
            "candidate_id": candidate_id,
            "candidate_base": list(match[0]),
            "utility": match[1],
        })
    graph_candidate_ids = {
        edge.get("candidate_id")
        for edge in graph_payload["edges"]
        if (edge.get("slot_id"), edge.get("candidate_id")) not in rejected_keys
    }
    if set(by_candidate) != graph_candidate_ids:
        raise CandidateBaseRubricError("utility_rubric_set_mismatch")
    body = {
        "version": UTILITY_RECEIPT_VERSION,
        "graph_hash": graph_hash,
        "constraint_hash": graph_payload["constraint_hash"],
        "candidate_facts_hash": graph_payload["candidate_facts_hash"],
        "projection_identity_hash": graph_payload["projection_identity_hash"],
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "parameter_set_hash": validated["parameter_set_hash"],
        "policy_version": validated["version"],
        "policy_hash": validated["policy_hash"],
        "diversity_policy_hash": diversity_policy_hash,
        "rejection_ledger_head_hash": ledger,
        "rejected_edges": sorted(rejected, key=lambda x: (x["slot_id"], x["candidate_id"])),
        "rubric_receipt_hash": _digest({"rubrics": [consistent[key] for key in sorted(consistent)]}),
        "utilities": sorted(utilities, key=lambda x: (x["slot_id"], x["candidate_id"])),
    }
    return {**body, "receipt_hash": _digest(body)}


def evaluate_candidate_base_rubric_windows(
    windows: Sequence[Any],
    material_receipt: Any,
    callback: Any,
    *,
    visibility_receipt: Any | None = None,
    utility_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run an injected rubric callback over frozen windows, provider-free.

    This is an orchestration seam for the future Stage-6 worker.  It performs
    no persistence and does not retry or choose questions.  Every callback
    request is deterministic; identical candidate-base identities observed in
    multiple windows must return identical rubric facts.  A hash-bound,
    complete material-visibility receipt is mandatory; without it the model
    is never called.  When no explicit utility policy is supplied the function
    returns rubric facts only and leaves utility derivation blocked.
    """

    if not isinstance(windows, Sequence) or isinstance(windows, (str, bytes)) or not windows:
        raise CandidateBaseRubricError("rubric_windows_invalid")
    if not callable(callback):
        raise CandidateBaseRubricError("rubric_callback_missing")
    if visibility_receipt is None:
        # Candidate-base evaluation is always gated by a complete, hash-bound
        # material visibility sidecar.  The legacy window adapter remains a
        # separate constructor, but this evaluation seam never bypasses it.
        raise CandidateBaseRubricError("candidate_material_visibility_missing")
    visibility = validate_candidate_material_visibility(
        visibility_receipt, material_receipt=material_receipt
    )
    requests: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    rubrics: list[Mapping[str, Any]] = []
    for window in windows:
        request = build_candidate_base_rubric_request(
            window, material_receipt, visibility
        )
        raw = callback(_canonical(request))
        parsed = parse_window_rubric_response(raw, request=request)
        requests.append(request)
        responses.append(parsed)
        rubrics.extend(parsed["candidates"])
    consistent = validate_candidate_base_consistency(rubrics)
    result: dict[str, Any] = {
        "version": "candidate-base-rubric-evaluation-v1",
        "catalog_version": RUBRIC_CATALOG_VERSION,
        "catalog_hash": FACTOR_CATALOG_HASH,
        "factor_applicability_version": FACTOR_APPLICABILITY_VERSION,
        "requests": requests,
        "responses": responses,
        "rubrics": [consistent[key] for key in sorted(consistent)],
        "visibility_hash": visibility["visibility_hash"],
        "utility_status": "blocked_policy_missing",
    }
    if utility_policy is not None:
        result["utility_receipt"] = build_utility_receipt(
            result["rubrics"], policy=utility_policy
        )
        result["utility_status"] = "derived"
    else:
        result["utility_receipt"] = None
    result["evaluation_hash"] = _digest(
        {key: result[key] for key in result if key != "evaluation_hash"}
    )
    return result

def candidate_base_rubric_prompt_v2() -> str:
    """Opt-in prompt for structural coherence calibration only."""
    return ("仅评价 content_completeness、statement_clarity、internal_coherence。"
            "internal_coherence 只能检查悬空图表引用、未绑定符号、条件/选项结构矛盾及明确一致性；"
            "图表或选项缺失归 content_completeness。禁止判断答案、解法、正确性或可解性，也不得输出 score、penalty、utility、selection、难度或推理。")


__all__ = ["CandidateBaseRubricError", "CANDIDATE_MATERIAL_VISIBILITY_VERSION", "FACTOR_APPLICABILITY_VERSION", "FACTOR_CATALOG", "FACTOR_CATALOG_HASH", "RUBRIC_CATALOG_VERSION", "RUBRIC_REQUEST_VERSION", "RUBRIC_RESPONSE_VERSION", "WINDOW_RUBRIC_REQUEST_VERSION", "WINDOW_RUBRIC_RESPONSE_VERSION", "UTILITY_POLICY_VERSION", "UTILITY_RECEIPT_VERSION", "factor_catalog", "extend_factor_catalog", "candidate_base_key", "build_rubric_request", "build_window_rubric_request", "build_candidate_base_rubric_request", "build_candidate_material_visibility_receipt", "validate_candidate_material_visibility", "parse_rubric_response", "parse_window_rubric_response", "validate_candidate_base_consistency", "validate_utility_policy", "build_utility_policy", "compute_candidate_utility", "explain_candidate_utility", "build_utility_receipt", "build_graph_utility_receipt", "evaluate_candidate_base_rubric_windows", "RUBRIC_CATALOG_V2_VERSION", "RUBRIC_REQUEST_V2_VERSION", "RUBRIC_RESPONSE_V2_VERSION", "FACTOR_APPLICABILITY_V2_VERSION", "FACTOR_CATALOG_V2", "FACTOR_CATALOG_V2_HASH", "factor_catalog_v2", "build_rubric_request_v2", "parse_rubric_response_v2", "candidate_base_rubric_prompt_v2"]
__all__.append("candidate_base_key_v2")
