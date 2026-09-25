"""Pure Stage 4 targeted difficulty resolution contracts.

Only unresolved candidates in a graph component that can reach a declared
deficit while ignoring difficulty are eligible.  Provider functions are plain
callbacks so this module cannot perform a real AI call by itself.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage4_candidate_facts import (
    FrozenCandidateFacts,
    evaluate_difficulty_fact,
)


DIFFICULTY_RESOLUTION_CONTRACT_VERSION = "past-v3-stage4-difficulty-resolution-v1"
CLASSIFICATION_RESPONSE_VERSION = "past-v3-difficulty-classification-response-v1"
VERIFICATION_RESPONSE_VERSION = "past-v3-difficulty-verification-response-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_EDGE_FIELDS = frozenset(
    {"slot_id", "candidate_id", "compatible_without_difficulty"}
)
_BINDING_FIELDS = frozenset({"model", "prompt_version", "rules_hash"})
_RESPONSE_FIELDS = frozenset({"schema_version", "round", "results"})
_RESULT_FIELDS = frozenset({"candidate_id", "difficulty", "confidence"})


class Stage4DifficultyResolutionError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = False


@dataclass(frozen=True, slots=True)
class DifficultyDefectComponent:
    candidate_facts_hash: str
    deficit_slot_ids: tuple[str, ...]
    connected_slot_ids: tuple[str, ...]
    candidate_ids: tuple[str, ...]
    component_hash: str


@dataclass(frozen=True, slots=True)
class EffectiveDifficultyOutcome:
    candidate_id: str
    status: str
    effective_difficulty: str | None
    reason: str
    classification_confidence: float | None
    verification_confidence: float | None
    evidence_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "status": self.status,
            "effective_difficulty": self.effective_difficulty,
            "reason": self.reason,
            "classification_confidence": self.classification_confidence,
            "verification_confidence": self.verification_confidence,
            "evidence_hash": self.evidence_hash,
        }


@dataclass(frozen=True, slots=True)
class DifficultyResolutionResult:
    contract_version: str
    candidate_facts_hash: str
    component_hash: str
    input_facts_hash: str
    outcomes: tuple[EffectiveDifficultyOutcome, ...]
    result_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            "difficulty_resolution_json_invalid",
            "难度解析合同无法规范化",
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", f"{field}_invalid", "难度解析合同字段无效"
        )
    return value.strip()


def locate_difficulty_defect_component(
    frozen: FrozenCandidateFacts,
    *,
    deficit_slot_ids: Sequence[str],
    edges: Sequence[Mapping[str, Any]],
) -> DifficultyDefectComponent:
    """Return only unresolved candidates connected to a real non-difficulty gap."""

    if not isinstance(frozen, FrozenCandidateFacts):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "frozen_candidate_facts_invalid", "冻结候选事实无效"
        )
    deficits = tuple(sorted({_required_text(item, "deficit_slot_id") for item in deficit_slot_ids}))
    if not deficits:
        return DifficultyDefectComponent(
            frozen.candidate_facts_hash,
            (),
            (),
            (),
            _digest(
                {
                    "candidate_facts_hash": frozen.candidate_facts_hash,
                    "deficits": [],
                    "edges": [],
                }
            ),
        )
    known = {str(item["candidate_id"]): item for item in frozen.candidates}
    if len(known) != len(frozen.candidates):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "candidate_id_not_globally_unique", "冻结候选 ID 不唯一"
        )
    slot_to_candidates: dict[str, set[str]] = {}
    candidate_to_slots: dict[str, set[str]] = {}
    normalized_edges: set[tuple[str, str]] = set()
    for edge in edges:
        if not isinstance(edge, Mapping) or set(edge) != _EDGE_FIELDS:
            raise Stage4DifficultyResolutionError(
                "candidate_fact_unresolved", "defect_edge_contract_invalid", "难度缺陷边合同无效"
            )
        compatible = edge.get("compatible_without_difficulty")
        if not isinstance(compatible, bool):
            raise Stage4DifficultyResolutionError(
                "candidate_fact_unresolved", "defect_edge_compatibility_invalid", "难度缺陷边兼容标志无效"
            )
        if not compatible:
            continue
        slot_id = _required_text(edge.get("slot_id"), "edge_slot_id")
        candidate_id = _required_text(edge.get("candidate_id"), "edge_candidate_id")
        if candidate_id not in known:
            raise Stage4DifficultyResolutionError(
                "candidate_fact_unresolved", "defect_edge_candidate_not_frozen", "难度缺陷边引用了未冻结候选"
            )
        normalized_edges.add((slot_id, candidate_id))
        slot_to_candidates.setdefault(slot_id, set()).add(candidate_id)
        candidate_to_slots.setdefault(candidate_id, set()).add(slot_id)
    visited_slots = set(deficits)
    visited_candidates: set[str] = set()
    pending_slots = list(deficits)
    while pending_slots:
        slot_id = pending_slots.pop()
        for candidate_id in slot_to_candidates.get(slot_id, ()):
            if candidate_id in visited_candidates:
                continue
            visited_candidates.add(candidate_id)
            for connected_slot in candidate_to_slots.get(candidate_id, ()):
                if connected_slot not in visited_slots:
                    visited_slots.add(connected_slot)
                    pending_slots.append(connected_slot)
    unresolved = tuple(
        sorted(
            candidate_id
            for candidate_id in visited_candidates
            if not bool(known[candidate_id]["difficulty_fact"]["resolved"])
        )
    )
    component_payload = {
        "deficit_slot_ids": list(deficits),
        "connected_slot_ids": sorted(visited_slots),
        "candidate_ids": list(unresolved),
        "edges": [list(edge) for edge in sorted(normalized_edges)],
        "candidate_facts_hash": frozen.candidate_facts_hash,
    }
    return DifficultyDefectComponent(
        frozen.candidate_facts_hash,
        deficits,
        tuple(sorted(visited_slots)),
        unresolved,
        _digest(component_payload),
    )


def _binding(value: object, *, round_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            f"{round_name}_binding_invalid",
            "难度解析模型、提示词或规则绑定不完整",
        )
    rendered = {
        "model": _required_text(value.get("model"), f"{round_name}_model"),
        "prompt_version": _required_text(
            value.get("prompt_version"), f"{round_name}_prompt_version"
        ),
        "rules_hash": _required_text(value.get("rules_hash"), f"{round_name}_rules_hash"),
    }
    if _HASH_RE.fullmatch(rendered["rules_hash"]) is None:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", f"{round_name}_rules_hash_invalid", "难度解析规则哈希无效"
        )
    return rendered


def _parse_response(
    raw: object,
    *,
    expected_ids: tuple[str, ...],
    round_name: str,
    schema_version: str,
) -> tuple[dict[str, tuple[str | None, float | None, str]], str]:
    if not isinstance(raw, Mapping) or set(raw) != _RESPONSE_FIELDS:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            f"{round_name}_response_whitelist_violation",
            "难度解析响应包含缺失或未授权字段",
        )
    if raw.get("schema_version") != schema_version or raw.get("round") != round_name:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            f"{round_name}_response_version_mismatch",
            "难度解析响应版本或轮次不一致",
        )
    results = raw.get("results")
    if not isinstance(results, list):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", f"{round_name}_results_invalid", "难度解析结果不是数组"
        )
    parsed: dict[str, tuple[str | None, float | None, str]] = {}
    for item in results:
        if not isinstance(item, Mapping) or set(item) != _RESULT_FIELDS:
            raise Stage4DifficultyResolutionError(
                "candidate_fact_unresolved",
                f"{round_name}_result_whitelist_violation",
                "难度解析单项结果包含缺失或未授权字段",
            )
        candidate_id = _required_text(item.get("candidate_id"), "response_candidate_id")
        if candidate_id in parsed:
            raise Stage4DifficultyResolutionError(
                "candidate_fact_unresolved", f"{round_name}_candidate_duplicate", "难度解析候选 ID 重复"
            )
        fact = evaluate_difficulty_fact(item.get("difficulty"), item.get("confidence"))
        parsed[candidate_id] = (fact.label, fact.confidence, fact.reason)
    if tuple(sorted(parsed)) != tuple(sorted(expected_ids)):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            f"{round_name}_candidate_coverage_mismatch",
            "难度解析响应未一一覆盖冻结候选 ID",
        )
    return parsed, _digest(raw)


def resolve_effective_difficulties(
    frozen: FrozenCandidateFacts,
    component: DifficultyDefectComponent,
    *,
    classification_binding: Mapping[str, Any],
    verification_binding: Mapping[str, Any],
    classify: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    verify: Callable[[Mapping[str, Any]], Mapping[str, Any]],
) -> DifficultyResolutionResult:
    """Run exactly two injected logical rounds and derive task-local difficulty."""

    if not isinstance(frozen, FrozenCandidateFacts) or not isinstance(
        component, DifficultyDefectComponent
    ):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "difficulty_resolution_input_invalid", "难度解析输入无效"
        )
    ids = tuple(sorted(component.candidate_ids))
    if component.candidate_facts_hash != frozen.candidate_facts_hash:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved",
            "component_candidate_facts_hash_mismatch",
            "难度缺陷分量不属于当前冻结候选事实",
        )
    known = {str(item["candidate_id"]): item for item in frozen.candidates}
    if any(candidate_id not in known for candidate_id in ids):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "component_candidate_not_frozen", "难度缺陷分量引用未冻结候选"
        )
    if any(bool(known[candidate_id]["difficulty_fact"]["resolved"]) for candidate_id in ids):
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "resolved_candidate_resolution_forbidden", "已有难度事实的候选不得定向解析"
        )
    classification = _binding(classification_binding, round_name="classification")
    verification = _binding(verification_binding, round_name="verification")
    if classification["rules_hash"] != verification["rules_hash"]:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "resolution_rules_hash_conflict", "两轮难度解析规则不一致"
        )
    if classification["prompt_version"] == verification["prompt_version"]:
        raise Stage4DifficultyResolutionError(
            "candidate_fact_unresolved", "independent_prompt_required", "独立核验必须使用不同提示词版本"
        )
    selected_facts = [known[candidate_id] for candidate_id in ids]
    selected_facts = json.loads(_canonical_json(selected_facts))
    input_facts_hash = _digest(
        {
            "candidate_facts_hash": frozen.candidate_facts_hash,
            "component_hash": component.component_hash,
            "candidates": selected_facts,
            "rules_hash": classification["rules_hash"],
        }
    )

    if not ids:
        payload = {
            "contract_version": DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
            "candidate_facts_hash": frozen.candidate_facts_hash,
            "component_hash": component.component_hash,
            "input_facts_hash": input_facts_hash,
            "classification_binding": classification,
            "verification_binding": verification,
            "classification_response_hash": None,
            "verification_response_hash": None,
            "outcomes": [],
        }
        payload_json = _canonical_json(payload)
        return DifficultyResolutionResult(
            DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
            frozen.candidate_facts_hash,
            component.component_hash,
            input_facts_hash,
            (),
            hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
            payload_json,
        )

    def request(round_name: str, binding: Mapping[str, str], schema: str) -> dict[str, Any]:
        payload = {
            "contract_version": DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
            "response_schema_version": schema,
            "round": round_name,
            "binding": dict(binding),
            "candidate_facts_hash": frozen.candidate_facts_hash,
            "component_hash": component.component_hash,
            "input_facts_hash": input_facts_hash,
            "candidates": selected_facts,
        }
        # The provider sees a detached JSON value and therefore cannot mutate
        # the frozen task facts or influence the independent second request.
        return json.loads(_canonical_json(payload))

    try:
        raw_classification = classify(
            request("classification", classification, CLASSIFICATION_RESPONSE_VERSION)
        )
    except TimeoutError:
        raw_classification = None
    try:
        raw_verification = verify(
            request("verification", verification, VERIFICATION_RESPONSE_VERSION)
        )
    except TimeoutError:
        raw_verification = None

    classification_results: dict[str, tuple[str | None, float | None, str]] = {}
    verification_results: dict[str, tuple[str | None, float | None, str]] = {}
    classification_hash: str | None = None
    verification_hash: str | None = None
    if raw_classification is not None:
        classification_results, classification_hash = _parse_response(
            raw_classification,
            expected_ids=ids,
            round_name="classification",
            schema_version=CLASSIFICATION_RESPONSE_VERSION,
        )
    if raw_verification is not None:
        verification_results, verification_hash = _parse_response(
            raw_verification,
            expected_ids=ids,
            round_name="verification",
            schema_version=VERIFICATION_RESPONSE_VERSION,
        )

    outcomes: list[EffectiveDifficultyOutcome] = []
    for candidate_id in ids:
        first = classification_results.get(candidate_id)
        second = verification_results.get(candidate_id)
        if first is None:
            status, label, reason = "excluded", None, "classification_timeout"
        elif second is None:
            status, label, reason = "excluded", None, "verification_timeout"
        elif first[2] != "difficulty_resolved":
            status, label, reason = "excluded", None, f"classification_{first[2]}"
        elif second[2] != "difficulty_resolved":
            status, label, reason = "excluded", None, f"verification_{second[2]}"
        elif first[0] != second[0]:
            status, label, reason = "conflict", None, "round_label_conflict"
        else:
            status, label, reason = "resolved", first[0], "two_round_consensus"
        evidence = {
            "candidate_id": candidate_id,
            "canonical_difficulty_fact": known[candidate_id]["difficulty_fact"],
            "input_facts_hash": input_facts_hash,
            "classification_binding": classification,
            "verification_binding": verification,
            "classification_response_hash": classification_hash,
            "verification_response_hash": verification_hash,
            "classification_result": first,
            "verification_result": second,
            "status": status,
            "effective_difficulty": label,
            "reason": reason,
        }
        outcomes.append(
            EffectiveDifficultyOutcome(
                candidate_id,
                status,
                label,
                reason,
                first[1] if first is not None else None,
                second[1] if second is not None else None,
                _digest(evidence),
            )
        )
    payload = {
        "contract_version": DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
        "candidate_facts_hash": frozen.candidate_facts_hash,
        "component_hash": component.component_hash,
        "input_facts_hash": input_facts_hash,
        "classification_binding": classification,
        "verification_binding": verification,
        "classification_response_hash": classification_hash,
        "verification_response_hash": verification_hash,
        "outcomes": [item.to_dict() for item in outcomes],
    }
    payload_json = _canonical_json(payload)
    return DifficultyResolutionResult(
        DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
        frozen.candidate_facts_hash,
        component.component_hash,
        input_facts_hash,
        tuple(outcomes),
        hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        payload_json,
    )


__all__ = [
    "CLASSIFICATION_RESPONSE_VERSION",
    "DIFFICULTY_RESOLUTION_CONTRACT_VERSION",
    "DifficultyDefectComponent",
    "DifficultyResolutionResult",
    "EffectiveDifficultyOutcome",
    "Stage4DifficultyResolutionError",
    "VERIFICATION_RESPONSE_VERSION",
    "locate_difficulty_defect_component",
    "resolve_effective_difficulties",
]
