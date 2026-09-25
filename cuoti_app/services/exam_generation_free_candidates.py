"""Free-exam candidate checkpoint preparation for generation stage F3.1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_source_context import read_optional_recent_question_ids
from .exam_generation_freeze import validate_frozen_candidates
from .exam_manifests import (
    exam_manifest_payload,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
    manifest_stage,
    merge_exam_generation_manifest,
)
from .exam_repetition_policy import (
    normalize_question_content,
    question_content_fingerprint,
)
from .semantic_recall import validate_frozen_recall_contract


FREE_CANDIDATE_MEMBERSHIP_POLICY_VERSION = "free-candidate-membership-v2"
FREE_CROSS_JOB_HISTORY_AUDIT_MARKER = "cross-job-history-soft-signal-only-v1"
# This marker makes the checkpoint interpretation explicit: raw recent IDs are
# audit evidence, while decision_recent_ids is the immutable input to the
# pre-existing semantic recent-ID soft seam. Exact content fingerprints from
# current unmastered mistakes may remove an ID from that soft input only; they
# never change candidate membership or the hard pool. Missing markers/fields
# identify an older checkpoint and are replayed read-only without derivation.
FREE_UNMASTERED_SOFT_EXEMPTION_MARKER = (
    "unmastered-exact-content-cancels-recent-soft-signal-v1"
)


@dataclass(frozen=True, slots=True)
class FreeCandidateCheckpointFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class FreeCandidateCheckpointResult:
    """Serializable evidence handed from F3.1 to semantic decision F3.2.

    ``raw_recent_ids`` preserves the memory read for audit. Under
    ``unmastered_soft_exemption_marker``, ``decision_recent_ids`` freezes the
    derived IDs passed to the existing semantic recent-ID soft seam. Neither
    field is a candidate-membership or hard-pool filter. The marker plus both
    ID lists enter the final freeze identity; old checkpoints lacking them are
    read-only compatible and are not re-derived from current mastery state.
    """

    question_bank_snapshot: str
    question_bank_coverage: Mapping[str, Any]
    candidate_questions: tuple[Mapping[str, Any], ...]
    frozen_manifest: Mapping[str, Any] | None
    frozen_payload: Mapping[str, Any]
    versioned_manifest: bool
    decision_recent_ids: tuple[str, ...]
    membership_policy_version: str | None = None
    history_signal_audit_marker: str | None = None
    raw_recent_ids: tuple[str, ...] = ()
    unmastered_soft_exemption_marker: str | None = None


@dataclass(frozen=True, slots=True)
class FreeCandidateCheckpointOutcome:
    result: FreeCandidateCheckpointResult | None = None
    failure: FreeCandidateCheckpointFailure | None = None


def _candidate_ids(candidates: list[Mapping[str, Any]]) -> list[str]:
    return [str(item.get("candidate_id")) for item in candidates]


def _decision_recent_ids(
    candidates: list[Mapping[str, Any]],
    recent_ids: set[str],
    unmastered_fingerprints: tuple[str, ...],
) -> tuple[str, ...]:
    """Derive the semantic recent soft signal from frozen candidate content.

    A candidate is exempt only when its non-empty frozen body has an exact
    fingerprint in the supplied current unmastered set. Unknown or missing
    content is deliberately left recent. This function does not alter the
    candidate sequence, hard pool, or the legacy semantic seam's weights.
    """

    unmastered = {
        str(value).strip() for value in unmastered_fingerprints if str(value).strip()
    }
    exempt_ids: set[str] = set()
    if unmastered:
        for candidate in candidates:
            candidate_id = str(candidate.get("candidate_id") or "").strip()
            content = candidate.get("content")
            if (
                candidate_id in recent_ids
                and normalize_question_content(content)
                and question_content_fingerprint(content) in unmastered
            ):
                exempt_ids.add(candidate_id)
    return tuple(sorted(recent_ids - exempt_ids))


def _restore_winner(
    manifest: Mapping[str, Any],
    *,
    bank_index: Any,
    subject: str,
    chapters: tuple[str, ...],
) -> tuple[dict[str, Any], list[Mapping[str, Any]]]:
    payload = exam_manifest_payload(manifest)
    candidates = bank_index.candidates_by_ids(
        payload.get("candidate_ids") or [],
        subject=subject,
        chapters=list(chapters),
    )
    validate_frozen_candidates(payload, candidates)
    return payload, candidates


def prepare_free_candidate_checkpoint(
    *,
    db: Any,
    database: str,
    memory: Any,
    bank_index: Any,
    bank_scope: Any,
    generation_job_id: str | None,
    subject: str,
    chapters: tuple[str, ...],
    question_count: int,
    ai_task_freezes: Mapping[str, Any],
    indexed_candidates: Callable[..., tuple[list[Mapping[str, Any]], Mapping[str, Any]]],
    has_background_job: Callable[[Any, str | None], bool],
    candidate_seed: Callable[[], str],
    unmastered_fingerprints: tuple[str, ...] = (),
) -> FreeCandidateCheckpointOutcome:
    """Freeze membership, raw history, and derived semantic soft IDs.

    For a new checkpoint, candidate membership remains the full indexed pool.
    ``recent_question_ids`` records raw cross-job history; the explicitly
    marked ``decision_recent_ids`` excludes only exact-content unmastered
    candidates before calling the existing semantic soft seam. Persisting the
    derived list prevents retry drift when memory or mastery later changes.
    Existing or concurrent checkpoints without the new marker/field are never
    upgraded or re-derived here, preserving their membership and behavior.
    """

    snapshot = str(bank_index.snapshot_id)
    loaded = (
        load_exam_generation_manifest(database, generation_job_id)
        if generation_job_id
        else None
    )
    if loaded and loaded.get("snapshot_id") != snapshot:
        return FreeCandidateCheckpointOutcome(
            failure=FreeCandidateCheckpointFailure(
                {"error": "题库索引已变化，无法使用该任务的冻结组卷清单"},
                409,
            )
        )

    payload = exam_manifest_payload(loaded)
    versioned = is_versioned_exam_manifest(loaded)
    stage = manifest_stage(loaded)
    if versioned:
        validate_frozen_recall_contract(db, payload)

    membership_policy_version = payload.get("free_candidate_membership_policy_version")
    if membership_policy_version is not None:
        membership_policy_version = str(membership_policy_version)
    history_signal_audit_marker = payload.get("free_history_signal_audit_marker")
    if history_signal_audit_marker is not None:
        history_signal_audit_marker = str(history_signal_audit_marker)
    unmastered_soft_exemption_marker = payload.get(
        "free_unmastered_soft_exemption_marker"
    )
    if unmastered_soft_exemption_marker is not None:
        unmastered_soft_exemption_marker = str(unmastered_soft_exemption_marker)
    if loaded is None:
        membership_policy_version = FREE_CANDIDATE_MEMBERSHIP_POLICY_VERSION
        history_signal_audit_marker = FREE_CROSS_JOB_HISTORY_AUDIT_MARKER
        unmastered_soft_exemption_marker = FREE_UNMASTERED_SOFT_EXEMPTION_MARKER

    frozen_candidate_ids = payload.get("candidate_ids")
    coverage = bank_scope.coverage(
        bank_index,
        subject=subject,
        chapters=list(chapters),
    )
    if frozen_candidate_ids:
        candidates = bank_index.candidates_by_ids(
            frozen_candidate_ids,
            subject=subject,
            chapters=list(chapters),
        )
    else:
        candidates, coverage = indexed_candidates(
            bank_index,
            subject,
            list(chapters),
            question_count,
            generation_job_id or candidate_seed(),
            coverage_result=coverage,
        )
    if versioned:
        validate_frozen_candidates(payload, candidates)

    stored_recent_ids = (
        payload.get("recent_question_ids")
        if stage == "candidate-decision-pending-v1"
        else None
    )
    if isinstance(stored_recent_ids, list):
        recent_ids = {
            str(value) for value in stored_recent_ids if str(value).strip()
        }
    else:
        recent_ids = (
            read_optional_recent_question_ids(memory, "free", "free_exam")
            if not versioned
            else set()
        )

    stored_decision_recent_ids = (
        payload.get("decision_recent_ids")
        if stage == "candidate-decision-pending-v1"
        else None
    )
    has_frozen_decision_recent_ids = isinstance(stored_decision_recent_ids, list)
    if has_frozen_decision_recent_ids:
        frozen_decision_recent_ids = tuple(
            sorted(
                {
                    str(value)
                    for value in stored_decision_recent_ids
                    if str(value).strip()
                }
            )
        )
    else:
        frozen_decision_recent_ids = ()

    new_soft_policy = (
        unmastered_soft_exemption_marker
        == FREE_UNMASTERED_SOFT_EXEMPTION_MARKER
    )
    derived_decision_recent_ids = (
        _decision_recent_ids(candidates, recent_ids, unmastered_fingerprints)
        if loaded is None and new_soft_policy
        else frozen_decision_recent_ids
    )

    if has_background_job(db, generation_job_id) and not versioned and loaded is None:
        loaded = merge_exam_generation_manifest(
            database,
            generation_job_id,
            snapshot,
            {
                "manifest_stage": "candidate-decision-pending-v1",
                "source": "free",
                "subject": subject,
                "chapters": list(chapters),
                "candidate_ids": _candidate_ids(candidates),
                "recent_question_ids": sorted(recent_ids),
                "decision_recent_ids": list(derived_decision_recent_ids),
                "free_candidate_membership_policy_version": (
                    FREE_CANDIDATE_MEMBERSHIP_POLICY_VERSION
                ),
                "free_history_signal_audit_marker": (
                    FREE_CROSS_JOB_HISTORY_AUDIT_MARKER
                ),
                "free_unmastered_soft_exemption_marker": (
                    FREE_UNMASTERED_SOFT_EXEMPTION_MARKER
                ),
                "ai_task_freezes": dict(ai_task_freezes),
            },
        )
        payload = exam_manifest_payload(loaded)
        versioned = is_versioned_exam_manifest(loaded)
        if versioned:
            recent_ids = set()
            payload, candidates = _restore_winner(
                loaded,
                bank_index=bank_index,
                subject=subject,
                chapters=chapters,
            )
            membership_policy_version = payload.get(
                "free_candidate_membership_policy_version"
            )
            history_signal_audit_marker = payload.get(
                "free_history_signal_audit_marker"
            )
            unmastered_soft_exemption_marker = payload.get(
                "free_unmastered_soft_exemption_marker"
            )
        else:
            membership_policy_version = payload.get(
                "free_candidate_membership_policy_version"
            )
            history_signal_audit_marker = payload.get(
                "free_history_signal_audit_marker"
            )
            unmastered_soft_exemption_marker = payload.get(
                "free_unmastered_soft_exemption_marker"
            )
            returned_recent_ids = payload.get("recent_question_ids")
            if isinstance(returned_recent_ids, list):
                recent_ids = {
                    str(value)
                    for value in returned_recent_ids
                    if str(value).strip()
                }
            returned_decision_recent_ids = payload.get("decision_recent_ids")
            has_frozen_decision_recent_ids = isinstance(
                returned_decision_recent_ids, list
            )
            if has_frozen_decision_recent_ids:
                frozen_decision_recent_ids = tuple(
                    sorted(
                        {
                            str(value)
                            for value in returned_decision_recent_ids
                            if str(value).strip()
                        }
                    )
                )

    new_membership_policy = (
        membership_policy_version == FREE_CANDIDATE_MEMBERSHIP_POLICY_VERSION
        and history_signal_audit_marker == FREE_CROSS_JOB_HISTORY_AUDIT_MARKER
    )
    new_soft_policy = (
        unmastered_soft_exemption_marker
        == FREE_UNMASTERED_SOFT_EXEMPTION_MARKER
    )
    # Missing markers identify an older persisted checkpoint whose candidate
    # bytes and former behavior must remain replay-only. New checkpoints freeze
    # both raw audit history and the derived semantic soft signal.
    decision_recent_ids = (
        (
            frozen_decision_recent_ids
            if has_frozen_decision_recent_ids and new_soft_policy
            else tuple(sorted(recent_ids))
        )
        if not versioned and (loaded is None or new_membership_policy)
        else ()
    )
    if loaded is None and new_soft_policy:
        decision_recent_ids = derived_decision_recent_ids

    return FreeCandidateCheckpointOutcome(
        result=FreeCandidateCheckpointResult(
            question_bank_snapshot=snapshot,
            question_bank_coverage=dict(coverage),
            candidate_questions=tuple(dict(item) for item in candidates),
            frozen_manifest=dict(loaded) if loaded is not None else None,
            frozen_payload=dict(payload),
            versioned_manifest=versioned,
            decision_recent_ids=decision_recent_ids,
            membership_policy_version=(
                str(membership_policy_version)
                if membership_policy_version is not None
                else None
            ),
            history_signal_audit_marker=(
                str(history_signal_audit_marker)
                if history_signal_audit_marker is not None
                else None
            ),
            raw_recent_ids=tuple(sorted(recent_ids)) if not versioned else (),
            unmastered_soft_exemption_marker=(
                str(unmastered_soft_exemption_marker)
                if unmastered_soft_exemption_marker is not None
                else None
            ),
        )
    )


__all__ = [
    "FreeCandidateCheckpointFailure",
    "FreeCandidateCheckpointOutcome",
    "FreeCandidateCheckpointResult",
    "FREE_CANDIDATE_MEMBERSHIP_POLICY_VERSION",
    "FREE_CROSS_JOB_HISTORY_AUDIT_MARKER",
    "FREE_UNMASTERED_SOFT_EXEMPTION_MARKER",
    "prepare_free_candidate_checkpoint",
]
