"""Read-only repetition policy primitives shared by exam source flows."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping


_OUTER_LAYOUT_ENVIRONMENTS = ("problem",)
_OUTER_LAYOUT = re.compile(
    r"\A\s*\\begin\s*\{(?P<environment>"
    + "|".join(re.escape(value) for value in _OUTER_LAYOUT_ENVIRONMENTS)
    + r")\}(?:\s*\[[^\]\r\n]*\])*\s*(?P<body>.*)"
    r"\s*\\end\s*\{(?P=environment)\}\s*\Z",
    re.DOTALL,
)
_LEADING_QUESTION_NUMBER = re.compile(
    r"\A(?:"
    r"\\(?:sub)*section\*?\s*\{\s*(?:第\s*)?(?:题目\s*)?[0-9]+\s*(?:题)?\s*\}\s*"
    r"|题目\s*[0-9]+\s*(?:[.\uff0e、:：]\s*)?"
    r"|第\s*[0-9]+\s*题\s*(?:[.\uff0e、:：]\s*)?"
    r"|[\(（]\s*[0-9]+\s*[\)）]\s*"
    r"|[0-9]+\s*[.\uff0e、:：\)）]\s*"
    r")"
)


def normalize_question_content(value: object) -> str:
    """Remove only presentation-level numbering, wrappers, and whitespace."""

    text = str(value or "")
    wrapper = _OUTER_LAYOUT.fullmatch(text)
    if wrapper is not None:
        text = wrapper.group("body")
    text = _LEADING_QUESTION_NUMBER.sub("", text, count=1)
    return re.sub(r"\s+", "", text)


def question_content_fingerprint(value: object) -> str:
    """Hash conservative question text without discarding mathematical tokens."""

    normalized = normalize_question_content(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def load_latest_mistake_mastery_by_fingerprint(
    db,
    *,
    subject_id: int,
) -> Mapping[str, str]:
    """Return latest active mistake status for each body fingerprint in a subject."""

    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
        raise ValueError("subject_id must be a positive integer")
    rows = db.execute(
        "SELECT id,content,status FROM mistakes "
        "WHERE subject_id=? AND deleted_at IS NULL "
        "ORDER BY julianday(updated_at) ASC,id ASC",
        (subject_id,),
    ).fetchall()
    latest: dict[str, str] = {}
    for row in rows:
        normalized = normalize_question_content(row["content"])
        if not normalized:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        latest[digest] = str(row["status"] or "").strip()
    return latest


# Compatibility names retained for source-flow integration already in progress.
conservative_question_text = normalize_question_content
conservative_question_fingerprint = question_content_fingerprint
latest_mistake_mastery_by_fingerprint = load_latest_mistake_mastery_by_fingerprint


__all__ = [
    "conservative_question_fingerprint",
    "conservative_question_text",
    "load_latest_mistake_mastery_by_fingerprint",
    "latest_mistake_mastery_by_fingerprint",
    "normalize_question_content",
    "question_content_fingerprint",
]
