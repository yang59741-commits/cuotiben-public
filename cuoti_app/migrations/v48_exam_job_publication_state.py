"""Schema 48: make committed native publication a background-job state."""

from __future__ import annotations

import re
import sqlite3


TABLE = "background_jobs"
REBUILD_TABLE = "background_jobs_v48"

V47_STATUSES = (
    "queued", "running", "cancel_requested", "cancelled", "succeeded", "failed",
)
V48_STATUSES = (
    "queued", "running", "checkpointed", "published_pending_receipt",
    "cancel_requested", "cancelled", "succeeded", "failed",
)

COLUMNS = (
    "id", "kind", "status", "idempotency_key", "input_hash", "attempts",
    "max_attempts", "progress_current", "progress_total", "progress_message",
    "result_json", "error_code", "error_message", "error_details_json",
    "cancel_requested", "lease_owner", "lease_expires_at", "heartbeat_at",
    "available_at", "created_at", "updated_at", "started_at", "finished_at",
    "lane", "priority", "attempts_in_cycle", "manual_retry_count",
    "error_category", "error_retryable", "error_correlation_id", "deadline_at",
    "deadline_policy_mode", "deadline_policy_version", "exam_timing_json",
)


def _create_table_sql(name: str, statuses: tuple[str, ...]) -> str:
    status_values = ",".join(f"'{value}'" for value in statuses)
    return f"""CREATE TABLE {name}(
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ({status_values})),
        idempotency_key TEXT,
        input_hash TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        max_attempts INTEGER NOT NULL DEFAULT 3 CHECK(max_attempts >= 1),
        progress_current INTEGER NOT NULL DEFAULT 0 CHECK(progress_current >= 0),
        progress_total INTEGER CHECK(progress_total IS NULL OR progress_total >= 0),
        progress_message TEXT NOT NULL DEFAULT '',
        result_json TEXT,
        error_code TEXT,
        error_message TEXT,
        error_details_json TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
        lease_owner TEXT,
        lease_expires_at TEXT,
        heartbeat_at TEXT,
        available_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        lane TEXT NOT NULL DEFAULT 'heavy' CHECK(lane IN ('fast','heavy')),
        priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100),
        attempts_in_cycle INTEGER NOT NULL DEFAULT 0 CHECK(attempts_in_cycle >= 0),
        manual_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(manual_retry_count >= 0),
        error_category TEXT,
        error_retryable INTEGER CHECK(error_retryable IS NULL OR error_retryable IN (0,1)),
        error_correlation_id TEXT,
        deadline_at TEXT,
        deadline_policy_mode TEXT CHECK(deadline_policy_mode IS NULL OR deadline_policy_mode IN ('observe','enforce')),
        deadline_policy_version TEXT,
        exam_timing_json TEXT NOT NULL DEFAULT '{{}}',
        UNIQUE(kind,idempotency_key)
    )"""


CREATE_V47_SQL = _create_table_sql(TABLE, V47_STATUSES)
CREATE_V48_SQL = _create_table_sql(TABLE, V48_STATUSES)
CREATE_REBUILD_SQL = _create_table_sql(REBUILD_TABLE, V48_STATUSES)
# Databases bootstrapped with the checked background_jobs definition before
# schema 28 received error_details_json through the published ALTER TABLE
# migration.  They therefore retain every strict v6 CHECK while placing that
# one column after error_correlation_id.  This is a third exact, historical
# source shape; it must not be conflated with the unchecked forward shape.
CREATE_V47_CHECKED_FORWARD_SQL = CREATE_V47_SQL.replace(
    "        error_details_json TEXT,\n", ""
).replace(
    "        error_correlation_id TEXT,\n",
    "        error_correlation_id TEXT,\n        error_details_json TEXT,\n",
)
# Published v6 databases acquired later columns through ALTER TABLE.  This is
# the other exact v47 source shape supported by the forward-only migration
# chain; notably, error_details_json is appended rather than reordered.
CREATE_V47_FORWARD_SQL = """CREATE TABLE background_jobs(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    idempotency_key TEXT,
    input_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER,
    progress_message TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    error_code TEXT,
    error_message TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    available_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    lane TEXT NOT NULL DEFAULT 'heavy' CHECK(lane IN ('fast','heavy')),
    priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100),
    attempts_in_cycle INTEGER NOT NULL DEFAULT 0 CHECK(attempts_in_cycle >= 0),
    manual_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(manual_retry_count >= 0),
    error_category TEXT,
    error_retryable INTEGER CHECK(error_retryable IS NULL OR error_retryable IN (0,1)),
    error_correlation_id TEXT,
    error_details_json TEXT,
    deadline_at TEXT,
    deadline_policy_mode TEXT CHECK(deadline_policy_mode IS NULL OR deadline_policy_mode IN ('observe','enforce')),
    deadline_policy_version TEXT,
    exam_timing_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(kind,idempotency_key)
)"""

