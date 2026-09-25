"""Transaction-owned mistake write use cases."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

from ..transactions import immediate_transaction
from ..validation import mistake_content_fingerprint, mistake_fingerprint
from .review_schedule import (
    SCHEDULE_VERSION,
    ASSISTED_SCHEDULE_VERSION,
    advance_review,
    initial_review_date,
    reopen_review,
)
from .learning_evidence import assistance_value, evidence


class MistakeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


def _mistake_latest_snapshot(row) -> dict | None:
    """Return the small, safe snapshot used to resolve a revision conflict."""

    if not row:
        return None
    keys = (
        "public_id",
        "revision",
        "status",
        "reasons",
        "note",
        "deleted_at",
        "next_review",
        "review_stage",
        "review_cycle",
        "updated_at",
    )
    snapshot = {key: row[key] for key in keys if key in row.keys()}
    snapshot["reasons"] = [
        item.strip()
        for item in str(snapshot.get("reasons") or "").split(",")
        if item.strip()
    ]
    return snapshot


def revision_conflict_details(
    db,
    public_id: str,
    *,
    operation: str,
    expected_revision: int | None = None,
    conflict_fields: tuple[str, ...] | list[str] = (),
) -> dict:
    """Build a consistent, actionable revision-conflict payload.

    This is intentionally read-only and can be called after a failed CAS.  A
    missing row is represented explicitly so the UI can refresh its list
    instead of offering a retry against a resource that no longer exists.
    """

    row = db.execute(
        "SELECT public_id,revision,status,reasons,note,deleted_at,next_review,"
        "review_stage,review_cycle,updated_at FROM mistakes WHERE public_id=?",
        (public_id,),
    ).fetchone()
    latest = _mistake_latest_snapshot(row)
    if latest is None:
        return {
            "operation": operation,
            "expected_revision": expected_revision,
            "current_revision": None,
            "latest": None,
            "resource_exists": False,
            "conflict_fields": list(conflict_fields),
            "actions": ["refresh_list"],
            "retryable": False,
        }
    return {
        "operation": operation,
        "expected_revision": expected_revision,
        "current_revision": int(latest.get("revision") or 0),
        "latest": latest,
        "resource_exists": True,
        "conflict_fields": list(conflict_fields),
        "actions": ["load_latest", "keep_draft", "retry"],
        "retryable": False,
    }


def sync_reason_items(db, mistake_id: int, reasons: str) -> None:
    """Synchronize normalized reasons without owning the caller transaction."""

    values = [item.strip() for item in (reasons or "").split(",") if item.strip()]
    db.execute("DELETE FROM mistake_reason_items WHERE mistake_id=?", (mistake_id,))
    db.executemany(
        "INSERT INTO mistake_reason_items(mistake_id,reason_code) VALUES(?,?)",
        [(mistake_id, reason) for reason in values],
    )


def create_mistake(
    db,
    *,
    public_id: str,
    subject,
    chapter,
    qnum: str,
    content: str,
    reasons: str,
    note: str,
    date_added: str,
    source_kind: str,
    source_ref: str,
    difficulty: str,
    timestamp: str,
):
    """Insert one validated mistake and return its joined read model."""

    fingerprint = mistake_fingerprint(
        subject["name"], chapter["name"] if chapter else "", qnum, content, date_added
    )
    with db:
        cursor = db.execute(
            "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,reasons,"
            "note,date_added,next_review,fingerprint,content_fingerprint,updated_at,"
            "source_kind,source_ref,difficulty,history_complete) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
            (
                public_id,
                subject["id"],
                chapter["id"] if chapter else None,
                qnum,
                content,
                reasons,
                note,
                date_added,
                initial_review_date(date_added),
                fingerprint,
                mistake_content_fingerprint(qnum, content),
                timestamp,
                source_kind,
                source_ref or None,
                difficulty or None,
            ),
        )
        sync_reason_items(db, cursor.lastrowid, reasons)
    return db.execute(
        "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
        "LEFT JOIN subjects s ON s.id=m.subject_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id WHERE m.id=?",
        (cursor.lastrowid,),
    ).fetchone()


def _chapter_assignment(db, subject_id: int, chapter_id: int | None):
    """Resolve a chapter while keeping a mistake's subject immutable."""

    if chapter_id is None:
        return None
    chapter = db.execute(
        "SELECT id,name,subject_id FROM chapters WHERE id=?", (chapter_id,)
    ).fetchone()
    if not chapter:
        raise MistakeError(
            "VALIDATION", "章节不存在", status_code=400
        )
    if int(chapter["subject_id"]) != int(subject_id):
        raise MistakeError(
            "VALIDATION", "所选章节不属于当前科目", status_code=400
        )
    return chapter


