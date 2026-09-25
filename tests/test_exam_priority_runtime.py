import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from cuoti_app.services import exam_generation_d2_runtime as runtime
from cuoti_app.services.exam_generation_d2_publication import D2PublicationError
from cuoti_app.services.exam_generation_mistake_relations import freeze_mistake_relations
from cuoti_app.services.exam_generation_priority_runtime import RESELECTIONS, load_priority_claims
from cuoti_app.services.exam_generation_priority_once import consume_priority_once_in_transaction
from cuoti_app.services.exam_generation_stage5_candidate_base_runner import build_candidate_stage5_d2_base
from cuoti_app.services.exam_generation_stage6_d2_repository import install_d2_candidate_schema_draft
from cuoti_app.services.semantic_index import semantic_content_hash
from cuoti_app.transactions import immediate_transaction
from tests.test_exam_d2_publication import _fixture, _db, _quality, _artifact
from tests.test_exam_generation_stage6_d2_repository import _case, _digest
from tests.test_exam_priority_publication import _install_priority_schema


def full_case(*, first_content=None):
    _, original_facts, original_contract = _fixture()
    template, *_ = _case()
    stage3 = {**original_contract.to_dict(), **template.stage3}
    contract = replace(original_contract, profile_hash=stage3["profile_hash"],
        exact_slot_hash=stage3["exact_slot_hash"],
        slot_demands_hash=_digest({"slot_demands": stage3["slot_demands"]}),
        source_quotas_hash=_digest({"candidate_source": "ordinary", "source_quotas": stage3["source_quotas"]}),
        stage3_contract_hash=_digest(stage3), _payload_json=json.dumps(stage3))
    stage4 = original_facts.to_dict()
    candidates = []
    for i in range(1, 3):
        value = copy.deepcopy(stage4["candidates"][0])
        content = first_content if i == 1 and first_content is not None else f"求 $x+{i}=3$ 的解。"
        snapshot = json.loads(value["content_snapshot"])
        snapshot["content"] = content
        value.update(candidate_id=f"candidate-{i}", chapter="第一章", question_type_family="single",
            content_hash=semantic_content_hash(content, figure_references=(), options=(), conditions=()),
            content_snapshot=json.dumps(snapshot), content_snapshot_hash=_digest(snapshot))
        candidates.append(value)
    stage4["candidates"] = candidates
    facts = replace(original_facts, candidates=tuple(candidates), candidate_facts_hash=_digest(stage4),
                    _payload_json=json.dumps(stage4))
    constraint = template.constraint.to_dict()
    constraint["chapter_weight_fact"]["fact_hash"] = _digest(constraint["chapter_weight_fact"])
    base = build_candidate_stage5_d2_base(
        stage3, stage4, stage3_contract_hash=contract.stage3_contract_hash,
        candidate_facts_hash=facts.candidate_facts_hash, projection_identity_hash=facts.projection_identity_hash,
        **{key: constraint[key] for key in ("chapter_weight_fact", "recent_usage", "diversity_policy",
                                           "coverage_policy", "uniqueness_policy", "policy_fact_set_hash")})
    return base, facts, contract


def setup_runtime(monkeypatch, tmp_path, *, first_content=None):
    base, facts, contract = full_case(first_content=first_content)
    db = _db()
    _install_priority_schema(db)
    install_d2_candidate_schema_draft(db)
    for index, candidate in enumerate(facts.candidates):
        db.execute("INSERT INTO mistakes VALUES(?,?,1,NULL)",
            (f"mistake-{index}", json.loads(candidate["content_snapshot"])["content"]))
    db.commit()
    monkeypatch.setattr(runtime, "recover_stage5_state", lambda *_a, **_k:
        SimpleNamespace(contract=base.constraint, active_graph=base.graph))
    stage5_calls = []
    def stage5(**_kwargs):
        stage5_calls.append(1)
        return SimpleNamespace(status="adapter_pending", graph_hash=base.graph.graph_hash)
    args = dict(job_id="job", worker_id="worker", lease_attempt=1, stage3_contract=contract,
        facts_loader=lambda: facts, stage5_runner=stage5, stage3_contract_loader=None,
        policy_fact_loader=None, classification_binding=None, verification_binding=None,
        classify_callback=None, difficulty_verify_callback=None, quality_callback=_quality(),
        subject_id=1, title="priority runtime", artifact_dir=tmp_path, artifact_callback=_artifact([]),
        receipt_callback=lambda _: None, priority_once=True)
    return db, args, stage5_calls


