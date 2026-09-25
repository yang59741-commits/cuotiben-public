from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from cuoti_app.services.exam_generation_d2_publication import (
    D2_TABLE,
    D2PublicationError,
    SCHEMA_STATEMENTS,
    published_d2_usage,
    run_d2_quality_and_publish,
)
from cuoti_app.services.exam_generation_past_v3_integration import (
    PAST_V3_PUBLICATION_RECEIPT_EXTENSION, make_published_recovery_dispatcher,
)
from cuoti_app.services.exam_generation_stage3_contract import PastV3GenerationContract
from cuoti_app.services.exam_generation_stage4_candidate_facts import FrozenCandidateFacts
from cuoti_app.services.exam_generation_stage6_d2_repository import D2CandidateState
from cuoti_app.services.exam_generation_v3_publish import ARTIFACT_VERSION
from cuoti_app.services.job_contracts import JobLeaseLost
from cuoti_app.services.semantic_index import semantic_content_hash
from cuoti_app.migrations.v57_question_review_markers import migrate_v56_to_v57


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _fixture(content="设 $x+1=2$，求 $x$。", source_kind='ordinary'):
    snapshot = {
        "schema_version": "semantic-candidate-content-snapshot-v1",
        "content": content,
        "figure_references": [], "options": [], "conditions": [],
    }
    content_hash = semantic_content_hash(snapshot["content"], figure_references=(), options=(), conditions=())
    candidate = {
        "source_kind": "ordinary", "candidate_id": "candidate-1", "subject": "数学",
        "question_type_family": "解答题", "chapter": "代数", "source_exam_id": None,
        "section_id": None, "source_institution": None,
        "content_snapshot": json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        "content_snapshot_hash": "1" * 64, "content_hash": content_hash,
        "card_version_id": 1, "card_hash": "2" * 64, "projection_document_hash": "3" * 64,
        "difficulty_fact": {"version": "past-v3-stage4-difficulty-fact-v1", "resolved": True, "label": "中等", "confidence": 0.9, "reason": "resolved"},
    }
    if source_kind == 'past_exam_local':
        candidate.update(source_kind=source_kind, chapter=None, source_exam_id='local-exam',
                         section_id='local-section', source_institution='湖南师范大学')
    facts_payload = {
        "contract_version": "past-v3-stage4-candidate-facts-v1",
        "projection_identity": {"id": "projection"}, "projection_identity_hash": "4" * 64,
        "target_institution": "目标院校", "candidates": [candidate], "unresolved_candidate_ids": [],
    }
    facts_hash = _digest(facts_payload)
    facts = FrozenCandidateFacts(facts_payload["contract_version"], "4" * 64, facts_hash, "目标院校", (candidate,), (), json.dumps(facts_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    scoring = {"mode": "unscored", "total_points": None, "evidence_hash": None, "sections": [], "slots": []}
    stage3_payload = {
        "contract_version": "past-v3-generation-contract-v1", "generation_request_hash": "5" * 64,
        "binding_hash": "6" * 64, "resolution_hash": "7" * 64, "profile_hash": "8" * 64,
        "exact_slot_hash": "9" * 64, "question_count": 1, "scoring": scoring,
        "slot_demands": [{"position": 1}], "sections": [], "candidate_source": "ordinary",
        "source_quotas": {"ordinary": 1, "past_exam_other": 0},
    }
    if source_kind == 'past_exam_local':
        stage3_payload.update(candidate_source='other_past', source_quotas={'ordinary': 0, 'past_exam_other': 1})
    stage3_hash = _digest(stage3_payload)
    contract = PastV3GenerationContract(
        stage3_payload["contract_version"], "5" * 64, "6" * 64, "7" * 64, "8" * 64, "9" * 64,
        1, "unscored", _digest({"slot_demands": stage3_payload["slot_demands"]}), _digest(scoring),
        _digest({"candidate_source": stage3_payload['candidate_source'], "source_quotas": stage3_payload["source_quotas"]}),
        stage3_hash, json.dumps(stage3_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    assignment = {
        "slot_id": "slot-1", "position": 1, "candidate_id": "candidate-1",
        "candidate_fact_hash": _digest(candidate), "content_hash": content_hash,
        "source_kind": "ordinary", "chapter": "代数", "source_exam_id": None,
        "section_id": None, "question_type_family": "解答题", "target_difficulty": "中等",
        "bucket_rank": 1, "factor_levels": {},
    }
    if source_kind == 'past_exam_local':
        assignment.update(source_kind='past_exam_other', chapter=None,
                          source_exam_id='local-exam', section_id='local-section')
    selection_hash, objective_hash = "a" * 64, "b" * 64
    selection_payload = {"selection_hash": selection_hash, "objective_hash": objective_hash, "graph_hash": "d" * 64, "constraint_hash": "c" * 64, "policy_hash": "e" * 64, "assignments": [assignment]}
    selection = SimpleNamespace(selection_hash=selection_hash, objective_hash=objective_hash, objective_vector=(1, 2), assignments=(assignment,), to_dict=lambda: selection_payload)
    constraint = SimpleNamespace(constraint_hash="c" * 64, candidate_facts_hash=facts_hash, stage3_contract_hash=stage3_hash)
    graph = SimpleNamespace(graph_hash="d" * 64)
    base = SimpleNamespace(constraint=constraint, graph=graph)
    bindings = {"selection_hash": selection_hash, "graph_hash": "d" * 64, "constraint_hash": "c" * 64, "candidate_facts_hash": facts_hash, "objective_policy_hash": "e" * 64, "factor_set_hash": "f" * 64}
    checkpoint = SimpleNamespace(checkpoint_hash="0" * 64, to_dict=lambda: {"bindings": bindings})
    factor_set = {"factor_set_hash": "f" * 64, "graph_hash": "d" * 64, "constraint_hash": "c" * 64, "candidate_facts_hash": facts_hash, "policy_hash": "e" * 64}
    state = D2CandidateState(base, {"policy_hash": "e" * 64}, (), factor_set, selection, checkpoint, {"state": "checkpointed", "checkpoint_hash": "0" * 64, "selection_hash": selection_hash, "objective_policy_hash": "e" * 64, "factor_set_hash": "f" * 64})
    return state, facts, contract


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript("""
    CREATE TABLE background_jobs(id TEXT PRIMARY KEY,kind TEXT,status TEXT,cancel_requested INTEGER,lease_owner TEXT,attempts INTEGER,result_json TEXT,updated_at TEXT);
    CREATE TABLE exams(id INTEGER PRIMARY KEY AUTOINCREMENT,subject_id INTEGER,title TEXT,config TEXT,content TEXT,created_at TEXT,generation_job_id TEXT,batch_index INTEGER,artifact_status TEXT,latex_source TEXT,latex_sha256 TEXT,pdf_sha256 TEXT,artifact_revision INTEGER,recompile_available INTEGER,scoring_mode TEXT,total_points TEXT,scoring_evidence_hash TEXT,scoring_projection_hash TEXT);
    CREATE TABLE exam_items(id INTEGER PRIMARY KEY AUTOINCREMENT,public_id TEXT UNIQUE,exam_id INTEGER,position INTEGER,question_type TEXT,difficulty TEXT,content TEXT,content_hash TEXT,points REAL,points_canonical TEXT,points_evidence_hash TEXT,source_kind TEXT,source_mistake_public_id TEXT,source_candidate_id TEXT,source_metadata_json TEXT,created_at TEXT,UNIQUE(exam_id,position));
    CREATE TABLE job_exam_results(job_id TEXT,exam_id INTEGER,position INTEGER,PRIMARY KEY(job_id,exam_id));
    CREATE TABLE job_business_results(job_id TEXT PRIMARY KEY,kind TEXT,result_json TEXT,committed_at TEXT);
    CREATE TABLE exam_generation_attempt_bindings(job_id TEXT PRIMARY KEY);
    """)
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    db.execute("INSERT INTO background_jobs VALUES('job','exam-generation','running',0,'worker',1,NULL,NULL)")
    db.execute("INSERT INTO exam_generation_attempt_bindings VALUES('job')")
    db.commit()
    return db


def _quality(passed=True):
    def callback(raw):
        request = json.loads(raw)
        return json.dumps({"version": request["version"], "selection_hash": request["selection_hash"], "pre_adaptation_hash": request["pre_adaptation_hash"], "results": [{"position": 1, "passed": passed, "reason_codes": ["none" if passed else "rendering_risk"]}]})
    return callback


def _artifact(calls):
    def callback(request):
        calls.append(request["request_hash"])
        Path(request["pdf_path"]).write_bytes(b"%PDF-1.4\nD2\n")
        return {"version": ARTIFACT_VERSION, "request_hash": request["request_hash"], "exam_id": request["exam_id"]}
    return callback


def test_d2_quality_passes_and_native_publication_completes(tmp_path):
    state, facts, contract = _fixture()
    db, artifacts = _db(), []
    outcome = run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact(artifacts), quality_callback=_quality(), receipt_callback=lambda _: None)
    assert outcome.state == "completed" and outcome.exam_id is not None
    assert len(artifacts) == 1
    row = db.execute(f"SELECT state,selection_hash,objective_hash,quality_response_json FROM {D2_TABLE}").fetchone()
    assert row["state"] == "completed" and row["selection_hash"] == state.selection.selection_hash
    assert json.loads(row["quality_response_json"])["results"][0]["passed"] is True


@pytest.mark.parametrize('source_kind', ['ordinary', 'past_exam_local'])
def test_d2_publishes_review_marker_without_changing_selection(tmp_path, source_kind):
    state, facts, contract = _fixture(source_kind=source_kind)
    db, artifacts = _db(), []
    migrate_v56_to_v57(db)
    db.execute(
        """INSERT INTO question_review_markers
        (source_kind,candidate_id,marker_kind,marker_scope,message,
         source_sequence,active,created_at,updated_at)
        VALUES(?,'candidate-1','uncertain','logic','题面尚未确认',1,1,'t','t')""", (source_kind,)
    )
    db.commit()
    outcome = run_d2_quality_and_publish(
        db, state=state, facts=facts, stage3_contract=contract,
        job_id="job", worker_id="worker", lease_attempt=1, subject_id=1,
        title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact(artifacts),
        quality_callback=_quality(), receipt_callback=lambda _: None,
    )
    assert outcome.exam_id is not None
    assert db.execute('SELECT source_kind FROM exam_items WHERE exam_id=?', (outcome.exam_id,)).fetchone()[0] == source_kind
    item = db.execute(
        "SELECT source_metadata_json FROM exam_items WHERE exam_id=?",
        (outcome.exam_id,),
    ).fetchone()
    metadata = json.loads(item["source_metadata_json"])
    assert metadata["review_marker"]["kind"] == "uncertain"
    assert metadata["review_marker"]["message"] == "题面尚未确认"
    exam = db.execute(
        "SELECT content,latex_source FROM exams WHERE id=?", (outcome.exam_id,)
    ).fetchone()
    assert "题面提示：不确定题" in exam["content"]
    assert r"\textbf{题面提示：不确定题}" in exam["latex_source"]
    assert "题面提示：题面提示" not in exam["content"]
    authority = db.execute(
        f"SELECT selection_hash FROM {D2_TABLE} WHERE job_id='job'"
    ).fetchone()
    assert authority["selection_hash"] == state.selection.selection_hash
    recovered = run_d2_quality_and_publish(
        db, state=state, facts=facts, stage3_contract=contract,
        job_id='job', worker_id='worker', lease_attempt=1, subject_id=1, title='D2',
        artifact_dir=str(tmp_path), artifact_callback=lambda *args: pytest.fail('artifact replayed'),
        quality_callback=lambda *args: pytest.fail('quality replayed'), receipt_callback=lambda _: None)
    assert recovered.exam_id == outcome.exam_id
    assert db.execute('SELECT count(*) FROM exam_items WHERE exam_id=?', (outcome.exam_id,)).fetchone()[0] == 1


def test_d2_layout_cleanup_changes_only_the_rendered_latex(tmp_path):
    canonical = "【例 5．17】设 $x=1$。（1）求 $x$；（2）验证。"
    state, facts, contract = _fixture(canonical)
    db = _db()
    outcome = run_d2_quality_and_publish(
        db, state=state, facts=facts, stage3_contract=contract, job_id="job",
        worker_id="worker", lease_attempt=1, subject_id=1, title="D2",
        artifact_dir=str(tmp_path), artifact_callback=_artifact([]),
        quality_callback=_quality(), receipt_callback=lambda _: None,
    )

    exam = db.execute(
        "SELECT latex_source FROM exams WHERE id=?", (outcome.exam_id,)
    ).fetchone()
    item = db.execute(
        "SELECT content,content_hash FROM exam_items WHERE exam_id=?", (outcome.exam_id,)
    ).fetchone()
    assert "【例 5．17】" not in exam["latex_source"]
    assert exam["latex_source"].count(r"\par\smallskip\noindent") == 2
    assert item["content"] == canonical
    assert item["content_hash"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_d2_quality_failure_publishes_nothing(tmp_path):
    state, facts, contract = _fixture()
    db, artifacts = _db(), []
    with pytest.raises(D2PublicationError, match="尚无本地复现证据"):
        run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact(artifacts), quality_callback=_quality(False))
    assert artifacts == []
    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0
    assert db.execute(f"SELECT state FROM {D2_TABLE}").fetchone()[0] == "quality_pending"


def test_published_recovery_skips_quality_and_artifact(tmp_path):
    state, facts, contract = _fixture()
    db, artifacts = _db(), []
    first = run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact(artifacts), quality_callback=_quality(), receipt_callback=None)
    assert first.state == "published_pending_receipt"
    second = run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=lambda _: pytest.fail("artifact repeated"), quality_callback=lambda _: pytest.fail("quality repeated"), receipt_callback=lambda _: None)
    assert second.state == "completed" and second.recovered is True
    assert len(artifacts) == 1
    usage = published_d2_usage(db, subject_id=1)
    assert len(usage) == 1
    assert usage[0] == {
        "job_id": "job", "position": 1, "candidate_id": "candidate-1",
        "content_hash": state.selection.assignments[0]["content_hash"],
        "usage_hash": usage[0]["usage_hash"], "created_at": usage[0]["created_at"],
    }
    assert published_d2_usage(db, subject_id=1) == usage


@pytest.mark.parametrize("receipt_registered", [False, True])
def test_application_dispatch_recovers_d2_before_dependency_resolution(tmp_path, receipt_registered):
    state, facts, contract = _fixture()
    db, artifacts = _db(), []
    run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact(artifacts), quality_callback=_quality(), receipt_callback=None)

    class Logger:
        def exception(self, *_args, **_kwargs):
            pytest.fail("recovery unexpectedly failed")

    from cuoti_app.services.exam_generation_past_v3_production import acknowledge_publication_receipt

    app = SimpleNamespace(extensions={
        PAST_V3_PUBLICATION_RECEIPT_EXTENSION: acknowledge_publication_receipt
    }) if receipt_registered else None
    dispatch = make_published_recovery_dispatcher(worker_id="worker", lease_attempt=1, app=app)
    response = dispatch(db, {"PDF_DIR": str(tmp_path)}, "job", logger=Logger())
    assert response.status == (200 if receipt_registered else 202)
    assert response.body["idempotency_replayed"] is True
    assert response.body["hashes"]["publication_hash"]
    assert len(artifacts) == 1


