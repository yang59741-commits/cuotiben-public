"""Transactional SQLite storage for explicit exact-slot profile activations.

The database is a durable projection of the pure activation contract.  Every
read reconstructs and validates that contract; no row order, timestamp, file,
or directory is used to guess an active profile.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from ..transactions import immediate_transaction
from .past_exam_profile_activation import (
    PROFILE_ACTIVATION_CONTRACT_VERSION,
    PROFILE_ACTIVATION_RECEIPT_VERSION,
    PROFILE_ACTIVATION_SCHEMA_VERSION,
    cas_activate_profile,
    cas_rollback_activation,
    empty_activation_registry,
    exact_slot_hash_for_attested_profile,
    freeze_active_exact_slot_profile,
    normalize_profile_combination,
    parse_attested_profile_v3,
    profile_availability,
    resolve_frozen_exact_slot_profile,
    resolve_selectable_profile,
    validate_activation_receipt,
    validate_activation_registry,
    validate_frozen_exact_slot_ref,
)
from .past_exam_profile_v3 import ProfileV3ContractError, profile_combination_key


BINDINGS_TABLE = "past_exam_profile_activation_bindings"
POINTERS_TABLE = "past_exam_profile_active_pointers"
TRANSITIONS_TABLE = "past_exam_profile_activation_transitions"

_BINDING_COLUMNS = (
    "activation_id",
    "combination_key",
    "profile_id",
    "profile_hash",
    "attestation_hash",
    "schema_version",
    "contract_version",
    "profile_json",
    "attestation_json",
)
_POINTER_COLUMNS = (
    "combination_key",
    "institution",
    "subject",
    "reference_year",
    "revision",
    "active_activation_id",
    "updated_at",
)
_TRANSITION_COLUMNS = (
    "combination_key",
    "revision",
    "schema_version",
    "contract_version",
    "operation",
    "entry_revision_before",
    "entry_revision_after",
    "previous_activation_id",
    "active_activation_id",
    "profile_id",
    "profile_hash",
    "attestation_hash",
    "occurred_at",
    "receipt_hash",
    "receipt_json",
)


class ProfileActivationStoreError(RuntimeError):
    """Stable, redacted repository failure."""

    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = str(code)
        self.retryable = bool(retryable)
        self.details = {"retryable": self.retryable}


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活制品无法规范序列化",
            details={"reason": "activation_store_json_invalid"},
        ) from exc


def _corrupt(reason: str, message: str) -> None:
    raise ProfileV3ContractError(
        "profile_v3_not_verified",
        message,
        details={"reason": reason},
    )


def _json_object(raw: object, *, component: str) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        _corrupt(
            "activation_store_json_invalid",
            f"持久化{component}不是规范 JSON",
        )
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            f"持久化{component} JSON 无效",
            details={"reason": "activation_store_json_invalid"},
        ) from exc
    if not isinstance(value, Mapping):
        _corrupt(
            "activation_store_json_invalid",
            f"持久化{component} JSON 必须是对象",
        )
    return value


def _rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    names = tuple(str(column[0]) for column in (cursor.description or ()))
    return [dict(zip(names, tuple(row), strict=True)) for row in cursor.fetchall()]


def _select_rows(
    db: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    *,
    suffix: str = "",
    parameters: tuple[object, ...] = (),
) -> list[dict[str, Any]]:
    sql = f'SELECT {",".join(columns)} FROM "{table}"'
    if suffix:
        sql += f" {suffix}"
    return _rows(db.execute(sql, parameters))


def _binding_from_row(
    row: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    profile = _json_object(row.get("profile_json"), component="画像")
    attestation = _json_object(
        row.get("attestation_json"),
        component="画像确认",
    )
    parsed = parse_attested_profile_v3(
        {"profile": profile, "attestation": attestation}
    )
    expected = {
        "activation_id": parsed["activation_id"],
        "combination_key": parsed["combination_key"],
        "profile_id": parsed["profile"]["profile_id"],
        "profile_hash": parsed["profile"]["profile_hash"],
        "attestation_hash": parsed["attestation_hash"],
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_ACTIVATION_CONTRACT_VERSION,
        "profile_json": _canonical(parsed["profile"]),
        "attestation_json": _canonical(parsed["attestation"]),
    }
    if any(row.get(key) != value for key, value in expected.items()):
        _corrupt(
            "activation_store_binding_mismatch",
            "持久化画像制品与不可变激活身份不一致",
        )
    binding = {
        key: expected[key]
        for key in (
            "activation_id",
            "combination_key",
            "profile_id",
            "profile_hash",
            "attestation_hash",
        )
    }
    artifact = {
        "profile": parsed["profile"],
        "attestation": parsed["attestation"],
    }
    return binding, artifact


def _receipt_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    raw_receipt = _json_object(row.get("receipt_json"), component="激活回执")
    receipt = validate_activation_receipt(raw_receipt)
    expected = {
        "combination_key": receipt["combination_key"],
        "revision": receipt["entry_revision_after"],
        "schema_version": receipt["schema_version"],
        "contract_version": receipt["contract_version"],
        "operation": receipt["operation"],
        "entry_revision_before": receipt["entry_revision_before"],
        "entry_revision_after": receipt["entry_revision_after"],
        "previous_activation_id": receipt["previous_activation_id"],
        "active_activation_id": receipt["active_activation_id"],
        "profile_id": receipt["profile_id"],
        "profile_hash": receipt["profile_hash"],
        "attestation_hash": receipt["attestation_hash"],
        "occurred_at": receipt["occurred_at"],
        "receipt_hash": receipt["receipt_hash"],
        "receipt_json": _canonical(receipt),
    }
    if any(row.get(key) != value for key, value in expected.items()):
        _corrupt(
            "activation_store_transition_mismatch",
            "持久化激活回执与不可变前驱链不一致",
        )
    return receipt


def _pointer_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    try:
        combination = normalize_profile_combination(
            {
                "institution": row.get("institution"),
                "subject": row.get("subject"),
                "reference_year": row.get("reference_year"),
            }
        )
    except ProfileV3ContractError as exc:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "持久化激活指针的组合无效",
            details={"reason": "activation_store_pointer_mismatch"},
        ) from exc
    if (
        row.get("combination_key") != profile_combination_key(combination)
        or type(row.get("revision")) is not int
        or row["revision"] < 1
        or not isinstance(row.get("active_activation_id"), str)
        or not row["active_activation_id"]
        or not isinstance(row.get("updated_at"), str)
        or not row["updated_at"]
    ):
        _corrupt(
            "activation_store_pointer_mismatch",
            "持久化激活指针与组合身份不一致",
        )
    return {
        "combination": combination,
        "combination_key": row["combination_key"],
        "revision": row["revision"],
        "active_activation_id": row["active_activation_id"],
        "updated_at": row["updated_at"],
    }


def _load_activation_state_in_snapshot(
    db: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    binding_rows = _select_rows(
        db,
        BINDINGS_TABLE,
        _BINDING_COLUMNS,
        suffix="ORDER BY combination_key,activation_id",
    )
    pointer_rows = _select_rows(
        db,
        POINTERS_TABLE,
        _POINTER_COLUMNS,
        suffix="ORDER BY combination_key",
    )
    transition_rows = _select_rows(
        db,
        TRANSITIONS_TABLE,
        _TRANSITION_COLUMNS,
        suffix="ORDER BY combination_key,revision",
    )

    bindings_by_combination: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    artifact_map: dict[str, dict[str, Any]] = {}
    for row in binding_rows:
        binding, artifact = _binding_from_row(row)
        activation_id = binding["activation_id"]
        combination_key = binding["combination_key"]
        if activation_id in artifact_map:
            _corrupt(
                "activation_store_duplicate_binding",
                "持久化画像激活身份重复",
            )
        bindings_by_combination[combination_key][activation_id] = binding
        artifact_map[activation_id] = artifact

    pointers: dict[str, dict[str, Any]] = {}
    for row in pointer_rows:
        pointer = _pointer_from_row(row)
        combination_key = pointer["combination_key"]
        if combination_key in pointers:
            _corrupt(
                "activation_store_duplicate_pointer",
                "持久化画像组合存在多个当前指针",
            )
        pointers[combination_key] = pointer

    transitions_by_combination: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in transition_rows:
        receipt = _receipt_from_row(row)
        combination_key = receipt["combination_key"]
        revision_key = str(receipt["entry_revision_after"])
        if revision_key in transitions_by_combination[combination_key]:
            _corrupt(
                "activation_store_duplicate_transition",
                "持久化画像激活修订号重复",
            )
        transitions_by_combination[combination_key][revision_key] = receipt

    pointer_keys = set(pointers)
    if (
        set(bindings_by_combination) != pointer_keys
        or set(transitions_by_combination) != pointer_keys
    ):
        _corrupt(
            "activation_store_orphan_history",
            "持久化画像激活历史存在孤立制品或回执",
        )

    entries: dict[str, Any] = {}
    for combination_key in sorted(pointer_keys):
        pointer = pointers[combination_key]
        transitions = transitions_by_combination[combination_key]
        current_receipt = transitions.get(str(pointer["revision"]))
        if current_receipt is None or current_receipt["occurred_at"] != pointer["updated_at"]:
            _corrupt(
                "activation_store_pointer_mismatch",
                "当前画像激活指针缺少精确回执证明",
            )
        entries[combination_key] = {
            "combination": pointer["combination"],
            "revision": pointer["revision"],
            "active_activation_id": pointer["active_activation_id"],
            "activations": bindings_by_combination[combination_key],
            "transitions": transitions,
        }

    registry = validate_activation_registry(
        {
            **empty_activation_registry(),
            "entries": entries,
        }
    )
    return registry, artifact_map


def _load_activation_state(
    db: sqlite3.Connection,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Read all three projections from one SQLite snapshot."""

    if db.in_transaction:
        return _load_activation_state_in_snapshot(db)
    db.execute("BEGIN")
    try:
        return _load_activation_state_in_snapshot(db)
    finally:
        # A read snapshot owns no writes; rollback only releases that snapshot.
        db.rollback()


