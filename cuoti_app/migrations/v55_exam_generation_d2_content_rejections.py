"""Task-local D2 content probe and rejection persistence."""

from __future__ import annotations

import sqlite3


def migrate_v54_to_v55(db: sqlite3.Connection) -> None:
    from ..services.exam_generation_d2_content_rejections import (
        d2_content_rejection_schema_issues,
        d2_content_rejection_schema_statements,
    )

    for statement in d2_content_rejection_schema_statements():
        db.execute(statement)
    issues = d2_content_rejection_schema_issues(db)
    if issues:
        raise RuntimeError(
            "v55 D2 content rejection schema validation failed: " + ";".join(issues)
        )


def schema_issues(db: sqlite3.Connection) -> list[str]:
    from ..services.exam_generation_d2_content_rejections import (
        d2_content_rejection_schema_issues,
    )

    return d2_content_rejection_schema_issues(db)


__all__ = ["migrate_v54_to_v55", "schema_issues"]
