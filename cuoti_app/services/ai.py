"""Backward-compatible AI facade with explicit quality and usage contracts."""

import json
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar

from flask import current_app, has_request_context, request

from .ai_providers import AIProviderError, CallbackCancellationToken, create_provider
from .ai_budgeting import (
    ContextSafetyError,
    ModelCapabilityError,
    bounded_output_tokens,
    ensure_context_safety,
    official_tokenizer_for_model,
    resolve_model_context_capabilities,
)
from .ai_quality_metrics import (
    QualityMetricsError,
    load_or_create_installation_secret,
    prompt_hmac,
    task_hmac,
)
from .ai_task_contracts import (
    AI_TASK_POLICIES,
    TASK_DEFAULT_POLICIES,
    capped_reasoning_effort,
    contract_for,
)
from .ai_telemetry import PhysicalCallAggregate, record_usage_best_effort
from .settings import SettingsError, get_settings_service


class DeepSeekConfigurationError(RuntimeError):
    """Raised when the local DeepSeek configuration is incomplete."""


class DeepSeekServiceError(RuntimeError):
    """Raised when DeepSeek cannot return a usable response."""

    def __init__(
        self,
        message,
        *,
        retryable=True,
        code="AI_PROVIDER_ERROR",
        error_class=None,
        details=None,
    ):
        super().__init__(message)
        self.retryable = bool(retryable)
        self.code = str(code or "AI_PROVIDER_ERROR")
        self.error_class = str(error_class or self.code).strip().casefold()
        self.details = dict(details) if isinstance(details, Mapping) else {}


_FREEZE_LOCK = threading.RLock()
_AI_ATTEMPT_GUARD = ContextVar("cuoti_ai_attempt_guard", default=None)


@contextmanager
def use_ai_attempt_guard(guard):
    """Bind durable-attempt ownership checks to provider calls in this context."""

    if not callable(guard):
        raise TypeError("AI attempt guard must be callable")
    token = _AI_ATTEMPT_GUARD.set(guard)
    try:
        yield
    finally:
        _AI_ATTEMPT_GUARD.reset(token)

_TASK_FREEZE_FIELDS = frozenset({
    "task",
    "policy",
    "model",
    "url",
    "provider",
    "wire_api",
    "version",
    "role",
    "dns_mode",
    "dns_servers",
    "reasoning_effort",
    "profile_id",
    "credential_ref",
    "capabilities",
})
_LEGACY_TASK_FREEZE_FIELDS = _TASK_FREEZE_FIELDS - {"wire_api"}


def _frozen_wire_api(item):
    value = str(item.get("wire_api") or "").strip().casefold()
    if not value:
        if str(item.get("profile_id") or "").strip().casefold() == "soulecho":
            value = "responses"
        elif str(item.get("provider") or "").strip().casefold() == "anthropic":
            value = "messages"
        else:
            value = "chat_completions"
    if value not in {"chat_completions", "responses", "messages"}:
        raise DeepSeekConfigurationError("AI 任务冻结通信协议无效")
    return value


def validate_ai_task_freeze_snapshot(snapshot):
    """Validate a persisted, secret-free set of task/model bindings."""

    if not isinstance(snapshot, Mapping) or len(snapshot) > 32:
        raise DeepSeekConfigurationError("AI 任务冻结快照无效")
    result = {}
    for binding_key, raw in snapshot.items():
        if not isinstance(binding_key, str) or not binding_key.strip():
            raise DeepSeekConfigurationError("AI 任务冻结键无效")
        if not isinstance(raw, Mapping) or set(raw) not in {
            _TASK_FREEZE_FIELDS,
            _LEGACY_TASK_FREEZE_FIELDS,
        }:
            raise DeepSeekConfigurationError("AI 任务冻结字段无效")
        item = dict(raw)
        wire_api = _frozen_wire_api(item)
        if "wire_api" in item:
            item["wire_api"] = wire_api
        task = str(item["task"] or "").strip()
        policy = str(item["policy"] or "").strip()
        if binding_key != f"{task}:{policy}":
            raise DeepSeekConfigurationError("AI 任务冻结身份不一致")
        if not task or not policy:
            raise DeepSeekConfigurationError("AI 任务冻结身份为空")
        if item["role"] not in {"fast", "quality"}:
            raise DeepSeekConfigurationError("AI 任务冻结模型角色无效")
        for field in ("model", "url", "provider", "profile_id", "credential_ref"):
            if not isinstance(item[field], str) or not item[field].strip():
                raise DeepSeekConfigurationError(f"AI 任务冻结 {field} 无效")
        if isinstance(item["version"], bool) or not isinstance(item["version"], int):
            raise DeepSeekConfigurationError("AI 任务冻结版本无效")
        if not isinstance(item["dns_mode"], str):
            raise DeepSeekConfigurationError("AI 任务冻结 DNS 模式无效")
        if not isinstance(item["dns_servers"], list) or not all(
            isinstance(value, str) for value in item["dns_servers"]
        ):
            raise DeepSeekConfigurationError("AI 任务冻结 DNS 列表无效")
        if not isinstance(item["reasoning_effort"], str):
            raise DeepSeekConfigurationError("AI 任务冻结推理强度无效")
        if not isinstance(item["capabilities"], Mapping):
            raise DeepSeekConfigurationError("AI 任务冻结能力快照无效")
        item["capabilities"] = dict(item["capabilities"])
        item["dns_servers"] = list(item["dns_servers"])
        result[binding_key] = item
    return result


