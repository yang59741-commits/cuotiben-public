"""User reports attached to immutable exam item snapshots."""

import json
import uuid
from datetime import datetime, timezone

from ..pagination import CursorError, decode_cursor, encode_cursor
from ..transactions import immediate_transaction

CATEGORIES = frozenset({'formula', 'condition', 'merged', 'classification', 'other'})
STATUSES = frozenset({'open', 'resolved', 'dismissed'})


class IssueError(ValueError):
    def __init__(self, message, *, status=400, code='VALIDATION', current=None):
        super().__init__(message)
        self.status, self.code, self.current = status, code, current


def _text(value, label, *, required=False):
    if not isinstance(value, str):
        raise IssueError(f'{label}必须为文字')
    value = value.strip()
    if len(value) > 2000 or (required and not value):
        raise IssueError(f'{label}须为' + ('1至2000字' if required else '2000字以内'))
    return value


def _serialize(row):
    result = dict(row)
    result.pop('id', None)
    result['snapshot'] = json.loads(result.pop('snapshot_json'))
    result['exam_deleted'] = bool(result.pop('deleted_at', None))
    return result


def _row(db, public_id):
    return db.execute('SELECT q.*,e.deleted_at FROM exam_question_issues q '
                      'JOIN exams e ON e.id=q.exam_id WHERE q.public_id=?', (public_id,)).fetchone()


def create_issue(db, exam_id, body):
    try:
        public_id = str(uuid.UUID(body.get('client_event_id', '')))
    except (ValueError, AttributeError, TypeError) as exc:
        raise IssueError('缺少有效的提交标识') from exc
    category = body.get('category')
    if not isinstance(category, str) or category not in CATEGORIES:
        raise IssueError('请选择问题类别')
    description = _text(body.get('description'), '问题说明', required=True)
    item_id = body.get('exam_item_public_id')
    if not isinstance(item_id, str) or not item_id:
        raise IssueError('请选择题目')
    with immediate_transaction(db):
        previous = _row(db, public_id)
        if previous:
            if (previous['exam_id'], previous['exam_item_public_id'], previous['category'], previous['description']) != (
                    exam_id, item_id, category, description):
                raise IssueError('提交标识已用于其他内容', status=409, code='IDEMPOTENCY_CONFLICT')
            return {**_serialize(previous), 'replayed': True}
        item = db.execute("""SELECT i.*,e.title,s.name AS subject FROM exam_items i
            JOIN exams e ON e.id=i.exam_id LEFT JOIN subjects s ON s.id=e.subject_id
            WHERE i.exam_id=? AND i.public_id=? AND e.artifact_status='published'
            AND e.deleted_at IS NULL""", (exam_id, item_id)).fetchone()
        if item is None:
            raise IssueError('试卷或所选题目不存在，可能已移入回收站', status=404, code='NOT_FOUND')
        snapshot = {key: item[key] for key in ('position', 'question_type', 'content', 'source_kind',
                                              'source_candidate_id', 'source_mistake_public_id', 'subject')}
        snapshot['exam_title'] = item['title']
        snapshot['source_metadata'] = json.loads(item['source_metadata_json'] or '{}')
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        db.execute("""INSERT INTO exam_question_issues(public_id,exam_id,exam_item_public_id,
            category,description,snapshot_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)""",
            (public_id, exam_id, item_id, category, description,
             json.dumps(snapshot, ensure_ascii=False), now, now))
        return {**_serialize(_row(db, public_id)), 'replayed': False}


def list_issues(db, *, exam_id=None, status='', limit=30, after=None):
    if status and status not in STATUSES:
        raise IssueError('问题状态无效')
    filters = {'exam_id': exam_id, 'status': status}
    cursor = decode_cursor(after, filters=filters)
    clauses, params = [], []
    if exam_id is not None:
        clauses.append('q.exam_id=?')
        params.append(exam_id)
    if status:
        clauses.append('q.status=?')
        params.append(status)
    snapshot = (cursor['s'] if cursor else
                db.execute('SELECT COALESCE(MAX(id),0) FROM exam_question_issues').fetchone()[0])
    clauses.append('q.id<=?')
    params.append(snapshot)
    where = ' AND '.join(clauses)
    total = db.execute('SELECT COUNT(*) FROM exam_question_issues q WHERE ' + where, params).fetchone()[0]
    if cursor:
        values = cursor['p']
        if len(values) != 1 or type(values[0]) is not int or not 0 < values[0] <= snapshot:
            raise CursorError('问题列表游标无效')
        where += ' AND q.id<?'
        params.append(values[0])
    rows = db.execute('SELECT q.*,e.deleted_at FROM exam_question_issues q JOIN exams e ON e.id=q.exam_id '
                      'WHERE ' + where + ' ORDER BY q.id DESC LIMIT ?', (*params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    return {'items': [_serialize(row) for row in rows], 'total': total, 'has_more': more,
            'next_cursor': encode_cursor(filters=filters, snapshot=snapshot, values=[rows[-1]['id']]) if more else None}


def update_issue(db, public_id, body):
    revision, status = body.get('revision'), body.get('status')
    if type(revision) is not int or revision < 1:
        raise IssueError('记录版本无效')
    if not isinstance(status, str) or status not in STATUSES:
        raise IssueError('请选择问题状态')
    note = _text(body['handling_note'], '处理备注') if 'handling_note' in body else None
    with immediate_transaction(db):
        row = _row(db, public_id)
        if row is None:
            raise IssueError('问题记录不存在', status=404, code='NOT_FOUND')
        if row['revision'] != revision:
            raise IssueError('记录已在其他页面更新，本地内容已保留', status=409,
                             code='REVISION_CONFLICT', current=_serialize(row))
        if note is None:
            note = row['handling_note']
        db.execute('UPDATE exam_question_issues SET status=?,handling_note=?,revision=revision+1,updated_at=? '
                   'WHERE public_id=? AND revision=?',
                   (status, note, datetime.now(timezone.utc).isoformat(timespec='seconds'), public_id, revision))
        return _serialize(_row(db, public_id))
