"""Past-exam fixed learning evidence preparation for generation stage P3.1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_responses import past_source_response
from .past_exams import (
    PastExamSourceError,
    learning_sample_questions,
    learning_sample_sections,
    past_exam_source_snapshot,
)


@dataclass(frozen=True, slots=True)
class PastLearningEvidenceRequest:
    past_source: Mapping[str, Any]
    reference_year: int
    past_exam_directory: str
    subject: str


@dataclass(frozen=True, slots=True)
class PastLearningEvidenceFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class PastLearningEvidenceResult:
    learning_samples: tuple[Mapping[str, Any], ...]
    learning_mode: str
    past_exam_snapshot_id: str
    learning_years: tuple[int, ...]
    past_learning_candidates: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PastLearningEvidenceOutcome:
    result: PastLearningEvidenceResult | None = None
    failure: PastLearningEvidenceFailure | None = None


def prepare_past_learning_evidence(
    request: PastLearningEvidenceRequest,
    *,
    sample_sections: Callable[..., tuple[list[Mapping[str, Any]], str]] = (
        learning_sample_sections
    ),
    source_snapshot: Callable[..., str] = past_exam_source_snapshot,
    sample_questions: Callable[..., list[Mapping[str, Any]]] = (
        learning_sample_questions
    ),
) -> PastLearningEvidenceOutcome:
    """Freeze fixed past learning inputs before question-bank or AI work."""

    try:
        learning_samples, learning_mode = sample_sections(
            request.past_source,
            reference_year=request.reference_year,
        )
        snapshot = source_snapshot(
            request.past_exam_directory,
            subject=request.subject,
        )
    except PastExamSourceError as error:
        body, status = past_source_response(error, fallback_status=400)
        return PastLearningEvidenceOutcome(
            failure=PastLearningEvidenceFailure(dict(body), int(status))
        )

    learning_years = tuple(int(item["year"]) for item in learning_samples)
    candidates = sample_questions(learning_samples)
    expected_count = sum(
        int((item.get("structure") or {}).get("question_count") or 0)
        for item in learning_samples
    )
    if len(candidates) != expected_count:
        return PastLearningEvidenceOutcome(
            failure=PastLearningEvidenceFailure(
                {"error": "固定学习年份存在未能完整解析的真题题目"},
                409,
            )
        )

    return PastLearningEvidenceOutcome(
        result=PastLearningEvidenceResult(
            learning_samples=tuple(dict(item) for item in learning_samples),
            learning_mode=str(learning_mode),
            past_exam_snapshot_id=str(snapshot),
            learning_years=learning_years,
            past_learning_candidates=tuple(dict(item) for item in candidates),
        )
    )


__all__ = [
    "PastLearningEvidenceFailure",
    "PastLearningEvidenceOutcome",
    "PastLearningEvidenceRequest",
    "PastLearningEvidenceResult",
    "prepare_past_learning_evidence",
]