def _chapter_update_row(db, *, public_id: str | None = None, mistake_id: int | None = None):
    if public_id is None and mistake_id is None:
        raise ValueError("public_id or mistake_id is required")
    where = "m.public_id=?" if public_id is not None else "m.id=?"
    value = public_id if public_id is not None else mistake_id
    return db.execute(
        "SELECT m.id,m.public_id,m.revision,m.subject_id,m.chapter_id,m.qnum,"
        "m.content,m.date_added,m.deleted_at,s.name AS subject "
        "FROM mistakes m JOIN subjects s ON s.id=m.subject_id "
        f"WHERE {where}",
        (value,),
    ).fetchone()


def _apply_chapter_update(db, row, chapter_id: int | None, timestamp: str, *, revision=None) -> dict:
    """Apply a validated chapter change inside the caller's transaction."""

    chapter = _chapter_assignment(db, row["subject_id"], chapter_id)
    resolved_id = int(chapter["id"]) if chapter else None
    chapter_name = str(chapter["name"]) if chapter else ""
    if row["chapter_id"] == resolved_id:
        return {
            "ok": True,
            "changed": False,
            "public_id": row["public_id"],
            "revision": int(row["revision"]),
            "subject_id": int(row["subject_id"]),
            "chapter_id": resolved_id,
            "chapter": chapter_name or None,
        }
    fingerprint = mistake_fingerprint(
        row["subject"], chapter_name, row["qnum"], row["content"], row["date_added"]
    )
    where = "id=?"
    parameters = [resolved_id, fingerprint, timestamp, row["id"]]
    if revision is not None:
        where += " AND revision=?"
        parameters.append(revision)
    changed = db.execute(
        "UPDATE mistakes SET chapter_id=?,fingerprint=?,revision=revision+1,updated_at=? "
        f"WHERE {where} AND deleted_at IS NULL",
        parameters,
    )
    if changed.rowcount != 1:
        raise MistakeError(
            "REVISION_CONFLICT",
            "错题已被其他操作更新",
            status_code=409,
            details={"conflict_fields": ["chapter_id"]},
        )
    return {
        "ok": True,
        "changed": True,
        "public_id": row["public_id"],
        "revision": int(row["revision"]) + 1,
        "subject_id": int(row["subject_id"]),
        "chapter_id": resolved_id,
        "chapter": chapter_name or None,
    }


def update_mistake_chapter(
    db,
    public_id: str,
    *,
    revision: int,
    chapter_id: int | None,
    timestamp: str,
) -> dict:
    """Change one active mistake's chapter under revision protection."""

    with immediate_transaction(db):
        row = _chapter_update_row(db, public_id=public_id)
        if not row or row["deleted_at"] is not None:
            raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
        if int(row["revision"]) != revision:
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
                details={
                    "current_revision": int(row["revision"]),
                    "conflict_fields": ["chapter_id"],
                },
            )
        return _apply_chapter_update(
            db, row, chapter_id, timestamp, revision=revision
        )


