"""Free and mistake raw-paper generation behind source-specific contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_local_repair import FrozenGenerationEvidence
from .free_exam_flow import (
    FreeGenerationInput,
    FreeGenerationResult,
    generate_free_exam,
)
from .exam_mistake_lineage_policy import (
    MistakeLineageSlot,
    validate_mistake_lineage_slots,
)
from .exam_repetition_policy import question_content_fingerprint


@dataclass(frozen=True, slots=True)
class FreeRawGenerationRequest:
    """Serializable inputs required by the free raw-generation flow."""

    subject: str
    chapters: tuple[str, ...]
    question_count: int
    question_types: tuple[str, ...]
    difficulty: str
    target: str
    note: str
    candidates: tuple[Mapping[str, Any], ...]
    question_bank_coverage: Mapping[str, Any]
    difficulty_quota: Mapping[str, int]
    material_understanding: Mapping[str, Any]
    memory_context: str


@dataclass(frozen=True, slots=True)
class MistakesRawGenerationRequest:
    """Serializable inputs required by the mistakes raw-generation flow."""

    system_prompt: str
    prompt: str
    question_count: int
    frozen_evidence: FrozenGenerationEvidence


@dataclass(frozen=True, slots=True)
class StructuredMistakesRawGenerationRequest:
    """Strict indexed generation inputs with precomputed mistake lineage."""

    system_prompt: str
    prompt: str
    subject: str
    difficulty_quota: Mapping[str, int]
    lineage_slots: tuple[MistakeLineageSlot, ...]
    frozen_evidence: FrozenGenerationEvidence


@dataclass(frozen=True, slots=True)
class RawGeneratedBatch:
    """Raw paper facts consumed by snapshot and quality stages."""

    bodies: tuple[str, ...]
    structured_items: tuple[Mapping[str, Any], ...]
    difficulty_labels: tuple[str, ...]
    frozen_evidence: FrozenGenerationEvidence


def generate_free_raw_papers(
    request: FreeRawGenerationRequest,
    *,
    generate_indexed: Callable[..., tuple[str, Sequence[dict[str, Any]], Sequence[str]]],
    free_exam_generator: Callable[..., FreeGenerationResult] = generate_free_exam,
) -> RawGeneratedBatch:
    """Generate one free paper and canonicalize structured question order."""

    result = free_exam_generator(
        FreeGenerationInput(
            subject=request.subject,
            chapters=request.chapters,
            question_count=request.question_count,
            question_types=request.question_types,
            difficulty=request.difficulty,
            target=request.target,
            note=request.note,
            candidates=request.candidates,
            question_bank_coverage=request.question_bank_coverage,
            difficulty_quota=request.difficulty_quota,
            material_understanding=request.material_understanding,
            memory_context=request.memory_context,
        ),
        generate_indexed=generate_indexed,
    )
    structured_items = tuple(
        sorted(
            (dict(item) for item in result.structured_items),
            key=lambda item: int(item["position"]),
        )
    )
    return RawGeneratedBatch(
        bodies=(result.latex_body,),
        structured_items=structured_items,
        difficulty_labels=tuple(result.difficulty_labels),
        frozen_evidence=result.frozen_evidence,
    )


def generate_mistakes_raw_papers(
    request: MistakesRawGenerationRequest,
    *,
    generate_problem: Callable[..., str],
) -> RawGeneratedBatch:
    """Generate one mistakes paper while preserving upstream frozen evidence."""

    body = generate_problem(
        request.system_prompt,
        request.prompt,
        request.question_count,
        # Raw generation is not the user-authorized adaptation stage.
        max_adaptations=0,
    )
    return RawGeneratedBatch(
        bodies=(body,),
        structured_items=(),
        difficulty_labels=(),
        frozen_evidence=request.frozen_evidence,
    )


def generate_structured_mistakes_raw_papers(
    request: StructuredMistakesRawGenerationRequest,
    *,
    generate_indexed: Callable[..., tuple[str, Sequence[dict[str, Any]], Sequence[str]]],
) -> RawGeneratedBatch:
    """Generate and strictly bind every indexed item to its frozen slot."""

    evidence_payload = request.frozen_evidence.payload()
    slots = validate_mistake_lineage_slots(
        tuple(evidence_payload.get("source_records") or ()),
        request.lineage_slots,
    )
    source_ids = sorted({value for slot in slots for value in slot.source_mistake_ids})
    body, generated, _labels = generate_indexed(
        request.system_prompt,
        request.prompt,
        len(slots),
        dict(request.difficulty_quota),
        request.subject,
        source_ids,
        sorted({slot.target_question_type for slot in slots}),
        sorted({slot.target_chapter for slot in slots}),
    )
    by_position: dict[int, Mapping[str, Any]] = {}
    for item in generated:
        position = item.get("position")
        if isinstance(position, bool) or not isinstance(position, int) or position in by_position:
            raise ValueError("结构化错题试卷 position 无效或重复")
        by_position[position] = item
    if set(by_position) != {slot.position for slot in slots}:
        raise ValueError("结构化错题试卷 position 与冻结题位不一致")

    structured = []
    for slot in slots:
        item = by_position[slot.position]
        references = item.get("reference_ids")
        if not isinstance(references, (list, tuple)) or any(
            not isinstance(value, str) for value in references
        ):
            raise ValueError(f"第{slot.position}题 reference_ids 无效")
        if tuple(references) != slot.source_mistake_ids:
            raise ValueError(f"第{slot.position}题 reference_ids 与冻结题位不一致")
        chapter = str(item.get("chapter") or "").strip()
        question_type = str(item.get("type") or item.get("question_type") or "").strip()
        if chapter != slot.target_chapter:
            raise ValueError(f"第{slot.position}题章节与冻结题位不一致")
        if question_type != slot.target_question_type:
            raise ValueError(f"第{slot.position}题题型与冻结题位不一致")
        content = str(item.get("content") or item.get("latex") or "").strip()
        difficulty = str(item.get("difficulty") or "").strip()
        if not content or difficulty not in {"简单", "中等", "困难"}:
            raise ValueError(f"第{slot.position}题正文或难度无效")
        structured.append({
            "position": slot.position,
            "chapter": chapter,
            "type": question_type,
            "question_type": question_type,
            "difficulty": difficulty,
            "content": content,
            "content_fingerprint": question_content_fingerprint(content),
            "source_mistake_ids": list(slot.source_mistake_ids),
            "source_lineage_hash": slot.source_lineage_hash,
            "lineage_policy_version": slot.policy_version,
            "reference_ids": list(slot.source_mistake_ids),
        })
    return RawGeneratedBatch(
        bodies=(body,),
        structured_items=tuple(structured),
        difficulty_labels=tuple(item["difficulty"] for item in structured),
        frozen_evidence=request.frozen_evidence,
    )


__all__ = [
    "FreeRawGenerationRequest",
    "MistakesRawGenerationRequest",
    "StructuredMistakesRawGenerationRequest",
    "RawGeneratedBatch",
    "generate_free_raw_papers",
    "generate_mistakes_raw_papers",
    "generate_structured_mistakes_raw_papers",
]
