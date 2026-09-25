"""Task-local D2 render-probe receipts and content rejections.

The ledger excludes a content identity only from the job that produced the
supporting probe.  It deliberately does not mutate candidate/source records.
Transactions belong to the caller: none of the functions in this module
commit or roll back the supplied connection.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Mapping

from .exam_generation_d2_render_probe import D2_RENDER_PROBE_VERSION


PROBE_RECEIPTS = "exam_generation_d2_content_probe_receipts_v1"
CONTENT_REJECTIONS = "exam_generation_d2_content_rejections_v1"
PROBE_RECEIPT_VERSION = D2_RENDER_PROBE_VERSION
CONTENT_REJECTION_VERSION = "d2-task-local-content-rejection-v1"

_HASH = re.compile(r"^[0-9a-f]{64}$")
_STABLE_CONTENT_FAILURE_CODES = frozenset(
    {
        "TEX_UNDEFINED_COMMAND",
        "TEX_ENVIRONMENT_MISMATCH",
        "TEX_BRACE_MISMATCH",
        "TEX_COMPILE_FAILED",
        "TEX_UNSAFE_BODY",
    }
)


class D2ContentRejectionError(RuntimeError):
    """A probe or task-local rejection cannot be persisted safely."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _hc(field: str) -> str:
    return f"CHECK(length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*')"


