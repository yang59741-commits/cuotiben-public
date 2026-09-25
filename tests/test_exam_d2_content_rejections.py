from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from cuoti_app import database
from tests.migration_fixtures import create_historical_database
from cuoti_app.migrations import MIGRATIONS, migration_schema_issues
from cuoti_app.migrations.v55_exam_generation_d2_content_rejections import (
    migrate_v54_to_v55,
    schema_issues,
)
from cuoti_app.services.exam_generation_d2_content_rejections import (
    CONTENT_REJECTIONS,
    PROBE_RECEIPTS,
    D2ContentRejectionError,
    append_probe,
    reject_content,
    rejected_content_hashes,
)


SELECTION = "a" * 64
CONTENT = "b" * 64
TEMPLATE = "c" * 64
SPEC = "d" * 64


def _db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    migrate_v54_to_v55(db)
    db.commit()
    db.execute("BEGIN")
    return db


def _receipt(*, classification: str = "CONTENT", observed: object = "fixture"):
    return {
        "version": "past-v3-d2-render-probe-v1",
        "classification": classification,
        "selection_hash": SELECTION,
        "candidate_id": "candidate-1",
        "content_hash": CONTENT,
        "content_snapshot_hash": CONTENT,
        "canonical_template": True,
        "template_hash": TEMPLATE,
        "rendering_spec_hash": SPEC,
        "repeat_requirement": 2,
        "reproduced_failure_count": 2,
        "candidate_attempts": [
            {"attempt": 1, "outcome": "failed", "reason_code": "TEX_COMPILE_FAILED"},
            {"attempt": 2, "outcome": "failed", "reason_code": "TEX_COMPILE_FAILED"},
        ],
        "control": {"outcome": "succeeded", "reason_code": "ok"},
        "control_succeeded": True,
        "environment": {"observed": observed, "reason_codes": []},
        "disposition": "reject_task_local_content",
    }


def _append(db, receipt=None, *, job_id="job-1", attempt=2):
    return append_probe(
        db,
        job_id=job_id,
        selection_hash=SELECTION,
        candidate_id="candidate-1",
        content_hash=CONTENT,
        attempt=attempt,
        receipt=receipt or _receipt(),
        created_at="2026-09-06T00:00:00+00:00",
    )


def test_schema55_registered_initialization_and_v54_upgrade_preserve_business_rows(
    tmp_path,
):
    assert MIGRATIONS[54] == (55, migrate_v54_to_v55)
    path = tmp_path / "schema55-fixture.db"
    app = Flask(__name__)
    app.config["DATABASE"] = str(path)
    with app.app_context():
        database.init_db()

    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA user_version").fetchone()[0] == database.DB_SCHEMA_VERSION
        assert schema_issues(db) == []
        assert migration_schema_issues(db, database.DB_SCHEMA_VERSION) == []
        for table in (PROBE_RECEIPTS, CONTENT_REJECTIONS):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0

    old_path = tmp_path / "schema54.db"
    create_historical_database(old_path, 54)
    with sqlite3.connect(old_path) as db:
        db.row_factory = sqlite3.Row
        db.execute(
            "INSERT INTO subjects(name,subject_code,display_order) VALUES(?,?,?)",
            ("迁移保留科目", "migration-marker", 91),
        )
        before = tuple(
            db.execute(
                "SELECT id,name,subject_code,display_order FROM subjects "
                "WHERE subject_code='migration-marker'"
            ).fetchone()
        )
        db.commit()

        assert database._run_migration_step(db, 54) == 55
        assert db.execute("PRAGMA user_version").fetchone()[0] == 55
        assert schema_issues(db) == []
        assert migration_schema_issues(db, 55) == []
        after = tuple(
            db.execute(
                "SELECT id,name,subject_code,display_order FROM subjects "
                "WHERE subject_code='migration-marker'"
            ).fetchone()
        )
        assert after == before


