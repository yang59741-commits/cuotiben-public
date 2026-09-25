"""Schema 38: bind every immutable job input to its canonical payload hash."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3

from .v37_job_input_immutability import CREATE_TRIGGER_SQL, TRIGGER_NAME


TABLE_NAME = "job_inputs"
LEGACY_TABLE_NAME = "job_inputs_v37"
LEGACY_IDENTITY_VERSION = "legacy-job-input-v37"
CURRENT_IDENTITY_VERSIONS = (
    LEGACY_IDENTITY_VERSION,
    "job-input-v1",
    "exam-generation-input-v1",
)

CREATE_TABLE_SQL = f"""CREATE TABLE IF NOT EXISTS {TABLE_NAME}(
    job_id TEXT NOT NULL PRIMARY KEY,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
        CHECK(
            length(payload_hash) = 64
            AND payload_hash NOT GLOB '*[^0-9a-f]*'
        ),
    identity_version TEXT NOT NULL
        CHECK(identity_version IN (
            'legacy-job-input-v37',
            'job-input-v1',
            'exam-generation-input-v1'
        )),
    created_at TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
)"""


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _canonical_payload(value: object) -> str:
    parsed = json.loads(
        str(value),
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )
    if not isinstance(parsed, dict):
        raise ValueError("job input must be a JSON object")
    return json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _normalized_sql(value: object) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).upper()
    return (
        normalized.replace("IFNOTEXISTS", "")
        .replace('"JOB_INPUTS"', "JOB_INPUTS")
    )


def schema_issues(db: sqlite3.Connection) -> list[str]:
    master = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (TABLE_NAME,),
    ).fetchone()
    if master is None or str(master[0]) != "table":
        return [f"版本 38 缺少表 {TABLE_NAME}"]
    issues: list[str] = []
    if _normalized_sql(master[1]) != _normalized_sql(CREATE_TABLE_SQL):
        issues.append(f"版本 38 的表 {TABLE_NAME} 定义不兼容")

    expected_columns = {
        "job_id": ("TEXT", 1, None, 1),
        "payload_json": ("TEXT", 1, None, 0),
        "payload_hash": ("TEXT", 1, None, 0),
        "identity_version": ("TEXT", 1, None, 0),
        "created_at": ("TEXT", 1, None, 0),
    }
    actual_columns = {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
            int(row[5]),
        )
        for row in db.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()
    }
    if actual_columns != expected_columns:
        issues.append(f"版本 38 的表 {TABLE_NAME} 字段集合不兼容")

    foreign_keys = {
        (str(row[2]), str(row[3]), str(row[4]), str(row[6]).upper())
        for row in db.execute(
            f'PRAGMA foreign_key_list("{TABLE_NAME}")'
        ).fetchall()
    }
    if foreign_keys != {("background_jobs", "job_id", "id", "CASCADE")}:
        issues.append(f"版本 38 的表 {TABLE_NAME} 外键不兼容")

    for row in db.execute(
        f"SELECT i.payload_json,i.payload_hash,i.identity_version,j.kind "
        f"FROM {TABLE_NAME} i JOIN background_jobs j ON j.id=i.job_id "
        "ORDER BY i.job_id"
    ).fetchall():
        try:
            canonical = _canonical_payload(row[0])
        except (TypeError, ValueError, json.JSONDecodeError):
            issues.append(f"版本 38 的表 {TABLE_NAME} 存在非法冻结输入")
            break
        actual_hash = str(row[1] or "")
        if canonical != str(row[0]) or not hmac.compare_digest(
            _digest(canonical), actual_hash
        ):
            issues.append(f"版本 38 的表 {TABLE_NAME} 存在哈希不一致")
            break
        identity_version = str(row[2] or "")
        job_kind = str(row[3] or "")
        if identity_version not in CURRENT_IDENTITY_VERSIONS or (
            identity_version == "exam-generation-input-v1"
            and job_kind != "exam-generation"
        ) or (
            identity_version == "job-input-v1"
            and job_kind == "exam-generation"
        ):
            issues.append(f"版本 38 的表 {TABLE_NAME} 存在身份分类不一致")
            break
    return issues


def migrate_v37_to_v38(db: sqlite3.Connection) -> None:
    collision = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?",
        (LEGACY_TABLE_NAME,),
    ).fetchone()
    if collision is not None:
        raise RuntimeError("job input migration staging table already exists")

    rows = db.execute(
        f"SELECT job_id,payload_json,created_at FROM {TABLE_NAME} ORDER BY job_id"
    ).fetchall()
    canonical_rows: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        try:
            payload_json = _canonical_payload(row[1])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("legacy job input is not a strict JSON object") from exc
        canonical_rows.append(
            (
                str(row[0]),
                payload_json,
                _digest(payload_json),
                LEGACY_IDENTITY_VERSION,
                str(row[2]),
            )
        )

    db.execute(f'DROP TRIGGER IF EXISTS "{TRIGGER_NAME}"')
    db.execute(f'ALTER TABLE "{TABLE_NAME}" RENAME TO "{LEGACY_TABLE_NAME}"')
    db.execute(CREATE_TABLE_SQL)
    for row in canonical_rows:
        db.execute(
            f"INSERT INTO {TABLE_NAME}("
            "job_id,payload_json,payload_hash,identity_version,created_at) "
            "VALUES(?,?,?,?,?)",
            row,
        )
    db.execute(f'DROP TABLE "{LEGACY_TABLE_NAME}"')
    db.execute(CREATE_TRIGGER_SQL)


__all__ = [
    "CREATE_TABLE_SQL",
    "LEGACY_TABLE_NAME",
    "TABLE_NAME",
    "migrate_v37_to_v38",
    "schema_issues",
]
