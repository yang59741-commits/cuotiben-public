"""Privacy-bounded, report-only AI quality observations.

The store deliberately accepts aggregate counters and boolean outcome flags
only.  Retrieval candidates, prompts and answers are outside its event
contract.  A prompt may be supplied to :meth:`record_outcome`; it is converted
to an installation-secret HMAC before the event reaches persistent storage.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


QUALITY_METRICS_SCHEMA_VERSION = "ai-quality-metrics-v1"
QUALITY_HMAC_SERVICE = "cuoti-app.ai-quality-metrics"
QUALITY_HMAC_USERNAME = "installation-hmac-v1"
QUALITY_REPORT_WINDOWS = (7, 30)

_LOCK = threading.RLock()
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}\Z")
_HMAC_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_HMAC_DOMAIN = b"cuoti-ai-quality-prompt-v1\0"
_TASK_HMAC_DOMAIN = b"cuoti-ai-telemetry-task-v1\0"
_DIMENSIONS = (
    "task",
    "prompt_version",
    "schema_version",
    "model_version",
    "capability_version",
    "recall_version",
)
_COUNTS = (
    "reference_opportunity_count",
    "final_reference_hit_count",
    "top_k_candidate_count",
    "top_k_adopted_count",
)
_FLAGS = (
    "immediate_regeneration",
    "deleted",
    "human_correction",
    "quota_relaxed",
    "partial_fallback",
    "low_quality_mode",
)
_ALLOWED_EVENT_FIELDS = frozenset(
    (*_DIMENSIONS, *_COUNTS, *_FLAGS, "occurred_at", "prompt_hmac")
)
_REQUIRED_EVENT_FIELDS = frozenset((*_DIMENSIONS, *_COUNTS, *_FLAGS, "prompt_hmac"))


class QualityMetricsError(ValueError):
    """A stable, non-sensitive quality metrics contract error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...


def load_or_create_installation_secret(
    keyring: KeyringBackend,
    *,
    service: str = QUALITY_HMAC_SERVICE,
    username: str = QUALITY_HMAC_USERNAME,
) -> bytes:
    """Load a strong installation secret from an injected credential vault.

    There is intentionally no file or environment-variable fallback: losing
    observability is safer than persisting the HMAC key alongside the events.
    """

    try:
        value = keyring.get_password(service, username)
        if value is None:
            value = secrets.token_urlsafe(32)
            keyring.set_password(service, username, value)
    except Exception as exc:
        raise QualityMetricsError(
            "HMAC_SECRET_UNAVAILABLE", "本机质量指标密钥不可用"
        ) from exc
    encoded = value.encode("utf-8")
    if len(encoded) < 32:
        raise QualityMetricsError(
            "HMAC_SECRET_INVALID", "本机质量指标密钥强度不足"
        )
    return encoded


