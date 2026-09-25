import hashlib, itertools, json, random, pytest
from collections import Counter
from dataclasses import replace
from cuoti_app.services.exam_generation_stage5_solver import FrozenCandidateEdgeGraph, Stage5GlobalConstraintContract
from cuoti_app.services.exam_generation_stage5_d2_solver import solve_stage5_d2_selection, D2SolverError, D2SearchIncompleteError
from cuoti_app.services.exam_generation_stage6_objective import build_objective_policy, build_objective_receipt
from cuoti_app.services.exam_generation_similarity import build_similarity_freeze, attach_similarity_freeze

def _h(v): return hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
def _setup():
    z="a"*64; policy=build_objective_policy(parameter_set_hash=z, level_mapping={"verified_mistake_relevance":{"unknown":0,"not_applicable":0,"none":0,"related":1,"strong":2},"history_exposure_novelty":{"unknown":0,"not_applicable":0,"exposed":-1,"novel":1}}, directions={"verified_mistake_relevance":"max","history_exposure_novelty":"max"})
    edges=[]
    for pos,(cid,src,rel) in enumerate((("o", "ordinary", "related"),("u", "past_exam_other", "strong")),1):
        edges.append({"slot_id":f"s{pos}","position":pos,"candidate_id":cid,"candidate_fact_hash":("b" if cid=="o" else "c")*64,"content_hash":("d" if cid=="o" else "e")*64,"source_kind":src,"chapter":"c1","source_exam_id":None,"section_id":"sec","question_type_family":"single","target_difficulty":"中等","bucket_rank":1})
    payload={"contract_version":"g","constraint_hash":"f"*64,"candidate_facts_hash":"1"*64,"projection_identity_hash":"2"*64,"effective_difficulty_hash":"3"*64,"graph_mode":"initial","expanded_slot_ids":[],"candidates":{},"edges":edges}
    graph=FrozenCandidateEdgeGraph("g", "f"*64, "1"*64,"initial",2,_h(payload),json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":")))
    cp={"slots":[{"slot_id":"s1","position":1},{"slot_id":"s2","position":2}],"source_quotas":{"ordinary":1,"past_exam_other":1},"ordinary_chapter_quotas":{"c1":1},"coverage_policy":{"payload":{"minima":[]}},"diversity_policy":{"payload":{"max_per_source_exam":1}}}
    contract=Stage5GlobalConstraintContract("c", "4"*64,"1"*64,"2"*64,2,"f"*64,json.dumps(cp,separators=(",",":"),sort_keys=True))
    rows=[{"candidate_id":"o","candidate_fact_hash":"b"*64,"content_hash":"d"*64,"factors":{"verified_mistake_relevance":{"level":"related","evidence_codes":["x"]},"history_exposure_novelty":{"level":"novel","evidence_codes":["x"]}}},{"candidate_id":"u","candidate_fact_hash":"c"*64,"content_hash":"e"*64,"factors":{"verified_mistake_relevance":{"level":"strong","evidence_codes":["x"]},"history_exposure_novelty":{"level":"exposed","evidence_codes":["x"]}}}]
    return contract,graph,policy,rows
def test_mixed_and_receipt_recompute():
    c,g,p,rows=_setup(); r=build_objective_receipt([{k:x[k] for k in ("candidate_id","candidate_fact_hash","factors")} for x in rows],policy=p)
    s=solve_stage5_d2_selection(c,g,objective_receipt=r,objective_policy=p)
    assert {x["source_kind"] for x in s.assignments}=={"ordinary","past_exam_other"}
    assert s.objective_vector==(0,3,0,0,0)
def test_unknown_and_hash_drift_fail_closed():
    c,g,p,rows=_setup(); rows[0]["factors"]["verified_mistake_relevance"]["level"]="unknown"
    with pytest.raises(D2SolverError): solve_stage5_d2_selection(c,g,factor_receipts=rows,objective_policy=p)
    rows[0]["factors"]["verified_mistake_relevance"]["level"]="related"; rows[0]["content_hash"]="f"*64
    with pytest.raises(D2SolverError, match="hash_drift"): solve_stage5_d2_selection(c,g,factor_receipts=rows,objective_policy=p)


