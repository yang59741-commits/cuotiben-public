"""Schema 43: immutable Stage-5 solver artifacts and one CAS head."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import defaultdict


CONTRACTS = "exam_generation_stage5_constraint_receipts"
GRAPHS = "exam_generation_stage5_graph_receipts"
EDGES = "exam_generation_stage5_graph_edges"
WITNESSES = "exam_generation_stage5_infeasible_witnesses"
SELECTIONS = "exam_generation_stage5_selection_receipts"
ASSIGNMENTS = "exam_generation_stage5_assignments"
REPAIRS = "exam_generation_stage5_repair_receipts"
HEADS = "exam_generation_stage5_heads"


def _hash_check(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = {
    CONTRACTS: f"""CREATE TABLE IF NOT EXISTS {CONTRACTS}(
        job_id TEXT NOT NULL PRIMARY KEY,
        receipt_contract_version TEXT NOT NULL CHECK(receipt_contract_version='past-v3-stage5-constraint-receipt-v1'),
        constraint_contract_version TEXT NOT NULL CHECK(constraint_contract_version='past-v3-stage5-global-constraints-v1'),
        stage3_contract_hash TEXT NOT NULL {_hash_check('stage3_contract_hash')},
        stage3_receipt_hash TEXT NOT NULL {_hash_check('stage3_receipt_hash')},
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        stage4_receipt_hash TEXT NOT NULL {_hash_check('stage4_receipt_hash')},
        projection_identity_hash TEXT NOT NULL {_hash_check('projection_identity_hash')},
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        question_count INTEGER NOT NULL CHECK(question_count BETWEEN 1 AND 20),
        payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        UNIQUE(job_id,receipt_hash), UNIQUE(job_id,constraint_hash),
        FOREIGN KEY(job_id,stage3_receipt_hash) REFERENCES exam_generation_stage3_contract_receipts(job_id,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,stage4_receipt_hash) REFERENCES exam_generation_stage4_fact_receipts(job_id,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    GRAPHS: f"""CREATE TABLE IF NOT EXISTS {GRAPHS}(
        graph_hash TEXT NOT NULL {_hash_check('graph_hash')},
        job_id TEXT NOT NULL,
        graph_contract_version TEXT NOT NULL CHECK(graph_contract_version='past-v3-stage5-candidate-edge-graph-v1'),
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        candidate_facts_hash TEXT NOT NULL {_hash_check('candidate_facts_hash')},
        graph_mode TEXT NOT NULL CHECK(graph_mode IN ('initial','expanded')),
        base_graph_hash TEXT,
        edge_count INTEGER NOT NULL CHECK(edge_count>=0),
        candidate_count INTEGER NOT NULL CHECK(candidate_count>=0),
        expanded_slot_ids_hash TEXT NOT NULL {_hash_check('expanded_slot_ids_hash')},
        payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,graph_hash), UNIQUE(job_id,graph_mode), UNIQUE(job_id,receipt_hash),
        CHECK((graph_mode='initial' AND base_graph_hash IS NULL) OR (graph_mode='expanded' AND length(base_graph_hash)=64 AND base_graph_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,constraint_hash) REFERENCES {CONTRACTS}(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,base_graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    EDGES: f"""CREATE TABLE IF NOT EXISTS {EDGES}(
        job_id TEXT NOT NULL,
        graph_hash TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        slot_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        content_hash TEXT NOT NULL {_hash_check('content_hash')},
        edge_json TEXT NOT NULL,
        edge_hash TEXT NOT NULL {_hash_check('edge_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,graph_hash,ordinal), UNIQUE(job_id,graph_hash,slot_id,candidate_id),
        FOREIGN KEY(job_id,graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    WITNESSES: f"""CREATE TABLE IF NOT EXISTS {WITNESSES}(
        witness_hash TEXT NOT NULL {_hash_check('witness_hash')},
        job_id TEXT NOT NULL,
        graph_hash TEXT NOT NULL {_hash_check('graph_hash')},
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        witness_version TEXT NOT NULL CHECK(witness_version='past-v3-stage5-infeasible-witness-v1'),
        witness_kind TEXT NOT NULL CHECK(witness_kind IN ('hall_min_cut','constrained_min_cut')),
        payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,witness_hash), UNIQUE(job_id,graph_hash), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,constraint_hash) REFERENCES {CONTRACTS}(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    SELECTIONS: f"""CREATE TABLE IF NOT EXISTS {SELECTIONS}(
        selection_hash TEXT NOT NULL {_hash_check('selection_hash')},
        job_id TEXT NOT NULL,
        graph_hash TEXT NOT NULL {_hash_check('graph_hash')},
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        selection_contract_version TEXT NOT NULL CHECK(selection_contract_version='past-v3-stage5-selection-v1'),
        score_identity_hash TEXT NOT NULL {_hash_check('score_identity_hash')},
        assignment_count INTEGER NOT NULL CHECK(assignment_count BETWEEN 1 AND 20),
        total_utility INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hash_check('receipt_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_hash), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,constraint_hash) REFERENCES {CONTRACTS}(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    ASSIGNMENTS: f"""CREATE TABLE IF NOT EXISTS {ASSIGNMENTS}(
        job_id TEXT NOT NULL,
        selection_hash TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position>0),
        slot_id TEXT NOT NULL,
        candidate_id TEXT NOT NULL,
        content_hash TEXT NOT NULL {_hash_check('content_hash')},
        assignment_json TEXT NOT NULL,
        assignment_hash TEXT NOT NULL {_hash_check('assignment_hash')},
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_hash,position), UNIQUE(job_id,selection_hash,slot_id), UNIQUE(job_id,selection_hash,candidate_id), UNIQUE(job_id,selection_hash,content_hash),
        FOREIGN KEY(job_id,selection_hash) REFERENCES {SELECTIONS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    REPAIRS: f"""CREATE TABLE IF NOT EXISTS {REPAIRS}(
        repair_hash TEXT NOT NULL {_hash_check('repair_hash')},
        job_id TEXT NOT NULL,
        repair_version TEXT NOT NULL CHECK(repair_version='past-v3-stage5-minimal-repair-v1'),
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        base_graph_hash TEXT NOT NULL {_hash_check('base_graph_hash')},
        repair_graph_hash TEXT NOT NULL {_hash_check('repair_graph_hash')},
        base_selection_hash TEXT,
        witness_hash TEXT,
        replacement_selection_hash TEXT NOT NULL {_hash_check('replacement_selection_hash')},
        repair_scope_hash TEXT NOT NULL {_hash_check('repair_scope_hash')},
        fixed_assignment_hash TEXT NOT NULL {_hash_check('fixed_assignment_hash')},
        receipt_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,repair_hash),
        CHECK((base_selection_hash IS NULL) != (witness_hash IS NULL)),
        CHECK(base_selection_hash IS NULL OR (length(base_selection_hash)=64 AND base_selection_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(witness_hash IS NULL OR (length(witness_hash)=64 AND witness_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,constraint_hash) REFERENCES {CONTRACTS}(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,base_graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,repair_graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,base_selection_hash) REFERENCES {SELECTIONS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,witness_hash) REFERENCES {WITNESSES}(job_id,witness_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,replacement_selection_hash) REFERENCES {SELECTIONS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    HEADS: f"""CREATE TABLE IF NOT EXISTS {HEADS}(
        job_id TEXT NOT NULL PRIMARY KEY,
        constraint_hash TEXT NOT NULL {_hash_check('constraint_hash')},
        initial_graph_hash TEXT,
        active_graph_hash TEXT,
        active_selection_hash TEXT,
        active_repair_hash TEXT,
        revision INTEGER NOT NULL CHECK(revision>0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK(initial_graph_hash IS NULL OR (length(initial_graph_hash)=64 AND initial_graph_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(active_graph_hash IS NULL OR (length(active_graph_hash)=64 AND active_graph_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(active_selection_hash IS NULL OR (length(active_selection_hash)=64 AND active_selection_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(active_repair_hash IS NULL OR (length(active_repair_hash)=64 AND active_repair_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,constraint_hash) REFERENCES {CONTRACTS}(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,initial_graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_graph_hash) REFERENCES {GRAPHS}(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_selection_hash) REFERENCES {SELECTIONS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_repair_hash) REFERENCES {REPAIRS}(job_id,repair_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}

TRIGGER_SQL: dict[str, tuple[str, str]] = {}
for _table in (CONTRACTS, GRAPHS, EDGES, WITNESSES, SELECTIONS, ASSIGNMENTS, REPAIRS):
    TRIGGER_SQL[f"{_table}_no_update"] = (_table, f"CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table} BEGIN SELECT RAISE(ABORT,'{_table}_immutable'); END")
TRIGGER_SQL[f"{HEADS}_cas_only"] = (HEADS, f"""CREATE TRIGGER IF NOT EXISTS {HEADS}_cas_only BEFORE UPDATE ON {HEADS}
    WHEN NEW.job_id!=OLD.job_id OR NEW.constraint_hash!=OLD.constraint_hash OR (OLD.initial_graph_hash IS NOT NULL AND NEW.initial_graph_hash IS NOT OLD.initial_graph_hash)
      OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
    BEGIN SELECT RAISE(ABORT,'{HEADS}_cas_only'); END""")

SCHEMA_STATEMENTS = (*CREATE_TABLE_SQL.values(), *(sql for _, sql in TRIGGER_SQL.values()))


def _normalized(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def _canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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
    CONTRACTS: {
        ("exam_generation_stage3_contract_receipts", ("job_id", "stage3_receipt_hash"), ("job_id", "receipt_hash"), "RESTRICT", "CASCADE", "NONE"),
        ("exam_generation_stage4_fact_receipts", ("job_id", "stage4_receipt_hash"), ("job_id", "receipt_hash"), "RESTRICT", "CASCADE", "NONE"),
    },
    GRAPHS: {(CONTRACTS, ("job_id", "constraint_hash"), ("job_id", "constraint_hash"), "RESTRICT", "CASCADE", "NONE"), (GRAPHS, ("job_id", "base_graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE")},
    EDGES: {(GRAPHS, ("job_id", "graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE")},
    WITNESSES: {(GRAPHS, ("job_id", "graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"), (CONTRACTS, ("job_id", "constraint_hash"), ("job_id", "constraint_hash"), "RESTRICT", "CASCADE", "NONE")},
    SELECTIONS: {(GRAPHS, ("job_id", "graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"), (CONTRACTS, ("job_id", "constraint_hash"), ("job_id", "constraint_hash"), "RESTRICT", "CASCADE", "NONE")},
    ASSIGNMENTS: {(SELECTIONS, ("job_id", "selection_hash"), ("job_id", "selection_hash"), "RESTRICT", "CASCADE", "NONE")},
    REPAIRS: {
        (CONTRACTS, ("job_id", "constraint_hash"), ("job_id", "constraint_hash"), "RESTRICT", "CASCADE", "NONE"),
        (GRAPHS, ("job_id", "base_graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"), (GRAPHS, ("job_id", "repair_graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"),
        (SELECTIONS, ("job_id", "base_selection_hash"), ("job_id", "selection_hash"), "RESTRICT", "CASCADE", "NONE"), (WITNESSES, ("job_id", "witness_hash"), ("job_id", "witness_hash"), "RESTRICT", "CASCADE", "NONE"),
        (SELECTIONS, ("job_id", "replacement_selection_hash"), ("job_id", "selection_hash"), "RESTRICT", "CASCADE", "NONE"),
    },
    HEADS: {
        (CONTRACTS, ("job_id", "constraint_hash"), ("job_id", "constraint_hash"), "RESTRICT", "CASCADE", "NONE"),
        (GRAPHS, ("job_id", "initial_graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"), (GRAPHS, ("job_id", "active_graph_hash"), ("job_id", "graph_hash"), "RESTRICT", "CASCADE", "NONE"),
        (SELECTIONS, ("job_id", "active_selection_hash"), ("job_id", "selection_hash"), "RESTRICT", "CASCADE", "NONE"), (REPAIRS, ("job_id", "active_repair_hash"), ("job_id", "repair_hash"), "RESTRICT", "CASCADE", "NONE"),
    },
}


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    receipt_specs = (
        (CONTRACTS, ("job_id", "receipt_contract_version", "constraint_contract_version", "stage3_contract_hash", "stage3_receipt_hash", "candidate_facts_hash", "stage4_receipt_hash", "projection_identity_hash", "constraint_hash", "question_count")),
        (GRAPHS, ("graph_hash", "job_id", "graph_contract_version", "constraint_hash", "candidate_facts_hash", "graph_mode", "base_graph_hash", "edge_count", "candidate_count", "expanded_slot_ids_hash")),
        (WITNESSES, ("job_id", "graph_hash", "constraint_hash", "witness_hash")),
        (SELECTIONS, ("selection_hash", "job_id", "graph_hash", "constraint_hash", "selection_contract_version", "score_identity_hash", "assignment_count", "total_utility")),
    )
    for table, fields in receipt_specs:
        for row in db.execute(f"SELECT * FROM {table}"):
            value = dict(row)
            if not hmac.compare_digest(str(value["receipt_hash"]), _canonical_hash({field: value[field] for field in fields})):
                issues.append(f"版本 43 的表 {table} 存在回执哈希漂移")
                break
    payload_tables = ((CONTRACTS, "payload_json", "constraint_hash"), (GRAPHS, "payload_json", "graph_hash"))
    for table, payload_column, hash_column in payload_tables:
        for row in db.execute(f"SELECT {payload_column},{hash_column} FROM {table}"):
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError):
                issues.append(f"版本 43 的表 {table} 存在无效 JSON")
                break
            if not hmac.compare_digest(str(row[1]), _canonical_hash(payload)):
                issues.append(f"版本 43 的表 {table} 存在 payload 哈希漂移")
                break
    for row in db.execute(f"SELECT payload_json,witness_hash FROM {WITNESSES}"):
        try: payload = json.loads(row[0])
        except (TypeError, ValueError):
            issues.append(f"版本 43 的表 {WITNESSES} 存在无效 JSON"); break
        body = dict(payload) if isinstance(payload, dict) else {}; stored = body.pop("witness_hash", None)
        if stored != row[1] or not hmac.compare_digest(str(row[1]), _canonical_hash(body)):
            issues.append(f"版本 43 的表 {WITNESSES} 存在 payload 哈希漂移"); break
    for row in db.execute(f"SELECT payload_json,selection_hash FROM {SELECTIONS}"):
        try: payload = json.loads(row[0])
        except (TypeError, ValueError):
            issues.append(f"版本 43 的表 {SELECTIONS} 存在无效 JSON"); break
        try:
            identity = {key: payload[key] for key in ("contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash", "constraint_hash", "score_identity_hash", "assignments")}
        except (KeyError, TypeError):
            identity = None
        if identity is None or payload.get("selection_hash") != row[1] or not hmac.compare_digest(str(row[1]), _canonical_hash(identity)):
            issues.append(f"版本 43 的表 {SELECTIONS} 存在 payload 哈希漂移"); break
    for table, json_column, hash_column in ((EDGES, "edge_json", "edge_hash"), (ASSIGNMENTS, "assignment_json", "assignment_hash")):
        for row in db.execute(f"SELECT {json_column},{hash_column} FROM {table}"):
            try:
                payload = json.loads(row[0])
            except (TypeError, ValueError):
                issues.append(f"版本 43 的表 {table} 存在无效 JSON")
                break
            if not hmac.compare_digest(str(row[1]), _canonical_hash(payload)):
                issues.append(f"版本 43 的表 {table} 存在条目哈希漂移")
                break
    for row in db.execute(f"SELECT receipt_json,repair_hash FROM {REPAIRS}"):
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            issues.append(f"版本 43 的表 {REPAIRS} 存在无效 JSON")
            break
        body = dict(payload) if isinstance(payload, dict) else {}
        stored = body.pop("repair_hash", None)
        if stored != row[1] or not hmac.compare_digest(str(row[1]), _canonical_hash(body)):
            issues.append(f"版本 43 的表 {REPAIRS} 存在修复哈希漂移")
            break
    for row in db.execute(f"SELECT job_id,graph_hash,edge_count,candidate_count,payload_json FROM {GRAPHS}"):
        try:
            payload = json.loads(row[4])
        except (TypeError, ValueError):
            continue
        edge_count = db.execute(f"SELECT COUNT(*) FROM {EDGES} WHERE job_id=? AND graph_hash=?", (row[0], row[1])).fetchone()[0]
        if int(edge_count) != int(row[2]) or len(payload.get("candidates", {})) != int(row[3]):
            issues.append(f"版本 43 的表 {GRAPHS} 存在边或候选计数漂移")
            break
    for row in db.execute(f"SELECT job_id,selection_hash,assignment_count FROM {SELECTIONS}"):
        count = db.execute(f"SELECT COUNT(*) FROM {ASSIGNMENTS} WHERE job_id=? AND selection_hash=?", (row[0], row[1])).fetchone()[0]
        if int(count) != int(row[2]):
            issues.append(f"版本 43 的表 {SELECTIONS} 存在 assignment 计数漂移")
            break
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None:
            issues.append(f"版本 43 缺少表 {table}")
            continue
        if str(row[0]) != "table" or _normalized(row[1]) != _normalized(sql):
            issues.append(f"版本 43 的表 {table} 定义不兼容")
        if _foreign_keys(db, table) != EXPECTED_FKS[table]:
            issues.append(f"版本 43 的表 {table} 外键定义不兼容")
        expected = {name for name, (owner, _) in TRIGGER_SQL.items() if owner == table}
        actual = {str(item[0]) for item in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if actual != expected:
            issues.append(f"版本 43 的表 {table} 触发器集合不兼容")
    for name, (table, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,tbl_name,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is None or str(row[0]) != "trigger" or str(row[1]) != table or _normalized(row[2]) != _normalized(sql):
            issues.append(f"版本 43 的触发器 {name} 定义不兼容")
    if not issues:
        issues.extend(_row_issues(db))
    return issues


def _existing_conflicts(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for name, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "table" or _normalized(row[1]) != _normalized(sql)):
            issues.append(f"版本 43 的对象 {name} 定义不兼容")
    for name, (_, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "trigger" or _normalized(row[1]) != _normalized(sql)):
            issues.append(f"版本 43 的对象 {name} 定义不兼容")
    return issues


def migrate_v42_to_v43(db: sqlite3.Connection) -> None:
    conflicts = _existing_conflicts(db)
    if conflicts:
        raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    issues = schema_issues(db)
    if issues:
        raise RuntimeError("；".join(issues))


__all__ = ["ASSIGNMENTS", "CONTRACTS", "EDGES", "GRAPHS", "HEADS", "REPAIRS", "SELECTIONS", "WITNESSES", "SCHEMA_STATEMENTS", "migrate_v42_to_v43", "schema_issues"]
