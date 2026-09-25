from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

from cuoti_app.migrations.v53_exam_generation_priority_once import migrate_v52_to_v53
from cuoti_app.services.exam_generation_d2_quality_repair import (
    content_repair_graph,
    repair_pool_graph,
    without_rejected_content,
)
from cuoti_app.services.exam_generation_mistake_relations import RELATION_VERSION
from cuoti_app.services.exam_generation_priority_runtime import (
    prepare_priority_base,
    select_with_priority,
)
from cuoti_app.services.exam_generation_stage5_candidate_base_runner import (
    build_candidate_stage5_d2_base,
)
from cuoti_app.services.exam_generation_stage5_d2_solver import (
    solve_stage5_d2_selection,
)
from cuoti_app.services.exam_generation_stage6_local_factor_contract import (
    build_local_factor_receipt,
    project_local_factor_receipts_for_d2,
)
from cuoti_app.services.exam_generation_stage6_objective import build_objective_policy


def _digest(value):
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _frozen(version, payload):
    body = {"version": version, "payload": payload}
    return {**body, "hash": _digest(body)}


def _candidate(
    candidate_id,
    *,
    question_type,
    source_kind="ordinary",
    content=None,
    source_exam_id=None,
):
    content = content or f"题目 {candidate_id}"
    is_ordinary = source_kind == "ordinary"
    return {
        "candidate_id": candidate_id,
        "source_kind": source_kind,
        "subject": "数学",
        "question_type_family": question_type,
        "chapter": "第一章" if is_ordinary else None,
        "source_exam_id": None if is_ordinary else source_exam_id,
        "section_id": None if is_ordinary else question_type,
        "source_institution": None if is_ordinary else "外校",
        "content_hash": _digest({"content": content}),
        "content_snapshot": {
            "schema_version": "semantic-candidate-content-snapshot-v1",
            "content": content,
            "conditions": [],
            "options": [],
            "figure_references": [],
        },
        "difficulty_fact": {"resolved": True, "label": "中等"},
    }


def _policy():
    return build_objective_policy(
        parameter_set_hash="a" * 64,
        level_mapping={
            "verified_mistake_relevance": {
                "unknown": 0,
                "not_applicable": 0,
                "none": 0,
                "related": 1,
                "strong": 2,
            },
            "history_exposure_novelty": {
                "unknown": 0,
                "not_applicable": 0,
                "exposed": -1,
                "novel": 1,
            },
        },
        directions={
            "verified_mistake_relevance": "max",
            "history_exposure_novelty": "max",
        },
    )


def _factor_receipts(graph, policy):
    identities = {
        (
            edge["candidate_id"],
            edge["candidate_fact_hash"],
            edge["content_hash"],
        )
        for edge in graph.to_dict()["edges"]
    }
    return [
        build_local_factor_receipt(
            candidate_id=candidate_id,
            candidate_fact_hash=fact_hash,
            content_hash=content_hash,
            factors={
                "verified_mistake_relevance": {
                    "level": "none",
                    "evidence_codes": ["fixture-no-relation"],
                },
                "history_exposure_novelty": {
                    "level": "novel",
                    "evidence_codes": ["fixture-local-history"],
                },
            },
            policy=policy,
        )
        for candidate_id, fact_hash, content_hash in sorted(identities)
    ]


def _solve(base, policy):
    receipts = _factor_receipts(base.graph, policy)
    return solve_stage5_d2_selection(
        base.constraint,
        base.graph,
        objective_policy=policy,
        factor_receipts=project_local_factor_receipts_for_d2(
            receipts, policy=policy
        ),
    )


