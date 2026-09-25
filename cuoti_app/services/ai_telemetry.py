"""Privacy-preserving AI usage observations stored separately from learning data."""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_LOCK = threading.RLock()
_WARNING_INTERVAL_SECONDS = 60.0
_LAST_WARNING_AT: dict[str, float] = {}
_REASON_KEY = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}\Z")
_TASK_HASH = re.compile(r"[0-9a-f]{64}\Z")
_STAGE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,63}\Z")


class AITelemetryContractError(ValueError):
    """Raised before an uncorrelatable AI event can reach durable storage."""


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _reason_key(value: Any) -> str:
    reason = str(value or "unspecified").strip().casefold()
    return reason if _REASON_KEY.fullmatch(reason) else "unspecified"


def _usage_tokens(usage: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(usage, Mapping):
        return {}
    completion_details = usage.get("completion_tokens_details")
    if not isinstance(completion_details, Mapping):
        completion_details = {}
    prompt_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_details, Mapping):
        prompt_details = {}
    reasoning_tokens = _nonnegative_int(usage.get("reasoning_tokens"))
    if reasoning_tokens is None:
        reasoning_tokens = _nonnegative_int(
            completion_details.get("reasoning_tokens")
        )
    cached_tokens = _nonnegative_int(usage.get("cached_tokens"))
    if cached_tokens is None:
        cached_tokens = _nonnegative_int(prompt_details.get("cached_tokens"))
    values = {
        "prompt_tokens": _nonnegative_int(usage.get("prompt_tokens")),
        "completion_tokens": _nonnegative_int(usage.get("completion_tokens")),
        "reasoning_tokens": reasoning_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": _nonnegative_int(usage.get("total_tokens")),
    }
    if (
        values["total_tokens"] is None
        and values["prompt_tokens"] is not None
        and values["completion_tokens"] is not None
    ):
        values["total_tokens"] = (
            values["prompt_tokens"] + values["completion_tokens"]
        )
    return {key: value for key, value in values.items() if value is not None}


@dataclass(slots=True)
class PhysicalCallAggregate:
    """Aggregate every Provider invocation belonging to one logical AI call.

    ``provider_calls`` counts calls to ``provider.complete`` while ``attempts``
    includes the transport retries reported by each call. Token totals contain
    only Provider-reported usage; ``usage_missing_calls`` makes partial usage
    explicit instead of silently treating it as zero.
    """

    provider_calls: int = 0
    attempts: int = 0
    duration_ms: int = 0
    usage_observations: int = 0
    usage_missing_calls: int = 0
    call_reasons: dict[str, int] = field(default_factory=dict)
    _tokens: dict[str, int] = field(default_factory=dict, repr=False)

    def add_call(
        self,
        *,
        attempts: Any,
        duration_ms: Any,
        usage: Mapping[str, Any] | None = None,
        reason: str = "initial",
    ) -> None:
        self.provider_calls += 1
        parsed_attempts = _nonnegative_int(attempts)
        parsed_duration = _nonnegative_int(duration_ms)
        self.attempts += parsed_attempts if parsed_attempts is not None else 0
        self.duration_ms += parsed_duration if parsed_duration is not None else 0
        reason_key = _reason_key(reason)
        self.call_reasons[reason_key] = self.call_reasons.get(reason_key, 0) + 1
        tokens = _usage_tokens(usage)
        if tokens:
            self.usage_observations += 1
            for key, value in tokens.items():
                self._tokens[key] = self._tokens.get(key, 0) + value
        else:
            self.usage_missing_calls += 1

    def add_response(self, response: Any, *, reason: str = "initial") -> None:
        self.add_call(
            attempts=getattr(response, "attempts", 1),
            duration_ms=getattr(response, "duration_ms", 0),
            usage=getattr(response, "usage", None),
            reason=reason,
        )

    def add_error(self, error: BaseException, *, reason: str = "initial") -> None:
        self.add_call(
            attempts=getattr(error, "attempts", 0),
            duration_ms=getattr(error, "duration_ms", 0),
            reason=reason,
        )

    def as_event_fields(self) -> dict[str, Any]:
        return {
            "provider_calls": self.provider_calls,
            "attempts": self.attempts,
            "duration_ms": self.duration_ms,
            "usage_observations": self.usage_observations,
            "usage_missing_calls": self.usage_missing_calls,
            "usage_missing": bool(self.usage_missing_calls),
            "call_reasons": dict(sorted(self.call_reasons.items())),
            **self._tokens,
        }


def _path(app) -> Path:
    configured = app.config.get("AI_USAGE_PATH")
    if configured:
        return Path(configured)
    settings = Path(app.config.get("SETTINGS_PATH") or "instance/settings.json")
    return settings.with_name("ai-usage.jsonl")


