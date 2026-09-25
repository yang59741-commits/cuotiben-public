"""Quality-gate execution and source-specific final-state projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_quality import QualityGateResult, enforce_exam_quality
from .exam_generation_quality_inputs import PreparedQualityInputs
from .exam_quality_review import ExamQualityError


@dataclass(frozen=True, slots=True)
class FreeQualityStageRequest:
    prepared: PreparedQualityInputs
    subject: str
    testing: bool


@dataclass(frozen=True, slots=True)
class MistakesQualityStageRequest:
    prepared: PreparedQualityInputs
    subject: str
    testing: bool


@dataclass(frozen=True, slots=True)
class PastQualityStageRequest:
    prepared: PreparedQualityInputs
    subject: str
    testing: bool
    selections: tuple[tuple[Mapping[str, Any], ...], ...]


@dataclass(frozen=True, slots=True)
class FreeQualityStageResult:
    bodies: tuple[str, ...]
    reviews: tuple[Mapping[str, Any], ...]
    generated_question_items: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class MistakesQualityStageResult:
    bodies: tuple[str, ...]
    reviews: tuple[Mapping[str, Any], ...]
    generated_question_items: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PastQualityStageResult:
    bodies: tuple[str, ...]
    reviews: tuple[Mapping[str, Any], ...]
    selections: tuple[tuple[Mapping[str, Any], ...], ...]
    replacement_positions: tuple[tuple[int, ...], ...] = ()


def _run_quality_gate(
    request: (
        FreeQualityStageRequest
        | MistakesQualityStageRequest
        | PastQualityStageRequest
    ),
    *,
    ai_call: Callable[..., str],
    quality_gate: Callable[..., QualityGateResult],
) -> QualityGateResult:
    prepared = request.prepared
    source_mode = (
        "free"
        if isinstance(request, FreeQualityStageRequest)
        else "mistakes"
        if isinstance(request, MistakesQualityStageRequest)
        else "past"
    )
    result = quality_gate(
        prepared.papers,
        subject=request.subject,
        allowed_chapters=prepared.allowed_chapters,
        allowed_question_types=prepared.allowed_question_types,
        ai_call=ai_call,
        same_model_review=prepared.same_model_review,
        testing=request.testing,
        quality_metadata=prepared.quality_metadata,
        source_mode=source_mode,
    )
    expected_papers = len(prepared.papers)
    if (
        len(result.bodies) != expected_papers
        or len(result.items) != expected_papers
        or len(result.reviews) != expected_papers
    ):
        raise ExamQualityError(
            "ITEM_SCHEMA", "质量结果与冻结试卷数量不一致"
        )
    return result


def run_free_quality_stage(
    request: FreeQualityStageRequest,
    *,
    ai_call: Callable[..., str],
    quality_gate: Callable[..., QualityGateResult] = enforce_exam_quality,
) -> FreeQualityStageResult:
    result = _run_quality_gate(
        request,
        ai_call=ai_call,
        quality_gate=quality_gate,
    )
    return FreeQualityStageResult(
        bodies=tuple(result.bodies),
        reviews=tuple(dict(item) for item in result.reviews),
        generated_question_items=tuple(
            dict(item) for item in result.items[0]
        ),
    )


def run_mistakes_quality_stage(
    request: MistakesQualityStageRequest,
    *,
    ai_call: Callable[..., str],
    quality_gate: Callable[..., QualityGateResult] = enforce_exam_quality,
) -> MistakesQualityStageResult:
    result = _run_quality_gate(
        request,
        ai_call=ai_call,
        quality_gate=quality_gate,
    )
    return MistakesQualityStageResult(
        bodies=tuple(result.bodies),
        reviews=tuple(dict(item) for item in result.reviews),
        generated_question_items=tuple(
            dict(item) for item in result.items[0]
        ),
    )


def run_past_quality_stage(
    request: PastQualityStageRequest,
    *,
    ai_call: Callable[..., str],
    quality_gate: Callable[..., QualityGateResult] = enforce_exam_quality,
) -> PastQualityStageResult:
    result = _run_quality_gate(
        request,
        ai_call=ai_call,
        quality_gate=quality_gate,
    )
    if (
        len(request.selections) != len(request.prepared.papers)
        or len(result.items) != len(request.selections)
    ):
        raise ExamQualityError(
            "ITEM_SCHEMA", "真题质量结果与冻结选题不一致"
        )

    merged_selections = []
    for original_paper, final_paper in zip(request.selections, result.items):
        if len(final_paper) != len(original_paper):
            raise ExamQualityError(
                "ITEM_SCHEMA", "真题质量结果与冻结选题不一致"
            )
        merged_paper = []
        for expected_position, (original, final) in enumerate(
            zip(original_paper, final_paper), start=1
        ):
            if not isinstance(original, Mapping) or not isinstance(final, Mapping):
                raise ExamQualityError(
                    "ITEM_SCHEMA", "真题质量结果与冻结选题不一致"
                )
            original_position = original.get("position")
            final_position = final.get("position")
            original_id = str(original.get("candidate_id") or "").strip()
            final_id = str(final.get("candidate_id") or "").strip()
            final_content = final.get("content")
            original_content = original.get("content")
            original_adapted = bool(original.get("adapted"))
            final_adapted = final.get("adapted")
            if (
                isinstance(original_position, bool)
                or not isinstance(original_position, int)
                or original_position < 1
                or isinstance(final_position, bool)
                or not isinstance(final_position, int)
                or final_position < 1
                or original_position != expected_position
                or final_position != expected_position
                or not original_id
                or original_id != final_id
                or not isinstance(final_content, str)
                or not final_content.strip()
                or not isinstance(original_content, str)
                or final_content != original_content
                or not isinstance(final_adapted, bool)
                or final_adapted != original_adapted
            ):
                raise ExamQualityError(
                    "ITEM_SCHEMA", "真题质量结果与冻结选题不一致"
                )
            merged = dict(original)
            merged_paper.append(merged)
        merged_selections.append(tuple(merged_paper))

    replacement_positions = tuple(
        tuple(
            sorted({
                int(position)
                for position in review.get("replacement_positions") or ()
            })
        )
        for review in result.reviews
    )

    return PastQualityStageResult(
        bodies=tuple(result.bodies),
        reviews=tuple(dict(item) for item in result.reviews),
        selections=tuple(merged_selections),
        replacement_positions=replacement_positions,
    )


__all__ = [
    "FreeQualityStageRequest",
    "FreeQualityStageResult",
    "MistakesQualityStageRequest",
    "MistakesQualityStageResult",
    "PastQualityStageRequest",
    "PastQualityStageResult",
    "run_free_quality_stage",
    "run_mistakes_quality_stage",
    "run_past_quality_stage",
]
