"""Explicit, auditable repair for published past exams missing item snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Iterable

from ..transactions import begin_immediate
from .exam_snapshots import ExamSnapshotError, extract_exam_item_snapshots


def _config(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _expected_count(config: dict[str, Any]) -> int | None:
    try:
        count = int(config.get("question_count"))
    except (TypeError, ValueError):
        selected = config.get("selected_questions")
        count = len(selected) if isinstance(selected, list) else 0
    return count if count > 0 else None


def _selected_by_position(config: dict[str, Any]) -> dict[int, dict[str, Any]]:
    selected = config.get("selected_questions")
    if not isinstance(selected, list):
        return {}
    values: dict[int, dict[str, Any]] = {}
    for position, item in enumerate(selected, start=1):
        if isinstance(item, dict):
            values[position] = item
    return values


def _candidate(row: sqlite3.Row, attempt_count: int) -> dict[str, Any] | None:
    config = _config(row["config"])
    if config.get("source") != "past":
        return None
    expected = _expected_count(config)
    content = str(row["content"] or "")
    result: dict[str, Any] = {
        "exam_id": int(row["id"]),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "expected_count": expected,
        "attempt_count": int(attempt_count),
    }
    if attempt_count:
        result.update(status="blocked", reason="exam_has_attempts", snapshot_count=None)
        return result
    try:
        snapshots = extract_exam_item_snapshots(content)
    except ExamSnapshotError as exc:
        result.update(status="blocked", reason="snapshot_ambiguous", detail=str(exc))
        result["snapshot_count"] = None
        return result
    result["snapshot_count"] = len(snapshots)
    if not snapshots:
        result.update(status="blocked", reason="no_snapshots")
    elif expected is None:
        result.update(status="blocked", reason="expected_count_missing")
    elif expected != len(snapshots):
        result.update(status="blocked", reason="question_count_mismatch")
    else:
        result.update(status="ready", reason=None)
    return result


def inspect_missing_past_exam_items(
    db: sqlite3.Connection,
    *,
    exam_ids: Iterable[int] | None = None,
) -> list[dict[str, Any]]:
    """Return content-free repair decisions for all matching published exams."""

    requested = sorted({int(value) for value in exam_ids or () if int(value) > 0})
    sql = (
        "SELECT e.id,e.config,e.content,e.created_at,"
        "(SELECT COUNT(*) FROM exam_attempts a WHERE a.exam_id=e.id) AS attempt_count "
        "FROM exams e WHERE e.artifact_status='published' AND e.deleted_at IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=e.id)"
    )
    parameters: list[Any] = []
    if requested:
        placeholders = ",".join("?" for _ in requested)
        sql += f" AND e.id IN ({placeholders})"
        parameters.extend(requested)
    sql += " ORDER BY e.id"
    decisions: list[dict[str, Any]] = []
    for row in db.execute(sql, parameters).fetchall():
        decision = _candidate(row, int(row["attempt_count"] or 0))
        if decision is not None:
            decisions.append(decision)
    return decisions


def _insert_snapshots(
    db: sqlite3.Connection,
    *,
    row: sqlite3.Row,
    snapshots: list[str],
    repair_id: str,
) -> None:
    config = _config(row["config"])
    structured = _selected_by_position(config)
    created_at = str(row["created_at"] or "")
    for position, content in enumerate(snapshots, start=1):
        item = structured.get(position, {})
        candidate_id = item.get("candidate_id")
        metadata = {
            "backfilled_from": "exams.content",
            "repair_id": repair_id,
            "source_file": item.get("source_file"),
            "source_number": item.get("source_number"),
            "source_institution": item.get("source_institution"),
            "source_year": item.get("source_year"),
            "adapted": bool(item.get("adapted")),
        }
        db.execute(
            "INSERT INTO exam_items(public_id,exam_id,position,question_type,"
            "difficulty,content,content_hash,points,source_kind,"
            "source_candidate_id,source_metadata_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",
            (
                str(uuid.uuid4()), int(row["id"]), position,
                str(item.get("type") or ""), str(item.get("difficulty") or ""),
                content, hashlib.sha256(content.encode("utf-8")).hexdigest(),
                str(item.get("source_kind") or "question_bank"),
                str(candidate_id) if candidate_id else None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), created_at,
            ),
        )


def repair_missing_past_exam_items(
    db: sqlite3.Connection,
    *,
    apply: bool = False,
    exam_ids: Iterable[int] | None = None,
    repair_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """Inspect or repair matching exams, committing each exam independently."""

    repair_id = repair_id or f"exam-item-repair-{uuid.uuid4().hex}"
    timestamp = timestamp or datetime.now().astimezone().isoformat(timespec="seconds")
    decisions = inspect_missing_past_exam_items(db, exam_ids=exam_ids)
    results: list[dict[str, Any]] = []
    for decision in decisions:
        current = dict(decision)
        if not apply or decision["status"] != "ready":
            results.append(current)
            continue
        exam_id = int(decision["exam_id"])
        try:
            begin_immediate(db)
            row = db.execute(
                "SELECT id,config,content,created_at FROM exams "
                "WHERE id=? AND artifact_status='published' AND deleted_at IS NULL",
                (exam_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("exam_no_longer_published")
            if db.execute(
                "SELECT 1 FROM exam_items WHERE exam_id=? LIMIT 1", (exam_id,)
            ).fetchone():
                raise RuntimeError("exam_items_already_exist")
            if db.execute(
                "SELECT 1 FROM exam_attempts WHERE exam_id=? LIMIT 1", (exam_id,)
            ).fetchone():
                raise RuntimeError("exam_has_attempts")
            config = _config(row["config"])
            snapshots = extract_exam_item_snapshots(row["content"])
            expected = _expected_count(config)
            if expected is None or expected != len(snapshots):
                raise RuntimeError("question_count_changed")
            current_hash = hashlib.sha256(
                str(row["content"] or "").encode("utf-8")
            ).hexdigest()
            if current_hash != decision["content_sha256"]:
                raise RuntimeError("exam_content_changed")
            _insert_snapshots(
                db, row=row, snapshots=snapshots, repair_id=repair_id
            )
            db.commit()
            current.update(status="repaired", repaired_count=len(snapshots))
        except Exception as exc:
            db.rollback()
            current.update(status="failed", reason=str(exc)[:120])
        results.append(current)
    quick_check = db.execute("PRAGMA quick_check").fetchone()[0]
    foreign_key_violations = len(db.execute("PRAGMA foreign_key_check").fetchall())
    counts = {
        "ready": sum(item["status"] == "ready" for item in results),
        "blocked": sum(item["status"] == "blocked" for item in results),
        "repaired": sum(item["status"] == "repaired" for item in results),
        "failed": sum(item["status"] == "failed" for item in results),
    }
    return {
        "schema": "exam-item-repair-v1",
        "repair_id": repair_id,
        "created_at": timestamp,
        "mode": "apply" if apply else "dry-run",
        "counts": counts,
        "quick_check": quick_check,
        "foreign_key_violations": foreign_key_violations,
        "items": results,
    }