def record_usage(app, event: dict) -> None:
    task_hash = event.get("task_hash")
    stage = event.get("stage")
    if not isinstance(task_hash, str) or not _TASK_HASH.fullmatch(task_hash):
        raise AITelemetryContractError("AI 遥测缺少隐私安全任务哈希")
    if not isinstance(stage, str) or not _STAGE.fullmatch(stage):
        raise AITelemetryContractError("AI 遥测缺少稳定阶段标识")
    allowed = {
        "task", "task_policy", "model", "model_role", "settings_version",
        "reasoning_effort", "max_tokens", "finish_reason", "duration_ms",
        "attempts", "prompt_tokens", "completion_tokens", "reasoning_tokens",
        "cached_tokens", "total_tokens", "success", "retry_expanded",
        "request_id", "prompt_hash", "error_code", "error_class",
        "event_scope", "outcome", "manual_retry_count",
        "provider_calls", "usage_observations", "usage_missing_calls",
        "usage_missing", "call_reasons", "prompt_version", "schema_version",
        "task_hash", "stage", "estimated_prompt_tokens",
        "context_input_limit", "context_window_tokens",
        "safety_reserve_tokens", "context_capability_policy",
        "context_capability_source",
    }
    row = {key: event.get(key) for key in allowed if event.get(key) is not None}
    row["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path = _path(app)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def record_usage_best_effort(app, event: dict) -> bool:
    """Record sanitized usage without allowing an I/O failure to fail work.

    A warning contains only the exception class and is rate-limited per usage
    file. The exception message and path are deliberately not logged.
    """

    try:
        record_usage(app, event)
        with _LOCK:
            _LAST_WARNING_AT.pop(str(_path(app)), None)
        return True
    except AITelemetryContractError:
        # Missing correlation is a privacy contract failure.  Skipping the
        # observation is safer than emitting an unlinkable row, and telemetry
        # must never change a valid business outcome.
        return False
    except OSError as exc:
        warning_key = str(_path(app))
        now = time.monotonic()
        with _LOCK:
            previous = _LAST_WARNING_AT.get(warning_key)
            should_warn = (
                previous is None or now - previous >= _WARNING_INTERVAL_SECONDS
            )
            if should_warn:
                _LAST_WARNING_AT[warning_key] = now
        if should_warn:
            try:
                app.logger.warning(
                    "AI usage telemetry unavailable; business result preserved (%s)",
                    type(exc).__name__,
                )
            except Exception:
                pass
        return False


def usage_summary(app, *, limit: int = 5000) -> dict:
    path = _path(app)
    rows=[]
    with _LOCK:
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
                try:
                    value=json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    provider_rows=[row for row in rows if row.get("event_scope")!="business_job"]
    business_rows=[row for row in rows if row.get("event_scope")=="business_job"]
    grouped=defaultdict(list)
    for row in provider_rows:
        grouped[str(row.get("task_policy") or row.get("task") or "其他")].append(row)

    def percentile(values, fraction):
        values=sorted(int(value) for value in values if isinstance(value, (int, float)))
        if not values:
            return 0
        return values[min(len(values)-1, round((len(values)-1)*fraction))]

    tasks=[]
    for task, events in sorted(grouped.items()):
        totals=[event.get("total_tokens",0) for event in events]
        successes=sum(bool(event.get("success")) for event in events)
        tasks.append({
            "task": task,
            "count": len(events),
            "p50_tokens": percentile(totals,.5),
            "p95_tokens": percentile(totals,.95),
            "truncations": sum(event.get("finish_reason")=="length" for event in events),
            "retries": sum(int(event.get("attempts") or 1)-1 for event in events),
            "success_rate": successes/len(events) if events else None,
        })
    business_grouped=defaultdict(list)
    for row in business_rows:
        business_grouped[str(row.get("task") or "其他")].append(row)
    business_tasks=[]
    for task, events in sorted(business_grouped.items()):
        successes=sum(bool(event.get("success")) for event in events)
        business_tasks.append({
            "task":task,
            "count":len(events),
            "success_rate":successes/len(events) if events else None,
            "failed":sum(event.get("outcome")=="failed" for event in events),
            "cancelled":sum(event.get("outcome")=="cancelled" for event in events),
            "attempts":sum(int(event.get("attempts") or 0) for event in events),
        })
    return {
        "event_count":len(rows),
        "provider_event_count":len(provider_rows),
        "business_event_count":len(business_rows),
        "tasks":tasks,
        "business_tasks":business_tasks,
    }
