from datetime import datetime, timedelta, timezone
import json
import uuid

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services import daily_review as daily, mistake_classification as classification
from cuoti_app.services import learning_evidence
from tests.test_review_optimization import add, value, publish


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'fixture.db'),
                       'PDF_DIR': str(tmp_path / 'pdfs'), 'REGISTER_JOB_HANDLERS': False})


def classified(db, *, method='subsequence_method', role='primary', automatic=False):
    row = add(db)
    payload = value(db, method_annotations=[{'code': method, 'role': role}])
    if automatic:
        classification.publish_prediction(db, row, payload, job_id=uuid.uuid4().hex, base_revision=0)
        db.commit()
    else:
        classification.update_classification(db, row['public_id'], {**payload, 'revision': 0})
    return row


def practice(db, row, *, result='correct', assistance='none', now=None):
    plan = daily.reserve_daily_review(db, selection={
        'practice_mode': 'focused', 'subject_id': row['subject_id'],
        'method_code': 'subsequence_method', 'count': 5,
    })
    publish(db, plan)
    item_id = db.execute('SELECT id FROM daily_review_items WHERE round_id=? AND mistake_id=?',
                         (plan.round_id, row['id'])).fetchone()[0]
    daily.record_result(db, round_id=plan.round_id, item_id=item_id,
                        result=result, assistance=assistance, now=now)
    return plan, item_id


def test_methods_use_frozen_primary_role_and_accept_auto_and_manual(app):
    with app.app_context():
        db = get_db()
        first, second = classified(db), classified(db, automatic=True)
        plan, _ = practice(db, first)
        classification.update_classification(db, first['public_id'], {
            **value(db, method_annotations=[{'code': 'squeeze_method', 'role': 'primary'}]), 'revision': 1})
        result = daily.method_summary(db, session_id=plan.round_id)
        assert len(result['items']) == 1
        assert result['items'][0]['method_code'] == 'subsequence_method'
        assert result['items'][0]['unique_mistake_count'] == 2
        assert result['items'][0]['independent_accuracy'] is None
        assert daily.method_summary(db, subject_id=9999)['items'] == []
        assert first['subject_id'] == second['subject_id']


def test_independent_samples_are_unique_and_hints_do_not_inflate_accuracy(app):
    with app.app_context():
        db = get_db()
        rows = [classified(db, automatic=bool(i % 2)) for i in range(6)]
        plan, _ = practice(db, rows[0], result='again')
        # Explicitly cover every frozen question in this batch.
        items = db.execute('SELECT id,mistake_id FROM daily_review_items WHERE round_id=?', (plan.round_id,)).fetchall()
        for index, item in enumerate(items):
            daily.record_result(db, round_id=plan.round_id, item_id=item['id'],
                                result='again' if index == 0 else 'correct', assistance='none')
        summary = daily.method_summary(db, session_id=plan.round_id)['items'][0]
        assert summary['independent_sample_count'] == 5
        assert summary['independent_accuracy'] == .8
        chosen = next(row for row in rows if row['id'] == items[1]['mistake_id'])
        practice(db, chosen, assistance='hint')
        summary = daily.method_summary(db)['items'][0]
        assert summary['independent_sample_count'] == 5
        assert summary['assisted_count'] == 1
        assert summary['independent_accuracy'] == .8


def test_legacy_unspecified_and_missing_methods_are_unattributed(app):
    with app.app_context():
        db = get_db()
        row = classified(db)
        plan, item_id = practice(db, row)
        snapshot = json.loads(db.execute('SELECT snapshot_json FROM daily_review_items WHERE id=?', (item_id,)).fetchone()[0])
        snapshot['classification']['method_annotations'][0]['role'] = 'unspecified'
        db.execute('UPDATE daily_review_items SET snapshot_json=? WHERE id=?', (json.dumps(snapshot), item_id))
        db.commit()
        result = daily.method_summary(db, session_id=plan.round_id)
        assert result == {'items': [], 'unattributed_count': 1}


def test_method_recovery_uses_existing_cross_day_evidence_and_matching_content(app):
    with app.app_context():
        db = get_db()
        row = classified(db)
        now = datetime.now(timezone.utc) - timedelta(days=2)
        plan, _ = practice(db, row, result='again', now=now)
        practice(db, row, now=now)
        assert daily.method_summary(db, session_id=plan.round_id)['items'][0]['remediation_count'] == 1
        practice(db, row, now=now + timedelta(days=1))
        assert daily.method_summary(db, session_id=plan.round_id)['items'][0]['recovered_count'] == 1
        db.execute("UPDATE mistakes SET content='题面已经修改' WHERE id=?", (row['id'],))
        db.commit()
        summary = daily.method_summary(db, session_id=plan.round_id)['items'][0]
        assert summary['remediation_count'] == summary['recovered_count'] == 0


def test_method_endpoint_validates_scope(app):
    client = app.test_client()
    assert client.get('/api/v2/daily-review/method-summary').json == {'items': [], 'unattributed_count': 0}
    for query in ('session_id=bad', 'subject_id=0', 'chapter_id=-1'):
        assert client.get('/api/v2/daily-review/method-summary?' + query).status_code == 400


def test_learning_actions_reuses_one_remediation_read(app, monkeypatch):
    from cuoti_app.services.statistics import parse_scope
    calls = []
    original = learning_evidence.remediation_status
    def counted(db):
        calls.append(1)
        return original(db)
    monkeypatch.setattr(learning_evidence, 'remediation_status', counted)
    monkeypatch.setattr(daily, 'remediation_status', counted)
    with app.app_context():
        learning_evidence.learning_actions(get_db(), parse_scope({}))
    assert len(calls) == 1
