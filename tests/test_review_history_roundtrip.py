"""Roundtrip and startup must preserve the meaning of manual review events."""
import json
import io
import zipfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from cuoti_app.database import get_db, init_db
from cuoti_app.services.mistakes import review_mistake, toggle_mistake
from tests.test_review_fixes import app


def add_mistake(app):
    response = app.test_client().post('/api/v2/mistakes', json={
        'subject_id': 1, 'chapter_id': 1, 'qnum': 'roundtrip', 'content': 'synthetic',
        'date_added': '2026-09-01'})
    assert response.status_code == 201, response.json
    return response.json['public_id']


def state(app, public_id):
    with app.app_context():
        return dict(get_db().execute('SELECT review_count,last_review,status,correct_streak,'
            'lapse_count,review_stage,review_cycle,next_review,schedule_version FROM mistakes WHERE public_id=?',
            (public_id,)).fetchone())


@pytest.mark.parametrize('events', [
    ['manual'], ['pass', 'manual'], ['again', 'manual', 'manual', 'assisted', 'pass'],
    ['pass', 'pass', 'pass', 'pass'],
])
@pytest.mark.parametrize('mode', ['merge', 'replace'])
def test_portable_roundtrip_counts_only_review_events(app, events, mode):
    public_id = add_mistake(app)
    with app.app_context():
        db = get_db()
        for i, event in enumerate(events):
            row = db.execute('SELECT revision FROM mistakes WHERE public_id=?', (public_id,)).fetchone()
            now = datetime(2026, 9, 2, 12, tzinfo=timezone.utc) + timedelta(days=i)
            if event == 'manual':
                toggle_mistake(db, public_id, revision=row['revision'], now=now)
            else:
                review_mistake(db, public_id, revision=row['revision'], now=now,
                    result='pass' if event == 'assisted' else event,
                    assistance='hint' if event == 'assisted' else 'none', client_event_id=str(uuid.uuid4()))
    before = state(app, public_id)
    client = app.test_client()
    response = client.get('/api/v3/export')
    assert response.status_code == 200
    raw = response.data
    response.close()
    preview = client.post(f'/api/v3/import/preview?mode={mode}', data=raw)
    assert preview.status_code == 200, preview.json
    result = client.post(f'/api/v3/import/commit?mode={mode}', data=raw,
        headers={'X-Import-Preview-Token': preview.json['preview_token']})
    assert result.status_code == 200, result.json
    assert state(app, public_id) == before


@pytest.mark.parametrize('streak', [0, 1, 2])
def test_manual_mastery_survives_repeated_startup(app, streak):
    public_id = add_mistake(app)
    with app.app_context():
        db = get_db()
        for i in range(streak):
            review_mistake(db, public_id, revision=i+1, now=datetime.now().astimezone(),
                result='pass', assistance='none', client_event_id=str(uuid.uuid4()))
        toggle_mistake(db, public_id, revision=streak+1, now=datetime.now().astimezone())
    before = state(app, public_id)
    with app.app_context():
        init_db()
        init_db()
    assert state(app, public_id) == before
    assert before['correct_streak'] == streak


def test_incomplete_import_preserves_explicit_streak_and_counters(app):
    public_id = add_mistake(app)
    response = app.test_client().get('/api/v3/export')
    with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    response.close()
    item = manifest['mistakes'][0]
    item.update(public_id=str(uuid.uuid4()), history_complete=False, status='已掌握',
                correct_streak=0, review_count=7, last_review='2026-09-03', reviews=[])
    raw = json.dumps(manifest).encode()
    client = app.test_client()
    preview = client.post('/api/v3/import/preview', data=raw)
    assert preview.status_code == 200, preview.json
    response = client.post('/api/v3/import/commit', data=raw,
        headers={'X-Import-Preview-Token':preview.json['preview_token']})
    assert response.status_code == 200, response.json
    restored = state(app, item['public_id'])
    assert (restored['correct_streak'], restored['review_count'], restored['last_review']) == (0, 7, '2026-09-03')
