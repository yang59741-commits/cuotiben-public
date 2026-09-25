import pytest

from cuoti_app.services.exam_generation_stage6_local_factor_contract import (
    LocalFactorReceiptError,
    build_local_factor_receipt,
    project_local_factor_receipts_for_d2,
    validate_local_factor_receipt,
)
from cuoti_app.services.exam_generation_stage6_objective import build_objective_policy


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


def _factors(relevance="related", novelty="novel"):
    return {
        "verified_mistake_relevance": {"level": relevance, "evidence_codes": ["mistake-event-1"]},
        "history_exposure_novelty": {"level": novelty, "evidence_codes": ["publication-snapshot-1"]},
    }


def test_explicit_receipt_round_trips_and_binds_all_identities(policy):
    row = build_local_factor_receipt(candidate_id="c1", candidate_fact_hash="f" * 64,
                                     content_hash="c" * 64, factors=_factors(), policy=policy)
    got = validate_local_factor_receipt(row, policy=policy,
                                        expected={"candidate_id": "c1", "candidate_fact_hash": "f" * 64, "content_hash": "c" * 64})
    assert got == row


@pytest.mark.parametrize("field", ["candidate_fact_hash", "content_hash", "policy_hash", "receipt_hash"])
def test_identity_evidence_or_policy_tampering_fails_closed(policy, field):
    row = build_local_factor_receipt(candidate_id="c1", candidate_fact_hash="f" * 64,
                                     content_hash="c" * 64, factors=_factors(), policy=policy)
    row[field] = "d" * 64
    with pytest.raises(LocalFactorReceiptError):
        validate_local_factor_receipt(row, policy=policy)


def test_unknown_is_not_implicitly_zero_and_missing_factor_is_rejected(policy):
    with pytest.raises(LocalFactorReceiptError, match="unknown_blocked"):
        build_local_factor_receipt(candidate_id="c1", candidate_fact_hash="f" * 64,
                                   content_hash="c" * 64, factors=_factors("unknown"), policy=policy)
    factors = _factors(); del factors["history_exposure_novelty"]
    with pytest.raises(LocalFactorReceiptError):
        build_local_factor_receipt(candidate_id="c1", candidate_fact_hash="f" * 64,
                                   content_hash="c" * 64, factors=factors, policy=policy)


def test_projection_verifies_evidence_before_d2_shape(policy):
    row = build_local_factor_receipt(candidate_id="c1", candidate_fact_hash="f" * 64,
                                     content_hash="c" * 64, factors=_factors(), policy=policy)
    projected = project_local_factor_receipts_for_d2([row], policy=policy)
    assert projected[0]["content_hash"] == "c" * 64
    assert "evidence_hash" not in projected[0]["factors"]["verified_mistake_relevance"]
    row["factors"]["verified_mistake_relevance"]["evidence_hash"] = "d" * 64
    with pytest.raises(LocalFactorReceiptError, match="evidence_hash"):
        project_local_factor_receipts_for_d2([row], policy=policy)

