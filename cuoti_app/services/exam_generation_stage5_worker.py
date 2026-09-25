"""Closed production orchestration for Stage 5 past-exam selection.

Only the current job's immutable Stage-3/4 payloads and explicitly injected,
hashed policy/scoring facts are accepted.  This module deliberately has no
legacy-state lookup and no provider implementation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_stage4_candidate_facts import FrozenCandidateFacts
from .exam_generation_stage4_difficulty import (
    locate_difficulty_defect_component,
    resolve_effective_difficulties,
)
from .exam_generation_stage4_repository import (
    Stage4RepositoryError,
    acquire_difficulty_flight,
    complete_difficulty_flight,
    recover_completed_difficulty_result,
    recover_completed_difficulty_result_for_component,
)
from .exam_generation_stage5_repository import (
    Stage5RepositoryError,
    persist_candidate_graph,
    persist_global_constraint,
    persist_infeasible_witness,
    persist_selection,
    recover_stage5_state,
)
from .exam_generation_stage5_solver import (
    EFFECTIVE_DIFFICULTY_SET_VERSION,
    Stage5ContractError,
    Stage5InfeasibleError,
    Stage5SearchIncompleteError,
    build_candidate_edge_graph,
    build_difficulty_defect_edges,
    build_global_constraint_contract,
    locate_candidate_graph_deficit,
    solve_global_selection,
)


POLICY_FACT_SET_VERSION = "past-v3-stage5-policy-fact-set-v1"


class Stage5WorkerError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Stage5WorkerResult:
    status: str
    code: str
    reason: str
    status_code: int
    constraint_hash: str | None = None
    graph_hash: str | None = None
    selection_hash: str | None = None
    witness: Mapping[str, Any] | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def freeze_policy_fact(*, version: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Create one detached versioned policy fact; validation remains solver-owned."""

    if not isinstance(version, str) or not version.strip() or not isinstance(payload, Mapping):
        raise Stage5WorkerError("exam_stage5_policy_fact_invalid", "policy_fact_input_invalid", "阶段 5 政策事实无效", status_code=422)
    body = {"version": version.strip(), "payload": json.loads(_canonical(payload))}
    return {**body, "hash": _digest(body)}