def _translate_database_error(exc: sqlite3.DatabaseError) -> None:
    message = str(exc).casefold()
    if isinstance(exc, sqlite3.OperationalError) and (
        "locked" in message or "busy" in message
    ):
        raise ProfileActivationStoreError(
            "profile_activation_store_busy",
            "画像激活存储忙，可稍后重试",
            retryable=True,
        ) from exc
    if isinstance(exc, sqlite3.IntegrityError):
        raise ProfileActivationStoreError(
            "profile_activation_store_corrupt",
            "画像激活存储约束校验失败",
            retryable=False,
        ) from exc
    raise ProfileActivationStoreError(
        "profile_activation_store_failed",
        "画像激活存储不可用",
        retryable=False,
    ) from exc


_T = TypeVar("_T")


def _database_read(call: Callable[[], _T]) -> _T:
    try:
        return call()
    except (ProfileV3ContractError, ProfileActivationStoreError):
        raise
    except sqlite3.DatabaseError as exc:
        _translate_database_error(exc)
    raise AssertionError("unreachable")


def load_activation_state_from_database(
    db: sqlite3.Connection,
) -> dict[str, Any]:
    """Rebuild the exact pure registry and immutable artifact map."""

    def load() -> dict[str, Any]:
        registry, artifacts = _load_activation_state(db)
        return {"registry": registry, "artifact_map": artifacts}

    return _database_read(load)


