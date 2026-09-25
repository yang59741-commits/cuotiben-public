from dataclasses import replace

import pytest

from cuoti_app.services import exam_generation_d2_runtime as runtime
from cuoti_app.services.exam_generation_selection_explanation import (
    explain_d2_selection, read_exam_selection_explanation,
)
from cuoti_app.services.exam_generation_stage5_d2_solver import D2SearchIncompleteError, solve_stage5_d2_selection
from cuoti_app.services.exam_generation_stage6_local_factor_contract import project_local_factor_receipts_for_d2
from tests.test_exam_priority_runtime import setup_runtime, full_case
from tests.test_exam_d2_repair_scope import _case as scope_case, _solve, _factor_receipts


def test_published_explanation_is_frozen_read_only_and_recovers_without_ai(monkeypatch, tmp_path):
    db, args, calls = setup_runtime(monkeypatch, tmp_path)
    result = runtime.run_d2_generation(db, **args)
    exam_id = db.execute("SELECT id FROM exams").fetchone()[0]
    before = db.total_changes
    first = read_exam_selection_explanation(db, exam_id=exam_id)
    assert first["status"] == "available"
    assert first["items"][0]["reason_code"] == "stable_tie_break"
    assert first["items"][0]["alternative"]["candidate_id"] == "candidate-2"
    assert "$x+2=3$" in first["items"][0]["alternative"]["content"]
    assert first["hard_dedup_preserved"] is True and first["similarity_enabled"] is False
    assert len(calls) == 1 and result.call_counts["quality"] == 1
    assert db.total_changes == before
    db.execute("UPDATE mistakes SET content='new unrelated content'")
    db.commit()
    assert read_exam_selection_explanation(db, exam_id=exam_id) == first
    with pytest.raises(D2SearchIncompleteError):
        read_exam_selection_explanation(db, exam_id=exam_id, budget_seconds=0)


def _state_with_factors(levels, *, priority=False, history=None):
    from types import SimpleNamespace
    base, _facts, _contract = full_case()
    if history is not None:
        from tests.test_exam_exact_history_solver import _with_history
        base = replace(base, constraint=_with_history(base.constraint,
            [(row["content_hash"], count, rank) for row, (count, rank) in zip(base.stage4["candidates"], history)]))
    policy = runtime.release_objective_policy()
    factors = runtime.local_factors(base, policy)
    for row, level in zip(factors, levels):
        row["factors"]["verified_mistake_relevance"]["level"] = level[0]
        row["factors"]["history_exposure_novelty"]["level"] = level[1]
    from cuoti_app.services.exam_generation_stage6_local_factor_contract import build_local_factor_receipt
    factors = [build_local_factor_receipt(candidate_id=row["candidate_id"], candidate_fact_hash=row["candidate_fact_hash"], content_hash=row["content_hash"],
        factors={key: {"level": value["level"], "evidence_codes": value["evidence_codes"]} for key, value in row["factors"].items()}, policy=policy) for row in factors]
    eligible = {(factors[1]["candidate_id"], factors[1]["candidate_fact_hash"], factors[1]["content_hash"])} if priority else None
    selection = solve_stage5_d2_selection(base.constraint, base.graph, objective_policy=policy,
        factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy),
        target_mode="past", m_once_eligible_identities=eligible)
    return SimpleNamespace(base=base, objective_policy=policy, factor_receipts=factors, selection=selection)


@pytest.mark.parametrize("levels,priority,reason", [
    ([("strong", "novel"), ("none", "exposed")], True, "past_m_once"),
    ([("none", "novel"), ("strong", "exposed")], False, "verified_mistake_relevance"),
    ([("none", "exposed"), ("none", "novel")], False, "history_exposure_novelty"),
])
def test_explanation_names_actual_deciding_layer(levels, priority, reason):
    state = _state_with_factors(levels, priority=priority)
    report = explain_d2_selection(state)
    assert report["items"][0]["reason_code"] == reason
    assert report["items"][0]["candidate_id"] == "candidate-2"
    assert report["dedupe_tolerance_used"] == (levels[1][1] == "exposed")
    assert report["items"][0]["history"] is None
    assert "未记录可回放" in report["items"][0]["history_text"]


