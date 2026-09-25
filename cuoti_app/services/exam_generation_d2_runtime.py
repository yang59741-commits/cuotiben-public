"""Run the released local D2 selection through the existing past job path."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from dataclasses import replace
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .exam_generation_past_v3_pipeline import PipelineResult
from .exam_generation_stage5_candidate_base_runner import CandidateStage6D2Base, CANDIDATE_BASE_D2_PROTOCOL
from .exam_generation_stage5_d2_solver import D2_V4_SOLVER_VERSION, solve_stage5_d2_selection
from .exam_generation_stage5_repository import recover_stage5_state
from .exam_generation_stage6_d2_repository import (
    D2_HEADS,
    _validate_selection,
    persist_d2_candidate_chain,
    recover_d2_candidate_chain,
)
from .exam_generation_stage6_local_factor_contract import build_local_factor_receipt, project_local_factor_receipts_for_d2
from .exam_generation_stage6_objective import build_objective_policy
from .exam_generation_mistake_relations import (
    exact_relation_factor, freeze_mistake_relations, relation_index,
)
from .exam_generation_d2_history import exact_history_usage
from .exam_generation_similarity import (
    attach_similarity_freeze,
    build_similarity_freeze,
    calibrated_similarity_rule,
    extend_similarity_freeze,
)


_PARAMETERS = {
    "version": "d2-local-release-20260906-v2",
    "exact_history_order": ["least_successful_uses", "oldest_latest_use"],
    "level_mapping": {
        "verified_mistake_relevance": {"unknown": 0, "not_applicable": 0, "none": 0, "related": 1, "strong": 2},
        "history_exposure_novelty": {"unknown": 0, "not_applicable": 0, "exposed": -1, "novel": 1},
    },
    "directions": {"verified_mistake_relevance": "max", "history_exposure_novelty": "max"},
    "allowed_degradation": 0,
}


def release_objective_policy() -> dict[str, Any]:
    parameters = json.loads(json.dumps(_PARAMETERS))
    parameter_hash = hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return build_objective_policy(parameter_set_hash=parameter_hash, level_mapping=parameters["level_mapping"], directions=parameters["directions"])


def _selection_search_guard(guard, stage):
    next_check = 0.0
    def check():
        nonlocal next_check
        now = time.monotonic()
        if now >= next_check:
            guard(stage)
            next_check = now + 0.1
        return True
    return check


def local_factors(base: CandidateStage6D2Base, policy: Mapping[str, Any], *,
                  mistake_relations: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Use frozen publication history; absent soft facts stay not-applicable."""
    recent = base.constraint.to_dict()["recent_usage"]["payload"]
    history_available = recent.get("mode") == "audit_only"
    exact_usage = exact_history_usage(recent)
    used_ids = set(recent.get("observed_candidate_ids", ()))
    used_hashes = set(recent.get("observed_content_hashes", ()))
    identities = sorted({(e["candidate_id"], e["candidate_fact_hash"], e["content_hash"])
                         for e in base.graph.to_dict()["edges"]})
    relations = relation_index(mistake_relations)
    candidates = {row["candidate_id"]: row for row in base.stage4["candidates"]}
    rows = []
    for cid, fact_hash, content_hash in identities:
        was_used = content_hash in exact_usage if exact_usage is not None else cid in used_ids or content_hash in used_hashes
        novelty = ("exposed" if was_used else "novel") if history_available else "not_applicable"
        rows.append(build_local_factor_receipt(
            candidate_id=cid, candidate_fact_hash=fact_hash, content_hash=content_hash, policy=policy,
            factors={
                "verified_mistake_relevance": exact_relation_factor(candidates.get(cid), relations),
                "history_exposure_novelty": {"level": novelty, "evidence_codes": ["frozen_native_publication_history" if history_available else "optional_history_unavailable"]},
            },
        ))
    return rows


