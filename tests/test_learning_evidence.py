from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services import daily_review as daily
from cuoti_app.services.learning_evidence import remediation_status


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'fixture.db'),
                       'PDF_DIR': str(tmp_path / 'pdfs'), 'REGISTER_JOB_HANDLERS': False})


def add(db, status='未掌握'):
    public_id = str(uuid.uuid4())
    sid, cid = db.execute('SELECT subject_id,id FROM chapters ORDER BY id LIMIT 1').fetchone()
    db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added,status,review_stage,correct_streak,lapse_count) VALUES(?,?,?,'Q1','x=1','2026-09-17',?,2,2,3)", (public_id, sid, cid, status))
    db.commit()
    return dict(db.execute('SELECT * FROM mistakes WHERE public_id=?', (public_id,)).fetchone())


def session(db, *, selection=None):
    plan = daily.reserve_daily_review(db, selection=selection)
    db.execute("UPDATE daily_review_items SET status='published' WHERE round_id=?", (plan.round_id,))
    db.execute("UPDATE daily_review_rounds SET status='published' WHERE id=?", (plan.round_id,))
    db.commit()
    return plan


def test_assisted_review_keeps_progress_and_replays(app):
    with app.app_context():
        row = add(get_db())
    client = app.test_client()
    data = {'result': 'pass', 'assistance': 'hint', 'revision': row['revision'], 'client_event_id': str(uuid.uuid4())}
    response = client.post(f"/api/v2/mistakes/{row['public_id']}/review", json=data)
    assert response.status_code == 200, response.json
    assert response.json['review_stage'] == 2
    assert response.json['correct_streak'] == 2
    assert response.json['lapse_count'] == 3
    assert response.json['next_review'] == (datetime.now().astimezone().date() + timedelta(days=1)).isoformat()
    assert client.post(f"/api/v2/mistakes/{row['public_id']}/review", json=data).json['replayed']
    assert client.post(f"/api/v2/mistakes/{row['public_id']}/review", json={**data, 'assistance':'none'}).status_code == 409
    with app.app_context():
        event = get_db().execute('SELECT * FROM review_events').fetchone()
        assert event['event_type'] == 'assisted'
        assert event['evidence_version'] == 2


def test_mastered_daily_failure_has_independent_followup(app):
    with app.app_context():
        db = get_db()
        row = add(db, '已掌握')
        before = dict(db.execute('SELECT * FROM mistakes').fetchone())
        first = session(db)
        now = datetime.now(timezone.utc)
        daily.record_result(db, round_id=first.round_id, item_id=first.item_ids[0], result='again', assistance='none', now=now)
        pending = remediation_status(db, now)['items']
        assert pending[0]['public_id'] == row['public_id']
        selected = {'practice_kind':'remediation', 'source_item_ids':[pending[0]['source_item_id']], 'pdf_purpose':'self_test'}
        retry = session(db, selection=selected)
        assert retry.practice_mode == 'focused'
        assert db.execute('SELECT coverage_round FROM daily_review_items WHERE id=?', (retry.item_ids[0],)).fetchone()[0] is None
        daily.record_result(db, round_id=retry.round_id, item_id=retry.item_ids[0], result='correct', assistance='none', now=now)
        assert remediation_status(db, now)['total'] == 1
        later = session(db, selection=selected)
        daily.record_result(db, round_id=later.round_id, item_id=later.item_ids[0], result='correct', assistance='none', now=now + timedelta(days=1))
        assert remediation_status(db, now + timedelta(days=1))['total'] == 0
        assert dict(db.execute('SELECT * FROM mistakes').fetchone()) == before
        assert db.execute('SELECT COUNT(*) FROM review_events').fetchone()[0] == 0


def test_reopen_is_directional_and_idempotent(app):
    with app.app_context():
        row = add(get_db(), '已掌握')
    client = app.test_client()
    data = {'revision':row['revision'], 'client_event_id':str(uuid.uuid4())}
    url = f"/api/v2/mistakes/{row['public_id']}/reopen"
    assert client.post(url, json=data).json['status'] == '未掌握'
    assert client.post(url, json=data).json['status'] == '未掌握'
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM review_events').fetchone()[0] == 1


