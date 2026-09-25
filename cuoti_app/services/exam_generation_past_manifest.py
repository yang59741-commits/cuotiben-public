"""Past-exam manifest read and frozen-contract checks for stage P3.2b1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_manifests import (
    ExamManifestError,
    exam_manifest_payload,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
    manifest_stage,
)
from .semantic_recall import SemanticRecallError, validate_frozen_recall_contract


@dataclass(frozen=True, slots=True)
class PastManifestPrerequisitesRequest:
    database: str
    generation_job_id: str | None
    question_bank_snapshot: str
    past_exam_snapshot_id: str
    reference_year: int
    candidate_source: str


@dataclass(frozen=True, slots=True)
class PastManifestPrerequisitesFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class PastManifestPrerequisitesResult:
    frozen_manifest: Mapping[str, Any] | None
    saved_payload: Mapping[str, Any]
    versioned_manifest: bool
    staged_manifest: str | None
    manifest_was_present: bool


@dataclass(frozen=True, slots=True)
class PastManifestPrerequisitesOutcome:
    result: PastManifestPrerequisitesResult | None = None
    failure: PastManifestPrerequisitesFailure | None = None


def _failure(body: Mapping[str, Any], status: int):
    return PastManifestPrerequisitesOutcome(
        failure=PastManifestPrerequisitesFailure(dict(body), int(status))
    )


def prepare_past_manifest_prerequisites(
    request: PastManifestPrerequisitesRequest,
    *,
    db: Any,
    manifest_loader: Callable[..., Mapping[str, Any] | None] = (
        load_exam_generation_manifest
    ),
    frozen_recall_validator: Callable[..., None] = validate_frozen_recall_contract,
) -> PastManifestPrerequisitesOutcome:
    """Load and validate an existing past manifest without writing it."""

    frozen_manifest = None
    if request.generation_job_id:
        try:
            frozen_manifest = manifest_loader(
                request.database,
                request.generation_job_id,
            )
        except ExamManifestError as error:
            return _failure({"error": str(error), "code": error.code}, 409)
    if frozen_manifest and (
        frozen_manifest.get("snapshot_id") != request.question_bank_snapshot
    ):
        return _failure(
            {"error": "题库索引已变化，无法使用该任务的冻结组卷清单"},
            409,
        )

    saved_payload = exam_manifest_payload(frozen_manifest)
    versioned_manifest = is_versioned_exam_manifest(frozen_manifest)
    staged_manifest = manifest_stage(frozen_manifest)
    manifest_was_present = frozen_manifest is not None
    if versioned_manifest:
        try:
            frozen_recall_validator(db, saved_payload)
        except SemanticRecallError as error:
            return _failure({"error": str(error), "code": error.code}, 409)
    if saved_payload:
        if saved_payload.get("past_exam_snapshot") not in (
            None,
            request.past_exam_snapshot_id,
        ):
            return _failure(
                {"error": "正式真题已变化，无法使用该任务的冻结组卷清单"},
                409,
            )
        frozen_contract = {
            "reference_year": saved_payload.get("reference_year"),
            "candidate_source": saved_payload.get("candidate_source"),
        }
        requested_contract = {
            "reference_year": request.reference_year,
            "candidate_source": request.candidate_source,
        }
        if any(
            frozen_contract[key] not in (None, requested_contract[key])
            for key in frozen_contract
        ):
            return _failure(
                {"error": "任务的结构年份或候选题源与冻结清单不一致"},
                409,
            )

    return PastManifestPrerequisitesOutcome(
        result=PastManifestPrerequisitesResult(
            frozen_manifest=(
                dict(frozen_manifest) if frozen_manifest is not None else None
            ),
            saved_payload=dict(saved_payload),
            versioned_manifest=versioned_manifest,
            staged_manifest=staged_manifest,
            manifest_was_present=manifest_was_present,
        )
    )


__all__ = [
    "PastManifestPrerequisitesFailure",
    "PastManifestPrerequisitesOutcome",
    "PastManifestPrerequisitesRequest",
    "PastManifestPrerequisitesResult",
    "prepare_past_manifest_prerequisites",
]