INDEX_SQL = {
    "idx_background_jobs_claim":
        "CREATE INDEX idx_background_jobs_claim ON background_jobs(status,available_at,created_at)",
    "idx_background_jobs_kind_created":
        "CREATE INDEX idx_background_jobs_kind_created ON background_jobs(kind,created_at)",
    "idx_background_jobs_lane_claim":
        "CREATE INDEX idx_background_jobs_lane_claim ON background_jobs(lane,status,available_at,priority,created_at,id)",
    "idx_background_jobs_lease":
        "CREATE INDEX idx_background_jobs_lease ON background_jobs(status,lease_expires_at)",
    "ux_background_jobs_id_input_hash":
        "CREATE UNIQUE INDEX ux_background_jobs_id_input_hash ON background_jobs(id,input_hash)",
}


def _norm(value: object) -> str:
    return (
        re.sub(r"\s+", "", str(value or ""))
        .upper()
        .replace("IFNOTEXISTS", "")
        .replace('"', "")
    )


def _master_sql(db: sqlite3.Connection, object_type: str, name: str) -> str | None:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?", (object_type, name)
    ).fetchone()
    return None if row is None else str(row[0])


def _user_indexes(db: sqlite3.Connection) -> dict[str, str]:
    return {
        str(name): str(sql)
        for name, sql in db.execute(
            "SELECT name,sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (TABLE,),
        )
    }


def _validate_v47_source(db: sqlite3.Connection) -> None:
    actual_sql = _norm(_master_sql(db, "table", TABLE))
    if actual_sql not in {
        _norm(CREATE_V47_SQL),
        _norm(CREATE_V47_CHECKED_FORWARD_SQL),
        _norm(CREATE_V47_FORWARD_SQL),
    }:
        raise RuntimeError("v48 background_jobs source definition is incompatible with v47")
    indexes = _user_indexes(db)
    # The v6 startup path repairs the three baseline indexes after forward
    # migrations.  The v9 lane and v39 composite-parent indexes must already
    # exist; every present index must still match its published definition.
    if not {"idx_background_jobs_lane_claim", "ux_background_jobs_id_input_hash"} <= set(indexes):
        raise RuntimeError("v48 background_jobs source index set is incomplete")
    if set(indexes) - set(INDEX_SQL) or any(
        _norm(indexes.get(name)) != _norm(sql) for name, sql in INDEX_SQL.items()
        if name in indexes
    ):
        raise RuntimeError("v48 background_jobs source index set is incompatible with v47")
    triggers = db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (TABLE,)
    ).fetchall()
    if triggers:
        raise RuntimeError("v48 background_jobs source trigger set is incompatible with v47")


def _committed_head_condition(alias: str) -> str:
    return (
        "EXISTS("
        "SELECT 1 FROM exam_generation_v3_publish_heads h "
        f"WHERE h.job_id={alias}.id "
        "AND ((h.state='published_pending_receipt' "
        f"AND {alias}.status!='published_pending_receipt') "
        "OR (h.state='completed' "
        f"AND {alias}.status NOT IN ('published_pending_receipt','succeeded'))))"
    )


