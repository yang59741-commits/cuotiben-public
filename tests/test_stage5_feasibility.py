import copy

import pytest

from cuoti_app.services import exam_generation_stage5_solver as solver
from cuoti_app.services.exam_generation_stage5_candidate_base_runner import build_candidate_stage5_d2_base
from tests.test_exam_generation_stage6_d2_repository import _case, _digest, _frozen


def _base(count=3, *, defect=None):
    seed, *_ = _case()
    stage3 = copy.deepcopy(seed.stage3)
    stage4 = copy.deepcopy(seed.stage4)
    stage3['question_count'] = count
    stage3['slot_demands'] = [dict(stage3['slot_demands'][0], position=i + 1) for i in range(count)]
    stage3['source_quotas'] = {'ordinary': count}
    candidate = stage4['candidates'][0]
    stage4['candidates'] = [dict(candidate, candidate_id=f'candidate-{i:03}', content_hash=_digest(i))
                            for i in range(count * 4)]
    weights = {candidate['chapter']: 1}
    cap = count
    minima = []
    if defect == 'diversity':
        stage3['source_quotas'] = {'past_exam': count}
        cap = 1
        for row in stage4['candidates']:
            row.update(source_kind='past_exam_other', source_exam_id='one-exam', source_institution='other', chapter=None, section_id='section')
    elif defect == 'coverage':
        minima = [dict(id='required-family', field='question_type_family', value='absent', min_count=1)]
    elif defect == 'source':
        stage3['source_quotas'] = {'ordinary': count - 1, 'past_exam': 1}
    elif defect == 'chapter':
        weights['absent-chapter'] = 1
    elif defect == 'content':
        for row in stage4['candidates']:
            row['content_hash'] = candidate['content_hash']
    chapter = dict(version='chapter-weights-v1', subject=stage3['combination']['subject'], weights=weights)
    return build_candidate_stage5_d2_base(
        stage3, stage4, stage3_contract_hash=_digest(stage3), candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4['projection_identity_hash'],
        chapter_weight_fact={**chapter, 'fact_hash': _digest(chapter)},
        recent_usage=seed.constraint.to_dict()['recent_usage'],
        diversity_policy=_frozen('diversity-v1', {'max_per_source_exam': cap}),
        coverage_policy=_frozen('coverage-v1', {'minima': minima}),
        uniqueness_policy=seed.constraint.to_dict()['uniqueness_policy'],
        policy_fact_set_hash='a' * 64,
    )


def _receipt(base, score=lambda edge: 0):
    rows = [dict(slot_id=e['slot_id'], candidate_id=e['candidate_id'], score=score(e), penalty=0)
            for e in base.graph.to_dict()['edges']]
    return solver._local_v2_score_receipt(base.constraint, base.graph, rows, purpose='stage5-local-feasibility-v2')


def test_feasibility_stops_on_a_complete_witness_not_all_equal_ties():
    base = _base(13)
    visits = 0

    def guard():
        nonlocal visits
        visits += 1
        return visits < 100

    assert solver.locate_candidate_graph_deficit(base.constraint, base.graph, search_guard=guard) is None
    assert 14 <= visits < 100


@pytest.mark.parametrize('defect', [None, 'source', 'chapter', 'coverage', 'diversity', 'content'])
def test_feasibility_agrees_with_exhaustive_legacy_solver_on_small_graphs(defect):
    base = _base(defect=defect)
    try:
        solver.solve_global_selection(base.constraint, base.graph, score_receipt=_receipt(base))
        expected = None
    except solver.Stage5InfeasibleError as exc:
        expected = exc.witness
    actual = solver.locate_candidate_graph_deficit(base.constraint, base.graph)
    assert actual == expected
    assert (actual is not None) == (defect is not None)


def test_budget_exhaustion_is_not_an_infeasible_witness():
    base = _base(13)
    with pytest.raises(solver.Stage5SearchIncompleteError) as exc:
        solver.locate_candidate_graph_deficit(base.constraint, base.graph, search_guard=lambda: False)
    assert exc.value.code == 'exam_selection_incomplete'
    assert not isinstance(exc.value, solver.Stage5InfeasibleError)


def test_legacy_optimization_still_evaluates_later_better_assignments():
    base = _base(2)
    receipt = _receipt(base, score=lambda e: e['bucket_rank'])
    chosen = solver.solve_global_selection(base.constraint, base.graph, score_receipt=receipt)
    assert chosen.total_utility == sum(max(e['bucket_rank'] for e in base.graph.to_dict()['edges']
                                         if e['slot_id'] == slot['slot_id'])
                                       for slot in base.constraint.to_dict()['slots'])
    assert chosen.selection_hash == solver.solve_global_selection(
        base.constraint, base.graph, score_receipt=receipt).selection_hash


def test_cancellation_from_parent_guard_propagates():
    base = _base(3)

    def cancelled():
        raise RuntimeError('parent-task-cancelled')

    with pytest.raises(RuntimeError, match='parent-task-cancelled'):
        solver.locate_candidate_graph_deficit(base.constraint, base.graph, search_guard=cancelled)


def test_solver_keeps_builtin_any_when_module_name_is_polluted(monkeypatch):
    """A stray module-level ``any`` must not break production feasibility."""
    base = _base(3)
    from cuoti_app.services.exams.selection import solver as implementation
    monkeypatch.setattr(implementation, "any", {"polluted": 0}.items(), raising=False)
    assert solver.locate_candidate_graph_deficit(base.constraint, base.graph) is None