def _insert_or_verify_binding(
    db: sqlite3.Connection,
    artifact: Mapping[str, Any],
) -> str:
    parsed = parse_attested_profile_v3(artifact)
    values = (
        parsed["activation_id"],
        parsed["combination_key"],
        parsed["profile"]["profile_id"],
        parsed["profile"]["profile_hash"],
        parsed["attestation_hash"],
        PROFILE_ACTIVATION_SCHEMA_VERSION,
        PROFILE_ACTIVATION_CONTRACT_VERSION,
        _canonical(parsed["profile"]),
        _canonical(parsed["attestation"]),
    )
    db.execute(
        f'INSERT OR IGNORE INTO "{BINDINGS_TABLE}"('
        f'{",".join(_BINDING_COLUMNS)}) VALUES(?,?,?,?,?,?,?,?,?)',
        values,
    )
    rows = _select_rows(
        db,
        BINDINGS_TABLE,
        _BINDING_COLUMNS,
        suffix="WHERE activation_id=?",
        parameters=(parsed["activation_id"],),
    )
    if len(rows) != 1:
        _corrupt(
            "activation_store_binding_missing",
            "画像激活制品写入后不可精确读取",
        )
    binding, stored_artifact = _binding_from_row(rows[0])
    expected_binding = {
        "activation_id": parsed["activation_id"],
        "combination_key": parsed["combination_key"],
        "profile_id": parsed["profile"]["profile_id"],
        "profile_hash": parsed["profile"]["profile_hash"],
        "attestation_hash": parsed["attestation_hash"],
    }
    expected_artifact = {
        "profile": parsed["profile"],
        "attestation": parsed["attestation"],
    }
    if binding != expected_binding or stored_artifact != expected_artifact:
        _corrupt(
            "activation_store_binding_mismatch",
            "已存在的画像激活制品与请求不一致",
        )
    return parsed["activation_id"]