def test_equal_lexicographic_vector_uses_stable_candidate_tie_break():
    edges = [
        {"slot_id": "s1", "position": 1, "candidate_id": cid,
         "candidate_fact_hash": fact, "content_hash": content,
         "source_kind": "ordinary", "chapter": "c1", "source_exam_id": None,
         "section_id": "sec", "question_type_family": "single",
         "target_difficulty": "中等", "bucket_rank": 1}
        for cid, fact, content in (("B", "b" * 64, "d" * 64), ("A", "c" * 64, "e" * 64))
    ]
    graph_payload = {"contract_version": "g", "constraint_hash": "f" * 64,
                     "candidate_facts_hash": "1" * 64, "projection_identity_hash": "2" * 64,
                     "effective_difficulty_hash": "3" * 64, "graph_mode": "initial",
                     "expanded_slot_ids": [], "candidates": {}, "edges": edges}
    graph = FrozenCandidateEdgeGraph(
        "g", "f" * 64, "1" * 64, "initial", 2, _h(graph_payload),
        json.dumps(graph_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    contract_payload = {"slots": [{"slot_id": "s1", "position": 1}],
                        "source_quotas": {"ordinary": 1, "past_exam_other": 0},
                        "ordinary_chapter_quotas": {"c1": 1},
                        "coverage_policy": {"payload": {"minima": []}},
                        "diversity_policy": {"payload": {"max_per_source_exam": 1}}}
    contract = Stage5GlobalConstraintContract(
        "c", "4" * 64, "1" * 64, "2" * 64, 1, "f" * 64,
        json.dumps(contract_payload, sort_keys=True, separators=(",", ":")),
    )
    policy = build_objective_policy(
        parameter_set_hash="a" * 64,
        level_mapping={
            "verified_mistake_relevance": {"unknown": 0, "not_applicable": 0, "none": 0, "related": 1, "strong": 2},
            "history_exposure_novelty": {"unknown": 0, "not_applicable": 0, "exposed": -1, "novel": 1},
        },
        directions={"verified_mistake_relevance": "max", "history_exposure_novelty": "max"},
    )
    rows = [
        {
            "candidate_id": edge["candidate_id"],
            "candidate_fact_hash": edge["candidate_fact_hash"],
            "content_hash": edge["content_hash"],
            "factors": {
                "verified_mistake_relevance": {"level": "related", "evidence_codes": ["local"]},
                "history_exposure_novelty": {"level": "novel", "evidence_codes": ["history"]},
            },
        }
        for edge in graph.to_dict()["edges"]
    ]
    result = solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy)
    assert result.assignments[0]["candidate_id"] == "A"
    assert "score" not in json.dumps(result.to_dict(), ensure_ascii=False)
    assert "penalty" not in json.dumps(result.to_dict(), ensure_ascii=False)


def _choice_case(slot_count, choices_per_slot, *, source="ordinary"):
    edges = []
    rows = []
    for position in range(1, slot_count + 1):
        for choice in range(choices_per_slot):
            candidate_id = f"C{position:02d}-{choice:02d}"
            fact_hash = hashlib.sha256(("fact:" + candidate_id).encode()).hexdigest()
            content_hash = hashlib.sha256(("content:" + candidate_id).encode()).hexdigest()
            edge = {
                "slot_id": f"s{position}", "position": position,
                "candidate_id": candidate_id, "candidate_fact_hash": fact_hash,
                "content_hash": content_hash, "source_kind": source,
                "chapter": "c1", "source_exam_id": None, "section_id": "sec",
                "question_type_family": "single", "target_difficulty": "中等",
                "bucket_rank": choice + 1,
            }
            edges.append(edge)
            rows.append({
                "candidate_id": candidate_id, "candidate_fact_hash": fact_hash,
                "content_hash": content_hash,
                "factors": {
                    "verified_mistake_relevance": {"level": "related", "evidence_codes": ["local"]},
                    "history_exposure_novelty": {"level": "novel", "evidence_codes": ["history"]},
                },
            })
    graph_payload = {
        "contract_version": "g", "constraint_hash": "f" * 64,
        "candidate_facts_hash": "1" * 64, "projection_identity_hash": "2" * 64,
        "effective_difficulty_hash": "3" * 64, "graph_mode": "initial",
        "expanded_slot_ids": [], "candidates": {}, "edges": edges,
    }
    graph = FrozenCandidateEdgeGraph(
        "g", "f" * 64, "1" * 64, "initial", len(edges), _h(graph_payload),
        json.dumps(graph_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    source_quotas = {"ordinary": slot_count if source == "ordinary" else 0,
                     "past_exam_other": slot_count if source == "past_exam_other" else 0}
    contract_payload = {
        "slots": [{"slot_id": f"s{i}", "position": i} for i in range(1, slot_count + 1)],
        "source_quotas": source_quotas,
        "ordinary_chapter_quotas": {"c1": slot_count} if source == "ordinary" else {},
        "coverage_policy": {"payload": {"minima": []}},
        "diversity_policy": {"payload": {"max_per_source_exam": slot_count}},
    }
    contract = Stage5GlobalConstraintContract(
        "c", "4" * 64, "1" * 64, "2" * 64, slot_count, "f" * 64,
        json.dumps(contract_payload, sort_keys=True, separators=(",", ":")),
    )
    policy = build_objective_policy(
        parameter_set_hash="a" * 64,
        level_mapping={
            "verified_mistake_relevance": {"unknown": 0, "not_applicable": 0, "none": 0, "related": 1, "strong": 2},
            "history_exposure_novelty": {"unknown": 0, "not_applicable": 0, "exposed": -1, "novel": 1},
        },
        directions={"verified_mistake_relevance": "max", "history_exposure_novelty": "max"},
    )
    return contract, graph, policy, rows


def test_m_once_precedes_both_local_factor_layers_and_is_hash_bound():
    contract, graph, policy, rows = _choice_case(1, 2, source="past_exam_other")
    rows[0]["factors"]["verified_mistake_relevance"]["level"] = "strong"
    rows[1]["factors"]["verified_mistake_relevance"]["level"] = "none"
    eligible = {(rows[1]["candidate_id"], rows[1]["candidate_fact_hash"], rows[1]["content_hash"])}
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy,
        target_mode="past", m_once_eligible_identities=eligible,
    )
    assert result.assignments[0]["candidate_id"] == rows[1]["candidate_id"]
    assert result.objective_vector == (1, 0, 1, 0, 0)
    assert result.to_dict()["m_once_eligibility"]["status"] == "supplied"


def test_missing_m_once_facts_are_explicit_zero_and_do_not_change_hard_graph():
    contract, graph, policy, rows = _choice_case(1, 2, source="past_exam_other")
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy, target_mode="past",
    )
    assert result.objective_vector[0] == 0
    assert result.to_dict()["m_once_eligibility"] == {"status": "not_supplied", "identities": []}


def test_absent_local_semantics_remain_not_applicable_and_fall_through_to_tie_break():
    contract, graph, policy, rows = _choice_case(1, 2, source="ordinary")
    for row in rows:
        row["factors"] = {
            "verified_mistake_relevance": {"level": "not_applicable", "evidence_codes": ["no_verified_relation"]},
            "history_exposure_novelty": {"level": "not_applicable", "evidence_codes": ["no_publication_fact"]},
        }
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy, target_mode="past",
    )
    assert result.objective_vector == (0, 0, 0, 0, 0)
    assert result.assignments[0]["candidate_id"] == "C01-00"
    assert result.to_dict()["m_once_eligibility"]["status"] == "not_supplied"


