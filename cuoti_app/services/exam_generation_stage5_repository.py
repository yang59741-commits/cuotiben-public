"""Transactional persistence for immutable Stage-5 artifacts and its CAS head."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .exam_contracts import validate_generation_job_id
from .exam_generation_stage5_solver import (
    CANDIDATE_GRAPH_VERSION,
    GLOBAL_CONSTRAINT_VERSION,
    REPAIR_VERSION,
    SELECTION_VERSION,
    WITNESS_VERSION,
    FrozenCandidateEdgeGraph,
    Stage5GlobalConstraintContract,
    Stage5RepairResult,
    Stage5Selection,
)
from .job_contracts import JobCancelled, JobLeaseLost


CONTRACTS = "exam_generation_stage5_constraint_receipts"
GRAPHS = "exam_generation_stage5_graph_receipts"
EDGES = "exam_generation_stage5_graph_edges"
WITNESSES = "exam_generation_stage5_infeasible_witnesses"
SELECTIONS = "exam_generation_stage5_selection_receipts"
ASSIGNMENTS = "exam_generation_stage5_assignments"
REPAIRS = "exam_generation_stage5_repair_receipts"
HEADS = "exam_generation_stage5_heads"
CONSTRAINT_RECEIPT_VERSION = "past-v3-stage5-constraint-receipt-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class Stage5RepositoryError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Stage5Head:
    job_id: str
    constraint_hash: str
    initial_graph_hash: str | None
    active_graph_hash: str | None
    active_selection_hash: str | None
    active_repair_hash: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class Stage5StoredState:
    head: Stage5Head
    contract: Stage5GlobalConstraintContract
    initial_graph: FrozenCandidateEdgeGraph | None
    active_graph: FrozenCandidateEdgeGraph | None
    witness: Mapping[str, Any] | None
    selection: Stage5Selection | None
    repair: Mapping[str, Any] | None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fail(code: str, reason: str, message: str, *, retryable: bool = False) -> None:
    raise Stage5RepositoryError(code, reason, message, retryable=retryable)


def _timestamp(value: str | None) -> str:
    return str(value or datetime.now(timezone.utc).isoformat(timespec="microseconds"))


def _actor(job_id: str, worker_id: str, lease_attempt: int) -> tuple[str, str]:
    try:
        normalized = validate_generation_job_id(job_id)
    except ValueError:
        normalized = None
    worker = str(worker_id or "").strip()
    if normalized is None:
        _fail("exam_stage5_contract_conflict", "generation_job_id_invalid", "组卷任务身份无效")
    if not worker or type(lease_attempt) is not int or lease_attempt < 1:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    return normalized, worker


def _publication_residue(db, job_id: str) -> bool:
    row = db.execute(
        "SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)",
        (job_id, job_id, job_id),
    ).fetchone()
    return bool(row[0])


_STAGE3_FIELDS = (
    "receipt_contract_version", "stage_name", "job_id", "stage3_contract_version",
    "binding_hash", "generation_request_hash", "resolution_hash", "exact_slot_hash",
    "question_count", "scoring_mode", "slot_demands_hash", "points_projection_hash",
    "source_quotas_hash", "stage3_contract_hash",
)
_STAGE4_FIELDS = (
    "receipt_contract_version", "candidate_facts_contract_version", "job_id",
    "stage3_contract_hash", "stage3_receipt_hash", "projection_identity_version",
    "projection_id", "semantic_snapshot_id", "source_snapshot", "algorithm_version",
    "dictionary_version", "weights_version", "projection_build_hash", "projection_item_count",
    "projection_identity_hash", "target_institution", "candidate_pool_hash",
    "candidate_facts_hash", "unresolved_candidate_ids_hash", "candidate_count", "unresolved_count",
)


def _authority(db, job_id: str, worker_id: str, lease_attempt: int):
    job = db.execute("SELECT kind,status,cancel_requested,lease_owner,attempts FROM background_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if bool(job["cancel_requested"]) or str(job["status"] or "") == "cancel_requested":
        raise JobCancelled("任务已取消")
    if (str(job["kind"] or "") != "exam-generation" or str(job["status"] or "") != "running"
            or str(job["lease_owner"] or "") != worker_id or int(job["attempts"] or 0) != lease_attempt):
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    stage3 = db.execute("SELECT * FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,)).fetchone()
    stage4 = db.execute("SELECT * FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()
    if stage3 is None or stage4 is None:
        _fail("exam_stage5_contract_conflict", "upstream_receipt_missing", "阶段 5 缺少冻结的阶段 3 或阶段 4 回执")
    if not hmac.compare_digest(str(stage3["receipt_hash"] or ""), _digest({field: stage3[field] for field in _STAGE3_FIELDS})):
        _fail("exam_stage5_hash_mismatch", "stage3_receipt_hash_mismatch", "阶段 3 回执哈希漂移")
    if not hmac.compare_digest(str(stage4["receipt_hash"] or ""), _digest({field: stage4[field] for field in _STAGE4_FIELDS})):
        _fail("exam_stage5_hash_mismatch", "stage4_receipt_hash_mismatch", "阶段 4 回执哈希漂移")
    try:
        facts = json.loads(stage4["facts_payload_json"])
    except (TypeError, ValueError):
        _fail("exam_stage5_hash_mismatch", "stage4_payload_invalid", "阶段 4 候选事实 JSON 无效")
    if (_digest(facts) != stage4["candidate_facts_hash"] or stage4["stage3_contract_hash"] != stage3["stage3_contract_hash"]
            or stage4["stage3_receipt_hash"] != stage3["receipt_hash"]):
        _fail("exam_stage5_hash_mismatch", "upstream_identity_drift", "阶段 3/4 上游身份漂移")
    if _publication_residue(db, job_id):
        _fail("published_recovery_required", "publication_residue_detected", "任务已有发布残留，必须先恢复发布")
    return stage3, stage4


def _head(row) -> Stage5Head:
    return Stage5Head(str(row["job_id"]), str(row["constraint_hash"]), row["initial_graph_hash"], row["active_graph_hash"], row["active_selection_hash"], row["active_repair_hash"], int(row["revision"]))


def _load_head(db, job_id: str):
    row = db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        _fail("exam_stage5_contract_conflict", "stage5_head_missing", "阶段 5 head 不存在")
    return row


def _contract_record(job_id: str, stage3, stage4, contract: Stage5GlobalConstraintContract, created_at: str) -> dict[str, Any]:
    if not isinstance(contract, Stage5GlobalConstraintContract):
        _fail("exam_stage5_contract_conflict", "constraint_type_invalid", "全局约束合同类型无效")
    payload = contract.to_dict()
    if (_digest(payload) != contract.constraint_hash or payload.get("contract_version") != GLOBAL_CONSTRAINT_VERSION
            or contract.stage3_contract_hash != stage3["stage3_contract_hash"]
            or contract.candidate_facts_hash != stage4["candidate_facts_hash"]
            or contract.projection_identity_hash != stage4["projection_identity_hash"]):
        _fail("exam_stage5_hash_mismatch", "constraint_upstream_identity_mismatch", "全局约束未绑定当前阶段 3/4 回执")
    record = {
        "job_id": job_id, "receipt_contract_version": CONSTRAINT_RECEIPT_VERSION,
        "constraint_contract_version": contract.contract_version,
        "stage3_contract_hash": contract.stage3_contract_hash, "stage3_receipt_hash": stage3["receipt_hash"],
        "candidate_facts_hash": contract.candidate_facts_hash, "stage4_receipt_hash": stage4["receipt_hash"],
        "projection_identity_hash": contract.projection_identity_hash, "constraint_hash": contract.constraint_hash,
        "question_count": contract.question_count, "payload_json": _canonical(payload), "created_at": created_at,
    }
    fields = tuple(key for key in record if key not in {"payload_json", "created_at"})
    record["receipt_hash"] = _digest({field: record[field] for field in fields})
    return record


def persist_global_constraint(db, *, job_id: str, contract: Stage5GlobalConstraintContract, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage5Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage5_store_failed", "nested_stage5_transaction_forbidden", "阶段 5 写入不能嵌套事务")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        stage3, stage4 = _authority(db, job_id, worker_id, lease_attempt)
        record = _contract_record(job_id, stage3, stage4, contract, timestamp)
        existing = db.execute(f"SELECT * FROM {CONTRACTS} WHERE job_id=?", (job_id,)).fetchone()
        if existing is not None:
            if any(existing[key] != record[key] for key in record if key != "created_at"):
                _fail("exam_stage5_contract_conflict", "stored_constraint_identity_conflict", "已有阶段 5 全局约束身份冲突")
            return _head(_load_head(db, job_id))
        columns = tuple(record)
        db.execute(f"INSERT INTO {CONTRACTS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(record[key] for key in columns))
        db.execute(f"INSERT INTO {HEADS}(job_id,constraint_hash,initial_graph_hash,active_graph_hash,active_selection_hash,active_repair_hash,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (job_id, contract.constraint_hash, None, None, None, None, 1, timestamp, timestamp))
        return _head(_load_head(db, job_id))


def _verify_graph(graph: FrozenCandidateEdgeGraph, constraint_hash: str) -> dict[str, Any]:
    if not isinstance(graph, FrozenCandidateEdgeGraph):
        _fail("exam_stage5_contract_conflict", "graph_type_invalid", "候选图类型无效")
    payload = graph.to_dict()
    if (_digest(payload) != graph.graph_hash or payload.get("contract_version") != CANDIDATE_GRAPH_VERSION
            or graph.contract_version != payload.get("contract_version") or graph.graph_mode != payload.get("graph_mode")
            or graph.constraint_hash != constraint_hash or payload.get("constraint_hash") != constraint_hash
            or payload.get("candidate_facts_hash") != graph.candidate_facts_hash
            or graph.edge_count != len(payload.get("edges", [])) or not isinstance(payload.get("candidates"), dict)):
        _fail("exam_stage5_hash_mismatch", "graph_payload_hash_mismatch", "候选图完整性或约束绑定无效")
    return payload


def _insert_graph(db, job_id: str, graph: FrozenCandidateEdgeGraph, payload: Mapping[str, Any], base_graph_hash: str | None, timestamp: str) -> None:
    record = {
        "graph_hash": graph.graph_hash, "job_id": job_id, "graph_contract_version": graph.contract_version,
        "constraint_hash": graph.constraint_hash, "candidate_facts_hash": graph.candidate_facts_hash,
        "graph_mode": graph.graph_mode, "base_graph_hash": base_graph_hash, "edge_count": graph.edge_count,
        "candidate_count": len(payload["candidates"]), "expanded_slot_ids_hash": _digest(payload.get("expanded_slot_ids", [])),
        "payload_json": _canonical(payload),
    }
    receipt_body = {key: value for key, value in record.items() if key != "payload_json"}
    record["receipt_hash"] = _digest(receipt_body)
    record["created_at"] = timestamp
    columns = tuple(record)
    db.execute(f"INSERT INTO {GRAPHS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(record[key] for key in columns))
    for ordinal, edge in enumerate(payload["edges"]):
        raw = _canonical(edge)
        db.execute(f"INSERT INTO {EDGES}(job_id,graph_hash,ordinal,slot_id,candidate_id,content_hash,edge_json,edge_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (job_id, graph.graph_hash, ordinal, edge["slot_id"], edge["candidate_id"], edge["content_hash"], raw, _digest(edge), timestamp))


def persist_candidate_graph(db, *, job_id: str, graph: FrozenCandidateEdgeGraph, expected_revision: int, worker_id: str, lease_attempt: int, base_graph_hash: str | None = None, created_at: str | None = None) -> Stage5Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or type(expected_revision) is not int or expected_revision < 1:
        _fail("exam_stage5_store_failed", "graph_persist_input_invalid", "候选图写入参数无效")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt)
        head = _load_head(db, job_id)
        payload = _verify_graph(graph, str(head["constraint_hash"]))
        contract = db.execute(f"SELECT candidate_facts_hash,question_count FROM {CONTRACTS} WHERE job_id=?", (job_id,)).fetchone()
        if contract is None or graph.candidate_facts_hash != contract["candidate_facts_hash"]:
            _fail("exam_stage5_contract_conflict", "graph_candidate_facts_mismatch", "候选图未绑定冻结候选事实")
        existing = db.execute(f"SELECT graph_hash FROM {GRAPHS} WHERE job_id=? AND graph_hash=?", (job_id, graph.graph_hash)).fetchone()
        if existing is not None:
            return _head(head)
        if graph.graph_mode == "initial":
            if base_graph_hash is not None or head["initial_graph_hash"] is not None or payload.get("expanded_slot_ids"):
                _fail("exam_stage5_contract_conflict", "initial_graph_conflict", "初始图身份冲突")
        elif graph.graph_mode == "expanded":
            if not isinstance(base_graph_hash, str) or base_graph_hash != head["initial_graph_hash"] or head["active_graph_hash"] != base_graph_hash:
                _fail("exam_stage5_contract_conflict", "expanded_graph_base_mismatch", "扩展图未绑定当前初始图")
            if db.execute(f"SELECT 1 FROM {GRAPHS} WHERE job_id=? AND graph_mode='expanded'", (job_id,)).fetchone() is not None:
                _fail("exam_stage5_contract_conflict", "second_expanded_graph_forbidden", "阶段 5 只允许一个扩展图")
            base = json.loads(db.execute(f"SELECT payload_json FROM {GRAPHS} WHERE job_id=? AND graph_hash=?", (job_id, base_graph_hash)).fetchone()[0])
            before = {(edge["slot_id"], edge["candidate_id"]): edge for edge in base["edges"]}
            after = {(edge["slot_id"], edge["candidate_id"]): edge for edge in payload["edges"]}
            if not set(before).issubset(after) or any(after[key] != value for key, value in before.items()):
                _fail("exam_stage5_contract_conflict", "expanded_graph_not_additive", "扩展图改写或删除了初始边")
            additions: dict[str, int] = {}
            for key in set(after) - set(before): additions[key[0]] = additions.get(key[0], 0) + 1
            expanded_slots = set(payload.get("expanded_slot_ids", []))
            if any(count > 2 or slot not in expanded_slots for slot, count in additions.items()) or len(after) > 6 * int(contract["question_count"]) or len(payload["candidates"]) > 6 * int(contract["question_count"]):
                _fail("exam_stage5_contract_conflict", "expanded_graph_budget_or_scope_invalid", "扩展图范围或 6N 预算无效")
        else:
            _fail("exam_stage5_contract_conflict", "graph_mode_invalid", "候选图模式无效")
        _insert_graph(db, job_id, graph, payload, base_graph_hash, timestamp)
        if graph.graph_mode == "initial":
            cursor = db.execute(f"UPDATE {HEADS} SET initial_graph_hash=?,active_graph_hash=?,revision=revision+1,updated_at=? WHERE job_id=? AND revision=? AND initial_graph_hash IS NULL",
                                (graph.graph_hash, graph.graph_hash, timestamp, job_id, expected_revision))
        else:
            cursor = db.execute(f"UPDATE {HEADS} SET active_graph_hash=?,active_repair_hash=NULL,revision=revision+1,updated_at=? WHERE job_id=? AND revision=?",
                                (graph.graph_hash, timestamp, job_id, expected_revision))
        if cursor.rowcount != 1:
            _fail("exam_stage5_contract_conflict", "stage5_head_cas_lost", "阶段 5 head CAS 已丢失", retryable=True)
        return _head(_load_head(db, job_id))


def persist_infeasible_witness(db, *, job_id: str, witness: Mapping[str, Any], worker_id: str, lease_attempt: int, created_at: str | None = None) -> str:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or not isinstance(witness, Mapping):
        _fail("exam_stage5_store_failed", "witness_input_invalid", "不可行见证写入参数无效")
    payload = dict(witness)
    witness_hash = payload.get("witness_hash")
    body = dict(payload); body.pop("witness_hash", None)
    if payload.get("version") != WITNESS_VERSION or payload.get("kind") not in {"hall_min_cut", "constrained_min_cut"} or not isinstance(witness_hash, str) or _digest(body) != witness_hash:
        _fail("exam_stage5_hash_mismatch", "witness_hash_mismatch", "不可行见证哈希无效")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt)
        head = _load_head(db, job_id)
        if payload.get("constraint_hash") != head["constraint_hash"] or payload.get("graph_hash") != head["active_graph_hash"]:
            _fail("exam_stage5_contract_conflict", "witness_head_mismatch", "不可行见证未绑定 active graph")
        existing = db.execute(f"SELECT payload_json FROM {WITNESSES} WHERE job_id=? AND graph_hash=?", (job_id, payload["graph_hash"])).fetchone()
        if existing is not None:
            if json.loads(existing[0]) != payload:
                _fail("exam_stage5_contract_conflict", "stored_witness_conflict", "已有不可行见证身份冲突")
            return witness_hash
        receipt = _digest({"job_id": job_id, "graph_hash": payload["graph_hash"], "constraint_hash": payload["constraint_hash"], "witness_hash": witness_hash})
        db.execute(f"INSERT INTO {WITNESSES}(witness_hash,job_id,graph_hash,constraint_hash,witness_version,witness_kind,payload_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (witness_hash, job_id, payload["graph_hash"], payload["constraint_hash"], payload["version"], payload["kind"], _canonical(payload), receipt, timestamp))
        return witness_hash


def _verify_selection(selection: Stage5Selection, graph_hash: str, constraint_hash: str, constraint_payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(selection, Stage5Selection):
        _fail("exam_stage5_contract_conflict", "selection_type_invalid", "选择合同类型无效")
    payload = selection.to_dict()
    identity = {key: payload[key] for key in ("contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash", "constraint_hash", "score_identity_hash", "assignments")}
    if (payload.get("contract_version") != SELECTION_VERSION or _digest(identity) != selection.selection_hash
            or payload.get("selection_hash") != selection.selection_hash or selection.graph_hash != graph_hash
            or selection.constraint_hash != constraint_hash or tuple(payload.get("assignments", [])) != selection.assignments
            or any(payload.get(field) != constraint_payload.get(field) for field in ("stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash"))
            or payload.get("total_utility") != selection.total_utility):
        _fail("exam_stage5_hash_mismatch", "selection_hash_or_binding_mismatch", "选择合同完整性或图绑定无效")
    return payload


def _insert_selection(db, job_id: str, selection: Stage5Selection, payload: Mapping[str, Any], timestamp: str) -> None:
    record = {"selection_hash": selection.selection_hash, "job_id": job_id, "graph_hash": selection.graph_hash,
              "constraint_hash": selection.constraint_hash, "selection_contract_version": selection.contract_version,
              "score_identity_hash": selection.score_identity_hash, "assignment_count": len(selection.assignments),
              "total_utility": selection.total_utility, "payload_json": _canonical(payload)}
    record["receipt_hash"] = _digest({key: value for key, value in record.items() if key != "payload_json"})
    record["created_at"] = timestamp
    columns = tuple(record)
    db.execute(f"INSERT INTO {SELECTIONS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(record[key] for key in columns))
    for assignment in selection.assignments:
        db.execute(f"INSERT INTO {ASSIGNMENTS}(job_id,selection_hash,position,slot_id,candidate_id,content_hash,assignment_json,assignment_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                   (job_id, selection.selection_hash, assignment["position"], assignment["slot_id"], assignment["candidate_id"], assignment["content_hash"], _canonical(assignment), _digest(assignment), timestamp))


def persist_selection(db, *, job_id: str, selection: Stage5Selection, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage5Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or type(expected_revision) is not int or expected_revision < 1:
        _fail("exam_stage5_store_failed", "selection_persist_input_invalid", "选择写入参数无效")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt)
        head = _load_head(db, job_id)
        contract_payload = json.loads(db.execute(f"SELECT payload_json FROM {CONTRACTS} WHERE job_id=?", (job_id,)).fetchone()[0])
        payload = _verify_selection(selection, str(head["active_graph_hash"]), str(head["constraint_hash"]), contract_payload)
        if head["active_selection_hash"] is not None:
            if head["active_selection_hash"] == selection.selection_hash:
                return _head(head)
            _fail(
                "exam_stage5_contract_conflict",
                "direct_selection_replacement_forbidden",
                "已有选择只能通过版本化原子修复回执替换",
            )
        existing = db.execute(f"SELECT selection_hash FROM {SELECTIONS} WHERE job_id=? AND selection_hash=?", (job_id, selection.selection_hash)).fetchone()
        if existing is None:
            _insert_selection(db, job_id, selection, payload, timestamp)
        cursor = db.execute(f"UPDATE {HEADS} SET active_selection_hash=?,active_repair_hash=NULL,revision=revision+1,updated_at=? WHERE job_id=? AND revision=? AND active_graph_hash=?",
                            (selection.selection_hash, timestamp, job_id, expected_revision, selection.graph_hash))
        if cursor.rowcount != 1:
            _fail("exam_stage5_contract_conflict", "stage5_head_cas_lost", "阶段 5 head CAS 已丢失", retryable=True)
        return _head(_load_head(db, job_id))


def persist_repair_result(db, *, job_id: str, result: Stage5RepairResult, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage5Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or type(expected_revision) is not int or expected_revision < 1 or not isinstance(result, Stage5RepairResult):
        _fail("exam_stage5_store_failed", "repair_persist_input_invalid", "修复写入参数无效")
    timestamp = _timestamp(created_at)
    receipt = result.repair_receipt
    body = dict(receipt); stored_hash = body.pop("repair_hash", None)
    if stored_hash != result.repair_hash or _digest(body) != result.repair_hash or receipt.get("version") != REPAIR_VERSION:
        _fail("exam_stage5_hash_mismatch", "repair_receipt_hash_mismatch", "修复回执哈希无效")
    replacement = result.replacement_selection
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt)
        head = _load_head(db, job_id)
        if receipt.get("constraint_hash") != head["constraint_hash"] or receipt.get("repair_graph_hash") != head["active_graph_hash"]:
            _fail("exam_stage5_contract_conflict", "repair_head_binding_mismatch", "修复未绑定 active graph")
        existing = db.execute(f"SELECT repair_hash FROM {REPAIRS} WHERE job_id=? AND repair_hash=?", (job_id, result.repair_hash)).fetchone()
        if existing is not None:
            if existing[0] == result.repair_hash and head["active_repair_hash"] == result.repair_hash:
                return _head(head)
        base_selection_hash, witness_hash = receipt.get("base_selection_hash"), receipt.get("witness_hash")
        if (base_selection_hash is None) == (witness_hash is None):
            _fail("exam_stage5_contract_conflict", "repair_origin_binding_invalid", "修复必须且只能绑定 base selection 或 witness")
        if base_selection_hash is not None and head["active_selection_hash"] != base_selection_hash:
            _fail("exam_stage5_contract_conflict", "repair_base_selection_not_active", "修复基础选择不是 active selection")
        if witness_hash is not None and db.execute(f"SELECT 1 FROM {WITNESSES} WHERE job_id=? AND witness_hash=?", (job_id, witness_hash)).fetchone() is None:
            _fail("exam_stage5_contract_conflict", "repair_witness_missing", "修复见证尚未持久化")
        if db.execute(f"SELECT 1 FROM {GRAPHS} WHERE job_id=? AND graph_hash=?", (job_id, receipt.get("base_graph_hash"))).fetchone() is None:
            _fail("exam_stage5_contract_conflict", "repair_base_graph_missing", "修复基础图尚未持久化")
        contract_payload = json.loads(db.execute(f"SELECT payload_json FROM {CONTRACTS} WHERE job_id=?", (job_id,)).fetchone()[0])
        payload = _verify_selection(replacement, str(head["active_graph_hash"]), str(head["constraint_hash"]), contract_payload)
        if replacement.selection_hash != receipt.get("replacement_selection_hash") or result.repair_scope_hash != receipt.get("repair_scope_hash") or result.fixed_assignment_hash != receipt.get("fixed_assignment_hash"):
            _fail("exam_stage5_contract_conflict", "repair_result_binding_mismatch", "替换选择未绑定修复回执")
        if db.execute(f"SELECT 1 FROM {SELECTIONS} WHERE job_id=? AND selection_hash=?", (job_id, replacement.selection_hash)).fetchone() is None:
            _insert_selection(db, job_id, replacement, payload, timestamp)
        columns = ("repair_hash", "job_id", "repair_version", "constraint_hash", "base_graph_hash", "repair_graph_hash", "base_selection_hash", "witness_hash", "replacement_selection_hash", "repair_scope_hash", "fixed_assignment_hash", "receipt_json", "created_at")
        values = (result.repair_hash, job_id, receipt["version"], receipt["constraint_hash"], receipt["base_graph_hash"], receipt["repair_graph_hash"], base_selection_hash, witness_hash, replacement.selection_hash, result.repair_scope_hash, result.fixed_assignment_hash, _canonical(receipt), timestamp)
        db.execute(f"INSERT INTO {REPAIRS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
        cursor = db.execute(f"UPDATE {HEADS} SET active_graph_hash=?,active_selection_hash=?,active_repair_hash=?,revision=revision+1,updated_at=? WHERE job_id=? AND revision=?",
                            (replacement.graph_hash, replacement.selection_hash, result.repair_hash, timestamp, job_id, expected_revision))
        if cursor.rowcount != 1:
            _fail("exam_stage5_contract_conflict", "stage5_head_cas_lost", "阶段 5 head CAS 已丢失", retryable=True)
        return _head(_load_head(db, job_id))


def _load_graph(db, job_id: str, graph_hash: str | None) -> FrozenCandidateEdgeGraph | None:
    if graph_hash is None:
        return None
    row = db.execute(f"SELECT * FROM {GRAPHS} WHERE job_id=? AND graph_hash=?", (job_id, graph_hash)).fetchone()
    if row is None:
        _fail("exam_stage5_hash_mismatch", "active_graph_missing", "active graph 不存在")
    try: payload = json.loads(row["payload_json"])
    except (TypeError, ValueError): _fail("exam_stage5_hash_mismatch", "stored_graph_json_invalid", "候选图 JSON 无效")
    if _digest(payload) != row["graph_hash"]:
        _fail("exam_stage5_hash_mismatch", "stored_graph_hash_mismatch", "候选图 payload 漂移")
    receipt_body = {key: row[key] for key in ("graph_hash", "job_id", "graph_contract_version", "constraint_hash", "candidate_facts_hash", "graph_mode", "base_graph_hash", "edge_count", "candidate_count", "expanded_slot_ids_hash")}
    if _digest(receipt_body) != row["receipt_hash"]:
        _fail("exam_stage5_hash_mismatch", "stored_graph_receipt_hash_mismatch", "候选图回执哈希漂移")
    stored_edges = []
    for item in db.execute(f"SELECT edge_json,edge_hash FROM {EDGES} WHERE job_id=? AND graph_hash=? ORDER BY ordinal", (job_id, graph_hash)):
        try: edge = json.loads(item[0])
        except (TypeError, ValueError): _fail("exam_stage5_hash_mismatch", "stored_graph_edge_json_invalid", "候选图边 JSON 无效")
        if _digest(edge) != item[1]: _fail("exam_stage5_hash_mismatch", "stored_graph_edge_hash_mismatch", "候选图边哈希漂移")
        stored_edges.append(edge)
    if stored_edges != payload.get("edges") or len(stored_edges) != int(row["edge_count"]) or len(payload.get("candidates", {})) != int(row["candidate_count"]):
        _fail("exam_stage5_hash_mismatch", "stored_graph_edges_mismatch", "候选图边与回执不一致")
    return FrozenCandidateEdgeGraph(row["graph_contract_version"], row["constraint_hash"], row["candidate_facts_hash"], row["graph_mode"], int(row["edge_count"]), row["graph_hash"], _canonical(payload))


def _load_selection(db, job_id: str, selection_hash: str | None) -> Stage5Selection | None:
    if selection_hash is None: return None
    row = db.execute(f"SELECT * FROM {SELECTIONS} WHERE job_id=? AND selection_hash=?", (job_id, selection_hash)).fetchone()
    if row is None: _fail("exam_stage5_hash_mismatch", "active_selection_missing", "active selection 不存在")
    try: payload = json.loads(row["payload_json"])
    except (TypeError, ValueError): _fail("exam_stage5_hash_mismatch", "stored_selection_json_invalid", "选择 JSON 无效")
    receipt_body = {key: row[key] for key in ("selection_hash", "job_id", "graph_hash", "constraint_hash", "selection_contract_version", "score_identity_hash", "assignment_count", "total_utility")}
    if _digest(receipt_body) != row["receipt_hash"]:
        _fail("exam_stage5_hash_mismatch", "stored_selection_receipt_hash_mismatch", "选择回执哈希漂移")
    values = []
    for item in db.execute(f"SELECT assignment_json,assignment_hash FROM {ASSIGNMENTS} WHERE job_id=? AND selection_hash=? ORDER BY position", (job_id, selection_hash)):
        try: assignment = json.loads(item[0])
        except (TypeError, ValueError): _fail("exam_stage5_hash_mismatch", "stored_assignment_json_invalid", "assignment JSON 无效")
        if _digest(assignment) != item[1]: _fail("exam_stage5_hash_mismatch", "stored_assignment_hash_mismatch", "assignment 哈希漂移")
        values.append(assignment)
    assignments = tuple(values)
    identity = {key: payload[key] for key in ("contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash", "constraint_hash", "score_identity_hash", "assignments")}
    if _digest(identity) != row["selection_hash"] or list(assignments) != payload.get("assignments") or len(assignments) != int(row["assignment_count"]):
        _fail("exam_stage5_hash_mismatch", "stored_selection_hash_mismatch", "选择或 assignment 漂移")
    return Stage5Selection(row["selection_contract_version"], row["graph_hash"], row["constraint_hash"], row["score_identity_hash"], int(row["total_utility"]), row["selection_hash"], assignments, _canonical(payload))


def recover_stage5_state(db, *, job_id: str, worker_id: str, lease_attempt: int) -> Stage5StoredState:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage5_store_failed", "nested_stage5_transaction_forbidden", "阶段 5 恢复不能嵌套事务")
    with immediate_transaction(db):
        stage3, stage4 = _authority(db, job_id, worker_id, lease_attempt)
        row = db.execute(f"SELECT * FROM {CONTRACTS} WHERE job_id=?", (job_id,)).fetchone()
        if row is None: _fail("exam_stage5_contract_conflict", "constraint_receipt_missing", "阶段 5 全局约束回执不存在")
        try: payload = json.loads(row["payload_json"])
        except (TypeError, ValueError): _fail("exam_stage5_hash_mismatch", "stored_constraint_json_invalid", "全局约束 JSON 无效")
        if (_digest(payload) != row["constraint_hash"] or row["stage3_receipt_hash"] != stage3["receipt_hash"]
                or row["stage4_receipt_hash"] != stage4["receipt_hash"] or row["candidate_facts_hash"] != stage4["candidate_facts_hash"]):
            _fail("exam_stage5_hash_mismatch", "stored_constraint_upstream_drift", "阶段 5 回执或上游身份漂移")
        constraint_receipt = {key: row[key] for key in ("job_id", "receipt_contract_version", "constraint_contract_version", "stage3_contract_hash", "stage3_receipt_hash", "candidate_facts_hash", "stage4_receipt_hash", "projection_identity_hash", "constraint_hash", "question_count")}
        if _digest(constraint_receipt) != row["receipt_hash"]:
            _fail("exam_stage5_hash_mismatch", "stored_constraint_receipt_hash_mismatch", "阶段 5 全局约束回执哈希漂移")
        contract = Stage5GlobalConstraintContract(row["constraint_contract_version"], row["stage3_contract_hash"], row["candidate_facts_hash"], row["projection_identity_hash"], int(row["question_count"]), row["constraint_hash"], _canonical(payload))
        head_row = _load_head(db, job_id); head = _head(head_row)
        initial, active = _load_graph(db, job_id, head.initial_graph_hash), _load_graph(db, job_id, head.active_graph_hash)
        selection = _load_selection(db, job_id, head.active_selection_hash)
        if selection is not None and active is not None and selection.graph_hash != active.graph_hash:
            active_row = db.execute(
                f"SELECT graph_mode,base_graph_hash FROM {GRAPHS} WHERE job_id=? AND graph_hash=?",
                (job_id, active.graph_hash),
            ).fetchone()
            if (
                active_row is None
                or active_row["graph_mode"] != "expanded"
                or active_row["base_graph_hash"] != selection.graph_hash
                or head.active_repair_hash is not None
            ):
                _fail(
                    "exam_stage5_hash_mismatch",
                    "active_selection_graph_transition_invalid",
                    "active graph 与选择不构成合法的原子修复过渡态",
                )
        witness_row = db.execute(f"SELECT * FROM {WITNESSES} WHERE job_id=? AND graph_hash=?", (job_id, head.active_graph_hash)).fetchone() if head.active_graph_hash else None
        witness = json.loads(witness_row["payload_json"]) if witness_row else None
        if witness is not None:
            witness_body = dict(witness); stored_witness_hash = witness_body.pop("witness_hash", None)
            witness_receipt = {"job_id": job_id, "graph_hash": witness_row["graph_hash"], "constraint_hash": witness_row["constraint_hash"], "witness_hash": witness_row["witness_hash"]}
            if stored_witness_hash != witness_row["witness_hash"] or _digest(witness_body) != stored_witness_hash or _digest(witness_receipt) != witness_row["receipt_hash"]:
                _fail("exam_stage5_hash_mismatch", "stored_witness_hash_mismatch", "不可行见证回执漂移")
        repair_row = db.execute(f"SELECT * FROM {REPAIRS} WHERE job_id=? AND repair_hash=?", (job_id, head.active_repair_hash)).fetchone() if head.active_repair_hash else None
        repair = json.loads(repair_row["receipt_json"]) if repair_row else None
        if repair is not None:
            body = dict(repair); repair_hash = body.pop("repair_hash", None)
            if (repair_hash != head.active_repair_hash or _digest(body) != repair_hash or repair.get("replacement_selection_hash") != head.active_selection_hash
                    or repair_row["job_id"] != job_id or repair_row["constraint_hash"] != head.constraint_hash
                    or repair_row["replacement_selection_hash"] != head.active_selection_hash):
                _fail("exam_stage5_hash_mismatch", "stored_repair_hash_mismatch", "修复回执或 active selection 漂移")
        return Stage5StoredState(head, contract, initial, active, witness, selection, repair)


__all__ = ["Stage5Head", "Stage5RepositoryError", "Stage5StoredState", "persist_candidate_graph", "persist_global_constraint", "persist_infeasible_witness", "persist_repair_result", "persist_selection", "recover_stage5_state"]
