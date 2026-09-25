"""Question-bank prerequisites for past-exam generation stage P3.2a."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .exam_contracts import ExamGenerationConstraintError
from .exam_generation_responses import constraint_response, question_bank_response
from .question_bank import QuestionBankError


@dataclass(frozen=True, slots=True)
class PastQuestionBankPrerequisitesRequest:
    subject_id: int
    subject: str


@dataclass(frozen=True, slots=True)
class PastQuestionBankPrerequisitesFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class PastQuestionBankPrerequisitesResult:
    question_bank_snapshot: str
    available_chapters: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True, slots=True)
class PastQuestionBankPrerequisitesOutcome:
    result: PastQuestionBankPrerequisitesResult | None = None
    failure: PastQuestionBankPrerequisitesFailure | None = None


def _failure(body: Mapping[str, Any], status: int):
    return PastQuestionBankPrerequisitesOutcome(
        failure=PastQuestionBankPrerequisitesFailure(dict(body), int(status))
    )


def prepare_past_question_bank_prerequisites(
    request: PastQuestionBankPrerequisitesRequest,
    *,
    db: Any,
    bank_index: Any,
    bank_scope: Any,
) -> PastQuestionBankPrerequisitesOutcome:
    """Resolve the serializable past chapter scope from one bank handle."""

    try:
        subject_coverage = bank_scope.coverage(
            bank_index,
            subject=request.subject,
        )
        question_bank_snapshot = str(bank_index.snapshot_id)
    except QuestionBankError as error:
        return _failure(*question_bank_response(error))
    except ValueError as error:
        return _failure({"error": str(error)}, 503)

    chapter_rows = db.execute(
        "SELECT id,name FROM chapters WHERE subject_id=?",
        (request.subject_id,),
    ).fetchall()
    chapter_ids_by_name = {
        str(row["name"]): int(row["id"]) for row in chapter_rows
    }
    available_chapters = []
    for item in subject_coverage.get("chapters") or []:
        name = str(item.get("chapter") or "")
        count = int(item.get("total_questions") or 0)
        if count <= 0 or name == "场论初步" or name not in chapter_ids_by_name:
            continue
        available_chapters.append(
            {
                "id": chapter_ids_by_name[name],
                "name": name,
                "question_count": count,
            }
        )
    if not available_chapters:
        return _failure(
            *constraint_response(
                ExamGenerationConstraintError(
                    "有效题库没有可用于真题模拟的章节",
                    code="no_usable_chapters",
                    hint="请改用其他真题题源，或重建普通题库索引",
                    actions=("adjust_candidate_source", "rebuild_question_bank"),
                )
            )
        )

    return PastQuestionBankPrerequisitesOutcome(
        result=PastQuestionBankPrerequisitesResult(
            question_bank_snapshot=question_bank_snapshot,
            available_chapters=tuple(dict(item) for item in available_chapters),
        )
    )


__all__ = [
    "PastQuestionBankPrerequisitesFailure",
    "PastQuestionBankPrerequisitesOutcome",
    "PastQuestionBankPrerequisitesRequest",
    "PastQuestionBankPrerequisitesResult",
    "prepare_past_question_bank_prerequisites",
]
