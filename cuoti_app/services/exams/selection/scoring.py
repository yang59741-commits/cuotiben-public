"""Validation and construction of frozen score and utility receipts."""

from __future__ import annotations

import math

import re

from builtins import (
    any as _builtin_any,
)

from typing import (
    Any,
    Mapping,
    Sequence,
)

from ...exam_generation_stage6_rubric import (
    FACTOR_APPLICABILITY_VERSION,
    FACTOR_CATALOG_HASH,
    RUBRIC_CATALOG_VERSION,
    UTILITY_RECEIPT_VERSION,
    CandidateBaseRubricError,
    candidate_base_key,
)

from .contracts import (
    FrozenCandidateEdgeGraph,
    STAGE6_SCORE_RECEIPT_VERSION,
    STAGE6_SCORING_IDENTITY_VERSION,
    Stage5ContractError,
    Stage5GlobalConstraintContract,
    _MAX_LOCAL_PENALTY,
    _MAX_LOCAL_SCORE,
    _digest,
    _exact_fields,
    _hash,
    _mapping,
    _text,
)

from .graph import (
    _initial_graph_from_expanded,
)



def _candidate_base_utility_receipt(
    value: Mapping[str, Any],
    graph: FrozenCandidateEdgeGraph,
    contract: Stage5GlobalConstraintContract,
) -> tuple[dict[tuple[str, str], tuple[int, int]], set[tuple[str, str]], str]:
    """Verify the new local-utility receipt and project it for the solver.

    The projection is an internal ``(utility, 0)`` tuple solely to reuse the
    existing branch-and-bound arithmetic.  The solver's new-mode assignment
    payload never serializes those legacy names; only ``utility`` is emitted.
    """

    raw = _mapping(value, "candidate_base_utility_receipt")
    fields = {
        "version", "graph_hash", "constraint_hash", "candidate_facts_hash",
        "projection_identity_hash", "catalog_version", "catalog_hash",
        "factor_applicability_version", "parameter_set_hash", "policy_version",
        "policy_hash", "diversity_policy_hash", "rejection_ledger_head_hash",
        "rejected_edges", "rubric_receipt_hash", "utilities", "receipt_hash",
    }
    if set(raw) != fields or raw.get("version") != UTILITY_RECEIPT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_receipt_shape_invalid", "候选级 utility 回执字段无效")
    graph_payload = graph.to_dict()
    if raw.get("graph_hash") != graph.graph_hash or raw.get("constraint_hash") != contract.constraint_hash or raw.get("candidate_facts_hash") != contract.candidate_facts_hash or raw.get("projection_identity_hash") != graph_payload.get("projection_identity_hash"):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "utility_receipt_identity_invalid", "候选级 utility 回执未绑定当前图")
    if raw.get("catalog_version") != RUBRIC_CATALOG_VERSION or raw.get("catalog_hash") != FACTOR_CATALOG_HASH or raw.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_receipt_catalog_invalid", "候选级 rubric 目录身份无效")
    for field in ("graph_hash", "constraint_hash", "candidate_facts_hash", "projection_identity_hash", "parameter_set_hash", "policy_hash", "rubric_receipt_hash", "rejection_ledger_head_hash"):
        _hash(raw.get(field), f"utility_{field}")
    _text(raw.get("policy_version"), "utility_policy_version", 128)
    if raw.get("diversity_policy_hash") is not None:
        _hash(raw.get("diversity_policy_hash"), "diversity_policy_hash")
    rejected_raw = raw.get("rejected_edges")
    if not isinstance(rejected_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edges_invalid", "候选级 utility 拒绝边集合无效")
    rejected: set[tuple[str, str]] = set()
    for item in rejected_raw:
        item = _mapping(item, "utility_rejected_edge")
        _exact_fields(item, {"slot_id", "candidate_id"}, "utility_rejected_edge")
        key = (_text(item["slot_id"], "utility_rejected_slot_id"), _text(item["candidate_id"], "utility_rejected_candidate_id"))
        if key in rejected:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edge_duplicate", "候选级 utility 拒绝边重复")
        rejected.add(key)
    ledger_hash = raw["rejection_ledger_head_hash"]
    if (rejected and ledger_hash == "0" * 64) or (not rejected and ledger_hash != "0" * 64):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejection_ledger_invalid", "候选级 utility 拒绝账本绑定无效")
    graph_edges = {(edge["slot_id"], edge["candidate_id"]): edge for edge in graph_payload.get("edges", [])}
    if not rejected.issubset(set(graph_edges)):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edge_unknown", "候选级 utility 拒绝边不在当前图")
    utilities = raw.get("utilities")
    if not isinstance(utilities, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entries_invalid", "候选级 utility 条目无效")
    result: dict[tuple[str, str], tuple[int, int]] = {}
    candidate_values: dict[str, int] = {}
    for item in utilities:
        item = _mapping(item, "utility_entry")
        _exact_fields(item, {"slot_id", "candidate_id", "candidate_base", "utility"}, "utility_entry")
        slot_id = _text(item["slot_id"], "utility_slot_id")
        candidate_id = _text(item["candidate_id"], "utility_candidate_id")
        key = (slot_id, candidate_id)
        if key not in graph_edges or key in rejected or key in result:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entry_identity_invalid", "候选级 utility 条目不是唯一冻结边")
        utility = item["utility"]
        if type(utility) is not int:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_value_invalid", "候选级 utility 必须是确定性整数")
        base = item["candidate_base"]
        if not isinstance(base, list) or len(base) != 8:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效")
        try:
            validated_base = candidate_base_key({
                "candidate_id": base[0],
                "candidate_fact_hash": base[1],
                "content_hash": base[2],
                "content_snapshot_hash": base[3] or None,
                "material_identity": base[4] or None,
                "catalog_version": base[5],
                "catalog_hash": base[6],
                "factor_applicability_version": base[7],
            })
        except CandidateBaseRubricError as exc:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效") from exc
        if list(validated_base) != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效")
        if base[0] != candidate_id or base[1] != graph_edges[key].get("candidate_fact_hash") or base[2] != graph_edges[key].get("content_hash"):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_base_key_mismatch", "候选级 utility 未绑定候选事实")
        if base[5] != RUBRIC_CATALOG_VERSION or base[6] != FACTOR_CATALOG_HASH or base[7] != FACTOR_APPLICABILITY_VERSION:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_catalog_mismatch", "候选级 identity 目录版本漂移")
        previous = candidate_values.get(candidate_id)
        if previous is not None and previous != utility:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_consistency_invalid", "同一候选跨槽 utility 不一致")
        candidate_values[candidate_id] = utility
        result[key] = (utility, 0)
    expected = set(graph_edges) - rejected
    if set(result) != expected:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entries_incomplete", "候选级 utility 未覆盖全部非拒绝冻结边")
    body = {key: raw[key] for key in fields - {"receipt_hash"}}
    receipt_hash = _hash(raw.get("receipt_hash"), "utility_receipt_hash")
    if _digest(body) != receipt_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "utility_receipt_hash_mismatch", "候选级 utility 回执哈希不匹配")
    return result, rejected, receipt_hash



def _score_receipt(value: Mapping[str, Any], graph: FrozenCandidateEdgeGraph, contract: Stage5GlobalConstraintContract) -> tuple[dict[tuple[str, str], tuple[int, int]], set[tuple[str, str]], str]:
    if isinstance(value, Mapping) and value.get("version") == UTILITY_RECEIPT_VERSION:
        return _candidate_base_utility_receipt(value, graph, contract)
    value = _mapping(value, "score_receipt")
    if set(value) != {"version", "graph_hash", "scoring_identity", "scores", "hash"} or value.get("version") != STAGE6_SCORE_RECEIPT_VERSION or value.get("graph_hash") != graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_receipt_identity_invalid", "评分回执身份无效")
    identity = _mapping(value.get("scoring_identity"), "scoring_identity")
    identity_fields = {"version", "constraint_hash", "base_graph_hash", "rejection_ledger_head_hash", "rejected_edges", "model_id", "prompt_version", "candidate_facts_hash", "profile_hash", "projection_identity_hash", "response_schema_version", "windows"}
    graph_payload, contract_payload = graph.to_dict(), contract.to_dict()
    if set(identity) != identity_fields or identity.get("version") != STAGE6_SCORING_IDENTITY_VERSION or identity.get("constraint_hash") != graph.constraint_hash or identity.get("candidate_facts_hash") != graph.candidate_facts_hash or identity.get("projection_identity_hash") != graph_payload.get("projection_identity_hash") or identity.get("profile_hash") != contract_payload.get("profile_hash"):
        raise Stage5ContractError("exam_stage5_contract_invalid", "scoring_identity_invalid", "评分回执完整来源身份无效")
    _text(identity.get("model_id"), "score_model_id", 128)
    _text(identity.get("prompt_version"), "score_prompt_version", 128)
    _text(identity.get("response_schema_version"), "score_response_schema_version", 128)
    expanded = graph_payload.get("graph_mode") == "expanded"
    base_graph = _initial_graph_from_expanded(graph) if expanded else None
    expected_base_hash = base_graph.graph_hash if base_graph is not None else None
    if identity.get("base_graph_hash") != expected_base_hash:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_base_graph_identity_invalid", "评分回执基础图身份无效")
    ledger_hash = _hash(identity.get("rejection_ledger_head_hash"), "rejection_ledger_head_hash")
    rejected_raw = identity.get("rejected_edges")
    if not isinstance(rejected_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edges_invalid", "评分拒绝边集合无效")
    rejected: set[tuple[str, str]] = set()
    for item in rejected_raw:
        item = _mapping(item, "rejected_edge")
        if set(item) != {"slot_id", "candidate_id"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_shape_invalid", "评分拒绝边字段无效")
        key = (_text(item.get("slot_id"), "rejected_slot_id"), _text(item.get("candidate_id"), "rejected_candidate_id"))
        if key in rejected:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_duplicate", "评分拒绝边重复")
        rejected.add(key)
    if (rejected and ledger_hash == "0" * 64) or (not rejected and ledger_hash != "0" * 64):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejection_ledger_binding_invalid", "评分拒绝边与账本头不一致")
    windows = identity.get("windows")
    if not isinstance(windows, list) or not windows:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_windows_invalid", "评分窗口身份无效")
    seen_windows = set()
    phase_counts = {"initial": 0, "expansion": 0}
    phase_windows: dict[str, list[dict[str, Any]]] = {"initial": [], "expansion": []}
    for window in windows:
        window = _mapping(window, "score_window")
        if set(window) != {"phase", "window_index", "graph_hash", "slot_ids", "window_hash", "request_hash", "material_hash", "numeric_response_hash"} or window.get("phase") not in {"initial", "expansion"} or type(window.get("window_index")) is not int or window["window_index"] < 1:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_identity_invalid", "评分窗口身份字段无效")
        slot_ids = window.get("slot_ids")
        if not isinstance(slot_ids, list) or not 1 <= len(slot_ids) <= 4 or len(set(slot_ids)) != len(slot_ids) or _builtin_any(not isinstance(slot_id, str) or re.fullmatch(r"slot-[0-9]{4}", slot_id) is None for slot_id in slot_ids):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "评分窗口槽位身份无效")
        key = (window["phase"], window["window_index"])
        if key in seen_windows:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_identity_duplicate", "评分窗口身份重复")
        seen_windows.add(key)
        phase_counts[window["phase"]] += 1
        phase_windows[window["phase"]].append(dict(window))
        expected_window_graph = expected_base_hash if expanded and window["phase"] == "initial" else graph.graph_hash
        if window.get("graph_hash") != expected_window_graph or (not expanded and window["phase"] != "initial"):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_graph_identity_invalid", "评分窗口未绑定对应阶段图")
        for field in ("graph_hash", "window_hash", "request_hash", "material_hash", "numeric_response_hash"):
            _hash(window.get(field), field)
    result: dict[tuple[str, str], tuple[int, int]] = {}
    if not isinstance(value["scores"], list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "scores_invalid", "评分条目无效")
    valid = {(edge["slot_id"], edge["candidate_id"]) for edge in graph_payload["edges"]}
    base_keys = {(edge["slot_id"], edge["candidate_id"]) for edge in (base_graph.to_dict()["edges"] if base_graph else graph_payload["edges"])}
    new_keys = valid - base_keys
    additions_by_slot: dict[str, int] = {}
    for slot_id, _candidate_id in new_keys:
        additions_by_slot[slot_id] = additions_by_slot.get(slot_id, 0) + 1
    question_count = contract.question_count
    edge_multiplier = 6 if expanded or graph_payload.get("graph_mode") == "repair-scope" else 4
    if len(valid) > edge_multiplier * question_count or _builtin_any(count > 2 for count in additions_by_slot.values()):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_graph_budget_exceeded", "评分图超过 4N/6N 或每槽扩展上限")
    expansion_limit = math.ceil(len(additions_by_slot) / 4)
    if phase_counts["initial"] != math.ceil(question_count / 4) or phase_counts["expansion"] > expansion_limit or (not expanded and phase_counts["expansion"]) or (new_keys - rejected and phase_counts["expansion"] == 0):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_count_invalid", "评分窗口调用身份超过初始或扩展上限")
    initial_windows = sorted(phase_windows["initial"], key=lambda item: item["window_index"])
    expected_slots = [slot["slot_id"] for slot in contract_payload["slots"]]
    expected_initial_groups = [expected_slots[index:index + 4] for index in range(0, len(expected_slots), 4)]
    if [window["window_index"] for window in initial_windows] != list(range(1, len(initial_windows) + 1)) or [window["slot_ids"] for window in initial_windows] != expected_initial_groups:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "初始评分窗口未完整规范绑定全部题槽")
    expansion_windows = sorted(phase_windows["expansion"], key=lambda item: item["window_index"])
    expansion_slot_ids = [slot_id for window in expansion_windows for slot_id in window["slot_ids"]]
    expected_expansion_groups = [expansion_slot_ids[index:index + 4] for index in range(0, len(expansion_slot_ids), 4)]
    if (
        [window["window_index"] for window in expansion_windows] != list(range(1, len(expansion_windows) + 1))
        or len(expansion_slot_ids) != len(set(expansion_slot_ids))
        or expansion_slot_ids != sorted(expansion_slot_ids)
        or not set(expansion_slot_ids).issubset(additions_by_slot)
        or [window["slot_ids"] for window in expansion_windows] != expected_expansion_groups
    ):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "扩展评分窗口槽位身份无效")
    if not rejected.issubset(new_keys):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_invalid", "评分拒绝边不是扩展新增边")
    for raw in value["scores"]:
        raw = _mapping(raw, "score")
        if set(raw) != {"slot_id", "candidate_id", "score", "penalty"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_fields_invalid", "评分条目字段无效")
        key = (_text(raw["slot_id"], "score_slot_id"), _text(raw["candidate_id"], "score_candidate_id"))
        score = raw["score"]
        penalty = raw["penalty"]
        if (
            key not in valid or key in result
            or type(score) is not int or not 0 <= score <= _MAX_LOCAL_SCORE
            or type(penalty) is not int or not 0 <= penalty <= min(_MAX_LOCAL_PENALTY, score)
        ):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_edge_invalid", "评分条目不是冻结边或整数")
        result[key] = (score, penalty)
    if set(result) & rejected or not base_keys.issubset(result) or set(result) | rejected != valid:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_edges_incomplete", "评分与显式拒绝未精确覆盖冻结图")
    scored_new_slots = {slot for slot, candidate in result if (slot, candidate) in new_keys}
    if scored_new_slots != set(expansion_slot_ids):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "扩展窗口未精确绑定成功评分的新增边槽位")
    normalized_scores = [
        {"slot_id": slot, "candidate_id": candidate, "score": result[(slot, candidate)][0], "penalty": result[(slot, candidate)][1]}
        for slot, candidate in sorted(result)
    ]
    normalized_identity = {**identity, "rejected_edges": [{"slot_id": slot, "candidate_id": candidate} for slot, candidate in sorted(rejected)], "windows": sorted((dict(item) for item in windows), key=lambda item: (item["phase"], item["window_index"], item["window_hash"]))}
    normalized_hash = _digest({"version": value["version"], "graph_hash": graph.graph_hash, "scoring_identity": normalized_identity, "scores": normalized_scores})
    if normalized_hash != _hash(value.get("hash"), "score_receipt_hash"):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "score_receipt_hash_mismatch", "评分回执哈希不匹配")
    return result, rejected, normalized_hash



