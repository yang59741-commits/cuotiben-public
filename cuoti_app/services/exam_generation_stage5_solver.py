"""Pure Stage 5 global constraints, candidate graph and exact local solver.

This module intentionally has no database, worker, provider, manifest or legacy
selection dependencies.  Its inputs are immutable mappings plus their explicit
hash identities; callers are responsible for persistence in later stages.
"""

from __future__ import annotations

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


GLOBAL_CONSTRAINT_VERSION = "past-v3-stage5-global-constraints-v1"
CANDIDATE_GRAPH_VERSION = "past-v3-stage5-candidate-edge-graph-v1"
SOLVER_VERSION = "past-v3-stage5-exact-bnb-v1"
SELECTION_VERSION = "past-v3-stage5-selection-v1"
WITNESS_VERSION = "past-v3-stage5-infeasible-witness-v1"
REPAIR_VERSION = "past-v3-stage5-minimal-repair-v1"
STAGE3_CONTRACT_VERSION = "past-v3-generation-contract-v1"
STAGE4_CONTRACT_VERSION = "past-v3-stage4-candidate-facts-v1"
EFFECTIVE_DIFFICULTY_VERSION = "past-v3-stage4-difficulty-resolution-v1"
EFFECTIVE_DIFFICULTY_SET_VERSION = "past-v3-stage5-effective-difficulty-set-v1"
RECENT_USAGE_AUDIT_POLICY_VERSION = "past-v3-stage5-recent-usage-audit-only-v2"
EXACT_HISTORY_AUDIT_POLICY_VERSION = "past-v3-stage5-exact-history-audit-v3"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_DIFFICULTIES = frozenset({"简单", "中等", "困难"})
# Canonical runtime namespace. ``past_exam`` remains an input-only alias for
# historical payloads and is normalized before any graph identity is signed.
_SOURCES = frozenset({"ordinary", "past_exam_other"})
_COVERAGE_FIELDS = frozenset({"source_kind", "chapter", "question_type_family", "section_id"})
_MAX_LOCAL_SCORE = 100
_MAX_LOCAL_PENALTY = 100
STAGE6_SCORE_RECEIPT_VERSION = "local-score-v2"
STAGE6_SCORING_IDENTITY_VERSION = "past-v3-stage6-scoring-identity-v2"
STAGE6_RESPONSE_SCHEMA_VERSION = "past-v3-stage6-score-response-v2"


class Stage5ContractError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = False


class Stage5InfeasibleError(Stage5ContractError):
    def __init__(self, witness: Mapping[str, Any]) -> None:
        super().__init__("exam_selection_infeasible", str(witness["kind"]), "冻结候选无法满足全卷硬约束", status_code=422)
        self.witness = dict(witness)


class Stage5SearchIncompleteError(Stage5ContractError):
    def __init__(self) -> None:
        super().__init__("exam_selection_incomplete", "feasibility_search_incomplete", "候选可行性搜索尚未完成")


@dataclass(frozen=True, slots=True)
class Stage5GlobalConstraintContract:
    contract_version: str
    stage3_contract_hash: str
    candidate_facts_hash: str
    projection_identity_hash: str
    question_count: int
    constraint_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True, slots=True)
class FrozenCandidateEdgeGraph:
    contract_version: str
    constraint_hash: str
    candidate_facts_hash: str
    graph_mode: str
    edge_count: int
    graph_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True, slots=True)
class Stage5Selection:
    contract_version: str
    graph_hash: str
    constraint_hash: str
    score_identity_hash: str
    total_utility: int
    selection_hash: str
    assignments: tuple[Mapping[str, Any], ...]
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True, slots=True)
class Stage5RepairResult:
    """Atomic pure result: a complete replacement plus its bound receipt."""

    replacement_selection: Stage5Selection
    repair_hash: str
    repair_scope_hash: str
    fixed_assignment_hash: str
    affected_slot_ids: tuple[str, ...]
    _receipt_json: str

    @property
    def selection(self) -> Stage5Selection:
        return self.replacement_selection

    @property
    def repair_receipt(self) -> dict[str, Any]:
        return json.loads(self._receipt_json)

    @property
    def receipt(self) -> dict[str, Any]:
        return self.repair_receipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "replacement_selection": self.replacement_selection.to_dict(),
            "repair_receipt": self.repair_receipt,
        }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Stage5ContractError("exam_stage5_contract_invalid", "non_canonical_json", "阶段 5 合同不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "阶段 5 依赖哈希无效")
    return value


def _text(value: object, field: str, maximum: int = 640) -> str:
    if not isinstance(value, str):
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "阶段 5 合同字段无效")
    result = unicodedata.normalize("NFC", value).strip()
    if not result or len(result) > maximum:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "阶段 5 合同字段无效")
    return result


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "阶段 5 合同对象无效")
    return value


def _exact_fields(value: Mapping[str, Any], expected: set[str], field: str) -> None:
    try:
        actual = set(value)
    except (TypeError, ValueError) as exc:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_shape_invalid", "阶段 5 合同字段集合无效") from exc
    if actual != expected:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_shape_invalid", "阶段 5 合同字段集合无效")


def _text_list(value: object, field: str, *, hashes: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "阶段 5 合同列表无效")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        normalized = _hash(item, field) if hashes else _text(item, field)
        if normalized in seen:
            raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_duplicate", "阶段 5 合同列表存在重复值")
        seen.add(normalized)
        result.append(normalized)
    return result


