"""Immutable Stage 3 handoff contract for native past-profile-v3 generation.

This module freezes exact slot demand and scoring semantics only.  It does not
authorize candidate retrieval, AI calls, selection, adaptation or publication.
"""

from __future__ import annotations

import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .exam_contracts import past_candidate_quotas, validate_generation_job_id
from .exam_generation_attempt_binding import (
    PAST_V3_ATTEMPT_BINDING_VERSION,
    PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION,
)
from .exam_generation_profile_worker import PastFrozenProfileContext
from .exam_generation_request_binding import (
    EXAM_GENERATION_INPUT_IDENTITY_VERSION,
    FORBIDDEN_EXAM_RUNTIME_IDENTITY_FIELDS,
    ExamGenerationRequestIdentityError,
    verify_persisted_input,
)
from .job_contracts import JobCancelled, JobLeaseLost, canonical_payload


TABLE = "exam_generation_stage3_contract_receipts"
BINDINGS_TABLE = "exam_generation_attempt_bindings"
PROFILE_RECEIPTS_TABLE = "exam_generation_profile_resolved_receipts"
STAGE_NAME = "exact_slot_contract_ready"
RECEIPT_CONTRACT_VERSION = "past-v3-stage3-contract-receipt-v1"
PAST_V3_GENERATION_CONTRACT_VERSION = "past-v3-generation-contract-v1"

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_FIELDS = (
    "binding_contract_version", "job_id", "job_input_identity_version",
    "generation_request_hash_version", "single_paper_contract_version",
    "past_profile_freeze_contract_version", "profile_worker_context_version",
    "exact_slot_context_version", "logical_request_hash", "job_input_hash",
    "generation_request_hash", "frozen_ref_hash", "profile_hash",
    "exact_slot_hash", "resolution_hash",
)
_PROFILE_RECEIPT_FIELDS = (
    "receipt_contract_version", "stage_name", "job_id", "binding_hash",
    "resolution_hash",
)
_RECEIPT_FIELDS = (
    "receipt_contract_version", "stage_name", "job_id",
    "stage3_contract_version", "binding_hash", "generation_request_hash",
    "resolution_hash", "exact_slot_hash", "question_count", "scoring_mode",
    "slot_demands_hash", "points_projection_hash", "source_quotas_hash",
    "stage3_contract_hash",
)


class ExamGenerationStage3ContractError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int, retryable: bool, reason: str, category: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        self.reason = reason
        self.category = category
        self.actions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PastV3GenerationContract:
    contract_version: str
    generation_request_hash: str
    binding_hash: str
    resolution_hash: str
    profile_hash: str
    exact_slot_hash: str
    question_count: int
    scoring_mode: str
    slot_demands_hash: str
    points_projection_hash: str
    source_quotas_hash: str
    stage3_contract_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True, slots=True)
class Stage3GenerationContractReceipt:
    job_id: str
    binding_hash: str
    generation_request_hash: str
    stage3_contract_hash: str
    receipt_hash: str
    contract: PastV3GenerationContract


def _digest(value: Mapping[str, Any]) -> str:
    return canonical_payload(value)[1]


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _HASH_RE.fullmatch(value) is not None


def _fail(code: str, reason: str, message: str, *, category: str = "contract", status_code: int = 409, retryable: bool = False) -> None:
    raise ExamGenerationStage3ContractError(code, message, status_code=status_code, retryable=retryable, reason=reason, category=category)