def freeze_chapter_weight_fact(*, version: str, subject: str, weights: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(version, str) or not version.strip() or not isinstance(subject, str) or not subject.strip() or not isinstance(weights, Mapping):
        raise Stage5WorkerError("exam_stage5_policy_fact_invalid", "chapter_weight_fact_input_invalid", "章节权重事实无效", status_code=422)
    body = {"version": version.strip(), "subject": subject.strip(), "weights": json.loads(_canonical(weights))}
    return {**body, "fact_hash": _digest(body)}


def freeze_policy_fact_set(
    *,
    source_identity: Mapping[str, Any],
    chapter_weight_fact: Mapping[str, Any],
    recent_usage: Mapping[str, Any],
    diversity_policy: Mapping[str, Any],
    coverage_policy: Mapping[str, Any],
    uniqueness_policy: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "contract_version": POLICY_FACT_SET_VERSION,
        "source_identity": json.loads(_canonical(source_identity)),
        "chapter_weight_fact": json.loads(_canonical(chapter_weight_fact)),
        "recent_usage": json.loads(_canonical(recent_usage)),
        "diversity_policy": json.loads(_canonical(diversity_policy)),
        "coverage_policy": json.loads(_canonical(coverage_policy)),
        "uniqueness_policy": json.loads(_canonical(uniqueness_policy)),
    }
    return {**body, "fact_set_hash": _digest(body)}


def _policy_facts(value: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        raise Stage5WorkerError("exam_stage5_policy_adapter_pending", "stage5_policy_fact_adapter_pending", "阶段 5 缺少正式政策事实适配器", status_code=422)
    expected = {
        "contract_version", "source_identity", "chapter_weight_fact", "recent_usage",
        "diversity_policy", "coverage_policy", "uniqueness_policy", "fact_set_hash",
    }
    if set(value) != expected or value.get("contract_version") != POLICY_FACT_SET_VERSION:
        raise Stage5WorkerError("exam_stage5_policy_fact_invalid", "policy_fact_set_shape_invalid", "阶段 5 政策事实集合无效", status_code=422)
    body = {key: value[key] for key in expected if key != "fact_set_hash"}
    if _digest(body) != value.get("fact_set_hash"):
        raise Stage5WorkerError("exam_stage5_policy_fact_invalid", "policy_fact_set_hash_mismatch", "阶段 5 政策事实集合哈希不匹配", status_code=422)
    return {key: value[key] for key in expected if key not in {"contract_version", "source_identity", "fact_set_hash"}}


def _validate_policy_source_identity(policy_facts: Mapping[str, Any], stage3: Mapping[str, Any]) -> None:
    if policy_facts.get("source_identity") != stage3.get("source_identity"):
        raise Stage5WorkerError("exam_stage5_policy_fact_invalid", "policy_source_identity_mismatch", "阶段 5 政策事实权威来源身份漂移", status_code=422)


def _load_upstream(db, job_id: str) -> tuple[Mapping[str, Any], FrozenCandidateFacts, Mapping[str, Any], Mapping[str, Any]]:
    stage3_row = db.execute("SELECT * FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,)).fetchone()
    stage4_row = db.execute("SELECT * FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()
    if stage3_row is None or stage4_row is None:
        raise Stage5WorkerError("exam_stage5_contract_conflict", "upstream_receipt_missing", "阶段 5 缺少正式阶段 3/4 回执")
    try:
        stage4 = json.loads(str(stage4_row["facts_payload_json"]))
    except (TypeError, ValueError) as exc:
        raise Stage5WorkerError("exam_stage5_hash_mismatch", "stage4_payload_invalid", "阶段 4 正式候选事实无效") from exc
    if _digest(stage4) != stage4_row["candidate_facts_hash"] or stage4.get("projection_identity_hash") != stage4_row["projection_identity_hash"]:
        raise Stage5WorkerError("exam_stage5_hash_mismatch", "stage4_payload_hash_mismatch", "阶段 4 正式候选事实哈希漂移")
    candidates = stage4.get("candidates")
    unresolved = stage4.get("unresolved_candidate_ids")
    if not isinstance(candidates, list) or not isinstance(unresolved, list):
        raise Stage5WorkerError("exam_stage5_hash_mismatch", "stage4_payload_shape_invalid", "阶段 4 正式候选事实形状无效")
    frozen = FrozenCandidateFacts(
        str(stage4.get("contract_version")), str(stage4_row["projection_identity_hash"]),
        str(stage4_row["candidate_facts_hash"]), str(stage4.get("target_institution") or ""),
        tuple(candidates), tuple(unresolved), _canonical(stage4),
    )
    return dict(stage3_row), frozen, stage4, dict(stage4_row)


def _effective_receipt(frozen: FrozenCandidateFacts, results: list[Any]) -> Mapping[str, Any] | None:
    if not results:
        return None
    members = sorted(
        ({"result_hash": result.result_hash, "payload": result.to_dict()} for result in results),
        key=lambda item: item["result_hash"],
    )
    body = {
        "contract_version": EFFECTIVE_DIFFICULTY_SET_VERSION,
        "candidate_facts_hash": frozen.candidate_facts_hash,
        "results": members,
    }
    return {**body, "receipt_hash": _digest(body)}


def _pending(code: str, reason: str, *, contract_hash: str | None = None, graph_hash: str | None = None) -> Stage5WorkerResult:
    return Stage5WorkerResult("adapter_pending", code, reason, 422, contract_hash, graph_hash)


def run_stage5_selection_worker(
    db,
    *,
    job_id: str,
    worker_id: str,
    lease_attempt: int,
    stage3_contract_loader: Callable[..., Mapping[str, Any]] | None = None,
    policy_fact_loader: Callable[..., Mapping[str, Any]] | None = None,
    score_receipt_loader: Callable[..., Mapping[str, Any]] | None = None,
    classification_binding: Mapping[str, Any] | None = None,
    verification_binding: Mapping[str, Any] | None = None,
    classify: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    verify: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    search_guard: Callable[[], bool] | None = None,
) -> Stage5WorkerResult:
    """Recover or advance exactly one Stage-5 attempt; never publish."""

    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}
    try:
        state = recover_stage5_state(db, **actor)
    except Stage5RepositoryError as exc:
        if exc.reason != "constraint_receipt_missing":
            raise
        state = None
    if state is not None and state.selection is not None:
        return Stage5WorkerResult(
            "adapter_pending", "exam_stage6_adapter_pending", "stage6_adapter_pending", 422,
            state.contract.constraint_hash, state.active_graph.graph_hash if state.active_graph else None,
            state.selection.selection_hash, None,
        )

    stage3_row, frozen, stage4, _stage4_row = _load_upstream(db, job_id)
    if state is None:
        if stage3_contract_loader is None:
            return _pending("exam_stage5_adapter_pending", "stage3_contract_payload_adapter_pending")
        stage3 = stage3_contract_loader(job_id=job_id, stage3_receipt=dict(stage3_row))
        if not isinstance(stage3, Mapping) or _digest(stage3) != stage3_row["stage3_contract_hash"]:
            raise Stage5WorkerError("exam_stage5_hash_mismatch", "stage3_contract_payload_hash_mismatch", "阶段 3 正式合同载荷哈希漂移")
        if policy_fact_loader is None:
            return _pending("exam_stage5_policy_adapter_pending", "stage5_policy_fact_adapter_pending")
        loaded_policy_facts = policy_fact_loader(job_id=job_id, stage3=dict(stage3), stage4=stage4)
        facts = _policy_facts(loaded_policy_facts)
        _validate_policy_source_identity(loaded_policy_facts, stage3)
        try:
            contract = build_global_constraint_contract(
                stage3=stage3, stage3_contract_hash=str(stage3_row["stage3_contract_hash"]),
                stage4=stage4, candidate_facts_hash=frozen.candidate_facts_hash,
                projection_identity_hash=frozen.projection_identity_hash,
                policy_fact_set_hash=str(loaded_policy_facts["fact_set_hash"]), **facts,
            )
            head = persist_global_constraint(db, contract=contract, **actor)
        except (Stage5ContractError, Stage5RepositoryError):
            raise
    else:
        contract, head = state.contract, state.head

    # Recompute the same bounded provisional graph only from frozen authority.
    # It is not persisted until any real difficulty defect has been resolved.
    effective_results: list[Any] = []
    provisional = build_candidate_edge_graph(contract, stage4=stage4)
    try:
        structural_witness = locate_candidate_graph_deficit(contract, provisional, search_guard=search_guard)
    except Stage5SearchIncompleteError as exc:
        return Stage5WorkerResult("not_completed", exc.code, exc.reason, exc.status_code, contract.constraint_hash)
    deficit_slots = list(structural_witness.get("deficient_slot_ids") or ()) if structural_witness else []

    component = locate_difficulty_defect_component(
        frozen,
        deficit_slot_ids=deficit_slots,
        edges=build_difficulty_defect_edges(contract, stage4=stage4),
    )
    if component.candidate_ids:
        if classification_binding is None or verification_binding is None:
            return _pending("stage4-difficulty-adapter-pending", "difficulty_binding_adapter_pending", contract_hash=contract.constraint_hash)
        recovered = recover_completed_difficulty_result_for_component(
            db, frozen=frozen, component=component,
            classification_binding=classification_binding,
            verification_binding=verification_binding, **actor,
        )
        if recovered is not None:
            effective_results.append(recovered)
            provisional = build_candidate_edge_graph(
                contract, stage4=stage4,
                effective_difficulty_receipt=_effective_receipt(frozen, effective_results),
            )
        elif classify is None or verify is None:
            return _pending("stage4-difficulty-adapter-pending", "difficulty_callbacks_adapter_pending", contract_hash=contract.constraint_hash)
        else:
            claim = acquire_difficulty_flight(
                db, frozen=frozen, component=component,
                classification_binding=classification_binding, verification_binding=verification_binding,
                **actor,
            )
            recovered = recover_completed_difficulty_result(db, flight_hash=claim.flight_hash, **actor) if claim.state == "completed" else None
            if recovered is not None:
                effective_results.append(recovered)
            elif not claim.execute:
                return _pending("stage4-difficulty-flight-pending", "difficulty_single_flight_owned", contract_hash=contract.constraint_hash)
            else:
                result = resolve_effective_difficulties(
                    frozen, component, classification_binding=classification_binding,
                    verification_binding=verification_binding, classify=classify, verify=verify,
                )
                complete_difficulty_flight(db, flight_hash=claim.flight_hash, expected_revision=claim.revision, result=result, **actor)
                effective_results.append(result)
            provisional = build_candidate_edge_graph(contract, stage4=stage4, effective_difficulty_receipt=_effective_receipt(frozen, effective_results))

    if state is None or state.initial_graph is None:
        head = persist_candidate_graph(db, graph=provisional, expected_revision=head.revision, **actor)
        graph = provisional
    else:
        graph = state.active_graph or state.initial_graph
        head = state.head
        if state.witness is not None and graph.graph_mode == "expanded":
            return Stage5WorkerResult("infeasible", "exam_selection_infeasible", str(state.witness["kind"]), 422, contract.constraint_hash, graph.graph_hash, witness=state.witness)

    # Complete all score-independent feasibility work before asking Stage 6
    # for a real scoring receipt.  A constrained witness can still be caused
    # by the bounded chapter window, so give both witness kinds one additive
    # 6N expansion before treating the remaining witness as infeasible.
    try:
        feasibility_witness = locate_candidate_graph_deficit(contract, graph, search_guard=search_guard)
    except Stage5SearchIncompleteError as exc:
        return Stage5WorkerResult("not_completed", exc.code, exc.reason, exc.status_code, contract.constraint_hash, graph.graph_hash)
    if feasibility_witness is not None:
        persist_infeasible_witness(db, witness=feasibility_witness, **actor)
        if graph.graph_mode != "initial":
            return Stage5WorkerResult(
                "infeasible", "exam_selection_infeasible", str(feasibility_witness["kind"]), 422,
                contract.constraint_hash, graph.graph_hash, witness=feasibility_witness,
            )
        expanded_slot_ids = tuple(feasibility_witness.get("deficient_slot_ids") or ())
        if not expanded_slot_ids:
            return Stage5WorkerResult(
                "infeasible", "exam_selection_infeasible", str(feasibility_witness["kind"]), 422,
                contract.constraint_hash, graph.graph_hash, witness=feasibility_witness,
            )
        expanded = build_candidate_edge_graph(
            contract, stage4=stage4,
            effective_difficulty_receipt=_effective_receipt(frozen, effective_results),
            expanded_slot_ids=expanded_slot_ids,
        )
        if expanded.to_dict().get("edges") == graph.to_dict().get("edges"):
            # A different graph mode alone is not a repair.  Avoid persisting
            # an empty expansion for a genuine quota/identity deficit.
            return Stage5WorkerResult(
                "infeasible", "exam_selection_infeasible", str(feasibility_witness["kind"]), 422,
                contract.constraint_hash, graph.graph_hash, witness=feasibility_witness,
            )
        head = persist_candidate_graph(
            db, graph=expanded, base_graph_hash=graph.graph_hash,
            expected_revision=head.revision, **actor,
        )
        graph = expanded
        try:
            final_witness = locate_candidate_graph_deficit(contract, graph, search_guard=search_guard)
        except Stage5SearchIncompleteError as exc:
            return Stage5WorkerResult("not_completed", exc.code, exc.reason, exc.status_code, contract.constraint_hash, graph.graph_hash)
        if final_witness is not None:
            persist_infeasible_witness(db, witness=final_witness, **actor)
            return Stage5WorkerResult(
                "infeasible", "exam_selection_infeasible", str(final_witness["kind"]), 422,
                contract.constraint_hash, graph.graph_hash, witness=final_witness,
            )

    if score_receipt_loader is None:
        return _pending("exam_stage6_adapter_pending", "stage6_scoring_adapter_pending", contract_hash=contract.constraint_hash, graph_hash=graph.graph_hash)
    score_receipt = score_receipt_loader(job_id=job_id, contract=contract, graph=graph)
    try:
        selection = solve_global_selection(contract, graph, score_receipt=score_receipt)
    except Stage5InfeasibleError as first:
        persist_infeasible_witness(db, witness=first.witness, **actor)
        return Stage5WorkerResult("infeasible", first.code, first.reason, first.status_code, contract.constraint_hash, graph.graph_hash, witness=first.witness)
    head = persist_selection(db, selection=selection, expected_revision=head.revision, **actor)
    return Stage5WorkerResult(
        "adapter_pending", "exam_stage6_adapter_pending", "stage6_adapter_pending", 422,
        contract.constraint_hash, graph.graph_hash, selection.selection_hash,
    )


__all__ = [
    "POLICY_FACT_SET_VERSION", "Stage5WorkerError", "Stage5WorkerResult",
    "freeze_chapter_weight_fact", "freeze_policy_fact", "freeze_policy_fact_set",
    "run_stage5_selection_worker",
]
