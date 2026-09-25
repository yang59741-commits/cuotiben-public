"""Pure explicit activation and frozen exact-slot profile contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping

from .past_exam_profile_v3 import (
    PROFILE_V3_CONTRACT_VERSION,
    PROFILE_V3_SCHEMA_VERSION,
    PROFILE_V3_TYPE,
    ProfileV3ContractError,
    profile_combination_key,
    validate_profile_attestation,
    validate_profile_v3,
)


PROFILE_ACTIVATION_SCHEMA_VERSION = 1
PROFILE_ACTIVATION_CONTRACT_VERSION = "past-exam-profile-activation-v1"
PROFILE_ACTIVATION_ID_PREFIX = "pact:"
PROFILE_ACTIVATION_RECEIPT_VERSION = "past-exam-profile-activation-receipt-v1"
PROFILE_EXACT_SLOT_FREEZE_VERSION = "past-exam-exact-slot-freeze-v1"

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID = re.compile(r"^ppv3:([0-9a-f]{64})$")
_ACTIVATION_ID = re.compile(r"^pact:([0-9a-f]{64})$")
_REGISTRY_KEYS = frozenset({"schema_version", "contract_version", "entries"})
_ENTRY_KEYS = frozenset({
    "combination",
    "revision",
    "active_activation_id",
    "activations",
    "transitions",
})
_COMBINATION_KEYS = frozenset({"institution", "subject", "reference_year"})
_BINDING_KEYS = frozenset({
    "activation_id",
    "combination_key",
    "profile_id",
    "profile_hash",
    "attestation_hash",
})
_ATTESTED_KEYS = frozenset({"profile", "attestation"})
_RECEIPT_KEYS = frozenset({
    "schema_version",
    "contract_version",
    "operation",
    "combination_key",
    "entry_revision_before",
    "entry_revision_after",
    "previous_activation_id",
    "active_activation_id",
    "profile_id",
    "profile_hash",
    "attestation_hash",
    "occurred_at",
    "receipt_hash",
})
_FROZEN_KEYS = frozenset({
    "schema_version",
    "contract_version",
    "combination",
    "combination_key",
    "activation_id",
    "profile_id",
    "profile_hash",
    "attestation_hash",
    "entry_revision",
    "activation_receipt_hash",
    "exact_slot_hash",
    "frozen_ref_hash",
})


def _canonical(value: Any, *, code: str = "profile_v3_not_verified") -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProfileV3ContractError(code, "画像激活合同无法规范序列化") from exc


def _digest(value: Any, *, code: str = "profile_v3_not_verified") -> str:
    return hashlib.sha256(_canonical(value, code=code).encode("utf-8")).hexdigest()


def _clone(value: Any) -> Any:
    return json.loads(_canonical(value))


def _mapping(value: object, label: str, *, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileV3ContractError(code, f"{label}必须是对象")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    keys: frozenset[str],
    label: str,
    *,
    code: str,
) -> None:
    actual = set(value)
    if actual != set(keys):
        raise ProfileV3ContractError(
            code,
            f"{label}字段集合无效",
            details={
                "missing": sorted(set(keys) - actual),
                "unknown": sorted(str(item) for item in actual - set(keys)),
            },
        )


def _text(value: object, label: str, *, code: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProfileV3ContractError(code, f"{label}不能为空")
    return value.strip()


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            f"{label}必须是非负整数",
        )
    return value


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            f"{label}必须是 64 位小写哈希",
        )
    return value


def _profile_id(value: object) -> str:
    if type(value) is not str or _PROFILE_ID.fullmatch(value) is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像身份无效",
        )
    return value


def _activation_id(value: object, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if type(value) is not str or _ACTIVATION_ID.fullmatch(value) is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活身份无效",
        )
    return value


def _timestamp(value: object, label: str) -> str:
    text = _text(value, label, code="profile_v3_not_verified")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            f"{label}无效",
        ) from exc
    if parsed.tzinfo is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            f"{label}必须带时区",
        )
    return parsed.isoformat()


def _normalize_profile_combination(
    value: object,
    *,
    code: str,
    allow_identity_fields: bool = False,
) -> dict[str, Any]:
    combination = _mapping(
        value,
        "画像组合",
        code=code,
    )
    if not allow_identity_fields:
        _exact_keys(
            combination,
            _COMBINATION_KEYS,
            "画像组合",
            code=code,
        )
    institution = _text(
        combination.get("institution"),
        "院校",
        code=code,
    )
    subject = _text(
        combination.get("subject"),
        "学科",
        code=code,
    )
    year = combination.get("reference_year")
    if isinstance(year, bool) or not isinstance(year, int) or year < 1:
        raise ProfileV3ContractError(
            code,
            "参考年份必须是正整数",
        )
    return {
        "institution": institution,
        "subject": subject,
        "reference_year": year,
    }


def normalize_profile_combination(value: object) -> dict[str, Any]:
    """Normalize the selectable institution/subject/reference-year triple."""

    return _normalize_profile_combination(
        value,
        code="unsupported_profile_combination",
    )


def _combination_from_profile(profile: Mapping[str, Any]) -> dict[str, Any]:
    return _normalize_profile_combination(
        profile["identity"],
        code="profile_v3_not_verified",
        allow_identity_fields=True,
    )


def _activation_binding(
    combination_key: str,
    profile_id: str,
    profile_hash: str,
    attestation_hash: str,
) -> dict[str, str]:
    content = {
        "contract_version": PROFILE_ACTIVATION_CONTRACT_VERSION,
        "combination_key": combination_key,
        "profile_id": profile_id,
        "profile_hash": profile_hash,
        "attestation_hash": attestation_hash,
    }
    activation_id = PROFILE_ACTIVATION_ID_PREFIX + _digest(content)
    return {
        "activation_id": activation_id,
        "combination_key": combination_key,
        "profile_id": profile_id,
        "profile_hash": profile_hash,
        "attestation_hash": attestation_hash,
    }


def _normalize_binding(value: object, combination_key: str) -> dict[str, str]:
    binding = _mapping(
        value,
        "激活绑定",
        code="profile_v3_not_verified",
    )
    _exact_keys(
        binding,
        _BINDING_KEYS,
        "激活绑定",
        code="profile_v3_not_verified",
    )
    normalized = _activation_binding(
        _text(
            binding.get("combination_key"),
            "激活组合键",
            code="profile_v3_not_verified",
        ),
        _profile_id(binding.get("profile_id")),
        _hash(binding.get("profile_hash"), "画像哈希"),
        _hash(binding.get("attestation_hash"), "确认回执哈希"),
    )
    if normalized["combination_key"] != combination_key:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活绑定与画像组合不一致",
            details={"reason": "activation_combination_mismatch"},
        )
    if normalized["profile_id"] != f"ppv3:{normalized['profile_hash']}":
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活绑定的画像身份与哈希不一致",
            details={"reason": "activation_profile_hash_mismatch"},
        )
    if binding.get("activation_id") != normalized["activation_id"]:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活身份与不可变绑定不一致",
            details={"reason": "activation_id_mismatch"},
        )
    return normalized


def empty_activation_registry() -> dict[str, Any]:
    return {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_ACTIVATION_CONTRACT_VERSION,
        "entries": {},
    }


def validate_activation_registry(payload: object) -> dict[str, Any]:
    registry = _mapping(
        payload,
        "画像激活注册表",
        code="profile_v3_not_verified",
    )
    _exact_keys(
        registry,
        _REGISTRY_KEYS,
        "画像激活注册表",
        code="profile_v3_not_verified",
    )
    if (
        type(registry.get("schema_version")) is not int
        or registry.get("schema_version") != PROFILE_ACTIVATION_SCHEMA_VERSION
        or registry.get("contract_version") != PROFILE_ACTIVATION_CONTRACT_VERSION
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活注册表版本无效",
        )
    raw_entries = _mapping(
        registry.get("entries"),
        "画像激活条目",
        code="profile_v3_not_verified",
    )
    entries: dict[str, Any] = {}
    for raw_key in sorted(raw_entries, key=lambda item: str(item)):
        combination_key = _text(
            raw_key,
            "画像组合键",
            code="profile_v3_not_verified",
        )
        entry = _mapping(
            raw_entries[raw_key],
            "画像激活条目",
            code="profile_v3_not_verified",
        )
        _exact_keys(
            entry,
            _ENTRY_KEYS,
            "画像激活条目",
            code="profile_v3_not_verified",
        )
        combination = _normalize_profile_combination(
            entry.get("combination"),
            code="profile_v3_not_verified",
        )
        if profile_combination_key(combination) != combination_key:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "注册表键与画像组合不一致",
                details={"reason": "activation_combination_mismatch"},
            )
        raw_activations = _mapping(
            entry.get("activations"),
            "不可变激活集合",
            code="profile_v3_not_verified",
        )
        activations: dict[str, Any] = {}
        for raw_activation_id in sorted(
            raw_activations,
            key=lambda item: str(item),
        ):
            activation_id = _activation_id(raw_activation_id)
            binding = _normalize_binding(
                raw_activations[raw_activation_id],
                combination_key,
            )
            if activation_id != binding["activation_id"]:
                raise ProfileV3ContractError(
                    "profile_v3_not_verified",
                    "激活集合键与绑定身份不一致",
                )
            activations[activation_id] = binding
        revision = _nonnegative_int(entry.get("revision"), "激活修订号")
        raw_transitions = _mapping(
            entry.get("transitions"),
            "不可变激活前驱链",
            code="profile_v3_not_verified",
        )
        expected_transition_keys = {str(value) for value in range(1, revision + 1)}
        if set(raw_transitions) != expected_transition_keys:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "激活修订号与不可变前驱链不一致",
                details={"reason": "activation_revision_invalid"},
            )
        transitions: dict[str, Any] = {}
        predecessor: str | None = None
        previous_receipt: Mapping[str, Any] | None = None
        referenced_activations: set[str] = set()
        for transition_revision in range(1, revision + 1):
            transition_key = str(transition_revision)
            receipt = validate_activation_receipt(raw_transitions[transition_key])
            if (
                receipt["combination_key"] != combination_key
                or receipt["entry_revision_before"] != transition_revision - 1
                or receipt["entry_revision_after"] != transition_revision
                or receipt["previous_activation_id"] != predecessor
            ):
                raise ProfileV3ContractError(
                    "profile_v3_not_verified",
                    "激活前驱链不连续",
                    details={"reason": "activation_predecessor_mismatch"},
                )
            if receipt["operation"] == "rollback" and (
                previous_receipt is None
                or previous_receipt["operation"] != "activate"
                or receipt["active_activation_id"]
                != previous_receipt["previous_activation_id"]
            ):
                raise ProfileV3ContractError(
                    "profile_v3_not_verified",
                    "回滚前驱链没有恢复紧邻激活的上一身份",
                    details={"reason": "activation_rollback_target_mismatch"},
                )
            if previous_receipt is not None and datetime.fromisoformat(
                receipt["occurred_at"]
            ) < datetime.fromisoformat(previous_receipt["occurred_at"]):
                raise ProfileV3ContractError(
                    "profile_v3_not_verified",
                    "激活前驱链时间倒退",
                    details={"reason": "activation_transition_time_invalid"},
                )
            successor = receipt["active_activation_id"]
            binding = activations.get(successor)
            if binding is None or any(
                receipt[key] != binding[key]
                for key in ("profile_id", "profile_hash", "attestation_hash")
            ):
                raise ProfileV3ContractError(
                    "profile_v3_not_verified",
                    "激活前驱链引用了不一致的画像绑定",
                    details={"reason": "activation_transition_binding_mismatch"},
                )
            transitions[transition_key] = receipt
            referenced_activations.add(successor)
            predecessor = successor
            previous_receipt = receipt
        active_id = _activation_id(
            entry.get("active_activation_id"),
            optional=True,
        )
        if active_id != predecessor:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "当前激活指针与不可变前驱链不一致",
                details={"reason": "dangling_activation_pointer"},
            )
        if set(activations) != referenced_activations:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "不可变激活集合包含未经前驱链证明的身份",
                details={"reason": "unreferenced_activation_binding"},
            )
        entries[combination_key] = {
            "combination": combination,
            "revision": revision,
            "active_activation_id": active_id,
            "activations": activations,
            "transitions": transitions,
        }
    return {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_ACTIVATION_CONTRACT_VERSION,
        "entries": entries,
    }


def parse_attested_profile_v3(envelope: object) -> dict[str, Any]:
    """Validate one explicit profile-plus-attestation immutable artifact."""

    value = _mapping(
        envelope,
        "已确认画像制品",
        code="profile_v3_not_verified",
    )
    _exact_keys(
        value,
        _ATTESTED_KEYS,
        "已确认画像制品",
        code="profile_v3_not_verified",
    )
    raw_profile = _mapping(
        value.get("profile"),
        "画像 v3",
        code="profile_v3_not_verified",
    )
    if (
        type(raw_profile.get("schema_version")) is not int
        or raw_profile.get("schema_version") != PROFILE_V3_SCHEMA_VERSION
        or raw_profile.get("contract_version") != PROFILE_V3_CONTRACT_VERSION
        or raw_profile.get("profile_type") != PROFILE_V3_TYPE
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "正式激活只接受 schema v3 画像",
        )
    supplied_id = _profile_id(raw_profile.get("profile_id"))
    supplied_hash = _hash(raw_profile.get("profile_hash"), "画像哈希")
    profile = validate_profile_v3(raw_profile)
    if supplied_id != profile["profile_id"] or supplied_hash != profile["profile_hash"]:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像显式身份与规范内容不一致",
        )
    attestation = validate_profile_attestation(
        profile,
        value.get("attestation"),
    )
    attestation_hash = _digest(attestation)
    combination = _combination_from_profile(profile)
    combination_key = profile_combination_key(combination)
    binding = _activation_binding(
        combination_key,
        profile["profile_id"],
        profile["profile_hash"],
        attestation_hash,
    )
    return {
        "profile": profile,
        "attestation": attestation,
        "attestation_hash": attestation_hash,
        "combination": combination,
        "combination_key": combination_key,
        "activation_id": binding["activation_id"],
    }


def _resolve_binding_artifact(
    binding: Mapping[str, Any],
    artifact_map: object,
) -> dict[str, Any]:
    artifacts = _mapping(
        artifact_map,
        "显式画像制品映射",
        code="profile_v3_not_verified",
    )
    activation_id = binding["activation_id"]
    if activation_id not in artifacts:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "当前激活画像制品缺失",
            details={"reason": "active_artifact_missing"},
        )
    parsed = parse_attested_profile_v3(artifacts[activation_id])
    expected = _activation_binding(
        parsed["combination_key"],
        parsed["profile"]["profile_id"],
        parsed["profile"]["profile_hash"],
        parsed["attestation_hash"],
    )
    if dict(binding) != expected:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活指针与画像制品不一致",
            details={"reason": "active_artifact_hash_mismatch"},
        )
    return parsed


def resolve_selectable_profile(
    registry: object,
    artifact_map: object,
    combination: object,
) -> dict[str, Any]:
    """Resolve only the explicit active activation for a selectable triple."""

    normalized_registry = validate_activation_registry(registry)
    normalized_combination = normalize_profile_combination(combination)
    combination_key = profile_combination_key(normalized_combination)
    entry = normalized_registry["entries"].get(combination_key)
    if entry is None or entry["active_activation_id"] is None:
        raise ProfileV3ContractError(
            "unsupported_profile_combination",
            "该院校、学科与参考年份没有已激活画像 v3",
            details={
                "reason": "missing_active_profile_v3",
                "combination_key": combination_key,
            },
        )
    binding = entry["activations"][entry["active_activation_id"]]
    parsed = _resolve_binding_artifact(binding, artifact_map)
    if parsed["combination"] != normalized_combination:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活画像与请求组合不一致",
            details={"reason": "active_profile_combination_mismatch"},
        )
    return {
        **parsed,
        "activation_id": binding["activation_id"],
        "entry_revision": entry["revision"],
        "activation_receipt_hash": entry["transitions"][str(entry["revision"])][
            "receipt_hash"
        ],
        "generation_eligible": True,
    }


def profile_availability(
    registry: object,
    artifact_map: object,
    combination: object,
) -> dict[str, Any]:
    """Return the same fail-closed availability decision for API and UI."""

    combination_key = None
    try:
        normalized = normalize_profile_combination(combination)
        combination_key = profile_combination_key(normalized)
        resolved = resolve_selectable_profile(registry, artifact_map, normalized)
    except ProfileV3ContractError as exc:
        details = dict(exc.details)
        return {
            "usable": False,
            "available": False,
            "code": exc.code,
            "reason": details.get("reason", exc.code),
            "retryable": False,
            "combination_key": details.get("combination_key", combination_key),
            "profile_id": None,
        }
    return {
        "usable": True,
        "available": True,
        "code": None,
        "reason": None,
        "retryable": False,
        "combination_key": combination_key,
        "profile_id": resolved["profile"]["profile_id"],
    }


def _cas_expected(
    entry: Mapping[str, Any],
    expected_entry_revision: object,
    expected_active_activation_id: object,
) -> None:
    expected_revision = _nonnegative_int(
        expected_entry_revision,
        "期望激活修订号",
    )
    expected_active = _activation_id(
        expected_active_activation_id,
        optional=True,
    )
    if (
        entry["revision"] != expected_revision
        or entry["active_activation_id"] != expected_active
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活 CAS 冲突",
            details={"reason": "activation_cas_conflict"},
        )


def _receipt(
    *,
    operation: str,
    combination_key: str,
    revision_before: int,
    previous_activation_id: str | None,
    active_binding: Mapping[str, str],
    occurred_at: object,
) -> dict[str, Any]:
    content = {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_ACTIVATION_RECEIPT_VERSION,
        "operation": operation,
        "combination_key": combination_key,
        "entry_revision_before": revision_before,
        "entry_revision_after": revision_before + 1,
        "previous_activation_id": previous_activation_id,
        "active_activation_id": active_binding["activation_id"],
        "profile_id": active_binding["profile_id"],
        "profile_hash": active_binding["profile_hash"],
        "attestation_hash": active_binding["attestation_hash"],
        "occurred_at": _timestamp(occurred_at, "激活操作时间"),
    }
    return {**content, "receipt_hash": _digest(content)}


def validate_activation_receipt(payload: object) -> dict[str, Any]:
    receipt = _mapping(
        payload,
        "画像激活回执",
        code="profile_v3_not_verified",
    )
    _exact_keys(
        receipt,
        _RECEIPT_KEYS,
        "画像激活回执",
        code="profile_v3_not_verified",
    )
    operation = receipt.get("operation")
    if operation not in {"activate", "rollback"}:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执操作无效",
        )
    revision_before = _nonnegative_int(
        receipt.get("entry_revision_before"),
        "回执变更前修订号",
    )
    revision_after = _nonnegative_int(
        receipt.get("entry_revision_after"),
        "回执变更后修订号",
    )
    if revision_after != revision_before + 1:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执修订号不连续",
        )
    content = {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_ACTIVATION_RECEIPT_VERSION,
        "operation": operation,
        "combination_key": _text(
            receipt.get("combination_key"),
            "回执组合键",
            code="profile_v3_not_verified",
        ),
        "entry_revision_before": revision_before,
        "entry_revision_after": revision_after,
        "previous_activation_id": _activation_id(
            receipt.get("previous_activation_id"),
            optional=True,
        ),
        "active_activation_id": _activation_id(
            receipt.get("active_activation_id")
        ),
        "profile_id": _profile_id(receipt.get("profile_id")),
        "profile_hash": _hash(receipt.get("profile_hash"), "回执画像哈希"),
        "attestation_hash": _hash(
            receipt.get("attestation_hash"),
            "回执确认哈希",
        ),
        "occurred_at": _timestamp(receipt.get("occurred_at"), "回执操作时间"),
    }
    if receipt.get("contract_version") != PROFILE_ACTIVATION_RECEIPT_VERSION:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执版本无效",
        )
    if (
        type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != PROFILE_ACTIVATION_SCHEMA_VERSION
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执模式无效",
        )
    if content["profile_id"] != f"ppv3:{content['profile_hash']}":
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "回执画像身份与哈希不一致",
        )
    expected_binding = _activation_binding(
        content["combination_key"],
        content["profile_id"],
        content["profile_hash"],
        content["attestation_hash"],
    )
    if content["active_activation_id"] != expected_binding["activation_id"]:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "回执激活身份与画像绑定不一致",
        )
    if content["previous_activation_id"] == content["active_activation_id"]:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执不得记录无变化切换",
        )
    if receipt.get("receipt_hash") != _digest(content):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像激活回执哈希不一致",
        )
    return {**content, "receipt_hash": receipt["receipt_hash"]}


def cas_activate_profile(
    registry: object,
    attested_profile: object,
    *,
    expected_entry_revision: object,
    expected_active_activation_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Plan one atomic compare-and-swap of an explicit active pointer."""

    normalized_registry = validate_activation_registry(registry)
    parsed = parse_attested_profile_v3(attested_profile)
    operation_time = _timestamp(occurred_at, "激活操作时间")
    if datetime.fromisoformat(operation_time) < datetime.fromisoformat(
        parsed["attestation"]["confirmed_at"]
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像必须先确认再激活",
            details={"reason": "activation_precedes_attestation"},
        )
    combination_key = parsed["combination_key"]
    current = normalized_registry["entries"].get(combination_key, {
        "combination": parsed["combination"],
        "revision": 0,
        "active_activation_id": None,
        "activations": {},
        "transitions": {},
    })
    _cas_expected(
        current,
        expected_entry_revision,
        expected_active_activation_id,
    )
    if current["revision"] and datetime.fromisoformat(operation_time) < (
        datetime.fromisoformat(
            current["transitions"][str(current["revision"])]["occurred_at"]
        )
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活操作时间不能早于当前前驱链",
            details={"reason": "activation_transition_time_invalid"},
        )
    binding = _activation_binding(
        combination_key,
        parsed["profile"]["profile_id"],
        parsed["profile"]["profile_hash"],
        parsed["attestation_hash"],
    )
    if current["active_activation_id"] == binding["activation_id"]:
        return {
            "changed": False,
            "registry": normalized_registry,
            "receipt": None,
            "activation_id": binding["activation_id"],
            "artifact": {
                "profile": parsed["profile"],
                "attestation": parsed["attestation"],
            },
        }
    receipt = _receipt(
        operation="activate",
        combination_key=combination_key,
        revision_before=current["revision"],
        previous_activation_id=current["active_activation_id"],
        active_binding=binding,
        occurred_at=operation_time,
    )
    entries = _clone(normalized_registry["entries"])
    activations = _clone(current["activations"])
    activations[binding["activation_id"]] = binding
    transitions = _clone(current["transitions"])
    transitions[str(current["revision"] + 1)] = receipt
    updated_entry = {
        "combination": parsed["combination"],
        "revision": current["revision"] + 1,
        "active_activation_id": binding["activation_id"],
        "activations": dict(sorted(activations.items())),
        "transitions": transitions,
    }
    entries[combination_key] = updated_entry
    updated_registry = validate_activation_registry({
        **normalized_registry,
        "entries": dict(sorted(entries.items())),
    })
    return {
        "changed": True,
        "registry": updated_registry,
        "receipt": receipt,
        "activation_id": binding["activation_id"],
        "artifact": {
            "profile": parsed["profile"],
            "attestation": parsed["attestation"],
        },
    }


