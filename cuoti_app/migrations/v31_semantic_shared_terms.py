"""Schema 31: reusable semantic term sets and projection references."""

from __future__ import annotations

import sqlite3


SHARED_TERM_TABLES = (
    "semantic_recall_term_dictionary",
    "semantic_recall_term_sets",
    "semantic_recall_term_set_items",
    "semantic_recall_projection_term_sets",
    "semantic_recall_weight_profiles",
    "semantic_recall_shared_migrations",
)

SHARED_TERM_SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS semantic_recall_term_dictionary(
        id INTEGER PRIMARY KEY,
        normalized_term TEXT NOT NULL UNIQUE
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_recall_term_sets(
        id INTEGER PRIMARY KEY,
        logical_key TEXT NOT NULL UNIQUE,
        card_version_id INTEGER NOT NULL,
        dictionary_version TEXT NOT NULL,
        logical_hash TEXT NOT NULL CHECK(length(logical_hash)=64),
        term_count INTEGER NOT NULL CHECK(term_count>=0),
        created_at TEXT NOT NULL,
        UNIQUE(card_version_id,dictionary_version,logical_hash),
        FOREIGN KEY(card_version_id) REFERENCES semantic_card_versions(id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_recall_term_set_items(
        term_set_id INTEGER NOT NULL,
        field_name TEXT NOT NULL,
        term_id INTEGER NOT NULL,
        PRIMARY KEY(term_set_id,field_name,term_id),
        FOREIGN KEY(term_set_id) REFERENCES semantic_recall_term_sets(id)
            ON DELETE CASCADE,
        FOREIGN KEY(term_id) REFERENCES semantic_recall_term_dictionary(id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_recall_projection_term_sets(
        projection_id TEXT NOT NULL,
        semantic_question_id INTEGER NOT NULL,
        term_set_id INTEGER NOT NULL,
        weights_version TEXT NOT NULL,
        PRIMARY KEY(projection_id,semantic_question_id),
        FOREIGN KEY(projection_id,semantic_question_id)
            REFERENCES semantic_recall_documents(projection_id,semantic_question_id)
            ON DELETE CASCADE,
        FOREIGN KEY(term_set_id) REFERENCES semantic_recall_term_sets(id)
            ON DELETE RESTRICT
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_recall_weight_profiles(
        weights_version TEXT NOT NULL,
        field_name TEXT NOT NULL,
        weight REAL NOT NULL CHECK(weight>0 AND weight<=10),
        PRIMARY KEY(weights_version,field_name)
    )""",
    """CREATE TABLE IF NOT EXISTS semantic_recall_shared_migrations(
        projection_id TEXT PRIMARY KEY,
        schema_name TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
        last_semantic_question_id INTEGER NOT NULL DEFAULT 0,
        migrated_documents INTEGER NOT NULL DEFAULT 0,
        legacy_term_count INTEGER NOT NULL DEFAULT 0,
        legacy_digest TEXT,
        shared_digest TEXT,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(projection_id) REFERENCES semantic_recall_projection_versions(id)
            ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_semantic_term_set_items_term "
    "ON semantic_recall_term_set_items(term_id,field_name,term_set_id)",
    "CREATE INDEX IF NOT EXISTS idx_semantic_projection_term_sets_set "
    "ON semantic_recall_projection_term_sets(term_set_id,projection_id)",
)


REQUIRED_SCHEMA = {
    "tables": set(SHARED_TERM_TABLES),
    "indexes": {
        "idx_semantic_term_set_items_term",
        "idx_semantic_projection_term_sets_set",
    },
    "columns": {
        "semantic_recall_term_dictionary": {"id", "normalized_term"},
        "semantic_recall_term_sets": {
            "id", "logical_key", "card_version_id", "dictionary_version",
            "logical_hash", "term_count", "created_at",
        },
        "semantic_recall_term_set_items": {"term_set_id", "field_name", "term_id"},
        "semantic_recall_projection_term_sets": {
            "projection_id", "semantic_question_id", "term_set_id", "weights_version",
        },
        "semantic_recall_weight_profiles": {"weights_version", "field_name", "weight"},
        "semantic_recall_shared_migrations": {
            "projection_id", "schema_name", "status", "last_semantic_question_id",
            "migrated_documents", "legacy_term_count", "legacy_digest", "shared_digest",
            "started_at", "updated_at",
        },
    },
    "foreign_keys": {
        "semantic_recall_term_sets": {
            ("card_version_id", "semantic_card_versions", "id"),
        },
        "semantic_recall_term_set_items": {
            ("term_set_id", "semantic_recall_term_sets", "id"),
            ("term_id", "semantic_recall_term_dictionary", "id"),
        },
        "semantic_recall_projection_term_sets": {
            ("projection_id", "semantic_recall_documents", "projection_id"),
            ("semantic_question_id", "semantic_recall_documents", "semantic_question_id"),
            ("term_set_id", "semantic_recall_term_sets", "id"),
        },
        "semantic_recall_shared_migrations": {
            ("projection_id", "semantic_recall_projection_versions", "id"),
        },
    },
}


def migrate_v30_to_v31(db: sqlite3.Connection) -> None:
    for statement in SHARED_TERM_SCHEMA_STATEMENTS:
        db.execute(statement)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    issues: list[str] = []
    for table in sorted(REQUIRED_SCHEMA["tables"]):
        if ("table", table) not in objects:
            issues.append(f"版本 31 缺少表 {table}")
    for index in sorted(REQUIRED_SCHEMA["indexes"]):
        if ("index", index) not in objects:
            issues.append(f"版本 31 缺少索引 {index}")
    for table, required in REQUIRED_SCHEMA["columns"].items():
        if ("table", table) not in objects:
            continue
        columns = {
            str(row[1])
            for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for column in sorted(required - columns):
            issues.append(f"版本 31 的表 {table} 缺少字段 {column}")
    for table, required in REQUIRED_SCHEMA["foreign_keys"].items():
        if ("table", table) not in objects:
            continue
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        }
        for source, target_table, target_column in sorted(required - foreign_keys):
            issues.append(
                f"版本 31 的表 {table} 缺少外键 "
                f"{source}->{target_table}.{target_column}"
            )
    return issues

