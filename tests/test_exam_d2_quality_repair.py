from functools import partial
import json

import pytest

from cuoti_app.migrations.v55_exam_generation_d2_content_rejections import migrate_v54_to_v55
from cuoti_app.services import exam_generation_d2_runtime as runtime
from cuoti_app.services.exam_generation_d2_publication import D2PublicationError, D2_TABLE
from cuoti_app.services.exam_generation_d2_content_rejections import CONTENT_REJECTIONS, PROBE_RECEIPTS
from cuoti_app.services.exam_generation_d2_render_probe import probe_rendering_risk
from cuoti_app.services.exam_generation_priority_runtime import RESELECTIONS
from cuoti_app.services.exam_generation_stage5_d2_solver import D2SearchIncompleteError
from cuoti_app.services.exam_generation_stage6_d2_repository import D2_CHECKPOINTS, recover_d2_candidate_chain
from cuoti_app.services.tex import TexCompileError
from tests.test_exam_priority_runtime import setup_runtime
from tests.test_exam_d2_publication import _quality


def _setup(monkeypatch, tmp_path, **kwargs):
    db, args, stage5_calls = setup_runtime(monkeypatch, tmp_path, **kwargs)
    migrate_v54_to_v55(db)
    db.commit()
    args["priority_once"] = False
    return db, args, stage5_calls


def _content_failure(request):
    if request["kind"] == "candidate":
        raise TexCompileError("fixture content", code="TEX_UNDEFINED_COMMAND")


