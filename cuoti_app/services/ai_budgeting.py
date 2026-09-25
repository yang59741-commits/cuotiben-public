"""Shared budgeting helpers for AI messages and logical calls."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
DEFAULT_CONTEXT_RESERVE_RATIO = 0.12
MODEL_CONTEXT_CAPABILITY_POLICY = "published-model-capabilities-2026-08-27-v1"


@dataclass(frozen=True, slots=True)
class ModelContextCapabilities:
    context_window_tokens: int
    max_output_tokens: int
    source: str
    policy_version: str = MODEL_CONTEXT_CAPABILITY_POLICY


@dataclass(frozen=True, slots=True)
class ModelCapabilityError(ValueError):
    code: str = "context_capability_unconfigured"

    def __str__(self) -> str:
        return "当前模型未配置上下文与输出能力，请先在 AI 设置中填写后重试"


# Exact built-in model IDs only.  Aliases or custom gateway names must carry
# explicit limits in their profile so a future model cannot silently inherit a
# fictional capacity.  Values are the Providers' published API limits.
_PUBLISHED_MODEL_CAPABILITIES: dict[str, tuple[int, int]] = {
    # The provider currently advertises the 4.1 Flash endpoint as the shorter
    # ``deepseek-flash`` id.  Keep it bound to the same published limits as
    # the fully qualified id rather than treating it as an unconfigured model.
    "deepseek-flash": (1_000_000, 384_000),
    "deepseek-v4-flash": (1_000_000, 384_000),
    "deepseek-v4-pro": (1_000_000, 384_000),
    "gpt-5.6-sol": (1_050_000, 128_000),
    "claude-opus-5": (1_000_000, 128_000),
    "claude-haiku-4-5-20251001": (200_000, 64_000),
}


@dataclass(frozen=True, slots=True)
class ContextSafetyError(ValueError):
    estimated_prompt_tokens: int
    input_limit_tokens: int
    context_window_tokens: int
    max_output_tokens: int
    code: str = "context_safety_exceeded"

    def __str__(self) -> str:
        return "单次 AI 上下文超过安全上限，请缩小输入范围后重试"


def _configured_limit(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("AI 上下文能力配置无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("AI 上下文能力配置无效") from exc
    if parsed < 0:
        raise ValueError("AI 上下文能力配置无效")
    return parsed


def resolve_model_context_capabilities(
    model: object,
    *,
    configured_context_window: object = None,
    configured_max_output_tokens: object = None,
) -> ModelContextCapabilities:
    """Resolve exact limits without assigning an arbitrary unknown fallback."""

    model_name = str(model or "").strip().casefold()
    configured_window = _configured_limit(configured_context_window)
    configured_output = _configured_limit(configured_max_output_tokens)
    published = _PUBLISHED_MODEL_CAPABILITIES.get(model_name)
    if published is None:
        if configured_window < 1 or configured_output < 1:
            raise ModelCapabilityError()
        return ModelContextCapabilities(
            context_window_tokens=configured_window,
            max_output_tokens=configured_output,
            source="profile",
        )

    published_window, published_output = published
    return ModelContextCapabilities(
        context_window_tokens=(
            min(configured_window, published_window)
            if configured_window
            else published_window
        ),
        max_output_tokens=(
            min(configured_output, published_output)
            if configured_output
            else published_output
        ),
        source="profile+published" if configured_window or configured_output else "published",
    )


def _official_token_count(
    text: str,
    tokenizer: Any,
) -> int | None:
    if tokenizer is None:
        return None
    try:
        encoded = (
            tokenizer.encode(text)
            if callable(getattr(tokenizer, "encode", None))
            else tokenizer(text)
            if callable(tokenizer)
            else None
        )
        if isinstance(encoded, bool):
            return None
        if isinstance(encoded, int):
            return encoded if encoded >= 0 else None
        return len(encoded) if encoded is not None else None
    except Exception:
        return None


@lru_cache(maxsize=32)
def official_tokenizer_for_model(model: object) -> Any:
    """Resolve an installed vendor tokenizer; return ``None`` when unavailable."""

    model_name = str(model or "").strip()
    if not model_name:
        return None
    try:
        import tiktoken  # type: ignore[import-not-found]

        return tiktoken.encoding_for_model(model_name)
    except (ImportError, KeyError, AttributeError, ValueError):
        return None


def estimate_text_tokens(value: object, *, tokenizer: Any = None) -> int:
    """Use an official tokenizer when supplied, otherwise the plan fallback.

    Fallback: ``ceil(1.5 * CJK chars + non-CJK UTF-8 bytes / 4)``.
    """

    text = str(value or "")
    if not text:
        return 0
    official = _official_token_count(text, tokenizer)
    if official is not None:
        return official
    cjk = 0
    non_cjk_bytes = 0
    for character in text:
        if _CJK_RE.fullmatch(character):
            cjk += 1
        else:
            non_cjk_bytes += len(character.encode("utf-8"))
    return max(1, math.ceil(1.5 * cjk + non_cjk_bytes / 4))


def estimate_messages_tokens(messages: object, *, tokenizer: Any = None) -> int:
    if not isinstance(messages, (list, tuple)):
        return estimate_text_tokens(messages, tokenizer=tokenizer)
    total = 0
    for message in messages:
        if isinstance(message, dict):
            total += 4
            total += estimate_text_tokens(message.get("role"), tokenizer=tokenizer)
            total += estimate_text_tokens(message.get("content"), tokenizer=tokenizer)
        else:
            total += estimate_text_tokens(message, tokenizer=tokenizer)
    return total + 2


def context_input_limit(
    context_window_tokens: object,
    max_output_tokens: object,
    *,
    reserve_ratio: float = DEFAULT_CONTEXT_RESERVE_RATIO,
) -> int:
    try:
        window = int(context_window_tokens)
        output = int(max_output_tokens or 0)
        reserve = float(reserve_ratio)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("AI 上下文能力配置无效") from exc
    if window < 1 or output < 0 or not 0.10 <= reserve <= 0.15:
        raise ValueError("AI 上下文能力配置无效")
    return max(0, window - output - math.ceil(window * reserve))


def ensure_context_safety(
    messages: object,
    *,
    context_window_tokens: object,
    max_output_tokens: object,
    reserve_ratio: float = DEFAULT_CONTEXT_RESERVE_RATIO,
    tokenizer: Any = None,
) -> dict[str, int]:
    window = int(context_window_tokens)
    output = max(0, int(max_output_tokens or 0))
    input_limit = context_input_limit(
        window,
        output,
        reserve_ratio=reserve_ratio,
    )
    estimate = estimate_messages_tokens(messages, tokenizer=tokenizer)
    if estimate > input_limit:
        raise ContextSafetyError(
            estimated_prompt_tokens=estimate,
            input_limit_tokens=input_limit,
            context_window_tokens=window,
            max_output_tokens=output,
        )
    return {
        "estimated_prompt_tokens": estimate,
        "context_input_limit": input_limit,
        "context_window_tokens": window,
        "max_output_tokens": output,
        "safety_reserve_tokens": math.ceil(window * reserve_ratio),
    }


def bounded_output_tokens(requested: int, capability_limit: object = None) -> int:
    value = max(1, int(requested))
    if isinstance(capability_limit, bool):
        return value
    try:
        limit = int(capability_limit or 0)
    except (TypeError, ValueError):
        limit = 0
    return min(value, limit) if limit > 0 else value
