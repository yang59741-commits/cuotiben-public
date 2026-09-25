"""Past-exam candidate-pool preparation and checkpointing for stage P3.3."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_contracts import ExamGenerationConstraintError
from .exam_generation_freeze import restore_frozen_past_candidates
from .exam_generation_past_characterization import (
    PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION,
    PAST_CROSS_JOB_HISTORY_AUDIT_MARKER,
    PAST_UNMASTERED_SOFT_EXEMPTION_MARKER,
    PastCharacterizationResult,
)
from .exam_manifests import (
    exam_manifest_payload,
    is_versioned_exam_manifest,
    manifest_stage,
    merge_exam_generation_manifest,
)
from .exam_repetition_policy import (
    normalize_question_content,
    question_content_fingerprint,
)
from .past_exam_flow import select_other_past_candidates
from .past_exam_profile import material_understanding_from_profile
from .past_exams import (
    build_other_past_candidate_pool,
    other_past_candidates_by_ids,
)


@dataclass(frozen=True, slots=True)
class PastCandidatePoolRequest:
    database: str
    generation_job_id: str | None
    past_exam_directory: str
    institution: str
    subject: str
    question_count: int
    question_bank_snapshot: str
    past_exam_snapshot_id: str
    candidate_source: str
    source_quotas: Mapping[str, int]
    manual_profile_required: bool
    learning_years: tuple[int, ...]
    past_learning_candidates: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PastCandidatePoolResult:
    chapter_names: tuple[str, ...]
    chapter_quotas: Mapping[str, int]
    question_bank_coverage: Mapping[str, Any]
    selection_snapshot: str
    ordinary_required: int
    other_required: int
    ordinary_candidates: tuple[Mapping[str, Any], ...]
    other_candidates: tuple[Mapping[str, Any], ...]
    other_pool: tuple[Mapping[str, Any], ...]
    candidate_pool: tuple[Mapping[str, Any], ...]
    material_understanding: Mapping[str, Any]
    decision_recent_past_ids: tuple[str, ...]
    recent_past_ids: tuple[str, ...]
    past_analysis: Mapping[str, Any]
    chapter_plan: tuple[Mapping[str, Any], ...]
    difficulty_quota: Mapping[str, int] | None
    past_profile_status: str
    past_profile_origin: str
    past_profile_id: str | None
    frozen_manifest: Mapping[str, Any] | None
    saved_payload: Mapping[str, Any]
    versioned_manifest: bool
    staged_manifest: str | None
    membership_policy_version: str | None = None
    history_signal_audit_marker: str | None = None
    unmastered_soft_exemption_marker: str | None = None


def _derive_decision_recent_past_ids(
    candidate_facts: list[Mapping[str, Any]],
    recent_ids: set[str],
    unmastered_fingerprints: tuple[str, ...],
) -> tuple[str, ...]:
    """Freeze the existing semantic soft input without changing membership.

    Only a non-empty frozen body whose exact fingerprint appears in the
    task-start unmastered snapshot cancels its cross-job recent soft signal.
    Missing or conflicting facts are not guessed, and no score is introduced.
    """

    unmastered = {
        str(value).strip()
        for value in unmastered_fingerprints
        if str(value).strip()
    }
    content_facts: dict[str, set[str | None]] = {}
    for item in candidate_facts:
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id not in recent_ids:
            continue
        content = item.get("content")
        content_facts.setdefault(candidate_id, set()).add(
            question_content_fingerprint(content)
            if normalize_question_content(content)
            else None
        )
    exempt_ids = {
        candidate_id
        for candidate_id, fingerprints in content_facts.items()
        if len(fingerprints) == 1
        and None not in fingerprints
        and next(iter(fingerprints)) in unmastered
    }
    return tuple(sorted(recent_ids - exempt_ids))


def prepare_past_candidate_pool(
    characterization: PastCharacterizationResult,
    request: PastCandidatePoolRequest,
    *,
    db: Any,
    bank_index: Any,
    bank_scope: Any,
    indexed_candidate_loader: Callable[..., tuple[list[Mapping[str, Any]], Mapping[str, Any]]],
    has_background_job: Callable[[Any, str | None], bool],
    other_pool_builder: Callable[..., list[Mapping[str, Any]]] = (
        build_other_past_candidate_pool
    ),
    frozen_other_loader: Callable[..., list[Mapping[str, Any]]] = (
        other_past_candidates_by_ids
    ),
    other_selector: Callable[..., list[Mapping[str, Any]]] = (
        select_other_past_candidates
    ),
    frozen_candidate_restorer: Callable[..., list[Mapping[str, Any]]] = (
        restore_frozen_past_candidates
    ),
    manifest_merger: Callable[..., Mapping[str, Any]] = (
        merge_exam_generation_manifest
    ),
    manifest_payload_reader: Callable[..., Mapping[str, Any]] = exam_manifest_payload,
    versioned_manifest_checker: Callable[..., bool] = is_versioned_exam_manifest,
    manifest_stage_reader: Callable[..., str | None] = manifest_stage,
    seed_factory: Callable[[], Any] = uuid.uuid4,
    profile_summary_builder: Callable[..., Mapping[str, Any]] = (
        material_understanding_from_profile
    ),
) -> PastCandidatePoolResult:
    """Build the replayable baseline pool before semantic recall begins."""

    chapter_plan = [dict(item) for item in characterization.chapter_plan]
    chapter_names = [str(item["chapter"]) for item in chapter_plan]
    chapter_quotas = {
        str(item["chapter"]): int(item["question_count"])
        for item in chapter_plan
    }
    question_bank_coverage = dict(
        bank_scope.coverage(
            bank_index,
            subject=request.subject,
            chapters=chapter_names,
        )
    )
    if request.generation_job_id:
        seed = request.generation_job_id
    else:
        generated_seed = seed_factory()
        seed = str(getattr(generated_seed, "hex", generated_seed))

    source_quotas = dict(request.source_quotas)
    ordinary_required = int(source_quotas.get("ordinary", 0))
    other_required = int(source_quotas.get("past_exam", 0))
    other_pool = (
        [
            dict(item)
            for item in other_pool_builder(
                request.past_exam_directory,
                request.subject,
                request.institution,
            )
        ]
        if other_required
        else []
    )
    selection_snapshot = hashlib.sha256(
        (
            f"{request.question_bank_snapshot}|"
            f"{request.past_exam_snapshot_id}|{request.candidate_source}"
        ).encode("utf-8")
    ).hexdigest()

    frozen_manifest = (
        dict(characterization.frozen_manifest)
        if characterization.frozen_manifest is not None
        else None
    )
    saved_payload = dict(characterization.saved_payload)
    versioned_manifest = bool(characterization.versioned_manifest)
    staged_manifest = characterization.staged_manifest
    manifest_was_present = bool(characterization.manifest_was_present)
    checkpoint_created_here = bool(characterization.checkpoint_created_here)
    has_candidate_checkpoint = bool(characterization.has_candidate_checkpoint)
    recent_past_ids = set(characterization.recent_past_ids)
    membership_policy_version = saved_payload.get(
        "past_candidate_membership_policy_version"
    )
    if membership_policy_version is not None:
        membership_policy_version = str(membership_policy_version)
    history_signal_audit_marker = saved_payload.get(
        "past_history_signal_audit_marker"
    )
    if history_signal_audit_marker is not None:
        history_signal_audit_marker = str(history_signal_audit_marker)
    unmastered_soft_exemption_marker = saved_payload.get(
        "past_unmastered_soft_exemption_marker"
    )
    if unmastered_soft_exemption_marker is not None:
        unmastered_soft_exemption_marker = str(
            unmastered_soft_exemption_marker
        )
    if not manifest_was_present or checkpoint_created_here:
        membership_policy_version = PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
        history_signal_audit_marker = PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
        unmastered_soft_exemption_marker = (
            PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
        )
    new_membership_policy = (
        membership_policy_version == PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
        and history_signal_audit_marker == PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
    )
    new_soft_policy = (
        new_membership_policy
        and unmastered_soft_exemption_marker
        == PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
    )
    # A marked candidate checkpoint already froze the task-start history read.
    # Recover that exact audit/soft input instead of consulting current memory
    # or silently dropping the soft signal after a crash.  Unmarked legacy
    # checkpoints keep their former replay behavior and are never upgraded.
    stored_recent_past_ids = saved_payload.get("recent_question_ids")
    if new_membership_policy and isinstance(stored_recent_past_ids, list):
        recent_past_ids = {
            str(value)
            for value in stored_recent_past_ids
            if str(value).strip()
        }
    past_analysis = dict(characterization.past_analysis)
    material_understanding = dict(characterization.material_understanding)
    difficulty_quota = (
        dict(characterization.difficulty_quota)
        if characterization.difficulty_quota is not None
        else None
    )
    past_profile_status = characterization.past_profile_status
    past_profile_origin = characterization.past_profile_origin
    past_profile_id = characterization.past_profile_id

    legacy_ordinary_ids = saved_payload.get("ordinary_candidate_ids")
    legacy_other_ids = saved_payload.get("other_past_candidate_ids")
    frozen_ordinary_ids = (
        list(legacy_ordinary_ids)
        if isinstance(legacy_ordinary_ids, list)
        else None
    )
    frozen_other_ids = (
        list(legacy_other_ids)
        if isinstance(legacy_other_ids, list)
        else None
    )
    if isinstance(saved_payload.get("candidate_decision_ordinary_ids"), list):
        frozen_ordinary_ids = list(
            saved_payload["candidate_decision_ordinary_ids"]
        )
    if isinstance(saved_payload.get("candidate_decision_other_past_ids"), list):
        frozen_other_ids = list(
            saved_payload["candidate_decision_other_past_ids"]
        )

    ordinary_candidates: list[dict[str, Any]] = []
    other_candidates: list[dict[str, Any]] = []
    if ordinary_required:
        if frozen_ordinary_ids is not None:
            ordinary_candidates = [
                dict(item)
                for item in bank_index.candidates_by_ids(
                    frozen_ordinary_ids,
                    subject=request.subject,
                    chapters=chapter_names,
                )
            ]
        else:
            loaded_candidates, loaded_coverage = indexed_candidate_loader(
                bank_index,
                request.subject,
                chapter_names,
                ordinary_required,
                seed,
                chapter_quotas=chapter_quotas,
                coverage_result=question_bank_coverage,
            )
            ordinary_candidates = [dict(item) for item in loaded_candidates]
            question_bank_coverage = dict(loaded_coverage)
        for item in ordinary_candidates:
            item["source_kind"] = "ordinary"
    if other_required:
        if frozen_other_ids is not None:
            other_candidates = [
                dict(item)
                for item in frozen_other_loader(
                    request.past_exam_directory,
                    request.subject,
                    request.institution,
                    frozen_other_ids,
                )
            ]
        else:
            other_candidates = [
                dict(item)
                for item in other_selector(
                    other_pool,
                    other_required,
                    seed,
                )
            ]

    candidate_pool = ordinary_candidates + other_candidates
    if versioned_manifest:
        candidate_pool = [
            dict(item)
            for item in frozen_candidate_restorer(
                saved_payload,
                bank_index=bank_index,
                subject=request.subject,
                chapters=chapter_names,
                other_pool=other_pool,
                other_directory=request.past_exam_directory,
                institution=request.institution,
            )
        ]

    stored_decision_recent_ids = saved_payload.get("decision_recent_past_ids")
    has_frozen_decision_recent_ids = isinstance(stored_decision_recent_ids, list)
    frozen_decision_recent_ids = (
        tuple(sorted({
            str(value)
            for value in stored_decision_recent_ids
            if str(value).strip()
        }))
        if has_frozen_decision_recent_ids
        else ()
    )
    if new_soft_policy and not has_candidate_checkpoint:
        candidate_facts = list(candidate_pool)
        known_ids = {
            str(item.get("candidate_id") or "") for item in candidate_facts
        }
        unresolved_recent_ids = recent_past_ids - known_ids
        if (
            unresolved_recent_ids
            and characterization.unmastered_content_fingerprints
        ):
            if ordinary_required:
                candidate_facts.extend(
                    dict(item)
                    for item in bank_scope.candidates(
                        bank_index,
                        request.subject,
                        chapter_names,
                    )
                    if str(item.get("candidate_id") or "")
                    in unresolved_recent_ids
                )
            candidate_facts.extend(
                dict(item)
                for item in other_pool
                if str(item.get("candidate_id") or "")
                in unresolved_recent_ids
            )
        frozen_decision_recent_ids = _derive_decision_recent_past_ids(
            candidate_facts,
            recent_past_ids,
            characterization.unmastered_content_fingerprints,
        )
        has_frozen_decision_recent_ids = True

    # The marker pair is identity/audit evidence only.  Its sole behavioral
    # boundary here is whether a *new* pool may be physically filtered; it
    # neither creates a weight nor participates in downstream hard validation.
    apply_recent_exclusion = (
        not new_membership_policy
        and bool(recent_past_ids)
        and (
            not manifest_was_present or not has_candidate_checkpoint
        )
    )
    if apply_recent_exclusion:
        fresh_past_candidates = [
            item
            for item in candidate_pool
            if str(item.get("candidate_id")) not in recent_past_ids
        ]
        if len(fresh_past_candidates) >= request.question_count:
            candidate_pool = fresh_past_candidates

    if (
        has_background_job(db, request.generation_job_id)
        and not versioned_manifest
        and staged_manifest == "past-profile-frozen-v1"
        and (checkpoint_created_here or not has_candidate_checkpoint)
    ):
        checkpoint_updates = {
            "candidate_ids": [
                str(item.get("candidate_id")) for item in candidate_pool
            ],
            "candidate_decision_candidate_ids": [
                str(item.get("candidate_id")) for item in candidate_pool
            ],
            "candidate_decision_ordinary_ids": [
                str(item.get("candidate_id"))
                for item in candidate_pool
                if str(item.get("source_kind")) == "ordinary"
            ],
            "candidate_decision_other_past_ids": [
                str(item.get("candidate_id"))
                for item in candidate_pool
                if str(item.get("source_kind"))
                in {"past_exam", "past_exam_other"}
            ],
            "ordinary_candidate_ids": [
                str(item.get("candidate_id"))
                for item in candidate_pool
                if str(item.get("source_kind")) == "ordinary"
            ],
            "other_past_candidate_ids": [
                str(item.get("candidate_id"))
                for item in candidate_pool
                if str(item.get("source_kind"))
                in {"past_exam", "past_exam_other"}
            ],
        }
        if new_membership_policy:
            checkpoint_updates.update({
                "past_candidate_membership_policy_version": (
                    PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
                ),
                "past_history_signal_audit_marker": (
                    PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
                ),
            })
        if new_soft_policy:
            checkpoint_updates.update({
                "past_unmastered_soft_exemption_marker": (
                    PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
                ),
                "decision_recent_past_ids": list(
                    frozen_decision_recent_ids
                ),
            })
        frozen_manifest = dict(
            manifest_merger(
                request.database,
                request.generation_job_id,
                request.question_bank_snapshot,
                checkpoint_updates,
            )
        )
        saved_payload = dict(manifest_payload_reader(frozen_manifest))
        membership_policy_version = saved_payload.get(
            "past_candidate_membership_policy_version"
        )
        history_signal_audit_marker = saved_payload.get(
            "past_history_signal_audit_marker"
        )
        unmastered_soft_exemption_marker = saved_payload.get(
            "past_unmastered_soft_exemption_marker"
        )
        new_membership_policy = (
            membership_policy_version
            == PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
            and history_signal_audit_marker
            == PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
        )
        new_soft_policy = (
            new_membership_policy
            and unmastered_soft_exemption_marker
            == PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
        )
        returned_decision_recent_ids = saved_payload.get(
            "decision_recent_past_ids"
        )
        has_frozen_decision_recent_ids = isinstance(
            returned_decision_recent_ids, list
        )
        if has_frozen_decision_recent_ids:
            frozen_decision_recent_ids = tuple(sorted({
                str(value)
                for value in returned_decision_recent_ids
                if str(value).strip()
            }))
        versioned_manifest = bool(versioned_manifest_checker(frozen_manifest))
        staged_manifest = manifest_stage_reader(frozen_manifest)
        has_candidate_checkpoint = True
        if versioned_manifest:
            manifest_was_present = True
            winner_analysis = saved_payload.get("past_analysis")
            winner_plan = saved_payload.get("chapter_plan")
            if isinstance(winner_analysis, Mapping):
                past_analysis = dict(winner_analysis)
                winner_difficulty = past_analysis.get("difficulty_evidence")
                if isinstance(winner_difficulty, Mapping):
                    winner_quota = winner_difficulty.get("quota")
                    if isinstance(winner_quota, Mapping):
                        difficulty_quota = dict(winner_quota)
            if isinstance(winner_plan, (list, tuple)):
                chapter_plan = [dict(item) for item in winner_plan]
                chapter_names = [str(item["chapter"]) for item in chapter_plan]
                chapter_quotas = {
                    str(item["chapter"]): int(item["question_count"])
                    for item in chapter_plan
                }
                question_bank_coverage = dict(
                    bank_scope.coverage(
                        bank_index,
                        subject=request.subject,
                        chapters=chapter_names,
                    )
                )
            candidate_pool = [
                dict(item)
                for item in frozen_candidate_restorer(
                    saved_payload,
                    bank_index=bank_index,
                    subject=request.subject,
                    chapters=chapter_names,
                    other_pool=other_pool,
                    other_directory=request.past_exam_directory,
                    institution=request.institution,
                )
            ]
            ordinary_candidates = [
                dict(item)
                for item in candidate_pool
                if str(item.get("source_kind")) == "ordinary"
            ]
            other_candidates = [
                dict(item)
                for item in candidate_pool
                if str(item.get("source_kind"))
                in {"past_exam", "past_exam_other"}
            ]
            winner_profile = saved_payload.get("past_profile")
            if isinstance(winner_profile, Mapping):
                past_profile_status = str(
                    winner_profile.get("status") or past_profile_status
                )
                past_profile_origin = str(
                    winner_profile.get("origin") or past_profile_origin
                )
                winner_profile_id = winner_profile.get("profile_id")
                past_profile_id = (
                    str(winner_profile_id)
                    if winner_profile_id is not None
                    else None
                )
            if request.manual_profile_required and isinstance(
                winner_analysis, Mapping
            ):
                material_understanding = dict(
                    profile_summary_builder(
                        {
                            "analysis": past_analysis,
                            "profile_id": past_profile_id,
                        },
                        [
                            dict(item)
                            for item in request.past_learning_candidates
                        ],
                        (
                            f"{request.past_exam_snapshot_id}:"
                            f"{','.join(map(str, request.learning_years))}"
                        ),
                    )
                )
            staged_manifest = None
            recent_past_ids = set()
            apply_recent_exclusion = False

    if new_membership_policy:
        decision_recent_past_ids = set(
            frozen_decision_recent_ids
            if new_soft_policy and has_frozen_decision_recent_ids
            else recent_past_ids
        )
    elif apply_recent_exclusion:
        decision_recent_past_ids = set(recent_past_ids)
    else:
        decision_recent_past_ids = set()
    if len(candidate_pool) < request.question_count:
        raise ExamGenerationConstraintError(
            (
                f"候选题不足：需要至少{request.question_count}题，"
                f"当前只有{len(candidate_pool)}题"
            ),
            code="candidate_pool_insufficient",
            hint="请调整候选题源或选择题量更少的结构年份",
            actions=("adjust_candidate_source", "adjust_reference_year"),
            details={
                "required": request.question_count,
                "available": len(candidate_pool),
            },
        )

    return PastCandidatePoolResult(
        chapter_names=tuple(chapter_names),
        chapter_quotas=dict(chapter_quotas),
        question_bank_coverage=dict(question_bank_coverage),
        selection_snapshot=selection_snapshot,
        ordinary_required=ordinary_required,
        other_required=other_required,
        ordinary_candidates=tuple(dict(item) for item in ordinary_candidates),
        other_candidates=tuple(dict(item) for item in other_candidates),
        other_pool=tuple(dict(item) for item in other_pool),
        candidate_pool=tuple(dict(item) for item in candidate_pool),
        material_understanding=dict(material_understanding),
        decision_recent_past_ids=tuple(sorted(decision_recent_past_ids)),
        recent_past_ids=tuple(sorted(recent_past_ids)),
        past_analysis=dict(past_analysis),
        chapter_plan=tuple(dict(item) for item in chapter_plan),
        difficulty_quota=(
            dict(difficulty_quota) if difficulty_quota is not None else None
        ),
        past_profile_status=past_profile_status,
        past_profile_origin=past_profile_origin,
        past_profile_id=past_profile_id,
        frozen_manifest=(
            dict(frozen_manifest) if frozen_manifest is not None else None
        ),
        saved_payload=dict(saved_payload),
        versioned_manifest=versioned_manifest,
        staged_manifest=staged_manifest,
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
        unmastered_soft_exemption_marker=(
            str(unmastered_soft_exemption_marker)
            if unmastered_soft_exemption_marker is not None
            else None
        ),
    )


__all__ = [
    "PastCandidatePoolRequest",
    "PastCandidatePoolResult",
    "PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION",
    "PAST_CROSS_JOB_HISTORY_AUDIT_MARKER",
    "PAST_UNMASTERED_SOFT_EXEMPTION_MARKER",
    "prepare_past_candidate_pool",
]
