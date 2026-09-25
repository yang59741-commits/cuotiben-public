"""Schema 36: persist immutable profile-v3 activations and pointer history."""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict


BINDINGS_TABLE = "past_exam_profile_activation_bindings"
POINTERS_TABLE = "past_exam_profile_active_pointers"
TRANSITIONS_TABLE = "past_exam_profile_activation_transitions"

CREATE_BINDINGS_SQL = f"""CREATE TABLE IF NOT EXISTS {BINDINGS_TABLE}(
    activation_id TEXT NOT NULL PRIMARY KEY,
    combination_key TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    attestation_hash TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK(contract_version = 'past-exam-profile-activation-v1'),
    profile_json TEXT NOT NULL,
    attestation_json TEXT NOT NULL,
    UNIQUE(combination_key, activation_id),
    UNIQUE(
        combination_key, activation_id, profile_id, profile_hash,
        attestation_hash
    )
)"""

CREATE_POINTERS_SQL = f"""CREATE TABLE IF NOT EXISTS {POINTERS_TABLE}(
    combination_key TEXT NOT NULL PRIMARY KEY,
    institution TEXT NOT NULL,
    subject TEXT NOT NULL,
    reference_year INTEGER NOT NULL CHECK(reference_year >= 1),
    revision INTEGER NOT NULL CHECK(revision >= 1),
    active_activation_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(institution, subject, reference_year),
    FOREIGN KEY(combination_key, active_activation_id)
        REFERENCES {BINDINGS_TABLE}(combination_key, activation_id)
)"""

CREATE_TRANSITIONS_SQL = f"""CREATE TABLE IF NOT EXISTS {TRANSITIONS_TABLE}(
    combination_key TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision >= 1),
    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
    contract_version TEXT NOT NULL
        CHECK(contract_version = 'past-exam-profile-activation-receipt-v1'),
    operation TEXT NOT NULL CHECK(operation IN ('activate', 'rollback')),
    entry_revision_before INTEGER NOT NULL CHECK(entry_revision_before >= 0),
    entry_revision_after INTEGER NOT NULL CHECK(entry_revision_after >= 1),
    previous_activation_id TEXT,
    active_activation_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    profile_hash TEXT NOT NULL,
    attestation_hash TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    receipt_hash TEXT NOT NULL UNIQUE,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY(combination_key, revision),
    CHECK(entry_revision_after = revision),
    CHECK(entry_revision_before = revision - 1),
    CHECK(
        previous_activation_id IS NULL
        OR previous_activation_id <> active_activation_id
    ),
    CHECK(operation <> 'rollback' OR previous_activation_id IS NOT NULL),
    FOREIGN KEY(combination_key)
        REFERENCES {POINTERS_TABLE}(combination_key),
    FOREIGN KEY(combination_key, previous_activation_id)
        REFERENCES {BINDINGS_TABLE}(combination_key, activation_id),
    FOREIGN KEY(
        combination_key, active_activation_id, profile_id, profile_hash,
        attestation_hash
    ) REFERENCES {BINDINGS_TABLE}(
        combination_key, activation_id, profile_id, profile_hash,
        attestation_hash
    )
)"""

CREATE_INDEX_SQL = (
    f"CREATE INDEX IF NOT EXISTS idx_past_exam_profile_bindings_combination "
    f"ON {BINDINGS_TABLE}(combination_key, activation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_past_exam_profile_pointers_active "
    f"ON {POINTERS_TABLE}(active_activation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_past_exam_profile_transitions_active "
    f"ON {TRANSITIONS_TABLE}(combination_key, active_activation_id, revision)",
    f"CREATE INDEX IF NOT EXISTS idx_past_exam_profile_transitions_previous "
    f"ON {TRANSITIONS_TABLE}(combination_key, previous_activation_id, revision) "
    "WHERE previous_activation_id IS NOT NULL",
)

