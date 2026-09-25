from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest

from cuoti_app.services.exam_generation_stage5_candidate_base_runner import (
    build_candidate_stage5_d2_base,
)
from cuoti_app.services.exam_generation_stage5_d2_solver import (
    D2SearchIncompleteError,
    solve_stage5_d2_selection,
)
from cuoti_app.services.exam_generation_stage6_d2_repository import (
    D2_CHECKPOINTS,
    D2_HEADS,
    D2_SELECTIONS,
    D2CandidateCheckpoint,
    D2RepositoryError,
    d2_candidate_schema_issues,
    install_d2_candidate_schema_draft,
    persist_d2_candidate_chain,
    recover_d2_candidate_chain,
    _selection_from_payload,
)
from cuoti_app.services.exam_generation_stage6_local_factor_contract import (
    build_local_factor_receipt,
    project_local_factor_receipts_for_d2,
)
from cuoti_app.services.exam_generation_stage6_objective import (
    build_objective_policy,
)
from cuoti_app.services.exam_generation_similarity import build_similarity_freeze, attach_similarity_freeze


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _frozen(version, payload):
    body = {"version": version, "payload": payload}
    return {**body, "hash": _digest(body)}


def _case():
    stage3 = {
        "contract_version": "past-v3-generation-contract-v1",
        "profile_hash": "b" * 64,
        "exact_slot_hash": "c" * 64,
        "question_count": 1,
        "combination": {"institution": "目标", "subject": "数学", "reference_year": 2025},
        "target_institution_exclusion": {"institution": "目标"},
        "source_identity": {
            "catalog_version": "catalog-v1",
            "course_version": "course-v1",
            "source_snapshot": "d" * 64,
            "source_exam_id": "source-exam",
        },
        "slot_demands": [
            {
                "position": 1,
                "section_id": "section",
                "question_type_family": "single",
                "target_difficulty": "中等",
            }
        ],
        "source_quotas": {"ordinary": 1},
    }
    stage4 = {
        "contract_version": "past-v3-stage4-candidate-facts-v1",
        "projection_identity_hash": "1" * 64,
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "source_kind": "ordinary",
                "subject": "数学",
                "question_type_family": "single",
                "chapter": "第一章",
                "source_exam_id": None,
                "section_id": None,
                "source_institution": None,
                "content_hash": "f" * 64,
                "difficulty_fact": {"resolved": True, "label": "中等"},
            }
        ],
    }
    chapter = {"version": "chapter-weights-v1", "subject": "数学", "weights": {"第一章": 1}}
    base = build_candidate_stage5_d2_base(
        stage3,
        stage4,
        stage3_contract_hash=_digest(stage3),
        candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4["projection_identity_hash"],
        chapter_weight_fact={**chapter, "fact_hash": _digest(chapter)},
        recent_usage=_frozen("recent-v1", {"excluded_candidate_ids": [], "excluded_content_hashes": []}),
        diversity_policy=_frozen("diversity-v1", {"max_per_source_exam": 1}),
        coverage_policy=_frozen("coverage-v1", {"minima": []}),
        uniqueness_policy=_frozen("uniqueness-v1", {"resource_keys": ["candidate_id", "content_hash"]}),
        policy_fact_set_hash="a" * 64,
    )
    policy = build_objective_policy(
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
    factors = [
        build_local_factor_receipt(
            candidate_id=edge["candidate_id"],
            candidate_fact_hash=edge["candidate_fact_hash"],
            content_hash=edge["content_hash"],
            factors={
                "verified_mistake_relevance": {"level": "related", "evidence_codes": ["local"]},
                "history_exposure_novelty": {"level": "novel", "evidence_codes": ["history"]},
            },
            policy=policy,
        )
        for edge in base.graph.to_dict()["edges"]
    ]
    selection = solve_stage5_d2_selection(
        base.constraint,
        base.graph,
        factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy),
        objective_policy=policy,
    )
    return base, policy, factors, selection


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    install_d2_candidate_schema_draft(db)
    return db


def test_checkpoint_replay_respects_search_budget_and_preserves_unknown():
    base, policy, factors, selection = _case()
    db = _db()
    state = persist_d2_candidate_chain(db, job_id="bounded-replay", base=base,
        objective_policy=policy, factor_receipts=factors, selection=selection,
        stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0})
    with pytest.raises(D2SearchIncompleteError):
        recover_d2_candidate_chain(db, job_id="bounded-replay", search_guard=lambda: False)
    assert recover_d2_candidate_chain(db, job_id="bounded-replay").selection == state.selection