def cas_rollback_activation(
    registry: object,
    artifact_map: object,
    activation_receipt: object,
    *,
    expected_entry_revision: object,
    expected_active_activation_id: object,
    occurred_at: object,
) -> dict[str, Any]:
    """Restore only the exact previous activation named by an activation receipt."""

    normalized_registry = validate_activation_registry(registry)
    receipt = validate_activation_receipt(activation_receipt)
    if receipt["operation"] != "activate":
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "回滚只接受原始激活回执",
        )
    previous_id = receipt["previous_activation_id"]
    if previous_id is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "首次激活没有可回滚的上一显式身份",
            details={"reason": "previous_activation_missing"},
        )
    entry = normalized_registry["entries"].get(receipt["combination_key"])
    if entry is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "回滚目标组合不存在",
        )
    _cas_expected(
        entry,
        expected_entry_revision,
        expected_active_activation_id,
    )
    if (
        entry["revision"] != receipt["entry_revision_after"]
        or entry["active_activation_id"] != receipt["active_activation_id"]
        or entry["transitions"].get(str(entry["revision"])) != receipt
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "激活回执已过期，不能猜测回滚目标",
            details={"reason": "activation_cas_conflict"},
        )
    previous_binding = entry["activations"].get(previous_id)
    if previous_binding is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "上一显式激活身份不存在",
            details={"reason": "previous_activation_missing"},
        )
    rollback_time = _timestamp(occurred_at, "回滚操作时间")
    if datetime.fromisoformat(rollback_time) < datetime.fromisoformat(
        receipt["occurred_at"]
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "回滚操作时间不能早于原激活回执",
        )
    parsed_previous = _resolve_binding_artifact(previous_binding, artifact_map)
    rollback_receipt = _receipt(
        operation="rollback",
        combination_key=receipt["combination_key"],
        revision_before=entry["revision"],
        previous_activation_id=receipt["active_activation_id"],
        active_binding=previous_binding,
        occurred_at=rollback_time,
    )
    entries = _clone(normalized_registry["entries"])
    updated_entry = entries[receipt["combination_key"]]
    updated_entry["revision"] = entry["revision"] + 1
    updated_entry["active_activation_id"] = previous_id
    updated_entry["transitions"][str(entry["revision"] + 1)] = rollback_receipt
    updated_registry = validate_activation_registry({
        **normalized_registry,
        "entries": dict(sorted(entries.items())),
    })
    return {
        "changed": True,
        "registry": updated_registry,
        "receipt": rollback_receipt,
        "restored_activation_id": previous_id,
        "resolved": {
            **parsed_previous,
            "activation_id": previous_id,
            "entry_revision": entry["revision"] + 1,
            "activation_receipt_hash": rollback_receipt["receipt_hash"],
            "generation_eligible": True,
        },
    }


