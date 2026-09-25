from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread
import pytest
from cuoti_app.database import get_db
from cuoti_app.routes.exams_legacy import _store_exam_items
from tests.test_review_fixes import app


@pytest.mark.parametrize('late_submit,delete_exam', [(False, False), (True, False), (False, True)])
def test_attempt_rechecks_status_after_competing_request(app, monkeypatch, late_submit, delete_exam):
    from cuoti_app.routes import attempts
    with app.app_context():
        db = get_db()
        subject = db.execute('SELECT id FROM subjects LIMIT 1').fetchone()[0]
        exam_id = db.execute(
            "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
            "VALUES(?,'race','{}','fixture','2026-09-25','published')", (subject,),
        ).lastrowid
        _store_exam_items(db, exam_id, r'\begin{problem}fixture\end{problem}', source='free')
        db.commit()
    client = app.test_client()
    draft = client.post(f'/api/v2/exams/{exam_id}/attempts', json={}).json
    url = f"/api/v2/exam-attempts/{draft['public_id']}"
    read_draft, proceed = Event(), Event()
    original_begin = attempts.begin_immediate
    def synchronized_begin(db):
        if current_thread().name.startswith('stale-save'):
            read_draft.set()
            assert proceed.wait(15), 'competing request did not finish'
        return original_begin(db)
    monkeypatch.setattr(attempts, 'begin_immediate', synchronized_begin)
    def stale_request():
        with app.test_client() as stale_client:
            return stale_client.patch(url, json={
                'submit': late_submit, 'note': 'stale', 'max_score': 100,
                'items': [{'public_id': draft['items'][0]['public_id'],
                           'result_status': 'incorrect', 'assistance': 'hint',
                           'score': 0, 'comment': 'stale'}],
            })
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='stale-save') as pool:
        future = pool.submit(stale_request)
        try:
            assert read_draft.wait(15), 'first request did not reach write boundary'
            if delete_exam:
                response = client.delete(f'/api/v2/exams/{exam_id}')
                assert response.status_code == 200, response.json
            else:
                response = client.patch(url, json={
                    'submit': True, 'note': 'winner', 'max_score': 10, 'duration_seconds': 120,
                    'items': [{'public_id': draft['items'][0]['public_id'],
                               'result_status': 'correct', 'assistance': 'none',
                               'score': 8, 'comment': 'winner'}],
                })
                assert response.status_code == 200, response.json
            with app.app_context():
                db = get_db()
                before = [tuple(r) for r in db.execute('SELECT * FROM exam_attempts')]
                items_before = [tuple(r) for r in db.execute('SELECT * FROM exam_attempt_items')]
        finally:
            proceed.set()
        stale = future.result(timeout=15)
    assert stale.status_code == (404 if delete_exam else 409), stale.json
    assert stale.json['code'] == ('NOT_FOUND' if delete_exam else 'ATTEMPT_SUBMITTED')
    with app.app_context():
        db = get_db()
        assert [tuple(r) for r in db.execute('SELECT * FROM exam_attempts')] == before
        assert [tuple(r) for r in db.execute('SELECT * FROM exam_attempt_items')] == items_before


