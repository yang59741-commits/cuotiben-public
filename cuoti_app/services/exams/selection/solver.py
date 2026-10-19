"""Feasibility witnesses, exact selection and bounded local repair."""

from __future__ import annotations

import hashlib

import itertools

from builtins import (
    any as _builtin_any,
)

from typing import (
    Any,
    Callable,
    Mapping,
    Sequence,
)

from ...exam_generation_stage6_rubric import (
    UTILITY_RECEIPT_VERSION,
)

from .contracts import (
    CANDIDATE_GRAPH_VERSION,
    FrozenCandidateEdgeGraph,
    REPAIR_VERSION,
    SELECTION_VERSION,
    SOLVER_VERSION,
    Stage5ContractError,
    Stage5GlobalConstraintContract,
    Stage5InfeasibleError,
    Stage5RepairResult,
    Stage5SearchIncompleteError,
    Stage5Selection,
    WITNESS_VERSION,
    _canonical,
    _digest,
    _hash,
    _mapping,
    _text,
    _text_list,
    _verified_contract_payload,
    _verified_graph_payload,
)

from .scoring import (
    _local_candidate_base_utility_receipt,
    _local_v2_score_receipt,
    _score_receipt,
)



def _maximum_matching(slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, str], dict[str, str]]:
    candidate_to_slot: dict[str, str] = {}
    slot_to_candidate: dict[str, str] = {}
    def visit(slot: str, seen: set[str]) -> bool:
        for edge in edges_by_slot.get(slot, ()):
            candidate = str(edge["candidate_id"])
            if candidate in seen:
                continue
            seen.add(candidate)
            owner = candidate_to_slot.get(candidate)
            if owner is None or visit(owner, seen):
                candidate_to_slot[candidate] = slot
                slot_to_candidate[slot] = candidate
                return True
        return False
    for slot in slots:
        visit(slot, set())
    return slot_to_candidate, candidate_to_slot



def _minimal_hall_set(
    slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]], resource_field: str,
) -> list[str] | None:
    projected = {
        slot: [{**edge, "candidate_id": edge[resource_field]} for edge in edges_by_slot.get(slot, ())]
        for slot in slots
    }
    slot_matching, resource_matching = _maximum_matching(slots, projected)
    if len(slot_matching) == len(slots):
        return None
    # Alternating reachability from unmatched left vertices yields a deficient Hall set.
    reachable_slots = set(slots) - set(slot_matching)
    pending = list(sorted(reachable_slots))
    reachable_resources: set[str] = set()
    while pending:
        slot = pending.pop(0)
        matched_resource = slot_matching.get(slot)
        for edge in projected.get(slot, ()):
            resource = str(edge["candidate_id"])
            if resource == matched_resource or resource in reachable_resources:
                continue
            reachable_resources.add(resource)
            owner = resource_matching.get(resource)
            if owner is not None and owner not in reachable_slots:
                reachable_slots.add(owner)
                pending.append(owner)
    # N is contract-bounded at 20.  Bitset enumeration therefore gives a true
    # minimum-cardinality witness (then lexicographically first), rather than
    # merely an inclusion-minimal result dependent on deletion order.
    resource_ids = sorted({str(edge[resource_field]) for slot in slots for edge in edges_by_slot.get(slot, ())})
    resource_bits = {resource: 1 << index for index, resource in enumerate(resource_ids)}
    masks = {
        slot: sum({resource_bits[str(edge[resource_field])] for edge in edges_by_slot.get(slot, ())})
        for slot in slots
    }
    upper_bound = len(reachable_slots)
    for size in range(1, upper_bound + 1):
        for trial in itertools.combinations(sorted(slots), size):
            neighbors = 0
            for slot in trial:
                neighbors |= masks[slot]
            if neighbors.bit_count() < size:
                return list(trial)
    raise AssertionError("maximum matching lacked a Hall witness")



