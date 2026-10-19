import pytest
import hashlib, json

from cuoti_app.services.exam_generation_stage5_candidate_base_runner import (
    CANDIDATE_BASE_D2_PROTOCOL,
    build_candidate_stage5_d2_base,
)

def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _frozen(version, payload):
    body={"version":version,"payload":payload}; return {**body,"hash":_digest(body)}


def _payloads():
    stage3 = {
        "contract_version": "past-v3-generation-contract-v1", "profile_hash": "b" * 64,
        "exact_slot_hash": "c" * 64, "question_count": 1,
        "combination": {"institution": "目标", "subject": "数学", "reference_year": 2025},
        "target_institution_exclusion": {"institution": "目标"},
        "source_identity": {"catalog_version": "c", "course_version": "v", "source_snapshot": "d" * 64, "source_exam_id": "e"},
        "slot_demands": [{"position": 1, "section_id": "s", "question_type_family": "t", "target_difficulty": "中等"}],
        "source_quotas": {"ordinary": 1},
    }
    stage4 = {"contract_version": "past-v3-stage4-candidate-facts-v1", "projection_identity_hash": "1" * 64, "candidates": [{
        "candidate_id": "x", "source_kind": "ordinary", "subject": "数学", "question_type_family": "t", "chapter": "A",
        "source_exam_id": None, "section_id": None, "source_institution": None, "content_hash": "f" * 64,
        "difficulty_fact": {"resolved": True, "label": "中等"},
    }]}
    return stage3, stage4