def _local_v2_score_receipt(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, scores: Sequence[Mapping[str, Any]], *, purpose: str) -> dict[str, Any]:
    """Bind deterministic Stage-5 verification/repair scores to a v2 identity."""
    graph_payload, contract_payload = graph.to_dict(), contract.to_dict()
    numeric = sorted((dict(item) for item in scores), key=lambda item: (item["slot_id"], item["candidate_id"]))
    numeric_response_hash = _digest({"purpose": purpose, "graph_hash": graph.graph_hash, "scores": numeric})
    base_graph = _initial_graph_from_expanded(graph) if graph_payload.get("graph_mode") == "expanded" else None
    slot_ids = [slot["slot_id"] for slot in contract_payload["slots"]]
    initial_slot_groups = [slot_ids[index:index + 4] for index in range(0, len(slot_ids), 4)]
    windows = [{
        "phase": "initial", "window_index": index, "graph_hash": base_graph.graph_hash if base_graph else graph.graph_hash,
        "slot_ids": initial_slot_groups[index - 1],
        "window_hash": _digest({"purpose": purpose, "graph_hash": base_graph.graph_hash if base_graph else graph.graph_hash, "window_index": index}),
        "request_hash": _digest({"purpose": purpose, "constraint_hash": graph.constraint_hash, "window_index": index}),
        "material_hash": _digest({"candidate_facts_hash": graph.candidate_facts_hash}),
        "numeric_response_hash": numeric_response_hash,
    } for index in range(1, math.ceil(contract.question_count / 4) + 1)]
    if base_graph is not None and base_graph.edge_count < graph.edge_count:
        base_keys = {(edge["slot_id"], edge["candidate_id"]) for edge in base_graph.to_dict()["edges"]}
        affected_slots = sorted({edge["slot_id"] for edge in graph_payload["edges"] if (edge["slot_id"], edge["candidate_id"]) not in base_keys})
        affected_groups = [affected_slots[index:index + 4] for index in range(0, len(affected_slots), 4)]
        windows.extend({
            "phase": "expansion", "window_index": index, "graph_hash": graph.graph_hash,
            "slot_ids": affected_groups[index - 1],
            "window_hash": _digest({"purpose": purpose, "graph_hash": graph.graph_hash, "window_index": index}),
            "request_hash": _digest({"purpose": purpose, "constraint_hash": graph.constraint_hash, "phase": "expansion", "window_index": index}),
            "material_hash": _digest({"candidate_facts_hash": graph.candidate_facts_hash}),
            "numeric_response_hash": numeric_response_hash,
        } for index in range(1, len(affected_groups) + 1))
    identity = {
        "version": STAGE6_SCORING_IDENTITY_VERSION,
        "constraint_hash": graph.constraint_hash,
        "base_graph_hash": base_graph.graph_hash if base_graph else None,
        "rejection_ledger_head_hash": "0" * 64,
        "rejected_edges": [],
        "model_id": "stage5-local-deterministic",
        "prompt_version": purpose,
        "candidate_facts_hash": graph.candidate_facts_hash,
        "profile_hash": contract_payload["profile_hash"],
        "projection_identity_hash": graph_payload["projection_identity_hash"],
        "response_schema_version": "stage5-local-numeric-v2",
        "windows": sorted(windows, key=lambda item: (item["phase"], item["window_index"], item["window_hash"])),
    }
    body = {"version": STAGE6_SCORE_RECEIPT_VERSION, "graph_hash": graph.graph_hash, "scoring_identity": identity, "scores": numeric}
    return {**body, "hash": _digest(body)}



