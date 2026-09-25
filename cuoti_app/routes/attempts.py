"""Answer-free exam attempt tracking and idempotent mistake return."""

from __future__ import annotations

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..database import get_db
from ..services.review_schedule import initial_review_date
from ..services.exam_paper_layout import question_display_content
from ..services.exam_item_chapters import resolve_exam_item_chapters
from ..services.mistakes import sync_reason_items
from ..validation import ValidationError, mistake_fingerprint, reasons_value
from ..transactions import begin_immediate
from ..pagination import CursorError, bounded_limit, decode_cursor, encode_cursor


bp = Blueprint("exam_attempts", __name__)
_RESULTS = {"correct", "incorrect", "unanswered"}
_ASSISTANCE = {"none", "hint", "unknown"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _uuid(value, label="ID") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ValueError(f"{label} 无效") from exc


def _attempt_payload(db, row) -> dict:
    attempt = dict(row)
    attempt["items"] = [
        dict(item)
        for item in db.execute(
            "SELECT ai.public_id,ai.result_status,ai.assistance,ai.score,ai.comment,"
            "ai.returned_mistake_public_id,ei.id AS exam_item_id_internal,"
            "ei.public_id AS exam_item_public_id,e.subject_id,"
            "ei.position,ei.question_type,ei.difficulty,ei.content,ei.content_hash,"
            "ei.points,ei.source_kind,ei.source_candidate_id FROM exam_attempt_items ai JOIN exam_items ei "
            "ON ei.id=ai.exam_item_id JOIN exams e ON e.id=ei.exam_id "
            "WHERE ai.attempt_id=? ORDER BY ei.position",
            (row["id"],),
        ).fetchall()
    ]
    chapters = resolve_exam_item_chapters(
        db, [item["exam_item_id_internal"] for item in attempt["items"]]
    )
    for item in attempt["items"]:
        context = dict(chapters[item.pop("exam_item_id_internal")])
        context["subject_id"] = int(item["subject_id"])
        context["default_chapter_id"] = context["chapter_id"]
        item["return_context"] = context
        if item.get("source_kind") in {"ordinary", "past_exam_local", "past_exam_other"}:
            item["display_content"] = question_display_content(
                item["content"], candidate_id=item.get("source_candidate_id")
            )
    attempt.pop("id", None)
    return attempt


@bp.route("/api/v2/exams/<int:exam_id>/attempts", methods=["GET", "POST"])
def exam_attempt_collection(exam_id: int):
    db = get_db()
    exam = db.execute(
        "SELECT id FROM exams WHERE id=? AND artifact_status='published' "
        "AND deleted_at IS NULL", (exam_id,)
    ).fetchone()
    if not exam:
        return jsonify({"error": "试卷不存在", "code": "NOT_FOUND"}), 404
    if request.method == "GET":
        filters = {"exam_id": exam_id}
        try:
            limit = bounded_limit(request.args.get("limit"), default=20, maximum=100)
            cursor = decode_cursor(request.args.get("after"), filters=filters)
        except CursorError as exc:
            return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400
        if cursor is None:
            snapshot = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM exam_attempts WHERE exam_id=?",
                (exam_id,),
            ).fetchone()[0]
            last_id = snapshot + 1
        else:
            snapshot = cursor["s"]
            if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
                return jsonify({"error": "分页游标无效", "code": "INVALID_CURSOR"}), 400
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
        return jsonify({
            "items": [_attempt_payload(db, row) for row in page_rows],
            "total": total,
            "has_more": has_more,
            "next_cursor": next_cursor,
        })
    data = request.get_json(silent=True) if request.is_json else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是对象", "code": "VALIDATION"}), 400
    note = str(data.get("note") or "").strip()
    if len(note) > 5000:
        return jsonify({"error": "备注过长", "code": "VALIDATION"}), 400
    now = _now()
    begin_immediate(db)
    if not db.execute(
        "SELECT 1 FROM exams WHERE id=? AND artifact_status='published' "
        "AND deleted_at IS NULL", (exam_id,),
    ).fetchone():
        db.rollback()
        return jsonify({"error": "试卷不存在", "code": "NOT_FOUND"}), 404
    exam_items = db.execute(
        "SELECT id FROM exam_items WHERE exam_id=? ORDER BY position", (exam_id,)
    ).fetchall()
    if not exam_items:
        db.rollback()
        return jsonify({"error": "该试卷没有结构化题目快照", "code": "NO_ITEMS"}), 409
    try:
        cursor = db.execute(
            "INSERT INTO exam_attempts(public_id,exam_id,status,started_at,note,"
            "created_at,updated_at) VALUES(?,?,'draft',?,?,?,?)",
            (str(uuid.uuid4()), exam_id, now, note, now, now),
        )
        attempt_id = cursor.lastrowid
        db.executemany(
            "INSERT INTO exam_attempt_items(public_id,attempt_id,exam_item_id,"
            "created_at,updated_at) VALUES(?,?,?,?,?)",
            [
                (str(uuid.uuid4()), attempt_id, row["id"], now, now)
                for row in exam_items
            ],
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM exam_attempts WHERE id=?", (attempt_id,)).fetchone()
    return jsonify(_attempt_payload(db, row)), 201


@bp.route("/api/v2/exam-attempts/<public_id>", methods=["GET", "PATCH", "DELETE"])
def exam_attempt_detail(public_id: str):
    try:
        public_id = _uuid(public_id, "作答 ID")
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    db = get_db()
    row = db.execute(
        "SELECT a.* FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
        "WHERE a.public_id=? AND e.artifact_status='published' "
        "AND e.deleted_at IS NULL", (public_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": "作答不存在", "code": "NOT_FOUND"}), 404
    if request.method == "GET":
        return jsonify(_attempt_payload(db, row))
    if request.method == "DELETE":
        begin_immediate(db)
        if not db.execute(
            "SELECT 1 FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
            "WHERE a.id=? AND e.artifact_status='published' "
            "AND e.deleted_at IS NULL", (row["id"],),
        ).fetchone():
            db.rollback()
            return jsonify({"error": "作答不存在", "code": "NOT_FOUND"}), 404
        try:
            # Keep mistakes that were returned from this attempt, but detach the
            # internal attempt-item reference before cascading the attempt away.
            db.execute(
                "UPDATE mistakes SET source_exam_attempt_item_id=NULL "
                "WHERE source_exam_attempt_item_id IN "
                "(SELECT id FROM exam_attempt_items WHERE attempt_id=?)",
                (row["id"],),
            )
            db.execute("DELETE FROM exam_attempts WHERE id=?", (row["id"],))
            db.commit()
        except BaseException:
            db.rollback()
            raise
        return jsonify({"ok": True, "public_id": public_id})
    if row["status"] != "draft":
        return jsonify({"error": "已提交作答不可修改", "code": "ATTEMPT_SUBMITTED"}), 409
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是对象", "code": "VALIDATION"}), 400
    note = str(data.get("note", row["note"]) or "").strip()
    if len(note) > 5000:
        return jsonify({"error": "备注过长", "code": "VALIDATION"}), 400
    updates = data.get("items") or []
    if not isinstance(updates, list) or len(updates) > 200:
        return jsonify({"error": "items 必须是数组", "code": "VALIDATION"}), 400
    validated = []
    try:
        for item in updates:
            if not isinstance(item, dict):
                raise ValueError("作答项必须是对象")
            item_id = _uuid(item.get("public_id"), "作答项 ID")
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
        max_score = float(data.get("max_score", row["max_score"]) or 0)
        if max_score < 0 or max_score > 1000000:
            raise ValueError("满分无效")
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    now = _now()
    try:
        begin_immediate(db)
        locked_attempt = db.execute(
            "SELECT a.status FROM exam_attempts a JOIN exams e ON e.id=a.exam_id "
            "WHERE a.id=? AND e.artifact_status='published' "
            "AND e.deleted_at IS NULL", (row["id"],),
        ).fetchone()
        if not locked_attempt:
            db.rollback()
            return jsonify({"error": "作答不存在", "code": "NOT_FOUND"}), 404
        if locked_attempt["status"] != "draft":
            db.rollback()
            return jsonify({"error": "已提交作答不可修改", "code": "ATTEMPT_SUBMITTED"}), 409
        for item_id, result_status, assistance, score, comment in validated:
            changed = db.execute(
                "UPDATE exam_attempt_items SET result_status=?,assistance=COALESCE(?,assistance),score=?,comment=?,"
                "updated_at=? WHERE public_id=? AND attempt_id=?",
                (result_status, assistance, score, comment, now, item_id, row["id"]),
            ).rowcount
            if changed != 1:
                raise ValueError("作答项不属于当前作答")
        total_score = db.execute(
            "SELECT COALESCE(SUM(score),0) FROM exam_attempt_items WHERE attempt_id=?",
            (row["id"],),
        ).fetchone()[0]
        submit = bool(data.get("submit"))
        if submit and total_score > max_score:
            raise ValueError("总分不能超过满分")
        submitted_at = now if submit else None
        duration = data.get("duration_seconds")
        if duration is not None:
            duration = int(duration)
            if duration < 0 or duration > 365 * 24 * 3600:
                raise ValueError("用时无效")
        db.execute(
            "UPDATE exam_attempts SET status=?,submitted_at=?,duration_seconds=?,"
            "total_score=?,max_score=?,note=?,updated_at=? WHERE id=?",
            (
                "submitted" if submit else "draft", submitted_at, duration,
                total_score, max_score, note, now, row["id"],
            ),
        )
        db.commit()
    except (TypeError, ValueError) as exc:
        db.rollback()
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    updated = db.execute(
        "SELECT * FROM exam_attempts WHERE id=?", (row["id"],)
    ).fetchone()
    return jsonify(_attempt_payload(db, updated))


@bp.post("/api/v2/exam-attempt-items/<public_id>/return-mistake")
def return_attempt_item_to_mistakes(public_id: str):
    try:
        public_id = _uuid(public_id, "作答项 ID")
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    data = request.get_json(silent=True) if request.is_json else {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是对象", "code": "VALIDATION"}), 400
    try:
        reasons = reasons_value(data.get("reasons", []))
        copy_comment = data.get("copy_comment", False)
        if not isinstance(copy_comment, bool):
            raise ValidationError("复制批注选项无效")
        requested_chapter = data.get("chapter_id")
        if requested_chapter not in (None, ""):
            if isinstance(requested_chapter, bool):
                raise ValidationError("章节编号无效")
            requested_chapter = int(requested_chapter)
            if requested_chapter <= 0:
                raise ValidationError("章节编号无效")
        else:
            requested_chapter = None
    except (TypeError, ValueError, ValidationError) as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    db = get_db()
    try:
        begin_immediate(db)
        row = db.execute(
            "SELECT ai.id,ai.returned_mistake_public_id,ai.result_status,ai.comment,"
            "a.id AS attempt_id,a.status AS attempt_status,a.exam_id,"
            "ei.id AS exam_item_id,ei.position,ei.content,e.subject_id,s.name AS subject "
            "FROM exam_attempt_items ai "
            "JOIN exam_attempts a ON a.id=ai.attempt_id "
            "JOIN exam_items ei ON ei.id=ai.exam_item_id "
            "JOIN exams e ON e.id=a.exam_id "
            "JOIN subjects s ON s.id=e.subject_id WHERE ai.public_id=? "
            "AND e.artifact_status='published' AND e.deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            raise LookupError("作答项不存在")
        if row["attempt_status"] != "submitted":
            raise ValueError("请先提交作答")
        if row["result_status"] != "incorrect":
            raise ValueError("只有错误题目可以回流错题本")
        existing_public_id = row["returned_mistake_public_id"]
        if existing_public_id:
            db.commit()
            return jsonify({"mistake_public_id": existing_public_id, "idempotency_replayed": True})
        existing = db.execute(
            "SELECT public_id,chapter_id,reasons,note FROM mistakes WHERE source_exam_attempt_item_id=?",
            (row["id"],),
        ).fetchone()
        if existing:
            mistake_public_id = existing["public_id"]
            chapter_id = existing["chapter_id"]
            reasons = str(existing["reasons"] or "")
            copy_comment = bool(existing["note"])
        else:
            mistake_public_id = str(uuid.uuid4())
            date_added = datetime.now().astimezone().date().isoformat()
            qnum = f"试卷{row['exam_id']}-第{row['position']}题"
            source_ref = (
                f"exam:{row['exam_id']}:position:{row['position']}:"
                f"attempt:{row['attempt_id']}"
            )
            context = resolve_exam_item_chapters(db, [row["exam_item_id"]])[int(row["exam_item_id"])]
            chapter_id = requested_chapter or context["chapter_id"]
            chapter_name = ""
            if chapter_id is not None:
                chapter_row = db.execute(
                    "SELECT id,name FROM chapters WHERE id=? AND subject_id=?",
                    (chapter_id, row["subject_id"]),
                ).fetchone()
                if not chapter_row:
                    raise ValueError("所选章节不属于当前科目")
                chapter_id, chapter_name = int(chapter_row["id"]), str(chapter_row["name"])
            note = str(row["comment"] or "").strip() if copy_comment else ""
            db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,reasons,note,date_added,next_review,"
                "status,fingerprint,source_kind,source_ref,source_exam_attempt_item_id,"
                "revision,updated_at,history_complete) "
                "VALUES(?,?,?,?,?,?,?,?,?,'未掌握',?,'exam_attempt',?,?,1,?,1)",
                (
                    mistake_public_id, row["subject_id"], chapter_id, qnum, row["content"],
                    reasons, note, date_added, initial_review_date(date_added),
                    mistake_fingerprint(
                        row["subject"], chapter_name, qnum, row["content"], date_added
                    ),
                    source_ref, row["id"], _now(),
                ),
            )
            mistake_id = db.execute(
                "SELECT id FROM mistakes WHERE public_id=?", (mistake_public_id,)
            ).fetchone()["id"]
            sync_reason_items(db, int(mistake_id), reasons)
        db.execute(
            "UPDATE exam_attempt_items SET returned_mistake_public_id=?,updated_at=? "
            "WHERE id=?",
            (mistake_public_id, _now(), row["id"]),
        )
        db.commit()
        return jsonify({"mistake_public_id": mistake_public_id, "idempotency_replayed": False,
                        "chapter_id": chapter_id, "reasons": reasons.split(",") if reasons else [],
                        "note_copied": bool(copy_comment and row["comment"])}), 201
    except LookupError as exc:
        db.rollback()
        return jsonify({"error": str(exc), "code": "NOT_FOUND"}), 404
    except ValueError as exc:
        db.rollback()
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 409
