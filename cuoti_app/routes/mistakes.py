"""Mistake tracking, review, and statistics routes."""

import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request
from ..transactions import begin_immediate

from ..database import get_db
from ..pagination import (
    CursorError,
    bounded_limit,
    decode_cursor,
    encode_cursor,
    escaped_like,
)
from ..services.review_schedule import (
    SCHEDULE_VERSION,
    advance_review,
    initial_review_date,
    reopen_review,
)
from ..validation import (
    MISTAKE_REASONS,
    STATUS_VALUES,
    ValidationError,
    date_value,
    json_object,
    mistake_fingerprint,
    mistake_content_fingerprint,
    positive_int,
    reasons_value,
    subject_chapter,
    text_value,
)

bp = Blueprint("mistakes", __name__)


def _mistake_filters(args):
    where, params = ["m.deleted_at IS NULL"], []
    filters = {}
    for key, label in (("subject_id", "科目编号"), ("chapter_id", "章节编号")):
        if args.get(key):
            value = positive_int(args[key], label)
            where.append(f"m.{key}=?")
            params.append(value)
            filters[key] = value
    status = args.get("status")
    if status:
        if status not in STATUS_VALUES:
            raise ValidationError("状态无效")
        where.append("m.status=?")
        params.append(status)
        filters["status"] = status
    search = args.get("search")
    if search:
        search = text_value(search, "搜索词", maximum=200)
        filters["search"] = search
        like = escaped_like(search)
        exact = (
            "(m.qnum LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\' "
            "OR m.content LIKE ? ESCAPE '\\')"
        )
        # Trigram is only an accelerator. Exact LIKE verification preserves behavior.
        if len(search) >= 3 and re.fullmatch(r"[\w\u3400-\u9fff]+", search):
            where.append(
                "(m.id IN (SELECT rowid FROM mistakes_fts "
                "WHERE mistakes_fts MATCH ?) AND " + exact + ")"
            )
            params.append('"' + search.replace('"', '""') + '"')
        else:
            where.append(exact)
        params.extend([like, like, like])
    return where, params, filters


def _fts_exists(db) -> bool:
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mistakes_fts'"
    ).fetchone() is not None


def _reason_array(value) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _v2_mistake(row) -> dict:
    item = dict(row)
    item.pop("answer", None)
    item.pop("solution", None)
    item["reasons"] = _reason_array(item.get("reasons"))
    return item


def _sync_reason_items(db, mistake_id: int, reasons: str) -> None:
    db.execute("DELETE FROM mistake_reason_items WHERE mistake_id=?", (mistake_id,))
    db.executemany(
        "INSERT INTO mistake_reason_items(mistake_id,reason_code) VALUES(?,?)",
        [(mistake_id, reason) for reason in _reason_array(reasons)],
    )

@bp.route('/api/subjects')
def api_subjects():
    return jsonify([dict(r) for r in get_db().execute("SELECT * FROM subjects").fetchall()])


@bp.route('/api/v2/mistake-counts')
def api_mistake_counts():
    db = get_db()
    status = request.args.get('status')
    join_sql = "FROM subjects s LEFT JOIN mistakes m ON m.subject_id=s.id AND m.deleted_at IS NULL"
    count_params = []
    total_sql = "SELECT COUNT(*) AS count FROM mistakes WHERE deleted_at IS NULL"
    total_params = []
    if status:
        if status not in STATUS_VALUES:
            return jsonify({'error': '状态无效'}), 400
        join_sql += " AND m.status=?"
        count_params.append(status)
        total_sql += " AND status=?"
        total_params.append(status)
    subjects = [
        dict(row)
        for row in db.execute(
            "SELECT s.id,s.name,COUNT(m.id) AS count "
            + join_sql
            + " GROUP BY s.id,s.name ORDER BY s.id",
            count_params,
        ).fetchall()
    ]
    total = db.execute(total_sql, total_params).fetchone()['count']
    return jsonify({'total': total, 'subjects': subjects})


@bp.route('/api/chapters')
def api_chapters():
    sid = request.args.get('subject_id')
    if sid:
        try:
            sid = positive_int(sid, "科目编号")
        except ValidationError as exc:
            return jsonify({'error': str(exc)}), 400
        return jsonify([dict(r) for r in get_db().execute("SELECT * FROM chapters WHERE subject_id=? ORDER BY id",(sid,))])
    return jsonify([dict(r) for r in get_db().execute("SELECT * FROM chapters ORDER BY id").fetchall()])