def prompt_hmac(prompt: str | Sequence[Mapping[str, Any]], secret: bytes | str) -> str:
    """Return a domain-separated HMAC without retaining the prompt."""

    key = _secret_bytes(secret)
    if isinstance(prompt, str):
        canonical = prompt
    elif isinstance(prompt, Sequence) and not isinstance(prompt, (bytes, bytearray)):
        canonical = json.dumps(
            list(prompt), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    else:
        raise QualityMetricsError("PROMPT_INVALID", "prompt 必须是文本或消息序列")
    return hmac.new(key, _HMAC_DOMAIN + canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def task_hmac(task_id: str, secret: bytes | str) -> str:
    """Hash a durable task identifier without persisting the identifier."""

    key = _secret_bytes(secret)
    normalized = str(task_id or "").strip()
    if not normalized:
        raise QualityMetricsError("TASK_ID_INVALID", "任务标识不能为空")
    return hmac.new(
        key,
        _TASK_HMAC_DOMAIN + normalized.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _secret_bytes(secret: bytes | str) -> bytes:
    if isinstance(secret, str):
        encoded = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        encoded = secret
    else:
        raise QualityMetricsError("HMAC_SECRET_INVALID", "本机质量指标密钥无效")
    if len(encoded) < 32:
        raise QualityMetricsError("HMAC_SECRET_INVALID", "本机质量指标密钥强度不足")
    return encoded


def _utc_datetime(value: Any, *, default_now: bool) -> datetime:
    if value is None and default_now:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QualityMetricsError("TIMESTAMP_INVALID", "质量指标时间无效") from exc
    else:
        raise QualityMetricsError("TIMESTAMP_INVALID", "质量指标时间无效")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise QualityMetricsError("TIMESTAMP_INVALID", "质量指标时间必须含时区")
    return parsed.astimezone(timezone.utc)


def _identifier(field: str, value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise QualityMetricsError(
            "VERSION_DIMENSION_INVALID", f"{field} 必须是受控版本标识"
        )
    return value


def _count(field: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QualityMetricsError("COUNT_INVALID", f"{field} 必须是非负整数")
    return value


def _flag(field: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise QualityMetricsError("FLAG_INVALID", f"{field} 必须是布尔值")
    return value


def normalize_quality_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and return the exact persistent event representation."""

    if not isinstance(event, Mapping):
        raise QualityMetricsError("EVENT_INVALID", "质量指标事件必须是对象")
    keys = set(event)
    unexpected = keys - _ALLOWED_EVENT_FIELDS
    if unexpected:
        # Do not echo forbidden field names: they may themselves be sensitive.
        raise QualityMetricsError("PRIVATE_FIELD_FORBIDDEN", "事件含未获准字段")
    missing = _REQUIRED_EVENT_FIELDS - keys
    if missing:
        raise QualityMetricsError("EVENT_FIELDS_MISSING", "事件缺少必填聚合字段")

    row = {field: _identifier(field, event[field]) for field in _DIMENSIONS}
    row.update({field: _count(field, event[field]) for field in _COUNTS})
    row.update({field: _flag(field, event[field]) for field in _FLAGS})
    digest = event["prompt_hmac"]
    if not isinstance(digest, str) or not _HMAC_DIGEST.fullmatch(digest):
        raise QualityMetricsError("PROMPT_HMAC_INVALID", "prompt HMAC 无效")
    row["prompt_hmac"] = digest
    occurred_at = _utc_datetime(event.get("occurred_at"), default_now=True)
    row["occurred_at"] = occurred_at.isoformat(timespec="seconds")

    if row["final_reference_hit_count"] > row["reference_opportunity_count"]:
        raise QualityMetricsError("REFERENCE_COUNT_INVALID", "reference 命中数超过机会数")
    if row["top_k_adopted_count"] > row["top_k_candidate_count"]:
        raise QualityMetricsError("TOP_K_COUNT_INVALID", "top-K 采用数超过候选数")
    return row


class AIQualityMetrics:
    """Append-only quality event store with fixed rolling reports."""

    def __init__(self, path: str | Path, *, secret: bytes | str):
        self.path = Path(path)
        self._secret = _secret_bytes(secret)

    def record(
        self,
        event: Mapping[str, Any],
        *,
        prompt: str | Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Persist an outcome after deriving its HMAC inside this store.

        Callers cannot provide a digest, so a bare SHA-256 value cannot enter
        the quality event file through the public persistence API.
        """

        if not isinstance(event, Mapping):
            raise QualityMetricsError("EVENT_INVALID", "质量指标事件必须是对象")
        if "prompt_hmac" in event or set(event) - (_ALLOWED_EVENT_FIELDS - {"prompt_hmac"}):
            raise QualityMetricsError("PRIVATE_FIELD_FORBIDDEN", "事件含未获准字段")
        row = normalize_quality_event(
            {**event, "prompt_hmac": prompt_hmac(prompt, self._secret)}
        )
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with _LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        return dict(row)

    def record_outcome(
        self,
        *,
        task: str,
        prompt_version: str,
        schema_version: str,
        model_version: str,
        capability_version: str,
        recall_version: str,
        prompt: str | Sequence[Mapping[str, Any]],
        reference_opportunity_count: int,
        final_reference_hit_count: int,
        top_k_candidate_count: int,
        top_k_adopted_count: int,
        immediate_regeneration: bool,
        deleted: bool,
        human_correction: bool,
        quota_relaxed: bool,
        partial_fallback: bool,
        low_quality_mode: bool,
        occurred_at: datetime | str | None = None,
    ) -> dict[str, Any]:
        event = {
            "task": task,
            "prompt_version": prompt_version,
            "schema_version": schema_version,
            "model_version": model_version,
            "capability_version": capability_version,
            "recall_version": recall_version,
            "reference_opportunity_count": reference_opportunity_count,
            "final_reference_hit_count": final_reference_hit_count,
            "top_k_candidate_count": top_k_candidate_count,
            "top_k_adopted_count": top_k_adopted_count,
            "immediate_regeneration": immediate_regeneration,
            "deleted": deleted,
            "human_correction": human_correction,
            "quota_relaxed": quota_relaxed,
            "partial_fallback": partial_fallback,
            "low_quality_mode": low_quality_mode,
        }
        if occurred_at is not None:
            event["occurred_at"] = occurred_at
        return self.record(event, prompt=prompt)

    def record_outcome_best_effort(self, **outcome: Any) -> bool:
        """Keep observability failures outside the business result path."""

        try:
            self.record_outcome(**outcome)
        except (OSError, QualityMetricsError, TypeError):
            return False
        return True

    def report(self, *, as_of: datetime | str | None = None) -> dict[str, Any]:
        end = _utc_datetime(as_of, default_now=True)
        rows = self._read_rows()
        windows: dict[str, Any] = {}
        for days in QUALITY_REPORT_WINDOWS:
            start = end - timedelta(days=days)
            selected = [
                row
                for row in rows
                if start < _utc_datetime(row["occurred_at"], default_now=False) <= end
            ]
            windows[str(days)] = _window_report(days, start, end, selected)
        return {
            "report_schema_version": QUALITY_METRICS_SCHEMA_VERSION,
            "mode": "report_only",
            "online_weight_updates": False,
            "as_of": end.isoformat(timespec="seconds"),
            "windows": windows,
        }

    def _read_rows(self) -> list[dict[str, Any]]:
        with _LOCK:
            if not self.path.is_file():
                return []
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
        rows: list[dict[str, Any]] = []
        for line in lines:
            try:
                value = json.loads(line)
                rows.append(normalize_quality_event(value))
            except (json.JSONDecodeError, QualityMetricsError, TypeError):
                continue
        return rows


def _window_report(
    days: int,
    start: datetime,
    end: datetime,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[tuple(row[field] for field in _DIMENSIONS)].append(row)
    series = []
    for key in sorted(grouped):
        dimensions = dict(zip(_DIMENSIONS, key))
        series.append({**dimensions, **_aggregate(grouped[key])})
    return {
        "days": days,
        "start_exclusive": start.isoformat(timespec="seconds"),
        "end_inclusive": end.isoformat(timespec="seconds"),
        "aggregate": _aggregate(rows),
        "series": series,
    }


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    event_count = len(rows)
    totals = {field: sum(row[field] for row in rows) for field in _COUNTS}
    flags = {field: sum(bool(row[field]) for row in rows) for field in _FLAGS}

    def ratio(numerator: int, denominator: int) -> float | None:
        return numerator / denominator if denominator else None

    return {
        "event_count": event_count,
        **totals,
        "final_reference_hit_rate": ratio(
            totals["final_reference_hit_count"], totals["reference_opportunity_count"]
        ),
        "top_k_adoption_rate": ratio(
            totals["top_k_adopted_count"], totals["top_k_candidate_count"]
        ),
        "immediate_regeneration_count": flags["immediate_regeneration"],
        "immediate_regeneration_rate": ratio(flags["immediate_regeneration"], event_count),
        "deletion_count": flags["deleted"],
        "deletion_rate": ratio(flags["deleted"], event_count),
        "human_correction_count": flags["human_correction"],
        "human_correction_rate": ratio(flags["human_correction"], event_count),
        "quota_relaxed_count": flags["quota_relaxed"],
        "quota_relaxed_rate": ratio(flags["quota_relaxed"], event_count),
        "partial_fallback_count": flags["partial_fallback"],
        "partial_fallback_rate": ratio(flags["partial_fallback"], event_count),
        "low_quality_mode_count": flags["low_quality_mode"],
        "low_quality_mode_rate": ratio(flags["low_quality_mode"], event_count),
    }
