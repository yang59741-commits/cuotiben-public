"""Immutable v38-input to past-profile-v3 resolution binding.

This is the first native stage receipt in the remediation generation path.  It
does not authorize candidate building, selection, adaptation or publication.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .exam_contracts import validate_generation_job_id, validate_single_paper_count
from .exam_generation_profile_freeze import (
    PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION,
)
from .exam_generation_profile_worker import (
    PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION,
    PAST_PROFILE_WORKER_CONTEXT_VERSION,
    PastFrozenProfileContext,
)
from .exam_generation_request_binding import (
    EXAM_GENERATION_INPUT_IDENTITY_VERSION,
    GENERATION_REQUEST_HASH_VERSION,
    ExamGenerationRequestIdentityError,
    verify_persisted_input,
)
from .exam_remediation_contracts import SINGLE_PAPER_CONTRACT_VERSION
from .job_contracts import JobLeaseLost, canonical_payload


PAST_V3_ATTEMPT_BINDING_VERSION = "past-v3-attempt-binding-v1"
PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION = (
    "past-v3-profile-resolved-receipt-v1"
)
PROFILE_RESOLVED_STAGE = "profile_resolved"

BINDINGS_TABLE = "exam_generation_attempt_bindings"
RECEIPTS_TABLE = "exam_generation_profile_resolved_receipts"

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_HASH_FIELDS = (
    "binding_contract_version",
    "job_id",
    "job_input_identity_version",
    "generation_request_hash_version",
    "single_paper_contract_version",
    "past_profile_freeze_contract_version",
    "profile_worker_context_version",
    "exact_slot_context_version",
    "logical_request_hash",
    "job_input_hash",
    "generation_request_hash",
    "frozen_ref_hash",
    "profile_hash",
    "exact_slot_hash",
    "resolution_hash",
)
_RECEIPT_HASH_FIELDS = (
    "receipt_contract_version",
    "stage_name",
    "job_id",
    "binding_hash",
    "resolution_hash",
)


class ExamGenerationAttemptBindingError(RuntimeError):
    """Stable, redacted failure while persisting the first native receipt."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool,
        reason: str,
        category: str,
        actions: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.reason = str(reason)
        self.category = str(category)
        self.actions = tuple(str(item) for item in actions)


@dataclass(frozen=True, slots=True)
class ProfileResolvedBinding:
    job_id: str
    binding_hash: str
    resolution_hash: str
    receipt_hash: str


@dataclass(frozen=True, slots=True)
class _PreparedBinding:
    binding: Mapping[str, str]
    receipt: Mapping[str, str]


def _digest(value: Mapping[str, Any]) -> str:
    return canonical_payload(value)[1]


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _contract_error(code: str, reason: str, message: str) -> None:
    raise ExamGenerationAttemptBindingError(
        code,
        message,
        status_code=409,
        retryable=False,
        reason=reason,
        category="contract",
    )


def _input_identity_error(exc: ExamGenerationRequestIdentityError) -> None:
    if exc.reason == "legacy_multi_paper_checkpoint_unsupported":
        raise ExamGenerationAttemptBindingError(
            "legacy_multi_paper_checkpoint_unsupported",
            "旧多卷未完成任务不能恢复，请重新提交单卷任务",
            status_code=409,
            retryable=False,
            reason=exc.reason,
            category="contract",
            actions=("resubmit_single_paper",),
        ) from exc
    if exc.reason == "legacy_exam_checkpoint_migration_pending":
        raise ExamGenerationAttemptBindingError(
            "legacy_exam_checkpoint_migration_pending",
            "旧组卷任务尚未进入白名单检查点迁移路径",
            status_code=409,
            retryable=False,
            reason=exc.reason,
            category="dependency",
            actions=("resubmit_after_upgrade",),
        ) from exc
    raise ExamGenerationAttemptBindingError(
        "job_input_hash_mismatch",
        "任务冻结输入完整性校验失败",
        status_code=409,
        retryable=False,
        reason=exc.reason,
        category="contract",
    ) from exc