def _hall_witness(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    failures = [
        (field, deficient)
        for field in ("candidate_id", "content_hash")
        if (deficient := _minimal_hall_set(slots, edges_by_slot, field)) is not None
    ]
    resource_field, deficient = min(failures, key=lambda item: (len(item[1]), item[1], item[0]))
    candidates = sorted({str(edge["candidate_id"]) for slot in deficient for edge in edges_by_slot.get(slot, ())})
    fingerprints = sorted({str(edge["content_hash"]) for slot in deficient for edge in edges_by_slot.get(slot, ())})
    deficient_resources = candidates if resource_field == "candidate_id" else fingerprints
    body = {
        "version": WITNESS_VERSION, "kind": "hall_min_cut", "constraint_hash": contract.constraint_hash,
        "graph_hash": graph.graph_hash, "deficient_slot_ids": deficient,
        "reachable_candidate_ids": candidates, "reachable_content_hashes": fingerprints,
        "resource_kind": resource_field, "deficiency": len(deficient) - len(deficient_resources),
        "constraint_ids": ["candidate_unique", "content_hash_unique"],
        "counts": {"slots": len(deficient), "candidates": len(candidates), "content_hashes": len(fingerprints)},
        "actions": ["expand_minimal_deficient_slots", "repair_candidate_or_content_identity"],
    }
    return {**body, "witness_hash": _digest(body)}



def _locate_structural_graph_deficit(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
) -> Mapping[str, Any] | None:
    """Locate a candidate/content Hall deficit without scoring."""

    c = _verified_contract_payload(contract)
    g = _verified_graph_payload(graph, contract=contract)
    slots = [slot["slot_id"] for slot in c["slots"]]
    edges_by_slot: dict[str, list[Mapping[str, Any]]] = {slot_id: [] for slot_id in slots}
    for edge in g["edges"]:
        edges_by_slot[edge["slot_id"]].append(edge)
    for edges in edges_by_slot.values():
        edges.sort(key=lambda edge: (edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]))
    candidate_matching, _ = _maximum_matching(slots, edges_by_slot)
    fingerprint_edges = {
        slot_id: [{**edge, "candidate_id": edge["content_hash"]} for edge in edges]
        for slot_id, edges in edges_by_slot.items()
    }
    fingerprint_matching, _ = _maximum_matching(slots, fingerprint_edges)
    if len(candidate_matching) == len(slots) and len(fingerprint_matching) == len(slots):
        return None
    return _hall_witness(contract, graph, slots, edges_by_slot)



def locate_candidate_graph_deficit(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
    *,
    search_guard: Callable[[], bool] | None = None,
) -> Mapping[str, Any] | None:
    """Locate any hard-constraint deficit with local deterministic feasibility.

    The neutral receipt exists only to drive the exact local solver through
    source/chapter/coverage/diversity constraints.  It performs no provider or
    injected scoring-adapter activity and cannot affect a later real score.
    """

    structural = _locate_structural_graph_deficit(contract, graph)
    if structural is not None:
        return structural
    neutral = [
        {
            "slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"],
            "score": 0, "penalty": 0,
        }
        for edge in graph.to_dict()["edges"]
    ]
    receipt = _local_v2_score_receipt(
        contract, graph, neutral, purpose="stage5-local-feasibility-v2"
    )
    try:
        _solve_global_selection(
            contract, graph, score_receipt=receipt,
            feasibility_only=True, search_guard=search_guard,
        )
    except Stage5InfeasibleError as exc:
        return exc.witness
    return None



def solve_global_selection(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, *, score_receipt: Mapping[str, Any]) -> Stage5Selection:
    """Use exact branch-and-bound over a frozen utility receipt.

    Legacy ``local-score-v2`` receipts are still accepted only for historical
    compatibility.  Candidate-base receipts use the same feasibility engine
    but serialize only locally derived ``utility``; no model score/penalty is
    introduced into the new selection payload.
    """

    selection = _solve_global_selection(contract, graph, score_receipt=score_receipt)
    assert selection is not None
    return selection