def _local_candidate_base_utility_receipt(
    graph: FrozenCandidateEdgeGraph,
    source_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Project an authenticated utility receipt onto an ephemeral repair graph."""

    source = _mapping(source_receipt, "source_utility_receipt")
    if source.get("version") != UTILITY_RECEIPT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "source_utility_receipt_invalid", "修复 utility 来源版本无效")
    by_key = {
        (item["slot_id"], item["candidate_id"]): item
        for item in source.get("utilities", [])
        if isinstance(item, Mapping)
    }
    by_candidate: dict[str, Mapping[str, Any]] = {}
    for item in by_key.values():
        candidate_id = item.get("candidate_id")
        base = item.get("candidate_base")
        if not isinstance(candidate_id, str) or not isinstance(base, list) or len(base) != 8 or base[0] != candidate_id:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效")
        try:
            validated_base = candidate_base_key({
                "candidate_id": base[0],
                "candidate_fact_hash": base[1],
                "content_hash": base[2],
                "content_snapshot_hash": base[3] or None,
                "material_identity": base[4] or None,
                "catalog_version": base[5],
                "catalog_hash": base[6],
                "factor_applicability_version": base[7],
            })
        except CandidateBaseRubricError as exc:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效") from exc
        if list(validated_base) != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效")
        previous = by_candidate.get(candidate_id)
        if previous is not None and previous.get("candidate_base") != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_ambiguous", "修复 utility 候选基础身份不唯一")
        by_candidate[candidate_id] = item
    utilities: list[dict[str, Any]] = []
    for edge in graph.to_dict()["edges"]:
        item = by_key.get((edge["slot_id"], edge["candidate_id"]))
        if item is None:
            item = by_candidate.get(edge["candidate_id"])
        if item is None:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_utility_missing", "修复图存在未评分候选边")
        base = item["candidate_base"]
        if base[0] != edge["candidate_id"] or base[1] != edge.get("candidate_fact_hash") or base[2] != edge.get("content_hash"):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_candidate_base_mismatch", "修复 utility 未绑定候选事实")
        utilities.append({
            "slot_id": edge["slot_id"],
            "candidate_id": edge["candidate_id"],
            "candidate_base": list(item["candidate_base"]),
            "utility": item["utility"],
        })
    payload = graph.to_dict()
    body = {
        "version": UTILITY_RECEIPT_VERSION,
        "graph_hash": graph.graph_hash,
        "constraint_hash": graph.constraint_hash,
        "candidate_facts_hash": graph.candidate_facts_hash,
        "projection_identity_hash": payload["projection_identity_hash"],
        "catalog_version": source["catalog_version"],
        "catalog_hash": source["catalog_hash"],
        "factor_applicability_version": source["factor_applicability_version"],
        "parameter_set_hash": source["parameter_set_hash"],
        "policy_version": source["policy_version"],
        "policy_hash": source["policy_hash"],
        "diversity_policy_hash": source["diversity_policy_hash"],
        "rejection_ledger_head_hash": "0" * 64,
        "rejected_edges": [],
        "rubric_receipt_hash": source["rubric_receipt_hash"],
        "utilities": sorted(utilities, key=lambda item: (item["slot_id"], item["candidate_id"])),
    }
    return {**body, "receipt_hash": _digest(body)}