def _case():
    component = [
        _candidate(f"component-{index}", question_type="component")
        for index in range(5)
    ]
    priority = _candidate(
        "zz-priority", question_type="component", content="错题本精确关联题"
    )
    outside_three = [
        _candidate(
            f"outside-three-{index}",
            question_type="outside-three",
            source_kind="past_exam_other",
            source_exam_id=f"exam-three-{index}",
        )
        for index in range(3)
    ]
    outside_four = [
        _candidate(
            f"outside-four-{index}",
            question_type="outside-four",
            source_kind="past_exam_other",
            source_exam_id=f"exam-four-{index}",
        )
        for index in range(3)
    ]
    candidates = component + [priority] + outside_three + outside_four
    stage3 = {
        "contract_version": "past-v3-generation-contract-v1",
        "profile_hash": "b" * 64,
        "exact_slot_hash": "c" * 64,
        "question_count": 4,
        "combination": {
            "institution": "目标院校",
            "subject": "数学",
            "reference_year": 2025,
        },
        "target_institution_exclusion": {"institution": "目标院校"},
        "source_identity": {
            "catalog_version": "catalog-v1",
            "course_version": "course-v1",
            "source_snapshot": "d" * 64,
            "source_exam_id": "reference-exam",
        },
        "slot_demands": [
            {
                "position": position,
                "section_id": f"section-{position}",
                "question_type_family": question_type,
                "target_difficulty": "中等",
            }
            for position, question_type in enumerate(
                ("component", "component", "outside-three", "outside-four"),
                1,
            )
        ],
        "source_quotas": {"ordinary": 2, "past_exam_other": 2},
    }
    stage4 = {
        "contract_version": "past-v3-stage4-candidate-facts-v1",
        "projection_identity_hash": "1" * 64,
        "candidates": candidates,
    }
    weights = {
        "version": "chapter-weights-v1",
        "subject": "数学",
        "weights": {"第一章": 1},
    }
    base = build_candidate_stage5_d2_base(
        stage3,
        stage4,
        stage3_contract_hash=_digest(stage3),
        candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4["projection_identity_hash"],
        chapter_weight_fact={**weights, "fact_hash": _digest(weights)},
        recent_usage=_frozen(
            "recent-v1",
            {"excluded_candidate_ids": [], "excluded_content_hashes": []},
        ),
        diversity_policy=_frozen("diversity-v1", {"max_per_source_exam": 1}),
        coverage_policy=_frozen("coverage-v1", {"minima": []}),
        uniqueness_policy=_frozen(
            "uniqueness-v1", {"resource_keys": ["candidate_id", "content_hash"]}
        ),
        policy_fact_set_hash="f" * 64,
    )
    policy = _policy()
    selection = _solve(base, policy)
    state = SimpleNamespace(base=base, selection=selection)
    return base, policy, state, candidates


def _by_slot(selection):
    return {row["slot_id"]: dict(row) for row in selection.assignments}


def _assert_hard_contract(selection):
    rows = list(selection.assignments)
    assert sum(row["source_kind"] == "ordinary" for row in rows) == 2
    assert sum(row["source_kind"] == "past_exam_other" for row in rows) == 2
    assert sum(
        row["source_kind"] == "ordinary" and row["chapter"] == "第一章"
        for row in rows
    ) == 2
    assert len({row["candidate_id"] for row in rows}) == len(rows)
    assert len({row["content_hash"] for row in rows}) == len(rows)
    source_exams = [row["source_exam_id"] for row in rows if row["source_exam_id"]]
    assert len(source_exams) == len(set(source_exams))


def test_content_repair_changes_only_minimal_hard_resource_component():
    base, policy, state, _candidates = _case()
    original = _by_slot(state.selection)
    rejected_hash = original["slot-0001"]["content_hash"]

    repair_graph, affected = content_repair_graph(state, {rejected_hash})
    repair_base = replace(base, graph=repair_graph)
    replacement = _solve(repair_base, policy)
    updated = _by_slot(replacement)

    assert affected == {"slot-0001", "slot-0002"}
    assert updated["slot-0001"]["content_hash"] != rejected_hash
    assert rejected_hash not in {row["content_hash"] for row in replacement.assignments}
    for slot_id in ("slot-0003", "slot-0004"):
        assert json.dumps(updated[slot_id], sort_keys=True, separators=(",", ":")) == (
            json.dumps(original[slot_id], sort_keys=True, separators=(",", ":"))
        )
    _assert_hard_contract(replacement)


