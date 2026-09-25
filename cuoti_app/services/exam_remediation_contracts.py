"""Frozen cross-stage contracts for the 2026-08-27 exam remediation.

This module is deliberately side-effect free.  Stage implementations import
these values instead of re-declaring transitional aliases or guessing which
legacy value is authoritative.
"""

from __future__ import annotations

from .exam_difficulty_policy import OverallDifficulty, QuestionDifficulty


SINGLE_PAPER_CONTRACT_VERSION = "exam-single-paper-v1"
EXAM_CHECKPOINT_SCHEMA_VERSION = 3
PAST_PROFILE_TARGET_SCHEMA_VERSION = 3
DEFAULT_DEADLINE_POLICY_MODE = "observe"
DEADLINE_POLICY_MODES = frozenset({"observe", "enforce"})


JOB_LIFECYCLE_STATES = frozenset(
    {
        "queued",
        "running",
        "checkpointed",
        "published_pending_receipt",
        "succeeded",
        "failed",
        "cancelled",
    }
)
JOB_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
JOB_STATE_TRANSITIONS = {
    "queued": frozenset({"running", "failed", "cancelled"}),
    "running": frozenset(
        {
            "checkpointed",
            "published_pending_receipt",
            "succeeded",
            "failed",
            "cancelled",
        }
    ),
    "checkpointed": frozenset({"running", "failed", "cancelled"}),
    "published_pending_receipt": frozenset({"succeeded"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


STABLE_EXAM_ERROR_CODES = frozenset(
    {
        "invalid_paper_count",
        "job_input_hash_mismatch",
        "legacy_exam_checkpoint_migration_pending",
        "legacy_multi_paper_checkpoint_unsupported",
        "exam_attempt_binding_conflict",
        "exam_profile_resolved_receipt_conflict",
        "exam_attempt_binding_version_unsupported",
        "exam_profile_resolved_receipt_version_unsupported",
        "exam_attempt_binding_hash_mismatch",
        "exam_profile_resolved_receipt_hash_mismatch",
        "published_recovery_required",
        "exam_publication_binding_fenced",
        "exam_attempt_binding_store_busy",
        "exam_attempt_binding_store_corrupt",
        "exam_attempt_binding_write_failed",
        "exam_stage3_contract_conflict",
        "exam_stage3_contract_version_unsupported",
        "exam_stage3_contract_hash_mismatch",
        "exam_stage3_contract_store_busy",
        "exam_stage3_contract_store_corrupt",
        "exam_stage3_contract_write_failed",
        "unsupported_profile_combination",
        "profile_v3_not_verified",
        "semantic_projection_unavailable",
        "invalid_difficulty_label",
        "context_safety_exceeded",
        "insufficient_question_type_coverage",
        "insufficient_chapter_coverage",
        "insufficient_mistake_coverage",
        "candidate_fact_unresolved",
        "exact_slot_infeasible",
        "global_matching_infeasible",
        "scoring_schema_invalid",
        "quality_gate_failed",
        "exam_deadline_exceeded",
        "publication_failed",
        "receipt_pending",
    }
)

ADAPTATION_NON_TERMINAL_REASON_CODES = frozenset(
    {
        "adaptation_verification_failed",
        "adaptation_schema_invalid",
        "adaptation_timeout",
    }
)


CHECKPOINT_V3_HASH_FIELDS = (
    "request_hash",
    "profile_hash",
    "projection_hash",
    "candidate_facts_hash",
    "recent_usage_snapshot_hash",
    "candidate_bucket_hash",
    "scoring_receipt_hash",
    "selection_hash",
    "pre_adaptation_exam_hash",
    "final_exam_hash",
)

SCORING_RECEIPT_IDENTITY_FIELDS = (
    "request_hash",
    "model_version",
    "prompt_version",
    "candidate_facts_hash",
    "profile_hash",
    "projection_hash",
    "scoring_schema_version",
)

SELECTION_HASH_FIELDS = (
    "position",
    "candidate_id",
    "candidate_fact_hash",
    "profile_hash",
    "candidate_bucket_hash",
)


__all__ = [
    "ADAPTATION_NON_TERMINAL_REASON_CODES",
    "CHECKPOINT_V3_HASH_FIELDS",
    "DEFAULT_DEADLINE_POLICY_MODE",
    "DEADLINE_POLICY_MODES",
    "EXAM_CHECKPOINT_SCHEMA_VERSION",
    "JOB_LIFECYCLE_STATES",
    "JOB_STATE_TRANSITIONS",
    "JOB_TERMINAL_STATES",
    "OverallDifficulty",
    "PAST_PROFILE_TARGET_SCHEMA_VERSION",
    "QuestionDifficulty",
    "SCORING_RECEIPT_IDENTITY_FIELDS",
    "SELECTION_HASH_FIELDS",
    "SINGLE_PAPER_CONTRACT_VERSION",
    "STABLE_EXAM_ERROR_CODES",
]
