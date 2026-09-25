"""Strict authority adapters for the Stage 5/7 production boundary.

The functions in this module deliberately accept only immutable, hashed
authorities.  They do not consult legacy flows/profiles and never call a model.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping, Sequence

from .exam_generation_stage4_candidate_facts import FrozenCandidateFacts
from .exam_generation_stage4_repository import (
    Stage4RepositoryError,
    recover_completed_difficulty_result,
)
from .exam_generation_stage5_solver import (
    EXACT_HISTORY_AUDIT_POLICY_VERSION,
    FrozenCandidateEdgeGraph,
    RECENT_USAGE_AUDIT_POLICY_VERSION,
    Stage5ContractError,
    Stage5GlobalConstraintContract,
    Stage5Selection,
    solve_global_selection,
)
from .exam_generation_stage5_worker import (
    freeze_chapter_weight_fact,
    freeze_policy_fact,
    freeze_policy_fact_set,
)
from .exam_generation_stage7_quality import (
    GLOBAL_CONSTRAINT_EVIDENCE_VERSION,
    LOCAL_EVIDENCE_VERSION,
)


AUTHORITY_REGISTRY_VERSION = "past-v3-stage5-authority-registry-v1"
RECENT_USAGE_SNAPSHOT_VERSION = "past-v3-stage5-recent-usage-snapshot-v2"
EXACT_HISTORY_SNAPSHOT_VERSION = "past-v3-stage5-recent-usage-snapshot-v3"
ANSWER_AUTHORITY_RECEIPT_VERSION = "past-v3-stage7-answer-authority-receipt-v1"
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_REGISTRY_KEYS = ("chapter_weight", "diversity", "coverage", "uniqueness")
_FAILURE_REASONS = frozenset({
    "authority_registry_invalid", "authority_registry_hash_mismatch",
    "authority_entry_missing", "authority_entry_invalid", "authority_entry_hash_mismatch",
    "policy_source_identity_mismatch", "ordinary_chapter_authority_missing",
    "recent_usage_snapshot_invalid", "recent_usage_snapshot_hash_mismatch",
    "selection_invalid", "constraint_invalid", "active_graph_invalid", "score_receipt_invalid",
    "selection_constraint_binding_mismatch", "selection_not_solver_authoritative",
    "answer_authority_invalid", "answer_authority_hash_mismatch", "answer_coverage_invalid",
    "answer_candidate_binding_mismatch", "answer_false",
    "difficulty_selection_invalid", "difficulty_candidate_facts_invalid",
    "difficulty_completed_result_missing", "difficulty_result_cross_job",
    "difficulty_result_invalid", "difficulty_result_not_canonical",
})


class Phase8AuthorityError(RuntimeError):
    """Stable 422 fail-closed error with finite machine-readable reasons."""

    def __init__(self, reason: str, message: str, *, details: Sequence[Mapping[str, Any]] = ()) -> None:
        stable = reason if reason in _FAILURE_REASONS else "authority_entry_invalid"
        super().__init__(message)
        self.code = "exam_phase8_authority_rejected"
        self.reason = stable
        self.reason_codes = (stable,)
        self.status_code = 422
        self.retryable = False
        self.details = tuple(dict(item) for item in details)[:100]


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Phase8AuthorityError("authority_entry_invalid", "权威输入不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(value: object, reason: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Phase8AuthorityError(reason, "权威哈希无效")
    return value


def _text(value: object, reason: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise Phase8AuthorityError(reason, "权威身份无效")
    return value.strip()


def _exact(value: object, fields: set[str], reason: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise Phase8AuthorityError(reason, "权威字段集合无效")
    return value


def _registry_entry(registry: Mapping[str, Any], name: str, *, required: bool = True) -> Mapping[str, Any] | None:
    entries = registry["entries"]
    raw = entries.get(name)
    if raw is None and not required:
        return None
    entry = _exact(raw, {"version", "payload", "authority_hash"}, "authority_entry_missing" if raw is None else "authority_entry_invalid")
    version = _text(entry["version"], "authority_entry_invalid", 128)
    if not isinstance(entry["payload"], Mapping):
        raise Phase8AuthorityError("authority_entry_invalid", "政策权威载荷无效")
    expected = _digest({"version": version, "payload": entry["payload"]})
    if not hmac.compare_digest(expected, _hash(entry["authority_hash"], "authority_entry_invalid")):
        raise Phase8AuthorityError("authority_entry_hash_mismatch", "政策权威条目哈希不匹配")
    return entry


def build_stage5_policy_fact_set(
    *,
    stage3: Mapping[str, Any],
    stage4: Mapping[str, Any],
    authority_registry: Mapping[str, Any],
    recent_usage_snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the existing Stage5 fact-set solely from explicit authorities."""

    if not isinstance(stage3, Mapping):
        raise Phase8AuthorityError("policy_source_identity_mismatch", "阶段 3 合同无效")
    source_identity = stage3.get("source_identity")
    if not isinstance(source_identity, Mapping):
        raise Phase8AuthorityError("policy_source_identity_mismatch", "阶段 3 来源身份缺失")
    registry = _exact(
        authority_registry,
        {"registry_version", "source_identity", "entries", "registry_hash"},
        "authority_registry_invalid",
    )
    if registry["registry_version"] != AUTHORITY_REGISTRY_VERSION or not isinstance(registry["entries"], Mapping):
        raise Phase8AuthorityError("authority_registry_invalid", "政策权威注册表版本或条目无效")
    if registry["source_identity"] != source_identity:
        raise Phase8AuthorityError("policy_source_identity_mismatch", "政策权威来源身份未逐字绑定阶段 3")
    registry_body = {key: registry[key] for key in ("registry_version", "source_identity", "entries")}
    if not hmac.compare_digest(_digest(registry_body), _hash(registry["registry_hash"], "authority_registry_invalid")):
        raise Phase8AuthorityError("authority_registry_hash_mismatch", "政策权威注册表哈希不匹配")
    if set(registry["entries"]) - set(_REGISTRY_KEYS):
        raise Phase8AuthorityError("authority_registry_invalid", "政策权威注册表含未知条目")

    quotas = stage3.get("source_quotas")
    ordinary_quota = quotas.get("ordinary", 0) if isinstance(quotas, Mapping) else 0
    if ordinary_quota > 0 and "chapter_weight" not in registry["entries"]:
        raise Phase8AuthorityError("ordinary_chapter_authority_missing", "普通题配额大于零但缺少章节权威")
    chapter = _registry_entry(registry, "chapter_weight")
    assert chapter is not None
    chapter_payload = _exact(chapter["payload"], {"subject", "weights"}, "authority_entry_invalid")
    if not isinstance(chapter_payload["weights"], Mapping):
        raise Phase8AuthorityError("authority_entry_invalid", "章节权威权重无效")
    chapter_fact = freeze_chapter_weight_fact(version=chapter["version"], subject=chapter_payload["subject"], weights=chapter_payload["weights"])

    if not isinstance(stage4, Mapping) or not isinstance(stage4.get("candidates"), list):
        raise Phase8AuthorityError("recent_usage_snapshot_invalid", "阶段 4 候选集合无法形成最近使用审计证据")
    stage4_ids: set[str] = set()
    stage4_hashes: set[str] = set()
    for raw in stage4["candidates"]:
        if not isinstance(raw, Mapping):
            raise Phase8AuthorityError("recent_usage_snapshot_invalid", "阶段 4 候选无法形成最近使用审计证据")
        candidate_id = _text(raw.get("candidate_id"), "recent_usage_snapshot_invalid")
        content_hash = _hash(raw.get("content_hash"), "recent_usage_snapshot_invalid")
        stage4_ids.add(candidate_id)
        stage4_hashes.add(content_hash)

    snapshot_version = recent_usage_snapshot.get("snapshot_version") if isinstance(recent_usage_snapshot, Mapping) else None
    if snapshot_version == RECENT_USAGE_SNAPSHOT_VERSION:
        snap = _exact(
            recent_usage_snapshot,
            {"snapshot_version", "snapshot_identity", "candidate_ids", "content_hashes", "snapshot_hash"},
            "recent_usage_snapshot_invalid",
        )
        _text(snap["snapshot_identity"], "recent_usage_snapshot_invalid", 512)
        if not isinstance(snap["candidate_ids"], list) or not isinstance(snap["content_hashes"], list):
            raise Phase8AuthorityError("recent_usage_snapshot_invalid", "最近使用快照集合无效")
        candidate_ids = [_text(value, "recent_usage_snapshot_invalid") for value in snap["candidate_ids"]]
        content_hashes = [_hash(value, "recent_usage_snapshot_invalid") for value in snap["content_hashes"]]
        if candidate_ids != sorted(set(candidate_ids)) or content_hashes != sorted(set(content_hashes)):
            raise Phase8AuthorityError("recent_usage_snapshot_invalid", "最近使用快照必须排序且去重")
        snapshot_body = {key: snap[key] for key in ("snapshot_version", "snapshot_identity", "candidate_ids", "content_hashes")}
        if not hmac.compare_digest(_digest(snapshot_body), _hash(snap["snapshot_hash"], "recent_usage_snapshot_invalid")):
            raise Phase8AuthorityError("recent_usage_snapshot_hash_mismatch", "最近使用快照哈希不匹配")
        recent_version = f"{RECENT_USAGE_AUDIT_POLICY_VERSION}:sha256-{snap['snapshot_hash']}"
        recent_payload = {
            "mode": "audit_only",
            "snapshot_identity": snap["snapshot_identity"],
            "snapshot_hash": snap["snapshot_hash"],
            "observed_candidate_ids": candidate_ids,
            "observed_content_hashes": content_hashes,
            "candidate_id_hits": sorted(set(candidate_ids).intersection(stage4_ids)),
            "content_hash_hits": sorted(set(content_hashes).intersection(stage4_hashes)),
        }
    elif snapshot_version == EXACT_HISTORY_SNAPSHOT_VERSION:
        snap = _exact(
            recent_usage_snapshot,
            {"snapshot_version", "mode", "subject_id", "window_limit", "snapshot_identity", "exact_uses", "snapshot_hash"},
            "recent_usage_snapshot_invalid",
        )
        if (
            snap["mode"] != "past"
            or type(snap["subject_id"]) is not int
            or snap["subject_id"] <= 0
            or type(snap["window_limit"]) is not int
            or not 1 <= snap["window_limit"] <= 256
            or not isinstance(snap["exact_uses"], list)
            or len(snap["exact_uses"]) > snap["window_limit"]
        ):
            raise Phase8AuthorityError("recent_usage_snapshot_invalid", "精确历史使用快照作用域无效")
        _text(snap["snapshot_identity"], "recent_usage_snapshot_invalid", 512)
        exact_uses: list[dict[str, Any]] = []
        usage_hashes: set[str] = set()
        for rank, raw in enumerate(snap["exact_uses"], start=1):
            row = _exact(
                raw,
                {"usage_hash", "content_hash", "used_at", "job_id", "position", "order_rank"},
                "recent_usage_snapshot_invalid",
            )
            usage_hash = _hash(row["usage_hash"], "recent_usage_snapshot_invalid")
            content_hash = _hash(row["content_hash"], "recent_usage_snapshot_invalid")
            used_at = _text(row["used_at"], "recent_usage_snapshot_invalid")
            job_id = _text(row["job_id"], "recent_usage_snapshot_invalid")
            if (
                usage_hash in usage_hashes
                or type(row["position"]) is not int
                or row["position"] <= 0
                or row["order_rank"] != rank
            ):
                raise Phase8AuthorityError("recent_usage_snapshot_invalid", "精确历史使用事件无效")
            usage_hashes.add(usage_hash)
            exact_uses.append({
                "usage_hash": usage_hash, "content_hash": content_hash,
                "used_at": used_at, "job_id": job_id,
                "position": row["position"], "order_rank": rank,
            })
        if exact_uses != sorted(
            exact_uses,
            key=lambda item: (item["used_at"], item["job_id"], item["position"]),
            reverse=True,
        ):
            raise Phase8AuthorityError("recent_usage_snapshot_invalid", "精确历史使用顺序无效")
        snapshot_body = {key: snap[key] for key in (
            "snapshot_version", "mode", "subject_id", "window_limit",
            "snapshot_identity", "exact_uses",
        )}
        if not hmac.compare_digest(_digest(snapshot_body), _hash(snap["snapshot_hash"], "recent_usage_snapshot_invalid")):
            raise Phase8AuthorityError("recent_usage_snapshot_hash_mismatch", "最近使用快照哈希不匹配")
        grouped: dict[str, dict[str, Any]] = {}
        for event in exact_uses:
            content_hash = event["content_hash"]
            if content_hash not in stage4_hashes:
                continue
            hit = grouped.setdefault(content_hash, {
                "content_hash": content_hash,
                "success_count": 0,
                "latest_order_rank": event["order_rank"],
                "latest_used_at": event["used_at"],
                "usage_hashes": [],
            })
            hit["success_count"] += 1
            hit["usage_hashes"].append(event["usage_hash"])
        recent_version = f"{EXACT_HISTORY_AUDIT_POLICY_VERSION}:sha256-{snap['snapshot_hash']}"
        recent_payload = {
            "mode": "audit_only",
            "scope": {
                "generation_mode": "past",
                "subject_id": snap["subject_id"],
                "window_limit": snap["window_limit"],
            },
            "snapshot_identity": snap["snapshot_identity"],
            "snapshot_hash": snap["snapshot_hash"],
            "ordered_usage_count": len(exact_uses),
            "content_hash_hits": [grouped[key] for key in sorted(grouped)],
        }
    else:
        raise Phase8AuthorityError("recent_usage_snapshot_invalid", "最近使用快照版本无效")
    recent_fact = freeze_policy_fact(
        version=recent_version,
        payload=recent_payload,
    )
    policies: dict[str, Mapping[str, Any]] = {}
    for name in ("diversity", "coverage", "uniqueness"):
        entry = _registry_entry(registry, name)
        assert entry is not None
        policies[name] = freeze_policy_fact(version=entry["version"], payload=entry["payload"])
    return freeze_policy_fact_set(
        source_identity=source_identity,
        chapter_weight_fact=chapter_fact,
        recent_usage=recent_fact,
        diversity_policy=policies["diversity"],
        coverage_policy=policies["coverage"],
        uniqueness_policy=policies["uniqueness"],
    )


