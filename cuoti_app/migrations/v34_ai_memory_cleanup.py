"""Schema 34: make the AI-memory cleanup ledger part of the database contract."""

from __future__ import annotations

import re
import sqlite3


TABLE_NAME = "ai_memory_cleanup"
CREATE_TABLE_SQL = """CREATE TABLE IF NOT EXISTS ai_memory_cleanup(
    cleanup_id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL CHECK(memory_type IN ('free','mistakes','past')),
    exam_id INTEGER NOT NULL,
    generation_job_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','db_deleted','applied','aborted','failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error_code TEXT,
    last_error_summary TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)"""
CREATE_INDEX_SQL = (
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_ai_memory_cleanup_exam_type
        ON ai_memory_cleanup(memory_type,exam_id)
        WHERE status IN ('pending','db_deleted','failed')""",
    """CREATE INDEX IF NOT EXISTS idx_ai_memory_cleanup_status
        ON ai_memory_cleanup(status,updated_at)""",
)
SCHEMA_STATEMENTS = (CREATE_TABLE_SQL, *CREATE_INDEX_SQL)


EXPECTED_COLUMNS = {
    "cleanup_id": ("TEXT", 0, None, 1),
    "memory_type": ("TEXT", 1, None, 0),
    "exam_id": ("INTEGER", 1, None, 0),
    "generation_job_id": ("TEXT", 0, None, 0),
    "status": ("TEXT", 1, None, 0),
    "attempts": ("INTEGER", 1, "0", 0),
    "last_error_code": ("TEXT", 0, None, 0),
    "last_error_summary": ("TEXT", 0, None, 0),
    "created_at": ("TEXT", 1, None, 0),
    "updated_at": ("TEXT", 1, None, 0),
}
EXPECTED_CHECKS = (
    "CHECK(MEMORY_TYPEIN('FREE','MISTAKES','PAST'))",
    "CHECK(STATUSIN('PENDING','DB_DELETED','APPLIED','ABORTED','FAILED'))",
    "CHECK(ATTEMPTS>=0)",
)
EXPECTED_INDEXES = {
    "idx_ai_memory_cleanup_exam_type": {
        "columns": ("memory_type", "exam_id"),
        "unique": 1,
        "partial": 1,
        "predicate": "WHERESTATUSIN('PENDING','DB_DELETED','FAILED')",
    },
    "idx_ai_memory_cleanup_status": {
        "columns": ("status", "updated_at"),
        "unique": 0,
        "partial": 0,
        "predicate": None,
    },
}


def _normalized_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _table_schema_issues(db: sqlite3.Connection) -> list[str]:
    row = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (TABLE_NAME,),
    ).fetchone()
    if row is None or str(row[0]) != "table":
        return [f"版本 34 缺少表 {TABLE_NAME}"]

    actual_columns = {
        str(info[1]): (
            str(info[2]).upper(),
            int(info[3]),
            None if info[4] is None else str(info[4]),
            int(info[5]),
        )
        for info in db.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()
    }
    issues: list[str] = []
    for column, expected in EXPECTED_COLUMNS.items():
        actual = actual_columns.get(column)
        if actual is None:
            issues.append(f"版本 34 的表 {TABLE_NAME} 缺少字段 {column}")
        elif actual != expected:
            issues.append(f"版本 34 的表 {TABLE_NAME} 字段 {column} 定义不兼容")

    normalized_table_sql = _normalized_sql(row[1])
    for check in EXPECTED_CHECKS:
        if check not in normalized_table_sql:
            issues.append(f"版本 34 的表 {TABLE_NAME} 缺少约束 {check}")
    return issues

def _index_schema_issues(db: sqlite3.Connection) -> list[str]:
    listed = {
        str(row[1]): (int(row[2]), int(row[4]))
        for row in db.execute(f'PRAGMA index_list("{TABLE_NAME}")').fetchall()
    }
    issues: list[str] = []
    for name, expected in EXPECTED_INDEXES.items():
        master = db.execute(
            "SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?",
            (name,),
        ).fetchone()
        if master is None or str(master[0]) != "index":
            issues.append(f"版本 34 缺少索引 {name}")
            continue
        columns = tuple(
            str(row[2])
            for row in db.execute(f'PRAGMA index_info("{name}")').fetchall()
        )
        flags = listed.get(name)
        if (
            str(master[1]) != TABLE_NAME
            or columns != expected["columns"]
            or flags != (expected["unique"], expected["partial"])
        ):
            issues.append(f"版本 34 的索引 {name} 定义不兼容")
            continue
        predicate = expected["predicate"]
        normalized_index_sql = _normalized_sql(master[2])
        if predicate is not None and predicate not in normalized_index_sql:
            issues.append(f"版本 34 的索引 {name} 条件不兼容")
    return issues


def migrate_v33_to_v34(db: sqlite3.Connection) -> None:
    existing = db.execute(
        "SELECT type FROM sqlite_master WHERE name=?",
        (TABLE_NAME,),
    ).fetchone()
    if existing is not None:
        issues = _table_schema_issues(db)
        if issues:
            raise RuntimeError("；".join(issues))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues = _table_schema_issues(db)
    if any(issue == f"版本 34 缺少表 {TABLE_NAME}" for issue in issues):
        return issues
    issues.extend(_index_schema_issues(db))
    return issues
