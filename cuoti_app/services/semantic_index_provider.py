"""Default-off production adapter for permanent semantic material cards.

The adapter deliberately bypasses the legacy AI facade: one durable Provider
attempt must cause at most one HTTP request.  Network outcomes that cannot be
distinguished from a successfully executed request are surfaced as ambiguous
and are never automatically replayed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .ai_providers import AIProviderError, ChatResponse, create_provider
from .ai_quality_metrics import (
    QualityMetricsError,
    load_or_create_installation_secret,
    prompt_hmac,
    task_hmac,
)
from .ai_telemetry import record_usage_best_effort
from .semantic_index_dispatch import (
    SemanticProviderTokenCost,
)
from .semantic_index_loop import SemanticDispatchRuntime
from .semantic_index_material import (
    MAX_MISSING_FILL_ATTEMPTS,
    AmbiguousSemanticProviderError,
    PermanentSemanticProviderError,
    SemanticCardProviderRequest,
    SemanticCardProviderResponse,
    TemporarySemanticProviderError,
)
from .semantic_index_refresh import SemanticAnalysisProfile
from .settings import SettingsError, get_settings_service


SEMANTIC_PROVIDER_COST_POLICY_VERSION = 1
SEMANTIC_PROVIDER_TEMPERATURE = 0.1
SEMANTIC_PROVIDER_MAX_ATTEMPTS = 1
SEMANTIC_PROVIDER_PROMPT_ENVELOPE_BYTES = 256


class SemanticProviderAdapterError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticProviderPolicy:
    timeout_seconds: int
    max_completion_tokens: int
    safety_margin: int
    max_response_bytes: int


def _canonical_messages(request: SemanticCardProviderRequest) -> bytes:
    return json.dumps(
        {"messages": [dict(message) for message in request.messages]},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def estimate_semantic_provider_cost(
    request: SemanticCardProviderRequest,
    *,
    policy: SemanticProviderPolicy,
) -> SemanticProviderTokenCost:
    """Use a deterministic byte upper bound when no model tokenizer is available."""

    prompt_bytes = len(_canonical_messages(request))
    return SemanticProviderTokenCost(
        estimated_prompt_tokens=max(
            1, prompt_bytes + SEMANTIC_PROVIDER_PROMPT_ENVELOPE_BYTES
        ),
        max_completion_tokens=policy.max_completion_tokens,
        safety_margin=policy.safety_margin,
    )


def _usage_total(response: ChatResponse) -> int | None:
    usage = response.usage
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        return None
    return total


def _safe_record(app: Any, event: dict[str, Any]) -> None:
    record_usage_best_effort(app, event)


@dataclass(slots=True)
class SemanticCardProviderAdapter:
    app: Any
    provider: Any
    profile: SemanticAnalysisProfile
    settings_version: int
    policy: SemanticProviderPolicy
    telemetry_secret: bytes | str | None = None

    def _telemetry_correlation(
        self, request: SemanticCardProviderRequest
    ) -> dict[str, str]:
        if self.telemetry_secret is None:
            return {}
        try:
            return {
                "task_hash": task_hmac(
                    f"semantic-index:{request.candidate_id}",
                    self.telemetry_secret,
                ),
                "stage": "semantic_permanent_card",
                "prompt_hash": prompt_hmac(
                    request.messages,
                    self.telemetry_secret,
                ),
            }
        except QualityMetricsError:
            return {}

    def __call__(
        self, request: SemanticCardProviderRequest
    ) -> SemanticCardProviderResponse:
        correlation = self._telemetry_correlation(request)
        try:
            response = self.provider.complete(
                [dict(message) for message in request.messages],
                model=self.profile.model_name,
                temperature=SEMANTIC_PROVIDER_TEMPERATURE,
                timeout=self.policy.timeout_seconds,
                max_attempts=SEMANTIC_PROVIDER_MAX_ATTEMPTS,
                max_tokens=self.policy.max_completion_tokens,
                thinking={"type": "enabled"},
                reasoning_effort=self.profile.reasoning_effort,
                response_format={"type": "json_object"},
                task_policy="semantic_permanent_card",
            )
        except AIProviderError as exc:
            _safe_record(
                self.app,
                {
                    "task": "semantic_index_refresh",
                    "task_policy": "semantic_permanent_card",
                    "model": self.profile.model_name,
                    "model_role": self.profile.model_role,
                    "settings_version": self.settings_version,
                    "reasoning_effort": self.profile.reasoning_effort,
                    "max_tokens": self.policy.max_completion_tokens,
                    "attempts": exc.attempts,
                    "duration_ms": exc.duration_ms,
                    "success": False,
                    "event_scope": "provider",
                    "error_code": exc.code,
                    "error_class": exc.error_class,
                    **correlation,
                },
            )
            if exc.code in {"NETWORK", "CANCELLED"}:
                raise AmbiguousSemanticProviderError(
                    "Provider 请求结果无法确认，已停止自动重调"
                ) from exc
            if exc.code in {"CONFIGURATION", "AUTHENTICATION"}:
                raise PermanentSemanticProviderError(
                    "Provider 配置或认证不可用"
                ) from exc
            if exc.retryable:
                raise TemporarySemanticProviderError(
                    "Provider 返回可重试错误"
                ) from exc
            raise PermanentSemanticProviderError(
                "Provider 返回不可恢复错误"
            ) from exc
        except (TimeoutError, ConnectionError, OSError) as exc:
            raise AmbiguousSemanticProviderError(
                "Provider 请求结果无法确认，已停止自动重调"
            ) from exc

        usage_total = _usage_total(response)
        usage = response.usage if isinstance(response.usage, dict) else {}
        details = (
            usage.get("completion_tokens_details")
            if isinstance(usage.get("completion_tokens_details"), dict)
            else {}
        )
        _safe_record(
            self.app,
            {
                "task": "semantic_index_refresh",
                "task_policy": "semantic_permanent_card",
                "model": response.model,
                "model_role": self.profile.model_role,
                "settings_version": self.settings_version,
                "reasoning_effort": self.profile.reasoning_effort,
                "max_tokens": self.policy.max_completion_tokens,
                "finish_reason": response.finish_reason,
                "duration_ms": response.duration_ms,
                "attempts": response.attempts,
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "reasoning_tokens": usage.get(
                    "reasoning_tokens", details.get("reasoning_tokens")
                ),
                "cached_tokens": usage.get("cached_tokens"),
                "total_tokens": usage_total,
                "success": True,
                "event_scope": "provider",
                "provider_calls": 1,
                "usage_observations": 1 if usage else 0,
                "usage_missing_calls": 0 if usage else 1,
                "usage_missing": not bool(usage),
                **correlation,
            },
        )
        return SemanticCardProviderResponse(
            raw_text=response.content,
            request_id=response.request_id,
            usage_total_tokens=usage_total,
        )


def _bounded_config(
    app: Any, name: str, *, minimum: int, maximum: int
) -> int:
    value = app.config.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > maximum
    ):
        raise SemanticProviderAdapterError(
            "SEMANTIC_PROVIDER_CONFIG_INVALID", f"{name} 配置无效"
        )
    return value


def build_semantic_provider_runtime(
    app: Any,
    *,
    profile: SemanticAnalysisProfile,
) -> SemanticDispatchRuntime | None:
    """Build the real adapter only behind the explicit default-off gate."""

    if app.config.get("SEMANTIC_INDEX_PROVIDER_ENABLED") is not True:
        return None
    policy = SemanticProviderPolicy(
        timeout_seconds=_bounded_config(
            app,
            "SEMANTIC_INDEX_PROVIDER_TIMEOUT_SECONDS",
            minimum=30,
            maximum=240,
        ),
        max_completion_tokens=_bounded_config(
            app,
            "SEMANTIC_INDEX_MAX_COMPLETION_TOKENS",
            minimum=256,
            maximum=32_768,
        ),
        safety_margin=_bounded_config(
            app,
            "SEMANTIC_INDEX_TOKEN_SAFETY_MARGIN",
            minimum=0,
            maximum=8_192,
        ),
        max_response_bytes=_bounded_config(
            app,
            "SEMANTIC_INDEX_MAX_RESPONSE_BYTES",
            minimum=65_536,
            maximum=4_194_304,
        ),
    )
    try:
        settings_service = get_settings_service(app)
        settings = settings_service.public_ai_settings()
        if settings.get("provider") != "openai_compatible":
            raise SemanticProviderAdapterError(
                "SEMANTIC_PROVIDER_UNSUPPORTED", "永久材料卡 Provider 类型不受支持"
            )
        model_field = (
            "fast_model" if profile.model_role == "fast" else "quality_model"
        )
        if str(settings.get(model_field) or "").strip() != profile.model_name:
            raise SemanticProviderAdapterError(
                "ANALYSIS_PROFILE_CHANGED", "Provider 模型与冻结 run 身份不一致"
            )
        if str(settings.get("wire_api") or "chat_completions") != profile.wire_api:
            raise SemanticProviderAdapterError(
                "ANALYSIS_PROFILE_CHANGED", "Provider 协议与冻结 run 身份不一致"
            )
        credential_ref = str(settings.get("credential_ref") or "").strip()
        api_key, _source = settings_service.credential(credential_ref)
        provider = create_provider(
            str(settings["provider"]),
            {
                "base_url": settings["base_url"],
                "wire_api": settings.get("wire_api", "chat_completions"),
                "dns_mode": settings["dns_mode"],
                "dns_servers": settings["dns_servers"],
            },
            api_key,
            max_response_bytes=policy.max_response_bytes,
        )
        try:
            telemetry_secret = load_or_create_installation_secret(
                settings_service.keyring
            )
        except (QualityMetricsError, AttributeError):
            telemetry_secret = None
    except SettingsError as exc:
        raise SemanticProviderAdapterError(
            "SEMANTIC_PROVIDER_UNAVAILABLE", "永久材料卡 Provider 配置不可用"
        ) from exc
    except AIProviderError as exc:
        raise SemanticProviderAdapterError(
            "SEMANTIC_PROVIDER_UNAVAILABLE", "永久材料卡 Provider 配置不可用"
        ) from exc
    adapter = SemanticCardProviderAdapter(
        app=app,
        provider=provider,
        profile=profile,
        settings_version=int(settings.get("version") or 0),
        policy=policy,
        telemetry_secret=telemetry_secret,
    )
    return SemanticDispatchRuntime(
        invoke_card=adapter,
        estimate_cost=lambda request: estimate_semantic_provider_cost(
            request, policy=policy
        ),
        extension_review=lambda _run_id, _index, _amount: True,
        # B7b can perform the initial request plus two strict fill attempts
        # under one item lease.  Keep the lease longer than their combined
        # hard timeouts; the outer durable-job heartbeat is maintained
        # independently while Provider calls run outside SQLite transactions.
        lease_seconds=(
            policy.timeout_seconds * (MAX_MISSING_FILL_ATTEMPTS + 1) + 120
        ),
        retry_delay_seconds=60,
    )