def test_d2_chain_persists_replays_and_keeps_old_schema_unreferenced():
    base, policy, factors, selection = _case()
    db = _db()
    state = persist_d2_candidate_chain(
        db,
        job_id="d2-job",
        base=base,
        objective_policy=policy,
        factor_receipts=factors,
        selection=selection,
        stage_durations_ms={"local_factor_ms": 1, "local_solve_ms": 2},
        created_at="t0",
    )
    assert state.selection.selection_hash == selection.selection_hash
    assert state.head["state"] == "checkpointed"
    assert state.head["revision"] == 4
    assert d2_candidate_schema_issues(db) == []
    assert db.execute(f"SELECT COUNT(*) FROM {D2_SELECTIONS}").fetchone()[0] == 1
    assert db.execute(f"SELECT COUNT(*) FROM {D2_HEADS}").fetchone()[0] == 1
    recovered = recover_d2_candidate_chain(db, job_id="d2-job")
    assert recovered.selection.to_dict() == selection.to_dict()
    replay = persist_d2_candidate_chain(
        db,
        job_id="d2-job",
        base=base,
        objective_policy=policy,
        factor_receipts=factors,
        selection=selection,
        stage_durations_ms={"local_factor_ms": 1, "local_solve_ms": 2},
        created_at="different-timestamp",
    )
    assert replay.checkpoint.checkpoint_hash == state.checkpoint.checkpoint_hash
    assert db.execute(f"SELECT revision FROM {D2_HEADS} WHERE job_id='d2-job'").fetchone()[0] == 4


def test_d2_chain_persists_and_replays_m_once_eligibility_identity():
    base, policy, factors, _selection = _case()
    edge = base.graph.to_dict()["edges"][0]
    eligible = {(edge["candidate_id"], edge["candidate_fact_hash"], edge["content_hash"])}
    selection = solve_stage5_d2_selection(
        base.constraint,
        base.graph,
        factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy),
        objective_policy=policy,
        target_mode="past",
        m_once_eligible_identities=eligible,
    )
    db = _db()
    state = persist_d2_candidate_chain(
        db,
        job_id="d2-m-once",
        base=base,
        objective_policy=policy,
        factor_receipts=factors,
        selection=selection,
        stage_durations_ms={"local_factor_ms": 1, "local_solve_ms": 1},
    )
    recovered = recover_d2_candidate_chain(db, job_id="d2-m-once")
    assert state.selection.objective_vector[0] == 1
    assert recovered.selection.to_dict()["m_once_eligibility"] == selection.to_dict()["m_once_eligibility"]
    assert recovered.selection.selection_hash == selection.selection_hash


def test_d2_v4_chain_persists_and_replays_frozen_similarity_receipt():
    base, policy, factors, _selection = _case()
    stage3 = dict(base.stage3)
    stage3["combination"] = dict(stage3["combination"]) | {"subject": "数学分析"}
    stage4 = copy.deepcopy(base.stage4)
    candidate = stage4["candidates"][0]
    candidate["subject"] = "数学分析"
    snapshot = {"schema_version": "semantic-candidate-content-snapshot-v1",
                "content": r"求极限 \(\lim_{x\to0}\frac{\sin(2x)}{x}\)。",
                "conditions": [], "figure_references": [], "options": []}
    raw_snapshot = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    candidate.update({"card_version_id": 1, "card_hash": "e" * 64,
                      "content_snapshot": raw_snapshot,
                      "content_snapshot_hash": _digest(raw_snapshot)})
    rebuilt = build_candidate_stage5_d2_base(
        stage3, stage4,
        stage3_contract_hash=_digest(stage3), candidate_facts_hash=_digest(stage4),
        projection_identity_hash=stage4["projection_identity_hash"],
            chapter_weight_fact={"version": "chapter-weights-v1", "subject": "数学分析", "weights": {"第一章": 1},
                                 "fact_hash": _digest({"version": "chapter-weights-v1", "subject": "数学分析", "weights": {"第一章": 1}})},
        recent_usage=base.constraint.to_dict()["recent_usage"],
        diversity_policy=base.constraint.to_dict()["diversity_policy"],
        coverage_policy=base.constraint.to_dict()["coverage_policy"],
        uniqueness_policy=base.constraint.to_dict()["uniqueness_policy"],
            policy_fact_set_hash="a" * 64,
        )
    edge = rebuilt.graph.to_dict()["edges"][0]
    rebuilt_factors = [
        build_local_factor_receipt(
            candidate_id=edge["candidate_id"],
            candidate_fact_hash=edge["candidate_fact_hash"],
            content_hash=edge["content_hash"],
            factors={
                "verified_mistake_relevance": {"level": "related", "evidence_codes": ["local"]},
                "history_exposure_novelty": {"level": "novel", "evidence_codes": ["history"]},
            },
            policy=policy,
        )
    ]
    receipt = build_similarity_freeze(
        subject="数学分析", candidates=list(rebuilt.stage4["candidates"]),
        graph_edges=rebuilt.graph.to_dict()["edges"],
        card_facts={("ordinary", candidate["candidate_id"]): {"knowledge_points": ["极限"], "methods": ["Taylor"], "question_type": "计算题"}},
    )
    graph = attach_similarity_freeze(rebuilt.graph, receipt)
    rebuilt = type(rebuilt)(rebuilt.protocol, rebuilt.stage3, rebuilt.stage4, rebuilt.constraint, graph)
    selection = solve_stage5_d2_selection(
        rebuilt.constraint, rebuilt.graph,
            factor_receipts=project_local_factor_receipts_for_d2(rebuilt_factors, policy=policy),
        objective_policy=policy, target_mode="past", solver_version="stage5-d2-lexicographic-solver-v4",
    )
    db = _db()
    state = persist_d2_candidate_chain(
        db, job_id="d2-v4-replay", base=rebuilt, objective_policy=policy,
        factor_receipts=rebuilt_factors, selection=selection,
        stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0},
    )
    recovered = recover_d2_candidate_chain(db, job_id="d2-v4-replay")
    assert recovered.selection.solver_version == "stage5-d2-lexicographic-solver-v4"
    assert recovered.selection.to_dict()["similarity_freeze_hash"] == receipt["freeze_hash"]
    assert recovered.base.graph.to_dict()["similarity_receipt"] == receipt


