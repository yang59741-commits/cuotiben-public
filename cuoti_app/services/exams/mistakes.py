"""Prepare mistake lineage and adapt its rules to the shared draft lifecycle."""

from dataclasses import dataclass, replace
from functools import partial
from typing import Any, Mapping

from ..exam_generation_contracts import ExamGenerationResult
from ..exam_generation_source_context import MistakesSourceContext, prepare_mistakes_source_context
from ..exam_generation_raw_papers import (
    StructuredMistakesRawGenerationRequest, generate_structured_mistakes_raw_papers,
)
from ..exam_generation_quality_inputs import (
    StructuredMistakesQualityInputRequest, prepare_structured_mistakes_quality_inputs,
)
from ..exam_generation_quality_stage import MistakesQualityStageRequest, run_mistakes_quality_stage
from ..exam_generation_quality import enforce_exam_quality
from ..exam_generation_coverage_fence import (
    validate_generation_coverage, refresh_mistake_content_fingerprints,
)
from ..exam_contracts import difficulty_quota
from ..exam_tex import audit_question_difficulties
from ..exam_quality_review import ExamQualityError
from ..exam_repetition_policy import load_latest_mistake_mastery_by_fingerprint
from .draft_cycle import review_draft
from .completion import base_draft_context, complete_exam, run_generation


@dataclass(frozen=True)
class PreparedMistakesExam:
    source: MistakesSourceContext
    raw_request: StructuredMistakesRawGenerationRequest
    unmastered: tuple[str, ...]
    ai_task_freezes: Mapping[str, Any]


def prepare_mistakes_exam(payload, request, *, db, memory, chapter_sources, tex_base,
                          logger, ai_task_freezes):
    outcome = prepare_mistakes_source_context(
        payload, db=db, memory=memory, chapter_sources=chapter_sources,
        tex_base=tex_base, logger=logger, selection_seed=request.operation_id,
    )
    if outcome.failure is not None:
        return ExamGenerationResult(dict(outcome.failure.body), outcome.failure.status)
    source = outcome.context
    if source is None:
        raise RuntimeError("错题组卷题源上下文为空")
    raw = StructuredMistakesRawGenerationRequest(
        system_prompt=source.system_prompt, prompt=source.prompt, subject=source.subject,
        difficulty_quota=difficulty_quota(source.question_count, source.difficulty),
        lineage_slots=source.lineage_slots, frozen_evidence=source.frozen_evidence,
    )
    mastery = load_latest_mistake_mastery_by_fingerprint(db, subject_id=source.subject_id)
    unmastered = tuple(sorted(key for key, value in mastery.items() if value == "未掌握"))
    return PreparedMistakesExam(source, raw, unmastered, ai_task_freezes)


def review_mistakes_draft(prepared, *, generate_indexed, ai_call, progress, testing, coverage):
    source = prepared.source

    def quality_inputs(raw, items):
        try:
            return prepare_structured_mistakes_quality_inputs(StructuredMistakesQualityInputRequest(
                bodies=raw.bodies, structured_items=tuple(items),
                lineage_slots=source.lineage_slots, frozen_evidence=raw.frozen_evidence,
                ai_task_freeze_snapshot=prepared.ai_task_freezes,
                unmastered_content_fingerprints=prepared.unmastered,
                training_target=source.training_target,
            ))
        except ExamQualityError as error:
            if str(error.code) in {"QUESTION_COUNT", "ITEM_SCHEMA"}:
                raise ExamQualityError(
                    "QUALITY_DRAFT_INVALID", "错题草稿正文结构无效",
                    failed_positions=tuple(getattr(error, "failed_positions", ()) or ()),
                    issue_codes=(str(error.code),),
                ) from error
            raise

    def prepare_difficulty(items):
        try:
            labels = audit_question_difficulties(
                [str(item.get("content") or item.get("latex") or "") for item in items],
                difficulty_quota(source.question_count, source.difficulty),
                source.subject, ai_call=ai_call,
            )
        except ValueError as error:
            raise ExamQualityError("DIFFICULTY_AUDIT_UNAVAILABLE", "错题组卷难度审核不可用，已停止发布") from error
        for item, label in zip(items, labels):
            item["difficulty"] = str(label)
        return items

    def quality_review(inputs):
        reviewed = run_mistakes_quality_stage(
            MistakesQualityStageRequest(prepared=inputs, subject=source.subject, testing=testing),
            ai_call=ai_call, quality_gate=enforce_exam_quality,
        )
        return replace(reviewed, generated_question_items=tuple(
            dict(item) for item in refresh_mistake_content_fingerprints(reviewed.generated_question_items)
        ))

    return review_draft(
        source, mode="mistakes",
        generate_raw=lambda: generate_structured_mistakes_raw_papers(
            prepared.raw_request, generate_indexed=generate_indexed,
        ),
        quality_inputs=quality_inputs, quality_review=quality_review,
        prepare_difficulty=prepare_difficulty, coverage=coverage, candidate_pool=(),
        unmastered=prepared.unmastered, ai_call=ai_call, progress=progress,
    )


def run_mistakes_exam(payload, request, runtime, memory, generation_job_id):
    prepared = prepare_mistakes_exam(
        payload, request, db=runtime.db, memory=memory,
        chapter_sources=runtime.chapter_sources, tex_base=runtime.config["TEX_BASE"],
        logger=runtime.logger, ai_task_freezes=dict(runtime.ai_task_freeze_snapshot or {}),
    )
    if isinstance(prepared, ExamGenerationResult):
        return prepared.body, prepared.status
    source = prepared.source
    coverage = partial(
        validate_generation_coverage, targets=source.minimum_coverage,
        lineage_slots=source.lineage_slots,
        public_id_by_internal_id=dict(source.public_id_by_internal_id),
    )

    def generate_and_complete(created_ids):
        reviewed = review_mistakes_draft(
            prepared, generate_indexed=runtime.generate_indexed_free_exam,
            ai_call=runtime.ai_call, progress=runtime.progress,
            testing=bool(runtime.config.get("TESTING")), coverage=coverage,
        )
        if isinstance(reviewed, ExamGenerationResult):
            return reviewed.body, reviewed.status
        context = replace(base_draft_context(source, "mistakes"), training_target=source.training_target)
        return complete_exam(
            source, context, reviewed, request, payload,
            generation_job_id=generation_job_id, db=runtime.db, config=runtime.config,
            memory=memory, logger=runtime.logger, compile_exam=runtime.compile_exam,
            store_exam_items=runtime.store_exam_items, progress=runtime.progress,
            publication_fence=runtime.publication_fence,
            observe_metric=runtime.observe_metric, created_ids=created_ids,
            coverage=coverage, reference_mistakes=source.mistakes,
            candidate_count=len(source.mistakes),
        )

    return run_generation(generate_and_complete, request, runtime)
