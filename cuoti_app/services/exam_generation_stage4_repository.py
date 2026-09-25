"""Transactional persistence for frozen Stage-4 facts and difficulty flights.

This module contains no provider integration.  It persists already validated
pure-contract values and re-reads all database authority under BEGIN IMMEDIATE.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .exam_contracts import validate_generation_job_id
from .exam_generation_stage4_candidate_facts import (
    CANDIDATE_FACTS_CONTRACT_VERSION,
    PROJECTION_IDENTITY_VERSION,
    FrozenCandidateFacts,
    build_projection_document_hash,
)
from .exam_generation_stage4_difficulty import (
    CLASSIFICATION_RESPONSE_VERSION,
    DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
    VERIFICATION_RESPONSE_VERSION,
    DifficultyDefectComponent,
    DifficultyResolutionResult,
    EffectiveDifficultyOutcome,
)
from .job_contracts import JobCancelled, JobLeaseLost


FACT_RECEIPTS = "exam_generation_stage4_fact_receipts"
FACT_ITEMS = "exam_generation_stage4_candidate_facts"
FLIGHTS = "exam_generation_stage4_difficulty_flights"
ROUNDS = "exam_generation_stage4_difficulty_round_receipts"
RESULTS = "exam_generation_stage4_difficulty_results"
OUTCOMES = "exam_generation_stage4_difficulty_outcomes"
CONFLICTS = "exam_generation_stage4_difficulty_conflicts"
FACT_RECEIPT_VERSION = "past-v3-stage4-fact-receipt-v1"
FLIGHT_VERSION = "past-v3-stage4-difficulty-flight-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class Stage4RepositoryError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CandidateFactsReceipt:
    job_id: str
    stage3_contract_hash: str
    candidate_facts_hash: str
    receipt_hash: str
    candidate_count: int
    unresolved_count: int


@dataclass(frozen=True, slots=True)
class DifficultyFlightClaim:
    flight_hash: str
    revision: int
    state: str
    execute: bool
    result_hash: str | None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _fail(code: str, reason: str, message: str, *, retryable: bool = False) -> None:
    raise Stage4RepositoryError(code, reason, message, retryable=retryable)


def _timestamp(value: str | None) -> str:
    return str(value or datetime.now(timezone.utc).isoformat(timespec="microseconds"))


def _validate_actor(job_id: str, worker_id: str, lease_attempt: int) -> tuple[str, str]:
    try:
        normalized = validate_generation_job_id(job_id)
    except ValueError:
        normalized = None
    worker = str(worker_id or "").strip()
    if normalized is None:
        _fail("exam_stage4_contract_conflict", "generation_job_id_invalid", "组卷任务身份无效")
    if not worker or type(lease_attempt) is not int or lease_attempt < 1:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    return normalized, worker


def _publication_residue_exists(db, job_id: str) -> bool:
    row = db.execute(
        "SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)",
        (job_id, job_id, job_id),
    ).fetchone()
    return bool(row[0])


_STAGE3_HASH_FIELDS = (
    "receipt_contract_version", "stage_name", "job_id", "stage3_contract_version",
    "binding_hash", "generation_request_hash", "resolution_hash", "exact_slot_hash",
    "question_count", "scoring_mode", "slot_demands_hash", "points_projection_hash",
    "source_quotas_hash", "stage3_contract_hash",
)


def _load_authority(db, job_id: str, worker_id: str, lease_attempt: int) -> Mapping[str, Any]:
    job = db.execute(
        "SELECT id,kind,status,cancel_requested,lease_owner,attempts FROM background_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if job is None:
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    if bool(job["cancel_requested"]) or str(job["status"] or "") == "cancel_requested":
        raise JobCancelled("任务已取消")
    if (str(job["kind"] or "") != "exam-generation" or str(job["status"] or "") != "running"
            or str(job["lease_owner"] or "") != worker_id or int(job["attempts"] or 0) != lease_attempt):
        raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
    stage3 = db.execute(
        "SELECT * FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,)
    ).fetchone()
    if stage3 is None:
        _fail("exam_stage4_contract_conflict", "stage3_receipt_missing", "候选事实冻结前缺少阶段 3 合同回执")
    expected = _digest({field: stage3[field] for field in _STAGE3_HASH_FIELDS})
    if not hmac.compare_digest(str(stage3["receipt_hash"] or ""), expected):
        _fail("exam_stage4_hash_mismatch", "stage3_receipt_hash_mismatch", "阶段 3 合同回执完整性校验失败")
    if _publication_residue_exists(db, job_id):
        _fail("published_recovery_required", "publication_residue_detected", "任务已有发布事实，必须先完成发布恢复")
    return stage3


def _projection_authority(db, projection: Mapping[str, Any]) -> dict[str, Any]:
    row = db.execute(
        "SELECT p.id,p.snapshot_id,s.source_snapshot,p.algorithm_version,p.dictionary_version,"
        "p.weights_version,p.build_hash,p.item_count,p.status,s.status "
        "FROM semantic_recall_projection_versions p JOIN semantic_index_snapshots s ON s.id=p.snapshot_id "
        "WHERE p.id=?",
        (projection.get("projection_id"),),
    ).fetchone()
    if row is None or str(row[8]) != "published" or str(row[9]) != "complete":
        _fail("semantic_projection_unavailable", "projection_not_published_complete", "语义投影或其快照尚未发布完成")
    document_count = int(db.execute(
        "SELECT COUNT(*) FROM semantic_recall_documents WHERE projection_id=?", (row[0],)
    ).fetchone()[0])
    if document_count != int(row[7]):
        _fail("semantic_projection_unavailable", "projection_document_count_mismatch", "语义投影文档计数与发布头不一致")
    current = {
        "identity_version": PROJECTION_IDENTITY_VERSION,
        "projection_id": str(row[0]), "semantic_snapshot_id": str(row[1]),
        "source_snapshot": str(row[2]), "algorithm_version": str(row[3]),
        "dictionary_version": str(row[4]), "weights_version": str(row[5]),
        "projection_build_hash": str(row[6]), "item_count": int(row[7]),
    }
    if current != dict(projection):
        _fail("semantic_projection_unavailable", "projection_identity_drift", "语义投影身份在冻结前发生漂移")
    return current


def _verify_live_candidate_rows(db, projection_id: str, candidates: list[Mapping[str, Any]]) -> None:
    identities = {(str(item["source_kind"]), str(item["candidate_id"])) for item in candidates}
    rows = db.execute(
        "SELECT d.projection_id,d.semantic_question_id,d.card_version_id,d.source_kind,d.candidate_id,"
        "d.subject,d.chapter,d.question_type,d.difficulty,d.similarity_group,d.source_year,d.source_file_key,"
        "d.confidence,d.content_hash,d.normalized_text_hash,c.card_hash,c.status,q.current_card_version_id,q.active,q.excluded "
        "FROM semantic_recall_documents d JOIN semantic_card_versions c ON c.id=d.card_version_id "
        "JOIN semantic_questions q ON q.id=d.semantic_question_id WHERE d.projection_id=?",
        (projection_id,),
    ).fetchall()
    live = {(str(row[3]), str(row[4])): row for row in rows if (str(row[3]), str(row[4])) in identities}
    if set(live) != identities:
        _fail("candidate_fact_unresolved", "candidate_projection_document_missing", "冻结候选未被当前投影一一覆盖")
    for item in candidates:
        identity = (str(item["source_kind"]), str(item["candidate_id"]))
        row = live[identity]
        document = {
            "projection_id": row[0], "semantic_question_id": row[1], "card_version_id": row[2],
            "source_kind": row[3], "candidate_id": row[4], "subject": row[5], "chapter": row[6],
            "question_type": row[7], "difficulty": row[8], "similarity_group": row[9],
            "source_year": row[10], "source_file_key": row[11], "confidence": row[12],
            "content_hash": row[13], "normalized_text_hash": row[14],
        }
        if (str(row[15]) != str(item["card_hash"]) or int(row[2]) != int(item["card_version_id"])
                or str(row[13]) != str(item["content_hash"])
                or build_projection_document_hash(document) != str(item["projection_document_hash"])
                or str(row[16]) != "valid" or int(row[17]) != int(row[2])
                or not bool(row[18]) or bool(row[19])):
            _fail("candidate_fact_unresolved", "candidate_live_identity_drift", "候选内容、卡片或投影身份已漂移")


_FACT_RECEIPT_HASH_FIELDS = (
    "receipt_contract_version", "candidate_facts_contract_version", "job_id",
    "stage3_contract_hash", "stage3_receipt_hash", "projection_identity_version",
    "projection_id", "semantic_snapshot_id", "source_snapshot", "algorithm_version",
    "dictionary_version", "weights_version", "projection_build_hash",
    "projection_item_count", "projection_identity_hash", "target_institution",
    "candidate_pool_hash", "candidate_facts_hash", "unresolved_candidate_ids_hash",
    "candidate_count", "unresolved_count",
)


def _prepare_fact_records(job_id: str, stage3, frozen: FrozenCandidateFacts, created_at: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(frozen, FrozenCandidateFacts):
        _fail("exam_stage4_contract_conflict", "frozen_candidate_facts_invalid", "冻结候选事实类型无效")
    payload = frozen.to_dict()
    if _digest(payload) != frozen.candidate_facts_hash or payload.get("contract_version") != CANDIDATE_FACTS_CONTRACT_VERSION:
        _fail("exam_stage4_hash_mismatch", "candidate_facts_hash_mismatch", "冻结候选事实完整性校验失败")
    projection = payload["projection_identity"]
    candidates = list(payload["candidates"])
    unresolved = list(payload["unresolved_candidate_ids"])
    pool_hash = _digest([
        {key: item[key] for key in ("source_kind", "candidate_id", "content_hash", "card_version_id", "card_hash", "projection_document_hash")}
        for item in candidates
    ])
    header = {
        "job_id": job_id, "receipt_contract_version": FACT_RECEIPT_VERSION,
        "candidate_facts_contract_version": CANDIDATE_FACTS_CONTRACT_VERSION,
        "stage3_contract_hash": str(stage3["stage3_contract_hash"]),
        "stage3_receipt_hash": str(stage3["receipt_hash"]),
        "projection_identity_version": projection["identity_version"],
        "projection_id": projection["projection_id"], "semantic_snapshot_id": projection["semantic_snapshot_id"],
        "source_snapshot": projection["source_snapshot"], "algorithm_version": projection["algorithm_version"],
        "dictionary_version": projection["dictionary_version"], "weights_version": projection["weights_version"],
        "projection_build_hash": projection["projection_build_hash"], "projection_item_count": projection["item_count"],
        "projection_identity_hash": frozen.projection_identity_hash,
        "target_institution": frozen.target_institution, "candidate_pool_hash": pool_hash,
        "candidate_facts_hash": frozen.candidate_facts_hash,
        "unresolved_candidate_ids_hash": _digest(unresolved), "candidate_count": len(candidates),
        "unresolved_count": len(unresolved), "facts_payload_json": _canonical(payload), "created_at": created_at,
    }
    header["receipt_hash"] = _digest({field: header[field] for field in _FACT_RECEIPT_HASH_FIELDS})
    items: list[dict[str, Any]] = []
    for ordinal, item in enumerate(candidates):
        fact_json = _canonical(item)
        difficulty = item["difficulty_fact"]
        items.append({
            "job_id": job_id, "candidate_facts_hash": frozen.candidate_facts_hash, "ordinal": ordinal,
            "source_kind": item["source_kind"], "candidate_id": item["candidate_id"], "subject": item["subject"],
            "question_type_family": item["question_type_family"], "chapter": item["chapter"],
            "source_exam_id": item["source_exam_id"], "section_id": item["section_id"],
            "source_institution": item["source_institution"], "content_hash": item["content_hash"],
            "content_snapshot": item["content_snapshot"], "content_snapshot_hash": item["content_snapshot_hash"],
            "card_version_id": item["card_version_id"], "card_hash": item["card_hash"],
            "projection_document_hash": item["projection_document_hash"],
            "difficulty_resolved": 1 if difficulty["resolved"] else 0,
            "difficulty_label": difficulty["label"], "difficulty_confidence": difficulty["confidence"],
            "difficulty_reason": difficulty["reason"], "fact_json": fact_json,
            "fact_hash": _digest(item), "created_at": created_at,
        })
    return header, items


def persist_candidate_facts(db, *, job_id: str, frozen: FrozenCandidateFacts, worker_id: str, lease_attempt: int, created_at: str | None = None) -> CandidateFactsReceipt:
    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage4_store_failed", "nested_stage4_transaction_forbidden", "阶段 4 事实不能进入嵌套写事务")
    timestamp = _timestamp(created_at)
    try:
        stage3 = _load_authority(db, job_id, worker_id, lease_attempt)
        header, items = _prepare_fact_records(job_id, stage3, frozen, timestamp)
        _projection_authority(db, frozen.to_dict()["projection_identity"])
        _verify_live_candidate_rows(db, header["projection_id"], [json.loads(item["fact_json"]) for item in items])
        with immediate_transaction(db):
            current_stage3 = _load_authority(db, job_id, worker_id, lease_attempt)
            current_header, current_items = _prepare_fact_records(job_id, current_stage3, frozen, timestamp)
            _projection_authority(db, frozen.to_dict()["projection_identity"])
            _verify_live_candidate_rows(db, current_header["projection_id"], [json.loads(item["fact_json"]) for item in current_items])
            if any(current_header[field] != header[field] for field in (*_FACT_RECEIPT_HASH_FIELDS, "receipt_hash")):
                _fail("exam_stage4_contract_conflict", "candidate_authority_changed_before_write", "候选事实权威身份在写入前发生冲突")
            columns = tuple(current_header)
            db.execute(f"INSERT INTO {FACT_RECEIPTS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(job_id) DO NOTHING", tuple(current_header[c] for c in columns))
            stored = db.execute(f"SELECT * FROM {FACT_RECEIPTS} WHERE job_id=?", (job_id,)).fetchone()
            if stored is None or any(stored[field] != current_header[field] for field in (*_FACT_RECEIPT_HASH_FIELDS, "receipt_hash")):
                _fail("exam_stage4_contract_conflict", "stored_candidate_facts_identity_conflict", "已有候选事实与当前冻结身份冲突")
            if not hmac.compare_digest(str(stored["receipt_hash"]), _digest({f: stored[f] for f in _FACT_RECEIPT_HASH_FIELDS})):
                _fail("exam_stage4_hash_mismatch", "stored_candidate_receipt_hash_mismatch", "已有候选事实回执完整性校验失败")
            for item in current_items:
                item_columns = tuple(item)
                db.execute(f"INSERT INTO {FACT_ITEMS}({','.join(item_columns)}) VALUES({','.join('?' for _ in item_columns)}) ON CONFLICT(job_id,candidate_id) DO NOTHING", tuple(item[c] for c in item_columns))
                stored_item = db.execute(f"SELECT * FROM {FACT_ITEMS} WHERE job_id=? AND candidate_id=?", (job_id, item["candidate_id"])).fetchone()
                if stored_item is None or any(stored_item[field] != item[field] for field in item if field != "created_at"):
                    _fail("exam_stage4_contract_conflict", "stored_candidate_fact_conflict", "已有逐候选事实与当前冻结身份冲突")
            count = int(db.execute(f"SELECT COUNT(*) FROM {FACT_ITEMS} WHERE job_id=?", (job_id,)).fetchone()[0])
            if count != len(current_items):
                _fail("exam_stage4_contract_conflict", "stored_candidate_fact_count_conflict", "已有逐候选事实数量冲突")
    except (Stage4RepositoryError, JobCancelled, JobLeaseLost):
        raise
    except sqlite3.DatabaseError as exc:
        busy = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
        raise Stage4RepositoryError("exam_stage4_store_busy" if busy else "exam_stage4_store_corrupt", "stage4_store_busy" if busy else "stage4_database_error", "阶段 4 存储暂时繁忙" if busy else "阶段 4 存储完整性校验失败", retryable=busy) from exc
    return CandidateFactsReceipt(job_id, header["stage3_contract_hash"], header["candidate_facts_hash"], header["receipt_hash"], header["candidate_count"], header["unresolved_count"])


def recover_candidate_facts(db, *, job_id: str, worker_id: str, lease_attempt: int) -> CandidateFactsReceipt | None:
    """Replay already frozen facts without consulting a refreshed projection/source."""

    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage4_store_failed", "nested_stage4_transaction_forbidden", "阶段 4 事实恢复不能进入嵌套写事务")
    try:
        with immediate_transaction(db):
            stage3 = _load_authority(db, job_id, worker_id, lease_attempt)
            stored = db.execute(f"SELECT * FROM {FACT_RECEIPTS} WHERE job_id=?", (job_id,)).fetchone()
            if stored is None:
                return None
            try:
                payload = json.loads(str(stored["facts_payload_json"]))
                payload_raw = _canonical(payload)
                candidates = tuple(payload["candidates"])
                unresolved = tuple(payload["unresolved_candidate_ids"])
                frozen = FrozenCandidateFacts(
                    str(payload["contract_version"]),
                    _digest(payload["projection_identity"]),
                    hashlib.sha256(payload_raw.encode("utf-8")).hexdigest(),
                    str(payload["target_institution"]),
                    candidates,
                    unresolved,
                    payload_raw,
                )
            except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
                _fail("exam_stage4_hash_mismatch", "stored_candidate_facts_payload_invalid", "已有候选事实载荷无法恢复")
            expected, expected_items = _prepare_fact_records(
                job_id, stage3, frozen, str(stored["created_at"])
            )
            if any(stored[field] != expected[field] for field in expected):
                _fail("exam_stage4_hash_mismatch", "stored_candidate_facts_receipt_drift", "已有候选事实回执完整性校验失败")
            rows = db.execute(
                f"SELECT * FROM {FACT_ITEMS} WHERE job_id=? ORDER BY ordinal", (job_id,)
            ).fetchall()
            if len(rows) != len(expected_items):
                _fail("exam_stage4_hash_mismatch", "stored_candidate_fact_count_drift", "已有逐候选事实数量不一致")
            for row, item in zip(rows, expected_items):
                if any(row[field] != item[field] for field in item):
                    _fail("exam_stage4_hash_mismatch", "stored_candidate_fact_item_drift", "已有逐候选事实完整性校验失败")
            return CandidateFactsReceipt(
                job_id,
                str(stored["stage3_contract_hash"]),
                str(stored["candidate_facts_hash"]),
                str(stored["receipt_hash"]),
                int(stored["candidate_count"]),
                int(stored["unresolved_count"]),
            )
    except (Stage4RepositoryError, JobCancelled, JobLeaseLost):
        raise
    except sqlite3.DatabaseError as exc:
        busy = "locked" in str(exc).casefold() or "busy" in str(exc).casefold()
        raise Stage4RepositoryError(
            "exam_stage4_store_busy" if busy else "exam_stage4_store_corrupt",
            "stage4_store_busy" if busy else "stage4_database_error",
            "阶段 4 存储暂时繁忙" if busy else "阶段 4 存储完整性校验失败",
            retryable=busy,
        ) from exc


def _binding(value: Mapping[str, Any], name: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {"model", "prompt_version", "rules_hash"}:
        _fail("candidate_fact_unresolved", f"{name}_binding_invalid", "难度解析绑定不完整")
    result = {key: str(value[key] or "").strip() for key in ("model", "prompt_version", "rules_hash")}
    if not result["model"] or not result["prompt_version"] or _HASH_RE.fullmatch(result["rules_hash"]) is None:
        _fail("candidate_fact_unresolved", f"{name}_binding_invalid", "难度解析绑定无效")
    return result


def _flight_identity(frozen: FrozenCandidateFacts, component: DifficultyDefectComponent, classification_binding: Mapping[str, Any], verification_binding: Mapping[str, Any]) -> dict[str, Any]:
    if component.candidate_facts_hash != frozen.candidate_facts_hash or not component.candidate_ids:
        _fail("candidate_fact_unresolved", "difficulty_component_invalid", "难度缺陷分量无效或为空")
    first = _binding(classification_binding, "classification")
    second = _binding(verification_binding, "verification")
    if first["rules_hash"] != second["rules_hash"] or first["prompt_version"] == second["prompt_version"]:
        _fail("candidate_fact_unresolved", "difficulty_binding_conflict", "两轮难度解析规则或提示词绑定冲突")
    known = {str(item["candidate_id"]): item for item in frozen.candidates}
    ids = tuple(sorted(component.candidate_ids))
    if any(item not in known or bool(known[item]["difficulty_fact"]["resolved"]) for item in ids):
        _fail("candidate_fact_unresolved", "difficulty_component_candidate_invalid", "难度缺陷分量引用无效候选")
    input_hash = _digest({
        "candidate_facts_hash": frozen.candidate_facts_hash, "component_hash": component.component_hash,
        "candidates": [known[item] for item in ids], "rules_hash": first["rules_hash"],
    })
    return {
        "flight_contract_version": FLIGHT_VERSION,
        "resolution_contract_version": DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
        "candidate_facts_hash": frozen.candidate_facts_hash, "component_hash": component.component_hash,
        "input_facts_hash": input_hash, "rules_hash": first["rules_hash"],
        "classification_model": first["model"], "classification_prompt_version": first["prompt_version"],
        "verification_model": second["model"], "verification_prompt_version": second["prompt_version"],
        "candidate_ids_hash": _digest(list(ids)), "candidate_count": len(ids),
    }


def acquire_difficulty_flight(db, *, job_id: str, frozen: FrozenCandidateFacts, component: DifficultyDefectComponent, classification_binding: Mapping[str, Any], verification_binding: Mapping[str, Any], worker_id: str, lease_attempt: int, created_at: str | None = None) -> DifficultyFlightClaim:
    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    if db.in_transaction:
        _fail("exam_stage4_store_failed", "nested_stage4_transaction_forbidden", "难度 flight 不能进入嵌套写事务")
    identity = _flight_identity(frozen, component, classification_binding, verification_binding)
    flight_hash = _digest(identity)
    timestamp = _timestamp(created_at)
    created = False
    with immediate_transaction(db):
        _load_authority(db, job_id, worker_id, lease_attempt)
        fact = db.execute(f"SELECT candidate_facts_hash FROM {FACT_RECEIPTS} WHERE job_id=?", (job_id,)).fetchone()
        if fact is None or str(fact[0]) != frozen.candidate_facts_hash:
            _fail("exam_stage4_contract_conflict", "candidate_fact_receipt_missing_or_drifted", "难度解析必须绑定已持久化候选事实")
        record = {"flight_hash": flight_hash, **identity, "state": "running", "revision": 1,
                  "claim_job_id": job_id, "claim_worker_id": worker_id, "claim_attempt": lease_attempt,
                  "result_hash": None, "created_at": timestamp, "updated_at": timestamp}
        columns = tuple(record)
        cursor = db.execute(f"INSERT INTO {FLIGHTS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)}) ON CONFLICT(flight_hash) DO NOTHING", tuple(record[c] for c in columns))
        created = cursor.rowcount == 1
        row = db.execute(f"SELECT * FROM {FLIGHTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
        if row is None or any(row[field] != identity[field] for field in identity):
            _fail("exam_stage4_contract_conflict", "difficulty_flight_identity_conflict", "已有难度 flight 身份冲突")
        execute = bool(created)
        if not created and str(row["state"]) == "running":
            old_job = db.execute(
                "SELECT kind,status,cancel_requested,lease_owner,attempts "
                "FROM background_jobs WHERE id=?",
                (row["claim_job_id"],),
            ).fetchone()
            old_claim_active = bool(
                old_job is not None
                and str(old_job["kind"] or "") == "exam-generation"
                and str(old_job["status"] or "") == "running"
                and not bool(old_job["cancel_requested"])
                and str(old_job["lease_owner"] or "") == str(row["claim_worker_id"])
                and int(old_job["attempts"] or 0) == int(row["claim_attempt"])
            )
            same_claim = (
                str(row["claim_job_id"]) == job_id
                and str(row["claim_worker_id"]) == worker_id
                and int(row["claim_attempt"]) == lease_attempt
            )
            if not old_claim_active and not same_claim:
                previous_revision = int(row["revision"])
                cursor = db.execute(
                    f"UPDATE {FLIGHTS} SET revision=revision+1,claim_job_id=?,"
                    "claim_worker_id=?,claim_attempt=?,updated_at=? "
                    "WHERE flight_hash=? AND state='running' AND revision=? "
                    "AND claim_job_id=? AND claim_worker_id=? AND claim_attempt=?",
                    (
                        job_id, worker_id, lease_attempt, timestamp, flight_hash,
                        previous_revision, row["claim_job_id"], row["claim_worker_id"],
                        row["claim_attempt"],
                    ),
                )
                if cursor.rowcount != 1:
                    _fail("exam_stage4_contract_conflict", "difficulty_flight_takeover_cas_lost", "难度 flight 接管发生并发冲突")
                row = db.execute(f"SELECT * FROM {FLIGHTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
                execute = True
        result = DifficultyFlightClaim(
            flight_hash,
            int(row["revision"]),
            str(row["state"]),
            execute,
            row["result_hash"],
        )
    return result


def complete_difficulty_flight(db, *, job_id: str, flight_hash: str, expected_revision: int, result: DifficultyResolutionResult, worker_id: str, lease_attempt: int, created_at: str | None = None) -> str:
    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    if type(expected_revision) is not int or expected_revision < 1 or not isinstance(result, DifficultyResolutionResult):
        _fail("exam_stage4_contract_conflict", "difficulty_completion_input_invalid", "难度解析完成参数无效")
    timestamp = _timestamp(created_at)
    payload = result.to_dict()
    if _digest(payload) != result.result_hash:
        _fail("exam_stage4_hash_mismatch", "difficulty_result_hash_mismatch", "难度解析结果完整性校验失败")
    with immediate_transaction(db):
        _load_authority(db, job_id, worker_id, lease_attempt)
        flight = db.execute(f"SELECT * FROM {FLIGHTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
        if flight is None:
            _fail("exam_stage4_contract_conflict", "difficulty_flight_missing", "难度 flight 不存在")
        if str(flight["state"]) == "completed":
            if str(flight["result_hash"]) == result.result_hash:
                return result.result_hash
            _fail("exam_stage4_contract_conflict", "difficulty_flight_already_completed", "难度 flight 已由不同结果完成")
        if int(flight["revision"]) != expected_revision:
            _fail("exam_stage4_contract_conflict", "difficulty_flight_stale_revision", "难度 flight 修订已过期")
        if str(flight["claim_job_id"]) != job_id or str(flight["claim_worker_id"]) != worker_id or int(flight["claim_attempt"]) != lease_attempt:
            raise JobLeaseLost("当前 worker attempt 不持有该难度 flight")
        if (result.contract_version != flight["resolution_contract_version"] or result.candidate_facts_hash != flight["candidate_facts_hash"]
                or result.component_hash != flight["component_hash"] or result.input_facts_hash != flight["input_facts_hash"]):
            _fail("exam_stage4_contract_conflict", "difficulty_result_flight_mismatch", "难度结果未严格绑定当前 flight")
        if payload.get("classification_binding") != {"model": flight["classification_model"], "prompt_version": flight["classification_prompt_version"], "rules_hash": flight["rules_hash"]}:
            _fail("exam_stage4_contract_conflict", "classification_round_binding_mismatch", "分类回执未严格绑定当前 flight")
        if payload.get("verification_binding") != {"model": flight["verification_model"], "prompt_version": flight["verification_prompt_version"], "rules_hash": flight["rules_hash"]}:
            _fail("exam_stage4_contract_conflict", "verification_round_binding_mismatch", "核验回执未严格绑定当前 flight")
        outcomes = list(payload.get("outcomes") or [])
        if len(outcomes) != int(flight["candidate_count"]):
            _fail("exam_stage4_contract_conflict", "difficulty_outcome_count_mismatch", "难度结果未覆盖 flight 候选")
        for round_name, schema, model_field, prompt_field, response_field in (
            ("classification", CLASSIFICATION_RESPONSE_VERSION, "classification_model", "classification_prompt_version", "classification_response_hash"),
            ("verification", VERIFICATION_RESPONSE_VERSION, "verification_model", "verification_prompt_version", "verification_response_hash"),
        ):
            response_hash = payload.get(response_field)
            receipt = {
                "flight_hash": flight_hash, "completed_from_revision": expected_revision, "round_name": round_name,
                "response_schema_version": schema, "model": flight[model_field], "prompt_version": flight[prompt_field],
                "rules_hash": flight["rules_hash"], "input_facts_hash": flight["input_facts_hash"],
                "response_status": "received" if response_hash is not None else "timeout", "response_hash": response_hash,
            }
            receipt["receipt_hash"] = _digest(receipt)
            receipt["created_at"] = timestamp
            columns = tuple(receipt)
            db.execute(f"INSERT INTO {ROUNDS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(receipt[c] for c in columns))
        result_record = {
            "flight_hash": flight_hash, "completed_from_revision": expected_revision,
            "result_contract_version": result.contract_version, "candidate_facts_hash": result.candidate_facts_hash,
            "component_hash": result.component_hash, "input_facts_hash": result.input_facts_hash,
            "result_hash": result.result_hash, "outcome_count": len(outcomes), "result_payload_json": _canonical(payload),
        }
        result_record["receipt_hash"] = _digest(result_record)
        result_record["created_at"] = timestamp
        columns = tuple(result_record)
        db.execute(f"INSERT INTO {RESULTS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(result_record[c] for c in columns))
        for outcome in outcomes:
            outcome_hash = _digest(outcome)
            record = {"flight_hash": flight_hash, "result_hash": result.result_hash, **outcome,
                      "outcome_hash": outcome_hash, "created_at": timestamp}
            columns = tuple(record)
            db.execute(f"INSERT INTO {OUTCOMES}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", tuple(record[c] for c in columns))
            if outcome["status"] == "conflict":
                conflict = {"type": outcome["reason"], "candidate_id": outcome["candidate_id"], "outcome_hash": outcome_hash}
                db.execute(f"INSERT INTO {CONFLICTS}(flight_hash,observed_revision,conflict_type,candidate_id,evidence_hash,conflict_json,created_at) VALUES(?,?,?,?,?,?,?)",
                           (flight_hash, expected_revision, outcome["reason"], outcome["candidate_id"], outcome["evidence_hash"], _canonical(conflict), timestamp))
        cursor = db.execute(f"UPDATE {FLIGHTS} SET state='completed',revision=revision+1,result_hash=?,updated_at=? WHERE flight_hash=? AND state='running' AND revision=?",
                            (result.result_hash, timestamp, flight_hash, expected_revision))
        if cursor.rowcount != 1:
            _fail("exam_stage4_contract_conflict", "difficulty_flight_stale_revision", "难度 flight 修订已过期")
    return result.result_hash


def recover_completed_difficulty_result(
    db,
    *,
    job_id: str,
    flight_hash: str,
    worker_id: str,
    lease_attempt: int,
) -> DifficultyResolutionResult | None:
    """Recover one completed flight without discarding any binding evidence."""

    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    if not isinstance(flight_hash, str) or _HASH_RE.fullmatch(flight_hash) is None:
        _fail("exam_stage4_contract_conflict", "difficulty_flight_hash_invalid", "难度 flight 身份无效")
    if db.in_transaction:
        _fail("exam_stage4_store_failed", "nested_stage4_transaction_forbidden", "难度结果恢复不能进入嵌套事务")
    with immediate_transaction(db):
        _load_authority(db, job_id, worker_id, lease_attempt)
        fact = db.execute(
            f"SELECT candidate_facts_hash FROM {FACT_RECEIPTS} WHERE job_id=?",
            (job_id,),
        ).fetchone()
        flight = db.execute(f"SELECT * FROM {FLIGHTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
        if flight is None or str(flight["state"]) != "completed":
            return None
        if fact is None or str(fact[0]) != str(flight["candidate_facts_hash"]):
            _fail("exam_stage4_contract_conflict", "difficulty_result_candidate_facts_mismatch", "难度结果不属于当前冻结候选事实")
        row = db.execute(f"SELECT * FROM {RESULTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
        if row is None or str(row["result_hash"]) != str(flight["result_hash"]):
            _fail("exam_stage4_hash_mismatch", "completed_difficulty_result_missing", "已完成难度 flight 缺少完整结果")
        try:
            payload = json.loads(str(row["result_payload_json"]))
        except (TypeError, ValueError):
            _fail("exam_stage4_hash_mismatch", "completed_difficulty_result_json_invalid", "已完成难度结果 JSON 无效")
        receipt_body = {
            "flight_hash": flight_hash,
            "completed_from_revision": row["completed_from_revision"],
            "result_contract_version": row["result_contract_version"],
            "candidate_facts_hash": row["candidate_facts_hash"],
            "component_hash": row["component_hash"],
            "input_facts_hash": row["input_facts_hash"],
            "result_hash": row["result_hash"],
            "outcome_count": row["outcome_count"],
            "result_payload_json": _canonical(payload),
        }
        binding_ok = (
            payload.get("contract_version") == DIFFICULTY_RESOLUTION_CONTRACT_VERSION
            and payload.get("candidate_facts_hash") == flight["candidate_facts_hash"]
            and payload.get("component_hash") == flight["component_hash"]
            and payload.get("input_facts_hash") == flight["input_facts_hash"]
            and payload.get("classification_binding") == {
                "model": flight["classification_model"],
                "prompt_version": flight["classification_prompt_version"],
                "rules_hash": flight["rules_hash"],
            }
            and payload.get("verification_binding") == {
                "model": flight["verification_model"],
                "prompt_version": flight["verification_prompt_version"],
                "rules_hash": flight["rules_hash"],
            }
        )
        if (
            _digest(payload) != row["result_hash"]
            or _digest(receipt_body) != row["receipt_hash"]
            or not binding_ok
            or not isinstance(payload.get("outcomes"), list)
            or len(payload["outcomes"]) != int(row["outcome_count"])
        ):
            _fail("exam_stage4_hash_mismatch", "completed_difficulty_result_binding_mismatch", "已完成难度结果完整性或绑定漂移")
        round_rows = db.execute(
            f"SELECT * FROM {ROUNDS} WHERE flight_hash=? ORDER BY round_name",
            (flight_hash,),
        ).fetchall()
        if len(round_rows) != 2 or {str(item["round_name"]) for item in round_rows} != {"classification", "verification"}:
            _fail("exam_stage4_hash_mismatch", "completed_difficulty_rounds_missing", "已完成难度结果缺少完整两轮回执")
        round_by_name = {str(item["round_name"]): item for item in round_rows}
        for round_name, schema, model_field, prompt_field, response_field in (
            ("classification", CLASSIFICATION_RESPONSE_VERSION, "classification_model", "classification_prompt_version", "classification_response_hash"),
            ("verification", VERIFICATION_RESPONSE_VERSION, "verification_model", "verification_prompt_version", "verification_response_hash"),
        ):
            item = round_by_name[round_name]
            response_hash = payload.get(response_field)
            expected_round = {
                "flight_hash": flight_hash,
                "completed_from_revision": row["completed_from_revision"],
                "round_name": round_name,
                "response_schema_version": schema,
                "model": flight[model_field],
                "prompt_version": flight[prompt_field],
                "rules_hash": flight["rules_hash"],
                "input_facts_hash": flight["input_facts_hash"],
                "response_status": "received" if response_hash is not None else "timeout",
                "response_hash": response_hash,
            }
            if any(item[key] != value for key, value in expected_round.items()) or _digest(expected_round) != item["receipt_hash"]:
                _fail("exam_stage4_hash_mismatch", "completed_difficulty_round_binding_mismatch", "已完成难度两轮回执完整性或绑定漂移")
        stored = db.execute(
            f"SELECT * FROM {OUTCOMES} WHERE flight_hash=? ORDER BY candidate_id",
            (flight_hash,),
        ).fetchall()
        expected = sorted(payload["outcomes"], key=lambda item: item.get("candidate_id", ""))
        if len(stored) != len(expected):
            _fail("exam_stage4_hash_mismatch", "completed_difficulty_outcome_count_mismatch", "已完成难度逐项结果不完整")
        outcomes: list[EffectiveDifficultyOutcome] = []
        for row_item, value in zip(stored, expected):
            if not isinstance(value, Mapping) or _digest(value) != row_item["outcome_hash"] or any(row_item[key] != value[key] for key in value):
                _fail("exam_stage4_hash_mismatch", "completed_difficulty_outcome_mismatch", "已完成难度逐项结果漂移")
            outcomes.append(EffectiveDifficultyOutcome(**value))
        return DifficultyResolutionResult(
            str(row["result_contract_version"]),
            str(row["candidate_facts_hash"]),
            str(row["component_hash"]),
            str(row["input_facts_hash"]),
            tuple(outcomes),
            str(row["result_hash"]),
            _canonical(payload),
        )


def recover_completed_difficulty_result_for_component(
    db,
    *,
    job_id: str,
    frozen: FrozenCandidateFacts,
    component: DifficultyDefectComponent,
    classification_binding: Mapping[str, Any],
    verification_binding: Mapping[str, Any],
    worker_id: str,
    lease_attempt: int,
) -> DifficultyResolutionResult | None:
    """Look up the exact immutable flight identity before acquiring execution."""

    identity = _flight_identity(
        frozen, component, classification_binding, verification_binding
    )
    return recover_completed_difficulty_result(
        db,
        job_id=job_id,
        flight_hash=_digest(identity),
        worker_id=worker_id,
        lease_attempt=lease_attempt,
    )


def append_difficulty_conflict(db, *, job_id: str, flight_hash: str, observed_revision: int, conflict_type: str, evidence: Mapping[str, Any], worker_id: str, lease_attempt: int, candidate_id: str | None = None, created_at: str | None = None) -> int:
    job_id, worker_id = _validate_actor(job_id, worker_id, lease_attempt)
    conflict_type = str(conflict_type or "").strip()
    if not conflict_type or type(observed_revision) is not int or observed_revision < 1 or not isinstance(evidence, Mapping):
        _fail("exam_stage4_contract_conflict", "difficulty_conflict_invalid", "难度冲突记录无效")
    payload = dict(evidence)
    payload_hash = _digest(payload)
    with immediate_transaction(db):
        _load_authority(db, job_id, worker_id, lease_attempt)
        flight = db.execute(f"SELECT revision FROM {FLIGHTS} WHERE flight_hash=?", (flight_hash,)).fetchone()
        if flight is None:
            _fail("exam_stage4_contract_conflict", "difficulty_flight_missing", "难度 flight 不存在")
        cursor = db.execute(f"INSERT INTO {CONFLICTS}(flight_hash,observed_revision,conflict_type,candidate_id,evidence_hash,conflict_json,created_at) VALUES(?,?,?,?,?,?,?)",
                            (flight_hash, observed_revision, conflict_type, candidate_id, payload_hash, _canonical(payload), _timestamp(created_at)))
        return int(cursor.lastrowid)


__all__ = ["CandidateFactsReceipt", "DifficultyFlightClaim", "Stage4RepositoryError", "acquire_difficulty_flight", "append_difficulty_conflict", "complete_difficulty_flight", "persist_candidate_facts", "recover_candidate_facts", "recover_completed_difficulty_result", "recover_completed_difficulty_result_for_component"]
