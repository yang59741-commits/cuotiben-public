from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from cuoti_app.services.exam_generation_stage5_d2_solver import (
    D2_LEGACY_SOLVER_VERSIONS,
    D2_SOLVER_VERSION,
    D2SolverError,
    solve_stage5_d2_selection,
)
from cuoti_app.services.exam_generation_stage5_solver import (
    Stage5GlobalConstraintContract,
)
from cuoti_app.services.exam_generation_stage6_d2_repository import (
    install_d2_candidate_schema_draft,
    persist_d2_candidate_chain,
    recover_d2_candidate_chain,
)
from cuoti_app.services.exam_generation_stage6_local_factor_contract import (
    project_local_factor_receipts_for_d2,
)
from cuoti_app.services import exam_generation_d2_quality_repair as quality_repair
from cuoti_app.services import exam_generation_d2_runtime as d2_runtime
from cuoti_app.services import exam_generation_priority_runtime as priority_runtime
from cuoti_app.services import exam_generation_stage5_d2_solver as d2_solver
from tests.test_exam_generation_stage5_d2_solver import (
    _choice_case,
    _mixed_quota_case,
)
from tests.test_exam_generation_stage6_d2_repository import _case as _repository_case


def _history_payload(rows=()):
    return {
        "mode": "audit_only",
        "scope": {
            "generation_mode": "past",
            "subject_id": 7,
            "window_limit": 30,
        },
        "content_hash_hits": [
            {
                "content_hash": content_hash,
                "success_count": success_count,
                "latest_order_rank": latest_order_rank,
                "latest_used_at": "2026-09-01T00:00:00Z",
                "usage_hashes": [f"{success_count + latest_order_rank:064x}"],
            }
            for content_hash, success_count, latest_order_rank in rows
        ],
    }


def _with_history(contract, rows=()):
    payload = contract.to_dict()
    payload["recent_usage"] = {
        "version": "past-v3-stage5-recent-usage-audit-only-v3",
        "payload": _history_payload(rows),
    }
    return Stage5GlobalConstraintContract(
        contract.contract_version,
        contract.stage3_contract_hash,
        contract.candidate_facts_hash,
        contract.projection_identity_hash,
        contract.question_count,
        contract.constraint_hash,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )


def _select(
    contract,
    graph,
    policy,
    rows,
    *,
    eligible=None,
    solver_version=None,
):
    return solve_stage5_d2_selection(
        contract,
        graph,
        factor_receipts=rows,
        objective_policy=policy,
        target_mode="past",
        m_once_eligible_identities=eligible,
        solver_version=solver_version,
    )


def _set_levels(rows, *, relevance="related", history="exposed"):
    for row in rows:
        row["factors"]["verified_mistake_relevance"]["level"] = relevance
        row["factors"]["history_exposure_novelty"]["level"] = history


@pytest.mark.parametrize("mode", ["free", "mistakes"])
def test_past_exact_history_cannot_be_applied_to_another_mode(mode):
    contract, graph, policy, rows = _choice_case(1, 2)
    contract = _with_history(contract, [(rows[0]["content_hash"], 1, 1)])
    with pytest.raises(D2SolverError, match="d2_history_mode_mismatch"):
        solve_stage5_d2_selection(contract, graph, factor_receipts=rows, objective_policy=policy, target_mode=mode)


def test_v3_missing_exact_soft_facts_contribute_zero():
    contract, graph, policy, rows = _choice_case(1, 2)
    _set_levels(rows, history="novel")

    result = _select(contract, graph, policy, rows)

    assert result.solver_version == D2_SOLVER_VERSION
    assert result.objective_vector == (0, 1, 1, 0, 0)
    assert result.assignments[0]["candidate_id"] == "C01-00"


def test_novelty_precedes_exact_frequency_and_exact_usage_is_content_bound():
    contract, graph, policy, rows = _choice_case(1, 2)
    _set_levels(rows)
    rows[1]["factors"]["history_exposure_novelty"]["level"] = "novel"
    contract = _with_history(
        contract,
        [(rows[0]["content_hash"], 1, 12)],
    )

    result = _select(contract, graph, policy, rows)

    assert result.assignments[0]["candidate_id"] == rows[1]["candidate_id"]
    assert result.objective_vector[2:] == (1, 0, 0)


