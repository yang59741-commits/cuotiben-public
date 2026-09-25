"""Generation-validation and TeX assembly stage for exam generation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .exam_publish import ExamPaperDraft
from .exam_tex import adapted_problem_count, assemble_exam_document
from .daily_tips import insert_daily_tips_before_first_problem


def _inject_review_marker_notes(
    body: str,
    structured_items: Sequence[Mapping[str, Any]],
) -> str:
    """Put display-only review notices into newly generated PDF bodies."""

    notes = {
        int(item["position"]): tuple(
            str(marker.get("label") or "").strip()
            for marker in item.get("review_markers", ())
            if isinstance(marker, Mapping)
            and str(marker.get("label") or "").strip()
        )
        for item in structured_items
        if str(item.get("position") or "").isdigit()
    }
    if not any(notes.values()):
        return body
    position = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal position
        position += 1
        labels = notes.get(position, ())
        if not labels:
            return match.group(0)
        return (
            match.group(0)
            + r"\par\textbf{"
            + "；".join(labels)
            + r"}"
            + r"\par"
        )

    return re.sub(r"\\begin\{problem\}", replace, body)


def _public_quality_review_summary(review: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a review to non-sensitive, stable publication metadata."""

    issue_counts: dict[str, int] = {}
    training_counts: dict[str, int] = {}
    training_findings: list[dict[str, Any]] = []
    label_counts: dict[str, dict[str, int]] = {
        "actual_chapter": {},
        "actual_question_type": {},
        "actual_difficulty": {},
    }
    raw_items = review.get("items")
    items = (
        raw_items
        if isinstance(raw_items, Sequence) and not isinstance(raw_items, (str, bytes))
        else ()
    )
    for raw_item in items:
        if not isinstance(raw_item, Mapping):
            continue
        issues = raw_item.get("issues")
        if isinstance(issues, Sequence) and not isinstance(issues, (str, bytes)):
            for issue in issues:
                key = str(issue or "").strip()
                if key:
                    issue_counts[key] = issue_counts.get(key, 0) + 1
        for field, counts in label_counts.items():
            label = str(raw_item.get(field) or "").strip()
            if label:
                counts[label] = counts.get(label, 0) + 1
        training_status = str(raw_item.get("training_target_status") or "").strip()
        if training_status in {"match", "mismatch", "unknown"}:
            training_counts[training_status] = training_counts.get(training_status, 0) + 1
        if training_status == "mismatch":
            training_findings.append({
                "position": int(raw_item.get("position") or 0),
                "status": "mismatch",
                "reason": str(raw_item.get("training_target_reason") or "")[:200],
            })
    failed_positions = review.get("failed_positions")
    if not isinstance(failed_positions, Sequence) or isinstance(
        failed_positions, (str, bytes)
    ):
        failed_positions = ()
    raw_repair = review.get("repair")
    repair = None
    if isinstance(raw_repair, Mapping):
        repair = {
            "contract_version": str(raw_repair.get("contract_version") or ""),
            "repaired_positions": [
                int(value)
                for value in (raw_repair.get("repaired_positions") or [])
                if isinstance(value, int) and not isinstance(value, bool) and value > 0
            ],
            "rereview_count": int(raw_repair.get("rereview_count") or 0),
            "passed": raw_repair.get("passed") is True,
        }
    summary = {
        "quality_mode": str(review.get("quality_mode") or "enforced"),
        "passed": review.get("passed") is True,
        "reviewed_count": len(items),
        "failed_positions": [
            int(value)
            for value in failed_positions
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        ],
        "same_model_review": review.get("same_model_review") is True,
        "prompt_version": str(review.get("prompt_version") or ""),
        "schema_version": str(review.get("schema_version") or ""),
        "recall_mode": str(review.get("recall_mode") or ""),
        "fallback_reason": str(review.get("fallback_reason") or ""),
        "card_coverage": review.get("card_coverage"),
        "training_target": str(review.get("training_target") or ""),
        "training_target_counts": dict(sorted(training_counts.items())),
        "training_target_findings": training_findings,
        "issue_counts": dict(sorted(issue_counts.items())),
        "label_counts": {
            field: dict(sorted(counts.items()))
            for field, counts in label_counts.items()
        },
    }
    if repair is not None:
        summary["repair"] = repair
    return summary


