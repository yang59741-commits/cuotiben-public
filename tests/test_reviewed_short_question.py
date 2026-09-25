from types import SimpleNamespace

from cuoti_app.services.question_bank_parser import Candidate, candidate_exclusion_reason


def test_reviewed_short_theorem_survives_heading_cleanup_without_opening_other_fragments():
    filename = 'chapterB3_18_多元函数极限.tex'
    content = '证明：连续映射把紧集映为紧集．'
    candidate = Candidate(17, 17, 'subsection', 100, content)
    catalog = SimpleNamespace(exclusions=[])
    assert candidate_exclusion_reason(filename, candidate, [], catalog) is None
    assert candidate_exclusion_reason('other.tex', candidate, [], catalog) == 'excluded_too_short'
    incomplete = Candidate(17, 17, 'subsection', 100, '证明：连续映射把紧集')
    assert candidate_exclusion_reason(filename, incomplete, [], catalog) == 'excluded_too_short'
    assert candidate_exclusion_reason(filename, candidate, [(0, 200, 'explicit_hold')], catalog) == 'explicit_hold'
    catalog.exclusions = [dict(kind='question_numbers', source_file=filename, numbers=[17], reason='permanently_excluded')]
    assert candidate_exclusion_reason(filename, candidate, [], catalog) == 'permanently_excluded'


def test_reviewed_compactness_exercise_keeps_its_existing_source_number():
    candidate = Candidate(12, 12, 'subsection', 100, '5．由紧性定理证明聚点定理。')
    assert candidate_exclusion_reason('chapterB3_17_高维空间点集.tex', candidate, [], SimpleNamespace(exclusions=[])) is None