def _validate_profile_context(
    context: PastFrozenProfileContext,
    persisted_payload: Mapping[str, Any],
) -> None:
    if not isinstance(context, PastFrozenProfileContext):
        _contract_error(
            "exam_attempt_binding_conflict",
            "profile_context_type_invalid",
            "真题画像解析结果不能绑定到当前任务",
        )
    try:
        rendered = context.to_dict()
    except (TypeError, ValueError) as exc:
        raise ExamGenerationAttemptBindingError(
            "exam_attempt_binding_hash_mismatch",
            "真题画像解析结果完整性校验失败",
            status_code=409,
            retryable=False,
            reason="profile_context_json_invalid",
            category="contract",
        ) from exc
    profile_binding = rendered.get("profile_binding")
    exact_slot_contract = rendered.get("exact_slot_contract")
    frozen_ref = rendered.get("past_profile_frozen_ref")
    if not all(
        isinstance(value, Mapping)
        for value in (profile_binding, exact_slot_contract, frozen_ref)
    ):
        _contract_error(
            "exam_attempt_binding_hash_mismatch",
            "profile_context_shape_invalid",
            "真题画像解析结果完整性校验失败",
        )
    hash_content = {
        "contract_version": rendered.get("contract_version"),
        "profile_binding": dict(profile_binding),
        "exact_slot_contract": dict(exact_slot_contract),
    }
    expected_resolution_hash = _digest(hash_content)
    if (
        rendered.get("contract_version") != PAST_PROFILE_WORKER_CONTEXT_VERSION
        or exact_slot_contract.get("contract_version")
        != PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION
        or rendered.get("resolution_hash") != context.resolution_hash
        or not _is_hash(context.resolution_hash)
        or not hmac.compare_digest(
            context.resolution_hash, expected_resolution_hash
        )
        or profile_binding.get("frozen_ref_hash") != context.frozen_ref_hash
        or frozen_ref.get("frozen_ref_hash") != context.frozen_ref_hash
        or profile_binding.get("profile_hash") != context.profile_hash
        or frozen_ref.get("profile_hash") != context.profile_hash
        or profile_binding.get("exact_slot_hash") != context.exact_slot_hash
        or frozen_ref.get("exact_slot_hash") != context.exact_slot_hash
        or exact_slot_contract.get("exact_slot_hash") != context.exact_slot_hash
        or not all(
            _is_hash(value)
            for value in (
                context.frozen_ref_hash,
                context.profile_hash,
                context.exact_slot_hash,
            )
        )
    ):
        _contract_error(
            "exam_attempt_binding_hash_mismatch",
            "profile_context_hash_mismatch",
            "真题画像解析结果完整性校验失败",
        )
    if persisted_payload.get("past_profile_frozen_ref") != frozen_ref:
        _contract_error(
            "exam_attempt_binding_conflict",
            "persisted_frozen_ref_context_mismatch",
            "真题画像解析结果与任务冻结输入不一致",
        )


