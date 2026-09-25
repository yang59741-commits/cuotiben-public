"""Schema 46: immutable native past-v3 publication facts and CAS head."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3


REQUESTS = "exam_generation_v3_publish_requests"
PUBLICATIONS = "exam_generation_v3_publications"
RECEIPTS = "exam_generation_v3_publication_receipts"
USAGE = "exam_generation_v3_candidate_usage"
HEADS = "exam_generation_v3_publish_heads"


def _hc(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = {
    REQUESTS: f"""CREATE TABLE IF NOT EXISTS {REQUESTS}(
        job_id TEXT NOT NULL PRIMARY KEY,
        request_version TEXT NOT NULL CHECK(request_version='past-v3-native-publish-request-v1'),
        binding_hash TEXT NOT NULL {_hc('binding_hash')},
        stage3_contract_hash TEXT NOT NULL {_hc('stage3_contract_hash')},
        stage7_result_hash TEXT NOT NULL {_hc('stage7_result_hash')},
        stage7_checkpoint_hash TEXT NOT NULL {_hc('stage7_checkpoint_hash')},
        final_exam_hash TEXT NOT NULL {_hc('final_exam_hash')},
        draft_hash TEXT NOT NULL {_hc('draft_hash')},
        request_hash TEXT NOT NULL {_hc('request_hash')},
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(job_id,request_hash),
        FOREIGN KEY(job_id,binding_hash) REFERENCES exam_generation_attempt_bindings(job_id,binding_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,stage7_result_hash) REFERENCES exam_generation_stage7_results(job_id,result_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,stage7_checkpoint_hash) REFERENCES exam_generation_stage6_checkpoints(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    PUBLICATIONS: f"""CREATE TABLE IF NOT EXISTS {PUBLICATIONS}(
        job_id TEXT NOT NULL PRIMARY KEY, request_hash TEXT NOT NULL {_hc('request_hash')},
        publication_version TEXT NOT NULL CHECK(publication_version='past-v3-native-publication-v1'),
        exam_id INTEGER NOT NULL UNIQUE, artifact_hash TEXT NOT NULL {_hc('artifact_hash')},
        publication_hash TEXT NOT NULL {_hc('publication_hash')}, payload_json TEXT NOT NULL, published_at TEXT NOT NULL,
        UNIQUE(job_id,publication_hash),
        FOREIGN KEY(job_id,request_hash) REFERENCES {REQUESTS}(job_id,request_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(exam_id) REFERENCES exams(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    RECEIPTS: f"""CREATE TABLE IF NOT EXISTS {RECEIPTS}(
        job_id TEXT NOT NULL PRIMARY KEY, publication_hash TEXT NOT NULL {_hc('publication_hash')},
        receipt_version TEXT NOT NULL CHECK(receipt_version='past-v3-native-publication-receipt-v1'),
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,publication_hash) REFERENCES {PUBLICATIONS}(job_id,publication_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    USAGE: f"""CREATE TABLE IF NOT EXISTS {USAGE}(
        job_id TEXT NOT NULL, exam_id INTEGER NOT NULL, position INTEGER NOT NULL CHECK(position BETWEEN 1 AND 256),
        candidate_id TEXT NOT NULL, content_hash TEXT NOT NULL {_hc('content_hash')},
        usage_hash TEXT NOT NULL {_hc('usage_hash')}, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,candidate_id), UNIQUE(job_id,position), UNIQUE(job_id,usage_hash),
        FOREIGN KEY(job_id) REFERENCES {PUBLICATIONS}(job_id) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(exam_id) REFERENCES exams(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    HEADS: f"""CREATE TABLE IF NOT EXISTS {HEADS}(
        job_id TEXT NOT NULL PRIMARY KEY, request_hash TEXT NOT NULL {_hc('request_hash')},
        publication_hash TEXT, receipt_hash TEXT,
        state TEXT NOT NULL CHECK(state IN ('request_frozen','published_pending_receipt','completed')),
        revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(publication_hash IS NULL OR (length(publication_hash)=64 AND publication_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(receipt_hash IS NULL OR (length(receipt_hash)=64 AND receipt_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK((state='request_frozen' AND publication_hash IS NULL AND receipt_hash IS NULL)
           OR (state='published_pending_receipt' AND publication_hash IS NOT NULL AND receipt_hash IS NULL)
           OR (state='completed' AND publication_hash IS NOT NULL AND receipt_hash IS NOT NULL)),
        FOREIGN KEY(job_id,request_hash) REFERENCES {REQUESTS}(job_id,request_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,publication_hash) REFERENCES {PUBLICATIONS}(job_id,publication_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,receipt_hash) REFERENCES {RECEIPTS}(job_id,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}

TRIGGER_SQL: dict[str, tuple[str, str]] = {}
for _table in (REQUESTS, PUBLICATIONS, RECEIPTS, USAGE):
    _name = f"{_table}_no_update"
    TRIGGER_SQL[_name] = (_table, f"CREATE TRIGGER IF NOT EXISTS {_name} BEFORE UPDATE ON {_table} BEGIN SELECT RAISE(ABORT,'{_table}_immutable'); END")
TRIGGER_SQL[f"{HEADS}_cas_only"] = (HEADS, f"""CREATE TRIGGER IF NOT EXISTS {HEADS}_cas_only BEFORE UPDATE ON {HEADS}
    WHEN NEW.job_id!=OLD.job_id OR NEW.request_hash!=OLD.request_hash OR NEW.created_at!=OLD.created_at
      OR NEW.revision!=OLD.revision+1
      OR (OLD.state='request_frozen' AND NEW.state!='published_pending_receipt')
      OR (OLD.state='published_pending_receipt' AND NEW.state!='completed')
      OR OLD.state='completed'
    BEGIN SELECT RAISE(ABORT,'{HEADS}_cas_only'); END""")

SCHEMA_STATEMENTS = (*CREATE_TABLE_SQL.values(), *(sql for _, sql in TRIGGER_SQL.values()))


def _norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "")


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, column in ((REQUESTS, "request_hash"), (PUBLICATIONS, "publication_hash"), (RECEIPTS, "receipt_hash"), (USAGE, "usage_hash")):
        for raw, expected in db.execute(f"SELECT payload_json,{column} FROM {table}"):
            try:
                valid = hmac.compare_digest(str(expected), _digest(json.loads(raw)))
            except (TypeError, ValueError):
                valid = False
            if not valid:
                issues.append(f"版本 46 的表 {table} 存在 payload 哈希漂移")
                break
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None:
            issues.append(f"版本 46 缺少表 {table}")
        elif str(row[0]) != "table" or _norm(row[1]) != _norm(sql):
            issues.append(f"版本 46 的表 {table} 定义不兼容")
        expected = {name for name, (owner, _) in TRIGGER_SQL.items() if owner == table}
        actual = {str(r[0]) for r in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if row is not None and actual != expected:
            issues.append(f"版本 46 的表 {table} 触发器集合不兼容")
    if not issues:
        issues.extend(_row_issues(db))
    return issues


def migrate_v45_to_v46(db: sqlite3.Connection) -> None:
    for name, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "table" or _norm(row[1]) != _norm(sql)):
            raise RuntimeError(f"版本 46 的对象 {name} 定义不兼容")
    for name, (_, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "trigger" or _norm(row[1]) != _norm(sql)):
            raise RuntimeError(f"版本 46 的对象 {name} 定义不兼容")
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)
    issues = schema_issues(db)
    if issues:
        raise RuntimeError("；".join(issues))


__all__ = ["HEADS", "PUBLICATIONS", "RECEIPTS", "REQUESTS", "SCHEMA_STATEMENTS", "USAGE", "migrate_v45_to_v46", "schema_issues"]