def test_content_probe_creates_task_local_ledger_entry_idempotently():
    db = _db()
    probe_id = _append(db)
    assert _append(db) == probe_id
    entry = reject_content(db, probe_id=probe_id, created_at="t1")
    assert reject_content(db, probe_id=probe_id, created_at="different") == entry
    assert entry["classification"] == "CONTENT"
    assert entry["probe_id"] == probe_id
    assert rejected_content_hashes(db, "job-1") == {CONTENT}
    assert db.execute(f"SELECT COUNT(*) FROM {PROBE_RECEIPTS}").fetchone()[0] == 1
    assert db.execute(f"SELECT COUNT(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 1
    assert schema_issues(db) == []


@pytest.mark.parametrize("classification", ["UNKNOWN", "TEMPLATE_OR_ENVIRONMENT"])
def test_unknown_or_non_content_probe_never_rejects(classification):
    db = _db()
    probe_id = _append(db, _receipt(classification=classification))
    with pytest.raises(D2ContentRejectionError, match="probe_not_content"):
        reject_content(db, probe_id=probe_id)
    assert rejected_content_hashes(db, "job-1") == set()


def test_environment_is_a_fact_label_not_a_production_authorization_gate():
    db = _db()
    probe_id = _append(db, _receipt(observed="fixture"))
    reject_content(db, probe_id=probe_id)
    assert rejected_content_hashes(db, "job-1") == {CONTENT}


def test_different_content_failure_codes_cannot_create_rejection():
    db = _db()
    receipt = _receipt()
    receipt["candidate_attempts"][1]["reason_code"] = "TEX_UNDEFINED_COMMAND"
    probe_id = _append(db, receipt)
    with pytest.raises(D2ContentRejectionError, match="probe_not_reproducible_content"):
        reject_content(db, probe_id=probe_id)
    assert rejected_content_hashes(db, "job-1") == set()


def test_control_and_two_distinct_reproductions_are_required():
    mutations = []
    no_control = _receipt()
    no_control["control_succeeded"] = False
    mutations.append(no_control)
    one_failure = _receipt()
    one_failure["reproduced_failure_count"] = 1
    one_failure["candidate_attempts"] = one_failure["candidate_attempts"][:1]
    mutations.append(one_failure)
    duplicate_attempt = _receipt()
    duplicate_attempt["candidate_attempts"][1]["attempt"] = 1
    mutations.append(duplicate_attempt)
    noncanonical = _receipt()
    noncanonical["canonical_template"] = False
    mutations.append(noncanonical)
    snapshot_drift = _receipt()
    snapshot_drift["content_snapshot_hash"] = "e" * 64
    mutations.append(snapshot_drift)
    environment_failure = _receipt()
    environment_failure["environment"]["reason_codes"] = ["TEX_TIMEOUT"]
    mutations.append(environment_failure)
    unstable_failure = _receipt()
    unstable_failure["candidate_attempts"][0]["reason_code"] = "TEX_TIMEOUT"
    mutations.append(unstable_failure)

    for receipt in mutations:
        db = _db()
        probe_id = _append(db, receipt)
        with pytest.raises(D2ContentRejectionError, match="probe_not_reproducible_content"):
            reject_content(db, probe_id=probe_id)
        assert rejected_content_hashes(db, "job-1") == set()


def test_same_content_isolated_by_job_and_does_not_touch_historical_exclusions():
    db = _db()
    db.execute("CREATE TABLE historical_false_exclusions(content_hash TEXT PRIMARY KEY)")
    db.execute("INSERT INTO historical_false_exclusions VALUES(?)", ("e" * 64,))
    first = _append(db, job_id="job-1")
    second = _append(db, job_id="job-2")
    reject_content(db, probe_id=first)
    assert rejected_content_hashes(db, "job-1") == {CONTENT}
    assert rejected_content_hashes(db, "job-2") == set()
    reject_content(db, probe_id=second)
    assert rejected_content_hashes(db, "job-2") == {CONTENT}
    assert db.execute("SELECT content_hash FROM historical_false_exclusions").fetchone()[0] == "e" * 64


def test_caller_owns_transaction_and_can_roll_back_probe_and_ledger():
    db = _db()
    probe_id = _append(db)
    reject_content(db, probe_id=probe_id)
    db.rollback()
    assert db.execute(f"SELECT COUNT(*) FROM {PROBE_RECEIPTS}").fetchone()[0] == 0
    assert db.execute(f"SELECT COUNT(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 0


def test_writes_require_an_explicit_caller_owned_transaction():
    db = _db()
    db.rollback()
    with pytest.raises(D2ContentRejectionError, match="caller_transaction_required"):
        _append(db)


def test_equivalent_json_order_is_idempotent_but_changed_receipt_conflicts():
    db = _db()
    receipt = _receipt()
    probe_id = _append(db, dict(reversed(list(receipt.items()))))
    assert _append(db, receipt) == probe_id
    changed = _receipt()
    changed["environment"] = {"reason_codes": ["changed"], "observed": True}
    with pytest.raises(D2ContentRejectionError, match="probe_identity_conflict"):
        _append(db, changed)
