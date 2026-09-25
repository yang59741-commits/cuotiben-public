"""Transaction-owned exam attempt write use cases."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from ..pagination import CursorError, decode_cursor, encode_cursor
from ..transactions import immediate_transaction
from ..validation import mistake_fingerprint
from .review_schedule import initial_review_date
from .exam_scoring import public_points


_RESULTS = {"correct", "incorrect", "unanswered"}
_ASSISTANCE = {"none", "hint", "unknown"}


class AttemptError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _validation(message: str) -> AttemptError:
    return AttemptError("VALIDATION", message, status_code=400)


def _item_uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise _validation("作答项 ID 无效") from exc


def attempt_payload(db, row, *, include_items: bool = True, items=None) -> dict:
    """Serialize one attempt and its immutable exam-item snapshots."""

    attempt = dict(row)
    if include_items:
        if items is None:
            items = db.execute(
                "SELECT ai.public_id,ai.result_status,ai.assistance,ai.score,ai.comment,"
                "ai.returned_mistake_public_id,ei.public_id AS exam_item_public_id,"
                "ei.position,ei.question_type,ei.difficulty,ei.content,ei.content_hash,"
                "ei.points,ei.points_evidence_hash,e.scoring_mode FROM exam_attempt_items ai JOIN exam_items ei "
                "ON ei.id=ai.exam_item_id JOIN exams e ON e.id=ei.exam_id WHERE ai.attempt_id=? ORDER BY ei.position",
                (row["id"],),
            ).fetchall()
        attempt["items"] = []
        for raw_item in items:
            item = dict(raw_item)
            mode = item.pop("scoring_mode", "legacy_unknown")
            item["points"] = public_points(mode, item.get("points"))
            attempt["items"].append(item)
    attempt.pop("id", None)
    attempt.pop("idempotency_key", None)
    return attempt


def _attempt_summaries(db, rows) -> dict[int, dict]:
    if not rows:
        return {}
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    aggregates = db.execute(
        "SELECT attempt_id,COUNT(*) AS item_count,"
        "SUM(CASE WHEN result_status <> 'unanswered' THEN 1 ELSE 0 END) "
        "AS answered_count,"
        "SUM(CASE WHEN result_status = 'incorrect' THEN 1 ELSE 0 END) "
        "AS incorrect_count,"
        "SUM(CASE WHEN returned_mistake_public_id IS NOT NULL THEN 1 ELSE 0 END) "
        "AS returned_count FROM exam_attempt_items WHERE attempt_id IN ("
        f"{placeholders}) GROUP BY attempt_id",
        ids,
    ).fetchall()
    return {
        int(row["attempt_id"]): {
            "item_count": int(row["item_count"] or 0),
            "answered_count": int(row["answered_count"] or 0),
            "incorrect_count": int(row["incorrect_count"] or 0),
            "returned_count": int(row["returned_count"] or 0),
        }
        for row in aggregates
    }


def _attempt_items_by_attempt(db, rows) -> dict[int, list[dict]]:
    if not rows:
        return {}
    ids = [int(row["id"]) for row in rows]
    placeholders = ",".join("?" for _ in ids)
    item_rows = db.execute(
        "SELECT ai.attempt_id,ai.public_id,ai.result_status,ai.assistance,ai.score,ai.comment,"
        "ai.returned_mistake_public_id,ei.public_id AS exam_item_public_id,"
        "ei.position,ei.question_type,ei.difficulty,ei.content,ei.content_hash,"
        "ei.points,ei.points_evidence_hash,e.scoring_mode FROM exam_attempt_items ai JOIN exam_items ei "
        "ON ei.id=ai.exam_item_id JOIN exams e ON e.id=ei.exam_id WHERE ai.attempt_id IN ("
        f"{placeholders}) ORDER BY ai.attempt_id,ei.position,ai.id",
        ids,
    ).fetchall()
    grouped = {attempt_id: [] for attempt_id in ids}
    for item in item_rows:
        value = {key: value for key, value in dict(item).items() if key != "attempt_id"}
        mode = value.pop("scoring_mode", "legacy_unknown")
        value["points"] = public_points(mode, value.get("points"))
        grouped[int(item["attempt_id"])].append(value)
    return grouped


def list_attempts(
    db,
    exam_id: int,
    *,
    limit: int,
    after: str | None,
    include_items: bool,
) -> dict:
    """Return one cursor page without N+1 item queries."""

    if not published_exam_exists(db, exam_id):
        raise AttemptError("NOT_FOUND", "试卷不存在", status_code=404)

    filters = {"exam_id": exam_id}
    cursor = decode_cursor(after, filters=filters)
    if cursor is None:
        snapshot = db.execute(
            "SELECT COALESCE(MAX(id),0) FROM exam_attempts WHERE exam_id=?",
            (exam_id,),
        ).fetchone()[0]
        last_id = snapshot + 1
    else:
        snapshot = cursor["s"]
        if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
            raise CursorError("分页游标无效")
        last_id = cursor["p"][0]
    rows = db.execute(
        "SELECT * FROM exam_attempts WHERE exam_id=? AND id<=? AND id<? "
        "ORDER BY id DESC LIMIT ?",
        (exam_id, snapshot, last_id, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    page_rows = rows[:limit]
    total = db.execute(
        "SELECT COUNT(*) FROM exam_attempts WHERE exam_id=? AND id<=?",
        (exam_id, snapshot),
    ).fetchone()[0]
    next_cursor = None
    if has_more and page_rows:
        next_cursor = encode_cursor(
            filters=filters, snapshot=snapshot, values=[page_rows[-1]["id"]]
        )
    summaries = _attempt_summaries(db, page_rows)
    item_map = _attempt_items_by_attempt(db, page_rows) if include_items else {}
    payload_items = []
    empty_summary = {
        "item_count": 0,
        "answered_count": 0,
        "incorrect_count": 0,
        "returned_count": 0,
    }
    for row in page_rows:
        payload = attempt_payload(
            db,
            row,
            include_items=include_items,
            items=item_map.get(int(row["id"]), []),
        )
        payload.update(summaries.get(int(row["id"]), empty_summary))
        payload_items.append(payload)
    return {
        "items": payload_items,
        "include_items": include_items,
        "total": total,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def find_attempt(db, public_id: str):
    return db.execute(
        "SELECT a.* FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "WHERE a.public_id=? AND e.artifact_status='published' "
        "AND e.deleted_at IS NULL", (public_id,)
    ).fetchone()


def find_attempt_by_id(db, attempt_id: int):
    return db.execute(
        "SELECT a.* FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "WHERE a.id=? AND e.artifact_status='published' "
        "AND e.deleted_at IS NULL", (attempt_id,)
    ).fetchone()


def published_exam_exists(db, exam_id: int) -> bool:
    return db.execute(
        "SELECT id FROM exams WHERE id=? AND artifact_status='published' "
        "AND deleted_at IS NULL", (exam_id,)
    ).fetchone() is not None


def delete_attempt(db, attempt_id: int) -> None:
    """Delete one attempt while preserving any mistakes returned from it."""

    with immediate_transaction(db):
        active = db.execute(
            "SELECT 1 FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
            "WHERE a.id=? AND e.artifact_status='published' "
            "AND e.deleted_at IS NULL", (attempt_id,),
        ).fetchone()
        if active is None:
            raise AttemptError("NOT_FOUND", "作答不存在", status_code=404)
        db.execute(
            "UPDATE mistakes SET source_exam_attempt_item_id=NULL "
            "WHERE source_exam_attempt_item_id IN "
            "(SELECT id FROM exam_attempt_items WHERE attempt_id=?)",
            (attempt_id,),
        )
        db.execute("DELETE FROM exam_attempts WHERE id=?", (attempt_id,))


def create_or_resume_attempt(
    db,
    exam_id: int,
    *,
    note: str,
    idempotency_key: str | None,
    timestamp: str,
) -> tuple[int, bool]:
    """Create one draft or resume the latest draft under a database write lock."""

    with immediate_transaction(db):
        if not published_exam_exists(db, exam_id):
            raise AttemptError("NOT_FOUND", "试卷不存在", status_code=404)
        if idempotency_key is not None:
            existing = db.execute(
                "SELECT a.id FROM exam_attempt_requests r "
                "JOIN exam_attempts a ON a.id=r.attempt_id "
                "WHERE r.exam_id=? AND r.idempotency_key=?",
                (exam_id, idempotency_key),
            ).fetchone()
            if existing:
                return int(existing["id"]), False

        existing = db.execute(
            "SELECT id,idempotency_key FROM exam_attempts "
            "WHERE exam_id=? AND status='draft' ORDER BY id DESC LIMIT 1",
            (exam_id,),
        ).fetchone()
        if existing:
            if idempotency_key is not None:
                if existing["idempotency_key"] is None:
                    db.execute(
                        "UPDATE exam_attempts SET idempotency_key=? WHERE id=?",
                        (idempotency_key, existing["id"]),
                    )
                db.execute(
                    "INSERT INTO exam_attempt_requests("
                    "exam_id,idempotency_key,attempt_id,created_at) VALUES(?,?,?,?)",
                    (exam_id, idempotency_key, existing["id"], timestamp),
                )
            return int(existing["id"]), False

        exam_items = db.execute(
            "SELECT id FROM exam_items WHERE exam_id=? ORDER BY position", (exam_id,)
        ).fetchall()
        if not exam_items:
            raise AttemptError(
                "NO_ITEMS", "该试卷没有结构化题目快照", status_code=409
            )
        cursor = db.execute(
            "INSERT INTO exam_attempts(public_id,exam_id,status,started_at,note,"
            "idempotency_key,created_at,updated_at) "
            "VALUES(?,?,'draft',?,?,?,?,?)",
            (
                str(uuid.uuid4()), exam_id, timestamp, note, idempotency_key,
                timestamp, timestamp,
            ),
        )
        attempt_id = int(cursor.lastrowid)
        db.executemany(
            "INSERT INTO exam_attempt_items(public_id,attempt_id,exam_item_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?)",
            [
                (str(uuid.uuid4()), attempt_id, row["id"], timestamp, timestamp)
                for row in exam_items
            ],
        )
        if idempotency_key is not None:
            db.execute(
                "INSERT INTO exam_attempt_requests("
                "exam_id,idempotency_key,attempt_id,created_at) VALUES(?,?,?,?)",
                (exam_id, idempotency_key, attempt_id, timestamp),
            )
    return attempt_id, True


def update_attempt(db, public_id: str, data, *, timestamp: str) -> int:
    """Update or submit one draft attempt in a single owned transaction."""

    if not isinstance(data, dict):
        raise _validation("请求体必须是对象")
    updates = data.get("items") or []
    if not isinstance(updates, list) or len(updates) > 200:
        raise _validation("items 必须是数组")
    validated = []
    try:
        for item in updates:
            if not isinstance(item, dict):
                raise ValueError("作答项必须是对象")
            item_id = _item_uuid(item.get("public_id"))
            result_status = str(item.get("result_status") or "unanswered")
            if result_status not in _RESULTS:
                raise ValueError("作答状态无效")
            assistance = item.get("assistance")
            if assistance is not None:
                assistance = str(assistance)
                if assistance not in _ASSISTANCE:
                    raise ValueError("辅助状态无效")
            score = float(item.get("score") or 0)
            if score < 0 or score > 100000:
                raise ValueError("分数无效")
            comment = str(item.get("comment") or "").strip()
            if len(comment) > 5000:
                raise ValueError("批注过长")
            validated.append((item_id, result_status, assistance, score, comment))
        duration = data.get("duration_seconds")
        if duration is not None:
            duration = int(duration)
            if duration < 0 or duration > 365 * 24 * 3600:
                raise ValueError("用时无效")
    except (TypeError, ValueError) as exc:
        raise _validation(str(exc)) from exc

    with immediate_transaction(db):
        row = db.execute(
            "SELECT a.id,a.status,a.note,a.max_score FROM exam_attempts a "
            "JOIN exams e ON e.id=a.exam_id WHERE a.public_id=? "
            "AND e.artifact_status='published' AND e.deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise AttemptError("NOT_FOUND", "作答不存在", status_code=404)
        if row["status"] != "draft":
            raise AttemptError(
                "ATTEMPT_SUBMITTED", "已提交作答不可修改", status_code=409
            )
        note = str(data.get("note", row["note"]) or "").strip()
        if len(note) > 5000:
            raise _validation("备注过长")
        try:
            max_score = float(data.get("max_score", row["max_score"]) or 0)
        except (TypeError, ValueError) as exc:
            raise _validation("满分无效") from exc
        if max_score < 0 or max_score > 1000000:
            raise _validation("满分无效")
        for item_id, result_status, assistance, score, comment in validated:
            changed = db.execute(
                "UPDATE exam_attempt_items SET result_status=?,assistance=COALESCE(?,assistance),score=?,comment=?,"
                "updated_at=? WHERE public_id=? AND attempt_id=?",
                (result_status, assistance, score, comment, timestamp, item_id, row["id"]),
            ).rowcount
            if changed != 1:
                raise _validation("作答项不属于当前作答")
        total_score = db.execute(
            "SELECT COALESCE(SUM(score),0) FROM exam_attempt_items WHERE attempt_id=?",
            (row["id"],),
        ).fetchone()[0]
        submit = bool(data.get("submit"))
        if submit and total_score > max_score:
            raise _validation("总分不能超过满分")
        db.execute(
            "UPDATE exam_attempts SET status=?,submitted_at=?,duration_seconds=?,"
            "total_score=?,max_score=?,note=?,updated_at=? WHERE id=?",
            (
                "submitted" if submit else "draft",
                timestamp if submit else None,
                duration,
                total_score,
                max_score,
                note,
                timestamp,
                row["id"],
            ),
        )
    return int(row["id"])


def return_attempt_item(db, public_id: str, *, now: datetime) -> dict:
    """Idempotently return one submitted incorrect item to the mistake book."""

    timestamp = now.isoformat(timespec="seconds")
    with immediate_transaction(db):
        row = db.execute(
            "SELECT ai.id,ai.returned_mistake_public_id,ai.result_status,"
            "a.id AS attempt_id,a.status AS attempt_status,a.exam_id,"
            "ei.position,ei.content,e.subject_id,s.name AS subject,e.config "
            "FROM exam_attempt_items ai "
            "JOIN exam_attempts a ON a.id=ai.attempt_id "
            "JOIN exam_items ei ON ei.id=ai.exam_item_id "
            "JOIN exams e ON e.id=a.exam_id "
            "JOIN subjects s ON s.id=e.subject_id WHERE ai.public_id=? "
            "AND e.artifact_status='published' AND e.deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise AttemptError("NOT_FOUND", "作答项不存在", status_code=404)
        if row["attempt_status"] != "submitted":
            raise AttemptError("VALIDATION", "请先提交作答", status_code=409)
        if row["result_status"] != "incorrect":
            raise AttemptError(
                "VALIDATION", "只有错误题目可以回流错题本", status_code=409
            )
        existing_public_id = row["returned_mistake_public_id"]
        if existing_public_id:
            return {
                "mistake_public_id": existing_public_id,
                "idempotency_replayed": True,
            }
        existing = db.execute(
            "SELECT public_id FROM mistakes WHERE source_exam_attempt_item_id=?",
            (row["id"],),
        ).fetchone()
        if existing:
            mistake_public_id = existing["public_id"]
        else:
            mistake_public_id = str(uuid.uuid4())
            date_added = now.date().isoformat()
            qnum = f"试卷{row['exam_id']}-第{row['position']}题"
            source_ref = (
                f"exam:{row['exam_id']}:position:{row['position']}:"
                f"attempt:{row['attempt_id']}"
            )
            chapter_id = None
            chapter_name = ""
            try:
                config = json.loads(row["config"] or "{}")
                raw_chapters = config.get("chapters") or config.get("chapter_ids") or []
                if isinstance(raw_chapters, (str, int)):
                    raw_chapters = [raw_chapters]
                chapter_ids = [
                    int(value) for value in raw_chapters if str(value).isdigit()
                ]
                if len(chapter_ids) == 1:
                    chapter_row = db.execute(
                        "SELECT id,name FROM chapters WHERE id=? AND subject_id=?",
                        (chapter_ids[0], row["subject_id"]),
                    ).fetchone()
                    if chapter_row:
                        chapter_id = chapter_row["id"]
                        chapter_name = chapter_row["name"]
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "date_added,next_review,status,fingerprint,source_kind,source_ref,"
                "source_exam_attempt_item_id,revision,updated_at,history_complete) "
                "VALUES(?,?,?,?,?,?,?,'未掌握',?,'exam_attempt',?,?,1,?,1)",
                (
                    mistake_public_id,
                    row["subject_id"],
                    chapter_id,
                    qnum,
                    row["content"],
                    date_added,
                    initial_review_date(date_added),
                    mistake_fingerprint(
                        row["subject"], chapter_name, qnum, row["content"], date_added
                    ),
                    source_ref,
                    row["id"],
                    timestamp,
                ),
            )
        db.execute(
            "UPDATE exam_attempt_items SET returned_mistake_public_id=?,updated_at=? "
            "WHERE id=?",
            (mistake_public_id, timestamp, row["id"]),
        )
    return {
        "mistake_public_id": mistake_public_id,
        "idempotency_replayed": False,
    }
