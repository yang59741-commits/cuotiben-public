"""Explain committed D2 choices from frozen facts, without AI or writes."""

from dataclasses import replace
import hashlib
import json
import time

from .exam_generation_d2_history import HISTORY_EXACT_ORDER, exact_history_usage
from .exam_generation_d2_publication import D2_TABLE, _validated_committed_payload
from .exam_generation_stage5_d2_solver import (
    D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION, D2_LEGACY_SOLVER_VERSIONS,
    D2SearchIncompleteError, D2SolverError, solve_stage5_d2_selection,
)
from .exam_generation_stage6_d2_repository import recover_d2_candidate_chain
from .exam_generation_stage6_local_factor_contract import project_local_factor_receipts_for_d2
from .exam_generation_stage6_objective import FACTOR_ORDER


EXPLANATION_VERSION = "d2-selection-explanation-v1"
_IDENTITY = ("candidate_id", "candidate_fact_hash", "content_hash")
_REASONS = {
    "past_m_once": "在本卷的一次性优先资格上优于另一候选。",
    "verified_mistake_relevance": "与本卷冻结的错题关联更强。",
    "same_paper_near_duplicate_pairs": "在满足前序条件时，同卷近重复题对更少。",
    "history_exposure_novelty": "按本卷历史规则更少重复出现。",
    "history_exact_success_count": "同等前序条件下，在本次历史窗口内成功使用次数更少。",
    "history_latest_use_order": "同等前序条件和使用次数下，最近一次使用更早。",
    "history_near_duplicate_count": "历史近重复使用次数更少。",
    "history_near_duplicate_latest_use_order": "历史近重复最近使用更早。",
    "stable_tie_break": "前序条件相同，按固定候选顺序确定。",
    "hard_constraints": "保持其他题目不变时，另一候选不能同时满足整卷约束。",
    "single_candidate": "本题位在本次候选范围内没有其他可替换项。",
    "preserved_after_repair": "本题位不在质量修复影响范围内，保留原选择。",
}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _identity(row):
    return tuple(row[field] for field in _IDENTITY)


def _objective_order(selection):
    if selection.solver_version == D2_LEGACY_SOLVER_VERSIONS[0]:
        return list(FACTOR_ORDER)
    if selection.solver_version == D2_V4_SOLVER_VERSION:
        return ["past_m_once", "verified_mistake_relevance", "same_paper_near_duplicate_pairs",
                "history_exposure_novelty", *HISTORY_EXACT_ORDER,
                "history_near_duplicate_count", "history_near_duplicate_latest_use_order"]
    return ["past_m_once", *FACTOR_ORDER, *(HISTORY_EXACT_ORDER if selection.solver_version == D2_SOLVER_VERSION else ())]


def _oriented(selection, policy):
    return tuple(-value if policy["directions"].get(key) == "min" else value
                 for key, value in zip(_objective_order(selection), selection.objective_vector))


def _fixed_alternative(state, selected_edges, alternative, factors, search_guard):
    edges = [alternative if row["slot_id"] == alternative["slot_id"] else row for row in selected_edges]
    payload = state.base.graph.to_dict()
    payload["edges"] = edges
    ids = {row["candidate_id"] for row in edges}
    payload["candidates"] = {key: value for key, value in payload["candidates"].items() if key in ids}
    raw = _canonical(payload)
    graph = replace(state.base.graph, edge_count=len(edges), graph_hash=hashlib.sha256(raw.encode()).hexdigest(), _payload_json=raw)
    identities = {_identity(row) for row in edges}
    receipts = [row for row in factors if _identity(row) in identities]
    original = state.selection.to_dict()
    eligibility = original.get("m_once_eligibility", {})
    eligible = None if eligibility.get("status", "not_supplied") == "not_supplied" else {
        tuple(row) for row in eligibility["identities"] if tuple(row) in identities
    }
    return solve_stage5_d2_selection(
        state.base.constraint, graph, factor_receipts=receipts,
        objective_policy=state.objective_policy, target_mode=original.get("target_mode", "free"),
        m_once_eligible_identities=eligible, search_guard=search_guard,
        solver_version=state.selection.solver_version,
    )