@bp.route('/internal/legacy/mistakes', methods=['GET','POST'])
def api_mistakes():
    db = get_db()
    if request.method == 'POST':
        try:
            d = json_object(request)
            subject, chapter = subject_chapter(
                db, d.get('subject_id'), d.get('chapter_id')
            )
            qnum = text_value(d.get('qnum'), '题号', maximum=80)
            content = text_value(
                d.get('content'), '题目内容', maximum=100_000, required=True
            )
            reasons = reasons_value(d.get('reasons'))
            note = text_value(d.get('note'), '备注', maximum=20_000)
            date_added = date_value(d.get('date_added'), '日期')
            fingerprint = mistake_fingerprint(
                subject['name'],
                chapter['name'] if chapter else '',
                qnum,
                content,
                date_added,
            )
        except ValidationError as exc:
            return jsonify({'error': str(exc)}), 400
        cursor = db.execute(
            "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,note,"
            "date_added,next_review,fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                subject['id'],
                chapter['id'] if chapter else None,
                qnum,
                content,
                reasons,
                note,
                date_added,
                initial_review_date(date_added),
                fingerprint,
            ),
        )
        _sync_reason_items(db, cursor.lastrowid, reasons)
        db.commit()
        created = db.execute(
            "SELECT public_id,revision FROM mistakes WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return jsonify({'ok': True, 'id': cursor.lastrowid, **dict(created)})
    try:
        where, params, _filters = _mistake_filters(request.args)
        if request.args.get("search") and not _fts_exists(db):
            where, params, _filters = _mistake_filters_without_fts(request.args)
    except ValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    sql = "SELECT m.*,s.name as subject,c.name as chapter FROM mistakes m LEFT JOIN subjects s ON m.subject_id=s.id LEFT JOIN chapters c ON m.chapter_id=c.id"
    if where: sql += " WHERE "+" AND ".join(where)
    sql += " ORDER BY m.id DESC LIMIT 200"
    return jsonify([dict(r) for r in db.execute(sql,params).fetchall()])


def _mistake_filters_without_fts(args):
    """Build filters without referencing an optional FTS table."""
    where, params, filters = _mistake_filters(args)
    if args.get("search") and any("mistakes_fts" in clause for clause in where):
        where = [clause for clause in where if "mistakes_fts" not in clause]
        search = filters["search"]
        like = escaped_like(search)
        where.append(
            "(m.qnum LIKE ? ESCAPE '\\' OR m.note LIKE ? ESCAPE '\\' "
            "OR m.content LIKE ? ESCAPE '\\')"
        )
        # Remove MATCH plus three LIKE parameters added by the first builder.
        params = params[:-4] + [like, like, like]
    return where, params, filters


@bp.route('/api/v2/mistakes', methods=['GET', 'POST'])
def api_v2_mistakes():
    db = get_db()
    if request.method == 'POST':
        try:
            data = json_object(request)
            subject, chapter = subject_chapter(
                db, data.get('subject_id'), data.get('chapter_id')
            )
            qnum = text_value(data.get('qnum'), '题号', maximum=80)
            content = text_value(
                data.get('content'), '题目内容', maximum=100_000, required=True
            )
            note = text_value(data.get('note'), '备注', maximum=20_000)
            reasons = reasons_value(data.get('reasons'))
            date_added = date_value(data.get('date_added'), '日期')
            source_kind = text_value(
                data.get('source_kind') or 'manual', '来源类型', maximum=40, required=True
            )
            source_ref = text_value(data.get('source_ref'), '来源引用', maximum=500)
            difficulty = text_value(data.get('difficulty'), '难度', maximum=40)
        except ValidationError as exc:
            return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
        public_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec='seconds')
        fingerprint = mistake_fingerprint(
            subject['name'], chapter['name'] if chapter else '', qnum, content, date_added
        )
        with db:
            cursor = db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,reasons,"
                "note,date_added,next_review,fingerprint,content_fingerprint,updated_at,"
                "source_kind,source_ref,difficulty,history_complete) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (public_id, subject['id'], chapter['id'] if chapter else None, qnum,
                 content, reasons, note, date_added, initial_review_date(date_added), fingerprint,
                 mistake_content_fingerprint(qnum, content), now, source_kind,
                 source_ref or None, difficulty or None),
            )
            _sync_reason_items(db, cursor.lastrowid, reasons)
        row = db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON s.id=m.subject_id "
            "LEFT JOIN chapters c ON c.id=m.chapter_id WHERE m.id=?",
            (cursor.lastrowid,),
        ).fetchone()
        return jsonify(_v2_mistake(row)), 201
    try:
        limit = bounded_limit(request.args.get("limit"))
        raw_search = str(request.args.get("search") or "").strip()
        fts_search = bool(
            raw_search
            and len(raw_search) >= 3
            and re.fullmatch(r"[\w\u3400-\u9fff]+", raw_search)
            and _fts_exists(db)
        )
        if raw_search and not fts_search:
            where, params, filters = _mistake_filters_without_fts(request.args)
        else:
            where, params, filters = _mistake_filters(request.args)
        cursor = decode_cursor(request.args.get("after"), filters=filters)
    except (ValidationError, CursorError) as exc:
        return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400

    base_where = list(where)
    base_params = list(params)
    relevance = bool(raw_search)
    escaped_search = raw_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    score_sql = (
        "CASE WHEN m.qnum=? THEN 0 WHEN m.qnum LIKE ? ESCAPE '\\' THEN 1 "
        "WHEN m.note LIKE ? ESCAPE '\\' THEN 2 ELSE 3 END"
    )
    score_params = [raw_search, escaped_search + "%", "%" + escaped_search + "%"]
    if cursor is None:
        max_sql = "SELECT COALESCE(MAX(m.id),0) AS n FROM mistakes m"
        if base_where:
            max_sql += " WHERE " + " AND ".join(base_where)
        snapshot = db.execute(max_sql, base_params).fetchone()["n"]
        last_score, last_id = -1, snapshot + 1
    else:
        snapshot = cursor["s"]
        if relevance:
            if (
                len(cursor["p"]) != 2
                or not isinstance(cursor["p"][0], int)
                or not isinstance(cursor["p"][1], int)
            ):
                return jsonify({"error": "分页游标无效", "code": "INVALID_CURSOR"}), 400
            last_score, last_id = cursor["p"]
        else:
            if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
                return jsonify({"error": "分页游标无效", "code": "INVALID_CURSOR"}), 400
            last_score, last_id = -1, cursor["p"][0]

    if relevance:
        page_where = base_where + ["m.id<=?", f"(({score_sql})>? OR (({score_sql})=? AND m.id<?))"]
        page_params = base_params + [snapshot, *score_params, last_score, *score_params, last_score, last_id, limit + 1]
        sql = (
            f"SELECT m.*,s.name AS subject,c.name AS chapter,({score_sql}) AS search_score "
            "FROM mistakes m LEFT JOIN subjects s ON m.subject_id=s.id "
            "LEFT JOIN chapters c ON m.chapter_id=c.id WHERE "
            + " AND ".join(page_where)
            + " ORDER BY search_score ASC,m.id DESC LIMIT ?"
        )
        page_params = [*score_params, *page_params]
    else:
        page_where = base_where + ["m.id<=?", "m.id<?"]
        page_params = base_params + [snapshot, last_id, limit + 1]
        sql = (
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON m.subject_id=s.id "
            "LEFT JOIN chapters c ON m.chapter_id=c.id WHERE "
            + " AND ".join(page_where)
            + " ORDER BY m.id DESC LIMIT ?"
        )
    rows = [_v2_mistake(row) for row in db.execute(sql, page_params).fetchall()]
    has_more = len(rows) > limit
    items = rows[:limit]
    count_where = base_where + ["m.id<=?"]
    total = db.execute(
        "SELECT COUNT(*) AS n FROM mistakes m WHERE " + " AND ".join(count_where),
        base_params + [snapshot],
    ).fetchone()["n"]
    next_cursor = None
    if has_more and items:
        cursor_values = (
            [int(items[-1]["search_score"]), items[-1]["id"]]
            if relevance
            else [items[-1]["id"]]
        )
        next_cursor = encode_cursor(
            filters=filters, snapshot=snapshot, values=cursor_values
        )
    return jsonify(
        {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total": total,
            "search_backend": (
                "fts5_trigram_explicit_score" if fts_search else ("like_explicit_score" if relevance else "none")
            ),
        }
    )