@dataclass(frozen=True)
class ExamDraftContext:
    source: str
    title: str
    paper_count: int
    exam_type: str
    chapter_names: Sequence[str]
    difficulty: str
    target: str
    question_count: int
    question_types: Sequence[str]
    note: str
    question_bank_snapshot: Any
    question_bank_coverage: Mapping[str, Any] | None
    candidate_questions: Sequence[Mapping[str, Any]]
    candidate_pool: Sequence[Mapping[str, Any]]
    difficulty_quota: Mapping[str, Any] | None
    generated_difficulty_labels: Sequence[str]
    generated_question_items: Sequence[Mapping[str, Any]]
    past_selections: Sequence[Sequence[Mapping[str, Any]]]
    reference_year: Any
    institution: Any
    reference_section: Mapping[str, Any] | None
    past_source: Mapping[str, Any] | None
    learning_samples: Sequence[Mapping[str, Any]]
    learning_mode: Any
    candidate_source: str
    source_quotas: Mapping[str, Any]
    past_exam_snapshot_id: Any
    past_analysis: Any
    chapter_plan: Sequence[Any]
    past_profile_status: str
    past_profile_id: Any
    past_profile_origin: Any
    past_diversity_audit: Sequence[Mapping[str, Any]]
    allow_adaptation: bool
    draft_attempt: int = 1
    quality_reviews: Sequence[Mapping[str, Any]] = ()
    daily_tips: Sequence[Mapping[str, object]] = ()
    adaptation: Mapping[str, Any] | None = None
    difficulty_audits: Sequence[Mapping[str, Any]] = ()
    warnings: Sequence[Mapping[str, Any]] = ()
    coverage_receipt: Mapping[str, Any] | None = None
    training_target: str | None = None


