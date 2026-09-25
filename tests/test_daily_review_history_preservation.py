import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from cuoti_app import create_app, database
from cuoti_app.database import get_db
from cuoti_app.services.daily_review import get_round, record_result, type_summary
from cuoti_app.services.mistake_exports import create_mistake_export, cleanup_exports
from cuoti_app.services.learning_evidence import remediation_status
from cuoti_app.validation import mistake_fingerprint
from tests.test_daily_review_pdf_reexport import fake_compile


@pytest.fixture
def legacy_book(tmp_path):
    path = tmp_path / 'history.db'
    config = {'TESTING': True, 'DATABASE': str(path), 'PDF_DIR': str(tmp_path / 'pdfs'),
              'REGISTER_JOB_HANDLERS': False}
    with patch.object(database, 'DB_SCHEMA_VERSION', 62), patch.object(database, 'SCHEMA_VERSION', 62):
        app = create_app(config)
    with app.app_context():
        db = get_db()
        for i in range(5):
            content = f'历史题目 {i} $x^2$'
            db.execute("INSERT INTO mistakes(subject_id,chapter_id,qnum,content,date_added,status,fingerprint) "
                       "VALUES(1,1,?,?, '2026-09-25','未掌握',?)",
                       (str(i), content, mistake_fingerprint('数学分析', '引论', str(i), content, '2026-09-25')))
        db.commit()
        original, _ = create_mistake_export(db, pdf_dir=config['PDF_DIR'], xelatex_command='xelatex',
            payload={'export_context': 'daily_review'}, compiler=fake_compile)
        session_id = original['daily_review_round_id']
        ids = [r[0] for r in db.execute('SELECT id FROM daily_review_items ORDER BY id')]
        for item_id in ids[:4]:
            record_result(db, round_id=session_id, item_id=item_id, result='again', assistance='none')
        before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY id')]
                  for t in ('mistakes', 'daily_review_rounds', 'daily_review_items')}
        indexes = [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='daily_review_items' ORDER BY name")]
        db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='daily_review_items'")
        db.commit()
    return config, before, indexes, original


def test_schema63_preserves_rows_indexes_sequence_and_rolls_back_on_failure(legacy_book):
    config, before, indexes, _ = legacy_book
    from cuoti_app.migrations import v63_daily_review_history as migration
    original_check = database.migration_schema_issues
    with patch.object(database, 'migration_schema_issues', side_effect=lambda db, target:
                      ['controlled migration failure'] if target == 63 else original_check(db, target)):
        with pytest.raises(database.DatabaseSchemaError):
            create_app(config)
    with closing(sqlite3.connect(config['DATABASE'])) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 62
        assert db.execute('SELECT * FROM daily_review_items ORDER BY id').fetchall() == before['daily_review_items']
        assert any(r[2] == 'mistakes' for r in db.execute('PRAGMA foreign_key_list(daily_review_items)'))
    app = create_app(config)
    with app.app_context():
        db = get_db()
        assert db.execute('PRAGMA user_version').fetchone()[0] == 63
        for table, rows in before.items():
            assert [tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')] == rows
        assert [tuple(r) for r in db.execute("SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='daily_review_items' ORDER BY name")] == indexes
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='daily_review_items'").fetchone()[0] == 100
        assert migration.schema_issues(db) == []
        assert database.schema_issues(db) == []
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('layout', ['compact', 'notes', 'two_column'])
def test_replace_keeps_readonly_history_and_does_not_assign_evidence_to_reimported_ids(legacy_book, layout):
    config, _, _, original = legacy_book
    app = create_app(config)
    client = app.test_client()
    session_id = original['daily_review_round_id']
    with app.app_context():
        db = get_db()
        session = get_round(db, session_id)
        summary = type_summary(db, session_id=session_id)
        history = [tuple(r) for r in db.execute('SELECT * FROM daily_review_items ORDER BY id')]
        old_ids = {r[0] for r in db.execute('SELECT id FROM mistakes')}
    backup = client.get('/api/v3/export').data
    preview = client.post('/api/v3/import/preview?mode=replace', data=backup)
    assert preview.status_code == 200, preview.json
    result = client.post('/api/v3/import/commit?mode=replace', data=backup,
                         headers={'X-Import-Preview-Token': preview.json['preview_token']})
    assert result.status_code == 200, result.json
    with app.app_context():
        db = get_db()
        assert [tuple(r) for r in db.execute('SELECT * FROM daily_review_items ORDER BY id')] == history
        new_ids = {r[0] for r in db.execute('SELECT id FROM mistakes')}
        assert old_ids.isdisjoint(new_ids)
        detached = get_round(db, session_id)
        assert detached['item_count'] == 5 and detached['completed_count'] == 4
        assert all(item['read_only'] for item in detached['items'])
        for old, new in zip(session['items'], detached['items']):
            assert {k:v for k,v in old.items() if k != 'read_only'} == {k:v for k,v in new.items() if k != 'read_only'}
        after_summary = type_summary(db, session_id=session_id)
        for old, new in zip(summary, after_summary):
            assert {k:v for k,v in old.items() if k not in ('remediation_count','recovered_count')} == {k:v for k,v in new.items() if k not in ('remediation_count','recovered_count')}
            assert new['remediation_count'] == new['recovered_count'] == 0
        assert remediation_status(db)['items'] == []
        db.execute("UPDATE mistake_exports SET expires_at='2000-01-01'")
        db.execute("UPDATE mistake_export_batches SET expires_at='2000-01-01'")
        db.commit()
        cleanup_exports(db, config['PDF_DIR'])
        rebuilt, _ = create_mistake_export(db, pdf_dir=config['PDF_DIR'], xelatex_command='xelatex',
            payload={'export_context':'daily_review','daily_review_session_id':session_id,'layout':layout}, compiler=fake_compile)
        assert [i['content'] for i in rebuilt['items']] == [i['content'] for i in original['items']]
        assert rebuilt['completed_count'] == 4
        assert get_round(db, session_id)['remaining_count'] == 1
        fresh, _ = create_mistake_export(db, pdf_dir=config['PDF_DIR'], xelatex_command='xelatex',
            payload={'export_context':'daily_review'}, compiler=fake_compile)
        assert {i['mistake_id'] for i in get_round(db, fresh['daily_review_round_id'])['items']} == new_ids
        assert all(not i['read_only'] for i in get_round(db, fresh['daily_review_round_id'])['items'])
    for item in detached['items']:
        rejected = client.post(f'/api/v2/daily-review/sessions/{session_id}/items/{item["id"]}/result',
                               json={'result':'again','assistance':'none'})
        assert rejected.status_code == 409
        assert rejected.json['code'] == 'DAILY_REVIEW_READ_ONLY'
    with app.app_context():
        db = get_db()
        # Export cleanup may unlink the original export batch; all learning facts stay fixed.
        for row, old in zip(db.execute('SELECT * FROM daily_review_items WHERE round_id=? ORDER BY id', (session_id,)), history):
            assert tuple(row)[0:4] == old[0:4]
            assert tuple(row)[5:] == old[5:]