@bp.put('/internal/legacy/mistakes/<int:mid>/toggle')
def api_toggle(mid):
    db = get_db()
    row = db.execute(
        "SELECT status,correct_streak,review_stage,review_cycle FROM mistakes "
        "WHERE id=? AND deleted_at IS NULL", (mid,)
    ).fetchone()
    if not row:
        return jsonify({'error': '错题不存在'}), 404
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    if row['status'] == '未掌握':
        status = '已掌握'
        streak = row['correct_streak'] or 0
        stage = row['review_stage'] or 0
        cycle = row['review_cycle'] or 1
        next_review = None
        event_type = 'manual_master'
    else:
        transition = reopen_review(review_cycle=row['review_cycle'], reopened_on=today)
        status = transition.status
        streak = transition.correct_streak
        stage = transition.review_stage
        cycle = transition.review_cycle
        next_review = transition.next_review
        event_type = 'manual_reopen'
    with db:
        db.execute(
            "UPDATE mistakes SET status=?,correct_streak=?,next_review=?,review_stage=?,"
            "review_cycle=?,schedule_version=?,"
            "revision=revision+1,updated_at=? WHERE id=?",
            (status, streak, next_review, stage, cycle, SCHEDULE_VERSION,
             now.isoformat(timespec='seconds'), mid),
        )
        db.execute(
            "INSERT INTO review_events(mistake_id,event_type,reviewed_at,"
            "status_after,correct_streak,stage_before,stage_after,cycle_after,"
            "next_review_after,schedule_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (mid, event_type, now.isoformat(timespec='seconds'), status, streak,
             row['review_stage'], stage, cycle, next_review, SCHEDULE_VERSION),
        )
    return jsonify({
        'ok': True,
        'status': status,
        'correct_streak': streak,
        'review_stage': stage,
        'review_cycle': cycle,
        'next_review': next_review,
    })

