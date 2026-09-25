"""Prepare free-exam inputs, then run draft review and durable completion."""

from dataclasses import dataclass, replace
from typing import Any, Mapping
import uuid

from ..exam_contracts import (
    ExamGenerationConstraintError, positive_int, parse_exam_options,
    difficulty_quota,
)
from ..exam_generation_contracts import ExamGenerationResult
from ..exam_generation_source_context import FreeSourceContext, prepare_free_source_context
from ..exam_generation_free_candidates import prepare_free_candidate_checkpoint
from ..exam_generation_free_semantic import (
    FreeSemanticFreezeRequest, FreeSemanticFreezeResult, prepare_free_semantic_freeze,
)
from ..exam_generation_free_material import (
    FreeMaterialUnderstandingRequest, prepare_free_material_understanding,
)
from ..exam_generation_raw_papers import FreeRawGenerationRequest
from ..exam_generation_sources import QuestionBankScopeCache
from ..exam_repetition_policy import load_latest_mistake_mastery_by_fingerprint
from ..exam_manifests import ExamManifestError
from ..semantic_recall import SemanticRecallError
from ..question_bank import QuestionBankError
from ..exam_generation_responses import (
    constraint_response, question_bank_response, semantic_recall_response,
)
from functools import partial
from ..exam_generation_raw_papers import generate_free_raw_papers
from ..exam_generation_quality_inputs import (
    FreeQualityInputRequest, FreeSnapshotFenceRequest,
    prepare_free_quality_inputs, require_free_snapshot_fence,
)
from ..exam_generation_quality_stage import FreeQualityStageRequest, run_free_quality_stage
from ..exam_generation_quality import enforce_exam_quality
from ..exam_generation_coverage_fence import validate_generation_coverage
from .draft_cycle import review_draft
from .completion import base_draft_context, complete_exam, run_generation


@dataclass(frozen=True)
class PreparedFreeExam:
    source: FreeSourceContext
    semantic: FreeSemanticFreezeResult
    material: Mapping[str, Any]
    raw_request: FreeRawGenerationRequest
    unmastered: tuple[str, ...]
    ai_task_freezes: Mapping[str, Any]


def prepare_free_exam(payload, request, *, generation_job_id, db, memory, database, logger,
                     question_bank_index, indexed_candidates, has_background_job,
                     understand_material, semantic_recall_decision,
                     semantic_recall_shadow, ai_task_freezes,
                     semantic_settings_revision, semantic_candidate_multiplier,
                     semantic_allow_partial_cache):
    source_result = prepare_free_source_context(
        payload, db=db, memory=memory, positive_int=positive_int,
        exam_options=parse_exam_options, difficulty_quota=difficulty_quota,
    )
    if source_result.failure is not None:
        return ExamGenerationResult(dict(source_result.failure.body), source_result.failure.status)
    source = source_result.context
    if source is None:
        raise RuntimeError("自由组卷题源上下文为空")
    mastery = load_latest_mistake_mastery_by_fingerprint(db, subject_id=source.subject_id)
    unmastered = tuple(sorted(key for key, value in mastery.items() if value == "未掌握"))
    scope = QuestionBankScopeCache()
    try:
        bank = question_bank_index()
        candidate_result = prepare_free_candidate_checkpoint(
            db=db, database=database, memory=memory, bank_index=bank, bank_scope=scope,
            generation_job_id=generation_job_id, subject=source.subject,
            chapters=source.chapters, question_count=source.question_count,
            ai_task_freezes=ai_task_freezes, indexed_candidates=indexed_candidates,
            has_background_job=has_background_job, candidate_seed=lambda: uuid.uuid4().hex,
            unmastered_fingerprints=unmastered,
        )
        if candidate_result.failure is not None:
            return ExamGenerationResult(dict(candidate_result.failure.body), candidate_result.failure.status)
        checkpoint = candidate_result.result
        if checkpoint is None:
            raise RuntimeError("自由组卷候选 checkpoint 为空")
        semantic = prepare_free_semantic_freeze(
            checkpoint, FreeSemanticFreezeRequest(
                subject=source.subject, chapters=source.chapters,
                question_count=source.question_count, note=source.note,
                question_types=source.question_types, difficulty=source.difficulty,
                difficulty_quota=source.difficulty_quota,
                generation_job_id=generation_job_id, operation_id=request.operation_id,
                ai_task_freezes=ai_task_freezes,
                semantic_settings_revision=semantic_settings_revision,
                semantic_candidate_multiplier=semantic_candidate_multiplier,
                semantic_allow_partial_cache=semantic_allow_partial_cache,
            ), db=db, database=database, bank_index=bank, bank_scope=scope,
            has_background_job=has_background_job,
            semantic_recall_decision=semantic_recall_decision,
            semantic_recall_shadow=semantic_recall_shadow, logger=logger,
        )
        material = prepare_free_material_understanding(
            semantic, FreeMaterialUnderstandingRequest(generation_job_id),
            db=db, deep_reader=understand_material,
        ).material_understanding
    except ExamManifestError as error:
        return ExamGenerationResult({"error": str(error), "code": error.code}, 409)
    except SemanticRecallError as error:
        return ExamGenerationResult(*semantic_recall_response(error, source="free"))
    except ExamGenerationConstraintError as error:
        return ExamGenerationResult(*constraint_response(error))
    except QuestionBankError as error:
        return ExamGenerationResult(*question_bank_response(error))
    except ValueError as error:
        return ExamGenerationResult({"error": str(error)}, 503)
    raw = FreeRawGenerationRequest(
        subject=source.subject, chapters=source.chapters, question_count=source.question_count,
        question_types=source.question_types, difficulty=source.difficulty,
        target=source.target, note=source.note, candidates=semantic.candidate_questions,
        question_bank_coverage=checkpoint.question_bank_coverage,
        difficulty_quota=source.difficulty_quota, material_understanding=material,
        memory_context=source.memory_context,
    )
    return PreparedFreeExam(source, semantic, material, raw, unmastered, ai_task_freezes)