def _solve_global_selection(
    contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, *,
    score_receipt: Mapping[str, Any], feasibility_only: bool = False,
    search_guard: Callable[[], bool] | None = None,
) -> Stage5Selection | None:
    c = _verified_contract_payload(contract)
    g = _verified_graph_payload(graph, contract=contract)
    utility_mode = isinstance(score_receipt, Mapping) and score_receipt.get("version") == UTILITY_RECEIPT_VERSION
    scores, rejected_edges, score_hash = _score_receipt(score_receipt, graph, contract)
    edges_by_slot: dict[str, list[dict[str, Any]]] = {slot["slot_id"]: [] for slot in c["slots"]}
    for edge in g["edges"]:
        if (edge["slot_id"], edge["candidate_id"]) in rejected_edges:
            continue
        score, penalty = scores[(edge["slot_id"], edge["candidate_id"])]
        utility = score if utility_mode else score - penalty
        candidate_edge = {**edge, "utility": utility}
        if not utility_mode:
            candidate_edge.update({"score": score, "penalty": penalty})
        edges_by_slot[edge["slot_id"]].append(candidate_edge)
    for edges in edges_by_slot.values():
        edges.sort(key=lambda edge: (-edge["utility"], edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]))
    slots = [slot["slot_id"] for slot in c["slots"]]
    candidate_matching, _ = _maximum_matching(slots, edges_by_slot)
    fingerprint_matching, _ = _maximum_matching(slots, {slot: [{**edge, "candidate_id": edge["content_hash"]} for edge in edges] for slot, edges in edges_by_slot.items()})
    structural_witness = None if len(candidate_matching) == len(slots) and len(fingerprint_matching) == len(slots) else _hall_witness(contract, graph, slots, edges_by_slot)
    if structural_witness is not None:
        raise Stage5InfeasibleError(structural_witness)
    source_targets = dict(c["source_quotas"])
    chapter_targets = dict(c["ordinary_chapter_quotas"])
    diversity_payload = _mapping(c["diversity_policy"]["payload"], "diversity_payload")
    max_source_exam = diversity_payload.get("max_per_source_exam", len(slots))
    if type(max_source_exam) is not int or max_source_exam < 1:
        raise Stage5ContractError("exam_stage5_contract_invalid", "diversity_max_invalid", "多样性上限无效")
    coverage_payload = _mapping(c["coverage_policy"]["payload"], "coverage_payload")
    minima = coverage_payload.get("minima", [])
    if not isinstance(minima, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minima_invalid", "覆盖最低约束无效")
    normalized_minima: list[tuple[str, str, str, int]] = []
    for item in minima:
        item = _mapping(item, "coverage_minimum")
        if set(item) != {"id", "field", "value", "min_count"} or item["field"] not in {"source_kind", "chapter", "question_type_family", "section_id"} or type(item["min_count"]) is not int or item["min_count"] < 0:
            raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minimum_invalid", "覆盖最低约束字段无效")
        normalized_minima.append((_text(item["id"], "coverage_id"), item["field"], _text(item["value"], "coverage_value"), item["min_count"]))
    best: tuple[int, tuple[tuple[int, str, str, int], ...], list[dict[str, Any]]] | None = None
    remaining = set(slots)
    chosen: list[dict[str, Any]] = []
    used_candidates: set[str] = set()
    used_content: set[str] = set()
    source_counts = {key: 0 for key in source_targets}
    chapter_counts = {key: 0 for key in chapter_targets}
    source_exam_counts: dict[str, int] = {}
    coverage_counts = {item[0]: 0 for item in normalized_minima}

    def eligible(edge: Mapping[str, Any]) -> bool:
        source = edge["source_kind"]
        chapter = edge["chapter"]
        exam = edge["source_exam_id"]
        return (
            edge["candidate_id"] not in used_candidates and edge["content_hash"] not in used_content
            and source_counts[source] < source_targets[source]
            and (source != "ordinary" or chapter in chapter_targets and chapter_counts[chapter] < chapter_targets[chapter])
            and (exam is None or source_exam_counts.get(exam, 0) < max_source_exam)
        )

    def bounds_possible() -> bool:
        left = len(remaining)
        if _builtin_any(count > source_targets[key] or count + left < source_targets[key] for key, count in source_counts.items()): return False
        if _builtin_any(count > chapter_targets[key] or count + left < chapter_targets[key] for key, count in chapter_counts.items()): return False
        if _builtin_any(coverage_counts[cid] + left < minimum for cid, _, _, minimum in normalized_minima): return False
        return True

    def visit(utility: int) -> bool:
        nonlocal best
        if search_guard is not None and not search_guard():
            raise Stage5SearchIncompleteError()
        if not remaining:
            if source_counts != source_targets or chapter_counts != chapter_targets or _builtin_any(coverage_counts[cid] < minimum for cid, _, _, minimum in normalized_minima):
                return False
            # Feasibility needs one witness, not the optimum among all zero-score ties.
            if feasibility_only:
                return True
            ordered = sorted(chosen, key=lambda edge: edge["position"])
            tie = tuple((edge["position"], edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]) for edge in ordered)
            candidate = (utility, tie, [dict(edge) for edge in ordered])
            if best is None or utility > best[0] or utility == best[0] and tie < best[1]: best = candidate
            return False
        domains = [(sum(1 for edge in edges_by_slot[slot] if eligible(edge)), slot) for slot in remaining]
        if best is not None:
            optimistic = utility
            for candidate_slot in remaining:
                available = [edge["utility"] for edge in edges_by_slot[candidate_slot] if eligible(edge)]
                if not available:
                    return False
                optimistic += max(available)
            if optimistic < best[0]:
                return False
        _, slot = min(domains, key=lambda item: (item[0], item[1]))
        if not _builtin_any(eligible(edge) for edge in edges_by_slot[slot]): return False
        remaining.remove(slot)
        for edge in edges_by_slot[slot]:
            if not eligible(edge): continue
            chosen.append(edge); used_candidates.add(edge["candidate_id"]); used_content.add(edge["content_hash"])
            source_counts[edge["source_kind"]] += 1
            if edge["source_kind"] == "ordinary": chapter_counts[edge["chapter"]] += 1
            if edge["source_exam_id"] is not None: source_exam_counts[edge["source_exam_id"]] = source_exam_counts.get(edge["source_exam_id"], 0) + 1
            hit: list[str] = []
            for cid, field, value, _minimum in normalized_minima:
                if edge[field] == value: coverage_counts[cid] += 1; hit.append(cid)
            feasible = bounds_possible() and visit(utility + edge["utility"])
            for cid in hit: coverage_counts[cid] -= 1
            if edge["source_exam_id"] is not None: source_exam_counts[edge["source_exam_id"]] -= 1
            if edge["source_kind"] == "ordinary": chapter_counts[edge["chapter"]] -= 1
            source_counts[edge["source_kind"]] -= 1
            used_candidates.remove(edge["candidate_id"]); used_content.remove(edge["content_hash"]); chosen.pop()
            if feasible:
                remaining.add(slot)
                return True
        remaining.add(slot)
        return False

    if visit(0):
        return None
    if best is None:
        source_capacity = {
            source: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge["source_kind"] == source})
            for source in source_targets
        }
        chapter_capacity = {
            chapter: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge["source_kind"] == "ordinary" and edge["chapter"] == chapter})
            for chapter in chapter_targets
        }
        coverage_capacity = {
            cid: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge[field] == value})
            for cid, field, value, _minimum in normalized_minima
        }
        # Prefer a deterministic smallest direct hard-constraint core.  Forced
        # unrelated singleton slots are not reported as affected merely because
        # the full paper is infeasible.
        direct_cores: list[tuple[int, tuple[str, ...], str, int]] = []
        def core_slots(predicate: Any) -> tuple[str, ...]:
            relevant = tuple(sorted(
                slot for slot, domain in edges_by_slot.items()
                if _builtin_any(predicate(edge) for edge in domain) or len(domain) > 1
            ))
            return relevant or (min(slots),)
        for source, target in source_targets.items():
            shortage = target - source_capacity[source]
            if shortage > 0:
                members = core_slots(lambda edge, source=source: edge["source_kind"] == source)
                direct_cores.append((len(members), members, "source_exact_quota", shortage))
        for chapter, target in chapter_targets.items():
            shortage = target - chapter_capacity[chapter]
            if shortage > 0:
                members = core_slots(lambda edge, chapter=chapter: edge["source_kind"] == "ordinary" and edge["chapter"] == chapter)
                direct_cores.append((len(members), members, "ordinary_chapter_exact_quota", shortage))
        for cid, field, value, minimum in normalized_minima:
            shortage = minimum - coverage_capacity[cid]
            if shortage > 0:
                members = core_slots(lambda edge, field=field, value=value: edge[field] == value)
                direct_cores.append((len(members), members, cid, shortage))
        exam_resources: dict[str, set[str]] = {}
        no_exam_resources: set[str] = set()
        for domain in edges_by_slot.values():
            for edge in domain:
                if edge["source_kind"] != "past_exam_other":
                    continue
                if edge["source_exam_id"] is None:
                    no_exam_resources.add(edge["content_hash"])
                else:
                    exam_resources.setdefault(edge["source_exam_id"], set()).add(edge["content_hash"])
        diversity_capacity = len(no_exam_resources) + sum(min(max_source_exam, len(values)) for values in exam_resources.values())
        diversity_shortage = source_targets.get("past_exam_other", 0) - diversity_capacity
        if diversity_shortage > 0:
            members = core_slots(lambda edge: edge["source_kind"] == "past_exam_other")
            direct_cores.append((len(members), members, "source_exam_diversity", diversity_shortage))
        if direct_cores:
            _size, deficient_slots, core_constraint, deficiency = min(direct_cores, key=lambda item: (item[0], item[1], item[2]))
            constraint_ids = [core_constraint]
        else:
            deficient_slots = tuple(slots)
            deficiency = 1
            constraint_ids = ["source_exact_quota", "ordinary_chapter_exact_quota", "source_exam_diversity", *sorted(item[0] for item in normalized_minima)]
        core_edges = [edge for slot in deficient_slots for edge in edges_by_slot[slot]]
        body = {
            "version": WITNESS_VERSION, "kind": "constrained_min_cut", "constraint_hash": contract.constraint_hash,
            "graph_hash": graph.graph_hash, "deficient_slot_ids": list(deficient_slots),
            "reachable_candidate_ids": sorted({edge["candidate_id"] for edge in core_edges}),
            "reachable_content_hashes": sorted({edge["content_hash"] for edge in core_edges}),
            "deficiency": deficiency, "constraint_ids": constraint_ids,
            "counts": {
                "source_targets": source_targets, "source_capacity": source_capacity,
                "chapter_targets": chapter_targets, "chapter_capacity": chapter_capacity,
                "coverage_minima": {item[0]: item[3] for item in normalized_minima}, "coverage_capacity": coverage_capacity,
            },
            "actions": ["expand_only_affected_slots", "repair_versioned_quota_or_coverage_fact", "do_not_relax_constraints"],
        }
        raise Stage5InfeasibleError({**body, "witness_hash": _digest(body)})
    utility, _tie, selected = best
    assignment_fields = (
        "slot_id", "position", "candidate_id", "candidate_fact_hash", "content_hash",
        "source_kind", "chapter", "source_exam_id", "section_id",
        "question_type_family", "target_difficulty", "bucket_rank", "utility",
    )
    if not utility_mode:
        assignment_fields = assignment_fields[:-1] + ("score", "penalty", "utility")
    assignments = tuple({key: edge[key] for key in assignment_fields} for edge in selected)
    identity = {
        "contract_version": SELECTION_VERSION, "solver_version": SOLVER_VERSION,
        "stage3_contract_hash": contract.stage3_contract_hash, "candidate_facts_hash": contract.candidate_facts_hash,
        "projection_identity_hash": contract.projection_identity_hash, "profile_hash": c["profile_hash"], "exact_slot_hash": c["exact_slot_hash"],
        "graph_hash": graph.graph_hash, "constraint_hash": contract.constraint_hash, "score_identity_hash": score_hash,
        "assignments": list(assignments),
    }
    selection_hash = _digest(identity)
    payload = {**identity, "total_utility": utility, "selection_hash": selection_hash}
    raw = _canonical(payload)
    return Stage5Selection(SELECTION_VERSION, graph.graph_hash, contract.constraint_hash, score_hash, utility, selection_hash, assignments, raw)



