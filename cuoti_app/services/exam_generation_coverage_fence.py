"""Request-local coverage and mistake-lineage fences for generated papers."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .exam_contracts import ExamGenerationConstraintError
from .exam_coverage_policy import MinimumCoverageTargets, validate_minimum_coverage
from .exam_mistake_lineage_policy import MistakeLineageSlot
from .exam_repetition_policy import question_content_fingerprint


def _failure(message: str, *, code: str, details: Mapping[str, Any]) -> ExamGenerationConstraintError:
    return ExamGenerationConstraintError(
        message,
        code=code,
        hint="请保持冻结题位、章节、题型和错题来源不变后重新生成",
        actions=("retry_generation", "adjust_generation_constraints"),
        details=dict(details),
        status_code=422,
    )


def normalize_free_coverage_items(
    items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Normalize the public type alias without inventing chapters or types."""

    normalized: list[dict[str, Any]] = []
    seen_positions: set[int] = set()
    for fallback, raw in enumerate(items, start=1):
        if not isinstance(raw, Mapping):
            raise _failure(
                "自由组卷题目结构无效",
                code="coverage_item_schema_invalid",
                details={"invalid_positions": [fallback]},
            )
        item = dict(raw)
        position = item.get("position", fallback)
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position < 1
            or position in seen_positions
        ):
            raise _failure(
                "自由组卷题位无效或重复",
                code="coverage_item_schema_invalid",
                details={"invalid_positions": [position]},
            )
        seen_positions.add(position)
        chapter = item.get("chapter")
        raw_type = item.get("question_type")
        alias_type = item.get("type")
        if type(chapter) is not str or not chapter.strip():
            raise _failure(
                "自由组卷题目缺少章节",
                code="insufficient_chapter_coverage",
                details={"invalid_positions": [position]},
            )
        if raw_type not in (None, "") and alias_type not in (None, ""):
            if raw_type != alias_type:
                raise _failure(
                    "自由组卷题型字段不一致",
                    code="insufficient_question_type_coverage",
                    details={"invalid_positions": [position]},
                )
        question_type = raw_type if raw_type not in (None, "") else alias_type
        if type(question_type) is not str or not question_type.strip():
            raise _failure(
                "自由组卷题目缺少题型",
                code="insufficient_question_type_coverage",
                details={"invalid_positions": [position]},
            )
        item["position"] = position
        item["chapter"] = chapter.strip()
        item["question_type"] = question_type.strip()
        item["type"] = question_type.strip()
        normalized.append(item)
    normalized.sort(key=lambda value: value["position"])
    if [item["position"] for item in normalized] != list(range(1, len(normalized) + 1)):
        raise _failure(
            "自由组卷题位不连续",
            code="coverage_item_schema_invalid",
            details={"positions": [item["position"] for item in normalized]},
        )
    return tuple(normalized)


def validate_mistake_lineage_items(
    items: Sequence[Mapping[str, Any]],
    lineage_slots: Sequence[MistakeLineageSlot],
) -> tuple[dict[str, Any], ...]:
    """Require exact, position-local lineage identity at every stage."""

    slots = tuple(sorted(lineage_slots, key=lambda value: value.position))
    if len(items) != len(slots):
        raise _failure(
            "错题组卷题量与冻结谱系不一致",
            code="mistake_lineage_changed",
            details={"expected_count": len(slots), "actual_count": len(items)},
        )
    by_position: dict[int, dict[str, Any]] = {}
    for raw in items:
        if not isinstance(raw, Mapping):
            raise _failure(
                "错题组卷题目结构无效",
                code="mistake_lineage_changed",
                details={},
            )
        item = dict(raw)
        position = item.get("position")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position in by_position
        ):
            raise _failure(
                "错题组卷题位无效或重复",
                code="mistake_lineage_changed",
                details={"invalid_position": position},
            )
        by_position[position] = item
    invalid: list[int] = []
    for slot in slots:
        item = by_position.get(slot.position)
        if item is None or (
            tuple(item.get("source_mistake_ids") or ()) != slot.source_mistake_ids
            or item.get("source_lineage_hash") != slot.source_lineage_hash
            or item.get("lineage_policy_version") != slot.policy_version
            or item.get("chapter") != slot.target_chapter
            or (item.get("question_type") or item.get("type"))
            != slot.target_question_type
        ):
            invalid.append(slot.position)
    if invalid or set(by_position) != {slot.position for slot in slots}:
        raise _failure(
            "错题组卷谱系在处理阶段发生变化",
            code="mistake_lineage_changed",
            details={"invalid_positions": invalid},
        )
    return tuple(by_position[slot.position] for slot in slots)


def refresh_mistake_content_fingerprints(
    items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    """Refresh derived fingerprints after a controlled body-only rewrite."""

    refreshed = []
    for position, raw in enumerate(items, start=1):
        item = dict(raw)
        content = item.get("content") or item.get("latex")
        if type(content) is not str or not content.strip():
            raise _failure(
                "错题组卷题目缺少正文",
                code="coverage_item_schema_invalid",
                details={"invalid_positions": [position]},
            )
        item["content_fingerprint"] = question_content_fingerprint(content)
        refreshed.append(item)
    return tuple(refreshed)


def validate_generation_coverage(
    items: Sequence[Mapping[str, Any]],
    targets: MinimumCoverageTargets,
    *,
    lineage_slots: Sequence[MistakeLineageSlot] = (),
    public_id_by_internal_id: Mapping[int, str] | None = None,
) -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Validate coverage and return normalized items plus a public receipt."""

    if lineage_slots:
        normalized = validate_mistake_lineage_items(items, lineage_slots)
        public_by_internal = dict(public_id_by_internal_id or {})
        internal_by_public = {value: key for key, value in public_by_internal.items()}
        if (
            set(public_by_internal) != set(targets.mistake_ids)
            or len(internal_by_public) != len(public_by_internal)
        ):
            raise _failure(
                "冻结错题身份映射无效",
                code="mistake_lineage_changed",
                details={},
            )
        coverage_items = []
        for item in normalized:
            public_ids = tuple(item.get("source_mistake_ids") or ())
            try:
                internal_ids = [internal_by_public[value] for value in public_ids]
            except (KeyError, TypeError) as exc:
                raise _failure(
                    "错题组卷包含未冻结来源",
                    code="mistake_lineage_changed",
                    details={"position": item.get("position")},
                ) from exc
            coverage_items.append({**item, "source_mistake_ids": internal_ids})
        receipt = validate_minimum_coverage(coverage_items, targets).to_dict()
        receipt["mistake_coverage_counts"] = {
            public_by_internal[int(key)]: value
            for key, value in receipt["mistake_coverage_counts"].items()
        }
        receipt["item_mistake_ids"] = [
            [public_by_internal[int(value)] for value in values]
            for values in receipt["item_mistake_ids"]
        ]
        receipt["mistake_identity"] = "public_id"
    else:
        normalized = normalize_free_coverage_items(items)
        receipt = validate_minimum_coverage(normalized, targets).to_dict()
    return normalized, receipt


def require_unchanged_coverage_receipt(
    receipt: Mapping[str, Any],
    expected: Mapping[str, Any],
    *,
    stage: str,
) -> None:
    if dict(receipt) != dict(expected):
        raise _failure(
            "生成结果覆盖收据在处理阶段发生变化",
            code="minimum_coverage_changed",
            details={"stage": str(stage)},
        )


__all__ = [
    "normalize_free_coverage_items",
    "refresh_mistake_content_fingerprints",
    "require_unchanged_coverage_receipt",
    "validate_generation_coverage",
    "validate_mistake_lineage_items",
]
