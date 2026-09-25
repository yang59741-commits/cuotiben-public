"""Schema 40: persist the immutable past-v3 exact-slot contract receipt."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter, defaultdict


TABLE = "exam_generation_stage3_contract_receipts"
TRIGGER = "exam_generation_stage3_contract_receipts_no_update"
STAGE_NAME = "exact_slot_contract_ready"
RECEIPT_CONTRACT_VERSION = "past-v3-stage3-contract-receipt-v1"
STAGE3_CONTRACT_VERSION = "past-v3-generation-contract-v1"


def _hash_check(column: str) -> str:
    return f"CHECK(length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
    job_id TEXT NOT NULL PRIMARY KEY,
    stage_name TEXT NOT NULL CHECK(stage_name = '{STAGE_NAME}'),
    receipt_contract_version TEXT NOT NULL
        CHECK(receipt_contract_version = '{RECEIPT_CONTRACT_VERSION}'),
    stage3_contract_version TEXT NOT NULL
        CHECK(stage3_contract_version = '{STAGE3_CONTRACT_VERSION}'),
    binding_hash TEXT NOT NULL {_hash_check('binding_hash')},
    generation_request_hash TEXT NOT NULL {_hash_check('generation_request_hash')},
    resolution_hash TEXT NOT NULL {_hash_check('resolution_hash')},
    exact_slot_hash TEXT NOT NULL {_hash_check('exact_slot_hash')},
    question_count INTEGER NOT NULL CHECK(question_count > 0),
    scoring_mode TEXT NOT NULL CHECK(scoring_mode IN ('verified','unscored')),
    slot_demands_hash TEXT NOT NULL {_hash_check('slot_demands_hash')},
    points_projection_hash TEXT NOT NULL {_hash_check('points_projection_hash')},
    source_quotas_hash TEXT NOT NULL {_hash_check('source_quotas_hash')},
    stage3_contract_hash TEXT NOT NULL {_hash_check('stage3_contract_hash')},
    receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
    created_at TEXT NOT NULL,
    UNIQUE(job_id, receipt_hash),
    FOREIGN KEY(job_id, binding_hash)
        REFERENCES exam_generation_attempt_bindings(job_id, binding_hash)
        ON UPDATE RESTRICT ON DELETE CASCADE
)"""

CREATE_TRIGGER_SQL = f"""CREATE TRIGGER IF NOT EXISTS {TRIGGER}
    BEFORE UPDATE ON {TABLE}
    BEGIN
        SELECT RAISE(ABORT, 'exam_generation_stage3_contract_receipt_immutable');
    END"""

SCHEMA_STATEMENTS = (CREATE_TABLE_SQL, CREATE_TRIGGER_SQL)

EXPECTED_COLUMNS = {
    "job_id": ("TEXT", 1, None, 1),
    "stage_name": ("TEXT", 1, None, 0),
    "receipt_contract_version": ("TEXT", 1, None, 0),
    "stage3_contract_version": ("TEXT", 1, None, 0),
    "binding_hash": ("TEXT", 1, None, 0),
    "generation_request_hash": ("TEXT", 1, None, 0),
    "resolution_hash": ("TEXT", 1, None, 0),
    "exact_slot_hash": ("TEXT", 1, None, 0),
    "question_count": ("INTEGER", 1, None, 0),
    "scoring_mode": ("TEXT", 1, None, 0),
    "slot_demands_hash": ("TEXT", 1, None, 0),
    "points_projection_hash": ("TEXT", 1, None, 0),
    "source_quotas_hash": ("TEXT", 1, None, 0),
    "stage3_contract_hash": ("TEXT", 1, None, 0),
    "receipt_hash": ("TEXT", 1, None, 0),
    "created_at": ("TEXT", 1, None, 0),
}

