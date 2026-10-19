import json
from datetime import date, timedelta

import pytest

from cuoti_app.services import daily_analysis, mistake_attention, mistake_classification as classification
from cuoti_app.services import solution_methods
from tests.test_review_optimization import app, db, add, value


def cache(db, row):
    source = daily_analysis.source_rows_by_ids(db, [row['id']])[0]
    card = {'note_assessment': '无备注', 'correction': '', 'hint': r'检查 $x^2$。', 'exam_pattern': '极限'}
    db.execute("INSERT INTO daily_analysis_items(mistake_id,analysis_date,analysis_schema_version,prompt_version,"
               "source_hash,analysis_json,source_job_id,generated_at,updated_at) VALUES(?,?,1,3,?,?,'fixture',?,?)",
               (row['id'], row['date_added'], daily_analysis.daily_analysis_source_hash(source),
                json.dumps(card), classification.now(), classification.now()))
    db.commit()


def test_global_counts_filters_dates_and_page_states_share_scope(app, db):
    missing, complete, pending, legacy, mastered = [add(db) for _ in range(5)]
    annotations = [{'code': 'subsequence_method', 'role': 'primary'}, {'code': 'squeeze_method', 'role': 'auxiliary'}]
    classification.update_classification(db, complete['public_id'], {**value(db), 'revision': 0, 'method_annotations': annotations})
    classification.publish_prediction(db, pending, {**value(db, confidence=0.5), 'method_annotations': annotations}, job_id='pending', base_revision=0)
    db.commit()
    classification.update_classification(db, legacy['public_id'], {**value(db), 'revision': 0})
    for row in [complete, pending, legacy]:
        cache(db, row)
    blank, deleted = add(db, content=''), add(db)
    db.execute("UPDATE mistakes SET deleted_at='2026-09-27' WHERE id=?", (deleted['id'],))
    db.execute("UPDATE mistakes SET status='已掌握',date_added='2025-01-01' WHERE id=?", (mastered['id'],))
    db.commit()
    client = app.test_client()
    assert client.get('/api/v2/mistakes/attention').get_json()['summary'] == {
        'analysis_missing': 2, 'methods_missing': 3, 'confirmation': 1 + 1}
    expected = {'analysis_missing': {missing['id'], mastered['id']},
                'methods_missing': {missing['id'], pending['id'], mastered['id']},
                'confirmation': {pending['id'], legacy['id']}}
    for attention, identities in expected.items():
        first = client.get('/api/v2/mistakes', query_string={'attention': attention, 'limit': 1}).get_json()
        assert first['total'] == len(identities)
        seen = {item['id'] for item in first['items']}
        cursor = first['next_cursor']
        while cursor:
            page = client.get('/api/v2/mistakes', query_string={'attention': attention, 'limit': 1, 'after': cursor}).get_json()
            seen.update(item['id'] for item in page['items'])
            cursor = page['next_cursor']
        assert seen == identities
        assert client.get('/api/v2/mistakes', query_string={'after': first['next_cursor']}).status_code == 400
    first = client.get('/api/v2/mistakes?limit=1&attention=confirmation').get_json()
    assert first['analysis_dates']['2026-09-16'] == {'total': 4, 'missing': 1}
    before = db.total_changes
    states = client.get('/api/v2/mistakes/attention', query_string=[('public_id', row['public_id']) for row in [complete, pending, blank, deleted]]).get_json()['items']
    assert states[complete['public_id']]['analysis'] == 'cached'
    assert states[complete['public_id']]['methods'] == 'annotated'
    assert states[pending['public_id']]['needs_confirmation']
    assert blank['public_id'] not in states and deleted['public_id'] not in states
    assert db.total_changes == before


def test_cache_changes_are_precise_without_calling_ai(db):
    row = add(db)
    cache(db, row)
    assert mistake_attention.item_states(db, [row])[row['public_id']]['analysis'] == 'cached'
    db.execute("UPDATE mistakes SET note='changed' WHERE id=?", (row['id'],))
    db.commit()
    assert mistake_attention.item_states(db, [row])[row['public_id']]['analysis'] == 'stale'
    assert mistake_attention.summary(db)['analysis_missing'] == 0