def _exact_slot_projection(parsed: Mapping[str, Any]) -> dict[str, Any]:
    profile = parsed["profile"]
    return {
        "profile_id": profile["profile_id"],
        "identity": profile["identity"],
        "sections": profile["sections"],
        "slots": profile["slots"],
        "scoring": profile["scoring"],
    }


def exact_slot_hash_for_attested_profile(payload: object) -> str:
    """Hash only the v3 fields that define the authoritative slot contract."""

    parsed = parse_attested_profile_v3(payload)
    return _digest(_exact_slot_projection(parsed))


def freeze_active_exact_slot_profile(
    registry: object,
    artifact_map: object,
    combination: object,
) -> dict[str, Any]:
    """Freeze one active immutable identity; do not embed a mutable current pointer."""

    resolved = resolve_selectable_profile(registry, artifact_map, combination)
    content = {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_EXACT_SLOT_FREEZE_VERSION,
        "combination": resolved["combination"],
        "combination_key": resolved["combination_key"],
        "activation_id": resolved["activation_id"],
        "profile_id": resolved["profile"]["profile_id"],
        "profile_hash": resolved["profile"]["profile_hash"],
        "attestation_hash": resolved["attestation_hash"],
        "entry_revision": resolved["entry_revision"],
        "activation_receipt_hash": resolved["activation_receipt_hash"],
        "exact_slot_hash": _digest(_exact_slot_projection(resolved)),
    }
    return {**content, "frozen_ref_hash": _digest(content)}