def test_repair_pool_retains_other_slot_candidates_for_later_local_repair():
    _base, _policy_value, state, _candidates = _case()
    original = _by_slot(state.selection)
    rejected_hash = original["slot-0001"]["content_hash"]

    repair_graph, affected = content_repair_graph(state, {rejected_hash})
    solve_view = repair_graph.to_dict()
    retained_pool = repair_pool_graph(repair_graph).to_dict()

    assert affected == {"slot-0001", "slot-0002"}
    for slot_id in ("slot-0003", "slot-0004"):
        solve_edges = [
            edge for edge in solve_view["edges"] if edge["slot_id"] == slot_id
        ]
        pool_edges = [
            edge for edge in retained_pool["edges"] if edge["slot_id"] == slot_id
        ]
        assert len(solve_edges) == 1
        assert solve_edges[0]["candidate_id"] == original[slot_id]["candidate_id"]
        assert len(pool_edges) == 3
        assert {edge["candidate_id"] for edge in pool_edges} > {
            original[slot_id]["candidate_id"]
        }


def test_rejected_content_cannot_return_through_priority_supplement():
    base, policy, state, candidates = _case()
    original = _by_slot(state.selection)
    rejected_hash = original["slot-0001"]["content_hash"]
    rejected_content = next(
        row["content_snapshot"]["content"]
        for row in candidates
        if row["content_hash"] == rejected_hash
    )
    priority_content = next(
        row["content_snapshot"]["content"]
        for row in candidates
        if row["candidate_id"] == "zz-priority"
    )
    relations = {
        "mode": "past",
        "allowed_sources": ["mistakes"],
        "payload": {
            "version": RELATION_VERSION,
            "subject_id": 7,
            "state": "usable",
            "records": [
                {"public_id": "mistake-priority", "content": priority_content},
                {"public_id": "mistake-rejected", "content": rejected_content},
            ],
        },
    }
    repair_graph, _affected = content_repair_graph(state, {rejected_hash})
    repair_base = replace(base, graph=repair_graph)
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE background_jobs(id TEXT PRIMARY KEY)")
    migrate_v52_to_v53(db)
    try:
        priority_base = prepare_priority_base(
            db,
            base=repair_base,
            subject_id=7,
            mistake_relations=relations,
            rejected={rejected_hash},
        )
        edges = priority_base.graph.to_dict()["edges"]
        assert any(edge["candidate_id"] == "zz-priority" for edge in edges)
        assert rejected_hash not in {edge["content_hash"] for edge in edges}

        factors = _factor_receipts(priority_base.graph, policy)
        selected, keys = select_with_priority(
            db,
            base=priority_base,
            policy=policy,
            factors=factors,
            subject_id=7,
            mistake_relations=relations,
            search_guard=None,
        )
        assert any(row["candidate_id"] == "zz-priority" for row in selected.assignments)
        assert rejected_hash not in {
            row["content_hash"] for row in selected.assignments
        }
        assert keys
        _assert_hard_contract(selected)
    finally:
        db.close()


def test_without_rejected_content_filters_nested_pool_and_updates_base_counts():
    base, _policy_value, state, _candidates = _case()
    rejected_hash = _by_slot(state.selection)["slot-0001"]["content_hash"]
    repair_graph, _affected = content_repair_graph(state, {rejected_hash})
    cleaned = without_rejected_content(repair_graph, {rejected_hash}).to_dict()

    assert "quality_repair_pool" not in cleaned
    assert rejected_hash not in {edge["content_hash"] for edge in cleaned["edges"]}
    assert all(
        candidate_id in {edge["candidate_id"] for edge in cleaned["edges"]}
        for candidate_id in cleaned["candidates"]
    )