RECEIPT_HASH_FIELDS = (
    "receipt_contract_version", "stage_name", "job_id",
    "stage3_contract_version", "binding_hash", "generation_request_hash",
    "resolution_hash", "exact_slot_hash", "question_count", "scoring_mode",
    "slot_demands_hash", "points_projection_hash", "source_quotas_hash",
    "stage3_contract_hash",
)


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def _canonical_hash(value: dict[str, object]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _foreign_keys(db: sqlite3.Connection) -> set[tuple]:
    grouped: dict[int, list[tuple]] = defaultdict(list)
    for row in db.execute(f'PRAGMA foreign_key_list("{TABLE}")'):
        grouped[int(row[0])].append(tuple(row))
    result = set()
    for rows in grouped.values():
        rows.sort(key=lambda item: int(item[1]))
        result.add((str(rows[0][2]), tuple(str(r[3]) for r in rows), tuple(str(r[4]) for r in rows), str(rows[0][5]), str(rows[0][6]), str(rows[0][7])))
    return result


def schema_issues(db: sqlite3.Connection) -> list[str]:
    master = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    if master is None:
        return [f"版本 40 缺少表 {TABLE}"]
    issues: list[str] = []
    if str(master[0]) != "table" or _normalized(master[1]) != _normalized(CREATE_TABLE_SQL):
        issues.append(f"版本 40 的表 {TABLE} 定义不兼容")
    columns = {str(r[1]): (str(r[2]).upper(), int(r[3]), None if r[4] is None else str(r[4]), int(r[5])) for r in db.execute(f'PRAGMA table_info("{TABLE}")')}
    if columns != EXPECTED_COLUMNS:
        issues.append(f"版本 40 的表 {TABLE} 字段集合不兼容")
    unique_sets = Counter(tuple(str(i[2]) for i in db.execute(f'PRAGMA index_info("{r[1]}")')) for r in db.execute(f'PRAGMA index_list("{TABLE}")') if int(r[2]) == 1)
    if unique_sets != Counter({("job_id",), ("job_id", "receipt_hash")}):
        issues.append(f"版本 40 的表 {TABLE} 唯一约束集不兼容")
    user_indexes = {
        str(row[1])
        for row in db.execute(f'PRAGMA index_list("{TABLE}")')
        if str(row[3]) == "c"
    }
    if user_indexes:
        issues.append(f"版本 40 的表 {TABLE} 存在未登记索引")
    if _foreign_keys(db) != {("exam_generation_attempt_bindings", ("job_id", "binding_hash"), ("job_id", "binding_hash"), "RESTRICT", "CASCADE", "NONE")}:
        issues.append(f"版本 40 的表 {TABLE} 外键定义不兼容")
    triggers = db.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,)).fetchall()
    if len(triggers) != 1 or str(triggers[0][0]) != TRIGGER or _normalized(triggers[0][1]) != _normalized(CREATE_TRIGGER_SQL):
        issues.append(f"版本 40 的表 {TABLE} 触发器集合不兼容")
    if not issues:
        columns = (*RECEIPT_HASH_FIELDS, "receipt_hash")
        parent_fields = ("generation_request_hash", "resolution_hash", "exact_slot_hash")
        selected = ",".join(f"r.{column}" for column in columns) + "," + ",".join(f"b.{column}" for column in parent_fields)
        for row in db.execute(f"SELECT {selected} FROM {TABLE} r LEFT JOIN exam_generation_attempt_bindings b ON b.job_id=r.job_id AND b.binding_hash=r.binding_hash ORDER BY r.job_id"):
            values = dict(zip((*columns, *(f"parent_{field}" for field in parent_fields)), tuple(row)))
            expected = _canonical_hash({field: values[field] for field in RECEIPT_HASH_FIELDS})
            if (
                not hmac.compare_digest(str(values["receipt_hash"]), expected)
                or any(values[field] != values[f"parent_{field}"] for field in parent_fields)
            ):
                issues.append(f"版本 40 的表 {TABLE} 存在哈希不一致")
                break
    return issues


def _existing_conflicts(db: sqlite3.Connection) -> list[str]:
    table = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE,)).fetchone()
    trigger = db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (TRIGGER,)).fetchone()
    issues = []
    if table is not None and (str(table[0]) != "table" or _normalized(table[1]) != _normalized(CREATE_TABLE_SQL)):
        issues.append(f"版本 40 的对象 {TABLE} 类型或定义不兼容")
    if trigger is not None and (str(trigger[0]) != "trigger" or str(trigger[1]) != TABLE or _normalized(trigger[2]) != _normalized(CREATE_TRIGGER_SQL)):
        issues.append(f"版本 40 的触发器 {TRIGGER} 定义不兼容")
    return issues


def migrate_v39_to_v40(db: sqlite3.Connection) -> None:
    conflicts = _existing_conflicts(db)
    if conflicts:
        raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


__all__ = ["CREATE_TABLE_SQL", "CREATE_TRIGGER_SQL", "RECEIPT_CONTRACT_VERSION", "SCHEMA_STATEMENTS", "STAGE3_CONTRACT_VERSION", "STAGE_NAME", "TABLE", "TRIGGER", "migrate_v39_to_v40", "schema_issues"]
