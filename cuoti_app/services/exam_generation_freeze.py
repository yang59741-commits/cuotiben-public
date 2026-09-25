"""Pure candidate-freeze contracts for staged exam generation."""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .ai_budgeting import estimate_text_tokens
from .exam_manifests import ExamManifestError
from .past_exams import other_past_candidates_by_ids
from .semantic_recall import semantic_candidate_content_hash, semantic_candidate_identity


def candidate_prompt_metrics(candidates: Sequence[Mapping[str, Any]]) -> dict:
    return {
        "count": len(candidates),
        "tokens": sum(
            max(
                1,
                int(
                    item.get("token_estimate")
                    or estimate_text_tokens(item.get("content"))
                    or 1
                ),
            )
            for item in candidates
        ),
        "bytes": sum(
            len(str(item.get("content") or "").encode("utf-8"))
            for item in candidates
        ),
    }


def candidate_decisions(decision, candidates: Sequence[Mapping[str, Any]]) -> list[dict]:
    by_identity = {
        (item.source_kind, item.candidate_id): item
        for item in (
            decision.effective_candidate_decisions if decision is not None else ()
        )
    }
    result = []
    for candidate in candidates:
        identity = semantic_candidate_identity(candidate)
        item = by_identity.get(identity)
        candidate_hash = semantic_candidate_content_hash(candidate)
        result.append(
            {
                "source_kind": identity[0],
                "candidate_id": identity[1],
                "card_version_id": item.card_version_id if item is not None else None,
                "content_hash": candidate_hash,
                "card_content_hash": item.content_hash if item is not None else None,
                "coverage_group": item.coverage_group if item is not None else "C",
                "coverage_reason": (
                    item.coverage_reason if item is not None else "missing_card"
                ),
            }
        )
    return result


def validate_frozen_candidates(
    payload: Mapping[str, Any], candidates: Sequence[Mapping[str, Any]]
) -> None:
    decisions = payload.get("candidate_decisions")
    if not isinstance(decisions, list) or len(decisions) != len(candidates):
        raise ExamManifestError("MANIFEST_CORRUPT", "冻结候选决策数量不一致")
    for candidate, decision in zip(candidates, decisions):
        identity = semantic_candidate_identity(candidate)
        if [identity[0], identity[1]] != [
            decision.get("source_kind"),
            decision.get("candidate_id"),
        ]:
            raise ExamManifestError("MANIFEST_CORRUPT", "冻结候选顺序或身份不一致")
        current_hash = semantic_candidate_content_hash(candidate)
        if current_hash != decision.get("content_hash"):
            raise ExamManifestError("MANIFEST_SOURCE_DRIFT", "冻结候选正文已变化")


def build_candidate_freeze_payload(
    *,
    source: str,
    decision,
    candidates: Sequence[Mapping[str, Any]],
    question_bank_snapshot,
    past_snapshot=None,
    paper_seeds: Sequence[int],
    semantic_settings_revision: int = 0,
    semantic_candidate_multiplier: int = 4,
    semantic_allow_partial_cache: bool = False,
    extra: Mapping[str, Any] | None = None,
) -> dict:
    """Build the immutable manifest stage that freezes candidate order."""

    if decision is None:
        raise ExamManifestError("MANIFEST_INVALID", "缺少召回决定")
    recall = decision.recall
    identities = [list(semantic_candidate_identity(item)) for item in candidates]
    metrics = candidate_prompt_metrics(candidates)
    decisions = candidate_decisions(decision, candidates)
    paper_orders = []
    for seed in paper_seeds:
        ordered = list(candidates)
        if source == "past":
            random.Random(seed).shuffle(ordered)
        paper_orders.append([str(item["candidate_id"]) for item in ordered])
    payload = {
        "source": source,
        "question_bank_snapshot": question_bank_snapshot,
        "past_exam_snapshot": past_snapshot,
        "semantic_snapshot_id": (
            recall.semantic_snapshot_id if recall is not None else "unavailable"
        ),
        "semantic_snapshot_identity_policy": "source-kind+candidate-id+content-hash-v1",
        "projection_id": decision.projection_id or "unavailable",
        "projection_build_hash": (
            recall.projection_build_hash if recall is not None else "0" * 64
        ),
        "recall_algorithm_version": (
            recall.algorithm_version if recall is not None else "legacy-fallback-v1"
        ),
        "dictionary_version": (
            recall.dictionary_version if recall is not None else "unavailable-v1"
        ),
        "weights_version": (
            recall.weights_version if recall is not None else "unavailable-v1"
        ),
        "recall_request_hash": (
            recall.request_hash if recall is not None else decision.legacy_ids_hash
        ),
        "hard_pool_hash": decision.hard_pool_hash,
        "recall_result_hash": (
            recall.result_hash if recall is not None else decision.legacy_ids_hash
        ),
        "candidate_identities": identities,
        "candidate_ids": [item[1] for item in identities],
        "candidate_decisions": decisions,
        "coverage_counts": dict(recall.coverage_counts) if recall is not None else {},
        "coverage_reason_counts": (
            dict(recall.coverage_reason_counts) if recall is not None else {}
        ),
        "partial_cache": bool(recall.partial_cache) if recall is not None else True,
        "semantic_settings_revision": int(semantic_settings_revision),
        "semantic_candidate_multiplier": int(semantic_candidate_multiplier),
        "semantic_allow_partial_cache": bool(semantic_allow_partial_cache),
        "relaxed_quotas": list(recall.relaxed_quotas) if recall is not None else [],
        "fallback_reason": decision.fallback_category,
        "deep_read_required_ids": [
            item["candidate_id"]
            for item in decisions
            if item["coverage_group"] == "C"
            or (
                item["coverage_group"] == "D"
                and item["coverage_reason"] in {"content_drift", "invalid_card", "stale"}
            )
        ],
        "permanent_card_reused_ids": [
            item["candidate_id"]
            for item in decisions
            if item["coverage_group"] in {"A", "B"}
            and item["card_version_id"] is not None
            and item["content_hash"] == item["card_content_hash"]
        ],
        "prompt_candidate_count": metrics["count"],
        "prompt_token_estimate": metrics["tokens"],
        "prompt_byte_estimate": metrics["bytes"],
        "paper_seeds": list(paper_seeds),
        "paper_candidate_orders": paper_orders,
        "created_at": datetime.now().astimezone().isoformat(),
    }
    if extra:
        payload.update(extra)
    return payload


