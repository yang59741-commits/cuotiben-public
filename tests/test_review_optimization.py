from __future__ import annotations

import io
import json
import uuid
import zipfile
import tempfile
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services import mistake_classification as classification
from cuoti_app.services.daily_analysis import generate_daily_analysis, query_daily_analysis
from cuoti_app.services import daily_analysis
from cuoti_app.services.daily_review import (
    reserve_daily_review, attach_export_batch, publish_daily_review, get_round,
    record_result, history_page, type_summary, recommendations,
)
from cuoti_app.services.mistake_exports import build_export_document
from cuoti_app.services.portable_backup import write_portable_backup
from cuoti_app.services.portable_import_commit import _restore_classification


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'fixture.db'),
                       'PDF_DIR': str(tmp_path / 'pdfs'), 'REGISTER_JOB_HANDLERS': False})


@pytest.fixture
def db(app):
    with app.app_context():
        yield get_db()


def add(db, *, content='证明数列收敛。', subject='数学分析', chapter='数列极限', status='未掌握'):
    sid = db.execute('SELECT id FROM subjects WHERE name=?', (subject,)).fetchone()[0]
    cid = db.execute('SELECT id FROM chapters WHERE subject_id=? AND name=?', (sid, chapter)).fetchone()
    public_id = str(uuid.uuid4())
    db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,content,qnum,note,date_added,status,next_review) VALUES(?,?,?,?,?,'我的提示','2026-09-16',?,'2026-10-01')",
               (public_id, sid, cid[0] if cid else None, content, 'Q-1', status))
    db.commit()
    return classification.mistake_row(db, public_id)


def value(db, name='数列收敛与发散判定', **kwargs):
    return {'primary_type_code': db.execute('SELECT code FROM question_types WHERE name=?', (name,)).fetchone()[0],
            'knowledge_points': ['数列极限'], 'methods': ['子列法'], 'confidence': 0.95, **kwargs}


def auto(db, row, payload=None, job=None, base=0):
    result = classification.publish_prediction(db, row, payload or value(db), job_id=job or uuid.uuid4().hex, base_revision=base)
    db.commit()
    return result


def publish(db, plan):
    batch = db.execute("INSERT INTO mistake_export_batches(public_id,created_at,expires_at,total_count,volume_count,layout,filter_snapshot_json,artifact_status,export_context) VALUES(?,?,'2099-01-01',?,1,'compact','{}','published','daily_review')",
                       (uuid.uuid4().hex, classification.now(), len(plan.items) if hasattr(plan, 'items') else len(plan.item_ids))).lastrowid
    attach_export_batch(db, plan.round_id, batch)
    publish_daily_review(db, plan.round_id, batch)
    db.commit()


def test_seed_identity_and_automatic_classification(db):
    types = classification.list_types(db)
    assert len(types) >= 50
    assert len({item['code'] for item in types}) == len(types)
    row = add(db)
    assert auto(db, row)
    saved = classification.get_classification(db, row)
    assert saved['status'] == 'auto'
    assert saved['type_name'] == '数列收敛与发散判定'
    assert saved['methods'] == ['子列法']


@pytest.mark.parametrize('changes', [{'confidence': 0.84}, {'confidence': float('nan')},
                                     {'proposed_type': {'name': '新类型'}}, {'primary_type_code': 'missing'},
                                     {'knowledge_points': ['a'] * 6}])
def test_uncertain_classification_is_a_suggestion(db, changes):
    row = add(db)
    assert not auto(db, row, value(db, **changes))
    assert classification.get_classification(db, row)['status'] == 'needs_review'


def test_notes_and_reviews_do_not_expire_classification(db):
    row = add(db)
    auto(db, row)
    db.execute("UPDATE mistakes SET note='changed',review_count=9 WHERE id=?", (row['id'],))
    db.commit()
    assert classification.get_classification(db, classification.mistake_row(db, row['public_id']))['status'] == 'auto'
    db.execute("UPDATE mistakes SET content='另一题' WHERE id=?", (row['id'],))
    db.commit()
    assert classification.get_classification(db, classification.mistake_row(db, row['public_id']))['status'] == 'stale'


def test_manual_lock_and_revision_conflict(db):
    row = add(db)
    saved = classification.update_classification(db, row['public_id'], {**value(db), 'revision': 0})
    assert saved['locked']
    assert not auto(db, row, job='old-job')
    assert classification.get_classification(db, row)['source'] == 'manual'
    with pytest.raises(classification.ClassificationError, match='已更新'):
        classification.update_classification(db, row['public_id'], {**value(db), 'revision': 0})
    unlocked = classification.update_classification(db, row['public_id'], {'revision': 1, 'action': 'unlock'})
    assert not unlocked['locked'] and unlocked['revision'] == 2


def test_subject_and_chapter_mismatch_require_confirmation(db):
    row = add(db, chapter='不定积分')
    assert not auto(db, row)
    row2 = add(db, subject='高等代数', chapter='矩阵')
    assert not auto(db, row2)


