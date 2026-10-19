import io
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path

import pytest

from cuoti_app import create_app, database, migrations
from cuoti_app.services import mistake_classification as classification, solution_methods
from cuoti_app.services.daily_review import (
    _snapshot, validate_selection, _focused_candidates, reserve_daily_review, get_round,
)
from cuoti_app.services.import_validation import _validated_classification
from cuoti_app.services.portable_import_commit import _restore_classification
from cuoti_app.services.portable_backup import write_portable_backup
from tests.migration_fixtures import create_historical_database
from tests.test_review_optimization import add, value, publish


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'fixture.db'),
                       'PDF_DIR': str(tmp_path / 'pdfs'), 'REGISTER_JOB_HANDLERS': False})


@pytest.fixture
def db(app):
    with app.app_context():
        yield database.get_db()


def save(db, row, method='subsequence_method', role='primary', *, revision=0):
    return classification.update_classification(db, row['public_id'], {
        **value(db), 'revision': revision,
        'method_annotations': [{'code': method, 'role': role}],
    })


def test_catalog_aliases_are_unambiguous_and_not_fuzzy():
    identities = {}
    for method in solution_methods.METHODS:
        assert method['definition'] and method['subjects']
        for subject in method['subjects']:
            for name in (method['name'], *method['aliases']):
                key = (subject, solution_methods.label_key(name))
                assert key not in identities or identities[key] == method['code']
                identities[key] = method['code']
    annotations = solution_methods.normalize_annotations(['分部积分', '分部积分法'], subject='数学分析')
    assert annotations == [{'code': 'integration_by_parts', 'name': '分部积分法', 'role': 'unspecified'}]
    assert solution_methods.match_label('积分') is None
    assert solution_methods.match_label('正交性') is None
    assert solution_methods.match_label('分部积分', '高等代数') is None


@pytest.mark.parametrize('annotations', [
    [{'code': 'subsequence_method', 'role': 'primary'}, {'code': 'squeeze_method', 'role': 'primary'}],
    [{'code': 'subsequence_method', 'role': 'primary'}, {'code': 'subsequence_method', 'role': 'auxiliary'}],
    [{'code': 'invented_code', 'role': 'primary'}],
    [{'code': 'rank_decomposition', 'role': 'primary'}],
    [{'code': None, 'name': '不确定的方法', 'role': 'primary'}],
])
def test_invalid_roles_or_subject_do_not_overwrite_classification(db, annotations):
    row = add(db)
    before = save(db, row)
    with pytest.raises(classification.ClassificationError):
        classification.update_classification(db, row['public_id'], {
            **value(db), 'revision': before['revision'], 'method_annotations': annotations})
    assert classification.get_classification(db, row)['method_annotations'] == before['method_annotations']


def test_unknown_label_is_preserved_without_guessing_role(db):
    row = add(db)
    saved = classification.update_classification(db, row['public_id'], {
        **value(db), 'revision': 0, 'methods': ['尚未归一的技巧']})
    assert saved['method_annotations'] == [{'code': None, 'name': '尚未归一的技巧', 'role': 'unspecified'}]
    assert saved['locked']


def test_legacy_suggestion_edit_can_override_new_method_annotations(db):
    row = add(db)
    classification.publish_prediction(db, row, {
        **value(db, confidence=0.5),
        'method_annotations': [{'code': 'subsequence_method', 'role': 'primary'}],
    }, job_id='suggestion', base_revision=0)
    db.commit()
    suggestion = classification.get_classification(db, row)['suggestion']
    saved = classification.resolve_suggestion(db, suggestion['id'], {
        'revision': 0, 'action': 'accept', 'methods': ['反证法']})
    assert saved['methods'] == ['反证法']
    assert saved['method_annotations'][0]['role'] == 'unspecified'


def test_stale_methods_and_knowledge_do_not_enter_new_mixed_snapshot(db):
    row = add(db)
    save(db, row)
    db.execute("UPDATE mistakes SET content='另一道题' WHERE id=?", (row['id'],))
    db.commit()
    typed = _snapshot(db, classification.mistake_row(db, row['public_id']))['classification']
    assert typed['status'] == 'stale'
    assert typed['method_annotations'] == typed['methods'] == typed['knowledge_points'] == []
    assert classification.get_classification(db, row)['methods'] == ['子列法']


def test_subject_change_keeps_old_methods_readable_but_excludes_them_from_snapshot(db):
    row = add(db)
    save(db, row, method='integration_by_parts')
    algebra = db.execute("SELECT id FROM subjects WHERE name='高等代数'").fetchone()[0]
    db.execute('UPDATE mistakes SET subject_id=? WHERE id=?', (algebra, row['id']))
    db.commit()
    changed = classification.mistake_row(db, row['public_id'])
    current = classification.get_classification(db, changed)
    assert current['status'] == 'stale'
    assert current['method_annotations'][0]['code'] == 'integration_by_parts'
    assert _snapshot(db, changed)['classification']['method_annotations'] == []