def validate_frozen_exact_slot_ref(payload: object) -> dict[str, Any]:
    frozen = _mapping(
        payload,
        "冻结画像引用",
        code="profile_v3_not_verified",
    )
    _exact_keys(
        frozen,
        _FROZEN_KEYS,
        "冻结画像引用",
        code="profile_v3_not_verified",
    )
    if (
        type(frozen.get("schema_version")) is not int
        or frozen.get("schema_version") != PROFILE_ACTIVATION_SCHEMA_VERSION
        or frozen.get("contract_version") != PROFILE_EXACT_SLOT_FREEZE_VERSION
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像引用版本无效",
        )
    combination = _normalize_profile_combination(
        frozen.get("combination"),
        code="profile_v3_not_verified",
    )
    combination_key = profile_combination_key(combination)
    if frozen.get("combination_key") != combination_key:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结引用与画像组合不一致",
        )
    profile_id = _profile_id(frozen.get("profile_id"))
    profile_hash = _hash(frozen.get("profile_hash"), "冻结画像哈希")
    if profile_id != f"ppv3:{profile_hash}":
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像身份与哈希不一致",
        )
    content = {
        "schema_version": PROFILE_ACTIVATION_SCHEMA_VERSION,
        "contract_version": PROFILE_EXACT_SLOT_FREEZE_VERSION,
        "combination": combination,
        "combination_key": combination_key,
        "activation_id": _activation_id(frozen.get("activation_id")),
        "profile_id": profile_id,
        "profile_hash": profile_hash,
        "attestation_hash": _hash(
            frozen.get("attestation_hash"),
            "冻结确认哈希",
        ),
        "entry_revision": _nonnegative_int(
            frozen.get("entry_revision"),
            "冻结激活修订号",
        ),
        "activation_receipt_hash": _hash(
            frozen.get("activation_receipt_hash"),
            "冻结激活回执哈希",
        ),
        "exact_slot_hash": _hash(
            frozen.get("exact_slot_hash"),
            "逐槽合同哈希",
        ),
    }
    expected_binding = _activation_binding(
        combination_key,
        profile_id,
        profile_hash,
        content["attestation_hash"],
    )
    if content["activation_id"] != expected_binding["activation_id"]:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结激活身份与画像绑定不一致",
        )
    if frozen.get("frozen_ref_hash") != _digest(content):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像引用哈希不一致",
        )
    return {**content, "frozen_ref_hash": frozen["frozen_ref_hash"]}


