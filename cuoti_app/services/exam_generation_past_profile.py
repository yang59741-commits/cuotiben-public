"""Manual past-profile admission and read-only resolution for stage P3.2b2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .past_exam_analysis import validate_past_analysis_result
from .past_exam_profile import (
    DEFAULT_MODEL_PROFILE,
    load_profile,
    profile_identity,
)


@dataclass(frozen=True, slots=True)
class PastProfileResolutionRequest:
    manual_profile_required: bool
    database: str
    configured_profile_root: str
    institution: str
    subject: str
    reference_year: int
    learning_years: tuple[int, ...]
    past_exam_snapshot_id: str
    question_bank_snapshot: str
    frozen_analysis: object | None
    frozen_chapter_plan: object | None
    structure_contract: Mapping[str, Any]
    available_chapters: tuple[Mapping[str, Any], ...]
    question_count: int


@dataclass(frozen=True, slots=True)
class PastProfileResolutionFailure:
    body: Mapping[str, Any]
    status: int


@dataclass(frozen=True, slots=True)
class PastProfileResolutionResult:
    manual_profile_required: bool
    past_profile_status: str
    past_profile_origin: str
    past_profile_id: str | None
    past_analysis: Mapping[str, Any] | None
    chapter_plan: tuple[Mapping[str, Any], ...] | None
    profile_write_root: str | None
    profile_identity: Mapping[str, Any] | None


@dataclass(frozen=True, slots=True)
class PastProfileResolutionOutcome:
    result: PastProfileResolutionResult | None = None
    failure: PastProfileResolutionFailure | None = None


def _failure(body: Mapping[str, Any], status: int):
    return PastProfileResolutionOutcome(
        failure=PastProfileResolutionFailure(dict(body), int(status))
    )


def prepare_past_profile_resolution(
    request: PastProfileResolutionRequest,
    *,
    profile_loader: Callable[..., Mapping[str, Any] | None] = load_profile,
    identity_builder: Callable[..., Mapping[str, Any]] = profile_identity,
    analysis_validator: Callable[..., tuple[Mapping[str, Any], list[Mapping[str, Any]]]] = (
        validate_past_analysis_result
    ),
    safe_error_text: Callable[[Exception], str],
) -> PastProfileResolutionOutcome:
    """Resolve the trusted profile pair without material reads or writes."""

    manual_required = bool(request.manual_profile_required)
    status = "manual_required" if manual_required else "not_required"
    origin = (
        "manual_strong_model_review" if manual_required else "runtime_in_app_ai"
    )
    profile_id = None
    past_analysis = request.frozen_analysis
    chapter_plan = request.frozen_chapter_plan
    cached_profile = None
    profile_root = None
    identity = None

    if manual_required:
        identity = identity_builder(
            source_snapshot=request.past_exam_snapshot_id,
            question_bank_snapshot=request.question_bank_snapshot,
            institution=request.institution,
            subject=request.subject,
            reference_year=request.reference_year,
            learning_years=request.learning_years,
            model_profile=DEFAULT_MODEL_PROFILE,
        )
        configured_root = request.configured_profile_root.strip()
        profile_root = (
            Path(configured_root)
            if configured_root
            else Path(request.database).parent / "past_exam_profiles"
        )
        profile_roots = (
            profile_root,
            Path(request.database).parent / "instance" / "past_exam_profiles",
            Path(__file__).resolve().parents[2] / "instance" / "past_exam_profiles",
        )
        for candidate_root in dict.fromkeys(profile_roots):
            cached_profile = profile_loader(candidate_root, identity)
            if cached_profile is not None:
                break
        if cached_profile:
            profile_id = cached_profile.get("profile_id")
            if not (past_analysis and chapter_plan):
                past_analysis = cached_profile.get("analysis")
                chapter_plan = cached_profile.get("chapter_plan")
                status = "reused"

    if past_analysis and chapter_plan:
        try:
            past_analysis, chapter_plan = analysis_validator(
                past_analysis,
                list(request.learning_years),
                dict(request.structure_contract),
                [dict(item) for item in request.available_chapters],
                request.question_count,
                prefer_chapter_name=manual_required,
            )
        except ValueError as error:
            if manual_required and cached_profile and not request.frozen_analysis:
                past_analysis = None
                chapter_plan = None
                status = "invalidated"
            else:
                return _failure(
                    {
                        "error": (
                            "任务的冻结真题分析无效："
                            f"{safe_error_text(error)}"
                        )
                    },
                    409,
                )
    elif manual_required:
        past_analysis = None
        chapter_plan = None

    if request.frozen_analysis:
        status = "frozen"

    if not (past_analysis and chapter_plan) and manual_required:
        return _failure(
            {
                "error": "缺少已验证的强模型真题画像，请先导入画像文件后再组卷",
                "code": "PAST_PROFILE_REQUIRED",
                "retryable": False,
                "hint": "目标院校不会使用项目内置 AI 自动生成真题画像；导入强模型画像后重新提交",
                "actions": ["import_past_profile", "rescan_past_sources"],
                "profile": {
                    "status": status,
                    "profile_id": profile_id,
                    "source_snapshot": request.past_exam_snapshot_id,
                    "learning_years": list(request.learning_years),
                    "origin": "manual_strong_model_review",
                },
            },
            422,
        )

    return PastProfileResolutionOutcome(
        result=PastProfileResolutionResult(
            manual_profile_required=manual_required,
            past_profile_status=status,
            past_profile_origin=origin,
            past_profile_id=(str(profile_id) if profile_id is not None else None),
            past_analysis=(
                dict(past_analysis) if isinstance(past_analysis, Mapping) else None
            ),
            chapter_plan=(
                tuple(dict(item) for item in chapter_plan)
                if isinstance(chapter_plan, (list, tuple))
                else None
            ),
            profile_write_root=(str(profile_root) if profile_root is not None else None),
            profile_identity=(dict(identity) if identity is not None else None),
        )
    )


__all__ = [
    "PastProfileResolutionFailure",
    "PastProfileResolutionOutcome",
    "PastProfileResolutionRequest",
    "PastProfileResolutionResult",
    "prepare_past_profile_resolution",
]
