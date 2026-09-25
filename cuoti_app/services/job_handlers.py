"""Job handler registration, safe error mapping and worker lifecycle."""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from collections.abc import Mapping

from .jobs import PermanentJobError, RetryableJobError, run_registered_worker
from .job_registry import register_handler_map
from .job_handlers_ai import make_ai_handlers
from .job_handlers_generation import make_exam_generation_handler
from .job_handlers_data import make_data_import_handler
from .job_handlers_documents import make_document_handlers
from .job_handlers_exam import make_exam_domain_handlers
from .job_handlers_maintenance import make_memory_maintenance_handler
from .job_handlers_semantic import make_semantic_index_refresh_handler
from .exam_generation_past_v3_production import (
    register_past_v3_production_dependencies,
)
from .ai import deepseek_api_call
from .ai_quality_metrics import (
    QualityMetricsError,
    load_or_create_installation_secret,
    task_hmac,
)
from .ai_telemetry import record_usage_best_effort


_AI_JOB_KINDS = frozenset(("analysis", "classification", "batch-mistakes", "exam-generation", "ocr"))


def _record_ai_job_outcome(app, job: dict) -> None:
    kind=str(job.get("kind") or "")
    if kind not in _AI_JOB_KINDS:
        return
    job_id = str(job.get("id") or "").strip()
    settings_service = app.extensions.get("ai_settings_service")
    if not job_id or settings_service is None:
        return
    try:
        secret = load_or_create_installation_secret(settings_service.keyring)
        correlation = {
            "task_hash": task_hmac(job_id, secret),
            "stage": "business_job",
        }
    except (QualityMetricsError, AttributeError):
        return
    error=job.get("error") if isinstance(job.get("error"),dict) else {}
    duration_ms=None
    try:
        started=datetime.fromisoformat(str(job.get("started_at") or ""))
        finished=datetime.fromisoformat(str(job.get("finished_at") or ""))
        duration_ms=max(0,int((finished-started).total_seconds()*1000))
    except (TypeError,ValueError):
        pass
    record_usage_best_effort(app, {
            "task":kind,
            "task_policy":"business_job",
            "event_scope":"business_job",
            "success":job.get("status")=="succeeded",
            "outcome":job.get("status"),
            "attempts":job.get("attempts"),
            "manual_retry_count":job.get("manual_retry_count"),
            "duration_ms":duration_ms,
            "error_code":error.get("code"),
            "error_class":error.get("category"),
            **correlation,
        })


