"""Deterministic fixed 3-5-10-15 review schedule."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta


SCHEDULE_VERSION = "fixed-3-5-10-15-v1"
ASSISTED_SCHEDULE_VERSION = "fixed-3-5-10-15-assisted-v2"


@dataclass(frozen=True)
class ReviewTransition:
    status: str
    review_stage: int
    review_cycle: int
    correct_streak: int
    next_review: str | None

    def as_dict(self) -> dict:
        return asdict(self)


def _day(value: date | str) -> date:
    return value if isinstance(value, date) else date.fromisoformat(value)


def initial_review_date(date_added: date | str) -> str:
    return (_day(date_added) + timedelta(days=3)).isoformat()


def advance_review(
    *, result: str, review_stage: int, review_cycle: int, reviewed_on: date | str,
    correct_streak: int | None = None,
) -> ReviewTransition:
    """Advance exactly one stage; an incorrect result starts a fresh cycle."""
    if result not in {"pass", "again", "assisted"}:
        raise ValueError("unsupported review result")
    stage = max(0, min(3, int(review_stage or 0)))
    cycle = max(1, int(review_cycle or 1))
    day = _day(reviewed_on)
    if result == 'assisted':
        return ReviewTransition(
            status='未掌握', review_stage=stage, review_cycle=cycle,
            correct_streak=stage if correct_streak is None else int(correct_streak),
            next_review=(day + timedelta(days=1)).isoformat(),
        )
    if result == "again":
        return ReviewTransition(
            status="未掌握",
            review_stage=0,
            review_cycle=cycle + 1,
            correct_streak=0,
            next_review=(day + timedelta(days=3)).isoformat(),
        )
    if stage == 3:
        return ReviewTransition(
            status="已掌握",
            review_stage=3,
            review_cycle=cycle,
            correct_streak=4,
            next_review=None,
        )
    next_stage = stage + 1
    interval = (5, 10, 15)[stage]
    return ReviewTransition(
        status="未掌握",
        review_stage=next_stage,
        review_cycle=cycle,
        correct_streak=next_stage,
        next_review=(day + timedelta(days=interval)).isoformat(),
    )


def reopen_review(*, review_cycle: int, reopened_on: date | str) -> ReviewTransition:
    day = _day(reopened_on)
    return ReviewTransition(
        status="未掌握",
        review_stage=0,
        review_cycle=max(1, int(review_cycle or 1)) + 1,
        correct_streak=0,
        next_review=(day + timedelta(days=3)).isoformat(),
    )