def resolve_frozen_exact_slot_profile(
    frozen_ref: object,
    registry: object,
    artifact_map: object,
) -> dict[str, Any]:
    """Resolve a freeze against immutable history, never the current pointer."""

    frozen = validate_frozen_exact_slot_ref(frozen_ref)
    normalized_registry = validate_activation_registry(registry)
    entry = normalized_registry["entries"].get(frozen["combination_key"])
    transition = (
        entry["transitions"].get(str(frozen["entry_revision"]))
        if entry is not None
        else None
    )
    if (
        transition is None
        or transition["receipt_hash"] != frozen["activation_receipt_hash"]
        or transition["active_activation_id"] != frozen["activation_id"]
        or transition["profile_id"] != frozen["profile_id"]
        or transition["profile_hash"] != frozen["profile_hash"]
        or transition["attestation_hash"] != frozen["attestation_hash"]
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像缺少不可变激活历史证明",
            details={"reason": "frozen_activation_proof_missing"},
        )
    artifacts = _mapping(
        artifact_map,
        "显式画像制品映射",
        code="profile_v3_not_verified",
    )
    activation_id = frozen["activation_id"]
    if activation_id not in artifacts:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像制品缺失",
            details={"reason": "frozen_artifact_missing"},
        )
    parsed = parse_attested_profile_v3(artifacts[activation_id])
    binding = _activation_binding(
        parsed["combination_key"],
        parsed["profile"]["profile_id"],
        parsed["profile"]["profile_hash"],
        parsed["attestation_hash"],
    )
    if (
        binding["activation_id"] != activation_id
        or parsed["combination"] != frozen["combination"]
        or parsed["combination_key"] != frozen["combination_key"]
        or binding["profile_id"] != frozen["profile_id"]
        or binding["profile_hash"] != frozen["profile_hash"]
        or binding["attestation_hash"] != frozen["attestation_hash"]
        or _digest(_exact_slot_projection(parsed)) != frozen["exact_slot_hash"]
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "冻结画像引用与不可变制品不一致",
            details={"reason": "frozen_artifact_hash_mismatch"},
        )
    return {
        **parsed,
        "activation_id": activation_id,
        "entry_revision": frozen["entry_revision"],
        "generation_eligible": True,
        "frozen_ref": frozen,
    }