def test_equal_exposed_prefers_fewer_uses_then_older_latest_use():
    contract, graph, policy, rows = _choice_case(1, 2)
    _set_levels(rows)
    fewer = _with_history(
        contract,
        [
            (rows[0]["content_hash"], 5, 20),
            (rows[1]["content_hash"], 1, 1),
        ],
    )
    fewer_result = _select(fewer, graph, policy, rows)
    assert fewer_result.assignments[0]["candidate_id"] == rows[1]["candidate_id"]
    assert fewer_result.objective_vector[-2:] == (-1, 1)

    older = _with_history(
        contract,
        [
            (rows[0]["content_hash"], 2, 1),
            (rows[1]["content_hash"], 2, 9),
        ],
    )
    older_result = _select(older, graph, policy, rows)
    assert older_result.assignments[0]["candidate_id"] == rows[1]["candidate_id"]
    assert older_result.objective_vector[-2:] == (-2, 9)


def test_not_applicable_history_neither_claims_novelty_nor_exact_order():
    contract, graph, policy, rows = _choice_case(1, 2)
    _set_levels(rows, relevance="none", history="exposed")
    rows[0]["factors"]["history_exposure_novelty"]["level"] = "not_applicable"
    contract = _with_history(
        contract,
        [
            (rows[0]["content_hash"], 99, 99),
            (rows[1]["content_hash"], 1, 1),
        ],
    )

    result = _select(contract, graph, policy, rows)

    assert result.assignments[0]["candidate_id"] == rows[0]["candidate_id"]
    assert result.objective_vector == (0, 0, 0, 0, 0)
    assert result.assignments[0]["factor_levels"]["history_exposure_novelty"] == (
        "not_applicable"
    )


def test_m_once_and_relevance_still_precede_all_history_dimensions():
    contract, graph, policy, rows = _choice_case(1, 2, source="past_exam_other")
    _set_levels(rows)
    rows[0]["factors"]["verified_mistake_relevance"]["level"] = "strong"
    rows[1]["factors"]["verified_mistake_relevance"]["level"] = "none"
    rows[1]["factors"]["history_exposure_novelty"]["level"] = "novel"
    contract = _with_history(
        contract,
        [
            (rows[0]["content_hash"], 100, 1),
            (rows[1]["content_hash"], 0, 99),
        ],
    )

    relevance_winner = _select(contract, graph, policy, rows)
    assert relevance_winner.assignments[0]["candidate_id"] == rows[0]["candidate_id"]

    eligible = {
        (
            rows[1]["candidate_id"],
            rows[1]["candidate_fact_hash"],
            rows[1]["content_hash"],
        )
    }
    once_winner = _select(contract, graph, policy, rows, eligible=eligible)
    assert once_winner.assignments[0]["candidate_id"] == rows[1]["candidate_id"]
    assert once_winner.objective_vector[0] == 1


def test_all_exposed_candidates_remain_feasible_and_hard_quotas_do_not_relax():
    contract, graph, policy, rows = _mixed_quota_case(4)
    _set_levels(rows)
    history = []
    for row in rows:
        candidate_id = row["candidate_id"]
        # Make ordinary-A choices look best on exact history.  The solver must
        # still emit exactly the frozen 2 ordinary / 2 past / A1 / B1 mix.
        success_count = 1 if candidate_id.endswith("-00") else 50
        history.append((row["content_hash"], success_count, 10))
    contract = _with_history(contract, history)

    result = _select(contract, graph, policy, rows)
    assignments = result.assignments

    assert len(assignments) == 4
    assert sum(row["source_kind"] == "ordinary" for row in assignments) == 2
    assert sum(row["source_kind"] == "past_exam_other" for row in assignments) == 2
    assert sum(row["chapter"] == "A" for row in assignments) == 1
    assert sum(row["chapter"] == "B" for row in assignments) == 1
    assert len({row["candidate_id"] for row in assignments}) == 4
    assert len({row["content_hash"] for row in assignments}) == 4


def test_runtime_derives_exact_exposure_by_content_hash_only():
    contract, graph, policy, rows = _choice_case(1, 2)
    contract = _with_history(contract, [(rows[1]["content_hash"], 3, 1)])
    base = SimpleNamespace(
        constraint=contract,
        graph=graph,
        stage4={
            "candidates": [
                {
                    "candidate_id": row["candidate_id"],
                    "content_snapshot": {
                        "content": f"candidate {index}",
                        "conditions": [],
                        "options": [],
                        "figure_references": [],
                    },
                }
                for index, row in enumerate(rows)
            ]
        },
    )

    factors = d2_runtime.local_factors(base, policy, mistake_relations=None)
    levels = {
        row["candidate_id"]: row["factors"]["history_exposure_novelty"]["level"]
        for row in factors
    }

    assert levels[rows[0]["candidate_id"]] == "novel"
    assert levels[rows[1]["candidate_id"]] == "exposed"


