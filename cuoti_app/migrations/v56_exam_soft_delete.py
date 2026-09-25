"""Schema 56: retain published exams and their artifacts in a trash state."""

from __future__ import annotations


EXAM_SOFT_DELETE_INDEX = "idx_exams_artifact_deleted_id"
EXAM_SOFT_DELETE_REVISION_TRIGGER = "revision_exams_soft_delete_update"


def _revision_trigger_sql() -> str:
    return (
        f"CREATE TRIGGER {EXAM_SOFT_DELETE_REVISION_TRIGGER} "
        "AFTER UPDATE OF deleted_at ON exams "
        "WHEN OLD.deleted_at IS NOT NEW.deleted_at BEGIN "
        "UPDATE app_revisions SET statistics_revision=statistics_revision+1,"
        "updated_at=datetime('now') WHERE id=1; END"
    )


def migrate_v55_to_v56(db) -> None:
    columns = {str(row[1]) for row in db.execute("PRAGMA table_info(exams)")}
    if "deleted_at" not in columns:
        db.execute("ALTER TABLE exams ADD COLUMN deleted_at TEXT")
    db.execute(
        f"CREATE INDEX IF NOT EXISTS {EXAM_SOFT_DELETE_INDEX} "
        "ON exams(artifact_status,deleted_at,id DESC)"
    )
    db.execute(_revision_trigger_sql())


def schema_issues(db) -> list[str]:
    columns = {
        str(row[1]): row
        for row in db.execute("PRAGMA table_info(exams)")
    }
    deleted = columns.get("deleted_at")
    issues: list[str] = []
    if deleted is None or int(deleted[3]) != 0:
        issues.append("exams.deleted_at missing or not nullable")
    index = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (EXAM_SOFT_DELETE_INDEX,),
    ).fetchone()
    expected = (
        f"CREATE INDEX {EXAM_SOFT_DELETE_INDEX} "
        "ON exams(artifact_status,deleted_at,id DESC)"
    )
    if index is None or " ".join(str(index[0]).split()).upper() != " ".join(expected.split()).upper():
        issues.append("exam soft-delete index missing or invalid")
    trigger = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (EXAM_SOFT_DELETE_REVISION_TRIGGER,),
    ).fetchone()
    if (
        trigger is None
        or " ".join(str(trigger[0]).split()).upper()
        != " ".join(_revision_trigger_sql().split()).upper()
    ):
        issues.append("exam soft-delete statistics revision trigger missing or invalid")
    return issues


__all__ = [
    "EXAM_SOFT_DELETE_INDEX",
    "EXAM_SOFT_DELETE_REVISION_TRIGGER",
    "migrate_v55_to_v56",
    "schema_issues",
]