def _load_authority(
    db,
    *,
    job_id: str,
    worker_id: str,
    lease_attempt: int,
    profile_context: PastFrozenProfileContext,
    created_at: str,
) -> _PreparedBinding:
    row = db.execute(
        "SELECT j.id,j.kind,j.status,j.input_hash,j.lease_owner,j.attempts,"
        "i.payload_json,i.payload_hash,i.identity_version "
        "FROM background_jobs j LEFT JOIN job_inputs i ON i.job_id=j.id "
        "WHERE j.id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if str(row["kind"] or "") != "exam-generation":
        _contract_error(
            "exam_attempt_binding_conflict",
            "job_kind_not_exam_generation",
            "任务类型不能写入组卷画像回执",
        )
    if (
        str(row["status"] or "") != "running"
        or str(row["lease_owner"] or "") != worker_id
        or int(row["attempts"] or 0) != lease_attempt
    ):
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if row["payload_json"] is None:
        _input_identity_error(
            ExamGenerationRequestIdentityError("job_input_missing")
        )
    try:
        persisted_payload = verify_persisted_input(
            row["payload_json"],
            row["payload_hash"],
            identity_version=row["identity_version"],
            job_kind=row["kind"],
            job_status=row["status"],
        )
    except ExamGenerationRequestIdentityError as exc:
        _input_identity_error(exc)

    if str(persisted_payload.get("source") or "free") != "past":
        _contract_error(
            "exam_attempt_binding_conflict",
            "job_source_not_past",
            "非真题任务不能写入真题画像回执",
        )
    try:
        validate_single_paper_count(persisted_payload.get("paper_count"))
    except ValueError as exc:
        raise ExamGenerationAttemptBindingError(
            "invalid_paper_count",
            str(exc),
            status_code=409,
            retryable=False,
            reason="paper_count_not_one",
            category="contract",
        ) from exc
    if (
        row["identity_version"] != EXAM_GENERATION_INPUT_IDENTITY_VERSION
        or persisted_payload.get("generation_request_hash_version")
        != GENERATION_REQUEST_HASH_VERSION
        or persisted_payload.get("single_paper_contract_version")
        != SINGLE_PAPER_CONTRACT_VERSION
        or persisted_payload.get("past_profile_freeze_contract_version")
        != PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION
    ):
        _contract_error(
            "exam_attempt_binding_version_unsupported",
            "attempt_binding_dependency_version_unsupported",
            "组卷任务的冻结合同版本不受支持",
        )
    logical_request_hash = str(row["input_hash"] or "")
    job_input_hash = str(row["payload_hash"] or "")
    generation_request_hash = str(
        persisted_payload.get("generation_request_hash") or ""
    )
    if not all(
        _is_hash(value)
        for value in (
            logical_request_hash,
            job_input_hash,
            generation_request_hash,
        )
    ):
        _contract_error(
            "exam_attempt_binding_hash_mismatch",
            "request_identity_hash_malformed",
            "组卷任务的冻结身份哈希无效",
        )

    return build_profile_binding_records(
        persisted_payload=persisted_payload, profile_context=profile_context,
        job_id=job_id, logical_request_hash=logical_request_hash,
        job_input_hash=job_input_hash, generation_request_hash=generation_request_hash,
        created_at=created_at,
    )


def build_profile_binding_records(
    *, persisted_payload: Mapping[str, Any], profile_context: PastFrozenProfileContext,
    job_id: str, logical_request_hash: str, job_input_hash: str,
    generation_request_hash: str, created_at: str,
) -> _PreparedBinding:
    """Build the same binding bytes for the worker and read-only preflight."""
    _validate_profile_context(profile_context, persisted_payload)
    binding = {
        "binding_contract_version": PAST_V3_ATTEMPT_BINDING_VERSION,
        "job_id": job_id,
        "job_input_identity_version": EXAM_GENERATION_INPUT_IDENTITY_VERSION,
        "generation_request_hash_version": GENERATION_REQUEST_HASH_VERSION,
        "single_paper_contract_version": SINGLE_PAPER_CONTRACT_VERSION,
        "past_profile_freeze_contract_version": (
            PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION
        ),
        "profile_worker_context_version": PAST_PROFILE_WORKER_CONTEXT_VERSION,
        "exact_slot_context_version": PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION,
        "logical_request_hash": logical_request_hash,
        "job_input_hash": job_input_hash,
        "generation_request_hash": generation_request_hash,
        "frozen_ref_hash": profile_context.frozen_ref_hash,
        "profile_hash": profile_context.profile_hash,
        "exact_slot_hash": profile_context.exact_slot_hash,
        "resolution_hash": profile_context.resolution_hash,
    }
    binding_hash = _digest(binding)
    binding_record = {
        **binding,
        "binding_hash": binding_hash,
        "created_at": created_at,
    }
    receipt = {
        "receipt_contract_version": (
            PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION
        ),
        "stage_name": PROFILE_RESOLVED_STAGE,
        "job_id": job_id,
        "binding_hash": binding_hash,
        "resolution_hash": profile_context.resolution_hash,
    }
    receipt_record = {
        "job_id": job_id,
        "stage_name": PROFILE_RESOLVED_STAGE,
        "receipt_contract_version": (
            PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION
        ),
        "binding_hash": binding_hash,
        "resolution_hash": profile_context.resolution_hash,
        "receipt_hash": _digest(receipt),
        "created_at": created_at,
    }
    return _PreparedBinding(binding=binding_record, receipt=receipt_record)


def _publication_residue_exists(db, job_id: str) -> bool:
    row = db.execute(
        "SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)",
        (job_id, job_id, job_id),
    ).fetchone()
    return bool(row[0])


def _published_recovery_required() -> None:
    raise ExamGenerationAttemptBindingError(
        "published_recovery_required",
        "任务已有发布事实，必须先完成发布恢复",
        status_code=409,
        retryable=False,
        reason="publication_residue_detected",
        category="dependency",
        actions=("retry_after_recovery",),
    )


def _binding_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in _BINDING_HASH_FIELDS}