def test_quality_lifecycle_interrupt_stays_pending_instead_of_model_failure(tmp_path):
    state, facts, contract = _fixture()
    db = _db()

    def interrupted(_request):
        raise JobLeaseLost("lease moved during quality")

    with pytest.raises(JobLeaseLost):
        run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact([]), quality_callback=interrupted)
    row = db.execute(f"SELECT state,failure_reason FROM {D2_TABLE}").fetchone()
    assert tuple(row) == ("quality_pending", "quality_call_started")
    with pytest.raises(D2PublicationError) as caught:
        run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact([]), quality_callback=lambda _: pytest.fail("unknown model call must not be replayed"))
    assert caught.value.reason == "quality_call_outcome_unknown"


def test_quality_result_write_requires_current_actor_fence(tmp_path):
    state, facts, contract = _fixture()
    db = _db()

    def loses_lease(raw):
        response = _quality()(raw)
        db.execute("UPDATE background_jobs SET lease_owner='replacement' WHERE id='job'")
        db.commit()
        return response

    with pytest.raises(JobLeaseLost):
        run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact([]), quality_callback=loses_lease)
    assert db.execute(f"SELECT state FROM {D2_TABLE}").fetchone()[0] == "quality_pending"
    with pytest.raises(D2PublicationError) as caught:
        run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract, job_id="job", worker_id="replacement", lease_attempt=1, subject_id=1, title="D2", artifact_dir=str(tmp_path), artifact_callback=_artifact([]), quality_callback=lambda _: pytest.fail("returned but unpersisted model result must not be replayed"))
    assert caught.value.reason == "quality_call_outcome_unknown"


def test_authentication_failure_is_clear_terminal_and_not_replayed(tmp_path):
    from cuoti_app.services.ai import DeepSeekServiceError

    state, facts, contract = _fixture()
    db, calls = _db(), []

    def rejected(_request):
        calls.append(1)
        raise DeepSeekServiceError("authentication failed", code="AUTHENTICATION", retryable=False)

    for _ in range(2):
        with pytest.raises(D2PublicationError) as failure:
            run_d2_quality_and_publish(db, state=state, facts=facts, stage3_contract=contract,
                job_id="job", worker_id="worker", lease_attempt=1, subject_id=1, title="D2",
                artifact_dir=str(tmp_path), artifact_callback=_artifact([]), quality_callback=rejected)
        assert failure.value.reason == "quality_provider_authentication"
        assert failure.value.status_code == 502
        assert failure.value.retryable is False
    assert len(calls) == 1
    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 0
