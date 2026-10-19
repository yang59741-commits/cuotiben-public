"""Selection, publication, and results for daily self-review sessions."""

from __future__ import annotations

import random
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from ..transactions import begin_immediate
from . import mistake_classification as classification
from . import solution_methods
from .learning_evidence import assistance_value, evidence, remediation_status


ROUND_TABLE = "daily_review_rounds"
ITEM_TABLE = "daily_review_items"

MIN_ITEMS = 5
MAX_ITEMS = 10
MIX_SUBJECTS = ("数学分析", "高等代数")
RESULT_VALUES = frozenset({"correct", "again", "incomplete"})


class DailyReviewError(RuntimeError):
    """Expected daily-review failure with an API-safe status and code."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "DAILY_REVIEW_ERROR",
        status: int = 400,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class DailyReviewEmpty(DailyReviewError):
    def __init__(self) -> None:
        super().__init__(
            "当前没有可复习的活动错题",
            code="DAILY_REVIEW_EMPTY",
            status=404,
        )


class DailyReviewConflict(DailyReviewError):
    def __init__(self, message: str = "每日复习状态已被其他任务更新") -> None:
        super().__init__(message, code="DAILY_REVIEW_CONFLICT", status=409)


@dataclass(frozen=True, slots=True)
class DailyReviewPlan:
    session_id: int
    round_id: int
    round_number: int
    generation_mode: str
    item_ids: tuple[int, ...]
    mistake_ids: tuple[int, ...]
    rows: tuple[dict, ...]
    reserved_at: str
    reused: bool = False
    practice_mode: str = 'mixed'
    selection: dict | None = None


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def _as_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _session_items(db, session_id: int, *, published_only: bool = False) -> list[dict]:
    status_sql = " AND dri.status='published'" if published_only else ""
    items = [
        _as_dict(row)
        for row in db.execute(
            "SELECT dri.*,m.public_id,m.subject_id,m.chapter_id,m.qnum,m.content,"
            "m.note,m.reasons,m.date_added,m.status AS mistake_status,m.deleted_at,m.id IS NULL AS source_missing,"
            "s.name AS subject,c.name AS chapter "
            "FROM daily_review_items dri LEFT JOIN mistakes m ON m.id=dri.mistake_id "
            "LEFT JOIN subjects s ON s.id=m.subject_id "
            "LEFT JOIN chapters c ON c.id=m.chapter_id "
            "WHERE dri.round_id=?" + status_sql + " ORDER BY dri.id",
            (int(session_id),),
        ).fetchall()
    ]
    for item in items:
        if item.get('snapshot_json'):
            item.update(json.loads(item['snapshot_json']))
        else:
            old = db.execute("SELECT ei.* FROM mistake_export_items ei JOIN mistake_exports e ON e.id=ei.export_id "
                "WHERE e.batch_id=? AND ei.mistake_public_id=? ORDER BY ei.position LIMIT 1",
                (item['export_batch_id'], item['public_id'])).fetchone()
            if old:
                for key in ('subject', 'chapter', 'qnum', 'date_added', 'reasons', 'note', 'content'):
                    item[key] = old[key]
            item['legacy_live_content'] = not bool(old)
            item['classification'] = {'status': 'historical_unknown', 'type_name': '', 'primary_type_code': None}
    return items


def _latest_round_number(db) -> int:
    return int(
        db.execute(
            "SELECT COALESCE(MAX(round_number),0) AS n FROM daily_review_rounds WHERE practice_mode='mixed'"
        ).fetchone()["n"]
    )


def _active_candidates(db, round_number: int) -> list[dict]:
    rows = db.execute(
        "SELECT m.*,s.name AS subject,c.name AS chapter "
        "FROM mistakes m LEFT JOIN subjects s ON s.id=m.subject_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id "
        "WHERE m.deleted_at IS NULL AND NOT EXISTS("
        "SELECT 1 FROM daily_review_items dri "
        "WHERE dri.coverage_round=? AND dri.mistake_id=m.id "
        "AND dri.status IN ('reserved','published'))",
        (int(round_number),),
    ).fetchall()
    return [_as_dict(row) for row in rows]


def _round_item_count(db, round_number: int, status: str) -> int:
    return int(
        db.execute(
            "SELECT COUNT(*) AS n FROM daily_review_items "
            "WHERE coverage_round=? AND status=?",
            (int(round_number), status),
        ).fetchone()["n"]
    )


def _sample_size(total: int, rng) -> int:
    """Choose 5-10 while avoiding an unusable 1-4 item tail."""

    if total <= MAX_ITEMS:
        return total
    allowed = [
        size
        for size in range(MIN_ITEMS, MAX_ITEMS + 1)
        if total - size == 0 or total - size >= MIN_ITEMS
    ]
    return int(rng.choice(allowed))


def _ordered_earliest(rows: list[dict], rng) -> list[dict]:
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        by_day.setdefault(str(row.get("date_added") or ""), []).append(row)
    ordered: list[dict] = []
    for day in sorted(by_day):
        same_day = by_day[day]
        rng.shuffle(same_day)
        ordered.extend(same_day)
    return ordered


def _ensure_subject_mix(
    selected: list[dict],
    ordered_candidates: list[dict],
) -> list[dict]:
    if len(selected) < 2:
        return selected
    available = {
        name
        for name in MIX_SUBJECTS
        if any(str(row.get("subject") or "") == name for row in ordered_candidates)
    }
    if len(available) != len(MIX_SUBJECTS):
        return selected
    present = {str(row.get("subject") or "") for row in selected}
    result = list(selected)
    for missing in (name for name in MIX_SUBJECTS if name not in present):
        replacement = next(
            (
                row
                for row in ordered_candidates
                if str(row.get("subject") or "") == missing and row not in result
            ),
            None,
        )
        if replacement is None:
            continue
        removable = next(
            (
                index
                for index in range(len(result) - 1, -1, -1)
                if sum(
                    str(item.get("subject") or "")
                    == str(result[index].get("subject") or "")
                    for item in result
                )
                > 1
            ),
            len(result) - 1,
        )
        result[removable] = replacement
        present.add(missing)
    return result


def choose_mistakes(
    candidates: Iterable[Mapping],
    *,
    generation_mode: str,
    rng=None,
    count: int | None = None,
) -> list[dict]:
    """Choose one session using the round's ordering and mix rules."""

    if generation_mode not in {"earliest", "random"}:
        raise ValueError("每日复习生成模式无效")
    rng = rng or random.SystemRandom()
    rows = [dict(row) for row in candidates]
    if not rows:
        return []
    if count is None:
        selected_count = _sample_size(len(rows), rng)
    else:
        selected_count = min(len(rows), max(MIN_ITEMS, min(MAX_ITEMS, int(count))))
    ordered = _ordered_earliest(rows, rng) if generation_mode == "earliest" else list(rows)
    if generation_mode == "random":
        rng.shuffle(ordered)
    return _ensure_subject_mix(ordered[:selected_count], ordered)