def _bounded_error_message(value, limit: int = 2000) -> str:
    text = str(value or "任务执行失败")
    if len(text) <= limit:
        return text
    head = min(500, limit // 3)
    tail = limit - head - len("\n...\n")
    return text[:head] + "\n...\n" + text[-tail:]


def _safe_error(payload, status: int) -> tuple[str, str]:
    if isinstance(payload, dict):
        raw = payload.get("error")
        if isinstance(raw, dict):
            code = str(raw.get("code") or f"http_{status}")[:80]
            message = _bounded_error_message(raw.get("message"))
            return code, message
        if isinstance(raw, str):
            code = str(payload.get("code") or f"http_{status}")[:80]
            return code, _bounded_error_message(raw)
    return f"http_{status}", "任务执行失败，请检查输入或服务配置"


def _structured_error_details(payload) -> dict:
    """Extract user-action metadata without persisting arbitrary response fields."""

    if not isinstance(payload, Mapping):
        return {}
    nested = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    details: dict = {}
    hint = payload.get("hint", nested.get("hint"))
    if isinstance(hint, str) and hint.strip():
        details["hint"] = _bounded_error_message(hint, 500)
    actions = payload.get("actions", nested.get("actions"))
    if isinstance(actions, (list, tuple)):
        normalized_actions = [
            str(action).strip()[:80]
            for action in actions
            if str(action).strip()
        ][:20]
        if normalized_actions:
            details["actions"] = normalized_actions
    reason = payload.get("reason", nested.get("reason"))
    if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", reason):
        details["reason"] = reason
    raw_details = payload.get("details", nested.get("details"))
    if isinstance(raw_details, Mapping):
        details["details"] = dict(raw_details)
    profile = payload.get("profile", nested.get("profile"))
    if isinstance(profile, Mapping):
        # Profile metadata is safe to persist and lets the UI explain how to
        # repair a missing strong-model characterization without exposing the
        # original prompt or question material.
        details["profile"] = dict(profile)
    return details


def _job_error_from_response(payload, status: int):
    code, message = _safe_error(payload, status)
    details = _structured_error_details(payload)
    lowered = code.lower()
    nested = (
        payload.get("error")
        if isinstance(payload, Mapping)
        and isinstance(payload.get("error"), Mapping)
        else {}
    )
    raw_category = (
        payload.get("category", nested.get("category"))
        if isinstance(payload, Mapping)
        else None
    )
    declared_category = (
        raw_category.strip().casefold()
        if isinstance(raw_category, str)
        else None
    )
    if declared_category not in {
        "business",
        "configuration",
        "contract",
        "data",
        "dependency",
        "input",
        "provider",
        "storage",
    }:
        declared_category = None
    raw_retryable = (
        payload.get("retryable", nested.get("retryable"))
        if isinstance(payload, Mapping)
        else None
    )
    if any(marker in lowered for marker in ("config", "credential", "api_key", "feature_disabled")):
        return PermanentJobError(
            message, code=code, category="configuration", details=details
        )
    if status in {429, 502, 503, 504} and raw_retryable is not False:
        return RetryableJobError(
            message, code=code, delay_seconds=2, details=details
        )
    category = declared_category or (
        "input"
        if status in {400, 404, 409, 410, 413, 415, 422}
        else "business"
    )
    return PermanentJobError(message, code=code, category=category, details=details)


def register_app_job_handlers(app) -> None:
    """Register handlers bound to one configured Flask application."""

    register_past_v3_production_dependencies(app)

    def call_ai(messages, **kwargs):
        # Keep the existing runtime provider-injection point used by acceptance tools.
        return deepseek_api_call(messages, **kwargs)

    handlers = make_ai_handlers(
        app, call_ai=call_ai, job_error_from_response=_job_error_from_response,
    )
    handlers["exam-generation"] = make_exam_generation_handler(
        app, call_ai=call_ai, job_error_from_response=_job_error_from_response,
    )

    handlers["data-import"] = make_data_import_handler(
        app, job_error_from_response=_job_error_from_response
    )
    handlers.update(
        make_document_handlers(
            app, job_error_from_response=_job_error_from_response
        )
    )

    handlers.update(
        make_exam_domain_handlers(
            app, bounded_error_message=_bounded_error_message
        )
    )

    handlers["semantic-index-refresh"] = make_semantic_index_refresh_handler(app)

    handlers["memory-maintenance"] = make_memory_maintenance_handler(app)

    register_handler_map(handlers)


def start_job_worker(app):
    if not app.config.get("JOB_WORKER_ENABLED", True):
        return None
    existing = app.extensions.get("job_worker")
    if existing:
        return existing
    stop_event = threading.Event()
    threads = []
    for lane, count in (
        ("fast", int(app.config.get("JOB_FAST_WORKERS", 1))),
        ("heavy", int(app.config.get("JOB_HEAVY_WORKERS", 1))),
    ):
        for index in range(count):
            thread = threading.Thread(
                target=run_registered_worker,
                args=(app.config["DATABASE"], stop_event),
                kwargs={
                    "poll_interval": 0.25,
                    "lease_seconds": 30,
                    "staged_input_roots": (app.config["JOB_INPUT_DIR"],),
                    "lane": lane,
                    "aging_seconds": app.config.get("JOB_AGING_SECONDS", 300),
                    "outcome_observer": lambda job: _record_ai_job_outcome(app, job),
                },
                name=f"cuoti-job-{lane}-{index + 1}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    handle = {"threads": threads, "thread": threads[0], "stop_event": stop_event}
    app.extensions["job_worker"] = handle
    return handle


def stop_job_worker(app, *, timeout: float = 3.0) -> None:
    handle = app.extensions.get("job_worker")
    if not handle:
        return
    handle["stop_event"].set()
    threads = handle.get("threads") or [handle.get("thread")]
    deadline = time.monotonic() + timeout
    for thread in threads:
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
    alive = [thread.name for thread in threads if thread is not None and thread.is_alive()]
    if alive:
        raise RuntimeError(
            "后台任务 worker 未在期限内确认停止：" + ", ".join(alive)
        )
    app.extensions.pop("job_worker", None)
