"""Crash-safe Stage-7 persistence, deterministic replay, and closed recovery."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..transactions import immediate_transaction
from .exam_contracts import validate_generation_job_id
from .exam_generation_stage4_candidate_facts import FrozenCandidateFacts
from .exam_generation_stage4_difficulty import DifficultyResolutionResult, EffectiveDifficultyOutcome
from .exam_generation_stage5_solver import Stage5Selection
from .exam_generation_stage6_checkpoint_v3 import CheckpointV3, build_checkpoint_v3, validate_checkpoint_v3
from .exam_generation_stage7_quality import (
    ADAPTATION_PROTOCOL_VERSION,
    FINAL_RECEIPT_VERSION,
    GLOBAL_CONSTRAINT_EVIDENCE_VERSION,
    LEGACY_FINAL_RECEIPT_VERSION,
    LEGACY_PRE_RECEIPT_VERSION,
    LEGACY_QUALITY_PROTOCOL_VERSION,
    LOCAL_EVIDENCE_VERSION,
    PRE_RECEIPT_VERSION,
    QUALITY_PROTOCOL_VERSION,
    STAGE7_VERSION,
    VERIFICATION_PROTOCOL_VERSION,
    Stage7Budget,
    Stage7LeaseLost,
    Stage7Result,
    run_stage7_quality_and_adaptation,
)
from .job_contracts import JobCancelled, JobLeaseLost


ROOTS = "exam_generation_stage7_context_receipts"
EVIDENCE = "exam_generation_stage7_evidence_receipts"
DIFFICULTY_REFS = "exam_generation_stage7_difficulty_references"
PAPERS = "exam_generation_stage7_papers"
RECEIPTS = "exam_generation_stage7_receipts"
REQUESTS = "exam_generation_stage7_requests"
RESPONSES = "exam_generation_stage7_responses"
RESULTS = "exam_generation_stage7_results"
HEADS = "exam_generation_stage7_heads"
STAGE6_HEADS = "exam_generation_stage6_heads"
STAGE6_CHECKPOINTS = "exam_generation_stage6_checkpoints"
LEGACY_CONTEXT_VERSION = "past-v3-stage7-persistence-context-v1"
CONTEXT_VERSION = "past-v3-stage7-persistence-context-v2"
MAX_PAYLOAD_BYTES = 2_000_000
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_V1_PROTOCOLS = {"quality": LEGACY_QUALITY_PROTOCOL_VERSION, "adaptation": ADAPTATION_PROTOCOL_VERSION, "verification": VERIFICATION_PROTOCOL_VERSION}
_V2_PROTOCOLS = {"quality": QUALITY_PROTOCOL_VERSION, "adaptation": ADAPTATION_PROTOCOL_VERSION, "verification": VERIFICATION_PROTOCOL_VERSION}


class Stage7RepositoryError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.reason, self.retryable = code, reason, retryable


@dataclass(frozen=True, slots=True)
class Stage7Head:
    job_id: str
    context_hash: str
    stage6_checkpoint_hash: str
    quality_request_hash: str | None
    quality_response_hash: str | None
    adaptation_request_hash: str | None
    adaptation_response_hash: str | None
    verification_request_hash: str | None
    verification_response_hash: str | None
    result_hash: str | None
    checkpoint_hash: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class Stage7StoredState:
    head: Stage7Head
    context: Mapping[str, Any]
    local_answer_evidence: Mapping[str, Any] | None
    global_constraint_evidence: Mapping[str, Any]
    difficulty_results: tuple[DifficultyResolutionResult, ...]
    pre_items: tuple[Mapping[str, Any], ...]
    pre_receipt: Mapping[str, Any]
    requests: Mapping[str, Mapping[str, Any]]
    dispositions: Mapping[str, Mapping[str, Any]]
    result: Stage7Result | None
    checkpoint: CheckpointV3 | None
    next_action: str


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Stage7RepositoryError("exam_stage7_store_failed", "non_canonical_json", "阶段 7 持久化数据不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _raw_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fail(code: str, reason: str, message: str, *, retryable: bool = False) -> None:
    raise Stage7RepositoryError(code, reason, message, retryable=retryable)


def _json(raw: object, reason: str) -> Any:
    try:
        return json.loads(str(raw))
    except (TypeError, ValueError, RecursionError):
        _fail("exam_stage7_hash_mismatch", reason, "阶段 7 持久化 JSON 无效")


def _timestamp(value: str | None) -> str:
    return str(value or datetime.now(timezone.utc).isoformat(timespec="microseconds"))


def _actor(job_id: str, worker_id: str, lease_attempt: int) -> tuple[str, str]:
    try: normalized = validate_generation_job_id(job_id)
    except ValueError: normalized = None
    worker = str(worker_id or "").strip()
    if normalized is None: _fail("exam_stage7_contract_conflict", "generation_job_id_invalid", "组卷任务身份无效")
    if not worker or type(lease_attempt) is not int or lease_attempt < 1: raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    return normalized, worker


def _publication_residue(db, job_id: str) -> bool:
    row = db.execute("SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)", (job_id, job_id, job_id)).fetchone()
    return bool(row[0])


def _fence(db, job_id: str, worker_id: str, lease_attempt: int):
    job = db.execute("SELECT kind,status,cancel_requested,lease_owner,attempts FROM background_jobs WHERE id=?", (job_id,)).fetchone()
    if job is None: raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if bool(job["cancel_requested"]) or str(job["status"] or "") == "cancel_requested": raise JobCancelled("任务已取消")
    if str(job["kind"] or "") != "exam-generation" or str(job["status"] or "") != "running" or str(job["lease_owner"] or "") != worker_id or int(job["attempts"] or 0) != lease_attempt:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if _publication_residue(db, job_id): _fail("published_recovery_required", "publication_residue_detected", "任务已有发布残留，必须先恢复发布")
    return job


def _head(row) -> Stage7Head:
    return Stage7Head(str(row["job_id"]), str(row["context_hash"]), str(row["stage6_checkpoint_hash"]), row["quality_request_hash"], row["quality_response_hash"], row["adaptation_request_hash"], row["adaptation_response_hash"], row["verification_request_hash"], row["verification_response_hash"], row["result_hash"], row["checkpoint_hash"], int(row["revision"]))


def _load_head(db, job_id: str):
    row = db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if row is None: _fail("exam_stage7_incomplete", "stage7_head_missing", "阶段 7 尚未建立持久化根")
    return row


def _cas(db, job_id: str, expected_revision: int, timestamp: str, setters: Mapping[str, Any]) -> Stage7Head:
    if type(expected_revision) is not int or expected_revision < 1: _fail("exam_stage7_store_failed", "expected_revision_invalid", "阶段 7 CAS revision 无效")
    assignments = "".join(f"{key}=?," for key in setters)
    cursor = db.execute(f"UPDATE {HEADS} SET {assignments}revision=revision+1,updated_at=? WHERE job_id=? AND revision=?", (*setters.values(), timestamp, job_id, expected_revision))
    if cursor.rowcount != 1: _fail("exam_stage7_contract_conflict", "stage7_head_cas_lost", "阶段 7 head CAS 已丢失", retryable=True)
    return _head(_load_head(db, job_id))


def _selection(db, job_id: str, selection_hash: str) -> Stage5Selection:
    row = db.execute("SELECT * FROM exam_generation_stage5_selection_receipts WHERE job_id=? AND selection_hash=?", (job_id, selection_hash)).fetchone()
    if row is None: _fail("exam_stage7_hash_mismatch", "selection_missing", "阶段 5 选择不存在")
    payload = _json(row["payload_json"], "selection_json_invalid")
    assignments = tuple(_json(x[0], "assignment_json_invalid") for x in db.execute("SELECT assignment_json FROM exam_generation_stage5_assignments WHERE job_id=? AND selection_hash=? ORDER BY position", (job_id, selection_hash)))
    identity = {key: payload[key] for key in ("contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash", "constraint_hash", "score_identity_hash", "assignments")}
    if _digest(identity) != selection_hash or tuple(payload.get("assignments", ())) != assignments or len(assignments) != int(row["assignment_count"]): _fail("exam_stage7_hash_mismatch", "selection_payload_drift", "阶段 5 选择或 assignments 漂移")
    return Stage5Selection(str(row["selection_contract_version"]), str(row["graph_hash"]), str(row["constraint_hash"]), str(row["score_identity_hash"]), int(row["total_utility"]), selection_hash, assignments, _canonical(payload))


def _facts(db, job_id: str, candidate_facts_hash: str) -> FrozenCandidateFacts:
    row = db.execute("SELECT * FROM exam_generation_stage4_fact_receipts WHERE job_id=? AND candidate_facts_hash=?", (job_id, candidate_facts_hash)).fetchone()
    if row is None: _fail("exam_stage7_hash_mismatch", "candidate_facts_missing", "阶段 4 冻结候选事实不存在")
    payload = _json(row["facts_payload_json"], "candidate_facts_json_invalid")
    if _digest(payload) != candidate_facts_hash: _fail("exam_stage7_hash_mismatch", "candidate_facts_payload_drift", "阶段 4 冻结候选载荷漂移")
    return FrozenCandidateFacts(str(row["candidate_facts_contract_version"]), str(row["projection_identity_hash"]), candidate_facts_hash, str(row["target_institution"]), tuple(payload.get("candidates", ())), tuple(payload.get("unresolved_candidate_ids", ())), _canonical(payload))


def _difficulty_from_db(db, job_id: str, result_hash: str) -> tuple[DifficultyResolutionResult, dict[str, Any]]:
    row = db.execute("SELECT r.*,f.* FROM exam_generation_stage4_difficulty_results r JOIN exam_generation_stage4_difficulty_flights f ON f.flight_hash=r.flight_hash WHERE r.result_hash=?", (result_hash,)).fetchone()
    if row is None or str(row["state"]) != "completed" or str(row["result_hash"]) != result_hash: _fail("exam_stage7_hash_mismatch", "difficulty_completed_result_missing", "阶段 4 completed difficulty result 不存在")
    if str(row["claim_job_id"] or "") != job_id:
        _fail("exam_stage7_contract_conflict", "difficulty_result_cross_job", "阶段 4 难度结果不属于当前 job-scoped flight")
    payload = _json(row["result_payload_json"], "difficulty_result_json_invalid")
    result_receipt = {"flight_hash": row["flight_hash"], "completed_from_revision": row["completed_from_revision"], "result_contract_version": row["result_contract_version"], "candidate_facts_hash": row["candidate_facts_hash"], "component_hash": row["component_hash"], "input_facts_hash": row["input_facts_hash"], "result_hash": row["result_hash"], "outcome_count": row["outcome_count"], "result_payload_json": _canonical(payload)}
    if _digest(payload) != result_hash or _digest(result_receipt) != row["receipt_hash"]: _fail("exam_stage7_hash_mismatch", "difficulty_result_receipt_drift", "阶段 4 难度结果或回执漂移")
    rounds = list(db.execute("SELECT * FROM exam_generation_stage4_difficulty_round_receipts WHERE flight_hash=? ORDER BY round_name", (row["flight_hash"],)))
    if len(rounds) != 2 or {x["round_name"] for x in rounds} != {"classification", "verification"}: _fail("exam_stage7_hash_mismatch", "difficulty_round_receipts_missing", "阶段 4 难度双轮回执不完整")
    round_hashes: dict[str, str] = {}
    for item in rounds:
        body = {key: item[key] for key in ("flight_hash", "completed_from_revision", "round_name", "response_schema_version", "model", "prompt_version", "rules_hash", "input_facts_hash", "response_status", "response_hash")}
        if _digest(body) != item["receipt_hash"] or item["completed_from_revision"] != row["completed_from_revision"] or item["input_facts_hash"] != row["input_facts_hash"]: _fail("exam_stage7_hash_mismatch", "difficulty_round_receipt_drift", "阶段 4 难度双轮回执漂移")
        binding = payload.get(f"{item['round_name']}_binding")
        if binding != {"model": item["model"], "prompt_version": item["prompt_version"], "rules_hash": item["rules_hash"]} or payload.get(f"{item['round_name']}_response_hash") != item["response_hash"]: _fail("exam_stage7_hash_mismatch", "difficulty_round_transitive_drift", "阶段 4 难度结果未绑定双轮回执")
        round_hashes[str(item["round_name"])] = str(item["receipt_hash"])
    outcomes = tuple(EffectiveDifficultyOutcome(**item) for item in payload.get("outcomes", ()))
    stored = list(db.execute("SELECT * FROM exam_generation_stage4_difficulty_outcomes WHERE flight_hash=? ORDER BY candidate_id", (row["flight_hash"],)))
    expected = sorted(payload.get("outcomes", ()), key=lambda x: x["candidate_id"])
    if len(stored) != len(expected) or any(_digest(value) != item["outcome_hash"] or any(item[key] != value[key] for key in value) for item, value in zip(stored, expected)): _fail("exam_stage7_hash_mismatch", "difficulty_outcomes_drift", "阶段 4 难度逐项结果漂移")
    result = DifficultyResolutionResult(str(row["result_contract_version"]), str(row["candidate_facts_hash"]), str(row["component_hash"]), str(row["input_facts_hash"]), outcomes, result_hash, _canonical(payload))
    ref = {"flight_hash": str(row["flight_hash"]), "result_hash": result_hash, "component_hash": str(row["component_hash"]), "input_facts_hash": str(row["input_facts_hash"]), "classification_receipt_hash": round_hashes["classification"], "verification_receipt_hash": round_hashes["verification"]}
    return result, ref


def _budget_payload(budget: Stage7Budget, adaptation_enabled: bool, verification_enabled: bool) -> dict[str, Any]:
    if not isinstance(budget, Stage7Budget) or type(adaptation_enabled) is not bool or type(verification_enabled) is not bool: _fail("exam_stage7_contract_conflict", "budget_or_callback_capability_invalid", "阶段 7 预算或 callback 能力无效")
    return {"mode": budget.mode, "structural_calls_remaining": budget.structural_calls_remaining, "remaining_seconds": budget.remaining_seconds, "formal_adaptation_min_remaining_seconds": budget.formal_adaptation_min_remaining_seconds, "adaptation_enabled": adaptation_enabled, "verification_enabled": verification_enabled}


def _budget(value: Mapping[str, Any]) -> Stage7Budget:
    return Stage7Budget(value["mode"], value["structural_calls_remaining"], value["remaining_seconds"], value["formal_adaptation_min_remaining_seconds"])


def _all_pass_callback(raw: str) -> str:
    request = json.loads(raw)
    return _canonical({"version": request["version"], "selection_hash": request["selection_hash"], "pre_adaptation_hash": request["pre_adaptation_hash"], "results": [{"position": item["position"], "passed": True, "reason_codes": ["none"]} for item in request["items"]]})


def _preflight(selection: Stage5Selection, facts: FrozenCandidateFacts, difficulties: Sequence[DifficultyResolutionResult], local: Mapping[str, Any] | None, global_: Mapping[str, Any], budget: Stage7Budget) -> Stage7Result:
    return run_stage7_quality_and_adaptation(selection=selection, candidate_facts=facts, difficulty_resolution_results=difficulties, local_answer_evidence=local, global_constraint_evidence=global_, quality_callback=_all_pass_callback, budget=budget)


def _stage6_authority(db, job_id: str, expected_checkpoint_hash: str | None = None):
    head = db.execute(f"SELECT * FROM {STAGE6_HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if head is None: _fail("exam_stage7_contract_conflict", "stage6_head_missing", "阶段 7 缺少阶段 6 head")
    checkpoint_hash = expected_checkpoint_hash or head["checkpoint_hash"]
    checkpoint = db.execute(f"SELECT * FROM {STAGE6_CHECKPOINTS} WHERE job_id=? AND checkpoint_hash=?", (job_id, checkpoint_hash)).fetchone()
    if checkpoint is None: _fail("exam_stage7_contract_conflict", "stage6_scored_checkpoint_missing", "阶段 7 缺少 stage6_scored checkpoint")
    payload = _json(checkpoint["payload_json"], "stage6_checkpoint_json_invalid")
    obj = CheckpointV3(str(checkpoint["checkpoint_version"]), str(checkpoint["identity_hash"]), str(checkpoint["checkpoint_hash"]), int(checkpoint["size_bytes"]), _canonical(payload))
    validate_checkpoint_v3(obj, expected_bindings=payload["bindings"])
    if checkpoint["checkpoint_stage"] != "stage6_scored" or payload["bindings"].get("checkpoint_stage") != "stage6_scored": _fail("exam_stage7_contract_conflict", "previous_checkpoint_not_stage6_scored", "阶段 7 根必须直接绑定 stage6_scored checkpoint")
    b = payload["bindings"]
    if any(head[key] != b[value] for key, value in (("material_hash", "material_receipt_hash"), ("score_set_hash", "score_receipt_hash"), ("audit_hash", "score_audit_hash"), ("ledger_head_hash", "rejection_ledger_head_hash"), ("selection_hash", "selection_hash"))): _fail("exam_stage7_hash_mismatch", "stage6_checkpoint_head_drift", "阶段 6 checkpoint 与 head 漂移")
    return head, checkpoint, obj


def persist_stage7_context(db, *, job_id: str, difficulty_resolution_results: Sequence[DifficultyResolutionResult], global_constraint_evidence: Mapping[str, Any], worker_id: str, lease_attempt: int, budget: Stage7Budget = Stage7Budget(), adaptation_enabled: bool = True, verification_enabled: bool = True, created_at: str | None = None) -> Stage7Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction: _fail("exam_stage7_store_failed", "nested_stage7_transaction_forbidden", "阶段 7 持久化不能嵌套事务")
    timestamp = _timestamp(created_at)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        existing = db.execute(f"SELECT 1 FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()
        if existing is not None:
            # A persisted root is already the authority.  In particular, a
            # historical v1 root must never be reconstructed from a new v2
            # argument set or force an external answer loader during recovery.
            core = _load_core(db, job_id)
            return _head(core[0])
        s6head, checkpoint, _ = _stage6_authority(db, job_id)
        selection = _selection(db, job_id, str(s6head["selection_hash"]))
        stage4 = db.execute("SELECT candidate_facts_hash FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()
        facts = _facts(db, job_id, str(stage4["candidate_facts_hash"]))
        supplied = tuple(difficulty_resolution_results)
        refs: list[dict[str, Any]] = []
        rebuilt: list[DifficultyResolutionResult] = []
        for value in supplied:
            if not isinstance(value, DifficultyResolutionResult): _fail("exam_stage7_contract_conflict", "difficulty_result_type_invalid", "阶段 7 必须接收完整 DifficultyResolutionResult")
            result, ref = _difficulty_from_db(db, job_id, value.result_hash)
            if result.to_dict() != value.to_dict(): _fail("exam_stage7_hash_mismatch", "difficulty_result_argument_drift", "传入难度结果与 v42 completed result 漂移")
            rebuilt.append(result); refs.append(ref)
        budget_payload = _budget_payload(budget, adaptation_enabled, verification_enabled)
        pre = _preflight(selection, facts, tuple(rebuilt), None, global_constraint_evidence, budget)
        pre_items = [dict(item) for item in pre.final_items]
        global_hash = str(global_constraint_evidence.get("evidence_hash"))
        context = {"version": CONTEXT_VERSION, "job_id": job_id, "stage6_checkpoint_hash": checkpoint["checkpoint_hash"], "selection_hash": selection.selection_hash, "candidate_facts_hash": facts.candidate_facts_hash, "constraint_hash": selection.constraint_hash, "graph_hash": selection.graph_hash, "score_set_hash": selection.score_identity_hash, "stage4_receipt_hash": db.execute("SELECT receipt_hash FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,)).fetchone()[0], "stage5_receipt_hash": db.execute("SELECT receipt_hash FROM exam_generation_stage5_constraint_receipts WHERE job_id=?", (job_id,)).fetchone()[0], "global_evidence_hash": global_hash, "difficulty_result_hashes": [x.result_hash for x in rebuilt], "budget": budget_payload, "budget_hash": _digest(budget_payload)}
        context_hash = _digest(context)
        db.execute(f"INSERT INTO {ROOTS}(job_id,context_hash,context_version,stage6_checkpoint_hash,selection_hash,candidate_facts_hash,constraint_hash,graph_hash,score_set_hash,local_evidence_hash,global_evidence_hash,budget_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, context_hash, CONTEXT_VERSION, checkpoint["checkpoint_hash"], selection.selection_hash, facts.candidate_facts_hash, selection.constraint_hash, selection.graph_hash, selection.score_identity_hash, None, global_hash, context["budget_hash"], _canonical(context), timestamp))
        db.execute(f"INSERT INTO {EVIDENCE}(job_id,context_hash,evidence_kind,evidence_version,evidence_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, context_hash, "global_constraint", GLOBAL_CONSTRAINT_EVIDENCE_VERSION, global_constraint_evidence["evidence_hash"], _canonical(global_constraint_evidence), timestamp))
        for ordinal, ref in enumerate(refs):
            ref_payload = {"version": "past-v3-stage7-difficulty-reference-v1", "job_id": job_id, "candidate_facts_hash": facts.candidate_facts_hash, **ref}
            db.execute(f"INSERT INTO {DIFFICULTY_REFS}(job_id,context_hash,ordinal,flight_hash,result_hash,component_hash,input_facts_hash,classification_receipt_hash,verification_receipt_hash,reference_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, context_hash, ordinal, ref["flight_hash"], ref["result_hash"], ref["component_hash"], ref["input_facts_hash"], ref["classification_receipt_hash"], ref["verification_receipt_hash"], _digest(ref_payload), _canonical(ref_payload), timestamp))
        paper_payload = {"version": STAGE7_VERSION, "items": pre_items}
        if _digest(paper_payload) != pre.pre_adaptation_hash: _fail("exam_stage7_hash_mismatch", "pre_paper_hash_mismatch", "改编前试卷身份不匹配")
        db.execute(f"INSERT INTO {PAPERS}(job_id,context_hash,paper_kind,paper_hash,item_count,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, context_hash, "pre", pre.pre_adaptation_hash, len(pre_items), _canonical(paper_payload), timestamp))
        db.execute(f"INSERT INTO {RECEIPTS}(job_id,context_hash,receipt_kind,receipt_hash,receipt_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, context_hash, "pre", pre.pre_adaptation_receipt["receipt_hash"], PRE_RECEIPT_VERSION, _canonical(pre.pre_adaptation_receipt), timestamp))
        db.execute(f"INSERT INTO {HEADS}(job_id,context_hash,stage6_checkpoint_hash,quality_request_hash,quality_response_hash,adaptation_request_hash,adaptation_response_hash,verification_request_hash,verification_response_hash,result_hash,checkpoint_hash,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, context_hash, checkpoint["checkpoint_hash"], None, None, None, None, None, None, None, None, 1, timestamp, timestamp))
        return _head(_load_head(db, job_id))


def _load_core(db, job_id: str):
    head = _load_head(db, job_id); root = db.execute(f"SELECT * FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()
    context = _json(root["payload_json"], "stored_context_json_invalid")
    if _digest(context) != root["context_hash"] or root["context_hash"] != head["context_hash"]: _fail("exam_stage7_hash_mismatch", "stored_context_hash_mismatch", "阶段 7 根载荷漂移")
    s6head, _s6row, s6checkpoint = _stage6_authority(db, job_id, head["stage6_checkpoint_hash"])
    s6bindings = s6checkpoint.to_dict()["bindings"]
    if (context.get("stage6_checkpoint_hash") != head["stage6_checkpoint_hash"]
            or context.get("selection_hash") != s6bindings.get("selection_hash")
            or context.get("graph_hash") != s6head["active_graph_hash"]
            or context.get("score_set_hash") != s6bindings.get("score_receipt_hash")
            or context.get("candidate_facts_hash") != s6bindings.get("candidate_facts_hash")
            or context.get("constraint_hash") != s6bindings.get("constraint_hash")
            or context.get("budget_hash") != _digest(context.get("budget"))):
        _fail("exam_stage7_hash_mismatch", "context_upstream_binding_drift", "阶段 7 根与 stage6_scored 权威链漂移")
    stage4_receipt = db.execute("SELECT receipt_hash FROM exam_generation_stage4_fact_receipts WHERE job_id=? AND candidate_facts_hash=?", (job_id, context["candidate_facts_hash"])).fetchone()
    stage5_receipt = db.execute("SELECT receipt_hash FROM exam_generation_stage5_constraint_receipts WHERE job_id=? AND constraint_hash=?", (job_id, context["constraint_hash"])).fetchone()
    if stage4_receipt is None or stage5_receipt is None or stage4_receipt[0] != context.get("stage4_receipt_hash") or stage5_receipt[0] != context.get("stage5_receipt_hash"):
        _fail("exam_stage7_hash_mismatch", "upstream_receipt_identity_drift", "阶段 4/5 权威回执身份漂移")
    context_version = context.get("version")
    if context_version not in {LEGACY_CONTEXT_VERSION, CONTEXT_VERSION}:
        _fail("exam_stage7_hash_mismatch", "context_version_invalid", "阶段 7 上下文版本无效")
    evidence = {row["evidence_kind"]: _json(row["payload_json"], "stored_evidence_json_invalid") for row in db.execute(f"SELECT * FROM {EVIDENCE} WHERE job_id=?", (job_id,))}
    expected_evidence = {"local_answer", "global_constraint"} if context_version == LEGACY_CONTEXT_VERSION else {"global_constraint"}
    if set(evidence) != expected_evidence: _fail("exam_stage7_hash_mismatch", "evidence_receipts_incomplete", "阶段 7 权威证据集合不完整")
    evidence_bindings = (("local_answer", "local_evidence_hash"), ("global_constraint", "global_evidence_hash")) if context_version == LEGACY_CONTEXT_VERSION else (("global_constraint", "global_evidence_hash"),)
    for kind, key in evidence_bindings:
        body = dict(evidence[kind]); embedded = body.pop("evidence_hash", None)
        if embedded != context[key] or _digest(body) != embedded: _fail("exam_stage7_hash_mismatch", "evidence_receipt_drift", "阶段 7 权威证据漂移")
    selection = _selection(db, job_id, context["selection_hash"]); facts = _facts(db, job_id, context["candidate_facts_hash"])
    difficulties: list[DifficultyResolutionResult] = []
    stored_refs = list(db.execute(f"SELECT * FROM {DIFFICULTY_REFS} WHERE job_id=? ORDER BY ordinal", (job_id,)))
    if [x["result_hash"] for x in stored_refs] != context["difficulty_result_hashes"]: _fail("exam_stage7_hash_mismatch", "difficulty_reference_set_drift", "阶段 7 难度引用集合漂移")
    for row in stored_refs:
        result, ref = _difficulty_from_db(db, job_id, row["result_hash"])
        payload = _json(row["payload_json"], "difficulty_reference_json_invalid")
        if _digest(payload) != row["reference_hash"] or any(row[key] != ref[key] for key in ref): _fail("exam_stage7_hash_mismatch", "difficulty_reference_drift", "阶段 7 难度引用或双轮绑定漂移")
        difficulties.append(result)
    pre_row = db.execute(f"SELECT * FROM {PAPERS} WHERE job_id=? AND paper_kind='pre'", (job_id,)).fetchone(); receipt_row = db.execute(f"SELECT * FROM {RECEIPTS} WHERE job_id=? AND receipt_kind='pre'", (job_id,)).fetchone()
    if pre_row is None or receipt_row is None: _fail("exam_stage7_hash_mismatch", "pre_artifacts_missing", "阶段 7 改编前制品不完整")
    pre_payload, pre_receipt = _json(pre_row["payload_json"], "pre_paper_json_invalid"), _json(receipt_row["payload_json"], "pre_receipt_json_invalid")
    pre_body = dict(pre_receipt); embedded = pre_body.pop("receipt_hash", None)
    if _digest(pre_payload) != pre_row["paper_hash"] or embedded != receipt_row["receipt_hash"] or _digest(pre_body) != embedded or pre_receipt.get("pre_adaptation_hash") != pre_row["paper_hash"]: _fail("exam_stage7_hash_mismatch", "pre_artifact_hash_drift", "阶段 7 改编前试卷或回执漂移")
    preflight = _preflight(selection, facts, tuple(difficulties), evidence.get("local_answer"), evidence["global_constraint"], _budget(context["budget"]))
    if list(preflight.final_items) != pre_payload["items"] or preflight.pre_adaptation_receipt != pre_receipt: _fail("exam_stage7_hash_mismatch", "pre_artifact_replay_drift", "阶段 7 改编前制品无法从权威链重放")
    requests: dict[str, Mapping[str, Any]] = {}
    protocols = _V1_PROTOCOLS if context_version == LEGACY_CONTEXT_VERSION else _V2_PROTOCOLS
    for row in db.execute(f"SELECT * FROM {REQUESTS} WHERE job_id=?", (job_id,)):
        value = _json(row["payload_json"], "request_json_invalid")
        if (_digest(value) != row["request_hash"] or value.get("version") != protocols.get(row["call_kind"])
                or len(_canonical(value).encode()) != int(row["size_bytes"]) or int(row["size_bytes"]) > MAX_PAYLOAD_BYTES):
            _fail("exam_stage7_hash_mismatch", "stored_request_receipt_drift", "阶段 7 请求回执漂移")
        requests[str(row["call_kind"])] = value
    dispositions: dict[str, Mapping[str, Any]] = {}
    for row in db.execute(f"SELECT * FROM {RESPONSES} WHERE job_id=?", (job_id,)):
        body = {"version": "past-v3-stage7-call-disposition-v1", "job_id": job_id, "call_kind": row["call_kind"], "request_hash": row["request_hash"], "outcome": row["outcome"], "response_hash": row["response_hash"]}
        raw = row["response_text"]
        valid_raw = ((row["outcome"] == "received" and isinstance(raw, str) and _raw_digest(raw) == row["response_hash"] and len(raw.encode()) == int(row["size_bytes"]))
                     or (row["outcome"] in {"timeout", "error"} and raw is None and row["response_hash"] is None and int(row["size_bytes"]) == 0))
        request_row = db.execute(f"SELECT request_hash FROM {REQUESTS} WHERE job_id=? AND call_kind=?", (job_id, row["call_kind"])).fetchone()
        if not valid_raw or _digest(body) != row["disposition_hash"] or request_row is None or request_row[0] != row["request_hash"]:
            _fail("exam_stage7_hash_mismatch", "stored_disposition_receipt_drift", "阶段 7 调用终态回执漂移")
        dispositions[str(row["call_kind"])] = {"outcome": row["outcome"], "response_text": raw, "response_hash": row["response_hash"], "disposition_hash": row["disposition_hash"], "request_hash": row["request_hash"]}
    return head, context, evidence, selection, facts, tuple(difficulties), tuple(pre_payload["items"]), pre_receipt, requests, dispositions


class _Capture(Stage7LeaseLost):
    pass


def _callbacks(context, requests, dispositions, *, capture: str | None = None):
    captured: dict[str, Any] = {}
    def callback(kind: str):
        def inner(raw: str) -> str:
            value = json.loads(raw)
            if capture == kind:
                captured["request"] = value
                raise _Capture("capture")
            stored = requests.get(kind)
            if stored != value: _fail("exam_stage7_hash_mismatch", f"{kind}_request_replay_drift", "阶段 7 请求无法从权威链重放")
            disposition = dispositions.get(kind)
            if disposition is None: raise _Capture("pending")
            if disposition["outcome"] == "timeout": raise TimeoutError("stored timeout")
            if disposition["outcome"] == "error": raise RuntimeError("stored ordinary failure")
            return str(disposition["response_text"])
        return inner
    return callback("quality"), (callback("adaptation") if context["budget"]["adaptation_enabled"] else None), (callback("verification") if context["budget"]["verification_enabled"] else None), captured


def _replay(core, *, capture: str | None = None) -> tuple[Stage7Result | None, Mapping[str, Any]]:
    _head_row, context, evidence, selection, facts, difficulties, _pre, _receipt, requests, dispositions = core
    quality, adaptation, verification, captured = _callbacks(context, requests, dispositions, capture=capture)
    stored_quality_request = requests.get("quality")
    quality_request_contract = None
    if stored_quality_request is not None:
        quality_request_contract = {
            "allowed_reason_codes": stored_quality_request.get(
                "allowed_reason_codes"
            )
        }
        if "review_policy" in stored_quality_request:
            quality_request_contract["review_policy"] = stored_quality_request[
                "review_policy"
            ]
    try:
        result = run_stage7_quality_and_adaptation(selection=selection, candidate_facts=facts, difficulty_resolution_results=difficulties, local_answer_evidence=evidence.get("local_answer"), global_constraint_evidence=evidence["global_constraint"], quality_callback=quality, adaptation_callback=adaptation, verification_callback=verification, budget=_budget(context["budget"]), quality_request_contract=quality_request_contract)
    except _Capture:
        return None, captured
    return result, captured


def expected_stage7_request(db, *, job_id: str, call_kind: str, worker_id: str, lease_attempt: int) -> Mapping[str, Any] | None:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if call_kind not in _V2_PROTOCOLS or db.in_transaction: _fail("exam_stage7_store_failed", "request_kind_invalid", "阶段 7 请求类型无效")
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id)
        _, captured = _replay(core, capture=call_kind)
        return captured.get("request")


def replay_stage7_result(db, *, job_id: str, worker_id: str, lease_attempt: int) -> Stage7Result:
    """Rebuild the result from durable interactions without invoking a provider."""

    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage7_store_failed", "nested_stage7_transaction_forbidden", "阶段 7 结果重放不能嵌套事务")
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        core = _load_core(db, job_id)
        try:
            result, _ = _replay(core)
        except Stage7RepositoryError as exc:
            missing_kind = next(
                (
                    kind
                    for kind in _V2_PROTOCOLS
                    if exc.reason == f"{kind}_request_replay_drift" and kind not in core[8]
                ),
                None,
            )
            if missing_kind is None:
                raise
            _fail("exam_stage7_incomplete", "interaction_pending", "阶段 7 尚有未持久化的调用终态")
        if result is None:
            _fail("exam_stage7_incomplete", "interaction_pending", "阶段 7 尚有未持久化的调用终态")
        return result


def persist_stage7_request(db, *, job_id: str, call_kind: str, request: Mapping[str, Any] | str, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage7Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if call_kind not in _V2_PROTOCOLS or db.in_transaction: _fail("exam_stage7_store_failed", "request_input_invalid", "阶段 7 请求参数无效")
    value = _json(request, "request_json_invalid") if isinstance(request, str) else dict(request)
    raw = _canonical(value); size = len(raw.encode()); request_hash = _digest(value)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id); head = core[0]
        protocols = _V1_PROTOCOLS if core[1].get("version") == LEGACY_CONTEXT_VERSION else _V2_PROTOCOLS
        if size > MAX_PAYLOAD_BYTES or value.get("version") != protocols[call_kind]: _fail("exam_stage7_contract_conflict", "request_protocol_or_size_invalid", "阶段 7 请求协议或大小无效")
        existing = db.execute(f"SELECT request_hash,payload_json FROM {REQUESTS} WHERE job_id=? AND call_kind=?", (job_id, call_kind)).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash or _json(existing["payload_json"], "stored_request_json_invalid") != value: _fail("exam_stage7_contract_conflict", f"{call_kind}_request_conflict", "阶段 7 同类请求冲突")
            return _head(head)
        _, captured = _replay(core, capture=call_kind)
        if captured.get("request") != value: _fail("exam_stage7_hash_mismatch", f"{call_kind}_request_binding_mismatch", "阶段 7 请求未绑定当前权威链")
        db.execute(f"INSERT INTO {REQUESTS}(job_id,context_hash,call_kind,protocol_version,request_hash,size_bytes,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, head["context_hash"], call_kind, protocols[call_kind], request_hash, size, raw, timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {f"{call_kind}_request_hash": request_hash})


def persist_stage7_disposition(db, *, job_id: str, call_kind: str, outcome: str, expected_revision: int, worker_id: str, lease_attempt: int, response_text: str | None = None, created_at: str | None = None) -> Stage7Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if call_kind not in _V2_PROTOCOLS or outcome not in {"received", "timeout", "error"} or db.in_transaction: _fail("exam_stage7_store_failed", "disposition_input_invalid", "阶段 7 调用终态参数无效")
    if outcome == "received":
        if not isinstance(response_text, str) or not response_text: _fail("exam_stage7_store_failed", "response_text_invalid", "阶段 7 received 终态缺少响应文本")
        size = len(response_text.encode()); response_hash = _raw_digest(response_text)
        if size > MAX_PAYLOAD_BYTES: _fail("exam_stage7_contract_conflict", "response_size_exceeded", "阶段 7 响应超过大小上限")
    else:
        if response_text is not None: _fail("exam_stage7_store_failed", "failure_response_forbidden", "终止性失败不得携带响应正文")
        size, response_hash = 0, None
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id); head = core[0]
        request_row = db.execute(f"SELECT request_hash FROM {REQUESTS} WHERE job_id=? AND call_kind=?", (job_id, call_kind)).fetchone()
        if request_row is None: _fail("exam_stage7_contract_conflict", "response_without_request", "阶段 7 调用终态缺少请求")
        body = {"version": "past-v3-stage7-call-disposition-v1", "job_id": job_id, "call_kind": call_kind, "request_hash": request_row["request_hash"], "outcome": outcome, "response_hash": response_hash}
        disposition_hash = _digest(body)
        existing = db.execute(f"SELECT * FROM {RESPONSES} WHERE job_id=? AND call_kind=?", (job_id, call_kind)).fetchone()
        if existing is not None:
            if existing["disposition_hash"] != disposition_hash or existing["response_text"] != response_text: _fail("exam_stage7_contract_conflict", f"{call_kind}_disposition_conflict", "阶段 7 同类调用终态冲突")
            return _head(head)
        db.execute(f"INSERT INTO {RESPONSES}(job_id,context_hash,call_kind,request_hash,outcome,response_hash,disposition_hash,size_bytes,response_text,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (job_id, head["context_hash"], call_kind, request_row["request_hash"], outcome, response_hash, disposition_hash, size, response_text, timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {f"{call_kind}_response_hash": disposition_hash})


def _result_payload(result: Stage7Result) -> dict[str, Any]:
    return {"pre_adaptation_hash": result.pre_adaptation_hash, "final_exam_hash": result.final_exam_hash, "adapted_position": result.adapted_position, "single_item_fallback": result.single_item_fallback, "final_items": list(result.final_items), "pre_adaptation_receipt": dict(result.pre_adaptation_receipt), "final_receipt": dict(result.final_receipt)}


def persist_stage7_result(db, *, job_id: str, result: Stage7Result, expected_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage7Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction or not isinstance(result, Stage7Result): _fail("exam_stage7_store_failed", "result_input_invalid", "阶段 7 最终结果参数无效")
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id); head = core[0]
        replayed, _ = _replay(core)
        if replayed is None: _fail("exam_stage7_incomplete", "interaction_pending", "阶段 7 尚有未持久化的调用终态")
        payload, replay_payload = _result_payload(result), _result_payload(replayed)
        if payload != replay_payload: _fail("exam_stage7_hash_mismatch", "stage7_result_replay_mismatch", "Stage7Result 无法由持久化权威制品重放")
        result_hash = _digest(payload)
        if head["result_hash"] is not None:
            if head["result_hash"] != result_hash: _fail("exam_stage7_contract_conflict", "stage7_result_conflict", "阶段 7 最终结果冲突")
            return _head(head)
        final_paper = {"version": STAGE7_VERSION, "items": list(result.final_items)}
        if _digest(final_paper) != result.final_exam_hash: _fail("exam_stage7_hash_mismatch", "final_paper_hash_mismatch", "最终试卷哈希不匹配")
        if result.single_item_fallback and result.pre_adaptation_hash != result.final_exam_hash: _fail("exam_stage7_hash_mismatch", "fallback_paper_changed", "fallback 路径不得改变试卷")
        pre_body = dict(result.pre_adaptation_receipt); pre_embedded = pre_body.pop("receipt_hash", None)
        final_body = dict(result.final_receipt); final_embedded = final_body.pop("receipt_hash", None)
        legacy_v1 = core[1].get("version") == LEGACY_CONTEXT_VERSION
        expected_pre_version = LEGACY_PRE_RECEIPT_VERSION if legacy_v1 else PRE_RECEIPT_VERSION
        expected_final_version = LEGACY_FINAL_RECEIPT_VERSION if legacy_v1 else FINAL_RECEIPT_VERSION
        if pre_embedded != _digest(pre_body) or final_embedded != _digest(final_body) or result.pre_adaptation_receipt.get("version") != expected_pre_version or result.final_receipt.get("version") != expected_final_version: _fail("exam_stage7_hash_mismatch", "result_receipt_hash_mismatch", "阶段 7 最终回执哈希漂移")
        pre_row = db.execute(f"SELECT receipt_hash FROM {RECEIPTS} WHERE job_id=? AND receipt_kind='pre'", (job_id,)).fetchone()
        if pre_row[0] != pre_embedded: _fail("exam_stage7_hash_mismatch", "result_pre_receipt_drift", "最终结果未绑定持久化改编前回执")
        existing_final = db.execute(f"SELECT paper_hash FROM {PAPERS} WHERE job_id=? AND paper_kind='final'", (job_id,)).fetchone()
        if existing_final is None:
            db.execute(f"INSERT INTO {PAPERS}(job_id,context_hash,paper_kind,paper_hash,item_count,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, head["context_hash"], "final", result.final_exam_hash, len(result.final_items), _canonical(final_paper), timestamp))
            db.execute(f"INSERT INTO {RECEIPTS}(job_id,context_hash,receipt_kind,receipt_hash,receipt_version,payload_json,created_at) VALUES(?,?,?,?,?,?,?)", (job_id, head["context_hash"], "final", final_embedded, expected_final_version, _canonical(result.final_receipt), timestamp))
        db.execute(f"INSERT INTO {RESULTS}(job_id,context_hash,result_hash,pre_adaptation_hash,final_exam_hash,pre_receipt_hash,final_receipt_hash,adapted_position,single_item_fallback,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, head["context_hash"], result_hash, result.pre_adaptation_hash, result.final_exam_hash, pre_embedded, final_embedded, result.adapted_position, 1 if result.single_item_fallback else 0, _canonical(payload), timestamp))
        return _cas(db, job_id, expected_revision, timestamp, {"result_hash": result_hash})


def persist_stage7_finalized_checkpoint(db, *, job_id: str, expected_revision: int, expected_stage6_revision: int, worker_id: str, lease_attempt: int, created_at: str | None = None) -> Stage7Head:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt); timestamp = _timestamp(created_at)
    if db.in_transaction: _fail("exam_stage7_store_failed", "nested_stage7_transaction_forbidden", "阶段 7 checkpoint 不能嵌套事务")
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id); head, context = core[0], core[1]
        if head["checkpoint_hash"] is not None: return _head(head)
        if head["result_hash"] is None: _fail("exam_stage7_incomplete", "final_result_missing", "阶段 7 最终结果尚未持久化")
        result_row = db.execute(f"SELECT * FROM {RESULTS} WHERE job_id=? AND result_hash=?", (job_id, head["result_hash"])).fetchone()
        s6head, previous_row, previous = _stage6_authority(db, job_id, head["stage6_checkpoint_hash"])
        if s6head["checkpoint_hash"] != head["stage6_checkpoint_hash"] or int(s6head["revision"]) != expected_stage6_revision: _fail("exam_stage7_contract_conflict", "stage6_head_cas_lost", "阶段 6 head CAS 已丢失", retryable=True)
        bindings = dict(previous.to_dict()["bindings"])
        bindings.update({"checkpoint_stage": "stage7_finalized", "pre_adaptation_hash": result_row["pre_adaptation_hash"], "final_exam_hash": result_row["final_exam_hash"], "stage7_pre_receipt_hash": result_row["pre_receipt_hash"], "stage7_final_receipt_hash": result_row["final_receipt_hash"]})
        checkpoint = build_checkpoint_v3(bindings=bindings)
        sequence = int(previous_row["sequence"]) + 1
        if db.execute(f"SELECT 1 FROM {STAGE6_CHECKPOINTS} WHERE job_id=? AND sequence=?", (job_id, sequence)).fetchone() is not None: _fail("exam_stage7_contract_conflict", "checkpoint_sequence_conflict", "阶段 7 checkpoint 序号冲突")
        db.execute(f"INSERT INTO {STAGE6_CHECKPOINTS}(job_id,checkpoint_hash,checkpoint_version,sequence,previous_checkpoint_hash,identity_hash,checkpoint_stage,graph_hash,material_hash,score_set_hash,audit_hash,ledger_head_hash,selection_hash,pre_adaptation_hash,final_exam_hash,stage7_pre_receipt_hash,stage7_final_receipt_hash,size_bytes,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, checkpoint.checkpoint_hash, checkpoint.version, sequence, previous.checkpoint_hash, checkpoint.identity_hash, "stage7_finalized", s6head["active_graph_hash"], s6head["material_hash"], s6head["score_set_hash"], s6head["audit_hash"], s6head["ledger_head_hash"], s6head["selection_hash"], bindings["pre_adaptation_hash"], bindings["final_exam_hash"], bindings["stage7_pre_receipt_hash"], bindings["stage7_final_receipt_hash"], checkpoint.size_bytes, _canonical(checkpoint.to_dict()), timestamp))
        cursor = db.execute(f"UPDATE {STAGE6_HEADS} SET checkpoint_hash=?,revision=revision+1,updated_at=? WHERE job_id=? AND revision=? AND checkpoint_hash=?", (checkpoint.checkpoint_hash, timestamp, job_id, expected_stage6_revision, previous.checkpoint_hash))
        if cursor.rowcount != 1: _fail("exam_stage7_contract_conflict", "stage6_head_cas_lost", "阶段 6 head CAS 已丢失", retryable=True)
        return _cas(db, job_id, expected_revision, timestamp, {"checkpoint_hash": checkpoint.checkpoint_hash})


def recover_stage7_state(db, *, job_id: str, worker_id: str, lease_attempt: int) -> Stage7StoredState:
    job_id, worker_id = _actor(job_id, worker_id, lease_attempt)
    if db.in_transaction: _fail("exam_stage7_store_failed", "nested_stage7_transaction_forbidden", "阶段 7 恢复不能嵌套事务")
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt); core = _load_core(db, job_id)
        head, context, evidence, _selection_obj, _facts_obj, difficulties, pre_items, pre_receipt, requests, dispositions = core
        _stage6_authority(db, job_id, head["stage6_checkpoint_hash"])
        result = None; checkpoint = None
        if head["result_hash"] is not None:
            row = db.execute(f"SELECT * FROM {RESULTS} WHERE job_id=? AND result_hash=?", (job_id, head["result_hash"])).fetchone()
            payload = _json(row["payload_json"], "stored_result_json_invalid") if row else None
            if not isinstance(payload, Mapping) or _digest(payload) != head["result_hash"]: _fail("exam_stage7_hash_mismatch", "stored_result_hash_drift", "阶段 7 最终结果漂移")
            result = Stage7Result(payload["pre_adaptation_hash"], payload["final_exam_hash"], payload["adapted_position"], payload["single_item_fallback"], tuple(payload["final_items"]), payload["pre_adaptation_receipt"], payload["final_receipt"])
            replayed, _ = _replay(core)
            if replayed is None or _result_payload(replayed) != payload: _fail("exam_stage7_hash_mismatch", "stored_result_replay_drift", "阶段 7 最终结果无法闭合重放")
            final_paper = db.execute(f"SELECT * FROM {PAPERS} WHERE job_id=? AND paper_kind='final'", (job_id,)).fetchone()
            final_receipt = db.execute(f"SELECT * FROM {RECEIPTS} WHERE job_id=? AND receipt_kind='final'", (job_id,)).fetchone()
            if final_paper is None or final_receipt is None: _fail("exam_stage7_hash_mismatch", "final_artifacts_missing", "阶段 7 最终试卷或回执缺失")
            paper_payload, receipt_payload = _json(final_paper["payload_json"], "final_paper_json_invalid"), _json(final_receipt["payload_json"], "final_receipt_json_invalid")
            receipt_body = dict(receipt_payload); receipt_hash = receipt_body.pop("receipt_hash", None)
            if (_digest(paper_payload) != final_paper["paper_hash"] or final_paper["paper_hash"] != result.final_exam_hash
                    or _digest(receipt_body) != receipt_hash or receipt_hash != final_receipt["receipt_hash"]
                    or receipt_payload != result.final_receipt):
                _fail("exam_stage7_hash_mismatch", "final_artifact_hash_drift", "阶段 7 最终试卷或回执漂移")
        if head["checkpoint_hash"] is not None:
            row = db.execute(f"SELECT * FROM {STAGE6_CHECKPOINTS} WHERE job_id=? AND checkpoint_hash=?", (job_id, head["checkpoint_hash"])).fetchone()
            if row is None or row["checkpoint_stage"] != "stage7_finalized" or row["previous_checkpoint_hash"] != head["stage6_checkpoint_hash"]: _fail("exam_stage7_hash_mismatch", "stage7_checkpoint_chain_drift", "stage7_finalized checkpoint 链漂移")
            payload = _json(row["payload_json"], "stage7_checkpoint_json_invalid")
            checkpoint = CheckpointV3(str(row["checkpoint_version"]), str(row["identity_hash"]), str(row["checkpoint_hash"]), int(row["size_bytes"]), _canonical(payload))
            validate_checkpoint_v3(checkpoint, expected_bindings=payload["bindings"])
            current_s6 = db.execute(f"SELECT * FROM {STAGE6_HEADS} WHERE job_id=?", (job_id,)).fetchone()
            bindings = payload["bindings"]
            row_bindings = (("graph_hash", current_s6["active_graph_hash"]), ("material_hash", current_s6["material_hash"]), ("score_set_hash", current_s6["score_set_hash"]), ("audit_hash", current_s6["audit_hash"]), ("ledger_head_hash", current_s6["ledger_head_hash"]), ("selection_hash", current_s6["selection_hash"]))
            head_bindings = (("material_receipt_hash", current_s6["material_hash"]), ("score_receipt_hash", current_s6["score_set_hash"]), ("score_audit_hash", current_s6["audit_hash"]), ("rejection_ledger_head_hash", current_s6["ledger_head_hash"]), ("selection_hash", current_s6["selection_hash"]))
            if (result is None or current_s6["checkpoint_hash"] != head["checkpoint_hash"]
                    or bindings.get("checkpoint_stage") != "stage7_finalized"
                    or any(row[key] != value for key, value in row_bindings)
                    or any(bindings[key] != value for key, value in head_bindings)
                    or any(bindings.get(key) != value for key, value in (("pre_adaptation_hash", result.pre_adaptation_hash), ("final_exam_hash", result.final_exam_hash), ("stage7_pre_receipt_hash", result.pre_adaptation_receipt["receipt_hash"]), ("stage7_final_receipt_hash", result.final_receipt["receipt_hash"])))):
                _fail("exam_stage7_hash_mismatch", "stage7_checkpoint_artifact_drift", "stage7_finalized checkpoint 与 HEAD/四项身份漂移")
        if checkpoint is not None: next_action = "complete"
        elif result is not None: next_action = "checkpoint"
        else:
            next_action = "quality"
            for kind in ("quality", "adaptation", "verification"):
                if kind in requests and kind not in dispositions: next_action = f"{kind}_response"; break
                if kind not in requests:
                    expected, captured = _replay(core, capture=kind)
                    if captured.get("request") is not None: next_action = f"{kind}_request"; break
            else: next_action = "final"
        return Stage7StoredState(_head(head), context, evidence.get("local_answer"), evidence["global_constraint"], difficulties, pre_items, pre_receipt, requests, dispositions, result, checkpoint, next_action)


__all__ = ["Stage7Head", "Stage7RepositoryError", "Stage7StoredState", "expected_stage7_request", "persist_stage7_context", "persist_stage7_disposition", "persist_stage7_finalized_checkpoint", "persist_stage7_request", "persist_stage7_result", "recover_stage7_state", "replay_stage7_result"]
