"""Frozen G0 contracts for semantic-index settings and operations APIs.

This module is intentionally transport- and storage-independent.  G1 and later
stages may consume these contracts, but importing it cannot read the database,
dispatch a provider request, or mutate settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping


OPERATIONS_SCHEMA = "semantic-index-operations-v1"
SETTINGS_SCHEMA = "semantic-index-settings-v1"
ERROR_SCHEMA = "semantic-index-error-v1"

RECALL_MODES = frozenset({"off", "shadow", "enforced"})
ACTIONS = ("refresh", "pause", "resume", "rebuild", "cleanup")
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?:^|[^a-z0-9_])[a-z]:[\\/][^\r\n\t\"']+")
LOCKED_RESUME_STATUSES = frozenset(
    {"paused_budget_review", "paused_token_hard_limit", "outcome_unknown"}
)
ACTIVE_RUN_STATUSES = frozenset({"scanning", "queued", "running"})

DEFAULT_SEMANTIC_INDEX_SETTINGS = MappingProxyType(
    {
        "schema": SETTINGS_SCHEMA,
        "recall_mode": "shadow",
        "max_concurrency": 1,
        "auto_refresh_enabled": False,
        "allow_partial_cache": False,
        "candidate_multiplier": 4,
        "retention": {
            "run_history_days": 30,
            "snapshot_history_count": 3,
        },
    }
)

SEMANTIC_ACTIVATION_SCHEMA = "semantic-index-activation-v2"
LEGACY_SEMANTIC_ACTIVATION_SCHEMA = "semantic-index-activation-v1"
DEFAULT_SEMANTIC_ACTIVATION = MappingProxyType(
    {
        "schema": SEMANTIC_ACTIVATION_SCHEMA,
        "provider": False,
        "auto_refresh": False,
        "allow_partial": False,
    }
)

SETTINGS_FIELDS = frozenset(DEFAULT_SEMANTIC_INDEX_SETTINGS)
RETENTION_FIELDS = frozenset({"run_history_days", "snapshot_history_count"})

# These names must never appear anywhere in a public operations response.
FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "question",
        "question_text",
        "stem",
        "card",
        "card_json",
        "prompt",
        "response",
        "raw_response",
        "api_key",
        "secret",
        "credential_ref",
        "provider_request_id",
        "absolute_path",
        "database_path",
        "source_path",
    }
)

OVERVIEW_FIELDS = frozenset(
    {
        "schema",
        "settings_revision",
        "settings",
        "source_snapshot",
        "semantic_snapshot",
        "projection",
        "counts",
        "coverage",
        "latest_run",
        "job_state",
        "concurrency",
        "tokens",
        "stop",
        "last_error",
        "capabilities",
    }
)

ERROR_STATUS = MappingProxyType(
    {
        "semantic_validation": 400,
        "semantic_json_required": 415,
        "semantic_origin_rejected": 403,
        "semantic_csrf_rejected": 403,
        "semantic_settings_conflict": 409,
        "semantic_active_job_conflict": 409,
        "semantic_capability_denied": 409,
        "semantic_confirmation_required": 409,
        "semantic_idempotency_conflict": 409,
        "semantic_not_found": 404,
        "semantic_state_corrupt": 503,
    }
)


class SemanticOperationsContractError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.status_code = ERROR_STATUS.get(code, 400)


def _require_plain_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SemanticOperationsContractError(
            "semantic_validation", f"{field} 必须是整数"
        )
    if not minimum <= value <= maximum:
        raise SemanticOperationsContractError(
            "semantic_validation", f"{field} 必须在 {minimum} 到 {maximum} 之间"
        )
    return value


def validate_semantic_index_settings(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the complete persisted ``semantic_index`` node."""

    if not isinstance(value, Mapping):
        raise SemanticOperationsContractError(
            "semantic_validation", "semantic_index 必须是对象"
        )
    unknown = set(value) - SETTINGS_FIELDS
    missing = SETTINGS_FIELDS - set(value)
    if unknown or missing:
        detail = "未知字段" if unknown else "缺少字段"
        names = ", ".join(sorted(unknown or missing))
        raise SemanticOperationsContractError(
            "semantic_validation", f"semantic_index {detail}: {names}"
        )
    if value["schema"] != SETTINGS_SCHEMA:
        raise SemanticOperationsContractError(
            "semantic_validation", "semantic_index schema 不受支持"
        )
    recall_mode = value["recall_mode"]
    if recall_mode not in RECALL_MODES:
        raise SemanticOperationsContractError(
            "semantic_validation", "recall_mode 必须是 off、shadow 或 enforced"
        )
    for name in ("auto_refresh_enabled", "allow_partial_cache"):
        if not isinstance(value[name], bool):
            raise SemanticOperationsContractError(
                "semantic_validation", f"{name} 必须是布尔值"
            )
    retention = value["retention"]
    if not isinstance(retention, Mapping) or set(retention) != RETENTION_FIELDS:
        raise SemanticOperationsContractError(
            "semantic_validation", "retention 仅允许固定的非路径保留策略"
        )
    return {
        "schema": SETTINGS_SCHEMA,
        "recall_mode": recall_mode,
        "max_concurrency": _require_plain_int(
            value["max_concurrency"], "max_concurrency", 1, 2
        ),
        "auto_refresh_enabled": value["auto_refresh_enabled"],
        "allow_partial_cache": value["allow_partial_cache"],
        "candidate_multiplier": _require_plain_int(
            value["candidate_multiplier"], "candidate_multiplier", 1, 10
        ),
        "retention": {
            "run_history_days": _require_plain_int(
                retention["run_history_days"], "run_history_days", 7, 365
            ),
            "snapshot_history_count": _require_plain_int(
                retention["snapshot_history_count"],
                "snapshot_history_count",
                2,
                20,
            ),
        },
    }