def test_attention_summary_uses_metadata_and_page_queries_are_bounded(db):
    rows = [add(db) for _ in range(30)]
    statements = []
    db.set_trace_callback(statements.append)
    try:
        assert mistake_attention.summary(db)['analysis_missing'] == 30
        assert len(statements) == 1
        assert 'source_hash' not in statements[0] and 'analysis_json' not in statements[0]
        statements.clear()
        assert len(mistake_attention.item_states(db, rows)) == 30
        assert len(statements) == 4
    finally:
        db.set_trace_callback(None)


def test_outdated_suggestions_cannot_apply_but_can_be_rejected(db):
    row = add(db)
    classification.publish_prediction(db, row, value(db, confidence=0.5), job_id='pending', base_revision=0)
    db.execute("UPDATE mistakes SET content='新的题干',revision=revision+1 WHERE id=?", (row['id'],))
    db.commit()
    current = classification.get_classification(db, classification.mistake_row(db, row['public_id']))
    assert not current['suggestion']['is_current']
    sid = current['suggestion']['id']
    with pytest.raises(classification.ClassificationError, match='变化'):
        classification.resolve_suggestion(db, sid, {'revision': 0, 'action': 'accept'})
    rejected = classification.resolve_suggestion(db, sid, {'revision': 0, 'action': 'reject'})
    assert rejected['suggestion'] is None and rejected['revision'] == 0


def test_suggestion_roles_are_saved_only_on_confirmation_and_source_edit_conflicts(db):
    row = add(db)
    annotations = [{'code': 'telescoping', 'role': 'primary'}, {'code': 'identity_transformation', 'role': 'auxiliary'}]
    classification.publish_prediction(db, row, {**value(db, confidence=0.5), 'method_annotations': annotations}, job_id='pending', base_revision=0)
    db.commit()
    current = classification.get_classification(db, row)
    assert current['method_annotations'] == []
    assert current['suggestion']['payload']['method_annotations'] == annotations
    saved = classification.resolve_suggestion(db, current['suggestion']['id'], {
        'revision': 0, 'mistake_revision': row['revision'], 'action': 'accept'})
    assert saved['locked'] and [item['role'] for item in saved['method_annotations']] == ['primary', 'auxiliary']
    assert mistake_attention.summary(db)['confirmation'] == 0
    db.execute("UPDATE mistakes SET content='新题',revision=revision+1 WHERE id=?", (row['id'],))
    db.commit()
    with pytest.raises(classification.ClassificationError, match='题目已更新'):
        classification.update_classification(db, row['public_id'], {
            **value(db), 'revision': saved['revision'], 'mistake_revision': row['revision']})


def test_overdue_reads_keep_original_question_once_and_dates_unchanged(app, db):
    overdue, today, unknown = [add(db) for _ in range(3)]
    past = (date.today() - timedelta(days=2)).isoformat()
    for row, due in [(overdue, past), (today, date.today().isoformat()), (unknown, None)]:
        db.execute('UPDATE mistakes SET next_review=? WHERE id=?', (due, row['id']))
    db.commit()
    client = app.test_client()
    for _ in range(2):
        first = client.get('/api/v2/reviews/due?limit=2').get_json()
        second = client.get('/api/v2/reviews/due', query_string={'limit': 2, 'after': first['next_cursor']}).get_json()
        items = first['items'] + second['items']
        assert len({item['public_id'] for item in items}) == len(items) == 3
        assert first['summary']['overdue'] == 1
        by_id = {item['id']: item for item in items}
        assert by_id[overdue['id']]['overdue_days'] == 2
        assert by_id[overdue['id']]['next_review'] == past
        assert by_id[today['id']]['due_status'] == 'today'
        assert by_id[unknown['id']]['due_status'] == 'unknown'
    assert db.execute('SELECT COUNT(*) FROM mistakes').fetchone()[0] == 3


def test_catalog_additions_keep_subject_boundaries():
    assert solution_methods.CATALOG_VERSION == 2
    assert solution_methods.match_label('裂项法', '数学分析')['code'] == 'telescoping'
    assert solution_methods.match_label('哈密顿-凯莱定理', '高等代数')['code'] == 'cayley_hamilton'
    assert solution_methods.match_label('拉格朗日插值', '数学分析') is None
    with pytest.raises(solution_methods.MethodError):
        solution_methods.resolve_method('integral_test', '高等代数')
