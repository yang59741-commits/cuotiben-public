"""Schema 37: make persisted background-job input rows update-immutable."""

from __future__ import annotations

import re
import sqlite3


TABLE_NAME = "job_inputs"
TRIGGER_NAME = "job_inputs_no_update"
CREATE_TRIGGER_SQL = f"""CREATE TRIGGER IF NOT EXISTS {TRIGGER_NAME}
    BEFORE UPDATE ON {TABLE_NAME}
    BEGIN
        SELECT RAISE(ABORT, 'job_input_immutable');
    END"""


def _canonical_sql(value: object) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    return normalized.replace("CREATE TRIGGER IF NOT EXISTS ", "CREATE TRIGGER ", 1)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    table = db.execute(
        "SELECT type FROM sqlite_master WHERE name=?",
        (TABLE_NAME,),
    ).fetchone()
    if table is None or str(table[0]) != "table":
        return [f"版本 37 缺少表 {TABLE_NAME}"]
    rows = db.execute(
        "SELECT name,sql FROM sqlite_master "
        "WHERE type='trigger' AND tbl_name=? ORDER BY name",
        (TABLE_NAME,),
    ).fetchall()
    names = [str(row[0]) for row in rows]
    if names != [TRIGGER_NAME]:
        return [f"版本 37 的表 {TABLE_NAME} 触发器集合不兼容"]
    if _canonical_sql(rows[0][1]) != _canonical_sql(CREATE_TRIGGER_SQL):
        return [f"版本 37 的触发器 {TRIGGER_NAME} 定义不兼容"]
    return []


def migrate_v36_to_v37(db: sqlite3.Connection) -> None:
    db.execute(CREATE_TRIGGER_SQL)


__all__ = [
    "CREATE_TRIGGER_SQL",
    "TABLE_NAME",
    "TRIGGER_NAME",
    "migrate_v36_to_v37",
    "schema_issues",
]
