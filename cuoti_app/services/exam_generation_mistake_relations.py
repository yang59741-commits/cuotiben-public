"""Verifiable exact-content relations, independent of mastery or AI labels."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from .exam_repetition_policy import normalize_question_content, question_content_fingerprint
from .exam_generation_soft_inputs import freeze_soft_input

RELATION_VERSION = "exam-exact-mistake-relations-v1"
_INLINE_MATH = re.compile(r"\\\((?:(?!\\[()]).)*?\\\)", re.DOTALL)


def relation_content_fingerprint(content: str) -> str:
    # Only balanced inline TeX delimiters are display syntax; retain every token.
    display_text = _INLINE_MATH.sub(lambda match: match.group()[2:-2], content)
    return question_content_fingerprint(display_text)


def read_mistake_relations(db, *, subject_id: int) -> dict:
    if type(subject_id) is not int or subject_id < 1:
        raise ValueError("mistake relation subject must be a positive integer")

    rows = db.execute(
        "SELECT public_id,content FROM mistakes "
        "WHERE subject_id=? AND deleted_at IS NULL ORDER BY public_id", (subject_id,),
    ).fetchall()
    records = [{"public_id": row[0], "content": row[1]} for row in rows
               if isinstance(row[0], str) and row[0].strip()
               and isinstance(row[1], str) and normalize_question_content(row[1])]
    return {"version": RELATION_VERSION, "subject_id": subject_id,
            "state": "usable" if records else "omitted",
            "reason": "exact_content_only" if records else "no_active_mistake_content",
            "records": records}


def freeze_mistake_relations(db, *, job_id: str, subject_id: int, guard=None) -> dict:
    if type(subject_id) is not int or subject_id < 1:
        raise ValueError("mistake relation subject must be a positive integer")

    result = freeze_soft_input(
        db, job_id=job_id, mode="past", input_key="mistake_relations",
        allowed_sources=("mistakes",), loader=lambda: read_mistake_relations(db, subject_id=subject_id), guard=guard,
    )
    if result["payload"].get("subject_id", subject_id) != subject_id:
        raise ValueError("frozen mistake relation subject changed")
    return result


def relation_index(snapshot: Mapping | None) -> dict[str, tuple[str, ...]] | None:
    """A missing/damaged relation set is absent evidence, not a negative fact."""
    if not isinstance(snapshot, Mapping) or snapshot.get("mode") != "past":
        return None
    if snapshot.get("allowed_sources") != ["mistakes"]:
        return None
    payload = snapshot.get("payload")
    if (not isinstance(payload, Mapping) or payload.get("version") != RELATION_VERSION
            or payload.get("state") != "usable" or not isinstance(payload.get("records"), list)):
        return None
    by_content: dict[str, list[str]] = {}
    seen = set()
    for row in payload["records"]:
        if not isinstance(row, Mapping):
            return None
        public_id, content = row.get("public_id"), row.get("content")
        if (not isinstance(public_id, str) or not public_id.strip() or public_id in seen
                or not isinstance(content, str) or not normalize_question_content(content)):
            return None
        seen.add(public_id)
        by_content.setdefault(relation_content_fingerprint(content), []).append(public_id)
    return {key: tuple(sorted(ids)) for key, ids in by_content.items()} if seen else None


def exact_relation_factor(candidate: Mapping | None, index: Mapping | None) -> dict:
    absent = {"level": "not_applicable", "evidence_codes": ["no_verified_mistake_relation_in_frozen_input"]}
    if index is None or not isinstance(candidate, Mapping):
        return absent
    try:
        raw = candidate.get("content_snapshot")
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("content"), str):
            return absent
        # A matching stem does not prove equality of options, conditions or figures.
        if any(snapshot.get(key) for key in ("conditions", "options", "figure_references")):
            return {"level": "not_applicable", "evidence_codes": ["exact_relation_auxiliary_content_not_comparable"]}
        if not normalize_question_content(snapshot["content"]):
            return absent
        ids = index.get(relation_content_fingerprint(snapshot["content"]), ())
    except (TypeError, ValueError):
        return absent
    return {
        "level": "strong" if ids else "none",
        "evidence_codes": ["exact_frozen_mistake_content_match" if ids else "no_exact_frozen_mistake_content_match",
                           *[f"mistake_public_id:{public_id}" for public_id in ids]],
    }