TABLE_SQL: Mapping[str, str] = {
    PROBE_RECEIPTS: f"""CREATE TABLE {PROBE_RECEIPTS}(
        probe_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        selection_hash TEXT NOT NULL {_hc('selection_hash')},
        candidate_id TEXT NOT NULL,
        content_hash TEXT NOT NULL {_hc('content_hash')},
        attempt INTEGER NOT NULL CHECK(attempt>0),
        classification TEXT NOT NULL CHECK(classification IN ('CONTENT','TEMPLATE_OR_ENVIRONMENT','UNKNOWN')),
        receipt_version TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        created_at TEXT NOT NULL,
        UNIQUE(job_id,selection_hash,candidate_id,content_hash,attempt)
    )""",
    CONTENT_REJECTIONS: f"""CREATE TABLE {CONTENT_REJECTIONS}(
        rejection_id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        content_hash TEXT NOT NULL {_hc('content_hash')},
        classification TEXT NOT NULL CHECK(classification='CONTENT'),
        probe_id INTEGER NOT NULL UNIQUE,
        entry_version TEXT NOT NULL CHECK(entry_version='{CONTENT_REJECTION_VERSION}'),
        created_at TEXT NOT NULL,
        UNIQUE(job_id,content_hash),
        FOREIGN KEY(probe_id) REFERENCES {PROBE_RECEIPTS}(probe_id)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
}


def _trigger_name(table: str, action: str) -> str:
    return f"{table}_{action.casefold()}_forbidden"


def _trigger_sql(table: str, action: str) -> str:
    return (
        f"CREATE TRIGGER {_trigger_name(table, action)} BEFORE {action} ON {table} "
        f"BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"
    )


def d2_content_rejection_schema_statements() -> tuple[str, ...]:
    statements = list(TABLE_SQL.values())
    for table in TABLE_SQL:
        statements.extend(
            (_trigger_sql(table, "UPDATE"), _trigger_sql(table, "DELETE"))
        )
    return tuple(statements)


def install_d2_content_rejection_schema(db: Any) -> None:
    for statement in d2_content_rejection_schema_statements():
        db.execute(statement)


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def d2_content_rejection_schema_issues(db: Any) -> list[str]:
    """Inspect only the newly introduced schema objects."""

    issues: list[str] = []
    for table, expected_sql in TABLE_SQL.items():
        row = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if row is None:
            issues.append(f"missing_table:{table}")
            continue
        actual_sql = row[0] if not isinstance(row, sqlite3.Row) else row["sql"]
        if _normalized(actual_sql) != _normalized(expected_sql):
            issues.append(f"table_sql_drift:{table}")
        for action in ("UPDATE", "DELETE"):
            name = _trigger_name(table, action)
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (name,),
            ).fetchone()
            if trigger is None:
                issues.append(f"missing_trigger:{name}")
                continue
            actual = trigger[0] if not isinstance(trigger, sqlite3.Row) else trigger["sql"]
            if _normalized(actual) != _normalized(_trigger_sql(table, action)):
                issues.append(f"trigger_sql_drift:{name}")
    return issues


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise D2ContentRejectionError("receipt_json_invalid") from exc


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise D2ContentRejectionError(f"{field}_invalid")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise D2ContentRejectionError(f"{field}_invalid")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_caller_transaction(db: Any) -> None:
    if getattr(db, "in_transaction", False) is not True:
        raise D2ContentRejectionError("caller_transaction_required")


def append_probe(
    db: Any,
    *,
    job_id: str,
    selection_hash: str,
    candidate_id: str,
    content_hash: str,
    attempt: int,
    receipt: Mapping[str, Any],
    created_at: str | None = None,
) -> int:
    """Append one canonical probe receipt, idempotently by probe identity."""

    _require_caller_transaction(db)
    job_id = _text(job_id, "job_id")
    selection_hash = _hash(selection_hash, "selection_hash")
    candidate_id = _text(candidate_id, "candidate_id")
    content_hash = _hash(content_hash, "content_hash")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise D2ContentRejectionError("attempt_invalid")
    if not isinstance(receipt, Mapping):
        raise D2ContentRejectionError("receipt_invalid")

    payload = dict(receipt)
    classification = payload.get("classification")
    if classification not in {"CONTENT", "TEMPLATE_OR_ENVIRONMENT", "UNKNOWN"}:
        raise D2ContentRejectionError("classification_invalid")
    for field, expected in (
        ("selection_hash", selection_hash),
        ("candidate_id", candidate_id),
        ("content_hash", content_hash),
    ):
        if payload.get(field) != expected:
            raise D2ContentRejectionError(f"receipt_{field}_mismatch")
    if payload.get("attempt", attempt) != attempt:
        raise D2ContentRejectionError("receipt_attempt_mismatch")
    version = _text(payload.get("version"), "receipt_version")
    if version != PROBE_RECEIPT_VERSION:
        raise D2ContentRejectionError("receipt_version_unsupported")
    payload_json = _canonical(payload)
    timestamp = _text(created_at or _now(), "created_at")

    existing = db.execute(
        f"SELECT probe_id,classification,receipt_version,payload_json FROM {PROBE_RECEIPTS} "
        "WHERE job_id=? AND selection_hash=? AND candidate_id=? AND content_hash=? AND attempt=?",
        (job_id, selection_hash, candidate_id, content_hash, attempt),
    ).fetchone()
    if existing is not None:
        values = tuple(existing)
        try:
            stored_payload = json.loads(values[3])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise D2ContentRejectionError("stored_probe_payload_invalid") from exc
        if (
            values[1] != classification
            or values[2] != version
            or stored_payload != payload
        ):
            raise D2ContentRejectionError("probe_identity_conflict")
        return int(values[0])

    cursor = db.execute(
        f"INSERT INTO {PROBE_RECEIPTS}(job_id,selection_hash,candidate_id,content_hash,attempt,classification,receipt_version,payload_json,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            job_id,
            selection_hash,
            candidate_id,
            content_hash,
            attempt,
            classification,
            version,
            payload_json,
            timestamp,
        ),
    )
    return int(cursor.lastrowid)


def _supports_content_rejection(receipt: Mapping[str, Any]) -> bool:
    if receipt.get("classification") != "CONTENT":
        return False
    if receipt.get("disposition") != "reject_task_local_content":
        return False
    if receipt.get("canonical_template") is not True:
        return False
    if receipt.get("control_succeeded") is not True:
        return False
    control = receipt.get("control")
    if not isinstance(control, Mapping) or control.get("outcome") != "succeeded":
        return False
    environment = receipt.get("environment")
    if not isinstance(environment, Mapping) or environment.get("reason_codes") not in ([], ()):
        return False
    try:
        _hash(receipt.get("template_hash"), "template_hash")
        _hash(receipt.get("rendering_spec_hash"), "rendering_spec_hash")
        content_hash = _hash(receipt.get("content_hash"), "content_hash")
        if _hash(receipt.get("content_snapshot_hash"), "content_snapshot_hash") != content_hash:
            return False
    except D2ContentRejectionError:
        return False
    repeat_requirement = receipt.get("repeat_requirement")
    reproduced = receipt.get("reproduced_failure_count")
    if (
        isinstance(repeat_requirement, bool)
        or not isinstance(repeat_requirement, int)
        or repeat_requirement < 2
        or isinstance(reproduced, bool)
        or not isinstance(reproduced, int)
        or reproduced < 2
        or reproduced < repeat_requirement
    ):
        return False
    attempts = receipt.get("candidate_attempts")
    if not isinstance(attempts, list) or len(attempts) != reproduced:
        return False
    failed_attempts: set[int] = set()
    failure_codes: set[str] = set()
    for item in attempts:
        if not isinstance(item, Mapping) or item.get("outcome") != "failed":
            return False
        if item.get("reason_code") not in _STABLE_CONTENT_FAILURE_CODES:
            return False
        attempt = item.get("attempt")
        if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
            failed_attempts.add(attempt)
            failure_codes.add(item["reason_code"])
    return len(failure_codes) == 1 and len(failed_attempts) >= repeat_requirement and len(failed_attempts) == reproduced


def reject_content(
    db: Any,
    *,
    probe_id: int,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a task-local CONTENT entry backed by a qualifying real probe."""

    _require_caller_transaction(db)
    if isinstance(probe_id, bool) or not isinstance(probe_id, int) or probe_id < 1:
        raise D2ContentRejectionError("probe_id_invalid")
    row = db.execute(
        f"SELECT probe_id,job_id,selection_hash,candidate_id,content_hash,classification,receipt_version,payload_json "
        f"FROM {PROBE_RECEIPTS} WHERE probe_id=?",
        (probe_id,),
    ).fetchone()
    if row is None:
        raise D2ContentRejectionError("probe_missing")
    values = tuple(row)
    try:
        receipt = json.loads(values[7])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise D2ContentRejectionError("probe_payload_invalid") from exc
    if (
        values[5] != "CONTENT"
        or values[6] != PROBE_RECEIPT_VERSION
        or not isinstance(receipt, Mapping)
        or receipt.get("version") != values[6]
    ):
        raise D2ContentRejectionError("probe_not_content")
    if (
        receipt.get("selection_hash") != values[2]
        or receipt.get("candidate_id") != values[3]
        or receipt.get("content_hash") != values[4]
        or receipt.get("classification") != values[5]
    ):
        raise D2ContentRejectionError("probe_column_binding_drift")
    if not _supports_content_rejection(receipt):
        raise D2ContentRejectionError("probe_not_reproducible_content")

    existing = db.execute(
        f"SELECT rejection_id,job_id,content_hash,classification,probe_id,entry_version,created_at "
        f"FROM {CONTENT_REJECTIONS} WHERE job_id=? AND content_hash=?",
        (values[1], values[4]),
    ).fetchone()
    if existing is not None:
        entry = dict(zip(
            ("rejection_id", "job_id", "content_hash", "classification", "probe_id", "entry_version", "created_at"),
            tuple(existing),
        ))
        if entry["probe_id"] != probe_id:
            raise D2ContentRejectionError("content_rejection_conflict")
        return entry

    timestamp = _text(created_at or _now(), "created_at")
    cursor = db.execute(
        f"INSERT INTO {CONTENT_REJECTIONS}(job_id,content_hash,classification,probe_id,entry_version,created_at) "
        "VALUES(?,?,'CONTENT',?,?,?)",
        (values[1], values[4], probe_id, CONTENT_REJECTION_VERSION, timestamp),
    )
    return {
        "rejection_id": int(cursor.lastrowid),
        "job_id": values[1],
        "content_hash": values[4],
        "classification": "CONTENT",
        "probe_id": probe_id,
        "entry_version": CONTENT_REJECTION_VERSION,
        "created_at": timestamp,
    }


def rejected_content_hashes(db: Any, job_id: str) -> set[str]:
    """Return only the task-local rejected content identities for ``job_id``."""

    job_id = _text(job_id, "job_id")
    return {
        str(row[0])
        for row in db.execute(
            f"SELECT content_hash FROM {CONTENT_REJECTIONS} WHERE job_id=? ORDER BY content_hash",
            (job_id,),
        )
    }


__all__ = [
    "CONTENT_REJECTIONS",
    "CONTENT_REJECTION_VERSION",
    "D2ContentRejectionError",
    "PROBE_RECEIPTS",
    "PROBE_RECEIPT_VERSION",
    "TABLE_SQL",
    "append_probe",
    "d2_content_rejection_schema_issues",
    "d2_content_rejection_schema_statements",
    "install_d2_content_rejection_schema",
    "reject_content",
    "rejected_content_hashes",
]
