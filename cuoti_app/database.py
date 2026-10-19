
"""SQLite lifecycle and idempotent schema initialization."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from flask import current_app, g

from .migrations import MIGRATIONS, migration_schema_issues
from .transactions import immediate_transaction
from .validation import mistake_fingerprint


DB_SCHEMA_VERSION = 65
SCHEMA_VERSION = DB_SCHEMA_VERSION  # Compatibility import for older tools/tests.
REQUIRED_TABLE_COLUMNS = {
    "daily_review_rounds": {
        "id", "round_number", "generation_mode", "status", "created_at",
        "completed_at", "source_job_id", "item_count", "published_at",
    },
    "daily_review_items": {
        "id", "round_id", "round_number", "mistake_id", "export_batch_id", "reserved_at",
        "status", "completion_result", "completed_at", "result_key", "source_job_id", "created_at",
    },
    "past_exam_profile_activation_bindings": {
        "activation_id", "combination_key", "profile_id", "profile_hash",
        "attestation_hash", "schema_version", "contract_version",
        "profile_json", "attestation_json",
    },
    "past_exam_profile_active_pointers": {
        "combination_key", "institution", "subject", "reference_year",
        "revision", "active_activation_id", "updated_at",
    },
    "past_exam_profile_activation_transitions": {
        "combination_key", "revision", "schema_version", "contract_version",
        "operation", "entry_revision_before", "entry_revision_after",
        "previous_activation_id", "active_activation_id", "profile_id",
        "profile_hash", "attestation_hash", "occurred_at", "receipt_hash",
        "receipt_json",
    },
    "ai_memory_cleanup": {
        "cleanup_id", "memory_type", "exam_id", "generation_job_id", "status",
        "attempts", "last_error_code", "last_error_summary", "created_at", "updated_at",
    },
    "daily_tips": {
        "id", "public_id", "content", "content_hash", "revision",
        "created_at", "updated_at",
    },
    "subjects": {"id", "name", "subject_code", "display_order"},
    "chapters": {
        "id", "subject_id", "name", "chapter_code", "display_order", "parent_code",
    },
    "mistakes": {
        "id", "public_id", "revision", "updated_at", "history_complete",
        "history_complete_from",
        "content_fingerprint", "content_fingerprint_version", "answer",
        "solution", "source_kind", "source_ref", "difficulty", "deleted_at",
        "source_exam_attempt_item_id",
        "review_stage", "review_cycle", "schedule_version",
    },
    "review_events": {
        "id", "public_id", "client_event_id", "created_at",
        "lapse_count_after", "source_import_id",
        "stage_before", "stage_after", "cycle_after", "next_review_after",
        "schedule_version",
    },
    "mistake_reason_items": {"mistake_id", "reason_code"},
    "tags": {"id", "name", "normalized_name"},
    "mistake_tags": {"mistake_id", "tag_id"},
    "attachments": {
        "id", "public_id", "mistake_id", "object_key", "mime_type",
        "extension", "byte_size", "width", "height", "sha256",
        "sort_order", "status", "created_at",
    },
    "mistake_exports": {"source_job_id", "batch_id", "volume_index", "layout"},
    "mistake_export_batches": {
        "id", "public_id", "created_at", "expires_at", "total_count",
        "volume_count", "layout", "filter_snapshot_json", "source_job_id",
        "artifact_status", "export_context",
    },
    "mistake_export_items": {
        "id", "export_id", "position", "mistake_public_id", "subject",
        "chapter", "qnum", "date_added", "reasons", "note", "content",
        "content_hash",
    },
    "background_jobs": {
        "id", "kind", "status", "input_hash", "attempts", "max_attempts",
        "lane", "priority", "attempts_in_cycle", "manual_retry_count",
        "progress_current", "progress_total", "progress_message", "result_json",
        "error_code", "error_message", "error_category", "error_retryable",
        "error_correlation_id", "error_details_json", "cancel_requested", "lease_owner",
        "lease_expires_at", "heartbeat_at", "available_at", "created_at",
        "updated_at", "started_at", "finished_at", "deadline_at",
        "deadline_policy_mode", "deadline_policy_version", "exam_timing_json",
    },
    "job_inputs": {
        "job_id", "payload_json", "payload_hash", "identity_version", "created_at",
    },
    "exam_generation_attempt_bindings": {
        "job_id", "binding_contract_version", "job_input_identity_version",
        "generation_request_hash_version", "single_paper_contract_version",
        "past_profile_freeze_contract_version", "profile_worker_context_version",
        "exact_slot_context_version", "logical_request_hash", "job_input_hash",
        "generation_request_hash", "frozen_ref_hash", "profile_hash",
        "exact_slot_hash", "resolution_hash", "binding_hash", "created_at",
    },
    "exam_generation_profile_resolved_receipts": {
        "job_id", "stage_name", "receipt_contract_version", "binding_hash",
        "resolution_hash", "receipt_hash", "created_at",
    },
    "exam_generation_stage4_fact_receipts": {
        "job_id", "receipt_contract_version", "candidate_facts_contract_version",
        "stage3_contract_hash", "stage3_receipt_hash", "projection_identity_version",
        "projection_id", "semantic_snapshot_id", "source_snapshot", "algorithm_version",
        "dictionary_version", "weights_version", "projection_build_hash",
        "projection_item_count", "projection_identity_hash", "target_institution",
        "candidate_pool_hash", "candidate_facts_hash", "unresolved_candidate_ids_hash",
        "candidate_count", "unresolved_count", "facts_payload_json", "receipt_hash", "created_at",
    },
    "exam_generation_stage4_candidate_facts": {
        "job_id", "candidate_facts_hash", "ordinal", "source_kind", "candidate_id",
        "subject", "question_type_family", "chapter", "source_exam_id", "section_id",
        "source_institution", "content_hash", "content_snapshot", "content_snapshot_hash",
        "card_version_id", "card_hash", "projection_document_hash", "difficulty_resolved",
        "difficulty_label", "difficulty_confidence", "difficulty_reason", "fact_json",
        "fact_hash", "created_at",
    },
    "exam_generation_stage4_difficulty_flights": {
        "flight_hash", "flight_contract_version", "resolution_contract_version",
        "candidate_facts_hash", "component_hash", "input_facts_hash", "rules_hash",
        "classification_model", "classification_prompt_version", "verification_model",
        "verification_prompt_version", "candidate_ids_hash", "candidate_count", "state",
        "revision", "claim_job_id", "claim_worker_id", "claim_attempt", "result_hash",
        "created_at", "updated_at",
    },
    "exam_generation_stage4_difficulty_round_receipts": {
        "flight_hash", "completed_from_revision", "round_name", "response_schema_version",
        "model", "prompt_version", "rules_hash", "input_facts_hash", "response_status",
        "response_hash", "receipt_hash", "created_at",
    },
    "exam_generation_stage4_difficulty_results": {
        "flight_hash", "completed_from_revision", "result_contract_version",
        "candidate_facts_hash", "component_hash", "input_facts_hash", "result_hash",
        "outcome_count", "result_payload_json", "receipt_hash", "created_at",
    },
    "exam_generation_stage4_difficulty_outcomes": {
        "flight_hash", "result_hash", "candidate_id", "status", "effective_difficulty",
        "reason", "classification_confidence", "verification_confidence", "evidence_hash",
        "outcome_hash", "created_at",
    },
    "exam_generation_stage4_difficulty_conflicts": {
        "conflict_id", "flight_hash", "observed_revision", "conflict_type", "candidate_id",
        "evidence_hash", "conflict_json", "created_at",
    },
    "job_exam_results": {"job_id", "exam_id", "position"},
    "job_attempts": {
        "id", "job_id", "attempt_number", "manual_retry_number", "worker_id",
        "lane", "started_at", "finished_at", "status", "error_code",
        "error_correlation_id",
    },
    "job_business_results": {"job_id", "kind", "result_json", "committed_at"},
    "import_previews": {
        "token_hash", "input_sha256", "database_revision", "mode", "plan_json",
        "expires_at", "used_at", "created_at",
    },
    "bulk_operations": {
        "operation_id", "input_hash", "status", "result_json", "created_at",
    },
    "bulk_previews": {
        "token_hash", "input_hash", "database_revision", "plan_json",
        "expires_at", "used_at", "created_at",
    },
    "exams": {
        "id", "subject_id", "title", "config", "content", "created_at",
        "generation_job_id", "batch_index", "artifact_status",
        "latex_source", "latex_sha256", "pdf_sha256", "artifact_revision",
        "recompile_available", "last_recompile_job_id",
        "scoring_mode", "total_points", "scoring_evidence_hash",
        "scoring_projection_hash", "deleted_at",
    },
    "exam_items": {
        "id", "public_id", "exam_id", "position", "question_type",
        "difficulty", "content", "content_hash", "points", "source_kind",
        "source_mistake_public_id", "source_candidate_id",
        "source_metadata_json", "created_at",
        "points_canonical", "points_evidence_hash",
    },
    "exam_item_mistake_lineage": {
        "exam_item_id", "source_order", "mistake_public_id",
        "source_lineage_hash", "lineage_policy_version",
        "content_fingerprint", "created_at",
    },
    "exam_attempts": {
        "id", "public_id", "exam_id", "status", "started_at", "submitted_at",
        "duration_seconds", "total_score", "max_score", "note", "created_at",
        "updated_at", "idempotency_key",
    },
    "daily_analysis_items": {
        "id", "mistake_id", "analysis_date", "analysis_schema_version",
        "prompt_version", "source_hash", "analysis_json", "source_job_id",
        "model_role", "model_name", "generated_at", "updated_at",
    },
    "daily_analysis_leases": {
        "mistake_id", "job_id", "generation_scope", "claimed_at",
    },
    "exam_attempt_requests": {
        "exam_id", "idempotency_key", "attempt_id", "created_at",
    },
    "exam_attempt_items": {
        "id", "public_id", "attempt_id", "exam_item_id", "result_status",
        "score", "comment", "returned_mistake_public_id", "created_at",
        "updated_at",
    },
    "statistics_metadata": {"id", "complete_since", "enabled_at"},
    "learning_daily_snapshots": {
        "id", "snapshot_day", "subject_id", "chapter_id", "total_count",
        "mastered_count", "active_count", "captured_at", "estimated",
        "learning_revision",
    },
    "app_revisions": {
        "id", "learning_revision", "statistics_revision", "updated_at",
    },
    "semantic_questions": {
        "id", "source_kind", "candidate_id", "subject", "chapter",
        "source_file", "source_year", "source_number", "content_hash",
        "metadata_hash", "token_estimate", "active", "excluded",
        "first_seen_snapshot", "last_seen_snapshot", "current_card_version_id",
        "created_at", "updated_at",
    },
    "semantic_card_versions": {
        "id", "semantic_question_id", "content_hash", "analysis_schema_version",
        "prompt_version", "model_role", "model_name", "reasoning_effort",
        "card_json", "normalized_terms_json", "card_hash", "status",
        "provider_request_id_hash", "analyzed_at", "superseded_at",
    },
    "semantic_index_runs": {
        "id", "run_kind", "source_snapshot", "status",
        "analysis_schema_version", "prompt_version", "model_role", "model_name",
        "reasoning_effort", "pending_count", "new_count", "changed_count",
        "stale_count", "metadata_only_count", "deleted_count", "completed_count",
        "failed_count", "reused_count", "requested_concurrency",
        "effective_concurrency", "token_initial_limit", "token_current_limit",
        "token_hard_limit", "token_consumed", "token_reserved",
        "token_consumed_unverified", "token_remaining", "token_extension_index",
        "token_next_extension", "token_last_extended_at", "token_stop_reason",
        "degrade_reason", "created_at", "started_at", "updated_at",
        "finished_at", "published_snapshot_id", "last_error_code",
        "last_error_summary", "control_requested", "control_requested_at",
    },
    "semantic_index_run_items": {
        "run_id", "semantic_question_id", "expected_content_hash", "change_kind",
        "status", "lease_owner", "lease_expires_at", "attempt_count",
        "available_at", "card_version_id", "error_code", "error_summary",
        "updated_at",
    },
    "semantic_index_snapshots": {
        "id", "source_snapshot", "run_id", "status", "eligible_count",
        "valid_card_count", "stale_card_count", "missing_card_count",
        "coverage_ratio", "analysis_schema_version", "identity_policy",
        "created_at", "published_at",
    },
    "semantic_index_snapshot_items": {
        "snapshot_id", "semantic_question_id", "card_version_id", "content_hash",
    },
    "semantic_index_token_events": {
        "id", "run_id", "event_type", "reservation_key", "extension_index",
        "previous_limit", "nominal_amount", "actual_amount", "new_limit",
        "token_delta_consumed", "token_delta_reserved", "token_delta_unverified",
        "consumed_after", "reserved_after", "unverified_after",
        "current_limit_after", "metadata_json", "created_at",
    },
    "semantic_index_provider_attempts": {
        "reservation_key", "run_id", "semantic_question_id", "item_attempt",
        "provider_attempt", "expected_content_hash", "prompt_hash",
        "request_fingerprint", "status", "usage_known", "usage_total_tokens",
        "provider_request_id_hash", "validated_card_json",
        "validated_card_hash", "error_code", "error_summary", "created_at",
        "updated_at", "dispatched_at", "responded_at", "checkpointed_at",
    },
    "semantic_question_deprecations": {
        "semantic_question_id", "reason_code", "reason_summary", "source_run_id",
        "source_shard_id", "created_at", "updated_at",
    },
    "semantic_index_snapshot_profiles": {
        "snapshot_id", "analysis_schema_version", "prompt_version", "model_role",
        "model_name", "reasoning_effort", "item_count", "created_at",
    },
    "semantic_recall_projection_versions": {
        "id", "snapshot_id", "algorithm_version", "dictionary_version",
        "weights_version", "status", "build_hash", "item_count", "created_at",
        "published_at",
    },
    "semantic_recall_documents": {
        "projection_id", "semantic_question_id", "card_version_id", "source_kind",
        "candidate_id", "subject", "chapter", "question_type", "difficulty",
        "similarity_group", "source_year", "source_file_key", "confidence",
        "content_hash", "normalized_text_hash",
    },
    "semantic_recall_terms": {
        "projection_id", "semantic_question_id", "field_name", "normalized_term",
        "weight",
    },
    "semantic_recall_shadow_runs": {
        "id", "request_hash", "source_snapshot", "projection_id", "algorithm_version",
        "dictionary_version", "weights_version", "hard_pool_count", "a_count",
        "b_count", "c_count", "d_count", "legacy_ids_hash", "recalled_ids_hash",
        "overlap_count", "added_count", "dropped_count", "partial_cache",
        "relaxed_quotas_json", "created_at",
    },
}
REQUIRED_INDEXES = {
    "idx_daily_review_rounds_status_created",
    "idx_daily_review_rounds_number",
    "idx_daily_review_items_round_status",
    "idx_daily_review_items_mistake_result",
    "idx_daily_review_items_reservation",
    "idx_daily_review_items_export_batch",
    "idx_daily_review_items_source_job",
    "ux_daily_review_items_round_mistake",
    "ux_daily_review_items_result_key",
    "ux_background_jobs_id_input_hash",
    "ux_job_inputs_job_payload_hash",
    "idx_past_exam_profile_bindings_combination",
    "idx_past_exam_profile_pointers_active",
    "idx_past_exam_profile_transitions_active",
    "idx_past_exam_profile_transitions_previous",
    "idx_ai_memory_cleanup_exam_type", "idx_ai_memory_cleanup_status",
    "idx_daily_tips_public_id", "idx_daily_tips_content_hash",
    "idx_background_jobs_claim", "idx_background_jobs_lane_claim",
    "idx_background_jobs_lease",
    "idx_background_jobs_kind_created",
    "idx_job_attempts_job",
    "idx_mistake_exports_source_job",
    "idx_mistakes_public_id",
    "idx_mistakes_active_updated",
    "idx_mistakes_active_id",
    "idx_review_events_public_id",
    "idx_review_events_client_event",
    "idx_attachments_mistake_sort",
    "idx_subjects_code",
    "idx_chapters_code",
    "idx_import_previews_expiry",
    "idx_bulk_previews_expiry",
    "idx_exam_items_exam_position",
    "idx_exam_attempts_exam_created",
    "idx_exam_attempts_exam_idempotency",
    "idx_exam_attempt_requests_attempt",
    "idx_exam_attempt_items_attempt",
    "idx_mistakes_source_attempt_item",
    "idx_mistake_exports_batch_volume",
    "idx_mistake_export_items_export_position",
    "idx_daily_analysis_date",
    "idx_daily_analysis_source_job",
    "idx_daily_analysis_leases_claimed",
    "idx_learning_snapshots_day",
    "idx_semantic_questions_identity",
    "idx_semantic_questions_active_scope",
    "idx_semantic_questions_content_hash",
    "idx_semantic_questions_last_seen",
    "idx_semantic_card_versions_unique",
    "idx_semantic_card_versions_question_status",
    "idx_semantic_card_versions_content_status",
    "idx_semantic_runs_status_created",
    "idx_semantic_runs_active_identity",
    "idx_semantic_run_items_claim",
    "idx_semantic_run_items_card",
    "idx_semantic_run_items_active_leases",
    "idx_semantic_snapshots_source_published",
    "idx_semantic_snapshot_items_question",
    "idx_semantic_snapshot_items_card",
    "idx_semantic_token_events_run_id",
    "idx_semantic_token_events_reservation",
    "idx_semantic_provider_attempts_status",
    "idx_semantic_provider_attempts_question",
    "idx_semantic_token_reservation_unique",
    "idx_semantic_token_terminal_unique",
    "idx_semantic_token_reconcile_unique",
    "idx_semantic_token_extension_unique",
    "idx_semantic_token_hard_stop_unique",
    "idx_semantic_question_deprecations_reason",
    "idx_semantic_snapshot_profiles_model",
    "idx_semantic_recall_documents_scope",
    "idx_semantic_recall_terms_lookup",
    "idx_semantic_recall_shadow_request",
}
REQUIRED_FOREIGN_KEYS = {
    "exam_generation_stage4_fact_receipts": {
        ("job_id", "exam_generation_stage3_contract_receipts", "job_id"),
        ("stage3_receipt_hash", "exam_generation_stage3_contract_receipts", "receipt_hash"),
    },
    "exam_item_mistake_lineage": {
        ("exam_item_id", "exam_items", "id"),
        ("mistake_public_id", "mistakes", "public_id"),
    },
    "exam_generation_stage4_candidate_facts": {
        ("job_id", "exam_generation_stage4_fact_receipts", "job_id"),
        ("candidate_facts_hash", "exam_generation_stage4_fact_receipts", "candidate_facts_hash"),
    },
    "exam_generation_stage4_difficulty_round_receipts": {
        ("flight_hash", "exam_generation_stage4_difficulty_flights", "flight_hash"),
    },
    "exam_generation_stage4_difficulty_results": {
        ("flight_hash", "exam_generation_stage4_difficulty_flights", "flight_hash"),
    },
    "exam_generation_stage4_difficulty_outcomes": {
        ("flight_hash", "exam_generation_stage4_difficulty_results", "flight_hash"),
        ("result_hash", "exam_generation_stage4_difficulty_results", "result_hash"),
    },
    "exam_generation_stage4_difficulty_conflicts": {
        ("flight_hash", "exam_generation_stage4_difficulty_flights", "flight_hash"),
    },
    "exam_generation_attempt_bindings": {
        ("job_id", "background_jobs", "id"),
        ("logical_request_hash", "background_jobs", "input_hash"),
        ("job_id", "job_inputs", "job_id"),
        ("job_input_hash", "job_inputs", "payload_hash"),
    },
    "exam_generation_profile_resolved_receipts": {
        ("job_id", "exam_generation_attempt_bindings", "job_id"),
        ("binding_hash", "exam_generation_attempt_bindings", "binding_hash"),
    },
    "past_exam_profile_active_pointers": {
        (
            "combination_key",
            "past_exam_profile_activation_bindings",
            "combination_key",
        ),
        (
            "active_activation_id",
            "past_exam_profile_activation_bindings",
            "activation_id",
        ),
    },
    "past_exam_profile_activation_transitions": {
        (
            "combination_key",
            "past_exam_profile_active_pointers",
            "combination_key",
        ),
        (
            "combination_key",
            "past_exam_profile_activation_bindings",
            "combination_key",
        ),
        (
            "previous_activation_id",
            "past_exam_profile_activation_bindings",
            "activation_id",
        ),
        (
            "active_activation_id",
            "past_exam_profile_activation_bindings",
            "activation_id",
        ),
        (
            "profile_id",
            "past_exam_profile_activation_bindings",
            "profile_id",
        ),
        (
            "profile_hash",
            "past_exam_profile_activation_bindings",
            "profile_hash",
        ),
        (
            "attestation_hash",
            "past_exam_profile_activation_bindings",
            "attestation_hash",
        ),
    },
    "semantic_questions": {
        ("current_card_version_id", "semantic_card_versions", "id"),
    },
    "semantic_card_versions": {
        ("semantic_question_id", "semantic_questions", "id"),
    },
    "semantic_index_runs": {
        ("published_snapshot_id", "semantic_index_snapshots", "id"),
    },
    "semantic_index_run_items": {
        ("run_id", "semantic_index_runs", "id"),
        ("semantic_question_id", "semantic_questions", "id"),
        ("card_version_id", "semantic_card_versions", "id"),
    },
    "semantic_index_snapshots": {
        ("run_id", "semantic_index_runs", "id"),
    },
    "semantic_index_snapshot_items": {
        ("snapshot_id", "semantic_index_snapshots", "id"),
        ("semantic_question_id", "semantic_questions", "id"),
        ("card_version_id", "semantic_card_versions", "id"),
    },
    "semantic_index_token_events": {
        ("run_id", "semantic_index_runs", "id"),
    },
    "semantic_index_provider_attempts": {
        ("run_id", "semantic_index_runs", "id"),
        ("semantic_question_id", "semantic_questions", "id"),
    },
    "semantic_question_deprecations": {
        ("semantic_question_id", "semantic_questions", "id"),
    },
    "semantic_index_snapshot_profiles": {
        ("snapshot_id", "semantic_index_snapshots", "id"),
    },
    "semantic_recall_projection_versions": {
        ("snapshot_id", "semantic_index_snapshots", "id"),
    },
    "semantic_recall_documents": {
        ("projection_id", "semantic_recall_projection_versions", "id"),
        ("semantic_question_id", "semantic_questions", "id"),
        ("card_version_id", "semantic_card_versions", "id"),
    },
    "semantic_recall_terms": {
        ("projection_id", "semantic_recall_projection_versions", "id"),
        ("semantic_question_id", "semantic_questions", "id"),
    },
    "semantic_recall_shadow_runs": {
        ("projection_id", "semantic_recall_projection_versions", "id"),
    },
    "exam_attempt_requests": {
        ("exam_id", "exams", "id"),
        ("attempt_id", "exam_attempts", "id"),
    },
    "daily_analysis_items": {
        ("mistake_id", "mistakes", "id"),
    },
    "daily_analysis_leases": {
        ("mistake_id", "mistakes", "id"),
    },
    "daily_review_items": {
        ("round_id", "daily_review_rounds", "id"),
        ("export_batch_id", "mistake_export_batches", "id"),
    },
}
REQUIRED_TRIGGERS = {
    "mistakes_fts_insert",
    "mistakes_fts_delete",
    "mistakes_fts_update",
    *{
        f"revision_{table}_{operation}"
        for table in (
            "mistakes", "review_events", "exam_attempts", "exam_attempt_items"
        )
        for operation in ("insert", "update", "delete")
    },
}
REQUIRED_ALWAYS_TRIGGERS = {
    "exam_generation_attempt_bindings_no_update",
    "exam_generation_profile_resolved_receipts_no_update",
    "exam_generation_stage4_fact_receipts_no_update",
    "exam_generation_stage4_candidate_facts_no_update",
    "exam_generation_stage4_difficulty_flights_identity_immutable",
    "exam_generation_stage4_difficulty_flights_no_delete",
    "exam_generation_stage4_difficulty_round_receipts_no_update",
    "exam_generation_stage4_difficulty_round_receipts_no_delete",
    "exam_generation_stage4_difficulty_results_no_update",
    "exam_generation_stage4_difficulty_results_no_delete",
    "exam_generation_stage4_difficulty_outcomes_no_update",
    "exam_generation_stage4_difficulty_outcomes_no_delete",
    "exam_generation_stage4_difficulty_conflicts_no_update",
    "exam_generation_stage4_difficulty_conflicts_no_delete",
    "past_exam_profile_bindings_no_update",
    "past_exam_profile_bindings_no_delete",
    "past_exam_profile_transitions_no_update",
    "past_exam_profile_transitions_no_delete",
}


class DatabaseSchemaError(RuntimeError):
    """Raised when startup migration cannot produce the required schema."""


ConnectionRole = Literal[
    "request",
    "background",
    "maintenance",
    "readonly",
    "backup",
    "initialization",
]

_ROLE_TIMEOUT_SECONDS: dict[ConnectionRole, float] = {
    "request": 5.0,
    "background": 30.0,
    "maintenance": 30.0,
    "readonly": 5.0,
    "backup": 30.0,
    "initialization": 30.0,
}


def connect_database(
    database: str | Path,
    *,
    role: ConnectionRole = "request",
    timeout: float | None = None,
) -> sqlite3.Connection:
    """Open SQLite with one explicit, role-based connection contract.

    WAL selection is intentionally absent here.  Changing journal mode takes a
    database lock and belongs to initialization, not the request hot path.
    """

    if role not in _ROLE_TIMEOUT_SECONDS:
        raise ValueError(f"unknown database connection role: {role}")
    effective_timeout = (
        _ROLE_TIMEOUT_SECONDS[role] if timeout is None else max(0.0, float(timeout))
    )
    value = str(database)
    readonly = role in {"readonly", "backup"}
    if readonly and value != ":memory:" and not value.startswith("file:"):
        value = Path(value).expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        value,
        timeout=effective_timeout,
        uri=value.startswith("file:"),
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={int(effective_timeout * 1000)}")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    return connection


def ensure_wal_mode(db: sqlite3.Connection) -> str:
    """Set and verify the durable runtime journal mode at initialization."""

    mode = str(db.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
    # SQLite in-memory databases report ``memory`` and cannot use WAL.
    if mode not in {"wal", "memory"}:
        raise DatabaseSchemaError(f"数据库日志模式为 {mode}，期望 WAL")
    return mode


def schema_issues(db: sqlite3.Connection) -> list[str]:
    """Return actionable schema differences after migrations have run."""

    issues: list[str] = []
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table, required_columns in REQUIRED_TABLE_COLUMNS.items():
        if table not in tables:
            issues.append(f"缺少表 {table}")
            continue
        columns = {
            row[1] for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for column in sorted(required_columns - columns):
            issues.append(f"表 {table} 缺少字段 {column}")
    indexes = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    for index in sorted(REQUIRED_INDEXES - indexes):
        issues.append(f"缺少索引 {index}")
    for table, required_foreign_keys in REQUIRED_FOREIGN_KEYS.items():
        if table not in tables:
            continue
        foreign_keys = {
            (row[3], row[2], row[4])
            for row in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        }
        for source_column, target_table, target_column in sorted(
            required_foreign_keys - foreign_keys
        ):
            issues.append(
                f"表 {table} 缺少外键 "
                f"{source_column}->{target_table}.{target_column}"
            )
    triggers = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    for trigger in sorted(REQUIRED_ALWAYS_TRIGGERS - triggers):
        issues.append(f"缺少触发器 {trigger}")
    if "mistakes_fts" in tables:
        for trigger in sorted(REQUIRED_TRIGGERS - triggers):
            issues.append(f"缺少触发器 {trigger}")
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version != SCHEMA_VERSION:
        issues.append(f"数据库版本为 {version}，期望 {SCHEMA_VERSION}")
    else:
        for issue in migration_schema_issues(db, SCHEMA_VERSION):
            if issue not in issues:
                issues.append(issue)
    return issues


_OPTIONAL_FTS_TRIGGERS = {
    "mistakes_fts_insert": (
        "CREATE TRIGGER mistakes_fts_insert AFTER INSERT ON mistakes "
        "WHEN new.deleted_at IS NULL BEGIN "
        "INSERT INTO mistakes_fts(rowid,qnum,note,content) "
        "VALUES(new.id,new.qnum,new.note,new.content); END"
    ),
    "mistakes_fts_delete": (
        "CREATE TRIGGER mistakes_fts_delete AFTER DELETE ON mistakes "
        "WHEN old.deleted_at IS NULL BEGIN "
        "INSERT INTO mistakes_fts(mistakes_fts,rowid,qnum,note,content) "
        "VALUES('delete',old.id,old.qnum,old.note,old.content); END"
    ),
    "mistakes_fts_update": (
        "CREATE TRIGGER mistakes_fts_update AFTER UPDATE ON mistakes "
        "BEGIN "
        "INSERT INTO mistakes_fts(mistakes_fts,rowid,qnum,note,content) "
        "SELECT 'delete',old.id,old.qnum,old.note,old.content "
        "WHERE old.deleted_at IS NULL; "
        "INSERT INTO mistakes_fts(rowid,qnum,note,content) "
        "SELECT new.id,new.qnum,new.note,new.content "
        "WHERE new.deleted_at IS NULL; END"
    ),
}


def _normalized_schema_sql(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def _optional_fts_triggers_match(db: sqlite3.Connection) -> bool:
    rows = db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND name IN (?,?,?)",
        tuple(_OPTIONAL_FTS_TRIGGERS),
    ).fetchall()
    actual = {str(row["name"]): _normalized_schema_sql(row["sql"]) for row in rows}
    return actual == {
        name: _normalized_schema_sql(statement)
        for name, statement in _OPTIONAL_FTS_TRIGGERS.items()
    }


def _ensure_optional_fts(db: sqlite3.Connection) -> bool:
    """Create the trigram search index when this SQLite build supports it."""

    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mistakes_fts'"
    ).fetchone()
    if exists and _optional_fts_triggers_match(db):
        return True

    db.execute("SAVEPOINT optional_fts")
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mistakes_fts USING fts5("
            "qnum,note,content,content='mistakes',content_rowid='id',tokenize='trigram'"
            ")"
        )
        db.execute("DROP TRIGGER IF EXISTS mistakes_fts_insert")
        db.execute("DROP TRIGGER IF EXISTS mistakes_fts_delete")
        db.execute("DROP TRIGGER IF EXISTS mistakes_fts_update")
        for statement in _OPTIONAL_FTS_TRIGGERS.values():
            db.execute(statement)
        if not exists:
            db.execute("INSERT INTO mistakes_fts(mistakes_fts) VALUES('rebuild')")
        db.execute("RELEASE optional_fts")
        return True
    except sqlite3.OperationalError as exc:
        db.execute("ROLLBACK TO optional_fts")
        db.execute("RELEASE optional_fts")
        message = str(exc).lower()
        if (
            "no such module" in message
            or "no such tokenizer" in message
            or "error in tokenizer constructor" in message
            or "expression tree is too large" in message
        ):
            return False
        raise




def _run_migration_step(db: sqlite3.Connection, version: int) -> int:
    """Run one registered upgrade as a single explicit transaction."""
    from .services.runtime_diagnostics import diagnostic_event
    migration = MIGRATIONS.get(version)
    if migration is None:
        raise DatabaseSchemaError(f"缺少从数据库版本 {version} 开始的迁移")
    target, function = migration
    diagnostic_event("migration_start", from_version=version, to_version=target)
    foreign_keys_off = bool(getattr(function, "requires_foreign_keys_off", False))
    if foreign_keys_off:
        db.execute("PRAGMA foreign_keys=OFF")
    try:
        with immediate_transaction(db):
            function(db)
            db.execute(f"PRAGMA user_version={target}")
            step_issues = migration_schema_issues(db, target)
            validation_tables = getattr(function, 'validation_tables', ())
            if validation_tables:
                quick = 'ok'
                foreign = []
                for table in validation_tables:
                    result = db.execute(f'PRAGMA quick_check("{table}")').fetchone()[0]
                    if result != 'ok':
                        quick = result
                    foreign.extend(db.execute(f'PRAGMA foreign_key_check("{table}")').fetchall())
            else:
                quick = db.execute("PRAGMA quick_check").fetchone()[0]
                foreign = db.execute("PRAGMA foreign_key_check").fetchall()
            if step_issues or quick != "ok" or foreign:
                details = "；".join(step_issues)
                raise DatabaseSchemaError(
                    f"数据库 {version}→{target} 迁移校验失败"
                    + (f"：{details}" if details else "")
                )
    finally:
        if foreign_keys_off:
            db.execute("PRAGMA foreign_keys=ON")
    diagnostic_event("migration_end", from_version=version, to_version=target)
    return target


def _run_migrations(db: sqlite3.Connection) -> None:
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > DB_SCHEMA_VERSION:
        raise DatabaseSchemaError(
            f"数据库版本 {version} 高于当前应用支持的 {DB_SCHEMA_VERSION}"
        )
    while version < DB_SCHEMA_VERSION:
        version = _run_migration_step(db, version)


def _ensure_identity_triggers(db: sqlite3.Connection) -> None:
    uuid_sql = (
        "lower(hex(randomblob(4)))||'-'||lower(hex(randomblob(2)))||'-4'||"
        "substr(lower(hex(randomblob(2))),2)||'-'||"
        "substr('89ab',abs(random()) % 4 + 1,1)||"
        "substr(lower(hex(randomblob(2))),2)||'-'||lower(hex(randomblob(6)))"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS mistakes_assign_public_id "
        "AFTER INSERT ON mistakes WHEN new.public_id IS NULL OR new.public_id='' "
        f"BEGIN UPDATE mistakes SET public_id={uuid_sql} WHERE id=new.id; END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS review_events_assign_public_id "
        "AFTER INSERT ON review_events WHEN new.public_id IS NULL OR new.public_id='' "
        f"BEGIN UPDATE review_events SET public_id={uuid_sql} WHERE id=new.id; END"
    )


def get_db():
    if "db" not in g:
        g.db = connect_database(current_app.config["DATABASE"], role="request")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _ensure_v6_runtime_indexes(db: sqlite3.Connection) -> None:
    """Repair baseline indexes without entering the pre-v6 script bootstrap."""

    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_claim "
        "ON background_jobs(status,available_at,created_at)",
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_lease "
        "ON background_jobs(status,lease_expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_kind_created "
        "ON background_jobs(kind,created_at)",
    ):
        db.execute(statement)


def init_db():
    database_path = Path(current_app.config["DATABASE"]).resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    db = connect_database(database_path, role="initialization")
    ensure_wal_mode(db)
    starting_version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if starting_version > DB_SCHEMA_VERSION:
        db.close()
        raise DatabaseSchemaError(
            f"数据库版本 {starting_version} 高于当前应用支持的 {DB_SCHEMA_VERSION}"
        )
    if starting_version >= 6:
        # Published forward upgrades are deliberately isolated from the legacy
        # bootstrap below. In particular, they must never pass through
        # sqlite3.executescript(), whose implicit commit breaks step atomicity.
        try:
            _run_migrations(db)
            db.execute(
                "UPDATE mistakes SET correct_streak=0 "
                "WHERE status='未掌握' AND correct_streak IS NULL"
            )
            catalog = current_app.extensions.get("catalog_service")
            if catalog is None:
                from .services.catalog import load_default_catalog
                catalog = load_default_catalog()
            catalog.sync_database(db)
            missing_fingerprints = db.execute(
                "SELECT m.id,m.qnum,m.content,m.date_added,s.name AS subject,"
                "c.name AS chapter FROM mistakes m "
                "LEFT JOIN subjects s ON s.id=m.subject_id "
                "LEFT JOIN chapters c ON c.id=m.chapter_id "
                "WHERE m.fingerprint IS NULL OR m.fingerprint=''"
            ).fetchall()
            for row in missing_fingerprints:
                db.execute(
                    "UPDATE mistakes SET fingerprint=? WHERE id=?",
                    (
                        mistake_fingerprint(
                            row["subject"], row["chapter"], row["qnum"],
                            row["content"], row["date_added"],
                        ),
                        row["id"],
                    ),
                )
            _ensure_v6_runtime_indexes(db)
            _ensure_identity_triggers(db)
            _ensure_optional_fts(db)
            db.commit()
            issues = schema_issues(db)
        finally:
            db.close()
        if issues:
            details = "；".join(issues)
            raise DatabaseSchemaError(
                f"数据库迁移后校验失败：{database_path}；{details}"
            )
        return
    # Only new/unversioned legacy databases use this pre-v6 bootstrap. All
    # published v6+ upgrades take the explicit transactional path above.
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS subjects(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            subject_code TEXT,
            display_order INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS chapters(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            chapter_code TEXT,
            display_order INTEGER NOT NULL DEFAULT 0,
            parent_code TEXT,
            UNIQUE(subject_id, name),
            FOREIGN KEY(subject_id) REFERENCES subjects(id)
        );
        CREATE TABLE IF NOT EXISTS mistakes(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            chapter_id INTEGER,
            qnum TEXT NOT NULL,
            reasons TEXT,
            note TEXT,
            date_added TEXT NOT NULL,
            status TEXT DEFAULT '未掌握',
            review_count INTEGER DEFAULT 0,
            last_review TEXT,
            next_review TEXT,
            content TEXT DEFAULT '',
            correct_streak INTEGER NOT NULL DEFAULT 0,
            lapse_count INTEGER NOT NULL DEFAULT 0,
            fingerprint TEXT,
            source_job_id TEXT,
            source_job_index INTEGER,
            FOREIGN KEY(subject_id) REFERENCES subjects(id),
            FOREIGN KEY(chapter_id) REFERENCES chapters(id)
        );
        CREATE TABLE IF NOT EXISTS review_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            reviewed_at TEXT NOT NULL,
            status_after TEXT NOT NULL,
            correct_streak INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS exams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            title TEXT NOT NULL,
            config TEXT,
            content TEXT,
            created_at TEXT NOT NULL,
            generation_job_id TEXT,
            batch_index INTEGER,
            artifact_status TEXT NOT NULL DEFAULT 'published'
        );
        CREATE TABLE IF NOT EXISTS mistake_exports(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK(item_count BETWEEN 1 AND 100),
            items_json TEXT NOT NULL,
            artifact_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(artifact_status IN ('pending','published'))
        );
        CREATE TABLE IF NOT EXISTS background_jobs(
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK(status IN (
                    'queued','running','cancel_requested','cancelled',
                    'succeeded','failed'
                )),
            idempotency_key TEXT,
            input_hash TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
            progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
            progress_total INTEGER CHECK(progress_total IS NULL OR progress_total >= 0),
            progress_message TEXT NOT NULL DEFAULT '',
            result_json TEXT,
            error_code TEXT,
            error_message TEXT,
            error_details_json TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0
                CHECK(cancel_requested IN (0,1)),
            lease_owner TEXT,
            lease_expires_at TEXT,
            heartbeat_at TEXT,
            available_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            UNIQUE(kind,idempotency_key)
        );
        CREATE TABLE IF NOT EXISTS job_inputs(
            job_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS job_exam_results(
            job_id TEXT NOT NULL,
            exam_id INTEGER NOT NULL,
            position INTEGER NOT NULL DEFAULT 1 CHECK(position >= 1),
            PRIMARY KEY(job_id,exam_id),
            UNIQUE(job_id,position),
            FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE,
            FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS exam_generation_manifests(
            job_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
        );
        """
    )
    columns = {
        row["name"] for row in db.execute("PRAGMA table_info(mistakes)").fetchall()
    }
    if "correct_streak" not in columns:
        db.execute(
            "ALTER TABLE mistakes ADD COLUMN correct_streak INTEGER NOT NULL DEFAULT 0"
        )
    if "lapse_count" not in columns:
        db.execute(
            "ALTER TABLE mistakes ADD COLUMN lapse_count INTEGER NOT NULL DEFAULT 0"
        )
    if "fingerprint" not in columns:
        db.execute("ALTER TABLE mistakes ADD COLUMN fingerprint TEXT")
    if "source_job_id" not in columns:
        db.execute("ALTER TABLE mistakes ADD COLUMN source_job_id TEXT")
    if "source_job_index" not in columns:
        db.execute("ALTER TABLE mistakes ADD COLUMN source_job_index INTEGER")
    exam_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(exams)").fetchall()
    }
    if "generation_job_id" not in exam_columns:
        db.execute("ALTER TABLE exams ADD COLUMN generation_job_id TEXT")
    if "batch_index" not in exam_columns:
        db.execute("ALTER TABLE exams ADD COLUMN batch_index INTEGER")
    if "artifact_status" not in exam_columns:
        db.execute(
            "ALTER TABLE exams ADD COLUMN artifact_status TEXT NOT NULL DEFAULT 'published'"
        )

    # Only pre-field databases need this legacy conversion; zero is valid for
    # a current manually-mastered mistake and must survive subsequent starts.
    if "correct_streak" not in columns:
        db.execute(
            "UPDATE mistakes SET correct_streak=3 WHERE status='已掌握'"
        )
    db.execute(
        "UPDATE mistakes SET correct_streak=0 "
        "WHERE status='未掌握' AND correct_streak IS NULL"
    )
    cursor = db.cursor()
    catalog = current_app.extensions.get("catalog_service")
    if catalog is None:
        from .services.catalog import load_default_catalog
        catalog = load_default_catalog()
    catalog.sync_database(db)

    missing_fingerprints = db.execute(
        "SELECT m.id,m.qnum,m.content,m.date_added,s.name AS subject,"
        "c.name AS chapter FROM mistakes m "
        "LEFT JOIN subjects s ON s.id=m.subject_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id "
        "WHERE m.fingerprint IS NULL OR m.fingerprint=''"
    ).fetchall()
    for row in missing_fingerprints:
        cursor.execute(
            "UPDATE mistakes SET fingerprint=? WHERE id=?",
            (
                mistake_fingerprint(
                    row["subject"],
                    row["chapter"],
                    row["qnum"],
                    row["content"],
                    row["date_added"],
                ),
                row["id"],
            ),
        )

    db.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_mistakes_status_next_review
            ON mistakes(status,next_review);
        CREATE INDEX IF NOT EXISTS idx_mistakes_subject ON mistakes(subject_id);
        CREATE INDEX IF NOT EXISTS idx_mistakes_chapter ON mistakes(chapter_id);
        CREATE INDEX IF NOT EXISTS idx_mistakes_date_added ON mistakes(date_added);
        CREATE INDEX IF NOT EXISTS idx_mistakes_fingerprint ON mistakes(fingerprint);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_mistakes_source_job
            ON mistakes(source_job_id,source_job_index)
            WHERE source_job_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_review_events_mistake_date
            ON review_events(mistake_id,reviewed_at);
        CREATE INDEX IF NOT EXISTS idx_review_events_date_status
            ON review_events(reviewed_at,status_after);
        CREATE INDEX IF NOT EXISTS idx_review_events_day_status
            ON review_events(substr(reviewed_at,1,10),status_after);
        CREATE INDEX IF NOT EXISTS idx_mistakes_date_status
            ON mistakes(date_added,status);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_claim
            ON background_jobs(status,available_at,created_at);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_lease
            ON background_jobs(status,lease_expires_at);
        CREATE INDEX IF NOT EXISTS idx_background_jobs_kind_created
            ON background_jobs(kind,created_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_exams_generation_job
            ON exams(generation_job_id,batch_index)
            WHERE generation_job_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_mistake_exports_status_expires
            ON mistake_exports(artifact_status,expires_at,id);
        """
    )
    if starting_version < 6:
        db.execute("PRAGMA user_version=6")
    db.commit()
    try:
        _run_migrations(db)
        _ensure_identity_triggers(db)
        _ensure_optional_fts(db)
        db.commit()
        issues = schema_issues(db)
    finally:
        db.close()
    if issues:
        details = "；".join(issues)
        raise DatabaseSchemaError(
            f"数据库迁移后校验失败：{database_path}；{details}"
        )


def init_app(app):
    app.teardown_appcontext(close_db)
