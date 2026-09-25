"""Provider-free candidate-base objective contract for Stage 5/6-R2.

This module deliberately has no persistence or provider integration.  It turns
finite, locally verified factor levels into a versioned lexicographic vector.
The caller supplies every numeric mapping; no score/penalty defaults are
inferred and legacy score receipts are rejected.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

OBJECTIVE_VERSION = "candidate-base-objective-v1"
FACTOR_ORDER = ("verified_mistake_relevance", "history_exposure_novelty")
FACTOR_LEVELS = {
    "verified_mistake_relevance": ("unknown", "not_applicable", "none", "related", "strong"),
    "history_exposure_novelty": ("unknown", "not_applicable", "exposed", "novel"),
}


class CandidateBaseObjectiveError(ValueError):
    pass


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    required = {"version", "factor_order", "level_mapping", "directions", "unknown_blocked", "parameter_set_hash", "policy_hash"}
    if not isinstance(policy, Mapping) or set(policy) != required:
        raise CandidateBaseObjectiveError("objective_policy_shape_invalid")
    body = {k: policy[k] for k in required - {"policy_hash"}}
    if policy.get("policy_hash") != _digest(body):
        raise CandidateBaseObjectiveError("objective_policy_hash_mismatch")
    if policy["version"] != OBJECTIVE_VERSION or tuple(policy["factor_order"]) != FACTOR_ORDER:
        raise CandidateBaseObjectiveError("objective_policy_identity_invalid")
    if type(policy["unknown_blocked"]) is not bool or not isinstance(policy["parameter_set_hash"], str) or not policy["parameter_set_hash"]:
        raise CandidateBaseObjectiveError("objective_policy_identity_invalid")
    mappings, directions = policy["level_mapping"], policy["directions"]
    if set(mappings) != set(FACTOR_ORDER) or set(directions) != set(FACTOR_ORDER):
        raise CandidateBaseObjectiveError("objective_policy_factor_invalid")
    for factor in FACTOR_ORDER:
        if set(mappings[factor]) != set(FACTOR_LEVELS[factor]) or directions[factor] not in {"max", "min"}:
            raise CandidateBaseObjectiveError("objective_policy_factor_invalid")
        if any(type(v) is not int for v in mappings[factor].values()):
            raise CandidateBaseObjectiveError("objective_policy_mapping_invalid")
    return dict(policy)


def build_objective_policy(*, parameter_set_hash: str, level_mapping: Mapping[str, Mapping[str, int]], directions: Mapping[str, str], unknown_blocked: bool = True) -> dict[str, Any]:
    body = {"version": OBJECTIVE_VERSION, "factor_order": list(FACTOR_ORDER), "level_mapping": level_mapping, "directions": directions, "unknown_blocked": unknown_blocked, "parameter_set_hash": parameter_set_hash}
    return _policy({**body, "policy_hash": _digest(body)})


def validate_objective_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return the immutable D2 objective policy envelope."""
    return _policy(policy)


def build_objective_receipt(candidates: Sequence[Mapping[str, Any]], *, policy: Mapping[str, Any], mode: str = "free") -> dict[str, Any]:
    p = _policy(policy)
    if not isinstance(mode, str) or not mode:
        raise CandidateBaseObjectiveError("objective_mode_invalid")
    rows = []
    sums = {factor: 0 for factor in FACTOR_ORDER}
    seen_candidates: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != {"candidate_id", "candidate_fact_hash", "factors"}:
            raise CandidateBaseObjectiveError("objective_candidate_shape_invalid")
        candidate_id, fact_hash = candidate["candidate_id"], candidate["candidate_fact_hash"]
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen_candidates or not isinstance(fact_hash, str) or not fact_hash:
            raise CandidateBaseObjectiveError("objective_candidate_identity_invalid")
        seen_candidates.add(candidate_id)
        factors = candidate["factors"]
        if not isinstance(factors, Mapping) or set(factors) != set(FACTOR_ORDER):
            raise CandidateBaseObjectiveError("objective_factor_set_invalid")
        clean = {}
        for factor in FACTOR_ORDER:
            item = factors[factor]
            if not isinstance(item, Mapping) or set(item) != {"level", "evidence_codes"} or not isinstance(item["evidence_codes"], list) or not item["evidence_codes"]:
                raise CandidateBaseObjectiveError("objective_factor_shape_invalid")
            level = item["level"]
            if level not in FACTOR_LEVELS[factor] or (level == "unknown" and p["unknown_blocked"]):
                raise CandidateBaseObjectiveError("objective_factor_unknown_blocked" if level == "unknown" else "objective_factor_level_invalid")
            codes = item["evidence_codes"]
            if any(not isinstance(code, str) or not code for code in codes) or len(set(codes)) != len(codes):
                raise CandidateBaseObjectiveError("objective_evidence_invalid")
            clean[factor] = {"level": level, "evidence_codes": sorted(codes)}
            if level != "not_applicable":
                sums[factor] += p["level_mapping"][factor][level]
        rows.append({"candidate_id": candidate_id, "candidate_fact_hash": fact_hash, "factors": clean})
    vector = [sums[factor] for factor in FACTOR_ORDER]
    body = {"version": OBJECTIVE_VERSION, "mode": mode, "policy_hash": p["policy_hash"], "candidates": rows, "vector": vector, "directions": {f: p["directions"][f] for f in FACTOR_ORDER}}
    return {**body, "objective_hash": _digest(body)}


def compare_objectives(left: Mapping[str, Any], right: Mapping[str, Any], *, policy: Mapping[str, Any]) -> int:
    p = _policy(policy)
    if left.get("policy_hash") != p["policy_hash"] or right.get("policy_hash") != p["policy_hash"] or left.get("version") != OBJECTIVE_VERSION or right.get("version") != OBJECTIVE_VERSION:
        raise CandidateBaseObjectiveError("objective_identity_invalid")
    for a, b, factor in zip(left.get("vector", ()), right.get("vector", ()), FACTOR_ORDER):
        if a != b:
            return (1 if a > b else -1) if p["directions"][factor] == "max" else (1 if a < b else -1)
    return 0


def explain_objective(receipt: Mapping[str, Any], *, policy: Mapping[str, Any]) -> dict[str, Any]:
    p = _policy(policy)
    if receipt.get("objective_hash") != _digest({k: receipt[k] for k in ("version", "mode", "policy_hash", "candidates", "vector", "directions")}):
        raise CandidateBaseObjectiveError("objective_hash_mismatch")
    rebuilt = build_objective_receipt(receipt["candidates"], policy=p, mode=receipt["mode"])
    if rebuilt["objective_hash"] != receipt["objective_hash"]:
        raise CandidateBaseObjectiveError("objective_recompute_mismatch")
    return {"version": "candidate-base-objective-explanation-v1", "objective_hash": receipt["objective_hash"], "vector": list(receipt["vector"]), "factor_order": list(FACTOR_ORDER), "directions": dict(receipt["directions"]), "mode": receipt["mode"]}


__all__ = ["OBJECTIVE_VERSION", "FACTOR_ORDER", "FACTOR_LEVELS", "CandidateBaseObjectiveError", "build_objective_policy", "validate_objective_policy", "build_objective_receipt", "compare_objectives", "explain_objective"]
