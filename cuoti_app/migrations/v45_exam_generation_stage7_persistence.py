"""Schema 45: immutable, job-scoped Stage-7 persistence and CAS head."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import defaultdict


ROOTS = "exam_generation_stage7_context_receipts"
EVIDENCE = "exam_generation_stage7_evidence_receipts"
DIFFICULTY_REFS = "exam_generation_stage7_difficulty_references"
PAPERS = "exam_generation_stage7_papers"
RECEIPTS = "exam_generation_stage7_receipts"
REQUESTS = "exam_generation_stage7_requests"
RESPONSES = "exam_generation_stage7_responses"
RESULTS = "exam_generation_stage7_results"
HEADS = "exam_generation_stage7_heads"


def _hc(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = {
    ROOTS: f"""CREATE TABLE IF NOT EXISTS {ROOTS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')},
        context_version TEXT NOT NULL CHECK(context_version='past-v3-stage7-persistence-context-v1'),
        stage6_checkpoint_hash TEXT NOT NULL {_hc('stage6_checkpoint_hash')},
        selection_hash TEXT NOT NULL {_hc('selection_hash')}, candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')}, graph_hash TEXT NOT NULL {_hc('graph_hash')},
        score_set_hash TEXT NOT NULL {_hc('score_set_hash')}, local_evidence_hash TEXT NOT NULL {_hc('local_evidence_hash')},
        global_evidence_hash TEXT NOT NULL {_hc('global_evidence_hash')}, budget_hash TEXT NOT NULL {_hc('budget_hash')},
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,context_hash), UNIQUE(job_id),
        FOREIGN KEY(job_id,stage6_checkpoint_hash) REFERENCES exam_generation_stage6_checkpoints(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,selection_hash) REFERENCES exam_generation_stage6_selection_references(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,candidate_facts_hash) REFERENCES exam_generation_stage4_fact_receipts(job_id,candidate_facts_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,constraint_hash) REFERENCES exam_generation_stage5_constraint_receipts(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,score_set_hash) REFERENCES exam_generation_stage6_score_receipts(job_id,graph_hash,score_set_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    EVIDENCE: f"""CREATE TABLE IF NOT EXISTS {EVIDENCE}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, evidence_kind TEXT NOT NULL CHECK(evidence_kind IN ('local_answer','global_constraint')),
        evidence_version TEXT NOT NULL, evidence_hash TEXT NOT NULL {_hc('evidence_hash')}, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,evidence_kind), UNIQUE(job_id,evidence_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    DIFFICULTY_REFS: f"""CREATE TABLE IF NOT EXISTS {DIFFICULTY_REFS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, ordinal INTEGER NOT NULL CHECK(ordinal>=0),
        flight_hash TEXT NOT NULL {_hc('flight_hash')}, result_hash TEXT NOT NULL {_hc('result_hash')},
        component_hash TEXT NOT NULL {_hc('component_hash')}, input_facts_hash TEXT NOT NULL {_hc('input_facts_hash')},
        classification_receipt_hash TEXT NOT NULL {_hc('classification_receipt_hash')}, verification_receipt_hash TEXT NOT NULL {_hc('verification_receipt_hash')},
        reference_hash TEXT NOT NULL {_hc('reference_hash')}, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,result_hash), UNIQUE(job_id,ordinal), UNIQUE(job_id,reference_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(flight_hash,result_hash) REFERENCES exam_generation_stage4_difficulty_results(flight_hash,result_hash) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    PAPERS: f"""CREATE TABLE IF NOT EXISTS {PAPERS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, paper_kind TEXT NOT NULL CHECK(paper_kind IN ('pre','final')),
        paper_hash TEXT NOT NULL {_hc('paper_hash')}, item_count INTEGER NOT NULL CHECK(item_count BETWEEN 1 AND 256),
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id,paper_kind), UNIQUE(job_id,paper_kind,paper_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RECEIPTS: f"""CREATE TABLE IF NOT EXISTS {RECEIPTS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, receipt_kind TEXT NOT NULL CHECK(receipt_kind IN ('pre','final')),
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, receipt_version TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,receipt_kind), UNIQUE(job_id,receipt_kind,receipt_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    REQUESTS: f"""CREATE TABLE IF NOT EXISTS {REQUESTS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, call_kind TEXT NOT NULL CHECK(call_kind IN ('quality','adaptation','verification')),
        protocol_version TEXT NOT NULL, request_hash TEXT NOT NULL {_hc('request_hash')}, size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 2000000),
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id,call_kind), UNIQUE(job_id,request_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RESPONSES: f"""CREATE TABLE IF NOT EXISTS {RESPONSES}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, call_kind TEXT NOT NULL CHECK(call_kind IN ('quality','adaptation','verification')),
        request_hash TEXT NOT NULL {_hc('request_hash')}, outcome TEXT NOT NULL CHECK(outcome IN ('received','timeout','error')),
        response_hash TEXT, disposition_hash TEXT NOT NULL {_hc('disposition_hash')}, size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 0 AND 2000000), response_text TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,call_kind), UNIQUE(job_id,disposition_hash), UNIQUE(job_id,response_hash),
        CHECK((outcome='received' AND length(response_hash)=64 AND response_hash NOT GLOB '*[^0-9a-f]*' AND response_text IS NOT NULL AND size_bytes>0)
           OR (outcome IN ('timeout','error') AND response_hash IS NULL AND response_text IS NULL AND size_bytes=0)),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,call_kind) REFERENCES {REQUESTS}(job_id,call_kind) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RESULTS: f"""CREATE TABLE IF NOT EXISTS {RESULTS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')}, result_hash TEXT NOT NULL {_hc('result_hash')},
        pre_adaptation_hash TEXT NOT NULL {_hc('pre_adaptation_hash')}, final_exam_hash TEXT NOT NULL {_hc('final_exam_hash')},
        pre_receipt_hash TEXT NOT NULL {_hc('pre_receipt_hash')}, final_receipt_hash TEXT NOT NULL {_hc('final_receipt_hash')},
        pre_paper_kind TEXT NOT NULL DEFAULT 'pre' CHECK(pre_paper_kind='pre'), final_paper_kind TEXT NOT NULL DEFAULT 'final' CHECK(final_paper_kind='final'),
        pre_receipt_kind TEXT NOT NULL DEFAULT 'pre' CHECK(pre_receipt_kind='pre'), final_receipt_kind TEXT NOT NULL DEFAULT 'final' CHECK(final_receipt_kind='final'),
        adapted_position INTEGER, single_item_fallback INTEGER NOT NULL CHECK(single_item_fallback IN (0,1)), payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,result_hash), UNIQUE(job_id),
        CHECK(adapted_position IS NULL OR adapted_position BETWEEN 1 AND 256),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,pre_paper_kind,pre_adaptation_hash) REFERENCES {PAPERS}(job_id,paper_kind,paper_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,final_paper_kind,final_exam_hash) REFERENCES {PAPERS}(job_id,paper_kind,paper_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,pre_receipt_kind,pre_receipt_hash) REFERENCES {RECEIPTS}(job_id,receipt_kind,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,final_receipt_kind,final_receipt_hash) REFERENCES {RECEIPTS}(job_id,receipt_kind,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    HEADS: f"""CREATE TABLE IF NOT EXISTS {HEADS}(
        job_id TEXT NOT NULL PRIMARY KEY, context_hash TEXT NOT NULL {_hc('context_hash')}, stage6_checkpoint_hash TEXT NOT NULL {_hc('stage6_checkpoint_hash')},
        quality_request_hash TEXT, quality_response_hash TEXT, adaptation_request_hash TEXT, adaptation_response_hash TEXT,
        verification_request_hash TEXT, verification_response_hash TEXT, result_hash TEXT, checkpoint_hash TEXT,
        revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(quality_request_hash IS NULL OR (length(quality_request_hash)=64 AND quality_request_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(quality_response_hash IS NULL OR (length(quality_response_hash)=64 AND quality_response_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(adaptation_request_hash IS NULL OR (length(adaptation_request_hash)=64 AND adaptation_request_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(adaptation_response_hash IS NULL OR (length(adaptation_response_hash)=64 AND adaptation_response_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(verification_request_hash IS NULL OR (length(verification_request_hash)=64 AND verification_request_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(verification_response_hash IS NULL OR (length(verification_response_hash)=64 AND verification_response_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(result_hash IS NULL OR (length(result_hash)=64 AND result_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(checkpoint_hash IS NULL OR (length(checkpoint_hash)=64 AND checkpoint_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,result_hash) REFERENCES {RESULTS}(job_id,result_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,checkpoint_hash) REFERENCES exam_generation_stage6_checkpoints(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}

TRIGGER_SQL: dict[str, tuple[str, str]] = {}
for _table in (ROOTS, EVIDENCE, DIFFICULTY_REFS, PAPERS, RECEIPTS, REQUESTS, RESPONSES, RESULTS):
    TRIGGER_SQL[f"{_table}_no_update"] = (_table, f"CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table} BEGIN SELECT RAISE(ABORT,'{_table}_immutable'); END")
TRIGGER_SQL[f"{HEADS}_cas_only"] = (HEADS, f"""CREATE TRIGGER IF NOT EXISTS {HEADS}_cas_only BEFORE UPDATE ON {HEADS}
    WHEN NEW.job_id!=OLD.job_id OR NEW.context_hash!=OLD.context_hash OR NEW.stage6_checkpoint_hash!=OLD.stage6_checkpoint_hash OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
    BEGIN SELECT RAISE(ABORT,'{HEADS}_cas_only'); END""")
SCHEMA_STATEMENTS = (*CREATE_TABLE_SQL.values(), *(sql for _, sql in TRIGGER_SQL.values()))


def _norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _foreign_keys(db: sqlite3.Connection, table: str) -> set[tuple]:
    grouped: dict[int, list[tuple]] = defaultdict(list)
    for row in db.execute(f'PRAGMA foreign_key_list("{table}")'):
        grouped[int(row[0])].append(tuple(row))
    result = set()
    for rows in grouped.values():
        rows.sort(key=lambda item: int(item[1]))
        result.add((str(rows[0][2]), tuple(str(r[3]) for r in rows), tuple(str(r[4]) for r in rows), str(rows[0][5]), str(rows[0][6]), str(rows[0][7])))
    return result


def _expected_fks() -> dict[str, set[tuple]]:
    probe = sqlite3.connect(":memory:")
    probe.execute("PRAGMA foreign_keys=OFF")
    for sql in CREATE_TABLE_SQL.values():
        probe.execute(sql)
    result = {table: _foreign_keys(probe, table) for table in CREATE_TABLE_SQL}
    probe.close()
    return result


EXPECTED_FKS = _expected_fks()


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, hash_column in ((ROOTS, "context_hash"), (PAPERS, "paper_hash"), (REQUESTS, "request_hash"), (RESULTS, "result_hash")):
        for raw, expected in db.execute(f"SELECT payload_json,{hash_column} FROM {table}"):
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                issues.append(f"版本 45 的表 {table} 存在无效 JSON"); break
            if table == RESULTS:
                valid = hmac.compare_digest(str(expected), _digest(payload))
            else:
                valid = hmac.compare_digest(str(expected), _digest(payload))
            if not valid:
                issues.append(f"版本 45 的表 {table} 存在 payload 哈希漂移"); break
    cross_job = db.execute(
        f"SELECT 1 FROM {DIFFICULTY_REFS} ref "
        "LEFT JOIN exam_generation_stage4_difficulty_results result "
        "ON result.flight_hash=ref.flight_hash AND result.result_hash=ref.result_hash "
        "LEFT JOIN exam_generation_stage4_difficulty_flights flight ON flight.flight_hash=ref.flight_hash "
        "WHERE result.flight_hash IS NULL OR flight.flight_hash IS NULL OR flight.claim_job_id!=ref.job_id LIMIT 1"
    ).fetchone()
    if cross_job is not None:
        issues.append("版本 45 的难度引用跨越 job-scoped flight 或缺少 v42 完整结果")
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None:
            issues.append(f"版本 45 缺少表 {table}"); continue
        if str(row[0]) != "table" or _norm(row[1]) != _norm(sql): issues.append(f"版本 45 的表 {table} 定义不兼容")
        if _foreign_keys(db, table) != EXPECTED_FKS[table]: issues.append(f"版本 45 的表 {table} 外键定义不兼容")
        expected = {name for name, (owner, _) in TRIGGER_SQL.items() if owner == table}
        actual = {str(x[0]) for x in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if actual != expected: issues.append(f"版本 45 的表 {table} 触发器集合不兼容")
    if not issues: issues.extend(_row_issues(db))
    return issues


def _conflicts(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for name, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "table" or _norm(row[1]) != _norm(sql)): issues.append(f"版本 45 的对象 {name} 定义不兼容")
    for name, (_, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "trigger" or _norm(row[1]) != _norm(sql)): issues.append(f"版本 45 的对象 {name} 定义不兼容")
    return issues


def migrate_v44_to_v45(db: sqlite3.Connection) -> None:
    conflicts = _conflicts(db)
    if conflicts: raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS: db.execute(statement)
    issues = schema_issues(db)
    if issues: raise RuntimeError("；".join(issues))


__all__ = ["CREATE_TABLE_SQL", "DIFFICULTY_REFS", "EVIDENCE", "HEADS", "PAPERS", "RECEIPTS", "REQUESTS", "RESPONSES", "RESULTS", "ROOTS", "SCHEMA_STATEMENTS", "migrate_v44_to_v45", "schema_issues"]