def _alternative_content(fact):
    snapshot = fact["content_snapshot"]
    if isinstance(snapshot, str):
        snapshot = json.loads(snapshot)
    parts = [snapshot["content"]]
    parts.extend("条件：" + value for value in snapshot["conditions"])
    parts.extend("图示：" + value for value in snapshot["figure_references"])
    parts.extend(f"{index}. {value}" for index, value in enumerate(snapshot["options"], 1))
    label = "对比候选"
    if fact.get("source_number"):
        label += " · 原题 " + str(fact["source_number"])
    return {"candidate_id": fact["candidate_id"], "label": label, "content": "\n\n".join(parts)}


def _history_text(assignment, exact):
    level = assignment["factor_levels"]["history_exposure_novelty"]
    if level == "not_applicable":
        return "本卷未使用历史软偏好。", None
    if exact is None:
        text = "按本卷当时规则，此候选曾出现在读取的历史范围内。" if level == "exposed" else "按本卷当时规则，此候选未命中读取的历史范围。"
        return text + "未记录可回放的使用次数和顺序。", None
    usage = exact.get(assignment["content_hash"])
    if usage is None:
        return "同一内容在本任务读取的成功使用范围内未出现。", {"success_count": 0, "latest_order_rank": None}
    return (
        f"同一内容在本任务历史窗口内成功使用 {usage['success_count']} 次，最近一次为 {usage['latest_used_at']}。",
        dict(usage),
    )