def test_repeated_question_does_not_create_independent_samples(app):
    with app.app_context():
        db = get_db()
        add(db)
        for _ in range(5):
            plan = session(db)
            daily.record_result(db, round_id=plan.round_id, item_id=plan.item_ids[0], result='correct', assistance='none')
        summary = daily.type_summary(db)[0]
        assert summary['correct'] == 5
        assert summary['independent_sample_count'] == 1
        assert summary['independent_accuracy'] is None


def test_weak_type_action_requires_current_candidates_in_scope(app, monkeypatch):
    from cuoti_app.services import mistake_classification as classification
    from cuoti_app.services.learning_evidence import learning_actions
    from cuoti_app.services.statistics import parse_scope
    with app.app_context():
        db = get_db()
        row = add(db)
        cid = db.execute("SELECT id FROM chapters WHERE subject_id=? AND name='数列极限'", (row['subject_id'],)).fetchone()[0]
        db.execute('UPDATE mistakes SET chapter_id=? WHERE id=?', (cid, row['id']))
        db.commit()
        row = classification.mistake_row(db, row['public_id'])
        code = db.execute("SELECT code FROM question_types WHERE name='数列收敛与发散判定'").fetchone()[0]
        classification.publish_prediction(db, classification.mistake_row(db, row['public_id']),
            {'primary_type_code': code, 'knowledge_points': ['数列极限'], 'methods': ['子列法'],
             'confidence': 0.95}, job_id=uuid.uuid4().hex, base_revision=0)
        db.commit()
        monkeypatch.setattr(daily, 'type_summary', lambda *_a, **_kw: [{
            'primary_type_code': code, 'type_name': '数列收敛与发散判定',
            'independent_accuracy': 0.2, 'independent_sample_count': 5}])
        scope = parse_scope({'subject_id': row['subject_id'], 'chapter_id': row['chapter_id']})
        assert any(action['title'] == '数列收敛与发散判定' for action in learning_actions(db, scope))
        other = db.execute('SELECT id FROM chapters WHERE subject_id=? AND id<>? LIMIT 1',
                           (row['subject_id'], row['chapter_id'])).fetchone()[0]
        scope = parse_scope({'subject_id': row['subject_id'], 'chapter_id': other})
        assert not any(action['title'] == '数列收敛与发散判定' for action in learning_actions(db, scope))


def test_content_changes_do_not_reuse_remediation_evidence(app):
    with app.app_context():
        db = get_db()
        row = add(db)
        plan = session(db)
        daily.record_result(db, round_id=plan.round_id, item_id=plan.item_ids[0], result='again', assistance='none')
        assert remediation_status(db)['total'] == 1
        db.execute("UPDATE mistakes SET content='x=2' WHERE id=?", (row['id'],))
        db.commit()
        assert remediation_status(db)['total'] == 0
        assert remediation_status(db)['recovered_count'] == 0


def test_assisted_statistics_and_portable_evidence(app):
    from tempfile import TemporaryFile
    from pathlib import Path
    import zipfile
    from cuoti_app.services.portable_backup import write_portable_backup
    from cuoti_app.services.statistics import build_statistics, build_statistics_legacy, parse_scope
    from cuoti_app.services.import_validation import parse_and_validate_import
    with app.app_context():
        row = add(get_db())
    response = app.test_client().post(f"/api/v2/mistakes/{row['public_id']}/review", json={'result':'pass','assistance':'hint','revision':row['revision'],'client_event_id':str(uuid.uuid4())})
    assert response.status_code == 200
    with app.app_context():
        db = get_db()
        current = build_statistics(db, parse_scope({}))
        legacy = build_statistics_legacy(db, parse_scope({}))
        assert current['summary'] == legacy['summary']
        assert current['summary']['assisted_count'] == 1
        assert current['summary']['pass'] == 0
        assert current['summary']['again'] == 0
        stream = TemporaryFile()
        write_portable_backup(db, Path(app.config['ATTACHMENT_DIR']), stream)
        with zipfile.ZipFile(stream) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['format_version'] == 7
        rows = manifest['mistakes']
        assert rows[0]['reviews'][0]['assistance'] == 'hint'
        parsed = parse_and_validate_import(json.dumps(manifest).encode('utf-8'), db)
        assert parsed[1][0]['review_records'][0]['assistance'] == 'hint'
        stream.close()
