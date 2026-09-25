import pytest

from cuoti_app.services.exam_generation_similarity import (
    PairSimilarity, SimilarityThresholds, calibrate_similarity_thresholds,
    compare_similarity_questions, evaluate_similarity_thresholds, is_near_duplicate,
    prepare_similarity_question, normalize_similarity_text, calibrated_similarity_rule,
    build_similarity_freeze, validate_similarity_freeze, extend_similarity_freeze,
)


def question(identity, text, **overrides):
    values = dict(content_hash=identity, content=text, knowledge_points=['limit'],
                  methods=['Taylor'], question_type_family='proof')
    return prepare_similarity_question(**(values | overrides))


def test_exact_and_unknown_do_not_enter_near_duplicate_counts():
    first = question('a', 'Find the limit of f(x).')
    exact = compare_similarity_questions(first, first)
    unknown = compare_similarity_questions(first, question('b', 'Find f(x).', methods=[]))
    assert exact.status == 'exact_content'
    assert unknown.status == 'unknown_incomplete_evidence'
    assert unknown.semantic_bps is None
    assert not is_near_duplicate(exact, SimilarityThresholds(0, 0))
    assert not is_near_duplicate(unknown, SimilarityThresholds(0, 0))
    unknown_words = compare_similarity_questions(first, question('c', 'Find f(x).', methods=['n/a']))
    assert unknown_words.status == 'unknown_incomplete_evidence'
    counts = evaluate_similarity_thresholds([(unknown_words, False)], SimilarityThresholds(0, 0))
    assert counts['unknown'] == 1 and counts['true_negative'] == 0


def test_body_evidence_distinguishes_same_knowledge_different_questions():
    first = question('a', r'Find the limit of \frac{\sin(2x)}{x} as x tends to zero.')
    variant = question('b', r'Find the limit of \frac{\sin(3x)}{x} as x tends to zero.')
    different = question('c', 'Prove uniform convergence of the sequence on a compact interval.')
    near = compare_similarity_questions(first, variant)
    other = compare_similarity_questions(first, different)
    assert near.semantic_bps == other.semantic_bps == 10000
    assert near.text_bps > other.text_bps
    thresholds = SimilarityThresholds(10000, near.text_bps)
    assert is_near_duplicate(near, thresholds)
    assert not is_near_duplicate(other, thresholds)
    assert compare_similarity_questions(variant, first) == near


def test_math_tokens_are_preserved_by_preparation():
    negative = question('a', r'1. x < 0, x^2 + 1 = 2')
    positive = question('b', r'2. x > 0, x^2 - 1 = 3')
    assert '<' in negative.text and '+' in negative.text
    assert '>' in positive.text and '-' in positive.text
    assert compare_similarity_questions(negative, positive).text_bps < 10000


def test_calibration_is_deterministic_and_prefers_stricter_text_on_ties():
    samples = [
        (PairSimilarity('available', 9000, 8500), True),
        (PairSimilarity('available', 9500, 9000), True),
        (PairSimilarity('available', 10000, 7500), False),
        (PairSimilarity('available', 8000, 9500), False),
    ]
    thresholds = calibrate_similarity_thresholds(samples)
    assert thresholds == SimilarityThresholds(9000, 8500)
    assert thresholds == calibrate_similarity_thresholds(list(reversed(samples)))
    counts = evaluate_similarity_thresholds(samples, thresholds)
    assert counts['true_positive'] == 2 and counts['false_positive'] == 0


def test_calibration_does_not_silently_claim_success_for_indistinguishable_pairs():
    pair = PairSimilarity('available', 9000, 8500)
    samples = [(pair, True), (pair, False)]
    thresholds = calibrate_similarity_thresholds(samples)
    counts = evaluate_similarity_thresholds(samples, thresholds)
    assert counts['false_positive'] == 0 and counts['false_negative'] == 1


def test_equivalent_tex_layout_does_not_change_math_tokens():
    pretty = r'\[\displaystyle \left(\dfrac{x^{2}+1}{y}\right)\;\geqslant 2\]'
    compact = r'\((\frac{x^{2}+1}{y})\geq 2\)'
    assert normalize_similarity_text(pretty) == normalize_similarity_text(compact)
    assert normalize_similarity_text(r'x\to 0, x\ne 1, x\le 2') == r'x\rightarrow0,x\neq1,x\leq2'
    assert normalize_similarity_text(r'\int f(x)\,\mathrm{~d}x') == r'\intf(x)\mathrm{d}x'


def test_layout_normalization_keeps_grouping_vector_fonts_rows_and_unknown_commands():
    raw = r'\begin{matrix}a&b\\c&d\end{matrix}+\mathbf{x}+x^{12}+x^12+\leftarrow+\unknown{q}+\~n'
    assert normalize_similarity_text(raw) == raw
    left = question('a', r'\(x<0, \frac{x+1}{y-2}\)')
    right = question('b', r'\(x>0, \frac{x-1}{y+2}\)')
    assert left.text != right.text
    assert '<' in left.text and '+' in left.text and '-' in left.text
    assert '>' in right.text and '-' in right.text and '+' in right.text


