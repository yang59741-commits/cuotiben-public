import pytest

from cuoti_app.services.exam_generation_stage6_objective import (
    CandidateBaseObjectiveError,
    build_objective_policy,
    build_objective_receipt,
    compare_objectives,
    explain_objective,
)


@pytest.fixture
def policy():
    return build_objective_policy(
        parameter_set_hash="p" * 64,
        level_mapping={
            "verified_mistake_relevance": {"unknown": 0, "not_applicable": 0, "none": 0, "related": 1, "strong": 2},
            "history_exposure_novelty": {"unknown": 0, "not_applicable": 0, "exposed": -1, "novel": 1},
        },
        directions={"verified_mistake_relevance": "max", "history_exposure_novelty": "max"},
    )


def _candidate(relevance="related", novelty="novel"):
    return {"candidate_id": "c1", "candidate_fact_hash": "f" * 64, "factors": {
        "verified_mistake_relevance": {"level": relevance, "evidence_codes": ["local_verified"]},
        "history_exposure_novelty": {"level": novelty, "evidence_codes": ["publication_exact"]},
    }}


def test_provider_free_objective_is_lexicographic_and_recomputable(policy):
    first = build_objective_receipt([_candidate("strong", "exposed")], policy=policy, mode="free")
    second = build_objective_receipt([_candidate("related", "novel")], policy=policy, mode="free")
    assert compare_objectives(first, second, policy=policy) > 0
    explanation = explain_objective(first, policy=policy)
    assert explanation["vector"] == [2, -1]
    assert explanation["factor_order"] == ["verified_mistake_relevance", "history_exposure_novelty"]


def test_unknown_factor_is_not_silently_zero(policy):
    with pytest.raises(CandidateBaseObjectiveError, match="unknown_blocked"):
        build_objective_receipt([_candidate("unknown")], policy=policy)


def test_legacy_score_penalty_cannot_enter_objective(policy):
    bad = _candidate()
    bad["score"] = 99
    with pytest.raises(CandidateBaseObjectiveError, match="candidate_shape"):
        build_objective_receipt([bad], policy=policy)