CREATE_TRIGGER_SQL = (
    f"""CREATE TRIGGER IF NOT EXISTS past_exam_profile_bindings_no_update
        BEFORE UPDATE ON {BINDINGS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'past_exam_profile_activation_binding_immutable');
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS past_exam_profile_bindings_no_delete
        BEFORE DELETE ON {BINDINGS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'past_exam_profile_activation_binding_immutable');
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS past_exam_profile_transitions_no_update
        BEFORE UPDATE ON {TRANSITIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'past_exam_profile_activation_transition_immutable');
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS past_exam_profile_transitions_no_delete
        BEFORE DELETE ON {TRANSITIONS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'past_exam_profile_activation_transition_immutable');
        END""",
)

SCHEMA_STATEMENTS = (
    CREATE_BINDINGS_SQL,
    CREATE_POINTERS_SQL,
    CREATE_TRANSITIONS_SQL,
    *CREATE_INDEX_SQL,
    *CREATE_TRIGGER_SQL,
)

EXPECTED_COLUMNS = {
    BINDINGS_TABLE: {
        "activation_id": ("TEXT", 1, None, 1),
        "combination_key": ("TEXT", 1, None, 0),
        "profile_id": ("TEXT", 1, None, 0),
        "profile_hash": ("TEXT", 1, None, 0),
        "attestation_hash": ("TEXT", 1, None, 0),
        "schema_version": ("INTEGER", 1, None, 0),
        "contract_version": ("TEXT", 1, None, 0),
        "profile_json": ("TEXT", 1, None, 0),
        "attestation_json": ("TEXT", 1, None, 0),
    },
    POINTERS_TABLE: {
        "combination_key": ("TEXT", 1, None, 1),
        "institution": ("TEXT", 1, None, 0),
        "subject": ("TEXT", 1, None, 0),
        "reference_year": ("INTEGER", 1, None, 0),
        "revision": ("INTEGER", 1, None, 0),
        "active_activation_id": ("TEXT", 1, None, 0),
        "updated_at": ("TEXT", 1, None, 0),
    },
    TRANSITIONS_TABLE: {
        "combination_key": ("TEXT", 1, None, 1),
        "revision": ("INTEGER", 1, None, 2),
        "schema_version": ("INTEGER", 1, None, 0),
        "contract_version": ("TEXT", 1, None, 0),
        "operation": ("TEXT", 1, None, 0),
        "entry_revision_before": ("INTEGER", 1, None, 0),
        "entry_revision_after": ("INTEGER", 1, None, 0),
        "previous_activation_id": ("TEXT", 0, None, 0),
        "active_activation_id": ("TEXT", 1, None, 0),
        "profile_id": ("TEXT", 1, None, 0),
        "profile_hash": ("TEXT", 1, None, 0),
        "attestation_hash": ("TEXT", 1, None, 0),
        "occurred_at": ("TEXT", 1, None, 0),
        "receipt_hash": ("TEXT", 1, None, 0),
        "receipt_json": ("TEXT", 1, None, 0),
    },
}
EXPECTED_TABLE_SQL = {
    BINDINGS_TABLE: CREATE_BINDINGS_SQL,
    POINTERS_TABLE: CREATE_POINTERS_SQL,
    TRANSITIONS_TABLE: CREATE_TRANSITIONS_SQL,
}

