"""Shared application adapter for exam-generation runtime dependencies."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from .ai import prepare_ai_task_freezes, resolve_deepseek_api_key
from .ai_memory import memory_from_app
from .exam_generation import ExamGenerationRuntime
from .exam_manifests import (
    ExamManifestError,
    exam_manifest_payload,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
)
from .exam_material import understand_material
from .exam_items import store_exam_items_in_transaction
from .exam_generation_profile_worker import (
    resolve_persisted_past_profile_for_attempt,
)
from .exam_selection import validated_past_selection
from .exam_sources import (
    has_background_job,
    indexed_candidates,
    parse_ai_json_object,
    remove_exam_artifacts,
    require_current_past_exam_snapshot,
    require_current_question_bank_snapshot,
    safe_error_text,
)
from .exam_tex import (
    audit_question_difficulties,
    generate_indexed_free_exam,
    generate_problem_exam,
)
from .jobs import JobCoordinator


_EXAM_AI_FREEZE_BINDINGS = (
    ("exam_generation", "exam_generation"),
    ("exam_generation", "structured_exam"),
    ("exam_generation", "past_structure"),
    ("exam_generation", "candidate_selection"),
    ("exam_generation", "exam_item_repair"),
    ("exam_difficulty_review", "exam_difficulty_review"),
    ("exam_quality_review", "exam_quality_review"),
)
from .material_understanding import ensure_material_understanding
from .past_exams import (
    past_exam_source_snapshot,
    select_candidates_from_ai,
)
from .question_bank import (
    QuestionBankError,
    ensure_question_bank_index,
    question_bank_unavailable_error,
    open_last_good_question_bank_index,
    question_bank_status,
    submit_question_bank_rebuild_job,
)
from .semantic_recall import (
    RecallRequest,
    decide_semantic_recall,
    latest_published_projection,
    shadow_recall,
)
from .settings import get_settings_service


def _question_bank_index_path(app):
    return app.config.get("QUESTION_BANK_DATABASE") or app.config.get(
        "QUESTION_BANK_INDEX"
    )


def _current_question_bank_index(app):
    tex_base = app.config["TEX_BASE"]
    catalog = app.extensions["catalog_service"]
    index_path = _question_bank_index_path(app)
    status = question_bank_status(tex_base, index_path=index_path, catalog=catalog)
    last_good = open_last_good_question_bank_index(index_path)
    rebuild_job = None
    if status["stale"]:
        rebuild_job, _created = submit_question_bank_rebuild_job(
            JobCoordinator(app.config["DATABASE"]),
            status["source_snapshot"],
            max_attempts=2,
        )
    if last_good is not None:
        return last_good
    if app.config.get("TESTING"):
        return ensure_question_bank_index(
            tex_base, index_path=index_path, catalog=catalog
        )
    raise question_bank_unavailable_error(rebuild_job, status["source_snapshot"])


def build_exam_generation_runtime(
    app,
    db,
    *,
    ai_call,
    compile_exam,
    progress,
    request_id=None,
    question_bank_index_loader=None,
    observe_metric=None,
    past_attempt_resolver=None,
    allow_legacy_past_characterization=False,
    publication_fence=None,
):
    """Build the same explicit runtime for HTTP and durable-job adapters."""

    testing = bool(app.config.get("TESTING"))
    question_bank_index_loader = question_bank_index_loader or (
        lambda: _current_question_bank_index(app)
    )
    observe_metric = observe_metric or (lambda _name, _value: None)
    if allow_legacy_past_characterization and not testing:
        raise RuntimeError(
            "legacy past characterization is available only under TESTING"
        )
    if not allow_legacy_past_characterization and past_attempt_resolver is None:
        past_attempt_resolver = resolve_persisted_past_profile_for_attempt
    semantic_runtime_settings = get_settings_service(app).semantic_runtime_settings()
    requested_semantic_settings = semantic_runtime_settings["requested"]
    effective_semantic_settings = semantic_runtime_settings["effective"]
    recall_mode = str(effective_semantic_settings.get("recall_mode") or "shadow")
    recall_algorithm_version = str(
        effective_semantic_settings.get("algorithm_version")
        or "semantic-recall-v3"
    )
    candidate_multiplier = int(requested_semantic_settings.get("candidate_multiplier") or 4)
    allow_partial_cache = bool(semantic_runtime_settings["effective"].get("allow_partial_cache"))
    frozen_projection_error = None
    manifest_load_failed = False
    try:
        existing_manifest = (
            load_exam_generation_manifest(app.config["DATABASE"], request_id)
            if request_id
            else None
        )
    except ExamManifestError:
        existing_manifest = None
        manifest_load_failed = True
    existing_payload = exam_manifest_payload(existing_manifest)
    ai_task_freeze_snapshot = prepare_ai_task_freezes(
        request_id,
        _EXAM_AI_FREEZE_BINDINGS,
        snapshot=(existing_payload.get("ai_task_freezes") or None),
    )

    def frozen_ai_call(*args, **kwargs):
        if request_id:
            kwargs.setdefault("freeze_id", request_id)
        return ai_call(*args, **kwargs)

    if manifest_load_failed:
        frozen_projection_id = None
        frozen_projection_error = "manifest_read_error"
    elif is_versioned_exam_manifest(existing_manifest):
        stored_projection = str(existing_payload.get("projection_id") or "")
        frozen_projection_id = None if stored_projection == "unavailable" else stored_projection
    elif recall_mode.strip().casefold() == "off":
        frozen_projection_id = None
    else:
        try:
            frozen_projection_id = latest_published_projection(
                db, algorithm_version=recall_algorithm_version
            )
        except sqlite3.DatabaseError:
            frozen_projection_id = None
            frozen_projection_error = "projection_read_error"

    def audit_questions(question_texts, quota, subject):
        return audit_question_difficulties(
            question_texts, quota, subject, ai_call=frozen_ai_call
        )

    def select_past(
        raw,
        candidates,
        question_count,
        chapter_plan,
        difficulty_quota,
        **kwargs,
    ):
        audit_callback = kwargs.pop("audit_questions", audit_questions)
        return validated_past_selection(
            raw,
            candidates,
            question_count,
            chapter_plan,
            difficulty_quota,
            parser=parse_ai_json_object,
            selector=select_candidates_from_ai,
            testing=testing,
            audit_questions=audit_callback,
            **kwargs,
        )

    def generate_problem(sys_prompt, prompt, question_count, **kwargs):
        return generate_problem_exam(
            sys_prompt, prompt, question_count, ai_call=frozen_ai_call, **kwargs
        )

    def generate_indexed(
        sys_prompt,
        prompt,
        question_count,
        quota,
        subject,
        candidate_ids=(),
        allowed_question_types=(),
        allowed_chapters=(),
    ):
        return generate_indexed_free_exam(
            sys_prompt,
            prompt,
            question_count,
            quota,
            subject,
            candidate_ids,
            allowed_question_types,
            allowed_chapters,
            ai_call=frozen_ai_call,
            testing=testing,
        )

    def require_ai_config():
        if resolve_deepseek_api_key():
            return None
        return (
            {"error": "未配置 DeepSeek API 密钥；请在应用的 AI 设置中保存密钥"},
            503,
        )

    def require_bank_snapshot(expected_snapshot):
        return require_current_question_bank_snapshot(
            expected_snapshot, index_loader=question_bank_index_loader
        )

    def require_past_snapshot(expected_snapshot, subject):
        return require_current_past_exam_snapshot(
            expected_snapshot,
            subject,
            snapshot_loader=lambda value: past_exam_source_snapshot(
                app.config["PAST_EXAM_DIR"], subject=value
            ),
        )

    def material(candidates, snapshot, kind, generation_job_id=None):
        settings = get_settings_service(app)
        public_settings = settings.public_ai_settings()
        material_binding = dict(
            ai_task_freeze_snapshot.get("exam_generation:exam_generation") or {}
        )
        return understand_material(
            candidates,
            snapshot,
            kind,
            generation_job_id,
            testing=testing,
            model=str(
                material_binding.get("model")
                or settings.model_for_task("exam_generation")
            ),
            reasoning_effort=str(
                material_binding.get("reasoning_effort")
                or public_settings.get("reasoning_effort")
                or "max"
            ),
            wire_api=str(
                material_binding.get("wire_api")
                or public_settings.get("wire_api")
                or "chat_completions"
            ),
            cache_root=Path(app.instance_path) / "material_understanding",
            ensure_understanding=ensure_material_understanding,
            progress=lambda _current, _total, message: progress(2, 7, message),
        )

    def indexed_for_generation(index, subject, chapters, question_count, seed, **kwargs):
        return indexed_candidates(
            index,
            subject,
            chapters,
            question_count,
            seed,
            candidate_multiplier=candidate_multiplier,
            **kwargs,
        )

    def semantic_shadow(
        *,
        subject,
        chapters,
        question_count,
        note,
        question_types,
        difficulty,
        chapter_quotas,
        difficulty_quotas,
        hard_pool_loader,
        legacy_candidates,
    ):
        if frozen_projection_id is None:
            return None
        hard_pool = hard_pool_loader()
        return shadow_recall(
            db,
            projection_id=frozen_projection_id,
            request=RecallRequest(
                subject=subject,
                chapters=tuple(chapters),
                question_count=question_count,
                note=note,
                question_types=tuple(question_types),
                difficulty=difficulty,
                chapter_quotas=chapter_quotas,
                difficulty_quotas=difficulty_quotas,
            ),
            hard_pool=hard_pool,
            legacy_candidates=legacy_candidates,
            timestamp=datetime.now().astimezone().isoformat(),
            result_limit=(
                min(len(legacy_candidates), question_count * candidate_multiplier)
                if candidate_multiplier
                else None
            ),
        )

    def semantic_decision(
        *,
        subject,
        chapters,
        question_count,
        note,
        question_types,
        difficulty,
        chapter_quotas,
        difficulty_quotas,
        hard_pool_loader,
        legacy_candidates,
        recent_candidate_ids=(),
        required_source_counts=None,
    ):
        return decide_semantic_recall(
            db,
            mode=recall_mode,
            projection_id=frozen_projection_id,
            request=RecallRequest(
                subject=subject,
                chapters=tuple(chapters),
                question_count=question_count,
                note=note,
                question_types=tuple(question_types),
                difficulty=difficulty,
                chapter_quotas=chapter_quotas,
                difficulty_quotas=difficulty_quotas,
                recent_candidate_ids=frozenset(recent_candidate_ids),
            ),
            hard_pool_loader=hard_pool_loader,
            legacy_candidates=legacy_candidates,
            timestamp=datetime.now().astimezone().isoformat(),
            byte_budget=int(app.config.get("MAX_CONTENT_LENGTH") or 0) or None,
            required_source_counts=required_source_counts,
            projection_fallback_category=frozen_projection_error,
            candidate_multiplier=candidate_multiplier,
            allow_partial_cache=allow_partial_cache,
        )

    return ExamGenerationRuntime(
        db=db,
        config=app.config,
        memory=memory_from_app(app),
        logger=app.logger,
        request_id=request_id,
        validated_past_selection=select_past,
        generate_problem_exam=generate_problem,
        generate_indexed_free_exam=generate_indexed,
        require_deepseek_config=require_ai_config,
        question_bank_index=question_bank_index_loader,
        require_question_bank_snapshot=require_bank_snapshot,
        require_past_exam_snapshot=require_past_snapshot,
        indexed_candidates=indexed_for_generation,
        understand_material=material,
        remove_exam_artifacts=lambda exam_ids: remove_exam_artifacts(
            exam_ids,
            pdf_dir=app.config["PDF_DIR"],
            warn=app.logger.warning,
        ),
        safe_error_text=lambda error: safe_error_text(
            error, secrets=(app.config.get("DEEPSEEK_API_KEY"),)
        ),
        has_background_job=has_background_job,
        parse_ai_json_object=parse_ai_json_object,
        store_exam_items=store_exam_items_in_transaction,
        compile_exam=compile_exam,
        progress=progress,
        ai_call=frozen_ai_call,
        ai_task_freeze_snapshot=ai_task_freeze_snapshot,
        chapter_sources=app.extensions["catalog_service"].chapter_sources,
        app_context=app.app_context,
        semantic_recall_decision=semantic_decision,
        semantic_recall_shadow=semantic_shadow,
        semantic_settings_revision=int(semantic_runtime_settings["settings_revision"]),
        semantic_candidate_multiplier=candidate_multiplier,
        semantic_allow_partial_cache=allow_partial_cache,
        observe_metric=observe_metric,
        past_attempt_resolver=(
            None if allow_legacy_past_characterization else past_attempt_resolver
        ),
        allow_legacy_past_characterization=bool(
            allow_legacy_past_characterization
        ),
        publication_fence=publication_fence,
    )
