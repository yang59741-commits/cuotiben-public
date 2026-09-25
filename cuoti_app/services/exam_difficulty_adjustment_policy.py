"""Pure policy for planning an atomic easy-excess difficulty adjustment."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Sequence

from .exam_difficulty_policy import (
    QuestionDifficulty,
    difficulty_target_counts,
    parse_overall_difficulty,
    parse_question_difficulty,
)


DIFFICULTY_ADJUSTMENT_POLICY_VERSION = "easy-excess-atomic-v1"
_LABELS = (
    QuestionDifficulty.EASY,
    QuestionDifficulty.MEDIUM,
    QuestionDifficulty.HARD,
)
_DEFICIT_TIE_ORDER = (
    QuestionDifficulty.HARD,
    QuestionDifficulty.MEDIUM,
)


@dataclass(frozen=True, slots=True)
class EasyExcessAdjustmentPlan:
    question_count: int
    overall_difficulty: str
    actual_counts: tuple[tuple[str, int], ...]
    target_counts: tuple[tuple[str, int], ...]
    easy_cap: int
    easy_excess: int
    target_labels: tuple[str, ...]
    advisory_codes: tuple[str, ...]
    policy_version: str = DIFFICULTY_ADJUSTMENT_POLICY_VERSION

    @property
    def requested(self) -> bool:
        return self.easy_excess > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "question_count": self.question_count,
            "overall_difficulty": self.overall_difficulty,
            "actual_counts": dict(self.actual_counts),
            "target_counts": dict(self.target_counts),
            "easy_cap": self.easy_cap,
            "easy_excess": self.easy_excess,
            "target_labels": list(self.target_labels),
            "advisory_codes": list(self.advisory_codes),
            "requested": self.requested,
        }


def plan_easy_excess_adjustment(
    labels: Sequence[str],
    overall_difficulty: object,
) -> EasyExcessAdjustmentPlan:
    """Plan every required target label without selecting or mutating items."""

    if isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence):
        raise ValueError("逐题难度标签必须是数组")
    if not labels:
        raise ValueError("逐题难度标签不能为空")
    overall = parse_overall_difficulty(overall_difficulty)
    normalized = tuple(parse_question_difficulty(value) for value in labels)
    count = len(normalized)
    actual = Counter(normalized)
    target = difficulty_target_counts(count, overall)
    actual_pairs = tuple((label.value, actual[label]) for label in _LABELS)
    target_pairs = tuple((label.value, target[label]) for label in _LABELS)

    if count < 5:
        advisory = (
            ()
            if actual_pairs == target_pairs
            else ("small_sample_target_deviation",)
        )
        return EasyExcessAdjustmentPlan(
            question_count=count,
            overall_difficulty=overall.value,
            actual_counts=actual_pairs,
            target_counts=target_pairs,
            easy_cap=count,
            easy_excess=0,
            target_labels=(),
            advisory_codes=advisory,
        )

    easy_cap = count * 3 // 10
    excess = max(0, actual[QuestionDifficulty.EASY] - easy_cap)
    deficits = {
        label: max(0, target[label] - actual[label])
        for label in _DEFICIT_TIE_ORDER
    }
    target_labels: list[str] = []
    for _offset in range(excess):
        available = [label for label in _DEFICIT_TIE_ORDER if deficits[label] > 0]
        if not available:
            raise RuntimeError("中等/困难目标缺口不足以承接简单题超额")
        selected = max(
            available,
            key=lambda label: (
                deficits[label],
                1 if label is QuestionDifficulty.HARD else 0,
            ),
        )
        target_labels.append(selected.value)
        deficits[selected] -= 1

    projected = Counter(actual)
    projected[QuestionDifficulty.EASY] -= excess
    projected.update(
        parse_question_difficulty(value) for value in target_labels
    )
    advisory_codes: list[str] = []
    if projected[QuestionDifficulty.MEDIUM] != target[QuestionDifficulty.MEDIUM]:
        advisory_codes.append("medium_target_deviation")
    if projected[QuestionDifficulty.HARD] != target[QuestionDifficulty.HARD]:
        advisory_codes.append("hard_target_deviation")
    return EasyExcessAdjustmentPlan(
        question_count=count,
        overall_difficulty=overall.value,
        actual_counts=actual_pairs,
        target_counts=target_pairs,
        easy_cap=easy_cap,
        easy_excess=excess,
        target_labels=tuple(target_labels),
        advisory_codes=tuple(advisory_codes),
    )


__all__ = [
    "DIFFICULTY_ADJUSTMENT_POLICY_VERSION",
    "EasyExcessAdjustmentPlan",
    "plan_easy_excess_adjustment",
]
