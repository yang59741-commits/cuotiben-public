"""Past-exam learning characterization and profile checkpoint for stage P3.2c."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .ai import DeepSeekConfigurationError, DeepSeekServiceError
from .job_contracts import JobError
from .exam_manifests import (
    exam_manifest_payload,
    is_versioned_exam_manifest,
    manifest_stage,
    merge_exam_generation_manifest,
)
from .exam_selection import (
    EXAM_AI_TIMEOUT_SECONDS,
    build_section_slot_contract,
    largest_remainder_difficulty_quota,
)
from .past_exam_analysis import validate_past_analysis_result
from .past_exam_profile import material_understanding_from_profile, save_profile
from .past_exams import learning_sample_prompt_text


# User decision (2026-08-29): every task created under this policy keeps the
# full hard-feasible past candidate membership; cross-job history is only an
# existing semantic soft input or audit fact.  The identifiers are written at
# the earliest durable past checkpoint so a crash before candidate selection
# cannot silently fall back to the legacy physical-exclusion behavior.  They
# introduce no score and do not upgrade checkpoints that predate the policy.
PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION = "past-candidate-membership-v2"
PAST_CROSS_JOB_HISTORY_AUDIT_MARKER = (
    "past-cross-job-history-soft-signal-only-v1"
)
# This marker binds the one permitted mastery interaction: an exact frozen
# content fingerprint marked unmastered may cancel the *cross-job soft* recent
# signal.  The frozen fingerprint set and later derived recent-ID set are
# identity inputs, never candidate-membership or current-job hard parameters.
PAST_UNMASTERED_SOFT_EXEMPTION_MARKER = (
    "unmastered-exact-content-cancels-past-recent-soft-signal-v1"
)


@dataclass(frozen=True, slots=True)
class PastCharacterizationRequest:
    database: str
    generation_job_id: str | None
    institution: str
    subject: str
    question_count: int
    reference_year: int
    candidate_source: str
    learning_samples: tuple[Mapping[str, Any], ...]
    learning_years: tuple[int, ...]
    learning_mode: str
    past_learning_candidates: tuple[Mapping[str, Any], ...]
    past_exam_snapshot_id: str
    question_bank_snapshot: str
    structure_contract: Mapping[str, Any]
    available_chapters: tuple[Mapping[str, Any], ...]
    memory_context: str
    testing: bool
    manual_profile_required: bool
    past_profile_status: str
    past_profile_origin: str
    past_profile_id: str | None
    profile_write_root: str | None
    profile_identity: Mapping[str, Any] | None
    past_analysis: Mapping[str, Any] | None
    chapter_plan: tuple[Mapping[str, Any], ...] | None
    difficulty_quota: Mapping[str, int] | None
    frozen_manifest: Mapping[str, Any] | None
    saved_payload: Mapping[str, Any]
    versioned_manifest: bool
    staged_manifest: str | None
    manifest_was_present: bool
    ai_task_freeze_snapshot: Mapping[str, Any]
    recent_content_fingerprints: tuple[str, ...] = ()
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PastCharacterizationFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class PastCharacterizationResult:
    material_understanding: Mapping[str, Any]
    difficulty_quota: Mapping[str, int] | None
    past_difficulty_evidence: Mapping[str, Any]
    past_analysis: Mapping[str, Any]
    chapter_plan: tuple[Mapping[str, Any], ...]
    past_profile_status: str
    past_profile_origin: str
    past_profile_id: str | None
    frozen_manifest: Mapping[str, Any] | None
    saved_payload: Mapping[str, Any]
    versioned_manifest: bool
    staged_manifest: str | None
    manifest_was_present: bool
    checkpoint_created_here: bool
    has_candidate_checkpoint: bool
    recent_past_ids: tuple[str, ...]
    recent_content_fingerprints: tuple[str, ...] = ()
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PastCharacterizationOutcome:
    result: PastCharacterizationResult | None = None
    failure: PastCharacterizationFailure | None = None


def _failure(body: Mapping[str, Any], status: int) -> PastCharacterizationOutcome:
    return PastCharacterizationOutcome(
        failure=PastCharacterizationFailure(dict(body), int(status))
    )


def prepare_past_characterization(
    request: PastCharacterizationRequest,
    *,
    db: Any,
    deep_reader: Callable[..., Mapping[str, Any]],
    progress: Callable[[int, int, str], None],
    ai_call: Callable[..., str],
    parse_ai_json_object: Callable[[str], Mapping[str, Any]],
    safe_error_text: Callable[[Exception], str],
    has_background_job: Callable[[Any, str | None], bool],
    recent_question_ids: Callable[[str, str], set[str]],
    logger: Any,
    profile_summary_builder: Callable[..., Mapping[str, Any]] = (
        material_understanding_from_profile
    ),
    difficulty_evidence_builder: Callable[..., tuple[Any, Mapping[str, Any]]] = (
        largest_remainder_difficulty_quota
    ),
    analysis_validator: Callable[..., tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = (
        validate_past_analysis_result
    ),
    section_slot_builder: Callable[..., Mapping[str, Any]] = (
        build_section_slot_contract
    ),
    manifest_merger: Callable[..., Mapping[str, Any]] = (
        merge_exam_generation_manifest
    ),
    manifest_payload_reader: Callable[..., Mapping[str, Any]] = exam_manifest_payload,
    versioned_manifest_checker: Callable[..., bool] = is_versioned_exam_manifest,
    manifest_stage_reader: Callable[..., str | None] = manifest_stage,
    profile_saver: Callable[..., Any] = save_profile,
) -> PastCharacterizationOutcome:
    """Characterize fixed learning evidence before any past candidate-pool work."""

    learning_samples = [dict(item) for item in request.learning_samples]
    learning_years = [int(year) for year in request.learning_years]
    past_learning_candidates = [
        dict(item) for item in request.past_learning_candidates
    ]
    structure_contract = dict(request.structure_contract)
    available_chapters = [dict(item) for item in request.available_chapters]
    past_analysis = (
        dict(request.past_analysis) if request.past_analysis is not None else None
    )
    chapter_plan = (
        [dict(item) for item in request.chapter_plan]
        if request.chapter_plan is not None
        else None
    )
    difficulty_quota = (
        dict(request.difficulty_quota)
        if request.difficulty_quota is not None
        else None
    )
    past_profile_status = str(request.past_profile_status)
    past_profile_origin = str(request.past_profile_origin)
    past_profile_id = request.past_profile_id
    material_snapshot = (
        f"{request.past_exam_snapshot_id}:"
        f"{','.join(map(str, learning_years))}"
    )

    if request.manual_profile_required and past_analysis:
        material_understanding = profile_summary_builder(
            {
                "analysis": past_analysis,
                "profile_id": request.past_profile_id,
            },
            past_learning_candidates,
            material_snapshot,
        )
    else:
        material_understanding = deep_reader(
            past_learning_candidates,
            material_snapshot,
            "past_learning",
            request.generation_job_id,
        )

    evidence_quota, past_difficulty_evidence = difficulty_evidence_builder(
        material_understanding.get("cards") or [],
        request.question_count,
        learning_years=learning_years,
    )
    if evidence_quota is not None:
        difficulty_quota = dict(evidence_quota)

    if not (past_analysis and chapter_plan):
        progress(3, 7, "正在进行 AI 结构分析")
        learning_rule = (
            "下面按年份顺序提供所选结构年及此前最多两份有效真题，"
            f"本次共 {len(learning_samples)} 份。"
        )
        analysis_prompt = f'''你是{request.institution}{request.subject}考研真题分析专家。
{learning_rule}只能根据这些试卷学习命题特色，
分析共同的题型偏好、难度梯度、知识点分布和命题风格，不得参考其他年份。

【最终模拟卷必须采用的所选年份固定结构（只含结构，不含原题）】
{json.dumps(structure_contract, ensure_ascii=False)}

【本次学习年份：{"、".join(str(year) for year in learning_years)}】
{learning_sample_prompt_text(learning_samples)}

【逐年逐题深读归纳】
已完整深读 {material_understanding["analyzed_count"]} 道主问题；analysis_confidence 低于 0.6 的卡只能作为软提示，不能作为硬槽位或多样性事实；以下结论必须与上方完整原文交叉核验：
{json.dumps(material_understanding["summary"], ensure_ascii=False)}

【真题 AI 历史记忆】
{request.memory_context or '暂无历史记忆，只根据本次固定年份样本分析。'}

【允许使用的有效题库章节】
{json.dumps(available_chapters, ensure_ascii=False)}
广义逆、辛空间、场论初步及物理场论不在允许范围内。

只返回 JSON 对象，至少包含：
{{
  "learned_years": {json.dumps(learning_years, ensure_ascii=False)},
  "structure_summary": "结构概述",
  "sections": [{{"title":"板块","role":"考查目的","difficulty":"难度"}}],
  "topic_distribution": ["知识点"],
  "difficulty_progression": "难度变化",
  "style_rules": ["命题风格规则"],
  "chapter_plan": [
{{"chapter_id":1,"chapter":"章节名","question_count":2,"reason":"真题依据"}}
  ]
}}
chapter_plan 的题量总和必须恰好为 {request.question_count}。
不要输出 Markdown。'''
        analysis_messages = [
            {
                "role": "system",
                "content": (
                    f"你是{request.institution}{request.subject}真题结构分析专家，"
                    "必须只返回有效JSON。"
                ),
            },
            {"role": "user", "content": analysis_prompt},
        ]
        try:
            analysis_raw = ai_call(
                analysis_messages,
                temperature=0.1,
                timeout=EXAM_AI_TIMEOUT_SECONDS,
                task="exam_generation",
                task_policy="past_structure",
            )
            parsed_analysis = parse_ai_json_object(analysis_raw)
            try:
                past_analysis, chapter_plan = analysis_validator(
                    parsed_analysis,
                    learning_years,
                    structure_contract,
                    available_chapters,
                    request.question_count,
                    allow_testing_plan_fallback=bool(request.testing),
                )
            except ValueError as contract_error:
                expected_sections = [
                    item.get("label")
                    for item in structure_contract.get("sections") or []
                ]
                corrected_raw = ai_call(
                    [
                        *analysis_messages,
                        {"role": "assistant", "content": analysis_raw},
                        {
                            "role": "user",
                            "content": (
                                "真题分析未通过完整契约校验："
                                f"{contract_error}。请完整重写JSON；"
                                f"learned_years必须严格等于{learning_years}；"
                                "structure_summary、style_rules、"
                                "topic_distribution、difficulty_progression均不得为空；"
                                f"sections必须覆盖板块{expected_sections}；"
                                "chapter_plan只能使用允许章节且"
                                f"题量总和必须为{request.question_count}。"
                            ),
                        },
                    ],
                    temperature=0.0,
                    timeout=EXAM_AI_TIMEOUT_SECONDS,
                    task="exam_generation",
                    task_policy="past_structure",
                )
                corrected_analysis = parse_ai_json_object(corrected_raw)
                past_analysis, chapter_plan = analysis_validator(
                    corrected_analysis,
                    learning_years,
                    structure_contract,
                    available_chapters,
                    request.question_count,
                    allow_testing_plan_fallback=bool(request.testing),
                )
        except (DeepSeekConfigurationError, DeepSeekServiceError, JobError):
            raise
        except Exception as analysis_error:
            return _failure(
                {
                    "error": (
                        "真题结构分析失败："
                        f"{safe_error_text(analysis_error)}"
                    )
                },
                500,
            )
        past_profile_status = "runtime_ai"

    if not isinstance(past_analysis, Mapping) or not isinstance(
        chapter_plan, (list, tuple)
    ):
        raise RuntimeError("真题结构分析结果为空")
    past_analysis = dict(past_analysis)
    chapter_plan = [dict(item) for item in chapter_plan]
    evidence_metadata = dict(past_difficulty_evidence or {})
    past_analysis["difficulty_evidence"] = {
        **evidence_metadata,
        "quota": dict(difficulty_quota or {}),
        "mode": (
            "evidence"
            if evidence_metadata and not evidence_metadata.get("fallback_reason")
            else "fallback"
        ),
    }
    past_analysis["section_slot_contract"] = section_slot_builder(
        structure_contract,
        evidence_cards=material_understanding.get("cards") or [],
    )

    frozen_manifest = (
        dict(request.frozen_manifest)
        if request.frozen_manifest is not None
        else None
    )
    saved_payload = dict(request.saved_payload)
    versioned_manifest = bool(request.versioned_manifest)
    staged_manifest = request.staged_manifest
    manifest_was_present = bool(request.manifest_was_present)
    checkpoint_created_here = False
    recent_content_fingerprints = {
        str(value).strip()
        for value in request.recent_content_fingerprints
        if str(value).strip()
    }
    unmastered_content_fingerprints = {
        str(value).strip()
        for value in request.unmastered_content_fingerprints
        if str(value).strip()
    }
    if (
        saved_payload.get("past_history_signal_audit_marker")
        == PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
        and isinstance(saved_payload.get("recent_content_fingerprints"), list)
    ):
        recent_content_fingerprints = {
            str(value).strip()
            for value in saved_payload["recent_content_fingerprints"]
            if str(value).strip()
        }
    if (
        saved_payload.get("past_unmastered_soft_exemption_marker")
        == PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
        and isinstance(saved_payload.get("unmastered_content_fingerprints"), list)
    ):
        # Marked recovery uses the task-start mastery snapshot; it never reads
        # a later status change to reinterpret the same checkpoint.
        unmastered_content_fingerprints = {
            str(value).strip()
            for value in saved_payload["unmastered_content_fingerprints"]
            if str(value).strip()
        }
    stored_recent_past_ids = (
        saved_payload.get("recent_question_ids")
        if staged_manifest == "past-profile-frozen-v1"
        else None
    )
    has_candidate_checkpoint = (
        staged_manifest == "past-profile-frozen-v1"
        and isinstance(saved_payload.get("candidate_decision_candidate_ids"), list)
    )
    if has_candidate_checkpoint:
        recent_past_ids: set[str] = set()
    elif isinstance(stored_recent_past_ids, list):
        recent_past_ids = {
            str(value)
            for value in stored_recent_past_ids
            if str(value).strip()
        }
    else:
        recent_past_ids = (
            set(recent_question_ids("past", "past_exam"))
            if not versioned_manifest
            else set()
        )

    if (
        has_background_job(db, request.generation_job_id)
        and not versioned_manifest
        and frozen_manifest is None
    ):
        frozen_manifest = manifest_merger(
            request.database,
            request.generation_job_id,
            request.question_bank_snapshot,
            {
                "manifest_stage": "past-profile-frozen-v1",
                "past_analysis": past_analysis,
                "chapter_plan": chapter_plan,
                "past_profile": {
                    "status": past_profile_status,
                    "profile_id": past_profile_id,
                    "origin": past_profile_origin,
                    "source_snapshot": request.past_exam_snapshot_id,
                },
                "learning_years": learning_years,
                "learning_mode": request.learning_mode,
                "reference_year": request.reference_year,
                "candidate_source": request.candidate_source,
                "past_exam_snapshot": request.past_exam_snapshot_id,
                "recent_question_ids": sorted(recent_past_ids),
                "past_candidate_membership_policy_version": (
                    PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
                ),
                "past_history_signal_audit_marker": (
                    PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
                ),
                "past_unmastered_soft_exemption_marker": (
                    PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
                ),
                "unmastered_content_fingerprints": sorted(
                    unmastered_content_fingerprints
                ),
                "recent_content_fingerprints": sorted(
                    recent_content_fingerprints
                ),
                "ai_task_freezes": dict(request.ai_task_freeze_snapshot),
            },
        )
        saved_payload = dict(manifest_payload_reader(frozen_manifest))
        if (
            saved_payload.get("past_history_signal_audit_marker")
            == PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
            and isinstance(saved_payload.get("recent_content_fingerprints"), list)
        ):
            recent_content_fingerprints = {
                str(value).strip()
                for value in saved_payload["recent_content_fingerprints"]
                if str(value).strip()
            }
        if (
            saved_payload.get("past_unmastered_soft_exemption_marker")
            == PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
            and isinstance(
                saved_payload.get("unmastered_content_fingerprints"), list
            )
        ):
            unmastered_content_fingerprints = {
                str(value).strip()
                for value in saved_payload["unmastered_content_fingerprints"]
                if str(value).strip()
            }
        versioned_manifest = bool(versioned_manifest_checker(frozen_manifest))
        staged_manifest = manifest_stage_reader(frozen_manifest)
        if versioned_manifest:
            winner_analysis = saved_payload.get("past_analysis")
            winner_plan = saved_payload.get("chapter_plan")
            if isinstance(winner_analysis, Mapping):
                past_analysis = dict(winner_analysis)
                winner_difficulty = past_analysis.get("difficulty_evidence")
                if isinstance(winner_difficulty, Mapping):
                    evidence_metadata = dict(winner_difficulty)
                    winner_quota = winner_difficulty.get("quota")
                    if isinstance(winner_quota, Mapping):
                        difficulty_quota = dict(winner_quota)
            if isinstance(winner_plan, (list, tuple)):
                chapter_plan = [dict(item) for item in winner_plan]
            winner_profile = saved_payload.get("past_profile")
            if isinstance(winner_profile, Mapping):
                past_profile_status = str(
                    winner_profile.get("status") or past_profile_status
                )
                past_profile_origin = str(
                    winner_profile.get("origin") or past_profile_origin
                )
                winner_profile_id = winner_profile.get("profile_id")
                past_profile_id = (
                    str(winner_profile_id)
                    if winner_profile_id is not None
                    else None
                )
            if request.manual_profile_required and isinstance(
                winner_analysis, Mapping
            ):
                material_understanding = profile_summary_builder(
                    {
                        "analysis": past_analysis,
                        "profile_id": past_profile_id,
                    },
                    past_learning_candidates,
                    material_snapshot,
                )
            manifest_was_present = True
            checkpoint_created_here = False
            has_candidate_checkpoint = True
            recent_past_ids = set()
        else:
            checkpoint_created_here = True

    profile_root = (
        Path(request.profile_write_root)
        if request.profile_write_root is not None
        else None
    )
    profile_identity = (
        dict(request.profile_identity)
        if request.profile_identity is not None
        else None
    )
    if (
        request.manual_profile_required
        and profile_root is not None
        and profile_identity is not None
        and not saved_payload.get("past_analysis")
    ):
        try:
            profile_saver(
                profile_root,
                profile_identity,
                past_analysis,
                chapter_plan,
            )
        except (OSError, TypeError, ValueError):
            logger.warning(
                "past exam profile persistence skipped", exc_info=True
            )

    return PastCharacterizationOutcome(
        result=PastCharacterizationResult(
            material_understanding=dict(material_understanding),
            difficulty_quota=(
                dict(difficulty_quota) if difficulty_quota is not None else None
            ),
            past_difficulty_evidence=evidence_metadata,
            past_analysis=past_analysis,
            chapter_plan=tuple(chapter_plan),
            past_profile_status=past_profile_status,
            past_profile_origin=past_profile_origin,
            past_profile_id=past_profile_id,
            frozen_manifest=(
                dict(frozen_manifest) if frozen_manifest is not None else None
            ),
            saved_payload=dict(saved_payload),
            versioned_manifest=versioned_manifest,
            staged_manifest=staged_manifest,
            manifest_was_present=manifest_was_present,
            checkpoint_created_here=checkpoint_created_here,
            has_candidate_checkpoint=has_candidate_checkpoint,
            recent_past_ids=tuple(sorted(recent_past_ids)),
            recent_content_fingerprints=tuple(
                sorted(recent_content_fingerprints)
            ),
            unmastered_content_fingerprints=tuple(
                sorted(unmastered_content_fingerprints)
            ),
        )
    )


__all__ = [
    "PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION",
    "PAST_CROSS_JOB_HISTORY_AUDIT_MARKER",
    "PAST_UNMASTERED_SOFT_EXEMPTION_MARKER",
    "PastCharacterizationFailure",
    "PastCharacterizationOutcome",
    "PastCharacterizationRequest",
    "PastCharacterizationResult",
    "prepare_past_characterization",
]
