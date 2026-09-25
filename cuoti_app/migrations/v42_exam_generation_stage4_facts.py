"""Schema 42: immutable Stage-4 candidate facts and difficulty flights."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter, defaultdict


FACT_RECEIPTS = "exam_generation_stage4_fact_receipts"
FACT_ITEMS = "exam_generation_stage4_candidate_facts"
FLIGHTS = "exam_generation_stage4_difficulty_flights"
ROUNDS = "exam_generation_stage4_difficulty_round_receipts"
RESULTS = "exam_generation_stage4_difficulty_results"
OUTCOMES = "exam_generation_stage4_difficulty_outcomes"
CONFLICTS = "exam_generation_stage4_difficulty_conflicts"


def _hash_check(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = {
    FACT_RECEIPTS: f"""CREATE TABLE IF NOT EXISTS {FACT_RECEIPTS}(
        job_id TEXT NOT NULL PRIMARY KEY,
        receipt_contract_version TEXT NOT NULL CHECK(receipt_contract_version='past-v3-stage4-fact-receipt-v1'),
        candidate_facts_contract_version TEXT NOT NULL CHECK(candidate_facts_contract_version='past-v3-stage4-candidate-facts-v1'),
        stage3_contract_hash TEXT NOT NULL {_hash_check('stage3_contract_hash')},
        stage3_receipt_hash TEXT NOT NULL {_hash_check('stage3_receipt_hash')},
        projection_identity_version TEXT NOT NULL CHECK(projection_identity_version='past-v3-stage4-projection-identity-v1'),
        projection_id TEXT NOT NULL,
        semantic_snapshot_id TEXT NOT NULL,
        source_snapshot TEXT NOT NULL,
        algorithm_version TEXT NOT NULL,
        dictionary_version TEXT NOT NULL,
        weights_version TEXT NOT NULL,
        projection_build_hash TEXT NOT NULL {_hash_check('projection_build_hash')},
        projection_item_count INTEGER NOT NULL CHECK(projection_item_count>=0),
        projection_identity_hash TEXT NOT NULL {_hash_check('projection_identity_hash')},
        target_institution TEXT NOT NULL,
        candidate_pool_hash TEXT NOT NULL {_hash_check('candidate_pool_hash')},
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        unresolved_candidate_ids_hash TEXT NOT NULL {_hash_check('unresolved_candidate_ids_hash')},
        candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
        unresolved_count INTEGER NOT NULL CHECK(unresolved_count>=0 AND unresolved_count<=candidate_count),
        facts_payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        UNIQUE(job_id,receipt_hash),
        UNIQUE(job_id,candidate_facts_hash),
        FOREIGN KEY(job_id,stage3_receipt_hash)
            REFERENCES exam_generation_stage3_contract_receipts(job_id,receipt_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    FACT_ITEMS: f"""CREATE TABLE IF NOT EXISTS {FACT_ITEMS}(
        job_id TEXT NOT NULL,
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('ordinary','past_exam_other')),
        candidate_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        question_type_family TEXT NOT NULL,
        chapter TEXT,
        source_exam_id TEXT,
        section_id TEXT,
        source_institution TEXT,
        content_hash TEXT NOT NULL {_hash_check('content_hash')},
        content_snapshot TEXT NOT NULL,
        content_snapshot_hash TEXT NOT NULL {_hash_check('content_snapshot_hash')},
        card_version_id INTEGER NOT NULL CHECK(card_version_id>0),
        card_hash TEXT NOT NULL {_hash_check('card_hash')},
        projection_document_hash TEXT NOT NULL {_hash_check('projection_document_hash')},
        difficulty_resolved INTEGER NOT NULL CHECK(difficulty_resolved IN (0,1)),
        difficulty_label TEXT CHECK(difficulty_label IN ('简单','中等','困难')),
        difficulty_confidence REAL CHECK(difficulty_confidence IS NULL OR (difficulty_confidence>=0 AND difficulty_confidence<=1)),
        difficulty_reason TEXT NOT NULL,
        fact_json TEXT NOT NULL,
        fact_hash TEXT NOT NULL {_hash_check('fact_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,candidate_id),
        UNIQUE(job_id,ordinal),
        UNIQUE(job_id,candidate_facts_hash,candidate_id),
        CHECK((source_kind='ordinary' AND chapter IS NOT NULL AND source_exam_id IS NULL AND section_id IS NULL AND source_institution IS NULL)
           OR (source_kind='past_exam_other' AND chapter IS NULL AND source_exam_id IS NOT NULL AND section_id IS NOT NULL AND source_institution IS NOT NULL)),
        CHECK((difficulty_resolved=1 AND difficulty_label IS NOT NULL AND difficulty_confidence>=0.6 AND difficulty_reason='difficulty_resolved')
           OR (difficulty_resolved=0 AND difficulty_label IS NULL)),
        FOREIGN KEY(job_id,candidate_facts_hash)
            REFERENCES {FACT_RECEIPTS}(job_id,candidate_facts_hash)
            ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    FLIGHTS: f"""CREATE TABLE IF NOT EXISTS {FLIGHTS}(
        flight_hash TEXT NOT NULL PRIMARY KEY {_hash_check('flight_hash')},
        flight_contract_version TEXT NOT NULL CHECK(flight_contract_version='past-v3-stage4-difficulty-flight-v1'),
        resolution_contract_version TEXT NOT NULL CHECK(resolution_contract_version='past-v3-stage4-difficulty-resolution-v1'),
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        component_hash TEXT NOT NULL {_hash_check('component_hash')},
        input_facts_hash TEXT NOT NULL {_hash_check('input_facts_hash')},
        rules_hash TEXT NOT NULL {_hash_check('rules_hash')},
        classification_model TEXT NOT NULL,
        classification_prompt_version TEXT NOT NULL,
        verification_model TEXT NOT NULL,
        verification_prompt_version TEXT NOT NULL,
        candidate_ids_hash TEXT NOT NULL {_hash_check('candidate_ids_hash')},
        candidate_count INTEGER NOT NULL CHECK(candidate_count>0),
        state TEXT NOT NULL CHECK(state IN ('running','completed')),
        revision INTEGER NOT NULL CHECK(revision>0),
        claim_job_id TEXT NOT NULL,
        claim_worker_id TEXT NOT NULL,
        claim_attempt INTEGER NOT NULL CHECK(claim_attempt>0),
        result_hash TEXT CHECK(result_hash IS NULL OR (length(result_hash)=64 AND result_hash NOT GLOB '*[^0-9a-f]*')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(flight_hash,revision),
        CHECK((state='running' AND result_hash IS NULL) OR (state='completed' AND result_hash IS NOT NULL))
    )""",
    ROUNDS: f"""CREATE TABLE IF NOT EXISTS {ROUNDS}(
        flight_hash TEXT NOT NULL,
        completed_from_revision INTEGER NOT NULL CHECK(completed_from_revision>0),
        round_name TEXT NOT NULL CHECK(round_name IN ('classification','verification')),
        response_schema_version TEXT NOT NULL,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        rules_hash TEXT NOT NULL {_hash_check('rules_hash')},
        input_facts_hash TEXT NOT NULL {_hash_check('input_facts_hash')},
        response_status TEXT NOT NULL CHECK(response_status IN ('received','timeout')),
        response_hash TEXT CHECK(response_hash IS NULL OR (length(response_hash)=64 AND response_hash NOT GLOB '*[^0-9a-f]*')),
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(flight_hash,round_name),
        UNIQUE(flight_hash,receipt_hash),
        CHECK((response_status='received' AND response_hash IS NOT NULL) OR (response_status='timeout' AND response_hash IS NULL)),
        FOREIGN KEY(flight_hash) REFERENCES {FLIGHTS}(flight_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    RESULTS: f"""CREATE TABLE IF NOT EXISTS {RESULTS}(
        flight_hash TEXT NOT NULL PRIMARY KEY,
        completed_from_revision INTEGER NOT NULL CHECK(completed_from_revision>0),
        result_contract_version TEXT NOT NULL CHECK(result_contract_version='past-v3-stage4-difficulty-resolution-v1'),
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        component_hash TEXT NOT NULL {_hash_check('component_hash')},
        input_facts_hash TEXT NOT NULL {_hash_check('input_facts_hash')},
        result_hash TEXT NOT NULL {_hash_check('result_hash')},
        outcome_count INTEGER NOT NULL CHECK(outcome_count>0),
        result_payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        UNIQUE(flight_hash,result_hash),
        FOREIGN KEY(flight_hash) REFERENCES {FLIGHTS}(flight_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    OUTCOMES: f"""CREATE TABLE IF NOT EXISTS {OUTCOMES}(
        flight_hash TEXT NOT NULL,
        result_hash TEXT NOT NULL {_hash_check('result_hash')},
        candidate_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('resolved','excluded','conflict')),
        effective_difficulty TEXT CHECK(effective_difficulty IN ('简单','中等','困难')),
        reason TEXT NOT NULL,
        classification_confidence REAL CHECK(classification_confidence IS NULL OR (classification_confidence>=0 AND classification_confidence<=1)),
        verification_confidence REAL CHECK(verification_confidence IS NULL OR (verification_confidence>=0 AND verification_confidence<=1)),
        evidence_hash TEXT NOT NULL {_hash_check('evidence_hash')},
        outcome_hash TEXT NOT NULL {_hash_check('outcome_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(flight_hash,candidate_id),
        CHECK((status='resolved' AND effective_difficulty IS NOT NULL) OR (status!='resolved' AND effective_difficulty IS NULL)),
        FOREIGN KEY(flight_hash,result_hash) REFERENCES {RESULTS}(flight_hash,result_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    CONFLICTS: f"""CREATE TABLE IF NOT EXISTS {CONFLICTS}(
        conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
        flight_hash TEXT NOT NULL,
        observed_revision INTEGER NOT NULL CHECK(observed_revision>0),
        conflict_type TEXT NOT NULL,
        candidate_id TEXT,
        evidence_hash TEXT NOT NULL {_hash_check('evidence_hash')},
        conflict_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(flight_hash) REFERENCES {FLIGHTS}(flight_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
}

INDEX_SQL = {
    "idx_exam_stage4_conflicts_flight": f"CREATE INDEX IF NOT EXISTS idx_exam_stage4_conflicts_flight ON {CONFLICTS}(flight_hash,conflict_id)",
}

TRIGGER_SQL: dict[str, tuple[str, str]] = {}
for _table in (FACT_RECEIPTS, FACT_ITEMS, ROUNDS, RESULTS, OUTCOMES, CONFLICTS):
    _name = f"{_table}_no_update"
    TRIGGER_SQL[_name] = (_table, f"""CREATE TRIGGER IF NOT EXISTS {_name} BEFORE UPDATE ON {_table}
        BEGIN SELECT RAISE(ABORT,'{_table}_immutable'); END""")
for _table in (FLIGHTS, ROUNDS, RESULTS, OUTCOMES, CONFLICTS):
    _name = f"{_table}_no_delete"
    TRIGGER_SQL[_name] = (_table, f"""CREATE TRIGGER IF NOT EXISTS {_name} BEFORE DELETE ON {_table}
        BEGIN SELECT RAISE(ABORT,'{_table}_append_only'); END""")
TRIGGER_SQL[f"{FLIGHTS}_identity_immutable"] = (
    FLIGHTS,
    f"""CREATE TRIGGER IF NOT EXISTS {FLIGHTS}_identity_immutable BEFORE UPDATE ON {FLIGHTS}
        WHEN NEW.flight_hash!=OLD.flight_hash
          OR NEW.flight_contract_version!=OLD.flight_contract_version
          OR NEW.resolution_contract_version!=OLD.resolution_contract_version
          OR NEW.candidate_facts_hash!=OLD.candidate_facts_hash
          OR NEW.component_hash!=OLD.component_hash
          OR NEW.input_facts_hash!=OLD.input_facts_hash
          OR NEW.rules_hash!=OLD.rules_hash
          OR NEW.classification_model!=OLD.classification_model
          OR NEW.classification_prompt_version!=OLD.classification_prompt_version
          OR NEW.verification_model!=OLD.verification_model
          OR NEW.verification_prompt_version!=OLD.verification_prompt_version
          OR NEW.candidate_ids_hash!=OLD.candidate_ids_hash
          OR NEW.candidate_count!=OLD.candidate_count
          OR NEW.created_at!=OLD.created_at
        BEGIN SELECT RAISE(ABORT,'exam_generation_stage4_difficulty_flight_identity_immutable'); END""",
)

SCHEMA_STATEMENTS = (
    *(CREATE_TABLE_SQL[name] for name in (FACT_RECEIPTS, FACT_ITEMS, FLIGHTS, ROUNDS, RESULTS, OUTCOMES, CONFLICTS)),
    *INDEX_SQL.values(),
    *(value[1] for value in TRIGGER_SQL.values()),
)


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def _foreign_keys(db: sqlite3.Connection, table: str) -> set[tuple]:
    grouped: dict[int, list[tuple]] = defaultdict(list)
    for row in db.execute(f'PRAGMA foreign_key_list("{table}")'):
        grouped[int(row[0])].append(tuple(row))
    result = set()
    for rows in grouped.values():
        rows.sort(key=lambda item: int(item[1]))
        result.add((str(rows[0][2]), tuple(str(r[3]) for r in rows), tuple(str(r[4]) for r in rows), str(rows[0][5]), str(rows[0][6]), str(rows[0][7])))
    return result


EXPECTED_FKS = {
    FACT_RECEIPTS: {("exam_generation_stage3_contract_receipts", ("job_id", "stage3_receipt_hash"), ("job_id", "receipt_hash"), "RESTRICT", "CASCADE", "NONE")},
    FACT_ITEMS: {(FACT_RECEIPTS, ("job_id", "candidate_facts_hash"), ("job_id", "candidate_facts_hash"), "RESTRICT", "CASCADE", "NONE")},
    FLIGHTS: set(),
    ROUNDS: {(FLIGHTS, ("flight_hash",), ("flight_hash",), "RESTRICT", "RESTRICT", "NONE")},
    RESULTS: {(FLIGHTS, ("flight_hash",), ("flight_hash",), "RESTRICT", "RESTRICT", "NONE")},
    OUTCOMES: {(RESULTS, ("flight_hash", "result_hash"), ("flight_hash", "result_hash"), "RESTRICT", "RESTRICT", "NONE")},
    CONFLICTS: {(FLIGHTS, ("flight_hash",), ("flight_hash",), "RESTRICT", "RESTRICT", "NONE")},
}


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def schema_issues(db: sqlite3.Connection, *, fact_items_sql: str | None = None) -> list[str]:
    issues: list[str] = []
    for table, expected_sql in CREATE_TABLE_SQL.items():
        if table == FACT_ITEMS and fact_items_sql is not None:
            expected_sql = fact_items_sql
        master = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if master is None:
            issues.append(f"版本 42 缺少表 {table}")
            continue
        if str(master[0]) != "table" or _normalized(master[1]) != _normalized(expected_sql):
            issues.append(f"版本 42 的表 {table} 定义不兼容")
        if _foreign_keys(db, table) != EXPECTED_FKS[table]:
            issues.append(f"版本 42 的表 {table} 外键定义不兼容")
        expected_indexes = {"idx_exam_stage4_conflicts_flight"} if table == CONFLICTS else set()
        actual_indexes = {
            str(row[1]) for row in db.execute(f'PRAGMA index_list("{table}")')
            if str(row[3]) == "c"
        }
        if actual_indexes != expected_indexes:
            issues.append(f"版本 42 的表 {table} 索引集合不兼容")
        expected_triggers = {name for name, value in TRIGGER_SQL.items() if value[0] == table}
        actual_triggers = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if actual_triggers != expected_triggers:
            issues.append(f"版本 42 的表 {table} 触发器集合不兼容")
    for name, expected_sql in INDEX_SQL.items():
        row = db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or str(row[0]) != "index" or str(row[1]) != CONFLICTS or _normalized(row[2]) != _normalized(expected_sql):
            issues.append(f"版本 42 的索引 {name} 定义不兼容")
    for name, (table, expected_sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or str(row[0]) != "trigger" or str(row[1]) != table or _normalized(row[2]) != _normalized(expected_sql):
            issues.append(f"版本 42 的触发器 {name} 定义不兼容")
    if not issues:
        issues.extend(_row_issues(db))
    return issues


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for row in db.execute(f"SELECT * FROM {FACT_RECEIPTS}"):
        value = dict(row)
        try:
            payload = json.loads(value["facts_payload_json"])
        except (TypeError, ValueError):
            issues.append(f"版本 42 的表 {FACT_RECEIPTS} 存在无效 JSON")
            break
        receipt_fields = (
            "receipt_contract_version", "candidate_facts_contract_version", "job_id",
            "stage3_contract_hash", "stage3_receipt_hash", "projection_identity_version",
            "projection_id", "semantic_snapshot_id", "source_snapshot", "algorithm_version",
            "dictionary_version", "weights_version", "projection_build_hash", "projection_item_count",
            "projection_identity_hash", "target_institution", "candidate_pool_hash",
            "candidate_facts_hash", "unresolved_candidate_ids_hash", "candidate_count", "unresolved_count",
        )
        projection = payload.get("projection_identity") if isinstance(payload, dict) else None
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        unresolved = payload.get("unresolved_candidate_ids") if isinstance(payload, dict) else None
        pool = (
            [
                {key: item[key] for key in ("source_kind", "candidate_id", "content_hash", "card_version_id", "card_hash", "projection_document_hash")}
                for item in candidates
            ]
            if isinstance(candidates, list)
            else None
        )
        stored_items = []
        try:
            stored_items = [
                json.loads(item[0])
                for item in db.execute(
                    f"SELECT fact_json FROM {FACT_ITEMS} WHERE job_id=? ORDER BY ordinal",
                    (value["job_id"],),
                )
            ]
        except (TypeError, ValueError):
            stored_items = [None]
        stage3 = db.execute(
            "SELECT stage3_contract_hash FROM exam_generation_stage3_contract_receipts "
            "WHERE job_id=? AND receipt_hash=?",
            (value["job_id"], value["stage3_receipt_hash"]),
        ).fetchone()
        if (
            _canonical_hash(payload) != value["candidate_facts_hash"]
            or not hmac.compare_digest(str(value["receipt_hash"]), _canonical_hash({field: value[field] for field in receipt_fields}))
            or not isinstance(projection, dict)
            or _canonical_hash(projection) != value["projection_identity_hash"]
            or not isinstance(candidates, list)
            or not isinstance(unresolved, list)
            or _canonical_hash(unresolved) != value["unresolved_candidate_ids_hash"]
            or _canonical_hash(pool) != value["candidate_pool_hash"]
            or len(candidates) != int(value["candidate_count"])
            or len(unresolved) != int(value["unresolved_count"])
            or stored_items != candidates
            or stage3 is None
            or str(stage3[0]) != str(value["stage3_contract_hash"])
            or any(projection.get(field) != value[column] for field, column in (
                ("identity_version", "projection_identity_version"),
                ("projection_id", "projection_id"), ("semantic_snapshot_id", "semantic_snapshot_id"),
                ("source_snapshot", "source_snapshot"), ("algorithm_version", "algorithm_version"),
                ("dictionary_version", "dictionary_version"), ("weights_version", "weights_version"),
                ("projection_build_hash", "projection_build_hash"), ("item_count", "projection_item_count"),
            ))
        ):
            issues.append(f"版本 42 的表 {FACT_RECEIPTS} 存在事实哈希不一致")
            break
        count = db.execute(f"SELECT COUNT(*) FROM {FACT_ITEMS} WHERE job_id=?", (value["job_id"],)).fetchone()[0]
        if int(count) != int(value["candidate_count"]):
            issues.append(f"版本 42 的表 {FACT_RECEIPTS} 存在候选计数不一致")
            break
    for row in db.execute(f"SELECT fact_json,fact_hash FROM {FACT_ITEMS}"):
        try:
            parsed = json.loads(row[0])
        except (TypeError, ValueError):
            issues.append(f"版本 42 的表 {FACT_ITEMS} 存在无效 JSON")
            break
        if not hmac.compare_digest(str(row[1]), _canonical_hash(parsed)):
            issues.append(f"版本 42 的表 {FACT_ITEMS} 存在事实哈希不一致")
            break
    for row in db.execute(f"SELECT * FROM {FLIGHTS}"):
        value = dict(row)
        identity_fields = (
            "flight_contract_version", "resolution_contract_version", "candidate_facts_hash",
            "component_hash", "input_facts_hash", "rules_hash", "classification_model",
            "classification_prompt_version", "verification_model", "verification_prompt_version",
            "candidate_ids_hash", "candidate_count",
        )
        if not hmac.compare_digest(str(value["flight_hash"]), _canonical_hash({field: value[field] for field in identity_fields})):
            issues.append(f"版本 42 的表 {FLIGHTS} 存在身份哈希不一致")
            break
    for row in db.execute(f"SELECT * FROM {ROUNDS}"):
        value = dict(row)
        fields = (
            "flight_hash", "completed_from_revision", "round_name", "response_schema_version",
            "model", "prompt_version", "rules_hash", "input_facts_hash", "response_status", "response_hash",
        )
        if not hmac.compare_digest(str(value["receipt_hash"]), _canonical_hash({field: value[field] for field in fields})):
            issues.append(f"版本 42 的表 {ROUNDS} 存在回执哈希不一致")
            break
    for row in db.execute(f"SELECT * FROM {RESULTS}"):
        value = dict(row)
        try:
            payload = json.loads(value["result_payload_json"])
        except (TypeError, ValueError):
            issues.append(f"版本 42 的表 {RESULTS} 存在无效 JSON")
            break
        fields = (
            "flight_hash", "completed_from_revision", "result_contract_version", "candidate_facts_hash",
            "component_hash", "input_facts_hash", "result_hash", "outcome_count", "result_payload_json",
        )
        outcome_count = db.execute(f"SELECT COUNT(*) FROM {OUTCOMES} WHERE flight_hash=?", (value["flight_hash"],)).fetchone()[0]
        if (
            not hmac.compare_digest(str(value["result_hash"]), _canonical_hash(payload))
            or not hmac.compare_digest(str(value["receipt_hash"]), _canonical_hash({field: value[field] for field in fields}))
            or int(outcome_count) != int(value["outcome_count"])
        ):
            issues.append(f"版本 42 的表 {RESULTS} 存在结果哈希不一致")
            break
    for row in db.execute(f"SELECT * FROM {OUTCOMES}"):
        value = dict(row)
        payload = {field: value[field] for field in (
            "candidate_id", "status", "effective_difficulty", "reason",
            "classification_confidence", "verification_confidence", "evidence_hash",
        )}
        if not hmac.compare_digest(str(value["outcome_hash"]), _canonical_hash(payload)):
            issues.append(f"版本 42 的表 {OUTCOMES} 存在结果哈希不一致")
            break
    for row in db.execute(f"SELECT conflict_json FROM {CONFLICTS}"):
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            issues.append(f"版本 42 的表 {CONFLICTS} 存在无效 JSON")
            break
        if not isinstance(value, dict):
            issues.append(f"版本 42 的表 {CONFLICTS} 存在无效冲突证据")
            break
    return issues


def _existing_conflicts(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for name, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "table" or _normalized(row[1]) != _normalized(sql)):
            issues.append(f"版本 42 的对象 {name} 定义不兼容")
    for name, sql in INDEX_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "index" or _normalized(row[1]) != _normalized(sql)):
            issues.append(f"版本 42 的对象 {name} 定义不兼容")
    for name, (_, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "trigger" or _normalized(row[1]) != _normalized(sql)):
            issues.append(f"版本 42 的对象 {name} 定义不兼容")
    return issues


def migrate_v41_to_v42(db: sqlite3.Connection) -> None:
    conflicts = _existing_conflicts(db)
    if conflicts:
        raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


__all__ = ["CONFLICTS", "FACT_ITEMS", "FACT_RECEIPTS", "FLIGHTS", "OUTCOMES", "RESULTS", "ROUNDS", "SCHEMA_STATEMENTS", "migrate_v41_to_v42", "schema_issues"]
