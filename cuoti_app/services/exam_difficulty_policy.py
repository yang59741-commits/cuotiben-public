"""Pure, domain-separated difficulty targets for exam generation."""

from __future__ import annotations

from enum import Enum
from typing import Final


class OverallDifficulty(Enum):
    BASIC = "基础"
    MEDIUM = "中等"
    HARD = "困难"


class QuestionDifficulty(Enum):
    EASY = "简单"
    MEDIUM = "中等"
    HARD = "困难"


OVERALL_DIFFICULTY_VALUES: Final = frozenset(
    value.value for value in OverallDifficulty
)
QUESTION_DIFFICULTY_VALUES: Final = frozenset(
    value.value for value in QuestionDifficulty
)

_OVERALL_TO_PRIMARY: Final = {
    OverallDifficulty.BASIC: QuestionDifficulty.EASY,
    OverallDifficulty.MEDIUM: QuestionDifficulty.MEDIUM,
    OverallDifficulty.HARD: QuestionDifficulty.HARD,
}
_WEIGHTS: Final = {
    OverallDifficulty.BASIC: {
        QuestionDifficulty.EASY: 30,
        QuestionDifficulty.MEDIUM: 50,
        QuestionDifficulty.HARD: 20,
    },
    OverallDifficulty.MEDIUM: {
        QuestionDifficulty.EASY: 20,
        QuestionDifficulty.MEDIUM: 50,
        QuestionDifficulty.HARD: 30,
    },
    OverallDifficulty.HARD: {
        QuestionDifficulty.EASY: 10,
        QuestionDifficulty.MEDIUM: 40,
        QuestionDifficulty.HARD: 50,
    },
}
_SMALL_TARGETS: Final = {
    1: {
        OverallDifficulty.BASIC: (1, 0, 0),
        OverallDifficulty.MEDIUM: (0, 1, 0),
        OverallDifficulty.HARD: (0, 0, 1),
    },
    2: {
        OverallDifficulty.BASIC: (1, 1, 0),
        OverallDifficulty.MEDIUM: (0, 1, 1),
        OverallDifficulty.HARD: (0, 0, 2),
    },
    3: {
        OverallDifficulty.BASIC: (1, 2, 0),
        OverallDifficulty.MEDIUM: (1, 1, 1),
        OverallDifficulty.HARD: (0, 1, 2),
    },
    4: {
        OverallDifficulty.BASIC: (1, 3, 0),
        OverallDifficulty.MEDIUM: (1, 2, 1),
        OverallDifficulty.HARD: (0, 1, 3),
    },
}
_LABEL_ORDER: Final = (
    QuestionDifficulty.EASY,
    QuestionDifficulty.MEDIUM,
    QuestionDifficulty.HARD,
)
_REMAINDER_TIE_ORDER: Final = {
    QuestionDifficulty.HARD: 0,
    QuestionDifficulty.MEDIUM: 1,
    QuestionDifficulty.EASY: 2,
}


def parse_overall_difficulty(value: object) -> OverallDifficulty:
    if isinstance(value, OverallDifficulty):
        return value
    if type(value) is not str:
        raise ValueError("整体难度必须是基础、中等或困难")
    try:
        return OverallDifficulty(value)
    except ValueError as exc:
        raise ValueError("整体难度必须是基础、中等或困难") from exc


def parse_question_difficulty(value: object) -> QuestionDifficulty:
    if isinstance(value, QuestionDifficulty):
        return value
    if type(value) is not str:
        raise ValueError("单题难度必须是简单、中等或困难")
    try:
        return QuestionDifficulty(value)
    except ValueError as exc:
        raise ValueError("单题难度必须是简单、中等或困难") from exc


def overall_to_primary_question_difficulty(
    value: OverallDifficulty,
) -> QuestionDifficulty:
    if not isinstance(value, OverallDifficulty):
        raise TypeError("整体难度映射必须接收 OverallDifficulty")
    return _OVERALL_TO_PRIMARY[value]


def difficulty_target_counts(
    question_count: int,
    overall: OverallDifficulty | str,
) -> dict[QuestionDifficulty, int]:
    """Return the plan's exact small table or constrained largest remainder."""

    if type(question_count) is not int or question_count < 1:
        raise ValueError("题量必须是正整数")
    mode = parse_overall_difficulty(overall)
    if question_count < 5:
        values = _SMALL_TARGETS[question_count][mode]
        return dict(zip(_LABEL_ORDER, values, strict=True))

    weights = _WEIGHTS[mode]
    counts = {
        label: question_count * weights[label] // 100
        for label in _LABEL_ORDER
    }
    remainders = {
        label: question_count * weights[label] % 100
        for label in _LABEL_ORDER
    }
    easy_cap = question_count * 3 // 10
    remaining = question_count - sum(counts.values())
    ranked = sorted(
        _LABEL_ORDER,
        key=lambda label: (-remainders[label], _REMAINDER_TIE_ORDER[label]),
    )
    while remaining:
        allocated = False
        for label in ranked:
            if label is QuestionDifficulty.EASY and counts[label] >= easy_cap:
                continue
            counts[label] += 1
            remaining -= 1
            allocated = True
            if not remaining:
                break
        if not allocated:
            raise ValueError("难度配额计算失败")
    if sum(counts.values()) != question_count or counts[QuestionDifficulty.EASY] > easy_cap:
        raise ValueError("难度配额计算失败")
    return counts


def serialized_difficulty_targets(
    question_count: int,
    overall: OverallDifficulty | str,
) -> dict[str, int]:
    return {
        label.value: count
        for label, count in difficulty_target_counts(question_count, overall).items()
    }


__all__ = [
    "OVERALL_DIFFICULTY_VALUES",
    "QUESTION_DIFFICULTY_VALUES",
    "OverallDifficulty",
    "QuestionDifficulty",
    "difficulty_target_counts",
    "overall_to_primary_question_difficulty",
    "parse_overall_difficulty",
    "parse_question_difficulty",
    "serialized_difficulty_targets",
]
