"""Schema 59: durable daily self-review sessions and item results."""

from __future__ import annotations

import sqlite3


DAILY_REVIEW_ROUNDS_TABLE = "daily_review_rounds"
DAILY_REVIEW_ITEMS_TABLE = "daily_review_items"

ROUND_STATUS_INDEX = "idx_daily_review_rounds_status_created"
ROUND_NUMBER_INDEX = "idx_daily_review_rounds_number"
ITEM_ROUND_INDEX = "idx_daily_review_items_round_status"
ITEM_MISTAKE_INDEX = "idx_daily_review_items_mistake_result"
ITEM_RESERVATION_INDEX = "idx_daily_review_items_reservation"
ITEM_EXPORT_INDEX = "idx_daily_review_items_export_batch"
ITEM_JOB_INDEX = "idx_daily_review_items_source_job"
ITEM_UNIQUE_INDEX = "ux_daily_review_items_round_mistake"
ITEM_RESULT_KEY_INDEX = "ux_daily_review_items_result_key"


ROUND_TABLE_SQL = f"""CREATE TABLE {DAILY_REVIEW_ROUNDS_TABLE}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number INTEGER NOT NULL CHECK(round_number>0),
    generation_mode TEXT NOT NULL CHECK(generation_mode IN ('earliest','random')),
    status TEXT NOT NULL DEFAULT 'generating'
        CHECK(status IN ('generating','published','completed','failed','cancelled')),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    source_job_id TEXT UNIQUE,
    item_count INTEGER NOT NULL DEFAULT 0 CHECK(item_count>=0),
    published_at TEXT
)"""

ITEM_TABLE_SQL = f"""CREATE TABLE {DAILY_REVIEW_ITEMS_TABLE}(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL,
    round_number INTEGER NOT NULL CHECK(round_number>0),
    mistake_id INTEGER NOT NULL,
    export_batch_id INTEGER,
    reserved_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK(status IN ('reserved','published')),
    completion_result TEXT
        CHECK(completion_result IS NULL OR completion_result IN ('correct','again','incomplete')),
    completed_at TEXT,
    result_key TEXT,
    source_job_id TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(round_id,mistake_id),
    FOREIGN KEY(round_id) REFERENCES {DAILY_REVIEW_ROUNDS_TABLE}(id) ON DELETE CASCADE,
    FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE,
    FOREIGN KEY(export_batch_id) REFERENCES mistake_export_batches(id) ON DELETE SET NULL
)"""


def migrate_v58_to_v59(db: sqlite3.Connection) -> None:
    """Add only the two state tables required by daily self-review."""

    db.execute(ROUND_TABLE_SQL)
    db.execute(ITEM_TABLE_SQL)
    db.execute(
        f"CREATE INDEX {ROUND_STATUS_INDEX} "
        f"ON {DAILY_REVIEW_ROUNDS_TABLE}(status,created_at DESC,id DESC)"
    )
    db.execute(
        f"CREATE INDEX {ROUND_NUMBER_INDEX} "
        f"ON {DAILY_REVIEW_ROUNDS_TABLE}(round_number,id DESC)"
    )
    db.execute(
        f"CREATE UNIQUE INDEX {ITEM_UNIQUE_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(round_number,mistake_id)"
    )
    db.execute(
        f"CREATE INDEX {ITEM_ROUND_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(round_id,status,mistake_id)"
    )
    db.execute(
        f"CREATE INDEX {ITEM_MISTAKE_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(mistake_id,completion_result,completed_at DESC)"
    )
    db.execute(
        f"CREATE INDEX {ITEM_RESERVATION_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(round_number,status,reserved_at)"
    )
    db.execute(
        f"CREATE INDEX {ITEM_EXPORT_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(export_batch_id)"
    )
    db.execute(
        f"CREATE INDEX {ITEM_JOB_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(source_job_id)"
    )
    db.execute(
        f"CREATE UNIQUE INDEX {ITEM_RESULT_KEY_INDEX} "
        f"ON {DAILY_REVIEW_ITEMS_TABLE}(result_key) WHERE result_key IS NOT NULL"
    )
def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in db.execute(f'PRAGMA table_info("{table}")')}


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    expected = {
        DAILY_REVIEW_ROUNDS_TABLE: {
            "id", "round_number", "generation_mode", "status", "created_at",
            "completed_at", "source_job_id", "item_count", "published_at",
        },
        DAILY_REVIEW_ITEMS_TABLE: {
            "id", "round_id", "round_number", "mistake_id", "export_batch_id",
            "reserved_at", "status", "completion_result", "completed_at",
            "result_key", "source_job_id", "created_at",
        },
    }
    for table, columns in expected.items():
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is None:
            issues.append(f"缺少表 {table}")
            continue
        missing = columns - _columns(db, table)
        if missing:
            issues.append(f"表 {table} 缺少字段 {','.join(sorted(missing))}")
    for index in (
        ROUND_STATUS_INDEX,
        ROUND_NUMBER_INDEX,
        ITEM_ROUND_INDEX,
        ITEM_MISTAKE_INDEX,
        ITEM_RESERVATION_INDEX,
        ITEM_EXPORT_INDEX,
        ITEM_JOB_INDEX,
        ITEM_UNIQUE_INDEX,
        ITEM_RESULT_KEY_INDEX,
    ):
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (index,)
        ).fetchone() is None:
            issues.append(f"缺少索引 {index}")
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]))
        for row in db.execute(
            f'PRAGMA foreign_key_list("{DAILY_REVIEW_ITEMS_TABLE}")'
        ).fetchall()
    }
    for source, target, column in (
        ("round_id", DAILY_REVIEW_ROUNDS_TABLE, "id"),
        ("mistake_id", "mistakes", "id"),
        ("export_batch_id", "mistake_export_batches", "id"),
    ):
        if (source, target, column) not in foreign_keys:
            issues.append(
                f"表 {DAILY_REVIEW_ITEMS_TABLE} 缺少外键 {source}->{target}.{column}"
            )
    return issues


migrate_v58_to_v59.validation_tables = (
    DAILY_REVIEW_ROUNDS_TABLE,
    DAILY_REVIEW_ITEMS_TABLE,
)


__all__ = [
    "DAILY_REVIEW_ITEMS_TABLE",
    "DAILY_REVIEW_ROUNDS_TABLE",
    "ITEM_EXPORT_INDEX",
    "ITEM_JOB_INDEX",
    "ITEM_MISTAKE_INDEX",
    "ITEM_RESERVATION_INDEX",
    "ITEM_RESULT_KEY_INDEX",
    "ITEM_ROUND_INDEX",
    "ITEM_UNIQUE_INDEX",
    "ROUND_NUMBER_INDEX",
    "ROUND_STATUS_INDEX",
    "migrate_v58_to_v59",
    "schema_issues",
]