EXPECTED_CHECKS = {
    BINDINGS_TABLE: (
        "CHECK(SCHEMA_VERSION=1)",
        "CHECK(CONTRACT_VERSION='PAST-EXAM-PROFILE-ACTIVATION-V1')",
    ),
    POINTERS_TABLE: (
        "CHECK(REFERENCE_YEAR>=1)",
        "CHECK(REVISION>=1)",
    ),
    TRANSITIONS_TABLE: (
        "CHECK(REVISION>=1)",
        "CHECK(SCHEMA_VERSION=1)",
        "CHECK(CONTRACT_VERSION='PAST-EXAM-PROFILE-ACTIVATION-RECEIPT-V1')",
        "CHECK(OPERATIONIN('ACTIVATE','ROLLBACK'))",
        "CHECK(ENTRY_REVISION_BEFORE>=0)",
        "CHECK(ENTRY_REVISION_AFTER>=1)",
        "CHECK(ENTRY_REVISION_AFTER=REVISION)",
        "CHECK(ENTRY_REVISION_BEFORE=REVISION-1)",
        "CHECK(PREVIOUS_ACTIVATION_IDISNULLORPREVIOUS_ACTIVATION_ID<>ACTIVE_ACTIVATION_ID)",
        "CHECK(OPERATION<>'ROLLBACK'ORPREVIOUS_ACTIVATION_IDISNOTNULL)",
    ),
}

EXPECTED_UNIQUE_COLUMN_SETS = {
    BINDINGS_TABLE: {
        ("activation_id",),
        ("combination_key", "activation_id"),
        (
            "combination_key",
            "activation_id",
            "profile_id",
            "profile_hash",
            "attestation_hash",
        ),
    },
    POINTERS_TABLE: {
        ("combination_key",),
        ("institution", "subject", "reference_year"),
    },
    TRANSITIONS_TABLE: {
        ("combination_key", "revision"),
        ("receipt_hash",),
    },
}

EXPECTED_INDEXES = {
    "idx_past_exam_profile_bindings_combination": {
        "table": BINDINGS_TABLE,
        "columns": ("combination_key", "activation_id"),
        "unique": 0,
        "partial": 0,
        "predicate": None,
    },
    "idx_past_exam_profile_pointers_active": {
        "table": POINTERS_TABLE,
        "columns": ("active_activation_id",),
        "unique": 0,
        "partial": 0,
        "predicate": None,
    },
    "idx_past_exam_profile_transitions_active": {
        "table": TRANSITIONS_TABLE,
        "columns": ("combination_key", "active_activation_id", "revision"),
        "unique": 0,
        "partial": 0,
        "predicate": None,
    },
    "idx_past_exam_profile_transitions_previous": {
        "table": TRANSITIONS_TABLE,
        "columns": ("combination_key", "previous_activation_id", "revision"),
        "unique": 0,
        "partial": 1,
        "predicate": "WHEREPREVIOUS_ACTIVATION_IDISNOTNULL",
    },
}
EXPECTED_USER_INDEX_NAMES = {
    table: {
        name
        for name, expected in EXPECTED_INDEXES.items()
        if expected["table"] == table
    }
    for table in EXPECTED_COLUMNS
}