@pytest.mark.parametrize("limit", [0, 2])
def test_runtime_consumption_conflict_reselects_within_persisted_budget(monkeypatch, tmp_path, limit):
    db, args, stage5_calls = setup_runtime(monkeypatch, tmp_path)
    calls = []
    artifact = args["artifact_callback"]
    def race(request):
        calls.append(request["selection_hash"])
        if len(calls) == 1:
            state = runtime.recover_d2_candidate_chain(db, job_id="job")
            keys = load_priority_claims(db, job_id="job", state=state)
            # Native two-job transaction racing is covered separately; inject its committed fact here.
            with immediate_transaction(db):
                consume_priority_once_in_transaction(db, keys=keys, publication_hash="d" * 64,
                    job_id="concurrent", exam_id=900, consumed_at="2026-09-06T00:00:00+00:00")
        return artifact(request)
    args["artifact_callback"] = race
    args["priority_reselection_limit"] = limit
    if limit == 0:
        with pytest.raises(D2PublicationError) as caught:
            runtime.run_d2_generation(db, **args)
        assert caught.value.reason == "priority_reselection_budget_exhausted"
        assert len(calls) == 1
        assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0
    else:
        result = runtime.run_d2_generation(db, **args)
        assert result.status == "completed"
        assert len(calls) == 2 and calls[0] != calls[1]
        assert len(stage5_calls) == 1
        assert db.execute(f"SELECT count(*) FROM {RESELECTIONS}").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 1
        assert result.call_counts["quality"] == 2
        recovered = runtime.run_d2_generation(db, **args)
        assert recovered.status == "recovered" and len(calls) == 2
        assert recovered.call_counts == {}


def test_required_priority_without_a_verified_relation_stops_before_quality(monkeypatch, tmp_path):
    db, args, _ = setup_runtime(monkeypatch, tmp_path)
    db.execute("DELETE FROM mistakes")
    db.commit()
    args["quality_callback"] = lambda _: pytest.fail("no eligible relation must not call AI")
    with pytest.raises(D2PublicationError) as caught:
        runtime.run_d2_generation(db, **args)
    assert caught.value.reason == "priority_once_unavailable"


def test_search_guard_bounds_database_polling_without_disabling_cancellation(monkeypatch):
    from cuoti_app.services.job_contracts import JobCancelled
    times = iter([10.0, 10.01, 10.05, 10.101])
    monkeypatch.setattr(runtime.time, "monotonic", lambda: next(times))
    checked = []
    def guard(stage):
        checked.append(stage)
        if len(checked) == 2:
            raise JobCancelled("cancelled")
    check = runtime._selection_search_guard(guard, "selection")
    assert check() and check() and check()
    assert checked == ["selection"]
    with pytest.raises(JobCancelled):
        check()


def test_runtime_replays_selection_before_opening_write_transaction(monkeypatch, tmp_path):
    db, args, _ = setup_runtime(monkeypatch, tmp_path)
    observed = []
    guard_observed = []
    recover_observed = []
    original = runtime._validate_selection
    original_recover = runtime.recover_d2_candidate_chain

    def validate(*call_args, **call_kwargs):
        observed.append(db.in_transaction)
        original_guard = call_kwargs["search_guard"]
        call_kwargs["search_guard"] = lambda: guard_observed.append(db.in_transaction) or original_guard()
        return original(*call_args, **call_kwargs)

    monkeypatch.setattr(runtime, "_validate_selection", validate)
    def recover(*call_args, **call_kwargs):
        recover_observed.append(db.in_transaction)
        return original_recover(*call_args, **call_kwargs)
    monkeypatch.setattr(runtime, "recover_d2_candidate_chain", recover)
    result = runtime.run_d2_generation(db, **args)
    assert result.status == "completed"
    assert observed == [False]
    assert guard_observed and set(guard_observed) == {False}
    assert recover_observed and set(recover_observed) == {False}