def test_only_confirmed_boundary_source_labels_are_removed():
    stem = r'证明：\(x^{n}+1>0\)。'
    source = '同济大学'
    assert normalize_similarity_text('例 3．1．12 ' + stem + '（同济大学）', source_labels=[source]) == normalize_similarity_text(stem)
    assert normalize_similarity_text('【例 3.12】（同济大学，2014年）' + stem, source_labels=[source]) == normalize_similarity_text(stem)
    assert normalize_similarity_text('（15 分）' + stem) == normalize_similarity_text(stem)
    assert '同济大学' in normalize_similarity_text(stem + '（同济大学）')
    condition = r'设 \(x\)（非负数），证明 \(x+1>0\)（当x为实数）。'
    assert normalize_similarity_text(condition, source_labels=['非负数', '当x为实数', source]) == normalize_similarity_text(condition)
    assert '同济大学' in normalize_similarity_text('题目（同济大学）条件仍在后面', source_labels=[source])
    assert normalize_similarity_text(r'\(15x+3.12=0\)') == '15x+3.12=0'


def test_calibrated_math_rule_recognizes_source_variant_but_not_different_limit():
    rule = calibrated_similarity_rule('数学分析')
    thresholds = SimilarityThresholds(**rule['thresholds'])
    left = question('a', r'例 3．1．12 证明：\( \left(x^{n-1} e^{\frac{1}{x}}\right)^{(n)}=\frac{(-1)^{n}}{x^{n+1}} e^{\frac{1}{x}} \) ．（同济大学）', source_labels=['同济大学'])
    right = question('b', r'设 \( y=x^{n-1} e^{\frac{1}{x}} \) ，证明：\( y^{(n)}=\frac{(-1)^{n}}{x^{n+1}} e^{\frac{1}{x}} \) ．')
    assert is_near_duplicate(compare_similarity_questions(left, right), thresholds)
    first_limit = question('c', r'求极限 \[ \lim _{n \rightarrow \infty}\left(\frac{1}{\sqrt{n^{2}+1^{2}}}+\frac{1}{\sqrt{n^{2}+2^{2}}}+\cdots+\frac{1}{\sqrt{n^{2}+n^{2}}}\right) . \]')
    second_limit = question('d', r'（10 分）求极限 \[ \lim _{n \rightarrow \infty}\left(\frac{\pi}{\sqrt{n^{2}-1}}+\frac{\pi}{\sqrt{n^{2}-2}}+\cdots+\frac{\pi}{\sqrt{n^{2}-2 n}}\right) . \]')
    assert not is_near_duplicate(compare_similarity_questions(first_limit, second_limit), thresholds)
    assert rule['version'] == 'd2-near-duplicate-evidence-v3'
    rule['thresholds']['text_bps'] = 0
    assert calibrated_similarity_rule('数学分析')['thresholds']['text_bps'] == 8791
    assert calibrated_similarity_rule('高等代数')['thresholds'] == {'semantic_bps': 7000, 'text_bps': 8818}


def test_similarity_freeze_limits_pairs_and_keeps_unknown_history_explicit():
    import hashlib, json

    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def candidate(cid, content):
        snapshot = {'schema_version': 'semantic-candidate-content-snapshot-v1', 'content': content,
                    'conditions': [], 'figure_references': [], 'options': []}
        return {'candidate_id': cid, 'source_kind': 'past_exam_local', 'content_hash': digest(cid),
                'card_version_id': 1, 'card_hash': digest('card-' + cid),
                'content_snapshot': json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')),
                'content_snapshot_hash': digest(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':')))}

    rows = [candidate('A', r'求 \(\lim_{x\to0}\frac{\sin(2x)}{x}\)。'),
            candidate('B', r'证明 \(x^2+1>0\)。'),
            candidate('C', r'求 \(\lim_{x\to0}\frac{\sin(3x)}{x}\)。')]
    edges = [dict(candidate_id=row['candidate_id'], candidate_fact_hash=digest('fact-' + row['candidate_id']),
                  content_hash=row['content_hash']) for row in rows]
    cards = {(row['source_kind'], row['candidate_id']): {'knowledge_points': ['极限'], 'methods': ['Taylor'],
             'question_type': '计算题'} for row in rows}
    history = [candidate('H', r'求 \(\lim_{x\to0}\frac{\sin(4x)}{x}\)。') | {'order_rank': 2},
               {'candidate_id': 'old-unknown', 'content_hash': digest('old-unknown'), 'source_kind': 'past_exam_local', 'order_rank': 1}]
    cards[('past_exam_local', 'H')] = {'knowledge_points': ['极限'], 'methods': ['Taylor'], 'question_type': '计算题'}
    receipt = build_similarity_freeze(subject='数学分析', candidates=rows, graph_edges=edges,
                                       card_facts=cards, history_candidates=history)
    assert len(receipt['candidate_inputs']) == 3
    assert len(receipt['pairs']) == 3
    assert receipt['historical_near_usage']['A']['success_count'] == 1
    assert receipt['history_inputs'][1]['status'] == 'unknown_incomplete_evidence'
    graph = {'edges': edges}
    assert validate_similarity_freeze(receipt, graph=graph, subject='数学分析')['freeze_hash'] == receipt['freeze_hash']
    receipt['pairs'][0]['near_duplicate'] = not receipt['pairs'][0]['near_duplicate']
    with pytest.raises(ValueError, match='similarity_freeze_hash_invalid'):
        validate_similarity_freeze(receipt, graph=graph, subject='数学分析')