def slot_by_position(
    frozen_ref: object,
    registry: object,
    artifact_map: object,
    position: object,
) -> dict[str, Any]:
    """Read one slot only after immutable activation-history resolution."""

    profile = resolve_frozen_exact_slot_profile(
        frozen_ref,
        registry,
        artifact_map,
    )["profile"]
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "题位必须是正整数",
        )
    for slot in profile["slots"]:
        if slot["position"] == position:
            return _clone(slot)
    raise ProfileV3ContractError(
        "scoring_schema_invalid",
        "题位超出画像逐槽合同",
        details={"position": position},
    )


__all__ = [
    "PROFILE_ACTIVATION_CONTRACT_VERSION",
    "PROFILE_ACTIVATION_ID_PREFIX",
    "PROFILE_ACTIVATION_RECEIPT_VERSION",
    "PROFILE_ACTIVATION_SCHEMA_VERSION",
    "PROFILE_EXACT_SLOT_FREEZE_VERSION",
    "cas_activate_profile",
    "cas_rollback_activation",
    "empty_activation_registry",
    "exact_slot_hash_for_attested_profile",
    "freeze_active_exact_slot_profile",
    "normalize_profile_combination",
    "parse_attested_profile_v3",
    "profile_availability",
    "resolve_frozen_exact_slot_profile",
    "resolve_selectable_profile",
    "slot_by_position",
    "validate_activation_receipt",
    "validate_activation_registry",
    "validate_frozen_exact_slot_ref",
]
