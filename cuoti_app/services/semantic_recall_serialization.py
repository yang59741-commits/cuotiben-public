"""Privacy-bounded serialization for semantic recall contracts."""

from __future__ import annotations

from typing import Any

from .semantic_recall_contracts import RecallResult, timestamp


def recall_contract_payload(
    result: RecallResult,
    *,
    generated_at: str,
) -> dict[str, Any]:
    """Return the F1 transfer contract without question or prompt text."""

    return {
        "projection_id": result.projection_id,
        "semantic_snapshot_id": result.semantic_snapshot_id,
        "source_snapshot": result.source_snapshot,
        "projection_build_hash": result.projection_build_hash,
        "algorithm_version": result.algorithm_version,
        "dictionary_version": result.dictionary_version,
        "weights_version": result.weights_version,
        "candidate_decisions": [
            {
                "source_kind": item.source_kind,
                "candidate_id": item.candidate_id,
                "card_version_id": item.card_version_id,
                "content_hash": item.content_hash,
                "coverage_group": item.coverage_group,
                "coverage_reason": item.coverage_reason,
                "score": item.explanation.score,
                "matched_terms": list(item.explanation.matched_terms),
                "reasons": list(item.explanation.reasons),
            }
            for item in result.candidate_decisions
        ],
        "coverage_counts": dict(result.coverage_counts),
        "coverage_reason_counts": dict(result.coverage_reason_counts),
        "compatibility_counts": dict(result.compatibility_counts),
        "eligible_hard_pool_count": result.eligible_hard_pool_count,
        "coverage_ratio": result.coverage_ratio,
        "partial_cache": result.partial_cache,
        "partial_cache_reasons": list(result.partial_cache_reasons),
        "relaxed_quotas": list(result.relaxed_quotas),
        "fallback_reason": result.fallback_reason,
        "request_hash": result.request_hash,
        "result_hash": result.result_hash,
        "generated_at": timestamp(generated_at),
    }


__all__ = ["recall_contract_payload"]
