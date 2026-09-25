from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cuoti_app.migrations.v52_exam_generation_soft_inputs import migrate_v51_to_v52
from cuoti_app.migrations.v53_exam_generation_priority_once import (
    PRIORITY_ONCE_CONSUMPTIONS,
    migrate_v52_to_v53,
)
from cuoti_app.services.exam_generation_d2_publication import (
    D2_TABLE,
    D2PublicationError,
    run_d2_quality_and_publish,
)
from cuoti_app.services.exam_generation_mistake_relations import (
    freeze_mistake_relations,
    relation_content_fingerprint,
)
from cuoti_app.services.exam_generation_priority_once import (
    PriorityOnceConflictError,
    PriorityOnceKey,
)
from cuoti_app.services.exam_generation_priority_runtime import (
    load_priority_claims,
    persist_priority_claims,
)
from cuoti_app.transactions import immediate_transaction
from cuoti_app.services.job_contracts import JobCancelled
from tests.test_exam_d2_publication import _artifact, _db, _fixture, _quality


def _install_priority_schema(db) -> None:
    migrate_v51_to_v52(db)
    migrate_v52_to_v53(db)
    db.execute(
        "CREATE TABLE mistakes("
        "public_id TEXT,content TEXT,subject_id INTEGER,deleted_at TEXT)"
    )
    db.commit()


def _priority_state_and_claim(db, *, job_id: str):
    state, facts, contract = _fixture()
    assignment = state.selection.assignments[0]
    identity = [
        assignment["candidate_id"],
        assignment["candidate_fact_hash"],
        assignment["content_hash"],
    ]
    selection_payload = state.selection.to_dict()
    selection_payload["m_once_eligibility"] = {
        "status": "supplied",
        "identities": [identity],
    }
    selection = SimpleNamespace(
        selection_hash=state.selection.selection_hash,
        objective_hash=state.selection.objective_hash,
        objective_vector=state.selection.objective_vector,
        assignments=state.selection.assignments,
        to_dict=lambda: selection_payload,
    )
    state = replace(state, selection=selection)

    content = json.loads(facts.candidates[0]["content_snapshot"])["content"]
    # Both jobs deliberately point at the same immutable lineage identity.
    public_id = "mistake-shared"
    if db.execute("SELECT 1 FROM mistakes WHERE public_id=?", (public_id,)).fetchone() is None:
        db.execute("INSERT INTO mistakes VALUES(?,?,1,NULL)", (public_id, content))
        db.commit()
    freeze_mistake_relations(db, job_id=job_id, subject_id=1)
    key = PriorityOnceKey(
        subject_id=1,
        mode="past",
        candidate_content_hash=assignment["content_hash"],
        source_mistake_public_id=public_id,
        relation_content_fingerprint=relation_content_fingerprint(content),
    )
    with immediate_transaction(db):
        persist_priority_claims(
            db,
            job_id=job_id,
            selection_hash=state.selection.selection_hash,
            keys=(key,),
        )
    return state, facts, contract, (key,)


def _publish(db, *, state, facts, contract, job_id: str, artifact_dir: Path,
             artifact_callback, quality_callback, receipt_callback=lambda _receipt: None):
    return run_d2_quality_and_publish(
        db,
        state=state,
        facts=facts,
        stage3_contract=contract,
        job_id=job_id,
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="D2 priority",
        artifact_dir=str(artifact_dir),
        artifact_callback=artifact_callback,
        quality_callback=quality_callback,
        receipt_callback=receipt_callback,
        priority_claims=load_priority_claims(db, job_id=job_id, state=state),
    )