def test_method_focus_roles_and_old_type_focus_are_independent(db):
    rows = [add(db) for _ in range(3)]
    for row, role in zip(rows, ('primary', 'auxiliary', 'unspecified')):
        save(db, row, role=role)
    base = {'practice_mode': 'focused', 'subject_id': rows[0]['subject_id'], 'method_code': 'subsequence_method'}
    main = validate_selection(db, base)
    assert [r['id'] for r in _focused_candidates(db, main)] == [rows[0]['id']]
    all_roles = validate_selection(db, {**base, 'method_role': 'any'})
    assert {r['id'] for r in _focused_candidates(db, all_roles)} == {r['id'] for r in rows}
    type_only = validate_selection(db, {'practice_mode': 'focused', 'primary_type_code': value(db)['primary_type_code']})
    assert len(_focused_candidates(db, type_only)) == 3
    db.execute("UPDATE mistakes SET content='changed' WHERE id=?", (rows[0]['id'],))
    db.commit()
    assert _focused_candidates(db, main) == []


def test_new_batch_freezes_method_roles_and_filter_reads_snapshot(db, app):
    row = add(db)
    save(db, row)
    selection = {'practice_mode': 'focused', 'subject_id': row['subject_id'], 'method_code': 'subsequence_method'}
    plan = reserve_daily_review(db, selection=selection)
    publish(db, plan)
    save(db, row, method='squeeze_method', revision=1)
    session = get_round(db, plan.round_id, filters={'method_code': 'subsequence_method'})
    assert len(session['items']) == 1
    assert session['items'][0]['classification']['method_annotations'][0]['role'] == 'primary'
    assert get_round(db, plan.round_id, filters={'method_code': 'squeeze_method'})['items'] == []
    response = app.test_client().get(f'/api/v2/daily-review/sessions/{plan.round_id}?method_code=subsequence_method')
    assert response.status_code == 200 and response.get_json()['items']


def test_explicit_refresh_preserves_manual_lock_and_revision(db):
    auto_row, manual_row = add(db), add(db)
    classification.publish_prediction(db, auto_row, value(db), job_id='first', base_revision=0)
    db.commit()
    classification.publish_prediction(db, auto_row, value(db, confidence=0.5), job_id='uncertain', base_revision=1)
    db.commit()
    save(db, manual_row)
    scope = {'subject_id': auto_row['subject_id']}
    assert classification.backfill_preview(db, scope)['count'] == 0
    preview = classification.backfill_preview(db, {**scope, 'refresh_auto': True})
    assert [t['public_id'] for t in preview['targets']] == [auto_row['public_id']]
    assert preview['pending_count'] == 1
    def ai(*args, **kwargs):
        return json.dumps({'items': [{'index': 1, 'classification': {
            **value(db), 'method_annotations': [{'code': 'subsequence_method', 'role': 'primary'}]}}]})
    result = classification.generate_classifications(db, preview['targets'], ai_call=ai, job_id='refresh')
    assert result['applied_count'] == 1
    assert classification.get_classification(db, auto_row)['method_annotations'][0]['role'] == 'primary'
    assert classification.get_classification(db, manual_row)['locked']


def test_portable_classification_preserves_method_roles(db, app):
    row = add(db)
    saved = save(db, row)
    with tempfile.TemporaryFile() as output:
        write_portable_backup(db, Path(app.config['ATTACHMENT_DIR']), output)
        output.seek(0)
        exported = output.read()
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        incoming = json.loads(archive.read('manifest.json'))['mistakes'][0]['classification']
    validated = _validated_classification(incoming)
    db.execute('DELETE FROM mistake_classifications WHERE mistake_id=?', (row['id'],))
    _restore_classification(db, row['id'], {'classification': validated})
    assert classification.get_classification(db, row)['method_annotations'] == saved['method_annotations']


def test_method_catalog_api_respects_subject(app):
    payload = app.test_client().get('/api/v2/solution-methods?subject=高等代数').get_json()
    codes = {item['code'] for item in payload['items']}
    assert 'rank_decomposition' in codes and 'contradiction' in codes
    assert 'integration_by_parts' not in codes


def test_v64_migration_preserves_raw_legacy_labels_and_lock(tmp_path):
    path = tmp_path / 'v63.db'
    create_historical_database(path, 63)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = add(db)
        db.execute("INSERT INTO mistake_classifications(mistake_id,primary_type_code,knowledge_points_json,methods_json,source_hash,source,locked,revision,catalog_version,updated_at) VALUES(?,?,'[]',?,?,'manual',1,2,1,'2026-09-25')",
                   (row['id'], value(db)['primary_type_code'], '["子列法"]', classification.source_hash(row)))
        db.commit()
        before = tuple(db.execute('SELECT * FROM mistake_classifications').fetchone())
        assert database._run_migration_step(db, 63) == 64
        after = tuple(db.execute('SELECT * FROM mistake_classifications').fetchone())
        assert after[:-1] == before and after[-1] is None
        saved = classification.get_classification(db, row)
        assert saved['locked'] and saved['method_annotations'][0]['role'] == 'unspecified'


def test_failed_v64_validation_rolls_back_column_and_version(tmp_path, monkeypatch):
    path = tmp_path / 'v63.db'
    create_historical_database(path, 63)
    monkeypatch.setattr(migrations, 'v64_schema_issues', lambda db: ['simulated validation failure'])
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        with pytest.raises(database.DatabaseSchemaError):
            database._run_migration_step(db, 63)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 63
        assert 'method_annotations_json' not in {r['name'] for r in db.execute('PRAGMA table_info(mistake_classifications)')}