def test_suggestion_confirm_and_new_type(db):
    row = add(db)
    auto(db, row, value(db, confidence=0.5))
    suggestion = classification.get_classification(db, row)['suggestion']
    saved = classification.resolve_suggestion(db, suggestion['id'], {'revision': 0, 'action': 'accept',
        'new_type': {'subject': row['subject'], 'name': '特殊数列任务', 'definition': '验证特殊条件下的收敛性质', 'chapters': ['数列极限']}})
    assert saved['type_name'] == '特殊数列任务' and saved['locked']
    assert saved['suggestion'] is None


def test_daily_analysis_adds_classification_without_changing_text_contract(db):
    row = add(db)
    ai = Mock(return_value=json.dumps({'items': [{'index': 1, 'note_assessment': '无备注',
        'correction': '', 'hint': '检查条件', 'exam_pattern': '收敛判定', 'classification': value(db)}]}, ensure_ascii=False))
    result = generate_daily_analysis(db, raw_date='2026-09-16', generation_scope='missing', job_id='daily', ai_call=ai)
    assert result['items'][0]['classification']['status'] == 'auto'
    assert set(result['items'][0]['analysis']) == {'note_assessment', 'correction', 'hint', 'exam_pattern'}
    query_daily_analysis(db, raw_date='2026-09-16')
    assert ai.call_count == 1
    assert classification.get_classification(db, row)['revision'] == 1


def test_classification_failure_preserves_valid_analysis(db):
    add(db)
    ai = Mock(return_value=json.dumps({'items': [{'index': 1, 'note_assessment': '无备注', 'hint': '保留这段分析'}]}))
    result = generate_daily_analysis(db, raw_date='2026-09-16', job_id='invalid-classification', ai_call=ai)
    assert result['items'][0]['analysis']['hint'] == '保留这段分析'
    assert result['items'][0]['classification']['status'] == 'needs_review'


def test_old_analysis_classification_only_batches_preserve_text(db):
    template = add(db)
    db.executemany(
        "INSERT INTO mistakes(public_id,subject_id,chapter_id,content,qnum,note,date_added) "
        "VALUES(?,?,?,'证明数列收敛。','Q-1','我的提示','2026-09-16')",
        [(str(uuid.uuid4()), template['subject_id'], template['chapter_id']) for _ in range(200)],
    )
    text = json.dumps({'note_assessment': '正确', 'correction': '', 'hint': '保留原分析', 'exam_pattern': ''})
    db.executemany(
        "INSERT INTO daily_analysis_items(mistake_id,analysis_date,analysis_schema_version,prompt_version,"
        "source_hash,analysis_json,generated_at,updated_at) VALUES(?,'2026-09-16',1,2,?,?,?,?)",
        [(row['id'], daily_analysis.daily_analysis_source_hash(row), text, classification.now(), classification.now())
         for row in daily_analysis._active_rows(db, '2026-09-16')],
    )
    db.commit()

    def ai(messages, **kwargs):
        rows = json.loads(messages[-1]['content'].split('\n题目：')[1])
        return json.dumps({'items': [{'index': row['index'], 'classification': value(db)} for row in rows]})

    result = generate_daily_analysis(db, raw_date='2026-09-16', job_id='old-cache', ai_call=ai)
    assert result['generated_count'] == 0
    assert result['classification_missing_count'] == 0
    assert len(result['items']) == 201
    assert all(item['analysis']['hint'] == '保留原分析' and item['prompt_version'] == 2 for item in result['items'])
    assert all(item['classification']['status'] == 'auto' for item in result['items'])


def test_backfill_explicit_scope_preview_and_replay(db):
    row = add(db)
    with pytest.raises(classification.ClassificationError):
        classification.backfill_preview(db, {})
    preview = classification.backfill_preview(db, {'subject_id': row['subject_id']})
    assert preview['count'] == 1 and preview['estimated_input_tokens'] > 0
    ai = Mock(return_value=json.dumps({'items': [{'index': 1, 'classification': value(db)}]}))
    first = classification.generate_classifications(db, preview['targets'], ai_call=ai, job_id='backfill')
    second = classification.generate_classifications(db, preview['targets'], ai_call=ai, job_id='backfill')
    assert first['applied_count'] == 1 and second['skipped_count'] == 1
    assert ai.call_count == 1


def test_backfill_does_not_attach_result_after_content_change(db):
    row = add(db)
    targets = classification.backfill_preview(db, {'subject_id': row['subject_id']})['targets']
    def ai(*args, **kwargs):
        db.execute("UPDATE mistakes SET content='changed' WHERE id=?", (row['id'],))
        db.commit()
        return json.dumps({'items': [{'index': 1, 'classification': value(db)}]})
    result = classification.generate_classifications(db, targets, ai_call=ai, job_id='changed')
    assert result['skipped_count'] == 1
    assert db.execute('SELECT COUNT(*) FROM mistake_classifications').fetchone()[0] == 0