def _verified_selection(
    value: Stage5Selection, contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph,
) -> dict[str, Any]:
    if not isinstance(value, Stage5Selection):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_selection_type_invalid", "待修复选择不是阶段 5 选择合同")
    payload = value.to_dict()
    if value.constraint_hash != contract.constraint_hash or value.graph_hash != graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_selection_identity_mismatch", "待修复选择未绑定基础图与全局约束")
    identity = {key: payload[key] for key in (
        "contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash",
        "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash",
        "constraint_hash", "score_identity_hash", "assignments",
    )}
    if (
        payload.get("contract_version") != SELECTION_VERSION
        or payload.get("selection_hash") != _digest(identity)
        or payload.get("selection_hash") != value.selection_hash
        or payload.get("total_utility") != value.total_utility
        or tuple(payload.get("assignments", ())) != value.assignments
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_selection_hash_mismatch", "待修复选择哈希不匹配")
    return payload



def _verified_witness(
    value: Mapping[str, Any], contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    witness = dict(_mapping(value, "repair_witness"))
    witness_hash = witness.pop("witness_hash", None)
    if (
        witness.get("version") != WITNESS_VERSION
        or witness.get("kind") not in {"hall_min_cut", "constrained_min_cut"}
        or witness.get("constraint_hash") != contract.constraint_hash
        or witness.get("graph_hash") != graph.graph_hash
        or _digest(witness) != _hash(witness_hash, "witness_hash")
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_witness_identity_mismatch", "修复见证未绑定基础图与全局约束")
    valid = {slot["slot_id"] for slot in contract.to_dict()["slots"]}
    affected = tuple(sorted(_text_list(witness.get("deficient_slot_ids"), "deficient_slot_ids")))
    if not affected or not set(affected).issubset(valid):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_slots_invalid", "修复见证槽位无效")
    verified = {**witness, "witness_hash": witness_hash}
    neutral_scores = [
        {"slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"], "score": 0, "penalty": 0}
        for edge in sorted(graph.to_dict()["edges"], key=lambda item: (item["slot_id"], item["candidate_id"]))
    ]
    try:
        solve_global_selection(contract, graph, score_receipt=_local_v2_score_receipt(contract, graph, neutral_scores, purpose="stage5-witness-verification-v2"))
    except Stage5InfeasibleError as exc:
        if exc.witness != verified:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_not_reproducible", "修复见证不是基础图的稳定最小见证") from exc
    else:
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_not_reproducible", "基础图可行，不能接受不可行见证")
    return verified, affected



def _validate_repair_expansion(
    contract: Stage5GlobalConstraintContract,
    base_graph: FrozenCandidateEdgeGraph,
    expanded_graph: FrozenCandidateEdgeGraph | None,
    allowed_slots: set[str],
) -> FrozenCandidateEdgeGraph:
    if expanded_graph is None:
        return base_graph
    if not isinstance(expanded_graph, FrozenCandidateEdgeGraph):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expanded_graph_type_invalid", "修复扩展图类型无效")
    base, expanded = base_graph.to_dict(), expanded_graph.to_dict()
    if _digest(expanded) != expanded_graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_expanded_graph_hash_mismatch", "修复扩展图哈希不匹配")
    if base.get("graph_mode") != "initial":
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_second_expansion_forbidden", "阶段 5 修复只允许一个扩展版本")
    if (
        expanded.get("graph_mode") != "expanded"
        or expanded_graph.constraint_hash != contract.constraint_hash
        or expanded.get("candidate_facts_hash") != base.get("candidate_facts_hash")
        or expanded.get("projection_identity_hash") != base.get("projection_identity_hash")
        or expanded.get("effective_difficulty_hash") != base.get("effective_difficulty_hash")
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_expanded_graph_identity_mismatch", "修复扩展图依赖身份不匹配")
    expanded_slots = set(_text_list(expanded.get("expanded_slot_ids"), "expanded_slot_ids"))
    if not expanded_slots or not expanded_slots.issubset(allowed_slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_scope_invalid", "扩展只能发生在见证影响槽")
    def by_slot(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for edge in payload["edges"]:
            result.setdefault(edge["slot_id"], []).append(edge)
        return result
    before, after = by_slot(base), by_slot(expanded)
    if _builtin_any(expanded["candidates"].get(candidate_id) != candidate for candidate_id, candidate in base["candidates"].items()):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_candidate_mutated", "修复扩展不得改写基础候选事实")
    for slot_id in sorted(set(before) | set(after)):
        old, new = before.get(slot_id, []), after.get(slot_id, [])
        if new[:len(old)] != old or len(new) - len(old) > 2 or (slot_id not in expanded_slots and new != old):
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_not_additive", "修复扩展必须逐槽追加且每槽最多两个候选")
    n = contract.question_count
    candidate_union = {edge["candidate_id"] for edge in expanded["edges"]}
    if len(expanded["edges"]) > 6 * n or len(candidate_union) > 6 * n or set(expanded["candidates"]) != candidate_union:
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_budget_exceeded", "修复扩展超过 6N 冻结预算")
    return expanded_graph



def _repair_component(
    contract_payload: Mapping[str, Any], graph_payload: Mapping[str, Any], seeds: set[str],
) -> tuple[str, ...]:
    """Stable connectivity over every hard resource that can couple assignments."""
    slots = {slot["slot_id"] for slot in contract_payload["slots"]}
    edges_by_slot = {slot: [] for slot in slots}
    for edge in graph_payload["edges"]:
        edges_by_slot[edge["slot_id"]].append(edge)
    minima = contract_payload["coverage_policy"]["payload"]["minima"]
    features: dict[str, set[str]] = {slot: set() for slot in slots}
    for slot, edges in edges_by_slot.items():
        sources = {edge["source_kind"] for edge in edges}
        chapters = {edge["chapter"] for edge in edges if edge["source_kind"] == "ordinary"}
        for edge in edges:
            features[slot].add("candidate:" + edge["candidate_id"])
            features[slot].add("content:" + edge["content_hash"])
            features[slot].add("source:" + edge["source_kind"])
            if edge["source_kind"] == "ordinary":
                features[slot].add("chapter:" + str(edge["chapter"]))
            if edge["source_exam_id"] is not None:
                features[slot].add("exam:" + edge["source_exam_id"])
            for item in minima:
                if edge[item["field"]] == item["value"]:
                    features[slot].add("coverage:" + item["id"])
        # A fixed-category domain cannot transfer exact quota to another slot.
        if len(sources) == 1:
            features[slot].discard("source:" + next(iter(sources)))
        if len(chapters) == 1 and sources == {"ordinary"}:
            features[slot].discard("chapter:" + str(next(iter(chapters))))
        for item in minima:
            hits = {edge[item["field"]] == item["value"] for edge in edges}
            if hits != {False, True}:
                features[slot].discard("coverage:" + item["id"])
    owners: dict[str, set[str]] = {}
    for slot, values in features.items():
        for value in values:
            owners.setdefault(value, set()).add(slot)
    reached = set(seeds)
    pending = sorted(seeds)
    while pending:
        slot = pending.pop(0)
        neighbors = set().union(*(owners[value] for value in features.get(slot, ()))) if features.get(slot) else set()
        for neighbor in sorted(neighbors - reached):
            reached.add(neighbor)
            pending.append(neighbor)
    return tuple(sorted(reached))



def repair_global_selection(
    contract: Stage5GlobalConstraintContract,
    base_graph: FrozenCandidateEdgeGraph,
    *,
    score_receipt: Mapping[str, Any],
    witness: Mapping[str, Any] | None = None,
    selection: Stage5Selection | None = None,
    rejected_edge: Mapping[str, Any] | None = None,
    rejected_candidate_id: str | None = None,
    rejected_content_hash: str | None = None,
    expanded_graph: FrozenCandidateEdgeGraph | None = None,
) -> Stage5RepairResult:
    """Atomically exact-resolve the smallest hard-constraint dependency component.

    There are two closed input modes: an authenticated infeasibility witness, or
    an authenticated existing selection plus exactly one rejected edge/resource.
    No partial replacement is ever returned.
    """
    if not isinstance(contract, Stage5GlobalConstraintContract) or not isinstance(base_graph, FrozenCandidateEdgeGraph):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_input_type_invalid", "阶段 5 修复输入类型无效")
    if base_graph.constraint_hash != contract.constraint_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_base_graph_constraint_mismatch", "基础图未绑定全局约束")
    c, base = contract.to_dict(), base_graph.to_dict()
    if _digest(c) != contract.constraint_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_constraint_hash_mismatch", "全局约束合同哈希不匹配")
    if (
        _digest(base) != base_graph.graph_hash
        or base.get("constraint_hash") != contract.constraint_hash
        or base.get("candidate_facts_hash") != contract.candidate_facts_hash
        or base.get("projection_identity_hash") != contract.projection_identity_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_base_graph_hash_mismatch", "基础图依赖身份或哈希不匹配")
    witness_value: dict[str, Any] | None = None
    witness_slots: tuple[str, ...] = ()
    if witness is not None:
        witness_value, witness_slots = _verified_witness(witness, contract, base_graph)
    rejection_values = [rejected_edge is not None, rejected_candidate_id is not None, rejected_content_hash is not None]
    witness_mode = witness_value is not None and selection is None and not _builtin_any(rejection_values)
    selection_mode = selection is not None and witness_value is None and sum(rejection_values) == 1
    if not (witness_mode or selection_mode):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_binding_incomplete", "修复必须绑定可信见证，或绑定已有选择与一个明确拒绝资源")
    selection_payload = _verified_selection(selection, contract, base_graph) if selection is not None else None

    rejection: dict[str, Any] | None = None
    seeds = set(witness_slots)
    if selection_payload is not None:
        assignments = list(selection_payload["assignments"])
        if rejected_edge is not None:
            edge = _mapping(rejected_edge, "rejected_edge")
            if set(edge) != {"slot_id", "candidate_id"}:
                raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_shape_invalid", "拒绝边必须只包含槽位与候选身份")
            rejection = {"kind": "edge", "slot_id": _text(edge["slot_id"], "rejected_slot_id"), "candidate_id": _text(edge["candidate_id"], "rejected_candidate_id")}
            matched = [item for item in assignments if item["slot_id"] == rejection["slot_id"] and item["candidate_id"] == rejection["candidate_id"]]
        elif rejected_candidate_id is not None:
            rejection = {"kind": "candidate_id", "candidate_id": _text(rejected_candidate_id, "rejected_candidate_id")}
            matched = [item for item in assignments if item["candidate_id"] == rejection["candidate_id"]]
        else:
            rejection = {"kind": "content_hash", "content_hash": _hash(rejected_content_hash, "rejected_content_hash")}
            matched = [item for item in assignments if item["content_hash"] == rejection["content_hash"]]
        if len(matched) != 1:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_assignment_not_selected", "拒绝资源不是现有选择中的唯一分配")
        seeds.add(matched[0]["slot_id"])

    initial_affected = set(_repair_component(c, base, seeds))
    active_graph = _validate_repair_expansion(contract, base_graph, expanded_graph, initial_affected)
    active = active_graph.to_dict()
    affected = _repair_component(c, active, seeds)
    utility_mode = isinstance(score_receipt, Mapping) and score_receipt.get("version") == UTILITY_RECEIPT_VERSION
    scores, _receipt_rejected_edges, active_score_hash = _score_receipt(score_receipt, active_graph, contract)

    fixed_assignments = [] if selection_payload is None else [item for item in selection_payload["assignments"] if item["slot_id"] not in set(affected)]
    fixed_assignment_hash = _digest({"base_selection_hash": selection.selection_hash if selection else None, "assignments": fixed_assignments})
    scope_body = {
        "base_graph_hash": base_graph.graph_hash,
        "repair_graph_hash": active_graph.graph_hash,
        "constraint_hash": contract.constraint_hash,
        "witness_hash": witness_value["witness_hash"] if witness_value else None,
        "rejection": rejection,
        "affected_slot_ids": list(affected),
        "fixed_assignment_hash": fixed_assignment_hash,
    }
    repair_scope_hash = _digest(scope_body)

    forbidden = rejection or {}
    fixed_by_slot = {item["slot_id"]: item for item in fixed_assignments}
    scoped_edges: list[dict[str, Any]] = []
    for edge in active["edges"]:
        if (edge["slot_id"], edge["candidate_id"]) in _receipt_rejected_edges:
            continue
        fixed = fixed_by_slot.get(edge["slot_id"])
        if fixed is not None and edge["candidate_id"] != fixed["candidate_id"]:
            continue
        if forbidden.get("kind") == "edge" and edge["slot_id"] == forbidden["slot_id"] and edge["candidate_id"] == forbidden["candidate_id"]:
            continue
        if forbidden.get("kind") == "candidate_id" and edge["candidate_id"] == forbidden["candidate_id"]:
            continue
        if forbidden.get("kind") == "content_hash" and edge["content_hash"] == forbidden["content_hash"]:
            continue
        scoped_edges.append(edge)
    scoped_candidates = {edge["candidate_id"]: active["candidates"][edge["candidate_id"]] for edge in scoped_edges}
    scoped_payload = {**active, "graph_mode": "repair-scope", "candidates": scoped_candidates, "edges": scoped_edges,
                      "base_graph_hash": base_graph.graph_hash, "repair_scope_hash": repair_scope_hash,
                      "fixed_assignment_hash": fixed_assignment_hash}
    scoped_raw = _canonical(scoped_payload)
    scoped_graph = FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, contract.constraint_hash, contract.candidate_facts_hash, "repair-scope", len(scoped_edges), hashlib.sha256(scoped_raw.encode()).hexdigest(), scoped_raw)
    scoped_scores = [
        {"slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"], "score": scores[(edge["slot_id"], edge["candidate_id"])][0], "penalty": scores[(edge["slot_id"], edge["candidate_id"])][1]}
        for edge in sorted(scoped_edges, key=lambda item: (item["slot_id"], item["candidate_id"]))
    ]
    scoped_receipt = (
        _local_candidate_base_utility_receipt(scoped_graph, score_receipt)
        if utility_mode
        else _local_v2_score_receipt(
            contract, scoped_graph, scoped_scores, purpose="stage5-repair-score-v2"
        )
    )
    try:
        solved = solve_global_selection(
            contract, scoped_graph,
            score_receipt=scoped_receipt,
        )
    except Stage5InfeasibleError as exc:
        # The deficient core is computed from the filtered repair scope, but a
        # durable witness must bind the authoritative active graph rather than
        # the ephemeral repair-scope graph.
        witness_body = dict(exc.witness)
        witness_body.pop("witness_hash", None)
        witness_body["graph_hash"] = active_graph.graph_hash
        raise Stage5InfeasibleError({**witness_body, "witness_hash": _digest(witness_body)}) from exc

    # Rebind the complete proven assignment to the actual frozen repair graph.
    assignments = solved.assignments
    identity = {
        "contract_version": SELECTION_VERSION, "solver_version": SOLVER_VERSION,
        "stage3_contract_hash": contract.stage3_contract_hash, "candidate_facts_hash": contract.candidate_facts_hash,
        "projection_identity_hash": contract.projection_identity_hash, "profile_hash": c["profile_hash"], "exact_slot_hash": c["exact_slot_hash"],
        "graph_hash": active_graph.graph_hash, "constraint_hash": contract.constraint_hash, "score_identity_hash": active_score_hash,
        "assignments": list(assignments),
    }
    selection_hash = _digest(identity)
    replacement_payload = {**identity, "total_utility": solved.total_utility, "selection_hash": selection_hash}
    replacement_raw = _canonical(replacement_payload)
    replacement = Stage5Selection(SELECTION_VERSION, active_graph.graph_hash, contract.constraint_hash, active_score_hash, solved.total_utility, selection_hash, assignments, replacement_raw)
    receipt_body = {
        "version": REPAIR_VERSION, **scope_body, "repair_scope_hash": repair_scope_hash,
        "fixed_assignment_hash": fixed_assignment_hash,
        "base_selection_hash": selection.selection_hash if selection else None,
        "replacement_selection_hash": replacement.selection_hash,
    }
    repair_hash = _digest(receipt_body)
    receipt_raw = _canonical({**receipt_body, "repair_hash": repair_hash})
    return Stage5RepairResult(replacement, repair_hash, repair_scope_hash, fixed_assignment_hash, affected, receipt_raw)
