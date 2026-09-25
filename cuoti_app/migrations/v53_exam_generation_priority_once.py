"""Schema 53: immutable exact M_once publication consumptions."""

from __future__ import annotations


PRIORITY_ONCE_CONSUMPTIONS = "exam_generation_priority_once_consumptions"
PRIORITY_ONCE_SELECTIONS = "exam_generation_priority_once_selections"
D2_RESELECTIONS = "exam_generation_d2_reselections"
_UPDATE_TRIGGER = f"{PRIORITY_ONCE_CONSUMPTIONS}_update_forbidden"
_DELETE_TRIGGER = f"{PRIORITY_ONCE_CONSUMPTIONS}_delete_forbidden"


def _hash_check(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def migrate_v52_to_v53(db) -> None:
    db.execute(f"""CREATE TABLE IF NOT EXISTS {PRIORITY_ONCE_CONSUMPTIONS}(
        subject_id INTEGER NOT NULL CHECK(subject_id>0),
        mode TEXT NOT NULL CHECK(mode IN ('free','mistakes','past')),
        candidate_content_hash TEXT NOT NULL CHECK({_hash_check('candidate_content_hash')}),
        source_mistake_public_id TEXT NOT NULL CHECK(length(trim(source_mistake_public_id))>0),
        relation_content_fingerprint TEXT NOT NULL CHECK({_hash_check('relation_content_fingerprint')}),
        job_id TEXT NOT NULL CHECK(length(trim(job_id))>0),
        exam_id INTEGER NOT NULL CHECK(exam_id>0),
        publication_hash TEXT NOT NULL CHECK({_hash_check('publication_hash')}),
        consumed_at TEXT NOT NULL CHECK(length(trim(consumed_at))>0),
        PRIMARY KEY(
            subject_id,mode,candidate_content_hash,
            source_mistake_public_id,relation_content_fingerprint
        )
    )""")
    db.execute(
        f"CREATE TRIGGER IF NOT EXISTS {_UPDATE_TRIGGER} BEFORE UPDATE "
        f"ON {PRIORITY_ONCE_CONSUMPTIONS} BEGIN "
        f"SELECT RAISE(ABORT,'{PRIORITY_ONCE_CONSUMPTIONS} is immutable'); END"
    )
    db.execute(
        f"CREATE TRIGGER IF NOT EXISTS {_DELETE_TRIGGER} BEFORE DELETE "
        f"ON {PRIORITY_ONCE_CONSUMPTIONS} BEGIN "
        f"SELECT RAISE(ABORT,'{PRIORITY_ONCE_CONSUMPTIONS} is immutable'); END"
    )
    db.execute(f"""CREATE TABLE IF NOT EXISTS {PRIORITY_ONCE_SELECTIONS}(
        job_id TEXT NOT NULL,
        selection_hash TEXT NOT NULL CHECK({_hash_check('selection_hash')}),
        claims_json TEXT NOT NULL CHECK(json_valid(claims_json)),
        PRIMARY KEY(job_id,selection_hash),
        FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
    )""")
    db.execute(f"""CREATE TABLE IF NOT EXISTS {D2_RESELECTIONS}(
        job_id TEXT NOT NULL,
        round INTEGER NOT NULL CHECK(round>0),
        previous_selection_hash TEXT NOT NULL CHECK({_hash_check('previous_selection_hash')}),
        next_selection_hash TEXT NOT NULL CHECK({_hash_check('next_selection_hash')}),
        reason TEXT NOT NULL CHECK(length(trim(reason))>0),
        quality_state_json TEXT NOT NULL CHECK(json_valid(quality_state_json)),
        created_at TEXT NOT NULL CHECK(length(trim(created_at))>0),
        PRIMARY KEY(job_id,round),
        FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
    )""")


def schema_issues(db) -> list[str]:
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({PRIORITY_ONCE_CONSUMPTIONS})")}
    expected = {
        "subject_id", "mode", "candidate_content_hash",
        "source_mistake_public_id", "relation_content_fingerprint",
        "job_id", "exam_id", "publication_hash", "consumed_at",
    }
    triggers = {
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name IN (?,?)",
            (_UPDATE_TRIGGER, _DELETE_TRIGGER),
        )
    }
    issues = []
    if columns != expected:
        issues.append("exam priority-once consumption table missing or invalid")
    if triggers != {_UPDATE_TRIGGER, _DELETE_TRIGGER}:
        issues.append("exam priority-once immutable triggers missing")
    selections = {row[1] for row in db.execute(f"PRAGMA table_info({PRIORITY_ONCE_SELECTIONS})")}
    if selections != {"job_id", "selection_hash", "claims_json"}:
        issues.append("exam priority-once selection table missing or invalid")
    reselections = {row[1] for row in db.execute(f"PRAGMA table_info({D2_RESELECTIONS})")}
    if reselections != {
        "job_id", "round", "previous_selection_hash", "next_selection_hash",
        "reason", "quality_state_json", "created_at",
    }:
        issues.append("exam D2 reselection table missing or invalid")
    return issues


__all__ = [
    "D2_RESELECTIONS", "PRIORITY_ONCE_CONSUMPTIONS", "PRIORITY_ONCE_SELECTIONS",
    "migrate_v52_to_v53", "schema_issues",
]