@dataclass(frozen=True, slots=True)
class CapabilityContext:
    run_status: str | None = None
    semantic_job_active: bool = False
    exam_generation_active: bool = False
    provider_enabled: bool = False
    has_projection: bool = False
    cleanup_preview_ready: bool = False
    implementation_enabled: bool = True


def _capability(enabled: bool, reason: str | None, **extra: Any) -> dict[str, Any]:
    return {"enabled": enabled, "reason": reason, **extra}


def derive_capabilities(context: CapabilityContext) -> dict[str, dict[str, Any]]:
    """Derive every UI operation capability exclusively on the server side."""

    if not isinstance(context, CapabilityContext):
        raise SemanticOperationsContractError(
            "semantic_validation", "capability context 类型错误"
        )
    active_conflict = context.semantic_job_active or context.exam_generation_active
    unavailable = not context.implementation_enabled
    mutation_reason = (
        "not_implemented"
        if unavailable
        else "active_job_conflict"
        if active_conflict
        else None
    )
    paused = context.run_status == "paused"
    locked_resume = context.run_status in LOCKED_RESUME_STATUSES
    running = context.run_status in ACTIVE_RUN_STATUSES

    refresh_reason = mutation_reason or (
        None if context.provider_enabled else "provider_disabled"
    )
    rebuild_reason = refresh_reason
    pause_reason = "not_implemented" if unavailable else (
        None if running else "no_pausable_run"
    )
    resume_reason = "not_implemented" if unavailable else (
        "manual_review_required"
        if locked_resume
        else "active_job_conflict"
        if active_conflict
        else None
        if paused
        else "no_resumable_run"
    )
    cleanup_reason = mutation_reason or (
        None if context.cleanup_preview_ready else "preview_required"
    )
    return {
        "refresh": _capability(refresh_reason is None, refresh_reason),
        "pause": _capability(pause_reason is None, pause_reason),
        "resume": _capability(resume_reason is None, resume_reason),
        "rebuild": _capability(
            rebuild_reason is None,
            rebuild_reason,
            confirmation_required=True,
        ),
        "cleanup": _capability(
            cleanup_reason is None,
            cleanup_reason,
            confirmation_required=True,
            destructive=True,
        ),
    }


def assert_public_payload_safe(value: Any, path: str = "$") -> None:
    """Reject secret/content-bearing keys and absolute Windows paths recursively."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_PUBLIC_FIELDS:
                raise SemanticOperationsContractError(
                    "semantic_validation", f"公开响应包含禁止字段: {path}.{key}"
                )
            assert_public_payload_safe(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            assert_public_payload_safe(nested, f"{path}[{index}]")
    elif isinstance(value, str):
        text = value.strip()
        if _WINDOWS_ABSOLUTE_PATH.search(text):
            raise SemanticOperationsContractError(
                "semantic_validation", f"公开响应包含本机绝对路径: {path}"
            )


def validate_overview_response(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the frozen top-level shape of the future read-only G1 response."""

    if not isinstance(value, Mapping) or not OVERVIEW_FIELDS <= set(value) or set(value) - OVERVIEW_FIELDS - {"settings_state"}:
        raise SemanticOperationsContractError(
            "semantic_validation", "运维总览响应字段与冻结 schema 不一致"
        )
    if value["schema"] != OPERATIONS_SCHEMA:
        raise SemanticOperationsContractError(
            "semantic_validation", "运维总览 schema 不受支持"
        )
    _require_plain_int(value["settings_revision"], "settings_revision", 0, 2**63 - 1)
    validate_semantic_index_settings(value["settings"])
    capabilities = value["capabilities"]
    if not isinstance(capabilities, Mapping) or set(capabilities) != set(ACTIONS):
        raise SemanticOperationsContractError(
            "semantic_validation", "capabilities 必须覆盖全部冻结操作"
        )
    for action, capability in capabilities.items():
        if not isinstance(capability, Mapping) or not isinstance(
            capability.get("enabled"), bool
        ) or "reason" not in capability:
            raise SemanticOperationsContractError(
                "semantic_validation", f"{action} capability 结构无效"
            )
    assert_public_payload_safe(value)
    return dict(value)


def error_envelope(
    code: str,
    message: str,
    correlation_id: str,
    *,
    retryable: bool = False,
    hint: str = "",
) -> dict[str, Any]:
    if code not in ERROR_STATUS:
        raise SemanticOperationsContractError(
            "semantic_validation", "错误码未在 G0 合同中登记"
        )
    envelope = {
        "schema": ERROR_SCHEMA,
        "code": code,
        "message": str(message)[:800],
        "retryable": bool(retryable),
        "hint": str(hint)[:800],
        "correlation_id": str(correlation_id),
    }
    assert_public_payload_safe(envelope)
    return envelope
