"""Worker-only resolution of a persisted past-profile freeze."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from .exam_contracts import positive_int
from .exam_generation_profile_freeze import (
    PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION,
    profile_freeze_database_error,
)
from .past_exam_profile_activation import normalize_profile_combination
from .past_exam_profile_activation_store import (
    ProfileActivationStoreError,
    resolve_historical_frozen_exact_slot_profile_from_database,
)
from .past_exam_profile_v3 import ProfileV3ContractError


PAST_PROFILE_WORKER_CONTEXT_VERSION = "past-profile-worker-context-v1"
PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION = "past-profile-v3-exact-slot-context-v1"

_SAFE_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_FORBIDDEN_IDENTITY_HINTS = frozenset(
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
    }
)


class ExamGenerationProfileWorkerError(RuntimeError):
    """Stable failure before a worker may enter the legacy past pipeline."""

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
class PastFrozenProfileContext:
    """Immutable, canonical bridge for the future v3-native generation path."""

    resolution_hash: str
    frozen_ref_hash: str
    profile_hash: str
    exact_slot_hash: str
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _safe_reason(value: object, fallback: str) -> str:
    reason = str(value or "").strip().casefold()
    return reason if _SAFE_REASON.fullmatch(reason) else fallback


def _missing(reason: str) -> None:
    raise ExamGenerationProfileWorkerError(
        "past_profile_freeze_missing",
        "旧真题任务缺少不可变画像冻结身份，请重新提交",
        status_code=409,
        retryable=False,
        reason=reason,
        category="contract",
        actions=("resubmit_past_exam",),
    )


def _invalid(reason: str) -> None:
    raise ExamGenerationProfileWorkerError(
        "profile_v3_not_verified",
        "任务冻结的画像 v3 未通过完整性验证",
        status_code=409,
        retryable=False,
        reason=reason,
        category="data",
        actions=("resubmit_past_exam",),
    )


def _map_profile_error(exc: ProfileV3ContractError) -> None:
    reason = _safe_reason(
        exc.details.get("reason", exc.code),
        "profile_v3_not_verified",
    )
    raise ExamGenerationProfileWorkerError(
        "profile_v3_not_verified",
        "任务冻结的画像 v3 未通过完整性验证",
        status_code=409,
        retryable=False,
        reason=reason,
        category="data",
        actions=("resubmit_past_exam",),
    ) from exc


def _map_store_error(exc: ProfileActivationStoreError) -> None:
    raise ExamGenerationProfileWorkerError(
        exc.code,
        "画像激活历史暂时不可读取"
        if exc.retryable
        else "画像激活历史不可读取",
        status_code=503 if exc.retryable else 500,
        retryable=exc.retryable,
        reason=_safe_reason(exc.code, "profile_activation_store_failed"),
        category="storage",
    ) from exc


def _map_database_error(exc: sqlite3.DatabaseError) -> None:
    mapped = profile_freeze_database_error(exc)
    raise ExamGenerationProfileWorkerError(
        mapped.code,
        str(mapped),
        status_code=503 if mapped.retryable else 500,
        retryable=mapped.retryable,
        reason=mapped.reason,
        category="storage",
    ) from exc


def _profile_context(resolved: Mapping[str, Any]) -> PastFrozenProfileContext:
    profile = resolved["profile"]
    frozen_ref = resolved["frozen_ref"]
    identity = profile["identity"]
    positions_by_section: dict[str, list[int]] = {
        str(section["section_id"]): [] for section in profile["sections"]
    }
    for slot in profile["slots"]:
        positions_by_section[str(slot["section_id"])].append(int(slot["position"]))
    exact_slot_contract = {
        "contract_version": PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION,
        "question_count": len(profile["slots"]),
        "sections": [
            {
                "section_id": section["section_id"],
                "title": section["title"],
                "positions": positions_by_section[str(section["section_id"])],
                "question_count": len(
                    positions_by_section[str(section["section_id"])]
                ),
                "points": section["points"],
                "points_evidence_hash": section["points_evidence_hash"],
            }
            for section in profile["sections"]
        ],
        "slots": profile["slots"],
        "scoring": profile["scoring"],
        "exact_slot_hash": frozen_ref["exact_slot_hash"],
    }
    profile_binding = {
        "combination": resolved["combination"],
        "activation_id": resolved["activation_id"],
        "entry_revision": resolved["entry_revision"],
        "activation_receipt_hash": resolved["activation_receipt_hash"],
        "profile_id": profile["profile_id"],
        "profile_hash": profile["profile_hash"],
        "attestation_hash": resolved["attestation_hash"],
        "exact_slot_hash": frozen_ref["exact_slot_hash"],
        "frozen_ref_hash": frozen_ref["frozen_ref_hash"],
        "source_snapshot": identity["source_snapshot"],
        "source_exam_id": identity["source_exam_id"],
        "catalog_version": identity["catalog_version"],
        "course_version": identity["course_version"],
    }
    hash_content = {
        "contract_version": PAST_PROFILE_WORKER_CONTEXT_VERSION,
        "profile_binding": profile_binding,
        "exact_slot_contract": exact_slot_contract,
    }
    resolution_hash = _digest(hash_content)
    payload = {
        **hash_content,
        "resolution_hash": resolution_hash,
        "past_profile_frozen_ref": frozen_ref,
    }
    return PastFrozenProfileContext(
        resolution_hash=resolution_hash,
        frozen_ref_hash=str(frozen_ref["frozen_ref_hash"]),
        profile_hash=str(profile["profile_hash"]),
        exact_slot_hash=str(frozen_ref["exact_slot_hash"]),
        _payload_json=_canonical(payload),
    )


def _resolve_subject_bound_historical_profile(
    db,
    *,
    subject_id: int,
    combination: Mapping[str, Any],
    frozen_ref: object,
) -> Mapping[str, Any]:
    rows = db.execute(
        "SELECT id,name FROM subjects WHERE id=?",
        (subject_id,),
    ).fetchall()
    if len(rows) != 1:
        _invalid("past_profile_subject_not_in_catalog")
    subject_name = str(rows[0]["name"] or "").strip()
    if subject_name != combination["subject"]:
        _invalid("past_profile_subject_identity_mismatch")
    return resolve_historical_frozen_exact_slot_profile_from_database(
        db,
        frozen_ref,
    )


def resolve_persisted_past_profile_for_attempt(
    db,
    payload: object,
) -> PastFrozenProfileContext | None:
    """Resolve a past worker attempt from job input history only.

    Non-past requests are a strict no-op.  No current pointer, directory,
    latest/mtime rule, legacy profile, model, memory or manifest is consulted.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("任务输入必须是 JSON 对象")
    if str(payload.get("source") or "free") != "past":
        return None
    if _FORBIDDEN_IDENTITY_HINTS.intersection(payload):
        _invalid("past_profile_identity_hint_forbidden")
    for key in (
        "past_profile_freeze_contract_version",
        "profile_combination",
        "past_profile_combination",
        "past_profile_frozen_ref",
    ):
        if key not in payload or payload.get(key) is None:
            _missing(f"{key}_missing")
    if (
        payload.get("past_profile_freeze_contract_version")
        != PAST_PROFILE_JOB_FREEZE_CONTRACT_VERSION
    ):
        raise ExamGenerationProfileWorkerError(
            "past_profile_freeze_contract_unsupported",
            "真题任务的画像冻结合同版本不受支持，请重新提交",
            status_code=409,
            retryable=False,
            reason="past_profile_freeze_contract_unsupported",
            category="contract",
            actions=("resubmit_past_exam",),
        )
    try:
        profile_combination = normalize_profile_combination(
            payload.get("profile_combination")
        )
        past_combination = normalize_profile_combination(
            payload.get("past_profile_combination")
        )
    except ProfileV3ContractError as exc:
        _map_profile_error(exc)
    if profile_combination != past_combination:
        _invalid("past_profile_combination_mismatch")
    if (
        payload.get("institution") != past_combination["institution"]
        or payload.get("reference_year") != past_combination["reference_year"]
    ):
        _invalid("past_profile_request_binding_mismatch")
    try:
        try:
            subject_id = positive_int(payload.get("subject_id"), "真题科目 ID")
        except ValueError:
            _invalid("past_profile_subject_id_invalid")

        if db.in_transaction:
            resolved = _resolve_subject_bound_historical_profile(
                db,
                subject_id=subject_id,
                combination=past_combination,
                frozen_ref=payload.get("past_profile_frozen_ref"),
            )
        else:
            db.execute("BEGIN")
            try:
                resolved = _resolve_subject_bound_historical_profile(
                    db,
                    subject_id=subject_id,
                    combination=past_combination,
                    frozen_ref=payload.get("past_profile_frozen_ref"),
                )
            finally:
                db.rollback()
    except ProfileV3ContractError as exc:
        _map_profile_error(exc)
    except ProfileActivationStoreError as exc:
        _map_store_error(exc)
    except sqlite3.DatabaseError as exc:
        _map_database_error(exc)
    if resolved["combination"] != past_combination:
        _invalid("past_profile_resolved_combination_mismatch")
    return _profile_context(resolved)


__all__ = [
    "ExamGenerationProfileWorkerError",
    "PAST_PROFILE_EXACT_SLOT_CONTEXT_VERSION",
    "PAST_PROFILE_WORKER_CONTEXT_VERSION",
    "PastFrozenProfileContext",
    "resolve_persisted_past_profile_for_attempt",
]
