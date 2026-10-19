"""Immutable selection contracts, identities and constraint normalization."""

from __future__ import annotations

import hashlib

import json

import math

import re

import unicodedata

from builtins import (
    any as _builtin_any,
)

from dataclasses import (
    dataclass,
)

from fractions import (
    Fraction,
)

from typing import (
    Any,
    Mapping,
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
