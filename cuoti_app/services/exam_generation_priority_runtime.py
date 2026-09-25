"""Bind priority claims to immutable D2 selections and bounded replacements."""

from dataclasses import asdict, replace
import json
import time
from typing import Mapping

from ..transactions import immediate_transaction
from .exam_generation_d2_publication import D2_TABLE, D2PublicationError
from .exam_generation_priority_once import PriorityOnceKey, read_unconsumed_priority_once, _read_consumption
from .exam_generation_stage5_d2_solver import solve_stage5_d2_selection
from .exam_generation_stage6_local_factor_contract import project_local_factor_receipts_for_d2
from .exam_generation_stage6_d2_repository import (
    D2_HEADS, _validate_selection, persist_d2_candidate_chain, recover_d2_candidate_chain,
)
from .exam_generation_v3_publish import _fence, _timestamp
from .exam_generation_mistake_relations import exact_relation_factor, relation_content_fingerprint, relation_index
from .exam_generation_soft_inputs import SOFT_INPUTS

PRIORITY_SELECTIONS = "exam_generation_priority_once_selections"
RESELECTIONS = "exam_generation_d2_reselections"


def prepare_priority_base(db, *, base, subject_id, mistake_relations, rejected=()):
    from .exam_generation_stage5_solver import supplement_priority_candidate_graph
    from .exam_generation_d2_quality_repair import without_rejected_content
    base = replace(base, graph=without_rejected_content(base.graph, rejected))
    index = relation_index(mistake_relations)
    preferred = []
    for candidate in base.stage4["candidates"]:
        if candidate["content_hash"] in rejected:
            continue
        if exact_relation_factor(candidate, index)["level"] != "strong":
            continue
        snapshot = candidate["content_snapshot"]
        snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        fingerprint = relation_content_fingerprint(snapshot["content"])
        keys = (PriorityOnceKey(subject_id, "past", candidate["content_hash"], public_id, fingerprint)
                for public_id in index[fingerprint])
        if any(_read_consumption(db, key) is None for key in keys):
            preferred.append(candidate["candidate_id"])
    graph = supplement_priority_candidate_graph(
        base.constraint, stage4=base.stage4, base_graph=base.graph,
        preferred_candidate_ids=preferred,
    )
    if isinstance(graph.to_dict().get("similarity_receipt"), Mapping):
        # Priority retries can restore a different bounded prefix.  Preserve
        # the task's frozen rule/history while extending only for new edges.
        from .exam_generation_d2_runtime import freeze_d2_similarity_graph
        base = replace(base, graph=graph)
        return freeze_d2_similarity_graph(db, base=base, subject_id=subject_id)
    return replace(base, graph=graph)


def select_with_priority(db, *, base, policy, factors, subject_id, mistake_relations, search_guard, solver_version=None):
    eligibility = read_unconsumed_priority_once(
        db, subject_id=subject_id, mode="past", stage4=base.stage4,
        graph=base.graph, mistake_relations=mistake_relations,
    )
    identities = {(row["candidate_id"], row["candidate_fact_hash"], row["content_hash"])
                  for row in base.graph.to_dict()["edges"] if row["candidate_id"] in eligibility}
    if not identities:
        raise D2PublicationError("priority_once_unavailable", "本次要求一次性优先，但没有尚未消费且符合题位的错题关联题")
    selection = solve_stage5_d2_selection(
        base.constraint, base.graph, objective_policy=policy,
        factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy),
        target_mode="past", m_once_eligible_identities=identities, search_guard=search_guard,
        solver_version=solver_version,
    )
    if selection.objective_vector[0] < 1:
        raise D2PublicationError("priority_once_globally_incompatible", "一次性优先候选无法同时满足本卷全部硬约束")
    keys = tuple(key for row in selection.assignments for key in eligibility.get(row["candidate_id"], ()))
    return selection, keys


