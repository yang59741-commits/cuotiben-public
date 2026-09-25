"""Pure minimum-coverage contracts for free and mistake exam generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .exam_contracts import ExamGenerationConstraintError


COVERAGE_POLICY_VERSION = "exam-coverage-v1"


@dataclass(frozen=True, slots=True)
class MinimumCoverageTargets:
    question_count: int
    question_types: tuple[str, ...] = ()
    chapters: tuple[str, ...] = ()
    mistake_ids: tuple[int, ...] = ()
    policy_version: str = COVERAGE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "question_count": self.question_count,
            "question_types": list(self.question_types),
            "chapters": list(self.chapters),
            "mistake_ids": list(self.mistake_ids),
        }


@dataclass(frozen=True, slots=True)
class MinimumCoverageReceipt:
    question_count: int
    question_type_counts: tuple[tuple[str, int], ...]
    chapter_counts: tuple[tuple[str, int], ...]
    mistake_coverage_counts: tuple[tuple[int, int], ...]
    item_mistake_ids: tuple[tuple[int, ...], ...]
    policy_version: str = COVERAGE_POLICY_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "question_count": self.question_count,
            "question_type_counts": dict(self.question_type_counts),
            "chapter_counts": dict(self.chapter_counts),
            "mistake_coverage_counts": dict(self.mistake_coverage_counts),
            "item_mistake_ids": [list(values) for values in self.item_mistake_ids],
        }


def _question_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("题量必须是正整数")
    return value


def _unique_text_values(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label}必须是数组")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if type(raw) is not str or not raw.strip():
            raise ValueError(f"{label}必须是非空文本数组")
        item = raw.strip()
        if item in seen:
            raise ValueError(f"{label}不能重复")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _unique_mistake_ids(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("错题 ID 必须是数组")
    result: list[int] = []
    seen: set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValueError("错题 ID 必须是正整数")
        if raw in seen:
            raise ValueError("错题 ID 不能重复")
        seen.add(raw)
        result.append(raw)
    return tuple(result)


def _preflight_error(
    *,
    domain: str,
    code: str,
    question_count: int,
    selected_count: int,
    actions: tuple[str, str],
) -> ExamGenerationConstraintError:
    return ExamGenerationConstraintError(
        f"题量 {question_count} 无法覆盖 {selected_count} 个已选{domain}",
        code=code,
        hint=f"请减少已选{domain}或增加题量",
        actions=actions,
        details={
            "question_count": question_count,
            "selected_count": selected_count,
            "required_minimum": selected_count,
            "shortfall": selected_count - question_count,
            "policy_version": COVERAGE_POLICY_VERSION,
        },
        status_code=422,
    )


def preflight_minimum_coverage(
    question_count: int,
    *,
    question_types: Sequence[str] = (),
    chapters: Sequence[str] = (),
    mistake_ids: Sequence[int] = (),
) -> MinimumCoverageTargets:
    """Freeze selected coverage sets and reject structurally impossible input."""

    count = _question_count(question_count)
    normalized_types = _unique_text_values(question_types, "题型")
    normalized_chapters = _unique_text_values(chapters, "章节")
    normalized_mistakes = _unique_mistake_ids(mistake_ids)
    if count < len(normalized_types):
        raise _preflight_error(
            domain="题型",
            code="insufficient_question_type_coverage",
            question_count=count,
            selected_count=len(normalized_types),
            actions=("reduce_question_types", "increase_question_count"),
        )
    if count < len(normalized_chapters):
        raise _preflight_error(
            domain="章节",
            code="insufficient_chapter_coverage",
            question_count=count,
            selected_count=len(normalized_chapters),
            actions=("reduce_chapters", "increase_question_count"),
        )
    if count < len(normalized_mistakes):
        raise _preflight_error(
            domain="错题",
            code="insufficient_mistake_coverage",
            question_count=count,
            selected_count=len(normalized_mistakes),
            actions=("reduce_mistakes", "increase_question_count"),
        )
    return MinimumCoverageTargets(
        question_count=count,
        question_types=normalized_types,
        chapters=normalized_chapters,
        mistake_ids=normalized_mistakes,
    )


def _coverage_failure(
    *,
    domain: str,
    code: str,
    selected: Sequence[Any],
    missing: Sequence[Any],
    invalid_positions: Sequence[int],
    invalid_values: Sequence[Any],
) -> ExamGenerationConstraintError:
    return ExamGenerationConstraintError(
        f"生成结果未完整覆盖已选{domain}",
        code=code,
        hint=f"请保持每个已选{domain}至少出现一次，并移除允许集合外的值",
        actions=("retry_generation", "adjust_generation_constraints"),
        details={
            "selected": list(selected),
            "missing": list(missing),
            "invalid_positions": list(invalid_positions),
            "invalid_values": list(invalid_values),
            "policy_version": COVERAGE_POLICY_VERSION,
        },
        status_code=422,
    )


def validate_minimum_coverage(
    items: Sequence[Mapping[str, Any]],
    targets: MinimumCoverageTargets,
) -> MinimumCoverageReceipt:
    """Validate allowed sets, minimum coverage and mistake lineage purely."""

    if not isinstance(targets, MinimumCoverageTargets):
        raise TypeError("覆盖目标合同无效")
    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        raise ValueError("生成题目必须是数组")
    if len(items) != targets.question_count:
        raise ValueError(
            f"生成题目数量不一致：应为{targets.question_count}题，实际{len(items)}题"
        )
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("生成题目格式无效")

    type_counts = {value: 0 for value in targets.question_types}
    invalid_type_positions: list[int] = []
    invalid_types: list[Any] = []
    if targets.question_types:
        allowed_types = set(targets.question_types)
        for position, item in enumerate(items, start=1):
            value = item.get("question_type")
            if type(value) is not str or value not in allowed_types:
                invalid_type_positions.append(position)
                invalid_types.append(value)
                continue
            type_counts[value] += 1
        missing_types = [
            value for value in targets.question_types if type_counts[value] == 0
        ]
        if missing_types or invalid_type_positions:
            raise _coverage_failure(
                domain="题型",
                code="insufficient_question_type_coverage",
                selected=targets.question_types,
                missing=missing_types,
                invalid_positions=invalid_type_positions,
                invalid_values=invalid_types,
            )

    chapter_counts = {value: 0 for value in targets.chapters}
    invalid_chapter_positions: list[int] = []
    invalid_chapters: list[Any] = []
    if targets.chapters:
        allowed_chapters = set(targets.chapters)
        for position, item in enumerate(items, start=1):
            value = item.get("chapter")
            if type(value) is not str or value not in allowed_chapters:
                invalid_chapter_positions.append(position)
                invalid_chapters.append(value)
                continue
            chapter_counts[value] += 1
        missing_chapters = [
            value for value in targets.chapters if chapter_counts[value] == 0
        ]
        if missing_chapters or invalid_chapter_positions:
            raise _coverage_failure(
                domain="章节",
                code="insufficient_chapter_coverage",
                selected=targets.chapters,
                missing=missing_chapters,
                invalid_positions=invalid_chapter_positions,
                invalid_values=invalid_chapters,
            )

    mistake_counts = {value: 0 for value in targets.mistake_ids}
    normalized_item_mistakes: list[tuple[int, ...]] = []
    invalid_mistake_positions: list[int] = []
    invalid_mistake_values: list[Any] = []
    if targets.mistake_ids:
        allowed_mistakes = set(targets.mistake_ids)
        for position, item in enumerate(items, start=1):
            raw_values = item.get("source_mistake_ids")
            try:
                values = _unique_mistake_ids(raw_values)
            except ValueError:
                values = ()
                invalid_mistake_positions.append(position)
                invalid_mistake_values.append(raw_values)
            else:
                unknown = [value for value in values if value not in allowed_mistakes]
                if not values or unknown:
                    invalid_mistake_positions.append(position)
                    invalid_mistake_values.extend(unknown or [raw_values])
                else:
                    for value in values:
                        mistake_counts[value] += 1
            normalized_item_mistakes.append(values)
        missing_mistakes = [
            value for value in targets.mistake_ids if mistake_counts[value] == 0
        ]
        if missing_mistakes or invalid_mistake_positions:
            raise _coverage_failure(
                domain="错题来源",
                code="insufficient_mistake_coverage",
                selected=targets.mistake_ids,
                missing=missing_mistakes,
                invalid_positions=invalid_mistake_positions,
                invalid_values=invalid_mistake_values,
            )
    else:
        normalized_item_mistakes = [() for _item in items]

    return MinimumCoverageReceipt(
        question_count=targets.question_count,
        question_type_counts=tuple(type_counts.items()),
        chapter_counts=tuple(chapter_counts.items()),
        mistake_coverage_counts=tuple(mistake_counts.items()),
        item_mistake_ids=tuple(normalized_item_mistakes),
    )


__all__ = [
    "COVERAGE_POLICY_VERSION",
    "MinimumCoverageReceipt",
    "MinimumCoverageTargets",
    "preflight_minimum_coverage",
    "validate_minimum_coverage",
]
