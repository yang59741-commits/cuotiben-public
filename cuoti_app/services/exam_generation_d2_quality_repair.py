"""Task-local content rejection and bounded D2 component reselection."""

from dataclasses import replace
import hashlib
import json
import time

from .exam_generation_stage5_solver import FrozenCandidateEdgeGraph, _repair_component
from ..transactions import immediate_transaction


class D2ContentRepairRequired(RuntimeError):
    def __init__(self, content_hashes):
        super().__init__("reproducible task-local content rendering failure")
        self.content_hashes = frozenset(content_hashes)


def known_content_rejections(db, job_id):
    from .exam_generation_d2_content_rejections import CONTENT_REJECTIONS, rejected_content_hashes
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (CONTENT_REJECTIONS,)).fetchone():
        return set()
    return rejected_content_hashes(db, job_id)


def attribute_rendering_failure(
    db, *, state, items, job_id, worker_id, lease_attempt,
    render_probe, positions=None, probe_limit=6,
):
    from .exam_generation_d2_publication import D2PublicationError
    from .exam_generation_d2_content_rejections import (
        PROBE_RECEIPTS, append_probe, reject_content, rejected_content_hashes,
    )
    from .exam_generation_v3_publish import _fence

    if render_probe is None:
        raise D2PublicationError("quality_attribution_unknown", "渲染风险尚无本地复现证据，未拒绝题目")
    rejected = rejected_content_hashes(db, job_id)
    if any(item["assignment"]["content_hash"] in rejected for item in items):
        raise D2ContentRepairRequired(rejected)
    for item in items:
        if positions is not None and item["position"] not in positions:
            continue
        used = db.execute(f"SELECT count(*) FROM {PROBE_RECEIPTS} WHERE job_id=?", (job_id,)).fetchone()[0]
        if used >= probe_limit:
            raise D2PublicationError("quality_probe_budget_exhausted", "已达到本任务渲染归因预算，尚未证明可修复")
        assignment = item["assignment"]
        receipt = render_probe(
            selection_hash=state.selection.selection_hash,
            candidate_id=assignment["candidate_id"], content_hash=assignment["content_hash"],
            content_snapshot=item["content_snapshot"],
        )
        with immediate_transaction(db):
            _fence(db, job_id, worker_id, lease_attempt)
            attempt = db.execute(
                f"SELECT coalesce(max(attempt),0)+1 FROM {PROBE_RECEIPTS} "
                "WHERE job_id=? AND selection_hash=? AND candidate_id=? AND content_hash=?",
                (job_id, state.selection.selection_hash, assignment["candidate_id"], assignment["content_hash"]),
            ).fetchone()[0]
            probe_id = append_probe(
                db, job_id=job_id, selection_hash=state.selection.selection_hash,
                candidate_id=assignment["candidate_id"], content_hash=assignment["content_hash"],
                attempt=attempt, receipt=receipt,
            )
            if receipt["classification"] == "CONTENT":
                reject_content(db, probe_id=probe_id)
                rejected.add(assignment["content_hash"])
        if receipt["classification"] == "CONTENT":
            raise D2ContentRepairRequired(rejected)
        if receipt["classification"] == "TEMPLATE_OR_ENVIRONMENT":
            raise D2PublicationError("quality_environment_requires_repair", "模板或编译环境需要修复，未将题目判错", retryable=False)
        if receipt["classification"] == "UNKNOWN" and positions is not None:
            raise D2PublicationError("quality_attribution_unknown", "质量提示未能归因到可重复内容故障，未自动换题")
    if rejected:
        raise D2ContentRepairRequired(rejected)
    raise D2PublicationError("quality_attribution_unknown", "有限本地渲染未定位内容故障，未声明无解")


def _freeze_graph(payload):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return FrozenCandidateEdgeGraph(
        payload["contract_version"], payload["constraint_hash"],
        payload["candidate_facts_hash"], payload["graph_mode"], len(payload["edges"]),
        hashlib.sha256(raw.encode()).hexdigest(), raw,
    )


def repair_pool_graph(graph):
    payload = graph.to_dict()
    pool = payload.get("quality_repair_pool")
    if pool is None:
        return graph
    if any(pool.get(key) != payload.get(key) for key in (
        "contract_version", "constraint_hash", "candidate_facts_hash", "projection_identity_hash",
    )) or "quality_repair_pool" in pool:
        raise ValueError("D2 repair pool binding changed")
    return _freeze_graph(pool)