def move_mistake_chapter_in_transaction(
    db, mistake_id: int, chapter_id: int, *, timestamp: str
) -> dict:
    """Move one mistake for an outer transaction such as a bulk operation."""

    row = _chapter_update_row(db, mistake_id=mistake_id)
    if not row or row["deleted_at"] is not None:
        raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
    return _apply_chapter_update(db, row, chapter_id, timestamp)


def review_mistake(
    db,
    public_id: str,
    *,
    client_event_id: str,
    revision: int,
    result: str,
    now: datetime,
    assistance: str = 'unknown',
) -> dict:
    """Apply one revisioned review event or replay its idempotent result."""

    assistance = assistance_value(assistance)
    event_type = 'assisted' if result == 'pass' and assistance == 'hint' else result
    if result not in ('pass', 'again'):
        raise MistakeError('VALIDATION', '复习结果无效', status_code=400)
    version = ASSISTED_SCHEDULE_VERSION if event_type == 'assisted' else SCHEDULE_VERSION
    try:
        with immediate_transaction(db):
            duplicate = db.execute(
                "SELECT e.*,m.public_id AS mistake_public_id FROM review_events e "
                "JOIN mistakes m ON m.id=e.mistake_id WHERE e.client_event_id=?",
                (client_event_id,),
            ).fetchone()
            if duplicate:
                if duplicate["mistake_public_id"] != public_id:
                    raise MistakeError(
                        "EVENT_CONFLICT",
                        "客户端事件标识已用于其他错题",
                        status_code=409,
                    )
                if duplicate['event_type'] != event_type or duplicate['assistance'] != assistance:
                    raise MistakeError('EVENT_CONFLICT', '同一事件不能提交不同结果', status_code=409)
                return {
                    "ok": True,
                    "replayed": True,
                    "event_public_id": duplicate["public_id"],
                    "status": duplicate["status_after"],
                    "correct_streak": duplicate["correct_streak"],
                    "lapse_count": duplicate["lapse_count_after"],
                    "review_stage": duplicate["stage_after"],
                    "review_cycle": duplicate["cycle_after"],
                    "next_review": duplicate["next_review_after"],
                    "assistance": duplicate['assistance'],
                }
            row = db.execute(
                "SELECT * FROM mistakes WHERE public_id=? AND deleted_at IS NULL",
                (public_id,),
            ).fetchone()
            if not row:
                raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
            if row["revision"] != revision:
                raise MistakeError(
                    "REVISION_CONFLICT",
                    "错题已被其他操作更新",
                    status_code=409,
                    details={"current_revision": row["revision"]},
                )
            today = now.strftime("%Y-%m-%d")
            review_count = (row["review_count"] or 0) + 1
            lapse_count = row["lapse_count"] or 0
            transition = advance_review(
                result=event_type,
                review_stage=row["review_stage"],
                review_cycle=row["review_cycle"],
                reviewed_on=today,
                correct_streak=row['correct_streak'],
            )
            if result == "again":
                lapse_count += 1
            timestamp = now.isoformat(timespec="seconds")
            updated = db.execute(
                "UPDATE mistakes SET review_count=?,correct_streak=?,lapse_count=?,"
                "last_review=?,next_review=?,status=?,review_stage=?,review_cycle=?,"
                "schedule_version=?,revision=revision+1,updated_at=? "
                "WHERE id=? AND revision=?",
                (
                    review_count,
                    transition.correct_streak,
                    lapse_count,
                    today,
                    transition.next_review,
                    transition.status,
                    transition.review_stage,
                    transition.review_cycle,
                    version,
                    timestamp,
                    row["id"],
                    revision,
                ),
            )
            if updated.rowcount != 1:
                raise MistakeError(
                    "REVISION_CONFLICT",
                    "错题已被其他操作更新",
                    status_code=409,
                )
            event_public_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO review_events(public_id,client_event_id,mistake_id,"
                "event_type,reviewed_at,created_at,status_after,correct_streak,"
                "lapse_count_after,stage_before,stage_after,cycle_after,"
                "next_review_after,schedule_version,assistance,evidence_version,reviewed_on,content_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_public_id,
                    client_event_id,
                    row["id"],
                    event_type,
                    timestamp,
                    timestamp,
                    transition.status,
                    transition.correct_streak,
                    lapse_count,
                    row["review_stage"],
                    transition.review_stage,
                    transition.review_cycle,
                    transition.next_review,
                    version,
                    *evidence(row['content'], assistance, now).values(),
                ),
            )
    except sqlite3.IntegrityError as exc:
        raise MistakeError(
            "EVENT_CONFLICT", "复习事件冲突，请重试", status_code=409
        ) from exc
    return {
        "ok": True,
        "replayed": False,
        "event_public_id": event_public_id,
        "public_id": public_id,
        "revision": revision + 1,
        "status": transition.status,
        "correct_streak": transition.correct_streak,
        "lapse_count": lapse_count,
        "review_stage": transition.review_stage,
        "review_cycle": transition.review_cycle,
        "next_review": transition.next_review,
        "assistance": assistance,
    }


