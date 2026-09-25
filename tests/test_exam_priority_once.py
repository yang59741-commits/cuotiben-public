from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from cuoti_app.migrations.v53_exam_generation_priority_once import (
    D2_RESELECTIONS,
    PRIORITY_ONCE_CONSUMPTIONS,
    PRIORITY_ONCE_SELECTIONS,
    migrate_v52_to_v53,
    schema_issues,
)
from cuoti_app.services.exam_generation_mistake_relations import RELATION_VERSION
from cuoti_app.services.exam_generation_priority_once import (
    PriorityOnceConflictError,
    PriorityOnceError,
    assert_priority_once_available,
    consume_priority_once_in_transaction,
    derive_priority_once_keys,
    read_unconsumed_priority_once,
    verify_priority_once_consumed,
)
from cuoti_app.services.exam_generation_stage5_solver import (
    CANDIDATE_GRAPH_VERSION,
    FrozenCandidateEdgeGraph,
)


def _open(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE background_jobs(id TEXT PRIMARY KEY)")
    migrate_v52_to_v53(db)
    db.commit()
    return db


def _candidate(candidate_id: str, content_hash: str, content: str, **extra):
    return {
        "candidate_id": candidate_id,
        "content_hash": content_hash,
        "content_snapshot": json.dumps({
            "content": content,
            "conditions": [],
            "options": [],
            "figure_references": [],
        }),
        **extra,
    }


def _inputs():
    content = "求 x+1=2 的解。"
    stage4 = {
        "candidates": [
            _candidate("hard", "a" * 64, content, candidate_fact_hash="old"),
            _candidate("outside-graph", "b" * 64, content),
            {
                **_candidate("auxiliary", "c" * 64, content),
                "content_snapshot": json.dumps({
                    "content": content,
                    "conditions": ["x is real"],
                    "options": [],
                    "figure_references": [],
                }),
            },
        ]
    }
    edges = [
        {"candidate_id": "hard", "content_hash": "a" * 64},
        {"candidate_id": "auxiliary", "content_hash": "c" * 64},
    ]
    graph_payload = {
        "contract_version": CANDIDATE_GRAPH_VERSION,
        "candidates": {"hard": {}, "auxiliary": {}},
        "edges": edges,
    }
    graph = FrozenCandidateEdgeGraph(
        CANDIDATE_GRAPH_VERSION, "d" * 64, "e" * 64, "initial", len(edges),
        "f" * 64, json.dumps(graph_payload),
    )
    relations = {
        "mode": "past",
        "allowed_sources": ["mistakes"],
        "payload": {
            "version": RELATION_VERSION,
            "subject_id": 7,
            "state": "usable",
            "records": [
                {"public_id": "mistake-a", "content": content},
                {"public_id": "mistake-b", "content": content},
            ],
        },
    }
    return stage4, graph, relations


def _derive(stage4=None):
    original, graph, relations = _inputs()
    return derive_priority_once_keys(
        subject_id=7,
        mode="past",
        stage4=original if stage4 is None else stage4,
        graph=graph,
        mistake_relations=relations,
    )


def _consume(db, keys, *, job="job-a", exam=11, publication="1" * 64):
    return consume_priority_once_in_transaction(
        db,
        keys=keys,
        publication_hash=publication,
        job_id=job,
        exam_id=exam,
        consumed_at="2026-09-06T00:00:00Z",
    )


def test_schema_has_exact_consumption_key_and_runtime_append_tables():
    with _open() as db:
        assert schema_issues(db) == []
        pk = {
            row[1]: row[5]
            for row in db.execute(f"PRAGMA table_info({PRIORITY_ONCE_CONSUMPTIONS})")
            if row[5]
        }
        assert pk == {
            "subject_id": 1,
            "mode": 2,
            "candidate_content_hash": 3,
            "source_mistake_public_id": 4,
            "relation_content_fingerprint": 5,
        }
        assert {row[1] for row in db.execute(f"PRAGMA table_info({PRIORITY_ONCE_SELECTIONS})")} == {
            "job_id", "selection_hash", "claims_json",
        }
        assert {row[1] for row in db.execute(f"PRAGMA table_info({D2_RESELECTIONS})")} == {
            "job_id", "round", "previous_selection_hash", "next_selection_hash",
            "reason", "quality_state_json", "created_at",
        }


def test_exact_hard_compatible_relation_returns_one_key_per_lineage():
    eligible = _derive()
    assert set(eligible) == {"hard"}
    assert [key.source_mistake_public_id for key in eligible["hard"]] == [
        "mistake-a", "mistake-b",
    ]
    assert len({key.relation_content_fingerprint for key in eligible["hard"]}) == 1
    assert all(key.candidate_content_hash == "a" * 64 for key in eligible["hard"])


def test_publication_rollback_does_not_consume_and_fact_recompute_does_not_restore():
    stage4, graph, relations = _inputs()
    keys = _derive(stage4)["hard"]
    with _open() as db:
        db.execute("BEGIN IMMEDIATE")
        _consume(db, keys)
        db.rollback()
        assert read_unconsumed_priority_once(
            db, subject_id=7, mode="past", stage4=stage4, graph=graph,
            mistake_relations=relations,
        )["hard"] == keys

        db.execute("BEGIN IMMEDIATE")
        _consume(db, keys)
        db.commit()
        recomputed = copy.deepcopy(stage4)
        recomputed["candidates"][0]["candidate_fact_hash"] = "new-nonsemantic-fact"
        assert read_unconsumed_priority_once(
            db, subject_id=7, mode="past", stage4=recomputed, graph=graph,
            mistake_relations=relations,
        ) == {}


def test_same_job_recovery_is_idempotent_and_identity_drift_is_rejected():
    keys = _derive()["hard"]
    with _open() as db:
        db.execute("BEGIN IMMEDIATE")
        first = _consume(db, keys)
        second = _consume(db, keys)
        assert first == second
        assert db.execute(f"SELECT COUNT(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 2
        with pytest.raises(PriorityOnceError, match="different publication") as exc:
            _consume(db, keys, publication="2" * 64)
        assert exc.value.reason == "same_job_publication_identity_drift"
        db.commit()


def test_read_only_availability_and_recovery_verification_are_strict():
    keys = _derive()["hard"]
    with _open() as db:
        assert_priority_once_available(db, keys=keys, job_id="job-a")
        with pytest.raises(PriorityOnceError) as missing:
            verify_priority_once_consumed(
                db, keys=keys, publication_hash="1" * 64, job_id="job-a", exam_id=11,
            )
        assert missing.value.reason == "priority_once_consumption_missing"

        db.execute("BEGIN IMMEDIATE")
        _consume(db, keys)
        db.commit()
        assert_priority_once_available(db, keys=keys, job_id="job-a")
        verify_priority_once_consumed(
            db, keys=keys, publication_hash="1" * 64, job_id="job-a", exam_id=11,
        )
        with pytest.raises(PriorityOnceConflictError):
            assert_priority_once_available(db, keys=keys, job_id="job-b")
        with pytest.raises(PriorityOnceError) as drift:
            verify_priority_once_consumed(
                db, keys=keys, publication_hash="2" * 64, job_id="job-a", exam_id=11,
            )
        assert drift.value.reason == "priority_once_consumption_identity_drift"


def test_multi_connection_stale_claim_conflicts_without_partial_consumption(tmp_path):
    database = str(tmp_path / "priority.db")
    with _open(database):
        pass
    first = sqlite3.connect(database, timeout=2)
    second = sqlite3.connect(database, timeout=2)
    first.row_factory = second.row_factory = sqlite3.Row
    try:
        keys = _derive()["hard"]
        # Both connections observed the same unconsumed eligibility before publish.
        assert all(_consume_key_unseen(first, key) for key in keys)
        assert all(_consume_key_unseen(second, key) for key in keys)

        first.execute("BEGIN IMMEDIATE")
        _consume(first, keys[:1], job="winner")
        first.commit()

        second.execute("BEGIN IMMEDIATE")
        with pytest.raises(PriorityOnceConflictError) as exc:
            _consume(second, keys, job="loser", exam=12, publication="2" * 64)
        assert exc.value.reason == "priority_once_already_consumed_by_other_job"
        assert exc.value.keys == keys[:1]
        second.commit()
        rows = second.execute(
            f"SELECT source_mistake_public_id,job_id FROM {PRIORITY_ONCE_CONSUMPTIONS} "
            "ORDER BY source_mistake_public_id"
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [("mistake-a", "winner")]
    finally:
        first.close()
        second.close()


def _consume_key_unseen(db, key) -> bool:
    row = db.execute(
        f"SELECT 1 FROM {PRIORITY_ONCE_CONSUMPTIONS} WHERE "
        "subject_id=? AND mode=? AND candidate_content_hash=? AND "
        "source_mistake_public_id=? AND relation_content_fingerprint=?",
        (
            key.subject_id, key.mode, key.candidate_content_hash,
            key.source_mistake_public_id, key.relation_content_fingerprint,
        ),
    ).fetchone()
    return row is None


def test_consumption_is_not_tied_to_exam_deletion_and_requires_transaction():
    keys = _derive()["hard"]
    with _open() as db:
        db.execute("CREATE TABLE exams(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO exams VALUES(11)")
        db.commit()
        with pytest.raises(PriorityOnceError) as exc:
            _consume(db, keys)
        assert exc.value.reason == "publication_transaction_required"
        db.execute("BEGIN IMMEDIATE")
        _consume(db, keys)
        db.execute("DELETE FROM exams WHERE id=11")
        db.commit()
        assert db.execute(f"SELECT COUNT(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 2
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute(f"DELETE FROM {PRIORITY_ONCE_CONSUMPTIONS}")