def persist_priority_claims(db, *, job_id, selection_hash, keys):
    if not db.in_transaction:
        raise ValueError("priority claims require the selection transaction")
    raw = json.dumps([asdict(key) for key in keys], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    db.execute(f"INSERT INTO {PRIORITY_SELECTIONS} VALUES(?,?,?) ON CONFLICT(job_id,selection_hash) DO NOTHING",
               (job_id, selection_hash, raw))
    stored = db.execute(f"SELECT claims_json FROM {PRIORITY_SELECTIONS} WHERE job_id=? AND selection_hash=?",
                        (job_id, selection_hash)).fetchone()
    if json.loads(stored[0]) != json.loads(raw):
        raise D2PublicationError("priority_claims_changed", "已冻结的一次性优先资格发生变化")


def load_priority_claims(db, *, job_id, state):
    eligibility = state.selection.to_dict().get("m_once_eligibility", {})
    if eligibility.get("status", "not_supplied") == "not_supplied":
        return None
    row = db.execute(f"SELECT claims_json FROM {PRIORITY_SELECTIONS} WHERE job_id=? AND selection_hash=?",
                     (job_id, state.selection.selection_hash)).fetchone()
    try:
        raw = json.loads(row[0]) if row is not None else None
        if not isinstance(raw, list) or not raw:
            raise ValueError("missing claims")
        return tuple(PriorityOnceKey(**item) for item in raw)
    except (TypeError, ValueError) as exc:
        raise D2PublicationError("priority_claims_unavailable", "已启用一次性优先，但冻结的消费资格无法恢复") from exc


def validate_priority_claim_sources(db, *, job_id, state, items, keys):
    row = db.execute(
        f"SELECT mode,allowed_sources_json,payload_json FROM {SOFT_INPUTS} "
        "WHERE job_id=? AND input_key='mistake_relations'", (job_id,),
    ).fetchone()
    try:
        index = relation_index({"mode": row[0], "allowed_sources": json.loads(row[1]),
                                "payload": json.loads(row[2])}) if row is not None else None
        eligible = {tuple(identity) for identity in state.selection.to_dict()["m_once_eligibility"]["identities"]}
        selected = {}
        for item in items:
            assignment, snapshot = item["assignment"], item["content_snapshot"]
            identity = tuple(assignment[field] for field in ("candidate_id", "candidate_fact_hash", "content_hash"))
            if identity not in eligible or any(snapshot.get(field) for field in ("conditions", "options", "figure_references")):
                continue
            selected[assignment["content_hash"]] = relation_content_fingerprint(snapshot["content"])
        if index is None or not keys or any(
            selected.get(key.candidate_content_hash) != key.relation_content_fingerprint
            or key.source_mistake_public_id not in index.get(key.relation_content_fingerprint, ())
            for key in keys
        ):
            raise ValueError("claim not supported by frozen source and selected content")
    except (KeyError, TypeError, ValueError) as exc:
        raise D2PublicationError("priority_claim_source_invalid", "一次性优先声明与已选题面或冻结错题谱系不一致") from exc


def replace_after_priority_conflict(
    db, *, state, job_id, worker_id, lease_attempt, subject_id,
    mistake_relations, limit, search_guard, rejected=(),
):
    if db.execute(f"SELECT count(*) FROM {RESELECTIONS} WHERE job_id=?", (job_id,)).fetchone()[0] >= limit:
        raise D2PublicationError("priority_reselection_budget_exhausted", "一次性优先并发冲突，已达到本任务重选预算")
    started = time.monotonic()
    from .exam_generation_d2_runtime import local_factors
    base = prepare_priority_base(db, base=state.base, subject_id=subject_id, mistake_relations=mistake_relations, rejected=rejected)
    factors = local_factors(base, state.objective_policy, mistake_relations=mistake_relations)
    selection, keys = select_with_priority(
        db, base=base, policy=state.objective_policy, factors=factors,
        subject_id=subject_id, mistake_relations=mistake_relations, search_guard=search_guard,
        solver_version=state.selection.solver_version,
    )
    return persist_reselection(
        db, state=state, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
        base=base, factors=factors, selection=selection, keys=keys, limit=limit,
        reason="priority_consumed_concurrently", started=started,
        search_guard=search_guard,
    )


def persist_reselection(
    db, *, state, job_id, worker_id, lease_attempt, base, factors, selection,
    keys, limit, reason, started, search_guard,
):
    if selection.selection_hash == state.selection.selection_hash:
        raise D2PublicationError("priority_conflict_without_progress", "一次性优先冲突后没有形成新的可用选择")
    _validate_selection(
        selection,
        base=base,
        policy=state.objective_policy,
        factor_receipts=factors,
        search_guard=search_guard,
    )
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        row = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        if row is not None and row["state"] not in {"quality_pending", "quality_passed", "request_frozen"}:
            raise D2PublicationError("priority_reselection_state_invalid", "不得重选已发布或已终止的试卷")
        if db.execute("SELECT 1 FROM exams WHERE generation_job_id=?", (job_id,)).fetchone():
            raise D2PublicationError("priority_reservation_remaining", "旧发布预留尚未清理，不能重选")
        round_number = db.execute(f"SELECT count(*) FROM {RESELECTIONS} WHERE job_id=?", (job_id,)).fetchone()[0] + 1
        if round_number > limit:
            raise D2PublicationError("priority_reselection_budget_exhausted", "已达到本任务重选预算")
        replacement = persist_d2_candidate_chain(
            db, job_id=job_id, base=base, objective_policy=state.objective_policy,
            factor_receipts=factors, selection=selection,
            stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": int((time.monotonic() - started) * 1000)}, advance_head=False,
            _selection_replay_validated=True,
        )
        if keys is not None:
            persist_priority_claims(db, job_id=job_id, selection_hash=selection.selection_hash, keys=keys)
        changed = db.execute(
            f"UPDATE {D2_HEADS} SET graph_hash=?,factor_set_hash=?,selection_hash=?,checkpoint_hash=?,revision=revision+1,updated_at=? "
            "WHERE job_id=? AND state='checkpointed' AND selection_hash=? AND revision=?",
            (base.graph.graph_hash, replacement.factor_set_receipt["factor_set_hash"],
             selection.selection_hash, replacement.checkpoint.checkpoint_hash, _timestamp(None),
             job_id, state.selection.selection_hash, state.head["revision"]),
        )
        if changed.rowcount != 1:
            raise D2PublicationError("priority_reselection_cas_lost", "并发重选状态已由其他尝试推进")
        db.execute(f"INSERT INTO {RESELECTIONS} VALUES(?,?,?,?,?,?,?)", (
            job_id, round_number, state.selection.selection_hash, selection.selection_hash,
            reason, json.dumps(dict(row) if row is not None else None,
                ensure_ascii=False, sort_keys=True, separators=(",", ":")), _timestamp(None),
        ))
        db.execute(f"DELETE FROM {D2_TABLE} WHERE job_id=?", (job_id,))
    return recover_d2_candidate_chain(db, job_id=job_id, search_guard=search_guard)