def _insert_transition(
    db: sqlite3.Connection,
    receipt: Mapping[str, Any],
) -> None:
    validated = validate_activation_receipt(receipt)
    values = (
        validated["combination_key"],
        validated["entry_revision_after"],
        validated["schema_version"],
        validated["contract_version"],
        validated["operation"],
        validated["entry_revision_before"],
        validated["entry_revision_after"],
        validated["previous_activation_id"],
        validated["active_activation_id"],
        validated["profile_id"],
        validated["profile_hash"],
        validated["attestation_hash"],
        validated["occurred_at"],
        validated["receipt_hash"],
        _canonical(validated),
    )
    db.execute(
        f'INSERT INTO "{TRANSITIONS_TABLE}"('
        f'{",".join(_TRANSITION_COLUMNS)}) VALUES('
        f'{",".join("?" for _ in _TRANSITION_COLUMNS)})',
        values,
    )


def _insert_first_pointer(
    db: sqlite3.Connection,
    combination: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> None:
    db.execute(
        f'INSERT INTO "{POINTERS_TABLE}"('
        "combination_key,institution,subject,reference_year,revision,"
        "active_activation_id,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            receipt["combination_key"],
            combination["institution"],
            combination["subject"],
            combination["reference_year"],
            receipt["entry_revision_after"],
            receipt["active_activation_id"],
            receipt["occurred_at"],
        ),
    )


def _update_pointer_cas(
    db: sqlite3.Connection,
    receipt: Mapping[str, Any],
    *,
    expected_revision: int,
    expected_active_activation_id: str,
) -> None:
    result = db.execute(
        f'UPDATE "{POINTERS_TABLE}" SET revision=?,active_activation_id=?,updated_at=? '
        "WHERE combination_key=? AND revision=? AND active_activation_id=?",
        (
            receipt["entry_revision_after"],
            receipt["active_activation_id"],
            receipt["occurred_at"],
            receipt["combination_key"],
            expected_revision,
            expected_active_activation_id,
        ),
    )
    if result.rowcount != 1:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活 CAS 冲突",
            details={"reason": "activation_cas_conflict"},
        )


def _verify_planned_registry(
    db: sqlite3.Connection,
    planned_registry: Mapping[str, Any],
) -> None:
    stored_registry, _artifacts = _load_activation_state(db)
    if stored_registry != planned_registry:
        _corrupt(
            "activation_store_commit_mismatch",
            "画像激活写入结果与纯合同计划不一致",
        )


