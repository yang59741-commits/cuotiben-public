"""Stable validation, published recovery and mode dispatch for generation."""

from __future__ import annotations

from typing import Any

from ..exam_contracts import (
    MAX_MISTAKE_REFERENCES, ExamGenerationConstraintError, parse_exam_options,
    positive_int, validate_overall_difficulty, validate_single_paper_count,
)
from ..exam_coverage_policy import preflight_minimum_coverage
from ..exam_sources import BackgroundJobLookupError
from ..question_bank import QuestionBankError
from ..exam_generation_contracts import ExamGenerationResult, ExamGenerationRuntime
from ..exam_generation_preflight import prepare_generation_preflight
from ..exam_generation_source_context import task_frozen_mode_memory
from ..exam_generation_responses import (
    constraint_response as _constraint_response,
    question_bank_response as _question_bank_response,
)
from .free import run_free_exam
from .mistakes import run_mistakes_exam
from .legacy_past import run_legacy_past_exam


def _preflight_request_minimum_coverage(payload: dict[str, Any]) -> None:
    """Reject normalized free/mistake coverage impossibilities at the root.

    Invalid or incomplete requests remain owned by their existing source
    adapters.  This guard only acts once the relevant request identity can be
    normalized without database or external-service access.
    """

    raw_source = payload.get("source")
    source = "free" if raw_source is None else raw_source
    if source not in {"free", "mistakes"}:
        return

    raw_mistake_ids = payload.get("mistake_ids", [])
    if source == "mistakes":
        if (
            not isinstance(raw_mistake_ids, list)
            or not raw_mistake_ids
            or len(raw_mistake_ids) > MAX_MISTAKE_REFERENCES
        ):
            return
        try:
            mistake_ids = tuple(
                sorted(positive_int(value, "错题 ID") for value in raw_mistake_ids)
            )
        except ValueError:
            return
        if len(mistake_ids) != len(set(mistake_ids)):
            return
        default_count = len(mistake_ids)
    else:
        mistake_ids = ()
        default_count = 5

    try:
        _difficulty, _target, question_count, question_types, _note = (
            parse_exam_options(payload, default_count=default_count)
        )
    except ValueError:
        return

    chapter_keys: tuple[str, ...] = ()
    if source == "free":
        raw_chapters = payload.get("chapters", [])
        if (
            not isinstance(raw_chapters, list)
            or not raw_chapters
            or len(raw_chapters) > 50
        ):
            return
        try:
            chapter_ids = tuple(
                sorted(positive_int(value, "章节 ID") for value in raw_chapters)
            )
        except ValueError:
            return
        if len(chapter_ids) != len(set(chapter_ids)):
            return
        # At root preflight the authoritative names are intentionally not read;
        # these stable identity keys enforce only the structural count.  The
        # source adapter freezes the corresponding real names before memory.
        chapter_keys = tuple(f"chapter:{value}" for value in chapter_ids)

    preflight_minimum_coverage(
        question_count,
        question_types=question_types,
        chapters=chapter_keys,
        mistake_ids=mistake_ids,
    )

def generate_exam(payload: dict[str, Any], runtime: ExamGenerationRuntime) -> ExamGenerationResult:
    """Drive source, material, selection, TeX, publish and rollback stages."""

    try:
        # This guard deliberately precedes preflight, manifest recovery, AI
        # configuration reads, progress writes and every source-specific path.
        # A legacy multi-paper payload must never be able to replay an old
        # published batch as a new generation result.
        validate_single_paper_count(payload.get("paper_count", 1))
        raw_source = payload.get("source")
        source = "free" if raw_source is None else raw_source
        if source in {"free", "mistakes"}:
            # Overall difficulty is a request contract, so it must fail before
            # recovery, AI configuration, progress or any source-side effect.
            validate_overall_difficulty(payload.get("difficulty", "中等"))
            _preflight_request_minimum_coverage(payload)
        result = _generate_exam(payload, runtime)
    except ExamGenerationConstraintError as exc:
        result = _constraint_response(exc)
    except BackgroundJobLookupError as exc:
        runtime.logger.warning(
            "background job lookup failed during exam generation",
            exc_info=True,
        )
        result = ({"error": str(exc), "code": exc.code.lower()}, 503)
    except QuestionBankError as exc:
        runtime.logger.warning(
            "question-bank dependency blocked exam generation: %s", exc
        )
        result = _question_bank_response(exc)
    if isinstance(result, tuple):
        body, status = result
        return ExamGenerationResult(body=body, status=int(status))
    return ExamGenerationResult(body=result, status=200)

def _generate_exam(payload, runtime):
    preflight = prepare_generation_preflight(
        payload, db=runtime.db, config=runtime.config, logger=runtime.logger,
        request_id=runtime.request_id, require_ai_config=runtime.require_deepseek_config,
        progress=runtime.progress, past_attempt_resolver=runtime.past_attempt_resolver,
        allow_legacy_past_characterization=runtime.allow_legacy_past_characterization,
    )
    if preflight.completed:
        if preflight.response_status is not None:
            return dict(preflight.response_body or {}), preflight.response_status
        return dict(preflight.response_body or {})
    request = preflight.request
    if request is None:
        raise RuntimeError("组卷预检未返回冻结请求")
    memory = task_frozen_mode_memory(
        runtime.memory, db=runtime.db, mode=request.source, job_id=preflight.generation_job_id,
    )
    if request.source == "free":
        return run_free_exam(payload, request, runtime, memory, preflight.generation_job_id)
    if request.source == "mistakes":
        return run_mistakes_exam(payload, request, runtime, memory, preflight.generation_job_id)
    return run_legacy_past_exam(payload, runtime, request, memory, preflight.generation_job_id)
