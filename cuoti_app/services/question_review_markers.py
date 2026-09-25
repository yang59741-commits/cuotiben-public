"""Read-only access to unresolved question-review markers.

Markers are display metadata only.  Callers must not use them to filter,
re-rank, downgrade, reject, or otherwise alter a candidate selection.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable, Mapping

from ..migrations.v57_question_review_markers import QUESTION_REVIEW_MARKERS_TABLE


MARKER_SCHEMA_VERSION = "question-review-marker-v1"
_LABELS = {
    "suspect": "题面提示：嫌疑题",
    "uncertain": "题面提示：不确定题",
}


def _source_variants(source_kind: str) -> tuple[str, ...]:
    source = str(source_kind or "").strip()
    if source == "past_exam":
        return ("past_exam_local", "past_exam_other")
    if source == "question_bank":
        return ("ordinary",)
    return (source,)


def _public_marker(row: Mapping[str, Any]) -> dict[str, Any]:
    kind = str(row["marker_kind"])
    scope = str(row["marker_scope"])
    message = str(row["message"]).strip()
    return {
        "schema_version": MARKER_SCHEMA_VERSION,
        "kind": kind,
        "scope": scope,
        "label": _LABELS[kind],
        "message": message,
    }


def load_review_markers(
    db: sqlite3.Connection,
    identities: Iterable[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Load active markers for exact source-kind/candidate identities."""

    pairs = sorted(
        {
            (variant, str(candidate))
            for source, candidate in identities
            for variant in _source_variants(str(source))
            if variant and candidate
        }
    )
    if not pairs:
        return {}
    clauses = " OR ".join("(source_kind=? AND candidate_id=?)" for _ in pairs)
    params = [value for pair in pairs for value in pair]
    try:
        rows = db.execute(
            f"SELECT source_kind,candidate_id,marker_kind,marker_scope,message "
            f"FROM {QUESTION_REVIEW_MARKERS_TABLE} "
            f"WHERE active=1 AND ({clauses})",
            params,
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return {}
        raise
    return {
        (str(row["source_kind"]), str(row["candidate_id"])): _public_marker(row)
        for row in rows
        if str(row["marker_kind"]) in _LABELS
    }


def marker_for(
    db: sqlite3.Connection,
    *,
    source_kind: str | None,
    candidate_id: str | None,
) -> dict[str, Any] | None:
    source = str(source_kind or "").strip()
    candidate = str(candidate_id or "").strip()
    if not source or not candidate:
        return None
    markers = load_review_markers(db, ((source, candidate),))
    for variant in _source_variants(source):
        marker = markers.get((variant, candidate))
        if marker is not None:
            return marker
    return None


def attach_review_markers(
    db: sqlite3.Connection,
    items: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Copy structured items and attach display-only marker metadata."""

    values = [dict(item) for item in items]
    identities: list[tuple[str, str]] = []
    for item in values:
        source = str(item.get("source_kind") or "").strip()
        candidate = str(item.get("candidate_id") or "").strip()
        if source and candidate:
            identities.append((source, candidate))
        for reference in item.get("reference_ids") or ():
            if reference:
                ref_source = "ordinary" if source in {"", "ai_generated"} else source
                identities.append((ref_source, str(reference)))
    markers = load_review_markers(db, identities)
    for item in values:
        found: list[dict[str, Any]] = []
        source = str(item.get("source_kind") or "").strip()
        candidate = str(item.get("candidate_id") or "").strip()
        for variant in _source_variants(source):
            marker = markers.get((variant, candidate)) if candidate else None
            if marker and marker not in found:
                found.append(marker)
        for reference in item.get("reference_ids") or ():
            ref_source = "ordinary" if source in {"", "ai_generated"} else source
            for variant in _source_variants(ref_source):
                marker = markers.get((variant, str(reference)))
                if marker and marker not in found:
                    found.append(marker)
        if found:
            item["review_markers"] = found
    return values


__all__ = [
    "MARKER_SCHEMA_VERSION",
    "attach_review_markers",
    "load_review_markers",
    "marker_for",
]
