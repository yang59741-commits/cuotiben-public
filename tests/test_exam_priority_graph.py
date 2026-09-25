from __future__ import annotations

import hashlib
import json

from cuoti_app.services.exam_generation_stage5_solver import (
    build_candidate_edge_graph,
    build_global_constraint_contract,
    supplement_priority_candidate_graph,
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _frozen(version, payload):
    body = {"version": version, "payload": payload}
    return {**body, "hash": _digest(body)}


def _candidate(
    candidate_id,
    *,
    question_type="proof",
    difficulty="中等",
    chapter="A",
    resolved=True,
):
    return {
        "candidate_id": candidate_id,
        "source_kind": "ordinary",
        "subject": "数学",
        "question_type_family": question_type,
        "chapter": chapter,
        "source_exam_id": None,
        "section_id": None,
        "source_institution": None,
        "content_hash": hashlib.sha256(candidate_id.encode()).hexdigest(),
        "difficulty_fact": {
            "resolved": resolved,
            "label": difficulty if resolved else None,
        },
    }


def _fixture():
    stage3 = {
        "contract_version": "past-v3-generation-contract-v1",
        "profile_hash": "b" * 64,
        "exact_slot_hash": "c" * 64,
        "question_count": 2,
        "combination": {
            "institution": "目标",
            "subject": "数学",
            "reference_year": 2025,
        },
        "target_institution_exclusion": {"institution": "目标"},
        "source_identity": {
            "catalog_version": "catalog",
            "course_version": "course",
            "source_snapshot": "d" * 64,
            "source_exam_id": "source",
        },
        "slot_demands": [
            {
                "position": position,
                "section_id": "section",
                "question_type_family": "proof",
                "target_difficulty": "中等",
            }
            for position in (1, 2)
        ],
        "source_quotas": {"ordinary": 2},
    }
    stage4 = {
        "contract_version": "past-v3-stage4-candidate-facts-v1",
        "projection_identity_hash": "1" * 64,
        "candidates": [
            *[_candidate(f"candidate-{index}") for index in range(7)],
            _candidate("wrong-type", question_type="calculation"),
            _candidate("wrong-difficulty", difficulty="困难"),
            _candidate("wrong-chapter", chapter="B"),
            _candidate("unresolved", resolved=False),
        ],
    }
    weights = {"version": "chapter-weights-v1", "subject": "数学", "weights": {"A": 1}}
    contract = build_global_constraint_contract(
        stage3=stage3,
        stage3_contract_hash=_digest(stage3),
        stage4=stage4,
        candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4["projection_identity_hash"],
        chapter_weight_fact={**weights, "fact_hash": _digest(weights)},
        recent_usage=_frozen(
            "recent-v1",
            {"excluded_candidate_ids": [], "excluded_content_hashes": []},
        ),
        diversity_policy=_frozen("diversity-v1", {"max_per_source_exam": 2}),
        coverage_policy=_frozen("coverage-v1", {"minima": []}),
        uniqueness_policy=_frozen(
            "uniqueness-v1", {"resource_keys": ["candidate_id", "content_hash"]}
        ),
        policy_fact_set_hash="f" * 64,
    )
    return contract, stage4, build_candidate_edge_graph(contract, stage4=stage4)


def test_verified_priority_outside_four_item_window_is_added_without_losing_edges():
    contract, stage4, base = _fixture()
    before = base.to_dict()
    assert all(
        "candidate-5" not in {edge["candidate_id"] for edge in edges}
        for edges in _by_slot(before["edges"]).values()
    )

    graph = supplement_priority_candidate_graph(
        contract,
        stage4=stage4,
        base_graph=base,
        preferred_candidate_ids=["candidate-5"],
    )
    payload = graph.to_dict()

    assert all(edge in payload["edges"] for edge in before["edges"])
    assert all(
        "candidate-5" in {edge["candidate_id"] for edge in edges}
        for edges in _by_slot(payload["edges"]).values()
    )
    assert payload["priority_base_counts"] == {
        "slot-0001": 4,
        "slot-0002": 4,
    }
    assert graph.edge_count == 10


def test_supplement_caps_each_slot_at_six_and_replaces_prior_priority_suffix():
    contract, stage4, base = _fixture()
    base_by_slot = _by_slot(base.to_dict()["edges"])
    first = supplement_priority_candidate_graph(
        contract,
        stage4=stage4,
        base_graph=base,
        preferred_candidate_ids=["candidate-4", "candidate-5", "candidate-6"],
    )
    assert all(len(edges) == 6 for edges in _by_slot(first.to_dict()["edges"]).values())
    assert first.edge_count == 6 * contract.question_count

    second = supplement_priority_candidate_graph(
        contract,
        stage4=stage4,
        base_graph=first,
        preferred_candidate_ids=["candidate-5"],
    )
    second_by_slot = _by_slot(second.to_dict()["edges"])
    assert all(len(edges) == 5 for edges in second_by_slot.values())
    assert all(
        edges[:4] == base_by_slot[slot_id]
        and [edge["candidate_id"] for edge in edges[4:]] == ["candidate-5"]
        for slot_id, edges in second_by_slot.items()
    )


def test_wrong_type_difficulty_chapter_and_unresolved_candidates_are_not_added():
    contract, stage4, base = _fixture()
    graph = supplement_priority_candidate_graph(
        contract,
        stage4=stage4,
        base_graph=base,
        preferred_candidate_ids=[
            "wrong-type",
            "wrong-difficulty",
            "wrong-chapter",
            "unresolved",
        ],
    )

    assert graph.to_dict()["edges"] == base.to_dict()["edges"]
    assert graph.to_dict()["priority_base_counts"] == {
        "slot-0001": 4,
        "slot-0002": 4,
    }


def test_existing_six_edge_base_is_never_grown_or_trimmed():
    contract, stage4, _base = _fixture()
    expanded = build_candidate_edge_graph(
        contract,
        stage4=stage4,
        expanded_slot_ids=["slot-0001", "slot-0002"],
    )
    graph = supplement_priority_candidate_graph(
        contract,
        stage4=stage4,
        base_graph=expanded,
        preferred_candidate_ids=["candidate-5"],
    )

    assert graph.to_dict()["edges"] == expanded.to_dict()["edges"]
    assert graph.edge_count == expanded.edge_count == 12


def _by_slot(edges):
    result = {}
    for edge in edges:
        result.setdefault(edge["slot_id"], []).append(edge)
    return result
