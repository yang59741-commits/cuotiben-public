"""D2 rendering-quality gate and native single-paper publication.

The D2 selection/objective namespace remains authoritative at this boundary.
No legacy score, penalty, Stage7Result, or candidate publication flag is
manufactured.  Publication reuses the native reservation, artifact, fencing,
atomic exam commit, cleanup and receipt lifecycle from the v3 publisher.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Sequence

from ..transactions import immediate_transaction
from .exam_generation_stage4_candidate_facts import (
    CANDIDATE_FACTS_CONTRACT_VERSION,
    FrozenCandidateFacts,
)
from .exam_generation_stage6_d2_repository import D2CandidateState
from .exam_generation_stage7_quality import (
    QUALITY_PROTOCOL_VERSION,
    STAGE7_VERSION,
    formal_original_quality_policy,
)
from .exam_paper_layout import D2_DOCUMENT_PACKAGES, question_display_content, render_question_content
from .exam_time_policy import ExamDeadlineExceeded
from .exam_generation_v3_publish import (
    ARTIFACT_VERSION,
    ExamGenerationV3PublishError,
    NativeV3PaperDraft,
    NativeV3PublishOutcome,
    _ReceiptDeferred,
    _artifact,
    _cleanup_staging,
    _escape_latex,
    _fence,
    _stage3_scoring,
    _timestamp,
    _validate_stage3_contract,
    commit_native_paper_publication,
    complete_native_publication_receipt,
    reserve_native_paper,
)
from .semantic_index import SemanticIndexContractError, semantic_content_hash
from .job_contracts import JobCancelled, JobLeaseLost
from .exam_generation_memory import question_fingerprint
from .question_review_markers import marker_for
from .exam_generation_memory_delivery import (
    GenerationMemoryDeliveryError,
    dispatch_generation_memory_delivery,
    recover_generation_memory_delivery,
    stage_generation_memory_delivery,
)
from .exam_generation_success import SuccessMemoryContext, build_success_memory_events
from .tex import TexCompileError, UnsafeLatexError, validate_latex_body


D2_PUBLICATION_VERSION = "past-v3-d2-native-publication-v1"
D2_STATE_VERSION = "past-v3-d2-quality-publication-state-v1"
D2_REQUEST_VERSION = "past-v3-d2-native-publish-request-v1"
D2_RECEIPT_VERSION = "past-v3-d2-native-publication-receipt-v1"
D2_USAGE_VERSION = "past-v3-d2-native-candidate-usage-v1"
D2_TABLE = "exam_generation_d2_quality_publications"
_HASH = re.compile(r"^[0-9a-f]{64}$")
_QUALITY_CODES = frozenset({"none", "rendering_risk"})
_FORBIDDEN = frozenset(
    {"score", "penalty", "score_identity_hash", "rubric_receipt_hash", "utility_receipt_hash"}
)


SCHEMA_STATEMENTS = (
    f"""CREATE TABLE IF NOT EXISTS {D2_TABLE}(
        job_id TEXT NOT NULL PRIMARY KEY,
        state_version TEXT NOT NULL CHECK(state_version='{D2_STATE_VERSION}'),
        state TEXT NOT NULL CHECK(state IN(
            'quality_pending','quality_passed','request_frozen',
            'published_pending_receipt','completed','failed'
        )),
        selection_hash TEXT NOT NULL CHECK(length(selection_hash)=64),
        objective_hash TEXT NOT NULL CHECK(length(objective_hash)=64),
        candidate_facts_hash TEXT NOT NULL CHECK(length(candidate_facts_hash)=64),
        stage3_contract_hash TEXT NOT NULL CHECK(length(stage3_contract_hash)=64),
        authority_hash TEXT NOT NULL CHECK(length(authority_hash)=64),
        authority_json TEXT NOT NULL,
        quality_request_hash TEXT NOT NULL CHECK(length(quality_request_hash)=64),
        quality_request_json TEXT NOT NULL,
        quality_response_hash TEXT,
        quality_response_json TEXT,
        draft_hash TEXT,
        request_hash TEXT,
        request_json TEXT,
        artifact_hash TEXT,
        exam_id INTEGER,
        publication_hash TEXT,
        publication_json TEXT,
        receipt_hash TEXT,
        failure_reason TEXT,
        revision INTEGER NOT NULL CHECK(revision>0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON UPDATE RESTRICT ON DELETE CASCADE,
        FOREIGN KEY(exam_id) REFERENCES exams(id) ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
)


class D2PublicationError(ExamGenerationV3PublishError):
    def __init__(self, reason: str, message: str, *, retryable: bool = False) -> None:
        super().__init__("exam_d2_publication_failed", reason, message, retryable=retryable)
        self.status_code = 502 if reason.startswith("quality_provider_") or reason == "quality_callback_failed" else 422


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise D2PublicationError("non_canonical_json", "D2 发布数据不能规范序列化") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hex(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise D2PublicationError(f"{field}_invalid", "D2 发布权威哈希无效")
    return value


def _reject_legacy(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in _FORBIDDEN:
                raise D2PublicationError("legacy_authority_forbidden", "D2 发布不得转换为旧评分权威")
            _reject_legacy(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _reject_legacy(child)


def _snapshot(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError) as exc:
        raise D2PublicationError("content_snapshot_invalid", "冻结题面快照无效") from exc
    expected = {"schema_version", "content", "figure_references", "options", "conditions"}
    if not isinstance(value, Mapping) or set(value) != expected or value.get("schema_version") != "semantic-candidate-content-snapshot-v1":
        raise D2PublicationError("content_snapshot_invalid", "冻结题面快照结构无效")
    content = value.get("content")
    if not isinstance(content, str) or not content.strip():
        raise D2PublicationError("content_snapshot_invalid", "冻结题面正文为空")
    result = {"schema_version": value["schema_version"], "content": content.strip()}
    for field in ("figure_references", "options", "conditions"):
        rows = value.get(field)
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence) or len(rows) > 500 or any(not isinstance(item, str) or not item.strip() for item in rows):
            raise D2PublicationError("content_snapshot_invalid", "冻结题面列表字段无效")
        result[field] = [item.strip() for item in rows]
    return result


def _state_authority(
    state: D2CandidateState,
    facts: FrozenCandidateFacts,
    stage3_contract,
    *,
    review_marker_loader: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(state, D2CandidateState):
        raise D2PublicationError("d2_state_type_invalid", "仅接受 D2CandidateState")
    if not isinstance(facts, FrozenCandidateFacts) or facts.contract_version != CANDIDATE_FACTS_CONTRACT_VERSION:
        raise D2PublicationError("candidate_facts_type_invalid", "冻结候选事实合同无效")
    stage3_payload = _validate_stage3_contract(stage3_contract)
    facts_payload = facts.to_dict()
    if _digest(facts_payload) != facts.candidate_facts_hash or tuple(facts_payload.get("candidates", ())) != facts.candidates:
        raise D2PublicationError("candidate_facts_hash_drift", "冻结候选事实发生漂移")
    selection = state.selection.to_dict()
    checkpoint = state.checkpoint.to_dict()
    policy = dict(state.objective_policy)
    factor_set = dict(state.factor_set_receipt)
    for value in (selection, checkpoint, policy, factor_set, state.factor_receipts, state.head):
        _reject_legacy(value)
    selection_hash = _hex(state.selection.selection_hash, "selection_hash")
    objective_hash = _hex(state.selection.objective_hash, "objective_hash")
    if selection.get("selection_hash") != selection_hash or selection.get("objective_hash") != objective_hash:
        raise D2PublicationError("d2_selection_identity_drift", "D2 选择或目标身份漂移")
    bindings = checkpoint.get("bindings")
    expected = {
        "selection_hash": selection_hash,
        "graph_hash": state.base.graph.graph_hash,
        "constraint_hash": state.base.constraint.constraint_hash,
        "candidate_facts_hash": facts.candidate_facts_hash,
        "objective_policy_hash": policy.get("policy_hash"),
        "factor_set_hash": factor_set.get("factor_set_hash"),
    }
    if not isinstance(bindings, Mapping) or any(bindings.get(key) != value for key, value in expected.items()):
        raise D2PublicationError("d2_checkpoint_binding_drift", "D2 checkpoint 未绑定当前选择链")
    if (
        state.head.get("state") != "checkpointed"
        or state.head.get("checkpoint_hash") != state.checkpoint.checkpoint_hash
        or state.head.get("selection_hash") != selection_hash
        or state.head.get("objective_policy_hash") != policy.get("policy_hash")
        or state.head.get("factor_set_hash") != factor_set.get("factor_set_hash")
    ):
        raise D2PublicationError("d2_head_binding_drift", "D2 head 未绑定当前 checkpoint")
    if any(
        factor_set.get(field) != expected_value
        for field, expected_value in (
            ("graph_hash", state.base.graph.graph_hash),
            ("constraint_hash", state.base.constraint.constraint_hash),
            ("candidate_facts_hash", facts.candidate_facts_hash),
            ("policy_hash", policy.get("policy_hash")),
        )
    ):
        raise D2PublicationError("d2_factor_set_binding_drift", "D2 factor set 未绑定当前选择链")
    if any(
        selection.get(field) != expected_value
        for field, expected_value in (
            ("graph_hash", state.base.graph.graph_hash),
            ("constraint_hash", state.base.constraint.constraint_hash),
            ("policy_hash", policy.get("policy_hash")),
        )
    ):
        raise D2PublicationError("d2_selection_binding_drift", "D2 选择未绑定当前图与目标策略")
    if stage3_contract.stage3_contract_hash != state.base.constraint.stage3_contract_hash:
        raise D2PublicationError("stage3_contract_binding_drift", "D2 选择未绑定当前阶段 3 合同")
    if facts.candidate_facts_hash != state.base.constraint.candidate_facts_hash:
        raise D2PublicationError("candidate_facts_binding_drift", "D2 选择未绑定当前候选事实")

    facts_by_id = {row.get("candidate_id"): row for row in facts_payload.get("candidates", ()) if isinstance(row, Mapping)}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, assignment in enumerate(selection.get("assignments", ()), start=1):
        if not isinstance(assignment, Mapping) or assignment.get("position") != position:
            raise D2PublicationError("selection_positions_invalid", "D2 选择位置不连续")
        candidate_id = assignment.get("candidate_id")
        fact = facts_by_id.get(candidate_id)
        if not isinstance(candidate_id, str) or candidate_id in seen or not isinstance(fact, Mapping):
            raise D2PublicationError("selected_candidate_fact_missing", "D2 选择引用的候选事实缺失")
        source_role = "past_exam_other" if fact.get("source_kind") == "past_exam_local" else fact.get("source_kind")
        if assignment.get("source_kind") != source_role or assignment.get("candidate_fact_hash") != _digest(fact) or any(
            assignment.get(field) != fact.get(field)
            for field in ("content_hash", "question_type_family")
        ):
            raise D2PublicationError("selected_candidate_fact_drift", "D2 选择与冻结候选事实不一致")
        snapshot = _snapshot(fact.get("content_snapshot"))
        try:
            replay = semantic_content_hash(
                snapshot["content"], figure_references=tuple(snapshot["figure_references"]),
                options=tuple(snapshot["options"]), conditions=tuple(snapshot["conditions"]),
            )
        except SemanticIndexContractError as exc:
            raise D2PublicationError("selected_content_unreplayable", "冻结题面无法重放") from exc
        if replay != assignment.get("content_hash"):
            raise D2PublicationError("selected_content_hash_drift", "冻结题面内容身份漂移")
        selected_item = {
            "position": position,
            "assignment": dict(assignment),
            "candidate_fact": dict(fact),
            "content_snapshot": snapshot,
        }
        if review_marker_loader is not None:
            marker = review_marker_loader(str(fact["source_kind"]), candidate_id)
            if marker is not None:
                selected_item["review_marker"] = dict(marker)
        selected.append(selected_item)
        seen.add(candidate_id)
    if not selected or len(selected) != stage3_contract.question_count:
        raise D2PublicationError("selected_item_count_mismatch", "D2 选择题数与阶段 3 合同不一致")
    authority = {
        "version": D2_STATE_VERSION,
        "selection": selection,
        "objective": {"objective_hash": objective_hash, "objective_vector": list(state.selection.objective_vector)},
        "checkpoint_hash": state.checkpoint.checkpoint_hash,
        "graph_hash": state.base.graph.graph_hash,
        "constraint_hash": state.base.constraint.constraint_hash,
        "candidate_facts_hash": facts.candidate_facts_hash,
        "objective_policy_hash": policy.get("policy_hash"),
        "factor_set_hash": factor_set.get("factor_set_hash"),
        "stage3_contract_hash": stage3_contract.stage3_contract_hash,
    }
    _reject_legacy(authority)
    return authority, selected


def _quality_request(selection_hash: str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    pre_paper = {"version": STAGE7_VERSION, "items": list(items)}
    return {
        "version": QUALITY_PROTOCOL_VERSION,
        "selection_hash": selection_hash,
        "pre_adaptation_hash": _digest(pre_paper),
        "items": [
            {
                "position": row["position"],
                "candidate_id": row["assignment"]["candidate_id"],
                "source_kind": row["assignment"]["source_kind"],
                "question_type_family": row["assignment"]["question_type_family"],
                "content_snapshot": row["content_snapshot"],
            }
            for row in items
        ],
        "allowed_reason_codes": sorted(_QUALITY_CODES),
        "review_policy": formal_original_quality_policy(),
    }


def _parse_quality(raw: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        value = dict(raw)
    elif isinstance(raw, str) and len(raw) <= 2_000_000:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, child in items:
                if key in result:
                    raise D2PublicationError("quality_response_duplicate_key", "质量响应包含重复 JSON 键")
                result[key] = child
            return result
        try:
            value = json.loads(
                raw,
                object_pairs_hook=pairs,
                parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
            )
        except D2PublicationError:
            raise
        except ValueError as exc:
            raise D2PublicationError("quality_response_invalid_json", "质量响应不是有效 JSON") from exc
    else:
        raise D2PublicationError("quality_response_invalid", "质量响应类型无效")
    if not isinstance(value, Mapping) or set(value) != {"version", "selection_hash", "pre_adaptation_hash", "results"}:
        raise D2PublicationError("quality_response_shape_invalid", "质量响应字段超出声明范围")
    if value.get("version") != QUALITY_PROTOCOL_VERSION or value.get("selection_hash") != request["selection_hash"] or value.get("pre_adaptation_hash") != request["pre_adaptation_hash"]:
        raise D2PublicationError("quality_response_binding_invalid", "质量响应未绑定当前 D2 选择")
    positions = {row["position"] for row in request["items"]}
    rows = value.get("results")
    if not isinstance(rows, list) or len(rows) != len(positions):
        raise D2PublicationError("quality_response_count_invalid", "质量响应题数不一致")
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"position", "passed", "reason_codes"}:
            raise D2PublicationError("quality_response_item_invalid", "质量响应项目字段无效")
        position, passed, codes = row.get("position"), row.get("passed"), row.get("reason_codes")
        if type(position) is not int or position not in positions or position in seen or type(passed) is not bool or not isinstance(codes, list) or not codes or len(codes) != len(set(codes)) or any(code not in _QUALITY_CODES for code in codes):
            raise D2PublicationError("quality_response_item_invalid", "质量响应项目超出声明范围")
        if (passed and codes != ["none"]) or (not passed and "none" in codes):
            raise D2PublicationError("quality_response_codes_invalid", "质量响应结论与原因码矛盾")
        normalized.append({"position": position, "passed": passed, "reason_codes": list(codes)})
        seen.add(position)
    if seen != positions:
        raise D2PublicationError("quality_response_positions_invalid", "质量响应位置不完整")
    return {"version": value["version"], "selection_hash": value["selection_hash"], "pre_adaptation_hash": value["pre_adaptation_hash"], "results": sorted(normalized, key=lambda row: row["position"])}


def _draft(title: str, items: Sequence[Mapping[str, Any]], stage3_contract, quality_request: Mapping[str, Any]) -> NativeV3PaperDraft:
    if not isinstance(title, str) or not title.strip():
        raise D2PublicationError("paper_title_invalid", "试卷标题不能为空")
    scoring = _stage3_scoring(stage3_contract, len(items))
    structured: list[dict[str, Any]] = []
    plain: list[str] = []
    latex: list[str] = []
    for row in items:
        position, assignment, snapshot = row["position"], row["assignment"], row["content_snapshot"]
        content = validate_latex_body(snapshot["content"], label=f"第 {position} 题正文")
        conditions = [validate_latex_body(value, label=f"第 {position} 题条件") for value in snapshot["conditions"]]
        options = [validate_latex_body(value, label=f"第 {position} 题选项") for value in snapshot["options"]]
        display_content = question_display_content(content, candidate_id=assignment['candidate_id'])
        lines = [f"{position}. {display_content}"]
        marker = row.get("review_marker")
        if isinstance(marker, Mapping):
            lines.append(str(marker.get("label") or "题面待核对"))
        lines.extend(f"条件：{value}" for value in snapshot["conditions"])
        lines.extend(f"图示：{value}" for value in snapshot["figure_references"])
        lines.extend(f"{chr(65 + index)}. {value}" for index, value in enumerate(snapshot["options"]))
        plain.append("\n".join(lines))
        blocks = [content]
        if isinstance(marker, Mapping):
            label = _escape_latex(str(marker.get("label") or "题面待核对"))
            blocks.append(f"\\par\\textbf{{{label}}}")
        blocks.extend(f"\\par\\textbf{{条件：}} {value}" for value in conditions)
        blocks.extend(f"\\par\\textit{{图示：}} {_escape_latex(value)}" for value in snapshot["figure_references"])
        if options:
            blocks.append("\\begin{enumerate}\n" + "\n".join(f"\\item {value}" for value in options) + "\n\\end{enumerate}")
        latex.append(
            "\\item "
            + render_question_content(
                "\n".join(blocks), candidate_id=assignment["candidate_id"]
            )
        )
        structured.append({
            "position": position, "candidate_id": assignment["candidate_id"],
            "content_hash": assignment["content_hash"], "content": content,
            "question_type": assignment["question_type_family"],
            "difficulty": assignment["target_difficulty"],
            "source_kind": row["candidate_fact"]["source_kind"], "adapted": False,
        })
        if isinstance(marker, Mapping):
            structured[-1]["review_marker"] = dict(marker)
    final_exam_hash = _digest({"version": D2_PUBLICATION_VERSION, "selection_hash": quality_request["selection_hash"], "pre_adaptation_hash": quality_request["pre_adaptation_hash"], "items": list(items)})
    latex_source = "\\documentclass[UTF8]{ctexart}\n" + D2_DOCUMENT_PACKAGES + "\n\\begin{document}\n\\section*{" + _escape_latex(title.strip()) + "}\n\\begin{enumerate}\n" + "\n".join(latex) + "\n\\end{enumerate}\n\\end{document}\n"
    provisional = NativeV3PaperDraft(title.strip(), "\n\n".join(plain), latex_source, tuple(structured), scoring, final_exam_hash, "0" * 64)
    return NativeV3PaperDraft(provisional.title, provisional.body, provisional.latex_source, provisional.structured_items, scoring, final_exam_hash, _digest(provisional.to_dict()))


def _lifecycle(callback: Callable[[Mapping[str, Any]], Any] | None, stage: str, job_id: str) -> None:
    if callback is not None and callback({"stage": stage, "job_id": job_id}) is False:
        raise D2PublicationError("lifecycle_guard_rejected", "任务生命周期门禁拒绝继续")


def _is_lifecycle_interrupt(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            JobCancelled,
            JobLeaseLost,
            ExamDeadlineExceeded,
            asyncio.CancelledError,
            concurrent.futures.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ),
    )


def _deliver_memory_before_receipt(
    db,
    *,
    job_id: str,
    memory: Any,
    logger: Any = None,
) -> bool:
    """Deliver a staged D2 envelope, while leaving old no-outbox history alone."""

    try:
        delivery = recover_generation_memory_delivery(db, operation_id=job_id)
    except GenerationMemoryDeliveryError as exc:
        if exc.reason == "memory_delivery_missing":
            return True
        raise
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return True
        raise
    if delivery.state == "delivered":
        return True
    if memory is None:
        return False
    try:
        outcome = dispatch_generation_memory_delivery(
            db, operation_id=job_id, memory=memory, logger=logger
        )
    except Exception:
        # Publication is already committed.  The durable pending envelope is
        # the retry authority; never turn this into a reason to republish.
        return False
    return outcome.get("state") == "delivered"


def _recover_receipt(
    db, *, row, job_id: str, worker_id: str, lease_attempt: int,
    receipt_callback, recovered: bool, memory: Any = None, memory_logger: Any = None,
) -> NativeV3PublishOutcome:
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt, side_effect_committed=True)
    if row["state"] not in {"published_pending_receipt", "completed"}:
        raise D2PublicationError("receipt_state_invalid", "D2 发布尚未提交")
    publication, _request = _validated_committed_payload(row)
    if "priority_once" in _request:
        from .exam_generation_priority_once import PriorityOnceKey, verify_priority_once_consumed
        verify_priority_once_consumed(
            db, keys=tuple(PriorityOnceKey(**key) for key in _request["priority_once"]["claims"]),
            job_id=job_id, exam_id=int(row["exam_id"]), publication_hash=row["publication_hash"],
        )
    if publication["job_id"] != job_id:
        raise D2PublicationError("publication_payload_drift", "D2 发布事实 job 身份漂移")
    if not _deliver_memory_before_receipt(
        db, job_id=job_id, memory=memory, logger=memory_logger
    ):
        return NativeV3PublishOutcome(
            job_id, "published_pending_receipt", row["request_hash"],
            int(row["exam_id"]), row["publication_hash"], None, recovered,
        )
    body = {"version": D2_RECEIPT_VERSION, "job_id": job_id, "request_hash": row["request_hash"], "publication_hash": row["publication_hash"], "exam_id": int(row["exam_id"]), "status": "published"}
    if row["state"] == "completed":
        if row["receipt_hash"] != _digest(body):
            raise D2PublicationError("receipt_payload_drift", "D2 发布回执身份漂移")
        return NativeV3PublishOutcome(job_id, "completed", row["request_hash"], int(row["exam_id"]), row["publication_hash"], row["receipt_hash"], recovered)
    def commit(connection, receipt_hash: str, committed_at: str) -> None:
        updated = connection.execute(
            f"UPDATE {D2_TABLE} SET state='completed',receipt_hash=?,revision=revision+1,updated_at=? "
            "WHERE job_id=? AND state='published_pending_receipt' AND publication_hash=?",
            (receipt_hash, committed_at, job_id, row["publication_hash"]),
        )
        if updated.rowcount != 1:
            raise D2PublicationError("receipt_cas_lost", "D2 回执状态 CAS 丢失", retryable=True)
    try:
        receipt_hash = complete_native_publication_receipt(
            db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt,
            receipt_body=body, receipt_callback=receipt_callback,
            receipt_commit=commit, created_at=_timestamp(None),
        )
    except _ReceiptDeferred:
        return NativeV3PublishOutcome(job_id, "published_pending_receipt", row["request_hash"], int(row["exam_id"]), row["publication_hash"], None, recovered)
    return NativeV3PublishOutcome(job_id, "completed", row["request_hash"], int(row["exam_id"]), row["publication_hash"], receipt_hash, recovered)


def _validated_committed_payload(row) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify one D2 publication and the request/quality chain it commits."""

    if row["state"] not in {"published_pending_receipt", "completed"}:
        raise D2PublicationError("publication_state_invalid", "D2 使用记录只接受已提交发布")
    try:
        publication = json.loads(row["publication_json"])
        request = json.loads(row["request_json"])
        authority = json.loads(row["authority_json"])
        quality_request = json.loads(row["quality_request_json"])
        quality_response = json.loads(row["quality_response_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise D2PublicationError("publication_payload_invalid", "D2 发布事实链 JSON 损坏") from exc
    if (
        _digest(publication) != row["publication_hash"]
        or _digest(request) != row["request_hash"]
        or _digest(authority) != row["authority_hash"]
        or _digest(quality_request) != row["quality_request_hash"]
        or _digest(quality_response) != row["quality_response_hash"]
        or publication.get("job_id") != row["job_id"]
        or publication.get("request_hash") != row["request_hash"]
        or publication.get("exam_id") != int(row["exam_id"])
        or publication.get("selection_hash") != row["selection_hash"]
        or publication.get("objective_hash") != row["objective_hash"]
        or publication.get("quality_response_hash") != row["quality_response_hash"]
        or publication.get("artifact_hash") != row["artifact_hash"]
        or request.get("authority_hash") != row["authority_hash"]
        or request.get("selection_hash") != row["selection_hash"]
        or request.get("objective_hash") != row["objective_hash"]
        or request.get("quality_request_hash") != row["quality_request_hash"]
        or request.get("quality_response_hash") != row["quality_response_hash"]
        or request.get("draft_hash") != row["draft_hash"]
        or request.get("final_exam_hash") != publication.get("final_exam_hash")
    ):
        raise D2PublicationError("publication_payload_drift", "D2 发布事实链身份漂移")
    draft = request.get("draft")
    normalized_quality = _parse_quality(quality_response, quality_request)
    if (
        not isinstance(draft, Mapping)
        or _digest(draft) != row["draft_hash"]
        or draft.get("final_exam_hash") != publication.get("final_exam_hash")
        or normalized_quality != quality_response
        or any(not item["passed"] for item in normalized_quality["results"])
        or not isinstance(publication.get("published_at"), str)
        or not publication["published_at"]
    ):
        raise D2PublicationError("publication_draft_drift", "D2 发布草稿或时间身份漂移")
    return publication, request


def published_d2_usage(db, *, subject_id: int, limit: int = 256) -> list[dict[str, Any]]:
    """Project recent D2 candidate usage from verified publication facts.

    The projection is read-only and deterministic.  It does not write the old
    v46 usage table, whose foreign key intentionally belongs to legacy-native
    publication facts.
    """

    if type(subject_id) is not int or subject_id <= 0:
        raise D2PublicationError("usage_subject_invalid", "D2 使用记录学科无效")
    if type(limit) is not int or not 1 <= limit <= 256:
        raise D2PublicationError("usage_limit_invalid", "D2 使用记录窗口无效")
    try:
        rows = db.execute(
            f"SELECT * FROM {D2_TABLE} "
            "WHERE state IN ('published_pending_receipt','completed') "
            "AND json_extract(request_json,'$.subject_id')=? "
            "ORDER BY json_extract(publication_json,'$.published_at') DESC,job_id DESC "
            "LIMIT ?",
            (subject_id, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).casefold():
            return []
        raise
    projected: list[dict[str, Any]] = []
    for row in rows:
        publication, request = _validated_committed_payload(row)
        if request.get("subject_id") != subject_id:
            raise D2PublicationError("usage_subject_binding_drift", "D2 使用记录学科绑定漂移")
        draft = request["draft"]
        items = draft.get("structured_items")
        if not isinstance(items, list) or len(items) != publication.get("item_count"):
            raise D2PublicationError("usage_items_invalid", "D2 使用记录题目集合无效")
        seen_positions: set[int] = set()
        for item in items:
            if not isinstance(item, Mapping):
                raise D2PublicationError("usage_item_invalid", "D2 使用记录题目无效")
            position = item.get("position")
            candidate_id = item.get("candidate_id")
            content_hash = item.get("content_hash")
            if (
                type(position) is not int
                or position < 1
                or position in seen_positions
                or not isinstance(candidate_id, str)
                or not candidate_id
            ):
                raise D2PublicationError("usage_item_invalid", "D2 使用记录题目身份无效")
            _hex(content_hash, "usage_content_hash")
            body = {
                "version": D2_USAGE_VERSION,
                "job_id": publication["job_id"],
                "exam_id": publication["exam_id"],
                "position": position,
                "candidate_id": candidate_id,
                "content_hash": content_hash,
                "publication_hash": row["publication_hash"],
            }
            projected.append(
                {
                    "job_id": body["job_id"],
                    "position": position,
                    "candidate_id": candidate_id,
                    "content_hash": content_hash,
                    "usage_hash": _digest(body),
                    "created_at": publication["published_at"],
                }
            )
            seen_positions.add(position)
    projected.sort(key=lambda item: (item["created_at"], item["job_id"], item["position"]), reverse=True)
    return projected[:limit]


def run_d2_quality_and_publish(
    db, *, state: D2CandidateState, facts: FrozenCandidateFacts, stage3_contract,
    job_id: str, worker_id: str, lease_attempt: int, subject_id: int, title: str,
    artifact_dir: str, artifact_callback: Callable[[Mapping[str, Any]], Any],
    quality_callback: Callable[[str], Any],
    receipt_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    lifecycle_guard: Callable[[Mapping[str, Any]], Any] | None = None,
    priority_claims=None,
    memory: Any = None,
    pending_memory_request: Mapping[str, Any] | None = None,
    memory_subject: str | None = None,
    memory_logger: Any = None,
    render_probe=None,
) -> NativeV3PublishOutcome:
    """Review the unmodified D2 paper and publish it through native primitives."""

    if db.in_transaction:
        raise D2PublicationError("nested_transaction_forbidden", "D2 发布不能嵌套写事务")
    if not isinstance(job_id, str) or not job_id or not isinstance(worker_id, str) or not worker_id or type(lease_attempt) is not int or lease_attempt < 1 or type(subject_id) is not int or subject_id <= 0:
        raise D2PublicationError("publisher_actor_invalid", "D2 发布 actor 或科目无效")
    existing = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    if existing is not None and existing["state"] in {"published_pending_receipt", "completed"}:
        return _recover_receipt(
            db, row=existing, job_id=job_id, worker_id=worker_id,
            lease_attempt=lease_attempt, receipt_callback=receipt_callback,
            recovered=True, memory=memory, memory_logger=memory_logger,
        )
    if existing is not None and existing["state"] == "failed":
        raise D2PublicationError(str(existing["failure_reason"] or "quality_failed"), "D2 质量审查已明确失败")

    from .exam_generation_priority_runtime import load_priority_claims, validate_priority_claim_sources
    from .exam_generation_priority_once import assert_priority_once_available, consume_priority_once_in_transaction
    frozen_claims = load_priority_claims(db, job_id=job_id, state=state)
    if priority_claims != frozen_claims:
        raise D2PublicationError("priority_claim_binding_changed", "发布消费资格未绑定当前冻结选择")
    if priority_claims is not None:
        if any(key.subject_id != subject_id or key.mode != "past" for key in priority_claims):
            raise D2PublicationError("priority_claim_scope_changed", "一次性消费科目或模式不匹配")
        if existing is None or existing["state"] != "request_frozen":
            assert_priority_once_available(db, keys=priority_claims, job_id=job_id)

    authority, items = _state_authority(
        state,
        facts,
        stage3_contract,
        review_marker_loader=lambda source_kind, candidate_id: marker_for(
            db, source_kind=source_kind, candidate_id=candidate_id
        ),
    )
    from .exam_generation_d2_quality_repair import D2ContentRepairRequired, known_content_rejections
    rejected = known_content_rejections(db, job_id)
    if any(item["assignment"]["content_hash"] in rejected for item in items):
        raise D2ContentRepairRequired(rejected)
    if priority_claims is not None:
        validate_priority_claim_sources(db, job_id=job_id, state=state, items=items, keys=priority_claims)
    request = _quality_request(state.selection.selection_hash, items)
    authority_hash, request_hash = _digest(authority), _digest(request)
    now = _timestamp(None)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        current = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        if current is None:
            db.execute(
                f"INSERT INTO {D2_TABLE}(job_id,state_version,state,selection_hash,objective_hash,candidate_facts_hash,stage3_contract_hash,authority_hash,authority_json,quality_request_hash,quality_request_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, D2_STATE_VERSION, "quality_pending", state.selection.selection_hash, state.selection.objective_hash, facts.candidate_facts_hash, stage3_contract.stage3_contract_hash, authority_hash, _canonical(authority), request_hash, _canonical(request), 1, now, now),
            )
        elif current["authority_hash"] != authority_hash or current["quality_request_hash"] != request_hash:
            raise D2PublicationError("frozen_quality_request_conflict", "已冻结 D2 质量请求与当前权威冲突")

    current = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    if current["state"] == "quality_pending":
        _lifecycle(lifecycle_guard, "before_quality", job_id)
        if current["quality_response_json"] is None:
            with immediate_transaction(db):
                _fence(db, job_id, worker_id, lease_attempt)
                claimed = db.execute(
                    f"UPDATE {D2_TABLE} SET failure_reason='quality_call_started',revision=revision+1,updated_at=? "
                    "WHERE job_id=? AND state='quality_pending' AND quality_response_json IS NULL AND failure_reason IS NULL",
                    (_timestamp(None), job_id),
                )
                if claimed.rowcount != 1:
                    raise D2PublicationError("quality_call_outcome_unknown", "先前质量调用没有持久结果，未自动重放模型请求")
        try:
            response = (
                _parse_quality(current["quality_response_json"], request)
                if current["quality_response_json"] is not None
                else _parse_quality(quality_callback(_canonical(request)), request)
            )
        except BaseException as exc:
            if _is_lifecycle_interrupt(exc):
                raise
            provider_code = str(getattr(exc, "code", "")).casefold()
            reason = (
                "quality_provider_authentication" if provider_code == "authentication"
                else str(getattr(exc, "reason", "quality_callback_failed"))[:128]
            )
            with immediate_transaction(db):
                _fence(db, job_id, worker_id, lease_attempt)
                db.execute(f"UPDATE {D2_TABLE} SET state='failed',failure_reason=?,revision=revision+1,updated_at=? WHERE job_id=? AND state='quality_pending'", (reason, _timestamp(None), job_id))
            if isinstance(exc, D2PublicationError):
                raise
            raise D2PublicationError(reason, "D2 质量回调失败") from exc
        if any(not row["passed"] for row in response["results"]):
            with immediate_transaction(db):
                _fence(db, job_id, worker_id, lease_attempt)
                db.execute(f"UPDATE {D2_TABLE} SET quality_response_hash=?,quality_response_json=?,failure_reason='awaiting_rendering_attribution',revision=revision+1,updated_at=? WHERE job_id=? AND state='quality_pending'", (_digest(response), _canonical(response), _timestamp(None), job_id))
            from .exam_generation_d2_quality_repair import attribute_rendering_failure
            attribute_rendering_failure(
                db, state=state, items=items, job_id=job_id, worker_id=worker_id,
                lease_attempt=lease_attempt, render_probe=render_probe,
                positions={row["position"] for row in response["results"] if not row["passed"]},
            )
        with immediate_transaction(db):
            _fence(db, job_id, worker_id, lease_attempt)
            updated = db.execute(f"UPDATE {D2_TABLE} SET state='quality_passed',quality_response_hash=?,quality_response_json=?,failure_reason=NULL,revision=revision+1,updated_at=? WHERE job_id=? AND state='quality_pending'", (_digest(response), _canonical(response), _timestamp(None), job_id))
            if updated.rowcount != 1:
                raise D2PublicationError("quality_state_cas_lost", "D2 质量状态 CAS 丢失", retryable=True)

    current = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    if current is None or current["state"] not in {"quality_passed", "request_frozen"}:
        raise D2PublicationError("quality_state_invalid", "D2 质量审查没有可发布终态")
    quality_response_hash = _hex(current["quality_response_hash"], "quality_response_hash")
    try:
        draft = _draft(title, items, stage3_contract, request)
    except (TexCompileError, UnsafeLatexError):
        from .exam_generation_d2_quality_repair import attribute_rendering_failure
        attribute_rendering_failure(
            db, state=state, items=items, job_id=job_id, worker_id=worker_id,
            lease_attempt=lease_attempt, render_probe=render_probe,
        )
    publish_request = {"version": D2_REQUEST_VERSION, "job_id": job_id, "subject_id": subject_id, "authority_hash": authority_hash, "selection_hash": state.selection.selection_hash, "objective_hash": state.selection.objective_hash, "quality_request_hash": request_hash, "quality_response_hash": quality_response_hash, "stage3_contract_hash": stage3_contract.stage3_contract_hash, "final_exam_hash": draft.final_exam_hash, "draft_hash": draft.draft_hash, "draft": draft.to_dict()}
    if priority_claims is not None:
        publish_request["priority_once"] = {"claims": [asdict(key) for key in priority_claims]}
    frozen_hash = _digest(publish_request)
    with immediate_transaction(db):
        _fence(db, job_id, worker_id, lease_attempt)
        row = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        if row["state"] == "quality_passed":
            db.execute(f"UPDATE {D2_TABLE} SET state='request_frozen',draft_hash=?,request_hash=?,request_json=?,revision=revision+1,updated_at=? WHERE job_id=? AND state='quality_passed'", (draft.draft_hash, frozen_hash, _canonical(publish_request), _timestamp(None), job_id))
        elif row["state"] != "request_frozen" or row["request_hash"] != frozen_hash:
            raise D2PublicationError("publish_request_conflict", "D2 发布请求状态冲突")

    def reserve_guard(connection) -> None:
        row = connection.execute(f"SELECT state,request_hash FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
        if row is None or row["state"] != "request_frozen" or row["request_hash"] != frozen_hash:
            raise D2PublicationError("reserve_state_conflict", "D2 发布预留状态冲突")

    exam_id = reserve_native_paper(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, request_hash=frozen_hash, draft=draft, subject_id=subject_id, created_at=now, publication_version=D2_PUBLICATION_VERSION, request_guard=reserve_guard)
    try:
        if priority_claims is not None:
            assert_priority_once_available(db, keys=priority_claims, job_id=job_id)
        _lifecycle(lifecycle_guard, "before_artifact", job_id)
        artifact, artifact_hash = _artifact(artifact_callback, publish_request, frozen_hash, exam_id=exam_id, artifact_dir=artifact_dir, worker_id=worker_id, lease_attempt=lease_attempt)
        _lifecycle(lifecycle_guard, "before_publish", job_id)

        def publication_guard(connection) -> None:
            row = connection.execute(f"SELECT state,request_hash FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
            if row is None or row["state"] != "request_frozen" or row["request_hash"] != frozen_hash:
                raise D2PublicationError("publication_state_conflict", "D2 原子发布状态冲突")
            rejected = known_content_rejections(connection, job_id)
            if any(item["assignment"]["content_hash"] in rejected for item in items):
                raise D2ContentRepairRequired(rejected)

        def publication_commit(connection, published_at: str) -> str:
            payload = {"version": D2_PUBLICATION_VERSION, "job_id": job_id, "request_hash": frozen_hash, "selection_hash": state.selection.selection_hash, "objective_hash": state.selection.objective_hash, "quality_response_hash": publish_request["quality_response_hash"], "exam_id": exam_id, "artifact_hash": artifact_hash, "final_exam_hash": draft.final_exam_hash, "item_count": len(draft.structured_items), "published_at": published_at}
            publication_hash = _digest(payload)
            if priority_claims is not None:
                consume_priority_once_in_transaction(
                    connection, keys=priority_claims, publication_hash=publication_hash,
                    job_id=job_id, exam_id=exam_id, consumed_at=published_at,
                )
            updated = connection.execute(f"UPDATE {D2_TABLE} SET state='published_pending_receipt',artifact_hash=?,exam_id=?,publication_hash=?,publication_json=?,revision=revision+1,updated_at=? WHERE job_id=? AND state='request_frozen' AND request_hash=?", (artifact_hash, exam_id, publication_hash, _canonical(payload), published_at, job_id, frozen_hash))
            if updated.rowcount != 1:
                raise D2PublicationError("publication_state_cas_lost", "D2 发布状态 CAS 丢失", retryable=True)
            if pending_memory_request is not None or memory_subject is not None or memory is not None:
                if (
                    not isinstance(pending_memory_request, Mapping)
                    or pending_memory_request.get("event_type") != "request"
                    or pending_memory_request.get("event_id") != f"{job_id}:request"
                    or pending_memory_request.get("operation_id") != job_id
                    or not isinstance(memory_subject, str)
                    or not memory_subject.strip()
                    or memory is None
                ):
                    raise D2PublicationError(
                        "memory_context_invalid", "D2 记忆上下文未绑定冻结请求"
                    )
                frozen_request = pending_memory_request.get("request")
                if not isinstance(frozen_request, Mapping):
                    raise D2PublicationError(
                        "memory_context_invalid", "D2 记忆请求载荷无效"
                    )
                if frozen_request.get("source") != "past":
                    raise D2PublicationError(
                        "memory_context_invalid", "D2 记忆请求模式无效"
                    )
                selection_items = []
                learning_years: set[int] = set()
                chapters: list[str] = []
                for selected_item in items:
                    assignment = selected_item["assignment"]
                    fact = selected_item["candidate_fact"]
                    snapshot = selected_item["content_snapshot"]
                    if fact.get("subject") != memory_subject.strip():
                        raise D2PublicationError(
                            "memory_context_invalid", "D2 记忆学科与冻结候选不一致"
                        )
                    chapter = str(fact.get("chapter") or assignment.get("chapter") or "").strip()
                    if chapter and chapter not in chapters:
                        chapters.append(chapter)
                    source_year = fact.get("source_year")
                    if type(source_year) is int:
                        learning_years.add(source_year)
                    selection_items.append(
                        {
                            "candidate_id": assignment["candidate_id"],
                            "chapter": chapter or None,
                            "source_file": fact.get("source_file_key"),
                            "source_number": fact.get("source_number"),
                            "difficulty": assignment.get("target_difficulty"),
                            "adapted": False,
                            "content": snapshot["content"],
                        }
                    )
                result_events = build_success_memory_events(
                    SuccessMemoryContext(
                        source="past",
                        operation_id=job_id,
                        subject=memory_subject.strip(),
                        chapters=tuple(chapters),
                        request=dict(frozen_request),
                        results=({"id": exam_id, "title": title.strip()},),
                        past_selections=(tuple(selection_items),),
                        learning_years=tuple(sorted(learning_years)),
                        allow_adaptation=False,
                    ),
                    fingerprint=question_fingerprint,
                )
                stage_generation_memory_delivery(
                    connection,
                    operation_id=job_id,
                    exam_id=exam_id,
                    mode="past",
                    actor="past_exam",
                    event_day=published_at[:10],
                    published_at=published_at,
                    events=(dict(pending_memory_request), *result_events),
                )
            return publication_hash

        publication_hash, _ = commit_native_paper_publication(db, job_id=job_id, worker_id=worker_id, lease_attempt=lease_attempt, request_hash=frozen_hash, exam_id=exam_id, artifact_dir=artifact_dir, draft=draft, artifact=artifact, publication_version=D2_PUBLICATION_VERSION, authority_guard=publication_guard, authority_commit=publication_commit)
    except BaseException as exc:
        _cleanup_staging(db, job_id=job_id, request_hash=frozen_hash, exam_id=exam_id, artifact_dir=artifact_dir, worker_id=worker_id, lease_attempt=lease_attempt)
        if isinstance(exc, (TexCompileError, UnsafeLatexError)):
            from .exam_generation_d2_quality_repair import attribute_rendering_failure
            attribute_rendering_failure(
                db, state=state, items=items, job_id=job_id, worker_id=worker_id,
                lease_attempt=lease_attempt, render_probe=render_probe,
            )
        raise
    row = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    return _recover_receipt(
        db, row=row, job_id=job_id, worker_id=worker_id,
        lease_attempt=lease_attempt, receipt_callback=receipt_callback,
        recovered=False, memory=memory, memory_logger=memory_logger,
    )


def recover_d2_publication_outcome(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    receipt_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    memory: Any = None,
    memory_logger: Any = None,
) -> NativeV3PublishOutcome | None:
    """Recover only an already-committed D2 publication.

    This entry deliberately needs no selection, candidate facts, Stage-3
    contract, quality callback, or artifact callback.  Therefore application
    dispatch can return the committed exam before profile/source preparation.
    An absent or pre-publication D2 row is not an error and lets normal pipeline
    resume continue.
    """

    if db.in_transaction:
        raise D2PublicationError("nested_recovery_transaction_forbidden", "D2 发布恢复不能嵌套事务")
    try:
        row = db.execute(f"SELECT * FROM {D2_TABLE} WHERE job_id=?", (job_id,)).fetchone()
    except sqlite3.OperationalError as exc:
        # A missing additive table means this installation has no D2 result.
        # Other database failures still need to remain visible.
        if "no such table" in str(exc).casefold():
            return None
        raise
    if row is None or row["state"] not in {"published_pending_receipt", "completed"}:
        return None
    return _recover_receipt(
        db, row=row, job_id=job_id, worker_id=worker_id,
        lease_attempt=lease_attempt, receipt_callback=receipt_callback,
        recovered=True, memory=memory, memory_logger=memory_logger,
    )


__all__ = [
    "D2_PUBLICATION_VERSION", "D2_REQUEST_VERSION", "D2_STATE_VERSION", "D2_USAGE_VERSION",
    "D2_TABLE", "D2PublicationError", "SCHEMA_STATEMENTS",
    "published_d2_usage", "recover_d2_publication_outcome", "run_d2_quality_and_publish",
]
