"""Snapshot fences and source-specific quality-input preparation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_quality import (
    QualityPaperInput,
    material_card_coverage,
    past_review_items,
    quality_reference_evidence,
    quality_slot_contracts,
    same_model_review_from_snapshot,
)
from .exam_local_repair import FrozenGenerationEvidence
from .exam_mistake_lineage_policy import (
    MistakeLineageSlot,
    validate_mistake_lineage_slots,
)
from .exam_quality_review import extract_review_items
from .exam_generation_coverage_fence import normalize_free_coverage_items
from .exam_contracts import ExamGenerationConstraintError


@dataclass(frozen=True, slots=True)
class FreeSnapshotFenceRequest:
    question_bank_snapshot: str


@dataclass(frozen=True, slots=True)
class PastSnapshotFenceRequest:
    question_bank_snapshot: str
    past_exam_snapshot_id: str
    subject: str


@dataclass(frozen=True, slots=True)
class SnapshotFenceResult:
    question_bank_index: Any


@dataclass(frozen=True, slots=True)
class FreeQualityInputRequest:
    bodies: tuple[str, ...]
    generated_question_items: tuple[Mapping[str, Any], ...]
    frozen_evidence: FrozenGenerationEvidence
    chapters: tuple[str, ...]
    question_types: tuple[str, ...]
    material_understanding: Mapping[str, Any] | None
    recall_effective_mode: str
    semantic_fallback_reason: str
    frozen_payload: Mapping[str, Any]
    ai_task_freeze_snapshot: Mapping[str, Mapping[str, Any]]
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MistakesQualityInputRequest:
    bodies: tuple[str, ...]
    question_count: int
    frozen_evidence: FrozenGenerationEvidence
    chapters: tuple[str, ...]
    question_types: tuple[str, ...]
    ai_task_freeze_snapshot: Mapping[str, Mapping[str, Any]]
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredMistakesQualityInputRequest:
    bodies: tuple[str, ...]
    structured_items: tuple[Mapping[str, Any], ...]
    lineage_slots: tuple[MistakeLineageSlot, ...]
    frozen_evidence: FrozenGenerationEvidence
    ai_task_freeze_snapshot: Mapping[str, Mapping[str, Any]]
    unmastered_content_fingerprints: tuple[str, ...] = ()
    training_target: str | None = None


@dataclass(frozen=True, slots=True)
class PastQualityInputRequest:
    bodies: tuple[str, ...]
    selections: tuple[tuple[Mapping[str, Any], ...], ...]
    candidate_pool: tuple[Mapping[str, Any], ...]
    chapter_names: tuple[str, ...]
    question_types: tuple[str, ...]
    past_analysis: Mapping[str, Any] | None
    allow_adaptation: bool
    material_understanding: Mapping[str, Any] | None
    recall_effective_mode: str
    semantic_fallback_reason: str
    frozen_payload: Mapping[str, Any]
    ai_task_freeze_snapshot: Mapping[str, Mapping[str, Any]]
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedQualityInputs:
    papers: tuple[QualityPaperInput, ...]
    allowed_chapters: tuple[str, ...]
    allowed_question_types: tuple[str, ...]
    same_model_review: bool
    quality_metadata: Mapping[str, Any]


def require_free_snapshot_fence(
    request: FreeSnapshotFenceRequest,
    *,
    require_question_bank_snapshot: Callable[[str], Any],
) -> SnapshotFenceResult:
    """Return the authoritative bank handle after the free-source fence."""

    return SnapshotFenceResult(
        question_bank_index=require_question_bank_snapshot(
            request.question_bank_snapshot
        )
    )


def require_past_snapshot_fence(
    request: PastSnapshotFenceRequest,
    *,
    require_question_bank_snapshot: Callable[[str], Any],
    require_past_exam_snapshot: Callable[[str, str], Any],
) -> SnapshotFenceResult:
    """Apply bank then past-source fences and retain the bank handle."""

    bank_index = require_question_bank_snapshot(request.question_bank_snapshot)
    require_past_exam_snapshot(
        request.past_exam_snapshot_id,
        request.subject,
    )
    return SnapshotFenceResult(question_bank_index=bank_index)


def _quality_metadata(
    *,
    recall_effective_mode: str,
    semantic_fallback_reason: str,
    frozen_payload: Mapping[str, Any],
    material_understanding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    fallback_reason = semantic_fallback_reason
    if not fallback_reason:
        fallback_reason = str(frozen_payload.get("fallback_reason") or "")
    return {
        "recall_mode": recall_effective_mode,
        "fallback_reason": fallback_reason,
        "card_coverage": material_card_coverage(material_understanding),
    }


def prepare_free_quality_inputs(
    request: FreeQualityInputRequest,
) -> PreparedQualityInputs:
    """Prepare the free paper without running the quality gate."""

    evidence_payload = request.frozen_evidence.payload()
    reference_evidence = quality_reference_evidence([
        *(evidence_payload.get("source_records") or []),
        *(evidence_payload.get("candidates") or []),
    ])
    fallback_reference_ids = sorted(reference_evidence)
    items = list(normalize_free_coverage_items(request.generated_question_items))
    for item in items:
        if (
            not item.get("reference_ids")
            and not item.get("candidate_id")
            and fallback_reference_ids
        ):
            item["reference_ids"] = list(fallback_reference_ids)
    paper = QualityPaperInput(
        body=request.bodies[0],
        items=tuple(items),
        reference_evidence=reference_evidence,
        frozen_evidence=request.frozen_evidence,
        max_adaptations=0,
        repetition_exempt_fingerprints=(
            request.unmastered_content_fingerprints
        ),
    )
    return PreparedQualityInputs(
        papers=(paper,),
        allowed_chapters=request.chapters,
        allowed_question_types=request.question_types,
        same_model_review=same_model_review_from_snapshot(
            request.ai_task_freeze_snapshot,
            generation_policy="structured_exam",
        ),
        quality_metadata=_quality_metadata(
            recall_effective_mode=request.recall_effective_mode,
            semantic_fallback_reason=request.semantic_fallback_reason,
            frozen_payload=request.frozen_payload,
            material_understanding=request.material_understanding,
        ),
    )


def prepare_mistakes_quality_inputs(
    request: MistakesQualityInputRequest,
    *,
    review_item_extractor: Callable[..., Sequence[Mapping[str, Any]]] = (
        extract_review_items
    ),
) -> PreparedQualityInputs:
    """Reject the retired unstructured/modulo mistake binding path."""

    _ = request, review_item_extractor
    raise ExamGenerationConstraintError(
        "旧错题质量输入缺少冻结题位谱系，已停止处理",
        code="mistake_lineage_required",
        hint="请使用结构化错题组卷重新生成",
        actions=("retry_generation",),
        details={"required_policy": "structured_exam"},
        status_code=422,
    )


def prepare_structured_mistakes_quality_inputs(
    request: StructuredMistakesQualityInputRequest,
) -> PreparedQualityInputs:
    """Consume lineage-bound items directly; no extraction or modulo binding."""

    if len(request.bodies) != 1:
        raise ValueError("结构化错题质量输入必须且只能包含一份正文")
    evidence_payload = request.frozen_evidence.payload()
    source_records = list(evidence_payload.get("source_records") or [])
    canonical_slots = validate_mistake_lineage_slots(
        source_records,
        request.lineage_slots,
    )
    slots = {slot.position: slot for slot in canonical_slots}
    source_ids = {
        str(record.get("public_id") or "").strip()
        for record in source_records
        if str(record.get("public_id") or "").strip()
    }
    items = []
    seen = set()
    for raw in request.structured_items:
        item = dict(raw)
        position = item.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or position in seen:
            raise ValueError("结构化错题质量项 position 无效或重复")
        seen.add(position)
        slot = slots.get(position)
        if slot is None:
            raise ValueError("结构化错题质量项包含未冻结题位")
        lineage_ids = item.get("source_mistake_ids")
        if not isinstance(lineage_ids, (list, tuple)) or tuple(lineage_ids) != slot.source_mistake_ids:
            raise ValueError(f"第{position}题错题来源与冻结题位不一致")
        if not set(lineage_ids).issubset(source_ids):
            raise ValueError(f"第{position}题来源不是冻结错题 public_id")
        if item.get("source_lineage_hash") != slot.source_lineage_hash:
            raise ValueError(f"第{position}题来源谱系哈希不一致")
        if item.get("lineage_policy_version") != slot.policy_version:
            raise ValueError(f"第{position}题来源谱系策略版本不一致")
        if item.get("chapter") != slot.target_chapter or item.get("type") != slot.target_question_type:
            raise ValueError(f"第{position}题结构约束与冻结题位不一致")
        item["target_chapter"] = slot.target_chapter
        item["target_question_type"] = slot.target_question_type
        item["reference_ids"] = list(slot.source_mistake_ids)
        items.append(item)
    if seen != set(slots):
        raise ValueError("结构化错题质量项未完整覆盖冻结题位")
    items.sort(key=lambda item: item["position"])
    reference_evidence = quality_reference_evidence(source_records)
    paper = QualityPaperInput(
        body=request.bodies[0],
        items=tuple(items),
        reference_evidence=reference_evidence,
        frozen_evidence=request.frozen_evidence,
        max_adaptations=0,
        repetition_exempt_fingerprints=request.unmastered_content_fingerprints,
    )
    return PreparedQualityInputs(
        papers=(paper,),
        allowed_chapters=tuple(sorted({slot.target_chapter for slot in slots.values()})),
        allowed_question_types=tuple(sorted({slot.target_question_type for slot in slots.values()})),
        same_model_review=same_model_review_from_snapshot(
            request.ai_task_freeze_snapshot,
            generation_policy="structured_exam",
        ),
        quality_metadata={"recall_mode": "off", "fallback_reason": "", "card_coverage": None,
                          **({"training_target": request.training_target}
                             if request.training_target else {})},
    )


def prepare_past_quality_inputs(
    request: PastQualityInputRequest,
) -> PreparedQualityInputs:
    """Prepare all past papers, slot contracts and review type bounds."""

    reference_evidence = quality_reference_evidence(request.candidate_pool)
    slot_contract = (
        request.past_analysis.get("section_slot_contract")
        if isinstance(request.past_analysis, Mapping)
        else None
    )
    slot_reviews = quality_slot_contracts(slot_contract)
    question_count = len(request.selections[0]) if request.selections else 0
    section_break_positions = []
    if isinstance(slot_contract, Mapping):
        for slot in slot_contract.get("slots") or ():
            if not isinstance(slot, Mapping):
                continue
            position_end = slot.get("position_end")
            if (
                isinstance(position_end, int)
                and not isinstance(position_end, bool)
                and 0 < position_end < question_count
            ):
                section_break_positions.append(position_end)
    frozen_section_break_positions = tuple(sorted(set(section_break_positions)))
    papers = []
    for body, selection in zip(request.bodies, request.selections):
        items = [dict(item) for item in past_review_items(selection)]
        for item in items:
            slot = slot_reviews.get(int(item["position"]))
            if slot:
                item["section"] = slot["section"]
                if slot["section"]:
                    # Past-exam candidates intentionally do not reuse their
                    # source `chapter` as a textbook chapter.  The frozen
                    # section contract is the authoritative review target.
                    item["target_chapter"] = slot["section"]
            if not str(item.get("target_chapter") or "").strip():
                fallback_chapter = str(
                    item.get("source_section") or item.get("section") or ""
                ).strip()
                if fallback_chapter:
                    item["target_chapter"] = fallback_chapter
        papers.append(QualityPaperInput(
            body=body,
            items=tuple(items),
            reference_evidence=reference_evidence,
            section_slot_contracts=slot_reviews,
            # Quality review never has permission to rewrite a selected source
            # item. User-authorized adaptation is a separate final stage.
            max_adaptations=0,
            past_section_break_positions=frozen_section_break_positions,
            repetition_exempt_fingerprints=(
                request.unmastered_content_fingerprints
            ),
        ))

    explicit_types = {
        str(item.get("question_type") or item.get("type") or "").strip()
        for paper in papers
        for item in paper.items
        if str(item.get("question_type") or item.get("type") or "").strip()
    }
    explicit_types.update(
        str(value).strip()
        for contract in slot_reviews.values()
        for value in contract.get("allowed_question_types") or []
        if str(value).strip()
    )
    allowed_question_types = (
        tuple(sorted(explicit_types))
        if explicit_types
        else request.question_types
    )
    review_chapters = list(request.chapter_names)
    for paper in papers:
        for item in paper.items:
            chapter = str(item.get("target_chapter") or "").strip()
            if chapter and chapter not in review_chapters:
                review_chapters.append(chapter)
    return PreparedQualityInputs(
        papers=tuple(papers),
        allowed_chapters=tuple(review_chapters),
        allowed_question_types=allowed_question_types,
        same_model_review=same_model_review_from_snapshot(
            request.ai_task_freeze_snapshot,
            generation_policy="candidate_selection",
        ),
        quality_metadata=_quality_metadata(
            recall_effective_mode=request.recall_effective_mode,
            semantic_fallback_reason=request.semantic_fallback_reason,
            frozen_payload=request.frozen_payload,
            material_understanding=request.material_understanding,
        ),
    )


__all__ = [
    "FreeQualityInputRequest",
    "FreeSnapshotFenceRequest",
    "MistakesQualityInputRequest",
    "StructuredMistakesQualityInputRequest",
    "PastQualityInputRequest",
    "PastSnapshotFenceRequest",
    "PreparedQualityInputs",
    "SnapshotFenceResult",
    "prepare_free_quality_inputs",
    "prepare_mistakes_quality_inputs",
    "prepare_structured_mistakes_quality_inputs",
    "prepare_past_quality_inputs",
    "require_free_snapshot_fence",
    "require_past_snapshot_fence",
]
