"""Small shared vocabulary for self-reported assistance and local-day evidence."""

from datetime import datetime, timedelta, date

from ..validation import ValidationError
from .exam_repetition_policy import question_content_fingerprint


def assistance_value(value=None):
    value = 'unknown' if value is None else value
    if value not in ('none', 'hint', 'unknown'):
        raise ValidationError('辅助情况必须是 none、hint 或 unknown')
    return value


def evidence(content, assistance=None, now=None):
    current = now or datetime.now().astimezone()
    return {
        'assistance': assistance_value(assistance), 'evidence_version': 2,
        'reviewed_on': current.astimezone().date().isoformat(),
        'content_fingerprint': question_content_fingerprint(content),
    }


def remediation_status(db, now=None):
    """Derive follow-ups from new evidence; never change either review domain."""
    today = (now or datetime.now().astimezone()).astimezone().date().isoformat()
    failures = db.execute("""SELECT i.*,m.public_id,m.content AS current_content,
        m.status AS mistake_status,m.revision,m.subject_id,m.chapter_id,m.qnum,
        s.name AS subject,c.name AS chapter FROM daily_review_items i
        JOIN mistakes m ON m.id=i.mistake_id
        LEFT JOIN subjects s ON s.id=m.subject_id LEFT JOIN chapters c ON c.id=m.chapter_id
        WHERE i.status='published' AND i.evidence_version=2 AND m.deleted_at IS NULL
        AND (i.completion_result='again' OR (i.completion_result='correct' AND i.assistance='hint'))
        ORDER BY i.completed_at,i.id""").fetchall()
    by_mistake = {}
    for raw in failures:
        item = dict(raw)
        if item['content_fingerprint'] != question_content_fingerprint(item['current_content']):
            continue
        previous = by_mistake.get(item['mistake_id'])
        item['again_count'] = (previous['again_count'] if previous else 0) + 1
        by_mistake[item['mistake_id']] = item
    pending, recovered = [], []
    for item in by_mistake.values():
        args = (item['mistake_id'], item['content_fingerprint'], item['reviewed_on'])
        success = db.execute("""SELECT 1 FROM daily_review_items
            WHERE mistake_id=? AND content_fingerprint=? AND reviewed_on>?
            AND evidence_version=2 AND status='published'
            AND completion_result='correct' AND assistance='none'
            UNION ALL SELECT 1 FROM review_events
            WHERE mistake_id=? AND content_fingerprint=? AND reviewed_on>?
            AND evidence_version=2 AND event_type='pass' AND assistance='none'
            LIMIT 1""", (*args, *args)).fetchone()
        if success:
            recovered.append(item['mistake_id'])
            continue
        due_on = (date.fromisoformat(item['reviewed_on']) + timedelta(days=1)).isoformat()
        pending.append({
            'source_item_id': item['id'], 'mistake_id': item['mistake_id'],
            'public_id': item['public_id'], 'revision': item['revision'],
            'subject_id': item['subject_id'], 'chapter_id': item['chapter_id'],
            'subject': item['subject'], 'chapter': item['chapter'], 'qnum': item['qnum'],
            'status': item['mistake_status'], 'again_count': item['again_count'],
            'due_on': due_on, 'due': due_on <= today,
        })
    pending.sort(key=lambda row: (row['due_on'], -row['again_count'], row['public_id']))
    return {'items': pending, 'total': len(pending), 'due_count': sum(item['due'] for item in pending), 'recovered_count': len(recovered), 'recovered_mistake_ids': recovered}


def learning_actions(db, scope):
    from urllib.parse import urlencode
    from .statistics import build_reminders
    from .daily_review import _focused_candidates, type_summary
    actions = []
    filters = {key: scope[key] for key in ('subject_id', 'chapter_id') if scope.get(key)}
    reminder = build_reminders(db, scope)
    if reminder['due_count']:
        actions.append({'title': '今日间隔复习', 'message': f"到期 {reminder['due_count']} 道，逾期 {reminder['overdue_count']} 道", 'count': reminder['due_count'], 'href': '#/review?' + urlencode({'mode':'spaced', **filters})})
    pending = [item for item in remediation_status(db)['items'] if item['due'] and all(item.get(key) == value for key, value in filters.items())]
    if pending:
        actions.append({'title': '复错再练', 'message': f'{len(pending)} 道题可进行跨日验证', 'count':len(pending), 'href':'#/review?' + urlencode({'mode':'daily', 'remediation':1, **filters})})
    summaries = type_summary(db, **filters)
    weak = sorted((row for row in summaries if row['primary_type_code'] and row['independent_accuracy'] is not None and row['independent_accuracy'] < 0.6), key=lambda row: (row['independent_accuracy'], row['primary_type_code']))
    for row in weak:
        code = row['primary_type_code']
        active_type = db.execute('SELECT 1 FROM question_types WHERE code=? AND active=1', (code,)).fetchone()
        available = active_type and _focused_candidates(db, {
            'primary_type_code': code, 'include_mastered': False,
            'chapter_id': filters.get('chapter_id'),
        })
        if available:
            actions.append({'title':row['type_name'], 'message':f"近30天 {row['independent_sample_count']} 道独立样本，正确率 {round(row['independent_accuracy'] * 100)}%", 'count':row['independent_sample_count'], 'href':'#/review?' + urlencode({'mode':'daily', 'primary_type_code':code, **filters})})
            break
    return actions[:3]
