"""Pure Stage 6 checkpoint-v3 and legacy single-paper migration decisions."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


CHECKPOINT_VERSION = "past-v3-stage6-checkpoint-v3"
LEGACY_VERSION = "past-exam-checkpoint-v1"
MIGRATION_DECISION_VERSION = "past-v3-stage6-legacy-p1-migration-v1"
MAX_CHECKPOINT_BYTES = 32_768
MAX_LEGACY_BYTES = 65_536
MAX_WINDOWS_PER_PHASE = 64
MAX_RECENT_HASHES = 256
MAX_MIGRATION_DECISION_BYTES = 24_576
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_HASH_FIELDS = {
    "single_paper_contract_hash", "request_hash", "stage3_contract_hash", "profile_hash",
    "projection_identity_hash", "candidate_facts_hash", "recent_snapshot_hash", "constraint_hash",
    "initial_graph_hash", "material_receipt_hash", "score_receipt_hash", "score_audit_hash",
    "rejection_ledger_head_hash", "selection_hash",
}
_BINDING_FIELDS = _BINDING_HASH_FIELDS | {
    "single_paper_contract_version", "question_count", "expanded_graph_hash",
    "initial_window_hashes", "expansion_window_hashes", "rejection_ledger_version",
    "rejection_ledger_entry_count", "time_policy_version", "stage_durations_ms", "checkpoint_stage",
    "pre_adaptation_hash", "final_exam_hash", "stage7_pre_receipt_hash", "stage7_final_receipt_hash",
}
_DURATION_FIELDS = {
    "material_preheat_ms", "initial_scoring_ms", "expansion_scoring_ms",
    "structural_correction_ms", "local_solve_ms",
}
_LEGACY_SAFE_FIELDS = {
    "version", "paper_count", "request_hash", "profile_hash", "projection_identity_hash",
    "recent_candidate_hashes", "recent_content_hashes", "recent_snapshot_hash",
}
_LEGACY_DISCARDED_FIELDS = {
    "candidates", "manifest", "selection", "shards", "fallback", "quality",
    "material", "ai_outputs",
}


class Stage6CheckpointError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = False


@dataclass(frozen=True, slots=True)
class CheckpointV3:
    version: str
    identity_hash: str
    checkpoint_hash: str
    size_bytes: int
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _checkpoint(self)


@dataclass(frozen=True, slots=True)
class LegacyP1MigrationDecision:
    version: str
    source_version: str
    migrated_identity_hash: str
    decision_hash: str
    requires_rescore_and_local_resolve: bool
    discarded_legacy_fields: tuple[str, ...]
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _decision(self)


def _duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "duplicate_json_key", "合同包含重复 JSON 键")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "non_finite_json_number", "合同包含非有限 JSON 数值")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "non_canonical_json", "合同不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_invalid", "合同依赖哈希无效")
    return value


def _text(value: object, field: str, maximum: int = 128) -> str:
    if not isinstance(value, str):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_invalid", "合同版本字段无效")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or len(text) > maximum:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_invalid", "合同版本字段无效")
    return text


def _hash_list(value: object, field: str, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_invalid", "合同哈希列表无效或过大")
    result = [_hash(item, field) for item in value]
    if len(set(result)) != len(result):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_duplicate", "合同哈希列表重复")
    return sorted(result)


def _normalize_bindings(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _BINDING_FIELDS:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "binding_fields_invalid", "checkpoint v3 绑定字段不完整或包含未知字段")
    result = {field: _hash(value.get(field), field) for field in _BINDING_HASH_FIELDS}
    result["single_paper_contract_version"] = _text(value.get("single_paper_contract_version"), "single_paper_contract_version")
    result["time_policy_version"] = _text(value.get("time_policy_version"), "time_policy_version")
    result["rejection_ledger_version"] = _text(value.get("rejection_ledger_version"), "rejection_ledger_version")
    checkpoint_stage = value.get("checkpoint_stage")
    if checkpoint_stage not in {"stage6_scored", "stage7_finalized"}:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "checkpoint_stage_invalid", "checkpoint v3 阶段无效")
    result["checkpoint_stage"] = checkpoint_stage
    stage7_fields = ("pre_adaptation_hash", "final_exam_hash", "stage7_pre_receipt_hash", "stage7_final_receipt_hash")
    stage7_values = [value.get(field) for field in stage7_fields]
    if checkpoint_stage == "stage6_scored":
        if any(item is not None for item in stage7_values):
            raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "stage7_binding_forbidden_at_stage6", "阶段 6 checkpoint 不得提前携带阶段 7 身份")
        result.update({field: None for field in stage7_fields})
    else:
        if any(item is None for item in stage7_values):
            raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "stage7_binding_incomplete", "阶段 7 checkpoint 的四项身份必须完整成组")
        result.update({field: _hash(value.get(field), field) for field in stage7_fields})
    question_count = value.get("question_count")
    ledger_count = value.get("rejection_ledger_entry_count")
    if type(question_count) is not int or not 1 <= question_count <= 256:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "question_count_invalid", "checkpoint v3 题数无效")
    if type(ledger_count) is not int or not 0 <= ledger_count <= 4096:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "rejection_ledger_count_invalid", "拒绝账本计数无效")
    result["question_count"] = question_count
    result["rejection_ledger_entry_count"] = ledger_count
    expanded = value.get("expanded_graph_hash")
    result["expanded_graph_hash"] = None if expanded is None else _hash(expanded, "expanded_graph_hash")
    initial_windows = _hash_list(value.get("initial_window_hashes"), "initial_window_hashes", MAX_WINDOWS_PER_PHASE)
    expansion_windows = _hash_list(value.get("expansion_window_hashes"), "expansion_window_hashes", MAX_WINDOWS_PER_PHASE)
    if len(initial_windows) != math.ceil(question_count / 4):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "initial_window_count_invalid", "初始评分窗口数必须严格等于 ceil(N/4)")
    if len(expansion_windows) > math.ceil(question_count / 4):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "expansion_window_count_invalid", "扩展评分窗口数超过上限")
    if (result["expanded_graph_hash"] is None) != (not expansion_windows):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "expansion_binding_invalid", "扩展图与扩展窗口身份不一致")
    result["initial_window_hashes"] = initial_windows
    result["expansion_window_hashes"] = expansion_windows
    durations = value.get("stage_durations_ms")
    if not isinstance(durations, Mapping) or set(durations) != _DURATION_FIELDS:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "stage_durations_invalid", "阶段累计耗时字段无效")
    normalized_durations: dict[str, int] = {}
    for field in sorted(_DURATION_FIELDS):
        duration = durations.get(field)
        if type(duration) is not int or not 0 <= duration <= 86_400_000:
            raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", f"{field}_invalid", "阶段累计耗时无效")
        normalized_durations[field] = duration
    result["stage_durations_ms"] = normalized_durations
    return result


def build_checkpoint_v3(*, bindings: Mapping[str, Any]) -> CheckpointV3:
    normalized = _normalize_bindings(bindings)
    identity_hash = _digest(normalized)
    payload = {"version": CHECKPOINT_VERSION, "identity_hash": identity_hash, "bindings": normalized}
    raw = _canonical(payload)
    size = len(raw.encode("utf-8"))
    if size > MAX_CHECKPOINT_BYTES:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "checkpoint_size_exceeded", "checkpoint v3 超过有界大小")
    return CheckpointV3(CHECKPOINT_VERSION, identity_hash, hashlib.sha256(raw.encode()).hexdigest(), size, raw)


def _checkpoint(value: object) -> dict[str, Any]:
    if not isinstance(value, CheckpointV3):
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "checkpoint_type_invalid", "checkpoint v3 类型无效")
    try:
        payload = json.loads(value._payload_json, object_pairs_hook=_duplicates, parse_constant=_invalid_constant)
    except Stage6CheckpointError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "checkpoint_payload_invalid", "checkpoint v3 载荷无效") from exc
    if not isinstance(payload, Mapping) or set(payload) != {"version", "identity_hash", "bindings"} or payload.get("version") != CHECKPOINT_VERSION:
        raise Stage6CheckpointError("exam_stage6_checkpoint_invalid", "checkpoint_fields_invalid", "checkpoint v3 字段无效")
    bindings = _normalize_bindings(payload.get("bindings"))
    raw = _canonical({"version": CHECKPOINT_VERSION, "identity_hash": payload.get("identity_hash"), "bindings": bindings})
    size = len(raw.encode("utf-8"))
    if (
        value.version != CHECKPOINT_VERSION or value.identity_hash != _digest(bindings)
        or payload.get("identity_hash") != value.identity_hash
        or value.checkpoint_hash != hashlib.sha256(raw.encode()).hexdigest()
        or value.size_bytes != size or size > MAX_CHECKPOINT_BYTES
    ):
        raise Stage6CheckpointError("exam_stage6_checkpoint_hash_mismatch", "checkpoint_integrity_mismatch", "checkpoint v3 完整性不匹配")
    return json.loads(raw)


def validate_checkpoint_v3(value: CheckpointV3, *, expected_bindings: Mapping[str, Any]) -> dict[str, Any]:
    payload = _checkpoint(value)
    expected = _normalize_bindings(expected_bindings)
    if payload["bindings"] != expected or value.identity_hash != _digest(expected):
        raise Stage6CheckpointError("exam_stage6_checkpoint_hash_mismatch", "checkpoint_binding_drift", "checkpoint v3 与当前完整权威身份漂移")
    return payload


def rebuild_checkpoint_v3(value: CheckpointV3, *, expected_bindings: Mapping[str, Any]) -> CheckpointV3:
    validate_checkpoint_v3(value, expected_bindings=expected_bindings)
    rebuilt = build_checkpoint_v3(bindings=expected_bindings)
    if rebuilt.checkpoint_hash != value.checkpoint_hash:
        raise Stage6CheckpointError("exam_stage6_checkpoint_hash_mismatch", "checkpoint_rebuild_mismatch", "checkpoint v3 无法幂等重建")
    return rebuilt


def _decision(value: object) -> dict[str, Any]:
    if not isinstance(value, LegacyP1MigrationDecision):
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_type_invalid", "旧检查点迁移决策类型无效")
    try:
        payload = json.loads(value._payload_json, object_pairs_hook=_duplicates, parse_constant=_invalid_constant)
    except Stage6CheckpointError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_payload_invalid", "旧检查点迁移决策载荷无效") from exc
    fields = {"version", "source_version", "source_paper_count", "migrated_identity", "migrated_identity_hash", "discarded_legacy_fields", "requires_rescore_and_local_resolve", "decision_hash"}
    if not isinstance(payload, Mapping):
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_integrity_mismatch", "旧检查点迁移决策完整性不匹配")
    body = {key: payload.get(key) for key in fields - {"decision_hash"}}
    migrated = payload.get("migrated_identity")
    migrated_fields = {"request_hash", "profile_hash", "projection_identity_hash", "recent_candidate_hashes", "recent_content_hashes", "recent_snapshot_hash"}
    if not isinstance(migrated, Mapping) or set(migrated) != migrated_fields:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_identity_invalid", "旧检查点迁移身份字段无效")
    normalized_migrated = {
        "request_hash": _hash(migrated.get("request_hash"), "request_hash"),
        "profile_hash": _hash(migrated.get("profile_hash"), "profile_hash"),
        "projection_identity_hash": _hash(migrated.get("projection_identity_hash"), "projection_identity_hash"),
        "recent_candidate_hashes": _hash_list(migrated.get("recent_candidate_hashes"), "recent_candidate_hashes", MAX_RECENT_HASHES),
        "recent_content_hashes": _hash_list(migrated.get("recent_content_hashes"), "recent_content_hashes", MAX_RECENT_HASHES),
        "recent_snapshot_hash": _hash(migrated.get("recent_snapshot_hash"), "recent_snapshot_hash"),
    }
    discarded = payload.get("discarded_legacy_fields")
    if (
        set(payload) != fields
        or value.version != MIGRATION_DECISION_VERSION or payload.get("version") != value.version
        or payload.get("source_version") != value.source_version or value.source_version != LEGACY_VERSION
        or payload.get("source_paper_count") != 1
        or payload.get("migrated_identity_hash") != value.migrated_identity_hash
        or payload.get("requires_rescore_and_local_resolve") is not True
        or value.requires_rescore_and_local_resolve is not True
        or not isinstance(discarded, list) or discarded != sorted(_LEGACY_DISCARDED_FIELDS)
        or tuple(discarded) != value.discarded_legacy_fields
        or payload.get("decision_hash") != value.decision_hash or value.decision_hash != _digest(body)
        or payload.get("migrated_identity_hash") != _digest(normalized_migrated)
        or migrated != normalized_migrated
        or normalized_migrated["recent_snapshot_hash"] != _digest({"recent_candidate_hashes": normalized_migrated["recent_candidate_hashes"], "recent_content_hashes": normalized_migrated["recent_content_hashes"]})
        or len(value._payload_json.encode("utf-8")) > MAX_MIGRATION_DECISION_BYTES
    ):
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_integrity_mismatch", "旧检查点迁移决策完整性不匹配")
    return dict(payload)


def decide_legacy_p1_migration(*, legacy_json: str, authoritative_identity: Mapping[str, Any]) -> LegacyP1MigrationDecision:
    if not isinstance(legacy_json, str) or len(legacy_json.encode("utf-8")) > MAX_LEGACY_BYTES:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_checkpoint_size_exceeded", "旧检查点超过迁移读取上限")
    try:
        legacy = json.loads(legacy_json, object_pairs_hook=_duplicates, parse_constant=_invalid_constant)
    except Stage6CheckpointError:
        raise
    except (json.JSONDecodeError, TypeError, RecursionError) as exc:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_json_invalid", "旧检查点不是严格 JSON") from exc
    if not isinstance(legacy, Mapping) or not _LEGACY_SAFE_FIELDS.issubset(legacy) or not set(legacy).issubset(_LEGACY_SAFE_FIELDS | _LEGACY_DISCARDED_FIELDS):
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_fields_invalid", "旧检查点字段缺失或包含未知字段")
    if legacy.get("version") != LEGACY_VERSION:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_version_unsupported", "旧检查点版本不受支持")
    paper_count = legacy.get("paper_count")
    if type(paper_count) is not int or paper_count < 1:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_paper_count_invalid", "旧检查点 paper_count 无效")
    if paper_count != 1:
        raise Stage6CheckpointError("exam_legacy_multi_paper_checkpoint_rejected", "legacy_p_gt_1_rejected", "旧 P>1 检查点禁止迁移", status_code=422)
    authoritative_fields = {"request_hash", "profile_hash", "projection_identity_hash", "recent_snapshot_hash"}
    if not isinstance(authoritative_identity, Mapping) or set(authoritative_identity) != authoritative_fields:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "authoritative_identity_fields_invalid", "当前权威身份字段无效")
    authoritative = {field: _hash(authoritative_identity.get(field), field) for field in authoritative_fields}
    candidate_hashes = _hash_list(legacy.get("recent_candidate_hashes"), "recent_candidate_hashes", MAX_RECENT_HASHES)
    content_hashes = _hash_list(legacy.get("recent_content_hashes"), "recent_content_hashes", MAX_RECENT_HASHES)
    migrated = {
        "request_hash": _hash(legacy.get("request_hash"), "request_hash"),
        "profile_hash": _hash(legacy.get("profile_hash"), "profile_hash"),
        "projection_identity_hash": _hash(legacy.get("projection_identity_hash"), "projection_identity_hash"),
        "recent_candidate_hashes": candidate_hashes,
        "recent_content_hashes": content_hashes,
        "recent_snapshot_hash": _hash(legacy.get("recent_snapshot_hash"), "recent_snapshot_hash"),
    }
    calculated_recent = _digest({"recent_candidate_hashes": candidate_hashes, "recent_content_hashes": content_hashes})
    if migrated["recent_snapshot_hash"] != calculated_recent:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "legacy_recent_snapshot_hash_mismatch", "旧最近使用事实哈希无法重算")
    for field in authoritative_fields:
        if migrated[field] != authoritative[field]:
            raise Stage6CheckpointError("exam_legacy_checkpoint_hash_mismatch", f"legacy_{field}_drift", "旧白名单前置身份与当前权威身份不一致")
    discarded = tuple(sorted(_LEGACY_DISCARDED_FIELDS))
    migrated_hash = _digest(migrated)
    body = {
        "version": MIGRATION_DECISION_VERSION, "source_version": LEGACY_VERSION, "source_paper_count": 1,
        "migrated_identity": migrated, "migrated_identity_hash": migrated_hash,
        "discarded_legacy_fields": list(discarded), "requires_rescore_and_local_resolve": True,
    }
    decision_hash = _digest(body)
    raw = _canonical({**body, "decision_hash": decision_hash})
    if len(raw.encode("utf-8")) > MAX_MIGRATION_DECISION_BYTES:
        raise Stage6CheckpointError("exam_legacy_checkpoint_invalid", "migration_decision_size_exceeded", "旧检查点迁移决策超过有界大小")
    return LegacyP1MigrationDecision(MIGRATION_DECISION_VERSION, LEGACY_VERSION, migrated_hash, decision_hash, True, discarded, raw)


__all__ = [
    "CHECKPOINT_VERSION", "CheckpointV3", "LEGACY_VERSION", "LegacyP1MigrationDecision",
    "MIGRATION_DECISION_VERSION", "Stage6CheckpointError", "build_checkpoint_v3",
    "decide_legacy_p1_migration", "rebuild_checkpoint_v3", "validate_checkpoint_v3",
]
