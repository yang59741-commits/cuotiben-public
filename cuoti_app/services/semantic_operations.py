"""Read-only semantic-index operations overview for the G1 API."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .semantic_operations_contract import (
    DEFAULT_SEMANTIC_INDEX_SETTINGS,
    OPERATIONS_SCHEMA,
    SETTINGS_SCHEMA,
    CapabilityContext,
    derive_capabilities,
    validate_overview_response,
    validate_semantic_index_settings,
)
from .settings import get_settings_service


ACTIVE_SEMANTIC_STATUSES = ("scanning", "queued", "running")
ACTIVE_JOB_STATUSES = ("queued", "running", "retry_wait", "cancel_requested")
_PATH = re.compile(r"(?i)\b[a-z]:[\\/][^\r\n\t\"']+")
_SECRET = re.compile(r"(?i)(api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+")


def _table_exists(db: Any, table: str) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _row(db: Any, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    value = db.execute(sql, parameters).fetchone()
    return dict(value) if value is not None else None


def _safe_error(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    text = _PATH.sub("[本机路径已隐藏]", text)
    text = _SECRET.sub(lambda match: f"{match.group(1)}=[已隐藏]", text)
    return text[:500]


def _settings(settings_path: str | Path) -> tuple[int, dict[str, Any], str | None]:
    path = Path(settings_path)
    if not path.exists():
        return 0, dict(DEFAULT_SEMANTIC_INDEX_SETTINGS), None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("settings 顶层不是对象")
        revision = document.get("settings_revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("settings_revision 无效")
        raw = document.get("semantic_index")
        if raw is None:
            raw = dict(DEFAULT_SEMANTIC_INDEX_SETTINGS)
        return revision, validate_semantic_index_settings(raw), None
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return 0, dict(DEFAULT_SEMANTIC_INDEX_SETTINGS), _safe_error(exc)


def collect_semantic_operations_overview(app: Any, db: Any) -> dict[str, Any]:
    """Collect a bounded overview using SELECTs and local JSON reads only."""

    revision, semantic_settings, settings_error = _settings(app.config["SETTINGS_PATH"])
    try:
        runtime_settings = get_settings_service(app).semantic_runtime_settings(
            provider_enabled=bool(app.config.get("SEMANTIC_INDEX_PROVIDER_ENABLED"))
        )
        # The service is authoritative when the local settings file is valid;
        # retain the sanitized fallback above if it is corrupt.
        if settings_error is None:
            revision = runtime_settings["settings_revision"]
            semantic_settings = runtime_settings["requested"]
    except Exception as exc:
        runtime_settings = {
            "settings_revision": revision,
            "requested": semantic_settings,
            "activation": {"schema": "semantic-index-activation-v2", "provider": False, "auto_refresh": False, "allow_partial": False},
            "provider": {"requested": False, "effective": False, "managed": False, "source": "default", "credential_configured": False, "reason": "settings_invalid"},
            "effective": {"recall_mode": semantic_settings["recall_mode"], "candidate_multiplier": semantic_settings["candidate_multiplier"], "provider_enabled": False, "auto_refresh_enabled": False, "allow_partial_cache": False},
            "reasons": {"provider_enabled": "settings_invalid", "auto_refresh_enabled": "settings_invalid", "allow_partial_cache": "settings_invalid"},
        }
        if settings_error is None:
            settings_error = _safe_error(exc)
    has_runs = _table_exists(db, "semantic_index_runs")
    has_snapshots = _table_exists(db, "semantic_index_snapshots")
    has_projection = _table_exists(db, "semantic_recall_projection_versions")

    latest_run = None
    if has_runs:
        latest_run = _row(
            db,
            "SELECT id,status,run_kind,source_snapshot,analysis_schema_version,"
            "prompt_version,pending_count,completed_count,failed_count,"
            "requested_concurrency,effective_concurrency,token_consumed,"
            "token_reserved,token_consumed_unverified,token_stop_reason,"
            "degrade_reason,created_at,updated_at,finished_at,last_error_code,"
            "last_error_summary FROM semantic_index_runs "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
        )

    snapshot = None
    if has_snapshots:
        snapshot = _row(
            db,
            "SELECT id,source_snapshot,status,eligible_count,valid_card_count,"
            "stale_card_count,missing_card_count,coverage_ratio,"
            "analysis_schema_version,published_at FROM semantic_index_snapshots "
            "ORDER BY published_at DESC,id DESC LIMIT 1",
        )

    projection = None
    if has_projection:
        projection = _row(
            db,
            "SELECT id,snapshot_id,algorithm_version,dictionary_version,"
            "weights_version,status,item_count,published_at "
            "FROM semantic_recall_projection_versions "
            "ORDER BY CASE status WHEN 'published' THEN 0 ELSE 1 END,"
            "published_at DESC,created_at DESC,id DESC LIMIT 1",
        )

    semantic_active = False
    if has_runs:
        semantic_active = db.execute(
            "SELECT 1 FROM semantic_index_runs WHERE status IN (?,?,?) LIMIT 1",
            ACTIVE_SEMANTIC_STATUSES,
        ).fetchone() is not None
    exam_active = False
    semantic_durable_active = False
    if _table_exists(db, "background_jobs"):
        placeholders = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        exam_active = db.execute(
            f"SELECT 1 FROM background_jobs WHERE kind LIKE 'exam%' "
            f"AND status IN ({placeholders}) LIMIT 1",
            ACTIVE_JOB_STATUSES,
        ).fetchone() is not None
        semantic_durable_active = db.execute(
            f"SELECT 1 FROM background_jobs WHERE kind='semantic-index-refresh' "
            f"AND status IN ({placeholders}) LIMIT 1",
            ACTIVE_JOB_STATUSES,
        ).fetchone() is not None
    semantic_active = semantic_active or semantic_durable_active

    eligible = int((snapshot or {}).get("eligible_count") or 0)
    valid = int((snapshot or {}).get("valid_card_count") or 0)
    stale = int((snapshot or {}).get("stale_card_count") or 0)
    missing = int((snapshot or {}).get("missing_card_count") or 0)
    excluded = 0
    if _table_exists(db, "semantic_questions"):
        excluded = int(
            db.execute(
                "SELECT COUNT(*) FROM semantic_questions WHERE excluded=1 OR active=0"
            ).fetchone()[0]
        )

    run_status = str((latest_run or {}).get("status") or "") or None
    provider_enabled = bool(runtime_settings["effective"]["provider_enabled"])
    capabilities = derive_capabilities(
        CapabilityContext(
            run_status=run_status,
            semantic_job_active=semantic_active,
            exam_generation_active=exam_active,
            provider_enabled=provider_enabled,
            has_projection=projection is not None and projection.get("status") == "published",
            cleanup_preview_ready=False,
            implementation_enabled=True,
        )
    )

    last_error = settings_error
    if latest_run and latest_run.get("last_error_summary"):
        last_error = _safe_error(latest_run["last_error_summary"])
    public_run = None
    if latest_run:
        public_run = {
            key: latest_run.get(key)
            for key in (
                "id", "status", "run_kind", "source_snapshot",
                "analysis_schema_version", "prompt_version", "pending_count",
                "completed_count", "failed_count", "created_at", "updated_at",
                "finished_at", "last_error_code",
            )
        }

    payload = {
        "schema": OPERATIONS_SCHEMA,
        "settings_revision": revision,
        "settings": semantic_settings,
        "settings_state": {
            "requested": runtime_settings["requested"],
            "effective": runtime_settings["effective"],
            "activation": runtime_settings["activation"],
            "provider": runtime_settings["provider"],
            "reasons": runtime_settings["reasons"],
        },
        "source_snapshot": {
            "id": (latest_run or {}).get("source_snapshot") or (snapshot or {}).get("source_snapshot"),
            "created_at": (latest_run or {}).get("created_at"),
        },
        "semantic_snapshot": {
            "id": (snapshot or {}).get("id"),
            "status": (snapshot or {}).get("status") or "missing",
            "published_at": (snapshot or {}).get("published_at"),
            "analysis_schema_version": (snapshot or {}).get("analysis_schema_version"),
        },
        "projection": {
            "id": (projection or {}).get("id"),
            "schema": (projection or {}).get("algorithm_version"),
            "version": (projection or {}).get("weights_version"),
            "dictionary_version": (projection or {}).get("dictionary_version"),
            "status": (projection or {}).get("status") or "missing",
            "item_count": int((projection or {}).get("item_count") or 0),
            "published_at": (projection or {}).get("published_at"),
        },
        "counts": {
            "eligible": eligible,
            "valid": valid,
            "stale": stale,
            "missing": missing,
            "excluded": excluded,
            "completed": int((latest_run or {}).get("completed_count") or 0),
            "failed": int((latest_run or {}).get("failed_count") or 0),
            "retry_pending": int((latest_run or {}).get("pending_count") or 0),
            "abandoned": 0,
        },
        "coverage": {
            "ratio": float((snapshot or {}).get("coverage_ratio") or 0.0),
            "complete": (snapshot or {}).get("status") == "complete",
        },
        "latest_run": public_run,
        "job_state": {
            "semantic_active": semantic_active,
            "exam_generation_active": exam_active,
        },
        "concurrency": {
            "configured": semantic_settings["max_concurrency"],
            "requested": int((latest_run or {}).get("requested_concurrency") or 0),
            "effective": int((latest_run or {}).get("effective_concurrency") or 0),
            "active": int(semantic_active),
        },
        "tokens": {
            "consumed": int((latest_run or {}).get("token_consumed") or 0),
            "reserved": int((latest_run or {}).get("token_reserved") or 0),
            "unverified": int((latest_run or {}).get("token_consumed_unverified") or 0),
        },
        "stop": {
            "paused": bool(run_status and run_status.startswith("paused")),
            "degraded": bool((latest_run or {}).get("degrade_reason")),
            "reason": (latest_run or {}).get("token_stop_reason")
            or (latest_run or {}).get("degrade_reason"),
        },
        "last_error": (
            {"code": (latest_run or {}).get("last_error_code") or "settings_invalid", "message": last_error}
            if last_error
            else None
        ),
        "capabilities": capabilities,
    }
    return validate_overview_response(payload)