def _receipt_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: row[field] for field in _RECEIPT_HASH_FIELDS}


def _cas_binding(db, expected: Mapping[str, str]) -> None:
    columns = (
        *_BINDING_HASH_FIELDS,
        "binding_hash",
        "created_at",
    )
    placeholders = ",".join("?" for _column in columns)
    db.execute(
        f"INSERT INTO {BINDINGS_TABLE}({','.join(columns)}) "
        f"VALUES({placeholders}) ON CONFLICT(job_id) DO NOTHING",
        tuple(expected[column] for column in columns),
    )
    row = db.execute(
        f"SELECT * FROM {BINDINGS_TABLE} WHERE job_id=?",
        (expected["job_id"],),
    ).fetchone()
    if row is None:
        raise ExamGenerationAttemptBindingError(
            "exam_attempt_binding_write_failed",
            "组卷画像绑定未能持久化",
            status_code=500,
            retryable=False,
            reason="binding_row_missing_after_insert",
            category="storage",
        )
    if str(row["binding_contract_version"]) != PAST_V3_ATTEMPT_BINDING_VERSION:
        _contract_error(
            "exam_attempt_binding_version_unsupported",
            "stored_binding_version_unsupported",
            "已有组卷画像绑定版本不受支持",
        )
    actual_hash = str(row["binding_hash"] or "")
    computed_hash = _digest(_binding_projection(row))
    if not _is_hash(actual_hash) or not hmac.compare_digest(
        actual_hash, computed_hash
    ):
        _contract_error(
            "exam_attempt_binding_hash_mismatch",
            "stored_binding_hash_mismatch",
            "已有组卷画像绑定完整性校验失败",
        )
    if any(
        row[field] != expected[field]
        for field in (*_BINDING_HASH_FIELDS, "binding_hash")
    ):
        _contract_error(
            "exam_attempt_binding_conflict",
            "stored_binding_identity_conflict",
            "已有组卷画像绑定与当前冻结身份冲突",
        )


def _cas_receipt(db, expected: Mapping[str, str]) -> None:
    columns = (
        "job_id",
        "stage_name",
        "receipt_contract_version",
        "binding_hash",
        "resolution_hash",
        "receipt_hash",
        "created_at",
    )
    placeholders = ",".join("?" for _column in columns)
    db.execute(
        f"INSERT INTO {RECEIPTS_TABLE}({','.join(columns)}) "
        f"VALUES({placeholders}) ON CONFLICT(job_id) DO NOTHING",
        tuple(expected[column] for column in columns),
    )
    row = db.execute(
        f"SELECT * FROM {RECEIPTS_TABLE} WHERE job_id=?",
        (expected["job_id"],),
    ).fetchone()
    if row is None:
        raise ExamGenerationAttemptBindingError(
            "exam_attempt_binding_write_failed",
            "真题画像解析回执未能持久化",
            status_code=500,
            retryable=False,
            reason="profile_resolved_receipt_missing_after_insert",
            category="storage",
        )
    if (
        str(row["receipt_contract_version"])
        != PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION
    ):
        _contract_error(
            "exam_profile_resolved_receipt_version_unsupported",
            "stored_profile_resolved_receipt_version_unsupported",
            "已有真题画像解析回执版本不受支持",
        )
    actual_hash = str(row["receipt_hash"] or "")
    computed_hash = _digest(_receipt_projection(row))
    if not _is_hash(actual_hash) or not hmac.compare_digest(
        actual_hash, computed_hash
    ):
        _contract_error(
            "exam_profile_resolved_receipt_hash_mismatch",
            "stored_profile_resolved_receipt_hash_mismatch",
            "已有真题画像解析回执完整性校验失败",
        )
    if any(row[field] != expected[field] for field in columns if field != "created_at"):
        _contract_error(
            "exam_profile_resolved_receipt_conflict",
            "stored_profile_resolved_receipt_conflict",
            "已有真题画像解析回执与当前冻结身份冲突",
        )


