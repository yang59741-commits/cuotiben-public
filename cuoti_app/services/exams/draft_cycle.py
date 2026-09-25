"""Shared free/mistake draft retries, coverage fences and final adaptation."""

from dataclasses import dataclass, replace
from typing import Any

from ..exam_generation_contracts import ExamGenerationResult
from ..exam_generation_quality import validate_draft_quality_inputs
from ..exam_generation_coverage_fence import require_unchanged_coverage_receipt
from ..exam_difficulty_adjustment import adjust_exam_difficulty_once
from ..exam_tail_adaptation import TAIL_ADAPTATION_CONTRACT_VERSION, attempt_tail_adaptation_once
from ..exam_contracts import audit_difficulty_labels
from ..exam_tex import RawDraftValidationError, DifficultyAuditUnavailableError
from ..exam_quality_review import ExamQualityError
from ..exam_local_repair import LocalItemRepairError
from ..past_selection_checkpoints import SelectionCheckpointError
from ..exam_generation_responses import quality_failure_response, selection_checkpoint_response
from ..exam_generation_policy import should_regenerate_draft


@dataclass(frozen=True)
class ReviewedDraft:
    bodies: tuple[str, ...]
    items: tuple[dict, ...]
    coverage_receipt: dict
    reviews: tuple[dict, ...]
    difficulty_audits: tuple[dict, ...]
    warnings: tuple[dict, ...]
    adaptation: dict
    draft_attempt: int
    bank_index: Any


def _checked_items(items, coverage, receipt, stage):
    normalized, current = coverage(items)
    require_unchanged_coverage_receipt(current, receipt, stage=stage)
    return [dict(item) for item in normalized]


def _review_attempt(source, raw, items, receipt, attempt, *, mode, quality_inputs,
                    quality_review, prepare_difficulty, candidate_pool, unmastered,
                    coverage, ai_call):
    quality = quality_inputs(raw, items)
    validate_draft_quality_inputs(
        quality.papers, allowed_chapters=quality.allowed_chapters,
        allowed_question_types=quality.allowed_question_types,
    )
    difficulty_items = [dict(item) for item in quality.papers[0].items]
    if prepare_difficulty is not None:
        difficulty_items = prepare_difficulty(difficulty_items)
    adjustment = adjust_exam_difficulty_once(
        bodies=raw.bodies,
        papers=(tuple(difficulty_items),),
        source_mode=mode, overall_difficulty=source.difficulty,
        candidate_pool=candidate_pool,
        unmastered_fingerprints=unmastered, ai_call=ai_call,
        past_section_break_positions=(), eligible_paper_indexes=None,
    )
    adjusted_items = _checked_items(
        adjustment.papers[0], coverage, receipt, "difficulty_adjustment",
    )
    final_quality = replace(
        quality,
        papers=tuple(replace(paper, body=adjustment.bodies[index], items=tuple(adjusted_items))
                     for index, paper in enumerate(quality.papers)),
        quality_metadata={**dict(quality.quality_metadata), "draft_attempt": attempt},
    )
    reviewed = quality_review(final_quality)
    items = _checked_items(reviewed.generated_question_items, coverage, receipt, "quality")
    warnings = [dict(item) for item in adjustment.warnings]
    labels = [str(item.get("difficulty") or "").strip() for item in items]
    if not labels or not set(labels) <= {"简单", "中等", "困难"}:
        raise ExamQualityError("DIFFICULTY_AUDIT_UNAVAILABLE", "逐题难度结果不完整，已停止发布")
    audit = audit_difficulty_labels(labels, overall_difficulty=source.difficulty)
    audit_payload = {**audit.to_dict(), "paper_index": 1}
    if adjustment.adjustment.get("requested") and int(adjustment.adjustment.get("paper_index") or 0) == 1:
        audit_payload["adjustment"] = dict(adjustment.adjustment)
    if audit.warning:
        warnings.append({"code": "DIFFICULTY_OUTSIDE_PREFERRED_RANGE", "message": audit.warning, "paper_index": 1})
    return reviewed, items, dict(receipt), dict(adjustment.adjustment), audit_payload, warnings


def _adapt(source, reviewed, items, receipt, adjustment, warnings, *, coverage, unmastered, ai_call):
    adaptation = {
        "contract_version": TAIL_ADAPTATION_CONTRACT_VERSION,
        "requested": bool(source.allow_adaptation), "completed": False,
    }
    bodies = reviewed.bodies
    if adjustment.get("completed"):
        if source.allow_adaptation:
            adaptation["reason_code"] = "DIFFICULTY_ADJUSTMENT_ALREADY_APPLIED"
            warnings.append({"code": "ADAPTATION_NOT_COMPLETED", "message": "本卷已更换一道题，未再执行改编。"})
    else:
        tail = attempt_tail_adaptation_once(
            bodies=bodies, papers=(tuple(dict(item) for item in items),),
            allow_adaptation=source.allow_adaptation, ai_call=ai_call,
            subject=source.subject, past_section_break_positions=(),
            unmastered_fingerprints=unmastered,
        )
        bodies, items = tail.bodies, tail.papers[0]
        adaptation = dict(tail.adaptation)
        warnings.extend(dict(item) for item in tail.warnings)
    return bodies, _checked_items(items, coverage, receipt, "tail_adaptation"), adaptation


def review_draft(source, *, mode, generate_raw, quality_inputs, quality_review,
                 coverage, candidate_pool, unmastered, ai_call, progress,
                 snapshot_guard=None, prepare_difficulty=None):
    """At most two raw drafts; adaptation remains outside the retry loop."""
    attempt = 1
    try:
        while True:
            try:
                progress(3, 7, "正在进行 AI 分析与命题")
                try:
                    raw = generate_raw()
                except RawDraftValidationError as error:
                    raise ExamQualityError(
                        "QUALITY_DRAFT_INVALID", "试卷草稿未通过本地确定性校验",
                        issue_codes=("raw_draft_validation",),
                    ) from error
                except DifficultyAuditUnavailableError as error:
                    raise ExamQualityError("DIFFICULTY_AUDIT_UNAVAILABLE", "自由组卷难度审核不可用，已停止发布") from error
                # The coverage fence still precedes the snapshot read, even
                # when the snapshot dependency is no longer available.
                items, receipt = coverage(raw.structured_items)
                bank = snapshot_guard() if snapshot_guard is not None else None
                reviewed, items, receipt, adjustment, audit, warnings = _review_attempt(
                    source, raw, items, receipt, attempt, mode=mode,
                    quality_inputs=quality_inputs, quality_review=quality_review,
                    prepare_difficulty=prepare_difficulty, candidate_pool=candidate_pool,
                    unmastered=unmastered, coverage=coverage, ai_call=ai_call,
                )
                break
            except SelectionCheckpointError as error:
                return ExamGenerationResult(*selection_checkpoint_response(error))
            except ExamQualityError as error:
                if should_regenerate_draft(source=mode, draft_attempt=attempt, error=error):
                    attempt = 2
                    continue
                raise
        bodies, items, adaptation = _adapt(
            source, reviewed, items, receipt, adjustment, warnings, coverage=coverage,
            unmastered=unmastered, ai_call=ai_call,
        )
        return ReviewedDraft(
            tuple(bodies), tuple(items), receipt,
            tuple({**dict(item), "draft_attempt": attempt} for item in reviewed.reviews),
            (audit,), tuple(warnings), adaptation, attempt, bank,
        )
    except (ExamQualityError, LocalItemRepairError) as error:
        return ExamGenerationResult(*quality_failure_response(error))
