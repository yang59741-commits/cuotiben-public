"""Crash-safe persistence and closed recovery for Stage-6 immutable artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..transactions import immediate_transaction
from .exam_contracts import validate_generation_job_id
from .exam_generation_stage6_checkpoint_v3 import (
    CHECKPOINT_VERSION,
    MIGRATION_DECISION_VERSION,
    CheckpointV3,
    LegacyP1MigrationDecision,
    validate_checkpoint_v3,
)
from .exam_generation_stage6_scoring import (
    AUDIT_RECEIPT_VERSION,
    MATERIAL_RECEIPT_VERSION,
    REJECTION_LEDGER_VERSION,
    REQUEST_VERSION,
    RESPONSE_VERSION,
    SCORE_RECEIPT_VERSION,
    WINDOW_VERSION,
    MaterialPreheatReceipt,
    RejectionLedgerHead,
    ScoreReceipt,
    ScoringWindow,
    WindowScoreResult,
    build_material_preheat_receipt,
    build_score_receipt,
    build_scoring_windows,
    parse_scoring_response,
    render_scoring_request,
)
from .exam_generation_stage5_solver import FrozenCandidateEdgeGraph
from .job_contracts import JobCancelled, JobLeaseLost


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
SELECTION_REFERENCE_VERSION = "past-v3-stage6-selection-reference-v1"
ZERO_HASH = "0" * 64
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")

# Never accepted by a production request or exported from this module.  The
# retired write path can only be exercised by explicit provider-free fixtures.
_LEGACY_SCORE_FIXTURE_WRITE_CAPABILITY = object()


class Stage6RepositoryError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class Stage6Head:
    job_id: str
    context_hash: str
    constraint_hash: str
    material_hash: str | None
    active_graph_hash: str | None
    score_set_hash: str | None
    audit_hash: str | None
    ledger_head_hash: str
    ledger_entry_count: int
    retry_event_head_hash: str
    retry_event_count: int
    transport_retry_count: int
    schema_correction_count: int
    transport_retry_limit: int
    selection_hash: str | None
    checkpoint_hash: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class Stage6StoredState:
    head: Stage6Head
    material: Mapping[str, Any]
    windows: tuple[Mapping[str, Any], ...]
    responses: tuple[Mapping[str, Any], ...]
    score_receipt: Mapping[str, Any]
    audit_receipt: Mapping[str, Any]
    rejection_entries: tuple[Mapping[str, Any], ...]
    selection_reference: Mapping[str, Any]
    checkpoint: CheckpointV3


@dataclass(frozen=True, slots=True)
class ScoringAttemptState:
    window_hash: str
    request_hash: str
    phase: str
    status: str
    terminal_reason: str | None
    rejected_edges: tuple[Mapping[str, str], ...]
    request_json: str
    window: Mapping[str, Any]
    retry_events: tuple[Mapping[str, Any], ...]
    transport_retry_count: int
    schema_correction_count: int
    transport_retry_limit: int


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _fail(code: str, reason: str, message: str, *, retryable: bool = False) -> None:
    raise Stage6RepositoryError(code, reason, message, retryable=retryable)


def _timestamp(value: str | None) -> str:
    return str(value or datetime.now(timezone.utc).isoformat(timespec="microseconds"))


def _actor(job_id: str, worker_id: str, lease_attempt: int) -> tuple[str, str]:
    try: normalized = validate_generation_job_id(job_id)
    except ValueError: normalized = None
    worker = str(worker_id or "").strip()
    if normalized is None: _fail("exam_stage6_contract_conflict", "generation_job_id_invalid", "组卷任务身份无效")
    if not worker or type(lease_attempt) is not int or lease_attempt < 1: raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    return normalized, worker


def _publication_residue(db, job_id: str) -> bool:
    row = db.execute(
        "SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)",
        (job_id, job_id, job_id),
    ).fetchone()
    return bool(row[0])


_BINDING_FIELDS = ("binding_contract_version", "job_id", "job_input_identity_version", "generation_request_hash_version", "single_paper_contract_version", "past_profile_freeze_contract_version", "profile_worker_context_version", "exact_slot_context_version", "logical_request_hash", "job_input_hash", "generation_request_hash", "frozen_ref_hash", "profile_hash", "exact_slot_hash", "resolution_hash")
_STAGE3_FIELDS = ("receipt_contract_version", "stage_name", "job_id", "stage3_contract_version", "binding_hash", "generation_request_hash", "resolution_hash", "exact_slot_hash", "question_count", "scoring_mode", "slot_demands_hash", "points_projection_hash", "source_quotas_hash", "stage3_contract_hash")
_STAGE4_FIELDS = ("receipt_contract_version", "candidate_facts_contract_version", "job_id", "stage3_contract_hash", "stage3_receipt_hash", "projection_identity_version", "projection_id", "semantic_snapshot_id", "source_snapshot", "algorithm_version", "dictionary_version", "weights_version", "projection_build_hash", "projection_item_count", "projection_identity_hash", "target_institution", "candidate_pool_hash", "candidate_facts_hash", "unresolved_candidate_ids_hash", "candidate_count", "unresolved_count")
_STAGE5_FIELDS = ("job_id", "receipt_contract_version", "constraint_contract_version", "stage3_contract_hash", "stage3_receipt_hash", "candidate_facts_hash", "stage4_receipt_hash", "projection_identity_hash", "constraint_hash", "question_count")


def _json(raw: object, reason: str) -> Any:
    try: return json.loads(raw)
    except (TypeError, ValueError, RecursionError): _fail("exam_stage6_hash_mismatch", reason, "持久化 JSON 无效")


def _authority(db, job_id: str, worker_id: str, lease_attempt: int):
    job = db.execute("SELECT kind,status,cancel_requested,lease_owner,attempts,input_hash,deadline_policy_version FROM background_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None: raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if bool(job["cancel_requested"]) or str(job["status"] or "") == "cancel_requested": raise JobCancelled("任务已取消")
    if str(job["kind"] or "") != "exam-generation" or str(job["status"] or "") != "running" or str(job["lease_owner"] or "") != worker_id or int(job["attempts"] or 0) != lease_attempt:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    stage3 = db.execute("SELECT * FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,)).fetchone()
    stage4 = db.execute("SELECT * FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()
    stage5 = db.execute("SELECT * FROM exam_generation_stage5_constraint_receipts WHERE job_id=?", (job_id,)).fetchone()
    stage5_head = db.execute("SELECT * FROM exam_generation_stage5_heads WHERE job_id=?", (job_id,)).fetchone()
    binding = db.execute("SELECT * FROM exam_generation_attempt_bindings WHERE job_id=?", (job_id,)).fetchone()
    input_row = db.execute("SELECT * FROM job_inputs WHERE job_id=?", (job_id,)).fetchone()
    if None in (stage3, stage4, stage5, stage5_head, binding, input_row): _fail("exam_stage6_contract_conflict", "upstream_receipt_missing", "阶段 6 缺少冻结输入或阶段 3/4/5 权威制品")
    input_payload = _json(input_row["payload_json"], "job_input_payload_invalid")
    if (_digest(input_payload) != input_row["payload_hash"] or input_row["payload_hash"] != binding["job_input_hash"]
            or binding["binding_hash"] != _digest({x: binding[x] for x in _BINDING_FIELDS})):
        _fail("exam_stage6_hash_mismatch", "job_input_or_binding_hash_mismatch", "冻结任务输入或 attempt binding 漂移")
    if not hmac.compare_digest(str(stage3["receipt_hash"]), _digest({x: stage3[x] for x in _STAGE3_FIELDS})): _fail("exam_stage6_hash_mismatch", "stage3_receipt_hash_mismatch", "阶段 3 回执漂移")
    if not hmac.compare_digest(str(stage4["receipt_hash"]), _digest({x: stage4[x] for x in _STAGE4_FIELDS})): _fail("exam_stage6_hash_mismatch", "stage4_receipt_hash_mismatch", "阶段 4 回执漂移")
    facts = _json(stage4["facts_payload_json"], "stage4_payload_invalid")
    constraint = _json(stage5["payload_json"], "stage5_constraint_payload_invalid")
    if (_digest({x: stage5[x] for x in _STAGE5_FIELDS}) != stage5["receipt_hash"]
            or _digest(facts) != stage4["candidate_facts_hash"] or _digest(constraint) != stage5["constraint_hash"]
            or stage4["stage3_receipt_hash"] != stage3["receipt_hash"] or stage5["stage3_receipt_hash"] != stage3["receipt_hash"]
            or stage5["stage4_receipt_hash"] != stage4["receipt_hash"] or stage5_head["constraint_hash"] != stage5["constraint_hash"]):
        _fail("exam_stage6_hash_mismatch", "upstream_identity_drift", "阶段 3/4/5 传递身份漂移")
    if _publication_residue(db, job_id): _fail("published_recovery_required", "publication_residue_detected", "任务已有发布残留，必须先恢复发布")
    return stage3, stage4, stage5, stage5_head, constraint, binding, input_row, input_payload, job


def _head(row) -> Stage6Head:
    return Stage6Head(str(row["job_id"]), str(row["context_hash"]), str(row["constraint_hash"]), row["material_hash"], row["active_graph_hash"], row["score_set_hash"], row["audit_hash"], str(row["ledger_head_hash"]), int(row["ledger_entry_count"]), str(row["retry_event_head_hash"]), int(row["retry_event_count"]), int(row["transport_retry_count"]), int(row["schema_correction_count"]), int(row["transport_retry_limit"]), row["selection_hash"], row["checkpoint_hash"], int(row["revision"]))


def _context_payload(job_id: str, stage3, stage5, constraint: Mapping[str, Any], binding, input_row, input_payload: Mapping[str, Any], job) -> dict[str, Any]:
    single_version = str(binding["single_paper_contract_version"])
    paper_count = input_payload.get("paper_count", 1)
    if type(paper_count) is not int or paper_count != 1: _fail("exam_stage6_contract_conflict", "single_paper_input_invalid", "冻结任务输入不是单卷合同")
    policy = input_payload.get("exam_time_policy")
    if not isinstance(policy, Mapping) or not str(policy.get("version") or "").strip() or policy.get("version") != job["deadline_policy_version"]:
        _fail("exam_stage6_contract_conflict", "time_policy_authority_invalid", "冻结任务输入的时限策略权威无效")
    recent = constraint.get("recent_usage")
    recent_payload = recent.get("payload") if isinstance(recent, Mapping) else None
    if not isinstance(recent_payload, Mapping):
        _fail("exam_stage6_contract_conflict", "recent_usage_authority_invalid", "阶段 5 最近使用快照权威无效")
    if set(recent_payload) == {"excluded_candidate_ids", "excluded_content_hashes"}:
        # Durable Stage 5 v1 recovery only.
        candidate_ids = recent_payload["excluded_candidate_ids"]
        content_hashes = recent_payload["excluded_content_hashes"]
    elif set(recent_payload) == {
        "mode", "snapshot_identity", "snapshot_hash",
        "observed_candidate_ids", "observed_content_hashes",
        "candidate_id_hits", "content_hash_hits",
    }:
        snapshot_hash = recent_payload["snapshot_hash"]
        if (
            recent_payload["mode"] != "audit_only"
            or not isinstance(recent_payload["snapshot_identity"], str)
            or not recent_payload["snapshot_identity"].strip()
            or not isinstance(snapshot_hash, str)
            or _HASH_RE.fullmatch(snapshot_hash) is None
            or recent.get("version") != f"past-v3-stage5-recent-usage-audit-only-v2:sha256-{snapshot_hash}"
        ):
            _fail("exam_stage6_contract_conflict", "recent_usage_authority_invalid", "阶段 5 最近使用只审计权威无效")
        candidate_ids = recent_payload["observed_candidate_ids"]
        content_hashes = recent_payload["observed_content_hashes"]
        hit_ids = recent_payload["candidate_id_hits"]
        hit_hashes = recent_payload["content_hash_hits"]
        if (
            not isinstance(candidate_ids, list) or not isinstance(content_hashes, list)
            or any(not isinstance(x, str) or not x for x in candidate_ids)
            or any(not isinstance(x, str) or _HASH_RE.fullmatch(x) is None for x in content_hashes)
            or candidate_ids != sorted(set(candidate_ids)) or content_hashes != sorted(set(content_hashes))
            or not isinstance(hit_ids, list) or not isinstance(hit_hashes, list)
            or any(not isinstance(x, str) or not x for x in hit_ids)
            or any(not isinstance(x, str) or _HASH_RE.fullmatch(x) is None for x in hit_hashes)
            or hit_ids != sorted(set(hit_ids)) or hit_hashes != sorted(set(hit_hashes))
            or not set(hit_ids).issubset(set(candidate_ids) if isinstance(candidate_ids, list) else set())
            or not set(hit_hashes).issubset(set(content_hashes) if isinstance(content_hashes, list) else set())
        ):
            _fail("exam_stage6_contract_conflict", "recent_usage_authority_invalid", "阶段 5 最近使用命中证据无效")
    else:
        _fail("exam_stage6_contract_conflict", "recent_usage_authority_invalid", "阶段 5 最近使用快照权威无效")
    if (not isinstance(candidate_ids, list) or not isinstance(content_hashes, list) or len(candidate_ids) > 256 or len(content_hashes) > 256
            or any(not isinstance(x, str) or not x for x in candidate_ids)
            or any(not isinstance(x, str) or _HASH_RE.fullmatch(x) is None for x in content_hashes)
            or candidate_ids != sorted(set(candidate_ids)) or content_hashes != sorted(set(content_hashes))):
        _fail("exam_stage6_contract_conflict", "recent_usage_authority_invalid", "阶段 5 最近使用快照超限或身份无效")
    candidate_hashes = sorted(_digest(x) for x in set(candidate_ids)); normalized_content = sorted(set(content_hashes))
    recent_hash = _digest({"recent_candidate_hashes": candidate_hashes, "recent_content_hashes": normalized_content})
    if recent.get("hash") != _digest({"version": recent.get("version"), "payload": recent_payload}):
        _fail("exam_stage6_hash_mismatch", "recent_usage_receipt_hash_mismatch", "阶段 5 最近使用事实回执漂移")
    return {
        "version": "past-v3-stage6-context-v1", "job_id": job_id,
        "stage3_contract_hash": stage3["stage3_contract_hash"], "stage3_receipt_hash": stage3["receipt_hash"],
        "constraint_hash": stage5["constraint_hash"], "stage5_receipt_hash": stage5["receipt_hash"],
        "input_payload_hash": input_row["payload_hash"], "single_paper_contract_version": single_version,
        "single_paper_contract_hash": _digest({"single_paper_contract_version": single_version, "paper_count": 1}),
        "recent_candidate_hashes": candidate_hashes, "recent_content_hashes": normalized_content,
        "recent_snapshot_hash": recent_hash, "time_policy_version": str(policy["version"]),
    }


def _load_head(db, job_id: str):
    row = db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if row is None: _fail("exam_stage6_contract_conflict", "stage6_head_missing", "阶段 6 head 不存在")
    return row


def _graph_object(row) -> FrozenCandidateEdgeGraph:
    payload = _json(row["payload_json"], "stored_graph_json_invalid")
    if _digest(payload) != row["graph_hash"]: _fail("exam_stage6_hash_mismatch", "stored_graph_hash_mismatch", "阶段 5 图 payload 漂移")
    return FrozenCandidateEdgeGraph(row["graph_contract_version"], row["constraint_hash"], row["candidate_facts_hash"], row["graph_mode"], int(row["edge_count"]), row["graph_hash"], _canonical(payload))


def _authoritative_graph(db, job_id: str, graph_hash: str) -> tuple[FrozenCandidateEdgeGraph, FrozenCandidateEdgeGraph | None]:
    row = db.execute("SELECT * FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, graph_hash)).fetchone()
    if row is None: _fail("exam_stage6_contract_conflict", "authoritative_graph_missing", "阶段 5 权威图不存在")
    graph = _graph_object(row); base = None
    if row["graph_mode"] == "expanded":
        base_row = db.execute("SELECT * FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, row["base_graph_hash"])).fetchone()
        if base_row is None: _fail("exam_stage6_contract_conflict", "authoritative_base_graph_missing", "扩展图缺少权威初始图")
        base = _graph_object(base_row)
    return graph, base


def _authoritative_material(db, job_id: str, graph_hash: str, question_count: int) -> MaterialPreheatReceipt:
    graph, base = _authoritative_graph(db, job_id, graph_hash)
    facts_row = db.execute("SELECT facts_payload_json FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()
    if facts_row is None: _fail("exam_stage6_contract_conflict", "candidate_facts_missing", "阶段 4 权威候选事实不存在")
    facts = _json(facts_row[0], "stage4_payload_invalid")
    return build_material_preheat_receipt(graph=graph, base_graph=base, stage4=facts, question_count=question_count)


def _window_result(payload: Mapping[str, Any]) -> WindowScoreResult:
    return WindowScoreResult(payload["version"], payload["graph_hash"], payload["window_hash"], payload["request_hash"], payload["numeric_response_hash"], payload["audit_response_hash"], _canonical(payload))


def _cas(db, job_id: str, expected_revision: int, timestamp: str, setters: Mapping[str, Any]) -> Stage6Head:
    if type(expected_revision) is not int or expected_revision < 1: _fail("exam_stage6_store_failed", "expected_revision_invalid", "阶段 6 CAS revision 无效")
    assignments = "".join(f"{key}=?," for key in setters)
    cursor = db.execute(f"UPDATE {HEADS} SET {assignments}revision=revision+1,updated_at=? WHERE job_id=? AND revision=?", (*setters.values(), timestamp, job_id, expected_revision))
    if cursor.rowcount != 1: _fail("exam_stage6_contract_conflict", "stage6_head_cas_lost", "阶段 6 head CAS 已丢失", retryable=True)
    return _head(_load_head(db, job_id))


def persist_material_receipt(db, *, job_id: str, receipt: MaterialPreheatReceipt, worker_id: str, lease_attempt: int, expected_revision: int | None = None, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or not isinstance(receipt, MaterialPreheatReceipt): _fail("exam_stage6_store_failed", "material_input_invalid", "材料回执写入参数无效")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        stage3, stage4, stage5, stage5_head, constraint, binding, input_row, input_payload, job = _authority(db, job_id, worker_id, lease_attempt)
        context = _context_payload(job_id, stage3, stage5, constraint, binding, input_row, input_payload, job)
        context_hash = _digest(context)
        payload = receipt.to_dict()
        if _digest(payload) != receipt.material_hash or receipt.candidate_facts_hash != stage4["candidate_facts_hash"]: _fail("exam_stage6_hash_mismatch", "material_hash_or_facts_mismatch", "材料回执身份无效")
        graph = db.execute("SELECT graph_mode,base_graph_hash,candidate_count,payload_json FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, receipt.graph_hash)).fetchone()
        if graph is None or receipt.graph_hash not in (stage5_head["initial_graph_hash"], stage5_head["active_graph_hash"]): _fail("exam_stage6_contract_conflict", "material_graph_not_authoritative", "材料未绑定阶段 5 job-scoped graph")
        limit = (4 if graph["graph_mode"] == "initial" else 6) * int(stage5["question_count"])
        if receipt.candidate_count > limit or receipt.candidate_count != int(graph["candidate_count"]): _fail("exam_stage6_budget_exceeded", "material_candidate_boundary_invalid", "材料候选规模违反 4N/6N 边界")
        rebuilt_material = _authoritative_material(db, job_id, receipt.graph_hash, int(stage5["question_count"]))
        if receipt != rebuilt_material or payload != rebuilt_material.to_dict():
            _fail("exam_stage6_hash_mismatch", "material_authority_rebuild_mismatch", "材料回执不能由阶段 4 事实和阶段 5 图逐字重建")
        existing = db.execute(f"SELECT * FROM {MATERIALS} WHERE job_id=? AND material_hash=?", (job_id, receipt.material_hash)).fetchone()
        if existing is not None:
            head = db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
            if head is not None and head["material_hash"] == receipt.material_hash: return _head(head)
            _fail("exam_stage6_contract_conflict", "material_head_conflict", "材料回执已存在但未闭合到 head")
        stored_root = db.execute(f"SELECT context_hash,payload_json FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()
        if stored_root is None:
            if graph["graph_mode"] != "initial" or expected_revision is not None: _fail("exam_stage6_contract_conflict", "initial_material_required_first", "阶段 6 必须先持久化初始图材料")
            db.execute(f"INSERT INTO {ROOTS}(job_id,context_hash,context_version,stage3_contract_hash,stage3_receipt_hash,constraint_hash,stage5_receipt_hash,input_payload_hash,single_paper_contract_version,single_paper_contract_hash,recent_snapshot_hash,time_policy_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, context_hash, context["version"], context["stage3_contract_hash"], context["stage3_receipt_hash"], context["constraint_hash"], context["stage5_receipt_hash"], context["input_payload_hash"], context["single_paper_contract_version"], context["single_paper_contract_hash"], context["recent_snapshot_hash"], context["time_policy_version"], _canonical(context), timestamp))
        elif stored_root["context_hash"] != context_hash or _json(stored_root["payload_json"], "stored_context_json_invalid") != context:
            _fail("exam_stage6_hash_mismatch", "stored_context_authority_mismatch", "阶段 6 context 回执与当前权威链漂移")
        body = {"job_id": job_id, "material_hash": receipt.material_hash, "context_hash": context_hash, "graph_hash": receipt.graph_hash, "candidate_facts_hash": receipt.candidate_facts_hash, "candidate_count": receipt.candidate_count}
        db.execute(f"INSERT INTO {MATERIALS}(job_id,material_hash,material_version,context_hash,graph_hash,candidate_facts_hash,candidate_count,payload_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (job_id, receipt.material_hash, receipt.version, context_hash, receipt.graph_hash, receipt.candidate_facts_hash, receipt.candidate_count, _canonical(payload), _digest(body), timestamp))
        db.execute(f"INSERT INTO {LEDGER_STATES}(job_id,graph_hash,head_hash,ledger_version,entry_count,previous_head_hash,state_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, receipt.graph_hash, ZERO_HASH, REJECTION_LEDGER_VERSION, 0, None, _canonical({"version": REJECTION_LEDGER_VERSION, "graph_hash": receipt.graph_hash, "entry_count": 0, "head_hash": ZERO_HASH}), timestamp))
        head_row = db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
        if graph["graph_mode"] == "initial":
            if head_row is not None: _fail("exam_stage6_contract_conflict", "duplicate_initial_material", "阶段 6 初始材料已持久化")
            db.execute(f"INSERT INTO {HEADS}(job_id,context_hash,constraint_hash,material_hash,active_graph_hash,score_set_hash,audit_hash,ledger_head_hash,ledger_entry_count,retry_event_head_hash,retry_event_count,transport_retry_count,schema_correction_count,transport_retry_limit,selection_hash,checkpoint_hash,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, context_hash, stage5["constraint_hash"], receipt.material_hash, receipt.graph_hash, None, None, ZERO_HASH, 0, ZERO_HASH, 0, 0, 0, 0, None, None, 1, timestamp, timestamp))
            return _head(_load_head(db, job_id))
        if head_row is None or graph["base_graph_hash"] != stage5_head["initial_graph_hash"] or head_row["active_graph_hash"] != graph["base_graph_hash"]:
            _fail("exam_stage6_contract_conflict", "expanded_material_base_mismatch", "扩展材料未绑定当前阶段 5 初始图")
        base_row = db.execute("SELECT candidate_count,payload_json FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, graph["base_graph_hash"])).fetchone()
        base_payload, expanded_payload = _json(base_row["payload_json"], "base_graph_json_invalid"), _json(graph["payload_json"], "expanded_graph_json_invalid")
        before = {(x["slot_id"], x["candidate_id"]): x for x in base_payload.get("edges", [])}; after = {(x["slot_id"], x["candidate_id"]): x for x in expanded_payload.get("edges", [])}
        added = set(after) - set(before)
        per_slot: dict[str, int] = {}
        for slot, _candidate in added: per_slot[slot] = per_slot.get(slot, 0) + 1
        if (not set(before).issubset(after) or any(after[key] != value for key, value in before.items()) or any(value > 2 for value in per_slot.values())
                or int(base_row["candidate_count"]) > 4 * int(stage5["question_count"]) or int(graph["candidate_count"]) > 6 * int(stage5["question_count"])):
            _fail("exam_stage6_budget_exceeded", "expanded_graph_boundary_invalid", "扩展图违反追加边或 4N/6N 边界")
        return _cas(db, job_id, expected_revision, timestamp, {"material_hash": receipt.material_hash, "active_graph_hash": receipt.graph_hash, "score_set_hash": None, "audit_hash": None, "ledger_head_hash": ZERO_HASH, "ledger_entry_count": 0, "selection_hash": None, "checkpoint_hash": None})


def persist_scoring_request(db, *, job_id: str, window: ScoringWindow, material_hash: str, request_json: str, expected_revision: int, worker_id: str, lease_attempt: int, transport_retry_limit: int = 0, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if (db.in_transaction or not isinstance(window, ScoringWindow) or type(transport_retry_limit) is not int
            or not 0 <= transport_retry_limit <= 16):
        _fail("exam_stage6_store_failed", "window_input_invalid", "评分窗口或重试预算写入参数无效")
    timestamp = _timestamp(created_at)
    request = _json(request_json, "request_json_invalid")
    request_body = dict(request); request_hash = request_body.pop("request_hash", None)
    if (request.get("version") != REQUEST_VERSION or request_hash != _digest(request_body) or request.get("window_hash") != window.window_hash
            or request.get("graph_hash") != window.graph_hash or request.get("material_hash") != material_hash):
        _fail("exam_stage6_hash_mismatch", "scoring_request_identity_invalid", "评分请求未绑定冻结窗口或材料")
    with immediate_transaction(db):
        _s3, stage4, stage5, _s5h, constraint, *_rest = _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        payload = window.to_dict()
        if _digest(payload) != window.window_hash or material_hash != head["material_hash"] or window.graph_hash != head["active_graph_hash"]: _fail("exam_stage6_contract_conflict", "window_head_binding_mismatch", "评分窗口未绑定当前材料与图")
        if (request.get("candidate_facts_hash") != stage4["candidate_facts_hash"] or request.get("profile_hash") != constraint.get("profile_hash") or request.get("projection_identity_hash") != stage4["projection_identity_hash"]):
            _fail("exam_stage6_hash_mismatch", "window_scoring_identity_mismatch", "评分窗口响应来源身份未绑定当前权威链")
        graph_object, base_object = _authoritative_graph(db, job_id, window.graph_hash)
        if base_object is None:
            expected_windows = build_scoring_windows(graph=graph_object, question_count=int(stage5["question_count"]), phase="initial")
        else:
            base_keys = {(x["slot_id"], x["candidate_id"]) for x in base_object.to_dict()["edges"]}
            new_edges = [x for x in graph_object.to_dict()["edges"] if (x["slot_id"], x["candidate_id"]) not in base_keys]
            affected = sorted({x["slot_id"] for x in new_edges})
            expected_windows = build_scoring_windows(graph=graph_object, base_graph=base_object, question_count=int(stage5["question_count"]), phase="expansion", affected_slot_ids=affected)
        expected_window = next((item for item in expected_windows if item.window_hash == window.window_hash), None)
        if expected_window is None or expected_window != window:
            _fail("exam_stage6_hash_mismatch", "window_authority_rebuild_mismatch", "评分窗口不属于权威图确定性窗口集合")
        material_object = _authoritative_material(db, job_id, window.graph_hash, int(stage5["question_count"]))
        rebuilt_request = render_scoring_request(window=expected_window, material_receipt=material_object, profile_hash=request["profile_hash"], projection_identity_hash=request["projection_identity_hash"], prompt_version=request["prompt_version"], model_id=request["model_id"])
        if rebuilt_request != _canonical(request):
            _fail("exam_stage6_hash_mismatch", "request_authority_rebuild_mismatch", "评分请求不能由权威窗口和材料逐字重建")
        existing = db.execute(f"SELECT request_json FROM {REQUESTS} WHERE job_id=? AND window_hash=?", (job_id, window.window_hash)).fetchone()
        policy_payload = {"version": "past-v3-stage6-retry-policy-v1", "job_id": job_id,
                          "transport_retry_limit": transport_retry_limit, "schema_correction_limit": 2}
        policy_hash = _digest(policy_payload)
        policy = db.execute(f"SELECT policy_hash,payload_json,transport_retry_limit FROM {RETRY_POLICIES} WHERE job_id=?", (job_id,)).fetchone()
        policy_setters: dict[str, Any] = {}
        if policy is None:
            if existing is not None: _fail("exam_stage6_hash_mismatch", "retry_policy_missing", "冻结评分请求缺少任务级重试策略")
            db.execute(f"INSERT INTO {RETRY_POLICIES}(job_id,policy_hash,policy_version,transport_retry_limit,schema_correction_limit,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                       (job_id, policy_hash, policy_payload["version"], transport_retry_limit, 2, _canonical(policy_payload), timestamp))
            policy_setters["transport_retry_limit"] = transport_retry_limit
        elif (policy["policy_hash"] != policy_hash or _json(policy["payload_json"], "stored_retry_policy_invalid") != policy_payload
              or int(policy["transport_retry_limit"]) != transport_retry_limit or int(head["transport_retry_limit"]) != transport_retry_limit):
            _fail("exam_stage6_contract_conflict", "retry_policy_conflict", "评分重试硬预算已冻结且不得漂移")
        if existing is not None:
            if _json(existing[0], "stored_request_json_invalid") != request: _fail("exam_stage6_contract_conflict", "stored_request_conflict", "评分窗口已有不同冻结请求")
            return _head(head)
        if int(head["revision"]) != expected_revision: _fail("exam_stage6_contract_conflict", "stage6_head_cas_lost", "阶段 6 head CAS 已丢失", retryable=True)
        receipt_body = {"job_id": job_id, "window_hash": window.window_hash, "graph_hash": window.graph_hash, "material_hash": material_hash}
        db.execute(f"INSERT INTO {WINDOWS}(job_id,window_hash,window_version,graph_hash,material_hash,phase,window_index,slot_count,edge_count,payload_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, window.window_hash, window.version, window.graph_hash, material_hash, window.phase, window.window_index, len(window.slot_ids), window.edge_count, _canonical(payload), _digest(receipt_body), timestamp))
        request_receipt = {"job_id": job_id, "window_hash": window.window_hash, "request_hash": request_hash}
        db.execute(f"INSERT INTO {REQUESTS}(job_id,window_hash,request_hash,request_version,request_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, window.window_hash, request_hash, request["version"], _canonical(request), _digest(request_receipt), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, policy_setters)


def persist_scoring_window(db, *, job_id: str, window: ScoringWindow, material_hash: str, request_json: str, response_json: str, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or not isinstance(window, ScoringWindow): _fail("exam_stage6_store_failed", "window_input_invalid", "评分窗口结果写入参数无效")
    timestamp = _timestamp(created_at); result = parse_scoring_response(response_json, request_json=request_json, window=window); result_payload = result.to_dict()
    request, response = _json(request_json, "request_json_invalid"), _json(response_json, "response_json_invalid")
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        frozen = db.execute(f"SELECT request_hash,request_json FROM {REQUESTS} WHERE job_id=? AND window_hash=?", (job_id, window.window_hash)).fetchone()
        if frozen is None or _json(frozen["request_json"], "stored_request_json_invalid") != request or frozen["request_hash"] != request.get("request_hash"):
            _fail("exam_stage6_contract_conflict", "scoring_request_not_frozen", "评分结果之前必须冻结完全相同的请求")
        existing = db.execute(f"SELECT outcome_status,response_hash FROM {RESPONSES} WHERE job_id=? AND window_hash=?", (job_id, window.window_hash)).fetchone()
        response_hash = _digest(response)
        if existing is not None:
            if existing["outcome_status"] == "succeeded" and existing["response_hash"] == response_hash: return _head(head)
            _fail("exam_stage6_contract_conflict", "scoring_outcome_conflict", "评分窗口已有终态结果")
        response_hash = _digest(response)
        outcome_body = {"job_id": job_id, "window_hash": window.window_hash, "request_hash": request["request_hash"], "outcome_status": "succeeded", "response_hash": response_hash}
        outcome_hash = _digest(outcome_body)
        db.execute(f"INSERT INTO {RESPONSES}(job_id,outcome_hash,window_hash,request_hash,outcome_status,terminal_reason,response_hash,response_version,response_json,parsed_scores_json,rejected_edges_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, outcome_hash, window.window_hash, request["request_hash"], "succeeded", None, response_hash, response["version"], _canonical(response), _canonical(result_payload), None, _digest(outcome_body), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {})


_RETRY_REASONS = {
    "transport_retry": frozenset({"unavailable", "transport_timeout", "transport_connection", "transport_rate_limited", "transport_unavailable", "transport_invalid_response"}),
    "schema_correction": frozenset({"validation_failed", "schema_invalid_json", "schema_validation_failed", "schema_identity_mismatch", "schema_forbidden_field"}),
}


def _retry_chain(db, job_id: str, head) -> tuple[Mapping[str, Any], ...]:
    policy = db.execute(f"SELECT * FROM {RETRY_POLICIES} WHERE job_id=?", (job_id,)).fetchone()
    if policy is None:
        if int(head["retry_event_count"]) != 0 or head["retry_event_head_hash"] != ZERO_HASH:
            _fail("exam_stage6_hash_mismatch", "retry_policy_missing", "重试事件链缺少冻结策略")
        return ()
    policy_payload = _json(policy["payload_json"], "stored_retry_policy_invalid")
    if (_digest(policy_payload) != policy["policy_hash"] or policy_payload != {"version": "past-v3-stage6-retry-policy-v1", "job_id": job_id,
            "transport_retry_limit": int(policy["transport_retry_limit"]), "schema_correction_limit": int(policy["schema_correction_limit"])}
            or int(policy["schema_correction_limit"]) != 2 or int(head["transport_retry_limit"]) != int(policy["transport_retry_limit"])):
        _fail("exam_stage6_hash_mismatch", "retry_policy_hash_mismatch", "任务级重试策略漂移")
    previous, events = ZERO_HASH, []
    counts = {"transport_retry": 0, "schema_correction": 0}
    for row in db.execute(f"SELECT * FROM {RETRY_EVENTS} WHERE job_id=? ORDER BY sequence", (job_id,)):
        payload = _json(row["payload_json"], "stored_retry_event_invalid"); body = dict(payload); embedded = body.pop("event_hash", None)
        request = db.execute(f"SELECT 1 FROM {REQUESTS} WHERE job_id=? AND window_hash=? AND request_hash=?", (job_id, row["window_hash"], row["request_hash"])).fetchone()
        if (embedded != row["event_hash"] or _digest(body) != embedded or int(row["sequence"]) != len(events) + 1
                or row["previous_event_hash"] != previous or payload.get("previous_event_hash") != previous
                or payload.get("sequence") != int(row["sequence"]) or payload.get("job_id") != job_id
                or payload.get("window_hash") != row["window_hash"] or payload.get("request_hash") != row["request_hash"]
                or payload.get("event_kind") != row["event_kind"] or payload.get("reason_code") != row["reason_code"]
                or row["event_kind"] not in _RETRY_REASONS or row["reason_code"] not in _RETRY_REASONS[row["event_kind"]] or request is None):
            _fail("exam_stage6_hash_mismatch", "retry_event_chain_mismatch", "重试事件链或请求归属漂移")
        counts[row["event_kind"]] += 1; previous = embedded; events.append(payload)
    if (len(events) != int(head["retry_event_count"]) or previous != head["retry_event_head_hash"]
            or counts["transport_retry"] != int(head["transport_retry_count"])
            or counts["schema_correction"] != int(head["schema_correction_count"])
            or counts["transport_retry"] > int(policy["transport_retry_limit"]) or counts["schema_correction"] > 2):
        _fail("exam_stage6_hash_mismatch", "retry_event_head_mismatch", "重试事件链 HEAD 或硬预算漂移")
    return tuple(events)


def append_scoring_retry_event(db, *, job_id: str, window_hash: str, event_kind: str, reason_code: str,
                               expected_revision: int, worker_id: str, lease_attempt: int,
                               created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if (db.in_transaction or event_kind not in _RETRY_REASONS or reason_code not in _RETRY_REASONS[event_kind]
            or not isinstance(window_hash, str) or _HASH_RE.fullmatch(window_hash) is None):
        _fail("exam_stage6_store_failed", "retry_event_input_invalid", "重试事件参数无效")
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        _retry_chain(db, job_id, head)
        request = db.execute(f"SELECT request_hash FROM {REQUESTS} WHERE job_id=? AND window_hash=?", (job_id, window_hash)).fetchone()
        outcome = db.execute(f"SELECT 1 FROM {RESPONSES} WHERE job_id=? AND window_hash=?", (job_id, window_hash)).fetchone()
        if request is None or outcome is not None: _fail("exam_stage6_contract_conflict", "retry_request_not_pending", "只能为冻结且未终结的评分请求追加重试事件")
        next_transport = int(head["transport_retry_count"]) + (event_kind == "transport_retry")
        next_schema = int(head["schema_correction_count"]) + (event_kind == "schema_correction")
        if next_transport > int(head["transport_retry_limit"]) or next_schema > 2:
            _fail("exam_stage6_budget_exceeded", "retry_budget_exhausted", "任务级评分重试硬预算已耗尽")
        sequence = int(head["retry_event_count"]) + 1
        body = {"version": "past-v3-stage6-retry-event-v1", "job_id": job_id, "sequence": sequence,
                "previous_event_hash": head["retry_event_head_hash"], "window_hash": window_hash,
                "request_hash": request["request_hash"], "event_kind": event_kind, "reason_code": reason_code}
        event_hash = _digest(body); payload = {**body, "event_hash": event_hash}
        db.execute(f"INSERT INTO {RETRY_EVENTS}(job_id,event_hash,sequence,previous_event_hash,window_hash,request_hash,event_kind,reason_code,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (job_id, event_hash, sequence, head["retry_event_head_hash"], window_hash, request["request_hash"], event_kind, reason_code, _canonical(payload), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"retry_event_head_hash": event_hash, "retry_event_count": sequence,
                    "transport_retry_count": next_transport, "schema_correction_count": next_schema})


def persist_scoring_terminal_failure(db, *, job_id: str, window_hash: str, reason: str, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or reason not in {"transport_exhausted", "schema_correction_exhausted"}: _fail("exam_stage6_store_failed", "terminal_failure_reason_invalid", "评分终止原因无效")
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        row = db.execute(f"SELECT w.phase,w.payload_json,r.request_hash FROM {WINDOWS} w JOIN {REQUESTS} r ON r.job_id=w.job_id AND r.window_hash=w.window_hash WHERE w.job_id=? AND w.window_hash=?", (job_id, window_hash)).fetchone()
        if row is None: _fail("exam_stage6_contract_conflict", "scoring_request_not_frozen", "终止失败之前必须冻结评分请求")
        existing = db.execute(f"SELECT outcome_status,terminal_reason FROM {RESPONSES} WHERE job_id=? AND window_hash=?", (job_id, window_hash)).fetchone()
        if existing is not None:
            if existing["outcome_status"] == "terminal_failed" and existing["terminal_reason"] == reason: return _head(head)
            _fail("exam_stage6_contract_conflict", "scoring_outcome_conflict", "评分窗口已有终态结果")
        window = _json(row["payload_json"], "stored_window_json_invalid")
        rejected = [{"slot_id": x["slot_id"], "candidate_id": x["candidate_id"]} for x in window.get("edges", [])] if row["phase"] == "expansion" else []
        body = {"job_id": job_id, "window_hash": window_hash, "request_hash": row["request_hash"], "outcome_status": "terminal_failed", "terminal_reason": reason, "rejected_edges": rejected}
        outcome_hash = _digest(body)
        db.execute(f"INSERT INTO {RESPONSES}(job_id,outcome_hash,window_hash,request_hash,outcome_status,terminal_reason,response_hash,response_version,response_json,parsed_scores_json,rejected_edges_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, outcome_hash, window_hash, row["request_hash"], "terminal_failed", reason, None, None, None, None, _canonical(rejected), _digest(body), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {})


def persist_score_receipt(db, *, job_id: str, receipt: ScoreReceipt, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None, _legacy_fixture_capability: object = None) -> Stage6Head:
    if _legacy_fixture_capability is not _LEGACY_SCORE_FIXTURE_WRITE_CAPABILITY:
        _fail(
            "exam_stage6_legacy_write_disabled",
            "legacy_score_write_disabled",
            "旧版 score/penalty 新写入已关闭，仅保留历史只读恢复",
        )
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction or not isinstance(receipt, ScoreReceipt): _fail("exam_stage6_store_failed", "score_input_invalid", "评分回执写入参数无效")
    score, audit = receipt.to_stage5_dict(), receipt.to_audit_dict(); timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        _s3, _s4, stage5, _s5h, _contract, *_rest = _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        if head["score_set_hash"] == receipt.score_set_hash: return _head(head)
        score_body = dict(score); embedded_score_hash = score_body.pop("hash", None)
        if receipt.graph_hash != head["active_graph_hash"] or embedded_score_hash != receipt.score_set_hash or _digest(score_body) != receipt.score_set_hash: _fail("exam_stage6_hash_mismatch", "score_head_binding_mismatch", "评分回执未绑定 active graph")
        audit_body = dict(audit); embedded = audit_body.pop("audit_hash", None)
        if embedded != receipt.audit_hash or _digest(audit_body) != receipt.audit_hash or audit.get("version") != AUDIT_RECEIPT_VERSION: _fail("exam_stage6_hash_mismatch", "score_audit_hash_mismatch", "评分审计回执漂移")
        initial_graph_hash = _s5h["initial_graph_hash"]
        rows = list(db.execute(f"SELECT w.phase,w.window_index,w.graph_hash,w.payload_json,r.response_hash,r.parsed_scores_json FROM {WINDOWS} w JOIN {RESPONSES} r ON r.job_id=w.job_id AND r.window_hash=w.window_hash AND r.outcome_status='succeeded' WHERE w.job_id=? AND ((w.graph_hash=? AND w.phase='initial') OR (w.graph_hash=? AND w.phase='expansion')) ORDER BY CASE w.phase WHEN 'initial' THEN 0 ELSE 1 END,w.window_index", (job_id, initial_graph_hash, receipt.graph_hash)))
        initial = [x for x in rows if x["phase"] == "initial"]
        expansion = [x for x in rows if x["phase"] == "expansion"]
        if len(initial) != math.ceil(int(stage5["question_count"]) / 4) or len(expansion) > math.ceil(int(stage5["question_count"]) / 4): _fail("exam_stage6_budget_exceeded", "scoring_window_count_invalid", "评分窗口数量违反边界")
        results = [_json(row["parsed_scores_json"], "parsed_scores_json_invalid") for row in rows]
        parsed = [item for result in results for item in result.get("scores", [])]
        numeric = sorted(({k: item[k] for k in ("slot_id", "candidate_id", "score", "penalty")} for item in parsed), key=lambda x: (x["slot_id"], x["candidate_id"]))
        audited = sorted(parsed, key=lambda x: (x["slot_id"], x["candidate_id"]))
        ordered_results = sorted(results, key=lambda x: (x["phase"], x["window_index"], x["window_hash"]))
        expected_windows = [{key: result[key] for key in ("phase", "window_index", "graph_hash", "slot_ids", "window_hash", "request_hash", "material_hash", "numeric_response_hash")} for result in ordered_results]
        expected_audits = [result["audit_response_hash"] for result in ordered_results]
        identity = score.get("scoring_identity", {})
        rejected_edges = []
        for ledger_row in db.execute(f"SELECT payload_json FROM {LEDGER_ENTRIES} WHERE job_id=? AND graph_hash=? ORDER BY sequence", (job_id, receipt.graph_hash)):
            ledger = _json(ledger_row[0], "stored_ledger_json_invalid")
            rejected_edges.extend(ledger.get("rejected_edges", []))
        normalized_rejected = [{"slot_id": slot, "candidate_id": candidate} for slot, candidate in sorted({(x["slot_id"], x["candidate_id"]) for x in rejected_edges})]
        terminal_rows = list(db.execute(f"SELECT w.phase,o.rejected_edges_json FROM {WINDOWS} w JOIN {RESPONSES} o ON o.job_id=w.job_id AND o.window_hash=w.window_hash WHERE w.job_id=? AND o.outcome_status='terminal_failed'", (job_id,)))
        if any(row["phase"] == "initial" for row in terminal_rows):
            _fail("exam_stage6_contract_conflict", "initial_scoring_terminal_failure", "初始评分存在终止失败，禁止形成评分回执")
        terminal_rejected = {(x["slot_id"], x["candidate_id"]) for row in terminal_rows for x in _json(row["rejected_edges_json"], "terminal_rejected_edges_invalid")}
        if terminal_rejected != {(x["slot_id"], x["candidate_id"]) for x in normalized_rejected}:
            _fail("exam_stage6_hash_mismatch", "terminal_ledger_union_mismatch", "扩展终止失败边与拒绝账本联合不一致")
        graph_object, base_object = _authoritative_graph(db, job_id, receipt.graph_hash)
        rebuilt = build_score_receipt(graph=graph_object, base_graph=base_object, parsed_windows=[_window_result(x) for x in results], rejected_edges=normalized_rejected, rejection_ledger_head_hash=str(head["ledger_head_hash"]))
        if receipt != rebuilt or score != rebuilt.to_stage5_dict() or audit != rebuilt.to_audit_dict():
            _fail("exam_stage6_hash_mismatch", "score_authority_rebuild_mismatch", "评分回执不能由持久化成功响应和拒绝账本逐字重建")
        if (numeric != score.get("scores") or audited != audit.get("scores")
                or identity.get("windows") != expected_windows
                or identity.get("constraint_hash") != stage5["constraint_hash"]
                or identity.get("base_graph_hash") != (initial_graph_hash if receipt.graph_hash != initial_graph_hash else None)
                or identity.get("rejection_ledger_head_hash") != head["ledger_head_hash"]
                or identity.get("rejected_edges") != normalized_rejected
                or identity.get("candidate_facts_hash") != _s4["candidate_facts_hash"]
                or identity.get("profile_hash") != _contract.get("profile_hash")
                or identity.get("projection_identity_hash") != _s4["projection_identity_hash"]
                or audit.get("window_audit_hashes") != expected_audits):
            _fail("exam_stage6_hash_mismatch", "score_response_transitive_mismatch", "评分回执与窗口响应身份不闭合")
        response_set_hash = _digest([result["numeric_response_hash"] for result in ordered_results])
        body = {"job_id": job_id, "score_set_hash": receipt.score_set_hash, "graph_hash": receipt.graph_hash, "audit_hash": receipt.audit_hash, "response_set_hash": response_set_hash}
        db.execute(f"INSERT INTO {SCORES}(job_id,score_set_hash,score_version,graph_hash,audit_hash,response_set_hash,score_payload_json,audit_payload_json,receipt_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (job_id, receipt.score_set_hash, receipt.version, receipt.graph_hash, receipt.audit_hash, response_set_hash, _canonical(score), _canonical(audit), _digest(body), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"score_set_hash": receipt.score_set_hash, "audit_hash": receipt.audit_hash})


def append_rejection_ledger(db, *, job_id: str, new_head: RejectionLedgerHead, entry: Mapping[str, Any], expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or not isinstance(new_head, RejectionLedgerHead) or not isinstance(entry, Mapping): _fail("exam_stage6_store_failed", "ledger_input_invalid", "拒绝账本写入参数无效")
    payload = dict(entry); body = dict(payload); entry_hash = body.pop("entry_hash", None)
    if new_head.version != REJECTION_LEDGER_VERSION or entry_hash != new_head.head_hash or _digest(body) != entry_hash or payload.get("sequence") != new_head.entry_count: _fail("exam_stage6_hash_mismatch", "ledger_entry_hash_mismatch", "拒绝账本条目身份无效")
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        if head["ledger_head_hash"] == new_head.head_hash: return _head(head)
        if new_head.graph_hash != head["active_graph_hash"] or payload.get("previous_head_hash") != head["ledger_head_hash"] or new_head.entry_count != int(head["ledger_entry_count"]) + 1: _fail("exam_stage6_contract_conflict", "ledger_chain_mismatch", "拒绝账本未顺序追加到当前 HEAD")
        graph_row = db.execute("SELECT graph_mode,base_graph_hash,payload_json FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, new_head.graph_hash)).fetchone()
        graph_payload = _json(graph_row["payload_json"], "ledger_graph_json_invalid") if graph_row is not None else {}
        frozen_edges = {(x.get("slot_id"), x.get("candidate_id")) for x in graph_payload.get("edges", []) if isinstance(x, Mapping)}
        rejected = {(x.get("slot_id"), x.get("candidate_id")) for x in payload.get("rejected_edges", []) if isinstance(x, Mapping)}
        if graph_row is None or graph_row["graph_mode"] != "expanded" or not graph_row["base_graph_hash"]:
            _fail("exam_stage6_contract_conflict", "ledger_requires_expanded_graph", "阶段 6 拒绝账本只能记录扩展评分新增边")
        base_row = db.execute("SELECT payload_json FROM exam_generation_stage5_graph_receipts WHERE job_id=? AND graph_hash=?", (job_id, graph_row["base_graph_hash"])).fetchone()
        base_payload = _json(base_row[0], "ledger_base_graph_json_invalid") if base_row is not None else {}
        base_edges = {(x.get("slot_id"), x.get("candidate_id")) for x in base_payload.get("edges", []) if isinstance(x, Mapping)}
        if not rejected or not rejected.issubset(frozen_edges - base_edges): _fail("exam_stage6_contract_conflict", "ledger_edge_not_expansion_addition", "拒绝账本包含初始边或非冻结扩展边")
        if int(head["revision"]) != expected_revision: _fail("exam_stage6_contract_conflict", "stage6_head_cas_lost", "阶段 6 head CAS 已丢失", retryable=True)
        state = {"version": REJECTION_LEDGER_VERSION, "graph_hash": new_head.graph_hash, "entry_count": new_head.entry_count, "previous_head_hash": payload["previous_head_hash"], "head_hash": new_head.head_hash}
        db.execute(f"INSERT INTO {LEDGER_STATES}(job_id,graph_hash,head_hash,ledger_version,entry_count,previous_head_hash,state_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, new_head.graph_hash, new_head.head_hash, new_head.version, new_head.entry_count, payload["previous_head_hash"], _canonical(state), timestamp))
        db.execute(f"INSERT INTO {LEDGER_ENTRIES}(job_id,graph_hash,entry_hash,sequence,previous_head_hash,reason_code,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, new_head.graph_hash, entry_hash, new_head.entry_count, payload["previous_head_hash"], payload["reason_code"], _canonical(payload), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"ledger_head_hash": entry_hash, "ledger_entry_count": new_head.entry_count})


def persist_selection_reference(db, *, job_id: str, selection_hash: str, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or not isinstance(selection_hash, str) or _HASH_RE.fullmatch(selection_hash) is None: _fail("exam_stage6_store_failed", "selection_reference_input_invalid", "选择引用参数无效")
    with immediate_transaction(db):
        _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        if head["selection_hash"] == selection_hash: return _head(head)
        row = db.execute("SELECT graph_hash,score_identity_hash,payload_json FROM exam_generation_stage5_selection_receipts WHERE job_id=? AND selection_hash=?", (job_id, selection_hash)).fetchone()
        if row is None: _fail("exam_stage6_contract_conflict", "job_scoped_selection_missing", "阶段 5 job-scoped selection 尚未持久化")
        payload = _json(row["payload_json"], "stage5_selection_json_invalid")
        if row["graph_hash"] != head["active_graph_hash"] or row["score_identity_hash"] != head["score_set_hash"] or payload.get("selection_hash") != selection_hash: _fail("exam_stage6_hash_mismatch", "selection_score_binding_mismatch", "选择未绑定当前评分回执或图")
        rejected = set()
        for ledger_row in db.execute(f"SELECT payload_json FROM {LEDGER_ENTRIES} WHERE job_id=? AND graph_hash=?", (job_id, row["graph_hash"])):
            ledger = _json(ledger_row[0], "stored_ledger_json_invalid")
            rejected.update((x.get("slot_id"), x.get("candidate_id")) for x in ledger.get("rejected_edges", []) if isinstance(x, Mapping))
        selected = {(x.get("slot_id"), x.get("candidate_id")) for x in payload.get("assignments", []) if isinstance(x, Mapping)}
        if rejected & selected: _fail("exam_stage6_contract_conflict", "selection_contains_rejected_edge", "阶段 5 selection 包含当前拒绝账本边")
        reference = {"version": SELECTION_REFERENCE_VERSION, "job_id": job_id, "selection_hash": selection_hash, "graph_hash": row["graph_hash"], "score_set_hash": row["score_identity_hash"]}
        ref_hash = _digest(reference)
        db.execute(f"INSERT INTO {SELECTION_REFS}(job_id,selection_hash,graph_hash,score_set_hash,reference_version,payload_json,reference_hash,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, selection_hash, row["graph_hash"], row["score_identity_hash"], SELECTION_REFERENCE_VERSION, _canonical(reference), ref_hash, timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"selection_hash": selection_hash})


def persist_checkpoint_v3(db, *, job_id: str, checkpoint: CheckpointV3, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage6Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or not isinstance(checkpoint, CheckpointV3): _fail("exam_stage6_store_failed", "checkpoint_input_invalid", "checkpoint v3 写入参数无效")
    payload = checkpoint.to_dict(); bindings = payload["bindings"]
    with immediate_transaction(db):
        stage3, stage4, stage5, stage5_head, constraint, binding, input_row, input_payload, job = _authority(db, job_id, worker_id, lease_attempt); head = _load_head(db, job_id)
        if head["checkpoint_hash"] == checkpoint.checkpoint_hash: return _head(head)
        expected = dict(bindings)
        context = _context_payload(job_id, stage3, stage5, constraint, binding, input_row, input_payload, job)
        root = db.execute(f"SELECT payload_json,context_hash FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()
        if root is None or _json(root["payload_json"], "stored_context_json_invalid") != context or _digest(context) != root["context_hash"] or root["context_hash"] != head["context_hash"]:
            _fail("exam_stage6_hash_mismatch", "checkpoint_root_authority_mismatch", "checkpoint 根权威回执漂移")
        authoritative = {
            "single_paper_contract_version": context["single_paper_contract_version"],
            "single_paper_contract_hash": context["single_paper_contract_hash"],
            "request_hash": stage3["generation_request_hash"], "stage3_contract_hash": stage3["stage3_contract_hash"],
            "profile_hash": constraint.get("profile_hash"), "projection_identity_hash": stage4["projection_identity_hash"],
            "candidate_facts_hash": stage4["candidate_facts_hash"], "constraint_hash": stage5["constraint_hash"],
            "initial_graph_hash": stage5_head["initial_graph_hash"], "material_receipt_hash": head["material_hash"],
            "score_receipt_hash": head["score_set_hash"], "score_audit_hash": head["audit_hash"],
            "rejection_ledger_head_hash": head["ledger_head_hash"], "rejection_ledger_entry_count": int(head["ledger_entry_count"]),
            "selection_hash": head["selection_hash"], "question_count": int(stage5["question_count"]),
            "recent_snapshot_hash": context["recent_snapshot_hash"], "time_policy_version": context["time_policy_version"],
        }
        if any(expected.get(key) != value for key, value in authoritative.items()): _fail("exam_stage6_hash_mismatch", "checkpoint_authority_binding_mismatch", "checkpoint v3 未绑定当前完整权威链")
        active_graph = str(head["active_graph_hash"])
        expanded = stage5_head["active_graph_hash"] if stage5_head["active_graph_hash"] != stage5_head["initial_graph_hash"] else None
        if expected.get("expanded_graph_hash") != expanded or active_graph != (expanded or stage5_head["initial_graph_hash"]): _fail("exam_stage6_hash_mismatch", "checkpoint_graph_binding_mismatch", "checkpoint v3 图身份漂移")
        stored_windows = list(db.execute(f"SELECT phase,window_hash FROM {WINDOWS} WHERE job_id=? ORDER BY phase,window_index", (job_id,)))
        initial_hashes = sorted(str(x["window_hash"]) for x in stored_windows if x["phase"] == "initial")
        expansion_hashes = sorted(str(x["window_hash"]) for x in stored_windows if x["phase"] == "expansion")
        if expected.get("initial_window_hashes") != initial_hashes or expected.get("expansion_window_hashes") != expansion_hashes: _fail("exam_stage6_hash_mismatch", "checkpoint_window_binding_mismatch", "checkpoint v3 窗口身份漂移")
        validate_checkpoint_v3(checkpoint, expected_bindings=expected)
        if expected.get("checkpoint_stage") != "stage6_scored":
            _fail("exam_stage6_contract_conflict", "stage7_authority_not_persisted", "阶段 7 权威制品尚未由对应仓储持久化，不能闭合 checkpoint")
        previous = head["checkpoint_hash"]
        sequence = db.execute(f"SELECT COUNT(*) FROM {CHECKPOINTS} WHERE job_id=?", (job_id,)).fetchone()[0] + 1
        db.execute(f"INSERT INTO {CHECKPOINTS}(job_id,checkpoint_hash,checkpoint_version,sequence,previous_checkpoint_hash,identity_hash,checkpoint_stage,graph_hash,material_hash,score_set_hash,audit_hash,ledger_head_hash,selection_hash,pre_adaptation_hash,final_exam_hash,stage7_pre_receipt_hash,stage7_final_receipt_hash,size_bytes,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, checkpoint.checkpoint_hash, checkpoint.version, sequence, previous, checkpoint.identity_hash, expected["checkpoint_stage"], active_graph, head["material_hash"], head["score_set_hash"], head["audit_hash"], head["ledger_head_hash"], head["selection_hash"], expected["pre_adaptation_hash"], expected["final_exam_hash"], expected["stage7_pre_receipt_hash"], expected["stage7_final_receipt_hash"], checkpoint.size_bytes, _canonical(payload), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"checkpoint_hash": checkpoint.checkpoint_hash})


def persist_legacy_migration_decision(db, *, job_id: str, decision: LegacyP1MigrationDecision, worker_id: str, lease_attempt: int, created_at: str | None = None) -> str:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or not isinstance(decision, LegacyP1MigrationDecision): _fail("exam_stage6_store_failed", "legacy_decision_input_invalid", "旧检查点迁移决策参数无效")
    payload = decision.to_dict()
    with immediate_transaction(db):
        stage3, stage4, stage5, _head5, constraint, binding, input_row, input_payload, job = _authority(db, job_id, worker_id, lease_attempt)
        context = _context_payload(job_id, stage3, stage5, constraint, binding, input_row, input_payload, job)
        migrated = payload["migrated_identity"]
        if (migrated["request_hash"] != stage3["generation_request_hash"] or migrated["profile_hash"] != constraint.get("profile_hash")
                or migrated["projection_identity_hash"] != stage4["projection_identity_hash"]
                or migrated["recent_snapshot_hash"] != context["recent_snapshot_hash"]
                or payload.get("requires_rescore_and_local_resolve") is not True):
            _fail("exam_stage6_hash_mismatch", "legacy_migration_authority_mismatch", "旧 P=1 白名单事实未绑定当前权威身份")
        existing = db.execute(f"SELECT payload_json FROM {LEGACY_DECISIONS} WHERE job_id=? AND decision_hash=?", (job_id, decision.decision_hash)).fetchone()
        if existing is not None:
            if _json(existing[0], "legacy_decision_json_invalid") != payload: _fail("exam_stage6_contract_conflict", "legacy_decision_conflict", "旧检查点迁移决策冲突")
            return decision.decision_hash
        db.execute(f"INSERT INTO {LEGACY_DECISIONS}(job_id,decision_hash,decision_version,source_version,migrated_identity_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, decision.decision_hash, decision.version, decision.source_version, decision.migrated_identity_hash, _canonical(payload), timestamp))
        return decision.decision_hash


def _verify_recovery(db, job_id: str, head) -> Stage6StoredState:
    _retry_chain(db, job_id, head)
    material_row = db.execute(f"SELECT * FROM {MATERIALS} WHERE job_id=? AND material_hash=?", (job_id, head["material_hash"])).fetchone()
    score_row = db.execute(f"SELECT * FROM {SCORES} WHERE job_id=? AND score_set_hash=?", (job_id, head["score_set_hash"])).fetchone()
    ref_row = db.execute(f"SELECT * FROM {SELECTION_REFS} WHERE job_id=? AND selection_hash=?", (job_id, head["selection_hash"])).fetchone()
    checkpoint_row = db.execute(f"SELECT * FROM {CHECKPOINTS} WHERE job_id=? AND checkpoint_hash=?", (job_id, head["checkpoint_hash"])).fetchone()
    if None in (material_row, score_row, ref_row, checkpoint_row): _fail("exam_stage6_incomplete", "closed_state_incomplete", "阶段 6 尚无可恢复的闭合 checkpoint")
    material = _json(material_row["payload_json"], "stored_material_json_invalid")
    question_count = int(db.execute("SELECT question_count FROM exam_generation_stage5_constraint_receipts WHERE job_id=?", (job_id,)).fetchone()[0])
    rebuilt_material = _authoritative_material(db, job_id, str(head["active_graph_hash"]), question_count)
    if material != rebuilt_material.to_dict() or material_row["material_hash"] != rebuilt_material.material_hash:
        _fail("exam_stage6_hash_mismatch", "recovery_material_authority_mismatch", "恢复材料不能由阶段 4/5 权威重建")
    score = _json(score_row["score_payload_json"], "stored_score_json_invalid")
    audit = _json(score_row["audit_payload_json"], "stored_audit_json_invalid")
    reference = _json(ref_row["payload_json"], "stored_selection_reference_json_invalid")
    checkpoint_payload = _json(checkpoint_row["payload_json"], "stored_checkpoint_json_invalid")
    score_body = dict(score); embedded_score_hash = score_body.pop("hash", None)
    material_receipt_body = {"job_id": job_id, "material_hash": material_row["material_hash"], "context_hash": material_row["context_hash"], "graph_hash": material_row["graph_hash"], "candidate_facts_hash": material_row["candidate_facts_hash"], "candidate_count": int(material_row["candidate_count"])}
    score_receipt_body = {"job_id": job_id, "score_set_hash": score_row["score_set_hash"], "graph_hash": score_row["graph_hash"], "audit_hash": score_row["audit_hash"], "response_set_hash": score_row["response_set_hash"]}
    if (_digest(material) != material_row["material_hash"] or _digest(material_receipt_body) != material_row["receipt_hash"]
            or embedded_score_hash != score_row["score_set_hash"] or _digest(score_body) != score_row["score_set_hash"]
            or _digest({k: v for k, v in audit.items() if k != "audit_hash"}) != score_row["audit_hash"]
            or audit.get("audit_hash") != score_row["audit_hash"] or _digest(score_receipt_body) != score_row["receipt_hash"]
            or _digest(reference) != ref_row["reference_hash"]
            or hashlib.sha256(_canonical(checkpoint_payload).encode()).hexdigest() != checkpoint_row["checkpoint_hash"]):
        _fail("exam_stage6_hash_mismatch", "stored_artifact_hash_mismatch", "阶段 6 持久化制品哈希漂移")
    windows, responses, results = [], [], []
    for row in db.execute(f"SELECT w.*,w.receipt_hash AS window_receipt_hash,q.request_json,q.receipt_hash AS request_receipt_hash,r.response_hash,r.request_hash,r.response_version,r.response_json,r.parsed_scores_json,r.receipt_hash AS response_receipt_hash,r.outcome_status FROM {WINDOWS} w JOIN {REQUESTS} q ON q.job_id=w.job_id AND q.window_hash=w.window_hash JOIN {RESPONSES} r ON r.job_id=w.job_id AND r.window_hash=w.window_hash WHERE w.job_id=? AND r.outcome_status='succeeded' ORDER BY w.phase,w.window_index", (job_id,)):
        window = _json(row["payload_json"], "stored_window_json_invalid")
        request = _json(row["request_json"], "stored_request_json_invalid"); response = _json(row["response_json"], "stored_response_json_invalid")
        window_receipt = {"job_id": job_id, "window_hash": row["window_hash"], "graph_hash": row["graph_hash"], "material_hash": row["material_hash"]}
        request_receipt = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"]}
        outcome_body = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"], "outcome_status": "succeeded", "response_hash": row["response_hash"]}
        if (_digest(window) != row["window_hash"] or _digest(window_receipt) != row["window_receipt_hash"]
                or _digest(request_receipt) != row["request_receipt_hash"] or _digest(response) != row["response_hash"]
                or _digest(outcome_body) != row["response_receipt_hash"]):
            _fail("exam_stage6_hash_mismatch", "stored_window_response_hash_mismatch", "评分窗口或响应漂移")
        stored_result = _json(row["parsed_scores_json"], "stored_window_result_json_invalid")
        window_object = ScoringWindow(row["window_version"], row["phase"], row["graph_hash"], int(row["window_index"]), tuple(window["slot_ids"]), int(row["edge_count"]), row["window_hash"], _canonical(window))
        graph_object, base_object = _authoritative_graph(db, job_id, row["graph_hash"])
        if base_object is None:
            authoritative_windows = build_scoring_windows(graph=graph_object, question_count=question_count, phase="initial")
        else:
            base_keys = {(x["slot_id"], x["candidate_id"]) for x in base_object.to_dict()["edges"]}
            affected = sorted({x["slot_id"] for x in graph_object.to_dict()["edges"] if (x["slot_id"], x["candidate_id"]) not in base_keys})
            authoritative_windows = build_scoring_windows(graph=graph_object, base_graph=base_object, question_count=question_count, phase="expansion", affected_slot_ids=affected)
        expected_window = next((x for x in authoritative_windows if x.window_hash == window_object.window_hash), None)
        if expected_window != window_object: _fail("exam_stage6_hash_mismatch", "recovery_window_authority_mismatch", "恢复评分窗口不属于权威窗口集合")
        authoritative_material = _authoritative_material(db, job_id, row["graph_hash"], question_count)
        rebuilt_request = render_scoring_request(window=window_object, material_receipt=authoritative_material, profile_hash=request["profile_hash"], projection_identity_hash=request["projection_identity_hash"], prompt_version=request["prompt_version"], model_id=request["model_id"])
        if rebuilt_request != _canonical(request): _fail("exam_stage6_hash_mismatch", "recovery_request_authority_mismatch", "恢复评分请求不能由权威制品重建")
        replay = parse_scoring_response(_canonical(response), request_json=_canonical(request), window=window_object).to_dict()
        if replay != stored_result: _fail("exam_stage6_hash_mismatch", "stored_window_result_mismatch", "评分响应回放结果漂移")
        results.append(replay)
        windows.append(window); responses.append({"request": request, "response": response})
    active_results = [x for x in results if (x["phase"] == "initial" and x["graph_hash"] == checkpoint_payload["bindings"]["initial_graph_hash"]) or (x["phase"] == "expansion" and x["graph_hash"] == checkpoint_row["graph_hash"])]
    active_results.sort(key=lambda x: (x["phase"], x["window_index"], x["window_hash"]))
    expected_numeric = sorted(({key: item[key] for key in ("slot_id", "candidate_id", "score", "penalty")} for result in active_results for item in result["scores"]), key=lambda x: (x["slot_id"], x["candidate_id"]))
    expected_audit = sorted((item for result in active_results for item in result["scores"]), key=lambda x: (x["slot_id"], x["candidate_id"]))
    expected_windows = [{key: result[key] for key in ("phase", "window_index", "graph_hash", "slot_ids", "window_hash", "request_hash", "material_hash", "numeric_response_hash")} for result in active_results]
    expected_response_set_hash = _digest([result["numeric_response_hash"] for result in active_results])
    if (score.get("scores") != expected_numeric or audit.get("scores") != expected_audit
            or score.get("scoring_identity", {}).get("windows") != expected_windows
            or audit.get("window_audit_hashes") != [x["audit_response_hash"] for x in active_results]
            or score_row["response_set_hash"] != expected_response_set_hash):
        _fail("exam_stage6_hash_mismatch", "stored_score_transitive_mismatch", "评分聚合回执与窗口结果不闭合")
    entries = []
    previous = ZERO_HASH
    for row in db.execute(f"SELECT * FROM {LEDGER_ENTRIES} WHERE job_id=? AND graph_hash=? ORDER BY sequence", (job_id, head["active_graph_hash"])):
        entry = _json(row["payload_json"], "stored_ledger_json_invalid"); body = dict(entry); embedded = body.pop("entry_hash", None)
        if embedded != row["entry_hash"] or _digest(body) != embedded or row["previous_head_hash"] != previous: _fail("exam_stage6_hash_mismatch", "stored_ledger_chain_mismatch", "拒绝账本链漂移")
        previous = embedded; entries.append(entry)
    if previous != head["ledger_head_hash"] or len(entries) != int(head["ledger_entry_count"]): _fail("exam_stage6_hash_mismatch", "stored_ledger_head_mismatch", "拒绝账本 HEAD 漂移")
    normalized_rejected = [{"slot_id": slot, "candidate_id": candidate} for slot, candidate in sorted({(edge["slot_id"], edge["candidate_id"]) for entry in entries for edge in entry.get("rejected_edges", [])})]
    scoring_identity = score.get("scoring_identity", {})
    expected_base_graph = checkpoint_payload["bindings"]["initial_graph_hash"] if checkpoint_row["graph_hash"] != checkpoint_payload["bindings"]["initial_graph_hash"] else None
    if (scoring_identity.get("rejection_ledger_head_hash") != head["ledger_head_hash"] or scoring_identity.get("rejected_edges") != normalized_rejected
            or scoring_identity.get("base_graph_hash") != expected_base_graph
            or scoring_identity.get("constraint_hash") != checkpoint_payload["bindings"]["constraint_hash"]
            or scoring_identity.get("candidate_facts_hash") != checkpoint_payload["bindings"]["candidate_facts_hash"]
            or scoring_identity.get("profile_hash") != checkpoint_payload["bindings"]["profile_hash"]
            or scoring_identity.get("projection_identity_hash") != checkpoint_payload["bindings"]["projection_identity_hash"]):
        _fail("exam_stage6_hash_mismatch", "stored_score_ledger_binding_mismatch", "评分回执与拒绝账本或基础图不闭合")
    terminal_rows = list(db.execute(f"SELECT w.*,w.receipt_hash AS window_receipt_hash,q.request_hash,q.request_json,q.receipt_hash AS request_receipt_hash,o.terminal_reason,o.rejected_edges_json,o.receipt_hash AS outcome_receipt_hash FROM {WINDOWS} w JOIN {REQUESTS} q ON q.job_id=w.job_id AND q.window_hash=w.window_hash JOIN {RESPONSES} o ON o.job_id=w.job_id AND o.window_hash=w.window_hash WHERE w.job_id=? AND o.outcome_status='terminal_failed'", (job_id,)))
    for row in terminal_rows:
        terminal_window = _json(row["payload_json"], "stored_terminal_window_invalid")
        terminal_request = _json(row["request_json"], "stored_terminal_request_invalid")
        window_object = ScoringWindow(row["window_version"], row["phase"], row["graph_hash"], int(row["window_index"]), tuple(terminal_window.get("slot_ids", [])), int(row["edge_count"]), row["window_hash"], _canonical(terminal_window))
        graph_object, base_object = _authoritative_graph(db, job_id, row["graph_hash"])
        if base_object is None:
            authoritative_windows = build_scoring_windows(graph=graph_object, question_count=question_count, phase="initial")
        else:
            base_keys = {(x["slot_id"], x["candidate_id"]) for x in base_object.to_dict()["edges"]}
            affected = sorted({x["slot_id"] for x in graph_object.to_dict()["edges"] if (x["slot_id"], x["candidate_id"]) not in base_keys})
            authoritative_windows = build_scoring_windows(graph=graph_object, base_graph=base_object, question_count=question_count, phase="expansion", affected_slot_ids=affected)
        material_object = _authoritative_material(db, job_id, row["graph_hash"], question_count)
        rebuilt_request = render_scoring_request(window=window_object, material_receipt=material_object, profile_hash=terminal_request.get("profile_hash"), projection_identity_hash=terminal_request.get("projection_identity_hash"), prompt_version=terminal_request.get("prompt_version"), model_id=terminal_request.get("model_id"))
        rejected = _json(row["rejected_edges_json"], "terminal_rejected_edges_invalid")
        expected_rejected = [{"slot_id": x["slot_id"], "candidate_id": x["candidate_id"]} for x in terminal_window.get("edges", [])] if row["phase"] == "expansion" else []
        outcome_body = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"], "outcome_status": "terminal_failed", "terminal_reason": row["terminal_reason"], "rejected_edges": expected_rejected}
        if (window_object not in authoritative_windows or row["material_hash"] != material_object.material_hash
                or rebuilt_request != _canonical(terminal_request) or rejected != expected_rejected
                or _digest({"job_id": job_id, "window_hash": row["window_hash"], "graph_hash": row["graph_hash"], "material_hash": row["material_hash"]}) != row["window_receipt_hash"]
                or _digest({"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"]}) != row["request_receipt_hash"]
                or _digest(outcome_body) != row["outcome_receipt_hash"]):
            _fail("exam_stage6_hash_mismatch", "recovery_terminal_authority_mismatch", "恢复终止评分请求不能由权威窗口、材料及请求重建")
    if any(row["phase"] == "initial" for row in terminal_rows): _fail("exam_stage6_hash_mismatch", "recovery_initial_terminal_failure", "恢复发现初始评分终止失败")
    terminal_union = {(x["slot_id"], x["candidate_id"]) for row in terminal_rows for x in _json(row["rejected_edges_json"], "terminal_rejected_edges_invalid")}
    if terminal_union != {(x["slot_id"], x["candidate_id"]) for x in normalized_rejected}: _fail("exam_stage6_hash_mismatch", "recovery_terminal_ledger_mismatch", "恢复发现扩展终止集合与账本不一致")
    active_graph, base_graph = _authoritative_graph(db, job_id, str(head["active_graph_hash"]))
    rebuilt_score = build_score_receipt(graph=active_graph, base_graph=base_graph, parsed_windows=[_window_result(x) for x in active_results], rejected_edges=normalized_rejected, rejection_ledger_head_hash=str(head["ledger_head_hash"]))
    if score != rebuilt_score.to_stage5_dict() or audit != rebuilt_score.to_audit_dict(): _fail("exam_stage6_hash_mismatch", "recovery_score_authority_mismatch", "恢复评分回执不能由成功响应与账本重建")
    checkpoint = CheckpointV3(checkpoint_row["checkpoint_version"], checkpoint_row["identity_hash"], checkpoint_row["checkpoint_hash"], int(checkpoint_row["size_bytes"]), _canonical(checkpoint_payload))
    validate_checkpoint_v3(checkpoint, expected_bindings=checkpoint_payload["bindings"])
    bindings = checkpoint_payload["bindings"]
    if checkpoint_row["checkpoint_stage"] != "stage6_scored" or bindings.get("checkpoint_stage") != "stage6_scored":
        _fail("exam_stage6_incomplete", "stage7_authority_not_persisted", "阶段 7 checkpoint 尚无可验证的持久化权威链")
    if (bindings["material_receipt_hash"] != head["material_hash"] or bindings["score_receipt_hash"] != head["score_set_hash"]
            or bindings["score_audit_hash"] != head["audit_hash"] or bindings["selection_hash"] != head["selection_hash"]
            or bindings["rejection_ledger_head_hash"] != head["ledger_head_hash"]): _fail("exam_stage6_hash_mismatch", "checkpoint_head_transitive_mismatch", "checkpoint 与阶段 6 HEAD 不闭合")
    return Stage6StoredState(_head(head), material, tuple(windows), tuple(responses), score, audit, tuple(entries), reference, checkpoint)


def recover_stage6_state(db, *, job_id: str, worker_id: str, lease_attempt: int) -> Stage6StoredState:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction: _fail("exam_stage6_store_failed", "nested_stage6_transaction_forbidden", "阶段 6 恢复不能嵌套事务")
    with immediate_transaction(db):
        stage3, stage4, stage5, stage5_head, constraint, binding, input_row, input_payload, job = _authority(db, job_id, worker_id, lease_attempt)
        head = _load_head(db, job_id)
        state = _verify_recovery(db, job_id, head)
        b = state.checkpoint.to_dict()["bindings"]
        context = _context_payload(job_id, stage3, stage5, constraint, binding, input_row, input_payload, job)
        root = db.execute(f"SELECT payload_json,context_hash FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()
        if root is None or _json(root["payload_json"], "stored_context_json_invalid") != context or _digest(context) != root["context_hash"] or root["context_hash"] != head["context_hash"]:
            _fail("exam_stage6_hash_mismatch", "recovery_root_authority_mismatch", "恢复时阶段 6 根权威回执漂移")
        authority = {"single_paper_contract_version": context["single_paper_contract_version"], "single_paper_contract_hash": context["single_paper_contract_hash"], "request_hash": stage3["generation_request_hash"], "stage3_contract_hash": stage3["stage3_contract_hash"], "profile_hash": constraint.get("profile_hash"), "projection_identity_hash": stage4["projection_identity_hash"], "candidate_facts_hash": stage4["candidate_facts_hash"], "recent_snapshot_hash": context["recent_snapshot_hash"], "constraint_hash": stage5["constraint_hash"], "initial_graph_hash": stage5_head["initial_graph_hash"], "question_count": int(stage5["question_count"]), "time_policy_version": context["time_policy_version"]}
        if any(b.get(key) != value for key, value in authority.items()): _fail("exam_stage6_hash_mismatch", "recovery_upstream_binding_mismatch", "恢复时上游传递绑定漂移")
        return state


def recover_scoring_attempts(db, *, job_id: str, worker_id: str, lease_attempt: int) -> tuple[ScoringAttemptState, ...]:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction: _fail("exam_stage6_store_failed", "nested_stage6_transaction_forbidden", "评分请求恢复不能嵌套事务")
    with immediate_transaction(db):
        _s3, _s4, stage5, _s5h, _constraint, *_rest = _authority(db, job_id, worker_id, lease_attempt)
        question_count = int(stage5["question_count"])
        head = _load_head(db, job_id)
        retry_events = _retry_chain(db, job_id, head)
        values = []
        for row in db.execute(f"SELECT w.phase,w.window_hash,w.window_version,w.graph_hash,w.material_hash,w.window_index,w.edge_count,w.payload_json,w.receipt_hash AS window_receipt_hash,q.request_hash,q.request_json,q.receipt_hash AS request_receipt_hash,o.outcome_status,o.terminal_reason,o.response_hash,o.response_json,o.parsed_scores_json,o.rejected_edges_json,o.receipt_hash AS outcome_receipt_hash FROM {WINDOWS} w JOIN {REQUESTS} q ON q.job_id=w.job_id AND q.window_hash=w.window_hash LEFT JOIN {RESPONSES} o ON o.job_id=w.job_id AND o.window_hash=w.window_hash WHERE w.job_id=? ORDER BY w.phase,w.window_index", (job_id,)):
            window, request = _json(row["payload_json"], "stored_window_json_invalid"), _json(row["request_json"], "stored_request_json_invalid")
            body = dict(request); request_hash = body.pop("request_hash", None)
            window_object = ScoringWindow(row["window_version"], row["phase"], row["graph_hash"], int(row["window_index"]), tuple(window.get("slot_ids", [])), int(row["edge_count"]), row["window_hash"], _canonical(window))
            graph_object, base_object = _authoritative_graph(db, job_id, row["graph_hash"])
            if base_object is None:
                expected_windows = build_scoring_windows(graph=graph_object, question_count=question_count, phase="initial")
            else:
                base_keys = {(x["slot_id"], x["candidate_id"]) for x in base_object.to_dict()["edges"]}
                affected = sorted({x["slot_id"] for x in graph_object.to_dict()["edges"] if (x["slot_id"], x["candidate_id"]) not in base_keys})
                expected_windows = build_scoring_windows(graph=graph_object, base_graph=base_object, question_count=question_count, phase="expansion", affected_slot_ids=affected)
            expected_window = next((item for item in expected_windows if item.window_hash == row["window_hash"]), None)
            material = _authoritative_material(db, job_id, row["graph_hash"], question_count)
            rebuilt_request = render_scoring_request(window=window_object, material_receipt=material, profile_hash=request.get("profile_hash"), projection_identity_hash=request.get("projection_identity_hash"), prompt_version=request.get("prompt_version"), model_id=request.get("model_id"))
            window_receipt = {"job_id": job_id, "window_hash": row["window_hash"], "graph_hash": row["graph_hash"], "material_hash": row["material_hash"]}
            request_receipt = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"]}
            if (_digest(window) != row["window_hash"] or expected_window != window_object
                    or row["material_hash"] != material.material_hash or request_hash != row["request_hash"]
                    or _digest(body) != request_hash or rebuilt_request != _canonical(request)
                    or _digest(window_receipt) != row["window_receipt_hash"] or _digest(request_receipt) != row["request_receipt_hash"]):
                _fail("exam_stage6_hash_mismatch", "stored_scoring_request_mismatch", "冻结评分请求身份漂移")
            status = str(row["outcome_status"] or "pending")
            rejected_raw = _json(row["rejected_edges_json"], "stored_terminal_edges_invalid") if row["rejected_edges_json"] is not None else []
            if status == "succeeded":
                response = _json(row["response_json"], "stored_response_json_invalid")
                replay = parse_scoring_response(_canonical(response), request_json=_canonical(request), window=window_object).to_dict()
                outcome = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"], "outcome_status": "succeeded", "response_hash": row["response_hash"]}
                if _digest(response) != row["response_hash"] or replay != _json(row["parsed_scores_json"], "stored_window_result_json_invalid") or _digest(outcome) != row["outcome_receipt_hash"]:
                    _fail("exam_stage6_hash_mismatch", "stored_scoring_outcome_mismatch", "冻结评分成功结果回放漂移")
            elif status == "terminal_failed":
                expected_rejected = [{"slot_id": x["slot_id"], "candidate_id": x["candidate_id"]} for x in window.get("edges", [])] if row["phase"] == "expansion" else []
                outcome = {"job_id": job_id, "window_hash": row["window_hash"], "request_hash": row["request_hash"], "outcome_status": "terminal_failed", "terminal_reason": row["terminal_reason"], "rejected_edges": expected_rejected}
                if row["terminal_reason"] not in {"transport_exhausted", "schema_correction_exhausted"} or rejected_raw != expected_rejected or _digest(outcome) != row["outcome_receipt_hash"]:
                    _fail("exam_stage6_hash_mismatch", "stored_scoring_outcome_mismatch", "冻结评分终止结果漂移")
            elif status != "pending":
                _fail("exam_stage6_hash_mismatch", "stored_scoring_outcome_mismatch", "冻结评分结果状态无效")
            window_events = tuple(dict(x) for x in retry_events if x["window_hash"] == row["window_hash"])
            values.append(ScoringAttemptState(str(row["window_hash"]), str(row["request_hash"]), str(row["phase"]), status, row["terminal_reason"], tuple(dict(x) for x in rejected_raw), _canonical(request), dict(window), window_events, int(head["transport_retry_count"]), int(head["schema_correction_count"]), int(head["transport_retry_limit"])))
        return tuple(values)


__all__ = ["ScoringAttemptState", "Stage6Head", "Stage6RepositoryError", "Stage6StoredState", "append_rejection_ledger", "append_scoring_retry_event", "persist_checkpoint_v3", "persist_legacy_migration_decision", "persist_material_receipt", "persist_score_receipt", "persist_scoring_request", "persist_scoring_terminal_failure", "persist_scoring_window", "persist_selection_reference", "recover_scoring_attempts", "recover_stage6_state"]
