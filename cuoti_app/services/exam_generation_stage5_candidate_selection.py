"""Pure candidate-base Stage-5 selection receipt without legacy score fields.

The exact solver is reused internally with its candidate-utility input, then
the result is re-issued under a candidate-only v2 identity bound to graph
utility receipt v2.  No ``score``, ``penalty``, or ``score_identity_hash`` is
serialized by this contract.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .exam_generation_stage5_solver import (
    FrozenCandidateEdgeGraph,
    SOLVER_VERSION,
    Stage5GlobalConstraintContract,
    solve_global_selection,
)
from .exam_generation_stage6_rubric_repository import (
    Stage6RubricRepositoryError,
    build_graph_utility_receipt_v2,
    validate_graph_utility_receipt_v2,
)


CANDIDATE_SELECTION_VERSION = "past-v3-stage5-candidate-selection-v2"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN_KEYS = {"score", "penalty", "score_identity_hash"}


class CandidateSelectionV2Error(RuntimeError):
    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CandidateStage5Selection:
    contract_version: str
    graph_hash: str
    constraint_hash: str
    utility_receipt_hash: str
    total_utility: int
    selection_hash: str
    assignments: tuple[Mapping[str, Any], ...]
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _selection_payload(self)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise CandidateSelectionV2Error("candidate_selection_duplicate_json_key")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise CandidateSelectionV2Error("candidate_selection_non_finite_number")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise CandidateSelectionV2Error("candidate_selection_non_canonical_json") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise CandidateSelectionV2Error(f"candidate_selection_{field}_invalid")
    return value


def _reject_forbidden(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(str(key).casefold() in _FORBIDDEN_KEYS for key in value):
            raise CandidateSelectionV2Error("candidate_selection_legacy_field_forbidden")
        for item in value.values():
            _reject_forbidden(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_forbidden(item)


def _validated_utility_pair(
    *,
    utility_receipt: Mapping[str, Any],
    utility_receipt_v2: Mapping[str, Any],
    rubric_set_receipt: Mapping[str, Any],
    member_receipts: Sequence[Mapping[str, Any]],
    active_graph_hash: str,
    rejection_ledger_head_hash: str,
    policy_hash: str,
) -> dict[str, Any]:
    try:
        rebuilt = build_graph_utility_receipt_v2(
            utility_receipt,
            rubric_set_receipt=rubric_set_receipt,
            member_receipts=member_receipts,
            active_graph_hash=active_graph_hash,
            rejection_ledger_head_hash=rejection_ledger_head_hash,
            policy_hash=policy_hash,
        )
        validated = validate_graph_utility_receipt_v2(
            utility_receipt_v2,
            rubric_set_receipt=rubric_set_receipt,
            member_receipts=member_receipts,
            active_graph_hash=active_graph_hash,
            rejection_ledger_head_hash=rejection_ledger_head_hash,
            policy_hash=policy_hash,
        )
    except Stage6RubricRepositoryError as exc:
        raise CandidateSelectionV2Error(str(exc)) from exc
    if _canonical(rebuilt) != _canonical(validated):
        raise CandidateSelectionV2Error("candidate_selection_utility_source_mismatch")
    return validated


def build_candidate_selection_v2(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
    *,
    utility_receipt: Mapping[str, Any],
    utility_receipt_v2: Mapping[str, Any],
    rubric_set_receipt: Mapping[str, Any],
    member_receipts: Sequence[Mapping[str, Any]],
    rejection_ledger_head_hash: str,
    policy_hash: str,
) -> CandidateStage5Selection:
    """Run the existing exact solver and emit only candidate utility identity."""

    if not isinstance(contract, Stage5GlobalConstraintContract):
        raise CandidateSelectionV2Error("candidate_selection_contract_invalid")
    if not isinstance(graph, FrozenCandidateEdgeGraph):
        raise CandidateSelectionV2Error("candidate_selection_graph_invalid")
    utility_v2 = _validated_utility_pair(
        utility_receipt=utility_receipt,
        utility_receipt_v2=utility_receipt_v2,
        rubric_set_receipt=rubric_set_receipt,
        member_receipts=member_receipts,
        active_graph_hash=graph.graph_hash,
        rejection_ledger_head_hash=rejection_ledger_head_hash,
        policy_hash=policy_hash,
    )
    solved = solve_global_selection(
        contract, graph, score_receipt=utility_receipt
    )
    solved_payload = solved.to_dict()
    assignments = [dict(item) for item in solved.assignments]
    _reject_forbidden(assignments)
    identity = {
        "contract_version": CANDIDATE_SELECTION_VERSION,
        "solver_version": SOLVER_VERSION,
        "stage3_contract_hash": solved_payload["stage3_contract_hash"],
        "candidate_facts_hash": solved_payload["candidate_facts_hash"],
        "projection_identity_hash": solved_payload["projection_identity_hash"],
        "profile_hash": solved_payload["profile_hash"],
        "exact_slot_hash": solved_payload["exact_slot_hash"],
        "graph_hash": graph.graph_hash,
        "constraint_hash": contract.constraint_hash,
        "rubric_set_receipt_hash": utility_v2["rubric_set_receipt_hash"],
        "utility_receipt_hash": utility_v2["receipt_hash"],
        "parameter_set_hash": utility_v2["parameter_set_hash"],
        "policy_hash": utility_v2["policy_hash"],
        "rejection_ledger_head_hash": utility_v2[
            "rejection_ledger_head_hash"
        ],
        "assignments": assignments,
    }
    _reject_forbidden(identity)
    selection_hash = _digest(identity)
    payload = {
        **identity,
        "total_utility": solved.total_utility,
        "selection_hash": selection_hash,
    }
    raw = _canonical(payload)
    return CandidateStage5Selection(
        CANDIDATE_SELECTION_VERSION,
        graph.graph_hash,
        contract.constraint_hash,
        utility_v2["receipt_hash"],
        solved.total_utility,
        selection_hash,
        tuple(assignments),
        raw,
    )


def _selection_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, CandidateStage5Selection):
        raise CandidateSelectionV2Error("candidate_selection_type_invalid")
    try:
        payload = json.loads(
            value._payload_json,
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except CandidateSelectionV2Error:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise CandidateSelectionV2Error("candidate_selection_payload_invalid") from exc
    identity_fields = {
        "contract_version",
        "solver_version",
        "stage3_contract_hash",
        "candidate_facts_hash",
        "projection_identity_hash",
        "profile_hash",
        "exact_slot_hash",
        "graph_hash",
        "constraint_hash",
        "rubric_set_receipt_hash",
        "utility_receipt_hash",
        "parameter_set_hash",
        "policy_hash",
        "rejection_ledger_head_hash",
        "assignments",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != identity_fields | {"total_utility", "selection_hash"}
        or payload.get("contract_version") != CANDIDATE_SELECTION_VERSION
        or payload.get("solver_version") != SOLVER_VERSION
        or not isinstance(payload.get("assignments"), list)
        or not payload["assignments"]
        or type(payload.get("total_utility")) is not int
    ):
        raise CandidateSelectionV2Error("candidate_selection_shape_invalid")
    _reject_forbidden(payload)
    for field in identity_fields - {"contract_version", "solver_version", "assignments"}:
        _hash(payload.get(field), field)
    _hash(payload.get("selection_hash"), "selection_hash")
    if any(
        not isinstance(item, Mapping)
        or type(item.get("utility")) is not int
        for item in payload["assignments"]
    ):
        raise CandidateSelectionV2Error("candidate_selection_assignment_invalid")
    if sum(item["utility"] for item in payload["assignments"]) != payload["total_utility"]:
        raise CandidateSelectionV2Error("candidate_selection_total_invalid")
    identity = {field: payload[field] for field in identity_fields}
    if payload["selection_hash"] != _digest(identity):
        raise CandidateSelectionV2Error("candidate_selection_hash_mismatch")
    normalized = _canonical(payload)
    if (
        value.contract_version != CANDIDATE_SELECTION_VERSION
        or value.graph_hash != payload["graph_hash"]
        or value.constraint_hash != payload["constraint_hash"]
        or value.utility_receipt_hash != payload["utility_receipt_hash"]
        or value.total_utility != payload["total_utility"]
        or value.selection_hash != payload["selection_hash"]
        or _canonical(list(value.assignments)) != _canonical(payload["assignments"])
        or value._payload_json != normalized
    ):
        raise CandidateSelectionV2Error("candidate_selection_integrity_mismatch")
    return dict(payload)


def validate_candidate_selection_v2(
    value: CandidateStage5Selection,
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
    *,
    utility_receipt: Mapping[str, Any],
    utility_receipt_v2: Mapping[str, Any],
    rubric_set_receipt: Mapping[str, Any],
    member_receipts: Sequence[Mapping[str, Any]],
    rejection_ledger_head_hash: str,
    policy_hash: str,
) -> dict[str, Any]:
    payload = _selection_payload(value)
    expected = build_candidate_selection_v2(
        contract,
        graph,
        utility_receipt=utility_receipt,
        utility_receipt_v2=utility_receipt_v2,
        rubric_set_receipt=rubric_set_receipt,
        member_receipts=member_receipts,
        rejection_ledger_head_hash=rejection_ledger_head_hash,
        policy_hash=policy_hash,
    )
    if value.selection_hash != expected.selection_hash or payload != expected.to_dict():
        raise CandidateSelectionV2Error("candidate_selection_binding_drift")
    return payload


__all__ = [
    "CANDIDATE_SELECTION_VERSION",
    "CandidateSelectionV2Error",
    "CandidateStage5Selection",
    "build_candidate_selection_v2",
    "validate_candidate_selection_v2",
]
