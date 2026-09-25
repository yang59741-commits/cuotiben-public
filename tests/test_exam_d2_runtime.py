import sqlite3
import pytest

from cuoti_app.services.exam_generation_request import (
    D2_SCORING_PROTOCOL, attach_current_scoring_protocol, freeze_generation_request,
)
from cuoti_app.services.exam_generation_d2_runtime import local_factors, release_objective_policy
from tests.test_exam_generation_stage6_d2_repository import _case


def test_new_past_protocol_is_stamped_without_changing_history_or_other_modes():
    original = {"source": "past", "allow_adaptation": False}
    stamped = attach_current_scoring_protocol(original)
    assert stamped["stage6_scoring_protocol"] == D2_SCORING_PROTOCOL
    assert "stage6_scoring_protocol" not in original
    assert freeze_generation_request(stamped, generation_job_id="job", request_id=None).stage6_scoring_protocol == D2_SCORING_PROTOCOL
    assert freeze_generation_request(original, generation_job_id="old", request_id=None).stage6_scoring_protocol is None
    explicit = {**original, "stage6_scoring_protocol": "legacy_score_read_only_v2"}
    assert attach_current_scoring_protocol(explicit) == explicit
    assert attach_current_scoring_protocol({"source": "free"}) == {"source": "free"}


def test_local_policy_is_deterministic_and_does_not_invent_mistake_evidence():
    base, *_ = _case()
    policy = release_objective_policy()
    assert policy == release_objective_policy()
    factors = local_factors(base, policy)
    assert factors[0]["factors"]["verified_mistake_relevance"]["level"] == "not_applicable"
    assert factors[0]["factors"]["history_exposure_novelty"]["level"] == "not_applicable"