def toggle_mistake(
    db,
    public_id: str,
    *,
    revision: int,
    now: datetime,
    reopen_only: bool = False,
    client_event_id: str | None = None,
) -> dict:
    """Manually master or reopen one mistake with a matching audit event."""

    with immediate_transaction(db):
        if client_event_id:
            previous = db.execute('SELECT e.*,m.public_id AS mistake_public_id FROM review_events e JOIN mistakes m ON m.id=e.mistake_id WHERE e.client_event_id=?', (client_event_id,)).fetchone()
            if previous:
                if previous['mistake_public_id'] != public_id or previous['event_type'] != 'manual_reopen':
                    raise MistakeError('EVENT_CONFLICT', '事件标识已用于其他操作', status_code=409)
                return {'ok': True, 'replayed': True, 'public_id': public_id, 'status': previous['status_after'], 'next_review': previous['next_review_after']}
        row = db.execute(
            "SELECT * FROM mistakes WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
        if reopen_only and row['status'] == '未掌握':
            return {'ok': True, 'replayed': True, 'public_id': public_id, 'status': row['status'], 'revision': row['revision'], 'next_review': row['next_review']}
        if row["revision"] != revision:
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
                details={"current_revision": row["revision"]},
            )
        today = now.strftime("%Y-%m-%d")
        if row["status"] == "未掌握":
            status = "已掌握"
            streak = row["correct_streak"] or 0
            stage = row["review_stage"] or 0
            cycle = row["review_cycle"] or 1
            next_review = None
            event_type = "manual_master"
        else:
            transition = reopen_review(
                review_cycle=row["review_cycle"], reopened_on=today
            )
            status = transition.status
            streak = transition.correct_streak
            stage = transition.review_stage
            cycle = transition.review_cycle
            next_review = transition.next_review
            event_type = "manual_reopen"
        timestamp = now.isoformat(timespec="seconds")
        changed = db.execute(
            "UPDATE mistakes SET status=?,correct_streak=?,next_review=?,review_stage=?,"
            "review_cycle=?,schedule_version=?,revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=?",
            (
                status,
                streak,
                next_review,
                stage,
                cycle,
                SCHEDULE_VERSION,
                timestamp,
                row["id"],
                revision,
            ),
        )
        if changed.rowcount != 1:
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
            )
        event_public_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO review_events(public_id,mistake_id,event_type,reviewed_at,"
            "created_at,status_after,correct_streak,lapse_count_after,stage_before,"
            "stage_after,cycle_after,next_review_after,schedule_version,client_event_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_public_id,
                row["id"],
                event_type,
                timestamp,
                timestamp,
                status,
                streak,
                row["lapse_count"] or 0,
                row["review_stage"],
                stage,
                cycle,
                next_review,
                SCHEDULE_VERSION,
                client_event_id,
            ),
        )
    return {
        "ok": True,
        "public_id": public_id,
        "revision": revision + 1,
        "event_public_id": event_public_id,
        "event_type": event_type,
        "status": status,
        "correct_streak": streak,
        "review_stage": stage,
        "review_cycle": cycle,
        "next_review": next_review,
    }