def activate_profile_in_database(
    db: sqlite3.Connection,
    attested_profile: object,
    *,
    expected_entry_revision: object,
    expected_active_activation_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Atomically activate one explicit attested v3 artifact."""

    try:
        with immediate_transaction(db):
            registry, _artifacts = _load_activation_state(db)
            planned = cas_activate_profile(
                registry,
                attested_profile,
                expected_entry_revision=expected_entry_revision,
                expected_active_activation_id=expected_active_activation_id,
                occurred_at=occurred_at,
            )
            if not planned["changed"]:
                return planned

            activation_id = _insert_or_verify_binding(db, planned["artifact"])
            if activation_id != planned["activation_id"]:
                _corrupt(
                    "activation_store_binding_mismatch",
                    "画像激活计划与持久化身份不一致",
                )
            receipt = planned["receipt"]
            parsed = parse_attested_profile_v3(planned["artifact"])
            if receipt["entry_revision_before"] == 0:
                _insert_first_pointer(db, parsed["combination"], receipt)
                _insert_transition(db, receipt)
            else:
                _insert_transition(db, receipt)
                _update_pointer_cas(
                    db,
                    receipt,
                    expected_revision=receipt["entry_revision_before"],
                    expected_active_activation_id=receipt["previous_activation_id"],
                )
            _verify_planned_registry(db, planned["registry"])
            return planned
    except (ProfileV3ContractError, ProfileActivationStoreError):
        raise
    except sqlite3.DatabaseError as exc:
        _translate_database_error(exc)
    raise AssertionError("unreachable")


def rollback_profile_in_database(
    db: sqlite3.Connection,
    activation_receipt: object,
    *,
    expected_entry_revision: object,
    expected_active_activation_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Atomically restore the exact predecessor named by a current receipt."""

    try:
        with immediate_transaction(db):
            registry, artifacts = _load_activation_state(db)
            planned = cas_rollback_activation(
                registry,
                artifacts,
                activation_receipt,
                expected_entry_revision=expected_entry_revision,
                expected_active_activation_id=expected_active_activation_id,
                occurred_at=occurred_at,
            )
            receipt = planned["receipt"]
            _insert_transition(db, receipt)
            _update_pointer_cas(
                db,
                receipt,
                expected_revision=receipt["entry_revision_before"],
                expected_active_activation_id=receipt["previous_activation_id"],
            )
            _verify_planned_registry(db, planned["registry"])
            return planned
    except (ProfileV3ContractError, ProfileActivationStoreError):
        raise
    except sqlite3.DatabaseError as exc:
        _translate_database_error(exc)
    raise AssertionError("unreachable")


def read_attested_profile_by_activation_id(
    db: sqlite3.Connection,
    activation_id: object,
) -> dict[str, Any]:
    """Read one immutable artifact by its explicit identity, never by recency."""

    def read() -> dict[str, Any]:
        if not isinstance(activation_id, str) or not activation_id:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "画像激活身份无效",
                details={"reason": "activation_artifact_missing"},
            )
        rows = _select_rows(
            db,
            BINDINGS_TABLE,
            _BINDING_COLUMNS,
            suffix="WHERE activation_id=?",
            parameters=(activation_id,),
        )
        if len(rows) != 1:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "指定的画像激活制品不存在",
                details={"reason": "activation_artifact_missing"},
            )
        binding, artifact = _binding_from_row(rows[0])
        return {**artifact, "activation_id": binding["activation_id"]}

    return _database_read(read)


def resolve_selectable_profile_from_database(
    db: sqlite3.Connection,
    combination: object,
) -> dict[str, Any]:
    def resolve() -> dict[str, Any]:
        registry, artifacts = _load_activation_state(db)
        return resolve_selectable_profile(registry, artifacts, combination)

    return _database_read(resolve)


def profile_availability_from_database(
    db: sqlite3.Connection,
    combination: object,
) -> dict[str, Any]:
    def availability() -> dict[str, Any]:
        registry, artifacts = _load_activation_state(db)
        return profile_availability(registry, artifacts, combination)

    return _database_read(availability)


def freeze_active_exact_slot_profile_in_database(
    db: sqlite3.Connection,
    combination: object,
) -> dict[str, Any]:
    def freeze() -> dict[str, Any]:
        registry, artifacts = _load_activation_state(db)
        return freeze_active_exact_slot_profile(registry, artifacts, combination)

    return _database_read(freeze)


