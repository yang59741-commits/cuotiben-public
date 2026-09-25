"""Past-exam final material preparation for stage P3.5."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_past_candidates import PastCandidatePoolResult
from .exam_generation_past_semantic import (
    PastSemanticFreezeRequest,
    PastSemanticFreezeResult,
    adopt_past_semantic_winner,
)
from .exam_manifests import (
    ExamManifestError,
    exam_manifest_payload,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
)
from .exam_material import understand_frozen_material


@dataclass(frozen=True, slots=True)
class PastFinalMaterialResult:
    """Semantic state and material evidence for the exact final candidate pool."""

    semantic: PastSemanticFreezeResult
    selection_material_understanding: Mapping[str, Any]
    winner_adopted: bool


def _read_final_material(
    semantic: PastSemanticFreezeResult,
    *,
    db: Any,
    selection_snapshot: str,
    generation_job_id: str | None,
    deep_reader: Callable[..., Mapping[str, Any]],
    frozen_material_reader: Callable[..., Mapping[str, Any]],
) -> Mapping[str, Any]:
    candidates = [dict(item) for item in semantic.candidate_pool]
    if semantic.versioned_manifest:
        return frozen_material_reader(
            db,
            candidates,
            semantic.saved_payload.get("candidate_decisions") or [],
            snapshot=selection_snapshot,
            kind="past_candidates",
            generation_job_id=generation_job_id,
            deep_reader=deep_reader,
        )
    if (
        generation_job_id is None
        and semantic.unfrozen_candidate_decisions is not None
    ):
        return frozen_material_reader(
            db,
            candidates,
            list(semantic.unfrozen_candidate_decisions),
            snapshot=selection_snapshot,
            kind="past_candidates",
            generation_job_id=generation_job_id,
            deep_reader=deep_reader,
        )
    return deep_reader(
        candidates,
        selection_snapshot,
        "past_candidates",
        generation_job_id,
    )


def prepare_past_final_material(
    semantic: PastSemanticFreezeResult,
    pool: PastCandidatePoolResult,
    request: PastSemanticFreezeRequest,
    *,
    db: Any,
    bank_index: Any,
    bank_scope: Any,
    selection_snapshot: str,
    deep_reader: Callable[..., Mapping[str, Any]],
    has_background_job: Callable[[Any, str | None], bool],
    manifest_loader: Callable[..., Mapping[str, Any] | None] = (
        load_exam_generation_manifest
    ),
    manifest_payload_reader: Callable[..., Mapping[str, Any]] = exam_manifest_payload,
    versioned_manifest_checker: Callable[..., bool] = is_versioned_exam_manifest,
    frozen_material_reader: Callable[..., Mapping[str, Any]] = (
        understand_frozen_material
    ),
    winner_adopter: Callable[..., PastSemanticFreezeResult] = (
        adopt_past_semantic_winner
    ),
) -> PastFinalMaterialResult:
    """Read final material and adopt any concurrent authoritative winner."""

    current = semantic
    winner_adopted = False
    background_job = False
    if not current.versioned_manifest:
        background_job = has_background_job(db, request.generation_job_id)

    def adopt(winner: Mapping[str, Any]) -> PastSemanticFreezeResult:
        return winner_adopter(
            pool,
            request,
            winner,
            db=db,
            bank_index=bank_index,
            bank_scope=bank_scope,
            manifest_payload_reader=manifest_payload_reader,
            versioned_manifest_checker=versioned_manifest_checker,
        )

    if background_job and not current.versioned_manifest:
        loaded = manifest_loader(request.database, request.generation_job_id)
        if versioned_manifest_checker(loaded):
            current = adopt(loaded)
            winner_adopted = True
        else:
            raise ExamManifestError(
                "MANIFEST_CONFLICT",
                "真题语义冻结尚未完成，请重试当前组卷任务",
            )

    material = dict(
        _read_final_material(
            current,
            db=db,
            selection_snapshot=selection_snapshot,
            generation_job_id=request.generation_job_id,
            deep_reader=deep_reader,
            frozen_material_reader=frozen_material_reader,
        )
    )

    return PastFinalMaterialResult(
        semantic=current,
        selection_material_understanding=material,
        winner_adopted=winner_adopted,
    )


__all__ = [
    "PastFinalMaterialResult",
    "prepare_past_final_material",
]