def prepare_ai_task_freezes(freeze_id, bindings, *, snapshot=None):
    """Create or restore immutable task bindings for one durable job.

    The returned snapshot deliberately contains only public connection metadata
    and a credential reference.  API keys are resolved from the frozen profile
    at call time and never enter the job manifest.
    """

    job_id = str(freeze_id or "").strip()
    if not job_id:
        return {}
    normalized_bindings = []
    for raw_task, raw_policy in bindings:
        task = str(raw_task or "").strip()
        policy = contract_for(task, raw_policy).name
        normalized_bindings.append((task, policy))
    expected_keys = {f"{task}:{policy}" for task, policy in normalized_bindings}
    freezes = current_app.extensions.setdefault("ai_task_freezes", {})
    if snapshot is None:
        with _FREEZE_LOCK:
            existing_entries = {}
            for task, policy in normalized_bindings:
                provider_binding = freezes.get(f"{job_id}:{task}:{policy}")
                if provider_binding is not None:
                    existing_entries[f"{task}:{policy}"] = {
                        "task": task,
                        "policy": policy,
                        **dict(provider_binding),
                    }
        if existing_entries and set(existing_entries) != expected_keys:
            raise DeepSeekConfigurationError("AI 任务冻结快照不完整")
        if existing_entries:
            snapshot = existing_entries
    if snapshot is not None:
        prepared = validate_ai_task_freeze_snapshot(snapshot)
        if set(prepared) != expected_keys:
            raise DeepSeekConfigurationError("AI 任务冻结范围不一致")
    else:
        service = get_settings_service(current_app._get_current_object())
        settings = service.public_ai_settings()
        prepared = {}
        for task, policy in normalized_bindings:
            role = service.model_role_for_task(task, task_policy=policy)
            selected_model = str(
                settings["fast_model" if role == "fast" else "quality_model"]
            )
            raw_capabilities = dict(settings.get("capabilities") or {})
            try:
                model_capabilities = resolve_model_context_capabilities(
                    selected_model,
                    configured_context_window=raw_capabilities.get("context_window"),
                    configured_max_output_tokens=raw_capabilities.get(
                        "max_output_tokens"
                    ),
                )
            except (ModelCapabilityError, ValueError) as exc:
                raise DeepSeekConfigurationError(str(exc)) from exc
            raw_capabilities.update(
                {
                    "context_window": model_capabilities.context_window_tokens,
                    "max_output_tokens": model_capabilities.max_output_tokens,
                    "context_capability_policy": model_capabilities.policy_version,
                    "context_capability_source": model_capabilities.source,
                }
            )
            prepared[f"{task}:{policy}"] = {
                "task": task,
                "policy": policy,
                "model": selected_model,
                "url": str(settings["base_url"]),
                "provider": str(settings["provider"]),
                "wire_api": str(settings.get("wire_api") or "chat_completions"),
                "version": int(settings["version"]),
                "role": role,
                "dns_mode": str(settings.get("dns_mode") or "domestic"),
                "dns_servers": list(settings.get("dns_servers") or []),
                "reasoning_effort": str(settings.get("reasoning_effort") or ""),
                "profile_id": str(settings.get("active_profile") or ""),
                "credential_ref": str(settings.get("credential_ref") or ""),
                "capabilities": raw_capabilities,
            }
        prepared = validate_ai_task_freeze_snapshot(prepared)
    with _FREEZE_LOCK:
        for item in prepared.values():
            freeze_key = f"{job_id}:{item['task']}:{item['policy']}"
            existing = freezes.get(freeze_key)
            provider_binding = {
                key: value for key, value in item.items() if key not in {"task", "policy"}
            }
            if existing is not None and existing != provider_binding:
                raise DeepSeekConfigurationError("同一任务已有不同的 AI 冻结绑定")
            freezes[freeze_key] = provider_binding
        while len(freezes) > 100:
            freezes.pop(next(iter(freezes)))
    return prepared