def test_d2_migration_is_additive():
    from cuoti_app.migrations.v51_exam_generation_local_d2 import migrate_v50_to_v51, schema_issues
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE background_jobs(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE exams(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO exams(id) VALUES(7)")
        migrate_v50_to_v51(db)
        assert schema_issues(db) == []
        assert db.execute("SELECT id FROM exams").fetchall() == [(7,)]


def test_similarity_history_reads_only_successful_quality_positions():
    import json
    from cuoti_app.services.exam_generation_d2_runtime import _similarity_history

    request = {
        "subject_id": 2,
        "draft": {"structured_items": [
            {"position": 1, "candidate_id": "A", "content_hash": "a" * 64,
             "content": "题面 A", "source_kind": "ordinary", "question_type": "calculation"},
            {"position": 2, "candidate_id": "B", "content_hash": "b" * 64,
             "content": "题面 B", "source_kind": "ordinary", "question_type": "proof"},
        ]},
    }
    quality = {"results": [
        {"position": 1, "passed": True}, {"position": 2, "passed": False},
    ]}
    publication = {"published_at": "2026-09-11T00:00:00+00:00"}
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE exam_generation_d2_quality_publications("
                   "request_json TEXT, quality_response_json TEXT, publication_json TEXT, "
                   "state TEXT)")
        db.execute("INSERT INTO exam_generation_d2_quality_publications VALUES(?,?,?,?)",
                   (json.dumps(request), json.dumps(quality), json.dumps(publication), "completed"))
        rows = _similarity_history(db, 2)
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == "A"
    assert rows[0]["content_snapshot"]["content"] == "题面 A"


@pytest.mark.parametrize("publication_state,http_status", [("completed", 200), ("published_pending_receipt", 202)])
def test_d2_runtime_persists_local_selection_then_reuses_it(monkeypatch, publication_state, http_status):
    from types import SimpleNamespace
    from cuoti_app.services import exam_generation_d2_runtime as runtime
    from cuoti_app.services import exam_generation_d2_publication as publication
    from cuoti_app.services.exam_generation_stage6_d2_repository import install_d2_candidate_schema_draft
    from cuoti_app.services.exam_generation_v3_publish import NativeV3PublishOutcome
    from tests.test_exam_d2_publication import _db

    base, *_ = _case()
    db = _db()
    install_d2_candidate_schema_draft(db)
    from cuoti_app.migrations.v52_exam_generation_soft_inputs import migrate_v51_to_v52
    migrate_v51_to_v52(db)
    db.execute("CREATE TABLE mistakes(public_id TEXT,content TEXT,subject_id INTEGER,deleted_at TEXT)")
    db.commit()
    monkeypatch.setattr(runtime, "recover_stage5_state", lambda *_a, **_k: SimpleNamespace(contract=base.constraint, active_graph=base.graph))
    selections = []
    guarded_stages = []

    def publish(*_a, **kwargs):
        kwargs["lifecycle_guard"]({"stage": "before_quality", "job_id": "job"})
        selections.append(kwargs["state"].selection.selection_hash)
        return NativeV3PublishOutcome("job", publication_state, "a" * 64, 1, "b" * 64, "c" * 64)

    monkeypatch.setattr(publication, "run_d2_quality_and_publish", publish)
    stage5_calls = []

    def stage5(**_kwargs):
        stage5_calls.append(1)
        assert _kwargs['search_guard']()
        return SimpleNamespace(status="adapter_pending", graph_hash=base.graph.graph_hash)

    arguments = dict(
        job_id="job", worker_id="worker", lease_attempt=1,
        stage3_contract=SimpleNamespace(to_dict=lambda: base.stage3),
        facts_loader=lambda: SimpleNamespace(to_dict=lambda: base.stage4),
        stage5_runner=stage5, stage3_contract_loader=None, policy_fact_loader=None,
        classification_binding=None, verification_binding=None, classify_callback=None,
        difficulty_verify_callback=None, quality_callback=None, subject_id=1,
        title="D2", artifact_dir="unused", artifact_callback=None,
        lifecycle_guard=lambda *, stage: guarded_stages.append(stage),
    )
    first = runtime.run_d2_generation(db, **arguments)
    second = runtime.run_d2_generation(db, **arguments)
    assert first.status == second.status == publication_state
    assert first.status_code == second.status_code == http_status
    assert len(stage5_calls) == 1
    assert 'stage5_feasibility' in guarded_stages
    assert selections[0] == selections[1]
    assert guarded_stages.count("before_quality") == 2
    assert first.call_counts == second.call_counts == {}
    db.close()


def test_incomplete_stage5_with_persisted_graph_does_not_advance_to_selection(monkeypatch):
    from types import SimpleNamespace
    from cuoti_app.services import exam_generation_d2_runtime as runtime
    from cuoti_app.services.exam_generation_stage6_d2_repository import D2_HEADS
    from cuoti_app.services.exam_generation_stage5_worker import Stage5WorkerResult

    with sqlite3.connect(':memory:') as db:
        db.execute(f'CREATE TABLE {D2_HEADS}(job_id TEXT)')
        monkeypatch.setattr(runtime, 'freeze_mistake_relations', lambda *a, **k: {})
        def unexpected(*a, **k):
            pytest.fail('an incomplete Stage5 result must stop before D2 selection')
        monkeypatch.setattr(runtime, 'recover_stage5_state', unexpected)
        result = runtime.run_d2_generation(
            db, job_id='job', worker_id='worker', lease_attempt=1, stage3_contract=SimpleNamespace(),
            facts_loader=unexpected, stage5_runner=lambda **k: Stage5WorkerResult(
                'not_completed', 'exam_selection_incomplete', 'feasibility_search_incomplete', 409, 'a' * 64, 'b' * 64),
            stage3_contract_loader=None, policy_fact_loader=None, classification_binding=None,
            verification_binding=None, classify_callback=None, difficulty_verify_callback=None,
            quality_callback=unexpected, subject_id=1, title='test', artifact_dir='unused', artifact_callback=unexpected,
        )
        assert result.status == 'not_completed'
        assert result.call_counts == {}
        assert db.execute(f'SELECT COUNT(*) FROM {D2_HEADS}').fetchone()[0] == 0