EXPECTED_FOREIGN_KEYS = {
    BINDINGS_TABLE: set(),
    POINTERS_TABLE: {
        (
            BINDINGS_TABLE,
            ("combination_key", "active_activation_id"),
            ("combination_key", "activation_id"),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
    TRANSITIONS_TABLE: {
        (
            POINTERS_TABLE,
            ("combination_key",),
            ("combination_key",),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
        (
            BINDINGS_TABLE,
            ("combination_key", "previous_activation_id"),
            ("combination_key", "activation_id"),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
        (
            BINDINGS_TABLE,
            (
                "combination_key",
                "active_activation_id",
                "profile_id",
                "profile_hash",
                "attestation_hash",
            ),
            (
                "combination_key",
                "activation_id",
                "profile_id",
                "profile_hash",
                "attestation_hash",
            ),
            "NO ACTION",
            "NO ACTION",
            "NONE",
        ),
    },
}

EXPECTED_TRIGGERS = {
    "past_exam_profile_bindings_no_update": CREATE_TRIGGER_SQL[0],
    "past_exam_profile_bindings_no_delete": CREATE_TRIGGER_SQL[1],
    "past_exam_profile_transitions_no_update": CREATE_TRIGGER_SQL[2],
    "past_exam_profile_transitions_no_delete": CREATE_TRIGGER_SQL[3],
}
EXPECTED_TRIGGER_NAMES = {
    BINDINGS_TABLE: {
        "past_exam_profile_bindings_no_update",
        "past_exam_profile_bindings_no_delete",
    },
    POINTERS_TABLE: set(),
    TRANSITIONS_TABLE: {
        "past_exam_profile_transitions_no_update",
        "past_exam_profile_transitions_no_delete",
    },
}


def _normalized_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _normalized_trigger_sql(value: object) -> str:
    # sqlite_master omits the optional IF NOT EXISTS phrase after creation.
    return _normalized_sql(value).replace("IFNOTEXISTS", "")


def _normalized_definition_sql(value: object) -> str:
    # Keep comments and every constraint token significant.  A comment that
    # merely contains CHECK text must never satisfy schema validation.
    return _normalized_sql(value).replace("IFNOTEXISTS", "")


def _table_schema_issues(
    db: sqlite3.Connection,
    table: str,
    *,
    missing_is_issue: bool = True,
) -> list[str]:
    master = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (table,),
    ).fetchone()
    if master is None:
        return [f"版本 36 缺少表 {table}"] if missing_is_issue else []
    if str(master[0]) != "table":
        return [f"版本 36 的对象 {table} 类型不兼容"]

    actual_columns = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    issues: list[str] = []
    if _normalized_definition_sql(master[1]) != _normalized_definition_sql(
        EXPECTED_TABLE_SQL[table]
    ):
        issues.append(f"版本 36 的表 {table} 定义不兼容")
    if set(actual_columns) != set(EXPECTED_COLUMNS[table]):
        missing = sorted(set(EXPECTED_COLUMNS[table]) - set(actual_columns))
        extra = sorted(set(actual_columns) - set(EXPECTED_COLUMNS[table]))
        if missing:
            issues.append(f"版本 36 的表 {table} 缺少字段 {','.join(missing)}")
        if extra:
            issues.append(f"版本 36 的表 {table} 存在未登记字段 {','.join(extra)}")
    for column, expected in EXPECTED_COLUMNS[table].items():
        actual = actual_columns.get(column)
        if actual is not None and actual != expected:
            issues.append(f"版本 36 的表 {table} 字段 {column} 定义不兼容")

    normalized_table_sql = _normalized_sql(master[1])
    for check in EXPECTED_CHECKS[table]:
        if check not in normalized_table_sql:
            issues.append(f"版本 36 的表 {table} 缺少约束 {check}")

    unique_column_sets = [
        tuple(
            str(info[2])
            for info in db.execute(f'PRAGMA index_info("{row[1]}")').fetchall()
        )
        for row in db.execute(f'PRAGMA index_list("{table}")').fetchall()
        if int(row[2]) == 1
    ]
    expected_unique = Counter(EXPECTED_UNIQUE_COLUMN_SETS[table])
    actual_unique = Counter(unique_column_sets)
    if actual_unique != expected_unique:
        issues.append(f"版本 36 的表 {table} 唯一约束集不兼容")

    actual_user_indexes = {
        str(row[1])
        for row in db.execute(f'PRAGMA index_list("{table}")').fetchall()
        if str(row[3]) == "c"
    }
    unexpected_indexes = sorted(
        actual_user_indexes - EXPECTED_USER_INDEX_NAMES[table]
    )
    if unexpected_indexes:
        issues.append(
            f"版本 36 的表 {table} 存在未登记索引 "
            f"{','.join(unexpected_indexes)}"
        )

    actual_triggers = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
    }
    unexpected_triggers = sorted(actual_triggers - EXPECTED_TRIGGER_NAMES[table])
    if unexpected_triggers:
        issues.append(
            f"版本 36 的表 {table} 存在未登记触发器 "
            f"{','.join(unexpected_triggers)}"
        )

    actual_foreign_keys = _foreign_key_groups(db, table)
    if actual_foreign_keys != EXPECTED_FOREIGN_KEYS[table]:
        issues.append(f"版本 36 的表 {table} 外键定义不兼容")
    return issues


def _foreign_key_groups(db: sqlite3.Connection, table: str) -> set[tuple]:
    grouped: dict[int, list[tuple]] = defaultdict(list)
    for row in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall():
        grouped[int(row[0])].append(tuple(row))
    result: set[tuple] = set()
    for rows in grouped.values():
        rows.sort(key=lambda row: int(row[1]))
        result.add(
            (
                str(rows[0][2]),
                tuple(str(row[3]) for row in rows),
                tuple(str(row[4]) for row in rows),
                str(rows[0][5]),
                str(rows[0][6]),
                str(rows[0][7]),
            )
        )
    return result


def _index_schema_issue(
    db: sqlite3.Connection,
    name: str,
    *,
    missing_is_issue: bool = True,
) -> str | None:
    expected = EXPECTED_INDEXES[name]
    master = db.execute(
        "SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchone()
    if master is None:
        return f"版本 36 缺少索引 {name}" if missing_is_issue else None
    if str(master[0]) != "index" or str(master[1]) != expected["table"]:
        return f"版本 36 的索引 {name} 定义不兼容"
    columns = tuple(
        str(row[2])
        for row in db.execute(f'PRAGMA index_info("{name}")').fetchall()
    )
    listed = {
        str(row[1]): (int(row[2]), int(row[4]))
        for row in db.execute(
            f'PRAGMA index_list("{expected["table"]}")'
        ).fetchall()
    }
    if columns != expected["columns"] or listed.get(name) != (
        expected["unique"],
        expected["partial"],
    ):
        return f"版本 36 的索引 {name} 定义不兼容"
    predicate = expected["predicate"]
    if predicate is not None and predicate not in _normalized_sql(master[2]):
        return f"版本 36 的索引 {name} 条件不兼容"
    return None


def _trigger_schema_issue(
    db: sqlite3.Connection,
    name: str,
    *,
    missing_is_issue: bool = True,
) -> str | None:
    master = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchone()
    if master is None:
        return f"版本 36 缺少触发器 {name}" if missing_is_issue else None
    if (
        str(master[0]) != "trigger"
        or _normalized_trigger_sql(master[1])
        != _normalized_trigger_sql(EXPECTED_TRIGGERS[name])
    ):
        return f"版本 36 的触发器 {name} 定义不兼容"
    return None


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table in EXPECTED_COLUMNS:
        issues.extend(_table_schema_issues(db, table))
    for name in EXPECTED_INDEXES:
        issue = _index_schema_issue(db, name)
        if issue:
            issues.append(issue)
    for name in EXPECTED_TRIGGERS:
        issue = _trigger_schema_issue(db, name)
        if issue:
            issues.append(issue)
    return issues


def _existing_object_conflicts(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table in EXPECTED_COLUMNS:
        issues.extend(_table_schema_issues(db, table, missing_is_issue=False))
    for name in EXPECTED_INDEXES:
        issue = _index_schema_issue(db, name, missing_is_issue=False)
        if issue:
            issues.append(issue)
    for name in EXPECTED_TRIGGERS:
        issue = _trigger_schema_issue(db, name, missing_is_issue=False)
        if issue:
            issues.append(issue)
    return issues


def migrate_v35_to_v36(db: sqlite3.Connection) -> None:
    conflicts = _existing_object_conflicts(db)
    if conflicts:
        raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


__all__ = [
    "BINDINGS_TABLE",
    "CREATE_BINDINGS_SQL",
    "CREATE_INDEX_SQL",
    "CREATE_POINTERS_SQL",
    "CREATE_TRANSITIONS_SQL",
    "CREATE_TRIGGER_SQL",
    "POINTERS_TABLE",
    "SCHEMA_STATEMENTS",
    "TRANSITIONS_TABLE",
    "migrate_v35_to_v36",
    "schema_issues",
]
