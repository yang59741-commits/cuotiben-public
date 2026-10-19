"""Candidate normalization, graph construction and priority supplementation."""

from __future__ import annotations

import hashlib

import math

from builtins import (
    any as _builtin_any,
)

from typing import (
    Any,
    Mapping,
    Sequence,
)

from .contracts import (
    CANDIDATE_GRAPH_VERSION,
    EFFECTIVE_DIFFICULTY_SET_VERSION,
    EFFECTIVE_DIFFICULTY_VERSION,
    FrozenCandidateEdgeGraph,
    STAGE4_CONTRACT_VERSION,
    Stage5ContractError,
    Stage5GlobalConstraintContract,
    _DIFFICULTIES,
    _canonical,
    _digest,
    _hash,
    _mapping,
    _recent_usage_effect,
    _source,
    _text,
    _verified_contract_payload,
    _verified_graph_payload,
)



def _effective_difficulties(
    value: Mapping[str, Any] | None,
    *,
    candidate_facts_hash: str,
) -> tuple[dict[str, str], str]:
    """Verify a deterministic set of complete Stage-4 resolution results.

    Stage 5 must not accept an outcomes-only summary: that would discard the
    Stage-4 model, prompt, rules and input-fact bindings.  The set wrapper is
    deliberately small, while each member is the exact immutable v42 result
    payload plus its real result hash.
    """

    candidate_facts_hash = _hash(candidate_facts_hash, "candidate_facts_hash")
    if value is None:
        empty = {
            "contract_version": EFFECTIVE_DIFFICULTY_SET_VERSION,
            "candidate_facts_hash": candidate_facts_hash,
            "results": [],
        }
        return {}, _digest(empty)
    value = _mapping(value, "effective_difficulty_receipt")
    if set(value) != {
        "contract_version", "candidate_facts_hash", "results", "receipt_hash"
    }:
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_receipt_shape_invalid", "有效难度回执字段无效")
    if value.get("contract_version") != EFFECTIVE_DIFFICULTY_SET_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_version_unsupported", "有效难度回执版本不受支持")
    if value.get("candidate_facts_hash") != candidate_facts_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_candidate_facts_mismatch", "有效难度回执未绑定当前候选事实")
    results = value.get("results")
    if not isinstance(results, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_results_invalid", "有效难度结果集合无效")
    receipt_body = {
        "contract_version": value["contract_version"],
        "candidate_facts_hash": candidate_facts_hash,
        "results": results,
    }
    receipt_hash = _hash(value.get("receipt_hash"), "effective_difficulty_receipt_hash")
    if _digest(receipt_body) != receipt_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_hash_mismatch", "有效难度回执哈希不匹配")
    result: dict[str, str] = {}
    seen_candidates: set[str] = set()
    seen_results: set[str] = set()
    ordered_result_hashes: list[str] = []
    payload_fields = {
        "contract_version", "candidate_facts_hash", "component_hash",
        "input_facts_hash", "classification_binding", "verification_binding",
        "classification_response_hash", "verification_response_hash", "outcomes",
    }
    outcome_fields = {
        "candidate_id", "status", "effective_difficulty", "reason",
        "classification_confidence", "verification_confidence", "evidence_hash",
    }
    for member in results:
        member = _mapping(member, "effective_difficulty_result")
        if set(member) != {"result_hash", "payload"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_shape_invalid", "有效难度结果引用字段无效")
        result_hash = _hash(member.get("result_hash"), "effective_difficulty_result_hash")
        if result_hash in seen_results:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_duplicate", "有效难度结果重复")
        seen_results.add(result_hash)
        ordered_result_hashes.append(result_hash)
        payload = _mapping(member.get("payload"), "effective_difficulty_result_payload")
        if set(payload) != payload_fields:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_payload_shape_invalid", "有效难度结果载荷字段无效")
        if (
            payload.get("contract_version") != EFFECTIVE_DIFFICULTY_VERSION
            or payload.get("candidate_facts_hash") != candidate_facts_hash
            or _digest(payload) != result_hash
        ):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_result_binding_mismatch", "有效难度结果未绑定当前候选事实或结果哈希")
        _hash(payload.get("component_hash"), "effective_difficulty_component_hash")
        _hash(payload.get("input_facts_hash"), "effective_difficulty_input_facts_hash")
        bindings: list[Mapping[str, Any]] = []
        for round_name in ("classification", "verification"):
            binding = _mapping(payload.get(f"{round_name}_binding"), f"{round_name}_binding")
            if set(binding) != {"model", "prompt_version", "rules_hash"}:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_binding_shape_invalid", "有效难度轮次绑定字段无效")
            _text(binding.get("model"), f"{round_name}_model", 256)
            _text(binding.get("prompt_version"), f"{round_name}_prompt_version", 128)
            _hash(binding.get("rules_hash"), f"{round_name}_rules_hash")
            bindings.append(binding)
        if (
            bindings[0]["rules_hash"] != bindings[1]["rules_hash"]
            or bindings[0]["prompt_version"] == bindings[1]["prompt_version"]
        ):
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_round_binding_invalid", "有效难度两轮绑定无效")
        for field in ("classification_response_hash", "verification_response_hash"):
            response_hash = payload.get(field)
            if response_hash is not None:
                _hash(response_hash, field)
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcomes_invalid", "有效难度回执无效")
        for raw in outcomes:
            raw = _mapping(raw, "effective_difficulty_outcome")
            if set(raw) != outcome_fields:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcome_shape_invalid", "有效难度条目字段无效")
            candidate = _text(raw.get("candidate_id"), "candidate_id")
            if candidate in seen_candidates:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_duplicate", "有效难度候选重复")
            seen_candidates.add(candidate)
            label = raw.get("effective_difficulty")
            status = raw.get("status")
            confidences = (raw.get("classification_confidence"), raw.get("verification_confidence"))
            if (
                status not in {"resolved", "excluded", "conflict"}
                or (status == "resolved" and label not in _DIFFICULTIES)
                or (status != "resolved" and label is not None)
                or _builtin_any(confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1) for confidence in confidences)
            ):
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcome_invalid", "有效难度条目无效")
            _text(raw.get("reason"), "effective_difficulty_reason", 128)
            _hash(raw.get("evidence_hash"), "effective_difficulty_evidence_hash")
            if status == "resolved":
                result[candidate] = label
    if ordered_result_hashes != sorted(ordered_result_hashes):
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_results_not_canonical", "有效难度结果集合顺序不规范")
    return result, receipt_hash



def _constraint_aware_order(items: Sequence[dict[str, Any]], contract_payload: Mapping[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return a stable, score-free order whose prefixes track frozen hard demand."""

    source_targets = contract_payload["source_quotas"]
    chapter_targets = contract_payload["ordinary_chapter_quotas"]
    coverage_items = contract_payload["coverage_policy"]["payload"]["minima"]
    question_count = contract_payload["question_count"]
    remaining = list(items)
    ordered: list[dict[str, Any]] = []
    source_counts = {key: 0 for key in source_targets}
    chapter_counts = {key: 0 for key in chapter_targets}
    coverage_counts = {item["id"]: 0 for item in coverage_items}
    exam_counts: dict[str, int] = {}
    content_counts: dict[str, int] = {}
    denominator = math.lcm(*(max(1, target) for target in (
        *source_targets.values(), *chapter_targets.values(),
        *(entry['min_count'] for entry in coverage_items),
    )))

    # A common positive scale preserves exact rational ordering without
    # allocating Fraction objects for every candidate at every prefix step.
    def pressure(target: int, count: int, step: int) -> int:
        return (target * step - count * question_count) * (denominator // max(1, target))

    while remaining and (limit is None or len(ordered) < limit):
        step = len(ordered) + 1

        def priority(item: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
            source = item["source_kind"]
            source_pressure = pressure(source_targets[source], source_counts[source], step)
            if source == "ordinary":
                chapter = item["chapter"]
                subgroup_pressure = pressure(chapter_targets[chapter], chapter_counts[chapter], step)
            else:
                subgroup_pressure = source_pressure
            coverage_pressure = sum(
                (pressure(entry["min_count"], coverage_counts[entry["id"]], step)
                 for entry in coverage_items
                 if entry["min_count"] and item[entry["field"]] == entry["value"]),
                0,
            )
            exam_count = exam_counts.get(item["source_exam_id"], 0) if item["source_exam_id"] is not None else 0
            return (
                source_pressure + subgroup_pressure + coverage_pressure,
                -content_counts.get(item["content_hash"], 0), -exam_count,
                item["candidate_id"], item["candidate_fact_hash"],
            )

        # Maximize demand pressure, but keep identifiers ascending on an exact tie.
        def order_key(item):
            rank = priority(item)
            return (-rank[0], -rank[1], -rank[2], rank[3], rank[4])

        chosen = min(remaining, key=order_key)
        remaining.remove(chosen)
        ordered.append(chosen)
        source_counts[chosen["source_kind"]] += 1
        if chosen["source_kind"] == "ordinary":
            chapter_counts[chosen["chapter"]] += 1
        if chosen["source_exam_id"] is not None:
            exam_counts[chosen["source_exam_id"]] = exam_counts.get(chosen["source_exam_id"], 0) + 1
        content_counts[chosen["content_hash"]] = content_counts.get(chosen["content_hash"], 0) + 1
        for entry in coverage_items:
            if chosen[entry["field"]] == entry["value"]:
                coverage_counts[entry["id"]] += 1
    return ordered



def _normalized_candidate_pool(
    contract_payload: Mapping[str, Any],
    stage4: Mapping[str, Any],
    effective: Mapping[str, str],
    *,
    embedded_resolved_only: bool = False,
) -> list[dict[str, Any]]:
    """Apply the shared Stage-5 hard candidate boundaries before windowing."""

    candidates_raw = stage4.get("candidates")
    if not isinstance(candidates_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_rows_invalid", "冻结候选集合无效")
    excluded_ids, excluded_hashes = _recent_usage_effect(
        _mapping(contract_payload["recent_usage"], "recent_usage"), candidates_raw
    )
    known_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in candidates_raw:
        raw = _mapping(raw, "candidate")
        candidate_id = _text(raw.get("candidate_id"), "candidate_id")
        if candidate_id in known_ids:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_id_not_unique", "候选 ID 不唯一")
        known_ids.add(candidate_id)
        content_hash = _hash(raw.get("content_hash"), "content_hash")
        if candidate_id in excluded_ids or content_hash in excluded_hashes:
            continue
        source = _source(raw.get("source_kind"))
        if _text(raw.get("subject"), "candidate_subject") != contract_payload["subject"]:
            continue
        if source == "ordinary":
            chapter = _text(raw.get("chapter"), "chapter")
            if _builtin_any(raw.get(field) not in (None, "") for field in ("source_exam_id", "section_id", "source_institution")):
                raise Stage5ContractError("exam_stage5_contract_invalid", "ordinary_provenance_conflict", "普通题不得携带真题来源字段")
            source_exam_id = section_id = source_institution = None
        else:
            if raw.get("chapter") not in (None, ""):
                raise Stage5ContractError("exam_stage5_contract_invalid", "past_exam_chapter_forbidden", "外校真题不得伪造教材章节")
            chapter = None
            source_exam_id = _text(raw.get("source_exam_id"), "source_exam_id")
            section_id = _text(raw.get("section_id"), "candidate_section_id")
            source_institution = _text(raw.get("source_institution"), "source_institution")
            if source_institution.casefold() == contract_payload["target_institution"].casefold():
                raise Stage5ContractError("exam_stage5_contract_invalid", "target_institution_candidate_forbidden", "候选来自目标院校")
        fact = _mapping(raw.get("difficulty_fact"), "difficulty_fact")
        embedded_resolved = fact.get("resolved") is True and fact.get("label") in _DIFFICULTIES
        if embedded_resolved_only and not embedded_resolved:
            continue
        difficulty = fact.get("label") if embedded_resolved else effective.get(candidate_id)
        normalized.append({
            "candidate_id": candidate_id, "source_kind": source, "chapter": chapter,
            "source_exam_id": source_exam_id, "section_id": section_id,
            "source_institution": source_institution,
            "question_type_family": _text(raw.get("question_type_family"), "question_type_family"),
            "difficulty": difficulty, "content_hash": content_hash,
            "candidate_fact_hash": _digest(raw),
        })
    normalized = [
        item for item in normalized
        if contract_payload["source_quotas"][item["source_kind"]] > 0
        and (
            item["source_kind"] != "ordinary"
            or contract_payload["ordinary_chapter_quotas"].get(item["chapter"], 0) > 0
        )
    ]
    normalized.sort(key=lambda item: (item["source_kind"], item["candidate_id"], item["candidate_fact_hash"]))
    return normalized



def _inject_missing_expanded_chapters(
    contract_payload: Mapping[str, Any],
    normalized: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
    selected_by_slot: dict[str, list[dict[str, Any]]],
    expanded: set[str],
) -> None:
    """Use spare expanded edges for a required chapter omitted by the window.

    This is intentionally conservative: the initial 4N graph is untouched,
    and expansion is strictly additive.  A slot that already has six edges
    is left alone rather than replacing an initial edge; the repository
    contract requires every initial edge to remain present in the expanded
    graph.
    """
    if not expanded:
        return
    required = [
        (chapter, int(count))
        for chapter, count in contract_payload["ordinary_chapter_quotas"].items()
        if count
    ]
    if not required:
        return
    def all_edges() -> list[dict[str, Any]]:
        return [edge for edges in selected_by_slot.values() for edge in edges]

    for chapter, target in required:
        while len({
            edge["content_hash"]
            for edge in all_edges()
            if edge["source_kind"] == "ordinary" and edge["chapter"] == chapter
        }) < target:
            chapter_rows = [
                item for item in normalized
                if item["source_kind"] == "ordinary" and item["chapter"] == chapter
            ]
            if not chapter_rows:
                break
            placed = False
            for slot in sorted(
                (slot for slot in slots if slot["slot_id"] in expanded),
                key=lambda item: item["position"],
            ):
                key = (slot["question_type_family"], slot["target_difficulty"])
                compatible = [
                    item for item in chapter_rows
                    if (item["question_type_family"], item["difficulty"]) == key
                ]
                if not compatible:
                    continue
                edges = selected_by_slot[slot["slot_id"]]
                existing_ids = {edge["candidate_id"] for edge in all_edges()}
                existing_hashes = {edge["content_hash"] for edge in all_edges()}
                item = next(
                    (
                        row for row in compatible
                        if row["candidate_id"] not in existing_ids
                        and row["content_hash"] not in existing_hashes
                    ),
                    None,
                )
                if item is None:
                    continue
                if len(edges) >= 6:
                    # Do not replace an existing edge.  Even replacing a
                    # newly-added position would remove a distinct
                    # (slot,candidate) key and violate the persisted additive
                    # graph contract.
                    break
                edge = {
                    "slot_id": slot["slot_id"], "position": slot["position"],
                    "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                    "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                    "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                    "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                    "bucket_rank": len(edges) + 1,
                }
                edges.append(edge)
                placed = True
                break
            if not placed:
                break



def build_candidate_edge_graph(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
    effective_difficulty_receipt: Mapping[str, Any] | None = None,
    expanded_slot_ids: Sequence[str] = (),
) -> FrozenCandidateEdgeGraph:
    """Build the bounded compatible graph: 4N initially, 6N after targeted expansion."""

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if stage4.get("contract_version") != STAGE4_CONTRACT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "stage4_contract_version_unsupported", "阶段 4 候选事实版本不受支持")
    if _digest(stage4) != contract.candidate_facts_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    effective, effective_hash = _effective_difficulties(
        effective_difficulty_receipt,
        candidate_facts_hash=contract.candidate_facts_hash,
    )
    normalized = _normalized_candidate_pool(c, stage4, effective)
    if isinstance(expanded_slot_ids, (str, bytes)) or not isinstance(expanded_slot_ids, Sequence):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_ids_invalid", "扩展槽位列表无效")
    expanded_list = [_text(item, "expanded_slot_id") for item in expanded_slot_ids]
    if len(set(expanded_list)) != len(expanded_list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_duplicate", "扩展槽位重复")
    expanded = set(expanded_list)
    valid_slots = {slot["slot_id"] for slot in c["slots"]}
    if not expanded.issubset(valid_slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_unknown", "扩展槽位不在冻结合同中")
    edges: list[dict[str, Any]] = []
    selected_by_slot: dict[str, list[dict[str, Any]]] = {slot["slot_id"]: [] for slot in c["slots"]}
    ordered_pools: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}
    for slot in c["slots"]:
        key = (slot["question_type_family"], slot["target_difficulty"])
        if key not in ordered_pools:
            compatible = [item for item in normalized if (item["question_type_family"], item["difficulty"]) == key]
            total = len(compatible)
            indices = [((other["position"] - 1) * 6 + offset) % total
                       for other in c["slots"] if (other["question_type_family"], other["target_difficulty"]) == key
                       for offset in range(min(6 if other["slot_id"] in expanded else 4, total))] if total else []
            prefix = max(indices) + 1 if indices else 0
            ordered_pools[key] = (total, _constraint_aware_order(compatible, c, limit=prefix))
        total, compatible = ordered_pools[key]
        limit = 6 if slot["slot_id"] in expanded else 4
        count = min(limit, total)
        start = ((slot["position"] - 1) * 6) % total if total else 0
        selected = [compatible[(start + offset) % total] for offset in range(count)]
        for bucket_rank, item in enumerate(selected, 1):
            selected_by_slot[slot["slot_id"]].append({
                "slot_id": slot["slot_id"], "position": slot["position"],
                "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                "bucket_rank": bucket_rank,
            })
    _inject_missing_expanded_chapters(c, normalized, c["slots"], selected_by_slot, expanded)
    edges = [edge for slot in c["slots"] for edge in selected_by_slot[slot["slot_id"]]]
    edges.sort(key=lambda item: (item["position"], item["bucket_rank"], item["candidate_id"], item["candidate_fact_hash"]))
    if len(edges) > (6 if expanded else 4) * contract.question_count:
        raise AssertionError("candidate edge budget violated")
    used_candidate_ids = {edge["candidate_id"] for edge in edges}
    candidate_index = {item["candidate_id"]: item for item in normalized if item["candidate_id"] in used_candidate_ids}
    candidate_limit = (6 if expanded else 4) * contract.question_count
    if len(candidate_index) > candidate_limit:
        raise AssertionError("candidate union budget violated")
    payload = {
        "contract_version": CANDIDATE_GRAPH_VERSION, "constraint_hash": contract.constraint_hash,
        "candidate_facts_hash": contract.candidate_facts_hash, "projection_identity_hash": contract.projection_identity_hash,
        "effective_difficulty_hash": effective_hash, "graph_mode": "expanded" if expanded else "initial",
        "expanded_slot_ids": sorted(expanded), "candidates": candidate_index, "edges": edges,
    }
    raw = _canonical(payload)
    return FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, contract.constraint_hash, contract.candidate_facts_hash, payload["graph_mode"], len(edges), hashlib.sha256(raw.encode()).hexdigest(), raw)



def supplement_priority_candidate_graph(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
    base_graph: FrozenCandidateEdgeGraph,
    preferred_candidate_ids: Sequence[str],
) -> FrozenCandidateEdgeGraph:
    """Add resolved exact-priority candidates omitted only by the 4N ID window."""

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if (
        stage4.get("contract_version") != STAGE4_CONTRACT_VERSION
        or _digest(stage4) != contract.candidate_facts_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    graph_payload = _verified_graph_payload(base_graph, contract=contract)
    if isinstance(preferred_candidate_ids, (str, bytes)) or not isinstance(preferred_candidate_ids, Sequence):
        raise Stage5ContractError("exam_stage5_contract_invalid", "priority_candidate_ids_invalid", "一次性优先候选列表无效")
    preferred = [_text(value, "priority_candidate_id") for value in preferred_candidate_ids]
    if len(preferred) != len(set(preferred)):
        raise Stage5ContractError("exam_stage5_contract_invalid", "priority_candidate_id_duplicate", "一次性优先候选重复")
    preferred_set = set(preferred)

    slots = {slot["slot_id"]: slot for slot in c["slots"]}
    current_by_slot: dict[str, list[dict[str, Any]]] = {slot_id: [] for slot_id in slots}
    for raw_edge in graph_payload["edges"]:
        edge = dict(_mapping(raw_edge, "candidate_edge"))
        slot_id = edge.get("slot_id")
        if slot_id not in slots:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_edge_slot_unknown", "候选边槽位无效")
        current_by_slot[slot_id].append(edge)

    stored_counts = graph_payload.get("priority_base_counts")
    if stored_counts is None:
        base_counts = {slot_id: len(edges) for slot_id, edges in current_by_slot.items()}
    else:
        stored_counts = _mapping(stored_counts, "priority_base_counts")
        if set(stored_counts) != set(slots):
            raise Stage5ContractError("exam_stage5_contract_invalid", "priority_base_counts_shape_invalid", "优先候选基线计数无效")
        base_counts = {}
        for slot_id, value in stored_counts.items():
            if type(value) is not int or not 0 <= value <= len(current_by_slot[slot_id]) or value > 6:
                raise Stage5ContractError("exam_stage5_contract_invalid", "priority_base_count_invalid", "优先候选基线计数无效")
            base_counts[slot_id] = value

    # A repeated call starts from the immutable pre-priority prefix, so a
    # consumed preferred candidate from an earlier round cannot linger.
    result_by_slot = {
        slot_id: edges[:base_counts[slot_id]]
        for slot_id, edges in current_by_slot.items()
    }
    normalized = _normalized_candidate_pool(
        c, stage4, {}, embedded_resolved_only=True
    )
    preferred_rows = {
        item["candidate_id"]: item
        for item in normalized
        if item["candidate_id"] in preferred_set
    }
    supplemented_slots: set[str] = set()
    for slot_id, slot in slots.items():
        edges = result_by_slot[slot_id]
        capacity = min(2, max(0, 6 - len(edges)))
        if not capacity:
            continue
        existing_ids = {edge["candidate_id"] for edge in edges}
        existing_hashes = {edge["content_hash"] for edge in edges}
        compatible = [
            item for item in preferred_rows.values()
            if item["candidate_id"] not in existing_ids
            and item["content_hash"] not in existing_hashes
            and item["question_type_family"] == slot["question_type_family"]
            and item["difficulty"] == slot["target_difficulty"]
        ]
        compatible.sort(key=lambda item: (item["candidate_id"], item["candidate_fact_hash"]))
        for item in compatible[:capacity]:
            edges.append({
                "slot_id": slot_id, "position": slot["position"],
                "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                "bucket_rank": len(edges) + 1,
            })
            existing_ids.add(item["candidate_id"])
            existing_hashes.add(item["content_hash"])
            supplemented_slots.add(slot_id)

    edges = [edge for slot_id in slots for edge in result_by_slot[slot_id]]
    edges.sort(key=lambda item: (item["position"], item["bucket_rank"], item["candidate_id"], item["candidate_fact_hash"]))
    if len(edges) > 6 * contract.question_count or _builtin_any(len(value) > 6 for value in result_by_slot.values()):
        raise AssertionError("priority candidate edge budget violated")
    original_candidates = _mapping(graph_payload.get("candidates"), "candidate_index")
    used_ids = {edge["candidate_id"] for edge in edges}
    candidate_index = {
        candidate_id: dict(value)
        for candidate_id, value in original_candidates.items()
        if candidate_id in used_ids
    }
    candidate_index.update({
        candidate_id: row for candidate_id, row in preferred_rows.items()
        if candidate_id in used_ids
    })
    expanded_slots = set(graph_payload.get("expanded_slot_ids") or ()) | supplemented_slots
    graph_mode = "expanded" if supplemented_slots or base_graph.graph_mode == "expanded" else base_graph.graph_mode
    payload = {
        **graph_payload,
        "graph_mode": graph_mode,
        "expanded_slot_ids": sorted(expanded_slots),
        "priority_base_counts": {slot_id: base_counts[slot_id] for slot_id in sorted(slots)},
        "candidates": candidate_index,
        "edges": edges,
    }
    raw = _canonical(payload)
    return FrozenCandidateEdgeGraph(
        CANDIDATE_GRAPH_VERSION, contract.constraint_hash,
        contract.candidate_facts_hash, graph_mode, len(edges),
        hashlib.sha256(raw.encode()).hexdigest(), raw,
    )



def build_difficulty_defect_edges(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Expose only unresolved, non-difficulty-compatible edges for Stage 4.

    These edges are not selectable and carry no score.  Their sole purpose is
    to let the Stage-4 two-round resolver locate the minimum component that can
    reach a real Stage-5 deficit without weakening any source, subject, chapter,
    target-institution or question-type boundary.
    """

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if (
        stage4.get("contract_version") != STAGE4_CONTRACT_VERSION
        or _digest(stage4) != contract.candidate_facts_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    candidates = stage4.get("candidates")
    if not isinstance(candidates, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_rows_invalid", "冻结候选集合无效")
    excluded_ids, excluded_hashes = _recent_usage_effect(
        _mapping(c["recent_usage"], "recent_usage"), candidates
    )
    seen: set[str] = set()
    unresolved: list[tuple[str, str]] = []
    for raw in candidates:
        raw = _mapping(raw, "candidate")
        candidate_id = _text(raw.get("candidate_id"), "candidate_id")
        if candidate_id in seen:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_id_not_unique", "候选 ID 不唯一")
        seen.add(candidate_id)
        content_hash = _hash(raw.get("content_hash"), "content_hash")
        if candidate_id in excluded_ids or content_hash in excluded_hashes:
            continue
        source = _source(raw.get("source_kind"))
        if _text(raw.get("subject"), "candidate_subject") != c["subject"] or c["source_quotas"][source] <= 0:
            continue
        if source == "ordinary":
            chapter = _text(raw.get("chapter"), "chapter")
            if c["ordinary_chapter_quotas"].get(chapter, 0) <= 0:
                continue
        else:
            institution = _text(raw.get("source_institution"), "source_institution")
            _text(raw.get("source_exam_id"), "source_exam_id")
            _text(raw.get("section_id"), "candidate_section_id")
            if institution.casefold() == c["target_institution"].casefold():
                raise Stage5ContractError("exam_stage5_contract_invalid", "target_institution_candidate_forbidden", "候选来自目标院校")
        fact = _mapping(raw.get("difficulty_fact"), "difficulty_fact")
        if fact.get("resolved") is True:
            continue
        unresolved.append((candidate_id, _text(raw.get("question_type_family"), "question_type_family")))
    edges = [
        {
            "slot_id": slot["slot_id"],
            "candidate_id": candidate_id,
            "compatible_without_difficulty": True,
        }
        for slot in c["slots"]
        for candidate_id, question_type in unresolved
        if question_type == slot["question_type_family"]
    ]
    edges.sort(key=lambda item: (item["slot_id"], item["candidate_id"]))
    return tuple(edges)



def _initial_graph_from_expanded(graph: FrozenCandidateEdgeGraph) -> FrozenCandidateEdgeGraph:
    payload = graph.to_dict()
    base_edges = [edge for edge in payload["edges"] if edge["bucket_rank"] <= 4]
    candidate_ids = {edge["candidate_id"] for edge in base_edges}
    base_payload = {**payload, "graph_mode": "initial", "expanded_slot_ids": [], "candidates": {key: value for key, value in payload["candidates"].items() if key in candidate_ids}, "edges": base_edges}
    raw = _canonical(base_payload)
    return FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, graph.constraint_hash, graph.candidate_facts_hash, "initial", len(base_edges), hashlib.sha256(raw.encode()).hexdigest(), raw)
