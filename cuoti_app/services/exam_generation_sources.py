"""Request-local source preparation helpers for exam generation."""

from __future__ import annotations

from copy import deepcopy


class QuestionBankScopeCache:
    """Reuse immutable question-bank reads without sharing mutable results."""

    def __init__(self) -> None:
        self._values: dict[tuple, object] = {}

    @staticmethod
    def _chapters(chapters) -> tuple[str, ...]:
        return tuple(
            str(value).strip()
            for value in (chapters or ())
            if str(value).strip()
        )

    def coverage(self, index, *, subject=None, chapters=None):
        chapter_values = self._chapters(chapters)
        key = (
            "coverage",
            str(getattr(index, "snapshot_id", "")),
            str(subject or ""),
            chapter_values,
        )
        if key not in self._values:
            self._values[key] = index.coverage(
                subject=subject,
                chapters=chapter_values,
            )
        return deepcopy(self._values[key])

    def candidates(self, index, subject, chapters):
        chapter_values = self._chapters(chapters)
        key = (
            "all_candidates",
            str(getattr(index, "snapshot_id", "")),
            str(subject or ""),
            chapter_values,
        )
        if key not in self._values:
            self._values[key] = index.all_candidates(subject, chapter_values)
        return deepcopy(self._values[key])


__all__ = ["QuestionBankScopeCache"]
