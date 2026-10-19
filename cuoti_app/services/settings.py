"""Atomic local settings and OS-keyring credential management."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

try:
    import keyring as system_keyring
except ImportError:  # pragma: no cover - exercised only in incomplete installs
    system_keyring = None

from .ai_providers import (
    ANTHROPIC_PRESET,
    DEEPSEEK_PRESET,
    AIProviderError,
    create_provider,
    validate_dns_mode,
    validate_dns_servers,
    validate_provider_url,
    validate_wire_api,
)
from .ai_telemetry import record_usage_best_effort
from .ai_quality_metrics import (
    QualityMetricsError,
    load_or_create_installation_secret,
    task_hmac,
)
from .ai_task_contracts import TASK_CONTRACTS
from .windows_credentials import WindowsCredentialBackend
from .semantic_operations_contract import (
    DEFAULT_SEMANTIC_ACTIVATION,
    DEFAULT_SEMANTIC_INDEX_SETTINGS,
    LEGACY_SEMANTIC_ACTIVATION_SCHEMA,
    SEMANTIC_ACTIVATION_SCHEMA,
    validate_semantic_index_settings,
)


KEYRING_SERVICE = "cuoti-app.ai"
DEFAULT_PROFILE_ID = "deepseek"
SOUL_ECHO_PROFILE_ID = "soulecho"
CLAUDE_PROFILE_ID = "claude"
MONETWL_PROFILE_ID = "monetwl"
CUSTOM_PROFILE_ID = "custom"
PROFILE_DEFINITIONS = {
    DEFAULT_PROFILE_ID: {
        "name": "DeepSeek",
        "connection_type": "openai_compatible",
        "preset": DEEPSEEK_PRESET,
    },
    SOUL_ECHO_PROFILE_ID: {
        "name": "GPT（SoulEcho / NewAPI）",
        "connection_type": "newapi_channel_conn",
        "preset": {
            "provider": "openai_compatible",
            "wire_api": "responses",
            "base_url": "https://gateway-one.example/v1",
            "quality_model": "gpt-5.6-sol",
            "fast_model": "gpt-5.6-sol",
            "reasoning_effort": "high",
            "dns_mode": "proxy",
            "dns_servers": ["223.5.5.5", "119.29.29.29"],
        },
    },
    MONETWL_PROFILE_ID: {
        "name": "GPT（Monetwl）",
        "connection_type": "newapi_channel_conn",
        "preset": {
            "provider": "openai_compatible",
            "wire_api": "chat_completions",
            "base_url": "https://gateway-two.example/v1",
            "quality_model": "gpt-6.1-sol",
            "fast_model": "gpt-6-luna",
            "reasoning_effort": "",
            "dns_mode": "proxy",
            "dns_servers": ["223.5.5.5", "119.29.29.29"],
        },
    },
    CLAUDE_PROFILE_ID: {
        "name": "Claude（Anthropic）",
        "connection_type": "anthropic_messages",
        "preset": ANTHROPIC_PRESET,
    },
    CUSTOM_PROFILE_ID: {
        "name": "其他 OpenAI 兼容服务",
        "connection_type": "newapi_channel_conn",
        "preset": {
            "provider": "openai_compatible",
            "wire_api": "chat_completions",
            "base_url": "",
            "quality_model": "",
            "fast_model": "",
            "reasoning_effort": "",
            "dns_mode": "domestic",
            "dns_servers": ["223.5.5.5", "119.29.29.29"],
        },
    },
}
DEFAULT_ASSIGNMENTS = {
    "classification": "fast",
    "problem_analysis": "quality",
    "daily_analysis": "quality",
    "exam_generation": "quality",
}
DEFAULT_POLICY_ROLES = {
    name: contract.default_model_role for name, contract in TASK_CONTRACTS.items()
}
CAPABILITY_FIELDS = {
    "json_object",
    "thinking",
    "reasoning_effort",
}
CAPABILITY_LIMIT_FIELDS = {
    "context_window",
    "max_output_tokens",
}
PROFILE_CAPABILITY_DEFAULTS = {
    DEFAULT_PROFILE_ID: {
        "json_object": True,
        "thinking": True,
        "reasoning_effort": True,
        "context_window": 0,
        "max_output_tokens": 0,
    },
    SOUL_ECHO_PROFILE_ID: {
        "json_object": True,
        "thinking": True,
        "reasoning_effort": True,
        "context_window": 0,
        "max_output_tokens": 0,
    },
    CLAUDE_PROFILE_ID: {
        "json_object": False,
        "thinking": True,
        "reasoning_effort": True,
        "context_window": 0,
        "max_output_tokens": 0,
    },
    MONETWL_PROFILE_ID: {
        "json_object": False,
        "thinking": False,
        "reasoning_effort": False,
        "context_window": 0,
        "max_output_tokens": 0,
    },
    CUSTOM_PROFILE_ID: {
        "json_object": False,
        "thinking": False,
        "reasoning_effort": False,
        "context_window": 0,
        "max_output_tokens": 0,
    },
}
ENVIRONMENT_FIELDS = {
    "base_url": "DEEPSEEK_URL",
    "quality_model": "DEEPSEEK_MODEL",
    "fast_model": "DEEPSEEK_FLASH_MODEL",
    "api_key": "DEEPSEEK_API_KEY",
}

_NON_CHAT_MODEL_MARKERS = (
    "embedding",
    "moderation",
    "realtime",
    "transcribe",
    "whisper",
    "tts",
    "dall-e",
    "image",
    "sora",
    "computer-use",
)
_FAST_MODEL_MARKERS = ("flash", "haiku", "luna", "nano", "mini", "lite", "instant")
_BALANCED_MODEL_MARKERS = ("sonnet", "terra", "chat")
_QUALITY_MODEL_MARKERS = ("fable", "opus", "sol", "pro", "reasoner", "speciale", "r1")


def _model_speed_tier(model_id: str) -> str:
    candidate = str(model_id or "").casefold()
    if any(marker in candidate for marker in _FAST_MODEL_MARKERS):
        return "fast"
    if any(marker in candidate for marker in _BALANCED_MODEL_MARKERS):
        return "balanced"
    if any(marker in candidate for marker in _QUALITY_MODEL_MARKERS):
        return "quality"
    return "balanced"


def _chat_model_items(models: list[dict]) -> list[dict]:
    filtered = [
        item
        for item in models
        if not any(
            marker in str(item.get("id") or "").casefold()
            for marker in _NON_CHAT_MODEL_MARKERS
        )
    ]
    source = filtered or models
    return [
        {**item, "speed_tier": _model_speed_tier(str(item.get("id") or ""))}
        for item in source[:500]
    ]


class SettingsError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _default_keyring_backend():
    """Resolve a usable OS backend without relying on packaged entry points."""
    if os.name == "nt":
        try:
            return WindowsCredentialBackend()
        except Exception:
            pass
    if system_keyring is None:
        return None
    try:
        return system_keyring.get_keyring()
    except Exception:
        return system_keyring


def _profile_from_document(document: dict, profile_id: str) -> dict:
    ai_settings = document.get("ai")
    if not isinstance(ai_settings, dict):
        return {}
    profiles = ai_settings.get("profiles")
    if not isinstance(profiles, dict):
        return {}
    profile = profiles.get(profile_id)
    return dict(profile) if isinstance(profile, dict) else {}


class SettingsService:
    def __init__(self, app, *, keyring_backend=None):
        self.app = app
        self.path = Path(app.config["SETTINGS_PATH"])
        self.keyring = (
            keyring_backend
            if keyring_backend is not None
            else _default_keyring_backend()
        )
        self._lock = threading.RLock()

    def _telemetry_correlation(self, identity: str, stage: str) -> dict[str, str]:
        """Return correlation fields or an empty mapping when HMAC is unavailable."""

        try:
            secret = load_or_create_installation_secret(self.keyring)
            return {
                "task_hash": task_hmac(identity, secret),
                "stage": stage,
            }
        except (QualityMetricsError, AttributeError):
            return {}

    def _read_document(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except json.JSONDecodeError as exc:
            raise SettingsError(
                "INVALID_SETTINGS",
                f"本地配置文件不是有效 JSON（第 {exc.lineno} 行）",
                status_code=500,
            ) from exc
        except OSError as exc:
            raise SettingsError(
                "SETTINGS_IO", "无法读取本地配置文件", status_code=500
            ) from exc
        if not isinstance(value, dict):
            raise SettingsError(
                "INVALID_SETTINGS", "本地配置文件顶层必须是对象", status_code=500
            )
        return value

    def _write_document(self, document: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            try:
                temporary_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise SettingsError(
                "SETTINGS_IO", "无法保存本地配置文件", status_code=500
            ) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _semantic_activation(document: dict) -> dict[str, Any]:
        raw = document.get("semantic_index_activation")
        activation = dict(DEFAULT_SEMANTIC_ACTIVATION)
        if not isinstance(raw, dict):
            return activation
        schema = raw.get("schema")
        if schema not in {
            SEMANTIC_ACTIVATION_SCHEMA,
            LEGACY_SEMANTIC_ACTIVATION_SCHEMA,
        }:
            return activation
        if schema == SEMANTIC_ACTIVATION_SCHEMA:
            activation["provider"] = bool(raw.get("provider"))
        activation["auto_refresh"] = bool(raw.get("auto_refresh"))
        activation["allow_partial"] = bool(raw.get("allow_partial"))
        return activation

    def _semantic_ai_ready(self, document: dict) -> bool:
        try:
            profile_id, profile = self._stored_profile(document)
            key, _source = self._profile_credential(document, profile_id, profile)
            validate_provider_url(profile.get("base_url"))
            return bool(
                key.strip()
                and str(profile.get("quality_model") or "").strip()
                and str(profile.get("fast_model") or "").strip()
            )
        except (SettingsError, ValueError):
            return False

    def semantic_runtime_settings(self, *, provider_enabled: bool | None = None) -> dict[str, Any]:
        """Return requested and actually usable semantic settings.

        Activation is deliberately stored outside the v1 settings node so old
        settings files remain readable.  A historical ``true`` value is only a
        request until the user explicitly confirms it in a later save.
        """

        with self._lock:
            document = self._read_document()
        raw = document.get("semantic_index")
        if raw is None:
            requested = dict(DEFAULT_SEMANTIC_INDEX_SETTINGS)
            configured_mode = str(self.app.config.get("SEMANTIC_RECALL_MODE") or "")
            if configured_mode in {"off", "shadow", "enforced"}:
                requested["recall_mode"] = configured_mode
        else:
            requested = validate_semantic_index_settings(raw)
        activation = self._semantic_activation(document)
        provider = bool(
            self.app.config.get("SEMANTIC_INDEX_PROVIDER_ENABLED")
            if provider_enabled is None
            else provider_enabled
        )
        provider_managed = bool(
            self.app.config.get("SEMANTIC_INDEX_PROVIDER_MANAGED", False)
        )
        provider_source = str(
            self.app.config.get("SEMANTIC_INDEX_PROVIDER_SOURCE") or "default"
        )
        credential_configured = self._semantic_ai_ready(document)
        provider_effective = provider and credential_configured
        provider_reason = (
            None
            if provider_effective
            else "ai_credential_missing"
            if provider
            else "provider_managed_disabled"
            if provider_managed
            else "disabled"
        )
        auto_requested = bool(requested["auto_refresh_enabled"])
        partial_requested = bool(requested["allow_partial_cache"])
        auto_active = auto_requested and bool(activation["auto_refresh"])
        partial_active = partial_requested and bool(activation["allow_partial"])
        promotion = {
            "offline_baseline_passed": bool(
                self.app.config.get("SEMANTIC_RECALL_OFFLINE_BASELINE_PASSED", False)
            ),
            "structure_safety_passed": bool(
                self.app.config.get("SEMANTIC_RECALL_STRUCTURE_SAFETY_PASSED", False)
            ),
            "user_acceptance_approved": bool(
                self.app.config.get("SEMANTIC_RECALL_USER_ACCEPTANCE_APPROVED", False)
            ),
        }
        promotion["approved"] = all(promotion.values())
        effective_recall_mode = requested["recall_mode"]
        recall_reason = None
        if effective_recall_mode == "enforced" and not promotion["approved"]:
            effective_recall_mode = "shadow"
            recall_reason = "promotion_approval_required"
        algorithm_version = str(
            self.app.config.get("SEMANTIC_RECALL_ALGORITHM_VERSION")
            or "semantic-recall-v3"
        ).strip()
        return {
            "settings_revision": int(document.get("settings_revision") or 0),
            "requested": requested,
            "activation": activation,
            "provider": {
                "requested": provider,
                "effective": provider_effective,
                "managed": provider_managed,
                "source": provider_source,
                "credential_configured": credential_configured,
                "reason": provider_reason,
            },
            "rollout": {
                "algorithm_version": algorithm_version,
                "promotion": promotion,
            },
            "effective": {
                "recall_mode": effective_recall_mode,
                "algorithm_version": algorithm_version,
                "candidate_multiplier": requested["candidate_multiplier"],
                "provider_enabled": provider_effective,
                "auto_refresh_enabled": auto_active and provider_effective,
                "allow_partial_cache": partial_active,
            },
            "reasons": {
                "recall_mode": recall_reason,
                "provider_enabled": provider_reason,
                "auto_refresh_enabled": (
                    None
                    if auto_active and provider_effective
                    else provider_reason
                    if auto_active and not provider_effective
                    else "activation_required"
                    if auto_requested
                    else "disabled"
                ),
                "allow_partial_cache": (
                    None
                    if partial_active
                    else "activation_required"
                    if partial_requested
                    else "disabled"
                ),
            },
        }

    def remove_obsolete_local_fields(self, *field_names: str) -> bool:
        """Atomically remove retired top-level settings without reading their values."""

        with self._lock:
            document = self._read_document()
            changed = False
            for name in field_names:
                if name in document:
                    document.pop(name, None)
                    changed = True
            if not changed:
                return False
            document["settings_revision"] = int(document.get("settings_revision") or 0) + 1
            self._write_document(document)
            return True

    def update_semantic_index_settings(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or not {"version", "settings"} <= set(payload) or set(payload) - {"version", "settings", "activation"}:
            raise SettingsError("VALIDATION", "语义索引设置请求字段无效")
        try:
            normalized = validate_semantic_index_settings(payload["settings"])
        except ValueError as exc:
            raise SettingsError("VALIDATION", str(exc)) from exc
        with self._lock:
            document = self._read_document()
            revision = int(document.get("settings_revision") or 0)
            self._expected_revision(payload, revision)
            old_settings = document.get("semantic_index")
            try:
                old_settings = validate_semantic_index_settings(old_settings) if old_settings is not None else dict(DEFAULT_SEMANTIC_INDEX_SETTINGS)
            except ValueError:
                old_settings = dict(DEFAULT_SEMANTIC_INDEX_SETTINGS)
            old_activation = self._semantic_activation(document)
            requested_activation = payload.get("activation")
            if requested_activation is not None and (
                not isinstance(requested_activation, dict)
                or set(requested_activation)
                - {"provider", "auto_refresh", "allow_partial"}
                or any(not isinstance(requested_activation.get(name), bool) for name in requested_activation)
            ):
                raise SettingsError("VALIDATION", "activation 必须是布尔确认对象")
            # Preserve an already confirmed capability when its request remains
            # true; new true values require an explicit confirmation in payload.
            activation = {
                "schema": SEMANTIC_ACTIVATION_SCHEMA,
                "provider": bool(old_activation.get("provider")),
                "auto_refresh": False,
                "allow_partial": False,
            }
            if requested_activation is not None and "provider" in requested_activation:
                requested_provider = bool(requested_activation["provider"])
                if self.app.config.get("SEMANTIC_INDEX_PROVIDER_MANAGED"):
                    if requested_provider != bool(
                        self.app.config.get("SEMANTIC_INDEX_PROVIDER_ENABLED")
                    ):
                        raise SettingsError(
                            "SEMANTIC_PROVIDER_MANAGED",
                            "语义分析由环境变量或启动配置管理，不能在页面修改",
                            status_code=409,
                        )
                else:
                    if requested_provider and not self._semantic_ai_ready(document):
                        raise SettingsError(
                            "SEMANTIC_PROVIDER_NOT_READY",
                            "请先在 AI 服务中保存可用的密钥和模型配置",
                            status_code=409,
                        )
                    activation["provider"] = requested_provider
            for field, requested_field in (("auto_refresh", "auto_refresh_enabled"), ("allow_partial", "allow_partial_cache")):
                if not normalized[requested_field]:
                    continue
                explicitly_confirmed = bool((requested_activation or {}).get(field))
                preserved = bool(old_settings.get(requested_field)) and bool(old_activation.get(field))
                activation[field] = explicitly_confirmed or preserved
            document["semantic_index"] = normalized
            document["semantic_index_activation"] = activation
            document["settings_revision"] = revision + 1
            self._write_document(document)
            self.app.config["SEMANTIC_RECALL_MODE"] = normalized["recall_mode"]
            if not self.app.config.get("SEMANTIC_INDEX_PROVIDER_MANAGED"):
                self.app.config["SEMANTIC_INDEX_PROVIDER_ENABLED"] = activation[
                    "provider"
                ]
                self.app.config["SEMANTIC_INDEX_PROVIDER_SOURCE"] = "local"
            return {
                "settings_revision": revision + 1,
                "settings": normalized,
                "activation": activation,
            }

    def _keyring_get(self, profile_id: str) -> str:
        if self.keyring is None:
            return ""
        try:
            return str(self.keyring.get_password(KEYRING_SERVICE, profile_id) or "")
        except Exception as exc:
            self.app.logger.exception("Credential backend read failed")
            raise SettingsError(
                "KEYRING_UNAVAILABLE",
                "Windows 凭据存储不可用，请检查 keyring 安装",
                status_code=503,
            ) from exc

    @staticmethod
    def _credential_error_message(exc: Exception, *, action: str) -> str:
        """Return a safe, actionable Credential Manager failure message."""

        if getattr(exc, "winerror", None) == 1312:
            return (
                "Windows 凭据存储需要交互式桌面会话，"
                "请从桌面双击启动错题本后重试"
            )
        return f"无法{action} Windows 凭据存储"

    def _keyring_set(self, profile_id: str, api_key: str) -> None:
        if self.keyring is None:
            raise SettingsError(
                "KEYRING_UNAVAILABLE",
                "Windows 凭据存储不可用，请安装项目依赖",
                status_code=503,
            )
        try:
            self.keyring.set_password(KEYRING_SERVICE, profile_id, api_key)
        except Exception as exc:
            raise SettingsError(
                "KEYRING_UNAVAILABLE",
                self._credential_error_message(exc, action="写入"),
                status_code=503,
            ) from exc

    def _keyring_delete(self, profile_id: str) -> None:
        if self.keyring is None:
            return
        try:
            self.keyring.delete_password(KEYRING_SERVICE, profile_id)
        except Exception as exc:
            if type(exc).__name__ not in {"PasswordDeleteError", "KeyError"}:
                raise SettingsError(
                    "KEYRING_UNAVAILABLE",
                    self._credential_error_message(exc, action="更新"),
                    status_code=503,
                ) from exc

    def _restore_keyring(self, credential_ref: str, previous_value: str) -> None:
        if previous_value:
            self._keyring_set(credential_ref, previous_value)
        else:
            self._keyring_delete(credential_ref)

    def _write_with_credential_rollback(
        self,
        document: dict,
        *,
        credential_ref: str,
        previous_value: str,
    ) -> None:
        try:
            self._write_document(document)
        except SettingsError as save_error:
            try:
                self._restore_keyring(credential_ref, previous_value)
            except SettingsError as rollback_error:
                raise SettingsError(
                    "SETTINGS_ROLLBACK_FAILED",
                    "本地设置保存失败，且 Windows 凭据回滚失败，请重新检查 AI 设置",
                    status_code=500,
                ) from rollback_error
            raise save_error

    @staticmethod
    def _profile_preset(profile_id: str) -> dict:
        definition = PROFILE_DEFINITIONS.get(profile_id)
        preset = definition["preset"] if definition else PROFILE_DEFINITIONS[CUSTOM_PROFILE_ID]["preset"]
        return dict(preset)

    @staticmethod
    def _capabilities(profile_id: str, value: object = None) -> dict:
        capabilities = dict(
            PROFILE_CAPABILITY_DEFAULTS.get(
                profile_id, PROFILE_CAPABILITY_DEFAULTS[CUSTOM_PROFILE_ID]
            )
        )
        if value is None:
            return capabilities
        if not isinstance(value, dict):
            raise SettingsError("VALIDATION", "模型能力必须是对象")
        unexpected = set(value) - CAPABILITY_FIELDS - CAPABILITY_LIMIT_FIELDS
        if unexpected:
            raise SettingsError(
                "VALIDATION", f"不支持的模型能力：{sorted(unexpected)[0]}"
            )
        for field in CAPABILITY_FIELDS:
            if field in value:
                if not isinstance(value[field], bool):
                    raise SettingsError("VALIDATION", f"{field} 必须是布尔值")
                capabilities[field] = value[field]
        for field in CAPABILITY_LIMIT_FIELDS:
            if field not in value:
                continue
            raw = value[field]
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                raise SettingsError("VALIDATION", f"{field} 必须是非负整数")
            capabilities[field] = raw
        return capabilities

    @staticmethod
    def _profile_id(value: object) -> str:
        profile_id = str(value or DEFAULT_PROFILE_ID).strip().casefold()
        if profile_id not in PROFILE_DEFINITIONS:
            raise SettingsError("VALIDATION", "AI 连接类型无效")
        return profile_id

    @staticmethod
    def _active_profile_id(document: dict) -> str:
        ai_settings = document.get("ai")
        if isinstance(ai_settings, dict):
            candidate = ai_settings.get("active_profile")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip().casefold()
        return DEFAULT_PROFILE_ID

    def managed_fields(self, profile_id: str | None = None) -> list[str]:
        resolved_id = profile_id or self._active_profile_id(self._read_document())
        if resolved_id != DEFAULT_PROFILE_ID:
            return []
        return sorted(
            field for field, env_name in ENVIRONMENT_FIELDS.items() if env_name in os.environ
        )

    def _resolved_profile(self, document: dict, profile_id: str) -> dict:
        profile = self._profile_preset(profile_id)
        if (
            profile_id == DEFAULT_PROFILE_ID
            and self.app.config.get("AI_ACTIVE_PROFILE", DEFAULT_PROFILE_ID)
            == DEFAULT_PROFILE_ID
        ):
            profile.update(
                {
                    "provider": self.app.config.get("AI_PROVIDER", profile["provider"]),
                    "base_url": self.app.config.get("DEEPSEEK_URL", profile["base_url"]),
                    "quality_model": self.app.config.get(
                        "DEEPSEEK_MODEL", profile["quality_model"]
                    ),
                    "fast_model": self.app.config.get(
                        "DEEPSEEK_FLASH_MODEL", profile["fast_model"]
                    ),
                }
            )
        profile.update(
            {
                key: value
                for key, value in _profile_from_document(document, profile_id).items()
                if value is not None
            }
        )
        profile["wire_api"] = validate_wire_api(
            self._profile_preset(profile_id).get("wire_api")
        )
        profile["capabilities"] = self._capabilities(
            profile_id, profile.get("capabilities")
        )
        if profile_id == DEFAULT_PROFILE_ID:
            for field, env_name in ENVIRONMENT_FIELDS.items():
                if field != "api_key" and env_name in os.environ:
                    profile[field] = os.environ[env_name]
        return profile

    def _stored_profile(self, document: dict | None = None) -> tuple[str, dict]:
        document = document if document is not None else self._read_document()
        profile_id = self._active_profile_id(document)
        return profile_id, self._resolved_profile(document, profile_id)

    def _editable_profile(self, document: dict, profile_id: str) -> dict:
        """Return persisted values without copying environment overrides."""
        profile = self._profile_preset(profile_id)
        legacy_fields = {
            "base_url": "deepseek_url",
            "quality_model": "deepseek_model",
            "fast_model": "deepseek_flash_model",
        }
        if profile_id == DEFAULT_PROFILE_ID:
            for field, legacy_name in legacy_fields.items():
                if legacy_name in document:
                    profile[field] = document[legacy_name]
        profile.update(_profile_from_document(document, profile_id))
        return profile

    def _profile_credential(
        self, document: dict, profile_id: str, profile: dict
    ) -> tuple[str, str]:
        if profile_id == DEFAULT_PROFILE_ID and "DEEPSEEK_API_KEY" in os.environ:
            return os.environ.get("DEEPSEEK_API_KEY", "").strip(), "environment"
        credential_ref = str(profile.get("credential_ref") or profile_id)
        legacy = (
            str(document.get("deepseek_api_key") or "").strip()
            if profile_id == DEFAULT_PROFILE_ID
            else ""
        )
        try:
            keyring_value = self._keyring_get(credential_ref).strip()
        except SettingsError:
            if legacy:
                return legacy, "legacy_file"
            raise
        if keyring_value:
            return keyring_value, "windows_keyring"
        if legacy:
            return legacy, "legacy_file"
        return "", "none"

    def credential(self, profile_id: str | None = None) -> tuple[str, str]:
        document = self._read_document()
        active_id, active_profile = self._stored_profile(document)
        active_ref = str(active_profile.get("credential_ref") or active_id)
        if profile_id is None or profile_id in {active_id, active_ref}:
            return self._profile_credential(document, active_id, active_profile)
        resolved_id = self._profile_id(profile_id)
        return self._profile_credential(
            document, resolved_id, self._resolved_profile(document, resolved_id)
        )

    def _public_profile(self, document: dict, profile_id: str) -> dict:
        profile = self._resolved_profile(document, profile_id)
        credential_ref = str(profile.get("credential_ref") or profile_id)
        key, source = self._profile_credential(document, profile_id, profile)
        try:
            public_url = validate_provider_url(profile.get("base_url"))
        except ValueError:
            # Never reflect malformed legacy URLs that could contain credentials.
            public_url = ""
        definition = PROFILE_DEFINITIONS.get(
            profile_id, PROFILE_DEFINITIONS[CUSTOM_PROFILE_ID]
        )
        return {
            "id": profile_id,
            "name": definition["name"],
            "connection_type": definition["connection_type"],
            "provider": profile.get("provider", "openai_compatible"),
            "wire_api": validate_wire_api(profile.get("wire_api")),
            "credential_ref": credential_ref,
            "base_url": public_url,
            "quality_model": str(profile.get("quality_model") or ""),
            "fast_model": str(profile.get("fast_model") or ""),
            "reasoning_effort": str(profile.get("reasoning_effort") or ""),
            "capabilities": dict(profile.get("capabilities") or {}),
            "dns_mode": validate_dns_mode(profile.get("dns_mode")),
            "dns_servers": list(validate_dns_servers(profile.get("dns_servers"))),
            "has_api_key": bool(key),
            "credential_source": source,
            "managed_fields": self.managed_fields(profile_id),
        }

    def public_ai_settings(self) -> dict:
        with self._lock:
            document = self._read_document()
            profile_id = self._active_profile_id(document)
            active = self._public_profile(document, profile_id)
            profiles = {
                candidate_id: self._public_profile(document, candidate_id)
                for candidate_id in PROFILE_DEFINITIONS
            }
            ai_settings = document.get("ai") if isinstance(document.get("ai"), dict) else {}
            stored_assignments = (
                ai_settings.get("assignments")
                if isinstance(ai_settings.get("assignments"), dict)
                else {}
            )
            assignments = dict(DEFAULT_ASSIGNMENTS)
            assignments.update(
                {
                    key: value
                    for key, value in stored_assignments.items()
                    if key in DEFAULT_ASSIGNMENTS and value in {"fast", "quality"}
                }
            )
            stored_policy_assignments = (
                ai_settings.get("policy_assignments")
                if isinstance(ai_settings.get("policy_assignments"), dict)
                else {}
            )
            policy_assignments = {
                key: value
                for key, value in stored_policy_assignments.items()
                if key in TASK_CONTRACTS and value in {"fast", "quality"}
            }
            return {
                "active_profile": profile_id,
                **{key: value for key, value in active.items() if key != "id"},
                "profiles": profiles,
                "assignments": assignments,
                "policy_assignments": policy_assignments,
                "version": int(document.get("settings_revision") or 0),
                "legacy_detected": bool(str(document.get("deepseek_api_key") or "").strip()),
            }

    @staticmethod
    def _text(value: Any, label: str, *, maximum: int) -> str:
        if not isinstance(value, str):
            raise SettingsError("VALIDATION", f"{label}必须是文本")
        value = value.strip()
        if not value or len(value) > maximum:
            raise SettingsError("VALIDATION", f"{label}长度无效")
        if "\r" in value or "\n" in value:
            raise SettingsError("VALIDATION", f"{label}不能包含换行")
        return value

    def update_ai_settings(self, payload: dict) -> dict:
        allowed = {
            "active_profile",
            "provider",
            "base_url",
            "quality_model",
            "fast_model",
            "reasoning_effort",
            "capabilities",
            "dns_mode",
            "dns_servers",
            "api_key_action",
            "api_key",
            "assignments",
            "policy_assignments",
            "version",
        }
        unexpected = set(payload) - allowed
        if unexpected:
            raise SettingsError("VALIDATION", f"包含不支持的字段：{sorted(unexpected)[0]}")
        requested_provider = payload.get("provider")
        action = payload.get("api_key_action", "keep")
        if action not in {"keep", "replace", "clear"}:
            raise SettingsError("VALIDATION", "api_key_action 无效")
        if action != "replace" and "api_key" in payload:
            raise SettingsError("VALIDATION", "仅 replace 操作可提交 api_key")

        with self._lock:
            document = self._read_document()
            revision = int(document.get("settings_revision") or 0)
            self._expected_revision(payload, revision)
            current_profile_id = self._active_profile_id(document)
            profile_id = self._profile_id(
                payload.get("active_profile", current_profile_id)
            )
            expected_provider = str(self._profile_preset(profile_id)["provider"])
            provider = str(requested_provider or expected_provider).strip().casefold()
            if provider != expected_provider:
                raise SettingsError(
                    "VALIDATION", "AI 连接类型与所选 Provider 不匹配"
                )
            managed = set(self.managed_fields(profile_id))
            changed_fields = {
                field for field in ENVIRONMENT_FIELDS if field in payload
            }
            if action != "keep":
                changed_fields.add("api_key")
            blocked = sorted(changed_fields & managed)
            if blocked:
                raise SettingsError(
                    "MANAGED_BY_ENVIRONMENT",
                    f"字段 {blocked[0]} 由环境变量管理，不能在界面修改",
                    status_code=409,
                )
            current = self._editable_profile(document, profile_id)
            updated = {
                "provider": provider,
                "wire_api": validate_wire_api(current.get("wire_api")),
                "credential_ref": str(current.get("credential_ref") or profile_id),
                "base_url": current["base_url"],
                "quality_model": current["quality_model"],
                "fast_model": current["fast_model"],
                "reasoning_effort": str(current.get("reasoning_effort") or ""),
                "capabilities": self._capabilities(
                    profile_id, current.get("capabilities")
                ),
                "dns_mode": current.get("dns_mode", "domestic"),
                "dns_servers": list(
                    validate_dns_servers(current.get("dns_servers"))
                ),
            }
            if "base_url" in payload:
                try:
                    updated["base_url"] = validate_provider_url(payload["base_url"])
                except ValueError as exc:
                    raise SettingsError("VALIDATION", str(exc)) from exc
            for field, label in (
                ("quality_model", "高质量模型"),
                ("fast_model", "快速模型"),
            ):
                if field in payload:
                    updated[field] = self._text(payload[field], label, maximum=200)
            if "reasoning_effort" in payload:
                effort = str(payload["reasoning_effort"] or "").strip().casefold()
                if effort not in {"", "low", "medium", "high", "max"}:
                    raise SettingsError("VALIDATION", "推理强度无效")
                updated["reasoning_effort"] = effort
            if "capabilities" in payload:
                updated["capabilities"] = self._capabilities(
                    profile_id, payload["capabilities"]
                )
            if "dns_mode" in payload:
                try:
                    updated["dns_mode"] = validate_dns_mode(payload["dns_mode"])
                except ValueError as exc:
                    raise SettingsError("VALIDATION", str(exc)) from exc
            if "dns_servers" in payload:
                try:
                    updated["dns_servers"] = list(
                        validate_dns_servers(payload["dns_servers"])
                    )
                except ValueError as exc:
                    raise SettingsError("VALIDATION", str(exc)) from exc
            if not str(updated["base_url"] or "").strip():
                raise SettingsError("VALIDATION", "API 地址不能为空")
            try:
                updated["base_url"] = validate_provider_url(updated["base_url"])
            except ValueError as exc:
                raise SettingsError("VALIDATION", str(exc)) from exc
            for field, label in (
                ("quality_model", "高质量模型"),
                ("fast_model", "快速模型"),
            ):
                updated[field] = self._text(updated[field], label, maximum=200)

            ai_settings = document.get("ai")
            if not isinstance(ai_settings, dict):
                ai_settings = {}
                document["ai"] = ai_settings
            profiles = ai_settings.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                ai_settings["profiles"] = profiles
            ai_settings["active_profile"] = profile_id
            profiles[profile_id] = updated
            current_assignments = dict(DEFAULT_ASSIGNMENTS)
            if isinstance(ai_settings.get("assignments"), dict):
                current_assignments.update(ai_settings["assignments"])
            if "assignments" in payload:
                incoming = payload["assignments"]
                if not isinstance(incoming, dict):
                    raise SettingsError("VALIDATION", "模型职责分配必须是对象")
                unexpected_assignments = set(incoming) - set(DEFAULT_ASSIGNMENTS)
                if unexpected_assignments:
                    raise SettingsError(
                        "VALIDATION",
                        f"不支持的模型职责：{sorted(unexpected_assignments)[0]}",
                    )
                for task, role in incoming.items():
                    if role not in {"fast", "quality"}:
                        raise SettingsError("VALIDATION", f"{task} 的模型角色无效")
                    current_assignments[task] = role
            ai_settings["assignments"] = current_assignments
            current_policy_assignments = {}
            if isinstance(ai_settings.get("policy_assignments"), dict):
                current_policy_assignments.update(ai_settings["policy_assignments"])
            if "policy_assignments" in payload:
                incoming = payload["policy_assignments"]
                if not isinstance(incoming, dict):
                    raise SettingsError("VALIDATION", "任务策略模型分配必须是对象")
                unexpected_policies = set(incoming) - set(TASK_CONTRACTS)
                if unexpected_policies:
                    raise SettingsError(
                        "VALIDATION",
                        f"不支持的任务策略：{sorted(unexpected_policies)[0]}",
                    )
                for policy_name, policy_role in incoming.items():
                    if policy_role not in {"fast", "quality"}:
                        raise SettingsError(
                            "VALIDATION", f"{policy_name} 的模型角色无效"
                        )
                    current_policy_assignments[policy_name] = policy_role
            ai_settings["policy_assignments"] = current_policy_assignments
            if action in {"replace", "clear"}:
                document.pop("deepseek_api_key", None)
            document["settings_revision"] = revision + 1
            if action == "keep":
                self._write_document(document)
            else:
                credential_ref = updated["credential_ref"]
                previous_value = self._keyring_get(credential_ref)
                if action == "replace":
                    api_key = self._text(
                        payload.get("api_key"), "API 密钥", maximum=4096
                    )
                    self._keyring_set(credential_ref, api_key)
                else:
                    self._keyring_delete(credential_ref)
                self._write_with_credential_rollback(
                    document,
                    credential_ref=credential_ref,
                    previous_value=previous_value,
                )
            self._apply_runtime(profile_id, updated)
            return self.public_ai_settings()

    def _apply_runtime(self, profile_id: str, profile: dict) -> None:
        self.app.config["AI_ACTIVE_PROFILE"] = profile_id
        self.app.config["AI_PROVIDER"] = profile["provider"]
        use_environment = profile_id == DEFAULT_PROFILE_ID
        if not use_environment or "DEEPSEEK_URL" not in os.environ:
            self.app.config["DEEPSEEK_URL"] = profile["base_url"]
        if not use_environment or "DEEPSEEK_MODEL" not in os.environ:
            self.app.config["DEEPSEEK_MODEL"] = profile["quality_model"]
        if not use_environment or "DEEPSEEK_FLASH_MODEL" not in os.environ:
            self.app.config["DEEPSEEK_FLASH_MODEL"] = profile["fast_model"]
        if not use_environment or "DEEPSEEK_API_KEY" not in os.environ:
            self.app.config["DEEPSEEK_API_KEY"] = ""

    @staticmethod
    def _expected_revision(payload: dict, revision: int) -> None:
        expected = payload.get("version")
        if expected is not None and expected != revision:
            raise SettingsError(
                "SETTINGS_CONFLICT",
                "设置已被其他操作更新，请处理冲突",
                status_code=409,
            )

    def reset_ai_settings(self, payload: dict) -> dict:
        if set(payload) - {"version"}:
            raise SettingsError("VALIDATION", "恢复默认设置请求包含不支持的字段")
        with self._lock:
            document = self._read_document()
            revision = int(document.get("settings_revision") or 0)
            self._expected_revision(payload, revision)
            profile_id, current = self._stored_profile(document)
            reset_profile = {
                **self._profile_preset(profile_id),
                "credential_ref": str(current.get("credential_ref") or profile_id),
            }
            ai_settings = document.get("ai")
            if not isinstance(ai_settings, dict):
                ai_settings = {}
                document["ai"] = ai_settings
            ai_settings["active_profile"] = profile_id
            profiles = ai_settings.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                ai_settings["profiles"] = profiles
            profiles[profile_id] = reset_profile
            ai_settings["assignments"] = dict(DEFAULT_ASSIGNMENTS)
            ai_settings["policy_assignments"] = {}
            document["settings_revision"] = revision + 1
            self._write_document(document)
            self._apply_runtime(profile_id, reset_profile)
            return self.public_ai_settings()

    def clear_ai_credential(self, payload: dict) -> dict:
        if set(payload) - {"version", "confirm"}:
            raise SettingsError("VALIDATION", "删除凭据请求包含不支持的字段")
        if payload.get("confirm") is not True:
            raise SettingsError("CONFIRMATION_REQUIRED", "删除凭据前必须明确确认")
        if "api_key" in self.managed_fields():
            raise SettingsError(
                "MANAGED_BY_ENVIRONMENT",
                "API 密钥由环境变量管理，不能在界面删除",
                status_code=409,
            )
        with self._lock:
            document = self._read_document()
            revision = int(document.get("settings_revision") or 0)
            self._expected_revision(payload, revision)
            profile_id, profile = self._stored_profile(document)
            credential_ref = str(profile.get("credential_ref") or profile_id)
            previous_value = self._keyring_get(credential_ref)
            self._keyring_delete(credential_ref)
            document.pop("deepseek_api_key", None)
            document["settings_revision"] = revision + 1
            self._write_with_credential_rollback(
                document,
                credential_ref=credential_ref,
                previous_value=previous_value,
            )
            if profile_id != DEFAULT_PROFILE_ID or "DEEPSEEK_API_KEY" not in os.environ:
                self.app.config["DEEPSEEK_API_KEY"] = ""
            return self.public_ai_settings()

    def migrate_legacy_key(self) -> dict:
        with self._lock:
            document = self._read_document()
            legacy = str(document.get("deepseek_api_key") or "").strip()
            if not legacy:
                return {"migrated": False, **self.public_ai_settings()}
            profile_id = DEFAULT_PROFILE_ID
            profile = self._resolved_profile(document, profile_id)
            credential_ref = str(profile.get("credential_ref") or profile_id)
            previous_value = self._keyring_get(credential_ref)
            self._keyring_set(credential_ref, legacy)
            document.pop("deepseek_api_key", None)
            document["settings_revision"] = int(document.get("settings_revision") or 0) + 1
            self._write_with_credential_rollback(
                document,
                credential_ref=credential_ref,
                previous_value=previous_value,
            )
            if "DEEPSEEK_API_KEY" not in os.environ:
                self.app.config["DEEPSEEK_API_KEY"] = ""
            return {"migrated": True, **self.public_ai_settings()}

    def provider_runtime(self, *, model_role: str = "quality"):
        with self._lock:
            profile_id, profile = self._stored_profile()
            api_key, _ = self.credential(profile_id)
            model_field = "fast_model" if model_role == "fast" else "quality_model"
            provider = create_provider(profile["provider"], profile, api_key)
            return provider, profile[model_field]

    def model_role_for_task(
        self, task: str | None, *, task_policy: str | None = None
    ) -> str:
        document = self._read_document()
        ai_settings = document.get("ai") if isinstance(document.get("ai"), dict) else {}
        policy_assignments = (
            ai_settings.get("policy_assignments")
            if isinstance(ai_settings.get("policy_assignments"), dict)
            else {}
        )
        if task_policy:
            policy_role = policy_assignments.get(task_policy)
            if policy_role in {"fast", "quality"}:
                return policy_role
        assignments = ai_settings.get("assignments") if isinstance(ai_settings.get("assignments"), dict) else {}
        role = assignments.get(task) if task else None
        if role not in {"fast", "quality"} and task:
            role = DEFAULT_ASSIGNMENTS.get(task)
        if role not in {"fast", "quality"} and task_policy:
            role = DEFAULT_POLICY_ROLES.get(task_policy)
        return role if role in {"fast", "quality"} else "quality"

    def model_for_task(self, task: str | None) -> str:
        role = self.model_role_for_task(task)
        _profile_id, profile = self._stored_profile()
        return str(profile["fast_model" if role == "fast" else "quality_model"])

    def available_models(self) -> dict:
        """Return chat-capable models visible to the saved profile credential."""

        with self._lock:
            document = self._read_document()
            profile_id, profile = self._stored_profile(document)
            api_key, credential_source = self._profile_credential(
                document, profile_id, profile
            )
            if not api_key:
                raise SettingsError(
                    "MISSING_CREDENTIAL",
                    "请先保存当前 AI 连接的 API 密钥",
                )
            provider = create_provider(profile["provider"], profile, api_key)
        models = _chat_model_items(provider.list_models(timeout=30))
        if not models:
            raise AIProviderError(
                "INVALID_RESPONSE",
                "当前账号没有返回可用的文本模型",
                error_class="empty_model_list",
            )

        by_tier = {
            tier: [item["id"] for item in models if item["speed_tier"] == tier]
            for tier in ("fast", "balanced", "quality")
        }

        def recommended(tier: str, fallbacks: tuple[str, ...]) -> str:
            candidates = by_tier[tier]
            configured = str(
                profile.get("fast_model" if tier == "fast" else "quality_model")
                or ""
            )
            if configured in candidates:
                return configured
            if candidates:
                return candidates[0]
            for fallback in fallbacks:
                if by_tier[fallback]:
                    return by_tier[fallback][0]
            return models[0]["id"]

        recommendations = {
            "fast": recommended("fast", ("balanced", "quality")),
            "balanced": recommended("balanced", ("fast", "quality")),
            "quality": recommended("quality", ("balanced", "fast")),
        }
        current_fast = str(profile.get("fast_model") or "")
        current_quality = str(profile.get("quality_model") or "")
        decorated = [
            {
                **item,
                "selected_for": [
                    role
                    for role, selected in (
                        ("fast", current_fast),
                        ("quality", current_quality),
                    )
                    if item["id"] == selected
                ],
            }
            for item in models
        ]
        return {
            "profile_id": profile_id,
            "profile_name": PROFILE_DEFINITIONS[profile_id]["name"],
            "provider": profile["provider"],
            "credential_source": credential_source,
            "models": decorated,
            "recommendations": recommendations,
        }

    def test_ai(self) -> dict:
        correlation = self._telemetry_correlation(
            "settings:connection-test", "settings_connection_test"
        )
        try:
            provider, model = self.provider_runtime(model_role="fast")
            response = provider.complete(
                [{"role": "user", "content": "Reply with OK."}],
                model=model,
                temperature=0,
                timeout=60,
                max_attempts=1,
                max_tokens=16,
            )
            if response.content.strip().casefold().rstrip(".") != "ok":
                raise AIProviderError(
                    "INVALID_RESPONSE",
                    "AI 连接测试未返回预期确认内容",
                    retryable=True,
                    attempts=response.attempts,
                    duration_ms=response.duration_ms,
                    error_class="connection_probe_content",
                )
        except SettingsError as exc:
            record_usage_best_effort(self.app, {
                    "task":"settings_test",
                    "task_policy":"connection_test",
                    "success":False,
                    "event_scope":"provider",
                    "error_code":exc.code,
                    "error_class":"configuration",
                    "attempts":0,
                    "max_tokens":16,
                    **correlation,
                })
            raise
        except AIProviderError as exc:
            record_usage_best_effort(self.app, {
                    "task":"settings_test",
                    "task_policy":"connection_test",
                    "model":locals().get("model"),
                    "success":False,
                    "event_scope":"provider",
                    "error_code":exc.code,
                    "error_class":exc.error_class,
                    "attempts":exc.attempts,
                    "duration_ms":exc.duration_ms,
                    "max_tokens":16,
                    **correlation,
                })
            raise
        usage=response.usage or {}
        details=(
            usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"),dict)
            else {}
        )
        record_usage_best_effort(self.app, {
                "task":"settings_test",
                "task_policy":"connection_test",
                "model":response.model,
                "success":True,
                "event_scope":"provider",
                "finish_reason":response.finish_reason,
                "attempts":response.attempts,
                "duration_ms":response.duration_ms,
                "max_tokens":16,
                "prompt_tokens":usage.get("prompt_tokens"),
                "completion_tokens":usage.get("completion_tokens"),
                "reasoning_tokens":usage.get(
                    "reasoning_tokens",details.get("reasoning_tokens")
                ),
                "cached_tokens":usage.get("cached_tokens"),
                "total_tokens":usage.get("total_tokens"),
                **correlation,
            })
        _profile_id, profile = self._stored_profile()
        return {
            "ok": True,
            "provider": profile["provider"],
            "model": response.model,
            "dns_mode": validate_dns_mode(profile.get("dns_mode")),
        }

    def test_ai_contract(self) -> dict:
        """Manually probe the optional fields used by real business calls."""

        basic = self.test_ai()
        with self._lock:
            document = self._read_document()
            profile_id, profile = self._stored_profile(document)
            api_key, _source = self._profile_credential(
                document, profile_id, profile
            )
            provider = create_provider(profile["provider"], profile, api_key)
            models = list(
                dict.fromkeys(
                    str(profile.get(field) or "").strip()
                    for field in ("fast_model", "quality_model")
                    if str(profile.get(field) or "").strip()
                )
            )
        probes = {
            "json_object": {
                "messages": [
                    {
                        "role": "user",
                        "content": 'Return exactly one JSON object: {"ok":true}',
                    }
                ],
                "response_format": {"type": "json_object"},
            },
            "thinking": {
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "thinking": {"type": "enabled"},
            },
            "reasoning_effort": {
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "reasoning_effort": "low",
            },
        }
        model_results = {}
        for model in models:
            feature_results = {}
            for feature, options in probes.items():
                correlation = self._telemetry_correlation(
                    f"settings:capability-probe:{model}:{feature}",
                    "settings_capability_probe",
                )
                try:
                    response = provider.complete(
                        options["messages"],
                        model=model,
                        temperature=0,
                        timeout=60,
                        max_attempts=1,
                        max_tokens=32,
                        thinking=options.get("thinking"),
                        reasoning_effort=options.get("reasoning_effort"),
                        response_format=options.get("response_format"),
                    )
                    if feature == "json_object":
                        parsed = json.loads(response.content)
                        supported = isinstance(parsed, dict) and parsed.get("ok") is True
                    else:
                        supported = (
                            response.content.strip().casefold().rstrip(".") == "ok"
                        )
                    feature_results[feature] = bool(supported)
                    record_usage_best_effort(
                        self.app,
                        {
                            "task": "settings_test",
                            "task_policy": "capability_probe",
                            "model": response.model,
                            "success": bool(supported),
                            "event_scope": "provider",
                            "attempts": response.attempts,
                            "duration_ms": response.duration_ms,
                            "max_tokens": 32,
                            "error_class": None if supported else "probe_content",
                            **correlation,
                        },
                    )
                except (AIProviderError, json.JSONDecodeError) as exc:
                    feature_results[feature] = False
                    record_usage_best_effort(
                        self.app,
                        {
                            "task": "settings_test",
                            "task_policy": "capability_probe",
                            "model": model,
                            "success": False,
                            "event_scope": "provider",
                            "attempts": getattr(exc, "attempts", 0),
                            "duration_ms": getattr(exc, "duration_ms", 0),
                            "max_tokens": 32,
                            "error_code": getattr(exc, "code", "INVALID_RESPONSE"),
                            "error_class": "capability_unsupported",
                            **correlation,
                        },
                    )
            model_results[model] = feature_results

        combined = dict(profile.get("capabilities") or {})
        for feature in CAPABILITY_FIELDS:
            combined[feature] = bool(model_results) and all(
                result.get(feature) is True for result in model_results.values()
            )
        with self._lock:
            document = self._read_document()
            active_id = self._active_profile_id(document)
            if active_id != profile_id:
                raise SettingsError(
                    "SETTINGS_CONFLICT",
                    "能力检测期间活动连接已切换，请重新检测",
                    status_code=409,
                )
            ai_settings = document.get("ai")
            if not isinstance(ai_settings, dict):
                ai_settings = {}
                document["ai"] = ai_settings
            profiles = ai_settings.get("profiles")
            if not isinstance(profiles, dict):
                profiles = {}
                ai_settings["profiles"] = profiles
            stored = profiles.get(profile_id)
            stored = dict(stored) if isinstance(stored, dict) else {}
            stored["capabilities"] = self._capabilities(profile_id, combined)
            profiles[profile_id] = stored
            document["settings_revision"] = int(
                document.get("settings_revision") or 0
            ) + 1
            self._write_document(document)
        return {
            **basic,
            "mode": "contract",
            "models": model_results,
            "capabilities": self._capabilities(profile_id, combined),
            "version": int(document.get("settings_revision") or 0),
        }


def get_settings_service(app, *, keyring_backend=None) -> SettingsService:
    extension_key = "ai_settings_service"
    if keyring_backend is not None:
        return SettingsService(app, keyring_backend=keyring_backend)
    service = app.extensions.get(extension_key)
    if service is None:
        service = SettingsService(app)
        app.extensions[extension_key] = service
    return service
