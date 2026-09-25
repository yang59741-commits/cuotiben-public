"""Schema 33: independent daily-tip storage."""

from __future__ import annotations

import sqlite3


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS daily_tips(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL,
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>=1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_tips_public_id "
    "ON daily_tips(public_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_tips_content_hash "
    "ON daily_tips(content_hash)",
)


def migrate_v32_to_v33(db: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    issues: list[str] = []
    if ("table", "daily_tips") not in objects:
        issues.append("版本 33 缺少表 daily_tips")
        return issues
    columns = {
        str(row[1])
        for row in db.execute('PRAGMA table_info("daily_tips")').fetchall()
    }
    required = {
        "id", "public_id", "content", "content_hash", "revision",
        "created_at", "updated_at",
    }
    for column in sorted(required - columns):
        issues.append(f"版本 33 的表 daily_tips 缺少字段 {column}")
    for index in (
        "idx_daily_tips_content_hash",
        "idx_daily_tips_public_id",
    ):
        if ("index", index) not in objects:
            issues.append(f"版本 33 缺少索引 {index}")
    return issues
