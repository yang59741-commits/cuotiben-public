"""Schema 49: dual-read frozen Stage-7 v1 and answer-free Stage-7 v2."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from .v45_exam_generation_stage7_persistence import (
    CREATE_TABLE_SQL as V45_CREATE_TABLE_SQL,
    EXPECTED_FKS as V45_EXPECTED_FKS,
    TRIGGER_SQL as V45_TRIGGER_SQL,
    EVIDENCE,
    ROOTS,
    _foreign_keys as _v45_foreign_keys,
    _row_issues as _v45_row_issues,
)


REBUILD_ROOTS = f"{ROOTS}_v49"
V1_CONTEXT = "past-v3-stage7-persistence-context-v1"
V2_CONTEXT = "past-v3-stage7-persistence-context-v2"


def _hc(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


def _create_roots_sql(name: str) -> str:
    return f"""CREATE TABLE {name}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')},
        context_version TEXT NOT NULL CHECK(context_version IN ('{V1_CONTEXT}','{V2_CONTEXT}')),
        stage6_checkpoint_hash TEXT NOT NULL {_hc('stage6_checkpoint_hash')},
        selection_hash TEXT NOT NULL {_hc('selection_hash')}, candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')}, graph_hash TEXT NOT NULL {_hc('graph_hash')},
        score_set_hash TEXT NOT NULL {_hc('score_set_hash')}, local_evidence_hash TEXT,
        global_evidence_hash TEXT NOT NULL {_hc('global_evidence_hash')}, budget_hash TEXT NOT NULL {_hc('budget_hash')},
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,context_hash), UNIQUE(job_id),
        CHECK((context_version='{V1_CONTEXT}' AND length(local_evidence_hash)=64 AND local_evidence_hash NOT GLOB '*[^0-9a-f]*')
           OR (context_version='{V2_CONTEXT}' AND local_evidence_hash IS NULL)),
        FOREIGN KEY(job_id,stage6_checkpoint_hash) REFERENCES exam_generation_stage6_checkpoints(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,selection_hash) REFERENCES exam_generation_stage6_selection_references(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,candidate_facts_hash) REFERENCES exam_generation_stage4_fact_receipts(job_id,candidate_facts_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,constraint_hash) REFERENCES exam_generation_stage5_constraint_receipts(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,score_set_hash) REFERENCES exam_generation_stage6_score_receipts(job_id,graph_hash,score_set_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )"""


CREATE_ROOTS_SQL = _create_roots_sql(ROOTS)
CREATE_REBUILD_SQL = _create_roots_sql(REBUILD_ROOTS)
ROOT_IMMUTABLE_SQL = f"CREATE TRIGGER {ROOTS}_no_update BEFORE UPDATE ON {ROOTS} BEGIN SELECT RAISE(ABORT,'{ROOTS}_immutable'); END"
V2_NO_LOCAL_SQL = f"""CREATE TRIGGER {EVIDENCE}_v2_no_local_answer BEFORE INSERT ON {EVIDENCE}
    WHEN NEW.evidence_kind='local_answer' AND EXISTS(
        SELECT 1 FROM {ROOTS} r WHERE r.job_id=NEW.job_id AND r.context_hash=NEW.context_hash
          AND r.context_version='{V2_CONTEXT}')
    BEGIN SELECT RAISE(ABORT,'stage7_v2_local_answer_forbidden'); END"""


def _norm(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper().replace("IFNOTEXISTS", "").replace('"', "")


def _master_sql(db: sqlite3.Connection, kind: str, name: str) -> str | None:
    row = db.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?", (kind, name)).fetchone()
    return None if row is None else str(row[0])


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for row in db.execute(f"SELECT job_id,context_hash,context_version,local_evidence_hash,payload_json FROM {ROOTS}"):
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            issues.append("版本 49 的 Stage7 上下文存在无效 JSON")
            break
        if _digest(payload) != str(row["context_hash"]) or payload.get("version") != row["context_version"]:
            issues.append("版本 49 的 Stage7 上下文哈希或版本漂移")
            break
        evidence_kinds = {str(x[0]) for x in db.execute(f"SELECT evidence_kind FROM {EVIDENCE} WHERE job_id=?", (row["job_id"],))}
        if row["context_version"] == V1_CONTEXT:
            if row["local_evidence_hash"] is None or evidence_kinds != {"local_answer", "global_constraint"}:
                issues.append("版本 49 的历史 Stage7 v1 证据不完整")
                break
        elif row["local_evidence_hash"] is not None or "local_evidence_hash" in payload or evidence_kinds != {"global_constraint"}:
            issues.append("版本 49 的 Stage7 v2 写入了答案事实")
            break
    cross_job = db.execute(
        "SELECT 1 FROM exam_generation_stage7_difficulty_references ref "
        "LEFT JOIN exam_generation_stage4_difficulty_results result "
        "ON result.flight_hash=ref.flight_hash AND result.result_hash=ref.result_hash "
        "LEFT JOIN exam_generation_stage4_difficulty_flights flight ON flight.flight_hash=ref.flight_hash "
        "WHERE result.flight_hash IS NULL OR flight.flight_hash IS NULL OR flight.claim_job_id!=ref.job_id LIMIT 1"
    ).fetchone()
    if cross_job is not None:
        issues.append("版本 49 的难度引用跨越 job-scoped flight 或缺少完整结果")
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, sql in V45_CREATE_TABLE_SQL.items():
        if table == ROOTS:
            continue
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None:
            issues.append(f"版本 49 缺少 Stage7 子表 {table}")
            continue
        if str(row[0]) != "table" or _norm(row[1]) != _norm(sql):
            issues.append(f"版本 49 的 Stage7 子表 {table} 定义不兼容")
        if _v45_foreign_keys(db, table) != V45_EXPECTED_FKS[table]:
            issues.append(f"版本 49 的 Stage7 子表 {table} 外键定义不兼容")
        expected_triggers = {name for name, (owner, _) in V45_TRIGGER_SQL.items() if owner == table}
        if table == EVIDENCE:
            expected_triggers.add(f"{EVIDENCE}_v2_no_local_answer")
        actual_triggers = {str(item[0]) for item in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if actual_triggers != expected_triggers:
            issues.append(f"版本 49 的 Stage7 子表 {table} 触发器集合不兼容")
    if _norm(_master_sql(db, "table", ROOTS)) != _norm(CREATE_ROOTS_SQL):
        issues.append("版本 49 的 Stage7 上下文表定义不兼容")
        return issues
    expected = {
        f"{ROOTS}_no_update": ROOT_IMMUTABLE_SQL,
        f"{EVIDENCE}_v2_no_local_answer": V2_NO_LOCAL_SQL,
    }
    for name, sql in expected.items():
        if _norm(_master_sql(db, "trigger", name)) != _norm(sql):
            issues.append(f"版本 49 缺少或错误定义触发器 {name}")
    if issues:
        return issues
    issues.extend(_v45_row_issues(db))
    issues.extend(_row_issues(db))
    return issues


def migrate_v48_to_v49(db: sqlite3.Connection) -> None:
    if int(db.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise RuntimeError("v49 Stage7 context rebuild requires foreign_keys=OFF")
    if _norm(_master_sql(db, "table", ROOTS)) == _norm(CREATE_ROOTS_SQL):
        db.execute(ROOT_IMMUTABLE_SQL.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))
        db.execute(V2_NO_LOCAL_SQL.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))
    else:
        from .v45_exam_generation_stage7_persistence import CREATE_TABLE_SQL
        if _norm(_master_sql(db, "table", ROOTS)) != _norm(CREATE_TABLE_SQL[ROOTS]):
            raise RuntimeError("v49 Stage7 context source definition is incompatible with v45")
        before = tuple(db.execute(f"SELECT job_id,context_hash,payload_json FROM {ROOTS} ORDER BY job_id"))
        db.execute(f"DROP TRIGGER {ROOTS}_no_update")
        db.execute(CREATE_REBUILD_SQL)
        columns = "job_id,context_hash,context_version,stage6_checkpoint_hash,selection_hash,candidate_facts_hash,constraint_hash,graph_hash,score_set_hash,local_evidence_hash,global_evidence_hash,budget_hash,payload_json,created_at"
        db.execute(f"INSERT INTO {REBUILD_ROOTS}({columns}) SELECT {columns} FROM {ROOTS}")
        db.execute(f"DROP TABLE {ROOTS}")
        db.execute(f"ALTER TABLE {REBUILD_ROOTS} RENAME TO {ROOTS}")
        db.execute(ROOT_IMMUTABLE_SQL)
        db.execute(V2_NO_LOCAL_SQL)
        after = tuple(db.execute(f"SELECT job_id,context_hash,payload_json FROM {ROOTS} ORDER BY job_id"))
        if before != after:
            raise RuntimeError("v49 Stage7 context rebuild changed historical rows or hashes")
    issues = schema_issues(db)
    if issues or str(db.execute("PRAGMA quick_check").fetchone()[0]) != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("v49 Stage7 post-migration validation failed" + ("：" + "；".join(issues) if issues else ""))


migrate_v48_to_v49.requires_foreign_keys_off = True

__all__ = ["CREATE_ROOTS_SQL", "V1_CONTEXT", "V2_CONTEXT", "migrate_v48_to_v49", "schema_issues"]