def _plan(db, session, *, reserved_at: str, reused: bool) -> DailyReviewPlan:
    items = _session_items(db, int(session["id"]))
    return DailyReviewPlan(
        session_id=int(session["id"]),
        round_id=int(session["id"]),
        round_number=int(session["round_number"]),
        generation_mode=str(session["generation_mode"]),
        item_ids=tuple(int(item["id"]) for item in items),
        mistake_ids=tuple(int(item["mistake_id"]) for item in items),
        rows=tuple(items),
        reserved_at=reserved_at,
        reused=reused,
        practice_mode=session['practice_mode'],
        selection=json.loads(session['selection_json']),
    )


def validate_selection(db, value=None):
    value = dict(value or {})
    pdf_purpose = value.get('pdf_purpose')
    if pdf_purpose not in (None, 'self_test', 'guided'):
        raise DailyReviewError('PDF 用途无效')
    extra = {'pdf_purpose': pdf_purpose} if pdf_purpose else {}
    if value.get('practice_kind') == 'remediation':
        ids = value.get('source_item_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(type(i) is not int or i < 1 for i in ids) or len(set(ids)) != len(ids):
            raise DailyReviewError('请选择 1 至 10 道待巩固题目')
        return {'practice_mode': 'focused', 'practice_kind': 'remediation', 'source_item_ids': ids, 'count': len(ids), **extra}
    mode = value.get('practice_mode', 'mixed')
    if mode not in {'mixed', 'focused'}:
        raise DailyReviewError('复习模式无效')
    if mode == 'mixed':
        if any(value.get(key) not in (None, '', False) for key in ('primary_type_code', 'method_code', 'method_role', 'chapter_id', 'subject_id', 'include_mastered', 'count')):
            raise DailyReviewError('混合复习使用全库轮次，不接受专项条件')
        return {'practice_mode': 'mixed', **extra}
    try:
        method_code = value.get('method_code')
        if method_code:
            if value.get('primary_type_code'):
                raise ValueError
            subject_id = int(value.get('subject_id'))
            subject = db.execute('SELECT name FROM subjects WHERE id=?', (subject_id,)).fetchone()
            if subject is None:
                raise ValueError
            solution_methods.resolve_method(method_code, subject[0])
            method_role = value.get('method_role', 'primary')
            if method_role not in {'primary', 'any'}:
                raise ValueError
            kind = None
        else:
            kind = classification.resolve_type(db, value.get('primary_type_code'))
            subject_id = db.execute('SELECT id FROM subjects WHERE name=?', (kind['subject'],)).fetchone()[0]
        if value.get('subject_id') and int(value['subject_id']) != subject_id:
            raise ValueError
        chapter_id = int(value['chapter_id']) if value.get('chapter_id') else None
        if chapter_id and not db.execute('SELECT 1 FROM chapters WHERE id=? AND subject_id=?', (chapter_id, subject_id)).fetchone():
            raise ValueError
        count = int(value.get('count', 5))
        if not 5 <= count <= 10 or not isinstance(value.get('include_mastered', False), bool):
            raise ValueError
    except (ValueError, TypeError, classification.ClassificationError, solution_methods.MethodError) as exc:
        raise DailyReviewError('请检查专项题型或方法、科目、章节和题量（5 至 10）') from exc
    target = ({'method_code': method_code, 'method_role': method_role}
              if method_code else {'primary_type_code': kind['code']})
    return {'practice_mode': mode, **target, 'subject_id': subject_id,
            'chapter_id': chapter_id, 'count': count, 'include_mastered': value.get('include_mastered', False), **extra}


def _focused_candidates(db, selection):
    if selection.get('practice_kind') == 'remediation':
        pending = remediation_status(db)['items']
        selected = set(selection['source_item_ids'])
        rows = []
        for item in pending:
            if item['source_item_id'] in selected:
                rows.append(classification.mistake_row(db, item['public_id']))
        return rows
    where = ['m.deleted_at IS NULL']
    if selection.get('method_code'):
        where.append('m.subject_id=?')
        params = [selection['subject_id']]
    else:
        where.append('mc.primary_type_code=?')
        params = [selection['primary_type_code']]
    if not selection['include_mastered']:
        where.append("m.status='未掌握'")
    if selection['chapter_id']:
        where.append('m.chapter_id=?')
        params.append(selection['chapter_id'])
    rows = db.execute("SELECT m.*,s.name AS subject,c.name AS chapter,"
        "(SELECT completion_result FROM daily_review_items WHERE mistake_id=m.id AND completion_result IS NOT NULL ORDER BY completed_at DESC,id DESC LIMIT 1) AS recent_result,"
        "(SELECT assistance FROM daily_review_items WHERE mistake_id=m.id AND completion_result IS NOT NULL ORDER BY completed_at DESC,id DESC LIMIT 1) AS recent_assistance,"
        "(SELECT MAX(completed_at) FROM daily_review_items WHERE mistake_id=m.id) AS recent_at,"
        "(SELECT COUNT(*) FROM daily_review_items WHERE mistake_id=m.id AND completion_result='again' AND completed_at>=datetime('now','-30 days')) AS again_30 "
        "FROM mistakes m JOIN mistake_classifications mc ON mc.mistake_id=m.id "
        "LEFT JOIN subjects s ON s.id=m.subject_id LEFT JOIN chapters c ON c.id=m.chapter_id WHERE " + ' AND '.join(where), params).fetchall()
    candidates = []
    for row in rows:
        typed = classification.get_classification(db, row)
        if typed['status'] not in {'auto', 'manual'}:
            continue
        if selection.get('method_code') and not solution_methods.matches(
                typed, selection['method_code'], selection['method_role'], subject=row['subject']):
            continue
        candidates.append(dict(row))
    order = {'again': 0, 'assisted': 1, 'incomplete': 2, None: 3, 'correct': 4}
    return sorted(candidates, key=lambda row: (
        order['assisted' if row['recent_result'] == 'correct' and row['recent_assistance'] == 'hint' else row['recent_result']],
        -row['again_30'], row['recent_at'] or '', row['date_added'], row['id'],
    ))


def _snapshot(db, row):
    result = {key: row.get(key) for key in ('public_id', 'subject_id', 'chapter_id', 'subject', 'chapter', 'qnum', 'content', 'note', 'reasons', 'date_added')}
    typed = classification.get_classification(db, row)
    result['classification'] = {key: typed.get(key) for key in ('primary_type_code', 'type_name', 'knowledge_points', 'methods', 'method_annotations', 'status', 'revision')}
    if typed['status'] not in {'auto', 'manual'}:
        result['classification']['primary_type_code'] = None
        result['classification']['type_name'] = ''
        result['classification']['knowledge_points'] = []
        result['classification']['methods'] = []
        result['classification']['method_annotations'] = []
    result['snapshot_version'] = 1
    return result


def reserve_daily_review(
    db,
    *,
    source_job_id: str | None = None,
    now: datetime | None = None,
    rng=None,
    count: int | None = None,
    selection: dict | None = None,
) -> DailyReviewPlan:
    """Atomically create a session and reserve its mistakes."""

    owned = not db.in_transaction
    if owned:
        begin_immediate(db)
    reserved_at = utc_timestamp(now)
    local_job_id = source_job_id or "local-" + uuid.uuid4().hex
    try:
        selection = validate_selection(db, selection)
        existing = None
        if source_job_id:
            existing = db.execute(
                "SELECT * FROM daily_review_rounds WHERE source_job_id=?",
                (source_job_id,),
            ).fetchone()
            if existing is not None and _session_items(db, int(existing["id"])):
                if owned:
                    db.commit()
                return _plan(db, existing, reserved_at=reserved_at, reused=True)

        focused = selection['practice_mode'] == 'focused'
        round_number = (db.execute("SELECT COALESCE(MAX(round_number),0)+1 FROM daily_review_rounds WHERE practice_mode='focused'").fetchone()[0]
                        if focused else (_latest_round_number(db) or 1))
        candidates = _focused_candidates(db, selection) if focused else _active_candidates(db, round_number)
        if not candidates and not focused:
            if _round_item_count(db, round_number, "reserved"):
                raise DailyReviewConflict("当前轮次仍有 PDF 正在生成")
            if _round_item_count(db, round_number, "published"):
                round_number += 1
                candidates = _active_candidates(db, round_number)
        if not candidates:
            raise DailyReviewEmpty()

        mode = "earliest" if round_number == 1 else "random"
        selected = candidates[:selection['count']] if focused else choose_mistakes(
            candidates,
            generation_mode=mode,
            rng=rng,
            count=count,
        )
        if existing is None:
            cursor = db.execute(
                "INSERT INTO daily_review_rounds(round_number,generation_mode,status,"
                "created_at,source_job_id,item_count,practice_mode,selection_json) VALUES(?,?, 'generating',?,?,?,?,?)",
                (round_number, mode, reserved_at, source_job_id, len(selected), selection['practice_mode'], json.dumps(selection)),
            )
            session_id = int(cursor.lastrowid)
        else:
            session_id = int(existing["id"])
            db.execute(
                "UPDATE daily_review_rounds SET round_number=?,generation_mode=?,"
                "status='generating',created_at=?,completed_at=NULL,published_at=NULL,"
                "item_count=?,practice_mode=?,selection_json=? WHERE id=?",
                (round_number, mode, reserved_at, len(selected), selection['practice_mode'], json.dumps(selection), session_id),
            )
        db.executemany(
            "INSERT INTO daily_review_items(round_id,round_number,mistake_id,"
            "reserved_at,status,source_job_id,created_at,coverage_round,snapshot_json) "
            "VALUES(?,?,?,?,'reserved',?,?,?,?)",
            [
                (
                    session_id,
                    round_number,
                    int(row["id"]),
                    reserved_at,
                    local_job_id,
                    reserved_at,
                    None if focused else round_number,
                    json.dumps(_snapshot(db, row), ensure_ascii=False),
                )
                for row in selected
            ],
        )
        session = db.execute(
            "SELECT * FROM daily_review_rounds WHERE id=?", (session_id,)
        ).fetchone()
        if owned:
            db.commit()
        return _plan(db, session, reserved_at=reserved_at, reused=False)
    except sqlite3.IntegrityError as exc:
        if owned and db.in_transaction:
            db.rollback()
        raise DailyReviewConflict("同轮题目已被其他生成任务占用") from exc
    except BaseException:
        if owned and db.in_transaction:
            db.rollback()
        raise


def attach_export_batch(
    db,
    round_id: int,
    batch_id: int,
    *,
    source_job_id: str | None = None,
    now: datetime | None = None,
) -> None:
    del source_job_id, now
    changed = db.execute(
        "UPDATE daily_review_items SET export_batch_id=? "
        "WHERE round_id=? AND status='reserved'",
        (int(batch_id), int(round_id)),
    )
    if changed.rowcount < 1:
        raise DailyReviewConflict("每日复习占位已经失效")


def publish_daily_review(
    db,
    round_id: int,
    batch_id: int,
    *,
    source_job_id: str | None = None,
    now: datetime | None = None,
) -> None:
    del source_job_id
    published_at = utc_timestamp(now)
    changed = db.execute(
        "UPDATE daily_review_items SET status='published',export_batch_id=? "
        "WHERE round_id=? AND status='reserved'",
        (int(batch_id), int(round_id)),
    )
    if changed.rowcount < 1:
        raise DailyReviewConflict("每日复习占位已经失效")
    db.execute(
        "UPDATE daily_review_rounds SET status='published',published_at=?,"
        "completed_at=NULL,item_count=? WHERE id=?",
        (published_at, changed.rowcount, int(round_id)),
    )


def release_daily_review(
    db,
    round_id: int,
    *,
    state: str = "failed",
    source_job_id: str | None = None,
    now: datetime | None = None,
) -> None:
    del source_job_id
    if state not in {"failed", "cancelled"}:
        raise ValueError("每日复习释放状态无效")
    db.execute(
        "DELETE FROM daily_review_items WHERE round_id=? AND status='reserved'",
        (int(round_id),),
    )
    db.execute(
        "UPDATE daily_review_rounds SET status=?,completed_at=?,item_count=0 "
        "WHERE id=? AND status='generating'",
        (state, utc_timestamp(now), int(round_id)),
    )


def _serialize_item(item: Mapping) -> dict:
    item = dict(item)
    return {
        "id": int(item["id"]),
        "round_id": int(item["round_id"]),
        "session_id": int(item["round_id"]),
        "round_number": int(item["round_number"]),
        "mistake_id": int(item["mistake_id"]),
        "public_id": item.get("public_id"),
        "subject_id": item.get("subject_id"),
        "chapter_id": item.get("chapter_id"),
        "subject": item.get("subject") or "",
        "chapter": item.get("chapter") or "",
        "qnum": item.get("qnum") or "",
        "content": item.get("content") or "",
        "note": item.get("note") or "",
        "reasons": [
            part.strip()
            for part in str(item.get("reasons") or "").split(",")
            if part.strip()
        ],
        "date_added": item.get("date_added") or "",
        "item_status": item.get("status") or "",
        "result": item.get("completion_result"),
        "completion_result": item.get("completion_result"),
        "assistance": item.get('assistance', 'unknown'),
        "evidence_version": item.get('evidence_version', 1),
        "reviewed_on": item.get('reviewed_on'),
        "completed_at": item.get("completed_at"),
        "export_batch_id": item.get("export_batch_id"),
        "reserved_at": item.get("reserved_at"),
        "mistake_deleted": item.get("deleted_at") is not None,
        "read_only": bool(item.get("source_missing")),
        "classification": item.get('classification', {'status': 'historical_unknown'}),
        "legacy_live_content": item.get('legacy_live_content', False),
    }


def serialize_session(db, session, *, subject_id: int | None = None, filters=None) -> dict:
    items = _session_items(db, int(session["id"]), published_only=True)
    all_count = len(items)
    if subject_id is not None:
        items = [
            item
            for item in items
            if int(item.get("subject_id") or 0) == int(subject_id)
        ]
    filters = filters or {}
    for key in ('chapter_id', 'primary_type_code', 'method_code', 'result', 'classification_status'):
        selected = filters.get(key)
        if selected in (None, ''):
            continue
        def matches(item):
            if key == 'method_code':
                typed = item.get('classification') or {}
                return typed.get('status') in {'auto', 'manual'} and solution_methods.matches(
                    typed, selected, subject=item.get('subject'))
            if key == 'primary_type_code':
                return item.get('classification', {}).get(key) == selected
            if key == 'classification_status':
                return item.get('classification', {}).get('status') == selected
            if key == 'result':
                return (item.get('completion_result') or 'pending') == selected
            return str(item.get(key) or '') == str(selected)
        items = [item for item in items if matches(item)]
    completed = sum(item.get("completion_result") is not None for item in items)
    return {
        "id": int(session["id"]),
        "round_id": int(session["id"]),
        "session_id": int(session["id"]),
        "round_number": int(session["round_number"]),
        "generation_mode": session["generation_mode"],
        "mode": session["generation_mode"],
        "practice_mode": session['practice_mode'],
        "selection": json.loads(session['selection_json']),
        "all_item_count": all_count,
        "result_counts": {key: sum((item.get('completion_result') or 'pending') == key for item in items) for key in ('correct', 'again', 'incomplete', 'pending')},
        "status": session["status"],
        "created_at": session["created_at"],
        "completed_at": session["completed_at"],
        "published_at": session["published_at"],
        "item_count": len(items),
        "completed_count": completed,
        "remaining_count": max(0, len(items) - completed),
        "items": [_serialize_item(item) for item in items],
    }


def current_round(db, *, subject_id: int | None = None, filters=None) -> dict | None:
    session = db.execute(
        "SELECT * FROM daily_review_rounds "
        "WHERE status IN ('published','completed') "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return (
        serialize_session(db, session, subject_id=subject_id, filters=filters)
        if session is not None
        else None
    )


def get_round(db, round_id: int, *, subject_id: int | None = None, filters=None) -> dict | None:
    session = db.execute(
        "SELECT * FROM daily_review_rounds WHERE id=?", (int(round_id),)
    ).fetchone()
    return (
        serialize_session(db, session, subject_id=subject_id, filters=filters)
        if session is not None
        else None
    )


def load_daily_review_pdf_plan(db, session_id: int) -> DailyReviewPlan:
    """Read a published session's frozen questions without reserving new ones."""
    session = db.execute(
        "SELECT * FROM daily_review_rounds WHERE id=?", (session_id,)
    ).fetchone()
    if session is None:
        raise DailyReviewError("每日复习批次不存在", code="ROUND_NOT_FOUND", status=404)
    if session["status"] not in {"published", "completed"}:
        raise DailyReviewConflict("仅已发布或已完成的批次可以重新导出 PDF")
    items = db.execute(
        "SELECT * FROM daily_review_items WHERE round_id=? AND status='published' ORDER BY id",
        (session_id,),
    ).fetchall()
    if not items or len(items) != session["item_count"]:
        raise DailyReviewError(
            "历史批次题目已不完整，无法重新导出完整 PDF",
            code="DAILY_REVIEW_SNAPSHOT_UNAVAILABLE", status=409,
        )
    rows = []
    for item in items:
        try:
            snapshot = json.loads(item["snapshot_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            snapshot = None
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("content"), str):
            raise DailyReviewError(
                "历史题目快照缺失，无法重新导出 PDF",
                code="DAILY_REVIEW_SNAPSHOT_UNAVAILABLE", status=409,
            )
        rows.append({**snapshot, "id": int(item["id"]), "mistake_id": int(item["mistake_id"])})
    return DailyReviewPlan(
        session_id=session_id, round_id=session_id, round_number=int(session["round_number"]),
        generation_mode=session["generation_mode"], item_ids=tuple(int(item["id"]) for item in items),
        mistake_ids=tuple(int(item["mistake_id"]) for item in items), rows=tuple(rows),
        reserved_at=session["created_at"], reused=True, practice_mode=session["practice_mode"],
        selection=json.loads(session["selection_json"]),
    )


def list_rounds(db, *, limit: int = 20) -> list[dict]:
    bounded = max(1, min(100, int(limit)))
    sessions = db.execute(
        "SELECT * FROM daily_review_rounds ORDER BY id DESC LIMIT ?",
        (bounded,),
    ).fetchall()
    return [serialize_session(db, session) for session in sessions]


def history_page(db, *, limit=20, after=None):
    args = []
    where = "WHERE r.status IN ('published','completed')"
    if after:
        where += ' AND r.id<?'
        args.append(int(after))
    rows = [dict(row) for row in db.execute("SELECT r.*,"
        "(SELECT COUNT(*) FROM daily_review_items i WHERE i.round_id=r.id AND i.completion_result IS NOT NULL) AS completed_count "
        "FROM daily_review_rounds r " + where + ' ORDER BY r.id DESC LIMIT ?', (*args, limit + 1))]
    total = db.execute("SELECT COUNT(*) FROM daily_review_rounds WHERE status IN ('published','completed')").fetchone()[0]
    for row in rows:
        row['selection'] = json.loads(row['selection_json'])
    return {'items': rows[:limit], 'total': total, 'has_more': len(rows) > limit,
            'next_cursor': str(rows[limit - 1]['id']) if len(rows) > limit else None}


def _summary_rows(db, *, session_id=None, subject_id=None, chapter_id=None):
    where = "i.status='published'"
    args = []
    if session_id:
        where += ' AND i.round_id=?'
        args.append(session_id)
    else:
        where += " AND COALESCE(i.completed_at,r.created_at)>=datetime('now','-30 days')"
    for field, value in (('subject_id', subject_id), ('chapter_id', chapter_id)):
        if value:
            where += f" AND json_extract(i.snapshot_json,'$.{field}')=?"
            args.append(int(value))
    return db.execute("SELECT i.*,r.practice_mode,r.selection_json,m.content AS current_content "
                      "FROM daily_review_items i "
                      "JOIN daily_review_rounds r ON r.id=i.round_id "
                      "LEFT JOIN mistakes m ON m.id=i.mistake_id WHERE " + where +
                      " ORDER BY i.completed_at,i.id", args).fetchall()


def type_summary(db, *, session_id=None, subject_id=None, chapter_id=None, recovery=None):
    rows = _summary_rows(db, session_id=session_id, subject_id=subject_id, chapter_id=chapter_id)
    groups = {}
    for raw in rows:
        item = dict(raw)
        snapshot = json.loads(item['snapshot_json'] or '{}')
        typed = snapshot.get('classification') or {}
        selection = json.loads(item['selection_json'])
        mode = 'remediation' if selection.get('practice_kind') == 'remediation' else item['practice_mode']
        key = (mode, typed.get('primary_type_code'))
        group = groups.setdefault(key, {
            'practice_mode': mode, 'primary_type_code': key[1],
            'type_name': typed.get('type_name') or '未记录分类',
            'correct': 0, 'again': 0, 'incomplete': 0, 'pending': 0,
            'assisted_count': 0, 'unknown_count': 0, '_ids': set(), '_independent': {},
        })
        result = item['completion_result'] or 'pending'
        group[result] += 1
        group['_ids'].add(item['mistake_id'])
        if result in ('correct', 'again'):
            group['assisted_count'] += item['assistance'] == 'hint'
            group['unknown_count'] += item['assistance'] == 'unknown'
            if item['assistance'] == 'none' and item['evidence_version'] == 2:
                group['_independent'][item['mistake_id']] = result
    if recovery is None:
        recovery = remediation_status(db)
    pending_ids = {item['mistake_id'] for item in recovery['items']}
    recovered_ids = set(recovery['recovered_mistake_ids'])
    for group in groups.values():
        ids, independent = group.pop('_ids'), group.pop('_independent')
        group['sample_count'] = group['correct'] + group['again']
        group['accuracy'] = round(group['correct'] / group['sample_count'], 4) if group['sample_count'] >= 5 else None
        group['unique_mistake_count'] = len(ids)
        group['independent_sample_count'] = len(independent)
        group['independent_accuracy'] = round(sum(value == 'correct' for value in independent.values()) / len(independent), 4) if len(independent) >= 5 else None
        total = sum(group[key] for key in ('correct', 'again', 'incomplete', 'pending'))
        group['completion_rate'] = round(group['sample_count'] / total, 4) if total else None
        group['remediation_count'] = len(ids & pending_ids)
        group['recovered_count'] = len(ids & recovered_ids)
    return sorted(groups.values(), key=lambda row: (row['practice_mode'], row['type_name']))


def method_summary(db, *, session_id=None, subject_id=None, chapter_id=None):
    from .exam_repetition_policy import question_content_fingerprint

    rows = _summary_rows(db, session_id=session_id, subject_id=subject_id, chapter_id=chapter_id)
    catalog = {method['code']: method for method in solution_methods.list_methods()}
    groups, unattributed, current_fingerprints = {}, 0, {}
    recovery = remediation_status(db)
    pending_ids = {item['mistake_id'] for item in recovery['items']}
    recovered_ids = set(recovery['recovered_mistake_ids'])
    for raw in rows:
        item = dict(raw)
        snapshot = json.loads(item['snapshot_json'] or '{}')
        typed = snapshot.get('classification') or {}
        primary = [annotation for annotation in typed.get('method_annotations') or []
                   if annotation.get('role') == 'primary' and annotation.get('code') in catalog]
        if typed.get('status') not in {'auto', 'manual'} or len(primary) != 1:
            unattributed += 1
            continue
        annotation = primary[0]
        method = catalog[annotation['code']]
        if snapshot.get('subject') not in method['subjects']:
            unattributed += 1
            continue
        selection = json.loads(item['selection_json'])
        mode = 'remediation' if selection.get('practice_kind') == 'remediation' else item['practice_mode']
        key = (snapshot.get('subject_id'), method['code'], mode)
        group = groups.setdefault(key, {
            'subject_id': key[0], 'subject': snapshot.get('subject') or '',
            'method_code': method['code'], 'method_name': annotation.get('name') or method['name'],
            'practice_mode': mode, 'correct': 0, 'again': 0, 'incomplete': 0, 'pending': 0,
            'assisted_count': 0, 'unknown_count': 0,
            '_ids': set(), '_independent': {}, '_current_ids': set(),
        })
        result = item['completion_result'] or 'pending'
        group[result] += 1
        identity = snapshot.get('public_id') or item['mistake_id']
        if identity is not None:
            group['_ids'].add(identity)
        if result in {'correct', 'again'}:
            group['assisted_count'] += result == 'correct' and item['assistance'] == 'hint'
            group['unknown_count'] += item['assistance'] == 'unknown'
            if identity is not None and item['assistance'] == 'none' and item['evidence_version'] == 2:
                group['_independent'][identity] = result
        if item['current_content'] is not None:
            mid = item['mistake_id']
            if mid not in current_fingerprints:
                current_fingerprints[mid] = question_content_fingerprint(item['current_content'])
            if item['content_fingerprint'] == current_fingerprints[mid]:
                group['_current_ids'].add(mid)
    for group in groups.values():
        ids, independent = group.pop('_ids'), group.pop('_independent')
        current_ids = group.pop('_current_ids')
        group['unique_mistake_count'] = len(ids)
        group['independent_sample_count'] = len(independent)
        group['independent_accuracy'] = (round(sum(value == 'correct' for value in independent.values()) /
                                              len(independent), 4) if len(independent) >= 5 else None)
        group['remediation_count'] = len(current_ids & pending_ids)
        group['recovered_count'] = len(current_ids & recovered_ids)
    return {'items': sorted(groups.values(), key=lambda row: (row['subject'], row['method_name'], row['practice_mode'])),
            'unattributed_count': unattributed}


def _normalize_result(value: object) -> str:
    text = str(value or "").strip().casefold()
    aliases = {
        "pass": "correct",
        "retry": "again",
        "答对": "correct",
        "再次答错": "again",
        "未完成": "incomplete",
    }
    result = aliases.get(text, text)
    if result not in RESULT_VALUES:
        raise DailyReviewError(
            "每日复习结果必须是 correct、again 或 incomplete",
            code="INVALID_RESULT",
        )
    return result


def record_result(
    db,
    *,
    round_id: int,
    result: str,
    item_id: int | None = None,
    mistake_id: int | None = None,
    idempotency_key: str | None = None,
    now: datetime | None = None,
    assistance: str = 'unknown',
) -> dict:
    normalized = _normalize_result(result)
    assistance = assistance_value(assistance)
    if item_id is None and mistake_id is None:
        raise DailyReviewError("缺少每日复习题目标识", code="ITEM_REQUIRED")
    owned = not db.in_transaction
    if owned:
        begin_immediate(db)
    try:
        selector = "dri.id=?" if item_id is not None else "dri.mistake_id=?"
        selector_value = int(item_id if item_id is not None else mistake_id)
        row = db.execute(
            "SELECT dri.*,m.public_id,m.subject_id,m.chapter_id,m.qnum,m.content,"
            "m.note,m.reasons,m.date_added,m.status AS mistake_status,m.deleted_at,m.id IS NULL AS source_missing,"
            "s.name AS subject,c.name AS chapter FROM daily_review_items dri "
            "LEFT JOIN mistakes m ON m.id=dri.mistake_id "
            "LEFT JOIN subjects s ON s.id=m.subject_id "
            "LEFT JOIN chapters c ON c.id=m.chapter_id "
            f"WHERE {selector} AND dri.round_id=?",
            (selector_value, int(round_id)),
        ).fetchone()
        if row is None:
            raise DailyReviewError(
                "每日复习题目不存在", code="ITEM_NOT_FOUND", status=404
            )
        if row["status"] != "published":
            raise DailyReviewConflict("题目尚未发布，暂不能记录结果")
        if row["source_missing"]:
            raise DailyReviewError(
                "原题已被替换或移除，此批次仅保留历史记录",
                code="DAILY_REVIEW_READ_ONLY", status=409,
            )
        if idempotency_key:
            previous = db.execute(
                "SELECT id FROM daily_review_items WHERE result_key=?",
                (idempotency_key,),
            ).fetchone()
            if previous is not None and int(previous["id"]) != int(row["id"]):
                raise DailyReviewConflict("结果幂等键已用于其他题目")
        if row["completion_result"] is not None:
            if row["completion_result"] != normalized or row['assistance'] != assistance:
                raise DailyReviewConflict("这道题已经提交过不同的结果")
            payload = _serialize_item(row)
            payload.update({"ok": True, "replayed": True})
            if owned:
                db.commit()
            return payload

        completed_at = utc_timestamp(now)
        snapshot = json.loads(row['snapshot_json']) if row['snapshot_json'] else dict(row)
        proof = evidence(snapshot.get('content', ''), assistance, now)
        changed = db.execute(
            "UPDATE daily_review_items SET completion_result=?,completed_at=?,"
            "result_key=?,assistance=?,evidence_version=?,reviewed_on=?,content_fingerprint=? WHERE id=? AND completion_result IS NULL",
            (normalized, completed_at, idempotency_key, *proof.values(), int(row["id"])),
        )
        if changed.rowcount != 1:
            raise DailyReviewConflict()
        remaining = int(
            db.execute(
                "SELECT COUNT(*) AS n FROM daily_review_items "
                "WHERE round_id=? AND status='published' AND completion_result IS NULL",
                (int(round_id),),
            ).fetchone()["n"]
        )
        if remaining == 0:
            db.execute(
                "UPDATE daily_review_rounds SET status='completed',completed_at=? "
                "WHERE id=? AND status='published'",
                (completed_at, int(round_id)),
            )
        fresh = db.execute(
            "SELECT dri.*,m.public_id,m.subject_id,m.chapter_id,m.qnum,m.content,"
            "m.note,m.reasons,m.date_added,m.status AS mistake_status,m.deleted_at,"
            "s.name AS subject,c.name AS chapter FROM daily_review_items dri "
            "JOIN mistakes m ON m.id=dri.mistake_id "
            "LEFT JOIN subjects s ON s.id=m.subject_id "
            "LEFT JOIN chapters c ON c.id=m.chapter_id WHERE dri.id=?",
            (int(row["id"]),),
        ).fetchone()
        payload = _serialize_item(fresh)
        payload.update({"ok": True, "replayed": False})
        if owned:
            db.commit()
        return payload
    except sqlite3.IntegrityError as exc:
        if owned and db.in_transaction:
            db.rollback()
        raise DailyReviewConflict("结果幂等键已用于其他题目") from exc
    except BaseException:
        if owned and db.in_transaction:
            db.rollback()
        raise


def recommendations(
    db,
    *,
    limit: int = 10,
    subject_id: int | None = None,
    chapter_id: int | None = None,
) -> dict:
    bounded = max(MIN_ITEMS, min(MAX_ITEMS, int(limit)))
    pending = [item for item in remediation_status(db)['items']
               if item['status'] == '未掌握'
               and (subject_id is None or item['subject_id'] == int(subject_id))
               and (chapter_id is None or item['chapter_id'] == int(chapter_id))][:bounded]
    if not pending:
        return {"items": [], "total": 0, "limit": bounded}
    marks = ','.join('?' for _ in pending)
    rows = db.execute(
        "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
        "LEFT JOIN subjects s ON s.id=m.subject_id LEFT JOIN chapters c ON c.id=m.chapter_id "
        f"WHERE m.id IN ({marks})", [item['mistake_id'] for item in pending],
    ).fetchall()
    by_id = {row['id']: row for row in rows}
    # Keep the public counters about actual errors, including focused practice.
    counts = {row['mistake_id']: row for row in db.execute(
        "SELECT i.mistake_id,COUNT(*) AS again_count,MAX(i.completed_at) AS last_again_at "
        "FROM daily_review_items i JOIN daily_review_items latest "
        "ON latest.mistake_id=i.mistake_id AND latest.content_fingerprint=i.content_fingerprint "
        f"WHERE latest.id IN ({marks}) AND i.status='published' AND i.evidence_version=2 "
        "AND i.completion_result='again' GROUP BY i.mistake_id",
        [item['source_item_id'] for item in pending],
    )}
    items: list[dict] = []
    for candidate in pending:
        item = _as_dict(by_id[candidate['mistake_id']])
        count = counts.get(candidate['mistake_id'])
        item["again_count"] = int(count['again_count']) if count else 0
        item["last_again_at"] = count['last_again_at'] if count else None
        item["repeated_again_count"] = item["again_count"]
        item["reasons"] = [
            part.strip()
            for part in str(item.get("reasons") or "").split(",")
            if part.strip()
        ]
        item.pop("answer", None)
        item.pop("solution", None)
        items.append(item)
    return {"items": items, "total": len(items), "limit": bounded}


__all__ = [
    "DailyReviewConflict",
    "DailyReviewEmpty",
    "DailyReviewError",
    "DailyReviewPlan",
    "MAX_ITEMS",
    "MIN_ITEMS",
    "attach_export_batch",
    "choose_mistakes",
    "current_round",
    "get_round",
    "list_rounds",
    "publish_daily_review",
    "record_result",
    "recommendations",
    "release_daily_review",
    "reserve_daily_review",
    "serialize_session",
    "utc_timestamp",
]
