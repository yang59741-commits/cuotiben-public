"""Durable delivery state for published exam request/result memory events."""

from __future__ import annotations

import re
import sqlite3


MEMORY_EVENT_OUTBOX = "exam_generation_memory_event_outbox_v1"
MEMORY_EVENT_DELIVERY_HEADS = "exam_generation_memory_event_delivery_heads_v1"


TABLE_SQL = {
    MEMORY_EVENT_OUTBOX: f"""CREATE TABLE {MEMORY_EVENT_OUTBOX}(
        operation_id TEXT NOT NULL PRIMARY KEY,
        exam_id INTEGER NOT NULL UNIQUE CHECK(exam_id>0),
        mode TEXT NOT NULL CHECK(mode IN ('free','mistakes','past')),
        actor TEXT NOT NULL CHECK(actor IN ('free_exam','mistake_exam','past_exam')),
        event_day TEXT NOT NULL CHECK(length(event_day)=10),
        published_at TEXT NOT NULL,
        events_json TEXT NOT NULL CHECK(json_valid(events_json)),
        created_at TEXT NOT NULL,
        CHECK((mode='free' AND actor='free_exam')
           OR (mode='mistakes' AND actor='mistake_exam')
           OR (mode='past' AND actor='past_exam'))
    )""",
    MEMORY_EVENT_DELIVERY_HEADS: f"""CREATE TABLE {MEMORY_EVENT_DELIVERY_HEADS}(
        operation_id TEXT NOT NULL PRIMARY KEY,
        state TEXT NOT NULL CHECK(state IN ('pending','delivered')),
        revision INTEGER NOT NULL CHECK(revision>0),
        delivered_at TEXT,
        updated_at TEXT NOT NULL,
        CHECK((state='pending' AND revision=1 AND delivered_at IS NULL)
           OR (state='delivered' AND revision=2 AND delivered_at IS NOT NULL)),
        FOREIGN KEY(operation_id) REFERENCES {MEMORY_EVENT_OUTBOX}(operation_id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}


TRIGGER_SQL = {
    f"{MEMORY_EVENT_DELIVERY_HEADS}_cas_only": (
        f"CREATE TRIGGER {MEMORY_EVENT_DELIVERY_HEADS}_cas_only "
        f"BEFORE UPDATE ON {MEMORY_EVENT_DELIVERY_HEADS} WHEN "
        "NEW.operation_id!=OLD.operation_id OR OLD.state!='pending' "
        "OR NEW.state!='delivered' OR NEW.revision!=OLD.revision+1 "
        "OR OLD.delivered_at IS NOT NULL OR NEW.delivered_at IS NULL "
        f"BEGIN SELECT RAISE(ABORT,'{MEMORY_EVENT_DELIVERY_HEADS} CAS only'); END"
    ),
}


INDEX_SQL = {
    f"{MEMORY_EVENT_DELIVERY_HEADS}_pending": (
        f"CREATE INDEX {MEMORY_EVENT_DELIVERY_HEADS}_pending ON "
        f"{MEMORY_EVENT_DELIVERY_HEADS}(state,updated_at,operation_id)"
    ),
}


SCHEMA_STATEMENTS = (
    *TABLE_SQL.values(),
    *TRIGGER_SQL.values(),
    *INDEX_SQL.values(),
)


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for name, expected in TABLE_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None or row[0] != "table" or _normalized(row[1]) != _normalized(expected):
            issues.append(f"table_drift:{name}")
    for name, expected in TRIGGER_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None or row[0] != "trigger" or _normalized(row[1]) != _normalized(expected):
            issues.append(f"trigger_drift:{name}")
    for name, expected in INDEX_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None or row[0] != "index" or _normalized(row[1]) != _normalized(expected):
            issues.append(f"index_drift:{name}")
    if issues:
        return issues
    return issues


def migrate_v53_to_v54(db: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    issues = schema_issues(db)
    if issues:
        raise RuntimeError(
            "v54 exam memory delivery schema validation failed: " + ";".join(issues)
        )


__all__ = [
    "INDEX_SQL",
    "MEMORY_EVENT_DELIVERY_HEADS",
    "MEMORY_EVENT_OUTBOX",
    "SCHEMA_STATEMENTS",
    "TABLE_SQL",
    "TRIGGER_SQL",
    "migrate_v53_to_v54",
    "schema_issues",
]