def _recent_usage_effect(
    recent: Mapping[str, Any],
    candidates: object,
) -> tuple[set[str], set[str]]:
    """Validate durable v1 and new audit-only v2 recent-usage facts."""

    payload = _mapping(recent.get("payload"), "recent_usage_payload")
    if set(payload) == {"excluded_candidate_ids", "excluded_content_hashes"}:
        return (
            set(_text_list(payload["excluded_candidate_ids"], "excluded_candidate_ids")),
            set(_text_list(payload["excluded_content_hashes"], "excluded_content_hashes", hashes=True)),
        )
    v2_expected = {
        "mode", "snapshot_identity", "snapshot_hash",
        "observed_candidate_ids", "observed_content_hashes",
        "candidate_id_hits", "content_hash_hits",
    }
    version = _text(recent.get("version"), "recent_usage_version", 128)
    snapshot_hash = _hash(payload.get("snapshot_hash"), "recent_usage_snapshot_hash")
    if not isinstance(candidates, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_rows_invalid", "冻结候选集合无效")
    stage4_ids: set[str] = set()
    stage4_hashes: set[str] = set()
    for raw in candidates:
        raw = _mapping(raw, "candidate")
        stage4_ids.add(_text(raw.get("candidate_id"), "candidate_id"))
        stage4_hashes.add(_hash(raw.get("content_hash"), "content_hash"))
    if set(payload) == v2_expected:
        if (
            payload.get("mode") != "audit_only"
            or version != f"{RECENT_USAGE_AUDIT_POLICY_VERSION}:sha256-{snapshot_hash}"
        ):
            raise Stage5ContractError(
                "exam_stage5_contract_invalid", "recent_usage_audit_policy_invalid",
                "最近使用事实不是受支持的只审计政策",
            )
        _text(payload.get("snapshot_identity"), "recent_usage_snapshot_identity", 512)
        observed_ids = _text_list(payload["observed_candidate_ids"], "observed_candidate_ids")
        observed_hashes = _text_list(payload["observed_content_hashes"], "observed_content_hashes", hashes=True)
        candidate_hits = _text_list(payload["candidate_id_hits"], "candidate_id_hits")
        content_hits = _text_list(payload["content_hash_hits"], "content_hash_hits", hashes=True)
        if _builtin_any(values != sorted(values) for values in (observed_ids, observed_hashes, candidate_hits, content_hits)):
            raise Stage5ContractError(
                "exam_stage5_contract_invalid", "recent_usage_audit_order_invalid",
                "最近使用审计集合必须排序",
            )
        if (
            set(candidate_hits) != set(observed_ids).intersection(stage4_ids)
            or set(content_hits) != set(observed_hashes).intersection(stage4_hashes)
        ):
            raise Stage5ContractError(
                "exam_stage5_contract_hash_mismatch", "recent_usage_audit_hits_mismatch",
                "最近使用审计命中证据未绑定阶段 4 候选",
            )
    else:
        v3_expected = {
            "mode", "scope", "snapshot_identity", "snapshot_hash",
            "ordered_usage_count", "content_hash_hits",
        }
        _exact_fields(payload, v3_expected, "recent_usage_exact_history_payload")
        if (
            payload.get("mode") != "audit_only"
            or version != f"{EXACT_HISTORY_AUDIT_POLICY_VERSION}:sha256-{snapshot_hash}"
        ):
            raise Stage5ContractError(
                "exam_stage5_contract_invalid", "recent_usage_audit_policy_invalid",
                "精确历史使用事实版本无效",
            )
        _text(payload.get("snapshot_identity"), "recent_usage_snapshot_identity", 512)
        scope = _mapping(payload.get("scope"), "recent_usage_scope")
        _exact_fields(scope, {"generation_mode", "subject_id", "window_limit"}, "recent_usage_scope")
        if (
            scope.get("generation_mode") != "past"
            or type(scope.get("subject_id")) is not int
            or scope["subject_id"] <= 0
            or type(scope.get("window_limit")) is not int
            or not 1 <= scope["window_limit"] <= 256
            or type(payload.get("ordered_usage_count")) is not int
            or not 0 <= payload["ordered_usage_count"] <= scope["window_limit"]
            or not isinstance(payload.get("content_hash_hits"), list)
        ):
            raise Stage5ContractError(
                "exam_stage5_contract_invalid", "recent_usage_scope_invalid",
                "精确历史使用作用域无效",
            )
        hit_hashes: list[str] = []
        all_usage_hashes: set[str] = set()
        for raw in payload["content_hash_hits"]:
            hit = _mapping(raw, "recent_usage_content_hit")
            _exact_fields(
                hit,
                {"content_hash", "success_count", "latest_order_rank", "latest_used_at", "usage_hashes"},
                "recent_usage_content_hit",
            )
            content_hash = _hash(hit.get("content_hash"), "recent_usage_content_hash")
            usage_hashes = _text_list(hit.get("usage_hashes"), "recent_usage_hashes", hashes=True)
            if (
                content_hash not in stage4_hashes
                or type(hit.get("success_count")) is not int
                or hit["success_count"] < 1
                or hit["success_count"] != len(usage_hashes)
                or type(hit.get("latest_order_rank")) is not int
                or not 1 <= hit["latest_order_rank"] <= payload["ordered_usage_count"]
                or not _text(hit.get("latest_used_at"), "recent_usage_latest_used_at", 128)
                or _builtin_any(value in all_usage_hashes for value in usage_hashes)
            ):
                raise Stage5ContractError(
                    "exam_stage5_contract_hash_mismatch", "recent_usage_audit_hits_mismatch",
                    "精确历史使用命中未绑定阶段 4 候选",
                )
            all_usage_hashes.update(usage_hashes)
            hit_hashes.append(content_hash)
        if hit_hashes != sorted(set(hit_hashes)) or len(all_usage_hashes) > payload["ordered_usage_count"]:
            raise Stage5ContractError(
                "exam_stage5_contract_invalid", "recent_usage_audit_order_invalid",
                "精确历史使用命中集合无效",
            )
    # Cross-task history is evidence only.  Same-job graph uniqueness remains
    # independently enforced by the Stage 5 uniqueness policy and solver.
    return set(), set()


def _verified_contract_payload(
    contract: Stage5GlobalConstraintContract,
) -> dict[str, Any]:
    if not isinstance(contract, Stage5GlobalConstraintContract):
        raise Stage5ContractError("exam_stage5_contract_invalid", "global_contract_type_invalid", "全局约束合同类型无效")
    payload = contract.to_dict()
    if (
        not isinstance(payload, dict)
        or payload.get("contract_version") != GLOBAL_CONSTRAINT_VERSION
        or contract.contract_version != GLOBAL_CONSTRAINT_VERSION
        or payload.get("stage3_contract_hash") != contract.stage3_contract_hash
        or payload.get("candidate_facts_hash") != contract.candidate_facts_hash
        or payload.get("projection_identity_hash") != contract.projection_identity_hash
        or payload.get("question_count") != contract.question_count
        or _digest(payload) != contract.constraint_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "global_contract_hash_mismatch", "全局约束合同身份或哈希不匹配")
    return payload


def _verified_graph_payload(
    graph: FrozenCandidateEdgeGraph,
    *,
    contract: Stage5GlobalConstraintContract | None = None,
) -> dict[str, Any]:
    if not isinstance(graph, FrozenCandidateEdgeGraph):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_graph_type_invalid", "候选图合同类型无效")
    payload = graph.to_dict()
    if (
        not isinstance(payload, dict)
        or payload.get("contract_version") != CANDIDATE_GRAPH_VERSION
        or graph.contract_version != CANDIDATE_GRAPH_VERSION
        or payload.get("constraint_hash") != graph.constraint_hash
        or payload.get("candidate_facts_hash") != graph.candidate_facts_hash
        or payload.get("graph_mode") != graph.graph_mode
        or not isinstance(payload.get("edges"), list)
        or len(payload["edges"]) != graph.edge_count
        or _digest(payload) != graph.graph_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_graph_hash_mismatch", "候选图身份或哈希不匹配")
    if contract is not None and (
        graph.constraint_hash != contract.constraint_hash
        or graph.candidate_facts_hash != contract.candidate_facts_hash
        or payload.get("projection_identity_hash") != contract.projection_identity_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "graph_constraint_hash_mismatch", "候选图与全局约束不匹配")
    return payload


def _source(value: object) -> str:
    source = _text(value, "source_kind", 64)
    if source in {"past_exam", "past_exam_local"}:
        source = "past_exam_other"
    if source not in _SOURCES:
        raise Stage5ContractError("exam_stage5_contract_invalid", "source_kind_unsupported", "候选来源类型不受支持")
    return source


def _fraction(value: object, field: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "章节权重必须是显式正数")
    try:
        result = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "章节权重无效") from exc
    if result <= 0:
        raise Stage5ContractError("exam_stage5_contract_invalid", f"{field}_invalid", "章节权重必须为正")
    return result


def _largest_remainder(weights: Mapping[str, object], total: int) -> dict[str, int]:
    normalized: list[tuple[str, Fraction]] = []
    seen: set[str] = set()
    for key, value in weights.items():
        chapter = _text(key, "chapter")
        if chapter in seen:
            raise Stage5ContractError("exam_stage5_contract_invalid", "chapter_weights_duplicate", "规范化后的章节权重重复")
        seen.add(chapter)
        normalized.append((chapter, _fraction(value, "chapter_weight")))
    normalized.sort(key=lambda item: item[0])
    if not normalized and total:
        raise Stage5ContractError("exam_stage5_contract_invalid", "chapter_weights_missing", "普通题章节配额缺少权威权重")
    denominator = sum((weight for _, weight in normalized), Fraction(0))
    raw = [(name, Fraction(total) * weight / denominator) for name, weight in normalized]
    result = {name: math.floor(value) for name, value in raw}
    remaining = total - sum(result.values())
    order = sorted(raw, key=lambda item: (-(item[1] - math.floor(item[1])), item[0]))
    for name, _ in order[:remaining]:
        result[name] += 1
    return result


def build_global_constraint_contract(
    *,
    stage3: Mapping[str, Any],
    stage3_contract_hash: str,
    stage4: Mapping[str, Any],
    candidate_facts_hash: str,
    projection_identity_hash: str,
    chapter_weight_fact: Mapping[str, Any],
    recent_usage: Mapping[str, Any],
    diversity_policy: Mapping[str, Any],
    coverage_policy: Mapping[str, Any],
    uniqueness_policy: Mapping[str, Any],
    policy_fact_set_hash: str,
) -> Stage5GlobalConstraintContract:
    """Freeze all global constraints without consulting legacy plans/checkpoints."""

    stage3 = _mapping(stage3, "stage3")
    stage4 = _mapping(stage4, "stage4")
    if stage3.get("contract_version") != STAGE3_CONTRACT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "stage3_contract_version_unsupported", "阶段 3 合同版本不受支持")
    if stage4.get("contract_version") != STAGE4_CONTRACT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "stage4_contract_version_unsupported", "阶段 4 候选事实版本不受支持")
    _hash(stage3_contract_hash, "stage3_contract_hash")
    _hash(candidate_facts_hash, "candidate_facts_hash")
    _hash(projection_identity_hash, "projection_identity_hash")
    if _digest(stage3) != stage3_contract_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "stage3_contract_hash_mismatch", "阶段 3 合同哈希不匹配")
    if _digest(stage4) != candidate_facts_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    if stage4.get("projection_identity_hash") != projection_identity_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "projection_identity_hash_mismatch", "语义投影身份不匹配")
    policy_fact_set_hash = _hash(policy_fact_set_hash, "policy_fact_set_hash")
    source_identity = _mapping(stage3.get("source_identity"), "source_identity")
    _exact_fields(
        source_identity,
        {"catalog_version", "course_version", "source_snapshot", "source_exam_id"},
        "source_identity",
    )
    frozen_source_identity = {
        "catalog_version": _text(source_identity.get("catalog_version"), "catalog_version", 128),
        "course_version": _text(source_identity.get("course_version"), "course_version", 128),
        "source_snapshot": _hash(source_identity.get("source_snapshot"), "source_snapshot"),
        "source_exam_id": _text(source_identity.get("source_exam_id"), "source_exam_id", 512),
    }
    slots = stage3.get("slot_demands")
    if not isinstance(slots, list) or not slots:
        raise Stage5ContractError("exam_stage5_contract_invalid", "slot_demands_invalid", "阶段 3 槽位合同无效")
    if stage3.get("question_count") != len(slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "question_count_mismatch", "槽位数量不一致")
    normalized_slots: list[dict[str, Any]] = []
    for position, raw in enumerate(slots, 1):
        raw = _mapping(raw, "slot")
        if raw.get("position") != position or raw.get("target_difficulty") not in _DIFFICULTIES:
            raise Stage5ContractError("exam_stage5_contract_invalid", "slot_identity_invalid", "槽位位置或难度无效")
        normalized_slots.append({
            "slot_id": f"slot-{position:04d}", "position": position,
            "section_id": _text(raw.get("section_id"), "section_id"),
            "question_type_family": _text(raw.get("question_type_family"), "question_type_family"),
            "target_difficulty": raw["target_difficulty"],
        })
    source_raw = _mapping(stage3.get("source_quotas"), "source_quotas")
    source_quotas = {"ordinary": 0, "past_exam_other": 0}
    for key, value in source_raw.items():
        source = _source(key)
        if type(value) is not int or value < 0:
            raise Stage5ContractError("exam_stage5_contract_invalid", "source_quota_invalid", "来源配额无效")
        source_quotas[source] += value
    if sum(source_quotas.values()) != len(slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "source_quota_total_mismatch", "来源配额总数与槽位不一致")
    weight_fact = _mapping(chapter_weight_fact, "chapter_weight_fact")
    _exact_fields(weight_fact, {"version", "subject", "weights", "fact_hash"}, "chapter_weight_fact")
    _text(weight_fact.get("version"), "chapter_weight_fact_version", 128)
    fact_body = {key: weight_fact[key] for key in ("version", "subject", "weights")}
    if _digest(fact_body) != _hash(weight_fact.get("fact_hash"), "chapter_weight_fact_hash"):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "chapter_weight_fact_hash_mismatch", "章节权重事实哈希不匹配")
    combination = _mapping(stage3.get("combination"), "combination")
    if _text(weight_fact.get("subject"), "chapter_weight_subject") != _text(combination.get("subject"), "subject"):
        raise Stage5ContractError("exam_stage5_contract_invalid", "chapter_weight_subject_mismatch", "章节权重事实学科不匹配")
    weights = _mapping(weight_fact.get("weights"), "chapter_weights")
    chapter_quotas = _largest_remainder(weights, source_quotas["ordinary"])
    recent = _mapping(recent_usage, "recent_usage")
    diversity = _mapping(diversity_policy, "diversity_policy")
    coverage = _mapping(coverage_policy, "coverage_policy")
    uniqueness = _mapping(uniqueness_policy, "uniqueness_policy")
    for identity, value in (("recent_usage", recent), ("diversity_policy", diversity), ("coverage_policy", coverage), ("uniqueness_policy", uniqueness)):
        _exact_fields(value, {"version", "payload", "hash"}, identity)
        _text(value.get("version"), f"{identity}_version", 128)
        if _digest({"version": value["version"], "payload": value["payload"]}) != _hash(value.get("hash"), f"{identity}_hash"):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", f"{identity}_hash_mismatch", "冻结全局约束事实哈希不匹配")
    _recent_usage_effect(recent, stage4.get("candidates"))
    diversity_payload = _mapping(diversity.get("payload"), "diversity_policy_payload")
    _exact_fields(diversity_payload, {"max_per_source_exam"}, "diversity_policy_payload")
    max_per_source_exam = diversity_payload["max_per_source_exam"]
    if type(max_per_source_exam) is not int or not 1 <= max_per_source_exam <= len(slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "diversity_max_invalid", "多样性上限无效")
    coverage_payload = _mapping(coverage.get("payload"), "coverage_policy_payload")
    _exact_fields(coverage_payload, {"minima"}, "coverage_policy_payload")
    minima = coverage_payload["minima"]
    if not isinstance(minima, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minima_invalid", "覆盖最低约束无效")
    coverage_ids: set[str] = set()
    for item in minima:
        item = _mapping(item, "coverage_minimum")
        _exact_fields(item, {"id", "field", "value", "min_count"}, "coverage_minimum")
        coverage_id = _text(item["id"], "coverage_id")
        if coverage_id in coverage_ids:
            raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_id_duplicate", "覆盖约束 ID 重复")
        coverage_ids.add(coverage_id)
        if item["field"] not in _COVERAGE_FIELDS or type(item["min_count"]) is not int or not 0 <= item["min_count"] <= len(slots):
            raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minimum_invalid", "覆盖最低约束字段无效")
        coverage_value = _text(item["value"], "coverage_value")
        if item["field"] == "source_kind" and coverage_value not in _SOURCES:
            raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_source_invalid", "覆盖来源值无效")
    uniqueness_payload = _mapping(uniqueness.get("payload"), "uniqueness_policy_payload")
    _exact_fields(uniqueness_payload, {"resource_keys"}, "uniqueness_policy_payload")
    if uniqueness_payload.get("resource_keys") != ["candidate_id", "content_hash"]:
        raise Stage5ContractError("exam_stage5_contract_invalid", "uniqueness_policy_unsupported", "阶段 5 只支持候选与内容指纹双重唯一")
    payload = {
        "contract_version": GLOBAL_CONSTRAINT_VERSION,
        "stage3_contract_hash": stage3_contract_hash,
        "candidate_facts_hash": candidate_facts_hash,
        "projection_identity_hash": projection_identity_hash,
        "profile_hash": _hash(stage3.get("profile_hash"), "profile_hash"),
        "exact_slot_hash": _hash(stage3.get("exact_slot_hash"), "exact_slot_hash"),
        "question_count": len(slots), "subject": combination["subject"],
        "source_identity": frozen_source_identity,
        "policy_fact_set_hash": policy_fact_set_hash,
        "target_institution": _text(_mapping(stage3.get("target_institution_exclusion"), "target_institution_exclusion").get("institution"), "target_institution"),
        "slots": normalized_slots, "source_quotas": source_quotas,
        "chapter_weight_fact": fact_body, "chapter_weight_fact_hash": weight_fact["fact_hash"],
        "ordinary_chapter_quotas": chapter_quotas,
        "recent_usage": recent, "diversity_policy": diversity, "coverage_policy": coverage,
        "uniqueness_policy": uniqueness,
    }
    raw = _canonical(payload)
    return Stage5GlobalConstraintContract(GLOBAL_CONSTRAINT_VERSION, stage3_contract_hash, candidate_facts_hash, projection_identity_hash, len(slots), hashlib.sha256(raw.encode()).hexdigest(), raw)


def _effective_difficulties(
    value: Mapping[str, Any] | None,
    *,
    candidate_facts_hash: str,
) -> tuple[dict[str, str], str]:
    """Verify a deterministic set of complete Stage-4 resolution results.

    Stage 5 must not accept an outcomes-only summary: that would discard the
    Stage-4 model, prompt, rules and input-fact bindings.  The set wrapper is
    deliberately small, while each member is the exact immutable v42 result
    payload plus its real result hash.
    """

    candidate_facts_hash = _hash(candidate_facts_hash, "candidate_facts_hash")
    if value is None:
        empty = {
            "contract_version": EFFECTIVE_DIFFICULTY_SET_VERSION,
            "candidate_facts_hash": candidate_facts_hash,
            "results": [],
        }
        return {}, _digest(empty)
    value = _mapping(value, "effective_difficulty_receipt")
    if set(value) != {
        "contract_version", "candidate_facts_hash", "results", "receipt_hash"
    }:
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_receipt_shape_invalid", "有效难度回执字段无效")
    if value.get("contract_version") != EFFECTIVE_DIFFICULTY_SET_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_version_unsupported", "有效难度回执版本不受支持")
    if value.get("candidate_facts_hash") != candidate_facts_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_candidate_facts_mismatch", "有效难度回执未绑定当前候选事实")
    results = value.get("results")
    if not isinstance(results, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_results_invalid", "有效难度结果集合无效")
    receipt_body = {
        "contract_version": value["contract_version"],
        "candidate_facts_hash": candidate_facts_hash,
        "results": results,
    }
    receipt_hash = _hash(value.get("receipt_hash"), "effective_difficulty_receipt_hash")
    if _digest(receipt_body) != receipt_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_hash_mismatch", "有效难度回执哈希不匹配")
    result: dict[str, str] = {}
    seen_candidates: set[str] = set()
    seen_results: set[str] = set()
    ordered_result_hashes: list[str] = []
    payload_fields = {
        "contract_version", "candidate_facts_hash", "component_hash",
        "input_facts_hash", "classification_binding", "verification_binding",
        "classification_response_hash", "verification_response_hash", "outcomes",
    }
    outcome_fields = {
        "candidate_id", "status", "effective_difficulty", "reason",
        "classification_confidence", "verification_confidence", "evidence_hash",
    }
    for member in results:
        member = _mapping(member, "effective_difficulty_result")
        if set(member) != {"result_hash", "payload"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_shape_invalid", "有效难度结果引用字段无效")
        result_hash = _hash(member.get("result_hash"), "effective_difficulty_result_hash")
        if result_hash in seen_results:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_duplicate", "有效难度结果重复")
        seen_results.add(result_hash)
        ordered_result_hashes.append(result_hash)
        payload = _mapping(member.get("payload"), "effective_difficulty_result_payload")
        if set(payload) != payload_fields:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_result_payload_shape_invalid", "有效难度结果载荷字段无效")
        if (
            payload.get("contract_version") != EFFECTIVE_DIFFICULTY_VERSION
            or payload.get("candidate_facts_hash") != candidate_facts_hash
            or _digest(payload) != result_hash
        ):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "effective_difficulty_result_binding_mismatch", "有效难度结果未绑定当前候选事实或结果哈希")
        _hash(payload.get("component_hash"), "effective_difficulty_component_hash")
        _hash(payload.get("input_facts_hash"), "effective_difficulty_input_facts_hash")
        bindings: list[Mapping[str, Any]] = []
        for round_name in ("classification", "verification"):
            binding = _mapping(payload.get(f"{round_name}_binding"), f"{round_name}_binding")
            if set(binding) != {"model", "prompt_version", "rules_hash"}:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_binding_shape_invalid", "有效难度轮次绑定字段无效")
            _text(binding.get("model"), f"{round_name}_model", 256)
            _text(binding.get("prompt_version"), f"{round_name}_prompt_version", 128)
            _hash(binding.get("rules_hash"), f"{round_name}_rules_hash")
            bindings.append(binding)
        if (
            bindings[0]["rules_hash"] != bindings[1]["rules_hash"]
            or bindings[0]["prompt_version"] == bindings[1]["prompt_version"]
        ):
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_round_binding_invalid", "有效难度两轮绑定无效")
        for field in ("classification_response_hash", "verification_response_hash"):
            response_hash = payload.get(field)
            if response_hash is not None:
                _hash(response_hash, field)
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcomes_invalid", "有效难度回执无效")
        for raw in outcomes:
            raw = _mapping(raw, "effective_difficulty_outcome")
            if set(raw) != outcome_fields:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcome_shape_invalid", "有效难度条目字段无效")
            candidate = _text(raw.get("candidate_id"), "candidate_id")
            if candidate in seen_candidates:
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_duplicate", "有效难度候选重复")
            seen_candidates.add(candidate)
            label = raw.get("effective_difficulty")
            status = raw.get("status")
            confidences = (raw.get("classification_confidence"), raw.get("verification_confidence"))
            if (
                status not in {"resolved", "excluded", "conflict"}
                or (status == "resolved" and label not in _DIFFICULTIES)
                or (status != "resolved" and label is not None)
                or _builtin_any(confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1) for confidence in confidences)
            ):
                raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_outcome_invalid", "有效难度条目无效")
            _text(raw.get("reason"), "effective_difficulty_reason", 128)
            _hash(raw.get("evidence_hash"), "effective_difficulty_evidence_hash")
            if status == "resolved":
                result[candidate] = label
    if ordered_result_hashes != sorted(ordered_result_hashes):
        raise Stage5ContractError("exam_stage5_contract_invalid", "effective_difficulty_results_not_canonical", "有效难度结果集合顺序不规范")
    return result, receipt_hash


def _constraint_aware_order(items: Sequence[dict[str, Any]], contract_payload: Mapping[str, Any], *, limit: int | None = None) -> list[dict[str, Any]]:
    """Return a stable, score-free order whose prefixes track frozen hard demand."""

    source_targets = contract_payload["source_quotas"]
    chapter_targets = contract_payload["ordinary_chapter_quotas"]
    coverage_items = contract_payload["coverage_policy"]["payload"]["minima"]
    question_count = contract_payload["question_count"]
    remaining = list(items)
    ordered: list[dict[str, Any]] = []
    source_counts = {key: 0 for key in source_targets}
    chapter_counts = {key: 0 for key in chapter_targets}
    coverage_counts = {item["id"]: 0 for item in coverage_items}
    exam_counts: dict[str, int] = {}
    content_counts: dict[str, int] = {}
    denominator = math.lcm(*(max(1, target) for target in (
        *source_targets.values(), *chapter_targets.values(),
        *(entry['min_count'] for entry in coverage_items),
    )))

    # A common positive scale preserves exact rational ordering without
    # allocating Fraction objects for every candidate at every prefix step.
    def pressure(target: int, count: int, step: int) -> int:
        return (target * step - count * question_count) * (denominator // max(1, target))

    while remaining and (limit is None or len(ordered) < limit):
        step = len(ordered) + 1

        def priority(item: Mapping[str, Any]) -> tuple[int, int, int, str, str]:
            source = item["source_kind"]
            source_pressure = pressure(source_targets[source], source_counts[source], step)
            if source == "ordinary":
                chapter = item["chapter"]
                subgroup_pressure = pressure(chapter_targets[chapter], chapter_counts[chapter], step)
            else:
                subgroup_pressure = source_pressure
            coverage_pressure = sum(
                (pressure(entry["min_count"], coverage_counts[entry["id"]], step)
                 for entry in coverage_items
                 if entry["min_count"] and item[entry["field"]] == entry["value"]),
                0,
            )
            exam_count = exam_counts.get(item["source_exam_id"], 0) if item["source_exam_id"] is not None else 0
            return (
                source_pressure + subgroup_pressure + coverage_pressure,
                -content_counts.get(item["content_hash"], 0), -exam_count,
                item["candidate_id"], item["candidate_fact_hash"],
            )

        # Maximize demand pressure, but keep identifiers ascending on an exact tie.
        def order_key(item):
            rank = priority(item)
            return (-rank[0], -rank[1], -rank[2], rank[3], rank[4])

        chosen = min(remaining, key=order_key)
        remaining.remove(chosen)
        ordered.append(chosen)
        source_counts[chosen["source_kind"]] += 1
        if chosen["source_kind"] == "ordinary":
            chapter_counts[chosen["chapter"]] += 1
        if chosen["source_exam_id"] is not None:
            exam_counts[chosen["source_exam_id"]] = exam_counts.get(chosen["source_exam_id"], 0) + 1
        content_counts[chosen["content_hash"]] = content_counts.get(chosen["content_hash"], 0) + 1
        for entry in coverage_items:
            if chosen[entry["field"]] == entry["value"]:
                coverage_counts[entry["id"]] += 1
    return ordered


def _normalized_candidate_pool(
    contract_payload: Mapping[str, Any],
    stage4: Mapping[str, Any],
    effective: Mapping[str, str],
    *,
    embedded_resolved_only: bool = False,
) -> list[dict[str, Any]]:
    """Apply the shared Stage-5 hard candidate boundaries before windowing."""

    candidates_raw = stage4.get("candidates")
    if not isinstance(candidates_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_rows_invalid", "冻结候选集合无效")
    excluded_ids, excluded_hashes = _recent_usage_effect(
        _mapping(contract_payload["recent_usage"], "recent_usage"), candidates_raw
    )
    known_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in candidates_raw:
        raw = _mapping(raw, "candidate")
        candidate_id = _text(raw.get("candidate_id"), "candidate_id")
        if candidate_id in known_ids:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_id_not_unique", "候选 ID 不唯一")
        known_ids.add(candidate_id)
        content_hash = _hash(raw.get("content_hash"), "content_hash")
        if candidate_id in excluded_ids or content_hash in excluded_hashes:
            continue
        source = _source(raw.get("source_kind"))
        if _text(raw.get("subject"), "candidate_subject") != contract_payload["subject"]:
            continue
        if source == "ordinary":
            chapter = _text(raw.get("chapter"), "chapter")
            if _builtin_any(raw.get(field) not in (None, "") for field in ("source_exam_id", "section_id", "source_institution")):
                raise Stage5ContractError("exam_stage5_contract_invalid", "ordinary_provenance_conflict", "普通题不得携带真题来源字段")
            source_exam_id = section_id = source_institution = None
        else:
            if raw.get("chapter") not in (None, ""):
                raise Stage5ContractError("exam_stage5_contract_invalid", "past_exam_chapter_forbidden", "外校真题不得伪造教材章节")
            chapter = None
            source_exam_id = _text(raw.get("source_exam_id"), "source_exam_id")
            section_id = _text(raw.get("section_id"), "candidate_section_id")
            source_institution = _text(raw.get("source_institution"), "source_institution")
            if source_institution.casefold() == contract_payload["target_institution"].casefold():
                raise Stage5ContractError("exam_stage5_contract_invalid", "target_institution_candidate_forbidden", "候选来自目标院校")
        fact = _mapping(raw.get("difficulty_fact"), "difficulty_fact")
        embedded_resolved = fact.get("resolved") is True and fact.get("label") in _DIFFICULTIES
        if embedded_resolved_only and not embedded_resolved:
            continue
        difficulty = fact.get("label") if embedded_resolved else effective.get(candidate_id)
        normalized.append({
            "candidate_id": candidate_id, "source_kind": source, "chapter": chapter,
            "source_exam_id": source_exam_id, "section_id": section_id,
            "source_institution": source_institution,
            "question_type_family": _text(raw.get("question_type_family"), "question_type_family"),
            "difficulty": difficulty, "content_hash": content_hash,
            "candidate_fact_hash": _digest(raw),
        })
    normalized = [
        item for item in normalized
        if contract_payload["source_quotas"][item["source_kind"]] > 0
        and (
            item["source_kind"] != "ordinary"
            or contract_payload["ordinary_chapter_quotas"].get(item["chapter"], 0) > 0
        )
    ]
    normalized.sort(key=lambda item: (item["source_kind"], item["candidate_id"], item["candidate_fact_hash"]))
    return normalized


def _inject_missing_expanded_chapters(
    contract_payload: Mapping[str, Any],
    normalized: Sequence[Mapping[str, Any]],
    slots: Sequence[Mapping[str, Any]],
    selected_by_slot: dict[str, list[dict[str, Any]]],
    expanded: set[str],
) -> None:
    """Use spare expanded edges for a required chapter omitted by the window.

    This is intentionally conservative: the initial 4N graph is untouched,
    and expansion is strictly additive.  A slot that already has six edges
    is left alone rather than replacing an initial edge; the repository
    contract requires every initial edge to remain present in the expanded
    graph.
    """
    if not expanded:
        return
    required = [
        (chapter, int(count))
        for chapter, count in contract_payload["ordinary_chapter_quotas"].items()
        if count
    ]
    if not required:
        return
    def all_edges() -> list[dict[str, Any]]:
        return [edge for edges in selected_by_slot.values() for edge in edges]

    for chapter, target in required:
        while len({
            edge["content_hash"]
            for edge in all_edges()
            if edge["source_kind"] == "ordinary" and edge["chapter"] == chapter
        }) < target:
            chapter_rows = [
                item for item in normalized
                if item["source_kind"] == "ordinary" and item["chapter"] == chapter
            ]
            if not chapter_rows:
                break
            placed = False
            for slot in sorted(
                (slot for slot in slots if slot["slot_id"] in expanded),
                key=lambda item: item["position"],
            ):
                key = (slot["question_type_family"], slot["target_difficulty"])
                compatible = [
                    item for item in chapter_rows
                    if (item["question_type_family"], item["difficulty"]) == key
                ]
                if not compatible:
                    continue
                edges = selected_by_slot[slot["slot_id"]]
                existing_ids = {edge["candidate_id"] for edge in all_edges()}
                existing_hashes = {edge["content_hash"] for edge in all_edges()}
                item = next(
                    (
                        row for row in compatible
                        if row["candidate_id"] not in existing_ids
                        and row["content_hash"] not in existing_hashes
                    ),
                    None,
                )
                if item is None:
                    continue
                if len(edges) >= 6:
                    # Do not replace an existing edge.  Even replacing a
                    # newly-added position would remove a distinct
                    # (slot,candidate) key and violate the persisted additive
                    # graph contract.
                    break
                edge = {
                    "slot_id": slot["slot_id"], "position": slot["position"],
                    "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                    "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                    "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                    "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                    "bucket_rank": len(edges) + 1,
                }
                edges.append(edge)
                placed = True
                break
            if not placed:
                break


def build_candidate_edge_graph(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
    effective_difficulty_receipt: Mapping[str, Any] | None = None,
    expanded_slot_ids: Sequence[str] = (),
) -> FrozenCandidateEdgeGraph:
    """Build the bounded compatible graph: 4N initially, 6N after targeted expansion."""

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if stage4.get("contract_version") != STAGE4_CONTRACT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "stage4_contract_version_unsupported", "阶段 4 候选事实版本不受支持")
    if _digest(stage4) != contract.candidate_facts_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    effective, effective_hash = _effective_difficulties(
        effective_difficulty_receipt,
        candidate_facts_hash=contract.candidate_facts_hash,
    )
    normalized = _normalized_candidate_pool(c, stage4, effective)
    if isinstance(expanded_slot_ids, (str, bytes)) or not isinstance(expanded_slot_ids, Sequence):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_ids_invalid", "扩展槽位列表无效")
    expanded_list = [_text(item, "expanded_slot_id") for item in expanded_slot_ids]
    if len(set(expanded_list)) != len(expanded_list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_duplicate", "扩展槽位重复")
    expanded = set(expanded_list)
    valid_slots = {slot["slot_id"] for slot in c["slots"]}
    if not expanded.issubset(valid_slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "expanded_slot_unknown", "扩展槽位不在冻结合同中")
    edges: list[dict[str, Any]] = []
    selected_by_slot: dict[str, list[dict[str, Any]]] = {slot["slot_id"]: [] for slot in c["slots"]}
    ordered_pools: dict[tuple[str, str], tuple[int, list[dict[str, Any]]]] = {}
    for slot in c["slots"]:
        key = (slot["question_type_family"], slot["target_difficulty"])
        if key not in ordered_pools:
            compatible = [item for item in normalized if (item["question_type_family"], item["difficulty"]) == key]
            total = len(compatible)
            indices = [((other["position"] - 1) * 6 + offset) % total
                       for other in c["slots"] if (other["question_type_family"], other["target_difficulty"]) == key
                       for offset in range(min(6 if other["slot_id"] in expanded else 4, total))] if total else []
            prefix = max(indices) + 1 if indices else 0
            ordered_pools[key] = (total, _constraint_aware_order(compatible, c, limit=prefix))
        total, compatible = ordered_pools[key]
        limit = 6 if slot["slot_id"] in expanded else 4
        count = min(limit, total)
        start = ((slot["position"] - 1) * 6) % total if total else 0
        selected = [compatible[(start + offset) % total] for offset in range(count)]
        for bucket_rank, item in enumerate(selected, 1):
            selected_by_slot[slot["slot_id"]].append({
                "slot_id": slot["slot_id"], "position": slot["position"],
                "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                "bucket_rank": bucket_rank,
            })
    _inject_missing_expanded_chapters(c, normalized, c["slots"], selected_by_slot, expanded)
    edges = [edge for slot in c["slots"] for edge in selected_by_slot[slot["slot_id"]]]
    edges.sort(key=lambda item: (item["position"], item["bucket_rank"], item["candidate_id"], item["candidate_fact_hash"]))
    if len(edges) > (6 if expanded else 4) * contract.question_count:
        raise AssertionError("candidate edge budget violated")
    used_candidate_ids = {edge["candidate_id"] for edge in edges}
    candidate_index = {item["candidate_id"]: item for item in normalized if item["candidate_id"] in used_candidate_ids}
    candidate_limit = (6 if expanded else 4) * contract.question_count
    if len(candidate_index) > candidate_limit:
        raise AssertionError("candidate union budget violated")
    payload = {
        "contract_version": CANDIDATE_GRAPH_VERSION, "constraint_hash": contract.constraint_hash,
        "candidate_facts_hash": contract.candidate_facts_hash, "projection_identity_hash": contract.projection_identity_hash,
        "effective_difficulty_hash": effective_hash, "graph_mode": "expanded" if expanded else "initial",
        "expanded_slot_ids": sorted(expanded), "candidates": candidate_index, "edges": edges,
    }
    raw = _canonical(payload)
    return FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, contract.constraint_hash, contract.candidate_facts_hash, payload["graph_mode"], len(edges), hashlib.sha256(raw.encode()).hexdigest(), raw)


def supplement_priority_candidate_graph(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
    base_graph: FrozenCandidateEdgeGraph,
    preferred_candidate_ids: Sequence[str],
) -> FrozenCandidateEdgeGraph:
    """Add resolved exact-priority candidates omitted only by the 4N ID window."""

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if (
        stage4.get("contract_version") != STAGE4_CONTRACT_VERSION
        or _digest(stage4) != contract.candidate_facts_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    graph_payload = _verified_graph_payload(base_graph, contract=contract)
    if isinstance(preferred_candidate_ids, (str, bytes)) or not isinstance(preferred_candidate_ids, Sequence):
        raise Stage5ContractError("exam_stage5_contract_invalid", "priority_candidate_ids_invalid", "一次性优先候选列表无效")
    preferred = [_text(value, "priority_candidate_id") for value in preferred_candidate_ids]
    if len(preferred) != len(set(preferred)):
        raise Stage5ContractError("exam_stage5_contract_invalid", "priority_candidate_id_duplicate", "一次性优先候选重复")
    preferred_set = set(preferred)

    slots = {slot["slot_id"]: slot for slot in c["slots"]}
    current_by_slot: dict[str, list[dict[str, Any]]] = {slot_id: [] for slot_id in slots}
    for raw_edge in graph_payload["edges"]:
        edge = dict(_mapping(raw_edge, "candidate_edge"))
        slot_id = edge.get("slot_id")
        if slot_id not in slots:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_edge_slot_unknown", "候选边槽位无效")
        current_by_slot[slot_id].append(edge)

    stored_counts = graph_payload.get("priority_base_counts")
    if stored_counts is None:
        base_counts = {slot_id: len(edges) for slot_id, edges in current_by_slot.items()}
    else:
        stored_counts = _mapping(stored_counts, "priority_base_counts")
        if set(stored_counts) != set(slots):
            raise Stage5ContractError("exam_stage5_contract_invalid", "priority_base_counts_shape_invalid", "优先候选基线计数无效")
        base_counts = {}
        for slot_id, value in stored_counts.items():
            if type(value) is not int or not 0 <= value <= len(current_by_slot[slot_id]) or value > 6:
                raise Stage5ContractError("exam_stage5_contract_invalid", "priority_base_count_invalid", "优先候选基线计数无效")
            base_counts[slot_id] = value

    # A repeated call starts from the immutable pre-priority prefix, so a
    # consumed preferred candidate from an earlier round cannot linger.
    result_by_slot = {
        slot_id: edges[:base_counts[slot_id]]
        for slot_id, edges in current_by_slot.items()
    }
    normalized = _normalized_candidate_pool(
        c, stage4, {}, embedded_resolved_only=True
    )
    preferred_rows = {
        item["candidate_id"]: item
        for item in normalized
        if item["candidate_id"] in preferred_set
    }
    supplemented_slots: set[str] = set()
    for slot_id, slot in slots.items():
        edges = result_by_slot[slot_id]
        capacity = min(2, max(0, 6 - len(edges)))
        if not capacity:
            continue
        existing_ids = {edge["candidate_id"] for edge in edges}
        existing_hashes = {edge["content_hash"] for edge in edges}
        compatible = [
            item for item in preferred_rows.values()
            if item["candidate_id"] not in existing_ids
            and item["content_hash"] not in existing_hashes
            and item["question_type_family"] == slot["question_type_family"]
            and item["difficulty"] == slot["target_difficulty"]
        ]
        compatible.sort(key=lambda item: (item["candidate_id"], item["candidate_fact_hash"]))
        for item in compatible[:capacity]:
            edges.append({
                "slot_id": slot_id, "position": slot["position"],
                "candidate_id": item["candidate_id"], "candidate_fact_hash": item["candidate_fact_hash"],
                "content_hash": item["content_hash"], "source_kind": item["source_kind"],
                "chapter": item["chapter"], "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
                "question_type_family": item["question_type_family"], "target_difficulty": item["difficulty"],
                "bucket_rank": len(edges) + 1,
            })
            existing_ids.add(item["candidate_id"])
            existing_hashes.add(item["content_hash"])
            supplemented_slots.add(slot_id)

    edges = [edge for slot_id in slots for edge in result_by_slot[slot_id]]
    edges.sort(key=lambda item: (item["position"], item["bucket_rank"], item["candidate_id"], item["candidate_fact_hash"]))
    if len(edges) > 6 * contract.question_count or _builtin_any(len(value) > 6 for value in result_by_slot.values()):
        raise AssertionError("priority candidate edge budget violated")
    original_candidates = _mapping(graph_payload.get("candidates"), "candidate_index")
    used_ids = {edge["candidate_id"] for edge in edges}
    candidate_index = {
        candidate_id: dict(value)
        for candidate_id, value in original_candidates.items()
        if candidate_id in used_ids
    }
    candidate_index.update({
        candidate_id: row for candidate_id, row in preferred_rows.items()
        if candidate_id in used_ids
    })
    expanded_slots = set(graph_payload.get("expanded_slot_ids") or ()) | supplemented_slots
    graph_mode = "expanded" if supplemented_slots or base_graph.graph_mode == "expanded" else base_graph.graph_mode
    payload = {
        **graph_payload,
        "graph_mode": graph_mode,
        "expanded_slot_ids": sorted(expanded_slots),
        "priority_base_counts": {slot_id: base_counts[slot_id] for slot_id in sorted(slots)},
        "candidates": candidate_index,
        "edges": edges,
    }
    raw = _canonical(payload)
    return FrozenCandidateEdgeGraph(
        CANDIDATE_GRAPH_VERSION, contract.constraint_hash,
        contract.candidate_facts_hash, graph_mode, len(edges),
        hashlib.sha256(raw.encode()).hexdigest(), raw,
    )


def build_difficulty_defect_edges(
    contract: Stage5GlobalConstraintContract,
    *,
    stage4: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Expose only unresolved, non-difficulty-compatible edges for Stage 4.

    These edges are not selectable and carry no score.  Their sole purpose is
    to let the Stage-4 two-round resolver locate the minimum component that can
    reach a real Stage-5 deficit without weakening any source, subject, chapter,
    target-institution or question-type boundary.
    """

    c = _verified_contract_payload(contract)
    stage4 = _mapping(stage4, "stage4")
    if (
        stage4.get("contract_version") != STAGE4_CONTRACT_VERSION
        or _digest(stage4) != contract.candidate_facts_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实哈希不匹配")
    candidates = stage4.get("candidates")
    if not isinstance(candidates, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_rows_invalid", "冻结候选集合无效")
    excluded_ids, excluded_hashes = _recent_usage_effect(
        _mapping(c["recent_usage"], "recent_usage"), candidates
    )
    seen: set[str] = set()
    unresolved: list[tuple[str, str]] = []
    for raw in candidates:
        raw = _mapping(raw, "candidate")
        candidate_id = _text(raw.get("candidate_id"), "candidate_id")
        if candidate_id in seen:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_id_not_unique", "候选 ID 不唯一")
        seen.add(candidate_id)
        content_hash = _hash(raw.get("content_hash"), "content_hash")
        if candidate_id in excluded_ids or content_hash in excluded_hashes:
            continue
        source = _source(raw.get("source_kind"))
        if _text(raw.get("subject"), "candidate_subject") != c["subject"] or c["source_quotas"][source] <= 0:
            continue
        if source == "ordinary":
            chapter = _text(raw.get("chapter"), "chapter")
            if c["ordinary_chapter_quotas"].get(chapter, 0) <= 0:
                continue
        else:
            institution = _text(raw.get("source_institution"), "source_institution")
            _text(raw.get("source_exam_id"), "source_exam_id")
            _text(raw.get("section_id"), "candidate_section_id")
            if institution.casefold() == c["target_institution"].casefold():
                raise Stage5ContractError("exam_stage5_contract_invalid", "target_institution_candidate_forbidden", "候选来自目标院校")
        fact = _mapping(raw.get("difficulty_fact"), "difficulty_fact")
        if fact.get("resolved") is True:
            continue
        unresolved.append((candidate_id, _text(raw.get("question_type_family"), "question_type_family")))
    edges = [
        {
            "slot_id": slot["slot_id"],
            "candidate_id": candidate_id,
            "compatible_without_difficulty": True,
        }
        for slot in c["slots"]
        for candidate_id, question_type in unresolved
        if question_type == slot["question_type_family"]
    ]
    edges.sort(key=lambda item: (item["slot_id"], item["candidate_id"]))
    return tuple(edges)


def _maximum_matching(slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[dict[str, str], dict[str, str]]:
    candidate_to_slot: dict[str, str] = {}
    slot_to_candidate: dict[str, str] = {}
    def visit(slot: str, seen: set[str]) -> bool:
        for edge in edges_by_slot.get(slot, ()):
            candidate = str(edge["candidate_id"])
            if candidate in seen:
                continue
            seen.add(candidate)
            owner = candidate_to_slot.get(candidate)
            if owner is None or visit(owner, seen):
                candidate_to_slot[candidate] = slot
                slot_to_candidate[slot] = candidate
                return True
        return False
    for slot in slots:
        visit(slot, set())
    return slot_to_candidate, candidate_to_slot


def _minimal_hall_set(
    slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]], resource_field: str,
) -> list[str] | None:
    projected = {
        slot: [{**edge, "candidate_id": edge[resource_field]} for edge in edges_by_slot.get(slot, ())]
        for slot in slots
    }
    slot_matching, resource_matching = _maximum_matching(slots, projected)
    if len(slot_matching) == len(slots):
        return None
    # Alternating reachability from unmatched left vertices yields a deficient Hall set.
    reachable_slots = set(slots) - set(slot_matching)
    pending = list(sorted(reachable_slots))
    reachable_resources: set[str] = set()
    while pending:
        slot = pending.pop(0)
        matched_resource = slot_matching.get(slot)
        for edge in projected.get(slot, ()):
            resource = str(edge["candidate_id"])
            if resource == matched_resource or resource in reachable_resources:
                continue
            reachable_resources.add(resource)
            owner = resource_matching.get(resource)
            if owner is not None and owner not in reachable_slots:
                reachable_slots.add(owner)
                pending.append(owner)
    # N is contract-bounded at 20.  Bitset enumeration therefore gives a true
    # minimum-cardinality witness (then lexicographically first), rather than
    # merely an inclusion-minimal result dependent on deletion order.
    resource_ids = sorted({str(edge[resource_field]) for slot in slots for edge in edges_by_slot.get(slot, ())})
    resource_bits = {resource: 1 << index for index, resource in enumerate(resource_ids)}
    masks = {
        slot: sum({resource_bits[str(edge[resource_field])] for edge in edges_by_slot.get(slot, ())})
        for slot in slots
    }
    upper_bound = len(reachable_slots)
    for size in range(1, upper_bound + 1):
        for trial in itertools.combinations(sorted(slots), size):
            neighbors = 0
            for slot in trial:
                neighbors |= masks[slot]
            if neighbors.bit_count() < size:
                return list(trial)
    raise AssertionError("maximum matching lacked a Hall witness")


def _hall_witness(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, slots: Sequence[str], edges_by_slot: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    failures = [
        (field, deficient)
        for field in ("candidate_id", "content_hash")
        if (deficient := _minimal_hall_set(slots, edges_by_slot, field)) is not None
    ]
    resource_field, deficient = min(failures, key=lambda item: (len(item[1]), item[1], item[0]))
    candidates = sorted({str(edge["candidate_id"]) for slot in deficient for edge in edges_by_slot.get(slot, ())})
    fingerprints = sorted({str(edge["content_hash"]) for slot in deficient for edge in edges_by_slot.get(slot, ())})
    deficient_resources = candidates if resource_field == "candidate_id" else fingerprints
    body = {
        "version": WITNESS_VERSION, "kind": "hall_min_cut", "constraint_hash": contract.constraint_hash,
        "graph_hash": graph.graph_hash, "deficient_slot_ids": deficient,
        "reachable_candidate_ids": candidates, "reachable_content_hashes": fingerprints,
        "resource_kind": resource_field, "deficiency": len(deficient) - len(deficient_resources),
        "constraint_ids": ["candidate_unique", "content_hash_unique"],
        "counts": {"slots": len(deficient), "candidates": len(candidates), "content_hashes": len(fingerprints)},
        "actions": ["expand_minimal_deficient_slots", "repair_candidate_or_content_identity"],
    }
    return {**body, "witness_hash": _digest(body)}


def _locate_structural_graph_deficit(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
) -> Mapping[str, Any] | None:
    """Locate a candidate/content Hall deficit without scoring."""

    c = _verified_contract_payload(contract)
    g = _verified_graph_payload(graph, contract=contract)
    slots = [slot["slot_id"] for slot in c["slots"]]
    edges_by_slot: dict[str, list[Mapping[str, Any]]] = {slot_id: [] for slot_id in slots}
    for edge in g["edges"]:
        edges_by_slot[edge["slot_id"]].append(edge)
    for edges in edges_by_slot.values():
        edges.sort(key=lambda edge: (edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]))
    candidate_matching, _ = _maximum_matching(slots, edges_by_slot)
    fingerprint_edges = {
        slot_id: [{**edge, "candidate_id": edge["content_hash"]} for edge in edges]
        for slot_id, edges in edges_by_slot.items()
    }
    fingerprint_matching, _ = _maximum_matching(slots, fingerprint_edges)
    if len(candidate_matching) == len(slots) and len(fingerprint_matching) == len(slots):
        return None
    return _hall_witness(contract, graph, slots, edges_by_slot)


def locate_candidate_graph_deficit(
    contract: Stage5GlobalConstraintContract,
    graph: FrozenCandidateEdgeGraph,
    *,
    search_guard: Callable[[], bool] | None = None,
) -> Mapping[str, Any] | None:
    """Locate any hard-constraint deficit with local deterministic feasibility.

    The neutral receipt exists only to drive the exact local solver through
    source/chapter/coverage/diversity constraints.  It performs no provider or
    injected scoring-adapter activity and cannot affect a later real score.
    """

    structural = _locate_structural_graph_deficit(contract, graph)
    if structural is not None:
        return structural
    neutral = [
        {
            "slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"],
            "score": 0, "penalty": 0,
        }
        for edge in graph.to_dict()["edges"]
    ]
    receipt = _local_v2_score_receipt(
        contract, graph, neutral, purpose="stage5-local-feasibility-v2"
    )
    try:
        _solve_global_selection(
            contract, graph, score_receipt=receipt,
            feasibility_only=True, search_guard=search_guard,
        )
    except Stage5InfeasibleError as exc:
        return exc.witness
    return None


def _initial_graph_from_expanded(graph: FrozenCandidateEdgeGraph) -> FrozenCandidateEdgeGraph:
    payload = graph.to_dict()
    base_edges = [edge for edge in payload["edges"] if edge["bucket_rank"] <= 4]
    candidate_ids = {edge["candidate_id"] for edge in base_edges}
    base_payload = {**payload, "graph_mode": "initial", "expanded_slot_ids": [], "candidates": {key: value for key, value in payload["candidates"].items() if key in candidate_ids}, "edges": base_edges}
    raw = _canonical(base_payload)
    return FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, graph.constraint_hash, graph.candidate_facts_hash, "initial", len(base_edges), hashlib.sha256(raw.encode()).hexdigest(), raw)


def _candidate_base_utility_receipt(
    value: Mapping[str, Any],
    graph: FrozenCandidateEdgeGraph,
    contract: Stage5GlobalConstraintContract,
) -> tuple[dict[tuple[str, str], tuple[int, int]], set[tuple[str, str]], str]:
    """Verify the new local-utility receipt and project it for the solver.

    The projection is an internal ``(utility, 0)`` tuple solely to reuse the
    existing branch-and-bound arithmetic.  The solver's new-mode assignment
    payload never serializes those legacy names; only ``utility`` is emitted.
    """

    raw = _mapping(value, "candidate_base_utility_receipt")
    fields = {
        "version", "graph_hash", "constraint_hash", "candidate_facts_hash",
        "projection_identity_hash", "catalog_version", "catalog_hash",
        "factor_applicability_version", "parameter_set_hash", "policy_version",
        "policy_hash", "diversity_policy_hash", "rejection_ledger_head_hash",
        "rejected_edges", "rubric_receipt_hash", "utilities", "receipt_hash",
    }
    if set(raw) != fields or raw.get("version") != UTILITY_RECEIPT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_receipt_shape_invalid", "候选级 utility 回执字段无效")
    graph_payload = graph.to_dict()
    if raw.get("graph_hash") != graph.graph_hash or raw.get("constraint_hash") != contract.constraint_hash or raw.get("candidate_facts_hash") != contract.candidate_facts_hash or raw.get("projection_identity_hash") != graph_payload.get("projection_identity_hash"):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "utility_receipt_identity_invalid", "候选级 utility 回执未绑定当前图")
    if raw.get("catalog_version") != RUBRIC_CATALOG_VERSION or raw.get("catalog_hash") != FACTOR_CATALOG_HASH or raw.get("factor_applicability_version") != FACTOR_APPLICABILITY_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_receipt_catalog_invalid", "候选级 rubric 目录身份无效")
    for field in ("graph_hash", "constraint_hash", "candidate_facts_hash", "projection_identity_hash", "parameter_set_hash", "policy_hash", "rubric_receipt_hash", "rejection_ledger_head_hash"):
        _hash(raw.get(field), f"utility_{field}")
    _text(raw.get("policy_version"), "utility_policy_version", 128)
    if raw.get("diversity_policy_hash") is not None:
        _hash(raw.get("diversity_policy_hash"), "diversity_policy_hash")
    rejected_raw = raw.get("rejected_edges")
    if not isinstance(rejected_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edges_invalid", "候选级 utility 拒绝边集合无效")
    rejected: set[tuple[str, str]] = set()
    for item in rejected_raw:
        item = _mapping(item, "utility_rejected_edge")
        _exact_fields(item, {"slot_id", "candidate_id"}, "utility_rejected_edge")
        key = (_text(item["slot_id"], "utility_rejected_slot_id"), _text(item["candidate_id"], "utility_rejected_candidate_id"))
        if key in rejected:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edge_duplicate", "候选级 utility 拒绝边重复")
        rejected.add(key)
    ledger_hash = raw["rejection_ledger_head_hash"]
    if (rejected and ledger_hash == "0" * 64) or (not rejected and ledger_hash != "0" * 64):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejection_ledger_invalid", "候选级 utility 拒绝账本绑定无效")
    graph_edges = {(edge["slot_id"], edge["candidate_id"]): edge for edge in graph_payload.get("edges", [])}
    if not rejected.issubset(set(graph_edges)):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_rejected_edge_unknown", "候选级 utility 拒绝边不在当前图")
    utilities = raw.get("utilities")
    if not isinstance(utilities, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entries_invalid", "候选级 utility 条目无效")
    result: dict[tuple[str, str], tuple[int, int]] = {}
    candidate_values: dict[str, int] = {}
    for item in utilities:
        item = _mapping(item, "utility_entry")
        _exact_fields(item, {"slot_id", "candidate_id", "candidate_base", "utility"}, "utility_entry")
        slot_id = _text(item["slot_id"], "utility_slot_id")
        candidate_id = _text(item["candidate_id"], "utility_candidate_id")
        key = (slot_id, candidate_id)
        if key not in graph_edges or key in rejected or key in result:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entry_identity_invalid", "候选级 utility 条目不是唯一冻结边")
        utility = item["utility"]
        if type(utility) is not int:
            raise Stage5ContractError("exam_stage5_contract_invalid", "utility_value_invalid", "候选级 utility 必须是确定性整数")
        base = item["candidate_base"]
        if not isinstance(base, list) or len(base) != 8:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效")
        try:
            validated_base = candidate_base_key({
                "candidate_id": base[0],
                "candidate_fact_hash": base[1],
                "content_hash": base[2],
                "content_snapshot_hash": base[3] or None,
                "material_identity": base[4] or None,
                "catalog_version": base[5],
                "catalog_hash": base[6],
                "factor_applicability_version": base[7],
            })
        except CandidateBaseRubricError as exc:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效") from exc
        if list(validated_base) != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_key_invalid", "候选级 identity key 无效")
        if base[0] != candidate_id or base[1] != graph_edges[key].get("candidate_fact_hash") or base[2] != graph_edges[key].get("content_hash"):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "candidate_base_key_mismatch", "候选级 utility 未绑定候选事实")
        if base[5] != RUBRIC_CATALOG_VERSION or base[6] != FACTOR_CATALOG_HASH or base[7] != FACTOR_APPLICABILITY_VERSION:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_catalog_mismatch", "候选级 identity 目录版本漂移")
        previous = candidate_values.get(candidate_id)
        if previous is not None and previous != utility:
            raise Stage5ContractError("exam_stage5_contract_invalid", "candidate_base_consistency_invalid", "同一候选跨槽 utility 不一致")
        candidate_values[candidate_id] = utility
        result[key] = (utility, 0)
    expected = set(graph_edges) - rejected
    if set(result) != expected:
        raise Stage5ContractError("exam_stage5_contract_invalid", "utility_entries_incomplete", "候选级 utility 未覆盖全部非拒绝冻结边")
    body = {key: raw[key] for key in fields - {"receipt_hash"}}
    receipt_hash = _hash(raw.get("receipt_hash"), "utility_receipt_hash")
    if _digest(body) != receipt_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "utility_receipt_hash_mismatch", "候选级 utility 回执哈希不匹配")
    return result, rejected, receipt_hash


def _score_receipt(value: Mapping[str, Any], graph: FrozenCandidateEdgeGraph, contract: Stage5GlobalConstraintContract) -> tuple[dict[tuple[str, str], tuple[int, int]], set[tuple[str, str]], str]:
    if isinstance(value, Mapping) and value.get("version") == UTILITY_RECEIPT_VERSION:
        return _candidate_base_utility_receipt(value, graph, contract)
    value = _mapping(value, "score_receipt")
    if set(value) != {"version", "graph_hash", "scoring_identity", "scores", "hash"} or value.get("version") != STAGE6_SCORE_RECEIPT_VERSION or value.get("graph_hash") != graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_receipt_identity_invalid", "评分回执身份无效")
    identity = _mapping(value.get("scoring_identity"), "scoring_identity")
    identity_fields = {"version", "constraint_hash", "base_graph_hash", "rejection_ledger_head_hash", "rejected_edges", "model_id", "prompt_version", "candidate_facts_hash", "profile_hash", "projection_identity_hash", "response_schema_version", "windows"}
    graph_payload, contract_payload = graph.to_dict(), contract.to_dict()
    if set(identity) != identity_fields or identity.get("version") != STAGE6_SCORING_IDENTITY_VERSION or identity.get("constraint_hash") != graph.constraint_hash or identity.get("candidate_facts_hash") != graph.candidate_facts_hash or identity.get("projection_identity_hash") != graph_payload.get("projection_identity_hash") or identity.get("profile_hash") != contract_payload.get("profile_hash"):
        raise Stage5ContractError("exam_stage5_contract_invalid", "scoring_identity_invalid", "评分回执完整来源身份无效")
    _text(identity.get("model_id"), "score_model_id", 128)
    _text(identity.get("prompt_version"), "score_prompt_version", 128)
    _text(identity.get("response_schema_version"), "score_response_schema_version", 128)
    expanded = graph_payload.get("graph_mode") == "expanded"
    base_graph = _initial_graph_from_expanded(graph) if expanded else None
    expected_base_hash = base_graph.graph_hash if base_graph is not None else None
    if identity.get("base_graph_hash") != expected_base_hash:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_base_graph_identity_invalid", "评分回执基础图身份无效")
    ledger_hash = _hash(identity.get("rejection_ledger_head_hash"), "rejection_ledger_head_hash")
    rejected_raw = identity.get("rejected_edges")
    if not isinstance(rejected_raw, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edges_invalid", "评分拒绝边集合无效")
    rejected: set[tuple[str, str]] = set()
    for item in rejected_raw:
        item = _mapping(item, "rejected_edge")
        if set(item) != {"slot_id", "candidate_id"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_shape_invalid", "评分拒绝边字段无效")
        key = (_text(item.get("slot_id"), "rejected_slot_id"), _text(item.get("candidate_id"), "rejected_candidate_id"))
        if key in rejected:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_duplicate", "评分拒绝边重复")
        rejected.add(key)
    if (rejected and ledger_hash == "0" * 64) or (not rejected and ledger_hash != "0" * 64):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejection_ledger_binding_invalid", "评分拒绝边与账本头不一致")
    windows = identity.get("windows")
    if not isinstance(windows, list) or not windows:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_windows_invalid", "评分窗口身份无效")
    seen_windows = set()
    phase_counts = {"initial": 0, "expansion": 0}
    phase_windows: dict[str, list[dict[str, Any]]] = {"initial": [], "expansion": []}
    for window in windows:
        window = _mapping(window, "score_window")
        if set(window) != {"phase", "window_index", "graph_hash", "slot_ids", "window_hash", "request_hash", "material_hash", "numeric_response_hash"} or window.get("phase") not in {"initial", "expansion"} or type(window.get("window_index")) is not int or window["window_index"] < 1:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_identity_invalid", "评分窗口身份字段无效")
        slot_ids = window.get("slot_ids")
        if not isinstance(slot_ids, list) or not 1 <= len(slot_ids) <= 4 or len(set(slot_ids)) != len(slot_ids) or _builtin_any(not isinstance(slot_id, str) or re.fullmatch(r"slot-[0-9]{4}", slot_id) is None for slot_id in slot_ids):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "评分窗口槽位身份无效")
        key = (window["phase"], window["window_index"])
        if key in seen_windows:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_identity_duplicate", "评分窗口身份重复")
        seen_windows.add(key)
        phase_counts[window["phase"]] += 1
        phase_windows[window["phase"]].append(dict(window))
        expected_window_graph = expected_base_hash if expanded and window["phase"] == "initial" else graph.graph_hash
        if window.get("graph_hash") != expected_window_graph or (not expanded and window["phase"] != "initial"):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_graph_identity_invalid", "评分窗口未绑定对应阶段图")
        for field in ("graph_hash", "window_hash", "request_hash", "material_hash", "numeric_response_hash"):
            _hash(window.get(field), field)
    result: dict[tuple[str, str], tuple[int, int]] = {}
    if not isinstance(value["scores"], list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "scores_invalid", "评分条目无效")
    valid = {(edge["slot_id"], edge["candidate_id"]) for edge in graph_payload["edges"]}
    base_keys = {(edge["slot_id"], edge["candidate_id"]) for edge in (base_graph.to_dict()["edges"] if base_graph else graph_payload["edges"])}
    new_keys = valid - base_keys
    additions_by_slot: dict[str, int] = {}
    for slot_id, _candidate_id in new_keys:
        additions_by_slot[slot_id] = additions_by_slot.get(slot_id, 0) + 1
    question_count = contract.question_count
    edge_multiplier = 6 if expanded or graph_payload.get("graph_mode") == "repair-scope" else 4
    if len(valid) > edge_multiplier * question_count or _builtin_any(count > 2 for count in additions_by_slot.values()):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_graph_budget_exceeded", "评分图超过 4N/6N 或每槽扩展上限")
    expansion_limit = math.ceil(len(additions_by_slot) / 4)
    if phase_counts["initial"] != math.ceil(question_count / 4) or phase_counts["expansion"] > expansion_limit or (not expanded and phase_counts["expansion"]) or (new_keys - rejected and phase_counts["expansion"] == 0):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_count_invalid", "评分窗口调用身份超过初始或扩展上限")
    initial_windows = sorted(phase_windows["initial"], key=lambda item: item["window_index"])
    expected_slots = [slot["slot_id"] for slot in contract_payload["slots"]]
    expected_initial_groups = [expected_slots[index:index + 4] for index in range(0, len(expected_slots), 4)]
    if [window["window_index"] for window in initial_windows] != list(range(1, len(initial_windows) + 1)) or [window["slot_ids"] for window in initial_windows] != expected_initial_groups:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "初始评分窗口未完整规范绑定全部题槽")
    expansion_windows = sorted(phase_windows["expansion"], key=lambda item: item["window_index"])
    expansion_slot_ids = [slot_id for window in expansion_windows for slot_id in window["slot_ids"]]
    expected_expansion_groups = [expansion_slot_ids[index:index + 4] for index in range(0, len(expansion_slot_ids), 4)]
    if (
        [window["window_index"] for window in expansion_windows] != list(range(1, len(expansion_windows) + 1))
        or len(expansion_slot_ids) != len(set(expansion_slot_ids))
        or expansion_slot_ids != sorted(expansion_slot_ids)
        or not set(expansion_slot_ids).issubset(additions_by_slot)
        or [window["slot_ids"] for window in expansion_windows] != expected_expansion_groups
    ):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "扩展评分窗口槽位身份无效")
    if not rejected.issubset(new_keys):
        raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_invalid", "评分拒绝边不是扩展新增边")
    for raw in value["scores"]:
        raw = _mapping(raw, "score")
        if set(raw) != {"slot_id", "candidate_id", "score", "penalty"}:
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_fields_invalid", "评分条目字段无效")
        key = (_text(raw["slot_id"], "score_slot_id"), _text(raw["candidate_id"], "score_candidate_id"))
        score = raw["score"]
        penalty = raw["penalty"]
        if (
            key not in valid or key in result
            or type(score) is not int or not 0 <= score <= _MAX_LOCAL_SCORE
            or type(penalty) is not int or not 0 <= penalty <= min(_MAX_LOCAL_PENALTY, score)
        ):
            raise Stage5ContractError("exam_stage5_contract_invalid", "score_edge_invalid", "评分条目不是冻结边或整数")
        result[key] = (score, penalty)
    if set(result) & rejected or not base_keys.issubset(result) or set(result) | rejected != valid:
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_edges_incomplete", "评分与显式拒绝未精确覆盖冻结图")
    scored_new_slots = {slot for slot, candidate in result if (slot, candidate) in new_keys}
    if scored_new_slots != set(expansion_slot_ids):
        raise Stage5ContractError("exam_stage5_contract_invalid", "score_window_slots_invalid", "扩展窗口未精确绑定成功评分的新增边槽位")
    normalized_scores = [
        {"slot_id": slot, "candidate_id": candidate, "score": result[(slot, candidate)][0], "penalty": result[(slot, candidate)][1]}
        for slot, candidate in sorted(result)
    ]
    normalized_identity = {**identity, "rejected_edges": [{"slot_id": slot, "candidate_id": candidate} for slot, candidate in sorted(rejected)], "windows": sorted((dict(item) for item in windows), key=lambda item: (item["phase"], item["window_index"], item["window_hash"]))}
    normalized_hash = _digest({"version": value["version"], "graph_hash": graph.graph_hash, "scoring_identity": normalized_identity, "scores": normalized_scores})
    if normalized_hash != _hash(value.get("hash"), "score_receipt_hash"):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "score_receipt_hash_mismatch", "评分回执哈希不匹配")
    return result, rejected, normalized_hash


def _local_v2_score_receipt(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, scores: Sequence[Mapping[str, Any]], *, purpose: str) -> dict[str, Any]:
    """Bind deterministic Stage-5 verification/repair scores to a v2 identity."""
    graph_payload, contract_payload = graph.to_dict(), contract.to_dict()
    numeric = sorted((dict(item) for item in scores), key=lambda item: (item["slot_id"], item["candidate_id"]))
    numeric_response_hash = _digest({"purpose": purpose, "graph_hash": graph.graph_hash, "scores": numeric})
    base_graph = _initial_graph_from_expanded(graph) if graph_payload.get("graph_mode") == "expanded" else None
    slot_ids = [slot["slot_id"] for slot in contract_payload["slots"]]
    initial_slot_groups = [slot_ids[index:index + 4] for index in range(0, len(slot_ids), 4)]
    windows = [{
        "phase": "initial", "window_index": index, "graph_hash": base_graph.graph_hash if base_graph else graph.graph_hash,
        "slot_ids": initial_slot_groups[index - 1],
        "window_hash": _digest({"purpose": purpose, "graph_hash": base_graph.graph_hash if base_graph else graph.graph_hash, "window_index": index}),
        "request_hash": _digest({"purpose": purpose, "constraint_hash": graph.constraint_hash, "window_index": index}),
        "material_hash": _digest({"candidate_facts_hash": graph.candidate_facts_hash}),
        "numeric_response_hash": numeric_response_hash,
    } for index in range(1, math.ceil(contract.question_count / 4) + 1)]
    if base_graph is not None and base_graph.edge_count < graph.edge_count:
        base_keys = {(edge["slot_id"], edge["candidate_id"]) for edge in base_graph.to_dict()["edges"]}
        affected_slots = sorted({edge["slot_id"] for edge in graph_payload["edges"] if (edge["slot_id"], edge["candidate_id"]) not in base_keys})
        affected_groups = [affected_slots[index:index + 4] for index in range(0, len(affected_slots), 4)]
        windows.extend({
            "phase": "expansion", "window_index": index, "graph_hash": graph.graph_hash,
            "slot_ids": affected_groups[index - 1],
            "window_hash": _digest({"purpose": purpose, "graph_hash": graph.graph_hash, "window_index": index}),
            "request_hash": _digest({"purpose": purpose, "constraint_hash": graph.constraint_hash, "phase": "expansion", "window_index": index}),
            "material_hash": _digest({"candidate_facts_hash": graph.candidate_facts_hash}),
            "numeric_response_hash": numeric_response_hash,
        } for index in range(1, len(affected_groups) + 1))
    identity = {
        "version": STAGE6_SCORING_IDENTITY_VERSION,
        "constraint_hash": graph.constraint_hash,
        "base_graph_hash": base_graph.graph_hash if base_graph else None,
        "rejection_ledger_head_hash": "0" * 64,
        "rejected_edges": [],
        "model_id": "stage5-local-deterministic",
        "prompt_version": purpose,
        "candidate_facts_hash": graph.candidate_facts_hash,
        "profile_hash": contract_payload["profile_hash"],
        "projection_identity_hash": graph_payload["projection_identity_hash"],
        "response_schema_version": "stage5-local-numeric-v2",
        "windows": sorted(windows, key=lambda item: (item["phase"], item["window_index"], item["window_hash"])),
    }
    body = {"version": STAGE6_SCORE_RECEIPT_VERSION, "graph_hash": graph.graph_hash, "scoring_identity": identity, "scores": numeric}
    return {**body, "hash": _digest(body)}


def _local_candidate_base_utility_receipt(
    graph: FrozenCandidateEdgeGraph,
    source_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Project an authenticated utility receipt onto an ephemeral repair graph."""

    source = _mapping(source_receipt, "source_utility_receipt")
    if source.get("version") != UTILITY_RECEIPT_VERSION:
        raise Stage5ContractError("exam_stage5_contract_invalid", "source_utility_receipt_invalid", "修复 utility 来源版本无效")
    by_key = {
        (item["slot_id"], item["candidate_id"]): item
        for item in source.get("utilities", [])
        if isinstance(item, Mapping)
    }
    by_candidate: dict[str, Mapping[str, Any]] = {}
    for item in by_key.values():
        candidate_id = item.get("candidate_id")
        base = item.get("candidate_base")
        if not isinstance(candidate_id, str) or not isinstance(base, list) or len(base) != 8 or base[0] != candidate_id:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效")
        try:
            validated_base = candidate_base_key({
                "candidate_id": base[0],
                "candidate_fact_hash": base[1],
                "content_hash": base[2],
                "content_snapshot_hash": base[3] or None,
                "material_identity": base[4] or None,
                "catalog_version": base[5],
                "catalog_hash": base[6],
                "factor_applicability_version": base[7],
            })
        except CandidateBaseRubricError as exc:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效") from exc
        if list(validated_base) != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_invalid", "修复 utility 候选身份无效")
        previous = by_candidate.get(candidate_id)
        if previous is not None and previous.get("candidate_base") != base:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_candidate_base_ambiguous", "修复 utility 候选基础身份不唯一")
        by_candidate[candidate_id] = item
    utilities: list[dict[str, Any]] = []
    for edge in graph.to_dict()["edges"]:
        item = by_key.get((edge["slot_id"], edge["candidate_id"]))
        if item is None:
            item = by_candidate.get(edge["candidate_id"])
        if item is None:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_utility_missing", "修复图存在未评分候选边")
        base = item["candidate_base"]
        if base[0] != edge["candidate_id"] or base[1] != edge.get("candidate_fact_hash") or base[2] != edge.get("content_hash"):
            raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_candidate_base_mismatch", "修复 utility 未绑定候选事实")
        utilities.append({
            "slot_id": edge["slot_id"],
            "candidate_id": edge["candidate_id"],
            "candidate_base": list(item["candidate_base"]),
            "utility": item["utility"],
        })
    payload = graph.to_dict()
    body = {
        "version": UTILITY_RECEIPT_VERSION,
        "graph_hash": graph.graph_hash,
        "constraint_hash": graph.constraint_hash,
        "candidate_facts_hash": graph.candidate_facts_hash,
        "projection_identity_hash": payload["projection_identity_hash"],
        "catalog_version": source["catalog_version"],
        "catalog_hash": source["catalog_hash"],
        "factor_applicability_version": source["factor_applicability_version"],
        "parameter_set_hash": source["parameter_set_hash"],
        "policy_version": source["policy_version"],
        "policy_hash": source["policy_hash"],
        "diversity_policy_hash": source["diversity_policy_hash"],
        "rejection_ledger_head_hash": "0" * 64,
        "rejected_edges": [],
        "rubric_receipt_hash": source["rubric_receipt_hash"],
        "utilities": sorted(utilities, key=lambda item: (item["slot_id"], item["candidate_id"])),
    }
    return {**body, "receipt_hash": _digest(body)}


def solve_global_selection(contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, *, score_receipt: Mapping[str, Any]) -> Stage5Selection:
    """Use exact branch-and-bound over a frozen utility receipt.

    Legacy ``local-score-v2`` receipts are still accepted only for historical
    compatibility.  Candidate-base receipts use the same feasibility engine
    but serialize only locally derived ``utility``; no model score/penalty is
    introduced into the new selection payload.
    """

    selection = _solve_global_selection(contract, graph, score_receipt=score_receipt)
    assert selection is not None
    return selection


def _solve_global_selection(
    contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph, *,
    score_receipt: Mapping[str, Any], feasibility_only: bool = False,
    search_guard: Callable[[], bool] | None = None,
) -> Stage5Selection | None:
    c = _verified_contract_payload(contract)
    g = _verified_graph_payload(graph, contract=contract)
    utility_mode = isinstance(score_receipt, Mapping) and score_receipt.get("version") == UTILITY_RECEIPT_VERSION
    scores, rejected_edges, score_hash = _score_receipt(score_receipt, graph, contract)
    edges_by_slot: dict[str, list[dict[str, Any]]] = {slot["slot_id"]: [] for slot in c["slots"]}
    for edge in g["edges"]:
        if (edge["slot_id"], edge["candidate_id"]) in rejected_edges:
            continue
        score, penalty = scores[(edge["slot_id"], edge["candidate_id"])]
        utility = score if utility_mode else score - penalty
        candidate_edge = {**edge, "utility": utility}
        if not utility_mode:
            candidate_edge.update({"score": score, "penalty": penalty})
        edges_by_slot[edge["slot_id"]].append(candidate_edge)
    for edges in edges_by_slot.values():
        edges.sort(key=lambda edge: (-edge["utility"], edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]))
    slots = [slot["slot_id"] for slot in c["slots"]]
    candidate_matching, _ = _maximum_matching(slots, edges_by_slot)
    fingerprint_matching, _ = _maximum_matching(slots, {slot: [{**edge, "candidate_id": edge["content_hash"]} for edge in edges] for slot, edges in edges_by_slot.items()})
    structural_witness = None if len(candidate_matching) == len(slots) and len(fingerprint_matching) == len(slots) else _hall_witness(contract, graph, slots, edges_by_slot)
    if structural_witness is not None:
        raise Stage5InfeasibleError(structural_witness)
    source_targets = dict(c["source_quotas"])
    chapter_targets = dict(c["ordinary_chapter_quotas"])
    diversity_payload = _mapping(c["diversity_policy"]["payload"], "diversity_payload")
    max_source_exam = diversity_payload.get("max_per_source_exam", len(slots))
    if type(max_source_exam) is not int or max_source_exam < 1:
        raise Stage5ContractError("exam_stage5_contract_invalid", "diversity_max_invalid", "多样性上限无效")
    coverage_payload = _mapping(c["coverage_policy"]["payload"], "coverage_payload")
    minima = coverage_payload.get("minima", [])
    if not isinstance(minima, list):
        raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minima_invalid", "覆盖最低约束无效")
    normalized_minima: list[tuple[str, str, str, int]] = []
    for item in minima:
        item = _mapping(item, "coverage_minimum")
        if set(item) != {"id", "field", "value", "min_count"} or item["field"] not in {"source_kind", "chapter", "question_type_family", "section_id"} or type(item["min_count"]) is not int or item["min_count"] < 0:
            raise Stage5ContractError("exam_stage5_contract_invalid", "coverage_minimum_invalid", "覆盖最低约束字段无效")
        normalized_minima.append((_text(item["id"], "coverage_id"), item["field"], _text(item["value"], "coverage_value"), item["min_count"]))
    best: tuple[int, tuple[tuple[int, str, str, int], ...], list[dict[str, Any]]] | None = None
    remaining = set(slots)
    chosen: list[dict[str, Any]] = []
    used_candidates: set[str] = set()
    used_content: set[str] = set()
    source_counts = {key: 0 for key in source_targets}
    chapter_counts = {key: 0 for key in chapter_targets}
    source_exam_counts: dict[str, int] = {}
    coverage_counts = {item[0]: 0 for item in normalized_minima}

    def eligible(edge: Mapping[str, Any]) -> bool:
        source = edge["source_kind"]
        chapter = edge["chapter"]
        exam = edge["source_exam_id"]
        return (
            edge["candidate_id"] not in used_candidates and edge["content_hash"] not in used_content
            and source_counts[source] < source_targets[source]
            and (source != "ordinary" or chapter in chapter_targets and chapter_counts[chapter] < chapter_targets[chapter])
            and (exam is None or source_exam_counts.get(exam, 0) < max_source_exam)
        )

    def bounds_possible() -> bool:
        left = len(remaining)
        if _builtin_any(count > source_targets[key] or count + left < source_targets[key] for key, count in source_counts.items()): return False
        if _builtin_any(count > chapter_targets[key] or count + left < chapter_targets[key] for key, count in chapter_counts.items()): return False
        if _builtin_any(coverage_counts[cid] + left < minimum for cid, _, _, minimum in normalized_minima): return False
        return True

    def visit(utility: int) -> bool:
        nonlocal best
        if search_guard is not None and not search_guard():
            raise Stage5SearchIncompleteError()
        if not remaining:
            if source_counts != source_targets or chapter_counts != chapter_targets or _builtin_any(coverage_counts[cid] < minimum for cid, _, _, minimum in normalized_minima):
                return False
            # Feasibility needs one witness, not the optimum among all zero-score ties.
            if feasibility_only:
                return True
            ordered = sorted(chosen, key=lambda edge: edge["position"])
            tie = tuple((edge["position"], edge["candidate_id"], edge["candidate_fact_hash"], edge["bucket_rank"]) for edge in ordered)
            candidate = (utility, tie, [dict(edge) for edge in ordered])
            if best is None or utility > best[0] or utility == best[0] and tie < best[1]: best = candidate
            return False
        domains = [(sum(1 for edge in edges_by_slot[slot] if eligible(edge)), slot) for slot in remaining]
        if best is not None:
            optimistic = utility
            for candidate_slot in remaining:
                available = [edge["utility"] for edge in edges_by_slot[candidate_slot] if eligible(edge)]
                if not available:
                    return False
                optimistic += max(available)
            if optimistic < best[0]:
                return False
        _, slot = min(domains, key=lambda item: (item[0], item[1]))
        if not _builtin_any(eligible(edge) for edge in edges_by_slot[slot]): return False
        remaining.remove(slot)
        for edge in edges_by_slot[slot]:
            if not eligible(edge): continue
            chosen.append(edge); used_candidates.add(edge["candidate_id"]); used_content.add(edge["content_hash"])
            source_counts[edge["source_kind"]] += 1
            if edge["source_kind"] == "ordinary": chapter_counts[edge["chapter"]] += 1
            if edge["source_exam_id"] is not None: source_exam_counts[edge["source_exam_id"]] = source_exam_counts.get(edge["source_exam_id"], 0) + 1
            hit: list[str] = []
            for cid, field, value, _minimum in normalized_minima:
                if edge[field] == value: coverage_counts[cid] += 1; hit.append(cid)
            feasible = bounds_possible() and visit(utility + edge["utility"])
            for cid in hit: coverage_counts[cid] -= 1
            if edge["source_exam_id"] is not None: source_exam_counts[edge["source_exam_id"]] -= 1
            if edge["source_kind"] == "ordinary": chapter_counts[edge["chapter"]] -= 1
            source_counts[edge["source_kind"]] -= 1
            used_candidates.remove(edge["candidate_id"]); used_content.remove(edge["content_hash"]); chosen.pop()
            if feasible:
                remaining.add(slot)
                return True
        remaining.add(slot)
        return False

    if visit(0):
        return None
    if best is None:
        source_capacity = {
            source: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge["source_kind"] == source})
            for source in source_targets
        }
        chapter_capacity = {
            chapter: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge["source_kind"] == "ordinary" and edge["chapter"] == chapter})
            for chapter in chapter_targets
        }
        coverage_capacity = {
            cid: len({edge["content_hash"] for edges in edges_by_slot.values() for edge in edges if edge[field] == value})
            for cid, field, value, _minimum in normalized_minima
        }
        # Prefer a deterministic smallest direct hard-constraint core.  Forced
        # unrelated singleton slots are not reported as affected merely because
        # the full paper is infeasible.
        direct_cores: list[tuple[int, tuple[str, ...], str, int]] = []
        def core_slots(predicate: Any) -> tuple[str, ...]:
            relevant = tuple(sorted(
                slot for slot, domain in edges_by_slot.items()
                if _builtin_any(predicate(edge) for edge in domain) or len(domain) > 1
            ))
            return relevant or (min(slots),)
        for source, target in source_targets.items():
            shortage = target - source_capacity[source]
            if shortage > 0:
                members = core_slots(lambda edge, source=source: edge["source_kind"] == source)
                direct_cores.append((len(members), members, "source_exact_quota", shortage))
        for chapter, target in chapter_targets.items():
            shortage = target - chapter_capacity[chapter]
            if shortage > 0:
                members = core_slots(lambda edge, chapter=chapter: edge["source_kind"] == "ordinary" and edge["chapter"] == chapter)
                direct_cores.append((len(members), members, "ordinary_chapter_exact_quota", shortage))
        for cid, field, value, minimum in normalized_minima:
            shortage = minimum - coverage_capacity[cid]
            if shortage > 0:
                members = core_slots(lambda edge, field=field, value=value: edge[field] == value)
                direct_cores.append((len(members), members, cid, shortage))
        exam_resources: dict[str, set[str]] = {}
        no_exam_resources: set[str] = set()
        for domain in edges_by_slot.values():
            for edge in domain:
                if edge["source_kind"] != "past_exam_other":
                    continue
                if edge["source_exam_id"] is None:
                    no_exam_resources.add(edge["content_hash"])
                else:
                    exam_resources.setdefault(edge["source_exam_id"], set()).add(edge["content_hash"])
        diversity_capacity = len(no_exam_resources) + sum(min(max_source_exam, len(values)) for values in exam_resources.values())
        diversity_shortage = source_targets.get("past_exam_other", 0) - diversity_capacity
        if diversity_shortage > 0:
            members = core_slots(lambda edge: edge["source_kind"] == "past_exam_other")
            direct_cores.append((len(members), members, "source_exam_diversity", diversity_shortage))
        if direct_cores:
            _size, deficient_slots, core_constraint, deficiency = min(direct_cores, key=lambda item: (item[0], item[1], item[2]))
            constraint_ids = [core_constraint]
        else:
            deficient_slots = tuple(slots)
            deficiency = 1
            constraint_ids = ["source_exact_quota", "ordinary_chapter_exact_quota", "source_exam_diversity", *sorted(item[0] for item in normalized_minima)]
        core_edges = [edge for slot in deficient_slots for edge in edges_by_slot[slot]]
        body = {
            "version": WITNESS_VERSION, "kind": "constrained_min_cut", "constraint_hash": contract.constraint_hash,
            "graph_hash": graph.graph_hash, "deficient_slot_ids": list(deficient_slots),
            "reachable_candidate_ids": sorted({edge["candidate_id"] for edge in core_edges}),
            "reachable_content_hashes": sorted({edge["content_hash"] for edge in core_edges}),
            "deficiency": deficiency, "constraint_ids": constraint_ids,
            "counts": {
                "source_targets": source_targets, "source_capacity": source_capacity,
                "chapter_targets": chapter_targets, "chapter_capacity": chapter_capacity,
                "coverage_minima": {item[0]: item[3] for item in normalized_minima}, "coverage_capacity": coverage_capacity,
            },
            "actions": ["expand_only_affected_slots", "repair_versioned_quota_or_coverage_fact", "do_not_relax_constraints"],
        }
        raise Stage5InfeasibleError({**body, "witness_hash": _digest(body)})
    utility, _tie, selected = best
    assignment_fields = (
        "slot_id", "position", "candidate_id", "candidate_fact_hash", "content_hash",
        "source_kind", "chapter", "source_exam_id", "section_id",
        "question_type_family", "target_difficulty", "bucket_rank", "utility",
    )
    if not utility_mode:
        assignment_fields = assignment_fields[:-1] + ("score", "penalty", "utility")
    assignments = tuple({key: edge[key] for key in assignment_fields} for edge in selected)
    identity = {
        "contract_version": SELECTION_VERSION, "solver_version": SOLVER_VERSION,
        "stage3_contract_hash": contract.stage3_contract_hash, "candidate_facts_hash": contract.candidate_facts_hash,
        "projection_identity_hash": contract.projection_identity_hash, "profile_hash": c["profile_hash"], "exact_slot_hash": c["exact_slot_hash"],
        "graph_hash": graph.graph_hash, "constraint_hash": contract.constraint_hash, "score_identity_hash": score_hash,
        "assignments": list(assignments),
    }
    selection_hash = _digest(identity)
    payload = {**identity, "total_utility": utility, "selection_hash": selection_hash}
    raw = _canonical(payload)
    return Stage5Selection(SELECTION_VERSION, graph.graph_hash, contract.constraint_hash, score_hash, utility, selection_hash, assignments, raw)