def _copy_rows(db: sqlite3.Connection) -> None:
    committed = _committed_head_condition("j")
    selected = []
    clear_to_null = {
        "error_code", "error_message", "error_details_json", "lease_owner",
        "lease_expires_at", "heartbeat_at", "finished_at", "error_category",
        "error_retryable", "error_correlation_id",
    }
    for column in COLUMNS:
        if column == "status":
            selected.append(
                f"CASE WHEN {committed} THEN 'published_pending_receipt' ELSE j.status END"
            )
        elif column == "cancel_requested":
            selected.append(f"CASE WHEN {committed} THEN 0 ELSE j.cancel_requested END")
        elif column in clear_to_null:
            selected.append(f"CASE WHEN {committed} THEN NULL ELSE j.{column} END")
        else:
            selected.append(f"j.{column}")
    db.execute(
        f"INSERT INTO {REBUILD_TABLE}({','.join(COLUMNS)}) "
        f"SELECT {','.join(selected)} FROM {TABLE} j"
    )


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    if _norm(_master_sql(db, "table", TABLE)) != _norm(CREATE_V48_SQL):
        issues.append("版本 48 的 background_jobs 表定义不兼容")
        return issues
    columns = tuple(str(row[1]) for row in db.execute(f"PRAGMA table_info({TABLE})"))
    if columns != COLUMNS:
        issues.append("版本 48 的 background_jobs 列集合或顺序不兼容")
    indexes = _user_indexes(db)
    if set(indexes) != set(INDEX_SQL) or any(
        _norm(indexes.get(name)) != _norm(sql) for name, sql in INDEX_SQL.items()
    ):
        issues.append("版本 48 的 background_jobs 索引集合不兼容")
    conflict = db.execute(
        "SELECT 1 FROM background_jobs j "
        "JOIN exam_generation_v3_publish_heads h ON h.job_id=j.id "
        "WHERE (h.state='published_pending_receipt' "
        "AND j.status!='published_pending_receipt') "
        "OR (h.state='completed' "
        "AND j.status NOT IN ('published_pending_receipt','succeeded')) LIMIT 1"
    ).fetchone()
    if conflict is not None:
        issues.append("版本 48 存在已提交发布事实与任务主状态冲突")
    return issues


def migrate_v47_to_v48(db: sqlite3.Connection) -> None:
    if int(db.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise RuntimeError("v48 background_jobs rebuild requires foreign_keys=OFF")
    actual_sql = _norm(_master_sql(db, "table", TABLE))
    if actual_sql == _norm(CREATE_V48_SQL):
        # Tolerate a deliberately lowered user_version only when the physical
        # schema is already the exact v48 definition (used by migration drills).
        indexes = _user_indexes(db)
        if set(indexes) != set(INDEX_SQL) or any(
            _norm(indexes.get(name)) != _norm(sql) for name, sql in INDEX_SQL.items()
        ):
            raise RuntimeError("v48 background_jobs current index set is incompatible")
        committed = _committed_head_condition("background_jobs")
        db.execute(
            "UPDATE background_jobs SET status='published_pending_receipt',"
            "cancel_requested=0,error_code=NULL,error_message=NULL,error_details_json=NULL,"
            "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,finished_at=NULL,"
            "error_category=NULL,error_retryable=NULL,error_correlation_id=NULL "
            f"WHERE {committed}"
        )
        issues = schema_issues(db)
        if issues:
            raise RuntimeError("；".join(issues))
        return
    _validate_v47_source(db)
    before_ids = tuple(str(row[0]) for row in db.execute(
        f"SELECT id FROM {TABLE} ORDER BY id"
    ))
    before_count = len(before_ids)

    db.execute(CREATE_REBUILD_SQL)
    _copy_rows(db)
    db.execute(f"DROP TABLE {TABLE}")
    db.execute(f"ALTER TABLE {REBUILD_TABLE} RENAME TO {TABLE}")
    for statement in INDEX_SQL.values():
        db.execute(statement)

    after_ids = tuple(str(row[0]) for row in db.execute(
        f"SELECT id FROM {TABLE} ORDER BY id"
    ))
    after_count = int(db.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    if after_count != before_count or after_ids != before_ids:
        raise RuntimeError("v48 background_jobs rebuild lost rows or primary keys")
    issues = schema_issues(db)
    quick = str(db.execute("PRAGMA quick_check").fetchone()[0])
    foreign = db.execute("PRAGMA foreign_key_check").fetchall()
    if issues or quick != "ok" or foreign:
        details = "；".join(issues)
        raise RuntimeError(
            "v48 background_jobs post-migration validation failed"
            + (f"：{details}" if details else "")
        )


migrate_v47_to_v48.requires_foreign_keys_off = True

__all__ = [
    "COLUMNS", "CREATE_V47_CHECKED_FORWARD_SQL", "CREATE_V47_FORWARD_SQL",
    "CREATE_V47_SQL", "CREATE_V48_SQL", "INDEX_SQL", "TABLE",
    "V48_STATUSES", "migrate_v47_to_v48", "schema_issues",
]
