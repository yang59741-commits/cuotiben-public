"""Shared free/mistake publication and the existing compensation boundary."""

from dataclasses import replace
from ..exam_sources import BackgroundJobLookupError
from ..job_contracts import JobError, WorkerStopRequested
from ..ai import DeepSeekConfigurationError, DeepSeekServiceError
from ..tex import TexCompileError, UnsafeLatexError
from ..exam_publish import compensate_exam_publication
from ..exam_generation_responses import constraint_response
from ..daily_tips import sample_daily_tips
from ..question_review_markers import attach_review_markers
from ..exam_generation_drafts import ExamDraftContext, build_exam_paper_drafts
from ..exam_generation_publish import (
    GenerationPublicationRequest, publish_generation_batch, finalize_generation_publication,
)
from ..exam_generation_coverage_fence import require_unchanged_coverage_receipt
from ..exam_generation_memory_delivery import stage_generation_memory_delivery
from ..exam_generation_success import SuccessMemoryContext, build_success_memory_events, build_success_response
from ..exam_repetition_policy import question_content_fingerprint
from ..exam_manifests import append_exam_generation_receipt
from ..exam_contracts import ExamGenerationConstraintError


def base_draft_context(source, mode):
    """Adapt modern single-paper sources to the existing publication context."""
    return ExamDraftContext(
        source=mode, title=source.title, paper_count=1, exam_type=source.exam_type,
        chapter_names=list(source.chapters), difficulty=source.difficulty,
        target=source.target, question_count=source.question_count,
        question_types=list(source.question_types), note=source.note,
        question_bank_snapshot=None, question_bank_coverage=None,
        candidate_questions=[], candidate_pool=[], difficulty_quota=None,
        generated_difficulty_labels=[], generated_question_items=[],
        past_selections=[], reference_year=None, institution=None, reference_section=None,
        past_source=None, learning_samples=[], learning_mode=None, candidate_source="ordinary",
        source_quotas={}, past_exam_snapshot_id=None, past_analysis=None, chapter_plan=[],
        past_profile_status="missing", past_profile_id=None, past_profile_origin=None,
        past_diversity_audit=[], allow_adaptation=source.allow_adaptation,
    )