def test_similarity_freeze_extension_retains_old_facts_and_adds_only_new_pairs():
    import hashlib, json

    def digest(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def candidate(cid, content):
        snapshot = {'schema_version': 'semantic-candidate-content-snapshot-v1', 'content': content,
                    'conditions': [], 'figure_references': [], 'options': []}
        raw_snapshot = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {'candidate_id': cid, 'source_kind': 'past_exam_local', 'content_hash': digest('content-' + cid),
                'card_version_id': 1, 'card_hash': digest('card-' + cid),
                'content_snapshot': raw_snapshot, 'content_snapshot_hash': digest(raw_snapshot)}

    rows = [candidate('A', r'求 \(\lim_{x\to0}\frac{\sin(2x)}{x}\)。'),
            candidate('B', r'证明 \(x^2+1>0\)。'),
            candidate('C', r'求 \(\lim_{x\to0}\frac{\sin(3x)}{x}\)。')]
    def edge(row):
        return {'candidate_id': row['candidate_id'], 'candidate_fact_hash': digest('fact-' + row['candidate_id']),
                'content_hash': row['content_hash']}
    cards = {(row['source_kind'], row['candidate_id']): {'knowledge_points': ['极限'], 'methods': ['Taylor'],
             'question_type': '计算题'} for row in rows}
    cards[('past_exam_local', 'B')] = {'knowledge_points': ['证明'], 'methods': ['直接证明'],
                                       'question_type': '证明题'}
    initial = build_similarity_freeze(subject='数学分析', candidates=rows[:2], graph_edges=[edge(row) for row in rows[:2]],
                                      card_facts=cards)
    expanded = extend_similarity_freeze(initial, subject='数学分析', candidates=rows,
                                        graph_edges=[edge(row) for row in rows], card_facts=cards)
    assert len(initial['candidate_inputs']) == 2
    assert len(initial['pairs']) == 1
    assert len(expanded['candidate_inputs']) == 3
    assert len(expanded['pairs']) == 3
    old_pair = next(row for row in expanded['pairs']
                    if {row['left_candidate_id'], row['right_candidate_id']} == {'A', 'B'})
    assert old_pair == next(row for row in initial['pairs']
                            if {row['left_candidate_id'], row['right_candidate_id']} == {'A', 'B'})
    assert validate_similarity_freeze(expanded, graph={'edges': [edge(row) for row in rows]}, subject='数学分析')


def test_text_similarity_uses_the_smaller_direction_without_autojunk():
    assert compare_similarity_questions(question('a', 'tide'), question('b', 'diet')).text_bps == 2500
    left, right = 'a' * 210 + 'b', 'c' + 'a' * 210
    pair = compare_similarity_questions(question('a', left), question('b', right))
    assert pair.text_bps == 20000 * 210 // (len(left) + len(right))


def test_review_evaluation_freezes_inputs_labels_and_measured_outcomes():
    from tools.calibrate_d2_similarity import evaluate

    pairs, labels = [], []
    for index in range(24):
        positive = index % 2 == 0
        def side(identity, content):
            return {'content_hash': identity, 'content': content,
                    'card': {'knowledge_points': ['limit'], 'methods': ['Taylor'], 'question_type': 'proof'}}
        pair_id = str(index)
        pairs.append({'pair_id': pair_id, 'subject': 'test', 'proposal': 'not_a_label',
                      'left': side(f'a{index}', 'abcdefghij'),
                      'right': side(f'b{index}', 'abcdefghiX' if positive else 'klmnopqrst')})
        labels.append({'pair_id': pair_id, 'split': 'calibration' if index < 16 else 'validation',
                       'near_duplicate': positive, 'reason': 'reviewed before fitting'})
    result = evaluate({'pairs': pairs}, labels)
    assert result['subjects']['test']['accepted']
    assert len(result['reviewed_pairs']) == 24
    evidence = result['reviewed_pairs'][0]
    assert evidence['left'] == pairs[0]['left']
    assert evidence['reason'] == labels[0]['reason']
    assert evidence['comparison']['text_bps'] == 9000
    assert evidence['predicted_near_duplicate'] is True
    assert result['provider_calls'] == result['database_writes'] == 0