@bp.put('/internal/legacy/mistakes/<int:mid>/reasons')
def api_update_reasons(mid):
    try:
        data = json_object(request)
        reasons = reasons_value(data.get('reasons', []))
    except ValidationError as exc:
        return jsonify({'error': str(exc)}), 400

    db = get_db()
    if not db.execute("SELECT 1 FROM mistakes WHERE id=? AND deleted_at IS NULL", (mid,)).fetchone():
        return jsonify({'error': '错题不存在'}), 404
    with db:
        db.execute(
            "UPDATE mistakes SET reasons=?,revision=revision+1,updated_at=? WHERE id=?",
            (reasons, datetime.now().isoformat(timespec='seconds'), mid),
        )
        _sync_reason_items(db, mid, reasons)
    return jsonify({'ok': True, 'reasons': reasons})

@bp.delete('/internal/legacy/mistakes/<int:mid>')
def api_delete(mid):
    db = get_db()
    if not db.execute("SELECT 1 FROM mistakes WHERE id=? AND deleted_at IS NULL", (mid,)).fetchone():
        return jsonify({'error': '错题不存在'}), 404
    db.execute(
        "UPDATE mistakes SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?",
        (datetime.now().isoformat(timespec='seconds'), datetime.now().isoformat(timespec='seconds'), mid),
    )
    db.commit()
    return jsonify({'ok':True})

@bp.put('/internal/legacy/mistakes/<int:mid>/review')
def api_review(mid):
    try:
        d = json_object(request)
    except ValidationError as exc:
        return jsonify({'error': str(exc)}), 400
    result = d.get('result')
    if result not in ('pass', 'again'):
        return jsonify({'error': '复习结果必须是 pass 或 again'}), 400
    db = get_db()
    row = db.execute(
        "SELECT review_count,correct_streak,lapse_count,review_stage,review_cycle "
        "FROM mistakes WHERE id=? AND deleted_at IS NULL",
        (mid,),
    ).fetchone()
    if not row:
        return jsonify({'error': '错题不存在'}), 404
    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    review_count = (row['review_count'] or 0) + 1
    lapse_count = row['lapse_count'] or 0
    transition = advance_review(
        result=result, review_stage=row['review_stage'],
        review_cycle=row['review_cycle'], reviewed_on=today,
    )
    streak = transition.correct_streak
    next_review = transition.next_review
    status = transition.status
    if result == 'again':
        lapse_count += 1
    with db:
        db.execute(
            "UPDATE mistakes SET review_count=?,correct_streak=?,lapse_count=?,"
            "last_review=?,next_review=?,status=?,review_stage=?,review_cycle=?,"
            "schedule_version=?,revision=revision+1,updated_at=? WHERE id=?",
            (
                review_count,
                streak,
                lapse_count,
                today,
                next_review,
                status,
                transition.review_stage,
                transition.review_cycle,
                SCHEDULE_VERSION,
                now.isoformat(timespec='seconds'),
                mid,
            ),
        )
        db.execute(
            "INSERT INTO review_events(mistake_id,event_type,reviewed_at,"
            "status_after,correct_streak,stage_before,stage_after,cycle_after,"
            "next_review_after,schedule_version) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                mid,
                result,
                now.isoformat(timespec='seconds'),
                status,
                streak,
                row['review_stage'],
                transition.review_stage,
                transition.review_cycle,
                next_review,
                SCHEDULE_VERSION,
            ),
        )
    return jsonify({
        'ok': True,
        'status': status,
        'correct_streak': streak,
        'review_stage': transition.review_stage,
        'review_cycle': transition.review_cycle,
        'next_review': next_review,
    })


