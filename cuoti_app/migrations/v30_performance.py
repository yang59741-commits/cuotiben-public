"""Schema 30: persistent learning/statistics revisions for exact cache invalidation."""

from __future__ import annotations

import sqlite3


REVISION_TABLES = (
    "mistakes",
    "review_events",
    "exam_attempts",
    "exam_attempt_items",
)


def migrate_v29_to_v30(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS app_revisions("
        "id INTEGER PRIMARY KEY CHECK(id=1),"
        "learning_revision INTEGER NOT NULL DEFAULT 0 CHECK(learning_revision>=0),"
        "statistics_revision INTEGER NOT NULL DEFAULT 0 CHECK(statistics_revision>=0),"
        "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    db.execute(
        "INSERT OR IGNORE INTO app_revisions(id,learning_revision,statistics_revision) "
        "VALUES(1,0,0)"
    )
    snapshot_columns = {
        str(row[1])
        for row in db.execute(
            'PRAGMA table_info("learning_daily_snapshots")'
        ).fetchall()
    }
    if "learning_revision" not in snapshot_columns:
        db.execute(
            "ALTER TABLE learning_daily_snapshots "
            "ADD COLUMN learning_revision INTEGER NOT NULL DEFAULT 0"
        )
    for table in REVISION_TABLES:
        learning_delta = "learning_revision=learning_revision+1," if table == "mistakes" else ""
        for operation in ("INSERT", "UPDATE", "DELETE"):
            trigger = f"revision_{table}_{operation.casefold()}"
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS {trigger} AFTER {operation} ON {table} BEGIN "
                f"UPDATE app_revisions SET {learning_delta}"
                "statistics_revision=statistics_revision+1,updated_at=datetime('now') "
                "WHERE id=1; END"
            )


REQUIRED_SCHEMA = {
    "tables": {"app_revisions"},
    "columns": {
        "app_revisions": {
            "id",
            "learning_revision",
            "statistics_revision",
            "updated_at",
        },
        "learning_daily_snapshots": {"learning_revision"},
    },
    "triggers": {
        f"revision_{table}_{operation}"
        for table in REVISION_TABLES
        for operation in ("insert", "update", "delete")
    },
}


def schema_issues(db: sqlite3.Connection) -> list[str]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger')"
        ).fetchall()
    }
    issues: list[str] = []
    for table in sorted(REQUIRED_SCHEMA["tables"]):
        if ("table", table) not in objects:
            issues.append(f"版本 30 缺少表 {table}")
    for table, required in REQUIRED_SCHEMA["columns"].items():
        if ("table", table) not in objects:
            continue
        columns = {
            str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for column in sorted(required - columns):
            issues.append(f"版本 30 的表 {table} 缺少字段 {column}")
    for trigger in sorted(REQUIRED_SCHEMA["triggers"]):
        if ("trigger", trigger) not in objects:
            issues.append(f"版本 30 缺少触发器 {trigger}")
    return issues
