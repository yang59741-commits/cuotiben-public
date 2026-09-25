from cuoti_app.services.past_exam_scoring import extract_explicit_scoring


def extract(headings, contents, text=''):
    sections, problems, number = [], [], 1
    for heading, bodies in zip(headings, contents):
        numbers = list(range(number, number + len(bodies)))
        sections.append({'label': heading, 'question_numbers': numbers})
        problems.extend({'source_number': n, 'content': body} for n, body in zip(numbers, bodies))
        number += len(bodies)
    return extract_explicit_scoring({'sections': sections}, problems, text)


def test_explicit_points_are_summed_without_assuming_150():
    result = extract(['试题'], [['（10 分）证明结论。', '（20分）求矩阵。']])
    assert result['mode'] == 'verified'
    assert result['slot_points'] == {'1': 10, '2': 20}
    assert result['total_points'] == 30


def test_section_per_question_points_and_decimal_points():
    result = extract(['一、填空题，每题2.5分，共5分。'], [['求极限。', '求导数。']], '满分5分')
    assert result['mode'] == 'verified'
    assert result['slot_points'] == {'1': 2.5, '2': 2.5}


def test_missing_points_are_not_filled_from_total_or_subquestions():
    result = extract(['试题，共30分'], [['（10分）求极限。', '设函数连续。\n（1）（20分）证明结论。']], '满分30分')
    assert result['mode'] == 'unscored'
    assert result['slot_points'] == {}
    assert 'missing_slot_points:2' in result['evidence']['issues']


def test_conflicting_hnnu_style_section_stays_unscored():
    result = extract(['简答题，每题5分，共30分'], [['证明结论。'] * 5])
    assert result['mode'] == 'unscored'
    assert any(issue.startswith('conflicting_section_total') for issue in result['evidence']['issues'])


def test_explicit_slot_conflict_and_paper_total_conflict():
    conflict = extract(['每题10分'], [['（15分）证明结论。']])
    assert conflict['mode'] == 'unscored'
    assert 'conflicting_slot_points:1' in conflict['evidence']['issues']
    total = extract(['试题'], [['（10分）证明结论。']], '试卷总分15分')
    assert total['mode'] == 'unscored'
    assert 'conflicting_paper_total' in total['evidence']['issues']


def test_numbered_heading_scores_use_printed_numbers_not_global_slot_ids():
    structure = {'sections': [{'label': '七．（第1，2题每题15分，第3题20分）',
                              'question_numbers': [7, 8, 9], 'raw_question_numbers': ['1', '2', '3']}]}
    problems = [{'source_number': n, 'content': '证明给定结论。'} for n in (7, 8, 9)]
    result = extract_explicit_scoring(structure, problems, '')
    assert result['slot_points'] == {'7': 15, '8': 15, '9': 20}
    assert result['total_points'] == 50
    problems[0]['content'] = '（10分）证明给定结论。'
    result = extract_explicit_scoring(structure, problems, '')
    assert result['mode'] == 'unscored'
    assert 'conflicting_slot_points:7' in result['evidence']['issues']
