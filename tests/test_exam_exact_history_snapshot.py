from __future__ import annotations

import copy
import hashlib
import json
import sqlite3

import pytest

from cuoti_app.migrations.v46_exam_generation_v3_publish import USAGE
from cuoti_app.services.exam_generation_phase8_authority import (
    AUTHORITY_REGISTRY_VERSION,
    EXACT_HISTORY_SNAPSHOT_VERSION,
    Phase8AuthorityError,
    build_stage5_policy_fact_set,
)
from cuoti_app.services.exam_generation_past_v3_production import (
    build_recent_usage_snapshot,
)
from cuoti_app.services.exam_generation_stage5_solver import (
    EXACT_HISTORY_AUDIT_POLICY_VERSION,
    RECENT_USAGE_AUDIT_POLICY_VERSION,
    Stage5ContractError,
    _recent_usage_effect,
)
from cuoti_app.services.exam_generation_v3_publish import USAGE_VERSION


CONTENT_A = "a" * 64
CONTENT_B = "b" * 64
CONTENT_CHANGED = "c" * 64


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _usage_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE exams(id INTEGER PRIMARY KEY,subject_id INTEGER NOT NULL)")
    db.execute(
        f"CREATE TABLE {USAGE}(job_id TEXT,exam_id INTEGER,position INTEGER,"
        "candidate_id TEXT,content_hash TEXT,usage_hash TEXT,payload_json TEXT,created_at TEXT)"
    )
    db.executemany("INSERT INTO exams VALUES(?,?)", [(1, 7), (2, 7), (3, 7), (4, 8)])
    rows = [
        ("job-old", 1, 1, "candidate-original", CONTENT_A, "2026-09-01T00:00:00+00:00"),
        ("job-middle", 2, 2, "candidate-b", CONTENT_B, "2026-09-02T00:00:00+00:00"),
        ("job-new", 3, 1, "candidate-renamed", CONTENT_A, "2026-09-03T00:00:00+00:00"),
        ("other-subject", 4, 1, "candidate-original", CONTENT_A, "2026-09-04T00:00:00+00:00"),
    ]
    for job_id, exam_id, position, candidate_id, content_hash, created_at in rows:
        publication_hash = hashlib.sha256(f"publication:{job_id}".encode()).hexdigest()
        payload = {
            "version": USAGE_VERSION,
            "job_id": job_id,
            "exam_id": exam_id,
            "position": position,
            "candidate_id": candidate_id,
            "content_hash": content_hash,
            "publication_hash": publication_hash,
        }
        db.execute(
            f"INSERT INTO {USAGE} VALUES(?,?,?,?,?,?,?,?)",
            (
                job_id,
                exam_id,
                position,
                candidate_id,
                content_hash,
                _digest(payload),
                _canonical(payload),
                created_at,
            ),
        )
    return db


def _entry(version, payload):
    body = {"version": version, "payload": payload}
    return {**body, "authority_hash": _digest(body)}


def _authority_inputs(stage4):
    source_identity = {"catalog": "fixture", "projection": "exact-history"}
    entries = {
        "chapter_weight": _entry("chapter-v1", {"subject": "数学", "weights": {"代数": 1}}),
        "diversity": _entry("diversity-v1", {"max_per_source_exam": 1}),
        "coverage": _entry("coverage-v1", {"minima": []}),
        "uniqueness": _entry("uniqueness-v1", {"resource_keys": ["candidate_id", "content_hash"]}),
    }
    body = {
        "registry_version": AUTHORITY_REGISTRY_VERSION,
        "source_identity": source_identity,
        "entries": entries,
    }
    registry = {**body, "registry_hash": _digest(body)}
    stage3 = {"source_identity": source_identity, "source_quotas": {"ordinary": 1}}
    return stage3, stage4, registry


def _fact_set(snapshot, stage4):
    stage3, stage4, registry = _authority_inputs(stage4)
    return build_stage5_policy_fact_set(
        stage3=stage3,
        stage4=stage4,
        authority_registry=registry,
        recent_usage_snapshot=snapshot,
    )


def test_snapshot_v3_keeps_verified_window_order_without_candidate_matching():
    snapshot = build_recent_usage_snapshot(_usage_db(), subject_id=7, limit=4)
    assert snapshot["snapshot_version"] == EXACT_HISTORY_SNAPSHOT_VERSION
    assert snapshot["mode"] == "past"
    assert snapshot["subject_id"] == 7
    assert snapshot["window_limit"] == 4
    assert [row["job_id"] for row in snapshot["exact_uses"]] == [
        "job-new", "job-middle", "job-old"
    ]
    assert [row["order_rank"] for row in snapshot["exact_uses"]] == [1, 2, 3]
    assert [row["content_hash"] for row in snapshot["exact_uses"]] == [
        CONTENT_A, CONTENT_B, CONTENT_A
    ]


