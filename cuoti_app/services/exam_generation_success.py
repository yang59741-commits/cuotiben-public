"""Pure post-publication memory and response assembly contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_contracts import require_single_paper_sequence


@dataclass(frozen=True, slots=True)
class SuccessMemoryContext:
    source: str
    operation_id: str
    subject: str
    chapters: Sequence[str]
    request: Mapping[str, Any]
    results: Sequence[Mapping[str, Any]]
    generated_question_items: Sequence[Mapping[str, Any]] = ()
    past_selections: Sequence[Sequence[Mapping[str, Any]]] = ()
    learning_years: Sequence[int] = ()
    past_analysis: Mapping[str, Any] | None = None
    diversity_audit: Sequence[Mapping[str, Any]] = ()
    allow_adaptation: bool = False
    selected_mistake_ids: Sequence[Any] = ()


def build_success_memory_events(
    context: SuccessMemoryContext,
    *,
    fingerprint: Callable[[Any], str],
) -> tuple[dict[str, Any], ...]:
    """Build result events without performing memory I/O."""

    events = []
    for paper_index, result in enumerate(context.results, start=1):
        if context.source == "free":
            questions = [
                {
                    "position": int(item.get("position", index)),
                    "content": item.get("content", ""),
                    "fingerprint": fingerprint(item.get("content", "")),
                    "difficulty": item.get("difficulty"),
                    "type": item.get("type"),
                    "reference_ids": item.get("reference_ids") or [],
                }
                for index, item in enumerate(
                    context.generated_question_items, start=1
                )
            ]
        elif context.source == "past":
            selection = context.past_selections[paper_index - 1]
            questions = [
                {
                    "position": index,
                    "candidate_id": item.get("candidate_id"),
                    "chapter": item.get("chapter"),
                    "source_file": item.get("source_file"),
                    "source_number": item.get("source_number"),
                    "difficulty": item.get("difficulty"),
                    "adapted": bool(item.get("adapted")),
                    "fingerprint": fingerprint(item.get("content", "")),
                }
                for index, item in enumerate(selection, start=1)
            ]
        else:
            questions = [
                {
                    "position": 1,
                    "content": result.get("content", ""),
                    "fingerprint": fingerprint(result.get("content", "")),
                }
            ]
        events.append(
            {
                "event_id": f"{context.operation_id}:result:{paper_index}",
                "event_type": "result",
                "operation_id": context.operation_id,
                "paper_index": paper_index,
                "subject": context.subject,
                "chapters": list(context.chapters),
                "request": dict(context.request),
                "questions": questions,
                "result": {
                    "exam_id": result.get("id"),
                    "title": result.get("title"),
                    "source": context.source,
                    "learning_years": (
                        list(context.learning_years)
                        if context.source == "past"
                        else []
                    ),
                    "structure_analysis": (
                        dict(context.past_analysis)
                        if context.source == "past"
                        and isinstance(context.past_analysis, Mapping)
                        else None
                    ),
                    "diversity_policy": (
                        dict(context.diversity_audit[paper_index - 1])
                        if context.source == "past"
                        and paper_index <= len(context.diversity_audit)
                        else None
                    ),
                },
                "user_behavior": {
                    "allow_adaptation": context.allow_adaptation,
                    "selected_mistake_ids": (
                        list(context.selected_mistake_ids)
                        if context.source == "mistakes"
                        else []
                    ),
                },
            }
        )
    return tuple(events)


def summarize_material_understanding(
    parts: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    normalized = [part for part in parts if isinstance(part, Mapping)]
    if not normalized:
        return None
    return {
        "complete": all(bool(part.get("complete")) for part in normalized),
        "total_count": sum(int(part.get("total_count") or 0) for part in normalized),
        "analyzed_count": sum(
            int(part.get("analyzed_count") or 0) for part in normalized
        ),
        "chapter_count": len(
            {
                chapter
                for part in normalized
                for chapter in (part.get("chapters") or [])
            }
        ),
        "source_count": len(
            {
                source
                for part in normalized
                for source in (part.get("sources") or {})
            }
        ),
        "years": sorted(
            {year for part in normalized for year in (part.get("years") or [])}
        ),
        "batch_count": sum(int(part.get("batch_count") or 0) for part in normalized),
        "reused_count": sum(int(part.get("reused_count") or 0) for part in normalized),
        "permanent_card_reused_count": sum(
            int(part.get("permanent_card_reused_count") or 0)
            for part in normalized
        ),
        "legacy_deep_read_count": sum(
            int(
                part.get("legacy_deep_read_count")
                if part.get("legacy_deep_read_count") is not None
                else part.get("total_count") or 0
            )
            for part in normalized
        ),
        "private_cache_reused_count": sum(
            int(
                part.get("private_cache_reused_count")
                if part.get("private_cache_reused_count") is not None
                else part.get("reused_count") or 0
            )
            for part in normalized
        ),
        "missing_count": sum(
            int(part.get("missing_count") or 0) for part in normalized
        ),
        "cache_keys": [
            part.get("cache_key") for part in normalized if part.get("cache_key")
        ],
    }


def build_success_response(
    results: Sequence[Mapping[str, Any]],
    *,
    source: str,
    diversity_audit: Sequence[Mapping[str, Any]],
    past_profile_status: str,
    past_profile_id: str | None,
    past_profile_origin: str | None,
    past_exam_snapshot_id: str | None,
    learning_samples: Sequence[Mapping[str, Any]],
    material_parts: Sequence[Mapping[str, Any]],
    adaptation: Mapping[str, Any] | None = None,
    warnings: Sequence[Mapping[str, Any]] = (),
    difficulty_audits: Sequence[Mapping[str, Any]] = (),
    coverage_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    primary = require_single_paper_sequence(results, label="组卷成功结果")
    response = dict(primary)
    response["count"] = 1
    response["exams"] = list(results)
    response["adaptation"] = dict(adaptation or {})
    response["warnings"] = [dict(item) for item in warnings]
    response["difficulty_audit"] = [
        dict(item) for item in difficulty_audits
    ]
    if source in {"free", "mistakes"}:
        if not isinstance(coverage_receipt, Mapping):
            raise ValueError("自由/错题组卷成功响应缺少覆盖收据")
        response["minimum_coverage"] = dict(coverage_receipt)
    if source == "past":
        response["diversity_audit"] = list(diversity_audit)
        response["past_profile"] = {
            "status": past_profile_status,
            "profile_id": past_profile_id,
            "origin": past_profile_origin,
            "source_snapshot": past_exam_snapshot_id,
            "learning_years": [item["year"] for item in learning_samples],
        }
    material = summarize_material_understanding(material_parts)
    if material is not None:
        response["material_understanding"] = material
    return response


__all__ = [
    "SuccessMemoryContext",
    "build_success_memory_events",
    "build_success_response",
    "summarize_material_understanding",
]
