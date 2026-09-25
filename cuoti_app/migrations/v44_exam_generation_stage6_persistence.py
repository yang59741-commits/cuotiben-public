"""Schema 44: immutable Stage-6 receipts, version chains and one CAS head."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import defaultdict


ROOTS = "exam_generation_stage6_context_receipts"
MATERIALS = "exam_generation_stage6_material_receipts"
WINDOWS = "exam_generation_stage6_scoring_windows"
REQUESTS = "exam_generation_stage6_scoring_requests"
RETRY_POLICIES = "exam_generation_stage6_retry_policies"
RETRY_EVENTS = "exam_generation_stage6_retry_events"
RESPONSES = "exam_generation_stage6_scoring_responses"
SCORES = "exam_generation_stage6_score_receipts"
LEDGER_STATES = "exam_generation_stage6_rejection_ledger_states"
LEDGER_ENTRIES = "exam_generation_stage6_rejection_ledger_entries"
SELECTION_REFS = "exam_generation_stage6_selection_references"
CHECKPOINTS = "exam_generation_stage6_checkpoints"
LEGACY_DECISIONS = "exam_generation_stage6_legacy_migration_decisions"
HEADS = "exam_generation_stage6_heads"


def _hc(column: str) -> str:
    return f"CHECK(length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*')"


CREATE_TABLE_SQL = {
    ROOTS: f"""CREATE TABLE IF NOT EXISTS {ROOTS}(
        job_id TEXT NOT NULL, context_hash TEXT NOT NULL {_hc('context_hash')},
        context_version TEXT NOT NULL CHECK(context_version='past-v3-stage6-context-v1'),
        stage3_contract_hash TEXT NOT NULL {_hc('stage3_contract_hash')}, stage3_receipt_hash TEXT NOT NULL {_hc('stage3_receipt_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')}, stage5_receipt_hash TEXT NOT NULL {_hc('stage5_receipt_hash')},
        input_payload_hash TEXT NOT NULL {_hc('input_payload_hash')}, single_paper_contract_version TEXT NOT NULL,
        single_paper_contract_hash TEXT NOT NULL {_hc('single_paper_contract_hash')}, recent_snapshot_hash TEXT NOT NULL {_hc('recent_snapshot_hash')},
        time_policy_version TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,context_hash), UNIQUE(job_id),
        FOREIGN KEY(job_id,stage3_receipt_hash) REFERENCES exam_generation_stage3_contract_receipts(job_id,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,stage5_receipt_hash) REFERENCES exam_generation_stage5_constraint_receipts(job_id,receipt_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,input_payload_hash) REFERENCES job_inputs(job_id,payload_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    MATERIALS: f"""CREATE TABLE IF NOT EXISTS {MATERIALS}(
        job_id TEXT NOT NULL, material_hash TEXT NOT NULL {_hc('material_hash')},
        material_version TEXT NOT NULL CHECK(material_version='past-v3-stage6-material-preheat-v1'),
        context_hash TEXT NOT NULL {_hc('context_hash')},
        graph_hash TEXT NOT NULL {_hc('graph_hash')}, candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        candidate_count INTEGER NOT NULL CHECK(candidate_count>=0), payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,material_hash), UNIQUE(job_id,graph_hash,material_hash), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash) REFERENCES exam_generation_stage5_graph_receipts(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,candidate_facts_hash) REFERENCES exam_generation_stage4_fact_receipts(job_id,candidate_facts_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    WINDOWS: f"""CREATE TABLE IF NOT EXISTS {WINDOWS}(
        job_id TEXT NOT NULL, window_hash TEXT NOT NULL {_hc('window_hash')},
        window_version TEXT NOT NULL CHECK(window_version='past-v3-stage6-score-window-v1'),
        graph_hash TEXT NOT NULL {_hc('graph_hash')}, material_hash TEXT NOT NULL {_hc('material_hash')},
        phase TEXT NOT NULL CHECK(phase IN ('initial','expansion')), window_index INTEGER NOT NULL CHECK(window_index>0),
        slot_count INTEGER NOT NULL CHECK(slot_count BETWEEN 1 AND 4), edge_count INTEGER NOT NULL CHECK(edge_count BETWEEN 0 AND 16),
        payload_json TEXT NOT NULL, receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,window_hash), UNIQUE(job_id,graph_hash,phase,window_index), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,graph_hash,material_hash) REFERENCES {MATERIALS}(job_id,graph_hash,material_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    REQUESTS: f"""CREATE TABLE IF NOT EXISTS {REQUESTS}(
        job_id TEXT NOT NULL, window_hash TEXT NOT NULL {_hc('window_hash')}, request_hash TEXT NOT NULL {_hc('request_hash')},
        request_version TEXT NOT NULL CHECK(request_version='past-v3-stage6-score-request-v1'), request_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,window_hash), UNIQUE(job_id,request_hash), UNIQUE(job_id,window_hash,request_hash), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,window_hash) REFERENCES {WINDOWS}(job_id,window_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RETRY_POLICIES: f"""CREATE TABLE IF NOT EXISTS {RETRY_POLICIES}(
        job_id TEXT NOT NULL PRIMARY KEY, policy_hash TEXT NOT NULL {_hc('policy_hash')},
        policy_version TEXT NOT NULL CHECK(policy_version='past-v3-stage6-retry-policy-v1'),
        transport_retry_limit INTEGER NOT NULL CHECK(transport_retry_limit BETWEEN 0 AND 16),
        schema_correction_limit INTEGER NOT NULL CHECK(schema_correction_limit=2), payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(job_id,policy_hash), FOREIGN KEY(job_id) REFERENCES {ROOTS}(job_id) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RETRY_EVENTS: f"""CREATE TABLE IF NOT EXISTS {RETRY_EVENTS}(
        job_id TEXT NOT NULL, event_hash TEXT NOT NULL {_hc('event_hash')}, sequence INTEGER NOT NULL CHECK(sequence>0),
        previous_event_hash TEXT NOT NULL {_hc('previous_event_hash')}, window_hash TEXT NOT NULL {_hc('window_hash')},
        request_hash TEXT NOT NULL {_hc('request_hash')}, event_kind TEXT NOT NULL CHECK(event_kind IN ('transport_retry','schema_correction')),
        reason_code TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,event_hash), UNIQUE(job_id,sequence),
        FOREIGN KEY(job_id,window_hash,request_hash) REFERENCES {REQUESTS}(job_id,window_hash,request_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    RESPONSES: f"""CREATE TABLE IF NOT EXISTS {RESPONSES}(
        job_id TEXT NOT NULL, outcome_hash TEXT NOT NULL {_hc('outcome_hash')}, window_hash TEXT NOT NULL {_hc('window_hash')},
        request_hash TEXT NOT NULL {_hc('request_hash')}, outcome_status TEXT NOT NULL CHECK(outcome_status IN ('succeeded','terminal_failed')),
        terminal_reason TEXT, response_hash TEXT, response_version TEXT, response_json TEXT, parsed_scores_json TEXT, rejected_edges_json TEXT,
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,outcome_hash), UNIQUE(job_id,window_hash), UNIQUE(job_id,request_hash), UNIQUE(job_id,receipt_hash),
        CHECK((outcome_status='succeeded' AND terminal_reason IS NULL AND length(response_hash)=64 AND response_hash NOT GLOB '*[^0-9a-f]*'
               AND response_version='past-v3-stage6-score-response-v2' AND response_json IS NOT NULL AND parsed_scores_json IS NOT NULL AND rejected_edges_json IS NULL)
           OR (outcome_status='terminal_failed' AND terminal_reason IN ('transport_exhausted','schema_correction_exhausted')
               AND response_hash IS NULL AND response_version IS NULL AND response_json IS NULL AND parsed_scores_json IS NULL AND rejected_edges_json IS NOT NULL)),
        FOREIGN KEY(job_id,window_hash,request_hash) REFERENCES {REQUESTS}(job_id,window_hash,request_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    SCORES: f"""CREATE TABLE IF NOT EXISTS {SCORES}(
        job_id TEXT NOT NULL, score_set_hash TEXT NOT NULL {_hc('score_set_hash')},
        score_version TEXT NOT NULL CHECK(score_version='local-score-v2'), graph_hash TEXT NOT NULL {_hc('graph_hash')},
        audit_hash TEXT NOT NULL {_hc('audit_hash')}, response_set_hash TEXT NOT NULL {_hc('response_set_hash')},
        score_payload_json TEXT NOT NULL, audit_payload_json TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,score_set_hash), UNIQUE(job_id,graph_hash,score_set_hash), UNIQUE(job_id,audit_hash), UNIQUE(job_id,receipt_hash),
        FOREIGN KEY(job_id,graph_hash) REFERENCES exam_generation_stage5_graph_receipts(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    LEDGER_STATES: f"""CREATE TABLE IF NOT EXISTS {LEDGER_STATES}(
        job_id TEXT NOT NULL, graph_hash TEXT NOT NULL {_hc('graph_hash')}, head_hash TEXT NOT NULL {_hc('head_hash')},
        ledger_version TEXT NOT NULL CHECK(ledger_version='past-v3-stage6-rejection-ledger-v1'),
        entry_count INTEGER NOT NULL CHECK(entry_count>=0), previous_head_hash TEXT,
        state_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,graph_hash,head_hash), UNIQUE(job_id,graph_hash,entry_count),
        CHECK((entry_count=0 AND head_hash='0000000000000000000000000000000000000000000000000000000000000000' AND previous_head_hash IS NULL)
           OR (entry_count>0 AND length(previous_head_hash)=64 AND previous_head_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,graph_hash) REFERENCES exam_generation_stage5_graph_receipts(job_id,graph_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,previous_head_hash) REFERENCES {LEDGER_STATES}(job_id,graph_hash,head_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    LEDGER_ENTRIES: f"""CREATE TABLE IF NOT EXISTS {LEDGER_ENTRIES}(
        job_id TEXT NOT NULL, graph_hash TEXT NOT NULL {_hc('graph_hash')}, entry_hash TEXT NOT NULL {_hc('entry_hash')},
        sequence INTEGER NOT NULL CHECK(sequence>0), previous_head_hash TEXT NOT NULL {_hc('previous_head_hash')},
        reason_code TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,graph_hash,entry_hash), UNIQUE(job_id,graph_hash,sequence),
        FOREIGN KEY(job_id,graph_hash,entry_hash) REFERENCES {LEDGER_STATES}(job_id,graph_hash,head_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,previous_head_hash) REFERENCES {LEDGER_STATES}(job_id,graph_hash,head_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    SELECTION_REFS: f"""CREATE TABLE IF NOT EXISTS {SELECTION_REFS}(
        job_id TEXT NOT NULL, selection_hash TEXT NOT NULL {_hc('selection_hash')}, graph_hash TEXT NOT NULL {_hc('graph_hash')},
        score_set_hash TEXT NOT NULL {_hc('score_set_hash')}, reference_version TEXT NOT NULL CHECK(reference_version='past-v3-stage6-selection-reference-v1'),
        payload_json TEXT NOT NULL, reference_hash TEXT NOT NULL {_hc('reference_hash')}, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_hash), UNIQUE(job_id,reference_hash),
        FOREIGN KEY(job_id,selection_hash) REFERENCES exam_generation_stage5_selection_receipts(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,score_set_hash) REFERENCES {SCORES}(job_id,graph_hash,score_set_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    CHECKPOINTS: f"""CREATE TABLE IF NOT EXISTS {CHECKPOINTS}(
        job_id TEXT NOT NULL, checkpoint_hash TEXT NOT NULL {_hc('checkpoint_hash')}, checkpoint_version TEXT NOT NULL CHECK(checkpoint_version='past-v3-stage6-checkpoint-v3'),
        sequence INTEGER NOT NULL CHECK(sequence>0), previous_checkpoint_hash TEXT,
        identity_hash TEXT NOT NULL {_hc('identity_hash')}, checkpoint_stage TEXT NOT NULL CHECK(checkpoint_stage IN ('stage6_scored','stage7_finalized')),
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        material_hash TEXT NOT NULL {_hc('material_hash')}, score_set_hash TEXT NOT NULL {_hc('score_set_hash')}, audit_hash TEXT NOT NULL {_hc('audit_hash')},
        ledger_head_hash TEXT NOT NULL {_hc('ledger_head_hash')}, selection_hash TEXT NOT NULL {_hc('selection_hash')},
        pre_adaptation_hash TEXT, final_exam_hash TEXT, stage7_pre_receipt_hash TEXT, stage7_final_receipt_hash TEXT,
        size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 32768), payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,checkpoint_hash), UNIQUE(job_id,sequence),
        CHECK(previous_checkpoint_hash IS NULL OR (length(previous_checkpoint_hash)=64 AND previous_checkpoint_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK((checkpoint_stage='stage6_scored' AND pre_adaptation_hash IS NULL AND final_exam_hash IS NULL AND stage7_pre_receipt_hash IS NULL AND stage7_final_receipt_hash IS NULL)
           OR (checkpoint_stage='stage7_finalized' AND length(pre_adaptation_hash)=64 AND pre_adaptation_hash NOT GLOB '*[^0-9a-f]*'
               AND length(final_exam_hash)=64 AND final_exam_hash NOT GLOB '*[^0-9a-f]*'
               AND length(stage7_pre_receipt_hash)=64 AND stage7_pre_receipt_hash NOT GLOB '*[^0-9a-f]*'
               AND length(stage7_final_receipt_hash)=64 AND stage7_final_receipt_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,previous_checkpoint_hash) REFERENCES {CHECKPOINTS}(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,material_hash) REFERENCES {MATERIALS}(job_id,graph_hash,material_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,score_set_hash) REFERENCES {SCORES}(job_id,graph_hash,score_set_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,audit_hash) REFERENCES {SCORES}(job_id,audit_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,graph_hash,ledger_head_hash) REFERENCES {LEDGER_STATES}(job_id,graph_hash,head_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,selection_hash) REFERENCES {SELECTION_REFS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    LEGACY_DECISIONS: f"""CREATE TABLE IF NOT EXISTS {LEGACY_DECISIONS}(
        job_id TEXT NOT NULL, decision_hash TEXT NOT NULL {_hc('decision_hash')},
        decision_version TEXT NOT NULL CHECK(decision_version='past-v3-stage6-legacy-p1-migration-v1'),
        source_version TEXT NOT NULL CHECK(source_version='past-exam-checkpoint-v1'), migrated_identity_hash TEXT NOT NULL {_hc('migrated_identity_hash')},
        payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(job_id,decision_hash), UNIQUE(job_id,migrated_identity_hash),
        FOREIGN KEY(job_id) REFERENCES exam_generation_stage3_contract_receipts(job_id) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
    HEADS: f"""CREATE TABLE IF NOT EXISTS {HEADS}(
        job_id TEXT NOT NULL PRIMARY KEY, context_hash TEXT NOT NULL {_hc('context_hash')}, constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        material_hash TEXT, active_graph_hash TEXT, score_set_hash TEXT, audit_hash TEXT,
        ledger_head_hash TEXT NOT NULL {_hc('ledger_head_hash')}, ledger_entry_count INTEGER NOT NULL CHECK(ledger_entry_count>=0),
        retry_event_head_hash TEXT NOT NULL {_hc('retry_event_head_hash')}, retry_event_count INTEGER NOT NULL CHECK(retry_event_count>=0),
        transport_retry_count INTEGER NOT NULL CHECK(transport_retry_count>=0), schema_correction_count INTEGER NOT NULL CHECK(schema_correction_count BETWEEN 0 AND 2),
        transport_retry_limit INTEGER NOT NULL CHECK(transport_retry_limit BETWEEN 0 AND 16),
        selection_hash TEXT, checkpoint_hash TEXT, revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK(material_hash IS NULL OR (length(material_hash)=64 AND material_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(active_graph_hash IS NULL OR (length(active_graph_hash)=64 AND active_graph_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(score_set_hash IS NULL OR (length(score_set_hash)=64 AND score_set_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(audit_hash IS NULL OR (length(audit_hash)=64 AND audit_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(selection_hash IS NULL OR (length(selection_hash)=64 AND selection_hash NOT GLOB '*[^0-9a-f]*')),
        CHECK(checkpoint_hash IS NULL OR (length(checkpoint_hash)=64 AND checkpoint_hash NOT GLOB '*[^0-9a-f]*')),
        FOREIGN KEY(job_id,context_hash) REFERENCES {ROOTS}(job_id,context_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,constraint_hash) REFERENCES exam_generation_stage5_constraint_receipts(job_id,constraint_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_graph_hash,material_hash) REFERENCES {MATERIALS}(job_id,graph_hash,material_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_graph_hash,score_set_hash) REFERENCES {SCORES}(job_id,graph_hash,score_set_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,audit_hash) REFERENCES {SCORES}(job_id,audit_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,active_graph_hash,ledger_head_hash) REFERENCES {LEDGER_STATES}(job_id,graph_hash,head_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,selection_hash) REFERENCES {SELECTION_REFS}(job_id,selection_hash) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(job_id,checkpoint_hash) REFERENCES {CHECKPOINTS}(job_id,checkpoint_hash) ON UPDATE RESTRICT ON DELETE CASCADE
    )""",
}

TRIGGER_SQL: dict[str, tuple[str, str]] = {}
for _table in (ROOTS, MATERIALS, WINDOWS, REQUESTS, RETRY_POLICIES, RETRY_EVENTS, RESPONSES, SCORES, LEDGER_STATES, LEDGER_ENTRIES, SELECTION_REFS, CHECKPOINTS, LEGACY_DECISIONS):
    TRIGGER_SQL[f"{_table}_no_update"] = (_table, f"CREATE TRIGGER IF NOT EXISTS {_table}_no_update BEFORE UPDATE ON {_table} BEGIN SELECT RAISE(ABORT,'{_table}_immutable'); END")
TRIGGER_SQL[f"{HEADS}_cas_only"] = (HEADS, f"""CREATE TRIGGER IF NOT EXISTS {HEADS}_cas_only BEFORE UPDATE ON {HEADS}
    WHEN NEW.job_id!=OLD.job_id OR NEW.context_hash!=OLD.context_hash OR NEW.constraint_hash!=OLD.constraint_hash OR NEW.created_at!=OLD.created_at OR NEW.revision!=OLD.revision+1
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
    result: dict[str, set[tuple]] = {}
    probe = sqlite3.connect(":memory:")
    probe.execute("PRAGMA foreign_keys=OFF")
    for sql in CREATE_TABLE_SQL.values():
        probe.execute(sql)
    for table in CREATE_TABLE_SQL:
        result[table] = _foreign_keys(probe, table)
    probe.close()
    return result


EXPECTED_FKS = _expected_fks()


def _row_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    checks = (
        (ROOTS, "payload_json", "context_hash", "whole"),
        (MATERIALS, "payload_json", "material_hash", "whole"),
        (WINDOWS, "payload_json", "window_hash", "whole"),
        (RETRY_POLICIES, "payload_json", "policy_hash", "whole"),
        (RETRY_EVENTS, "payload_json", "event_hash", "embedded"),
        (LEGACY_DECISIONS, "payload_json", "decision_hash", "embedded"),
    )
    for table, column, hash_column, mode in checks:
        for row in db.execute(f"SELECT {column},{hash_column} FROM {table}"):
            try: payload = json.loads(row[0])
            except (TypeError, ValueError): issues.append(f"版本 44 的表 {table} 存在无效 JSON"); break
            if mode == "embedded":
                body = dict(payload) if isinstance(payload, dict) else {}; stored = body.pop(hash_column, None)
                valid = stored == row[1] and hmac.compare_digest(str(row[1]), _digest(body))
            else:
                valid = hmac.compare_digest(str(row[1]), _digest(payload))
            if not valid: issues.append(f"版本 44 的表 {table} 存在 payload 哈希漂移"); break
    for row in db.execute(f"SELECT score_payload_json,audit_payload_json,score_set_hash,audit_hash FROM {SCORES}"):
        try: score, audit = json.loads(row[0]), json.loads(row[1])
        except (TypeError, ValueError): issues.append(f"版本 44 的表 {SCORES} 存在无效 JSON"); break
        audit_body = dict(audit) if isinstance(audit, dict) else {}; embedded = audit_body.pop("audit_hash", None)
        score_body = dict(score) if isinstance(score, dict) else {}; score_embedded = score_body.pop("hash", None)
        if score_embedded != row[2] or _digest(score_body) != row[2] or embedded != row[3] or _digest(audit_body) != row[3]:
            issues.append(f"版本 44 的表 {SCORES} 存在评分或审计哈希漂移"); break
    for row in db.execute(f"SELECT job_id,window_hash,request_hash,request_json,receipt_hash FROM {REQUESTS}"):
        try: payload = json.loads(row[3])
        except (TypeError, ValueError): issues.append(f"版本 44 的表 {REQUESTS} 存在无效 JSON"); break
        body = dict(payload) if isinstance(payload, dict) else {}; embedded = body.pop("request_hash", None)
        receipt = {"job_id": row[0], "window_hash": row[1], "request_hash": row[2]}
        if embedded != row[2] or _digest(body) != row[2] or _digest(receipt) != row[4]: issues.append(f"版本 44 的表 {REQUESTS} 存在请求哈希漂移"); break
    for row in db.execute(f"SELECT job_id,window_hash,request_hash,outcome_status,terminal_reason,response_hash,response_json,rejected_edges_json,receipt_hash FROM {RESPONSES}"):
        if row[3] == "succeeded":
            try: response = json.loads(row[6])
            except (TypeError, ValueError): issues.append(f"版本 44 的表 {RESPONSES} 存在无效成功 JSON"); break
            body = {"job_id": row[0], "window_hash": row[1], "request_hash": row[2], "outcome_status": "succeeded", "response_hash": row[5]}
            valid = _digest(response) == row[5]
        else:
            try: rejected = json.loads(row[7])
            except (TypeError, ValueError): issues.append(f"版本 44 的表 {RESPONSES} 存在无效终止 JSON"); break
            body = {"job_id": row[0], "window_hash": row[1], "request_hash": row[2], "outcome_status": "terminal_failed", "terminal_reason": row[4], "rejected_edges": rejected}
            valid = isinstance(rejected, list)
        if not valid or _digest(body) != row[8]: issues.append(f"版本 44 的表 {RESPONSES} 存在 outcome 哈希漂移"); break
    for row in db.execute(f"SELECT job_id,context_hash,constraint_hash,material_hash,active_graph_hash,score_set_hash,audit_hash,ledger_head_hash,ledger_entry_count,retry_event_head_hash,retry_event_count,transport_retry_count,schema_correction_count,transport_retry_limit,selection_hash,checkpoint_hash FROM {HEADS}"):
        checks = ((MATERIALS, "material_hash", row[3]), (SCORES, "score_set_hash", row[5]), (SELECTION_REFS, "selection_hash", row[14]), (CHECKPOINTS, "checkpoint_hash", row[15]))
        if any(value is not None and db.execute(f"SELECT 1 FROM {table} WHERE job_id=? AND {column}=?", (row[0], value)).fetchone() is None for table, column, value in checks):
            issues.append(f"版本 44 的表 {HEADS} 存在悬空活动引用"); break
        state = db.execute(f"SELECT entry_count FROM {LEDGER_STATES} WHERE job_id=? AND graph_hash=? AND head_hash=?", (row[0], row[4], row[7])).fetchone()
        if state is None or int(state[0]) != int(row[8]): issues.append(f"版本 44 的表 {HEADS} 存在拒绝账本 HEAD 漂移"); break
        events = list(db.execute(f"SELECT event_hash,event_kind FROM {RETRY_EVENTS} WHERE job_id=? ORDER BY sequence", (row[0],)))
        if (len(events) != int(row[10]) or (events[-1][0] if events else "0" * 64) != row[9]
                or sum(x[1] == "transport_retry" for x in events) != int(row[11])
                or sum(x[1] == "schema_correction" for x in events) != int(row[12])
                or int(row[11]) > int(row[13])):
            issues.append(f"版本 44 的表 {HEADS} 存在重试事件 HEAD 或预算漂移"); break
    return issues


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    for table, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None: issues.append(f"版本 44 缺少表 {table}"); continue
        if str(row[0]) != "table" or _norm(row[1]) != _norm(sql): issues.append(f"版本 44 的表 {table} 定义不兼容")
        if _foreign_keys(db, table) != EXPECTED_FKS[table]: issues.append(f"版本 44 的表 {table} 外键定义不兼容")
        expected = {name for name, (owner, _) in TRIGGER_SQL.items() if owner == table}
        actual = {str(x[0]) for x in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (table,))}
        if actual != expected: issues.append(f"版本 44 的表 {table} 触发器集合不兼容")
    if not issues: issues.extend(_row_issues(db))
    return issues


def _conflicts(db: sqlite3.Connection) -> list[str]:
    issues = []
    for name, sql in CREATE_TABLE_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "table" or _norm(row[1]) != _norm(sql)): issues.append(f"版本 44 的对象 {name} 定义不兼容")
    for name, (_, sql) in TRIGGER_SQL.items():
        row = db.execute("SELECT type,sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
        if row is not None and (str(row[0]) != "trigger" or _norm(row[1]) != _norm(sql)): issues.append(f"版本 44 的对象 {name} 定义不兼容")
    return issues


def migrate_v43_to_v44(db: sqlite3.Connection) -> None:
    conflicts = _conflicts(db)
    if conflicts: raise RuntimeError("；".join(conflicts))
    for statement in SCHEMA_STATEMENTS: db.execute(statement)
    issues = schema_issues(db)
    if issues: raise RuntimeError("；".join(issues))


__all__ = ["CHECKPOINTS", "CREATE_TABLE_SQL", "HEADS", "LEDGER_ENTRIES", "LEDGER_STATES", "LEGACY_DECISIONS", "MATERIALS", "REQUESTS", "RETRY_EVENTS", "RETRY_POLICIES", "RESPONSES", "ROOTS", "SCHEMA_STATEMENTS", "SCORES", "SELECTION_REFS", "WINDOWS", "migrate_v43_to_v44", "schema_issues"]
