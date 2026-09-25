"""Behavior regressions for the September review; all data is disposable."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services.jobs import JobCoordinator
from cuoti_app.services.legacy_import import import_legacy_backup


@pytest.fixture
def app():
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        yield create_app({
            "TESTING": True, "REGISTER_JOB_HANDLERS": True,
            "DATABASE": str(root / "fixture.db"),
            "PDF_DIR": str(root / "pdfs"),
            "ATTACHMENT_DIR": str(root / "attachments"),
            "JOB_INPUT_DIR": str(root / "inputs"),
        })


def seed_mistake(db):
    chapter = db.execute("SELECT id,subject_id FROM chapters ORDER BY id LIMIT 1").fetchone()
    cursor = db.execute(
        "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,date_added,status) "
        "VALUES(?,?,'review-fix','fixture','2026-09-25','未掌握')",
        (chapter['subject_id'], chapter['id']),
    )
    db.commit()
    return cursor.lastrowid, chapter['subject_id'], chapter['id']


def facts(app):
    with app.app_context():
        db = get_db()
        return {table: [tuple(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY id')]
                for table in ('mistakes', 'review_events', 'attachments', 'background_jobs')}


@pytest.mark.parametrize('raw', [b'[]', b'not json'])
def test_disabled_http_import_never_reads_body_or_changes_data(app, monkeypatch, raw):
    with app.app_context():
        seed_mistake(get_db())
    before = facts(app)
    def no_body(*args, **kwargs):
        raise AssertionError('disabled endpoint read the body')
    monkeypatch.setattr(app.request_class, 'get_data', no_body)
    response = app.test_client().post('/internal/legacy/import?mode=replace', data=raw)
    assert response.status_code == 410
    assert response.json['code'] == 'legacy_import_disabled'
    assert facts(app) == before


@pytest.mark.parametrize('url', ['/api/v2/jobs', '/api/v2/jobs/data-import'])
def test_public_jobs_reject_unpreviewed_replace_without_creating_job(app, url):
    with app.app_context():
        seed_mistake(get_db())
    before = facts(app)
    response = app.test_client().post(url, json={
        'kind': 'data-import', 'input': {'backup': [], 'mode': 'replace'},
    })
    assert response.status_code == 400
    assert response.json['code'] == 'import_preview_required'
    assert facts(app) == before


@pytest.mark.parametrize('committed', [False, True])
def test_persisted_legacy_replace_replays_only_committed_business_result(app, committed):
    coordinator = JobCoordinator(app.config['DATABASE'])
    job, _ = coordinator.submit('data-import', {'backup': [], 'mode': 'replace'})
    with app.app_context():
        db = get_db()
        if committed:
            import_legacy_backup(db, raw=b'[]', mode='replace', source_job_id=job['id'])
        seed_mistake(db)
        before = [tuple(r) for r in db.execute('SELECT * FROM mistakes')]
    result = coordinator.run_once()
    assert result['status'] == ('succeeded' if committed else 'failed'), result
    if committed:
        assert result['result']['idempotency_replayed'] is True
    else:
        assert 'import_preview_required' in json.dumps(result)
    with app.app_context():
        assert [tuple(r) for r in get_db().execute('SELECT * FROM mistakes')] == before


