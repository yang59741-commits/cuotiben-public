"""Schema 50: approved mode-memory publication authority and durable outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from typing import Any, Callable


FACT_VERSION = "mode-memory-publication-fact-v1"
BATCH_VERSION = "mode-memory-publication-fact-batch-v1"

MEMORY_PUBLICATION_AUTHORITIES = (
    "exam_generation_mode_memory_publication_authorities_v1"
)
MEMORY_FACT_BATCHES = "exam_generation_mode_memory_fact_batches_v1"
MEMORY_FACT_ROWS = "exam_generation_mode_memory_fact_rows_v1"
MEMORY_OUTBOX = "exam_generation_mode_memory_outbox_v1"
MEMORY_DELIVERY_HEADS = "exam_generation_mode_memory_delivery_heads_v1"
MEMORY_DELIVERY_LEASES = "exam_generation_mode_memory_delivery_leases_v1"


def _hc(field: str) -> str:
    return f"CHECK(length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*')"


TABLE_SQL = {
    MEMORY_PUBLICATION_AUTHORITIES: f"""CREATE TABLE {MEMORY_PUBLICATION_AUTHORITIES}(
        authority_bridge_hash TEXT NOT NULL PRIMARY KEY {_hc('authority_bridge_hash')},
        job_id TEXT NOT NULL UNIQUE,exam_id INTEGER NOT NULL CHECK(exam_id>0),
        publication_hash TEXT NOT NULL UNIQUE {_hc('publication_hash')},
        commit_authority_hash TEXT NOT NULL {_hc('commit_authority_hash')},
        mode TEXT NOT NULL CHECK(mode IN ('free','mistakes','past')),
        namespace TEXT NOT NULL CHECK(namespace IN ('free','mistake','past')),
        memory_type TEXT NOT NULL CHECK(memory_type IN ('free','mistakes','past')),
        actor TEXT NOT NULL CHECK(actor IN ('free_exam','mistake_exam','past_exam')),
        production_activation_allowed INTEGER NOT NULL CHECK(production_activation_allowed=0),
        published_at TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
        UNIQUE(job_id,publication_hash,commit_authority_hash),
        CHECK((mode='free' AND namespace='free' AND memory_type='free' AND actor='free_exam')
           OR (mode='mistakes' AND namespace='mistake' AND memory_type='mistakes' AND actor='mistake_exam')
           OR (mode='past' AND namespace='past' AND memory_type='past' AND actor='past_exam'))
    )""",
    MEMORY_FACT_BATCHES: f"""CREATE TABLE {MEMORY_FACT_BATCHES}(
        job_id TEXT NOT NULL,
        fact_batch_hash TEXT NOT NULL {_hc('fact_batch_hash')},
        mode_memory_policy_hash TEXT NOT NULL {_hc('mode_memory_policy_hash')},
        memory_snapshot_hash TEXT NOT NULL {_hc('memory_snapshot_hash')},
        publication_hash TEXT NOT NULL {_hc('publication_hash')},
        commit_authority_hash TEXT NOT NULL {_hc('commit_authority_hash')},
        mode TEXT NOT NULL CHECK(mode IN ('free','mistakes','past')),
        namespace TEXT NOT NULL CHECK(namespace IN ('free','mistake','past')),
        actor TEXT NOT NULL CHECK(actor IN ('free_exam','mistake_exam','past_exam')),
        production_activation_allowed INTEGER NOT NULL CHECK(production_activation_allowed=0),
        fact_count INTEGER NOT NULL CHECK(fact_count>0),
        batch_version TEXT NOT NULL CHECK(batch_version='{BATCH_VERSION}'),
        payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,fact_batch_hash),UNIQUE(job_id),UNIQUE(fact_batch_hash),
        FOREIGN KEY(job_id,publication_hash,commit_authority_hash)
            REFERENCES {MEMORY_PUBLICATION_AUTHORITIES}(job_id,publication_hash,commit_authority_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    MEMORY_FACT_ROWS: f"""CREATE TABLE {MEMORY_FACT_ROWS}(
        fact_batch_hash TEXT NOT NULL {_hc('fact_batch_hash')},
        idempotency_key TEXT NOT NULL {_hc('idempotency_key')},
        fact_hash TEXT NOT NULL {_hc('fact_hash')},
        fact_type TEXT NOT NULL CHECK(fact_type IN (
            'free_publication_history_use_fact','mistake_publication_history_use_fact',
            'past_publication_history_use_fact','past_mistake_priority_consumed_fact'
        )),
        namespace TEXT NOT NULL CHECK(namespace IN ('free','mistake','past')),
        actor TEXT NOT NULL CHECK(actor IN ('free_exam','mistake_exam','past_exam')),
        position INTEGER NOT NULL CHECK(position>0),candidate_id TEXT NOT NULL,
        candidate_fact_hash TEXT NOT NULL {_hc('candidate_fact_hash')},
        content_hash TEXT NOT NULL {_hc('content_hash')},
        lineage_hash TEXT NOT NULL {_hc('lineage_hash')},m_once_identity_hash TEXT,
        fact_version TEXT NOT NULL CHECK(fact_version='{FACT_VERSION}'),
        payload_json TEXT NOT NULL,
        CHECK(m_once_identity_hash IS NULL OR (
            length(m_once_identity_hash)=64 AND m_once_identity_hash NOT GLOB '*[^0-9a-f]*'
        )),
        PRIMARY KEY(fact_batch_hash,idempotency_key),UNIQUE(idempotency_key),UNIQUE(fact_hash),
        FOREIGN KEY(fact_batch_hash) REFERENCES {MEMORY_FACT_BATCHES}(fact_batch_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    MEMORY_OUTBOX: f"""CREATE TABLE {MEMORY_OUTBOX}(
        outbox_id TEXT NOT NULL PRIMARY KEY {_hc('outbox_id')},
        fact_batch_hash TEXT NOT NULL UNIQUE {_hc('fact_batch_hash')},
        memory_type TEXT NOT NULL CHECK(memory_type IN ('free','mistakes','past')),
        actor TEXT NOT NULL CHECK(actor IN ('free_exam','mistake_exam','past_exam')),
        event_id TEXT NOT NULL UNIQUE,event_day TEXT NOT NULL CHECK(length(event_day)=10),
        production_activation_allowed INTEGER NOT NULL CHECK(production_activation_allowed=0),
        envelope_hash TEXT NOT NULL {_hc('envelope_hash')},
        payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
        FOREIGN KEY(fact_batch_hash) REFERENCES {MEMORY_FACT_BATCHES}(fact_batch_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    MEMORY_DELIVERY_HEADS: f"""CREATE TABLE {MEMORY_DELIVERY_HEADS}(
        outbox_id TEXT NOT NULL PRIMARY KEY,
        fact_batch_hash TEXT NOT NULL UNIQUE {_hc('fact_batch_hash')},
        state TEXT NOT NULL CHECK(state IN ('pending','delivered')),
        revision INTEGER NOT NULL CHECK(revision>0),
        delivered_event_hash TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        CHECK((state='pending' AND revision=1 AND delivered_event_hash IS NULL)
           OR (state='delivered' AND revision=2 AND length(delivered_event_hash)=64
               AND delivered_event_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(outbox_id) REFERENCES {MEMORY_OUTBOX}(outbox_id)
            ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(fact_batch_hash) REFERENCES {MEMORY_FACT_BATCHES}(fact_batch_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    MEMORY_DELIVERY_LEASES: f"""CREATE TABLE {MEMORY_DELIVERY_LEASES}(
        outbox_id TEXT NOT NULL PRIMARY KEY,
        lease_owner TEXT NOT NULL,lease_attempt INTEGER NOT NULL CHECK(lease_attempt>0),
        lease_expires_at TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        updated_at TEXT NOT NULL,
        FOREIGN KEY(outbox_id) REFERENCES {MEMORY_OUTBOX}(outbox_id)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}


IMMUTABLE_TABLES = (
    MEMORY_PUBLICATION_AUTHORITIES,
    MEMORY_FACT_BATCHES,
    MEMORY_FACT_ROWS,
    MEMORY_OUTBOX,
)


def _immutable_trigger(table: str, action: str) -> str:
    return (
        f"CREATE TRIGGER {table}_{action.casefold()}_forbidden BEFORE {action} "
        f"ON {table} BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"
    )


DELIVERY_CAS_TRIGGER = f"""CREATE TRIGGER {MEMORY_DELIVERY_HEADS}_cas_only
    BEFORE UPDATE ON {MEMORY_DELIVERY_HEADS}
    WHEN NOT(
        OLD.state='pending' AND OLD.revision=1 AND OLD.delivered_event_hash IS NULL
        AND NEW.outbox_id=OLD.outbox_id AND NEW.fact_batch_hash=OLD.fact_batch_hash
        AND NEW.state='delivered' AND NEW.revision=OLD.revision+1
        AND length(NEW.delivered_event_hash)=64
        AND NEW.delivered_event_hash NOT GLOB '*[^0-9a-f]*'
        AND NEW.created_at=OLD.created_at
    )
    BEGIN SELECT RAISE(ABORT,'{MEMORY_DELIVERY_HEADS}_cas_only'); END"""

DELIVERY_DELETE_TRIGGER = _immutable_trigger(MEMORY_DELIVERY_HEADS, "DELETE")

LEASE_CAS_TRIGGER = f"""CREATE TRIGGER {MEMORY_DELIVERY_LEASES}_cas_only
    BEFORE UPDATE ON {MEMORY_DELIVERY_LEASES}
    WHEN NOT(
        NEW.outbox_id=OLD.outbox_id
        AND NEW.lease_attempt=OLD.lease_attempt+1
        AND NEW.revision=OLD.revision+1
        AND length(NEW.lease_owner)>0 AND length(NEW.lease_expires_at)>0
    )
    BEGIN SELECT RAISE(ABORT,'{MEMORY_DELIVERY_LEASES}_cas_only'); END"""


TRIGGER_SQL = {
    **{
        f"{table}_{action.casefold()}_forbidden": _immutable_trigger(table, action)
        for table in IMMUTABLE_TABLES
        for action in ("UPDATE", "DELETE")
    },
    f"{MEMORY_DELIVERY_HEADS}_cas_only": DELIVERY_CAS_TRIGGER,
    f"{MEMORY_DELIVERY_HEADS}_delete_forbidden": DELIVERY_DELETE_TRIGGER,
    f"{MEMORY_DELIVERY_LEASES}_cas_only": LEASE_CAS_TRIGGER,
}

INDEX_SQL = {
    f"{MEMORY_DELIVERY_HEADS}_pending_idx": (
        f"CREATE INDEX {MEMORY_DELIVERY_HEADS}_pending_idx "
        f"ON {MEMORY_DELIVERY_HEADS}(state,revision,outbox_id)"
    ),
    f"{MEMORY_DELIVERY_LEASES}_expiry_idx": (
        f"CREATE INDEX {MEMORY_DELIVERY_LEASES}_expiry_idx "
        f"ON {MEMORY_DELIVERY_LEASES}(lease_expires_at,outbox_id)"
    ),
    f"{MEMORY_FACT_ROWS}_m_once_consumed_idx": (
        f"CREATE INDEX {MEMORY_FACT_ROWS}_m_once_consumed_idx "
        f"ON {MEMORY_FACT_ROWS}(m_once_identity_hash) "
        "WHERE fact_type='past_mistake_priority_consumed_fact'"
    ),
}


SCHEMA_STATEMENTS = (
    *TABLE_SQL.values(),
    *TRIGGER_SQL.values(),
    *INDEX_SQL.values(),
)


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace('"', "")


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _payload(raw: Any) -> dict[str, Any] | None:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or _canonical(value) != raw:
        return None
    return value


def _valid_instant(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for row in db.execute(f"SELECT * FROM {MEMORY_PUBLICATION_AUTHORITIES}"):
        payload = _payload(row["payload_json"])
        if payload is None:
            issues.append("publication_authority_payload_invalid")
            break
        body = {key: value for key, value in payload.items() if key != "authority_bridge_hash"}
        expected = (
            payload.get("authority_bridge_hash"),
            payload.get("job_id"),
            payload.get("exam_id"),
            payload.get("publication_hash"),
            payload.get("commit_authority_hash"),
            payload.get("mode"),
            payload.get("namespace"),
            payload.get("memory_type"),
            payload.get("actor"),
            payload.get("published_at"),
        )
        actual = (
            row["authority_bridge_hash"], row["job_id"], row["exam_id"],
            row["publication_hash"], row["commit_authority_hash"], row["mode"],
            row["namespace"], row["memory_type"], row["actor"], row["published_at"],
        )
        if (
            expected != actual
            or _digest(body) != row["authority_bridge_hash"]
            or payload.get("production_activation_allowed") is not False
            or int(row["production_activation_allowed"]) != 0
            or not _valid_instant(row["published_at"])
        ):
            issues.append("publication_authority_row_drift")
            break
    for row in db.execute(f"SELECT * FROM {MEMORY_FACT_BATCHES}"):
        payload = _payload(row["payload_json"])
        if payload is None:
            issues.append("memory_fact_batch_payload_invalid")
            break
        body = {key: value for key, value in payload.items() if key != "fact_batch_hash"}
        publication = payload.get("publication_authority") or {}
        actual = (
            row["job_id"], row["fact_batch_hash"], row["mode_memory_policy_hash"],
            row["memory_snapshot_hash"], row["publication_hash"],
            row["commit_authority_hash"], row["mode"], row["namespace"], row["actor"],
            row["fact_count"], row["batch_version"],
        )
        expected = (
            publication.get("job_id"), payload.get("fact_batch_hash"),
            payload.get("mode_memory_policy_hash"), payload.get("memory_snapshot_hash"),
            publication.get("publication_hash"), publication.get("commit_authority_hash"),
            payload.get("mode"), payload.get("namespace"), payload.get("actor"),
            payload.get("fact_count"), payload.get("version"),
        )
        fact_count = db.execute(
            f"SELECT COUNT(*) FROM {MEMORY_FACT_ROWS} WHERE fact_batch_hash=?",
            (row["fact_batch_hash"],),
        ).fetchone()[0]
        if (
            actual != expected
            or _digest(body) != row["fact_batch_hash"]
            or int(row["production_activation_allowed"]) != 0
            or payload.get("production_write_allowed") is not False
            or int(fact_count) != int(row["fact_count"])
        ):
            issues.append("memory_fact_batch_row_drift")
            break
    for row in db.execute(f"SELECT * FROM {MEMORY_FACT_ROWS}"):
        payload = _payload(row["payload_json"])
        if payload is None:
            issues.append("memory_fact_row_payload_invalid")
            break
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"idempotency_key", "fact_hash"}
        }
        identity = {
            "fact_type": payload.get("fact_type"),
            "namespace": payload.get("namespace"),
            "actor": payload.get("actor"),
            "publication_hash": payload.get("publication_hash"),
            "position": payload.get("position"),
            "candidate_id": payload.get("candidate_id"),
            "content_hash": payload.get("content_hash"),
            "m_once_identity_hash": payload.get("m_once_identity_hash"),
        }
        actual = (
            row["idempotency_key"], row["fact_hash"], row["fact_type"],
            row["namespace"], row["actor"], row["position"], row["candidate_id"],
            row["candidate_fact_hash"], row["content_hash"], row["lineage_hash"],
            row["m_once_identity_hash"], row["fact_version"],
        )
        expected = (
            payload.get("idempotency_key"), payload.get("fact_hash"),
            payload.get("fact_type"), payload.get("namespace"), payload.get("actor"),
            payload.get("position"), payload.get("candidate_id"),
            payload.get("candidate_fact_hash"), payload.get("content_hash"),
            payload.get("lineage_hash"), payload.get("m_once_identity_hash"),
            payload.get("version"),
        )
        if (
            actual != expected
            or _digest(body) != row["fact_hash"]
            or _digest(identity) != row["idempotency_key"]
        ):
            issues.append("memory_fact_row_drift")
            break
    for row in db.execute(f"SELECT * FROM {MEMORY_OUTBOX}"):
        payload = _payload(row["payload_json"])
        if payload is None:
            issues.append("memory_outbox_payload_invalid")
            break
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"outbox_id", "envelope_hash"}
        }
        expected_outbox_id = _digest(
            {"version": payload.get("version"), "fact_batch_hash": payload.get("fact_batch_hash")}
        )
        try:
            event_day_valid = date.fromisoformat(str(row["event_day"])).isoformat() == row["event_day"]
        except ValueError:
            event_day_valid = False
        actual = (
            row["outbox_id"], row["fact_batch_hash"], row["memory_type"], row["actor"],
            row["event_id"], row["event_day"], row["envelope_hash"],
        )
        expected = (
            payload.get("outbox_id"), payload.get("fact_batch_hash"),
            payload.get("memory_type"), payload.get("actor"), payload.get("event_id"),
            payload.get("event_day"), payload.get("envelope_hash"),
        )
        if (
            actual != expected
            or _digest(body) != row["envelope_hash"]
            or expected_outbox_id != row["outbox_id"]
            or int(row["production_activation_allowed"]) != 0
            or not event_day_valid
            or not _valid_instant(payload.get("publication_committed_at"))
        ):
            issues.append("memory_outbox_row_drift")
            break
    for row in db.execute(f"SELECT * FROM {MEMORY_DELIVERY_LEASES}"):
        if not _valid_instant(row["lease_expires_at"]) or not _valid_instant(row["updated_at"]):
            issues.append("memory_delivery_lease_time_invalid")
            break
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, expected in TABLE_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (table,)
        ).fetchone()
        if row is None:
            issues.append(f"missing_table:{table}")
        elif str(row[0]) != "table" or _norm(row[1]) != _norm(expected):
            issues.append(f"table_sql_drift:{table}")
    for name, expected in TRIGGER_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None or str(row[0]) != "trigger" or _norm(row[1]) != _norm(expected):
            issues.append(f"trigger_sql_drift:{name}")
    for name, expected in INDEX_SQL.items():
        row = db.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()
        if row is None or str(row[0]) != "index" or _norm(row[1]) != _norm(expected):
            issues.append(f"index_sql_drift:{name}")
    try:
        issues.extend(
            f"foreign_key_violation:{row[0]}:{row[1]}"
            for row in db.execute("PRAGMA foreign_key_check")
        )
    except sqlite3.DatabaseError:
        issues.append("foreign_key_check_failed")
    if not issues:
        issues.extend(_row_issues(db))
    return issues


def migrate_v49_to_v50(
    db: sqlite3.Connection,
    *,
    fault_injector: Callable[[str], None] | None = None,
) -> None:
    for index, statement in enumerate(SCHEMA_STATEMENTS, start=1):
        db.execute(statement)
        if fault_injector is not None:
            fault_injector(f"after_statement_{index}")
    issues = schema_issues(db)
    if issues:
        raise RuntimeError("v50 mode-memory outbox schema validation failed: " + ";".join(issues))
    if str(db.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
        raise RuntimeError("v50 mode-memory outbox quick_check failed")


__all__ = [
    "BATCH_VERSION",
    "FACT_VERSION",
    "MEMORY_DELIVERY_HEADS",
    "MEMORY_DELIVERY_LEASES",
    "MEMORY_FACT_BATCHES",
    "MEMORY_FACT_ROWS",
    "MEMORY_OUTBOX",
    "MEMORY_PUBLICATION_AUTHORITIES",
    "INDEX_SQL",
    "SCHEMA_STATEMENTS",
    "TABLE_SQL",
    "migrate_v49_to_v50",
    "schema_issues",
]