def resolve_frozen_exact_slot_profile_from_database(
    db: sqlite3.Connection,
    frozen_ref: object,
) -> dict[str, Any]:
    def resolve() -> dict[str, Any]:
        registry, artifacts = _load_activation_state(db)
        return resolve_frozen_exact_slot_profile(frozen_ref, registry, artifacts)

    return _database_read(resolve)


def resolve_historical_frozen_exact_slot_profile_from_database(
    db: sqlite3.Connection,
    frozen_ref: object,
) -> dict[str, Any]:
    """Resolve one frozen profile without reading the mutable current pointer.

    Worker attempts use only the content-addressed binding and the exact
    historical transition named by the persisted frozen reference.  This is
    intentionally separate from availability/current-pointer reads.
    """

    def resolve_in_snapshot() -> dict[str, Any]:
        frozen = validate_frozen_exact_slot_ref(frozen_ref)
        binding_rows = _select_rows(
            db,
            BINDINGS_TABLE,
            _BINDING_COLUMNS,
            suffix="WHERE activation_id=?",
            parameters=(frozen["activation_id"],),
        )
        if len(binding_rows) != 1:
            _corrupt(
                "frozen_artifact_missing",
                "冻结画像制品缺失",
            )
        binding, artifact = _binding_from_row(binding_rows[0])
        transition_rows = _select_rows(
            db,
            TRANSITIONS_TABLE,
            _TRANSITION_COLUMNS,
            suffix="WHERE combination_key=? AND revision=?",
            parameters=(
                frozen["combination_key"],
                frozen["entry_revision"],
            ),
        )
        if len(transition_rows) != 1:
            _corrupt(
                "frozen_activation_proof_missing",
                "冻结画像缺少不可变激活历史证明",
            )
        receipt = _receipt_from_row(transition_rows[0])
        parsed = parse_attested_profile_v3(artifact)
        if (
            binding["activation_id"] != frozen["activation_id"]
            or binding["combination_key"] != frozen["combination_key"]
            or binding["profile_id"] != frozen["profile_id"]
            or binding["profile_hash"] != frozen["profile_hash"]
            or binding["attestation_hash"] != frozen["attestation_hash"]
            or parsed["combination"] != frozen["combination"]
            or receipt["combination_key"] != frozen["combination_key"]
            or receipt["entry_revision_after"] != frozen["entry_revision"]
            or receipt["receipt_hash"] != frozen["activation_receipt_hash"]
            or receipt["active_activation_id"] != frozen["activation_id"]
            or receipt["profile_id"] != frozen["profile_id"]
            or receipt["profile_hash"] != frozen["profile_hash"]
            or receipt["attestation_hash"] != frozen["attestation_hash"]
            or exact_slot_hash_for_attested_profile(artifact)
            != frozen["exact_slot_hash"]
        ):
            _corrupt(
                "frozen_artifact_hash_mismatch",
                "冻结画像引用与不可变历史不一致",
            )
        return {
            **parsed,
            "activation_id": frozen["activation_id"],
            "entry_revision": frozen["entry_revision"],
            "activation_receipt_hash": frozen["activation_receipt_hash"],
            "generation_eligible": True,
            "frozen_ref": frozen,
        }

    def resolve() -> dict[str, Any]:
        if db.in_transaction:
            return resolve_in_snapshot()
        db.execute("BEGIN")
        try:
            return resolve_in_snapshot()
        finally:
            db.rollback()

    return _database_read(resolve)


__all__ = [
    "BINDINGS_TABLE",
    "POINTERS_TABLE",
    "TRANSITIONS_TABLE",
    "ProfileActivationStoreError",
    "activate_profile_in_database",
    "freeze_active_exact_slot_profile_in_database",
    "load_activation_state_from_database",
    "profile_availability_from_database",
    "read_attested_profile_by_activation_id",
    "resolve_frozen_exact_slot_profile_from_database",
    "resolve_historical_frozen_exact_slot_profile_from_database",
    "resolve_selectable_profile_from_database",
    "rollback_profile_in_database",
]