def _verified_selection(
    value: Stage5Selection, contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph,
) -> dict[str, Any]:
    if not isinstance(value, Stage5Selection):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_selection_type_invalid", "待修复选择不是阶段 5 选择合同")
    payload = value.to_dict()
    if value.constraint_hash != contract.constraint_hash or value.graph_hash != graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_selection_identity_mismatch", "待修复选择未绑定基础图与全局约束")
    identity = {key: payload[key] for key in (
        "contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash",
        "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash",
        "constraint_hash", "score_identity_hash", "assignments",
    )}
    if (
        payload.get("contract_version") != SELECTION_VERSION
        or payload.get("selection_hash") != _digest(identity)
        or payload.get("selection_hash") != value.selection_hash
        or payload.get("total_utility") != value.total_utility
        or tuple(payload.get("assignments", ())) != value.assignments
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_selection_hash_mismatch", "待修复选择哈希不匹配")
    return payload


def _verified_witness(
    value: Mapping[str, Any], contract: Stage5GlobalConstraintContract, graph: FrozenCandidateEdgeGraph,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    witness = dict(_mapping(value, "repair_witness"))
    witness_hash = witness.pop("witness_hash", None)
    if (
        witness.get("version") != WITNESS_VERSION
        or witness.get("kind") not in {"hall_min_cut", "constrained_min_cut"}
        or witness.get("constraint_hash") != contract.constraint_hash
        or witness.get("graph_hash") != graph.graph_hash
        or _digest(witness) != _hash(witness_hash, "witness_hash")
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_witness_identity_mismatch", "修复见证未绑定基础图与全局约束")
    valid = {slot["slot_id"] for slot in contract.to_dict()["slots"]}
    affected = tuple(sorted(_text_list(witness.get("deficient_slot_ids"), "deficient_slot_ids")))
    if not affected or not set(affected).issubset(valid):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_slots_invalid", "修复见证槽位无效")
    verified = {**witness, "witness_hash": witness_hash}
    neutral_scores = [
        {"slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"], "score": 0, "penalty": 0}
        for edge in sorted(graph.to_dict()["edges"], key=lambda item: (item["slot_id"], item["candidate_id"]))
    ]
    try:
        solve_global_selection(contract, graph, score_receipt=_local_v2_score_receipt(contract, graph, neutral_scores, purpose="stage5-witness-verification-v2"))
    except Stage5InfeasibleError as exc:
        if exc.witness != verified:
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_not_reproducible", "修复见证不是基础图的稳定最小见证") from exc
    else:
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_witness_not_reproducible", "基础图可行，不能接受不可行见证")
    return verified, affected


def _validate_repair_expansion(
    contract: Stage5GlobalConstraintContract,
    base_graph: FrozenCandidateEdgeGraph,
    expanded_graph: FrozenCandidateEdgeGraph | None,
    allowed_slots: set[str],
) -> FrozenCandidateEdgeGraph:
    if expanded_graph is None:
        return base_graph
    if not isinstance(expanded_graph, FrozenCandidateEdgeGraph):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expanded_graph_type_invalid", "修复扩展图类型无效")
    base, expanded = base_graph.to_dict(), expanded_graph.to_dict()
    if _digest(expanded) != expanded_graph.graph_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_expanded_graph_hash_mismatch", "修复扩展图哈希不匹配")
    if base.get("graph_mode") != "initial":
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_second_expansion_forbidden", "阶段 5 修复只允许一个扩展版本")
    if (
        expanded.get("graph_mode") != "expanded"
        or expanded_graph.constraint_hash != contract.constraint_hash
        or expanded.get("candidate_facts_hash") != base.get("candidate_facts_hash")
        or expanded.get("projection_identity_hash") != base.get("projection_identity_hash")
        or expanded.get("effective_difficulty_hash") != base.get("effective_difficulty_hash")
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_expanded_graph_identity_mismatch", "修复扩展图依赖身份不匹配")
    expanded_slots = set(_text_list(expanded.get("expanded_slot_ids"), "expanded_slot_ids"))
    if not expanded_slots or not expanded_slots.issubset(allowed_slots):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_scope_invalid", "扩展只能发生在见证影响槽")
    def by_slot(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for edge in payload["edges"]:
            result.setdefault(edge["slot_id"], []).append(edge)
        return result
    before, after = by_slot(base), by_slot(expanded)
    if _builtin_any(expanded["candidates"].get(candidate_id) != candidate for candidate_id, candidate in base["candidates"].items()):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_candidate_mutated", "修复扩展不得改写基础候选事实")
    for slot_id in sorted(set(before) | set(after)):
        old, new = before.get(slot_id, []), after.get(slot_id, [])
        if new[:len(old)] != old or len(new) - len(old) > 2 or (slot_id not in expanded_slots and new != old):
            raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_not_additive", "修复扩展必须逐槽追加且每槽最多两个候选")
    n = contract.question_count
    candidate_union = {edge["candidate_id"] for edge in expanded["edges"]}
    if len(expanded["edges"]) > 6 * n or len(candidate_union) > 6 * n or set(expanded["candidates"]) != candidate_union:
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_expansion_budget_exceeded", "修复扩展超过 6N 冻结预算")
    return expanded_graph


def _repair_component(
    contract_payload: Mapping[str, Any], graph_payload: Mapping[str, Any], seeds: set[str],
) -> tuple[str, ...]:
    """Stable connectivity over every hard resource that can couple assignments."""
    slots = {slot["slot_id"] for slot in contract_payload["slots"]}
    edges_by_slot = {slot: [] for slot in slots}
    for edge in graph_payload["edges"]:
        edges_by_slot[edge["slot_id"]].append(edge)
    minima = contract_payload["coverage_policy"]["payload"]["minima"]
    features: dict[str, set[str]] = {slot: set() for slot in slots}
    for slot, edges in edges_by_slot.items():
        sources = {edge["source_kind"] for edge in edges}
        chapters = {edge["chapter"] for edge in edges if edge["source_kind"] == "ordinary"}
        for edge in edges:
            features[slot].add("candidate:" + edge["candidate_id"])
            features[slot].add("content:" + edge["content_hash"])
            features[slot].add("source:" + edge["source_kind"])
            if edge["source_kind"] == "ordinary":
                features[slot].add("chapter:" + str(edge["chapter"]))
            if edge["source_exam_id"] is not None:
                features[slot].add("exam:" + edge["source_exam_id"])
            for item in minima:
                if edge[item["field"]] == item["value"]:
                    features[slot].add("coverage:" + item["id"])
        # A fixed-category domain cannot transfer exact quota to another slot.
        if len(sources) == 1:
            features[slot].discard("source:" + next(iter(sources)))
        if len(chapters) == 1 and sources == {"ordinary"}:
            features[slot].discard("chapter:" + str(next(iter(chapters))))
        for item in minima:
            hits = {edge[item["field"]] == item["value"] for edge in edges}
            if hits != {False, True}:
                features[slot].discard("coverage:" + item["id"])
    owners: dict[str, set[str]] = {}
    for slot, values in features.items():
        for value in values:
            owners.setdefault(value, set()).add(slot)
    reached = set(seeds)
    pending = sorted(seeds)
    while pending:
        slot = pending.pop(0)
        neighbors = set().union(*(owners[value] for value in features.get(slot, ()))) if features.get(slot) else set()
        for neighbor in sorted(neighbors - reached):
            reached.add(neighbor)
            pending.append(neighbor)
    return tuple(sorted(reached))


def repair_global_selection(
    contract: Stage5GlobalConstraintContract,
    base_graph: FrozenCandidateEdgeGraph,
    *,
    score_receipt: Mapping[str, Any],
    witness: Mapping[str, Any] | None = None,
    selection: Stage5Selection | None = None,
    rejected_edge: Mapping[str, Any] | None = None,
    rejected_candidate_id: str | None = None,
    rejected_content_hash: str | None = None,
    expanded_graph: FrozenCandidateEdgeGraph | None = None,
) -> Stage5RepairResult:
    """Atomically exact-resolve the smallest hard-constraint dependency component.

    There are two closed input modes: an authenticated infeasibility witness, or
    an authenticated existing selection plus exactly one rejected edge/resource.
    No partial replacement is ever returned.
    """
    if not isinstance(contract, Stage5GlobalConstraintContract) or not isinstance(base_graph, FrozenCandidateEdgeGraph):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_input_type_invalid", "阶段 5 修复输入类型无效")
    if base_graph.constraint_hash != contract.constraint_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_base_graph_constraint_mismatch", "基础图未绑定全局约束")
    c, base = contract.to_dict(), base_graph.to_dict()
    if _digest(c) != contract.constraint_hash:
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_constraint_hash_mismatch", "全局约束合同哈希不匹配")
    if (
        _digest(base) != base_graph.graph_hash
        or base.get("constraint_hash") != contract.constraint_hash
        or base.get("candidate_facts_hash") != contract.candidate_facts_hash
        or base.get("projection_identity_hash") != contract.projection_identity_hash
    ):
        raise Stage5ContractError("exam_stage5_contract_hash_mismatch", "repair_base_graph_hash_mismatch", "基础图依赖身份或哈希不匹配")
    witness_value: dict[str, Any] | None = None
    witness_slots: tuple[str, ...] = ()
    if witness is not None:
        witness_value, witness_slots = _verified_witness(witness, contract, base_graph)
    rejection_values = [rejected_edge is not None, rejected_candidate_id is not None, rejected_content_hash is not None]
    witness_mode = witness_value is not None and selection is None and not _builtin_any(rejection_values)
    selection_mode = selection is not None and witness_value is None and sum(rejection_values) == 1
    if not (witness_mode or selection_mode):
        raise Stage5ContractError("exam_stage5_contract_invalid", "repair_binding_incomplete", "修复必须绑定可信见证，或绑定已有选择与一个明确拒绝资源")
    selection_payload = _verified_selection(selection, contract, base_graph) if selection is not None else None

    rejection: dict[str, Any] | None = None
    seeds = set(witness_slots)
    if selection_payload is not None:
        assignments = list(selection_payload["assignments"])
        if rejected_edge is not None:
            edge = _mapping(rejected_edge, "rejected_edge")
            if set(edge) != {"slot_id", "candidate_id"}:
                raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_edge_shape_invalid", "拒绝边必须只包含槽位与候选身份")
            rejection = {"kind": "edge", "slot_id": _text(edge["slot_id"], "rejected_slot_id"), "candidate_id": _text(edge["candidate_id"], "rejected_candidate_id")}
            matched = [item for item in assignments if item["slot_id"] == rejection["slot_id"] and item["candidate_id"] == rejection["candidate_id"]]
        elif rejected_candidate_id is not None:
            rejection = {"kind": "candidate_id", "candidate_id": _text(rejected_candidate_id, "rejected_candidate_id")}
            matched = [item for item in assignments if item["candidate_id"] == rejection["candidate_id"]]
        else:
            rejection = {"kind": "content_hash", "content_hash": _hash(rejected_content_hash, "rejected_content_hash")}
            matched = [item for item in assignments if item["content_hash"] == rejection["content_hash"]]
        if len(matched) != 1:
            raise Stage5ContractError("exam_stage5_contract_invalid", "rejected_assignment_not_selected", "拒绝资源不是现有选择中的唯一分配")
        seeds.add(matched[0]["slot_id"])

    initial_affected = set(_repair_component(c, base, seeds))
    active_graph = _validate_repair_expansion(contract, base_graph, expanded_graph, initial_affected)
    active = active_graph.to_dict()
    affected = _repair_component(c, active, seeds)
    utility_mode = isinstance(score_receipt, Mapping) and score_receipt.get("version") == UTILITY_RECEIPT_VERSION
    scores, _receipt_rejected_edges, active_score_hash = _score_receipt(score_receipt, active_graph, contract)

    fixed_assignments = [] if selection_payload is None else [item for item in selection_payload["assignments"] if item["slot_id"] not in set(affected)]
    fixed_assignment_hash = _digest({"base_selection_hash": selection.selection_hash if selection else None, "assignments": fixed_assignments})
    scope_body = {
        "base_graph_hash": base_graph.graph_hash,
        "repair_graph_hash": active_graph.graph_hash,
        "constraint_hash": contract.constraint_hash,
        "witness_hash": witness_value["witness_hash"] if witness_value else None,
        "rejection": rejection,
        "affected_slot_ids": list(affected),
        "fixed_assignment_hash": fixed_assignment_hash,
    }
    repair_scope_hash = _digest(scope_body)

    forbidden = rejection or {}
    fixed_by_slot = {item["slot_id"]: item for item in fixed_assignments}
    scoped_edges: list[dict[str, Any]] = []
    for edge in active["edges"]:
        if (edge["slot_id"], edge["candidate_id"]) in _receipt_rejected_edges:
            continue
        fixed = fixed_by_slot.get(edge["slot_id"])
        if fixed is not None and edge["candidate_id"] != fixed["candidate_id"]:
            continue
        if forbidden.get("kind") == "edge" and edge["slot_id"] == forbidden["slot_id"] and edge["candidate_id"] == forbidden["candidate_id"]:
            continue
        if forbidden.get("kind") == "candidate_id" and edge["candidate_id"] == forbidden["candidate_id"]:
            continue
        if forbidden.get("kind") == "content_hash" and edge["content_hash"] == forbidden["content_hash"]:
            continue
        scoped_edges.append(edge)
    scoped_candidates = {edge["candidate_id"]: active["candidates"][edge["candidate_id"]] for edge in scoped_edges}
    scoped_payload = {**active, "graph_mode": "repair-scope", "candidates": scoped_candidates, "edges": scoped_edges,
                      "base_graph_hash": base_graph.graph_hash, "repair_scope_hash": repair_scope_hash,
                      "fixed_assignment_hash": fixed_assignment_hash}
    scoped_raw = _canonical(scoped_payload)
    scoped_graph = FrozenCandidateEdgeGraph(CANDIDATE_GRAPH_VERSION, contract.constraint_hash, contract.candidate_facts_hash, "repair-scope", len(scoped_edges), hashlib.sha256(scoped_raw.encode()).hexdigest(), scoped_raw)
    scoped_scores = [
        {"slot_id": edge["slot_id"], "candidate_id": edge["candidate_id"], "score": scores[(edge["slot_id"], edge["candidate_id"])][0], "penalty": scores[(edge["slot_id"], edge["candidate_id"])][1]}
        for edge in sorted(scoped_edges, key=lambda item: (item["slot_id"], item["candidate_id"]))
    ]
    scoped_receipt = (
        _local_candidate_base_utility_receipt(scoped_graph, score_receipt)
        if utility_mode
        else _local_v2_score_receipt(
            contract, scoped_graph, scoped_scores, purpose="stage5-repair-score-v2"
        )
    )
    try:
        solved = solve_global_selection(
            contract, scoped_graph,
            score_receipt=scoped_receipt,
        )
    except Stage5InfeasibleError as exc:
        # The deficient core is computed from the filtered repair scope, but a
        # durable witness must bind the authoritative active graph rather than
        # the ephemeral repair-scope graph.
        witness_body = dict(exc.witness)
        witness_body.pop("witness_hash", None)
        witness_body["graph_hash"] = active_graph.graph_hash
        raise Stage5InfeasibleError({**witness_body, "witness_hash": _digest(witness_body)}) from exc

    # Rebind the complete proven assignment to the actual frozen repair graph.
    assignments = solved.assignments
    identity = {
        "contract_version": SELECTION_VERSION, "solver_version": SOLVER_VERSION,
        "stage3_contract_hash": contract.stage3_contract_hash, "candidate_facts_hash": contract.candidate_facts_hash,
        "projection_identity_hash": contract.projection_identity_hash, "profile_hash": c["profile_hash"], "exact_slot_hash": c["exact_slot_hash"],
        "graph_hash": active_graph.graph_hash, "constraint_hash": contract.constraint_hash, "score_identity_hash": active_score_hash,
        "assignments": list(assignments),
    }
    selection_hash = _digest(identity)
    replacement_payload = {**identity, "total_utility": solved.total_utility, "selection_hash": selection_hash}
    replacement_raw = _canonical(replacement_payload)
    replacement = Stage5Selection(SELECTION_VERSION, active_graph.graph_hash, contract.constraint_hash, active_score_hash, solved.total_utility, selection_hash, assignments, replacement_raw)
    receipt_body = {
        "version": REPAIR_VERSION, **scope_body, "repair_scope_hash": repair_scope_hash,
        "fixed_assignment_hash": fixed_assignment_hash,
        "base_selection_hash": selection.selection_hash if selection else None,
        "replacement_selection_hash": replacement.selection_hash,
    }
    repair_hash = _digest(receipt_body)
    receipt_raw = _canonical({**receipt_body, "repair_hash": repair_hash})
    return Stage5RepairResult(replacement, repair_hash, repair_scope_hash, fixed_assignment_hash, affected, receipt_raw)


__all__ = [
    "CANDIDATE_GRAPH_VERSION", "EFFECTIVE_DIFFICULTY_SET_VERSION", "EFFECTIVE_DIFFICULTY_VERSION", "EXACT_HISTORY_AUDIT_POLICY_VERSION", "GLOBAL_CONSTRAINT_VERSION", "RECENT_USAGE_AUDIT_POLICY_VERSION", "REPAIR_VERSION", "SELECTION_VERSION", "SOLVER_VERSION", "UTILITY_RECEIPT_VERSION", "WITNESS_VERSION",
    "FrozenCandidateEdgeGraph", "Stage5ContractError", "Stage5GlobalConstraintContract", "Stage5InfeasibleError", "Stage5RepairResult", "Stage5Selection",
    "build_candidate_edge_graph", "build_difficulty_defect_edges", "build_global_constraint_contract", "locate_candidate_graph_deficit", "repair_global_selection", "solve_global_selection", "supplement_priority_candidate_graph",
]
