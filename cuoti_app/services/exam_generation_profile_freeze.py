"""Atomic submission freeze for persisted active past-exam profile v3."""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .exam_contracts import positive_int
from .past_exam_profile_activation import normalize_profile_combination
from .past_exam_profile_activation_store import (
    ProfileActivationStoreError,
    freeze_active_exact_slot_profile_in_database,
    resolve_frozen_exact_slot_profile_from_database,
)
from .past_exam_profile_v3 import ProfileV3ContractError


PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION = "past-profile-job-freeze-v1"

_SERVER_OWNED_KEYS = frozenset(
    {
        "activation_id",
        "profile_id",
        "profile_hash",
        "attestation_hash",
        "entry_revision",
        "activation_receipt_hash",
        "exact_slot_hash",
        "frozen_ref_hash",
        "generation_eligible",
        "past_profile_combination",
        "past_profile_frozen_ref",
        "past_profile_freeze_contract_version",
    }
)


class ExamGenerationProfileFreezeError(ValueError):
    """Stable, public-safe failure before a past generation job is created."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        retryable: bool,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.reason = str(reason)


def _invalid(reason: str = "invalid_profile_combination") -> None:
    raise ExamGenerationProfileFreezeError(
        "invalid_profile_combination",
        "院校、学科与参考年份组合无效",
        status_code=400,
        retryable=False,
        reason=reason,
    )


def normalize_profile_freeze_request(payload: object) -> dict[str, Any]:
    """Reject client-owned activation hints without reading persistence."""

    if not isinstance(payload, Mapping):
        raise ValueError("任务输入必须是 JSON 对象")
    normalized = dict(payload)
    if str(normalized.get("source") or "free") != "past":
        return normalized
    if _SERVER_OWNED_KEYS.intersection(normalized):
        _invalid("client_profile_identity_forbidden")
    nested = normalized.get("profile_combination")
    if nested is not None:
        try:
            normalized["profile_combination"] = normalize_profile_combination(
                nested
            )
        except ProfileV3ContractError:
            _invalid()
    return normalized


def _subject_name_for_id(db, subject_id: object) -> tuple[int, str]:
    try:
        normalized_id = positive_int(subject_id, "科目 ID")
    except ValueError:
        _invalid("profile_subject_id_invalid")
    rows = db.execute(
        "SELECT id,name FROM subjects WHERE id=?",
        (normalized_id,),
    ).fetchall()
    if len(rows) != 1:
        _invalid("profile_subject_not_in_catalog")
    name = str(rows[0]["name"] or "").strip()
    if not name:
        _invalid("profile_subject_not_in_catalog")
    return normalized_id, name


def _combination_in_transaction(db, payload: Mapping[str, Any]) -> tuple[dict, int]:
    nested = payload.get("profile_combination")
    if nested is not None:
        try:
            combination = normalize_profile_combination(nested)
        except ProfileV3ContractError:
            _invalid()
        rows = db.execute(
            "SELECT id,name FROM subjects WHERE name=?",
            (combination["subject"],),
        ).fetchall()
        if len(rows) != 1:
            _invalid("profile_subject_not_in_catalog")
        subject_id = int(rows[0]["id"])
        if payload.get("subject_id") not in (None, ""):
            supplied_id, supplied_name = _subject_name_for_id(
                db, payload.get("subject_id")
            )
            if supplied_id != subject_id or supplied_name != combination["subject"]:
                _invalid("profile_subject_identity_conflict")
        for key in ("institution", "reference_year"):
            supplied = payload.get(key)
            if supplied not in (None, "") and supplied != combination[key]:
                _invalid("profile_combination_conflict")
        return combination, subject_id

    institution = payload.get("institution")
    reference_year = payload.get("reference_year")
    subject_id, subject = _subject_name_for_id(db, payload.get("subject_id"))
    try:
        combination = normalize_profile_combination(
            {
                "institution": institution,
                "subject": subject,
                "reference_year": reference_year,
            }
        )
    except ProfileV3ContractError:
        _invalid()
    return combination, subject_id


def _map_contract_error(exc: ProfileV3ContractError) -> None:
    reason = str(exc.details.get("reason", exc.code))
    if exc.code == "unsupported_profile_combination":
        raise ExamGenerationProfileFreezeError(
            "unsupported_profile_combination",
            "该院校、学科与参考年份没有已激活画像 v3",
            status_code=422,
            retryable=False,
            reason=reason,
        ) from exc
    raise ExamGenerationProfileFreezeError(
        "profile_v3_not_verified",
        "已激活画像未通过完整性验证",
        status_code=409,
        retryable=False,
        reason=reason,
    ) from exc


def _map_store_error(exc: ProfileActivationStoreError) -> None:
    corrupt = exc.code == "profile_activation_store_corrupt"
    raise ExamGenerationProfileFreezeError(
        exc.code,
        "画像激活存储暂时不可读取"
        if exc.retryable
        else "画像激活存储不可读取",
        status_code=500 if corrupt else 503,
        retryable=exc.retryable,
        reason=exc.code,
    ) from exc


def profile_freeze_database_error(
    exc: sqlite3.DatabaseError,
) -> ExamGenerationProfileFreezeError:
    """Return the public-safe contract error for any freeze-store DB failure."""

    message = str(exc).casefold()
    busy = isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    )
    return ExamGenerationProfileFreezeError(
        "profile_activation_store_busy"
        if busy
        else "profile_activation_store_failed",
        "画像激活存储暂时不可读取" if busy else "画像激活存储不可读取",
        status_code=503,
        retryable=busy,
        reason=(
            "profile_activation_store_busy"
            if busy
            else "profile_activation_store_failed"
        ),
    )


def freeze_exam_generation_profile_in_transaction(
    db,
    payload: object,
) -> dict[str, Any]:
    """Freeze current v3 into the exact transaction that inserts the job."""

    normalized = normalize_profile_freeze_request(payload)
    if str(normalized.get("source") or "free") != "past":
        return normalized
    if not db.in_transaction:
        raise RuntimeError("past profile freeze requires an active transaction")
    try:
        combination, subject_id = _combination_in_transaction(db, normalized)
        frozen_ref = freeze_active_exact_slot_profile_in_database(
            db, combination
        )
        resolved = resolve_frozen_exact_slot_profile_from_database(
            db, frozen_ref
        )
    except ExamGenerationProfileFreezeError:
        raise
    except ProfileV3ContractError as exc:
        _map_contract_error(exc)
    except ProfileActivationStoreError as exc:
        _map_store_error(exc)
    except sqlite3.DatabaseError as exc:
        raise profile_freeze_database_error(exc) from exc
    profile_identity = resolved["profile"]["identity"]
    if any(
        profile_identity[key] != combination[key]
        for key in ("institution", "subject", "reference_year")
    ):
        raise ExamGenerationProfileFreezeError(
            "profile_v3_not_verified",
            "冻结画像与请求组合不一致",
            status_code=409,
            retryable=False,
            reason="frozen_profile_combination_mismatch",
        )
    return {
        **normalized,
        "institution": combination["institution"],
        "subject_id": subject_id,
        "reference_year": combination["reference_year"],
        "profile_combination": combination,
        "past_profile_freeze_contract_version": (
            PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION
        ),
        "past_profile_combination": combination,
        "past_profile_frozen_ref": frozen_ref,
    }


__all__ = [
    "ExamGenerationProfileFreezeError",
    "PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION",
    "freeze_exam_generation_profile_in_transaction",
    "normalize_profile_freeze_request",
    "profile_freeze_database_error",
]
