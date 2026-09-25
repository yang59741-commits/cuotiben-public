"""Coordinator-owned quality gate for generated exam paper drafts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_local_repair import FrozenGenerationEvidence
from .exam_quality_review import (
    TRAINING_REVIEW_PROMPT_VERSION,
    TRAINING_REVIEW_SCHEMA_VERSION,
    ExamQualityError,
    ExamQualityReview,
    apply_targeted_item_repairs,
    build_targeted_repair_plan,
    deterministic_exam_issues,
    extract_review_items,
    replace_exam_item_bodies,
    request_targeted_item_repair,
    review_exam_items,
)
from .exam_repetition_policy import question_content_fingerprint


_REFERENCE_SCALAR_FIELDS = (
    "difficulty", "chapter", "source_kind", "source_file", "source_number",
    "source_institution", "source_year", "source_section",
)
_REFERENCE_LIST_FIELDS = (
    ("knowledge_points", "knowledge_points"),
    ("methods", "methods"),
    ("traps", "traps"),
    ("traps", "common_traps"),
    ("source_constraints", "source_constraints"),
)


@dataclass(frozen=True, slots=True)
class QualityPaperInput:
    body: str
    items: Sequence[Mapping[str, Any]]
    reference_evidence: Mapping[str, object]
    frozen_evidence: FrozenGenerationEvidence | None = None
    section_slot_contracts: Mapping[int, Mapping[str, Any]] | None = None
    max_adaptations: int | None = None
    past_section_break_positions: tuple[int, ...] = ()
    repetition_exempt_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QualityGateResult:
    bodies: tuple[str, ...]
    items: tuple[tuple[dict[str, Any], ...], ...]
    reviews: tuple[dict[str, Any], ...]


def _exact_duplicate_positions(
    items: Sequence[Mapping[str, Any]],
    prior_question_texts: Sequence[str],
    repetition_exempt_fingerprints: Sequence[str] = (),
) -> set[int]:
    exempt = {
        str(value) for value in repetition_exempt_fingerprints if str(value)
    }
    seen = {
        question_content_fingerprint(value)
        for value in prior_question_texts
        if str(value or "").strip()
    }
    duplicates: set[int] = set()
    for position, item in enumerate(items, start=1):
        content = str(item.get("content") or item.get("latex") or "")
        if not content.strip():
            continue
        digest = question_content_fingerprint(content)
        if digest in exempt:
            continue
        if digest in seen:
            duplicates.add(position)
        seen.add(digest)
    return duplicates


def _actionable_review(
    review: ExamQualityReview,
    *,
    items: Sequence[Mapping[str, Any]],
    prior_question_texts: Sequence[str],
    repetition_exempt_fingerprints: Sequence[str] = (),
) -> tuple[ExamQualityReview, tuple[int, ...]]:
    """Ignore model similarity alone; retain duplicate failure only for exact bodies."""

    exact_duplicates = _exact_duplicate_positions(
        items,
        prior_question_texts,
        repetition_exempt_fingerprints,
    )
    ignored_similarity: list[int] = []
    normalized_items = []
    failed: list[int] = []
    for fallback, item in enumerate(review.items, start=1):
        copied = deepcopy(dict(item))
        position = int(copied.get("position") or fallback)
        issues = [str(value) for value in copied.get("issues") or ()]
        if "near_duplicate" in issues and position not in exact_duplicates:
            issues = [value for value in issues if value != "near_duplicate"]
            ignored_similarity.append(position)
        copied["issues"] = issues
        if issues:
            failed.append(position)
        normalized_items.append(copied)
    return (
        ExamQualityReview(
            items=tuple(normalized_items),
            passed=not failed,
            failed_positions=tuple(sorted(failed)),
            same_model_review=review.same_model_review,
            prompt_version=review.prompt_version,
            schema_version=review.schema_version,
        ),
        tuple(sorted(ignored_similarity)),
    )


def quality_reference_evidence(records: Sequence[Mapping[str, Any]]) -> dict[str, dict]:
    """Build the redacted candidate evidence map consumed by the quality gate."""

    result = {}
    for item in records or ():
        if not isinstance(item, Mapping):
            continue
        identity = ""
        for field in ("candidate_id", "public_id", "id"):
            value = item.get(field)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                identity = str(value).strip()
                if identity:
                    break
        content = ""
        for field in ("content", "latex"):
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                content = value.strip()
                break
        if identity and content and identity not in result:
            safe_item: dict[str, Any] = {
                "candidate_id": identity,
                "content": content,
            }
            for field in ("question_type", "type"):
                question_type = item.get(field)
                if isinstance(question_type, (str, int, float, bool)) and str(
                    question_type
                ).strip():
                    safe_item["question_type"] = question_type
                    break
            for field in _REFERENCE_SCALAR_FIELDS:
                value = item.get(field)
                if isinstance(value, (str, int, float, bool)) and str(
                    value
                ).strip():
                    safe_item[field] = value
            for field in ("confidence", "analysis_confidence"):
                confidence = item.get(field)
                if isinstance(confidence, (int, float)) and not isinstance(
                    confidence, bool
                ):
                    safe_item["confidence"] = confidence
                    break
            for output_field, source_field in _REFERENCE_LIST_FIELDS:
                if output_field in safe_item:
                    continue
                value = item.get(source_field)
                if isinstance(value, (list, tuple)):
                    strings = [
                        entry
                        for entry in value
                        if isinstance(entry, str) and entry.strip()
                    ]
                    if strings:
                        safe_item[output_field] = strings
            result[identity] = safe_item
    return result


def material_card_coverage(value: Mapping[str, Any] | None) -> float | None:
    if not isinstance(value, Mapping):
        return None
    analyzed = value.get("analyzed_count")
    missing = value.get("missing_count")
    if not isinstance(analyzed, int) or not isinstance(missing, int):
        return None
    total = analyzed + missing
    return round(analyzed / total, 4) if total else 0.0


def past_review_items(selection: Sequence[Mapping[str, Any]]) -> tuple[dict, ...]:
    result = []
    for fallback, raw in enumerate(selection, start=1):
        item = dict(raw)
        if str(item.get("source_kind") or "") == "past_exam":
            item["section"] = str(
                item.get("source_section") or item.get("chapter") or ""
            )
            item["chapter"] = ""
        adapted = str(item.get("adapted_content") or "").strip()
        item["position"] = int(item.get("position") or fallback)
        item["content"] = adapted or str(item.get("content") or "")
        item["adapted"] = bool(item.get("adapted")) or bool(adapted)
        candidate_id = str(item.get("candidate_id") or "")
        item["reference_ids"] = [candidate_id] if candidate_id else []
        result.append(item)
    return tuple(sorted(result, key=lambda value: int(value["position"])))


def quality_slot_contracts(contract: Mapping[str, Any] | None) -> dict[int, dict]:
    result = {}
    if not isinstance(contract, Mapping):
        return result
    for slot in contract.get("slots") or ():
        if not isinstance(slot, Mapping) or not slot.get("high_confidence_evidence_count"):
            continue
        converted = {
            "section": str(slot.get("section_label") or ""),
            "allowed_question_types": list(
                slot.get("allowed_question_type_families") or []
            ),
            "allowed_difficulties": list(slot.get("allowed_difficulties") or []),
        }
        for position in range(
            int(slot.get("position_start") or 0),
            int(slot.get("position_end") or -1) + 1,
        ):
            if position > 0:
                result[position] = converted
    return result


def same_model_review_from_snapshot(
    snapshot: Mapping[str, Mapping[str, Any]], *, generation_policy: str
) -> bool:
    generation = snapshot.get(f"exam_generation:{generation_policy}") or {}
    review = snapshot.get("exam_quality_review:exam_quality_review") or {}
    identity_fields = ("provider", "profile_id", "model")
    return bool(generation and review) and all(
        str(generation.get(field) or "") == str(review.get(field) or "")
        for field in identity_fields
    )


def _testing_review(
    items: Sequence[Mapping[str, Any]],
    *,
    expected_count: int,
    candidate_ids: Sequence[str],
    allowed_question_types: Sequence[str],
    allowed_chapters: Sequence[str],
    prior_question_texts: Sequence[str],
    section_slot_contracts: Mapping[int, Mapping[str, Any]] | None,
    max_adaptations: int | None,
    same_model_review: bool,
) -> ExamQualityReview:
    issues = deterministic_exam_issues(
        items,
        expected_count=expected_count,
        candidate_ids=candidate_ids,
        allowed_question_types=allowed_question_types,
        allowed_chapters=allowed_chapters,
        prior_question_texts=prior_question_texts,
        section_slot_contracts=section_slot_contracts,
        max_adaptations=max_adaptations,
    )
    failed = tuple(sorted(position for position, values in issues.items() if values))
    review_items = []
    for position, item in enumerate(items, start=1):
        position_issues = issues.get(position, [])
        review_items.append({
            "position": position,
            "actual_chapter": str(
                item.get("chapter") or item.get("target_chapter") or "未知"
            ),
            "actual_question_type": str(
                item.get("type") or item.get("question_type")
                or item.get("target_question_type") or "未知"
            ),
            "actual_difficulty": str(
                item.get("difficulty") or item.get("target_difficulty") or "未知"
            ),
            "section_slot_match": "section_slot_mismatch" not in position_issues,
            "reference_grounded": "reference_ungrounded" not in position_issues,
            "duplicate_group": "",
            "duplicate_risk": 1.0 if "near_duplicate" in position_issues else 0.0,
            "adaptation_scope_valid": "adaptation_scope_invalid" not in position_issues,
            "answer_leak": "answer_leak" in position_issues,
            "issues": list(position_issues),
            "confidence": 1.0,
        })
    return ExamQualityReview(
        tuple(review_items), not failed, failed, same_model_review
    )


def validate_draft_quality_inputs(
    papers: Sequence[QualityPaperInput],
    *,
    allowed_chapters: Sequence[str],
    allowed_question_types: Sequence[str],
) -> None:
    """Reject a malformed draft before difficulty or provider review.

    This stage is deterministic and read-only.  ``QUALITY_DRAFT_INVALID`` is
    reserved for defects proven to be in the generated draft, so the
    coordinator can distinguish them from frozen-input and provider failures.
    """

    prior_question_texts: list[str] = []
    for paper in papers:
        items = tuple(dict(item) for item in paper.items)
        try:
            extracted = extract_review_items(paper.body, expected_count=len(items))
            if paper.frozen_evidence is None:
                mismatched_positions = tuple(
                    position
                    for position, (body_item, structured_item) in enumerate(
                        zip(extracted, items), start=1
                    )
                    if str(body_item.get("content") or "").count(str(
                        structured_item.get("content")
                        or structured_item.get("latex")
                        or ""
                    ).strip()) != 1
                )
            else:
                mismatched_positions = tuple(
                    position
                    for position, (body_item, structured_item) in enumerate(
                        zip(extracted, items), start=1
                    )
                    if str(body_item.get("content") or "").strip()
                    != str(
                        structured_item.get("content")
                        or structured_item.get("latex")
                        or ""
                    ).strip()
                )
            if mismatched_positions:
                raise ExamQualityError(
                    "QUALITY_DRAFT_INVALID",
                    "草稿正文与结构化题位不一致",
                    failed_positions=mismatched_positions,
                    issue_codes=("body_item_mismatch",),
                )
            deterministic = _testing_review(
                items,
                expected_count=len(items),
                candidate_ids=tuple(str(value) for value in paper.reference_evidence),
                allowed_question_types=allowed_question_types,
                allowed_chapters=allowed_chapters,
                prior_question_texts=tuple(prior_question_texts),
                section_slot_contracts=paper.section_slot_contracts,
                max_adaptations=paper.max_adaptations,
                same_model_review=False,
            )
            deterministic, _ignored = _actionable_review(
                deterministic,
                items=items,
                prior_question_texts=tuple(prior_question_texts),
                repetition_exempt_fingerprints=(
                    paper.repetition_exempt_fingerprints
                ),
            )
        except ExamQualityError as exc:
            if exc.code == "QUALITY_DRAFT_INVALID":
                raise
            raise ExamQualityError(
                "QUALITY_DRAFT_INVALID",
                "草稿确定性结构或题源校验失败",
                failed_positions=tuple(
                    getattr(exc, "failed_positions", ()) or ()
                ),
                issue_codes=(str(exc.code),),
            ) from exc
        if not deterministic.passed:
            raise ExamQualityError(
                "QUALITY_DRAFT_INVALID",
                "草稿确定性结构或题源校验失败",
                failed_positions=deterministic.failed_positions,
                issue_codes=tuple(
                    sorted({
                        str(code)
                        for item in deterministic.items
                        for code in item.get("issues") or ()
                    })
                ),
            )
        prior_question_texts.extend(
            str(item.get("content") or item.get("latex") or "")
            for item in items
        )


def enforce_exam_quality(
    papers: Sequence[QualityPaperInput],
    *,
    subject: str,
    allowed_chapters: Sequence[str],
    allowed_question_types: Sequence[str],
    ai_call: Callable[..., str],
    same_model_review: bool,
    testing: bool = False,
    quality_metadata: Mapping[str, Any] | None = None,
    source_mode: str = "auto",
) -> QualityGateResult:
    """Review papers with source-specific, one-pass quality disposition."""

    normalized_source = str(source_mode or "auto").strip().casefold()
    if normalized_source not in {"auto", "free", "mistakes", "past"}:
        raise ExamQualityError("QUALITY_SOURCE", "试卷质量来源无效")

    bodies: list[str] = []
    final_items_by_paper: list[tuple[dict[str, Any], ...]] = []
    review_payloads: list[dict[str, Any]] = []
    prior_question_texts: list[str] = []

    for paper in papers:
        items = tuple(dict(item) for item in paper.items)
        body = paper.body
        paper_source = normalized_source
        if paper_source == "auto":
            paper_source = (
                str(paper.frozen_evidence.source)
                if paper.frozen_evidence is not None
                else "past"
            )
        candidate_ids = tuple(str(value) for value in paper.reference_evidence)
        training_target = (
            str((quality_metadata or {}).get("training_target") or "").strip()
            if paper_source == "mistakes" else ""
        )
        review_kwargs = {
            "subject": subject,
            "allowed_chapters": tuple(allowed_chapters),
            "allowed_question_types": tuple(allowed_question_types),
            "candidate_ids": candidate_ids,
            "ai_call": ai_call,
            "same_model_review": same_model_review,
            "reference_evidence": paper.reference_evidence,
            "prior_question_texts": tuple(prior_question_texts),
            "expected_count": len(items),
            "section_slot_contracts": paper.section_slot_contracts,
            "max_adaptations": paper.max_adaptations,
            "training_target": training_target or None,
        }
        if testing:
            review = _testing_review(
                items,
                expected_count=len(items),
                candidate_ids=candidate_ids,
                allowed_question_types=tuple(allowed_question_types),
                allowed_chapters=tuple(allowed_chapters),
                prior_question_texts=tuple(prior_question_texts),
                section_slot_contracts=paper.section_slot_contracts,
                max_adaptations=paper.max_adaptations,
                same_model_review=same_model_review,
            )
            if training_target:
                review = ExamQualityReview(
                    tuple({**dict(item), "training_target_status": "match",
                           "training_target_reason": ""} for item in review.items),
                    review.passed, review.failed_positions, review.same_model_review,
                    TRAINING_REVIEW_PROMPT_VERSION, TRAINING_REVIEW_SCHEMA_VERSION,
                )
        else:
            review = review_exam_items(items, **review_kwargs)

        repair_receipt = None
        mismatch_positions = tuple(
            int(item["position"]) for item in review.items
            if item.get("training_target_status") == "mismatch"
        )
        if (training_target and paper_source == "mistakes" and mismatch_positions
                and int((quality_metadata or {}).get("draft_attempt") or 1) == 1):
            plan = build_targeted_repair_plan(items, review)
            target = {**next(item for item in plan if item["position"] == mismatch_positions[0]),
                      "training_target": training_target}
            repaired_content = request_targeted_item_repair(
                target, ("training_target_mismatch",), paper.reference_evidence,
                ai_call=ai_call,
            )
            repairs = {int(target["position"]): repaired_content}
            items = apply_targeted_item_repairs(
                items, repairs, allowed_positions=(target["position"],)
            )
            body = replace_exam_item_bodies(
                paper.body, repairs,
                past_section_break_positions=paper.past_section_break_positions,
            )
            review = review_exam_items(items, **review_kwargs)
            repair_receipt = {
                "contract_version": "training-target-repair-v1",
                "repaired_positions": [int(target["position"])],
                "rereview_count": 1,
                "passed": review.passed,
            }

        review, ignored_similarity = _actionable_review(
            review,
            items=items,
            prior_question_texts=tuple(prior_question_texts),
            repetition_exempt_fingerprints=(
                paper.repetition_exempt_fingerprints
            ),
        )

        quality_disposition = "accepted"
        if not review.passed:
            if paper_source == "past":
                quality_disposition = "replace_candidates"
            else:
                raise ExamQualityError(
                    "QUALITY_DRAFT_REJECTED",
                    "最终只读质量审核未通过",
                    failed_positions=review.failed_positions,
                    issue_codes=tuple(
                        sorted({
                            str(code)
                            for item in review.items
                            for code in item.get("issues") or ()
                        })
                    ),
                )

        payload = review.as_payload()
        payload.update(dict(quality_metadata or {}))
        payload["quality_mode"] = "testing_deterministic" if testing else "enforced"
        payload["source_mode"] = paper_source
        payload["quality_disposition"] = quality_disposition
        if repair_receipt is not None:
            payload["repair"] = repair_receipt
        if quality_disposition == "replace_candidates":
            payload["replacement_positions"] = list(review.failed_positions)
        if ignored_similarity:
            payload["ignored_similarity_positions"] = list(ignored_similarity)
        bodies.append(body)
        final_items = tuple(dict(item) for item in items)
        final_items_by_paper.append(final_items)
        review_payloads.append(payload)
        prior_question_texts.extend(
            str(item.get("content") or item.get("latex") or "")
            for item in final_items
        )

    return QualityGateResult(
        tuple(bodies), tuple(final_items_by_paper), tuple(review_payloads)
    )


__all__ = [
    "QualityGateResult",
    "QualityPaperInput",
    "enforce_exam_quality",
    "material_card_coverage",
    "past_review_items",
    "quality_reference_evidence",
    "quality_slot_contracts",
    "same_model_review_from_snapshot",
    "validate_draft_quality_inputs",
]
