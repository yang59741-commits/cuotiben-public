"""Exercise the HTTP adapters, not only their already-correct service helpers."""
import uuid
from pathlib import Path

from cuoti_app.database import get_db
from cuoti_app.services.jobs import JobCoordinator
from cuoti_app.services.job_contracts import ACTIVE_STATUSES
from tests.test_review_fixes import app


def test_active_http_filter_precedes_limit_and_includes_all_active_states(app):
    coordinator = JobCoordinator(app.config['DATABASE'])
    expected = set()
    for state in sorted(ACTIVE_STATUSES):
        job, _ = coordinator.submit('classification', {'state':state}, idempotency_key=state)
        expected.add(job['id'])
        with app.app_context():
            db = get_db()
            db.execute('UPDATE background_jobs SET status=?,created_at=? WHERE id=?',
                       (state, '2000-01-01T00:00:00+00:00', job['id']))
            db.commit()
    for i in range(205):
        job, _ = coordinator.submit('analysis', {'i':i}, idempotency_key=f'terminal-{i}')
        with app.app_context():
            db = get_db()
            db.execute("UPDATE background_jobs SET status='succeeded' WHERE id=?", (job['id'],))
            db.commit()
    client = app.test_client()
    for limit in (50, 200):
        result = client.get(f'/api/v2/jobs?state=active&limit={limit}')
        assert result.status_code == 200, result.json
        assert {job['id'] for job in result.json['jobs']} == expected
    assert client.get('/api/v2/jobs?state=active&kind=analysis').json['jobs'] == []
    assert len(client.get('/api/v2/jobs?state=active&status=checkpointed').json['jobs']) == 1


def test_trash_http_pages_every_item_with_equal_deletion_times(app):
    with app.app_context():
        db = get_db()
        db.executemany("INSERT INTO mistakes(public_id,subject_id,qnum,content,date_added,deleted_at) "
            "VALUES(?,1,?,'fixture','2026-09-01','2026-09-20T12:00:00')",
            [(str(uuid.uuid4()), str(i)) for i in range(231)])
        db.commit()
    client = app.test_client()
    seen = []
    cursor = None
    while True:
        url = '/api/v2/trash/mistakes?limit=30' + ('&after='+cursor if cursor else '')
        response = client.get(url)
        assert response.status_code == 200, response.json
        page = response.json
        assert page['total'] == 231 and len(page['items']) <= 30
        seen.extend(item['public_id'] for item in page['items'])
        if not page['has_more']:
            assert page['next_cursor'] is None
            break
        assert page['next_cursor']
        cursor = page['next_cursor']
    assert len(seen) == len(set(seen)) == 231
    response = client.post(f'/api/v2/mistakes/{seen[-1]}/restore', json={'revision':1})
    assert response.status_code == 200, response.json
    assert client.get('/api/v2/trash/mistakes').json['total'] == 230
    assert client.get('/api/v2/trash/mistakes?after=invalid').status_code == 400
    assert client.get('/api/v2/trash/mistakes?limit=101').status_code == 400


def test_rebuild_http_reuses_active_and_respects_explicit_retry_identity(app, monkeypatch):
    from cuoti_app.routes import question_bank
    monkeypatch.setattr(question_bank, 'question_bank_status', lambda *args, **kwargs: {'source_snapshot':'fixture-source'})
    client = app.test_client()
    first = client.post('/api/v2/question-bank/rebuild', headers={'Idempotency-Key':'first'}).json['job']
    active = client.post('/api/v2/question-bank/rebuild', headers={'Idempotency-Key':'second'}).json['job']
    assert active['id'] == first['id']
    with app.app_context():
        db=get_db(); db.execute("UPDATE background_jobs SET status='failed' WHERE id=?", (first['id'],)); db.commit()
    assert client.post('/api/v2/question-bank/rebuild', headers={'Idempotency-Key':'first'}).json['job']['id'] == first['id']
    replacement = client.post('/api/v2/question-bank/rebuild').json['job']
    assert replacement['id'] != first['id'] and replacement['status'] == 'queued'
    assert client.post('/api/v2/question-bank/rebuild').json['job']['id'] == replacement['id']


def test_automatic_rebuild_after_failure_is_bounded(app, monkeypatch):
    from cuoti_app.routes import question_bank
    class Index:
        def coverage(self, **kwargs): return {'chapters':[]}
    app.config['TEX_BASE'] = str(Path(app.config['DATABASE']).parent)
    monkeypatch.setattr(question_bank, 'question_bank_status', lambda *a, **kw: {'source_snapshot':'auto-fixture','stale':True})
    monkeypatch.setattr(question_bank, 'open_last_good_question_bank_index', lambda *a: Index())
    coordinator = JobCoordinator(app.config['DATABASE'])
    client = app.test_client()
    assert client.get('/api/v2/question-bank/coverage').status_code == 200
    first = coordinator.list_jobs(kind='question-bank-rebuild')[0]
    with app.app_context():
        db=get_db(); db.execute("UPDATE background_jobs SET status='failed' WHERE id=?",(first['id'],)); db.commit()
    assert client.get('/api/v2/question-bank/coverage').status_code == 200
    second = coordinator.list_jobs(kind='question-bank-rebuild')[0]
    assert second['id'] != first['id'] and second['status'] == 'queued'
    with app.app_context():
        db=get_db(); db.execute("UPDATE background_jobs SET status='failed' WHERE id=?",(second['id'],)); db.commit()
    assert client.get('/api/v2/question-bank/coverage').status_code == 200
    assert len(coordinator.list_jobs(kind='question-bank-rebuild')) == 2