def test_legacy_v1_selection_payload_remains_readable_and_hash_checked():
    _base, policy, _factors, selection = _case()
    current = selection.to_dict()
    legacy_version = "stage5-d2-lexicographic-solver-v1"
    assignments = current["assignments"]
    vector = current["objective_vector"][1:3]
    objective = {
        "version": legacy_version,
        "policy_hash": policy["policy_hash"],
        "factor_order": ["verified_mistake_relevance", "history_exposure_novelty"],
        "vector": vector,
        "graph_hash": current["graph_hash"],
        "assignments": assignments,
    }
    identity = {
        "solver_version": legacy_version,
        "graph_hash": current["graph_hash"],
        "constraint_hash": current["constraint_hash"],
        "policy_hash": current["policy_hash"],
        "factor_receipt_hash": current["factor_receipt_hash"],
        "objective_hash": _digest(objective),
        "assignments": assignments,
    }
    payload = {**identity, "objective_vector": vector, "selection_hash": _digest(identity)}
    restored = _selection_from_payload(payload)
    assert restored.solver_version == legacy_version
    assert restored.to_dict() == payload
    payload["objective_hash"] = "0" * 64
    with pytest.raises(D2RepositoryError, match="selection_hash_mismatch|objective_hash_mismatch"):
        _selection_from_payload(payload)


def test_d2_chain_rejects_bad_cas_and_rolls_back_all_rows():
    base, policy, factors, selection = _case()
    db = _db()
    with pytest.raises(D2RepositoryError, match="head_revision_conflict"):
        persist_d2_candidate_chain(
            db,
            job_id="bad-cas",
            base=base,
            objective_policy=policy,
            factor_receipts=factors,
            selection=selection,
            stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0},
            expected_revision=9,
        )
    assert db.execute(f"SELECT COUNT(*) FROM {D2_HEADS}").fetchone()[0] == 0
    assert db.execute(f"SELECT COUNT(*) FROM {D2_CHECKPOINTS}").fetchone()[0] == 0


def test_d2_recovery_detects_immutable_payload_drift():
    base, policy, factors, selection = _case()
    db = _db()
    state = persist_d2_candidate_chain(
        db,
        job_id="tamper",
        base=base,
        objective_policy=policy,
        factor_receipts=factors,
        selection=selection,
        stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0},
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            f"UPDATE {D2_CHECKPOINTS} SET payload_json='{{}}' WHERE job_id=?",
            ("tamper",),
        )
    db.execute(f"DROP TRIGGER {D2_CHECKPOINTS}_update_forbidden")
    db.execute(
        f"UPDATE {D2_CHECKPOINTS} SET payload_json='{{}}' WHERE job_id=?",
        ("tamper",),
    )
    with pytest.raises(D2RepositoryError):
        recover_d2_candidate_chain(
            db, job_id="tamper", checkpoint_hash=state.checkpoint.checkpoint_hash
        )


def test_d2_selection_is_not_accepted_when_replayed_identity_changes():
    base, policy, factors, selection = _case()
    forged = copy.deepcopy(selection.to_dict())
    forged["objective_vector"] = [999]
    raw = json.dumps(forged, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    from cuoti_app.services.exam_generation_stage5_d2_solver import D2Selection, D2_SOLVER_VERSION

    forged_selection = D2Selection(
        D2_SOLVER_VERSION,
        forged["graph_hash"],
        forged["constraint_hash"],
        forged["objective_hash"],
        forged["selection_hash"],
        tuple(forged["objective_vector"]),
        tuple(forged["assignments"]),
        raw,
    )
    with pytest.raises(D2RepositoryError, match="selection_replay_invalid"):
        persist_d2_candidate_chain(
            _db(),
            job_id="forged",
            base=base,
            objective_policy=policy,
            factor_receipts=factors,
            selection=forged_selection,
            stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0},
        )