def test_focused_repetition_does_not_consume_mixed_cycle(db):
    row = add(db)
    auto(db, row)
    settings = {'practice_mode': 'focused', 'primary_type_code': value(db)['primary_type_code']}
    first = reserve_daily_review(db, selection=settings)
    publish(db, first)
    second = reserve_daily_review(db, selection=settings)
    publish(db, second)
    mixed = reserve_daily_review(db)
    assert first.mistake_ids == second.mistake_ids == mixed.mistake_ids == (row['id'],)
    assert mixed.round_number == 1
    assert db.execute('SELECT COUNT(*) FROM daily_review_items WHERE coverage_round IS NOT NULL').fetchone()[0] == 1


def test_focused_filters_stale_deleted_and_mastered(db):
    rows = [add(db) for _ in range(4)]
    for row in rows:
        auto(db, row)
    db.execute("UPDATE mistakes SET content='changed' WHERE id=?", (rows[0]['id'],))
    db.execute("UPDATE mistakes SET deleted_at='2026-09-16' WHERE id=?", (rows[1]['id'],))
    db.execute("UPDATE mistakes SET status='已掌握' WHERE id=?", (rows[2]['id'],))
    db.commit()
    settings = {'practice_mode': 'focused', 'primary_type_code': value(db)['primary_type_code']}
    assert reserve_daily_review(db, selection=settings).mistake_ids == (rows[3]['id'],)
    assert set(reserve_daily_review(db, selection={**settings, 'include_mastered': True}).mistake_ids) == {rows[2]['id'], rows[3]['id']}


def test_frozen_content_classification_summary_and_recommendation(db):
    row = add(db)
    auto(db, row)
    focused = reserve_daily_review(db, selection={'practice_mode': 'focused', 'primary_type_code': value(db)['primary_type_code']})
    publish(db, focused)
    record_result(db, round_id=focused.round_id, item_id=focused.item_ids[0], result='again', idempotency_key='result')
    db.execute("UPDATE mistakes SET content='changed' WHERE id=?", (row['id'],))
    db.execute("UPDATE question_types SET name='新名称' WHERE code=?", (value(db)['primary_type_code'],))
    db.commit()
    session = get_round(db, focused.round_id)
    assert session['items'][0]['content'] == row['content']
    assert session['items'][0]['classification']['type_name'] == '数列收敛与发散判定'
    assert session['result_counts'] == {'correct': 0, 'again': 1, 'incomplete': 0, 'pending': 0}
    summary = type_summary(db, session_id=focused.round_id)[0]
    assert summary['again'] == 1 and summary['accuracy'] is None
    assert recommendations(db)['total'] == 0


def test_pdf_visibility_and_no_method_labels(db):
    row = add(db)
    row['classification'] = {'type_name': '数列收敛判定', 'methods': ['子列法']}
    mixed, _ = build_export_document([row], created_at=datetime.now(timezone.utc), export_context='daily_review', practice_mode='mixed')
    focused, _ = build_export_document([row], created_at=datetime.now(timezone.utc), export_context='daily_review', practice_mode='focused')
    assert '我的提示' not in mixed and '数列收敛判定' not in mixed
    assert '我的提示' in focused and '数列收敛判定' in focused
    assert '子列法' not in focused


def test_history_pagination_and_empty_filtered_batch(db):
    row = add(db)
    for _ in range(3):
        plan = reserve_daily_review(db)
        publish(db, plan)
    first = history_page(db, limit=2)
    assert len(first['items']) == 2 and first['has_more']
    assert len(history_page(db, limit=2, after=first['next_cursor'])['items']) == 1
    session = get_round(db, plan.round_id, filters={'result': 'again'})
    assert session['item_count'] == 0 and session['all_item_count'] == 1
    assert session['items'] == [] and row['id']


def test_portable_v6_preserves_manual_classification(db, app, tmp_path):
    row = add(db)
    classification.update_classification(db, row['public_id'], {**value(db), 'revision': 0})
    with tempfile.TemporaryFile() as output:
        write_portable_backup(db, tmp_path, output)
        output.seek(0)
        raw = output.read()
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    assert manifest['format_version'] == 7
    incoming = manifest['mistakes'][0]['classification']
    assert incoming['locked'] and incoming['question_type']['definition']
    db.execute('DELETE FROM mistake_classifications')
    db.commit()
    from cuoti_app.transactions import immediate_transaction
    with immediate_transaction(db):
        _restore_classification(db, row['id'], {'classification': incoming})
    assert classification.get_classification(db, row)['locked']
    client = app.test_client()
    response = client.post('/api/v3/import/preview?mode=merge', data=raw)
    assert response.status_code == 200, response.get_json()


def test_classification_routes_conflict_and_readonly(app, db):
    row = add(db)
    client = app.test_client()
    assert client.get('/api/v2/question-types').status_code == 200
    endpoint = '/api/v2/mistakes/' + row['public_id'] + '/classification'
    assert client.get(endpoint).get_json()['status'] == 'unclassified'
    assert client.put(endpoint, json={**value(db), 'revision': 0}).status_code == 200
    assert client.put(endpoint, json={**value(db), 'revision': 0}).status_code == 409
    assert client.post('/api/v2/mistake-classifications/backfill/preview', json={}).status_code == 400