def test_equal_13_by_6_fixture_finishes_with_exact_stable_tie_break():
    contract, graph, policy, rows = _choice_case(13, 6)
    visits = 0
    def guard():
        nonlocal visits
        visits += 1
        return True
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy, search_guard=guard,
    )
    assert [row["candidate_id"] for row in result.assignments] == [f"C{i:02d}-00" for i in range(1, 14)]
    assert visits < 500


def test_search_guard_never_returns_an_unproven_incumbent():
    contract, graph, policy, rows = _choice_case(13, 6)
    calls = 0
    def guard():
        nonlocal calls
        calls += 1
        return calls < 8
    with pytest.raises(D2SearchIncompleteError, match="d2_search_incomplete"):
        solve_stage5_d2_selection(
            contract, graph, factor_receipts=rows, objective_policy=policy, search_guard=guard,
        )


def _mixed_quota_case(slot_count, copies_per_kind=1):
    edges = []
    rows = []
    kinds = [("ordinary", "A"), ("ordinary", "B"), ("past_exam_other", "external")]
    for position in range(1, slot_count + 1):
        choice = 0
        for source, chapter in kinds:
            for _copy in range(copies_per_kind):
                candidate_id = f"M{position:02d}-{choice:02d}"
                fact_hash = hashlib.sha256(("fact:" + candidate_id).encode()).hexdigest()
                content_hash = hashlib.sha256(("content:" + candidate_id).encode()).hexdigest()
                edges.append({
                    "slot_id": f"s{position}", "position": position,
                    "candidate_id": candidate_id, "candidate_fact_hash": fact_hash,
                    "content_hash": content_hash, "source_kind": source,
                    "chapter": chapter if source == "ordinary" else None,
                    "source_exam_id": None, "section_id": "sec",
                    "question_type_family": "single", "target_difficulty": "中等",
                    "bucket_rank": choice + 1,
                })
                rows.append({
                    "candidate_id": candidate_id, "candidate_fact_hash": fact_hash,
                    "content_hash": content_hash,
                    "factors": {
                        "verified_mistake_relevance": {"level": "related", "evidence_codes": ["local"]},
                        "history_exposure_novelty": {"level": "novel", "evidence_codes": ["history"]},
                    },
                })
                choice += 1
    graph_payload = {
        "contract_version": "g", "constraint_hash": "f" * 64,
        "candidate_facts_hash": "1" * 64, "projection_identity_hash": "2" * 64,
        "effective_difficulty_hash": "3" * 64, "graph_mode": "initial",
        "expanded_slot_ids": [], "candidates": {}, "edges": edges,
    }
    graph = FrozenCandidateEdgeGraph(
        "g", "f" * 64, "1" * 64, "initial", len(edges), _h(graph_payload),
        json.dumps(graph_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    ordinary = (slot_count + 1) // 2
    chapter_a = (ordinary + 1) // 2
    contract_payload = {
        "slots": [{"slot_id": f"s{i}", "position": i} for i in range(1, slot_count + 1)],
        "source_quotas": {"ordinary": ordinary, "past_exam_other": slot_count - ordinary},
        "ordinary_chapter_quotas": {"A": chapter_a, "B": ordinary - chapter_a},
        "coverage_policy": {"payload": {"minima": []}},
        "diversity_policy": {"payload": {"max_per_source_exam": slot_count}},
    }
    contract = Stage5GlobalConstraintContract(
        "c", "4" * 64, "1" * 64, "2" * 64, slot_count, "f" * 64,
        json.dumps(contract_payload, sort_keys=True, separators=(",", ":")),
    )
    policy = build_objective_policy(
        parameter_set_hash="a" * 64,
        level_mapping={
            "verified_mistake_relevance": {"unknown": 0, "not_applicable": 0, "none": 0, "related": 1, "strong": 2},
            "history_exposure_novelty": {"unknown": 0, "not_applicable": 0, "exposed": -1, "novel": 1},
        },
        directions={"verified_mistake_relevance": "max", "history_exposure_novelty": "max"},
    )
    return contract, graph, policy, rows


def test_mixed_13_slot_source_and_chapter_capacity_pruning_keeps_stable_winner():
    contract, graph, policy, rows = _mixed_quota_case(13, copies_per_kind=2)
    visits = 0
    def guard():
        nonlocal visits
        visits += 1
        return True
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy, search_guard=guard,
    )
    expected = [f"M{i:02d}-00" for i in range(1, 5)]
    expected += [f"M{i:02d}-02" for i in range(5, 8)]
    expected += [f"M{i:02d}-04" for i in range(8, 14)]
    assert [row["candidate_id"] for row in result.assignments] == expected
    assert result.objective_vector == (0, 13, 13, 0, 0)
    assert visits < 2_000


@pytest.mark.parametrize("seed", range(12))
def test_branch_and_bound_matches_small_bruteforce_oracle(seed):
    contract, graph, policy, rows = _mixed_quota_case(4)
    rng = random.Random(seed)
    relation_levels = ("none", "related", "strong")
    history_levels = ("exposed", "novel")
    for row in rows:
        row["factors"]["verified_mistake_relevance"]["level"] = rng.choice(relation_levels)
        row["factors"]["history_exposure_novelty"]["level"] = rng.choice(history_levels)
    eligible = {
        (row["candidate_id"], row["candidate_fact_hash"], row["content_hash"])
        for row in rows if rng.randrange(3) == 0
    }
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy,
        target_mode="past", m_once_eligible_identities=eligible,
    )

    factors = {row["candidate_id"]: row["factors"] for row in rows}
    domains = [[edge for edge in graph.to_dict()["edges"] if edge["slot_id"] == f"s{i}"] for i in range(1, 5)]
    best = None
    for chosen in itertools.product(*domains):
        if sum(edge["source_kind"] == "ordinary" for edge in chosen) != 2:
            continue
        if sum(edge["source_kind"] == "past_exam_other" for edge in chosen) != 2:
            continue
        if sum(edge["source_kind"] == "ordinary" and edge["chapter"] == "A" for edge in chosen) != 1:
            continue
        if sum(edge["source_kind"] == "ordinary" and edge["chapter"] == "B" for edge in chosen) != 1:
            continue
        m_once = sum((edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"]) in eligible for edge in chosen)
        relation = sum(policy["level_mapping"]["verified_mistake_relevance"][factors[edge["candidate_id"]]["verified_mistake_relevance"]["level"]] for edge in chosen)
        history = sum(policy["level_mapping"]["history_exposure_novelty"][factors[edge["candidate_id"]]["history_exposure_novelty"]["level"]] for edge in chosen)
        objective = (m_once, relation, history)
        tie = tuple((edge["position"], edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"], edge["bucket_rank"]) for edge in chosen)
        if best is None or objective > best[0] or (objective == best[0] and tie < best[1]):
            best = (objective, tie, chosen)
    assert best is not None
    assert result.objective_vector == (*best[0], 0, 0)
    assert [row["candidate_id"] for row in result.assignments] == [edge["candidate_id"] for edge in best[2]]


def _graph_with_edges(graph, edges):
    payload = graph.to_dict() | {'edges': edges}
    return replace(graph, edge_count=len(edges), graph_hash=_h(payload),
                   _payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))


def test_joint_quota_matching_rejects_overlapping_capacity_at_the_root():
    contract, graph, policy, rows = _mixed_quota_case(4, copies_per_kind=2)
    edges = [e for e in graph.to_dict()['edges'] if e['position'] <= 2 or e['source_kind'] != 'ordinary']
    for edge in edges:
        if edge['position'] == 2 and edge['source_kind'] == 'ordinary':
            edge['chapter'] = 'C'
    graph = _graph_with_edges(graph, edges)
    ids = {e['candidate_id'] for e in edges}
    visits = []
    # A and B each have one compatible slot, but it is the same slot.
    with pytest.raises(D2SolverError, match='d2_hard_constraints_infeasible'):
        solve_stage5_d2_selection(contract, graph, factor_receipts=[r for r in rows if r['candidate_id'] in ids],
                                 objective_policy=policy, search_guard=lambda: visits.append(1) or True)
    assert len(visits) == 1


@pytest.mark.parametrize('seed', range(16))
def test_quota_pruning_matches_enumeration_with_content_and_diversity_conflicts(seed):
    contract, graph, policy, rows = _mixed_quota_case(4)
    rng = random.Random(seed)
    edges = graph.to_dict()['edges']
    factors = {row['candidate_id']: row for row in rows}
    for edge in edges:
        cid = edge['candidate_id']
        edge['content_hash'] = _h(rng.randrange(7))
        if edge['source_kind'] == 'past_exam_other':
            edge['source_exam_id'] = str(rng.randrange(2))
        factors[cid]['content_hash'] = edge['content_hash']
        factors[cid]['factors']['verified_mistake_relevance']['level'] = rng.choice(('none', 'related', 'strong'))
        factors[cid]['factors']['history_exposure_novelty']['level'] = rng.choice(('novel', 'exposed'))
    graph = _graph_with_edges(graph, edges)
    payload = contract.to_dict()
    payload['diversity_policy']['payload']['max_per_source_exam'] = 1
    contract = replace(contract, _payload_json=json.dumps(payload))
    best = None
    for chosen in itertools.product(*[[e for e in edges if e['position'] == p] for p in range(1, 5)]):
        if len({e['content_hash'] for e in chosen}) != 4:
            continue
        if Counter(e['source_kind'] for e in chosen) != Counter({'ordinary': 2, 'past_exam_other': 2}):
            continue
        if Counter(e['chapter'] for e in chosen if e['source_kind'] == 'ordinary') != Counter({'A': 1, 'B': 1}):
            continue
        if max(Counter(e['source_exam_id'] for e in chosen if e['source_exam_id'] is not None).values(), default=0) > 1:
            continue
        vector = (0, *(sum(policy['level_mapping'][name][factors[e['candidate_id']]['factors'][name]['level']]
                           for e in chosen) for name in ('verified_mistake_relevance', 'history_exposure_novelty')), 0, 0)
        tie = tuple((e['position'], e['candidate_id'], e['candidate_fact_hash'], e['content_hash'], e['bucket_rank']) for e in chosen)
        if best is None or vector > best[0] or vector == best[0] and tie < best[1]:
            best = (vector, tie, [e['candidate_id'] for e in chosen])
    if best is None:
        with pytest.raises(D2SolverError, match='d2_hard_constraints_infeasible'):
            solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy)
    else:
        result = solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy)
        assert result.objective_vector == best[0]
        assert [e['candidate_id'] for e in result.assignments] == best[2]


def test_v4_similarity_is_soft_ordered_after_relevance_and_before_history():
    contract, graph, policy, rows = _choice_case(2, 2)
    payload = contract.to_dict() | {'subject': '数学分析'}
    contract = replace(contract, _payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    candidates = []
    cards = {}
    for edge in graph.to_dict()['edges']:
        cid = edge['candidate_id']
        text = r'求极限 \(\lim_{x\to0}\frac{\sin(2x)}{x}\)。' if cid.endswith('-00') else r'证明 \(x^2+1>0\)。'
        snapshot = {'schema_version': 'semantic-candidate-content-snapshot-v1', 'content': text,
                    'conditions': [], 'figure_references': [], 'options': []}
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        candidates.append({'candidate_id': cid, 'source_kind': 'ordinary', 'content_hash': edge['content_hash'],
                           'card_version_id': 1, 'card_hash': 'a' * 64, 'content_snapshot': raw,
                           'content_snapshot_hash': _h(raw)})
        cards[('ordinary', cid)] = {'knowledge_points': ['极限'], 'methods': ['Taylor'], 'question_type': '计算题'}
    receipt = build_similarity_freeze(subject='数学分析', candidates=candidates,
                                      graph_edges=graph.to_dict()['edges'], card_facts=cards)
    graph = attach_similarity_freeze(graph, receipt)
    result = solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy,
                                       target_mode='past', solver_version='stage5-d2-lexicographic-solver-v4')
    assert result.to_dict()['similarity_freeze_hash'] == receipt['freeze_hash']
    assert result.objective_vector[2] == 0
    assert len(result.objective_vector) == 8
    assert [e['candidate_id'] for e in result.assignments] == ['C01-00', 'C02-01']


def test_v4_same_paper_penalty_occupies_its_own_objective_dimension():
    contract, graph, policy, rows = _choice_case(2, 1)
    payload = contract.to_dict() | {'subject': '数学分析'}
    contract = replace(contract, _payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    candidates = []
    cards = {}
    for edge in graph.to_dict()['edges']:
        snapshot = {
            'schema_version': 'semantic-candidate-content-snapshot-v1',
            'content': r'求极限 \(\lim_{x\to0}\frac{\sin(2x)}{x}。',
            'conditions': [], 'figure_references': [], 'options': [],
        }
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        candidates.append({
            'candidate_id': edge['candidate_id'], 'source_kind': 'ordinary',
            'content_hash': edge['content_hash'], 'card_version_id': 1,
            'card_hash': 'a' * 64, 'content_snapshot': raw,
            'content_snapshot_hash': _h(raw),
        })
        cards[('ordinary', edge['candidate_id'])] = {
            'knowledge_points': ['极限'], 'methods': ['Taylor'],
            'question_type': '计算题',
        }
    receipt = build_similarity_freeze(
        subject='数学分析', candidates=candidates,
        graph_edges=graph.to_dict()['edges'], card_facts=cards,
    )
    graph = attach_similarity_freeze(graph, receipt)
    result = solve_stage5_d2_selection(
        contract, graph, factor_receipts=rows, objective_policy=policy,
        target_mode='past', solver_version='stage5-d2-lexicographic-solver-v4',
    )
    assert result.objective_vector[1] == 2  # relevance remains ahead of the soft penalty
    assert result.objective_vector[2] == -1


def test_v4_similarity_history_usage_is_later_than_exact_history():
    contract, graph, policy, rows = _choice_case(1, 2)
    payload = contract.to_dict() | {'subject': '数学分析'}
    contract = replace(contract, _payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
    def item(edge, text):
        snapshot = {'schema_version': 'semantic-candidate-content-snapshot-v1', 'content': text,
                    'conditions': [], 'figure_references': [], 'options': []}
        raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return {'candidate_id': edge['candidate_id'], 'source_kind': 'ordinary', 'content_hash': edge['content_hash'],
                'card_version_id': 1, 'card_hash': 'a' * 64, 'content_snapshot': raw,
                'content_snapshot_hash': _h(raw)}
    candidates = [item(edge, r'求极限 \(\lim_{x\to0}\frac{\sin(2x)}{x}\)。') for edge in graph.to_dict()['edges']]
    cards = {(row['source_kind'], row['candidate_id']): {'knowledge_points': ['极限'], 'methods': ['Taylor'], 'question_type': '计算题'} for row in candidates}
    history = [item({'candidate_id': 'old', 'content_hash': _h('old')}, r'求极限 \(\lim_{x\to0}\frac{\sin(4x)}{x}\)。') | {'order_rank': 4}]
    cards[('ordinary', 'old')] = {'knowledge_points': ['极限'], 'methods': ['Taylor'], 'question_type': '计算题'}
    receipt = build_similarity_freeze(subject='数学分析', candidates=candidates,
                                      graph_edges=graph.to_dict()['edges'], card_facts=cards,
                                      history_candidates=history)
    graph = attach_similarity_freeze(graph, receipt)
    result = solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy,
                                       target_mode='past', solver_version='stage5-d2-lexicographic-solver-v4')
    assert result.objective_vector[-2] == -1
    assert result.to_dict()['similarity_freeze_hash'] == receipt['freeze_hash']