def _database_error(exc: sqlite3.DatabaseError) -> ExamGenerationAttemptBindingError:
    message = str(exc).casefold()
    if "locked" in message or "busy" in message:
        return ExamGenerationAttemptBindingError(
            "exam_attempt_binding_store_busy",
            "组卷画像绑定存储暂时繁忙，请稍后重试",
            status_code=503,
            retryable=True,
            reason="attempt_binding_store_busy",
            category="storage",
        )
    return ExamGenerationAttemptBindingError(
        "exam_attempt_binding_store_corrupt",
        "组卷画像绑定存储完整性校验失败",
        status_code=500,
        retryable=False,
        reason="attempt_binding_database_error",
        category="storage",
    )


def persist_profile_resolved_binding(
    db,
    *,
    job_id: str,
    profile_context: PastFrozenProfileContext,
    worker_id: str,
    lease_attempt: int,
    created_at: str | None = None,
) -> ProfileResolvedBinding:
    """CAS the attempt header and first receipt in one immediate transaction."""

    try:
        normalized_job_id = validate_generation_job_id(job_id)
    except ValueError as exc:
        raise ExamGenerationAttemptBindingError(
            "exam_attempt_binding_conflict",
            "组卷任务身份不能写入画像回执",
            status_code=409,
            retryable=False,
            reason="generation_job_id_invalid",
            category="contract",
        ) from exc
    if normalized_job_id is None:
        _contract_error(
            "exam_attempt_binding_conflict",
            "generation_job_id_missing",
            "组卷任务身份不能写入画像回执",
        )
    normalized_worker_id = str(worker_id or "").strip()
    if not normalized_worker_id or type(lease_attempt) is not int or lease_attempt < 1:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if db.in_transaction:
        raise ExamGenerationAttemptBindingError(
            "exam_attempt_binding_write_failed",
            "组卷画像绑定不能进入嵌套写事务",
            status_code=500,
            retryable=False,
            reason="nested_binding_transaction_forbidden",
            category="storage",
        )
    timestamp = str(
        created_at
        or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    )
    try:
        prepared = _load_authority(
            db,
            job_id=normalized_job_id,
            worker_id=normalized_worker_id,
            lease_attempt=lease_attempt,
            profile_context=profile_context,
            created_at=timestamp,
        )
        with immediate_transaction(db):
            current = _load_authority(
                db,
                job_id=normalized_job_id,
                worker_id=normalized_worker_id,
                lease_attempt=lease_attempt,
                profile_context=profile_context,
                created_at=timestamp,
            )
            if _publication_residue_exists(db, normalized_job_id):
                _published_recovery_required()
            if (
                dict(current.binding) != dict(prepared.binding)
                or dict(current.receipt) != dict(prepared.receipt)
            ):
                _contract_error(
                    "exam_attempt_binding_conflict",
                    "binding_authority_changed_before_write",
                    "组卷任务冻结身份在写入前发生冲突",
                )
            _cas_binding(db, current.binding)
            _cas_receipt(db, current.receipt)
    except (ExamGenerationAttemptBindingError, JobLeaseLost):
        raise
    except sqlite3.DatabaseError as exc:
        raise _database_error(exc) from exc

    return ProfileResolvedBinding(
        job_id=normalized_job_id,
        binding_hash=str(prepared.binding["binding_hash"]),
        resolution_hash=str(prepared.binding["resolution_hash"]),
        receipt_hash=str(prepared.receipt["receipt_hash"]),
    )


__all__ = [
    "BINDINGS_TABLE",
    "ExamGenerationAttemptBindingError",
    "PAST_V3_ATTEMPT_BINDING_VERSION",
    "PAST_V3_PROFILE_RESOLVED_RECEIPT_VERSION",
    "PROFILE_RESOLVED_STAGE",
    "ProfileResolvedBinding",
    "RECEIPTS_TABLE",
    "persist_profile_resolved_binding",
]
