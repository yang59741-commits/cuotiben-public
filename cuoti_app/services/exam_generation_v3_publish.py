"""Deterministic single-paper assembly and native past-v3 publication.

This module deliberately does not import the legacy draft/manifest publisher.
The Stage-7 finalized chain is the only paper authority accepted here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

from ..migrations.v39_exam_generation_attempt_binding import BINDING_HASH_FIELDS
from ..migrations.v40_exam_generation_stage3_contract import RECEIPT_HASH_FIELDS as STAGE3_RECEIPT_HASH_FIELDS
from ..migrations.v46_exam_generation_v3_publish import HEADS, PUBLICATIONS, RECEIPTS, REQUESTS, USAGE
from ..transactions import immediate_transaction
from .exam_generation_stage3_contract import PastV3GenerationContract
from .exam_generation_stage6_checkpoint_v3 import CheckpointV3, validate_checkpoint_v3
from .exam_generation_stage7_quality import STAGE7_VERSION, Stage7Result
from .exam_scoring import ExamScoringProjection, normalize_scoring_projection
from .exam_time_policy import ExamDeadlineExceeded, parse_timestamp
from .job_contracts import JobCancelled, JobLeaseLost
from .tex import UnsafeLatexError, validate_latex_body
from .exam_generation_compat import PipelineIdentity
from .exam_item_chapters import native_frozen_chapter_snapshot


REQUEST_VERSION = "past-v3-native-publish-request-v1"
PUBLICATION_VERSION = "past-v3-native-publication-v1"
RECEIPT_VERSION = "past-v3-native-publication-receipt-v1"
USAGE_VERSION = "past-v3-native-candidate-usage-v1"
ARTIFACT_VERSION = "past-v3-native-artifact-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


class ExamGenerationV3PublishError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code, self.reason, self.retryable = code, reason, retryable


class _ReceiptDeferred(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NativeV3PaperDraft:
    title: str
    body: str
    latex_source: str
    structured_items: tuple[Mapping[str, Any], ...]
    scoring: ExamScoringProjection
    final_exam_hash: str
    draft_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": "past-v3-native-paper-draft-v1", "title": self.title,
            "body": self.body, "latex_source": self.latex_source,
            "structured_items": [dict(item) for item in self.structured_items],
            "scoring": self.scoring.to_dict(), "final_exam_hash": self.final_exam_hash,
        }


@dataclass(frozen=True, slots=True)
class NativeV3PublishOutcome:
    job_id: str
    state: str
    request_hash: str
    exam_id: int | None = None
    publication_hash: str | None = None
    receipt_hash: str | None = None
    recovered: bool = False

    @property
    def published(self) -> bool:
        return self.exam_id is not None

    @property
    def status(self) -> str:
        if self.state == "published_pending_receipt":
            return "published_pending_receipt"
        return "recovered" if self.recovered else "completed"

    @property
    def exam(self) -> Mapping[str, Any] | None:
        return None if self.exam_id is None else {"id": self.exam_id}

    def to_coordinator_result(self) -> dict[str, Any]:
        if self.exam is None:
            _fail("coordinator_exam_missing", "原生发布结果缺少单卷身份")
        return {
            "status": self.status, "exam": dict(self.exam), "state": self.state,
            "request_hash": self.request_hash, "publication_hash": self.publication_hash,
            "receipt_hash": self.receipt_hash,
        }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ExamGenerationV3PublishError("exam_v3_publish_invalid", "non_canonical_json", "原生发布数据不能规范序列化") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _raw_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fail(reason: str, message: str, *, retryable: bool = False) -> None:
    raise ExamGenerationV3PublishError("exam_v3_publish_conflict", reason, message, retryable=retryable)


def _hash(value: object, reason: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        _fail(reason, "原生发布权威哈希无效")
    return value


def _timestamp(value: str | None) -> str:
    return str(value or datetime.now(timezone.utc).isoformat(timespec="microseconds"))


def _result_payload(result: Stage7Result) -> dict[str, Any]:
    return {
        "pre_adaptation_hash": result.pre_adaptation_hash, "final_exam_hash": result.final_exam_hash,
        "adapted_position": result.adapted_position, "single_item_fallback": result.single_item_fallback,
        "final_items": list(result.final_items), "pre_adaptation_receipt": dict(result.pre_adaptation_receipt),
        "final_receipt": dict(result.final_receipt),
    }


def _escape_latex(value: str) -> str:
    table = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(table.get(char, char) for char in value)


def _stage3_scoring(contract: PastV3GenerationContract, item_count: int) -> ExamScoringProjection:
    payload = contract.to_dict()
    scoring = payload.get("scoring")
    if not isinstance(scoring, Mapping) or scoring.get("mode") not in {"verified", "unscored"}:
        _fail("stage3_scoring_projection_invalid", "阶段 3 计分投影无效")
    if scoring["mode"] == "unscored":
        value = {"mode": "unscored", "total_points": None, "evidence_hash": None, "items": []}
    else:
        slots = scoring.get("slots")
        if not isinstance(slots, list):
            _fail("stage3_scoring_projection_invalid", "阶段 3 已验证分值槽位无效")
        value = {
            "mode": "verified", "total_points": scoring.get("total_points"),
            "evidence_hash": scoring.get("evidence_hash"),
            "items": [{"position": row.get("position"), "points": row.get("points"), "evidence_hash": row.get("points_evidence_hash")} for row in slots if isinstance(row, Mapping)],
        }
    try:
        projection = normalize_scoring_projection(value, item_count=item_count)
    except (TypeError, ValueError) as exc:
        raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "stage3_scoring_projection_invalid", "阶段 3 计分投影无法无损发布") from exc
    if projection.mode != contract.scoring_mode:
        _fail("stage3_scoring_mode_drift", "阶段 3 计分模式发生漂移")
    return projection


def _validate_stage3_contract(contract: PastV3GenerationContract) -> Mapping[str, Any]:
    try:
        payload = contract.to_dict()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "stage3_contract_json_invalid", "阶段 3 合同 JSON 无效") from exc
    scalar = {
        "contract_version": contract.contract_version, "generation_request_hash": contract.generation_request_hash,
        "binding_hash": contract.binding_hash, "resolution_hash": contract.resolution_hash,
        "profile_hash": contract.profile_hash, "exact_slot_hash": contract.exact_slot_hash,
        "question_count": contract.question_count,
    }
    if _digest(payload) != contract.stage3_contract_hash or any(payload.get(key) != value for key, value in scalar.items()):
        _fail("stage3_contract_hash_drift", "阶段 3 合同载荷或身份哈希漂移")
    scoring, slots, quotas = payload.get("scoring"), payload.get("slot_demands"), payload.get("source_quotas")
    if (not isinstance(scoring, Mapping) or not isinstance(slots, list) or not isinstance(quotas, Mapping)
            or scoring.get("mode") != contract.scoring_mode
            or _digest({"slot_demands": slots}) != contract.slot_demands_hash
            or _digest(scoring) != contract.points_projection_hash
            or _digest({"candidate_source": payload.get("candidate_source"), "source_quotas": quotas}) != contract.source_quotas_hash):
        _fail("stage3_projection_hash_drift", "阶段 3 槽位、计分或来源配额投影漂移")
    return payload


def assemble_v3_single_paper(*, stage7_checkpoint: CheckpointV3, stage7_result: Stage7Result, stage3_contract: PastV3GenerationContract, title: str) -> NativeV3PaperDraft:
    """Build exactly one draft from finalized Stage-7 and Stage-3 facts."""

    if not isinstance(stage7_checkpoint, CheckpointV3) or not isinstance(stage7_result, Stage7Result) or not isinstance(stage3_contract, PastV3GenerationContract):
        _fail("assembler_authority_type_invalid", "原生组装仅接受阶段 3/7 的强类型权威制品")
    if not isinstance(title, str) or not title.strip():
        _fail("paper_title_invalid", "试卷标题不能为空")
    _validate_stage3_contract(stage3_contract)
    bindings = stage7_checkpoint.to_dict().get("bindings")
    if not isinstance(bindings, Mapping):
        _fail("stage7_checkpoint_bindings_missing", "stage7_finalized checkpoint 缺少绑定")
    try:
        validate_checkpoint_v3(stage7_checkpoint, expected_bindings=bindings)
    except Exception as exc:
        raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "stage7_checkpoint_invalid", "stage7_finalized checkpoint 无法验证") from exc
    if bindings.get("checkpoint_stage") != "stage7_finalized" or bindings.get("final_exam_hash") != stage7_result.final_exam_hash:
        _fail("stage7_checkpoint_not_finalized", "仅 stage7_finalized checkpoint 可进入原生组装")
    expected_final_hash = _digest({"version": STAGE7_VERSION, "items": list(stage7_result.final_items)})
    if not hmac.compare_digest(expected_final_hash, stage7_result.final_exam_hash):
        _fail("stage7_final_hash_drift", "阶段 7 最终试卷哈希漂移")
    items = list(stage7_result.final_items)
    if len(items) != stage3_contract.question_count or not items:
        _fail("final_item_count_mismatch", "最终题目数量与阶段 3 合同不一致")
    scoring = _stage3_scoring(stage3_contract, len(items))
    structured: list[dict[str, Any]] = []
    latex_items: list[str] = []
    plain_items: list[str] = []
    seen_candidates: set[str] = set()
    for position, raw in enumerate(items, start=1):
        if not isinstance(raw, Mapping) or raw.get("position") != position:
            _fail("final_item_positions_invalid", "阶段 7 最终题目位置不连续")
        assignment, snapshot = raw.get("assignment"), raw.get("content_snapshot")
        if not isinstance(assignment, Mapping) or not isinstance(snapshot, Mapping):
            _fail("final_item_shape_invalid", "阶段 7 最终题目结构无效")
        candidate_id, content = assignment.get("candidate_id"), snapshot.get("content")
        if not isinstance(candidate_id, str) or not candidate_id or candidate_id in seen_candidates or not isinstance(content, str) or not content.strip():
            _fail("final_item_identity_invalid", "阶段 7 最终题目身份或正文无效")
        seen_candidates.add(candidate_id)
        try:
            validated_content = validate_latex_body(content, label=f"第 {position} 题正文")
            raw_options = snapshot.get("options") or []
            if not isinstance(raw_options, list) or any(not isinstance(option, str) for option in raw_options):
                _fail("final_item_options_invalid", "阶段 7 最终题目选项无效")
            validated_options = [validate_latex_body(option, label=f"第 {position} 题选项") for option in raw_options]
        except UnsafeLatexError as exc:
            raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "unsafe_final_item_latex", "阶段 7 最终题目包含禁用 LaTeX") from exc
        content_hash = _hash(assignment.get("content_hash"), "final_item_content_hash_invalid")
        structured.append({
            "position": position, "candidate_id": candidate_id, "content_hash": content_hash,
            "content": validated_content, "question_type": str(assignment.get("question_type_family") or ""),
            "difficulty": str(assignment.get("target_difficulty") or ""),
            "source_kind": str(assignment.get("source_kind") or "question_bank"),
            "adapted": "adaptation" in raw,
        })
        option_text = "".join(f"\\\\ {chr(65+i)}. {option}" for i, option in enumerate(validated_options))
        latex_items.append(f"\\item {validated_content}{option_text}")
        plain_items.append(f"{position}. {content}")
    body = "\n\n".join(plain_items)
    latex = "\\documentclass[UTF8]{ctexart}\n\\usepackage{amsmath,amssymb}\n\\begin{document}\n\\section*{" + _escape_latex(title.strip()) + "}\n\\begin{enumerate}\n" + "\n".join(latex_items) + "\n\\end{enumerate}\n\\end{document}\n"
    provisional = NativeV3PaperDraft(title.strip(), body, latex, tuple(structured), scoring, stage7_result.final_exam_hash, "0" * 64)
    return NativeV3PaperDraft(provisional.title, body, latex, provisional.structured_items, scoring, stage7_result.final_exam_hash, _digest(provisional.to_dict()))


def _job_columns(db) -> set[str]:
    return {str(row[1]) for row in db.execute("PRAGMA table_info(background_jobs)").fetchall()}


def _fence(db, job_id: str, worker_id: str, lease_attempt: int, *, side_effect_committed: bool = False) -> None:
    columns = _job_columns(db)
    selected = ["kind", "status", "cancel_requested", "lease_owner", "attempts"]
    selected.extend(
        name for name in (
            "lease_expires_at", "deadline_at", "deadline_policy_mode",
            "deadline_policy_version",
        ) if name in columns
    )
    row = db.execute(
        f"SELECT {','.join(selected)} FROM background_jobs WHERE id=?", (job_id,)
    ).fetchone()
    if row is None or str(row["kind"] or "") != "exam-generation" or str(row["lease_owner"] or "") != worker_id or int(row["attempts"] or 0) != lease_attempt:
        raise JobLeaseLost("当前 worker attempt 不持有原生发布权限")
    now = datetime.now(timezone.utc)
    if "lease_expires_at" in columns and row["lease_expires_at"]:
        try:
            if parse_timestamp(row["lease_expires_at"]) <= now:
                raise JobLeaseLost("当前 worker attempt 租约已经到期")
        except ValueError as exc:
            raise JobLeaseLost("当前 worker attempt 租约时间无效") from exc
    cancelled = bool(row["cancel_requested"]) or str(row["status"] or "") == "cancel_requested"
    if cancelled and not side_effect_committed:
        raise JobCancelled("任务已请求取消")
    if (
        not side_effect_committed
        and "deadline_policy_mode" in columns
        and str(row["deadline_policy_mode"] or "") == "enforce"
        and row["deadline_at"]
        and parse_timestamp(row["deadline_at"]) <= now
    ):
        raise ExamDeadlineExceeded(
            stage="pdf_render_and_publication",
            deadline_at=str(row["deadline_at"]),
            policy_version=str(row["deadline_policy_version"] or ""),
            now=now,
        )
    allowed = (
        {"running", "cancel_requested", "published_pending_receipt"}
        if side_effect_committed else {"running"}
    )
    if str(row["status"] or "") not in allowed:
        raise JobLeaseLost("当前 worker attempt 不持有原生发布权限")


def _verify_authority(db, *, job_id: str, stage7_checkpoint: CheckpointV3, stage7_result: Stage7Result, stage3_contract: PastV3GenerationContract) -> tuple[str, str]:
    binding = db.execute("SELECT * FROM exam_generation_attempt_bindings WHERE job_id=?", (job_id,)).fetchone()
    stage3 = db.execute("SELECT * FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,)).fetchone()
    head = db.execute("SELECT * FROM exam_generation_stage7_heads WHERE job_id=?", (job_id,)).fetchone()
    if binding is None or stage3 is None or head is None:
        _fail("native_authority_missing", "原生发布缺少 v39、阶段 3 或阶段 7 权威事实")
    binding_expected = _digest({field: binding[field] for field in BINDING_HASH_FIELDS})
    if not hmac.compare_digest(str(binding["binding_hash"] or ""), binding_expected) or binding["binding_hash"] != stage3_contract.binding_hash:
        _fail("attempt_binding_hash_drift", "v39 attempt binding 完整性校验失败")
    stage3_expected_receipt = _digest({field: stage3[field] for field in STAGE3_RECEIPT_HASH_FIELDS})
    if (stage3["stage3_contract_hash"] != stage3_contract.stage3_contract_hash
            or stage3["scoring_mode"] != stage3_contract.scoring_mode
            or stage3["binding_hash"] != stage3_contract.binding_hash
            or stage3["question_count"] != stage3_contract.question_count
            or not hmac.compare_digest(str(stage3["receipt_hash"] or ""), stage3_expected_receipt)):
        _fail("stage3_contract_hash_drift", "阶段 3 合同哈希漂移")
    result_hash = _digest(_result_payload(stage7_result))
    if head["result_hash"] != result_hash or head["checkpoint_hash"] != stage7_checkpoint.checkpoint_hash:
        _fail("stage7_head_drift", "阶段 7 head 与输入权威制品不一致")
    result_row = db.execute("SELECT payload_json,final_exam_hash FROM exam_generation_stage7_results WHERE job_id=? AND result_hash=?", (job_id, result_hash)).fetchone()
    checkpoint_row = db.execute("SELECT payload_json,checkpoint_stage,final_exam_hash FROM exam_generation_stage6_checkpoints WHERE job_id=? AND checkpoint_hash=?", (job_id, stage7_checkpoint.checkpoint_hash)).fetchone()
    s6head = db.execute("SELECT checkpoint_hash FROM exam_generation_stage6_heads WHERE job_id=?", (job_id,)).fetchone()
    if result_row is None or checkpoint_row is None or s6head is None or _digest(json.loads(result_row["payload_json"])) != result_hash:
        _fail("stage7_authority_missing_or_corrupt", "阶段 7 最终权威事实缺失或损坏")
    if checkpoint_row["checkpoint_stage"] != "stage7_finalized" or s6head["checkpoint_hash"] != stage7_checkpoint.checkpoint_hash or checkpoint_row["final_exam_hash"] != stage7_result.final_exam_hash or result_row["final_exam_hash"] != stage7_result.final_exam_hash:
        _fail("stage7_final_hash_drift", "stage7_finalized checkpoint 或最终哈希漂移")
    if json.loads(checkpoint_row["payload_json"]) != stage7_checkpoint.to_dict():
        _fail("stage7_checkpoint_payload_drift", "stage7_finalized checkpoint payload 漂移")
    return result_hash, str(binding["binding_hash"])


def _residue(db, job_id: str) -> bool:
    return bool(db.execute("SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)", (job_id, job_id, job_id)).fetchone()[0])


def _request_payload(job_id: str, subject_id: int, draft: NativeV3PaperDraft, stage3: PastV3GenerationContract, result_hash: str, checkpoint_hash: str, binding_hash: str) -> dict[str, Any]:
    return {
        "version": REQUEST_VERSION, "job_id": job_id, "subject_id": subject_id,
        "binding_hash": binding_hash, "stage3_contract_hash": stage3.stage3_contract_hash,
        "stage7_result_hash": result_hash, "stage7_checkpoint_hash": checkpoint_hash,
        "final_exam_hash": draft.final_exam_hash, "draft_hash": draft.draft_hash, "draft": draft.to_dict(),
    }


def _freeze_request(db, *, job_id: str, worker_id: str, lease_attempt: int, payload: Mapping[str, Any], created_at: str) -> str:
    request_hash = _digest(payload)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        existing = db.execute(f"SELECT * FROM {REQUESTS} WHERE job_id=?", (job_id,)).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash or json.loads(existing["payload_json"]) != payload:
                _fail("frozen_request_conflict", "已有原生发布请求与当前权威链冲突")
            return request_hash
        if _residue(db, job_id):
            _fail("legacy_or_conflicting_publication_residue", "任务已有旧发布或冲突发布残留")
        columns = ("job_id", "request_version", "binding_hash", "stage3_contract_hash", "stage7_result_hash", "stage7_checkpoint_hash", "final_exam_hash", "draft_hash", "request_hash", "payload_json", "created_at")
        values = (job_id, REQUEST_VERSION, payload["binding_hash"], payload["stage3_contract_hash"], payload["stage7_result_hash"], payload["stage7_checkpoint_hash"], payload["final_exam_hash"], payload["draft_hash"], request_hash, _canonical(payload), created_at)
        db.execute(f"INSERT INTO {REQUESTS}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})", values)
        db.execute(f"INSERT INTO {HEADS}(job_id,request_hash,state,revision,created_at,updated_at) VALUES(?,?,'request_frozen',1,?,?)", (job_id, request_hash, created_at, created_at))
    return request_hash


def _reservation_config(*, request_hash: str, draft: NativeV3PaperDraft, worker_id: str, lease_attempt: int, publication_version: str = PUBLICATION_VERSION) -> dict[str, Any]:
    # Recovery may only act on a fully identified, lineage-closed draft.
    required = ("candidate_id", "content_hash", "source_kind")
    lineage_closed = bool(draft.structured_items) and all(
        isinstance(item, Mapping) and all(str(item.get(key) or "").strip() for key in required)
        for item in draft.structured_items
    )
    if not lineage_closed:
        _fail("lineage_evidence_incomplete", "冻结草稿缺少完整题目谱系证据")
    return {"source": "past", "native_publish_version": publication_version, "request_hash": request_hash, "final_exam_hash": draft.final_exam_hash, "unscored": draft.scoring.mode == "unscored", "reservation_worker_id": worker_id, "reservation_attempt": lease_attempt, "pipeline_identity": PipelineIdentity().as_dict(), "lineage_closed": lineage_closed}


def reserve_native_paper(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    request_hash: str, draft: NativeV3PaperDraft, subject_id: int,
    created_at: str, publication_version: str,
    request_guard: Callable[[Any], None],
) -> int:
    """Reserve one native exam row behind a caller-owned immutable request."""

    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        request_guard(db)
        rows = db.execute("SELECT id,config,artifact_status FROM exams WHERE generation_job_id=?", (job_id,)).fetchall()
        if rows:
            if len(rows) != 1 or rows[0]["artifact_status"] != "publishing":
                _fail("reserve_exam_conflict", "任务已有非预期试卷预留")
            try:
                config = json.loads(rows[0]["config"])
            except (TypeError, ValueError) as exc:
                raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "reserve_config_invalid", "试卷预留身份损坏") from exc
            if config.get("request_hash") != request_hash or config.get("native_publish_version") != publication_version:
                _fail("reserve_request_drift", "试卷预留未绑定当前冻结请求")
            if config.get("reservation_worker_id") != worker_id or config.get("reservation_attempt") != lease_attempt:
                replacement = _reservation_config(request_hash=request_hash, draft=draft, worker_id=worker_id, lease_attempt=lease_attempt, publication_version=publication_version)
                updated = db.execute("UPDATE exams SET config=? WHERE id=? AND generation_job_id=? AND artifact_status='publishing' AND config=?", (_canonical(replacement), int(rows[0]["id"]), job_id, rows[0]["config"]))
                if updated.rowcount != 1:
                    _fail("reservation_takeover_cas_lost", "publishing reservation 接管 CAS 丢失", retryable=True)
            return int(rows[0]["id"])
        config = _reservation_config(request_hash=request_hash, draft=draft, worker_id=worker_id, lease_attempt=lease_attempt, publication_version=publication_version)
        db.execute("INSERT INTO exams(subject_id,title,config,content,created_at,generation_job_id,batch_index,artifact_status,latex_source,latex_sha256,pdf_sha256,artifact_revision,recompile_available,scoring_mode,total_points,scoring_evidence_hash,scoring_projection_hash) VALUES(?,?,?,?,?,?,1,'publishing',?,?,NULL,1,0,?,?,?,?)", (subject_id, draft.title, _canonical(config), draft.body, created_at, job_id, draft.latex_source, _raw_digest(draft.latex_source), draft.scoring.mode, draft.scoring.total_points, draft.scoring.evidence_hash, draft.scoring.projection_hash))
        return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


def _reserve_exam(db, *, job_id: str, worker_id: str, lease_attempt: int, request_hash: str, draft: NativeV3PaperDraft, subject_id: int, created_at: str) -> int:
    def guard(connection) -> None:
        head = connection.execute(f"SELECT state,request_hash FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
        if head is None or head["state"] != "request_frozen" or head["request_hash"] != request_hash:
            _fail("reserve_head_conflict", "原生发布预留与冻结请求 head 冲突")
    return reserve_native_paper(
        db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
        request_hash=request_hash, draft=draft, subject_id=subject_id,
        created_at=created_at, publication_version=PUBLICATION_VERSION,
        request_guard=guard,
    )


def _attempt_paths(artifact_dir: str, exam_id: int, worker_id: str, lease_attempt: int) -> tuple[Path, Path]:
    token = hashlib.sha256(worker_id.encode()).hexdigest()[:16]
    root = Path(artifact_dir)
    return root / f".exam_{exam_id}.{token}.{lease_attempt}.tex.tmp", root / f".exam_{exam_id}.{token}.{lease_attempt}.pdf.tmp"


def _artifact(callback: Callable[[Mapping[str, Any]], Any], payload: Mapping[str, Any], request_hash: str, *, exam_id: int, artifact_dir: str, worker_id: str, lease_attempt: int) -> tuple[dict[str, Any], str]:
    root = Path(artifact_dir)
    root.mkdir(parents=True, exist_ok=True)
    tex_path, pdf_path = _attempt_paths(artifact_dir, exam_id, worker_id, lease_attempt)
    latex_source = str(payload["draft"]["latex_source"])
    with tex_path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(latex_source)
    value = callback({**dict(payload), "request_hash": request_hash, "exam_id": exam_id, "tex_path": str(tex_path), "pdf_path": str(pdf_path), "latex_source": latex_source})
    if value is not None:
        if not isinstance(value, Mapping) or set(value) != {"version", "request_hash", "exam_id"} or value.get("version") != ARTIFACT_VERSION or value.get("request_hash") != request_hash or value.get("exam_id") != exam_id:
            _fail("artifact_result_invalid", "artifact 回调结果未绑定冻结请求与预留试卷")
    if not tex_path.is_file() or tex_path.read_text(encoding="utf-8") != latex_source:
        _fail("artifact_latex_hash_mismatch", "磁盘 LaTeX 产物未绑定冻结源")
    if not pdf_path.is_file() or pdf_path.stat().st_size < 1:
        _fail("artifact_pdf_missing", "artifact 回调未生成可读 PDF")
    normalized = {"version": ARTIFACT_VERSION, "request_hash": request_hash, "exam_id": exam_id, "latex_sha256": hashlib.sha256(tex_path.read_bytes()).hexdigest(), "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest()}
    return normalized, _digest(normalized)


def _cleanup_staging(db, *, job_id: str, request_hash: str, exam_id: int, artifact_dir: str, worker_id: str, lease_attempt: int) -> None:
    try:
        if db.in_transaction:
            db.rollback()
    except sqlite3.DatabaseError:
        pass
    may_delete_shared = False
    try:
        with immediate_transaction(db):
            row = db.execute("SELECT config,artifact_status FROM exams WHERE id=? AND generation_job_id=?", (exam_id, job_id)).fetchone()
            if row is not None and row["artifact_status"] == "publishing":
                try:
                    config = json.loads(row["config"])
                    bound = config.get("request_hash") == request_hash and config.get("reservation_worker_id") == worker_id and config.get("reservation_attempt") == lease_attempt
                except (TypeError, ValueError):
                    bound = False
                job = db.execute("SELECT lease_owner,attempts FROM background_jobs WHERE id=?", (job_id,)).fetchone()
                current_identity = job is not None and job["lease_owner"] == worker_id and int(job["attempts"] or 0) == lease_attempt
                if bound and current_identity:
                    db.execute("DELETE FROM exams WHERE id=? AND artifact_status='publishing'", (exam_id,))
                    may_delete_shared = True
    finally:
        temporary = _attempt_paths(artifact_dir, exam_id, worker_id, lease_attempt)
        shared = (Path(artifact_dir) / f"exam_{exam_id}.tex", Path(artifact_dir) / f"exam_{exam_id}.pdf") if may_delete_shared else ()
        for path in (*temporary, *shared):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _insert_exam_item(db, *, exam_id: int, job_id: str, item: Mapping[str, Any], scoring: ExamScoringProjection, created_at: str, publication_version: str = PUBLICATION_VERSION) -> None:
    position = int(item["position"])
    points_text, points_evidence = scoring.item(position)
    metadata = {"native_publish_version": publication_version, "generation_job_id": job_id, "adapted": bool(item["adapted"])}
    metadata["chapter_snapshot"] = native_frozen_chapter_snapshot(
        db, exam_id=exam_id, job_id=job_id,
        candidate_id=str(item["candidate_id"]),
    )
    if isinstance(item.get("review_marker"), Mapping):
        metadata["review_marker"] = dict(item["review_marker"])
    public_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"past-v3:{job_id}:{position}:{item['candidate_id']}"))
    db.execute("INSERT INTO exam_items(public_id,exam_id,position,question_type,difficulty,content,content_hash,points,points_canonical,points_evidence_hash,source_kind,source_mistake_public_id,source_candidate_id,source_metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (public_id, exam_id, position, item["question_type"], item["difficulty"], item["content"], _raw_digest(item["content"]), float(Decimal(points_text)) if points_text is not None else None, points_text, points_evidence, item["source_kind"], None, item["candidate_id"], _canonical(metadata), created_at))


def commit_native_paper_publication(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    request_hash: str, exam_id: int, artifact_dir: str,
    draft: NativeV3PaperDraft, artifact: Mapping[str, Any],
    publication_version: str,
    authority_guard: Callable[[Any], None],
    authority_commit: Callable[[Any, str], str],
) -> tuple[str, str]:
    """Atomically publish a reserved native draft using caller-owned facts.

    The caller owns its versioned request/publication records.  This primitive
    owns the shared fence, artifact replacement, exam rows and parent job CAS.
    """

    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        published_at = _timestamp(None)
        authority_guard(db)
        reserved = db.execute(
            "SELECT artifact_status,config,latex_sha256 FROM exams WHERE id=? AND generation_job_id=?",
            (exam_id, job_id),
        ).fetchone()
        config = json.loads(reserved["config"]) if reserved is not None else {}
        if (
            reserved is None
            or reserved["artifact_status"] != "publishing"
            or config.get("request_hash") != request_hash
            or config.get("native_publish_version") != publication_version
            or config.get("reservation_worker_id") != worker_id
            or config.get("reservation_attempt") != lease_attempt
            or reserved["latex_sha256"] != artifact["latex_sha256"]
        ):
            _fail("publishing_exam_identity_conflict", "预留试卷身份或 LaTeX 哈希冲突")
        temp_tex, temp_pdf = _attempt_paths(artifact_dir, exam_id, worker_id, lease_attempt)
        final_tex = Path(artifact_dir) / f"exam_{exam_id}.tex"
        final_pdf = Path(artifact_dir) / f"exam_{exam_id}.pdf"
        if (
            not temp_tex.is_file()
            or not temp_pdf.is_file()
            or hashlib.sha256(temp_tex.read_bytes()).hexdigest() != artifact["latex_sha256"]
            or hashlib.sha256(temp_pdf.read_bytes()).hexdigest() != artifact["pdf_sha256"]
        ):
            _fail("staged_artifact_hash_drift", "attempt 临时产物在发布前漂移")
        temp_tex.replace(final_tex)
        temp_pdf.replace(final_pdf)
        for item in draft.structured_items:
            _insert_exam_item(
                db, exam_id=exam_id, job_id=job_id, item=item,
                scoring=draft.scoring, created_at=published_at,
                publication_version=publication_version,
            )
        updated_exam = db.execute(
            "UPDATE exams SET artifact_status='published',pdf_sha256=?,recompile_available=1 "
            "WHERE id=? AND generation_job_id=? AND artifact_status='publishing'",
            (artifact["pdf_sha256"], exam_id, job_id),
        )
        if updated_exam.rowcount != 1:
            _fail("publishing_exam_cas_lost", "预留试卷发布 CAS 丢失", retryable=True)
        publication_hash = authority_commit(db, published_at)
        _hash(publication_hash, "publication_hash_invalid")
        pending = {
            "state": "published_pending_receipt", "exam_id": exam_id,
            "publication_hash": publication_hash, "request_hash": request_hash,
        }
        pending_json = _canonical(pending)
        db.execute("INSERT INTO job_exam_results(job_id,exam_id,position) VALUES(?,?,1)", (job_id, exam_id))
        db.execute("INSERT INTO job_business_results(job_id,kind,result_json,committed_at) VALUES(?,'exam-generation',?,?)", (job_id, pending_json, published_at))
        job_columns = _job_columns(db)
        assignments = ["status='published_pending_receipt'"]
        parameters: list[Any] = []
        if "result_json" in job_columns:
            assignments.append("result_json=?")
            parameters.append(pending_json)
        if "updated_at" in job_columns:
            assignments.append("updated_at=?")
            parameters.append(published_at)
        where = "id=? AND kind='exam-generation' AND status='running' AND cancel_requested=0 AND lease_owner=? AND attempts=?"
        parameters.extend((job_id, worker_id, lease_attempt))
        if "lease_expires_at" in job_columns:
            where += " AND (lease_expires_at IS NULL OR julianday(lease_expires_at)>julianday(?))"
            parameters.append(published_at)
        if "deadline_policy_mode" in job_columns:
            where += " AND (deadline_policy_mode IS NULL OR deadline_policy_mode!='enforce' OR (deadline_at IS NOT NULL AND julianday(deadline_at)>julianday(?)))"
            parameters.append(published_at)
        parent = db.execute(
            f"UPDATE background_jobs SET {','.join(assignments)} WHERE {where}", tuple(parameters)
        )
        if parent.rowcount != 1:
            raise JobLeaseLost("原生发布父状态 CAS 丢失")
    return publication_hash, published_at


def _publish(db, *, job_id: str, worker_id: str, lease_attempt: int, request_hash: str, exam_id: int, artifact_dir: str, draft: NativeV3PaperDraft, artifact: Mapping[str, Any], artifact_hash: str, created_at: str) -> NativeV3PublishOutcome:
    def guard(connection) -> None:
        head = connection.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
        if head is None or head["request_hash"] != request_hash:
            _fail("publish_head_missing", "冻结请求 head 缺失")
        existing = connection.execute(f"SELECT * FROM {PUBLICATIONS} WHERE job_id=?", (job_id,)).fetchone()
        if existing is not None:
            _fail("publication_conflict", "已有原生发布事实冲突")
        if head["state"] != "request_frozen" or int(head["revision"]) != 1:
            _fail("publication_slot_conflict", "原生发布槽位已被占用")

    def commit(connection, published_at: str) -> str:
        publication_payload = {"version": PUBLICATION_VERSION, "job_id": job_id, "request_hash": request_hash, "exam_id": exam_id, "artifact_hash": artifact_hash, "final_exam_hash": draft.final_exam_hash, "item_count": len(draft.structured_items)}
        publication_hash = _digest(publication_payload)
        connection.execute(f"INSERT INTO {PUBLICATIONS}(job_id,request_hash,publication_version,exam_id,artifact_hash,publication_hash,payload_json,published_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, request_hash, PUBLICATION_VERSION, exam_id, artifact_hash, publication_hash, _canonical(publication_payload), published_at))
        for item in draft.structured_items:
            usage_payload = {"version": USAGE_VERSION, "job_id": job_id, "exam_id": exam_id, "position": item["position"], "candidate_id": item["candidate_id"], "content_hash": item["content_hash"], "publication_hash": publication_hash}
            usage_hash = _digest(usage_payload)
            connection.execute(f"INSERT INTO {USAGE}(job_id,exam_id,position,candidate_id,content_hash,usage_hash,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)", (job_id, exam_id, item["position"], item["candidate_id"], item["content_hash"], usage_hash, _canonical(usage_payload), published_at))
        updated = connection.execute(f"UPDATE {HEADS} SET publication_hash=?,state='published_pending_receipt',revision=revision+1,updated_at=? WHERE job_id=? AND request_hash=? AND state='request_frozen' AND revision=1", (publication_hash, published_at, job_id, request_hash))
        if updated.rowcount != 1:
            _fail("publish_head_cas_lost", "原生发布 head CAS 丢失", retryable=True)
        return publication_hash

    publication_hash, _published_at = commit_native_paper_publication(
        db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
        request_hash=request_hash, exam_id=exam_id, artifact_dir=artifact_dir,
        draft=draft, artifact=artifact, publication_version=PUBLICATION_VERSION,
        authority_guard=guard, authority_commit=commit,
    )
    return NativeV3PublishOutcome(job_id, "published_pending_receipt", request_hash, exam_id, publication_hash, None)


def _complete_receipt(db, *, job_id: str, worker_id: str, lease_attempt: int, receipt_callback: Callable[[Mapping[str, Any]], Any] | None, created_at: str, fault_injector: Callable[[str], None] | None = None) -> NativeV3PublishOutcome:
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt, side_effect_committed=True)
        row = db.execute(f"SELECT h.*,p.exam_id,p.payload_json FROM {HEADS} h JOIN {PUBLICATIONS} p ON p.job_id=h.job_id AND p.publication_hash=h.publication_hash WHERE h.job_id=?", (job_id,)).fetchone()
    if row is None:
        _fail("published_fact_missing", "待恢复的原生发布事实缺失")
    try:
        publication_payload = json.loads(row["payload_json"])
    except (TypeError, ValueError) as exc:
        raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "publication_payload_invalid", "原生发布事实 JSON 损坏") from exc
    if (_digest(publication_payload) != row["publication_hash"] or publication_payload.get("job_id") != job_id
            or publication_payload.get("request_hash") != row["request_hash"] or publication_payload.get("exam_id") != int(row["exam_id"])):
        _fail("publication_fact_drift", "原生发布事实哈希或身份漂移")
    exam = db.execute("SELECT generation_job_id,artifact_status FROM exams WHERE id=?", (int(row["exam_id"]),)).fetchone()
    job_exam_count = int(db.execute("SELECT COUNT(*) FROM exams WHERE generation_job_id=?", (job_id,)).fetchone()[0])
    job_exam = db.execute("SELECT exam_id,position FROM job_exam_results WHERE job_id=?", (job_id,)).fetchall()
    if exam is None or job_exam_count != 1 or exam["generation_job_id"] != job_id or exam["artifact_status"] != "published" or len(job_exam) != 1 or tuple(job_exam[0]) != (int(row["exam_id"]), 1):
        _fail("published_exam_mapping_drift", "原生发布试卷或 job 结果映射漂移")
    if row["state"] == "completed":
        receipt = db.execute(f"SELECT receipt_hash,payload_json FROM {RECEIPTS} WHERE job_id=? AND publication_hash=?", (job_id, row["publication_hash"])).fetchone()
        if receipt is None:
            _fail("completed_receipt_missing", "已完成发布缺少不可变回执")
        try:
            receipt_payload = json.loads(receipt["payload_json"])
        except (TypeError, ValueError) as exc:
            raise ExamGenerationV3PublishError("exam_v3_publish_conflict", "receipt_payload_invalid", "原生发布回执 JSON 损坏") from exc
        if receipt["receipt_hash"] != row["receipt_hash"] or _digest(receipt_payload) != row["receipt_hash"]:
            _fail("completed_receipt_drift", "已完成发布回执哈希漂移")
        return NativeV3PublishOutcome(job_id, "completed", row["request_hash"], int(row["exam_id"]), row["publication_hash"], row["receipt_hash"])
    if row["state"] != "published_pending_receipt":
        _fail("receipt_state_conflict", "原生发布回执状态矛盾")
    if db.execute(f"SELECT 1 FROM {RECEIPTS} WHERE job_id=?", (job_id,)).fetchone() is not None:
        _fail("pending_state_has_receipt", "待回执状态已存在终态回执，状态矛盾")
    body = {"version": RECEIPT_VERSION, "job_id": job_id, "publication_hash": row["publication_hash"], "request_hash": row["request_hash"], "exam_id": int(row["exam_id"]), "status": "published"}

    def commit(connection, receipt_hash: str, committed_at: str) -> None:
        current = connection.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
        if current is None or current["request_hash"] != row["request_hash"] or current["publication_hash"] != row["publication_hash"]:
            _fail("receipt_head_drift", "回执写入前发布 head 漂移")
        existing = connection.execute(f"SELECT * FROM {RECEIPTS} WHERE job_id=?", (job_id,)).fetchone()
        if existing is not None:
            if existing["receipt_hash"] != receipt_hash or json.loads(existing["payload_json"]) != body:
                _fail("receipt_fact_conflict", "已有发布回执事实冲突")
        else:
            connection.execute(f"INSERT INTO {RECEIPTS}(job_id,publication_hash,receipt_version,receipt_hash,payload_json,created_at) VALUES(?,?,?,?,?,?)", (job_id, row["publication_hash"], RECEIPT_VERSION, receipt_hash, _canonical(body), committed_at))
        updated = connection.execute(f"UPDATE {HEADS} SET receipt_hash=?,state='completed',revision=revision+1,updated_at=? WHERE job_id=? AND state='published_pending_receipt' AND revision=? AND publication_hash=?", (receipt_hash, committed_at, job_id, int(current["revision"]), row["publication_hash"]))
        if updated.rowcount != 1:
            _fail("receipt_head_cas_lost", "发布回执 head CAS 丢失", retryable=True)

    receipt_hash = complete_native_publication_receipt(
        db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
        receipt_body=body, receipt_callback=receipt_callback,
        receipt_commit=commit, created_at=created_at,
        fault_injector=fault_injector,
    )
    return NativeV3PublishOutcome(job_id, "completed", row["request_hash"], int(row["exam_id"]), row["publication_hash"], receipt_hash)


def complete_native_publication_receipt(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    receipt_body: Mapping[str, Any],
    receipt_callback: Callable[[Mapping[str, Any]], Any] | None,
    receipt_commit: Callable[[Any, str, str], None], created_at: str,
    fault_injector: Callable[[str], None] | None = None,
) -> str:
    """Complete one caller-owned native receipt behind the shared fence."""

    try:
        if receipt_callback is None:
            raise _ReceiptDeferred("authoritative publication receipt callback is unavailable")
        callback_result = receipt_callback(dict(receipt_body))
        if isinstance(callback_result, Mapping) and callback_result.get("publication_hash") not in {None, receipt_body.get("publication_hash")}:
            _fail("receipt_callback_publication_mismatch", "回执回调返回了不同的发布身份")
        if fault_injector is not None:
            fault_injector("receipt")
    except _ReceiptDeferred:
        raise
    except ExamGenerationV3PublishError:
        raise
    except Exception as exc:
        raise _ReceiptDeferred("publication receipt is pending") from exc
    receipt_hash = _digest(receipt_body)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt, side_effect_committed=True)
        receipt_commit(db, receipt_hash, created_at)
    return receipt_hash


def publish_v3_single_paper(db, *, job_id: str, worker_id: str, lease_attempt: int, subject_id: int, title: str, stage7_checkpoint: CheckpointV3, stage7_result: Stage7Result, stage3_contract: PastV3GenerationContract, artifact_dir: str, artifact_callback: Callable[[Mapping[str, Any]], Any], receipt_callback: Callable[[Mapping[str, Any]], Any] | None = None, created_at: str | None = None, fault_injector: Callable[[str], None] | None = None) -> NativeV3PublishOutcome:
    """Freeze, artifact, atomically publish, then persist the receipt."""

    if db.in_transaction:
        _fail("nested_publish_transaction_forbidden", "原生发布不能嵌套写事务")
    if not isinstance(job_id, str) or not job_id or not isinstance(worker_id, str) or not worker_id or type(lease_attempt) is not int or lease_attempt < 1 or type(subject_id) is not int or subject_id <= 0:
        _fail("publisher_actor_invalid", "原生发布 actor 或科目无效")
    timestamp = _timestamp(created_at)
    # Once publication exists, this API is a receipt-only recovery entry.
    # In particular it must not re-run authority loading, assembly or PDF.
    current = db.execute(f"SELECT h.*,p.exam_id FROM {HEADS} h LEFT JOIN {PUBLICATIONS} p ON p.job_id=h.job_id WHERE h.job_id=?", (job_id,)).fetchone()
    if current is not None and current["state"] in {"published_pending_receipt", "completed"}:
        try:
            return _complete_receipt(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, receipt_callback=receipt_callback, created_at=timestamp, fault_injector=fault_injector)
        except _ReceiptDeferred:
            return NativeV3PublishOutcome(
                job_id, "published_pending_receipt", str(current["request_hash"]),
                int(current["exam_id"]), str(current["publication_hash"]), None,
            )
    draft = assemble_v3_single_paper(stage7_checkpoint=stage7_checkpoint, stage7_result=stage7_result, stage3_contract=stage3_contract, title=title)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        result_hash, binding_hash = _verify_authority(db, job_id=job_id, stage7_checkpoint=stage7_checkpoint, stage7_result=stage7_result, stage3_contract=stage3_contract)
    payload = _request_payload(job_id, subject_id, draft, stage3_contract, result_hash, stage7_checkpoint.checkpoint_hash, binding_hash)
    request_hash = _freeze_request(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, payload=payload, created_at=timestamp)
    if fault_injector is not None:
        fault_injector("request")
    exam_id = _reserve_exam(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, request_hash=request_hash, draft=draft, subject_id=subject_id, created_at=timestamp)
    try:
        with immediate_transaction(db):
            _fence(db, job_id, worker_id, lease_attempt)
        artifact, artifact_hash = _artifact(artifact_callback, payload, request_hash, exam_id=exam_id, artifact_dir=artifact_dir, worker_id=worker_id, lease_attempt=lease_attempt)
        if fault_injector is not None:
            fault_injector("publish")
        outcome = _publish(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, request_hash=request_hash, exam_id=exam_id, artifact_dir=artifact_dir, draft=draft, artifact=artifact, artifact_hash=artifact_hash, created_at=timestamp)
    except BaseException:
        _cleanup_staging(db, job_id=job_id, request_hash=request_hash, exam_id=exam_id, artifact_dir=artifact_dir, worker_id=worker_id, lease_attempt=lease_attempt)
        raise
    try:
        return _complete_receipt(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, receipt_callback=receipt_callback, created_at=timestamp, fault_injector=fault_injector)
    except _ReceiptDeferred:
        return NativeV3PublishOutcome(job_id, "published_pending_receipt", request_hash, outcome.exam_id, outcome.publication_hash, None)


def recover_v3_publication_receipt(db, *, job_id: str, worker_id: str, lease_attempt: int, receipt_callback: Callable[[Mapping[str, Any]], Any] | None = None, created_at: str | None = None, fault_injector: Callable[[str], None] | None = None) -> NativeV3PublishOutcome:
    """Recover only a committed publication receipt; never assemble or publish."""

    if db.in_transaction:
        _fail("nested_recovery_transaction_forbidden", "原生发布恢复不能嵌套事务")
    head = db.execute(f"SELECT state FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()
    if head is None or head["state"] == "request_frozen":
        _fail("publication_not_committed", "恢复只接受已提交但缺回执的发布")
    try:
        outcome = _complete_receipt(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, receipt_callback=receipt_callback, created_at=_timestamp(created_at), fault_injector=fault_injector)
    except _ReceiptDeferred:
        row = db.execute(f"SELECT h.request_hash,h.publication_hash,p.exam_id FROM {HEADS} h JOIN {PUBLICATIONS} p ON p.job_id=h.job_id WHERE h.job_id=? AND h.state='published_pending_receipt'", (job_id,)).fetchone()
        if row is None:
            _fail("pending_recovery_state_drift", "回执恢复失败后待恢复状态漂移")
        return NativeV3PublishOutcome(job_id, "published_pending_receipt", row["request_hash"], int(row["exam_id"]), row["publication_hash"], None, True)
    return NativeV3PublishOutcome(outcome.job_id, outcome.state, outcome.request_hash, outcome.exam_id, outcome.publication_hash, outcome.receipt_hash, True)


# Explicit aliases make the phase boundary discoverable without preserving any
# legacy publisher API surface.
assemble_native_v3_exam = assemble_v3_single_paper
publish_native_v3_exam = publish_v3_single_paper
recover_native_v3_receipt = recover_v3_publication_receipt

__all__ = ["ARTIFACT_VERSION", "ExamGenerationV3PublishError", "NativeV3PaperDraft", "NativeV3PublishOutcome", "assemble_native_v3_exam", "assemble_v3_single_paper", "commit_native_paper_publication", "complete_native_publication_receipt", "publish_native_v3_exam", "publish_v3_single_paper", "recover_native_v3_receipt", "recover_v3_publication_receipt", "reserve_native_paper"]
