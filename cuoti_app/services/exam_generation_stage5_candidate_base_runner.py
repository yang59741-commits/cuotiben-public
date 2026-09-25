"""Provider-free candidate Stage-5/D2 runner seam.

This is deliberately an in-memory adapter.  It consumes only the immutable
Stage-3 and Stage-4 payloads and hands the resulting hard-constraint contract
and bounded edge graph to a future ``run_candidate_stage6_d2`` implementation.
It does not open a database, invoke a provider, or serialize legacy score /
penalty fields.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

from .exam_generation_stage5_solver import (
    FrozenCandidateEdgeGraph,
    Stage5GlobalConstraintContract,
    build_candidate_edge_graph,
    build_global_constraint_contract,
)


CANDIDATE_BASE_D2_PROTOCOL = "past-v3-stage6-candidate-base-d2-v1"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _frozen_mapping(value: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    """Capture a JSON-safe immutable-by-convention copy of an upstream receipt."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError(f"{field} is not canonical JSON") from exc


@dataclass(frozen=True, slots=True)
class CandidateStage6D2Base:
    """Explicit hand-off object for a provider-backed Stage-6 D2 runner."""

    protocol: str
    stage3: Mapping[str, Any]
    stage4: Mapping[str, Any]
    constraint: Stage5GlobalConstraintContract
    graph: FrozenCandidateEdgeGraph

    @property
    def candidate_base_contract(self) -> Mapping[str, Any]:
        """The explicit protocol envelope consumed by Stage 6 D2."""
        return {
            "protocol": self.protocol,
            "constraint_hash": self.constraint.constraint_hash,
            "graph_hash": self.graph.graph_hash,
            "graph_contract_version": self.graph.contract_version,
            "stage3_contract_hash": self.constraint.stage3_contract_hash,
            "candidate_facts_hash": self.constraint.candidate_facts_hash,
            "projection_identity_hash": self.constraint.projection_identity_hash,
            "policy_fact_set_hash": self.constraint.to_dict()["policy_fact_set_hash"],
            "legacy_scoring": False,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": self.protocol,
            "candidate_base_contract": dict(self.candidate_base_contract),
            "stage3": dict(self.stage3),
            "stage4": dict(self.stage4),
            "constraint": self.constraint.to_dict(),
            "graph": self.graph.to_dict(),
        }


def build_candidate_stage5_d2_base(
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    *,
    stage3_contract_hash: str,
    candidate_facts_hash: str,
    projection_identity_hash: str,
    chapter_weight_fact: Mapping[str, Any],
    recent_usage: Mapping[str, Any],
    diversity_policy: Mapping[str, Any],
    coverage_policy: Mapping[str, Any],
    uniqueness_policy: Mapping[str, Any],
    policy_fact_set_hash: str,
    effective_difficulty_receipt: Mapping[str, Any] | None = None,
) -> CandidateStage6D2Base:
    """Build the provider-free D2 base contract and graph in memory.

    Policy arguments are already-frozen receipts (``{version,payload,hash}``).
    Every identity and policy is required: omitted or forged receipts fail
    closed in the existing Stage-5 contract validator.
    """
    if not isinstance(stage3, Mapping) or not isinstance(stage4, Mapping):
        raise TypeError("stage3 and stage4 must be mappings")
    n = stage3.get("question_count")
    if type(n) is not int or n < 1:
        raise ValueError("stage3.question_count must be a positive integer")
    candidates = stage4.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("stage4.candidates must be a list")
    if any(not isinstance(value, Mapping) for value in (chapter_weight_fact, recent_usage, diversity_policy, coverage_policy, uniqueness_policy)):
        raise TypeError("all frozen policy receipts are required mappings")
    constraint = build_global_constraint_contract(
        stage3=stage3, stage3_contract_hash=stage3_contract_hash, stage4=stage4,
        candidate_facts_hash=candidate_facts_hash,
        projection_identity_hash=projection_identity_hash,
        chapter_weight_fact=chapter_weight_fact, recent_usage=recent_usage,
        diversity_policy=diversity_policy, coverage_policy=coverage_policy,
        uniqueness_policy=uniqueness_policy, policy_fact_set_hash=policy_fact_set_hash,
    )
    graph = build_candidate_edge_graph(constraint, stage4=stage4, effective_difficulty_receipt=effective_difficulty_receipt)
    return CandidateStage6D2Base(
        CANDIDATE_BASE_D2_PROTOCOL,
        _frozen_mapping(stage3, "stage3"),
        _frozen_mapping(stage4, "stage4"),
        constraint,
        graph,
    )


__all__ = ["CANDIDATE_BASE_D2_PROTOCOL", "CandidateStage6D2Base", "build_candidate_stage5_d2_base"]