def _similarity_card_facts(db, rows: list[Mapping[str, Any]]) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Read card semantics for the small graph/history scope only."""
    expected = {
        (str(row.get("source_kind")), str(row.get("candidate_id"))): row
        for row in rows if row.get("source_kind") and row.get("candidate_id")
    }
    identities = set(expected)
    for source_kind, candidate_id in tuple(identities):
        # The formal source artifact and the persisted semantic index use
        # different role labels for the same immutable past-question identity.
        # Read both labels, then project the card back onto the candidate's
        # local role without changing its source identity.
        if source_kind in {"past_exam_local", "past_exam_other"}:
            identities.add(("past_exam_local", candidate_id))
            identities.add(("past_exam_other", candidate_id))
    if not identities:
        return {}
    try:
        result = {}
        # Keep the predicate shallow as well as under SQLite's variable limit.
        # A long OR chain can hit SQLITE_MAX_EXPR_DEPTH while another worker
        # is reparsing the schema during stage-7 replay.
        identity_rows = sorted(identities)
        for offset in range(0, len(identity_rows), 100):
            batch = identity_rows[offset:offset + 100]
            clauses = " OR ".join("(q.source_kind=? AND q.candidate_id=?)" for _ in batch)
            params = tuple(value for identity in batch for value in identity)
            query = (
                "SELECT q.source_kind,q.candidate_id,q.content_hash,q.current_card_version_id,"
                "c.card_json,c.card_hash,c.status FROM semantic_questions q "
                "JOIN semantic_card_versions c ON c.id=q.current_card_version_id WHERE " + clauses
            )
            fetched = db.execute(query, params).fetchall()
            for row in fetched:
                expected_row = expected.get((str(row[0]), str(row[1])))
                if expected_row is None and str(row[0]) in {"past_exam_local", "past_exam_other"}:
                    expected_row = expected.get((
                        "past_exam_other" if str(row[0]) == "past_exam_local" else "past_exam_local",
                        str(row[1]),
                    ))
                if expected_row is None or str(row[2]) != str(expected_row.get("content_hash")):
                    continue
                expected_version = expected_row.get("card_version_id")
                if expected_version is not None and int(row[3]) != int(expected_version):
                    continue
                expected_hash = expected_row.get("card_hash")
                if expected_hash is not None and str(row[5]) != str(expected_hash):
                    continue
                try:
                    card = json.loads(str(row[4]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(card, Mapping) or str(row[6]) != "valid":
                    continue
                result[(str(row[0]), str(row[1]))] = card
                # Older usage rows identify a local past card under a normalized
                # role; accepting both labels does not alter source identity.
                if str(row[0]) in {"past_exam_local", "past_exam_other"}:
                    result[("past_exam_local", str(row[1]))] = card
                    result[("past_exam_other", str(row[1]))] = card
        return result
    except sqlite3.OperationalError:
        return {}


def _similarity_history(db, subject_id: int, *, limit: int = 256) -> list[dict[str, Any]]:
    """Project successful published D2 snapshots into a read-only history window."""
    try:
        rows = db.execute(
            "SELECT request_json,quality_response_json,publication_json FROM "
            "exam_generation_d2_quality_publications WHERE state IN "
            "('published_pending_receipt','completed') AND "
            "json_extract(request_json,'$.subject_id')=? "
            "ORDER BY json_extract(publication_json,'$.published_at') DESC LIMIT ?",
            (subject_id, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    result: list[dict[str, Any]] = []
    for request_raw, quality_raw, publication_raw in rows:
        try:
            request = json.loads(str(request_raw))
            quality = json.loads(str(quality_raw))
            publication = json.loads(str(publication_raw))
            # The committed quality envelope stores verdicts in ``results``;
            # the immutable candidate snapshots live in the request draft.
            # Older rehearsal envelopes may carry an ``items`` list, so keep
            # that shape readable without treating missing verdicts as uses.
            if not isinstance(request, Mapping) or not isinstance(quality, Mapping):
                continue
            quality_results = quality.get("results", quality.get("items", []))
            if not isinstance(quality_results, (list, tuple)):
                continue
            passed_positions = {
                int(item["position"])
                for item in quality_results
                if isinstance(item, Mapping) and item.get("passed") is True
                and type(item.get("position")) is int
            }
            draft = request.get("draft", {})
            if not isinstance(draft, Mapping):
                continue
            structured_items = draft.get("structured_items", [])
            if not isinstance(structured_items, (list, tuple)):
                continue
            for item in structured_items:
                if not isinstance(item, Mapping) or type(item.get("position")) is not int:
                    continue
                if item.get("position") not in passed_positions:
                    continue
                # A malformed identity must not poison the frozen similarity
                # graph; keep processing other valid items in this snapshot.
                if not isinstance(item.get("candidate_id"), str) or not item.get("candidate_id"):
                    continue
                if not isinstance(item.get("content_hash"), str) or not item.get("content_hash"):
                    continue
                snapshot = {
                    "schema_version": "semantic-candidate-content-snapshot-v1",
                    "content": item.get("content", ""),
                    "conditions": item.get("conditions", []),
                    "figure_references": item.get("figure_references", []),
                    "options": item.get("options", []),
                }
                result.append({
                    "candidate_id": item.get("candidate_id"),
                    "content_hash": item.get("content_hash"),
                    "source_kind": item.get("source_kind"),
                    "question_type_family": item.get("question_type"),
                    "content_snapshot": snapshot,
                    "order_rank": len(result) + 1,
                })
                if len(result) >= limit:
                    return result
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return result


def freeze_d2_similarity_graph(db, *, base: CandidateStage6D2Base, subject_id: int):
    """Freeze v4 similarity evidence after the final bounded graph is known."""
    graph_payload = base.graph.to_dict()
    graph_ids = {
        str(edge.get("candidate_id")) for edge in graph_payload.get("edges", ())
        if isinstance(edge, Mapping) and edge.get("candidate_id") is not None
    }
    graph_candidates = [
        row for row in base.stage4.get("candidates", ())
        if isinstance(row, Mapping) and str(row.get("candidate_id")) in graph_ids
    ]
    subject = str(base.constraint.to_dict().get("subject") or "")
    existing = graph_payload.get("similarity_receipt")
    if isinstance(existing, Mapping):
        # A priority retry may add a small number of edges.  Extend the
        # already-frozen receipt instead of rereading/reordering history.
        card_facts = _similarity_card_facts(db, graph_candidates)
        receipt = extend_similarity_freeze(
            existing, subject=subject, candidates=graph_candidates,
            graph_edges=graph_payload.get("edges", ()), card_facts=card_facts,
        )
    else:
        history = _similarity_history(db, subject_id)
        card_facts = _similarity_card_facts(db, graph_candidates + history)
        receipt = build_similarity_freeze(
            subject=subject, candidates=graph_candidates,
            graph_edges=graph_payload.get("edges", ()),
            card_facts=card_facts, history_candidates=history,
        )
    return replace(base, graph=attach_similarity_freeze(base.graph, receipt))


def _supports_d2_v4(subject: str) -> bool:
    try:
        calibrated_similarity_rule(subject)
    except (KeyError, TypeError, ValueError):
        return False
    return True


def run_d2_generation(
    db, *, job_id, worker_id, lease_attempt, stage3_contract, facts_loader,
    stage5_runner, stage3_contract_loader, policy_fact_loader,
    classification_binding, verification_binding, classify_callback,
    difficulty_verify_callback, quality_callback, subject_id, title, artifact_dir,
    artifact_callback, receipt_callback=None, lifecycle_guard=None,
    priority_once=False, priority_reselection_limit=2,
    memory=None, pending_memory_request=None, memory_subject=None,
    memory_logger=None, render_probe=None,
) -> PipelineResult:
    from .exam_generation_d2_publication import run_d2_quality_and_publish
    from .exam_generation_v3_publish import _fence
    from .exam_generation_priority_once import PriorityOnceConflictError
    from .exam_generation_d2_quality_repair import (
        D2ContentRepairRequired, known_content_rejections, replace_after_content_rejection,
    )
    from .exam_generation_priority_runtime import (
        load_priority_claims, persist_priority_claims, select_with_priority, prepare_priority_base,
        replace_after_priority_conflict,
    )

    if type(priority_once) is not bool or type(priority_reselection_limit) is not int or priority_reselection_limit < 0:
        raise ValueError("invalid priority-once execution options")

    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}
    calls = Counter()
    elapsed = {}
    stages = {"stage3": "completed", "stage4": "completed"}

    def guard(stage):
        if lifecycle_guard:
            lifecycle_guard(stage=stage)

    def tracked(callback, name):
        def invoke(*args, **kwargs):
            guard(name)
            if callback is None:
                raise RuntimeError(f"{name}_callback_missing")
            calls[name] += 1
            return callback(*args, **kwargs)
        correction = getattr(callback, "correct_schema", None)
        if callable(correction):
            def correct_schema(*args, **kwargs):
                guard(name)
                calls[name] += 1
                return correction(*args, **kwargs)
            invoke.correct_schema = correct_schema
        return invoke

    guard("stage5")
    started = time.monotonic()
    head = db.execute(f"SELECT 1 FROM {D2_HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if head is not None:
        state = recover_d2_candidate_chain(db, job_id=job_id,
            search_guard=_selection_search_guard(guard, "selection_recovery"))
        stages.update(stage5="recovered", stage6="recovered")
    else:
        mistake_relations = freeze_mistake_relations(
            db, job_id=job_id, subject_id=subject_id,
            guard=lambda: _fence(db, job_id, worker_id, lease_attempt),
        )
        result = stage5_runner(
            db=db, **actor, stage3_contract_loader=stage3_contract_loader,
            policy_fact_loader=policy_fact_loader, score_receipt_loader=None,
            classification_binding=classification_binding, verification_binding=verification_binding,
            classify=tracked(classify_callback, "difficulty_classification"),
            verify=tracked(difficulty_verify_callback, "difficulty_verification"),
            search_guard=_selection_search_guard(guard, "stage5_feasibility"),
        )
        elapsed["stage5"] = int((time.monotonic() - started) * 1000)
        if result.status in {"infeasible", "not_completed"} or result.graph_hash is None:
            return PipelineResult(result.status, result.code, result.reason, result.status_code, None,
                                  {**stages, "stage5": result.status}, {}, elapsed, dict(calls), {}, result.witness)
        old_state = recover_stage5_state(db, **actor)
        facts = facts_loader()
        base = CandidateStage6D2Base(CANDIDATE_BASE_D2_PROTOCOL, stage3_contract.to_dict(), facts.to_dict(),
                                    old_state.contract, old_state.active_graph)
        policy = release_objective_policy()
        similarity_v4 = _supports_d2_v4(str(base.constraint.to_dict().get("subject") or ""))
        if priority_once:
            base = prepare_priority_base(db, base=base, subject_id=subject_id, mistake_relations=mistake_relations)
        if similarity_v4:
            base = freeze_d2_similarity_graph(db, base=base, subject_id=subject_id)
        factors = local_factors(base, policy, mistake_relations=mistake_relations)
        guard("stage6_local_selection")
        started = time.monotonic()
        keys = None
        if priority_once:
            priority_kwargs = {
                "solver_version": D2_V4_SOLVER_VERSION,
            } if similarity_v4 else {}
            selection, keys = select_with_priority(
                db, base=base, policy=policy, factors=factors, subject_id=subject_id,
                mistake_relations=mistake_relations,
                **priority_kwargs,
                search_guard=_selection_search_guard(guard, "stage6_local_selection"),
            )
        else:
            solver_kwargs = {"solver_version": D2_V4_SOLVER_VERSION} if similarity_v4 else {}
            selection = solve_stage5_d2_selection(
                base.constraint, base.graph, objective_policy=policy,
                factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy),
                target_mode="past", m_once_eligible_identities=None,
                **solver_kwargs,
                search_guard=_selection_search_guard(guard, "stage6_local_selection"),
            )
        elapsed["stage6"] = int((time.monotonic() - started) * 1000)
        # Replay is deliberately outside the write transaction.  Large v4
        # searches must not prevent the job heartbeat from renewing its lease.
        _validate_selection(
            selection,
            base=base,
            policy=policy,
            factor_receipts=factors,
            search_guard=_selection_search_guard(guard, "stage6_local_selection"),
        )
        with immediate_transaction(db):
            _fence(db, job_id, worker_id, lease_attempt)
            state = persist_d2_candidate_chain(
                db, job_id=job_id, base=base, objective_policy=policy,
                factor_receipts=factors, selection=selection,
                stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": elapsed["stage6"]},
                _selection_replay_validated=True,
            )
            if keys is not None:
                persist_priority_claims(db, job_id=job_id, selection_hash=selection.selection_hash, keys=keys)
        # Full parent/selection/checkpoint recovery must run after the write
        # lock is released so the heartbeat can renew the lease.
        state = recover_d2_candidate_chain(
            db, job_id=job_id, search_guard=_selection_search_guard(guard, "stage6_local_selection")
        )
        stages.update(stage5="completed", stage6="local_d2_completed")
    guard("stage7")
    started = time.monotonic()
    while True:
        claims = load_priority_claims(db, job_id=job_id, state=state)
        if priority_once != (claims is not None):
            raise ValueError("priority-once request differs from its frozen selection")
        try:
            outcome = run_d2_quality_and_publish(
                db, **actor, state=state, facts=facts_loader(), stage3_contract=stage3_contract,
                subject_id=subject_id, title=title, artifact_dir=str(artifact_dir),
                artifact_callback=tracked(artifact_callback, "artifact"),
                quality_callback=tracked(quality_callback, "quality"), receipt_callback=receipt_callback,
                lifecycle_guard=lambda event: guard(event["stage"]),
                priority_claims=claims,
                memory=memory,
                pending_memory_request=pending_memory_request,
                memory_subject=memory_subject,
                memory_logger=memory_logger,
                render_probe=render_probe,
            )
            break
        except PriorityOnceConflictError:
            if not priority_once:
                raise
            guard("priority_reselection")
            mistake_relations = freeze_mistake_relations(
                db, job_id=job_id, subject_id=subject_id,
                guard=lambda: _fence(db, job_id, worker_id, lease_attempt),
            )
            state = replace_after_priority_conflict(
                db, state=state, **actor, subject_id=subject_id, mistake_relations=mistake_relations,
                limit=priority_reselection_limit,
                search_guard=_selection_search_guard(guard, "priority_reselection"),
                rejected=known_content_rejections(db, job_id),
            )
        except D2ContentRepairRequired as rejection:
            guard("quality_reselection")
            mistake_relations = freeze_mistake_relations(
                db, job_id=job_id, subject_id=subject_id,
                guard=lambda: _fence(db, job_id, worker_id, lease_attempt),
            )
            state = replace_after_content_rejection(
                db, state=state, **actor, subject_id=subject_id,
                mistake_relations=mistake_relations, rejected=rejection.content_hashes,
                limit=priority_reselection_limit,
                search_guard=_selection_search_guard(guard, "quality_reselection"),
            )
    elapsed["quality_and_publication"] = int((time.monotonic() - started) * 1000)
    stages.update(stage7="completed", publication=outcome.state)
    receipt_pending = outcome.status == "published_pending_receipt"
    memory_pending = False
    if receipt_pending:
        from .exam_generation_memory_delivery import (
            GenerationMemoryDeliveryError,
            recover_generation_memory_delivery,
        )
        try:
            memory_pending = (
                recover_generation_memory_delivery(db, operation_id=job_id).state
                == "pending"
            )
        except GenerationMemoryDeliveryError as exc:
            if exc.reason != "memory_delivery_missing":
                raise
            # Old D2 publications have no outbox and remain receipt-only.
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).casefold():
                raise
    return PipelineResult(
        outcome.status,
        (
            "memory_delivery_pending" if memory_pending
            else "receipt_pending" if receipt_pending
            else "exam_past_v3_completed"
        ),
        (
            "memory_delivery_pending" if memory_pending
            else "publication_receipt_pending" if receipt_pending
            else "local_d2_published"
        ),
        202 if receipt_pending else 200,
        dict(outcome.exam), stages,
        {"selection_hash": state.selection.selection_hash, "objective_hash": state.selection.objective_hash,
         "checkpoint_hash": state.checkpoint.checkpoint_hash, "publication_hash": outcome.publication_hash},
        elapsed, dict(calls), {},
        actions=("complete_publication_receipt", "retry_the_same_job") if receipt_pending else (),
    )