def test_scaled_integer_pressure_matches_original_rational_order():
    from cuoti_app.services.exam_generation_stage5_solver import _constraint_aware_order
    contract = dict(source_quotas={'ordinary': 7, 'past_exam_other': 5},
                    ordinary_chapter_quotas={'A': 3, 'B': 4}, question_count=12,
                    coverage_policy={'payload': {'minima': [{'id': 'cover', 'field': 'chapter', 'value': 'A', 'min_count': 2}]}})
    items = [dict(candidate_id=f'q{i:02}', candidate_fact_hash=str(i), content_hash=str(i // 2),
                  source_kind='ordinary' if i % 3 else 'past_exam_other',
                  chapter=('A' if i % 2 else 'B') if i % 3 else None,
                  source_exam_id=None if i % 3 else f'e{i % 2}') for i in range(18)]
    expected = ['q01', 'q03', 'q04', 'q06', 'q08', 'q11', 'q12', 'q14', 'q09',
                'q16', 'q00', 'q05', 'q02', 'q15', 'q07', 'q10', 'q13', 'q17']
    assert [row['candidate_id'] for row in _constraint_aware_order(items, contract)] == expected
    assert [row['candidate_id'] for row in _constraint_aware_order(items, contract, limit=8)] == expected[:8]


@pytest.mark.parametrize('count,expected_hash,expanded_hash', [
    (2, 'd68832d960b0772bea9521e7ac9326b455f60d1490a4b55ada5f6aa054d6023a', None),
    (7, 'eff088ddaa2c56560ec7972fe2332d4f46931509406b96c45b17155a50ed1c1c', '0bb4052b2942c5f7944aa41266420502ea33e2e14bcc0d7b0a0d9e81236f0a1e'),
    (80, '6700895189227bc3b1f24b0a74256feccccc11a255b9f637efc8000fa88da306', '911be042286b4809d3e1d1facd522e315a22cf248abbd01dff2e9fc127112eda'),
])
def test_repeated_slot_family_reuses_order_without_changing_frozen_graph(monkeypatch, count, expected_hash, expanded_hash):
    from cuoti_app.services.exams.selection import graph as solver
    stage3, stage4 = _payloads()
    stage3['question_count'] = 2
    stage3['slot_demands'].append(dict(stage3['slot_demands'][0], position=2))
    stage3['source_quotas'] = {'ordinary': 2}
    if count == 2:
        stage4['candidates'].append(dict(stage4['candidates'][0], candidate_id='y', content_hash='e' * 64))
    else:
        stage4['candidates'].extend(dict(stage4['candidates'][0], candidate_id=f'y{i:03}', content_hash=f'{i:064x}')
                                   for i in range(count - 1))
    original = solver._constraint_aware_order
    calls = []
    def ordered(items, contract, **kwargs):
        calls.append((len(items), kwargs['limit']))
        return original(items, contract, **kwargs)
    monkeypatch.setattr(solver, '_constraint_aware_order', ordered)
    weights = {'version': 'v', 'subject': '数学', 'weights': {'A': 1}}
    base = build_candidate_stage5_d2_base(
        stage3, stage4, stage3_contract_hash=_digest(stage3), candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4['projection_identity_hash'],
        chapter_weight_fact={**weights, 'fact_hash': _digest(weights)},
        recent_usage=_frozen('r', {'excluded_candidate_ids': [], 'excluded_content_hashes': []}),
        diversity_policy=_frozen('d', {'max_per_source_exam': 2}), coverage_policy=_frozen('c', {'minima': []}),
        uniqueness_policy=_frozen('u', {'resource_keys': ['candidate_id', 'content_hash']}), policy_fact_set_hash='f' * 64)
    assert calls == [(count, min(count, 10))]
    assert base.graph.graph_hash == expected_hash
    if expanded_hash:
        graph = solver.build_candidate_edge_graph(base.constraint, stage4=stage4, expanded_slot_ids=('slot-0001',))
        assert graph.graph_hash == expanded_hash
        base_by_slot = {}
        expanded_by_slot = {}
        for edge in base.graph.to_dict()['edges']:
            base_by_slot.setdefault(edge['slot_id'], []).append(edge)
        for edge in graph.to_dict()['edges']:
            expanded_by_slot.setdefault(edge['slot_id'], []).append(edge)
        for slot_id, edges in base_by_slot.items():
            assert expanded_by_slot[slot_id][:len(edges)] == edges


def test_d2_runner_returns_explicit_provider_free_candidate_base():
    stage3, stage4 = _payloads()
    wb={"version":"chapter-weights-v1","subject":"数学","weights":{"A":1}}
    result = build_candidate_stage5_d2_base(stage3, stage4, stage3_contract_hash=_digest(stage3), candidate_facts_hash=_digest(stage4), projection_identity_hash=stage4["projection_identity_hash"], chapter_weight_fact={**wb,"fact_hash":_digest(wb)}, recent_usage=_frozen("recent-v1",{"excluded_candidate_ids":[],"excluded_content_hashes":[]}), diversity_policy=_frozen("diversity-v1",{"max_per_source_exam":1}), coverage_policy=_frozen("coverage-v1",{"minima":[]}), uniqueness_policy=_frozen("uniqueness-v1",{"resource_keys":["candidate_id","content_hash"]}), policy_fact_set_hash="f"*64)
    assert result.protocol == CANDIDATE_BASE_D2_PROTOCOL
    assert result.candidate_base_contract["legacy_scoring"] is False
    assert result.graph.to_dict()["edges"]
    assert "score" not in result.to_dict()["graph"]
    assert "penalty" not in result.to_dict()["graph"]


def test_d2_runner_rejects_forged_stage4_identity_before_graph_build():
    stage3, stage4 = _payloads()
    stage4["projection_identity_hash"] = "not-a-hash"
    with pytest.raises(Exception):
        build_candidate_stage5_d2_base(stage3, stage4)


def test_d2_base_requires_every_policy_receipt():
    stage3, stage4 = _payloads()
    with pytest.raises(TypeError, match="required"):
        build_candidate_stage5_d2_base(
            stage3,
            stage4,
            stage3_contract_hash=_digest(stage3),
            candidate_facts_hash=_digest(stage4),
            projection_identity_hash=stage4["projection_identity_hash"],
            chapter_weight_fact=None,
            recent_usage={},
            diversity_policy={},
            coverage_policy={},
            uniqueness_policy={},
            policy_fact_set_hash="f" * 64,
        )


def test_expanded_graph_injects_required_chapter_without_touching_initial_edges():
    from cuoti_app.services.exam_generation_stage5_solver import _inject_missing_expanded_chapters

    payload = {"ordinary_chapter_quotas": {"A": 1, "B": 1}}
    slots = [{"slot_id": "slot-0001", "position": 1,
              "question_type_family": "proof", "target_difficulty": "中等"}]
    a = {"candidate_id": "a", "candidate_fact_hash": "a" * 64, "content_hash": "1" * 64,
         "source_kind": "ordinary", "chapter": "A", "question_type_family": "proof",
         "difficulty": "中等", "source_exam_id": None, "section_id": None}
    b = {"candidate_id": "b", "candidate_fact_hash": "b" * 64, "content_hash": "2" * 64,
         "source_kind": "ordinary", "chapter": "B", "question_type_family": "proof",
         "difficulty": "中等", "source_exam_id": None, "section_id": None}
    selected = {"slot-0001": [{**a, "slot_id": "slot-0001", "position": 1, "target_difficulty": "中等", "bucket_rank": 1}]}
    _inject_missing_expanded_chapters(payload, [a, b], slots, selected, {"slot-0001"})
    assert {edge["chapter"] for edge in selected["slot-0001"]} == {"A", "B"}


def test_expanded_chapter_injection_keeps_full_slot_when_displaced_chapter_is_singleton():
    from cuoti_app.services.exam_generation_stage5_solver import _inject_missing_expanded_chapters

    payload = {"ordinary_chapter_quotas": {"A": 1, "B": 1}}
    slots = [{"slot_id": "slot-0001", "position": 1,
              "question_type_family": "proof", "target_difficulty": "中等"}]
    rows = [{"candidate_id": f"{chapter}{i}", "candidate_fact_hash": f"{chapter}{i}" * 64,
             "content_hash": f"{chapter}{i}" * 64, "source_kind": "ordinary", "chapter": chapter,
             "question_type_family": "proof", "difficulty": "中等", "source_exam_id": None, "section_id": None}
            for chapter, i in (("A", 0), ("B", 0), ("C", 0), ("D", 0), ("E", 0), ("F", 0), ("G", 0))]
    selected = {"slot-0001": [{**row, "slot_id": "slot-0001", "position": 1, "target_difficulty": "中等", "bucket_rank": 1}
                              for row in rows[:1]]}
    # A full slot has no safe replacement when its only represented chapter is singleton.
    selected["slot-0001"].extend({**row, "slot_id": "slot-0001", "position": 1, "target_difficulty": "中等", "bucket_rank": i}
                                  for i, row in enumerate(rows[2:7], 2))
    _inject_missing_expanded_chapters(payload, rows, slots, selected, {"slot-0001"})
    assert {edge["chapter"] for edge in selected["slot-0001"]} == {"A", "C", "D", "E", "F", "G"}


def test_expanded_chapter_does_not_replace_full_slot_prefix():
    from cuoti_app.services.exam_generation_stage5_solver import _inject_missing_expanded_chapters

    payload = {"ordinary_chapter_quotas": {"A": 1, "B": 1}}
    slots = [{"slot_id": "slot-0001", "position": 1,
              "question_type_family": "proof", "target_difficulty": "中等"}]
    def row(candidate, chapter):
        return {"candidate_id": candidate, "candidate_fact_hash": candidate * 64,
                "content_hash": (candidate + "h") * 32, "source_kind": "ordinary", "chapter": chapter,
                "question_type_family": "proof", "difficulty": "中等", "source_exam_id": None, "section_id": None}
    a0, a1, a2, a3, a4, b0 = (row(name, chapter) for name, chapter in
                               (("a0", "A"), ("a1", "A"), ("a2", "A"), ("a3", "A"), ("a4", "A"), ("b0", "B")))
    selected = {"slot-0001": [{**item, "slot_id": "slot-0001", "position": 1,
                               "target_difficulty": "中等", "bucket_rank": index + 1}
                              for index, item in enumerate((a0, a1, a2, a3, a4, a4))]}
    prefix = [edge["candidate_id"] for edge in selected["slot-0001"][:4]]
    _inject_missing_expanded_chapters(payload, [a0, a1, a2, a3, a4, b0], slots, selected, {"slot-0001"})
    assert [edge["candidate_id"] for edge in selected["slot-0001"][:4]] == prefix
    assert [edge["candidate_id"] for edge in selected["slot-0001"]] == ["a0", "a1", "a2", "a3", "a4", "a4"]
    assert not any(edge["chapter"] == "B" for edge in selected["slot-0001"])


@pytest.mark.parametrize('source_kind', ['past_exam', 'past_exam_local'])
def test_historical_past_exam_alias_is_signed_as_canonical_past_exam_other(source_kind):
    stage3, stage4 = _payloads()
    stage3["source_quotas"] = {"past_exam": 1}
    candidate = stage4["candidates"][0]
    candidate.update({
        "source_kind": source_kind,
        "chapter": None,
        "source_exam_id": "external-exam",
        "section_id": "s",
        "source_institution": "外校",
    })
    weights = {"version": "chapter-weights-v1", "subject": "数学", "weights": {}}
    base = build_candidate_stage5_d2_base(
        stage3,
        stage4,
        stage3_contract_hash=_digest(stage3),
        candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4["projection_identity_hash"],
        chapter_weight_fact={**weights, "fact_hash": _digest(weights)},
        recent_usage=_frozen("recent-v1", {"excluded_candidate_ids": [], "excluded_content_hashes": []}),
        diversity_policy=_frozen("diversity-v1", {"max_per_source_exam": 1}),
        coverage_policy=_frozen("coverage-v1", {"minima": []}),
        uniqueness_policy=_frozen("uniqueness-v1", {"resource_keys": ["candidate_id", "content_hash"]}),
        policy_fact_set_hash="f" * 64,
    )
    assert base.constraint.to_dict()["source_quotas"] == {"ordinary": 0, "past_exam_other": 1}
    assert base.graph.to_dict()["edges"][0]["source_kind"] == "past_exam_other"
    assert base.stage4['candidates'][0]['source_kind'] == source_kind