def update_mistake_note(
    db,
    public_id: str,
    *,
    revision: int,
    note: str,
    timestamp: str,
) -> dict:
    """Update one note with revision conflict details in an owned transaction."""

    with immediate_transaction(db):
        row = db.execute(
            "SELECT revision,note FROM mistakes "
            "WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
        changed = db.execute(
            "UPDATE mistakes SET note=?,revision=revision+1,updated_at=? "
            "WHERE public_id=? AND revision=? AND deleted_at IS NULL",
            (note, timestamp, public_id, revision),
        )
        if changed.rowcount != 1:
            latest = db.execute(
                "SELECT revision,note FROM mistakes "
                "WHERE public_id=? AND deleted_at IS NULL",
                (public_id,),
            ).fetchone()
            if not latest:
                raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
            raise MistakeError(
                "REVISION_CONFLICT",
                "此题已在其他页面更新",
                status_code=409,
                details={
                    "current_revision": latest["revision"],
                    "current_note": latest["note"] or "",
                },
            )
    return {
        "ok": True,
        "public_id": public_id,
        "revision": revision + 1,
        "note": note,
    }


def update_mistake_reasons(
    db,
    public_id: str,
    *,
    revision: int,
    reasons: str,
    timestamp: str,
) -> dict:
    """Update denormalized and normalized reasons under one revision CAS."""

    with immediate_transaction(db):
        row = db.execute(
            "SELECT id,revision FROM mistakes "
            "WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
        if row["revision"] != revision:
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
                details={"current_revision": row["revision"]},
            )
        changed = db.execute(
            "UPDATE mistakes SET reasons=?,revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=?",
            (reasons, timestamp, row["id"], revision),
        )
        if changed.rowcount != 1:
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
            )
        sync_reason_items(db, row["id"], reasons)
    return {
        "ok": True,
        "public_id": public_id,
        "revision": revision + 1,
        "reasons": [
            item.strip() for item in (reasons or "").split(",") if item.strip()
        ],
    }


def soft_delete_mistake(
    db,
    public_id: str,
    *,
    revision: int,
    timestamp: str,
) -> dict:
    """Move one active mistake to trash under a revision CAS."""

    with immediate_transaction(db):
        changed = db.execute(
            "UPDATE mistakes SET deleted_at=?,revision=revision+1,updated_at=? "
            "WHERE public_id=? AND revision=? AND deleted_at IS NULL",
            (timestamp, timestamp, public_id, revision),
        )
        if changed.rowcount != 1:
            row = db.execute(
                "SELECT revision FROM mistakes WHERE public_id=?", (public_id,)
            ).fetchone()
            if not row:
                raise MistakeError("NOT_FOUND", "错题不存在", status_code=404)
            raise MistakeError(
                "REVISION_CONFLICT",
                "错题已被其他操作更新",
                status_code=409,
                details={"current_revision": row["revision"]},
            )
    return {
        "ok": True,
        "public_id": public_id,
        "revision": revision + 1,
        "deleted_at": timestamp,
    }


def restore_mistake(
    db,
    public_id: str,
    *,
    revision: int,
    timestamp: str,
) -> dict:
    """Restore one tombstone under a revision CAS."""

    with immediate_transaction(db):
        changed = db.execute(
            "UPDATE mistakes SET deleted_at=NULL,revision=revision+1,updated_at=? "
            "WHERE public_id=? AND revision=? AND deleted_at IS NOT NULL",
            (timestamp, public_id, revision),
        )
        if changed.rowcount != 1:
            raise MistakeError(
                "REVISION_CONFLICT",
                "回收站记录不存在或版本冲突",
                status_code=409,
            )
    return {"ok": True, "public_id": public_id, "revision": revision + 1}