def test_content_replacement_reuses_persisted_legacy_solver_version(monkeypatch):
    base, policy, factors, _selection = _repository_case()
    legacy_version = D2_LEGACY_SOLVER_VERSIONS[1]
    selection = solve_stage5_d2_selection(
        base.constraint,
        base.graph,
        factor_receipts=project_local_factor_receipts_for_d2(
            factors, policy=policy
        ),
        objective_policy=policy,
        solver_version=legacy_version,
    )
    state = SimpleNamespace(
        base=base,
        selection=selection,
        objective_policy=policy,
    )

    class _Cursor:
        @staticmethod
        def fetchone():
            return (0,)

    class _Database:
        @staticmethod
        def execute(*_args, **_kwargs):
            return _Cursor()

    captured = {}
    monkeypatch.setattr(
        quality_repair,
        "content_repair_graph",
        lambda *_args: (base.graph, {"slot-0001"}),
    )
    monkeypatch.setattr(d2_runtime, "local_factors", lambda *_args, **_kwargs: factors)
    monkeypatch.setattr(
        priority_runtime, "load_priority_claims", lambda *_args, **_kwargs: None
    )

    def solve(*_args, **kwargs):
        captured["solver_version"] = kwargs.get("solver_version")
        return selection

    monkeypatch.setattr(d2_solver, "solve_stage5_d2_selection", solve)
    monkeypatch.setattr(
        priority_runtime,
        "persist_reselection",
        lambda *_args, **kwargs: kwargs["selection"],
    )

    replaced = quality_repair.replace_after_content_rejection(
        _Database(),
        state=state,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=7,
        mistake_relations=None,
        rejected={selection.assignments[0]["content_hash"]},
        limit=1,
        search_guard=None,
    )

    assert captured["solver_version"] == legacy_version
    assert replaced.solver_version == legacy_version


@pytest.mark.parametrize("legacy_version", D2_LEGACY_SOLVER_VERSIONS)
def test_explicit_legacy_solver_bytes_ignore_v3_history(legacy_version):
    contract, graph, policy, rows = _choice_case(1, 2)
    without_history = _select(
        contract, graph, policy, rows, solver_version=legacy_version
    )
    with_history = _select(
        _with_history(
            contract,
            [
                (rows[0]["content_hash"], 99, 1),
                (rows[1]["content_hash"], 1, 99),
            ],
        ),
        graph,
        policy,
        rows,
        solver_version=legacy_version,
    )

    assert with_history.to_dict() == without_history.to_dict()
    assert with_history.selection_hash == without_history.selection_hash
    assert with_history.objective_hash == without_history.objective_hash
    expected_dimensions = 2 if legacy_version.endswith("v1") else 3
    assert len(with_history.objective_vector) == expected_dimensions
    golden = {
        "stage5-d2-lexicographic-solver-v1": {
            "objective_hash": "49a0c7586ff2e88dbe76b7f14893888e2690c86c2698d1013f8424d92d23f243",
            "selection_hash": "16206dbb2550eeb42237fb619a08ee900f36e8eb457cb21611c496c56ced2df2",
            "objective_vector": (1, 1),
        },
        "stage5-d2-lexicographic-solver-v2": {
            "objective_hash": "e6a299b42801f55edb6153e540b29063de5fa0fe36f92b8d170fc97459c0125a",
            "selection_hash": "047fa8ffd1e1a31788336887cb3430ca317822c2eafe96f4f95a59b09e9a2f83",
            "objective_vector": (0, 1, 1),
        },
    }[legacy_version]
    assert without_history.objective_hash == golden["objective_hash"]
    assert without_history.selection_hash == golden["selection_hash"]
    assert without_history.objective_vector == golden["objective_vector"]


@pytest.mark.parametrize("legacy_version", D2_LEGACY_SOLVER_VERSIONS)
def test_repository_recovers_with_persisted_solver_version(legacy_version):
    base, policy, factors, _selection = _repository_case()
    selection = solve_stage5_d2_selection(
        base.constraint,
        base.graph,
        factor_receipts=project_local_factor_receipts_for_d2(
            factors, policy=policy
        ),
        objective_policy=policy,
        solver_version=legacy_version,
    )
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    install_d2_candidate_schema_draft(db)
    try:
        persisted = persist_d2_candidate_chain(
            db,
            job_id=f"history-{legacy_version}",
            base=base,
            objective_policy=policy,
            factor_receipts=factors,
            selection=selection,
            stage_durations_ms={"local_factor_ms": 0, "local_solve_ms": 0},
        )
        recovered = recover_d2_candidate_chain(
            db, job_id=f"history-{legacy_version}"
        )
    finally:
        db.close()

    assert persisted.selection.solver_version == legacy_version
    assert recovered.selection.solver_version == legacy_version
    assert recovered.selection.to_dict() == selection.to_dict()