def without_rejected_content(graph, rejected):
    payload = repair_pool_graph(graph).to_dict()
    if not rejected:
        return _freeze_graph(payload)
    original_edges = payload["edges"]
    base_counts = payload.get("priority_base_counts")
    if base_counts is not None:
        payload["priority_base_counts"] = {
            slot: sum(edge["content_hash"] not in rejected for edge in
                      [edge for edge in original_edges if edge["slot_id"] == slot][:count])
            for slot, count in base_counts.items()
        }
    payload["edges"] = [edge for edge in original_edges if edge["content_hash"] not in rejected]
    used = {edge["candidate_id"] for edge in payload["edges"]}
    payload["candidates"] = {key: value for key, value in payload["candidates"].items() if key in used}
    return _freeze_graph(payload)


def content_repair_graph(state, rejected):
    pool = repair_pool_graph(state.base.graph)
    selected = {row["slot_id"]: row for row in state.selection.assignments}
    seeds = {slot for slot, row in selected.items() if row["content_hash"] in rejected}
    if not seeds:
        raise ValueError("content rejection does not affect the active selection")
    affected = set(_repair_component(state.base.constraint.to_dict(), pool.to_dict(), seeds))
    clean = without_rejected_content(pool, rejected).to_dict()
    payload = dict(clean)
    payload["quality_repair_pool"] = clean
    payload["quality_repair_slots"] = sorted(affected)
    payload["graph_mode"] = "repair-scope"
    identity_fields = ("candidate_id", "candidate_fact_hash", "content_hash")
    payload["edges"] = [edge for edge in clean["edges"] if edge["slot_id"] in affected or all(
        edge[key] == selected[edge["slot_id"]][key] for key in identity_fields
    )]
    used = {edge["candidate_id"] for edge in payload["edges"]}
    payload["candidates"] = {key: value for key, value in clean["candidates"].items() if key in used}
    # Counts belong to the retained pool, not to the fixed-assignment solve view.
    payload.pop("priority_base_counts", None)
    return _freeze_graph(payload), affected


def replace_after_content_rejection(
    db, *, state, job_id, worker_id, lease_attempt, subject_id,
    mistake_relations, rejected, limit, search_guard,
):
    from .exam_generation_d2_runtime import local_factors
    from .exam_generation_d2_publication import D2PublicationError
    from .exam_generation_priority_runtime import (
        RESELECTIONS, load_priority_claims, persist_reselection, select_with_priority,
    )
    from .exam_generation_stage5_d2_solver import solve_stage5_d2_selection
    from .exam_generation_stage6_local_factor_contract import project_local_factor_receipts_for_d2

    if db.execute(f"SELECT count(*) FROM {RESELECTIONS} WHERE job_id=?", (job_id,)).fetchone()[0] >= limit:
        raise D2PublicationError("quality_reselection_budget_exhausted", "质量修复已达到本任务总重选预算")
    started = time.monotonic()
    graph, affected = content_repair_graph(state, rejected)
    base = replace(state.base, graph=graph)
    factors = local_factors(base, state.objective_policy, mistake_relations=mistake_relations)
    if load_priority_claims(db, job_id=job_id, state=state) is not None:
        selection, keys = select_with_priority(
            db, base=base, policy=state.objective_policy, factors=factors,
            subject_id=subject_id, mistake_relations=mistake_relations, search_guard=search_guard,
            solver_version=state.selection.solver_version,
        )
    else:
        selection = solve_stage5_d2_selection(
            base.constraint, graph, objective_policy=state.objective_policy,
            factor_receipts=project_local_factor_receipts_for_d2(factors, policy=state.objective_policy),
            target_mode="past", m_once_eligible_identities=None, search_guard=search_guard,
            solver_version=state.selection.solver_version,
        )
        keys = None
    old = {row["slot_id"]: dict(row) for row in state.selection.assignments}
    if any(dict(row) != old[row["slot_id"]] for row in selection.assignments if row["slot_id"] not in affected):
        raise D2PublicationError("quality_repair_scope_drift", "质量修复改变了范围外题位，未发布替换结果")
    return persist_reselection(
        db, state=state, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
        base=base, factors=factors, selection=selection, keys=keys, limit=limit,
        reason="reproducible_content_rendering_rejected", started=started,
        search_guard=search_guard,
    )
