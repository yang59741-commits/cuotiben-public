import json
import sqlite3
import uuid

import pytest

from cuoti_app import create_app, database
from cuoti_app.migrations import migration_schema_issues
from cuoti_app.migrations.v65_exam_question_issues import migrate_v64_to_v65
from cuoti_app.routes.exams_legacy import _store_exam_items
from tests.migration_fixtures import create_historical_database


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'fixture.db'),
                       'PDF_DIR': str(tmp_path / 'pdfs'), 'REGISTER_JOB_HANDLERS': False})


def exam(app, title='合成试卷'):
    with app.app_context():
        db = database.get_db()
        eid = db.execute("INSERT INTO exams(title,content,config,created_at,artifact_status) "
                         "VALUES(?,?,'{}','2026-09-27','published')", (title, r'\begin{problem}x=1\end{problem}')).lastrowid
        _store_exam_items(db, eid, r'\begin{problem}x=1\end{problem}', source='free',
                          structured_items=[{'position': 1, 'type': '计算题', 'difficulty': '基础'}])
        db.commit()
        item = db.execute('SELECT public_id FROM exam_items WHERE exam_id=?', (eid,)).fetchone()[0]
    return eid, item


def report(client, eid, item, **extra):
    body = {'client_event_id': str(uuid.uuid4()), 'exam_item_public_id': item,
            'category': 'formula', 'description': '公式显示需要核对', **extra}
    return client.post(f'/api/v2/exams/{eid}/question-issues', json=body), body


def test_report_freezes_server_source_and_replays_without_modifying_exam(app):
    client = app.test_client()
    eid, item = exam(app)
    response, body = report(client, eid, item, snapshot={'content': '伪造来源'})
    assert response.status_code == 201
    result = response.json
    assert result['snapshot']['content'] == 'x=1'
    assert result['snapshot']['exam_title'] == '合成试卷'
    repeated = client.post(f'/api/v2/exams/{eid}/question-issues', json=body)
    assert repeated.status_code == 200 and repeated.json['replayed']
    assert repeated.json['public_id'] == result['public_id']
    assert client.post(f'/api/v2/exams/{eid}/question-issues', json={**body, 'description': '不同内容'}).status_code == 409
    with app.app_context():
        db = database.get_db()
        assert db.execute('SELECT COUNT(*) FROM exam_question_issues').fetchone()[0] == 1
        assert db.execute('SELECT content FROM exam_items WHERE public_id=?', (item,)).fetchone()[0] == 'x=1'


def test_cross_exam_invalid_input_and_unpublished_sources_are_rejected(app):
    client = app.test_client()
    eid, item = exam(app)
    other, _ = exam(app)
    assert report(client, other, item)[0].status_code == 404
    for values in ({'category': []}, {'category': 'bad'}, {'description': ''},
                   {'description': 'x' * 2001}, {'client_event_id': 'bad'}, {'exam_item_public_id': None}):
        assert report(client, eid, item, **values)[0].status_code == 400
    with app.app_context():
        db = database.get_db()
        db.execute("UPDATE exams SET artifact_status='publishing' WHERE id=?", (eid,))
        db.commit()
    assert report(client, eid, item)[0].status_code == 404
    assert client.post(f'/api/v2/exams/{eid}/question-issues', data='not json').status_code == 415


def test_update_conflict_preserves_note_and_soft_delete_preserves_report(app):
    client = app.test_client()
    eid, item = exam(app)
    response, body = report(client, eid, item)
    url = '/api/v2/question-issues/' + response.json['public_id']
    changed = client.patch(url, json={'revision': 1, 'status': 'resolved', 'handling_note': '已核对来源'})
    assert changed.status_code == 200 and changed.json['revision'] == 2
    conflict = client.patch(url, json={'revision': 1, 'status': 'dismissed', 'handling_note': '旧页面'})
    assert conflict.status_code == 409
    assert conflict.json['current']['handling_note'] == '已核对来源'
    reopened = client.patch(url, json={'revision': 2, 'status': 'open'}).json
    assert reopened['status'] == 'open'
    assert reopened['handling_note'] == '已核对来源'
    assert client.delete(f'/api/v2/exams/{eid}').status_code == 200
    rows = client.get('/api/v2/question-issues').json['items']
    assert rows[0]['exam_deleted'] and rows[0]['snapshot']['exam_title'] == '合成试卷'
    assert client.post(f'/api/v2/exams/{eid}/question-issues', json=body).json['replayed']
    assert report(client, eid, item)[0].status_code == 404


def test_issue_pagination_is_filter_bound_and_has_no_duplicates(app):
    client = app.test_client()
    eid, item = exam(app)
    ids = {report(client, eid, item, description=f'问题{i}')[0].json['public_id'] for i in range(7)}
    first = client.get('/api/v2/question-issues?status=open&limit=3').json
    assert first['total'] == 7 and first['has_more']
    cursor = first['next_cursor']
    assert client.get('/api/v2/question-issues?status=resolved&after=' + cursor).status_code == 400
    seen = [row['public_id'] for row in first['items']]
    while cursor:
        page = client.get('/api/v2/question-issues?status=open&limit=3&after=' + cursor).json
        seen.extend(row['public_id'] for row in page['items'])
        cursor = page['next_cursor']
    assert set(seen) == ids and len(seen) == len(ids)


def test_v65_migration_is_additive_and_rolls_back_as_one_transaction(tmp_path):
    path = tmp_path / 'historical.db'
    create_historical_database(path, 64)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        before = list(db.iterdump())
        db.execute('BEGIN IMMEDIATE')
        migrate_v64_to_v65(db)
        db.rollback()
        assert list(db.iterdump()) == before
        database._run_migration_step(db, 64)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 65
        assert migration_schema_issues(db, 65) == []
        old_tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'exam_question_issues'")]
        assert 'mistakes' in old_tables
        assert db.execute('SELECT COUNT(*) FROM exam_question_issues').fetchone()[0] == 0