@pytest.mark.parametrize("limit", [0, 2])
def test_confirmed_content_reselection_and_total_budget(monkeypatch, tmp_path, limit):
    db, args, stage5_calls = _setup(monkeypatch, tmp_path)
    calls = []
    def quality(request):
        calls.append(json.loads(request)["selection_hash"])
        return _quality(len(calls) > 1)(request)
    args.update(quality_callback=quality, priority_reselection_limit=limit,
                render_probe=partial(probe_rendering_risk, compile_callback=_content_failure))
    if limit == 0:
        with pytest.raises(D2PublicationError) as caught:
            runtime.run_d2_generation(db, **args)
        assert caught.value.reason == "quality_reselection_budget_exhausted"
        assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0
    else:
        result = runtime.run_d2_generation(db, **args)
        assert result.status == "completed"
        assert len(calls) == 2 and calls[0] != calls[1]
        assert len(stage5_calls) == 1
        assert db.execute(f"SELECT count(*) FROM {RESELECTIONS}").fetchone()[0] == 1
        assert db.execute("SELECT source_candidate_id FROM exam_items").fetchone()[0] == "candidate-2"
        recovered = runtime.run_d2_generation(db, **args)
        assert recovered.status == "recovered" and len(calls) == 2
        active = recover_d2_candidate_chain(db, job_id="job")
        previous = db.execute(
            f"SELECT checkpoint_hash FROM {D2_CHECKPOINTS} WHERE job_id=? AND selection_hash=?",
            ("job", calls[0]),
        ).fetchone()[0]
        historical = recover_d2_candidate_chain(db, job_id="job", checkpoint_hash=previous, require_head=False)
        assert len(active.factor_receipts) == 1
        assert len(historical.factor_receipts) == 2
        assert historical.selection.assignments[0]["candidate_id"] == "candidate-1"
    assert db.execute(f"SELECT count(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 1
    assert db.execute(f"SELECT count(*) FROM {PROBE_RECEIPTS}").fetchone()[0] == 1


def test_environment_failure_preserves_passed_quality_for_same_task(monkeypatch, tmp_path):
    db, args, _ = _setup(monkeypatch, tmp_path)
    original_artifact = args["artifact_callback"]
    quality_calls = []
    def quality(request):
        quality_calls.append(1)
        return _quality()(request)
    def environment(_request):
        raise TexCompileError("fixture environment", code="XELATEX_NOT_FOUND")
    args.update(quality_callback=quality, artifact_callback=environment,
                render_probe=partial(probe_rendering_risk, compile_callback=environment))
    with pytest.raises(D2PublicationError) as caught:
        runtime.run_d2_generation(db, **args)
    assert caught.value.reason == "quality_environment_requires_repair"
    assert db.execute(f"SELECT count(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 0
    assert db.execute(f"SELECT count(*) FROM {RESELECTIONS}").fetchone()[0] == 0
    assert db.execute(f"SELECT state FROM {D2_TABLE}").fetchone()[0] == "request_frozen"
    args["artifact_callback"] = original_artifact
    result = runtime.run_d2_generation(db, **args)
    assert result.status == "completed" and quality_calls == [1]


def test_unreproduced_ai_risk_is_unknown_not_content_or_infeasible(monkeypatch, tmp_path):
    db, args, _ = _setup(monkeypatch, tmp_path)
    quality_calls = []
    def quality(request):
        quality_calls.append(1)
        return _quality(False)(request)
    args.update(quality_callback=quality,
                render_probe=partial(probe_rendering_risk, compile_callback=lambda _request: None))
    for _ in range(6):
        with pytest.raises(D2PublicationError) as caught:
            runtime.run_d2_generation(db, **args)
        assert caught.value.reason == "quality_attribution_unknown"
    assert quality_calls == [1]
    with pytest.raises(D2PublicationError) as caught:
        runtime.run_d2_generation(db, **args)
    assert caught.value.reason == "quality_probe_budget_exhausted"
    assert db.execute(f"SELECT count(*) FROM {PROBE_RECEIPTS}").fetchone()[0] == 6
    assert db.execute(f"SELECT count(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 0
    assert db.execute(f"SELECT count(*) FROM {RESELECTIONS}").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0


def test_incomplete_repair_search_recovers_without_reprobing_rejected_content(monkeypatch, tmp_path):
    db, args, stage5_calls = _setup(monkeypatch, tmp_path)
    calls = []
    def quality(request):
        calls.append(1)
        return _quality(len(calls) > 1)(request)
    args.update(quality_callback=quality,
                render_probe=partial(probe_rendering_risk, compile_callback=_content_failure))
    original_guard = runtime._selection_search_guard
    with monkeypatch.context() as local_patch:
        local_patch.setattr(runtime, "_selection_search_guard", lambda guard, stage:
            (lambda: False) if stage == "quality_reselection" else original_guard(guard, stage))
        with pytest.raises(D2SearchIncompleteError):
            runtime.run_d2_generation(db, **args)
    assert db.execute(f"SELECT count(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 1
    assert db.execute(f"SELECT count(*) FROM {RESELECTIONS}").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0
    args["render_probe"] = lambda **_: pytest.fail("confirmed content must not be probed again")
    result = runtime.run_d2_generation(db, **args)
    assert result.status == "completed" and calls == [1, 1] and len(stage5_calls) == 1
    assert db.execute(f"SELECT count(*) FROM {PROBE_RECEIPTS}").fetchone()[0] == 1


def test_priority_and_quality_replacements_share_one_total_budget(monkeypatch, tmp_path):
    from cuoti_app.services.exam_generation_priority_runtime import load_priority_claims
    from cuoti_app.services.exam_generation_priority_once import consume_priority_once_in_transaction
    from cuoti_app.transactions import immediate_transaction

    db, args, _ = _setup(monkeypatch, tmp_path)
    artifact = args["artifact_callback"]
    calls = []
    def quality(request):
        calls.append(1)
        return _quality(len(calls) == 1)(request)
    def race(request):
        state = recover_d2_candidate_chain(db, job_id="job")
        keys = load_priority_claims(db, job_id="job", state=state)
        with immediate_transaction(db):
            consume_priority_once_in_transaction(db, keys=keys, publication_hash="d" * 64,
                job_id="concurrent", exam_id=900, consumed_at="2026-09-06T00:00:00+00:00")
        return artifact(request)
    args.update(priority_once=True, priority_reselection_limit=1, quality_callback=quality,
                artifact_callback=race,
                render_probe=partial(probe_rendering_risk, compile_callback=_content_failure))
    with pytest.raises(D2PublicationError) as caught:
        runtime.run_d2_generation(db, **args)
    assert caught.value.reason == "quality_reselection_budget_exhausted"
    assert calls == [1, 1]
    assert db.execute(f"SELECT reason FROM {RESELECTIONS}").fetchall()[0][0] == "priority_consumed_concurrently"
    assert db.execute(f"SELECT count(*) FROM {CONTENT_REJECTIONS}").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM exams").fetchone()[0] == 0


def test_forbidden_candidate_body_is_replaced_without_executing_unsafe_tex(monkeypatch, tmp_path):
    db, args, _ = _setup(monkeypatch, tmp_path, first_content=r"\input{forbidden}")
    compilations = []
    def compile_safe_control(request):
        assert request["kind"] == "control"
        assert r"\input" not in request["latex_source"]
        compilations.append(1)
    args["render_probe"] = partial(probe_rendering_risk, compile_callback=compile_safe_control)
    result = runtime.run_d2_generation(db, **args)
    assert result.status == "completed" and compilations == [1]
    assert result.call_counts["quality"] == 2
    assert db.execute("SELECT source_candidate_id FROM exam_items").fetchone()[0] == "candidate-2"
    receipt = json.loads(db.execute(f"SELECT payload_json FROM {PROBE_RECEIPTS}").fetchone()[0])
    assert receipt["classification"] == "CONTENT"
    assert all(row["stage"] == "body_validation" and row["reason_code"] == "TEX_UNSAFE_BODY"
               for row in receipt["candidate_attempts"])