def complete_exam(source, context, reviewed, request, payload, *, generation_job_id,
                  db, config, memory, logger, compile_exam, store_exam_items,
                  progress, publication_fence, observe_metric, created_ids,
                  coverage, reference_mistakes=(), material_parts=(), candidate_count):
    mode = context.source
    progress(4, 7, "候选选择与结果校验完成")
    progress(5, 7, "正在生成排版内容")
    daily_tips = sample_daily_tips(db, selection_key=generation_job_id or request.operation_id)
    normalized, receipt = coverage(reviewed.items)
    require_unchanged_coverage_receipt(receipt, reviewed.coverage_receipt, stage="draft")
    items = attach_review_markers(db, [dict(item) for item in normalized])
    context = replace(
        context, generated_question_items=items,
        generated_difficulty_labels=[str(item.get("difficulty") or "").strip() for item in reviewed.items],
        draft_attempt=reviewed.draft_attempt, quality_reviews=reviewed.reviews,
        daily_tips=daily_tips, adaptation=reviewed.adaptation,
        difficulty_audits=reviewed.difficulty_audits, warnings=reviewed.warnings,
        coverage_receipt=reviewed.coverage_receipt,
    )
    drafts, created_at = build_exam_paper_drafts(reviewed.bodies, context)
    publication = GenerationPublicationRequest.from_exam_drafts(
        subject_id=source.subject_id, source=mode, generation_job_id=generation_job_id,
        drafts=drafts, reference_mistakes=reference_mistakes, created_at=created_at,
    )
    if len(publication.drafts) != 1:
        raise ExamGenerationConstraintError(
            "发布前覆盖校验只允许一份试卷", code="invalid_paper_count",
            hint="请重新生成单份试卷", actions=("retry_generation",),
            details={"actual_count": len(publication.drafts)}, status_code=422,
        )
    _, receipt = coverage(publication.drafts[0].to_exam_draft().structured_items)
    require_unchanged_coverage_receipt(receipt, reviewed.coverage_receipt, stage="pre_publish")

    def stage_memory(connection, published_result):
        events = build_success_memory_events(SuccessMemoryContext(
            source=mode, operation_id=request.operation_id, subject=source.subject,
            chapters=source.chapters,
            request={"difficulty": source.difficulty, "target": source.target,
                     "question_count": source.question_count, "types": list(source.question_types), "note": source.note},
            results=(published_result.to_dict(),), generated_question_items=tuple(items),
            allow_adaptation=source.allow_adaptation,
            selected_mistake_ids=tuple(payload.get("mistake_ids", [])),
        ), fingerprint=question_content_fingerprint)
        stage_generation_memory_delivery(
            connection, operation_id=request.operation_id, exam_id=published_result.id,
            mode=request.memory_type, actor=request.memory_actor,
            event_day=created_at[:10], published_at=created_at,
            events=(request.pending_memory_request, *events),
        )

    published = publish_generation_batch(
        publication, db=db, pdf_dir=config["PDF_DIR"], xelatex_command=config["XELATEX_COMMAND"],
        xelatex_timeout=config.get("XELATEX_TIMEOUT", 60), testing=bool(config.get("TESTING")),
        compile_exam=compile_exam, store_exam_items=store_exam_items, progress=progress,
        warn=logger.warning, publication_fence=publication_fence,
        stage_published=stage_memory, memory_delivery_operation_id=request.operation_id,
    )
    created_ids.extend(published.exam_ids)
    results = [item.to_dict() for item in published.results]
    response = build_success_response(
        results, source=mode, diversity_audit=[], past_profile_status="missing",
        past_profile_id=None, past_profile_origin=None, past_exam_snapshot_id=None, learning_samples=[],
        material_parts=material_parts,
        adaptation=reviewed.adaptation, warnings=reviewed.warnings,
        difficulty_audits=reviewed.difficulty_audits, coverage_receipt=reviewed.coverage_receipt,
    )
    observe_metric("candidate_count", candidate_count)
    # Keep this transition immediately before finalization: after it, memory
    # or receipt failures must never compensate the committed paper/files.
    created_ids.clear()
    completion = finalize_generation_publication(
        published, generation_job_id, db=db, database_path=config["DATABASE"], source=mode,
        operation_id=request.operation_id, response=response, receipt_writer=append_exam_generation_receipt,
        bank_index=reviewed.bank_index, candidate_questions=context.candidate_questions,
        candidate_pool=[], past_selections=[], generated_question_items=items, memory=memory,
        memory_type=request.memory_type, memory_actor=request.memory_actor,
        memory_events=[request.pending_memory_request], logger=logger,
    )
    if completion.error_body is not None:
        return dict(completion.error_body), completion.error_status
    return completion.results


def run_generation(action, request, runtime):
    """Run only post-preparation work inside the original compensation scope."""
    created_ids = []
    try:
        return action(created_ids)
    except WorkerStopRequested:
        if created_ids:
            compensate_exam_publication(
                runtime.db, created_ids, pdf_dir=runtime.config["PDF_DIR"],
                warn=runtime.logger.warning, memory_delivery_operation_id=request.operation_id,
            )
        raise
    except Exception as error:
        if created_ids:
            compensate_exam_publication(
                runtime.db, created_ids, pdf_dir=runtime.config["PDF_DIR"],
                warn=runtime.logger.warning, memory_delivery_operation_id=request.operation_id,
            )
        else:
            try:
                runtime.db.rollback()
            except Exception:
                try:
                    runtime.logger.warning("failed to roll back exam generation transaction", exc_info=True)
                except Exception:
                    pass
        if isinstance(error, JobError):
            raise
        if isinstance(error, BackgroundJobLookupError):
            runtime.logger.warning("background job lookup failed during exam generation", exc_info=True)
            return {"error": str(error), "code": error.code.lower()}, 503
        if isinstance(error, ExamGenerationConstraintError):
            return constraint_response(error)
        if isinstance(error, (DeepSeekConfigurationError, DeepSeekServiceError)):
            raise
        message = str(error) if isinstance(error, (TexCompileError, UnsafeLatexError)) else runtime.safe_error_text(error)
        return {"error": f"生成失败: {message}"}, 500