def build_stage7_global_constraint_evidence(
    *,
    selection: Stage5Selection,
    constraint: Stage5GlobalConstraintContract,
    active_graph: FrozenCandidateEdgeGraph,
    score_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Re-run the public exact solver and attest only an identical result."""

    try:
        rebuilt = solve_global_selection(constraint, active_graph, score_receipt=score_receipt)
    except Stage5ContractError as exc:
        reason = {
            "global_contract_type_invalid": "constraint_invalid",
            "candidate_graph_type_invalid": "active_graph_invalid",
        }.get(exc.reason, "score_receipt_invalid")
        raise Phase8AuthorityError(reason, "全卷硬约束无法本地复验", details=({"upstream_reason": exc.reason},)) from exc
    if not isinstance(selection, Stage5Selection):
        raise Phase8AuthorityError("selection_invalid", "阶段 5 选择合同无效")
    if selection.constraint_hash != constraint.constraint_hash or selection.graph_hash != active_graph.graph_hash:
        raise Phase8AuthorityError("selection_constraint_binding_mismatch", "选择未绑定当前约束或活动图")
    try:
        identical = selection.to_dict() == rebuilt.to_dict() and selection.selection_hash == rebuilt.selection_hash
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Phase8AuthorityError("selection_invalid", "阶段 5 选择载荷无效") from exc
    if not identical:
        raise Phase8AuthorityError(
            "selection_not_solver_authoritative",
            "选择未通过确定性本地硬约束复验",
            details=({"expected_selection_hash": rebuilt.selection_hash, "actual_selection_hash": selection.selection_hash},),
        )
    body = {
        "version": GLOBAL_CONSTRAINT_EVIDENCE_VERSION,
        "selection_hash": selection.selection_hash,
        "constraint_hash": constraint.constraint_hash,
        "passed": True,
    }
    return {**body, "evidence_hash": _digest(body)}


def build_local_answer_evidence(
    *, selection: Stage5Selection, answer_authority_receipt: Mapping[str, Any]
) -> dict[str, Any]:
    """Project exact selected-candidate answer authorities into Stage7 evidence."""

    if not isinstance(selection, Stage5Selection):
        raise Phase8AuthorityError("selection_invalid", "阶段 5 选择合同无效")
    receipt = _exact(
        answer_authority_receipt,
        {"receipt_version", "selection_hash", "authority_identity", "entries", "receipt_hash"},
        "answer_authority_invalid",
    )
    if receipt["receipt_version"] != ANSWER_AUTHORITY_RECEIPT_VERSION or receipt["selection_hash"] != selection.selection_hash:
        raise Phase8AuthorityError("answer_authority_invalid", "答案权威回执未绑定当前选择")
    _text(receipt["authority_identity"], "answer_authority_invalid")
    if not isinstance(receipt["entries"], list):
        raise Phase8AuthorityError("answer_authority_invalid", "答案权威条目无效")
    receipt_body = {key: receipt[key] for key in ("receipt_version", "selection_hash", "authority_identity", "entries")}
    if not hmac.compare_digest(_digest(receipt_body), _hash(receipt["receipt_hash"], "answer_authority_invalid")):
        raise Phase8AuthorityError("answer_authority_hash_mismatch", "答案权威回执哈希不匹配")
    selected = {item["candidate_id"]: item["content_hash"] for item in selection.assignments}
    answers: dict[str, bool] = {}
    for raw in receipt["entries"]:
        entry = _exact(raw, {"candidate_id", "content_hash", "answer_exists", "authority_hash"}, "answer_authority_invalid")
        body = {key: entry[key] for key in ("candidate_id", "content_hash", "answer_exists")}
        if _digest(body) != _hash(entry["authority_hash"], "answer_authority_invalid"):
            raise Phase8AuthorityError("answer_authority_hash_mismatch", "答案权威条目哈希不匹配")
        candidate_id = _text(entry["candidate_id"], "answer_authority_invalid")
        content_hash = _hash(entry["content_hash"], "answer_authority_invalid")
        if candidate_id in answers or candidate_id not in selected or selected[candidate_id] != content_hash:
            raise Phase8AuthorityError("answer_candidate_binding_mismatch", "答案权威未精确绑定所选候选与内容")
        if type(entry["answer_exists"]) is not bool:
            raise Phase8AuthorityError("answer_authority_invalid", "答案存在性必须由权威显式给出")
        answers[candidate_id] = entry["answer_exists"]
    if set(answers) != set(selected):
        missing = sorted(set(selected) - set(answers))
        raise Phase8AuthorityError("answer_coverage_invalid", "答案权威未逐题覆盖", details=tuple({"candidate_id": value} for value in missing))
    false_ids = sorted(key for key, value in answers.items() if not value)
    if false_ids:
        raise Phase8AuthorityError("answer_false", "所选候选存在明确的答案缺失证据", details=tuple({"candidate_id": value} for value in false_ids))
    ordered = {key: answers[key] for key in sorted(answers)}
    body = {"version": LOCAL_EVIDENCE_VERSION, "selection_hash": selection.selection_hash, "answer_exists": ordered}
    return {**body, "evidence_hash": _digest(body)}


def load_completed_difficulty_results(
    db,
    *,
    job_id: str,
    selection: Stage5Selection,
    candidate_facts: FrozenCandidateFacts,
    worker_id: str,
    lease_attempt: int,
) -> tuple[Any, ...]:
    """Load only current-job v42 results needed by selected unresolved facts."""

    if not isinstance(selection, Stage5Selection):
        raise Phase8AuthorityError("difficulty_selection_invalid", "阶段 5 选择合同无效")
    if not isinstance(candidate_facts, FrozenCandidateFacts):
        raise Phase8AuthorityError("difficulty_candidate_facts_invalid", "阶段 4 候选事实合同无效")
    try:
        facts = candidate_facts.to_dict()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Phase8AuthorityError("difficulty_candidate_facts_invalid", "阶段 4 候选事实载荷无效") from exc
    if _digest(facts) != candidate_facts.candidate_facts_hash:
        raise Phase8AuthorityError("difficulty_candidate_facts_invalid", "阶段 4 候选事实哈希漂移")
    if selection.to_dict().get("candidate_facts_hash") != candidate_facts.candidate_facts_hash:
        raise Phase8AuthorityError("difficulty_selection_invalid", "选择未绑定当前候选事实")
    facts_by_id = {item.get("candidate_id"): item for item in facts.get("candidates", []) if isinstance(item, Mapping)}
    required: set[str] = set()
    for assignment in selection.assignments:
        fact = facts_by_id.get(assignment.get("candidate_id"))
        difficulty = fact.get("difficulty_fact") if isinstance(fact, Mapping) else None
        if not isinstance(difficulty, Mapping):
            raise Phase8AuthorityError("difficulty_candidate_facts_invalid", "所选候选难度事实缺失")
        if difficulty.get("resolved") is False:
            required.add(assignment["candidate_id"])
        elif difficulty.get("resolved") is not True:
            raise Phase8AuthorityError("difficulty_candidate_facts_invalid", "所选候选难度事实状态无效")
    if not required:
        return ()
    placeholders = ",".join("?" for _ in required)
    rows = db.execute(
        "SELECT DISTINCT f.flight_hash,f.claim_job_id,o.candidate_id,r.result_hash "
        "FROM exam_generation_stage4_difficulty_flights f "
        "JOIN exam_generation_stage4_difficulty_results r ON r.flight_hash=f.flight_hash "
        "JOIN exam_generation_stage4_difficulty_outcomes o ON o.flight_hash=f.flight_hash "
        f"WHERE f.state='completed' AND o.candidate_id IN ({placeholders})",
        tuple(sorted(required)),
    ).fetchall()
    current = [row for row in rows if str(row["claim_job_id"]) == job_id]
    covered = {str(row["candidate_id"]) for row in current}
    if covered != required:
        cross = sorted(required.intersection({str(row["candidate_id"]) for row in rows}) - covered)
        reason = "difficulty_result_cross_job" if cross else "difficulty_completed_result_missing"
        missing = sorted(required - covered)
        raise Phase8AuthorityError(reason, "当前 job 缺少所选未解析候选的 completed 难度结果", details=tuple({"candidate_id": value} for value in missing))
    flight_hashes = sorted({str(row["flight_hash"]) for row in current}, key=lambda value: next(str(row["result_hash"]) for row in current if str(row["flight_hash"]) == value))
    results = []
    try:
        for flight_hash in flight_hashes:
            result = recover_completed_difficulty_result(
                db, job_id=job_id, flight_hash=flight_hash,
                worker_id=worker_id, lease_attempt=lease_attempt,
            )
            if result is None:
                raise Phase8AuthorityError("difficulty_completed_result_missing", "completed 难度结果恢复失败")
            results.append(result)
    except Stage4RepositoryError as exc:
        raise Phase8AuthorityError("difficulty_result_invalid", "completed 难度结果权威校验失败", details=({"upstream_reason": exc.reason},)) from exc
    results.sort(key=lambda value: value.result_hash)
    if len({value.result_hash for value in results}) != len(results):
        raise Phase8AuthorityError("difficulty_result_not_canonical", "completed 难度结果集合重复")
    outcome_ids = {outcome.candidate_id for result in results for outcome in result.outcomes}
    if not required.issubset(outcome_ids):
        raise Phase8AuthorityError("difficulty_completed_result_missing", "completed 难度结果未覆盖所需候选")
    return tuple(results)


__all__ = [
    "ANSWER_AUTHORITY_RECEIPT_VERSION", "AUTHORITY_REGISTRY_VERSION",
    "EXACT_HISTORY_SNAPSHOT_VERSION", "RECENT_USAGE_AUDIT_POLICY_VERSION",
    "RECENT_USAGE_SNAPSHOT_VERSION", "Phase8AuthorityError",
    "build_local_answer_evidence", "build_stage5_policy_fact_set",
    "build_stage7_global_constraint_evidence", "load_completed_difficulty_results",
]