def test_coupled_hard_constraints_are_not_explained_as_individual_preference():
    from types import SimpleNamespace
    from cuoti_app.services.exam_generation_d2_quality_repair import _freeze_graph
    base, policy, _state, _candidates = scope_case()
    payload = base.graph.to_dict()
    payload["edges"] = [row for row in payload["edges"] if row["position"] > 2 or row["candidate_id"] in {"component-0", "component-1"}]
    base = replace(base, graph=_freeze_graph(payload))
    selection = _solve(base, policy)
    factors = _factor_receipts(base.graph, policy)
    report = explain_d2_selection(SimpleNamespace(base=base, selection=selection, objective_policy=policy, factor_receipts=factors))
    assert len(report["items"]) == 4
    assert report["hard_dedup_preserved"]
    assert all(row["reason_code"] in {"stable_tie_break", "hard_constraints"} for row in report["items"])
    assert all(row["reason_code"] == "hard_constraints" for row in report["items"][:2])


@pytest.mark.parametrize("history,reason", [
    ([(4, 1), (2, 3)], "history_exact_success_count"),
    ([(2, 1), (2, 3)], "history_latest_use_order"),
])
def test_exact_history_decision_and_reuse_explanation(history, reason):
    state = _state_with_factors([("none", "exposed"), ("none", "exposed")], history=history)
    report = explain_d2_selection(state)
    item = report["items"][0]
    assert item["reason_code"] == reason
    assert item["candidate_id"] == "candidate-2" and item["history"]["success_count"] == 2
    assert "成功使用 2 次" in item["history_text"]
    assert report["dedupe_tolerance_used"] is True
    assert "未放宽同卷精确去重" in report["summary_text"]


def test_api_keeps_paper_available_when_explanation_fails(monkeypatch, tmp_path):
    from flask import Flask
    from cuoti_app.migrations.v56_exam_soft_delete import migrate_v55_to_v56
    from cuoti_app.routes import exams, exams_access
    from cuoti_app.services import exam_generation_selection_explanation as explanations
    db, args, _ = setup_runtime(monkeypatch, tmp_path)
    migrate_v55_to_v56(db)
    db.commit()
    runtime.run_d2_generation(db, **args)
    exam_id = db.execute("SELECT id FROM exams").fetchone()[0]
    app = Flask(__name__)
    app.register_blueprint(exams.bp)
    monkeypatch.setattr(exams_access, "get_db", lambda: db)
    client = app.test_client()
    response = client.get(f"/api/v2/exams/{exam_id}/selection-explanation")
    assert response.status_code == 200 and response.json["status"] == "available"
    monkeypatch.setattr(explanations, "read_exam_selection_explanation", lambda *_a, **_k: (_ for _ in ()).throw(D2SearchIncompleteError("bounded")))
    response = client.get(f"/api/v2/exams/{exam_id}/selection-explanation")
    assert response.status_code == 200 and response.json["status"] == "unavailable"
    assert db.execute("SELECT artifact_status FROM exams WHERE id=?", (exam_id,)).fetchone()[0] == "published"
    assert client.get("/api/v2/exams/999/selection-explanation").status_code == 404


@pytest.mark.parametrize("version", ["stage5-d2-lexicographic-solver-v1", "stage5-d2-lexicographic-solver-v2"])
def test_published_old_versions_explain_without_new_history_layers(monkeypatch, tmp_path, version):
    db, args, _ = setup_runtime(monkeypatch, tmp_path)
    args["priority_once"] = False
    original_solver = runtime.solve_stage5_d2_selection
    monkeypatch.setattr(runtime, "solve_stage5_d2_selection", lambda *a, **kw: original_solver(*a, **kw, solver_version=version))
    runtime.run_d2_generation(db, **args)
    exam_id = db.execute("SELECT id FROM exams").fetchone()[0]
    before = db.total_changes
    report = read_exam_selection_explanation(db, exam_id=exam_id)
    assert report["status"] == "available" and report["solver_version"] == version
    assert "history_exact_success_count" not in report["objective_order"]
    assert all(row["history"] is None for row in report["items"])
    assert db.total_changes == before


def test_omitted_history_is_not_reported_as_reuse_or_preference():
    state = _state_with_factors([("none", "not_applicable"), ("none", "not_applicable")], history=[(1, 1), (2, 3)])
    report = explain_d2_selection(state)
    item = report["items"][0]
    assert item["history_text"] == "本卷未使用历史软偏好。"
    assert item["history"] is None
    assert item["reason_code"] == "stable_tie_break"
    assert not item["dedupe_tolerance_used"] and not report["dedupe_tolerance_used"]
