"""Service-only historical past flow; current past jobs use the native D2 pipeline."""

from __future__ import annotations
import hashlib
from pathlib import Path
from typing import Any
from ..exam_manifests import (
    ExamManifestError,
    append_exam_generation_receipt,
)
from ..ai import DeepSeekConfigurationError, DeepSeekServiceError
from ..exam_contracts import ExamGenerationConstraintError, bounded_text, past_candidate_quotas, positive_int
from ..exam_quality_review import ExamQualityError
from ..exam_local_repair import LocalItemRepairError
from ..exam_repetition_policy import (
    load_latest_mistake_mastery_by_fingerprint,
    question_content_fingerprint,
)
from ..exam_selection import (
    PAST_CANDIDATE_SOURCES,
)
from ..exam_publish import compensate_exam_publication
from ..exam_sources import BackgroundJobLookupError
from ..job_contracts import JobError, WorkerStopRequested
from ..past_exams import PastExamSourceError
from ..question_bank import QuestionBankError
from ..semantic_recall import (
    SemanticRecallError,
)
from ..tex import TexCompileError, UnsafeLatexError
from ..exam_generation_drafts import ExamDraftContext, build_exam_paper_drafts
from ..exam_generation_quality import (
    enforce_exam_quality,
    validate_draft_quality_inputs,
)
from ..exam_generation_quality_inputs import PastQualityInputRequest, PastSnapshotFenceRequest, prepare_past_quality_inputs, require_past_snapshot_fence
from ..exam_generation_quality_stage import PastQualityStageRequest, run_past_quality_stage
from ..exam_difficulty_adjustment import adjust_exam_difficulty_once
from ..exam_tail_adaptation import (
    TAIL_ADAPTATION_CONTRACT_VERSION,
    attempt_tail_adaptation_once,
)
from ..exam_contracts import audit_difficulty_labels
from ..past_selection_checkpoints import (
    SelectionCheckpointConflictError,
    SelectionCheckpointError,
)
from ..exam_generation_past_learning import (
    PastLearningEvidenceRequest,
    prepare_past_learning_evidence,
)
from ..exam_generation_past_prerequisites import (
    PastQuestionBankPrerequisitesRequest,
    prepare_past_question_bank_prerequisites,
)
from ..exam_generation_past_manifest import (
    PastManifestPrerequisitesRequest,
    prepare_past_manifest_prerequisites,
)
from ..exam_generation_past_profile import (
    PastProfileResolutionRequest,
    prepare_past_profile_resolution,
)
from ..exam_generation_past_characterization import (
    PastCharacterizationRequest,
    prepare_past_characterization,
)
from ..exam_generation_past_candidates import (
    PastCandidatePoolRequest,
    prepare_past_candidate_pool,
)
from ..exam_generation_past_semantic import (
    PastSemanticFreezeRequest,
    prepare_past_semantic_freeze,
)
from ..exam_generation_past_material import prepare_past_final_material
from ..exam_generation_past_papers import (
    PastPaperCandidateOrderError,
    PastPaperGenerationRequest,
    generate_past_papers,
    validate_past_paper_selections,
)
from ..exam_generation_past_checkpoints import (
    PastSelectionCheckpointContext,
    PastSelectionCheckpointSession,
)
from ..exam_generation_policy import target_institution_uses_manual_profile
from ..exam_generation_publish import (
    GenerationPublicationRequest,
    finalize_generation_publication,
    publish_generation_batch,
)
from ..exam_generation_policy import should_regenerate_draft as _should_regenerate_draft
from ..exam_generation_responses import (
    selection_checkpoint_response as _selection_checkpoint_response, quality_failure_response,
)
from ..exam_generation_sources import QuestionBankScopeCache
from ..exam_generation_source_context import prepare_past_source_context, read_optional_recent_question_fingerprints, read_optional_recent_question_ids
from ..exam_generation_success import (
    SuccessMemoryContext,
    build_success_memory_events,
    build_success_response,
)
from ..question_review_markers import attach_review_markers
from ..exam_generation_responses import (
    constraint_response as _constraint_response,
    past_constraint_response as _past_constraint_response,
    past_source_response as _past_source_response,
    question_bank_response as _question_bank_response,
    semantic_recall_response as _semantic_recall_response,
)

def _checkpoint_papers_by_index(
    session: PastSelectionCheckpointSession,
) -> dict[int, dict[str, Any]]:
    return {
        int(paper["paper_index"]): dict(paper)
        for paper in session.checkpoint["papers"]
    }

