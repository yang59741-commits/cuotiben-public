"""Free-exam semantic decision and immutable candidate freeze for stage F3.2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .exam_generation_free_candidates import FreeCandidateCheckpointResult
from .exam_generation_freeze import (
    build_candidate_freeze_payload,
    candidate_decisions,
)
from .exam_manifests import (
    exam_manifest_payload,
    freeze_exam_generation_manifest,
)


@dataclass(frozen=True, slots=True)
class FreeSemanticFreezeRequest:
    subject: str
    chapters: tuple[str, ...]
    question_count: int
    note: str
    question_types: tuple[str, ...]
    difficulty: str
    difficulty_quota: Mapping[str, int]
    generation_job_id: str | None
    operation_id: str
    ai_task_freezes: Mapping[str, Any]
    semantic_settings_revision: int
    semantic_candidate_multiplier: int
    semantic_allow_partial_cache: bool


@dataclass(frozen=True, slots=True)
class FreeSemanticFreezeResult:
    """Serializable evidence handed from semantic freeze to material stage F3.3."""

    question_bank_snapshot: str
    question_bank_coverage: Mapping[str, Any]
    candidate_questions: tuple[Mapping[str, Any], ...]
    frozen_manifest: Mapping[str, Any] | None
    frozen_payload: Mapping[str, Any]
    versioned_manifest: bool
    recall_effective_mode: str
    fallback_reason: str | None
    unfrozen_candidate_decisions: tuple[Mapping[str, Any], ...] | None


def prepare_free_semantic_freeze(
    checkpoint: FreeCandidateCheckpointResult,
    request: FreeSemanticFreezeRequest,
    *,
    db: Any,
    database: str | Path,
    bank_index: Any,
    bank_scope: Any,
    has_background_job: Callable[[Any, str | None], bool],
    semantic_recall_decision: Callable[..., Any] | None,
    semantic_recall_shadow: Callable[..., Any] | None,
    logger: Any,
) -> FreeSemanticFreezeResult:
    """Decide free recall and freeze final candidates without material reads."""

    candidates = [dict(item) for item in checkpoint.candidate_questions]
    frozen_manifest = (
        dict(checkpoint.frozen_manifest)
        if checkpoint.frozen_manifest is not None
        else None
    )
    frozen_payload = dict(checkpoint.frozen_payload)
    versioned = checkpoint.versioned_manifest
    decision = None

    if versioned:
        recall_effective_mode = "frozen"
    else:
        decision_kwargs = {
            "subject": request.subject,
            "chapters": list(request.chapters),
            "question_count": request.question_count,
            "note": request.note,
            "question_types": list(request.question_types),
            "difficulty": request.difficulty,
            "chapter_quotas": {},
            "difficulty_quotas": dict(request.difficulty_quota),
            "hard_pool_loader": lambda: [
                {**item, "source_kind": "ordinary"}
                for item in bank_scope.candidates(
                    bank_index,
                    request.subject,
                    list(request.chapters),
                )
            ],
            "legacy_candidates": candidates,
            "recent_candidate_ids": set(checkpoint.decision_recent_ids),
            "required_source_counts": {"ordinary": request.question_count},
        }
        if semantic_recall_decision is None:
            if semantic_recall_shadow is not None:
                try:
                    semantic_recall_shadow(
                        **{
                            key: value
                            for key, value in decision_kwargs.items()
                            if key
                            not in {"recent_candidate_ids", "required_source_counts"}
                        }
                    )
                except Exception:
                    logger.exception("semantic recall shadow failed")
            recall_effective_mode = "shadow"
        else:
            decision = semantic_recall_decision(**decision_kwargs)
            if decision.fallback_category:
                logger.warning(
                    "semantic recall fallback category=%s projection_id=%s legacy_ids_hash=%s",
                    decision.fallback_category,
                    decision.projection_id,
                    decision.legacy_ids_hash,
                )
            candidates = [dict(item) for item in decision.effective_candidates]
            recall_effective_mode = str(decision.effective_mode)

        if has_background_job(db, request.generation_job_id):
            extra = {
                "subject": request.subject,
                "chapters": list(request.chapters),
                "difficulty_quota": dict(request.difficulty_quota),
                "ai_task_freezes": dict(request.ai_task_freezes),
            }
            if checkpoint.membership_policy_version is not None:
                extra["free_candidate_membership_policy_version"] = (
                    checkpoint.membership_policy_version
                )
            if checkpoint.history_signal_audit_marker is not None:
                extra["free_history_signal_audit_marker"] = (
                    checkpoint.history_signal_audit_marker
                )
            if checkpoint.unmastered_soft_exemption_marker is not None:
                # These are freeze-identity inputs, not scoring parameters:
                # raw IDs retain audit provenance and decision IDs replay the
                # exact legacy semantic soft-seam input selected at checkpoint
                # time. Candidate membership and hard-pool identity are
                # unchanged, and no semantic weight is introduced here.
                extra["free_unmastered_soft_exemption_marker"] = (
                    checkpoint.unmastered_soft_exemption_marker
                )
                extra["recent_question_ids"] = list(checkpoint.raw_recent_ids)
                extra["decision_recent_ids"] = list(
                    checkpoint.decision_recent_ids
                )
            frozen_manifest = freeze_exam_generation_manifest(
                database,
                request.generation_job_id,
                checkpoint.question_bank_snapshot,
                build_candidate_freeze_payload(
                    source="free",
                    decision=decision,
                    candidates=candidates,
                    question_bank_snapshot=checkpoint.question_bank_snapshot,
                    paper_seeds=[request.generation_job_id or request.operation_id],
                    semantic_settings_revision=request.semantic_settings_revision,
                    semantic_candidate_multiplier=request.semantic_candidate_multiplier,
                    semantic_allow_partial_cache=request.semantic_allow_partial_cache,
                    extra=extra,
                ),
            )
            frozen_payload = exam_manifest_payload(frozen_manifest)
            versioned = True

    unfrozen_decisions = None
    if request.generation_job_id is None and decision is not None:
        unfrozen_decisions = tuple(
            dict(item) for item in candidate_decisions(decision, candidates)
        )
    fallback_reason = (
        str(decision.fallback_category)
        if decision is not None and decision.fallback_category is not None
        else frozen_payload.get("fallback_reason")
    )
    if fallback_reason is not None:
        fallback_reason = str(fallback_reason)
    return FreeSemanticFreezeResult(
        question_bank_snapshot=checkpoint.question_bank_snapshot,
        question_bank_coverage=dict(checkpoint.question_bank_coverage),
        candidate_questions=tuple(dict(item) for item in candidates),
        frozen_manifest=frozen_manifest,
        frozen_payload=dict(frozen_payload),
        versioned_manifest=versioned,
        recall_effective_mode=recall_effective_mode,
        fallback_reason=fallback_reason,
        unfrozen_candidate_decisions=unfrozen_decisions,
    )


__all__ = [
    "FreeSemanticFreezeRequest",
    "FreeSemanticFreezeResult",
    "prepare_free_semantic_freeze",
]
