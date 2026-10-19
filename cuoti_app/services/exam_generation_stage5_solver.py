"""Compatibility imports for the pure selection implementation.

Existing workers, replay tools and frozen contracts keep this import path.
"""

import hashlib

import itertools

import json

import math

import re

import unicodedata

from builtins import any as _builtin_any

from dataclasses import dataclass

from fractions import Fraction

from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage6_rubric import (
    FACTOR_APPLICABILITY_VERSION,
    FACTOR_CATALOG_HASH,
    RUBRIC_CATALOG_VERSION,
    UTILITY_RECEIPT_VERSION,
    CandidateBaseRubricError,
    candidate_base_key,
)

from .exams.selection.contracts import (
    CANDIDATE_GRAPH_VERSION,
    EFFECTIVE_DIFFICULTY_SET_VERSION,
    EFFECTIVE_DIFFICULTY_VERSION,
    EXACT_HISTORY_AUDIT_POLICY_VERSION,
    FrozenCandidateEdgeGraph,
    GLOBAL_CONSTRAINT_VERSION,
    RECENT_USAGE_AUDIT_POLICY_VERSION,
    REPAIR_VERSION,
    SELECTION_VERSION,
    SOLVER_VERSION,
    STAGE3_CONTRACT_VERSION,
    STAGE4_CONTRACT_VERSION,
    STAGE6_RESPONSE_SCHEMA_VERSION,
    STAGE6_SCORE_RECEIPT_VERSION,
    STAGE6_SCORING_IDENTITY_VERSION,
    Stage5ContractError,
    Stage5GlobalConstraintContract,
    Stage5InfeasibleError,
    Stage5RepairResult,
    Stage5SearchIncompleteError,
    Stage5Selection,
    WITNESS_VERSION,
    _COVERAGE_FIELDS,
    _DIFFICULTIES,
    _HASH_RE,
    _MAX_LOCAL_PENALTY,
    _MAX_LOCAL_SCORE,
    _SOURCES,
    _canonical,
    _digest,
    _exact_fields,
    _fraction,
    _hash,
    _largest_remainder,
    _mapping,
    _recent_usage_effect,
    _source,
    _text,
    _text_list,
    _verified_contract_payload,
    _verified_graph_payload,
    build_global_constraint_contract,
)

from .exams.selection.graph import (
    _constraint_aware_order,
    _effective_difficulties,
    _initial_graph_from_expanded,
    _inject_missing_expanded_chapters,
    _normalized_candidate_pool,
    build_candidate_edge_graph,
    build_difficulty_defect_edges,
    supplement_priority_candidate_graph,
)

from .exams.selection.scoring import (
    _candidate_base_utility_receipt,
    _local_candidate_base_utility_receipt,
    _local_v2_score_receipt,
    _score_receipt,
)

from .exams.selection.solver import (
    _hall_witness,
    _locate_structural_graph_deficit,
    _maximum_matching,
    _minimal_hall_set,
    _repair_component,
    _solve_global_selection,
    _validate_repair_expansion,
    _verified_selection,
    _verified_witness,
    locate_candidate_graph_deficit,
    repair_global_selection,
    solve_global_selection,
)

__all__ = [
    "CANDIDATE_GRAPH_VERSION", "EFFECTIVE_DIFFICULTY_SET_VERSION", "EFFECTIVE_DIFFICULTY_VERSION", "EXACT_HISTORY_AUDIT_POLICY_VERSION", "GLOBAL_CONSTRAINT_VERSION", "RECENT_USAGE_AUDIT_POLICY_VERSION", "REPAIR_VERSION", "SELECTION_VERSION", "SOLVER_VERSION", "UTILITY_RECEIPT_VERSION", "WITNESS_VERSION",
    "FrozenCandidateEdgeGraph", "Stage5ContractError", "Stage5GlobalConstraintContract", "Stage5InfeasibleError", "Stage5RepairResult", "Stage5Selection",
    "build_candidate_edge_graph", "build_difficulty_defect_edges", "build_global_constraint_contract", "locate_candidate_graph_deficit", "repair_global_selection", "solve_global_selection", "supplement_priority_candidate_graph",
]