def test_phase8_projects_content_only_frequency_and_latest_order_for_stage4():
    snapshot = build_recent_usage_snapshot(_usage_db(), subject_id=7, limit=4)
    stage4 = {
        "candidates": [
            {"candidate_id": "candidate-original", "content_hash": CONTENT_CHANGED},
            {"candidate_id": "entirely-new-id", "content_hash": CONTENT_A},
        ]
    }
    recent = _fact_set(snapshot, stage4)["recent_usage"]
    assert recent["version"] == (
        f"{EXACT_HISTORY_AUDIT_POLICY_VERSION}:sha256-{snapshot['snapshot_hash']}"
    )
    assert recent["payload"]["scope"] == {
        "generation_mode": "past", "subject_id": 7, "window_limit": 4
    }
    assert recent["payload"]["ordered_usage_count"] == 3
    assert recent["payload"]["content_hash_hits"] == [
        {
            "content_hash": CONTENT_A,
            "success_count": 2,
            "latest_order_rank": 1,
            "latest_used_at": "2026-09-03T00:00:00+00:00",
            "usage_hashes": [
                snapshot["exact_uses"][0]["usage_hash"],
                snapshot["exact_uses"][2]["usage_hash"],
            ],
        }
    ]
    assert _recent_usage_effect(recent, stage4["candidates"]) == (set(), set())


def test_empty_valid_v3_history_is_available_but_has_no_hits():
    snapshot = build_recent_usage_snapshot(_usage_db(), subject_id=99, limit=4)
    stage4 = {"candidates": [{"candidate_id": "new", "content_hash": CONTENT_A}]}
    recent = _fact_set(snapshot, stage4)["recent_usage"]
    assert snapshot["exact_uses"] == []
    assert recent["payload"]["ordered_usage_count"] == 0
    assert recent["payload"]["content_hash_hits"] == []
    assert _recent_usage_effect(recent, stage4["candidates"]) == (set(), set())


def test_exact_history_scope_cannot_cross_modes():
    snapshot = build_recent_usage_snapshot(_usage_db(), subject_id=7, limit=4)
    stage4 = {"candidates": [{"candidate_id": "new", "content_hash": CONTENT_A}]}
    changed = copy.deepcopy(snapshot)
    changed["mode"] = "free"
    changed["snapshot_hash"] = _digest({key: value for key, value in changed.items() if key != "snapshot_hash"})
    with pytest.raises(Phase8AuthorityError):
        _fact_set(changed, stage4)
    recent = _fact_set(snapshot, stage4)["recent_usage"]
    recent["payload"]["scope"]["generation_mode"] = "free"
    with pytest.raises(Stage5ContractError):
        _recent_usage_effect(recent, stage4["candidates"])


def test_v2_recent_fact_keeps_candidate_or_content_semantics_for_old_replay():
    snapshot_hash = "d" * 64
    candidates = [
        {"candidate_id": "same-candidate", "content_hash": CONTENT_CHANGED},
        {"candidate_id": "renamed-candidate", "content_hash": CONTENT_A},
    ]
    recent = {
        "version": f"{RECENT_USAGE_AUDIT_POLICY_VERSION}:sha256-{snapshot_hash}",
        "payload": {
            "mode": "audit_only",
            "snapshot_identity": "legacy-v2",
            "snapshot_hash": snapshot_hash,
            "observed_candidate_ids": ["same-candidate"],
            "observed_content_hashes": [CONTENT_A],
            "candidate_id_hits": ["same-candidate"],
            "content_hash_hits": [CONTENT_A],
        },
    }
    assert _recent_usage_effect(recent, candidates) == (set(), set())


def test_v3_rejects_reordered_events_and_inconsistent_hit_count():
    snapshot = build_recent_usage_snapshot(_usage_db(), subject_id=7, limit=4)
    reordered = copy.deepcopy(snapshot)
    reordered["exact_uses"][0], reordered["exact_uses"][1] = (
        reordered["exact_uses"][1], reordered["exact_uses"][0]
    )
    for rank, row in enumerate(reordered["exact_uses"], start=1):
        row["order_rank"] = rank
    reordered["snapshot_hash"] = _digest(
        {key: reordered[key] for key in reordered if key != "snapshot_hash"}
    )
    stage4 = {"candidates": [{"candidate_id": "candidate", "content_hash": CONTENT_A}]}
    with pytest.raises(Phase8AuthorityError) as phase8_error:
        _fact_set(reordered, stage4)
    assert phase8_error.value.reason == "recent_usage_snapshot_invalid"

    recent = _fact_set(snapshot, stage4)["recent_usage"]
    recent["payload"]["content_hash_hits"][0]["success_count"] += 1
    with pytest.raises(Stage5ContractError) as stage5_error:
        _recent_usage_effect(recent, stage4["candidates"])
    assert stage5_error.value.reason == "recent_usage_audit_hits_mismatch"