def explain_d2_selection(state, *, search_guard=None):
    """Compare each choice with one-slot alternatives, keeping other slots fixed."""
    graph = state.base.graph.to_dict()
    selected = {row["slot_id"]: row for row in state.selection.assignments}
    selected_edges = [next(edge for edge in graph["edges"] if edge["slot_id"] == row["slot_id"] and _identity(edge) == _identity(row))
                      for row in state.selection.assignments]
    facts = {row["candidate_id"]: row for row in state.base.stage4["candidates"]}
    factors = project_local_factor_receipts_for_d2(state.factor_receipts, policy=state.objective_policy)
    original_vector = _oriented(state.selection, state.objective_policy)
    recent = state.base.constraint.to_dict()["recent_usage"]["payload"]
    exact = exact_history_usage(recent)
    selected_candidate_ids = {row["candidate_id"] for row in state.selection.assignments}
    if state.selection.solver_version not in (D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION):
        exact = None
    order = _objective_order(state.selection)
    eligibility = state.selection.to_dict().get("m_once_eligibility", {})
    eligible_identities = {tuple(row) for row in eligibility.get("identities", ())}
    items = []
    for slot, assignment in sorted(selected.items(), key=lambda item: item[1]["position"]):
        alternatives = [edge for edge in graph["edges"] if edge["slot_id"] == slot and _identity(edge) != _identity(assignment)]
        best = None
        blocked = None
        for alternative in alternatives:
            try:
                candidate_selection = _fixed_alternative(state, selected_edges, alternative, factors, search_guard)
            except D2SearchIncompleteError:
                raise
            except D2SolverError as exc:
                if str(exc) != "d2_hard_constraints_infeasible":
                    raise
                blocked = blocked or alternative
                continue
            vector = _oriented(candidate_selection, state.objective_policy)
            if vector > original_vector:
                raise ValueError("explanation_alternative_exceeds_frozen_optimum")
            if best is None or vector > best[0] or (vector == best[0] and _identity(alternative) < _identity(best[1])):
                best = (vector, alternative)
        if best is not None:
            vector, alternative = best
            reason = next((key for key, left, right in zip(order, original_vector, vector) if left != right), "stable_tie_break")
        elif blocked is not None:
            reason, alternative = "hard_constraints", blocked
        else:
            reason, alternative = "single_candidate", None
            if "quality_repair_slots" in graph and slot not in graph["quality_repair_slots"]:
                reason = "preserved_after_repair"
        history_text, history = _history_text(assignment, exact)
        reused = assignment["factor_levels"]["history_exposure_novelty"] == "exposed"
        eligible = _identity(assignment) in eligible_identities
        reason_text = ("本题在本卷的一次性优先资格内。" if eligible else "") + _REASONS[reason]
        similarity = None
        if state.selection.solver_version == D2_V4_SOLVER_VERSION:
            frozen_similarity = graph.get("similarity_receipt")
            if isinstance(frozen_similarity, dict):
                usage = frozen_similarity.get("historical_near_usage", {}).get(assignment["candidate_id"], {})
                similarity = {
                    "same_paper_near_duplicate_pairs": sum(
                        int(row.get("near_duplicate") is True)
                        for row in frozen_similarity.get("pairs", ())
                        if assignment["candidate_id"] in {
                            row.get("left_candidate_id"), row.get("right_candidate_id")
                        }
                        and {row.get("left_candidate_id"), row.get("right_candidate_id")} <= selected_candidate_ids
                    ),
                    "historical_near_usage": dict(usage) if isinstance(usage, dict) else None,
                }
        items.append({
            "position": assignment["position"], "candidate_id": assignment["candidate_id"],
            "reason_code": reason, "reason_text": reason_text, "priority_once_eligible": eligible,
            "history_text": history_text, "history": history,
            "factor_levels": dict(assignment["factor_levels"]),
            "dedupe_tolerance_used": reused,
            "similarity": similarity,
            "comparison_text": "对比保留本卷其他题目不变。" if alternative is not None else "",
            "alternative": _alternative_content(facts[alternative["candidate_id"]]) if alternative is not None else None,
        })
    reused_count = sum(row["dedupe_tolerance_used"] for row in items)
    priority = eligibility.get("status", "not_supplied") != "not_supplied"
    history_scope = (
        f"历史范围为同科目真题最近 {recent['scope']['window_limit']} 条成功题目使用记录，本任务实际读取 {recent['ordered_usage_count']} 条。"
        if exact is not None and "ordered_usage_count" in recent else "历史来自本任务冻结的成功使用记录。"
    )
    return {
        "version": EXPLANATION_VERSION, "status": "available", "selection_hash": state.selection.selection_hash,
        "solver_version": state.selection.solver_version, "objective_order": order,
        "summary_text": ("本卷启用一次性优先。" if priority else "本卷未启用一次性优先。")
            + (f"选用了 {reused_count} 道历史出现过的候选，未放宽同卷精确去重。" if reused_count else "未放宽同卷精确去重。"),
        "scope_text": "仅比较本次候选范围，保持其他题位不变。" + history_scope
            + ("已启用冻结的近重复证据，仅作软排序。" if state.selection.solver_version == D2_V4_SOLVER_VERSION
               else "相似度判断未启用。"),
        "similarity_enabled": state.selection.solver_version == D2_V4_SOLVER_VERSION
            and isinstance(graph.get("similarity_receipt"), dict), "hard_dedup_preserved": True,
        "dedupe_tolerance_used": bool(reused_count), "items": items,
    }


def read_exam_selection_explanation(db, *, exam_id, budget_seconds=60):
    row = db.execute(f"SELECT * FROM {D2_TABLE} WHERE exam_id=? AND state IN ('published_pending_receipt','completed')", (exam_id,)).fetchone()
    if row is None:
        return {"version": EXPLANATION_VERSION, "status": "not_applicable", "items": [],
                "summary_text": "本卷未记录可回放的本地选题对比。", "scope_text": "不将其他组卷链解释为真题本地选题。"}
    started = time.monotonic()
    guard = lambda: time.monotonic() - started < budget_seconds
    publication, _request = _validated_committed_payload(row)
    authority = json.loads(row["authority_json"])
    state = recover_d2_candidate_chain(db, job_id=row["job_id"], checkpoint_hash=authority["checkpoint_hash"],
                                       require_head=False, search_guard=guard)
    if state.selection.selection_hash != publication["selection_hash"]:
        raise ValueError("explanation_publication_selection_mismatch")
    return explain_d2_selection(state, search_guard=guard)
