"""Learning outcomes shared by original-question practice and mistake exams."""

from datetime import datetime, timedelta

import pytest

from tests.test_review_optimization import app, db, add, auto, value, publish
from cuoti_app.services import daily_review as daily
from cuoti_app.services.exam_generation_quality import QualityPaperInput, validate_draft_quality_inputs
from cuoti_app.services.exam_quality_review import ExamQualityError
from cuoti_app.services.exam_repetition_policy import question_content_fingerprint
from cuoti_app.services.mistakes import review_mistake
import uuid


def record(db, row, result, assistance, now, *, focused=False):
    selection = {'practice_mode': 'focused', 'primary_type_code': value(db)['primary_type_code']} if focused else None
    plan = daily.reserve_daily_review(db, selection=selection, count=10)
    publish(db, plan)
    item_id = dict(zip(plan.mistake_ids, plan.item_ids))[row['id']]
    daily.record_result(db, round_id=plan.round_id, item_id=item_id,
                        result=result, assistance=assistance, now=now)
    return item_id


def test_recommendations_share_recovery_across_practice_modes(db):
    recovered, wrong, hinted = [add(db, content=f'计算极限 {i}。') for i in range(3)]
    for row in (recovered, wrong, hinted):
        auto(db, row)
    now = datetime.now().astimezone()
    record(db, recovered, 'again', 'none', now - timedelta(days=3))
    record(db, recovered, 'correct', 'none', now - timedelta(days=2), focused=True)
    record(db, wrong, 'again', 'none', now - timedelta(days=1), focused=True)
    record(db, hinted, 'correct', 'hint', now, focused=True)

    result = daily.recommendations(db)['items']
    assert [item['id'] for item in result] == [wrong['id'], hinted['id']]
    assert [item['again_count'] for item in result] == [1, 0]
    assert [item['repeated_again_count'] for item in result] == [1, 0]
    assert result[1]['last_again_at'] is None
    # A same-day independent success is not yet cross-day recovery.
    record(db, hinted, 'correct', 'none', now, focused=True)
    assert hinted['id'] in [item['id'] for item in daily.recommendations(db)['items']]
    fresh = db.execute('SELECT * FROM mistakes WHERE id=?', (hinted['id'],)).fetchone()
    review_mistake(db, hinted['public_id'], client_event_id=str(uuid.uuid4()),
                   revision=fresh['revision'], result='pass', assistance='none',
                   now=now + timedelta(days=1))
    assert [item['id'] for item in daily.recommendations(db)['items']] == [wrong['id']]


@pytest.mark.parametrize('change', ['legacy', 'content', 'deleted', 'mastered'])
def test_recommendations_do_not_reuse_ineligible_evidence(db, change):
    row = add(db)
    item_id = record(db, row, 'again', 'none', datetime.now().astimezone())
    assert daily.recommendations(db)['total'] == 1
    if change == 'legacy':
        db.execute('UPDATE daily_review_items SET evidence_version=1 WHERE id=?', (item_id,))
    else:
        column, changed = {'content': ('content', '新题面'), 'deleted': ('deleted_at', '2026-09-26'),
                           'mastered': ('status', '已掌握')}[change]
        db.execute(f'UPDATE mistakes SET {column}=? WHERE id=?', (changed, row['id']))
    db.commit()
    assert daily.recommendations(db)['items'] == []


def test_recommendations_keep_subject_and_chapter_scope(db):
    row = add(db)
    record(db, row, 'again', 'none', datetime.now().astimezone())
    assert daily.recommendations(db, subject_id=row['subject_id'], chapter_id=row['chapter_id'])['total'] == 1
    assert daily.recommendations(db, subject_id=999)['total'] == 0
    assert daily.recommendations(db, chapter_id=999)['total'] == 0


def test_focused_practice_prioritizes_hint_before_unattempted_questions(db):
    rows = [add(db, content=f'证明数列 {i} 收敛。') for i in range(6)]
    for row in rows:
        auto(db, row)
    now = datetime.now().astimezone()
    for index, result, assistance in [(0, 'again', 'none'), (1, 'correct', 'hint'),
                                       (2, 'incomplete', 'none'), (4, 'correct', 'none'),
                                       (5, 'correct', 'unknown')]:
        record(db, rows[index], result, assistance, now, focused=True)
    candidates = daily._focused_candidates(db, {'primary_type_code': value(db)['primary_type_code'],
                                              'include_mastered': False, 'chapter_id': None})
    assert [item['id'] for item in candidates] == [row['id'] for row in rows]
    assert candidates[-1]['recent_assistance'] == 'unknown'


def quality_paper(contents, exempt=()):
    return QualityPaperInput(
        body='\n'.join(r'\begin{problem}' + text + r'\end{problem}' for text in contents),
        items=tuple({'position': i, 'content': text, 'chapter': '极限', 'type': '计算题',
                     'difficulty': '中等', 'reference_ids': ['m1']} for i, text in enumerate(contents, 1)),
        reference_evidence={'m1': {'content': '原错题'}},
        repetition_exempt_fingerprints=tuple(exempt),
    )


def test_unmastered_exemption_does_not_allow_duplicate_slots_before_ai():
    text = '求数列 1/n 的极限。'
    paper = quality_paper([text, text], [question_content_fingerprint(text)])
    with pytest.raises(ExamQualityError) as error:
        validate_draft_quality_inputs((paper,), allowed_chapters=('极限',), allowed_question_types=('计算题',))
    assert error.value.code == 'QUALITY_DRAFT_INVALID'
    assert error.value.failed_positions == (2,)


def test_cross_paper_exemption_and_nonidentical_variants_still_work():
    text = '求数列 1/n 的极限。'
    exempt = [question_content_fingerprint(text)]
    validate_draft_quality_inputs((quality_paper([text]), quality_paper([text], exempt)),
                                  allowed_chapters=('极限',), allowed_question_types=('计算题',))
    validate_draft_quality_inputs((quality_paper([text, '求数列 2/n 的极限。'], exempt),),
                                  allowed_chapters=('极限',), allowed_question_types=('计算题',))