def _uuid_value(value, label: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValidationError(f"{label}必须是 UUID")


@bp.route('/api/v2/mistakes/<public_id>/review', methods=['POST', 'PUT'])
def api_v2_review(public_id):
    from ..services.mistakes import review_mistake, MistakeError
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        result = review_mistake(
            get_db(), public_id,
            client_event_id=_uuid_value(data.get('client_event_id'), '客户端事件标识'),
            revision=positive_int(data.get('revision'), '版本号'),
            result=data.get('result'), assistance=data.get('assistance', 'unknown'),
            now=datetime.now().astimezone(),
        )
        return jsonify(result)
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
    except MistakeError as exc:
        return jsonify({'error': str(exc), 'code': exc.code, **exc.details}), exc.status_code


@bp.post('/api/v2/mistakes/<public_id>/reopen')
def api_v2_reopen(public_id):
    from ..services.mistakes import toggle_mistake, MistakeError
    try:
        data = json_object(request)
        return jsonify(toggle_mistake(
            get_db(), _uuid_value(public_id, '错题标识'),
            revision=positive_int(data.get('revision'), '版本号'),
            now=datetime.now().astimezone(), reopen_only=True,
            client_event_id=_uuid_value(data.get('client_event_id'), '客户端事件标识'),
        ))
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
    except MistakeError as exc:
        return jsonify({'error': str(exc), 'code': exc.code, **exc.details}), exc.status_code


@bp.put('/api/v2/mistakes/<public_id>/toggle')
def api_v2_toggle(public_id):
    """Manually mark a mistake mastered or reopen it with revision protection."""
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        revision = positive_int(data.get('revision'), '版本号')
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400

    db = get_db()
    db.commit()
    try:
        begin_immediate(db)
        row = db.execute(
            "SELECT * FROM mistakes WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            db.rollback()
            return jsonify({'error': '错题不存在', 'code': 'NOT_FOUND'}), 404
        if row['revision'] != revision:
            db.rollback()
            return jsonify({
                'error': '错题已被其他操作更新', 'code': 'REVISION_CONFLICT',
                'current_revision': row['revision'],
            }), 409

        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        if row['status'] == '未掌握':
            status = '已掌握'
            streak = row['correct_streak'] or 0
            stage = row['review_stage'] or 0
            cycle = row['review_cycle'] or 1
            next_review = None
            event_type = 'manual_master'
        else:
            transition = reopen_review(
                review_cycle=row['review_cycle'], reopened_on=today
            )
            status = transition.status
            streak = transition.correct_streak
            stage = transition.review_stage
            cycle = transition.review_cycle
            next_review = transition.next_review
            event_type = 'manual_reopen'
        timestamp = now.isoformat(timespec='seconds')
        changed = db.execute(
            "UPDATE mistakes SET status=?,correct_streak=?,next_review=?,review_stage=?,"
            "review_cycle=?,schedule_version=?,revision=revision+1,updated_at=? "
            "WHERE id=? AND revision=?",
            (status, streak, next_review, stage, cycle, SCHEDULE_VERSION,
             timestamp, row['id'], revision),
        )
        if changed.rowcount != 1:
            db.rollback()
            return jsonify({
                'error': '错题已被其他操作更新', 'code': 'REVISION_CONFLICT'
            }), 409
        event_public_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO review_events(public_id,mistake_id,event_type,reviewed_at,"
            "created_at,status_after,correct_streak,lapse_count_after,stage_before,"
            "stage_after,cycle_after,next_review_after,schedule_version) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_public_id, row['id'], event_type, timestamp, timestamp, status,
             streak, row['lapse_count'] or 0, row['review_stage'], stage, cycle,
             next_review, SCHEDULE_VERSION),
        )
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return jsonify({
        'ok': True, 'public_id': public_id, 'revision': revision + 1,
        'event_public_id': event_public_id, 'event_type': event_type,
        'status': status, 'correct_streak': streak, 'review_stage': stage,
        'review_cycle': cycle, 'next_review': next_review,
    })


@bp.put('/api/v2/mistakes/<public_id>/reasons')
def api_v2_update_reasons(public_id):
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        revision = positive_int(data.get('revision'), '版本号')
        reasons = reasons_value(data.get('reasons', []))
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
    db = get_db()
    now = datetime.now().isoformat(timespec='seconds')
    with db:
        row = db.execute(
            "SELECT id,revision FROM mistakes WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            return jsonify({'error': '错题不存在', 'code': 'NOT_FOUND'}), 404
        if row['revision'] != revision:
            return jsonify({'error': '错题已被其他操作更新', 'code': 'REVISION_CONFLICT', 'current_revision': row['revision']}), 409
        db.execute(
            "UPDATE mistakes SET reasons=?,revision=revision+1,updated_at=? WHERE id=?",
            (reasons, now, row['id']),
        )
        _sync_reason_items(db, row['id'], reasons)
    return jsonify({'ok': True, 'public_id': public_id, 'revision': revision + 1, 'reasons': _reason_array(reasons)})


@bp.patch('/api/v2/mistakes/<public_id>')
def api_v2_update_mistake(public_id):
    """Update the user-editable note without exposing review state fields."""
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        unexpected = sorted(set(data) - {'revision', 'note'})
        if unexpected:
            raise ValidationError(f"不支持修改字段：{unexpected[0]}")
        revision = positive_int(data.get('revision'), '版本号')
        note = text_value(data.get('note'), '备注', maximum=20_000)
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400

    db = get_db()
    db.commit()
    try:
        begin_immediate(db)
        row = db.execute(
            "SELECT revision,note FROM mistakes "
            "WHERE public_id=? AND deleted_at IS NULL",
            (public_id,),
        ).fetchone()
        if not row:
            db.rollback()
            return jsonify({'error': '错题不存在', 'code': 'NOT_FOUND'}), 404
        now = datetime.now().isoformat(timespec='seconds')
        changed = db.execute(
            "UPDATE mistakes SET note=?,revision=revision+1,updated_at=? "
            "WHERE public_id=? AND revision=? AND deleted_at IS NULL",
            (note, now, public_id, revision),
        )
        if changed.rowcount != 1:
            latest = db.execute(
                "SELECT revision,note FROM mistakes "
                "WHERE public_id=? AND deleted_at IS NULL",
                (public_id,),
            ).fetchone()
            db.rollback()
            if not latest:
                return jsonify({'error': '错题不存在', 'code': 'NOT_FOUND'}), 404
            return jsonify({
                'error': '此题已在其他页面更新',
                'code': 'REVISION_CONFLICT',
                'current_revision': latest['revision'],
                'current_note': latest['note'] or '',
            }), 409
        db.commit()
    except sqlite3.Error:
        db.rollback()
        raise
    return jsonify({
        'ok': True,
        'public_id': public_id,
        'revision': revision + 1,
        'note': note,
    })


@bp.delete('/api/v2/mistakes/<public_id>')
def api_v2_delete(public_id):
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        revision = positive_int(data.get('revision'), '版本号')
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
    db = get_db()
    now = datetime.now().isoformat(timespec='seconds')
    changed = db.execute(
        "UPDATE mistakes SET deleted_at=?,revision=revision+1,updated_at=? "
        "WHERE public_id=? AND revision=? AND deleted_at IS NULL",
        (now, now, public_id, revision),
    )
    db.commit()
    if changed.rowcount != 1:
        row = db.execute("SELECT revision FROM mistakes WHERE public_id=?", (public_id,)).fetchone()
        if not row:
            return jsonify({'error': '错题不存在', 'code': 'NOT_FOUND'}), 404
        return jsonify({'error': '错题已被其他操作更新', 'code': 'REVISION_CONFLICT', 'current_revision': row['revision']}), 409
    return jsonify({'ok': True, 'public_id': public_id, 'revision': revision + 1, 'deleted_at': now})


@bp.post('/api/v2/mistakes/<public_id>/restore')
def api_v2_restore(public_id):
    try:
        public_id = _uuid_value(public_id, '错题标识')
        data = json_object(request)
        revision = positive_int(data.get('revision'), '版本号')
    except ValidationError as exc:
        return jsonify({'error': str(exc), 'code': 'VALIDATION'}), 400
    db = get_db()
    now = datetime.now().isoformat(timespec='seconds')
    changed = db.execute(
        "UPDATE mistakes SET deleted_at=NULL,revision=revision+1,updated_at=? "
        "WHERE public_id=? AND revision=? AND deleted_at IS NOT NULL",
        (now, public_id, revision),
    )
    db.commit()
    if changed.rowcount != 1:
        return jsonify({'error': '回收站记录不存在或版本冲突', 'code': 'REVISION_CONFLICT'}), 409
    return jsonify({'ok': True, 'public_id': public_id, 'revision': revision + 1})


@bp.get('/api/v2/trash/mistakes')
def api_v2_trash():
    rows = get_db().execute(
        "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
        "LEFT JOIN subjects s ON s.id=m.subject_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id "
        "WHERE m.deleted_at IS NOT NULL ORDER BY m.deleted_at DESC LIMIT 200"
    ).fetchall()
    return jsonify({'items': [_v2_mistake(row) for row in rows]})

@bp.get('/internal/legacy/stats')
def api_stats():
    db = get_db()
    total = db.execute("SELECT COUNT(*) as n FROM mistakes WHERE deleted_at IS NULL").fetchone()['n']
    active = db.execute("SELECT COUNT(*) as n FROM mistakes WHERE status='未掌握' AND deleted_at IS NULL").fetchone()['n']
    mastered = db.execute("SELECT COUNT(*) as n FROM mistakes WHERE status='已掌握' AND deleted_at IS NULL").fetchone()['n']
    today = datetime.now().strftime('%Y-%m-%d')
    tr = db.execute("SELECT COUNT(*) as n FROM mistakes WHERE (next_review<=? OR next_review IS NULL) AND status='未掌握' AND deleted_at IS NULL",(today,)).fetchone()['n']
    wa = (datetime.now()-timedelta(days=7)).strftime('%Y-%m-%d')
    wn = db.execute("SELECT COUNT(*) as n FROM mistakes WHERE date_added>=? AND deleted_at IS NULL",(wa,)).fetchone()['n']
    chs = [dict(r) for r in db.execute("SELECT c.name,COUNT(*) as cnt FROM mistakes m JOIN chapters c ON m.chapter_id=c.id WHERE m.status='未掌握' AND m.deleted_at IS NULL GROUP BY c.name ORDER BY cnt DESC LIMIT 8").fetchall()]
    reason_sql = "SELECT " + ",".join(
        f"SUM(CASE WHEN instr(','||reasons||',', ?) > 0 THEN 1 ELSE 0 END) AS r{i}"
        for i in range(len(MISTAKE_REASONS))
    ) + " FROM mistakes WHERE status='未掌握' AND deleted_at IS NULL"
    reason_row = db.execute(
        reason_sql, [f",{reason}," for reason in MISTAKE_REASONS]
    ).fetchone()
    reasons = [
        {"name": reason, "cnt": reason_row[f"r{i}"] or 0}
        for i, reason in enumerate(MISTAKE_REASONS)
        if reason_row[f"r{i}"]
    ]
    reasons.sort(key=lambda item: -item["cnt"])
    # Trend is event-based so reopening a mistake does not erase past mastery.
    trend_start = (datetime.now() - timedelta(days=29)).strftime("%Y-%m-%d")
    trend_rows = db.execute(
        "SELECT substr(reviewed_at,1,10) AS day,COUNT(*) AS n "
        "FROM review_events e JOIN mistakes m ON m.id=e.mistake_id "
        "WHERE status_after='已掌握' AND m.deleted_at IS NULL "
        "AND reviewed_at>=? GROUP BY substr(reviewed_at,1,10)",
        (trend_start,),
    ).fetchall()
    trend_map = {row["day"]: row["n"] for row in trend_rows}
    trend = []
    for i in range(29, -1, -1):
        day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append({"day": day[5:], "cnt": trend_map.get(day, 0)})
    recent = [dict(r) for r in db.execute("SELECT m.*,s.name as subject FROM mistakes m LEFT JOIN subjects s ON m.subject_id=s.id WHERE m.deleted_at IS NULL ORDER BY m.id DESC LIMIT 5").fetchall()]
    return jsonify({'total':total,'active':active,'mastered':mastered,'today_review':tr,'week_new':wn,'chapters':chs,'reasons':reasons,'trend':trend,'recent':recent})

@bp.get('/internal/legacy/review-today')
def api_review_today():
    today = datetime.now().strftime('%Y-%m-%d')
    rows = get_db().execute("SELECT m.*,s.name as subject,c.name as chapter FROM mistakes m LEFT JOIN subjects s ON m.subject_id=s.id LEFT JOIN chapters c ON m.chapter_id=c.id WHERE (m.next_review<=? OR m.next_review IS NULL) AND m.status='未掌握' AND m.deleted_at IS NULL ORDER BY m.review_cycle DESC,COALESCE(m.next_review,''),m.id LIMIT 50",(today,)).fetchall()
    return jsonify([dict(r) for r in rows])


@bp.route('/api/v2/reviews/due')
def api_v2_reviews_due():
    db = get_db()
    today = datetime.now().strftime("%Y-%m-%d")
    filters = {"due": today, "status": "未掌握"}
    where = [
        "(m.next_review<=? OR m.next_review IS NULL)",
        "m.status='未掌握'",
        "m.deleted_at IS NULL",
    ]
    where_params = [today]
    summary_where = ["m.deleted_at IS NULL"]
    summary_params = []
    try:
        for key, label in (("subject_id", "科目编号"), ("chapter_id", "章节编号")):
            raw = request.args.get(key)
            if raw not in (None, ""):
                value = positive_int(raw, label)
                filters[key] = value
                where.append(f"m.{key}=?")
                where_params.append(value)
                summary_where.append(f"m.{key}=?")
                summary_params.append(value)
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    try:
        limit = bounded_limit(request.args.get("limit"))
        cursor = decode_cursor(request.args.get("after"), filters=filters)
    except CursorError as exc:
        return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400
    if cursor is None:
        snapshot = db.execute(
            "SELECT COALESCE(MAX(m.id),0) AS n FROM mistakes m WHERE "
            + " AND ".join(where),
            where_params,
        ).fetchone()["n"]
        last_cycle, last_date, last_id = 2147483647, "", 0
    else:
        snapshot = cursor["s"]
        if (
            len(cursor["p"]) != 3
            or not isinstance(cursor["p"][0], int)
            or not isinstance(cursor["p"][1], str)
            or not isinstance(cursor["p"][2], int)
        ):
            return jsonify({"error": "分页游标无效", "code": "INVALID_CURSOR"}), 400
        last_cycle, last_date, last_id = cursor["p"]
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON m.subject_id=s.id "
            "LEFT JOIN chapters c ON m.chapter_id=c.id WHERE m.id<=? "
            "AND " + " AND ".join(where) + " AND "
            "(m.review_cycle<? OR (m.review_cycle=? AND "
            "(COALESCE(m.next_review,'')>? OR "
            "(COALESCE(m.next_review,'')=? AND m.id>?)))) "
            "ORDER BY m.review_cycle DESC,COALESCE(m.next_review,''),m.id LIMIT ?",
            (snapshot, *where_params, last_cycle, last_cycle, last_date,
             last_date, last_id, limit + 1),
        ).fetchall()
    ]
    has_more = len(rows) > limit
    items = rows[:limit]
    counts = db.execute(
        "SELECT SUM(CASE WHEN m.id<=? AND m.status='未掌握' "
        "AND (m.next_review<=? OR m.next_review IS NULL) THEN 1 ELSE 0 END) AS due,"
        "SUM(CASE WHEN m.status='未掌握' THEN 1 ELSE 0 END) AS active,"
        "SUM(CASE WHEN m.status='已掌握' THEN 1 ELSE 0 END) AS mastered "
        "FROM mistakes m WHERE " + " AND ".join(summary_where),
        (snapshot, today, *summary_params),
    ).fetchone()
    total = int(counts["due"] or 0)
    next_cursor = None
    if has_more and items:
        last = items[-1]
        next_cursor = encode_cursor(
            filters=filters,
            snapshot=snapshot,
            values=[last["review_cycle"], last["next_review"] or "", last["id"]],
        )
    return jsonify(
        {
            "items": items,
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total": total,
            "summary": {key: int(counts[key] or 0) for key in ("active", "mastered")},
        }
    )
