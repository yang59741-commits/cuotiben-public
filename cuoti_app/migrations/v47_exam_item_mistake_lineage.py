"""Schema 47: immutable, multi-source mistake lineage for published items."""

from __future__ import annotations

import re
import sqlite3


TABLE = "exam_item_mistake_lineage"

CREATE_TABLE_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
    exam_item_id INTEGER NOT NULL,
    source_order INTEGER NOT NULL CHECK(source_order >= 1),
    mistake_public_id TEXT NOT NULL CHECK(length(trim(mistake_public_id)) > 0),
    source_lineage_hash TEXT NOT NULL CHECK(length(source_lineage_hash)=64 AND source_lineage_hash NOT GLOB '*[^0-9a-f]*'),
    lineage_policy_version TEXT NOT NULL CHECK(length(trim(lineage_policy_version)) > 0),
    content_fingerprint TEXT NOT NULL CHECK(length(content_fingerprint)=64 AND content_fingerprint NOT GLOB '*[^0-9a-f]*'),
    created_at TEXT NOT NULL,
    PRIMARY KEY(exam_item_id,source_order),
    UNIQUE(exam_item_id,mistake_public_id),
    FOREIGN KEY(exam_item_id) REFERENCES exam_items(id) ON UPDATE RESTRICT ON DELETE CASCADE,
    FOREIGN KEY(mistake_public_id) REFERENCES mistakes(public_id) ON UPDATE RESTRICT ON DELETE RESTRICT
)"""

CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE}_mistake ON {TABLE}(mistake_public_id,exam_item_id)",
)

TRIGGER_SQL = {
    f"{TABLE}_no_update": f"""CREATE TRIGGER IF NOT EXISTS {TABLE}_no_update
        BEFORE UPDATE ON {TABLE}
        BEGIN SELECT RAISE(ABORT,'{TABLE}_immutable'); END""",
}

SCHEMA_STATEMENTS = (
    CREATE_TABLE_SQL,
    *CREATE_INDEX_SQL,
    *TRIGGER_SQL.values(),
)


def _norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    row = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)
    ).fetchone()
    if row is None:
        issues.append(f"版本 47 缺少表 {TABLE}")
        return issues
    if str(row[0]) != "table" or _norm(row[1]) != _norm(CREATE_TABLE_SQL):
        issues.append(f"版本 47 的表 {TABLE} 定义不兼容")
    expected_indexes = {f"idx_{TABLE}_mistake"}
    actual_indexes = {
        str(index[1])
        for index in db.execute(f"PRAGMA index_list('{TABLE}')")
        if str(index[1]).startswith(f"idx_{TABLE}_")
    }
    if actual_indexes != expected_indexes:
        issues.append(f"版本 47 的表 {TABLE} 索引集合不兼容")
    expected_triggers = set(TRIGGER_SQL)
    actual_triggers = {
        str(trigger[0])
        for trigger in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (TABLE,),
        )
    }
    if actual_triggers != expected_triggers:
        issues.append(f"版本 47 的表 {TABLE} 触发器集合不兼容")
    return issues


def migrate_v46_to_v47(db: sqlite3.Connection) -> None:
    row = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)
    ).fetchone()
    if row is not None and (
        str(row[0]) != "table" or _norm(row[1]) != _norm(CREATE_TABLE_SQL)
    ):
        raise RuntimeError(f"版本 47 的对象 {TABLE} 定义不兼容")
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    issues = schema_issues(db)
    if issues:
        raise RuntimeError("；".join(issues))


__all__ = ["TABLE", "SCHEMA_STATEMENTS", "migrate_v46_to_v47", "schema_issues"]