def run_legacy_past_exam(d, runtime, frozen_request, memory, generation_job_id):
    jsonify = lambda value: value
    _validated_past_selection = runtime.validated_past_selection
    _generate_problem_exam = runtime.generate_problem_exam
    _generate_indexed_free_exam = runtime.generate_indexed_free_exam
    _require_deepseek_config = runtime.require_deepseek_config
    _question_bank_index = runtime.question_bank_index
    _require_current_question_bank_snapshot = runtime.require_question_bank_snapshot
    _require_current_past_exam_snapshot = runtime.require_past_exam_snapshot
    _indexed_candidates = runtime.indexed_candidates
    _understand_material = runtime.understand_material
    _safe_error_text = runtime.safe_error_text
    _has_background_job = runtime.has_background_job
    _parse_ai_json_object = runtime.parse_ai_json_object
    store_exam_items = runtime.store_exam_items
    compile_exam = runtime.compile_exam
    report_job_progress = runtime.progress
    deepseek_api_call = runtime.ai_call

    db = runtime.db
    source = "past"
    memory_type = frozen_request.memory_type
    memory_actor = frozen_request.memory_actor
    operation_id = frozen_request.operation_id
    # Keep the request in memory only after the whole output has compiled and
    # been published.  A failed AI response must leave no request/result/failure
    # trace that could later be learned as a successful generation attempt.
    pending_memory_request = frozen_request.pending_memory_request
    past_source = None
    past_analysis = None
    past_profile_status = "missing"
    past_profile_id = None
    past_profile_origin = None
    past_selections = []
    past_diversity_audit = []
    learning_samples = []
    learning_years = []
    learning_mode = None
    paper_count = 1
    allow_adaptation = frozen_request.allow_adaptation
    institution = None
    reference_year = None
    reference_section = None
    candidate_source = "ordinary"
    source_quotas = {}
    past_exam_snapshot_id = None
    created_exam_ids = []
    question_bank_snapshot = None
    question_bank_coverage = None
    candidate_questions = []
    candidate_pool = []
    recall_effective_mode = "off"
    semantic_fallback_reason = ""
    chapter_plan = []
    difficulty_quota = None
    generated_difficulty_labels = []
    generated_question_items = []
    memory_context = ""
    material_understanding = None
    selection_material_understanding = None
    recent_content_fingerprints = set()
    unmastered_content_fingerprints = set()
    mastery_fingerprints_loaded = False
    coverage_receipt: dict[str, Any] | None = None
    frozen_payload = {}
    training_target = None
    past_paper_request = None
    past_checkpoint_session = None
    past_selection_hashes: dict[int, str] = {}
    past_quality_reselection_used = False
    generate_past_selection_attempt = None
    ai_task_freeze_snapshot = dict(
        getattr(runtime, "ai_task_freeze_snapshot", None) or {}
    )

    # Coverage validation, candidate selection and semantic recall share one
    # immutable request-local source scope while callers receive fresh copies.
    bank_scope = QuestionBankScopeCache()

    # ── 真题模拟组卷模式 ──
    source_outcome = prepare_past_source_context(
        d,
        db=db,
        memory=memory,
        config=runtime.config,
        positive_int=positive_int,
        bounded_text=bounded_text,
        candidate_sources=PAST_CANDIDATE_SOURCES,
        candidate_quotas=past_candidate_quotas,
        source_error_response=_past_source_response,
    )
    if source_outcome.failure is not None:
        return jsonify(dict(source_outcome.failure.body)), source_outcome.failure.status
    source_context = source_outcome.context
    if source_context is None:
        raise RuntimeError("真题组卷题源上下文为空")
    sid = source_context.subject_id
    subj_name = source_context.subject
    ch_names = list(source_context.chapters)
    difficulty = source_context.difficulty
    target = source_context.target
    qcount = source_context.question_count
    types = list(source_context.question_types)
    note = source_context.note
    title = source_context.title
    exam_type = source_context.exam_type
    sys_prompt = source_context.system_prompt
    prompt = source_context.prompt
    memory_context = source_context.memory_context
    past_source = dict(source_context.past_source)
    institution = source_context.institution
    paper_count = source_context.paper_count
    allow_adaptation = source_context.allow_adaptation
    reference_year = source_context.reference_year
    reference_section = dict(source_context.reference_section)
    structure_contract = dict(source_context.structure_contract)
    candidate_source = source_context.candidate_source
    source_quotas = dict(source_context.source_quotas)
    mastery_by_fingerprint = load_latest_mistake_mastery_by_fingerprint(
        db,
        subject_id=sid,
    )
    unmastered_content_fingerprints = {
        fingerprint
        for fingerprint, status in mastery_by_fingerprint.items()
        if status == "未掌握"
    }
    mastery_fingerprints_loaded = True
    recent_content_fingerprints = read_optional_recent_question_fingerprints(
        memory, "past", "past_exam"
    )

    if not mastery_fingerprints_loaded:
        mastery_by_fingerprint = load_latest_mistake_mastery_by_fingerprint(
            db,
            subject_id=sid,
        )
        unmastered_content_fingerprints = {
            fingerprint
            for fingerprint, status in mastery_by_fingerprint.items()
            if status == "未掌握"
        }
        mastery_fingerprints_loaded = True
    try:
        learning_outcome = prepare_past_learning_evidence(
            PastLearningEvidenceRequest(
                past_source=past_source,
                reference_year=reference_year,
                past_exam_directory=str(runtime.config["PAST_EXAM_DIR"]),
                subject=subj_name,
            )
        )
        if learning_outcome.failure is not None:
            return (
                jsonify(dict(learning_outcome.failure.body)),
                learning_outcome.failure.status,
            )
        learning_evidence = learning_outcome.result
        if learning_evidence is None:
            raise RuntimeError("真题学习证据为空")
        learning_samples = [
            dict(item) for item in learning_evidence.learning_samples
        ]
        learning_mode = learning_evidence.learning_mode
        past_exam_snapshot_id = learning_evidence.past_exam_snapshot_id
        learning_years = list(learning_evidence.learning_years)
        past_learning_candidates = [
            dict(item) for item in learning_evidence.past_learning_candidates
        ]
        # Past papers are governed by the frozen per-position profile.
        # Do not synthesize a whole-paper ratio when profile evidence is
        # absent; that would compete with exact-slot requirements.
        difficulty_quota = None
        try:
            bank_index = _question_bank_index()
        except QuestionBankError as bank_error:
            return _question_bank_response(bank_error)
        except ValueError as bank_error:
            return jsonify({"error": str(bank_error)}), 503
        prerequisite_outcome = prepare_past_question_bank_prerequisites(
            PastQuestionBankPrerequisitesRequest(
                subject_id=sid,
                subject=subj_name,
            ),
            db=db,
            bank_index=bank_index,
            bank_scope=bank_scope,
        )
        if prerequisite_outcome.failure is not None:
            return (
                jsonify(dict(prerequisite_outcome.failure.body)),
                prerequisite_outcome.failure.status,
            )
        prerequisites = prerequisite_outcome.result
        if prerequisites is None:
            raise RuntimeError("真题题库前置结果为空")
        question_bank_snapshot = prerequisites.question_bank_snapshot
        available_chapters = [
            dict(item) for item in prerequisites.available_chapters
        ]

        manifest_outcome = prepare_past_manifest_prerequisites(
            PastManifestPrerequisitesRequest(
                database=str(runtime.config["DATABASE"]),
                generation_job_id=generation_job_id,
                question_bank_snapshot=question_bank_snapshot,
                past_exam_snapshot_id=past_exam_snapshot_id,
                reference_year=reference_year,
                candidate_source=candidate_source,
            ),
            db=db,
        )
        if manifest_outcome.failure is not None:
            return (
                jsonify(dict(manifest_outcome.failure.body)),
                manifest_outcome.failure.status,
            )
        manifest_context = manifest_outcome.result
        if manifest_context is None:
            raise RuntimeError("真题冻结清单前置结果为空")
        frozen_manifest = manifest_context.frozen_manifest
        saved_payload = dict(manifest_context.saved_payload)
        versioned_manifest = manifest_context.versioned_manifest
        staged_manifest = manifest_context.staged_manifest
        # Once a background task has a checkpoint, its candidate-decision
        # inputs are immutable for retry purposes.  The first invocation
        # alone may consult current recent-question memory.
        manifest_was_present = manifest_context.manifest_was_present
        # Only the configured target institution uses the portrait that was
        # reviewed by the external strong model.  Other institutions keep
        # the historical one-shot in-app structural analysis, but its
        # validated result is still frozen in the job manifest before any
        # candidate selection so retries remain deterministic.
        manual_profile_required = target_institution_uses_manual_profile(
            runtime.config, institution
        )
        profile_outcome = prepare_past_profile_resolution(
            PastProfileResolutionRequest(
                manual_profile_required=manual_profile_required,
                database=str(runtime.config["DATABASE"]),
                configured_profile_root=str(
                    runtime.config.get("PAST_EXAM_PROFILE_DIR") or ""
                ).strip(),
                institution=institution,
                subject=subj_name,
                reference_year=reference_year,
                learning_years=tuple(learning_years),
                past_exam_snapshot_id=past_exam_snapshot_id,
                question_bank_snapshot=question_bank_snapshot,
                frozen_analysis=saved_payload.get("past_analysis"),
                frozen_chapter_plan=saved_payload.get("chapter_plan"),
                structure_contract=structure_contract,
                available_chapters=tuple(available_chapters),
                question_count=qcount,
            ),
            safe_error_text=_safe_error_text,
        )
        if profile_outcome.failure is not None:
            return (
                jsonify(dict(profile_outcome.failure.body)),
                profile_outcome.failure.status,
            )
        profile_context = profile_outcome.result
        if profile_context is None:
            raise RuntimeError("真题画像准入结果为空")
        manual_profile_required = profile_context.manual_profile_required
        past_profile_status = profile_context.past_profile_status
        past_profile_origin = profile_context.past_profile_origin
        past_profile_id = profile_context.past_profile_id
        past_analysis = (
            dict(profile_context.past_analysis)
            if profile_context.past_analysis is not None
            else None
        )
        chapter_plan = (
            [dict(item) for item in profile_context.chapter_plan]
            if profile_context.chapter_plan is not None
            else None
        )
        profile_root = profile_context.profile_write_root
        profile_identity_value = (
            dict(profile_context.profile_identity)
            if profile_context.profile_identity is not None
            else None
        )

        characterization_outcome = prepare_past_characterization(
            PastCharacterizationRequest(
                database=str(runtime.config["DATABASE"]),
                generation_job_id=generation_job_id,
                institution=str(institution),
                subject=subj_name,
                question_count=qcount,
                reference_year=int(reference_year),
                candidate_source=candidate_source,
                learning_samples=tuple(learning_samples),
                learning_years=tuple(learning_years),
                learning_mode=str(learning_mode),
                past_learning_candidates=tuple(past_learning_candidates),
                past_exam_snapshot_id=past_exam_snapshot_id,
                question_bank_snapshot=question_bank_snapshot,
                structure_contract=structure_contract,
                available_chapters=tuple(available_chapters),
                memory_context=memory_context,
                testing=bool(runtime.config.get("TESTING")),
                manual_profile_required=manual_profile_required,
                past_profile_status=past_profile_status,
                past_profile_origin=str(past_profile_origin),
                past_profile_id=past_profile_id,
                profile_write_root=profile_root,
                profile_identity=profile_identity_value,
                past_analysis=past_analysis,
                chapter_plan=(
                    tuple(chapter_plan) if chapter_plan is not None else None
                ),
                difficulty_quota=difficulty_quota,
                frozen_manifest=frozen_manifest,
                saved_payload=saved_payload,
                versioned_manifest=versioned_manifest,
                staged_manifest=staged_manifest,
                manifest_was_present=manifest_was_present,
                ai_task_freeze_snapshot=ai_task_freeze_snapshot,
                recent_content_fingerprints=tuple(
                    sorted(recent_content_fingerprints)
                ),
                unmastered_content_fingerprints=tuple(
                    sorted(unmastered_content_fingerprints)
                ),
            ),
            db=db,
            deep_reader=_understand_material,
            progress=report_job_progress,
            ai_call=deepseek_api_call,
            parse_ai_json_object=_parse_ai_json_object,
            safe_error_text=_safe_error_text,
            has_background_job=_has_background_job,
            recent_question_ids=lambda namespace, actor: (
                read_optional_recent_question_ids(memory, namespace, actor)
            ),
            logger=runtime.logger,
        )
        if characterization_outcome.failure is not None:
            return (
                jsonify(dict(characterization_outcome.failure.body)),
                characterization_outcome.failure.status,
            )
        characterization = characterization_outcome.result
        if characterization is None:
            raise RuntimeError("真题学习画像结果为空")
        material_understanding = dict(characterization.material_understanding)
        # From this point onward the task uses the mastery snapshot frozen
        # with raw content history by the earliest past checkpoint.  A
        # retry must not reinterpret soft history, prompt audit, or final
        # identity from later memory/mastery changes.
        recent_content_fingerprints = set(
            characterization.recent_content_fingerprints
        )
        unmastered_content_fingerprints = set(
            characterization.unmastered_content_fingerprints
        )
        try:
            pool_context = prepare_past_candidate_pool(
                characterization,
                PastCandidatePoolRequest(
                    database=str(runtime.config["DATABASE"]),
                    generation_job_id=generation_job_id,
                    past_exam_directory=str(runtime.config["PAST_EXAM_DIR"]),
                    institution=str(institution),
                    subject=subj_name,
                    question_count=qcount,
                    question_bank_snapshot=question_bank_snapshot,
                    past_exam_snapshot_id=past_exam_snapshot_id,
                    candidate_source=candidate_source,
                    source_quotas=source_quotas,
                    manual_profile_required=manual_profile_required,
                    learning_years=tuple(learning_years),
                    past_learning_candidates=tuple(
                        past_learning_candidates
                    ),
                ),
                db=db,
                bank_index=bank_index,
                bank_scope=bank_scope,
                indexed_candidate_loader=_indexed_candidates,
                has_background_job=_has_background_job,
            )
            chapter_names = list(pool_context.chapter_names)
            chapter_quotas = dict(pool_context.chapter_quotas)
            question_bank_coverage = dict(
                pool_context.question_bank_coverage
            )
            selection_snapshot = pool_context.selection_snapshot
            ordinary_required = pool_context.ordinary_required
            other_required = pool_context.other_required
            ordinary_candidates = [
                dict(item) for item in pool_context.ordinary_candidates
            ]
            other_candidates = [
                dict(item) for item in pool_context.other_candidates
            ]
            other_pool = [dict(item) for item in pool_context.other_pool]
            candidate_pool = [
                dict(item) for item in pool_context.candidate_pool
            ]
            material_understanding = dict(
                pool_context.material_understanding
            )
            decision_recent_past_ids = set(
                pool_context.decision_recent_past_ids
            )
            recent_past_ids = set(pool_context.recent_past_ids)
            past_analysis = dict(pool_context.past_analysis)
            chapter_plan = [
                dict(item) for item in pool_context.chapter_plan
            ]
            difficulty_quota = (
                dict(pool_context.difficulty_quota)
                if pool_context.difficulty_quota is not None
                else None
            )
            past_profile_status = pool_context.past_profile_status
            past_profile_origin = pool_context.past_profile_origin
            past_profile_id = pool_context.past_profile_id
            frozen_manifest = (
                dict(pool_context.frozen_manifest)
                if pool_context.frozen_manifest is not None
                else None
            )
            saved_payload = dict(pool_context.saved_payload)
            versioned_manifest = pool_context.versioned_manifest
            staged_manifest = pool_context.staged_manifest
            semantic_request = PastSemanticFreezeRequest(
                database=str(runtime.config["DATABASE"]),
                generation_job_id=generation_job_id,
                past_exam_directory=str(runtime.config["PAST_EXAM_DIR"]),
                institution=str(institution),
                subject=subj_name,
                question_count=qcount,
                note=note,
                question_types=tuple(types),
                difficulty=difficulty,
                paper_count=paper_count,
                question_bank_snapshot=question_bank_snapshot,
                past_exam_snapshot_id=past_exam_snapshot_id,
                learning_years=tuple(learning_years),
                learning_mode=str(learning_mode),
                reference_year=reference_year,
                candidate_source=candidate_source,
                source_quotas=source_quotas,
                manual_profile_required=manual_profile_required,
                past_learning_candidates=tuple(past_learning_candidates),
                ai_task_freezes=ai_task_freeze_snapshot,
                semantic_settings_revision=getattr(
                    runtime, "semantic_settings_revision", 0
                ),
                semantic_candidate_multiplier=getattr(
                    runtime, "semantic_candidate_multiplier", 4
                ),
                semantic_allow_partial_cache=getattr(
                    runtime, "semantic_allow_partial_cache", False
                ),
                recent_content_fingerprints=tuple(
                    sorted(recent_content_fingerprints)
                ),
                unmastered_content_fingerprints=tuple(
                    sorted(unmastered_content_fingerprints)
                ),
            )
            semantic_context = prepare_past_semantic_freeze(
                pool_context,
                semantic_request,
                db=db,
                bank_index=bank_index,
                bank_scope=bank_scope,
                has_background_job=_has_background_job,
                semantic_recall_decision=runtime.semantic_recall_decision,
                semantic_recall_shadow=runtime.semantic_recall_shadow,
                logger=runtime.logger,
            )
            material_context = prepare_past_final_material(
                semantic_context,
                pool_context,
                semantic_request,
                db=db,
                bank_index=bank_index,
                bank_scope=bank_scope,
                selection_snapshot=selection_snapshot,
                deep_reader=_understand_material,
                has_background_job=_has_background_job,
            )
            semantic_context = material_context.semantic
            chapter_names = list(semantic_context.chapter_names)
            chapter_quotas = dict(semantic_context.chapter_quotas)
            question_bank_coverage = dict(
                semantic_context.question_bank_coverage
            )
            ordinary_candidates = [
                dict(item) for item in semantic_context.ordinary_candidates
            ]
            other_candidates = [
                dict(item) for item in semantic_context.other_candidates
            ]
            candidate_pool = [
                dict(item) for item in semantic_context.candidate_pool
            ]
            material_understanding = dict(
                semantic_context.material_understanding
            )
            recent_past_ids = set(semantic_context.recent_past_ids)
            past_analysis = dict(semantic_context.past_analysis)
            chapter_plan = [
                dict(item) for item in semantic_context.chapter_plan
            ]
            difficulty_quota = (
                dict(semantic_context.difficulty_quota)
                if semantic_context.difficulty_quota is not None
                else None
            )
            past_profile_status = semantic_context.past_profile_status
            past_profile_origin = semantic_context.past_profile_origin
            past_profile_id = semantic_context.past_profile_id
            frozen_manifest = semantic_context.frozen_manifest
            saved_payload = dict(semantic_context.saved_payload)
            versioned_manifest = semantic_context.versioned_manifest
            staged_manifest = semantic_context.staged_manifest
            recall_effective_mode = semantic_context.recall_effective_mode
            semantic_fallback_reason = semantic_context.fallback_reason or ""
            paper_seeds = list(semantic_context.paper_seeds)
            recent_content_fingerprints = set(
                semantic_context.recent_content_fingerprints
            )
            unmastered_content_fingerprints = set(
                semantic_context.unmastered_content_fingerprints
            )
            selection_material_understanding = dict(
                material_context.selection_material_understanding
            )
        except ExamManifestError as manifest_error:
            return jsonify({
                "error": str(manifest_error),
                "code": manifest_error.code,
            }), 409
        except SemanticRecallError as recall_error:
            return _semantic_recall_response(recall_error, source="past")
        except ExamGenerationConstraintError as constraint_error:
            return _past_constraint_response(constraint_error)
        except QuestionBankError as pool_error:
            return _question_bank_response(pool_error)
        except ValueError as pool_error:
            return jsonify({'error': str(pool_error)}), 503
        except PastExamSourceError as pool_error:
            return _past_source_response(pool_error, fallback_status=422)
        ch_names = chapter_names
        checkpoint_identity = str(generation_job_id or operation_id)
        checkpoint_path = (
            Path(str(runtime.config["DATABASE"])).parent
            / "material_understanding"
            / "past_selection"
            / (
                hashlib.sha256(
                    checkpoint_identity.encode("utf-8")
                ).hexdigest()
                + ".json"
            )
        )
        past_paper_request = PastPaperGenerationRequest(
            generation_job_id=generation_job_id,
            institution=str(institution),
            subject=subj_name,
            paper_count=paper_count,
            question_count=qcount,
            reference_year=reference_year,
            structure_contract=structure_contract,
            section_slot_contract=past_analysis.get(
                "section_slot_contract"
            ),
            past_analysis=past_analysis,
            material_understanding=selection_material_understanding,
            candidate_pool=tuple(candidate_pool),
            paper_seeds=tuple(paper_seeds),
            paper_candidate_orders=(
                tuple(
                    tuple(order) if isinstance(order, list) else ()
                    for order in (
                        saved_payload.get("paper_candidate_orders")
                        if isinstance(
                            saved_payload.get("paper_candidate_orders"),
                            list,
                        )
                        else ()
                    )
                )
                if versioned_manifest
                else None
            ),
            candidate_source=candidate_source,
            source_quotas=source_quotas,
            chapter_plan=tuple(chapter_plan),
            # The characterization quota is evidence metadata only.  It
            # must never become a past-paper selection or adjustment rule.
            difficulty_quota={},
            allow_adaptation=allow_adaptation,
            note=note,
            past_source=past_source,
            testing=bool(runtime.config.get("TESTING")),
            recent_question_ids=tuple(sorted(recent_past_ids)),
            recent_content_fingerprints=tuple(
                sorted(recent_content_fingerprints)
            ),
            unmastered_content_fingerprints=tuple(
                sorted(unmastered_content_fingerprints)
            ),
        )
        try:
            checkpoint_context = PastSelectionCheckpointContext.from_request(
                past_paper_request,
                checkpoint_identity,
            )
            past_checkpoint_session = PastSelectionCheckpointSession.open(
                checkpoint_path,
                checkpoint_context,
            )

            def generate_past_selection_attempt():
                result = generate_past_papers(
                    past_paper_request,
                    app_context=runtime.app_context,
                    ai_call=deepseek_api_call,
                    progress=report_job_progress,
                    selection_validator=_validated_past_selection,
                    selection_checkpoint=(
                        past_checkpoint_session.checkpoint
                    ),
                    job_hash=checkpoint_context.job_hash,
                    context_hash=checkpoint_context.context_hash,
                    upsert_selection_shard=(
                        past_checkpoint_session.upsert_selection_shard
                    ),
                    mark_selection_validated=(
                        past_checkpoint_session.mark_selection_validated
                    ),
                    reject_selection_candidates=(
                        past_checkpoint_session.reject_selection_candidates
                    ),
                    clear_selection_paper=(
                        past_checkpoint_session.clear_selection_paper
                    ),
                )
                if (
                    result.job_hash != checkpoint_context.job_hash
                    or result.context_hash != checkpoint_context.context_hash
                ):
                    raise SelectionCheckpointConflictError(
                        "past selection result identity does not match"
                    )
                return result

            past_result = generate_past_selection_attempt()
            past_selection_hashes = {
                int(paper_index): str(selection_hash)
                for paper_index, selection_hash
                in past_result.selection_hashes.items()
            }
        except PastPaperCandidateOrderError as flow_error:
            return jsonify({"error": str(flow_error)}), 409
        except SelectionCheckpointError as checkpoint_error:
            body, status = _selection_checkpoint_response(checkpoint_error)
            return jsonify(body), status
        except ExamGenerationConstraintError as flow_error:
            return _constraint_response(flow_error)
        except (DeepSeekServiceError, PastExamSourceError, ValueError) as flow_error:
            if isinstance(flow_error, DeepSeekServiceError):
                raise
            return jsonify({'error': _safe_error_text(flow_error)}), 500
        past_selections = [list(item) for item in past_result.selections]
        past_diversity_audit = [dict(item) for item in past_result.diversity_audit]
        latex_bodies = list(past_result.latex_bodies)
        latex_body = latex_bodies[0]

        snapshot_fence = require_past_snapshot_fence(
            PastSnapshotFenceRequest(
                question_bank_snapshot=question_bank_snapshot,
                past_exam_snapshot_id=past_exam_snapshot_id,
                subject=subj_name,
            ),
            require_question_bank_snapshot=(
                _require_current_question_bank_snapshot
            ),
            require_past_exam_snapshot=_require_current_past_exam_snapshot,
        )
        bank_index = snapshot_fence.question_bank_index
        quality_reviews = []
        response_warnings = []
        adaptation_result = {
            "contract_version": TAIL_ADAPTATION_CONTRACT_VERSION,
            "requested": bool(allow_adaptation),
            "completed": False,
        }
        difficulty_audits = []
        draft_attempt = 1
        difficulty_adjustment = {}
        quality_replacement_completed = False

        def prepare_current_quality_inputs():
            return prepare_past_quality_inputs(PastQualityInputRequest(
                bodies=tuple(latex_bodies),
                selections=tuple(tuple(items) for items in past_selections),
                candidate_pool=tuple(candidate_pool),
                chapter_names=tuple(chapter_names),
                question_types=tuple(types),
                past_analysis=past_analysis,
                allow_adaptation=allow_adaptation,
                material_understanding=selection_material_understanding,
                recall_effective_mode=recall_effective_mode,
                semantic_fallback_reason=semantic_fallback_reason,
                frozen_payload=frozen_payload,
                ai_task_freeze_snapshot=ai_task_freeze_snapshot,
                unmastered_content_fingerprints=tuple(
                    sorted(unmastered_content_fingerprints)
                ),
            ))

        try:
            while True:
                attempt_warnings = []
                try:
                    try:
                        prepared_quality = prepare_current_quality_inputs()
                    except ExamQualityError as exc:
                        raise
                    validate_draft_quality_inputs(
                        prepared_quality.papers,
                        allowed_chapters=prepared_quality.allowed_chapters,
                        allowed_question_types=(
                            prepared_quality.allowed_question_types
                        ),
                    )
                    difficulty_papers = [
                        [dict(item) for item in paper]
                        for paper in past_selections
                    ]

                    difficulty_breaks = (
                        (tuple(
                            paper.past_section_break_positions
                            for paper in prepared_quality.papers
                        ))
                    )
                    past_before_adjustment = (
                        (tuple(
                            tuple(dict(item) for item in paper)
                            for paper in difficulty_papers
                        ))
                    )
                    bodies_before_adjustment = tuple(latex_bodies)
                    eligible_paper_indexes = None
                    if past_checkpoint_session is None:
                        raise SelectionCheckpointConflictError(
                            "past selection checkpoint session is missing"
                        )
                    checkpoint_papers = _checkpoint_papers_by_index(
                        past_checkpoint_session
                    )
                    if any(
                        paper["validation_status"]
                        not in {"selection_validated", "quality_validated"}
                        for paper in checkpoint_papers.values()
                    ):
                        raise SelectionCheckpointConflictError(
                            "past selection checkpoint is not validated"
                        )
                    # Past exact-slot facts are authoritative.  The
                    # free/mistake whole-paper easy-ratio adjustment is not
                    # eligible to rewrite a past-paper slot.
                    eligible_paper_indexes = ()
                    adjustment_result = adjust_exam_difficulty_once(
                        bodies=tuple(latex_bodies),
                        papers=tuple(
                            tuple(dict(item) for item in paper)
                            for paper in difficulty_papers
                        ),
                        source_mode=source,
                        overall_difficulty=(
                            (None)
                        ),
                        candidate_pool=(
                            (tuple(candidate_pool))
                        ),
                        unmastered_fingerprints=tuple(
                            sorted(unmastered_content_fingerprints)
                        ),
                        ai_call=deepseek_api_call,
                        past_section_break_positions=difficulty_breaks,
                        eligible_paper_indexes=eligible_paper_indexes,
                    )
                    latex_bodies = list(adjustment_result.bodies)
                    latex_body = latex_bodies[0]
                    difficulty_papers = [
                        [dict(item) for item in paper]
                        for paper in adjustment_result.papers
                    ]
                    attempt_warnings.extend(
                        dict(item) for item in adjustment_result.warnings
                    )
                    difficulty_adjustment = dict(
                        adjustment_result.adjustment
                    )

                    if (
                        source == "past"
                        and difficulty_adjustment.get("completed")
                    ):
                        if (
                            past_paper_request is None
                            or past_checkpoint_session is None
                        ):
                            raise SelectionCheckpointConflictError(
                                "past selection validation context is missing"
                            )
                        try:
                            validated_adjustment = validate_past_paper_selections(
                                past_paper_request,
                                tuple(
                                    tuple(dict(item) for item in paper)
                                    for paper in difficulty_papers
                                ),
                                selection_checkpoint=(
                                    past_checkpoint_session.checkpoint
                                ),
                                job_hash=(
                                    past_checkpoint_session.context.job_hash
                                ),
                                context_hash=(
                                    past_checkpoint_session.context.context_hash
                                ),
                                selection_validator=_validated_past_selection,
                            )
                            changes = tuple(
                                (
                                    paper_index,
                                    position,
                                    str(before.get("candidate_id") or ""),
                                    str(after.get("candidate_id") or ""),
                                )
                                for paper_index, (before_paper, after_paper)
                                in enumerate(
                                    zip(
                                        past_before_adjustment,
                                        validated_adjustment.selections,
                                    ),
                                    start=1,
                                )
                                for position, (before, after) in enumerate(
                                    zip(before_paper, after_paper), start=1
                                )
                                if str(before.get("candidate_id") or "")
                                != str(after.get("candidate_id") or "")
                            )
                            changed_paper_index = int(
                                difficulty_adjustment.get("paper_index") or 0
                            )
                            changed_position = int(
                                difficulty_adjustment.get("position") or 0
                            )
                            if (
                                len(changes) != 1
                                or changes[0][0] != changed_paper_index
                                or changes[0][1] != changed_position
                                or not changes[0][2]
                                or not changes[0][3]
                            ):
                                raise ExamQualityError(
                                    "DIFFICULTY_ADJUSTMENT_IDENTITY",
                                    "难度换题未保持单一冻结题位",
                                )
                            replacement_hash = str(
                                validated_adjustment.selection_hashes[
                                    changed_paper_index
                                ]
                            )
                            past_checkpoint_session.downgrade_following(
                                changed_paper_index=changed_paper_index
                            )
                            past_checkpoint_session.replace_validated_candidate(
                                paper_index=changed_paper_index,
                                position=changed_position,
                                expected_candidate_id=changes[0][2],
                                replacement_candidate_id=changes[0][3],
                                validated_selection_hash=replacement_hash,
                            )
                            past_selection_hashes = {
                                int(paper_index): str(selection_hash)
                                for paper_index, selection_hash
                                in validated_adjustment.selection_hashes.items()
                            }
                            past_diversity_audit = [
                                dict(item)
                                for item in validated_adjustment.diversity_audit
                            ]
                            latex_bodies = list(
                                validated_adjustment.latex_bodies
                            )
                            latex_body = latex_bodies[0]
                            difficulty_papers = [
                                [dict(item) for item in paper]
                                for paper in validated_adjustment.selections
                            ]
                        except SelectionCheckpointError:
                            raise
                        except (ExamQualityError, ValueError) as exc:
                            difficulty_adjustment["completed"] = False
                            difficulty_adjustment["reason_code"] = str(
                                getattr(exc, "code", "")
                                or "DIFFICULTY_REPLACEMENT_VALIDATION_FAILED"
                            )
                            latex_bodies = list(bodies_before_adjustment)
                            latex_body = latex_bodies[0]
                            difficulty_papers = [
                                [dict(item) for item in paper]
                                for paper in past_before_adjustment
                            ]
                            attempt_warnings.append({
                                "code": "DIFFICULTY_ADJUSTMENT_NOT_APPLIED",
                                "message": (
                                    "简单题比例调整未完成，已保留原试卷。"
                                ),
                            })

                    past_selections = difficulty_papers
                    final_prepared = prepare_current_quality_inputs()

                    quality_stage = run_past_quality_stage(
                        PastQualityStageRequest(
                            prepared=final_prepared,
                            subject=subj_name,
                            testing=bool(runtime.config.get("TESTING")),
                            selections=tuple(
                                tuple(items) for items in past_selections
                            ),
                        ),
                        ai_call=deepseek_api_call,
                        quality_gate=enforce_exam_quality,
                    )
                    failed_reviews = tuple(
                        (
                            paper_index,
                            tuple(
                                quality_stage.replacement_positions[
                                    paper_index - 1
                                ]
                            ),
                        )
                        for paper_index, review in enumerate(
                            quality_stage.reviews, start=1
                        )
                        if (
                            review.get("quality_disposition")
                            == "replace_candidates"
                            or review.get("passed") is False
                        )
                    )
                    if failed_reviews:
                        failed_positions = tuple(
                            position
                            for _paper_index, positions in failed_reviews
                            for position in positions
                        )
                        if (
                            difficulty_adjustment.get("completed")
                            or past_quality_reselection_used
                            or len(failed_reviews) != 1
                            or len(failed_positions) > 1
                        ):
                            raise ExamQualityError(
                                "PAST_REPLACEMENT_LIMIT",
                                "最终质量审核需要再次改变题目，已停止发布",
                                failed_positions=failed_positions,
                            )
                        if (
                            past_checkpoint_session is None
                            or not callable(generate_past_selection_attempt)
                        ):
                            raise SelectionCheckpointConflictError(
                                "past selection retry context is missing"
                            )
                        failed_paper_index, positions = failed_reviews[0]
                        if positions:
                            position = positions[0]
                            selected_by_position = {
                                int(item.get("position")): item
                                for item in quality_stage.selections[
                                    failed_paper_index - 1
                                ]
                                if isinstance(item.get("position"), int)
                                and not isinstance(
                                    item.get("position"), bool
                                )
                            }
                            rejected_item = selected_by_position.get(position)
                            rejected_id = str(
                                (rejected_item or {}).get("candidate_id")
                                or ""
                            ).strip()
                            if not rejected_id:
                                raise ExamQualityError(
                                    "ITEM_SCHEMA",
                                    "最终质量审核失败题位缺少候选身份",
                                )
                            past_checkpoint_session.reject_selection_candidates(
                                paper_index=failed_paper_index,
                                rejected_candidate_ids_by_position={
                                    position: [rejected_id]
                                },
                            )
                        else:
                            past_checkpoint_session.clear_selection_paper(
                                paper_index=failed_paper_index
                            )
                        past_quality_reselection_used = True
                        quality_replacement_completed = True
                        past_result = generate_past_selection_attempt()
                        past_selection_hashes = {
                            int(paper_index): str(selection_hash)
                            for paper_index, selection_hash
                            in past_result.selection_hashes.items()
                        }
                        past_selections = [
                            [dict(item) for item in paper]
                            for paper in past_result.selections
                        ]
                        past_diversity_audit = [
                            dict(item)
                            for item in past_result.diversity_audit
                        ]
                        latex_bodies = list(past_result.latex_bodies)
                        latex_body = latex_bodies[0]
                        difficulty_adjustment = {}
                        continue

                    if (
                        len(quality_stage.reviews) != paper_count
                        or any(
                            review.get("passed") is not True
                            or review.get("quality_disposition")
                            != "accepted"
                            for review in quality_stage.reviews
                        )
                    ):
                        raise ExamQualityError(
                            "REVIEW_SCHEMA",
                            "最终质量审核状态不完整",
                        )
                    if past_checkpoint_session is None:
                        raise SelectionCheckpointConflictError(
                            "past selection checkpoint session is missing"
                        )
                    for paper_index in range(1, paper_count + 1):
                        reviewed_hash = past_selection_hashes.get(paper_index)
                        if not reviewed_hash:
                            raise SelectionCheckpointConflictError(
                                "reviewed selection hash is missing"
                            )
                        checkpoint_paper = _checkpoint_papers_by_index(
                            past_checkpoint_session
                        ).get(paper_index)
                        if checkpoint_paper is None:
                            raise SelectionCheckpointConflictError(
                                "selection checkpoint paper is missing"
                            )
                        status = checkpoint_paper["validation_status"]
                        if status == "selection_validated":
                            past_checkpoint_session.mark_quality_validated(
                                paper_index=paper_index,
                                reviewed_selection_hash=reviewed_hash,
                            )
                        elif (
                            status != "quality_validated"
                            or checkpoint_paper["selection_hash"]
                            != reviewed_hash
                        ):
                            raise SelectionCheckpointConflictError(
                                "quality checkpoint identity does not match"
                            )

                    latex_bodies = list(quality_stage.bodies)
                    latex_body = latex_bodies[0]
                    quality_reviews = [
                        {**dict(item), "draft_attempt": draft_attempt}
                        for item in quality_stage.reviews
                    ]
                    past_selections = [
                        [dict(item) for item in selection]
                        for selection in quality_stage.selections
                    ]
                    difficulty_papers = past_selections

                    difficulty_audits = []
                    audit_papers = (
                        ()
                    )
                    for paper_index, paper_items in enumerate(
                        audit_papers, start=1
                    ):
                        labels = [
                            str(item.get("difficulty") or "").strip()
                            for item in paper_items
                        ]
                        if not labels or not set(labels) <= {
                            "简单", "中等", "困难"
                        }:
                            raise ExamQualityError(
                                "DIFFICULTY_AUDIT_UNAVAILABLE",
                                "逐题难度结果不完整，已停止发布",
                            )
                        difficulty_audit = audit_difficulty_labels(
                            labels,
                            overall_difficulty=difficulty,
                        )
                        audit_payload = difficulty_audit.to_dict()
                        audit_payload["paper_index"] = paper_index
                        if (
                            difficulty_adjustment.get("requested")
                            and int(
                                difficulty_adjustment.get("paper_index") or 0
                            ) == paper_index
                        ):
                            audit_payload["adjustment"] = dict(
                                difficulty_adjustment
                            )
                        difficulty_audits.append(audit_payload)
                        if difficulty_audit.warning:
                            attempt_warnings.append({
                                "code": "DIFFICULTY_OUTSIDE_PREFERRED_RANGE",
                                "message": difficulty_audit.warning,
                                "paper_index": paper_index,
                            })
                    response_warnings = attempt_warnings
                    break
                except SelectionCheckpointError as checkpoint_error:
                    body, status = _selection_checkpoint_response(
                        checkpoint_error
                    )
                    return jsonify(body), status
                except ExamQualityError as draft_error:
                    if _should_regenerate_draft(
                        source=source,
                        draft_attempt=draft_attempt,
                        error=draft_error,
                    ):
                        draft_attempt = 2
                        latex_bodies = []
                        generated_question_items = []
                        generated_difficulty_labels = []
                        difficulty_adjustment = {}
                        quality_replacement_completed = False
                        continue
                    raise

            tail_papers = tuple(
                tuple(dict(item) for item in paper)
                for paper in difficulty_papers
            )
            if (
                difficulty_adjustment.get("completed")
                or quality_replacement_completed
            ):
                if allow_adaptation:
                    adaptation_result = {
                        "contract_version": TAIL_ADAPTATION_CONTRACT_VERSION,
                        "requested": True,
                        "completed": False,
                        "reason_code": (
                            "DIFFICULTY_ADJUSTMENT_ALREADY_APPLIED"
                            if difficulty_adjustment.get("completed")
                            else "QUALITY_REPLACEMENT_ALREADY_APPLIED"
                        ),
                    }
                    response_warnings.append({
                        "code": "ADAPTATION_NOT_COMPLETED",
                        "message": "本卷已更换一道题，未再执行改编。",
                    })
                final_papers = tail_papers
            else:
                tail_result = attempt_tail_adaptation_once(
                    bodies=tuple(latex_bodies),
                    papers=tail_papers,
                    allow_adaptation=allow_adaptation,
                    ai_call=deepseek_api_call,
                    subject=subj_name,
                    past_section_break_positions=difficulty_breaks,
                    unmastered_fingerprints=tuple(
                        sorted(unmastered_content_fingerprints)
                    ),
                )
                latex_bodies = list(tail_result.bodies)
                latex_body = latex_bodies[0]
                adaptation_result = dict(tail_result.adaptation)
                response_warnings.extend(
                    dict(item) for item in tail_result.warnings
                )
                final_papers = tail_result.papers
            past_selections = [
                [dict(item) for item in paper]
                for paper in final_papers
            ]
        except (ExamQualityError, LocalItemRepairError) as quality_error:
            return quality_failure_response(quality_error)
        report_job_progress(4, 7, "候选选择与结果校验完成")
        report_job_progress(5, 7, "正在生成排版内容")
        daily_tips = (
            ([])
        )
        past_selections = [
            attach_review_markers(db, selection)
            for selection in past_selections
        ]
        drafts, publication_created_at = build_exam_paper_drafts(
            latex_bodies,
            ExamDraftContext(
                source=source,
                title=title,
                paper_count=paper_count,
                exam_type=exam_type,
                chapter_names=ch_names,
                difficulty=difficulty,
                target=target,
                question_count=qcount,
                question_types=types,
                note=note,
                question_bank_snapshot=question_bank_snapshot,
                question_bank_coverage=question_bank_coverage,
                candidate_questions=candidate_questions,
                candidate_pool=candidate_pool,
                difficulty_quota=difficulty_quota,
                generated_difficulty_labels=generated_difficulty_labels,
                generated_question_items=generated_question_items,
                past_selections=past_selections,
                reference_year=reference_year,
                institution=institution,
                reference_section=reference_section,
                past_source=past_source,
                learning_samples=learning_samples,
                learning_mode=(learning_mode),
                candidate_source=candidate_source,
                source_quotas=source_quotas,
                past_exam_snapshot_id=past_exam_snapshot_id,
                past_analysis=past_analysis,
                chapter_plan=chapter_plan,
                past_profile_status=past_profile_status,
                past_profile_id=past_profile_id,
                past_profile_origin=(
                    (past_profile_origin)
                ),
                past_diversity_audit=past_diversity_audit,
                allow_adaptation=allow_adaptation,
                draft_attempt=draft_attempt,
                quality_reviews=quality_reviews,
                daily_tips=daily_tips,
                adaptation=adaptation_result,
                difficulty_audits=difficulty_audits,
                warnings=response_warnings,
                coverage_receipt=coverage_receipt,
                training_target=training_target,
            ),
        )
        publication_request = GenerationPublicationRequest.from_exam_drafts(
            subject_id=sid,
            source=source,
            generation_job_id=generation_job_id,
            drafts=drafts,
            reference_mistakes=(),
            created_at=publication_created_at,
        )


        published = publish_generation_batch(
            publication_request,
            db=db,
            pdf_dir=runtime.config['PDF_DIR'],
            xelatex_command=runtime.config['XELATEX_COMMAND'],
            xelatex_timeout=runtime.config.get('XELATEX_TIMEOUT', 60),
            testing=bool(runtime.config.get("TESTING")),
            compile_exam=compile_exam,
            store_exam_items=store_exam_items,
            progress=report_job_progress,
            warn=runtime.logger.warning,
            publication_fence=runtime.publication_fence,
            stage_published=(
                (None)
            ),
            memory_delivery_operation_id=(
                (None)
            ),
        )
        created_exam_ids.extend(published.exam_ids)
        results = [result.to_dict() for result in published.results]
        pending_memory_results = (
            (build_success_memory_events(
                SuccessMemoryContext(
                    source=source,
                    operation_id=operation_id,
                    subject=subj_name,
                    chapters=tuple(ch_names),
                    request={
                        "difficulty": difficulty,
                        "target": target,
                        "question_count": qcount,
                        "types": list(types),
                        "note": note,
                    },
                    results=tuple(results),
                    past_selections=tuple(tuple(items) for items in past_selections),
                    learning_years=tuple(learning_years),
                    past_analysis=past_analysis,
                    diversity_audit=tuple(past_diversity_audit),
                    allow_adaptation=allow_adaptation,
                ),
                fingerprint=question_content_fingerprint,
            ))
        )
        response = build_success_response(
            results,
            source=source,
            diversity_audit=past_diversity_audit,
            past_profile_status=past_profile_status,
            past_profile_id=past_profile_id,
            past_profile_origin=past_profile_origin,
            past_exam_snapshot_id=past_exam_snapshot_id,
            learning_samples=learning_samples,
            material_parts=tuple(
                part
                for part in (material_understanding, selection_material_understanding)
                if isinstance(part, dict)
            ),
            adaptation=adaptation_result,
            warnings=tuple(response_warnings),
            difficulty_audits=tuple(difficulty_audits),
            coverage_receipt=coverage_receipt,
        )
        candidate_count = (
            (len(candidate_pool))
        )
        runtime.observe_metric("candidate_count", int(candidate_count))
        # The database publication and its pending envelope are now durable.
        # Memory failures after this point are recoverable and must never
        # trigger exam/file compensation.
        created_exam_ids.clear()
        completion = finalize_generation_publication(
            published,
            generation_job_id,
            db=db,
            database_path=runtime.config["DATABASE"],
            source=source,
            operation_id=operation_id,
            response=response,
            receipt_writer=append_exam_generation_receipt,
            bank_index=(bank_index),
            candidate_questions=candidate_questions,
            candidate_pool=candidate_pool,
            past_selections=past_selections,
            generated_question_items=generated_question_items,
            memory=memory,
            memory_type=memory_type,
            memory_actor=memory_actor,
            memory_events=[pending_memory_request, *pending_memory_results],
            logger=runtime.logger,
        )
        if completion.error_body is not None:
            return jsonify(dict(completion.error_body)), completion.error_status
        return jsonify(completion.results)
    except WorkerStopRequested:
        if created_exam_ids:
            compensate_exam_publication(
                db,
                created_exam_ids,
                pdf_dir=runtime.config["PDF_DIR"],
                warn=runtime.logger.warning,
                memory_delivery_operation_id=(
                    (None)
                ),
            )
        raise
    except Exception as e:
        if created_exam_ids:
            compensate_exam_publication(
                db,
                created_exam_ids,
                pdf_dir=runtime.config["PDF_DIR"],
                warn=runtime.logger.warning,
                memory_delivery_operation_id=(
                    (None)
                ),
            )
        else:
            try:
                db.rollback()
            except Exception:
                try:
                    runtime.logger.warning(
                        "failed to roll back exam generation transaction",
                        exc_info=True,
                    )
                except Exception:
                    pass
        if isinstance(e, JobError):
            raise
        if isinstance(e, BackgroundJobLookupError):
            runtime.logger.warning(
                "background job lookup failed during exam generation",
                exc_info=True,
            )
            return jsonify({
                "error": str(e),
                "code": e.code.lower(),
            }), 503
        if isinstance(e, ExamGenerationConstraintError):
            body, status = _constraint_response(e)
            return jsonify(body), status
        if isinstance(e, (DeepSeekConfigurationError, DeepSeekServiceError)):
            raise
        if isinstance(e, (TexCompileError, UnsafeLatexError)):
            message = str(e)
        else:
            message = _safe_error_text(e)
        return jsonify({'error': f'生成失败: {message}'}), 500