def resolve_deepseek_api_key(api_key=None, *, profile_id=None):
    """Resolve the credential exactly as AI calls do, without exposing it."""
    resolved_key = str(api_key or '').strip() if api_key is not None else ''
    if api_key is None and current_app.config.get('AI_API_KEY_CONFIG_OVERRIDE'):
        resolved_key = str(current_app.config.get('DEEPSEEK_API_KEY') or '').strip()
    elif not resolved_key:
        try:
            service = get_settings_service(current_app._get_current_object())
            resolved_key, _source = service.credential(profile_id)
        except SettingsError:
            resolved_key = str(current_app.config.get('DEEPSEEK_API_KEY') or '').strip()
    if not resolved_key:
        # Test overrides and older programmatic integrations may still provide
        # the credential directly in Flask config.
        resolved_key = str(current_app.config.get('DEEPSEEK_API_KEY') or '').strip()
    return resolved_key


def deepseek_api_call(
    messages,
    temperature=0.7,
    timeout=300,
    model=None,
    max_attempts=3,
    api_url=None,
    api_key=None,
    task=None,
    max_tokens=None,
    thinking=None,
    reasoning_effort=None,
    response_format=None,
    task_policy=None,
    return_response=False,
    freeze_id=None,
    cancellation=None,
    attempt_guard=None,
    stream=False,
    stream_fallback=True,
    usage_observer=None,
):
    """Call the configured AI provider while preserving the legacy signature."""
    provider_guard = (
        attempt_guard
        if attempt_guard is not None
        else _AI_ATTEMPT_GUARD.get()
    )
    if provider_guard is not None and not callable(provider_guard):
        raise TypeError("AI attempt guard must be callable")
    if usage_observer is not None and not callable(usage_observer):
        raise TypeError("AI usage observer must be callable")

    def notify_usage(fields):
        if usage_observer is None:
            return
        try:
            usage_observer(dict(fields))
        except Exception:
            # Usage telemetry is best effort and must not change a valid
            # Provider or business outcome.
            pass
    if cancellation is None and provider_guard is not None:
        def guard_cancellation_requested():
            provider_guard()
            return False

        cancellation = CallbackCancellationToken(guard_cancellation_requested)

    try:
        contract = contract_for(task, task_policy)
    except KeyError as exc:
        raise DeepSeekConfigurationError(
            f"未知 AI 任务策略: {exc.args[0]}"
        ) from exc
    policy_name = contract.name
    policy = AI_TASK_POLICIES[policy_name]
    role = "quality"
    assigned_model = ""
    settings_version = 0
    resolved_provider = current_app.config.get('AI_PROVIDER', 'openai_compatible')
    resolved_wire_api = "chat_completions"
    frozen_url = ""
    resolved_dns_mode = "domestic"
    resolved_dns_servers = None
    frozen_profile_id = None
    frozen_capabilities = {}
    job_id = str(freeze_id or (request.headers.get("X-Cuoti-Job-Id", "") if has_request_context() else ""))
    try:
        settings_service = get_settings_service(current_app._get_current_object())
        settings = settings_service.public_ai_settings()
        role = settings_service.model_role_for_task(task, task_policy=policy_name)
        freeze_policy_name = (
            "material_analysis"
            if policy_name in {
                "material_analysis",
                "material_analysis_shard",
                "material_analysis_summary",
            }
            else policy_name
        )
        freeze_key = (
            f"{job_id}:{task or 'unspecified'}:{freeze_policy_name}" if job_id else ""
        )
        freezes = current_app.extensions.setdefault("ai_task_freezes", {})
        with _FREEZE_LOCK:
            frozen = freezes.get(freeze_key) if freeze_key else None
            if frozen is None:
                frozen = {
                    "model": settings["fast_model" if role == "fast" else "quality_model"],
                    "url": settings["base_url"],
                    "provider": settings["provider"],
                    "wire_api": settings.get("wire_api", "chat_completions"),
                    "version": settings["version"],
                    "role": role,
                    "dns_mode": settings.get("dns_mode", "domestic"),
                    "dns_servers": settings.get("dns_servers"),
                    "reasoning_effort": settings.get("reasoning_effort", ""),
                    "profile_id": settings.get("active_profile"),
                    "credential_ref": settings.get("credential_ref"),
                    "capabilities": settings.get("capabilities", {}),
                }
                if freeze_key:
                    if len(freezes) >= 100:
                        freezes.pop(next(iter(freezes)))
                    freezes[freeze_key] = frozen
        assigned_model = frozen["model"]
        frozen_url = frozen["url"]
        resolved_provider = frozen["provider"]
        resolved_wire_api = _frozen_wire_api(frozen)
        settings_version = frozen["version"]
        role = frozen["role"]
        resolved_dns_mode = frozen.get("dns_mode", "domestic")
        resolved_dns_servers = frozen.get("dns_servers")
        profile_reasoning_effort = frozen.get("reasoning_effort", "")
        frozen_profile_id = frozen.get("profile_id")
        frozen_capabilities = (
            dict(frozen.get("capabilities"))
            if isinstance(frozen.get("capabilities"), dict)
            else {}
        )
    except SettingsError:
        role = "quality"
        profile_reasoning_effort = ""
    configured_model = (
        current_app.config['DEEPSEEK_FLASH_MODEL']
        if role == "fast"
        else current_app.config['DEEPSEEK_MODEL']
    )
    resolved_model = str(model or assigned_model or configured_model or '').strip()
    resolved_url = str(api_url or frozen_url or current_app.config['DEEPSEEK_URL'] or '').strip()
    resolved_key = resolve_deepseek_api_key(api_key, profile_id=frozen_profile_id)

    if not resolved_key:
        raise DeepSeekConfigurationError(
            '未配置 DeepSeek API 密钥，请设置 DEEPSEEK_API_KEY'
        )
    if not resolved_url:
        raise DeepSeekConfigurationError(
            '未配置 DeepSeek API 地址，请设置 DEEPSEEK_URL'
        )
    if not resolved_model:
        raise DeepSeekConfigurationError(
            '未配置 DeepSeek 模型，请设置 DEEPSEEK_MODEL'
        )
    try:
        model_capabilities = resolve_model_context_capabilities(
            resolved_model,
            configured_context_window=frozen_capabilities.get("context_window"),
            configured_max_output_tokens=frozen_capabilities.get(
                "max_output_tokens"
            ),
        )
    except (ModelCapabilityError, ValueError) as exc:
        raise DeepSeekConfigurationError(str(exc)) from exc
    try:
        provider = create_provider(
            resolved_provider,
            {
                'base_url': resolved_url,
                'wire_api': resolved_wire_api,
                'dns_mode': resolved_dns_mode,
                'dns_servers': resolved_dns_servers,
            },
            resolved_key,
        )
        supports_thinking = frozen_capabilities.get("thinking", True) is not False
        supports_effort = frozen_capabilities.get("reasoning_effort", True) is not False
        supports_json = frozen_capabilities.get("json_object", True) is not False
        effective_thinking = thinking
        if effective_thinking is None and contract.thinking and supports_thinking:
            effective_thinking = {"type": "enabled"}
        if not supports_thinking:
            effective_thinking = None
        effective_effort = capped_reasoning_effort(
            reasoning_effort,
            contract_effort=contract.reasoning_effort,
            profile_cap=profile_reasoning_effort,
        )
        if not supports_effort:
            effective_effort = None
        effective_format = response_format
        if effective_format is None and contract.json_output and supports_json:
            effective_format = {"type": "json_object"}
        capability_output_limit = model_capabilities.max_output_tokens
        first_limit = bounded_output_tokens(
            int(max_tokens or contract.first_output_tokens), capability_output_limit
        )
        retry_limit = bounded_output_tokens(
            max(first_limit, int(contract.retry_output_tokens)),
            capability_output_limit,
        )
        try:
            telemetry_secret = load_or_create_installation_secret(
                settings_service.keyring
            )
            prompt_hash = prompt_hmac(messages, telemetry_secret)
            task_hash = task_hmac(
                job_id or f"unscoped:{policy_name}:{prompt_hash}",
                telemetry_secret,
            )
        except (QualityMetricsError, AttributeError):
            # Missing observability credentials must not weaken privacy or fail
            # the business call. In that case the prompt identity is omitted.
            prompt_hash = None
            task_hash = None

        try:
            context_metrics = ensure_context_safety(
                messages,
                context_window_tokens=model_capabilities.context_window_tokens,
                max_output_tokens=retry_limit,
                tokenizer=official_tokenizer_for_model(resolved_model),
            )
            context_metrics.update(
                {
                    "context_capability_policy": model_capabilities.policy_version,
                    "context_capability_source": model_capabilities.source,
                }
            )
        except ContextSafetyError as exc:
            safe_context = {
                "estimated_prompt_tokens": exc.estimated_prompt_tokens,
                "context_input_limit": exc.input_limit_tokens,
                "context_window_tokens": exc.context_window_tokens,
                "context_capability_policy": model_capabilities.policy_version,
                "context_capability_source": model_capabilities.source,
                "safety_reserve_tokens": max(
                    0,
                    exc.context_window_tokens
                    - exc.max_output_tokens
                    - exc.input_limit_tokens,
                ),
            }
            event_fields = {
                "provider_calls": 0,
                "attempts": 0,
                "usage_observations": 0,
                "usage_missing_calls": 0,
                "usage_missing": False,
                "call_reasons": {},
                **safe_context,
            }
            record_usage_best_effort(
                current_app._get_current_object(),
                {
                    "task": task or "unspecified",
                    "task_policy": policy_name,
                    "stage": policy_name,
                    "task_hash": task_hash,
                    "prompt_hash": prompt_hash,
                    "model": resolved_model,
                    "model_role": role,
                    "settings_version": settings_version,
                    "reasoning_effort": effective_effort,
                    "max_tokens": retry_limit,
                    "success": False,
                    "event_scope": "provider",
                    "error_code": exc.code,
                    "error_class": "context_safety",
                    "prompt_version": contract.prompt_version,
                    "schema_version": contract.schema_version,
                    **event_fields,
                },
            )
            notify_usage(event_fields)
            raise DeepSeekServiceError(
                str(exc),
                retryable=False,
                code=exc.code,
                error_class="context_safety",
                details=safe_context,
            ) from exc

        aggregate = PhysicalCallAggregate()
        logical_started = time.monotonic()

        def remaining_timeout(*, first: bool = False) -> float:
            if first:
                return float(timeout)
            remaining = float(timeout) - (time.monotonic() - logical_started)
            if remaining <= 0:
                raise AIProviderError(
                    "NETWORK",
                    "AI 逻辑调用已超过总超时，请稍后重试",
                    retryable=True,
                    attempts=aggregate.attempts,
                    duration_ms=max(
                        0, round((time.monotonic() - logical_started) * 1000)
                    ),
                    error_class="logical_timeout",
                )
            return remaining

        def complete_with_attempt_guard(*, call_timeout, output_limit):
            if provider_guard is not None:
                provider_guard()
            provider_kwargs = {
                "model": resolved_model,
                "temperature": temperature,
                "timeout": call_timeout,
                "max_attempts": max_attempts,
                "max_tokens": output_limit,
                "thinking": effective_thinking,
                "reasoning_effort": effective_effort,
                "response_format": effective_format,
                "task_policy": policy_name,
                "cancellation": cancellation,
            }
            response = None
            stream_error = None
            if stream:
                for event in provider.stream_complete(messages, **provider_kwargs):
                    if provider_guard is not None:
                        provider_guard()
                    if event.kind == "error":
                        stream_error = event.error or AIProviderError(
                            "INVALID_RESPONSE",
                            "AI 流式响应异常，请稍后重试",
                            retryable=True,
                            error_class="invalid_stream",
                        )
                        break
                    if event.kind == "completed":
                        response = event.response
                if (
                    stream_error is not None
                    and stream_error.code == "STREAM_UNSUPPORTED"
                    and stream_fallback
                ):
                    aggregate.add_error(stream_error, reason="stream_fallback")
                    response = provider.complete(messages, **provider_kwargs)
                    stream_error = None
                if stream_error is not None:
                    raise stream_error
                if response is None:
                    raise AIProviderError(
                        "INVALID_RESPONSE",
                        "AI 流式响应未正常结束",
                        retryable=True,
                        error_class="incomplete_stream",
                    )
            else:
                response = provider.complete(messages, **provider_kwargs)
            if provider_guard is not None:
                provider_guard()
            return response

        try:
            response = complete_with_attempt_guard(
                call_timeout=remaining_timeout(first=True),
                output_limit=first_limit,
            )
        except AIProviderError as exc:
            aggregate.add_error(exc, reason="initial")
            raise
        aggregate.add_response(response, reason="initial")
        invalid_json = False
        if contract.json_output or effective_format:
            try:
                json.loads(response.content)
            except json.JSONDecodeError:
                invalid_json = True
        expanded = response.finish_reason == "length" or invalid_json
        if expanded:
            reason = "json_repair" if invalid_json else "output_expansion"
            second_timeout = remaining_timeout()
            try:
                response = complete_with_attempt_guard(
                    call_timeout=second_timeout,
                    output_limit=retry_limit,
                )
            except AIProviderError as exc:
                aggregate.add_error(exc, reason=reason)
                raise
            aggregate.add_response(response, reason=reason)
            if response.finish_reason == "length":
                raise AIProviderError("OUTPUT_TRUNCATED", "AI 输出仍被截断，未保存部分结果")
            if contract.json_output or effective_format:
                try:
                    json.loads(response.content)
                except json.JSONDecodeError as exc:
                    raise AIProviderError("INVALID_RESPONSE", "AI JSON 输出校验失败，未保存部分结果") from exc
        aggregate_fields = aggregate.as_event_fields()
        telemetry = {
            "task": task or "unspecified", "task_policy": policy_name,
            "stage": policy_name, "task_hash": task_hash,
            "model": response.model, "model_role": role,
            "settings_version": settings_version, "reasoning_effort": effective_effort,
            "max_tokens": retry_limit if expanded else first_limit,
            "finish_reason": response.finish_reason,
            "success": True, "retry_expanded": expanded, "event_scope":"provider",
            "request_id": response.request_id, "prompt_hash": prompt_hash,
            "prompt_version": contract.prompt_version,
            "schema_version": contract.schema_version,
            **context_metrics,
            **aggregate_fields,
        }
        record_usage_best_effort(current_app._get_current_object(), telemetry)
        notify_usage(aggregate_fields)
        return response if return_response else response.content
    except AIProviderError as exc:
        failure_aggregate = locals().get("aggregate")
        if not isinstance(failure_aggregate, PhysicalCallAggregate):
            failure_aggregate = PhysicalCallAggregate()
            failure_aggregate.add_error(exc, reason="initial")
        failure_fields = failure_aggregate.as_event_fields()
        record_usage_best_effort(current_app._get_current_object(), {
                "task": task or "unspecified", "task_policy": policy_name,
                "stage": policy_name,
                "task_hash": locals().get("task_hash"),
                "prompt_hash": locals().get("prompt_hash"),
                "model": resolved_model, "model_role": role,
                "settings_version": settings_version,
                "reasoning_effort": locals().get("effective_effort") or reasoning_effort or contract.reasoning_effort,
                "max_tokens": max_tokens or policy["first"], "success": False,
                "event_scope":"provider",
                "error_code": exc.code,
                "attempts": exc.attempts,
                "duration_ms": exc.duration_ms,
                "error_class": exc.error_class,
                "prompt_version": contract.prompt_version,
                "schema_version": contract.schema_version,
                **locals().get("context_metrics", {}),
                **failure_fields,
            })
        notify_usage(failure_fields)
        if exc.code == 'CONFIGURATION':
            raise DeepSeekConfigurationError(str(exc)) from exc
        raise DeepSeekServiceError(
            str(exc),
            retryable=exc.retryable,
            code=exc.code,
            error_class=exc.error_class,
            details={
                key: value
                for key, value in locals().get("context_metrics", {}).items()
                if key in {
                    "estimated_prompt_tokens",
                    "context_input_limit",
                    "context_window_tokens",
                    "safety_reserve_tokens",
                    "context_capability_policy",
                    "context_capability_source",
                }
            },
        ) from exc
