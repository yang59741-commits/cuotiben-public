"""Schema 39: persist the first immutable past-v3 generation binding."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter, defaultdict


BINDINGS_TABLE = "exam_generation_attempt_bindings"
RECEIPTS_TABLE = "exam_generation_profile_resolved_receipts"

BINDING_CONTRACT_VERSION = "past-v3-attempt-binding-v1"
RECEIPT_CONTRACT_VERSION = "past-v3-profile-resolved-receipt-v1"
JOB_INPUT_IDENTITY_VERSION = "exam-generation-input-v1"
GENERATION_REQUEST_HASH_VERSION = "exam-generation-request-hash-v1"
SINGLE_PAPER_CONTRACT_VERSION = "exam-single-paper-v1"
PAST_PROFILE_FREEZE_CONTRACT_VERSION = "past-profile-job-freeze-v1"
PROFILE_WORKER_CONTEXT_VERSION = "past-profile-worker-context-v1"
EXACT_SLOT_CONTEXT_VERSION = "past-profile-v3-exact-slot-context-v1"

BACKGROUND_JOB_IDENTITY_INDEX = "ux_background_jobs_id_input_hash"
JOB_INPUT_IDENTITY_INDEX = "ux_job_inputs_job_payload_hash"
BINDINGS_TRIGGER = "exam_generation_attempt_bindings_no_update"
RECEIPTS_TRIGGER = "exam_generation_profile_resolved_receipts_no_update"


def _hash_check(column: str) -> str:
    return (
        f"CHECK(length({column}) = 64 "
        f"AND {column} NOT GLOB '*[^0-9a-f]*')"
    )


CREATE_BINDINGS_SQL = f"""CREATE TABLE IF NOT EXISTS {BINDINGS_TABLE}(
    job_id TEXT NOT NULL PRIMARY KEY,
    binding_contract_version TEXT NOT NULL
        CHECK(binding_contract_version = '{BINDING_CONTRACT_VERSION}'),
    job_input_identity_version TEXT NOT NULL
        CHECK(job_input_identity_version = '{JOB_INPUT_IDENTITY_VERSION}'),
    generation_request_hash_version TEXT NOT NULL
        CHECK(generation_request_hash_version = '{GENERATION_REQUEST_HASH_VERSION}'),
    single_paper_contract_version TEXT NOT NULL
        CHECK(single_paper_contract_version = '{SINGLE_PAPER_CONTRACT_VERSION}'),
    past_profile_freeze_contract_version TEXT NOT NULL
        CHECK(past_profile_freeze_contract_version = '{PAST_PROFILE_FREEZE_CONTRACT_VERSION}'),
    profile_worker_context_version TEXT NOT NULL
        CHECK(profile_worker_context_version = '{PROFILE_WORKER_CONTEXT_VERSION}'),
    exact_slot_context_version TEXT NOT NULL
        CHECK(exact_slot_context_version = '{EXACT_SLOT_CONTEXT_VERSION}'),
    logical_request_hash TEXT NOT NULL {_hash_check('logical_request_hash')},
    job_input_hash TEXT NOT NULL {_hash_check('job_input_hash')},
    generation_request_hash TEXT NOT NULL {_hash_check('generation_request_hash')},
    frozen_ref_hash TEXT NOT NULL {_hash_check('frozen_ref_hash')},
    profile_hash TEXT NOT NULL {_hash_check('profile_hash')},
    exact_slot_hash TEXT NOT NULL {_hash_check('exact_slot_hash')},
    resolution_hash TEXT NOT NULL {_hash_check('resolution_hash')},
    binding_hash TEXT NOT NULL {_hash_check('binding_hash')},
    created_at TEXT NOT NULL,
    UNIQUE(job_id, binding_hash),
    FOREIGN KEY(job_id, logical_request_hash)
        REFERENCES background_jobs(id, input_hash)
        ON UPDATE RESTRICT ON DELETE CASCADE,
    FOREIGN KEY(job_id, job_input_hash)
        REFERENCES job_inputs(job_id, payload_hash)
        ON UPDATE RESTRICT ON DELETE CASCADE
)"""

CREATE_RECEIPTS_SQL = f"""CREATE TABLE IF NOT EXISTS {RECEIPTS_TABLE}(
    job_id TEXT NOT NULL PRIMARY KEY,
    stage_name TEXT NOT NULL CHECK(stage_name = 'profile_resolved'),
    receipt_contract_version TEXT NOT NULL
        CHECK(receipt_contract_version = '{RECEIPT_CONTRACT_VERSION}'),
    binding_hash TEXT NOT NULL {_hash_check('binding_hash')},
    resolution_hash TEXT NOT NULL {_hash_check('resolution_hash')},
    receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
    created_at TEXT NOT NULL,
    UNIQUE(job_id, receipt_hash),
    FOREIGN KEY(job_id, binding_hash)
        REFERENCES {BINDINGS_TABLE}(job_id, binding_hash)
        ON UPDATE RESTRICT ON DELETE CASCADE
)"""

CREATE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX IF NOT EXISTS {BACKGROUND_JOB_IDENTITY_INDEX} "
    "ON background_jobs(id, input_hash)",
    f"CREATE UNIQUE INDEX IF NOT EXISTS {JOB_INPUT_IDENTITY_INDEX} "
    "ON job_inputs(job_id, payload_hash)",
)

CREATE_TRIGGER_SQL = (
    f"""CREATE TRIGGER IF NOT EXISTS {BINDINGS_TRIGGER}
        BEFORE UPDATE ON {BINDINGS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'exam_generation_attempt_binding_immutable');
        END""",
    f"""CREATE TRIGGER IF NOT EXISTS {RECEIPTS_TRIGGER}
        BEFORE UPDATE ON {RECEIPTS_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'exam_generation_profile_resolved_receipt_immutable');
        END""",
)

SCHEMA_STATEMENTS = (
    *CREATE_INDEX_SQL,
    CREATE_BINDINGS_SQL,
    CREATE_RECEIPTS_SQL,
    *CREATE_TRIGGER_SQL,
)

EXPECTED_COLUMNS = {
    BINDINGS_TABLE: {
        "job_id": ("TEXT", 1, None, 1),
        "binding_contract_version": ("TEXT", 1, None, 0),
        "job_input_identity_version": ("TEXT", 1, None, 0),
        "generation_request_hash_version": ("TEXT", 1, None, 0),
        "single_paper_contract_version": ("TEXT", 1, None, 0),
        "past_profile_freeze_contract_version": ("TEXT", 1, None, 0),
        "profile_worker_context_version": ("TEXT", 1, None, 0),
        "exact_slot_context_version": ("TEXT", 1, None, 0),
        "logical_request_hash": ("TEXT", 1, None, 0),
        "job_input_hash": ("TEXT", 1, None, 0),
        "generation_request_hash": ("TEXT", 1, None, 0),
        "frozen_ref_hash": ("TEXT", 1, None, 0),
        "profile_hash": ("TEXT", 1, None, 0),
        "exact_slot_hash": ("TEXT", 1, None, 0),
        "resolution_hash": ("TEXT", 1, None, 0),
        "binding_hash": ("TEXT", 1, None, 0),
        "created_at": ("TEXT", 1, None, 0),
    },
    RECEIPTS_TABLE: {
        "job_id": ("TEXT", 1, None, 1),
        "stage_name": ("TEXT", 1, None, 0),
        "receipt_contract_version": ("TEXT", 1, None, 0),
        "binding_hash": ("TEXT", 1, None, 0),
        "resolution_hash": ("TEXT", 1, None, 0),
        "receipt_hash": ("TEXT", 1, None, 0),
        "created_at": ("TEXT", 1, None, 0),
    },
}

EXPECTED_UNIQUE_COLUMN_SETS = {
    BINDINGS_TABLE: {
        ("job_id",),
        ("job_id", "binding_hash"),
    },
    RECEIPTS_TABLE: {
        ("job_id",),
        ("job_id", "receipt_hash"),
    },
}

EXPECTED_FOREIGN_KEYS = {
    BINDINGS_TABLE: {
        (
            "background_jobs",
            ("job_id", "logical_request_hash"),
            ("id", "input_hash"),
            "RESTRICT",
            "CASCADE",
            "NONE",
        ),
        (
            "job_inputs",
            ("job_id", "job_input_hash"),
            ("job_id", "payload_hash"),
            "RESTRICT",
            "CASCADE",
            "NONE",
        ),
    },
    RECEIPTS_TABLE: {
        (
            BINDINGS_TABLE,
            ("job_id", "binding_hash"),
            ("job_id", "binding_hash"),
            "RESTRICT",
            "CASCADE",
            "NONE",
        ),
    },
}

EXPECTED_INDEXES = {
    BACKGROUND_JOB_IDENTITY_INDEX: (
        "background_jobs",
        ("id", "input_hash"),
        1,
        0,
    ),
    JOB_INPUT_IDENTITY_INDEX: (
        "job_inputs",
        ("job_id", "payload_hash"),
        1,
        0,
    ),
}

EXPECTED_TRIGGERS = {
    BINDINGS_TRIGGER: (BINDINGS_TABLE, CREATE_TRIGGER_SQL[0]),
    RECEIPTS_TRIGGER: (RECEIPTS_TABLE, CREATE_TRIGGER_SQL[1]),
}
EXPECTED_TRIGGER_NAMES = {
    BINDINGS_TABLE: {BINDINGS_TRIGGER},
    RECEIPTS_TABLE: {RECEIPTS_TRIGGER},
}

BINDING_HASH_FIELDS = (
    "binding_contract_version",
    "job_id",
    "job_input_identity_version",
    "generation_request_hash_version",
    "single_paper_contract_version",
    "past_profile_freeze_contract_version",
    "profile_worker_context_version",
    "exact_slot_context_version",
    "logical_request_hash",
    "job_input_hash",
    "generation_request_hash",
    "frozen_ref_hash",
    "profile_hash",
    "exact_slot_hash",
    "resolution_hash",
)
RECEIPT_HASH_FIELDS = (
    "receipt_contract_version",
    "stage_name",
    "job_id",
    "binding_hash",
    "resolution_hash",
)


def _normalized_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _normalized_definition_sql(value: object) -> str:
    return _normalized_sql(value).replace("IFNOTEXISTS", "")


def _canonical_hash(value: dict[str, object]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
        return [f"版本 39 缺少表 {table}"] if missing_is_issue else []
    if str(master[0]) != "table":
        return [f"版本 39 的对象 {table} 类型不兼容"]
    expected_sql = (
        CREATE_BINDINGS_SQL if table == BINDINGS_TABLE else CREATE_RECEIPTS_SQL
    )
    issues: list[str] = []
    if _normalized_definition_sql(master[1]) != _normalized_definition_sql(
        expected_sql
    ):
        issues.append(f"版本 39 的表 {table} 定义不兼容")

    actual_columns = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
    }
    if actual_columns != EXPECTED_COLUMNS[table]:
        issues.append(f"版本 39 的表 {table} 字段集合不兼容")

    unique_sets = Counter(
        tuple(
            str(info[2])
            for info in db.execute(
                f'PRAGMA index_info("{row[1]}")'
            ).fetchall()
        )
        for row in db.execute(f'PRAGMA index_list("{table}")').fetchall()
        if int(row[2]) == 1
    )
    if unique_sets != Counter(EXPECTED_UNIQUE_COLUMN_SETS[table]):
        issues.append(f"版本 39 的表 {table} 唯一约束集不兼容")

    user_indexes = {
        str(row[1])
        for row in db.execute(f'PRAGMA index_list("{table}")').fetchall()
        if str(row[3]) == "c"
    }
    if user_indexes:
        issues.append(f"版本 39 的表 {table} 存在未登记索引")

    if _foreign_key_groups(db, table) != EXPECTED_FOREIGN_KEYS[table]:
        issues.append(f"版本 39 的表 {table} 外键定义不兼容")

    trigger_names = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
            (table,),
        ).fetchall()
    }
    expected_triggers = EXPECTED_TRIGGER_NAMES[table]
    if (
        trigger_names != expected_triggers
        if missing_is_issue
        else bool(trigger_names - expected_triggers)
    ):
        issues.append(f"版本 39 的表 {table} 触发器集合不兼容")
    return issues


def _index_schema_issue(
    db: sqlite3.Connection,
    name: str,
    *,
    missing_is_issue: bool = True,
) -> str | None:
    expected_table, expected_columns, expected_unique, expected_partial = (
        EXPECTED_INDEXES[name]
    )
    master = db.execute(
        "SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchone()
    if master is None:
        return f"版本 39 缺少索引 {name}" if missing_is_issue else None
    listed = {
        str(row[1]): (int(row[2]), int(row[4]))
        for row in db.execute(
            f'PRAGMA index_list("{expected_table}")'
        ).fetchall()
    }
    columns = tuple(
        str(row[2])
        for row in db.execute(f'PRAGMA index_info("{name}")').fetchall()
    )
    if (
        str(master[0]) != "index"
        or str(master[1]) != expected_table
        or columns != expected_columns
        or listed.get(name) != (expected_unique, expected_partial)
        or _normalized_definition_sql(master[2])
        != _normalized_definition_sql(
            CREATE_INDEX_SQL[
                0 if name == BACKGROUND_JOB_IDENTITY_INDEX else 1
            ]
        )
    ):
        return f"版本 39 的索引 {name} 定义不兼容"
    return None


def _trigger_schema_issue(
    db: sqlite3.Connection,
    name: str,
    *,
    missing_is_issue: bool = True,
) -> str | None:
    expected_table, expected_sql = EXPECTED_TRIGGERS[name]
    master = db.execute(
        "SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchone()
    if master is None:
        return f"版本 39 缺少触发器 {name}" if missing_is_issue else None
    if (
        str(master[0]) != "trigger"
        or str(master[1]) != expected_table
        or _normalized_definition_sql(master[2])
        != _normalized_definition_sql(expected_sql)
    ):
        return f"版本 39 的触发器 {name} 定义不兼容"
    return None


def _row_hash_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    binding_columns = (*BINDING_HASH_FIELDS, "binding_hash")
    for row in db.execute(
        f"SELECT {','.join(binding_columns)} FROM {BINDINGS_TABLE} "
        "ORDER BY job_id"
    ).fetchall():
        values = dict(zip(binding_columns, tuple(row)))
        expected = _canonical_hash(
            {field: values[field] for field in BINDING_HASH_FIELDS}
        )
        if not hmac.compare_digest(str(values["binding_hash"]), expected):
            issues.append(f"版本 39 的表 {BINDINGS_TABLE} 存在哈希不一致")
            break
    receipt_columns = (*RECEIPT_HASH_FIELDS, "receipt_hash")
    for row in db.execute(
        "SELECT "
        + ",".join(f"r.{column}" for column in receipt_columns)
        + ",b.resolution_hash "
        f"FROM {RECEIPTS_TABLE} r JOIN {BINDINGS_TABLE} b ON b.job_id=r.job_id "
        "ORDER BY r.job_id"
    ).fetchall():
        values = dict(
            zip((*receipt_columns, "binding_resolution_hash"), tuple(row))
        )
        expected = _canonical_hash(
            {field: values[field] for field in RECEIPT_HASH_FIELDS}
        )
        if (
            not hmac.compare_digest(str(values["receipt_hash"]), expected)
            or str(values["resolution_hash"])
            != str(values["binding_resolution_hash"])
        ):
            issues.append(f"版本 39 的表 {RECEIPTS_TABLE} 存在哈希不一致")
            break
    return issues


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
    if not issues:
        issues.extend(_row_hash_issues(db))
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


def migrate_v38_to_v39(db: sqlite3.Connection) -> None:
    conflicts = _existing_object_conflicts(db)
    if conflicts:
        raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


__all__ = [
    "BACKGROUND_JOB_IDENTITY_INDEX",
    "BINDINGS_TABLE",
    "BINDINGS_TRIGGER",
    "CREATE_BINDINGS_SQL",
    "CREATE_INDEX_SQL",
    "CREATE_RECEIPTS_SQL",
    "CREATE_TRIGGER_SQL",
    "JOB_INPUT_IDENTITY_INDEX",
    "RECEIPTS_TABLE",
    "RECEIPTS_TRIGGER",
    "SCHEMA_STATEMENTS",
    "migrate_v38_to_v39",
    "schema_issues",
]
