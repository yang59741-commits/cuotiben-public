"""Pure Stage 6 material, scoring-window and receipt contracts.

No persistence, worker, provider or AI policy lives here.  Provider calls are
represented only by opaque caller-owned callbacks in tests and later adapters.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .semantic_index import SemanticIndexContractError, semantic_content_hash
from .exam_generation_stage6_rubric import (
    FACTOR_APPLICABILITY_VERSION,
    FACTOR_CATALOG_HASH,
    RUBRIC_CATALOG_VERSION,
    RUBRIC_REQUEST_VERSION,
    RUBRIC_RESPONSE_VERSION,
    UTILITY_POLICY_VERSION,
    UTILITY_RECEIPT_VERSION,
    WINDOW_RUBRIC_REQUEST_VERSION,
    WINDOW_RUBRIC_RESPONSE_VERSION,
    CandidateBaseRubricError,
    build_graph_utility_receipt as build_candidate_base_graph_utility_receipt,
    build_utility_policy,
    build_utility_receipt as build_candidate_base_utility_receipt,
    build_window_rubric_request as render_candidate_base_rubric_request,
    evaluate_candidate_base_rubric_windows,
    explain_candidate_utility,
    parse_window_rubric_response as parse_candidate_base_rubric_response,
)


MATERIAL_RECEIPT_VERSION = "past-v3-stage6-material-preheat-v1"
WINDOW_VERSION = "past-v3-stage6-score-window-v1"
REQUEST_VERSION = "past-v3-stage6-score-request-v1"
RESPONSE_VERSION = "past-v3-stage6-score-response-v2"
WINDOW_RESULT_VERSION = "past-v3-stage6-window-score-result-v2"
SCORING_IDENTITY_VERSION = "past-v3-stage6-scoring-identity-v2"
SCORE_RECEIPT_VERSION = "local-score-v2"
AUDIT_RECEIPT_VERSION = "past-v3-stage6-score-audit-v2"
REJECTION_LEDGER_VERSION = "past-v3-stage6-rejection-ledger-v1"
BUDGET_VERSION = "past-v3-stage6-call-budget-v1"
STAGE4_VERSION = "past-v3-stage4-candidate-facts-v1"
STAGE5_GRAPH_VERSION = "past-v3-stage5-candidate-edge-graph-v1"

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_REASON_CODES = frozenset({"strong_match", "acceptable_match", "weak_match", "content_risk", "source_risk", "format_risk"})
_FORBIDDEN_KEYS = frozenset({
    "selected", "selection", "final_choice", "final_selection", "difficulty",
    "actual_difficulty", "target_difficulty", "adapted_content", "answer", "answers",
    "solution", "reasoning", "chain_of_thought", "答案", "解析", "推理", "难度",
})


class Stage6ContractError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = False


@dataclass(frozen=True, slots=True)
class MaterialPreheatReceipt:
    version: str
    graph_hash: str
    candidate_facts_hash: str
    candidate_count: int
    material_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _material_receipt(self)


@dataclass(frozen=True, slots=True)
class ScoringWindow:
    version: str
    phase: str
    graph_hash: str
    window_index: int
    slot_ids: tuple[str, ...]
    edge_count: int
    window_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _window_receipt(self)


@dataclass(frozen=True, slots=True)
class ScoreReceipt:
    version: str
    graph_hash: str
    score_set_hash: str
    audit_hash: str
    scores: tuple[Mapping[str, Any], ...]
    scoring_identity: Mapping[str, Any]
    _audit_json: str

    def to_stage5_dict(self) -> dict[str, Any]:
        identity = dict(self.scoring_identity)
        identity["windows"] = [{**dict(item), "slot_ids": list(item["slot_ids"])} for item in self.scoring_identity["windows"]]
        identity["rejected_edges"] = [dict(item) for item in self.scoring_identity["rejected_edges"]]
        return {"version": self.version, "graph_hash": self.graph_hash, "scoring_identity": identity, "scores": [dict(x) for x in self.scores], "hash": self.score_set_hash}

    def to_audit_dict(self) -> dict[str, Any]:
        return json.loads(self._audit_json)


@dataclass(frozen=True, slots=True)
class RejectionLedgerHead:
    version: str
    graph_hash: str
    entry_count: int
    head_hash: str


@dataclass(frozen=True, slots=True)
class WindowScoreResult:
    version: str
    graph_hash: str
    window_hash: str
    request_hash: str
    numeric_response_hash: str
    audit_response_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _window_score_result(self)


@dataclass(frozen=True, slots=True)
class CallBudgetState:
    version: str
    initial_limit: int
    expansion_limit: int
    correction_limit: int
    transport_retry_limit: int
    initial_calls: int = 0
    expansion_calls: int = 0
    correction_calls: int = 0
    transport_retries: int = 0


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_contract_invalid", "non_canonical_json", "阶段 6 合同不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Stage6ContractError("exam_stage6_contract_invalid", f"{field}_invalid", "阶段 6 依赖哈希无效")
    return value


def _text(value: object, field: str, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise Stage6ContractError("exam_stage6_contract_invalid", f"{field}_invalid", "阶段 6 文本字段无效")
    result = unicodedata.normalize("NFC", value).strip()
    if not result or len(result) > maximum:
        raise Stage6ContractError("exam_stage6_contract_invalid", f"{field}_invalid", "阶段 6 文本字段无效")
    return result


def _graph(value: object) -> dict[str, Any]:
    if not hasattr(value, "to_dict") or not hasattr(value, "graph_hash"):
        raise Stage6ContractError("exam_stage6_contract_invalid", "graph_type_invalid", "阶段 6 需要冻结的阶段 5 图")
    payload = value.to_dict()
    if not isinstance(payload, dict) or payload.get("contract_version") != STAGE5_GRAPH_VERSION or _digest(payload) != value.graph_hash:
        raise Stage6ContractError("exam_stage6_contract_hash_mismatch", "graph_hash_mismatch", "阶段 5 图身份不匹配")
    return payload


def _snapshot_and_summary(snapshot: str, content_hash: str) -> dict[str, Any]:
    try:
        payload = json.loads(snapshot, object_pairs_hook=_reject_duplicate_pairs)
    except Stage6ContractError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_schema_invalid", "冻结候选正文不是结构化快照") from exc
    fields = {"schema_version", "content", "figure_references", "options", "conditions"}
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("schema_version") != "semantic-candidate-content-snapshot-v1":
        raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_schema_invalid", "冻结候选正文结构版本或字段无效")
    content = payload.get("content")
    sequences: dict[str, list[str]] = {}
    if not isinstance(content, str) or not content.strip():
        raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_schema_invalid", "冻结候选正文结构内容无效")
    content = unicodedata.normalize("NFC", content).strip()
    for field in ("figure_references", "options", "conditions"):
        values = payload.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_schema_invalid", "冻结候选正文结构列表无效")
        sequences[field] = [unicodedata.normalize("NFC", value).strip() for value in values]
    try:
        replay_hash = semantic_content_hash(content, figure_references=sequences["figure_references"], options=sequences["options"], conditions=sequences["conditions"])
    except SemanticIndexContractError as exc:
        raise Stage6ContractError("exam_stage6_material_rejected", "content_identity_replay_failed", "冻结候选内容身份无法重放") from exc
    if replay_hash != content_hash:
        raise Stage6ContractError("exam_stage6_material_rejected", "content_hash_drift", "冻结候选结构正文与内容身份不一致")
    # Every structured component remains visible, with deterministic per-field bounds.
    return {
        "schema_version": "stage6-local-material-summary-v1",
        "content": " ".join(content.split())[:1200],
        "figure_references": [" ".join(value.split())[:256] for value in sequences["figure_references"][:16]],
        "options": [" ".join(value.split())[:512] for value in sequences["options"][:16]],
        "conditions": [" ".join(value.split())[:512] for value in sequences["conditions"][:16]],
        "source_counts": {field: len(sequences[field]) for field in ("figure_references", "options", "conditions")},
    }


def _valid_local_summary(value: object) -> bool:
    fields = {"schema_version", "content", "figure_references", "options", "conditions", "source_counts"}
    if not isinstance(value, Mapping) or set(value) != fields or value.get("schema_version") != "stage6-local-material-summary-v1":
        return False
    if not isinstance(value.get("content"), str) or not value["content"] or len(value["content"]) > 1200:
        return False
    limits = {"figure_references": 256, "options": 512, "conditions": 512}
    counts = value.get("source_counts")
    if not isinstance(counts, Mapping) or set(counts) != set(limits):
        return False
    for field, limit in limits.items():
        items = value.get(field)
        if not isinstance(items, list) or len(items) > 16 or any(not isinstance(item, str) or len(item) > limit for item in items):
            return False
        if type(counts.get(field)) is not int or counts[field] < len(items):
            return False
    return True


def _material_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, MaterialPreheatReceipt):
        raise Stage6ContractError("exam_stage6_contract_invalid", "material_receipt_type_invalid", "材料预热回执类型无效")
    try:
        payload = json.loads(value._payload_json, object_pairs_hook=_reject_duplicate_pairs)
    except Stage6ContractError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_contract_invalid", "material_receipt_payload_invalid", "材料预热回执载荷无效") from exc
    fields = {"version", "graph_hash", "candidate_facts_hash", "candidate_count", "materials", "ai_summary_calls"}
    if (
        not isinstance(payload, dict) or set(payload) != fields
        or payload.get("version") != MATERIAL_RECEIPT_VERSION
        or value.version != MATERIAL_RECEIPT_VERSION
        or payload.get("graph_hash") != value.graph_hash
        or payload.get("candidate_facts_hash") != value.candidate_facts_hash
        or payload.get("candidate_count") != value.candidate_count
        or type(value.candidate_count) is not int or value.candidate_count < 0
        or payload.get("ai_summary_calls") != 0
        or not isinstance(payload.get("materials"), list)
        or len(payload["materials"]) != value.candidate_count
        or _digest(payload) != value.material_hash
    ):
        raise Stage6ContractError("exam_stage6_contract_hash_mismatch", "material_receipt_integrity_mismatch", "材料预热回执完整性不匹配")
    _hash(value.graph_hash, "material_graph_hash")
    _hash(value.candidate_facts_hash, "material_candidate_facts_hash")
    _hash(value.material_hash, "material_hash")
    material_fields = {"candidate_id", "candidate_fact_hash", "content_hash", "content_snapshot_hash", "local_summary"}
    seen: set[str] = set()
    for item in payload["materials"]:
        if not isinstance(item, Mapping) or set(item) != material_fields:
            raise Stage6ContractError("exam_stage6_contract_invalid", "material_receipt_entry_invalid", "材料预热回执条目无效")
        candidate_id = _text(item.get("candidate_id"), "material_candidate_id")
        if candidate_id in seen:
            raise Stage6ContractError("exam_stage6_contract_invalid", "material_receipt_candidate_duplicate", "材料预热回执候选重复")
        seen.add(candidate_id)
        for field in ("candidate_fact_hash", "content_hash", "content_snapshot_hash"):
            _hash(item.get(field), field)
        if not _valid_local_summary(item.get("local_summary")):
            raise Stage6ContractError("exam_stage6_contract_invalid", "material_receipt_summary_invalid", "材料预热回执本地摘要无效")
    return payload


def _window_receipt(value: object) -> dict[str, Any]:
    if not isinstance(value, ScoringWindow):
        raise Stage6ContractError("exam_stage6_contract_invalid", "window_receipt_type_invalid", "评分窗口回执类型无效")
    try:
        payload = json.loads(value._payload_json, object_pairs_hook=_reject_duplicate_pairs)
    except Stage6ContractError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_contract_invalid", "window_receipt_payload_invalid", "评分窗口回执载荷无效") from exc
    fields = {"version", "phase", "graph_hash", "window_index", "slot_ids", "edges"}
    if (
        not isinstance(payload, dict) or set(payload) != fields
        or payload.get("version") != WINDOW_VERSION or value.version != WINDOW_VERSION
        or payload.get("phase") != value.phase or value.phase not in {"initial", "expansion"}
        or payload.get("graph_hash") != value.graph_hash
        or payload.get("window_index") != value.window_index
        or type(value.window_index) is not int or value.window_index < 1
        or not isinstance(payload.get("slot_ids"), list)
        or tuple(payload["slot_ids"]) != value.slot_ids
        or not 1 <= len(value.slot_ids) <= 4 or len(set(value.slot_ids)) != len(value.slot_ids)
        or not isinstance(payload.get("edges"), list)
        or len(payload["edges"]) != value.edge_count
        or type(value.edge_count) is not int or not 0 <= value.edge_count <= 16
        or _digest(payload) != value.window_hash
    ):
        raise Stage6ContractError("exam_stage6_contract_hash_mismatch", "window_receipt_integrity_mismatch", "评分窗口回执完整性不匹配")
    _hash(value.graph_hash, "window_graph_hash")
    _hash(value.window_hash, "window_hash")
    seen_edges: set[tuple[str, str]] = set()
    for edge in payload["edges"]:
        if not isinstance(edge, Mapping):
            raise Stage6ContractError("exam_stage6_contract_invalid", "window_receipt_edge_invalid", "评分窗口冻结边无效")
        key = (edge.get("slot_id"), edge.get("candidate_id"))
        if key[0] not in value.slot_ids or not isinstance(key[1], str) or key in seen_edges:
            raise Stage6ContractError("exam_stage6_contract_invalid", "window_receipt_edge_invalid", "评分窗口冻结边身份无效")
        seen_edges.add(key)
    return payload


def build_material_preheat_receipt(*, graph: object, stage4: Mapping[str, Any], question_count: int, base_graph: object | None = None) -> MaterialPreheatReceipt:
    """Read only candidates actually present in Stage 5 edges and fail atomically."""

    if type(question_count) is not int or question_count < 1:
        raise Stage6ContractError("exam_stage6_contract_invalid", "question_count_invalid", "题数无效")
    current = _graph(graph)
    base = _graph(base_graph) if base_graph is not None else None
    if current.get("candidate_facts_hash") != _digest(stage4) or stage4.get("contract_version") != STAGE4_VERSION:
        raise Stage6ContractError("exam_stage6_contract_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 候选事实身份不匹配")
    if current.get("graph_mode") == "expanded" and base is None:
        raise Stage6ContractError("exam_stage6_contract_invalid", "expanded_base_graph_missing", "扩展预热必须绑定初始图")
    if current.get("graph_mode") not in {"initial", "expanded"}:
        raise Stage6ContractError("exam_stage6_contract_invalid", "graph_mode_invalid", "冻结候选图模式无效")
    if base is not None and (base.get("candidate_facts_hash") != current.get("candidate_facts_hash") or base.get("graph_mode") != "initial" or current.get("graph_mode") != "expanded"):
        raise Stage6ContractError("exam_stage6_contract_invalid", "base_graph_identity_invalid", "扩展预热基础图无效")
    current_ids = {edge.get("candidate_id") for edge in current.get("edges", [])}
    base_ids = {edge.get("candidate_id") for edge in base.get("edges", [])} if base else set()
    if None in current_ids or None in base_ids:
        raise Stage6ContractError("exam_stage6_contract_invalid", "edge_candidate_identity_invalid", "冻结边候选身份无效")
    if base is None and len(current_ids) > 4 * question_count:
        raise Stage6ContractError("exam_stage6_budget_exceeded", "initial_candidate_union_exceeded", "初始预热候选并集超过 4N")
    if len(base_ids | current_ids) > 6 * question_count:
        raise Stage6ContractError("exam_stage6_budget_exceeded", "expanded_candidate_union_exceeded", "扩展预热候选总并集超过 6N")
    rows = stage4.get("candidates")
    if not isinstance(rows, list):
        raise Stage6ContractError("exam_stage6_contract_invalid", "candidate_rows_invalid", "阶段 4 候选集合无效")
    index: dict[str, Mapping[str, Any]] = {}
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise Stage6ContractError("exam_stage6_contract_invalid", "candidate_row_invalid", "阶段 4 候选行无效")
        candidate_id = raw.get("candidate_id")
        if candidate_id in index:
            raise Stage6ContractError("exam_stage6_contract_invalid", "candidate_id_duplicate", "候选身份重复")
        if isinstance(candidate_id, str):
            index[candidate_id] = raw
    required_ids = base_ids | current_ids
    edge_facts = {(e.get("candidate_id"), e.get("candidate_fact_hash"), e.get("content_hash")) for source in ([base, current] if base else [current]) for e in source.get("edges", [])}
    materials: list[dict[str, Any]] = []
    for candidate_id in sorted(required_ids):
        raw = index.get(candidate_id)
        if raw is None:
            raise Stage6ContractError("exam_stage6_material_rejected", "candidate_snapshot_missing", "冻结候选正文缺失")
        snapshot = raw.get("content_snapshot")
        if not isinstance(snapshot, str) or not snapshot.strip():
            raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_missing", "冻结候选正文缺失")
        snapshot_hash = _digest(snapshot)
        if raw.get("content_snapshot_hash") != snapshot_hash:
            raise Stage6ContractError("exam_stage6_material_rejected", "content_snapshot_hash_drift", "冻结候选正文哈希漂移")
        content_hash = _hash(raw.get("content_hash"), "content_hash")
        fact_hash = _digest(raw)
        matching = {(fact, content) for cid, fact, content in edge_facts if cid == candidate_id}
        if matching != {(fact_hash, content_hash)}:
            raise Stage6ContractError("exam_stage6_material_rejected", "candidate_content_identity_drift", "候选正文身份与冻结边不一致")
        materials.append({"candidate_id": candidate_id, "candidate_fact_hash": fact_hash, "content_hash": content_hash, "content_snapshot_hash": snapshot_hash, "local_summary": _snapshot_and_summary(snapshot, content_hash)})
    payload = {"version": MATERIAL_RECEIPT_VERSION, "graph_hash": graph.graph_hash, "candidate_facts_hash": current["candidate_facts_hash"], "candidate_count": len(materials), "materials": materials, "ai_summary_calls": 0}
    raw = _canonical(payload)
    return MaterialPreheatReceipt(MATERIAL_RECEIPT_VERSION, graph.graph_hash, current["candidate_facts_hash"], len(materials), hashlib.sha256(raw.encode()).hexdigest(), raw)


def build_scoring_windows(*, graph: object, question_count: int, phase: str = "initial", base_graph: object | None = None, affected_slot_ids: Sequence[str] = ()) -> tuple[ScoringWindow, ...]:
    current = _graph(graph)
    if type(question_count) is not int or question_count < 1 or phase not in {"initial", "expansion"}:
        raise Stage6ContractError("exam_stage6_contract_invalid", "window_input_invalid", "评分窗口输入无效")
    edges = current.get("edges")
    if not isinstance(edges, list):
        raise Stage6ContractError("exam_stage6_contract_invalid", "graph_edges_invalid", "冻结边集合无效")
    by_slot: dict[str, list[dict[str, Any]]] = {f"slot-{i:04d}": [] for i in range(1, question_count + 1)}
    for edge in edges:
        slot = edge.get("slot_id") if isinstance(edge, Mapping) else None
        if slot not in by_slot or type(edge.get("position")) is not int:
            raise Stage6ContractError("exam_stage6_contract_invalid", "edge_position_invalid", "冻结边位置身份无效")
        by_slot[slot].append(dict(edge))
    if phase == "initial":
        if current.get("graph_mode") != "initial":
            raise Stage6ContractError("exam_stage6_contract_invalid", "initial_graph_mode_invalid", "初始窗口必须绑定初始图")
        if len(edges) > 4 * question_count or any(len(items) > 4 for items in by_slot.values()):
            raise Stage6ContractError("exam_stage6_budget_exceeded", "initial_edge_limit_exceeded", "初始可评分图超过每槽 4 边或全卷 4N")
        slots = list(by_slot)
        selected = by_slot
    else:
        base = _graph(base_graph)
        if base.get("candidate_facts_hash") != current.get("candidate_facts_hash") or base.get("graph_mode") != "initial" or current.get("graph_mode") != "expanded":
            raise Stage6ContractError("exam_stage6_contract_hash_mismatch", "expansion_base_identity_mismatch", "扩展窗口基础图身份不匹配")
        if isinstance(affected_slot_ids, (str, bytes)) or len(set(affected_slot_ids)) != len(affected_slot_ids):
            raise Stage6ContractError("exam_stage6_contract_invalid", "affected_slots_invalid", "受影响槽位无效")
        slots = sorted(affected_slot_ids, key=lambda x: by_slot.get(x, [{}])[0].get("position", int(x.rsplit("-", 1)[-1])) if x in by_slot else question_count + 1)
        if any(slot not in by_slot for slot in slots):
            raise Stage6ContractError("exam_stage6_contract_invalid", "affected_slot_unknown", "受影响槽位不在冻结图中")
        old = {(e["slot_id"], e["candidate_id"]) for e in base.get("edges", [])}
        now = {(e["slot_id"], e["candidate_id"]) for e in edges}
        if not old.issubset(now):
            raise Stage6ContractError("exam_stage6_contract_invalid", "expansion_not_additive", "扩展图不得删除基础冻结边")
        new_edge_slots = {slot for slot, candidate in now - old}
        if not new_edge_slots.issubset(set(slots)):
            raise Stage6ContractError("exam_stage6_contract_invalid", "expansion_scope_invalid", "扩展图包含受影响槽位之外的新边")
        selected = {slot: [e for e in by_slot[slot] if (slot, e["candidate_id"]) not in old] for slot in slots}
        if len(edges) > 6 * question_count or any(len(items) > 2 for items in selected.values()):
            raise Stage6ContractError("exam_stage6_budget_exceeded", "expansion_edge_limit_exceeded", "扩展图超过每受影响槽新增 2 边或全卷 6N")
        slots = [slot for slot in slots if selected[slot]]
    windows: list[ScoringWindow] = []
    for offset in range(0, len(slots), 4):
        group = slots[offset:offset + 4]
        window_edges = sorted((e for slot in group for e in selected[slot]), key=lambda e: (e["position"], e["candidate_id"], e["candidate_fact_hash"]))
        if len(window_edges) > 16:
            raise Stage6ContractError("exam_stage6_budget_exceeded", "window_edge_limit_exceeded", "评分窗口超过 16 条边")
        payload = {"version": WINDOW_VERSION, "phase": phase, "graph_hash": graph.graph_hash, "window_index": len(windows) + 1, "slot_ids": group, "edges": window_edges}
        raw = _canonical(payload)
        windows.append(ScoringWindow(WINDOW_VERSION, phase, graph.graph_hash, len(windows) + 1, tuple(group), len(window_edges), hashlib.sha256(raw.encode()).hexdigest(), raw))
    expected = math.ceil(question_count / 4) if phase == "initial" else math.ceil(len(affected_slot_ids) / 4)
    if (phase == "initial" and len(windows) != expected) or (phase == "expansion" and len(windows) > expected):
        raise AssertionError("scoring window budget violated")
    return tuple(windows)


def render_scoring_request(*, window: ScoringWindow, material_receipt: MaterialPreheatReceipt, profile_hash: str, projection_identity_hash: str, prompt_version: str, model_id: str) -> str:
    window_payload = _window_receipt(window)
    material_payload = _material_receipt(material_receipt)
    if window.graph_hash != material_receipt.graph_hash:
        raise Stage6ContractError("exam_stage6_contract_invalid", "request_binding_invalid", "评分请求绑定无效")
    materials = {x["candidate_id"]: x for x in material_payload["materials"]}
    edges = []
    for edge in window_payload["edges"]:
        material = materials.get(edge["candidate_id"])
        if material is None:
            raise Stage6ContractError("exam_stage6_contract_invalid", "request_material_missing", "评分请求缺少冻结材料")
        edges.append({"slot_id": edge["slot_id"], "position": edge["position"], "candidate_id": edge["candidate_id"], "candidate_fact_hash": edge["candidate_fact_hash"], "content_hash": edge["content_hash"], "source_kind": edge["source_kind"], "chapter": edge["chapter"], "section_id": edge["section_id"], "question_type_family": edge["question_type_family"], "bucket_rank": edge["bucket_rank"], "content_snapshot_hash": material["content_snapshot_hash"], "local_summary": material["local_summary"]})
    body = {"version": REQUEST_VERSION, "window_hash": window.window_hash, "graph_hash": window.graph_hash, "candidate_facts_hash": material_receipt.candidate_facts_hash, "material_hash": material_receipt.material_hash, "profile_hash": _hash(profile_hash, "profile_hash"), "projection_identity_hash": _hash(projection_identity_hash, "projection_identity_hash"), "prompt_version": _text(prompt_version, "prompt_version", 128), "model_id": _text(model_id, "model_id", 128), "candidate_material_trust": "untrusted_data", "edges": edges}
    body["request_hash"] = _digest(body)
    return _canonical(body)


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Stage6ContractError("exam_stage6_response_invalid", "duplicate_json_key", "评分响应包含重复 JSON 键")
        result[key] = value
    return result


def _scan_forbidden(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if unicodedata.normalize("NFC", str(key)).strip().casefold() in _FORBIDDEN_KEYS:
                raise Stage6ContractError("exam_stage6_response_invalid", "forbidden_decision_field", "评分响应越权输出选择、难度、答案或推理字段")
            _scan_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _scan_forbidden(item)


def _window_score_result(value: object) -> dict[str, Any]:
    if not isinstance(value, WindowScoreResult):
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_type_invalid", "窗口评分结果类型无效")
    try:
        payload = json.loads(value._payload_json, object_pairs_hook=_reject_duplicate_pairs)
    except Stage6ContractError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_payload_invalid", "窗口评分结果载荷无效") from exc
    fields = {
        "version", "phase", "window_index", "graph_hash", "window_hash", "request_hash",
        "slot_ids",
        "model_id", "prompt_version", "candidate_facts_hash", "material_hash", "profile_hash",
        "projection_identity_hash", "response_schema_version", "scores", "numeric_response_hash",
        "audit_response_hash",
    }
    identity_fields = fields - {"scores", "numeric_response_hash", "audit_response_hash", "version"}
    if not isinstance(payload, Mapping) or set(payload) != fields or payload.get("version") != WINDOW_RESULT_VERSION:
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_shape_invalid", "窗口评分结果字段无效")
    identity = {field: payload[field] for field in identity_fields}
    slot_ids = payload.get("slot_ids")
    if not isinstance(slot_ids, list) or not 1 <= len(slot_ids) <= 4 or len(set(slot_ids)) != len(slot_ids) or any(not isinstance(item, str) or re.fullmatch(r"slot-[0-9]{4}", item) is None for item in slot_ids):
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_slots_invalid", "窗口评分结果槽位身份无效")
    scores = payload.get("scores")
    if not isinstance(scores, list):
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_scores_invalid", "窗口评分结果分数无效")
    numeric, audit, seen = [], [], set()
    for item in scores:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id", "score", "penalty", "reason_codes"}:
            raise Stage6ContractError("exam_stage6_response_invalid", "window_result_score_shape_invalid", "窗口评分条目字段无效")
        key = (item.get("slot_id"), item.get("candidate_id"))
        score, penalty, reasons = item.get("score"), item.get("penalty"), item.get("reason_codes")
        if key in seen or not all(isinstance(x, str) and x for x in key) or type(score) is not int or not 0 <= score <= 100 or type(penalty) is not int or not 0 <= penalty <= score:
            raise Stage6ContractError("exam_stage6_response_invalid", "window_result_score_invalid", "窗口评分条目数值或身份无效")
        if not isinstance(reasons, list) or not reasons or reasons != sorted(reasons) or len(set(reasons)) != len(reasons) or any(code not in _REASON_CODES for code in reasons):
            raise Stage6ContractError("exam_stage6_response_invalid", "window_result_reasons_invalid", "窗口评分原因码无效")
        seen.add(key)
        if key[0] not in slot_ids:
            raise Stage6ContractError("exam_stage6_response_invalid", "window_result_score_slot_invalid", "窗口评分条目不属于绑定槽位")
        number = {"slot_id": key[0], "candidate_id": key[1], "score": score, "penalty": penalty}
        numeric.append(number)
        audit.append({**number, "reason_codes": reasons})
    numeric.sort(key=lambda x: (x["slot_id"], x["candidate_id"]))
    audit.sort(key=lambda x: (x["slot_id"], x["candidate_id"]))
    numeric_hash = _digest({"version": WINDOW_RESULT_VERSION, "identity": identity, "scores": numeric})
    audit_hash = _digest({"version": WINDOW_RESULT_VERSION, "identity": identity, "numeric_response_hash": numeric_hash, "scores": audit})
    if (
        value.version != WINDOW_RESULT_VERSION or value.graph_hash != payload["graph_hash"]
        or value.window_hash != payload["window_hash"] or value.request_hash != payload["request_hash"]
        or value.numeric_response_hash != payload["numeric_response_hash"] or value.audit_response_hash != payload["audit_response_hash"]
        or payload["numeric_response_hash"] != numeric_hash or payload["audit_response_hash"] != audit_hash
        or payload["scores"] != audit
    ):
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_integrity_mismatch", "窗口评分结果完整性不匹配")
    for field in ("graph_hash", "window_hash", "request_hash", "candidate_facts_hash", "material_hash", "profile_hash", "projection_identity_hash"):
        _hash(payload[field], field)
    if payload["phase"] not in {"initial", "expansion"} or type(payload["window_index"]) is not int or payload["window_index"] < 1:
        raise Stage6ContractError("exam_stage6_response_invalid", "window_result_identity_invalid", "窗口评分结果来源身份无效")
    _text(payload["response_schema_version"], "response_schema_version", 128)
    _text(payload["model_id"], "model_id", 128)
    _text(payload["prompt_version"], "prompt_version", 128)
    return dict(payload)


def parse_scoring_response(raw_json: str, *, request_json: str, window: ScoringWindow) -> WindowScoreResult:
    window_payload = _window_receipt(window)
    try:
        response = json.loads(raw_json, object_pairs_hook=_reject_duplicate_pairs)
        request = json.loads(request_json, object_pairs_hook=_reject_duplicate_pairs)
    except Stage6ContractError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6ContractError("exam_stage6_response_invalid", "json_invalid", "评分响应不是严格 JSON") from exc
    request_fields = {"version", "window_hash", "graph_hash", "candidate_facts_hash", "material_hash", "profile_hash", "projection_identity_hash", "prompt_version", "model_id", "candidate_material_trust", "edges", "request_hash"}
    if not isinstance(request, Mapping) or set(request) != request_fields or request.get("version") != REQUEST_VERSION or request.get("window_hash") != window.window_hash or request.get("graph_hash") != window.graph_hash or request.get("candidate_material_trust") != "untrusted_data":
        raise Stage6ContractError("exam_stage6_response_invalid", "request_binding_invalid", "评分请求自身字段或窗口绑定无效")
    request_body = {key: request[key] for key in request_fields - {"request_hash"}}
    if request.get("request_hash") != _digest(request_body):
        raise Stage6ContractError("exam_stage6_response_invalid", "request_hash_mismatch", "评分请求哈希无法重算")
    window_edges = {(e["slot_id"], e["candidate_id"]): e for e in window_payload["edges"]}
    request_edges = request.get("edges")
    request_edge_fields = {"slot_id", "position", "candidate_id", "candidate_fact_hash", "content_hash", "source_kind", "chapter", "section_id", "question_type_family", "bucket_rank", "content_snapshot_hash", "local_summary"}
    if not isinstance(request_edges, list) or len(request_edges) != len(window_edges):
        raise Stage6ContractError("exam_stage6_response_invalid", "request_edges_invalid", "评分请求冻结边集合无效")
    seen_request_edges = set()
    for edge in request_edges:
        if not isinstance(edge, Mapping) or set(edge) != request_edge_fields:
            raise Stage6ContractError("exam_stage6_response_invalid", "request_edges_invalid", "评分请求冻结边字段无效")
        key = (edge.get("slot_id"), edge.get("candidate_id"))
        frozen_edge = window_edges.get(key)
        if key in seen_request_edges or frozen_edge is None or any(edge.get(field) != frozen_edge.get(field) for field in ("position", "candidate_fact_hash", "content_hash", "source_kind", "chapter", "section_id", "question_type_family", "bucket_rank")):
            raise Stage6ContractError("exam_stage6_response_invalid", "request_edges_invalid", "评分请求边未绑定冻结窗口")
        seen_request_edges.add(key)
        _hash(edge.get("content_snapshot_hash"), "content_snapshot_hash")
        if not _valid_local_summary(edge.get("local_summary")):
            raise Stage6ContractError("exam_stage6_response_invalid", "request_edges_invalid", "评分请求缺少本地结构摘要")
    _scan_forbidden(response)
    if not isinstance(response, Mapping) or set(response) != {"version", "request_hash", "scores"} or response.get("version") != RESPONSE_VERSION or response.get("request_hash") != request.get("request_hash"):
        raise Stage6ContractError("exam_stage6_response_invalid", "response_shape_invalid", "评分响应字段或请求绑定无效")
    if not isinstance(response["scores"], list):
        raise Stage6ContractError("exam_stage6_response_invalid", "scores_invalid", "评分响应条目无效")
    frozen = {(e["slot_id"], e["candidate_id"]) for e in window_payload["edges"]}
    parsed: dict[tuple[str, str], dict[str, Any]] = {}
    for item in response["scores"]:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id", "score", "penalty", "reason_codes"}:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_shape_invalid", "评分条目字段不符合白名单")
        key = (item["slot_id"], item["candidate_id"])
        score, penalty, reasons = item["score"], item["penalty"], item["reason_codes"]
        if key not in frozen or key in parsed:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_edge_invalid", "评分条目重复或不是冻结边")
        if type(score) is not int or not 0 <= score <= 100 or type(penalty) is not int or not 0 <= penalty <= score:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_value_invalid", "评分必须是范围内整数")
        if not isinstance(reasons, list) or not reasons or any(not isinstance(x, str) or x not in _REASON_CODES for x in reasons) or len(set(reasons)) != len(reasons):
            raise Stage6ContractError("exam_stage6_response_invalid", "reason_codes_invalid", "评分原因码无效")
        parsed[key] = {"slot_id": key[0], "candidate_id": key[1], "score": score, "penalty": penalty, "reason_codes": sorted(reasons)}
    if set(parsed) != frozen:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_edges_incomplete", "任一窗口必须完整返回全部冻结边评分")
    scores = [parsed[key] for key in sorted(parsed)]
    identity = {
        "phase": window.phase, "window_index": window.window_index, "graph_hash": window.graph_hash,
        "slot_ids": list(window.slot_ids),
        "window_hash": window.window_hash, "request_hash": request["request_hash"], "model_id": request["model_id"],
        "prompt_version": request["prompt_version"], "candidate_facts_hash": request["candidate_facts_hash"],
        "material_hash": request["material_hash"], "profile_hash": request["profile_hash"],
        "projection_identity_hash": request["projection_identity_hash"], "response_schema_version": RESPONSE_VERSION,
    }
    numeric = [{key: item[key] for key in ("slot_id", "candidate_id", "score", "penalty")} for item in scores]
    numeric_hash = _digest({"version": WINDOW_RESULT_VERSION, "identity": identity, "scores": numeric})
    audit_hash = _digest({"version": WINDOW_RESULT_VERSION, "identity": identity, "numeric_response_hash": numeric_hash, "scores": scores})
    payload = {"version": WINDOW_RESULT_VERSION, **identity, "scores": scores, "numeric_response_hash": numeric_hash, "audit_response_hash": audit_hash}
    raw = _canonical(payload)
    return WindowScoreResult(WINDOW_RESULT_VERSION, window.graph_hash, window.window_hash, request["request_hash"], numeric_hash, audit_hash, raw)


def build_score_receipt(
    *, graph: object, parsed_windows: Sequence[WindowScoreResult], base_graph: object | None = None,
    rejected_edges: Sequence[Mapping[str, str]] = (), rejection_ledger_head_hash: str = "0" * 64,
) -> ScoreReceipt:
    graph_payload = _graph(graph)
    frozen = {(e["slot_id"], e["candidate_id"]) for e in graph_payload["edges"]}
    expanded = graph_payload.get("graph_mode") == "expanded"
    if expanded:
        if base_graph is None:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_base_graph_missing", "扩展评分回执必须绑定初始图")
        base_payload = _graph(base_graph)
        if base_payload.get("graph_mode") != "initial" or any(base_payload.get(field) != graph_payload.get(field) for field in ("constraint_hash", "candidate_facts_hash", "projection_identity_hash")):
            raise Stage6ContractError("exam_stage6_response_invalid", "score_base_graph_identity_invalid", "扩展评分基础图身份无效")
        base_by_key = {(e["slot_id"], e["candidate_id"]): e for e in base_payload["edges"]}
        current_by_key = {(e["slot_id"], e["candidate_id"]): e for e in graph_payload["edges"]}
        if not set(base_by_key).issubset(current_by_key) or any(current_by_key[key] != edge for key, edge in base_by_key.items()):
            raise Stage6ContractError("exam_stage6_response_invalid", "score_expansion_not_additive", "扩展图不是初始图的严格追加")
        base_keys = set(base_by_key)
        new_keys = frozen - base_keys
        additions_by_slot: dict[str, int] = {}
        for slot_id, _candidate_id in new_keys:
            additions_by_slot[slot_id] = additions_by_slot.get(slot_id, 0) + 1
        if any(count > 2 for count in additions_by_slot.values()):
            raise Stage6ContractError("exam_stage6_response_invalid", "score_expansion_budget_exceeded", "扩展评分图超过每槽新增 2 边或全卷 6N")
    else:
        if base_graph is not None:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_base_graph_forbidden", "初始评分回执不得绑定基础图")
        base_payload, base_keys, new_keys = graph_payload, frozen, set()
    rejected: set[tuple[str, str]] = set()
    if isinstance(rejected_edges, (str, bytes)) or not isinstance(rejected_edges, Sequence):
        raise Stage6ContractError("exam_stage6_response_invalid", "rejected_edges_invalid", "显式拒绝边集合无效")
    for item in rejected_edges:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id"}:
            raise Stage6ContractError("exam_stage6_response_invalid", "rejected_edge_shape_invalid", "显式拒绝边字段无效")
        key = (_text(item.get("slot_id"), "rejected_slot_id"), _text(item.get("candidate_id"), "rejected_candidate_id"))
        if key in rejected or key not in new_keys:
            raise Stage6ContractError("exam_stage6_response_invalid", "rejected_edge_invalid", "只能拒绝扩展新增边")
        rejected.add(key)
    ledger_hash = _hash(rejection_ledger_head_hash, "rejection_ledger_head_hash")
    if (bool(rejected) and ledger_hash == "0" * 64) or (not rejected and ledger_hash != "0" * 64):
        raise Stage6ContractError("exam_stage6_response_invalid", "rejection_ledger_binding_invalid", "拒绝边与拒绝账本头不一致")
    collected: dict[tuple[str, str], Mapping[str, Any]] = {}
    window_payloads = [_window_score_result(window) for window in parsed_windows]
    if not window_payloads:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_windows_missing", "评分窗口结果缺失")
    source_fields = ("model_id", "prompt_version", "candidate_facts_hash", "profile_hash", "projection_identity_hash", "response_schema_version")
    source = {field: window_payloads[0][field] for field in source_fields}
    if source["candidate_facts_hash"] != graph_payload.get("candidate_facts_hash") or source["projection_identity_hash"] != graph_payload.get("projection_identity_hash"):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_upstream_identity_mismatch", "评分窗口未绑定候选事实或投影权威身份")
    window_keys = set()
    phase_counts = {"initial": 0, "expansion": 0}
    for window in window_payloads:
        expected_graph_hash = base_graph.graph_hash if expanded and window["phase"] == "initial" else graph.graph_hash
        if window["graph_hash"] != expected_graph_hash or (not expanded and window["phase"] != "initial") or any(window[field] != source[field] for field in source_fields):
            raise Stage6ContractError("exam_stage6_response_invalid", "score_window_source_mismatch", "评分窗口结果并非同源")
        phase_counts[window["phase"]] += 1
        window_key = (window["phase"], window["window_index"])
        if window_key in window_keys:
            raise Stage6ContractError("exam_stage6_response_invalid", "score_window_duplicate", "评分窗口结果重复")
        window_keys.add(window_key)
        for item in window["scores"]:
            key = (item.get("slot_id"), item.get("candidate_id"))
            allowed = base_keys if window["phase"] == "initial" else new_keys
            if key not in allowed or key in collected or key in rejected:
                raise Stage6ContractError("exam_stage6_response_invalid", "score_set_edge_invalid", "评分集合存在重复或非冻结边")
            score, penalty, reasons = item.get("score"), item.get("penalty"), item.get("reason_codes")
            if type(score) is not int or not 0 <= score <= 100 or type(penalty) is not int or not 0 <= penalty <= score:
                raise Stage6ContractError("exam_stage6_response_invalid", "score_set_value_invalid", "评分集合数值无效")
            if not isinstance(reasons, list) or not reasons or any(not isinstance(x, str) or x not in _REASON_CODES for x in reasons) or len(set(reasons)) != len(reasons):
                raise Stage6ContractError("exam_stage6_response_invalid", "score_set_reasons_invalid", "评分集合原因码无效")
            collected[key] = item
    initial_windows = sorted((window for window in window_payloads if window["phase"] == "initial"), key=lambda item: item["window_index"])
    if [window["window_index"] for window in initial_windows] != list(range(1, len(initial_windows) + 1)):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_index_invalid", "初始评分窗口索引不连续")
    initial_slot_ids = [slot_id for window in initial_windows for slot_id in window["slot_ids"]]
    if not initial_slot_ids or len(set(initial_slot_ids)) != len(initial_slot_ids):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "初始评分窗口槽位缺失或重复")
    expected_initial_slots = [f"slot-{index:04d}" for index in range(1, len(initial_slot_ids) + 1)]
    expected_initial_groups = [expected_initial_slots[index:index + 4] for index in range(0, len(expected_initial_slots), 4)]
    if initial_slot_ids != expected_initial_slots or [window["slot_ids"] for window in initial_windows] != expected_initial_groups:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "初始评分窗口槽位必须规范连续且按四题分组")
    question_count = len(initial_slot_ids)
    base_edge_slots = {edge["slot_id"] for edge in base_payload["edges"]}
    if not base_edge_slots.issubset(set(initial_slot_ids)):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "初始评分窗口未覆盖图中的全部槽位")
    if len(base_payload["edges"]) > 4 * question_count:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_graph_budget_exceeded", "初始评分图槽位或 4N 边预算无效")
    if expanded and len(graph_payload["edges"]) > 6 * question_count:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_expansion_budget_exceeded", "扩展评分图超过全卷 6N")
    expansion_windows = sorted((window for window in window_payloads if window["phase"] == "expansion"), key=lambda item: item["window_index"])
    if [window["window_index"] for window in expansion_windows] != list(range(1, len(expansion_windows) + 1)):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_index_invalid", "扩展评分窗口索引不连续")
    expansion_slot_ids = [slot_id for window in expansion_windows for slot_id in window["slot_ids"]]
    affected_slots = sorted({slot for slot, _candidate in new_keys})
    if len(set(expansion_slot_ids)) != len(expansion_slot_ids) or not set(expansion_slot_ids).issubset(affected_slots):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "扩展评分窗口槽位重复或不属于新增边槽位")
    expected_expansion_groups = [expansion_slot_ids[index:index + 4] for index in range(0, len(expansion_slot_ids), 4)]
    if expansion_slot_ids != sorted(expansion_slot_ids) or [window["slot_ids"] for window in expansion_windows] != expected_expansion_groups:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "扩展评分窗口槽位必须规范排序并按四题分组")
    scored_new_slots = {slot for slot, candidate in collected if (slot, candidate) in new_keys}
    if scored_new_slots != set(expansion_slot_ids):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_slots_invalid", "扩展评分窗口槽位未精确绑定成功评分的新增边")
    scored = set(collected)
    if not base_keys.issubset(scored) or scored | rejected != frozen or scored & rejected:
        raise Stage6ContractError("exam_stage6_response_invalid", "score_set_incomplete", "基础评分、扩展评分与显式拒绝未精确覆盖扩展图")
    affected_count = len({slot for slot, _candidate in new_keys})
    if phase_counts["initial"] != math.ceil(question_count / 4) or phase_counts["expansion"] > math.ceil(affected_count / 4) or (expanded and new_keys - rejected and phase_counts["expansion"] == 0):
        raise Stage6ContractError("exam_stage6_response_invalid", "score_window_count_invalid", "评分窗口调用数不符合初始或扩展上限")
    numeric = [{"slot_id": k[0], "candidate_id": k[1], "score": collected[k]["score"], "penalty": collected[k]["penalty"]} for k in sorted(collected)]
    window_identities = [{key: window[key] for key in ("phase", "window_index", "graph_hash", "slot_ids", "window_hash", "request_hash", "material_hash", "numeric_response_hash")} for window in sorted(window_payloads, key=lambda x: (x["phase"], x["window_index"], x["window_hash"]))]
    normalized_rejected = [{"slot_id": slot, "candidate_id": candidate} for slot, candidate in sorted(rejected)]
    scoring_identity = {"version": SCORING_IDENTITY_VERSION, "constraint_hash": graph_payload["constraint_hash"], "base_graph_hash": base_graph.graph_hash if expanded else None, "rejection_ledger_head_hash": ledger_hash, "rejected_edges": normalized_rejected, **source, "windows": window_identities}
    score_body = {"version": SCORE_RECEIPT_VERSION, "graph_hash": graph.graph_hash, "scoring_identity": scoring_identity, "scores": numeric}
    score_hash = _digest(score_body)
    audit_scores = [{**item, "reason_codes": list(collected[(item["slot_id"], item["candidate_id"])]["reason_codes"])} for item in numeric]
    audit_body = {"version": AUDIT_RECEIPT_VERSION, "graph_hash": graph.graph_hash, "scoring_identity": scoring_identity, "score_set_hash": score_hash, "window_audit_hashes": [window["audit_response_hash"] for window in sorted(window_payloads, key=lambda x: (x["phase"], x["window_index"], x["window_hash"]))], "scores": audit_scores}
    audit_hash = _digest(audit_body)
    audit_raw = _canonical({**audit_body, "audit_hash": audit_hash})
    immutable_scores = tuple(MappingProxyType(dict(item)) for item in numeric)
    immutable_windows = tuple(MappingProxyType({**item, "slot_ids": tuple(item["slot_ids"])}) for item in window_identities)
    immutable_identity = MappingProxyType({**scoring_identity, "windows": immutable_windows, "rejected_edges": tuple(MappingProxyType(dict(item)) for item in normalized_rejected)})
    return ScoreReceipt(SCORE_RECEIPT_VERSION, graph.graph_hash, score_hash, audit_hash, immutable_scores, immutable_identity, audit_raw)


def append_rejection(*, head: RejectionLedgerHead | None, graph_hash: str, rejected_edges: Sequence[Mapping[str, str]], reason_code: str) -> tuple[RejectionLedgerHead, Mapping[str, Any]]:
    previous = "0" * 64 if head is None else head.head_hash
    count = 0 if head is None else head.entry_count
    graph_hash = _hash(graph_hash, "graph_hash")
    if head is not None and (head.version != REJECTION_LEDGER_VERSION or head.graph_hash != graph_hash):
        raise Stage6ContractError("exam_stage6_contract_invalid", "ledger_version_invalid", "拒绝账本版本无效")
    edges = []
    seen = set()
    for item in rejected_edges:
        if not isinstance(item, Mapping) or set(item) != {"slot_id", "candidate_id"}:
            raise Stage6ContractError("exam_stage6_contract_invalid", "rejected_edge_invalid", "拒绝边身份无效")
        key = (_text(item["slot_id"], "slot_id"), _text(item["candidate_id"], "candidate_id"))
        if key in seen:
            raise Stage6ContractError("exam_stage6_contract_invalid", "rejected_edge_duplicate", "拒绝边重复")
        seen.add(key)
        edges.append({"slot_id": key[0], "candidate_id": key[1]})
    if not edges:
        raise Stage6ContractError("exam_stage6_contract_invalid", "rejected_edges_empty", "拒绝账本条目不得为空")
    body = {"version": REJECTION_LEDGER_VERSION, "sequence": count + 1, "previous_head_hash": _hash(previous, "previous_head_hash"), "graph_hash": graph_hash, "reason_code": _text(reason_code, "reason_code", 128), "rejected_edges": sorted(edges, key=lambda x: (x["slot_id"], x["candidate_id"]))}
    entry_hash = _digest(body)
    entry = {**body, "entry_hash": entry_hash}
    return RejectionLedgerHead(REJECTION_LEDGER_VERSION, graph_hash, count + 1, entry_hash), entry


def new_call_budget(*, question_count: int, affected_slot_count: int = 0, transport_retry_limit: int = 0) -> CallBudgetState:
    if any(type(x) is not int or x < 0 for x in (question_count, affected_slot_count, transport_retry_limit)) or question_count < 1 or affected_slot_count > question_count:
        raise Stage6ContractError("exam_stage6_contract_invalid", "call_budget_input_invalid", "调用预算输入无效")
    return CallBudgetState(BUDGET_VERSION, math.ceil(question_count / 4), math.ceil(affected_slot_count / 4), 2, transport_retry_limit)


def consume_call(state: CallBudgetState, kind: str) -> CallBudgetState:
    if not isinstance(state, CallBudgetState) or state.version != BUDGET_VERSION:
        raise Stage6ContractError("exam_stage6_contract_invalid", "call_budget_state_invalid", "调用预算状态无效")
    fields = {"initial": ("initial_calls", state.initial_limit), "expansion": ("expansion_calls", state.expansion_limit), "correction": ("correction_calls", state.correction_limit), "transport_retry": ("transport_retries", state.transport_retry_limit)}
    if kind not in fields:
        raise Stage6ContractError("exam_stage6_contract_invalid", "call_kind_invalid", "调用类型无效")
    field, limit = fields[kind]
    if getattr(state, field) >= limit:
        raise Stage6ContractError("exam_stage6_call_budget_exceeded", f"{kind}_limit_exceeded", "阶段 6 调用预算已耗尽")
    return replace(state, **{field: getattr(state, field) + 1})


def provider_concurrency(capability: object) -> int:
    return min(4, capability) if type(capability) is int and capability > 0 else 1


__all__ = [
    "AUDIT_RECEIPT_VERSION", "BUDGET_VERSION", "CallBudgetState", "MATERIAL_RECEIPT_VERSION",
    "MaterialPreheatReceipt", "REJECTION_LEDGER_VERSION", "REQUEST_VERSION", "RESPONSE_VERSION",
    "RejectionLedgerHead", "SCORE_RECEIPT_VERSION", "SCORING_IDENTITY_VERSION", "ScoreReceipt", "ScoringWindow", "Stage6ContractError",
    "WINDOW_RESULT_VERSION", "WindowScoreResult",
    "append_rejection", "build_material_preheat_receipt", "build_score_receipt", "build_scoring_windows",
    "consume_call", "new_call_budget", "parse_scoring_response", "provider_concurrency", "render_scoring_request",
    # Candidate-base rubric is additive and separately versioned.  These
    # aliases expose the new contract without changing legacy receipt parsing.
    "CandidateBaseRubricError", "FACTOR_APPLICABILITY_VERSION", "FACTOR_CATALOG_HASH",
    "RUBRIC_CATALOG_VERSION", "RUBRIC_REQUEST_VERSION", "RUBRIC_RESPONSE_VERSION",
    "WINDOW_RUBRIC_REQUEST_VERSION", "WINDOW_RUBRIC_RESPONSE_VERSION",
    "UTILITY_POLICY_VERSION", "UTILITY_RECEIPT_VERSION",
    "render_candidate_base_rubric_request", "parse_candidate_base_rubric_response",
    "build_candidate_base_utility_receipt", "build_candidate_base_graph_utility_receipt",
    "evaluate_candidate_base_rubric_windows", "explain_candidate_utility", "build_utility_policy",
]
