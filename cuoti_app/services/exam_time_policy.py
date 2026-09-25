"""Frozen deadline policy and deterministic exam timing receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .job_contracts import PermanentJobError, timestamp


TIME_POLICY_SCHEMA_VERSION = "exam-time-policy-v1"
DEFAULT_OBSERVATION_TARGET_MS = 1_200_000
DEFAULT_FINAL_RESERVE_MS = 90_000
EXAM_DEADLINE_ERROR_CODE = "exam_deadline_exceeded"
_VERSION_RE = re.compile(r"[a-z0-9][a-z0-9_.:-]{0,79}\Z")

PROGRESS_STAGE_NAMES = {
    0: "validate_input",
    1: "prepare_sources",
    2: "material_understanding",
    3: "ai_generation",
    4: "selection_and_validation",
    5: "layout_generation",
    6: "pdf_render_and_publication",
    7: "receipt_and_finalization",
}


def _strict_int(value: Any, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label}必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def parse_timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("组卷时限时间戳无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ExamTimePolicySpec:
    mode: str
    version: str
    target_elapsed_ms: int
    final_reserve_ms: int
    schema_version: str = TIME_POLICY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode,
            "version": self.version,
            "target_elapsed_ms": self.target_elapsed_ms,
            "final_reserve_ms": self.final_reserve_ms,
        }


@dataclass(frozen=True, slots=True)
class FrozenExamTimePolicy:
    spec: ExamTimePolicySpec
    created_at: str
    deadline_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "created_at": self.created_at,
            "deadline_at": self.deadline_at,
        }


def normalize_policy_spec(value: Mapping[str, Any] | None = None) -> ExamTimePolicySpec:
    raw = dict(value or {})
    allowed = {
        "schema_version",
        "mode",
        "version",
        "target_elapsed_ms",
        "final_reserve_ms",
    }
    if set(raw) - allowed:
        raise ValueError("组卷时限策略包含未知字段")
    schema_version = str(
        raw.get("schema_version") or TIME_POLICY_SCHEMA_VERSION
    ).strip()
    if schema_version != TIME_POLICY_SCHEMA_VERSION:
        raise ValueError("组卷时限策略结构版本无效")
    mode = str(raw.get("mode") or "observe").strip().casefold()
    if mode not in {"observe", "enforce"}:
        raise ValueError("组卷时限策略必须是 observe 或 enforce")
    default_version = (
        "exam-time-observe-v1" if mode == "observe" else "exam-time-enforce-v1"
    )
    version = str(raw.get("version") or default_version).strip().casefold()
    if not _VERSION_RE.fullmatch(version):
        raise ValueError("组卷时限策略版本无效")
    target_elapsed_ms = _strict_int(
        raw.get("target_elapsed_ms", DEFAULT_OBSERVATION_TARGET_MS),
        "组卷目标时长",
        minimum=1_000,
        maximum=86_400_000,
    )
    final_reserve_ms = _strict_int(
        raw.get("final_reserve_ms", DEFAULT_FINAL_RESERVE_MS),
        "组卷最终保留时长",
        minimum=0,
        maximum=3_600_000,
    )
    if final_reserve_ms >= target_elapsed_ms:
        raise ValueError("组卷最终保留时长必须小于目标时长")
    return ExamTimePolicySpec(
        mode=mode,
        version=version,
        target_elapsed_ms=target_elapsed_ms,
        final_reserve_ms=final_reserve_ms,
    )


def policy_spec_from_config(config: Mapping[str, Any]) -> ExamTimePolicySpec:
    mode = str(config.get("EXAM_DEADLINE_MODE") or "observe").strip().casefold()
    default_version = (
        "exam-time-observe-v1" if mode == "observe" else "exam-time-enforce-v1"
    )
    deadline_seconds = config.get("EXAM_DEADLINE_SECONDS")
    final_reserve_seconds = config.get("EXAM_FINAL_RESERVE_SECONDS")
    return normalize_policy_spec(
        {
            "mode": mode,
            "version": config.get("EXAM_DEADLINE_POLICY_VERSION") or default_version,
            "target_elapsed_ms": int(
                1_200 if deadline_seconds is None else deadline_seconds
            )
            * 1_000,
            "final_reserve_ms": int(
                90 if final_reserve_seconds is None else final_reserve_seconds
            )
            * 1_000,
        }
    )


def freeze_policy(
    spec: ExamTimePolicySpec | Mapping[str, Any] | None,
    created_at: datetime,
) -> FrozenExamTimePolicy:
    normalized = (
        spec if isinstance(spec, ExamTimePolicySpec) else normalize_policy_spec(spec)
    )
    created = created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created = created.astimezone(timezone.utc)
    deadline = (
        created + timedelta(milliseconds=normalized.target_elapsed_ms)
        if normalized.mode == "enforce"
        else None
    )
    return FrozenExamTimePolicy(
        spec=normalized,
        created_at=timestamp(created),
        deadline_at=timestamp(deadline) if deadline is not None else None,
    )


class ExamDeadlineExceeded(PermanentJobError):
    """A frozen enforce deadline has elapsed; retrying cannot reset it."""

    def __init__(
        self,
        *,
        stage: str,
        deadline_at: str,
        policy_version: str,
        now: datetime,
    ) -> None:
        super().__init__(
            "组卷任务已达到正式截止时间，未发布的工作已停止",
            code=EXAM_DEADLINE_ERROR_CODE,
            category="business",
            details={
                "details": {
                    "failed_stage": str(stage or "unknown"),
                    "deadline_at": deadline_at,
                    "policy_version": policy_version,
                    "checked_at": timestamp(now),
                }
            },
        )


def enforce_remaining_seconds(
    job: Mapping[str, Any],
    *,
    now: datetime,
    stage: str,
    reserve_seconds: float = 0.0,
) -> float | None:
    policy = job.get("time_policy")
    if not isinstance(policy, Mapping) or policy.get("mode") != "enforce":
        return None
    deadline_at = str(policy.get("deadline_at") or "")
    policy_version = str(policy.get("version") or "")
    deadline = parse_timestamp(deadline_at)
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    remaining = (deadline - current.astimezone(timezone.utc)).total_seconds()
    usable = remaining - max(0.0, float(reserve_seconds))
    if usable <= 0:
        raise ExamDeadlineExceeded(
            stage=stage,
            deadline_at=deadline_at,
            policy_version=policy_version,
            now=current,
        )
    return usable


def bounded_stage_timeout(
    job: Mapping[str, Any],
    requested_seconds: float | None,
    *,
    now: datetime,
    stage: str,
    reserve_seconds: float = 0.0,
) -> float | None:
    usable = enforce_remaining_seconds(
        job,
        now=now,
        stage=stage,
        reserve_seconds=reserve_seconds,
    )
    if usable is None:
        return requested_seconds
    if requested_seconds is None:
        return usable
    return min(max(0.001, float(requested_seconds)), usable)


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, parsed)


def normalize_timing_accumulator(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    stages = raw.get("stage_elapsed_ms")
    metrics = raw.get("metrics")
    return {
        "stage_elapsed_ms": {
            str(key): _nonnegative_int(item)
            for key, item in (stages.items() if isinstance(stages, Mapping) else ())
            if str(key) in set(PROGRESS_STAGE_NAMES.values()) | {"worker_overhead"}
        },
        "metrics": {
            str(key): _nonnegative_int(item)
            for key, item in (metrics.items() if isinstance(metrics, Mapping) else ())
            if str(key)
            in {
                "model_calls",
                "model_retry_count",
                "candidate_count",
                "provider_calls",
                "pdf_render_elapsed_ms",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cached_tokens",
                "total_tokens",
                "usage_missing_calls",
            }
        },
    }


def merge_timing_accumulators(*values: Mapping[str, Any] | None) -> dict[str, Any]:
    merged = normalize_timing_accumulator(None)
    for value in values:
        normalized = normalize_timing_accumulator(value)
        for key, amount in normalized["stage_elapsed_ms"].items():
            merged["stage_elapsed_ms"][key] = (
                merged["stage_elapsed_ms"].get(key, 0) + amount
            )
        for key, amount in normalized["metrics"].items():
            if key == "candidate_count":
                merged["metrics"][key] = max(
                    merged["metrics"].get(key, 0), amount
                )
            else:
                merged["metrics"][key] = merged["metrics"].get(key, 0) + amount
    return merged


def build_timing_receipt(
    *,
    created_at: str,
    started_at: str | None,
    finished_at: str,
    time_policy: Mapping[str, Any],
    accumulator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    created = parse_timestamp(created_at)
    finished = parse_timestamp(finished_at)
    started = parse_timestamp(started_at) if started_at else finished
    if started < created:
        started = created
    if finished < started:
        finished = started
    queue_elapsed_ms = max(0, round((started - created).total_seconds() * 1_000))
    generation_elapsed_ms = max(
        0, round((finished - started).total_seconds() * 1_000)
    )
    total_elapsed_ms = max(
        0, round((finished - created).total_seconds() * 1_000)
    )
    normalized = normalize_timing_accumulator(accumulator)
    stages = dict(normalized["stage_elapsed_ms"])
    recorded = sum(stages.values())
    if recorded < generation_elapsed_ms:
        stages["worker_overhead"] = stages.get("worker_overhead", 0) + (
            generation_elapsed_ms - recorded
        )
    elif recorded > generation_elapsed_ms and stages:
        overflow = recorded - generation_elapsed_ms
        for key in reversed(tuple(stages)):
            reduction = min(stages[key], overflow)
            stages[key] -= reduction
            overflow -= reduction
            if overflow == 0:
                break
    stages = {key: value for key, value in stages.items() if value > 0}
    target_elapsed_ms = _nonnegative_int(time_policy.get("target_elapsed_ms"))
    bottleneck_stage = (
        max(stages, key=lambda key: (stages[key], key)) if stages else None
    )
    metrics = normalized["metrics"]
    return {
        "schema_version": "exam-timing-receipt-v1",
        "policy_mode": str(time_policy.get("mode") or "observe"),
        "policy_version": str(time_policy.get("version") or ""),
        "deadline_at": time_policy.get("deadline_at"),
        "observation_target_ms": target_elapsed_ms,
        "observation_target_exceeded": bool(
            target_elapsed_ms and total_elapsed_ms > target_elapsed_ms
        ),
        "created_at": timestamp(created),
        "started_at": timestamp(started) if started_at else None,
        "finished_at": timestamp(finished),
        "queue_elapsed_ms": queue_elapsed_ms,
        "generation_elapsed_ms": generation_elapsed_ms,
        "total_elapsed_ms": total_elapsed_ms,
        "stage_elapsed_ms": dict(sorted(stages.items())),
        "bottleneck_stage": bottleneck_stage,
        "model_calls": metrics.get("model_calls", 0),
        "model_retry_count": metrics.get("model_retry_count", 0),
        "provider_calls": metrics.get("provider_calls", 0),
        "candidate_count": metrics.get("candidate_count", 0),
        "pdf_render_elapsed_ms": metrics.get("pdf_render_elapsed_ms", 0),
        "prompt_tokens": metrics.get("prompt_tokens", 0),
        "completion_tokens": metrics.get("completion_tokens", 0),
        "reasoning_tokens": metrics.get("reasoning_tokens", 0),
        "cached_tokens": metrics.get("cached_tokens", 0),
        "total_tokens": metrics.get("total_tokens", 0),
        "usage_missing": bool(metrics.get("usage_missing_calls", 0)),
    }


__all__ = [
    "DEFAULT_FINAL_RESERVE_MS",
    "DEFAULT_OBSERVATION_TARGET_MS",
    "EXAM_DEADLINE_ERROR_CODE",
    "ExamDeadlineExceeded",
    "ExamTimePolicySpec",
    "FrozenExamTimePolicy",
    "PROGRESS_STAGE_NAMES",
    "TIME_POLICY_SCHEMA_VERSION",
    "bounded_stage_timeout",
    "build_timing_receipt",
    "enforce_remaining_seconds",
    "freeze_policy",
    "merge_timing_accumulators",
    "normalize_policy_spec",
    "normalize_timing_accumulator",
    "parse_timestamp",
    "policy_spec_from_config",
]