def build_exam_paper_drafts(
    latex_bodies: Sequence[str],
    context: ExamDraftContext,
) -> tuple[list[ExamPaperDraft], str]:
    """Validate source outputs and assemble publication-ready paper drafts."""

    if (
        isinstance(context.draft_attempt, bool)
        or not isinstance(context.draft_attempt, int)
        or context.draft_attempt < 1
    ):
        raise ValueError("草稿尝试编号无效")
    difficulty_audits_by_paper: dict[int, dict[str, Any]] = {}
    for raw_audit in context.difficulty_audits:
        if not isinstance(raw_audit, Mapping):
            raise ValueError("难度审计格式无效")
        paper_index = raw_audit.get("paper_index")
        if (
            isinstance(paper_index, bool)
            or not isinstance(paper_index, int)
            or paper_index < 1
            or paper_index > len(latex_bodies)
            or paper_index in difficulty_audits_by_paper
        ):
            raise ValueError("难度审计试卷编号重复或越界")
        difficulty_audits_by_paper[paper_index] = dict(raw_audit)
    if difficulty_audits_by_paper and len(difficulty_audits_by_paper) != len(
        latex_bodies
    ):
        raise ValueError("难度审计未覆盖全部试卷")

    drafts = []
    publication_created_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    for paper_index, current_body in enumerate(latex_bodies, start=1):
        current_title = (
            context.title
            if context.paper_count == 1
            else f"{context.title}（第{paper_index}份）"
        )
        latex_summary = (
            f"{context.exam_type} | 章节：{'、'.join(context.chapter_names[:5])} | "
            f"难度：{context.difficulty} | 场景：{context.target} | "
            f"题量：{context.question_count}题"
        )
        printable_body = (
            insert_daily_tips_before_first_problem(current_body, context.daily_tips)
            if context.source in {"free", "mistakes"}
            else current_body
        )
        latex_full = assemble_exam_document(current_title, latex_summary, printable_body)
        exam_config = {
            "source": context.source,
            "chapters": context.chapter_names,
            "difficulty": context.difficulty,
            "target": context.target,
            "question_count": context.question_count,
            "types": context.question_types,
            "note": context.note,
            "allow_adaptation": context.allow_adaptation,
            "max_adaptations": 1 if context.allow_adaptation else 0,
            "draft_attempt": context.draft_attempt,
            "adaptation": dict(context.adaptation or {}),
            "warnings": [dict(item) for item in context.warnings],
        }
        if context.source in {"free", "mistakes"}:
            coverage_receipt = getattr(context, "coverage_receipt", None)
            if isinstance(context, ExamDraftContext) and not isinstance(
                coverage_receipt, Mapping
            ):
                raise ValueError("自由/错题组卷缺少覆盖收据")
            if isinstance(coverage_receipt, Mapping):
                exam_config["minimum_coverage"] = dict(coverage_receipt)
        if paper_index in difficulty_audits_by_paper:
            exam_config["difficulty_audit"] = dict(
                difficulty_audits_by_paper[paper_index]
            )
        if context.source == "mistakes":
            if context.training_target:
                exam_config["training_target"] = context.training_target
            exam_config["adapted_count"] = (
                1
                if bool((context.adaptation or {}).get("completed"))
                and int((context.adaptation or {}).get("paper_index") or 0)
                == paper_index
                else adapted_problem_count(current_body)
            )
        if paper_index <= len(context.quality_reviews):
            exam_config["quality_review"] = _public_quality_review_summary(
                context.quality_reviews[paper_index - 1]
            )
        if context.source in {"free", "mistakes"}:
            exam_config["daily_tips"] = [dict(item) for item in context.daily_tips]
        if context.source in {"free", "past"}:
            exam_config.update(
                {
                    "question_bank_snapshot": context.question_bank_snapshot,
                    "question_bank_total_questions": int(
                        (context.question_bank_coverage or {}).get(
                            "total_questions", 0
                        )
                    ),
                    "candidate_count": len(
                        context.candidate_questions or context.candidate_pool
                    ),
                    "candidate_ids": [
                        item["candidate_id"]
                        for item in (
                            context.candidate_questions or context.candidate_pool
                        )
                    ],
                    "difficulty_quota": context.difficulty_quota,
                }
            )
        if context.source == "free":
            exam_config["generated_question_difficulties"] = (
                context.generated_difficulty_labels
            )
            exam_config["generated_question_references"] = [
                {
                    "position": int(item["position"]),
                    "reference_ids": item.get("reference_ids") or [],
                }
                for item in context.generated_question_items
            ]
        if context.source == "past":
            current_selection = context.past_selections[paper_index - 1]
            actual_source_counts = {
                "ordinary": sum(
                    str(item.get("source_kind") or "ordinary") == "ordinary"
                    for item in current_selection
                ),
                "past_exam": sum(
                    str(item.get("source_kind") or "ordinary") == "past_exam"
                    for item in current_selection
                ),
            }
            reference_section = context.reference_section or {}
            past_source = context.past_source or {}
            exam_config.update(
                {
                    "reference_year": context.reference_year,
                    "reference_institution": context.institution,
                    "reference_file": (
                        reference_section.get("relative_path")
                        or past_source["filename"]
                    ),
                    "reference_files": past_source["filenames"],
                    "learning_years": [
                        item["year"] for item in context.learning_samples
                    ],
                    "learning_sample_count": len(context.learning_samples),
                    "learning_mode": context.learning_mode,
                    "candidate_source": context.candidate_source,
                    "source_quotas": context.source_quotas,
                    "actual_source_counts": actual_source_counts,
                    "past_exam_snapshot": context.past_exam_snapshot_id,
                    "structure_analysis": context.past_analysis,
                    "chapter_plan": context.chapter_plan,
                    "past_profile": {
                        "status": context.past_profile_status,
                        "profile_id": context.past_profile_id,
                        "origin": context.past_profile_origin,
                        "source_snapshot": context.past_exam_snapshot_id,
                        "learning_years": [
                            item["year"] for item in context.learning_samples
                        ],
                    },
                    "batch_size": context.paper_count,
                    "batch_index": paper_index,
                    "diversity_policy": (
                        context.past_diversity_audit[paper_index - 1]
                        if paper_index <= len(context.past_diversity_audit)
                        else {}
                    ),
                    "adapted_count": sum(
                        bool(item.get("adapted")) for item in current_selection
                    ),
                    "selected_questions": [
                        {
                            "candidate_id": item["candidate_id"],
                            "source_kind": str(
                                item.get("source_kind") or "ordinary"
                            ),
                            "chapter": item.get("chapter"),
                            "source_file": item.get("source_file"),
                            "source_number": item.get("source_number"),
                            "source_institution": item.get("source_institution"),
                            "source_year": item.get("source_year"),
                            "source_section": item.get("source_section"),
                            "adapted": bool(item.get("adapted")),
                            "difficulty": item.get("difficulty"),
                        }
                        for item in current_selection
                    ],
                }
            )
        structured_snapshot_items = (
            context.generated_question_items
            if context.source in {"free", "mistakes"}
            else (
                context.past_selections[paper_index - 1]
                if context.source == "past"
                else []
            )
        )
        printable_body = _inject_review_marker_notes(
            printable_body,
            structured_snapshot_items,
        )
        latex_full = assemble_exam_document(
            current_title,
            latex_summary,
            printable_body,
        )
        drafts.append(
            ExamPaperDraft(
                title=current_title,
                body=current_body,
                config=exam_config,
                latex_source=latex_full,
                structured_items=structured_snapshot_items,
            )
        )
    return drafts, publication_created_at


__all__ = ["ExamDraftContext", "build_exam_paper_drafts"]
