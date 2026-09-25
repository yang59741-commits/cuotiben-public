"""Sanitized status and durable batch-boundary controls for semantic runs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..transactions import immediate_transaction
from .semantic_index import assert_run_item_transition, assert_run_transition
from .semantic_index_refresh import SemanticAnalysisProfile


class SemanticIndexControlError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _run_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 256:
        raise SemanticIndexControlError("VALIDATION", "run_id 无效")
    return value.strip()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _run(db: Any, run_id: str) -> Any:
    row = db.execute(
        "SELECT * FROM semantic_index_runs WHERE id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise SemanticIndexControlError("RUN_NOT_FOUND", "semantic run 不存在")
    return row


def semantic_run_status(db: Any, *, run_id: str) -> dict[str, Any]:
    """Return operational state without source text or Provider payloads."""

    run_id = _run_id(run_id)
    row = _run(db, run_id)
    unresolved = int(
        db.execute(
            "SELECT COUNT(*) FROM semantic_index_provider_attempts WHERE run_id=? "
            "AND status='outcome_unknown'",
            (run_id,),
        ).fetchone()[0]
    )
    return {
        "run_id": run_id,
        "run_kind": str(row["run_kind"]),
        "status": str(row["status"]),
        "counts": {
            "pending": int(row["pending_count"]),
            "completed": int(row["completed_count"]),
            "failed": int(row["failed_count"]),
            "new": int(row["new_count"]),
            "changed": int(row["changed_count"]),
            "stale": int(row["stale_count"]),
            "reused": int(row["reused_count"]),
        },
        "concurrency": {
            "requested": int(row["requested_concurrency"]),
            "effective": int(row["effective_concurrency"]),
        },
        "tokens": {
            "initial_limit": int(row["token_initial_limit"]),
            "current_limit": int(row["token_current_limit"]),
            "hard_limit": int(row["token_hard_limit"]),
            "consumed": int(row["token_consumed"]),
            "reserved": int(row["token_reserved"]),
            "consumed_unverified": int(row["token_consumed_unverified"]),
            "remaining": int(row["token_remaining"]),
            "extension_index": int(row["token_extension_index"]),
        },
        "control": {
            "requested": row["control_requested"],
            "requested_at": row["control_requested_at"],
        },
        "stop_reason": row["token_stop_reason"],
        "last_error_code": row["last_error_code"],
        "outcome_unknown_count": unresolved,
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def semantic_token_events(
    db: Any, *, run_id: str, limit: int = 100
) -> dict[str, Any]:
    run_id = _run_id(run_id)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise SemanticIndexControlError("VALIDATION", "limit 必须在 1 到 200 之间")
    _run(db, run_id)
    rows = db.execute(
        "SELECT id,event_type,extension_index,previous_limit,nominal_amount,"
        "actual_amount,new_limit,token_delta_consumed,token_delta_reserved,"
        "token_delta_unverified,consumed_after,reserved_after,unverified_after,"
        "current_limit_after,created_at FROM semantic_index_token_events "
        "WHERE run_id=? ORDER BY id DESC LIMIT ?",
        (run_id, limit),
    ).fetchall()
    return {
        "run_id": run_id,
        "events": [
            {key: row[key] for key in row.keys()}
            for row in rows
        ],
    }


def request_semantic_run_control(
    db: Any, *, run_id: str, action: str
) -> dict[str, Any]:
    run_id = _run_id(run_id)
    if action not in {"pause", "cancel"}:
        raise SemanticIndexControlError("VALIDATION", "控制操作无效")
    timestamp = _timestamp()
    with immediate_transaction(db):
        row = _run(db, run_id)
        status = str(row["status"])
        if status in {"succeeded", "failed", "cancelled"}:
            if status == "cancelled" and action == "cancel":
                return semantic_run_status(db, run_id=run_id)
            raise SemanticIndexControlError("RUN_TERMINAL", "终态 run 不能接受控制操作")
        if action == "pause":
            if status not in {"paused", "paused_budget_review", "paused_token_hard_limit"}:
                db.execute(
                    "UPDATE semantic_index_runs SET control_requested='pause',"
                    "control_requested_at=?,updated_at=? WHERE id=?",
                    (timestamp, timestamp, run_id),
                )
            return semantic_run_status(db, run_id=run_id)
        if status == "running":
            db.execute(
                "UPDATE semantic_index_runs SET control_requested='cancel',"
                "control_requested_at=?,updated_at=? WHERE id=?",
                (timestamp, timestamp, run_id),
            )
            return semantic_run_status(db, run_id=run_id)
        if int(row["token_reserved"]):
            raise SemanticIndexControlError(
                "TOKEN_REQUESTS_IN_FLIGHT", "存在在途 Token 预留，不能立即取消"
            )
        assert_run_transition(status, "cancelled")
        for item in db.execute(
            "SELECT status FROM semantic_index_run_items WHERE run_id=? AND status IN "
            "('pending','leased','retry_wait')",
            (run_id,),
        ).fetchall():
            assert_run_item_transition(str(item["status"]), "cancelled")
        db.execute(
            "UPDATE semantic_index_run_items SET status='cancelled',lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE run_id=? AND status IN "
            "('pending','leased','retry_wait')",
            (timestamp, run_id),
        )
        db.execute(
            "UPDATE semantic_index_runs SET status='cancelled',control_requested=NULL,"
            "control_requested_at=NULL,updated_at=?,finished_at=? WHERE id=?",
            (timestamp, timestamp, run_id),
        )
        return semantic_run_status(db, run_id=run_id)


def semantic_resume_payload(
    db: Any, *, run_id: str, profile: SemanticAnalysisProfile
) -> dict[str, str]:
    run_id = _run_id(run_id)
    with immediate_transaction(db):
        row = _run(db, run_id)
        if str(row["status"]) != "paused":
            raise SemanticIndexControlError(
                "RUN_NOT_RESUMABLE", "只有普通 paused run 可以通过此接口恢复"
            )
        identity = (
            int(row["analysis_schema_version"]),
            str(row["prompt_version"]),
            str(row["model_role"]),
            str(row["model_name"]),
            str(row["reasoning_effort"]),
        )
        expected = (
            profile.analysis_schema_version,
            profile.prompt_version,
            profile.model_role,
            profile.model_name,
            profile.reasoning_effort,
        )
        if identity != expected:
            raise SemanticIndexControlError(
                "ANALYSIS_PROFILE_CHANGED", "当前分析配置与冻结 run 不一致"
            )
        db.execute(
            "UPDATE semantic_index_runs SET control_requested=NULL,"
            "control_requested_at=NULL WHERE id=?",
            (run_id,),
        )
        return {
            "operation": "resume",
            "run_kind": str(row["run_kind"]),
            "expected_source_snapshot": str(row["source_snapshot"]),
            "analysis_profile_id": profile.profile_id,
            "analysis_fingerprint": profile.fingerprint,
        }
