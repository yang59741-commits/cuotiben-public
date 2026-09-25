"""Free-exam material understanding selection for generation stage F3.3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_free_semantic import FreeSemanticFreezeResult
from .exam_manifests import is_versioned_exam_manifest
from .exam_material import understand_frozen_material


@dataclass(frozen=True, slots=True)
class FreeMaterialUnderstandingRequest:
    generation_job_id: str | None


@dataclass(frozen=True, slots=True)
class FreeMaterialUnderstandingResult:
    """Serializable material evidence handed to free prompt construction."""

    material_understanding: Mapping[str, Any]


def prepare_free_material_understanding(
    semantic: FreeSemanticFreezeResult,
    request: FreeMaterialUnderstandingRequest,
    *,
    db: Any,
    deep_reader: Callable[..., Mapping[str, Any]],
    frozen_material_reader: Callable[..., Mapping[str, Any]] = understand_frozen_material,
) -> FreeMaterialUnderstandingResult:
    """Read the final free candidate pool without changing freeze precedence."""

    candidates = [dict(item) for item in semantic.candidate_questions]
    if is_versioned_exam_manifest(semantic.frozen_manifest):
        material = frozen_material_reader(
            db,
            candidates,
            semantic.frozen_payload.get("candidate_decisions") or [],
            snapshot=semantic.question_bank_snapshot,
            kind="question_bank",
            generation_job_id=request.generation_job_id,
            deep_reader=deep_reader,
        )
    elif semantic.unfrozen_candidate_decisions is not None:
        material = frozen_material_reader(
            db,
            candidates,
            semantic.unfrozen_candidate_decisions,
            snapshot=semantic.question_bank_snapshot,
            kind="question_bank",
            generation_job_id=request.generation_job_id,
            deep_reader=deep_reader,
        )
    else:
        material = deep_reader(
            candidates,
            semantic.question_bank_snapshot,
            "question_bank",
            request.generation_job_id,
        )
    return FreeMaterialUnderstandingResult(material_understanding=material)


__all__ = [
    "FreeMaterialUnderstandingRequest",
    "FreeMaterialUnderstandingResult",
    "prepare_free_material_understanding",
]
