"""Bounded, read-only runtime health checks with a stable public contract."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask

from ..database import connect_database
from .settings import SettingsService


CONTRACT_VERSION = "1"
DEFAULT_CACHE_SECONDS = 3.0
_CACHE_LOCK = threading.Lock()


def _check(
    check_id: str,
    status: str,
    message: str,
    *,
    affected_features: tuple[str, ...] = (),
    retryable: bool = False,
) -> dict:
    return {
        "check_id": check_id,
        "status": status,
        "affected_features": list(affected_features),
        "message": message,
        "retryable": retryable,
    }


def _database_check(app: Flask) -> dict:
    try:
        database = Path(str(app.config["DATABASE"])).resolve(strict=True)
        connection = connect_database(database, role="readonly", timeout=0.5)
        try:
            # Readiness must not scan the entire multi-gigabyte database.
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error, KeyError):
        return _check(
            "database",
            "fatal",
            "主数据库不可用",
            affected_features=("all_data",),
            retryable=True,
        )
    return _check("database", "ok", "主数据库连接正常")


def _worker_check(app: Flask) -> dict:
    if not app.config.get("JOB_WORKER_ENABLED", True):
        return _check("job_worker", "ok", "后台任务在当前运行模式中停用")
    handle = app.extensions.get("job_worker")
    threads = handle.get("threads") if isinstance(handle, dict) else None
    if not threads and isinstance(handle, dict) and handle.get("thread") is not None:
        threads = [handle["thread"]]
    if not threads or any(not thread.is_alive() for thread in threads):
        return _check(
            "job_worker",
            "degraded",
            "后台任务暂未就绪",
            affected_features=("background_jobs",),
            retryable=True,
        )
    return _check("job_worker", "ok", f"后台任务运行正常（{len(threads)} 个 lane worker）")


def _xelatex_check(app: Flask) -> dict:
    command = str(app.config.get("XELATEX_COMMAND") or "xelatex").strip()
    if shutil.which(command) or Path(command).is_file():
        return _check("xelatex", "ok", "PDF 编译组件可用")
    return _check(
        "xelatex",
        "degraded",
        "PDF 编译组件未配置或不可用",
        affected_features=("pdf_generation",),
    )


def _question_bank_check(app: Flask) -> dict:
    tex_base = str(app.config.get("TEX_BASE") or "").strip()
    if not tex_base or not Path(tex_base).is_dir():
        return _check(
            "question_bank",
            "degraded",
            "普通题库来源未配置或不可用",
            affected_features=("question_bank",),
        )
    index_value = (
        app.config.get("QUESTION_BANK_DATABASE")
        or app.config.get("QUESTION_BANK_INDEX")
    )
    if index_value and not Path(str(index_value)).is_file():
        return _check(
            "question_bank",
            "degraded",
            "普通题库索引尚未就绪",
            affected_features=("question_bank",),
            retryable=True,
        )
    return _check("question_bank", "ok", "普通题库来源可用")


def _ai_check(app: Flask) -> dict:
    try:
        key, _source = SettingsService(app).credential()
    except Exception:
        return _check(
            "ai_configuration",
            "degraded",
            "AI 配置状态暂时无法读取",
            affected_features=("ai_features",),
            retryable=True,
        )
    if str(key or "").strip():
        return _check("ai_configuration", "ok", "AI 凭据已配置")
    return _check(
        "ai_configuration",
        "degraded",
        "AI 凭据未配置",
        affected_features=("ai_features",),
    )


def _collect(app: Flask) -> dict:
    checks = [
        _database_check(app),
        _worker_check(app),
        _xelatex_check(app),
        _question_bank_check(app),
        _ai_check(app),
    ]
    statuses = {item["status"] for item in checks}
    overall = "fatal" if "fatal" in statuses else (
        "degraded" if "degraded" in statuses else "healthy"
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "overall_status": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


def collect_runtime_health(app: Flask, *, force: bool = False) -> dict:
    """Return a cached health payload without calling external services."""

    now = time.monotonic()
    cache_seconds = float(
        app.config.get("HEALTH_CACHE_SECONDS", DEFAULT_CACHE_SECONDS)
    )
    with _CACHE_LOCK:
        cached = app.extensions.get("runtime_health_cache")
        if (
            not force
            and isinstance(cached, dict)
            and now - float(cached.get("created_at", 0)) < cache_seconds
        ):
            return dict(cached["payload"])
        payload = _collect(app)
        app.extensions["runtime_health_cache"] = {
            "created_at": now,
            "payload": payload,
        }
        return dict(payload)