def _mapping(value: object, reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail("exam_stage3_contract_hash_mismatch", reason, "真题生成合同完整性校验失败")
    return value


def build_past_v3_generation_contract(
    persisted_payload: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
    profile_receipt: Mapping[str, Any],
    profile_context: PastFrozenProfileContext,
) -> PastV3GenerationContract:
    """Build the pure semantic Stage 3 contract; no runtime identity is accepted."""

    if not isinstance(persisted_payload, Mapping) or str(persisted_payload.get("source") or "") != "past":
        _fail("exam_stage3_contract_conflict", "job_source_not_past", "非真题任务不能建立真题生成合同")
    if FORBIDDEN_EXAM_RUNTIME_IDENTITY_FIELDS.intersection(persisted_payload):
        _fail(
            "exam_stage3_contract_conflict",
            "runtime_identity_field_forbidden",
            "真题生成合同不能绑定运行时或旧检查点字段",
        )
    if not isinstance(profile_context, PastFrozenProfileContext):
        _fail("exam_stage3_contract_conflict", "profile_context_type_invalid", "真题画像上下文无效")
    try:
        rendered = profile_context.to_dict()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        _fail("exam_stage3_contract_hash_mismatch", "profile_context_json_invalid", "真题画像上下文完整性校验失败")
    profile_binding = _mapping(rendered.get("profile_binding"), "profile_binding_missing")
    exact = _mapping(rendered.get("exact_slot_contract"), "exact_slot_contract_missing")
    frozen_ref = _mapping(rendered.get("past_profile_frozen_ref"), "frozen_ref_missing")
    try:
        rendered_resolution_hash = _digest({
            "contract_version": rendered.get("contract_version"),
            "profile_binding": dict(profile_binding),
            "exact_slot_contract": dict(exact),
        })
    except (TypeError, ValueError) as exc:
        _fail("exam_stage3_contract_hash_mismatch", "profile_context_json_invalid", "真题画像上下文完整性校验失败")
    if (
        rendered.get("resolution_hash") != profile_context.resolution_hash
        or not hmac.compare_digest(profile_context.resolution_hash, rendered_resolution_hash)
        or profile_binding.get("profile_hash") != profile_context.profile_hash
        or frozen_ref.get("profile_hash") != profile_context.profile_hash
        or profile_binding.get("exact_slot_hash") != profile_context.exact_slot_hash
        or frozen_ref.get("exact_slot_hash") != profile_context.exact_slot_hash
        or exact.get("exact_slot_hash") != profile_context.exact_slot_hash
        or profile_binding.get("frozen_ref_hash") != profile_context.frozen_ref_hash
        or frozen_ref.get("frozen_ref_hash") != profile_context.frozen_ref_hash
        or persisted_payload.get("past_profile_frozen_ref") != frozen_ref
    ):
        _fail("exam_stage3_contract_hash_mismatch", "profile_context_hash_mismatch", "真题画像上下文完整性校验失败")
    combination = _mapping(profile_binding.get("combination"), "profile_combination_missing")
    sections_source = exact.get("sections")
    slots_source = exact.get("slots")
    scoring = _mapping(exact.get("scoring"), "scoring_projection_missing")
    if not isinstance(sections_source, list) or not isinstance(slots_source, list) or not slots_source:
        _fail("exam_stage3_contract_hash_mismatch", "exact_slot_shape_invalid", "真题精确槽位合同无效")
    question_count = len(slots_source)
    if exact.get("question_count") != question_count:
        _fail("exam_stage3_contract_hash_mismatch", "question_count_mismatch", "真题精确槽位数量不一致")
    slot_demands: list[dict[str, Any]] = []
    points_slots: list[dict[str, Any]] = []
    for expected_position, raw in enumerate(slots_source, start=1):
        slot = _mapping(raw, "slot_shape_invalid")
        if slot.get("position") != expected_position:
            _fail("exam_stage3_contract_hash_mismatch", "slot_positions_not_contiguous", "真题精确槽位位置不连续")
        for key in ("section_id", "question_type_family", "target_difficulty"):
            if not isinstance(slot.get(key), str) or not str(slot[key]).strip():
                _fail("exam_stage3_contract_hash_mismatch", f"slot_{key}_invalid", "真题精确槽位字段无效")
        slot_demands.append({
            "position": expected_position,
            "section_id": slot["section_id"],
            "question_type_family": slot["question_type_family"],
            "target_difficulty": slot["target_difficulty"],
        })
        evidence = _mapping(slot.get("evidence"), "slot_evidence_missing")
        points_slots.append({"position": expected_position, "points": slot.get("points"), "points_evidence_hash": evidence.get("points")})
    scoring_mode = scoring.get("mode")
    if scoring_mode not in {"verified", "unscored"}:
        _fail("exam_stage3_contract_hash_mismatch", "scoring_mode_invalid", "真题计分模式无效")
    sections: list[dict[str, Any]] = []
    points_sections: list[dict[str, Any]] = []
    for raw in sections_source:
        section = _mapping(raw, "section_shape_invalid")
        if not isinstance(section.get("section_id"), str) or not isinstance(section.get("title"), str):
            _fail("exam_stage3_contract_hash_mismatch", "section_identity_invalid", "真题分区身份无效")
        sections.append({"section_id": section["section_id"], "title": section["title"], "positions": list(section.get("positions") or []), "question_count": section.get("question_count")})
        points_sections.append({"section_id": section["section_id"], "points": section.get("points"), "points_evidence_hash": section.get("points_evidence_hash")})
    total_points = scoring.get("total_points")
    scoring_evidence = scoring.get("evidence_hash")
    if scoring_mode == "unscored":
        if total_points is not None or scoring_evidence is not None or any(item["points"] is not None or item["points_evidence_hash"] is not None for item in (*points_sections, *points_slots)):
            _fail("exam_stage3_contract_hash_mismatch", "unscored_points_not_null", "未计分画像不得推断分值")
    else:
        values = [item["points"] for item in (*points_sections, *points_slots)]
        hashes = [item["points_evidence_hash"] for item in (*points_sections, *points_slots)]
        try:
            total_decimal = Decimal(str(total_points))
            decimals = [Decimal(str(value)) for value in values]
        except (InvalidOperation, ValueError) as exc:
            _fail("exam_stage3_contract_hash_mismatch", "verified_points_invalid", "已验证画像分值或证据无效")
        if type(total_points) not in {int, float} or not total_decimal.is_finite() or total_decimal <= 0 or not _is_hash(scoring_evidence) or any(type(v) not in {int, float} or not d.is_finite() or d <= 0 for v, d in zip(values, decimals, strict=True)) or any(not _is_hash(v) for v in hashes):
            _fail("exam_stage3_contract_hash_mismatch", "verified_points_invalid", "已验证画像分值或证据无效")
        if sum((Decimal(str(item["points"])) for item in points_slots), Decimal(0)) != total_decimal or sum((Decimal(str(item["points"])) for item in points_sections), Decimal(0)) != total_decimal:
            _fail("exam_stage3_contract_hash_mismatch", "verified_points_total_mismatch", "已验证画像分值总计不一致")
    candidate_source = persisted_payload.get("candidate_source")
    try:
        source_quotas = past_candidate_quotas(
            candidate_source,
            question_count,
            institution=combination.get("institution"),
            subject=combination.get("subject"),
            reference_year=combination.get("reference_year"),
        )
    except ValueError as exc:
        _fail("exam_stage3_contract_conflict", "candidate_source_invalid", str(exc))
    generation_request_hash = binding.get("generation_request_hash")
    binding_hash = binding.get("binding_hash")
    if not all(_is_hash(v) for v in (generation_request_hash, binding_hash, profile_context.resolution_hash, profile_context.profile_hash, profile_context.exact_slot_hash)):
        _fail("exam_stage3_contract_hash_mismatch", "dependency_hash_malformed", "真题生成合同依赖哈希无效")
    if (
        binding.get("binding_contract_version") != PAST_V3_ATTEMPT_BINDING_VERSION
        or profile_receipt.get("receipt_contract_version") != PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION
        or profile_receipt.get("stage_name") != "profile_resolved"
        or profile_receipt.get("binding_hash") != binding_hash
        or binding.get("resolution_hash") != profile_context.resolution_hash
        or profile_receipt.get("resolution_hash") != profile_context.resolution_hash
        or binding.get("profile_hash") != profile_context.profile_hash
        or binding.get("exact_slot_hash") != profile_context.exact_slot_hash
        or exact.get("exact_slot_hash") != profile_context.exact_slot_hash
    ):
        _fail("exam_stage3_contract_conflict", "profile_binding_context_mismatch", "真题生成合同依赖身份不一致")
    slot_demands_hash = _digest({"slot_demands": slot_demands})
    points_projection = {"mode": scoring_mode, "total_points": total_points, "evidence_hash": scoring_evidence, "sections": points_sections, "slots": points_slots}
    points_projection_hash = _digest(points_projection)
    source_quotas_hash = _digest({"candidate_source": candidate_source, "source_quotas": source_quotas})
    source_identity = {key: profile_binding.get(key) for key in ("source_snapshot", "source_exam_id", "catalog_version", "course_version")}
    if not all(isinstance(value, str) and value for value in source_identity.values()):
        _fail("exam_stage3_contract_hash_mismatch", "source_identity_invalid", "真题来源身份无效")
    payload = {
        "contract_version": PAST_V3_GENERATION_CONTRACT_VERSION,
        "generation_request_hash": generation_request_hash,
        "binding_hash": binding_hash,
        "resolution_hash": profile_context.resolution_hash,
        "profile_hash": profile_context.profile_hash,
        "exact_slot_hash": profile_context.exact_slot_hash,
        "combination": dict(combination),
        "source_identity": source_identity,
        "question_count": question_count,
        "sections": sections,
        "slot_demands": slot_demands,
        "scoring": points_projection,
        "candidate_source": candidate_source,
        "source_quotas": source_quotas,
        "target_institution_exclusion": {"institution": combination.get("institution")},
        "slot_demands_hash": slot_demands_hash,
        "points_projection_hash": points_projection_hash,
        "source_quotas_hash": source_quotas_hash,
    }
    raw, stage3_hash = canonical_payload(payload)
    return PastV3GenerationContract(PAST_V3_GENERATION_CONTRACT_VERSION, str(generation_request_hash), str(binding_hash), profile_context.resolution_hash, profile_context.profile_hash, profile_context.exact_slot_hash, question_count, str(scoring_mode), slot_demands_hash, points_projection_hash, source_quotas_hash, stage3_hash, raw)


def _row_projection(row: Mapping[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row[field] for field in fields}


def _load_authority(db, job_id: str, worker_id: str, lease_attempt: int, profile_context: PastFrozenProfileContext) -> tuple[PastV3GenerationContract, Mapping[str, Any]]:
    row = db.execute("SELECT j.id,j.kind,j.status,j.cancel_requested,j.input_hash,j.lease_owner,j.attempts,i.payload_json,i.payload_hash,i.identity_version FROM background_jobs j LEFT JOIN job_inputs i ON i.job_id=j.id WHERE j.id=?", (job_id,)).fetchone()
    if row is None:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if str(row["kind"] or "") != "exam-generation":
        _fail("exam_stage3_contract_conflict", "job_kind_not_exam_generation", "任务类型不能写入真题生成合同")
    if bool(row["cancel_requested"]) or str(row["status"] or "") == "cancel_requested":
        raise JobCancelled("任务已取消")
    if str(row["status"] or "") != "running" or str(row["lease_owner"] or "") != worker_id or int(row["attempts"] or 0) != lease_attempt:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if row["payload_json"] is None or row["identity_version"] != EXAM_GENERATION_INPUT_IDENTITY_VERSION:
        _fail("exam_stage3_contract_version_unsupported", "job_input_identity_version_unsupported", "任务冻结输入版本不受支持")
    try:
        payload = verify_persisted_input(row["payload_json"], row["payload_hash"], identity_version=row["identity_version"], job_kind=row["kind"], job_status=row["status"])
    except ExamGenerationRequestIdentityError as exc:
        _fail("exam_stage3_contract_hash_mismatch", exc.reason, "任务冻结输入完整性校验失败")
    binding = db.execute(f"SELECT * FROM {BINDINGS_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    receipt = db.execute(f"SELECT * FROM {PROFILE_RECEIPTS_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    if binding is None or receipt is None:
        _fail("exam_stage3_contract_conflict", "profile_resolved_binding_missing", "真题画像解析回执尚未完成")
    if not hmac.compare_digest(str(binding["binding_hash"] or ""), _digest(_row_projection(binding, _BINDING_FIELDS))):
        _fail("exam_stage3_contract_hash_mismatch", "stored_binding_hash_mismatch", "真题画像绑定完整性校验失败")
    if not hmac.compare_digest(str(receipt["receipt_hash"] or ""), _digest(_row_projection(receipt, _PROFILE_RECEIPT_FIELDS))):
        _fail("exam_stage3_contract_hash_mismatch", "stored_profile_receipt_hash_mismatch", "真题画像回执完整性校验失败")
    if binding["logical_request_hash"] != row["input_hash"] or binding["job_input_hash"] != row["payload_hash"] or binding["generation_request_hash"] != payload.get("generation_request_hash"):
        _fail("exam_stage3_contract_conflict", "stored_binding_input_mismatch", "真题画像绑定与当前冻结输入冲突")
    contract = build_past_v3_generation_contract(payload, binding=dict(binding), profile_receipt=dict(receipt), profile_context=profile_context)
    return contract, payload


def _publication_residue_exists(db, job_id: str) -> bool:
    row = db.execute("SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)", (job_id, job_id, job_id)).fetchone()
    return bool(row[0])


def _expected_receipt(job_id: str, contract: PastV3GenerationContract, created_at: str) -> dict[str, Any]:
    value = {
        "job_id": job_id, "stage_name": STAGE_NAME,
        "receipt_contract_version": RECEIPT_CONTRACT_VERSION,
        "stage3_contract_version": contract.contract_version,
        "binding_hash": contract.binding_hash,
        "generation_request_hash": contract.generation_request_hash,
        "resolution_hash": contract.resolution_hash,
        "exact_slot_hash": contract.exact_slot_hash,
        "question_count": contract.question_count,
        "scoring_mode": contract.scoring_mode,
        "slot_demands_hash": contract.slot_demands_hash,
        "points_projection_hash": contract.points_projection_hash,
        "source_quotas_hash": contract.source_quotas_hash,
        "stage3_contract_hash": contract.stage3_contract_hash,
    }
    return {**value, "receipt_hash": _digest({field: value[field] for field in _RECEIPT_FIELDS}), "created_at": created_at}


def _cas_receipt(db, expected: Mapping[str, Any]) -> None:
    columns = (*_RECEIPT_FIELDS, "receipt_hash", "created_at")
    db.execute(f"INSERT INTO {TABLE}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(job_id) DO NOTHING", tuple(expected[c] for c in columns))
    row = db.execute(f"SELECT * FROM {TABLE} WHERE job_id=?", (expected["job_id"],)).fetchone()
    if row is None:
        _fail("exam_stage3_contract_write_failed", "receipt_missing_after_insert", "真题生成合同回执未能持久化", category="storage", status_code=500)
    if row["receipt_contract_version"] != RECEIPT_CONTRACT_VERSION or row["stage3_contract_version"] != PAST_V3_GENERATION_CONTRACT_VERSION:
        _fail("exam_stage3_contract_version_unsupported", "stored_stage3_contract_version_unsupported", "已有真题生成合同版本不受支持")
    if not hmac.compare_digest(str(row["receipt_hash"] or ""), _digest(_row_projection(row, _RECEIPT_FIELDS))):
        _fail("exam_stage3_contract_hash_mismatch", "stored_stage3_receipt_hash_mismatch", "已有真题生成合同完整性校验失败")
    if any(row[field] != expected[field] for field in (*_RECEIPT_FIELDS, "receipt_hash")):
        _fail("exam_stage3_contract_conflict", "stored_stage3_contract_identity_conflict", "已有真题生成合同与当前冻结身份冲突")


def _database_error(exc: sqlite3.DatabaseError) -> ExamGenerationStage3ContractError:
    busy = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
    return ExamGenerationStage3ContractError("exam_stage3_contract_store_busy" if busy else "exam_stage3_contract_store_corrupt", "真题生成合同存储暂时繁忙" if busy else "真题生成合同存储完整性校验失败", status_code=503 if busy else 500, retryable=busy, reason="stage3_contract_store_busy" if busy else "stage3_contract_database_error", category="storage")


def persist_stage3_generation_contract(db, *, job_id: str, profile_context: PastFrozenProfileContext, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage3GenerationContractReceipt:
    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        _fail("exam_stage3_contract_conflict", "generation_job_id_invalid", "组卷任务身份无效")
    if normalized_job_id is None:
        _fail("exam_stage3_contract_conflict", "generation_job_id_missing", "组卷任务身份缺失")
    worker = str(worker_id or "").strip()
    if not worker or type(lease_attempt) is not int or lease_attempt < 1:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if db.in_transaction:
        _fail("exam_stage3_contract_write_failed", "nested_stage3_transaction_forbidden", "真题生成合同不能进入嵌套写事务", category="storage", status_code=500)
    timestamp = str(created_at or datetime.now(timezone.utc).isoformat(timespec="microseconds"))
    try:
        prepared, _payload = _load_authority(db, normalized_job_id, worker, lease_attempt, profile_context)
        expected = _expected_receipt(normalized_job_id, prepared, timestamp)
        with immediate_transaction(db):
            current, _current_payload = _load_authority(db, normalized_job_id, worker, lease_attempt, profile_context)
            if _publication_residue_exists(db, normalized_job_id):
                _fail("published_recovery_required", "publication_residue_detected", "任务已有发布事实，必须先完成发布恢复", category="dependency")
            current_expected = _expected_receipt(normalized_job_id, current, timestamp)
            if any(current_expected[field] != expected[field] for field in (*_RECEIPT_FIELDS, "receipt_hash")):
                _fail("exam_stage3_contract_conflict", "stage3_authority_changed_before_write", "真题生成合同权威身份在写入前发生冲突")
            _cas_receipt(db, current_expected)
    except (ExamGenerationStage3ContractError, JobCancelled, JobLeaseLost):
        raise
    except sqlite3.DatabaseError as exc:
        raise _database_error(exc) from exc
    return Stage3GenerationContractReceipt(normalized_job_id, prepared.binding_hash, prepared.generation_request_hash, prepared.stage3_contract_hash, str(expected["receipt_hash"]), prepared)


__all__ = ["ExamGenerationStage3ContractError", "PAST_V3_GENERATION_CONTRACT_VERSION", "PastV3GenerationContract", "Stage3GenerationContractReceipt", "build_past_v3_generation_contract", "persist_stage3_generation_contract"]