def run_free_exam(payload, request, runtime, memory, generation_job_id):
    """Keep source preparation outside the existing generation compensation scope."""
    prepared = prepare_free_exam(
        payload, request, generation_job_id=generation_job_id, db=runtime.db, memory=memory,
        database=runtime.config["DATABASE"], logger=runtime.logger,
        question_bank_index=runtime.question_bank_index,
        indexed_candidates=runtime.indexed_candidates,
        has_background_job=runtime.has_background_job,
        understand_material=runtime.understand_material,
        semantic_recall_decision=runtime.semantic_recall_decision,
        semantic_recall_shadow=runtime.semantic_recall_shadow,
        ai_task_freezes=dict(runtime.ai_task_freeze_snapshot or {}),
        semantic_settings_revision=runtime.semantic_settings_revision,
        semantic_candidate_multiplier=runtime.semantic_candidate_multiplier,
        semantic_allow_partial_cache=runtime.semantic_allow_partial_cache,
    )
    if isinstance(prepared, ExamGenerationResult):
        return prepared.body, prepared.status
    coverage = partial(validate_generation_coverage, targets=prepared.source.minimum_coverage)

    def generate_and_complete(created_ids):
        reviewed = review_free_draft(
            prepared, generate_indexed=runtime.generate_indexed_free_exam,
            require_snapshot=runtime.require_question_bank_snapshot,
            ai_call=runtime.ai_call, progress=runtime.progress,
            testing=bool(runtime.config.get("TESTING")), coverage=coverage,
        )
        if isinstance(reviewed, ExamGenerationResult):
            return reviewed.body, reviewed.status
        source = prepared.source
        chapters = list(source.chapters)
        context = replace(
            base_draft_context(source, "free"),
            title=f"{source.subject} - {'、'.join(chapters[:3])}{'等' if len(chapters)>3 else ''} ({source.difficulty})",
            exam_type="自由组卷", question_bank_snapshot=prepared.semantic.question_bank_snapshot,
            question_bank_coverage=prepared.raw_request.question_bank_coverage,
            candidate_questions=list(prepared.semantic.candidate_questions),
            difficulty_quota=dict(source.difficulty_quota),
        )
        return complete_exam(
            source, context, reviewed, request, payload,
            generation_job_id=generation_job_id, db=runtime.db, config=runtime.config,
            memory=memory, logger=runtime.logger, compile_exam=runtime.compile_exam,
            store_exam_items=runtime.store_exam_items, progress=runtime.progress,
            publication_fence=runtime.publication_fence,
            observe_metric=runtime.observe_metric, created_ids=created_ids,
            coverage=coverage, candidate_count=len(prepared.semantic.candidate_questions),
            material_parts=(prepared.material,) if isinstance(prepared.material, dict) else (),
        )

    return run_generation(generate_and_complete, request, runtime)


def review_free_draft(prepared, *, generate_indexed, require_snapshot, ai_call, progress, testing, coverage):
    source, semantic = prepared.source, prepared.semantic

    def quality_inputs(raw, items):
        return prepare_free_quality_inputs(FreeQualityInputRequest(
            bodies=raw.bodies, generated_question_items=tuple(items),
            frozen_evidence=raw.frozen_evidence, chapters=source.chapters,
            question_types=source.question_types, material_understanding=prepared.material,
            recall_effective_mode=semantic.recall_effective_mode,
            semantic_fallback_reason=semantic.fallback_reason or "",
            frozen_payload=dict(semantic.frozen_payload),
            ai_task_freeze_snapshot=prepared.ai_task_freezes,
            unmastered_content_fingerprints=prepared.unmastered,
        ))

    def quality_review(inputs):
        return run_free_quality_stage(
            FreeQualityStageRequest(prepared=inputs, subject=source.subject, testing=testing),
            ai_call=ai_call, quality_gate=enforce_exam_quality,
        )

    return review_draft(
        source, mode="free",
        generate_raw=lambda: generate_free_raw_papers(prepared.raw_request, generate_indexed=generate_indexed),
        quality_inputs=quality_inputs, quality_review=quality_review,
        coverage=coverage, candidate_pool=semantic.candidate_questions,
        unmastered=prepared.unmastered, ai_call=ai_call, progress=progress,
        snapshot_guard=lambda: require_free_snapshot_fence(
            FreeSnapshotFenceRequest(semantic.question_bank_snapshot),
            require_question_bank_snapshot=require_snapshot,
        ).question_bank_index,
    )
