"""Database-row serialization for the public durable-job contract."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .exam_generation_request_binding import verify_persisted_input


def publication_head_state(
    db: sqlite3.Connection, job_id: str
) -> str | None:
    """Return committed/request-frozen state from either native namespace."""

    authorities = (
        (
            "exam_generation_v3_publish_heads",
            {"request_frozen", "published_pending_receipt", "completed"},
        ),
        (
            "exam_generation_d2_quality_publications",
            {"request_frozen", "published_pending_receipt", "completed"},
        ),
    )
    try:
        existing = {
            str(row["name"])
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)",
                tuple(table for table, _states in authorities),
            ).fetchall()
        }
        found: list[str] = []
        for table, states in authorities:
            if table not in existing:
                continue
            row = db.execute(
                f"SELECT state FROM {table} WHERE job_id=?", (job_id,)
            ).fetchone()
            state = str(row["state"]) if row is not None else None
            if state in states:
                found.append(state)
        memory_table = "exam_generation_memory_event_outbox_v1"
        memory_head = "exam_generation_memory_event_delivery_heads_v1"
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (memory_table,)).fetchone():
            delivery = db.execute(
                f"SELECT h.state FROM {memory_table} o JOIN {memory_head} h "
                "ON h.operation_id=o.operation_id WHERE o.operation_id=?", (job_id,),
            ).fetchone()
            if delivery is not None:
                if delivery["state"] == "pending":
                    return "published_pending_receipt"
                if delivery["state"] == "delivered" and not found:
                    return "completed"
        for state in ("completed", "published_pending_receipt", "request_frozen"):
            if state in found:
                return state
    except sqlite3.DatabaseError:
        return None
    return None


def deserialize_job(
    db: sqlite3.Connection,
    row: sqlite3.Row,
    exam_ids: list[int] | None = None,
) -> dict[str, Any]:
    result = json.loads(row["result_json"]) if row["result_json"] else None
    error_details: dict[str, Any] = {}
    # Keep compatibility with small read-only schemas used by maintenance
    # tools that predate the optional structured error column.
    if "error_details_json" in row.keys() and row["error_details_json"]:
        try:
            parsed_details = json.loads(row["error_details_json"])
            if isinstance(parsed_details, dict):
                error_details = parsed_details
        except (TypeError, ValueError, json.JSONDecodeError):
            error_details = {}
    if exam_ids is None:
        exam_ids = [
            item["exam_id"]
            for item in db.execute(
                "SELECT exam_id FROM job_exam_results WHERE job_id=? ORDER BY position",
                (row["id"],),
            ).fetchall()
        ]
    error = (
        {
            "code": row["error_code"],
            "message": row["error_message"],
            "category": row["error_category"] or "internal",
            "retryable": bool(row["error_retryable"]),
            "correlation_id": row["error_correlation_id"],
        }
        if row["error_code"]
        else None
    )
    if error is not None:
        # Only these public metadata keys may be overlaid; core error identity
        # remains sourced from the durable columns above.
        for key in ("hint", "actions", "details", "profile"):
            if key in error_details:
                error[key] = error_details[key]
        reason = error_details.get("reason")
        if isinstance(reason, str) and re.fullmatch(
            r"[A-Za-z0-9_.:-]{1,120}", reason
        ):
            error["reason"] = reason
    time_policy = None
    row_keys = set(row.keys())
    if "deadline_policy_mode" in row_keys and row["deadline_policy_mode"]:
        policy_payload: dict[str, Any] = {}
        try:
            input_row = db.execute(
                "SELECT payload_json,payload_hash,identity_version "
                "FROM job_inputs WHERE job_id=?",
                (row["id"],),
            ).fetchone()
            frozen_input = (
                verify_persisted_input(
                    input_row["payload_json"],
                    input_row["payload_hash"],
                    identity_version=input_row["identity_version"],
                    job_kind=row["kind"],
                    job_status=row["status"],
                    allow_legacy_exam_read=True,
                )
                if input_row
                else {}
            )
            raw_policy = (
                frozen_input.get("exam_time_policy")
                if isinstance(frozen_input, dict)
                else None
            )
            if isinstance(raw_policy, dict):
                policy_payload = dict(raw_policy)
        except (TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError):
            policy_payload = {}
        time_policy = {
            **policy_payload,
            "mode": row["deadline_policy_mode"],
            "version": row["deadline_policy_version"],
            "deadline_at": row["deadline_at"],
        }
    timing = None
    if "exam_timing_json" in row_keys and row["exam_timing_json"]:
        try:
            parsed_timing = json.loads(row["exam_timing_json"])
            if isinstance(parsed_timing, dict) and parsed_timing:
                timing = parsed_timing
        except (TypeError, ValueError, json.JSONDecodeError):
            timing = None
    if isinstance(result, dict) and isinstance(result.get("timing"), dict):
        timing = result["timing"]
    elif (
        isinstance(error, dict)
        and isinstance(error.get("details"), dict)
        and isinstance(error["details"].get("timing"), dict)
    ):
        timing = error["details"]["timing"]
    publication_state = publication_head_state(db, str(row["id"]))
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "lane": row["lane"],
        "priority": row["priority"],
        "attempts": row["attempts"],
        "attempts_in_cycle": row["attempts_in_cycle"],
        "manual_retry_count": row["manual_retry_count"],
        "max_attempts": row["max_attempts"],
        "progress": {
            "current": row["progress_current"],
            "total": row["progress_total"],
            "message": row["progress_message"],
        },
        "result": result,
        "exam_ids": exam_ids,
        "error": error,
        "cancel_requested": bool(row["cancel_requested"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "time_policy": time_policy,
        "timing": timing,
        "publication_state": publication_state,
    }


__all__ = ["deserialize_job", "publication_head_state"]
