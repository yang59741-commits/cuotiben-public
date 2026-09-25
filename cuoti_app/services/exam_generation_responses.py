"""HTTP-neutral error-envelope mapping for exam generation."""

from __future__ import annotations

from .exam_contracts import ExamGenerationConstraintError
from .past_exams import PastExamSourceError
from .question_bank import QuestionBankError
from .semantic_recall_contracts import SemanticRecallError


def constraint_response(error: ExamGenerationConstraintError):
    body = {
        "error": str(error),
        "code": error.code,
        "retryable": False,
        "hint": error.hint,
        "actions": list(error.actions),
    }
    if error.details:
        body["details"] = dict(error.details)
    return body, error.status_code


def question_bank_response(error: QuestionBankError):
    """Preserve rebuild dependency metadata for HTTP and durable jobs."""

    body = {
        "error": str(error),
        "code": error.code,
        "retryable": bool(error.retryable),
    }
    if error.details:
        body["details"] = dict(error.details)
        actions = error.details.get("actions")
        if isinstance(actions, (list, tuple)):
            body["actions"] = [str(item) for item in actions]
        for key in ("stage", "progress", "retry_after"):
            if key in error.details:
                body[key] = error.details[key]
    if error.code == "QUESTION_BANK_BUILDING":
        body["hint"] = "题库索引完成后，组卷任务会自动继续。"
    elif error.code == "QUESTION_BANK_BUILD_FAILED":
        body["hint"] = "请重建题库后再提交组卷任务。"
    if error.job:
        body["job"] = dict(error.job)
        body["job_id"] = error.job.get("id")
    return body, int(error.status_code)


def semantic_recall_response(error: SemanticRecallError, *, source: str):
    if error.code in {"HARD_CONSTRAINT_CONFLICT", "CANDIDATE_BUDGET_INSUFFICIENT"}:
        if source == "past":
            hint = "请调整候选题源，或选择题量更少的结构年份后重新提交"
            actions = ("adjust_candidate_source", "adjust_reference_year")
        else:
            hint = "请减少题量，或调整章节后重新提交"
            actions = ("reduce_question_count", "adjust_chapters")
        return constraint_response(
            ExamGenerationConstraintError(
                str(error),
                code=error.code.lower(),
                hint=hint,
                actions=actions,
            )
        )
    status = (
        409
        if error.code
        in {"SNAPSHOT_DRIFT", "FROZEN_DATA_CORRUPT", "PARTIAL_CACHE_NOT_ALLOWED"}
        else 503
    )
    return {
        "error": str(error),
        "code": error.code.lower(),
        "retryable": status == 503,
    }, status


def past_constraint_response(error: ExamGenerationConstraintError):
    """Map shared question-bank actions to controls available in past mode."""

    free_only = {"reduce_question_count", "adjust_chapters"}
    actions = [action for action in error.actions if action not in free_only]
    if any(action in free_only for action in error.actions):
        actions.extend(("adjust_candidate_source", "adjust_reference_year"))
    actions = tuple(dict.fromkeys(actions))
    if actions == error.actions:
        return constraint_response(error)
    return constraint_response(
        ExamGenerationConstraintError(
            str(error),
            code=error.code,
            hint="请调整候选题源，或选择题量更少的结构年份后重新提交",
            actions=actions,
            details=error.details,
        )
    )


def past_source_response(error: PastExamSourceError, *, fallback_status: int = 422):
    status = int(getattr(error, "status_code", 0) or fallback_status)
    body = {
        "error": str(error),
        "code": getattr(error, "code", "past_source_error"),
        "retryable": bool(getattr(error, "retryable", False)),
        "hint": getattr(error, "hint", ""),
        "phase": getattr(error, "phase", "source"),
        "actions": (
            ["rescan_past_sources"]
            if not getattr(error, "retryable", False)
            else ["retry_scan"]
        ),
    }
    if getattr(error, "file", None) is not None:
        body["file"] = error.file
    if getattr(error, "year", None) is not None:
        body["year"] = error.year
    if getattr(error, "details", None):
        body["details"] = dict(error.details)
    return body, status


__all__ = [
    "constraint_response",
    "past_constraint_response",
    "past_source_response",
    "question_bank_response",
    "semantic_recall_response",
    "selection_checkpoint_response",
    "quality_failure_response",
]


def selection_checkpoint_response(error):
    retryable = bool(getattr(error, "retryable", False))
    return {
        "error": (
            "真题选题检查点暂时无法写入，请稍后重试"
            if retryable
            else "真题选题检查点与当前冻结任务不一致，已停止生成"
        ),
        "code": str(getattr(error, "code", "selection_checkpoint_conflict")),
        "retryable": retryable,
    }, (503 if retryable else 409)


def quality_failure_response(quality_error):
    return constraint_response(ExamGenerationConstraintError(
        "试卷内容质量审核未通过，已停止发布",
        code=str(getattr(quality_error, "code", "quality_gate_failed")).lower(),
        hint="请调整章节、题型、难度或候选来源后重新生成",
        actions=("adjust_chapters", "adjust_question_types", "retry_generation"),
        details={
            "failed_positions": list(getattr(quality_error, "failed_positions", ())),
            "issue_codes": list(getattr(quality_error, "issue_codes", ())),
            **({
                "schema_error_code": quality_error.schema_error_code,
                "schema_error_reason": quality_error.schema_error_reason,
            } if getattr(quality_error, "schema_error_code", None)
            and getattr(quality_error, "schema_error_reason", None) else {}),
        },
    ))
