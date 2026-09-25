"""Schema 57: preserve unresolved question-review markers for publication."""

from __future__ import annotations

QUESTION_REVIEW_MARKERS_TABLE = "question_review_markers"
QUESTION_REVIEW_MARKERS_INDEX = "idx_question_review_markers_active"


def migrate_v56_to_v57(db) -> None:
    db.execute(
        f"""CREATE TABLE IF NOT EXISTS {QUESTION_REVIEW_MARKERS_TABLE}(
            source_kind TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            marker_kind TEXT NOT NULL CHECK(marker_kind IN('suspect','uncertain')),
            marker_scope TEXT NOT NULL CHECK(marker_scope IN('logic','structure')),
            message TEXT NOT NULL,
            source_sequence INTEGER,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(source_kind,candidate_id)
        )"""
    )
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {QUESTION_REVIEW_MARKERS_INDEX} "
        f"ON {QUESTION_REVIEW_MARKERS_TABLE}(active,source_kind,candidate_id)"
    )


def schema_issues(db) -> list[str]:
    columns = {
        str(row[1]): row
        for row in db.execute(
            f"PRAGMA table_info({QUESTION_REVIEW_MARKERS_TABLE})"
        )
    }
    expected = {
        "source_kind",
        "candidate_id",
        "marker_kind",
        "marker_scope",
        "message",
        "source_sequence",
        "active",
        "created_at",
        "updated_at",
    }
    issues: list[str] = []
    if set(columns) != expected:
        issues.append("question review marker table columns missing or unexpected")
    index = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (QUESTION_REVIEW_MARKERS_INDEX,),
    ).fetchone()
    expected_sql = (
        f"CREATE INDEX {QUESTION_REVIEW_MARKERS_INDEX} "
        f"ON {QUESTION_REVIEW_MARKERS_TABLE}(active,source_kind,candidate_id)"
    )
    if index is None or " ".join(str(index[0]).split()).upper() != " ".join(
        expected_sql.split()
    ).upper():
        issues.append("question review marker index missing or invalid")
    return issues


__all__ = [
    "QUESTION_REVIEW_MARKERS_TABLE",
    "QUESTION_REVIEW_MARKERS_INDEX",
    "migrate_v56_to_v57",
    "schema_issues",
]