def test_native_success_consumes_once_and_recovery_runs_no_callbacks(tmp_path):
    db = _db()
    _install_priority_schema(db)
    state, facts, contract, keys = _priority_state_and_claim(db, job_id="job")
    calls = {"quality": 0, "artifact": 0}

    def quality(raw):
        calls["quality"] += 1
        return _quality()(raw)

    artifact = _artifact([])

    def render(request):
        calls["artifact"] += 1
        return artifact(request)

    first = _publish(
        db, state=state, facts=facts, contract=contract, job_id="job",
        artifact_dir=tmp_path, artifact_callback=render,
        quality_callback=quality, receipt_callback=None,
    )
    assert first.state == "published_pending_receipt"
    row = db.execute(f"SELECT * FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()
    assert row["job_id"] == "job" and row["exam_id"] == first.exam_id
    assert row["publication_hash"] == first.publication_hash
    assert row["candidate_content_hash"] == keys[0].candidate_content_hash

    recovered = _publish(
        db, state=state, facts=facts, contract=contract, job_id="job",
        artifact_dir=tmp_path,
        artifact_callback=lambda _request: pytest.fail("artifact repeated"),
        quality_callback=lambda _raw: pytest.fail("quality repeated"),
    )
    assert recovered.state == "completed" and recovered.recovered is True
    assert calls == {"quality": 1, "artifact": 1}
    assert db.execute(f"SELECT COUNT(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 1


def test_quality_rejection_and_artifact_failure_do_not_consume(tmp_path):
    rejected_db = _db()
    _install_priority_schema(rejected_db)
    state, facts, contract, _keys = _priority_state_and_claim(rejected_db, job_id="job")
    with pytest.raises(D2PublicationError) as rejected:
        _publish(
            rejected_db, state=state, facts=facts, contract=contract, job_id="job",
            artifact_dir=tmp_path / "rejected", artifact_callback=_artifact([]),
            quality_callback=_quality(False),
        )
    assert rejected.value.reason == "quality_attribution_unknown"
    assert rejected_db.execute(f"SELECT COUNT(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 0
    assert rejected_db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0

    failed_db = _db()
    _install_priority_schema(failed_db)
    state, facts, contract, _keys = _priority_state_and_claim(failed_db, job_id="job")

    def fail_artifact(_request):
        raise RuntimeError("fixture artifact failure")

    with pytest.raises(RuntimeError, match="fixture artifact failure"):
        _publish(
            failed_db, state=state, facts=facts, contract=contract, job_id="job",
            artifact_dir=tmp_path / "artifact-failed", artifact_callback=fail_artifact,
            quality_callback=_quality(),
        )
    assert failed_db.execute(f"SELECT COUNT(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 0
    assert failed_db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0


def test_two_native_publishers_race_and_only_the_committed_job_consumes(tmp_path):
    template = _db()
    _install_priority_schema(template)
    database = tmp_path / "priority-publication.db"
    first = sqlite3.connect(database, timeout=5)
    first.row_factory = sqlite3.Row
    template.backup(first)
    template.close()
    first.execute("PRAGMA foreign_keys=ON")
    first.execute(
        "INSERT INTO background_jobs VALUES("
        "'job-2','exam-generation','running',0,'worker',1,NULL,NULL)"
    )
    first.execute("INSERT INTO exam_generation_attempt_bindings VALUES('job-2')")
    first.commit()
    second = sqlite3.connect(database, timeout=5)
    second.row_factory = sqlite3.Row
    second.execute("PRAGMA foreign_keys=ON")
    try:
        state1, facts1, contract1, keys1 = _priority_state_and_claim(first, job_id="job")
        state2, facts2, contract2, keys2 = _priority_state_and_claim(second, job_id="job-2")
        assert keys1 == keys2
        winner = {}
        render_first = _artifact([])

        def publish_second_before_first_commit(request):
            winner["outcome"] = _publish(
                second,
                state=state2,
                facts=facts2,
                contract=contract2,
                job_id="job-2",
                artifact_dir=tmp_path / "winner",
                artifact_callback=_artifact([]),
                quality_callback=_quality(),
            )
            return render_first(request)

        with pytest.raises(PriorityOnceConflictError) as conflict:
            _publish(
                first,
                state=state1,
                facts=facts1,
                contract=contract1,
                job_id="job",
                artifact_dir=tmp_path / "loser",
                artifact_callback=publish_second_before_first_commit,
                quality_callback=_quality(),
            )
        assert conflict.value.keys == keys1
        assert winner["outcome"].state == "completed"

        consumption = first.execute(
            f"SELECT job_id,exam_id,publication_hash FROM {PRIORITY_ONCE_CONSUMPTIONS}"
        ).fetchall()
        assert len(consumption) == 1
        assert consumption[0]["job_id"] == "job-2"
        exams = first.execute(
            "SELECT generation_job_id,artifact_status FROM exams ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in exams] == [("job-2", "published")]
        assert first.execute("SELECT COUNT(*) FROM job_business_results").fetchone()[0] == 1
        loser = first.execute(f"SELECT state FROM {D2_TABLE} WHERE job_id='job'").fetchone()
        assert loser["state"] == "request_frozen"
    finally:
        first.close()
        second.close()


def test_cancelled_artifact_never_consumes_priority(tmp_path):
    db = _db()
    _install_priority_schema(db)
    state, facts, contract, _keys = _priority_state_and_claim(db, job_id="job")
    artifact = _artifact([])
    def cancel(request):
        db.execute("UPDATE background_jobs SET cancel_requested=1 WHERE id='job'")
        db.commit()
        return artifact(request)
    with pytest.raises(JobCancelled):
        _publish(db, state=state, facts=facts, contract=contract, job_id="job",
                 artifact_dir=tmp_path, artifact_callback=cancel, quality_callback=_quality())
    assert db.execute(f"SELECT count(*) FROM {PRIORITY_ONCE_CONSUMPTIONS}").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0