def restore_frozen_past_candidates(
    payload: Mapping[str, Any],
    *,
    bank_index,
    subject,
    chapters,
    other_pool,
    other_directory,
    institution,
) -> list[dict]:
    """Rehydrate a final past manifest in its semantic candidate order."""

    identities = payload.get("candidate_identities")
    if not isinstance(identities, list) or not identities:
        raise ExamManifestError("MANIFEST_CORRUPT", "冻结真题候选身份缺失")
    ordinary_ids = []
    other_ids = []
    normalized = []
    for identity in identities:
        if not isinstance(identity, list) or len(identity) != 2:
            raise ExamManifestError("MANIFEST_CORRUPT", "冻结真题候选身份无效")
        source_kind = str(identity[0] or "").strip()
        candidate_id = str(identity[1] or "").strip()
        normalized_source = (
            "past_exam_other"
            if source_kind in {"past_exam", "past_exam_other"}
            else source_kind
        )
        if normalized_source == "ordinary":
            ordinary_ids.append(candidate_id)
        elif normalized_source == "past_exam_other":
            other_ids.append(candidate_id)
        else:
            raise ExamManifestError(
                "MANIFEST_CORRUPT",
                "冻结真题候选包含不支持的来源类型",
            )
        normalized.append((normalized_source, candidate_id))

    by_identity = {}
    if ordinary_ids:
        ordinary = bank_index.candidates_by_ids(
            ordinary_ids,
            subject=subject,
            chapters=chapters,
        )
        for item in ordinary:
            item["source_kind"] = "ordinary"
            by_identity[("ordinary", str(item["candidate_id"]))] = item
    if other_ids:
        other_by_id = {
            str(item.get("candidate_id")): item
            for item in (other_pool or [])
            if item.get("candidate_id")
        }
        missing = [item for item in other_ids if item not in other_by_id]
        if missing:
            restored = other_past_candidates_by_ids(
                other_directory,
                subject,
                institution,
                other_ids,
            )
            other_by_id.update({str(item["candidate_id"]): item for item in restored})
        for candidate_id in other_ids:
            item = other_by_id.get(candidate_id)
            if item is None:
                raise ExamManifestError(
                    "MANIFEST_SOURCE_DRIFT",
                    "冻结真题候选已从其他院校题源中消失",
                )
            item["source_kind"] = "past_exam"
            by_identity[("past_exam_other", candidate_id)] = item

    missing_identity = [identity for identity in normalized if identity not in by_identity]
    if missing_identity:
        raise ExamManifestError(
            "MANIFEST_SOURCE_DRIFT",
            "冻结真题候选无法从当前题源恢复",
        )
    return [by_identity[identity] for identity in normalized]


__all__ = [
    "build_candidate_freeze_payload",
    "candidate_decisions",
    "candidate_prompt_metrics",
    "restore_frozen_past_candidates",
    "validate_frozen_candidates",
]
