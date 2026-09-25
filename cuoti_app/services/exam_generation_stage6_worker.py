"""Recoverable offline orchestration for Stage 6 scoring and local solving.

The worker owns no provider configuration and performs no network calls.  A
caller may inject a scoring callback; every callback receives the exact JSON
object which was durably frozen by the Stage 6 repository first.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage5_repository import (
    persist_infeasible_witness,
    persist_repair_result,
    persist_selection,
    recover_stage5_state,
)
from .exam_generation_stage5_solver import (
    Stage5InfeasibleError,
    locate_candidate_graph_deficit,
    repair_global_selection,
    solve_global_selection,
)
from .exam_generation_stage6_checkpoint_v3 import build_checkpoint_v3
from .exam_generation_stage6_repository import (
    HEADS,
    MATERIALS,
    REQUESTS,
    RESPONSES,
    ROOTS,
    WINDOWS,
    _LEGACY_SCORE_FIXTURE_WRITE_CAPABILITY,
    Stage6RepositoryError,
    append_rejection_ledger,
    append_scoring_retry_event,
    persist_checkpoint_v3,
    persist_material_receipt,
    persist_score_receipt,
    persist_scoring_request,
    persist_scoring_terminal_failure,
    persist_scoring_window,
    persist_selection_reference,
    recover_scoring_attempts,
    recover_stage6_state,
)
from .exam_generation_stage6_scoring import (
    REJECTION_LEDGER_VERSION,
    RejectionLedgerHead,
    ScoringWindow,
    Stage6ContractError,
    WindowScoreResult,
    append_rejection,
    build_material_preheat_receipt,
    build_score_receipt,
    build_scoring_windows,
    parse_scoring_response,
    provider_concurrency,
    render_scoring_request,
    evaluate_candidate_base_rubric_windows,
)
from .exam_generation_stage6_rubric import (
    CandidateBaseRubricError,
    validate_utility_policy,
)
from .job_contracts import JobCancelled, JobLeaseLost


ScoreCallback = Callable[[Mapping[str, Any]], str | Mapping[str, Any]]
StepHook = Callable[[str], None]
ZERO_HASH = "0" * 64


class Stage6WorkerError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = False


@dataclass(frozen=True, slots=True)
class Stage6WorkerResult:
    status: str
    code: str
    reason: str
    status_code: int
    graph_hash: str
    score_set_hash: str | None = None
    selection_hash: str | None = None
    checkpoint_hash: str | None = None
    witness: Mapping[str, Any] | None = None


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _step(hook: StepHook | None, name: str) -> None:
    if hook is not None:
        hook(name)


def _window_from_payload(payload: Mapping[str, Any]) -> ScoringWindow:
    raw = _canonical(payload)
    return ScoringWindow(
        str(payload["version"]), str(payload["phase"]), str(payload["graph_hash"]),
        int(payload["window_index"]), tuple(payload["slot_ids"]), len(payload["edges"]),
        hashlib.sha256(raw.encode()).hexdigest(), raw,
    )


def _result_from_payload(payload: Mapping[str, Any]) -> WindowScoreResult:
    return WindowScoreResult(
        str(payload["version"]), str(payload["graph_hash"]), str(payload["window_hash"]),
        str(payload["request_hash"]), str(payload["numeric_response_hash"]),
        str(payload["audit_response_hash"]), _canonical(payload),
    )


def _partial_head(db, job_id: str):
    return db.execute(f"SELECT * FROM {HEADS} WHERE job_id=?", (job_id,)).fetchone()


def _validated_retry_limit(attempts: Sequence[Any], requested_limit: int) -> int:
    frozen = {int(item.transport_retry_limit) for item in attempts}
    if len(frozen) > 1:
        raise Stage6WorkerError(
            "exam_stage6_hash_mismatch", "retry_policy_window_drift",
            "恢复窗口返回了不一致的冻结 transport retry 上限",
        )
    if frozen:
        value = next(iter(frozen))
        if value != requested_limit:
            raise Stage6WorkerError(
                "exam_stage6_contract_conflict", "retry_policy_parameter_drift",
                "本次 worker 参数与任务已冻结的 transport retry 上限不一致",
            )
        return value
    return requested_limit


def _stored_attempts(db, job_id: str) -> dict[str, Mapping[str, Any]]:
    rows = db.execute(
        f"SELECT w.payload_json,q.request_json,r.outcome_status,r.terminal_reason,"
        f"r.parsed_scores_json,r.rejected_edges_json FROM {WINDOWS} w "
        f"JOIN {REQUESTS} q ON q.job_id=w.job_id AND q.window_hash=w.window_hash "
        f"LEFT JOIN {RESPONSES} r ON r.job_id=w.job_id AND r.window_hash=w.window_hash "
        f"WHERE w.job_id=?",
        (job_id,),
    )
    values: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        window = json.loads(row["payload_json"])
        values[_window_from_payload(window).window_hash] = {
            "window": window,
            "request": json.loads(row["request_json"]),
            "status": str(row["outcome_status"] or "pending"),
            "terminal_reason": row["terminal_reason"],
            "result": json.loads(row["parsed_scores_json"]) if row["parsed_scores_json"] else None,
            "rejected_edges": json.loads(row["rejected_edges_json"]) if row["rejected_edges_json"] else [],
        }
    return values


def _callback_once(
    callback: ScoreCallback,
    request_json: str,
    *,
    schema_correction_reason: str | None = None,
) -> str:
    request = json.loads(request_json)
    correction = getattr(callback, "correct_schema", None)
    if schema_correction_reason is not None and callable(correction):
        value = correction(request, schema_correction_reason)
    else:
        value = callback(request)
    if isinstance(value, Mapping):
        return _canonical(value)
    if isinstance(value, str):
        return value
    raise Stage6ContractError("exam_stage6_response_invalid", "callback_result_invalid", "评分 callback 返回值无效")


def _schema_retry_reason(error: Stage6ContractError) -> str:
    """Project a parser failure into the repository's finite safe ledger."""

    if error.reason == "json_invalid":
        return "schema_invalid_json"
    if error.reason == "forbidden_decision_field":
        return "schema_forbidden_field"
    if error.reason in {
        "request_binding_invalid",
        "request_hash_mismatch",
        "response_shape_invalid",
        "score_edge_invalid",
        "score_edges_incomplete",
    }:
        return "schema_identity_mismatch"
    if error.reason == "forbidden_decision_field":
        return "schema_forbidden_field"
    return "schema_validation_failed"


def _run_pending_callbacks(
    pending: Sequence[tuple[ScoringWindow, str]], callback: ScoreCallback, concurrency: int,
    *, transport_retries_used: int = 0, schema_corrections_used: int = 0,
    transport_retry_limit: int = 2,
    authorize_retry: Callable[[str, str, str], None] | None = None,
    persist_outcome: Callable[[ScoringWindow, str, str | None, str | None], None] | None = None,
) -> dict[str, tuple[str | None, str | None]]:
    """Settle each completed future on the caller thread, with global caps."""

    def invoke(item: tuple[ScoringWindow, str]):
        window, request_json = item
        try:
            return window, request_json, _callback_once(callback, request_json), None
        except (JobCancelled, JobLeaseLost):
            raise
        except Exception as exc:  # provider/transport boundary is intentionally injected
            return window, request_json, None, exc

    results: dict[str, tuple[str | None, str | None]] = {}
    transport_retries = transport_retries_used
    correction_calls = schema_corrections_used

    def settle(window: ScoringWindow, request_json: str, raw: str | None, error: Exception | None) -> None:
        nonlocal transport_retries, correction_calls
        while True:
            if error is not None:
                if transport_retries >= transport_retry_limit:
                    outcome = (None, "transport_exhausted")
                    break
                if authorize_retry is not None:
                    authorize_retry("transport_retry", window.window_hash, "unavailable")
                transport_retries += 1
                try:
                    raw, error = _callback_once(callback, request_json), None
                except (JobCancelled, JobLeaseLost):
                    raise
                except Exception as exc:
                    raw, error = None, exc
                continue
            try:
                parse_scoring_response(str(raw), request_json=request_json, window=window)
                outcome = (str(raw), None)
                break
            except Stage6ContractError as exc:
                if correction_calls >= 2:
                    outcome = (None, "schema_correction_exhausted")
                    break
                correction_reason = _schema_retry_reason(exc)
                if authorize_retry is not None:
                    authorize_retry(
                        "schema_correction",
                        window.window_hash,
                        correction_reason,
                    )
                correction_calls += 1
                try:
                    raw, error = _callback_once(
                        callback,
                        request_json,
                        schema_correction_reason=correction_reason,
                    ), None
                except (JobCancelled, JobLeaseLost):
                    raise
                except Exception as exc:
                    raw, error = None, exc
        results[window.window_hash] = outcome
        if persist_outcome is not None:
            persist_outcome(window, request_json, outcome[0], outcome[1])

    if concurrency == 1 or len(pending) < 2:
        for item in pending:
            settle(*invoke(item))
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="stage6-score") as pool:
            futures = [pool.submit(invoke, item) for item in pending]
            for future in as_completed(futures):
                settle(*future.result())
    return results


def _score_windows(
    db, *, windows: Sequence[ScoringWindow], material, callback: ScoreCallback | None,
    profile_hash: str, projection_identity_hash: str, prompt_version: str, model_id: str,
    provider_capability: object, actor: Mapping[str, Any], hook: StepHook | None,
    transport_retry_limit: int,
) -> tuple[tuple[WindowScoreResult, ...], tuple[Mapping[str, Any], ...]]:
    # This read-only recovery also fences cancellation and worker attempt.
    recovered_before = recover_scoring_attempts(db, **actor)
    frozen_retry_limit = _validated_retry_limit(recovered_before, transport_retry_limit)
    stored = _stored_attempts(db, str(actor["job_id"]))
    requests: dict[str, str] = {}
    pending: list[tuple[ScoringWindow, str]] = []
    head = _partial_head(db, str(actor["job_id"]))
    for window in windows:
        request_json = render_scoring_request(
            window=window, material_receipt=material, profile_hash=profile_hash,
            projection_identity_hash=projection_identity_hash,
            prompt_version=prompt_version, model_id=model_id,
        )
        requests[window.window_hash] = request_json
        record = stored.get(window.window_hash)
        if record is None:
            if callback is None:
                raise Stage6WorkerError("exam_stage6_adapter_pending", "scoring_callback_pending", "阶段 6 评分 callback 尚未注入", status_code=422)
            persist_scoring_request(
                db, window=window, material_hash=material.material_hash, request_json=request_json,
                expected_revision=int(head["revision"]), transport_retry_limit=frozen_retry_limit, **actor,
            )
            # Repository writes return a typed Stage6Head while this loop uses
            # the SQLite row interface.  Re-read after every revision advance
            # so a multi-window paper never mixes the two representations.
            head = _partial_head(db, str(actor["job_id"]))
            _step(hook, "request")
            stored = _stored_attempts(db, str(actor["job_id"]))
            record = stored[window.window_hash]
        elif _canonical(record["request"]) != request_json:
            raise Stage6WorkerError("exam_stage6_hash_mismatch", "frozen_request_drift", "恢复请求与确定性冻结请求不一致")
        if record["status"] == "pending":
            if callback is None:
                raise Stage6WorkerError("exam_stage6_adapter_pending", "scoring_callback_pending", "阶段 6 评分 callback 尚未注入", status_code=422)
            pending.append((window, request_json))

    attempts = recover_scoring_attempts(db, **actor)
    frozen_retry_limit = _validated_retry_limit(attempts, transport_retry_limit)

    def authorize(kind: str, window_hash: str, reason_code: str) -> None:
        current = _partial_head(db, str(actor["job_id"]))
        append_scoring_retry_event(
            db, window_hash=window_hash, event_kind=kind, reason_code=reason_code,
            expected_revision=int(current["revision"]), **actor,
        )
        _step(hook, "retry_event")

    def persist_outcome(window: ScoringWindow, request_json: str, response_json: str | None, terminal: str | None) -> None:
        current = _partial_head(db, str(actor["job_id"]))
        if terminal is None:
            persist_scoring_window(
                db, window=window, material_hash=material.material_hash,
                request_json=request_json, response_json=str(response_json),
                expected_revision=int(current["revision"]), **actor,
            )
        else:
            persist_scoring_terminal_failure(
                db, window_hash=window.window_hash, reason=terminal,
                expected_revision=int(current["revision"]), **actor,
            )
        _step(hook, "response")

    _run_pending_callbacks(
        pending, callback, provider_concurrency(provider_capability),
        transport_retries_used=(attempts[0].transport_retry_count if attempts else 0),
        schema_corrections_used=(attempts[0].schema_correction_count if attempts else 0),
        transport_retry_limit=frozen_retry_limit, authorize_retry=authorize,
        persist_outcome=persist_outcome,
    ) if pending and callback else {}

    stored = _stored_attempts(db, str(actor["job_id"]))
    results: list[WindowScoreResult] = []
    failures: list[Mapping[str, Any]] = []
    for window in windows:
        record = stored.get(window.window_hash)
        if record is None or record["status"] == "pending":
            raise Stage6WorkerError("exam_stage6_incomplete", "window_outcome_missing", "评分窗口尚无终态")
        if record["status"] == "succeeded":
            results.append(_result_from_payload(record["result"]))
        else:
            failures.append({"window_hash": window.window_hash, "reason": record["terminal_reason"], "rejected_edges": record["rejected_edges"]})
    return tuple(results), tuple(failures)


def _append_missing_rejections(db, graph_hash: str, failures: Sequence[Mapping[str, Any]], actor: Mapping[str, Any]):
    head_row = _partial_head(db, str(actor["job_id"]))
    current = None if int(head_row["ledger_entry_count"]) == 0 else RejectionLedgerHead(
        REJECTION_LEDGER_VERSION, graph_hash, int(head_row["ledger_entry_count"]), str(head_row["ledger_head_hash"]),
    )
    existing = set()
    rows = db.execute(
        "SELECT payload_json FROM exam_generation_stage6_rejection_ledger_entries WHERE job_id=? AND graph_hash=? ORDER BY sequence",
        (actor["job_id"], graph_hash),
    )
    for row in rows:
        existing.update((x["slot_id"], x["candidate_id"]) for x in json.loads(row[0])["rejected_edges"])
    for failure in failures:
        edges = [dict(x) for x in failure["rejected_edges"] if (x["slot_id"], x["candidate_id"]) not in existing]
        if not edges:
            continue
        new_head, entry = append_rejection(head=current, graph_hash=graph_hash, rejected_edges=edges, reason_code=str(failure["reason"]))
        append_rejection_ledger(db, new_head=new_head, entry=entry, expected_revision=int(head_row["revision"]), **actor)
        head_row = _partial_head(db, str(actor["job_id"]))
        current = new_head
        existing.update((x["slot_id"], x["candidate_id"]) for x in edges)
    return _partial_head(db, str(actor["job_id"])), current, tuple({"slot_id": s, "candidate_id": c} for s, c in sorted(existing))


def _checkpoint(db, *, actor: Mapping[str, Any], state5, material, score, selection_hash: str, durations: Mapping[str, int]):
    job_id = str(actor["job_id"])
    context = json.loads(db.execute(f"SELECT payload_json FROM {ROOTS} WHERE job_id=?", (job_id,)).fetchone()[0])
    stage3 = db.execute(
        "SELECT stage3_contract_hash,generation_request_hash FROM exam_generation_stage3_contract_receipts WHERE job_id=?", (job_id,),
    ).fetchone()
    head = _partial_head(db, job_id)
    rows = list(db.execute(f"SELECT phase,window_hash FROM {WINDOWS} WHERE job_id=?", (job_id,)))
    expanded = state5.active_graph.graph_hash if state5.active_graph.graph_mode == "expanded" else None
    bindings = {
        "single_paper_contract_version": context["single_paper_contract_version"],
        "single_paper_contract_hash": context["single_paper_contract_hash"],
        "request_hash": stage3["generation_request_hash"], "stage3_contract_hash": stage3["stage3_contract_hash"],
        "profile_hash": state5.contract.to_dict()["profile_hash"],
        "projection_identity_hash": state5.contract.projection_identity_hash,
        "candidate_facts_hash": state5.contract.candidate_facts_hash,
        "recent_snapshot_hash": context["recent_snapshot_hash"], "constraint_hash": state5.contract.constraint_hash,
        "question_count": state5.contract.question_count, "initial_graph_hash": state5.initial_graph.graph_hash,
        "expanded_graph_hash": expanded, "material_receipt_hash": material.material_hash,
        "initial_window_hashes": sorted(str(x["window_hash"]) for x in rows if x["phase"] == "initial"),
        "expansion_window_hashes": sorted(str(x["window_hash"]) for x in rows if x["phase"] == "expansion"),
        "score_receipt_hash": score.score_set_hash, "score_audit_hash": score.audit_hash,
        "rejection_ledger_version": REJECTION_LEDGER_VERSION,
        "rejection_ledger_head_hash": str(head["ledger_head_hash"]),
        "rejection_ledger_entry_count": int(head["ledger_entry_count"]), "selection_hash": selection_hash,
        "time_policy_version": context["time_policy_version"], "checkpoint_stage": "stage6_scored",
        "pre_adaptation_hash": None, "final_exam_hash": None,
        "stage7_pre_receipt_hash": None, "stage7_final_receipt_hash": None,
        "stage_durations_ms": dict(durations),
    }
    return build_checkpoint_v3(bindings=bindings)


def _run_legacy_stage6_scoring_fixture_worker(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    score_callback: ScoreCallback | None, prompt_version: str, model_id: str,
    provider_capability: object = None, stage_hook: StepHook | None = None,
    stage_durations_ms: Mapping[str, int] | None = None,
    transport_retry_limit: int = 2,
    scoring_protocol: str = "legacy_score_read_only_v2",
    rubric_callback: ScoreCallback | None = None,
    utility_policy: Mapping[str, Any] | None = None,
) -> Stage6WorkerResult:
    """Provider-free fixture engine for the retired legacy write protocol.

    Production dependencies never reference this private helper.  It remains
    only so the historical score/retry/repository contracts can be exercised
    against disposable test databases while the public worker is read-only.
    """

    if scoring_protocol == "candidate_base_rubric_v1":
        # The pure R2-CAL runner is available below, but production persistence
        # and checkpoint recovery are deliberately not activated until an E3
        # utility policy is promoted.  Fail before opening the DB or invoking
        # either callback; importantly, do not fall back to legacy score/
        # penalty semantics for a request that explicitly selected the new
        # protocol.
        if rubric_callback is None or utility_policy is None:
            raise Stage6WorkerError(
                "exam_stage6_candidate_base_blocked",
                "candidate_base_policy_missing",
                "候选级 rubric 尚无已批准 utility policy",
                status_code=422,
            )
        try:
            validate_utility_policy(utility_policy)
        except CandidateBaseRubricError as exc:
            raise Stage6WorkerError(
                "exam_stage6_candidate_base_blocked",
                exc.reason,
                "候选级 utility policy 未通过显式合同校验",
                status_code=422,
            ) from exc
        raise Stage6WorkerError(
            "exam_stage6_candidate_base_blocked",
            "candidate_base_persistence_not_activated",
            "候选级 rubric 持久化/恢复链尚未完成 E3 激活",
            status_code=422,
        )
    if scoring_protocol != "legacy_score_read_only_v2":
        raise Stage6WorkerError(
            "exam_stage6_contract_invalid",
            "scoring_protocol_unknown",
            "阶段 6 评分协议标识无效",
            status_code=422,
        )

    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}
    if type(transport_retry_limit) is not int or not 0 <= transport_retry_limit <= 2:
        raise Stage6WorkerError("exam_stage6_contract_invalid", "transport_retry_limit_invalid", "阶段 6 transport retry 上限必须在 0..2")
    try:
        closed = recover_stage6_state(db, **actor)
        return Stage6WorkerResult("completed", "exam_stage6_completed", "stage6_scored", 200, closed.head.active_graph_hash or "", closed.head.score_set_hash, closed.head.selection_hash, closed.head.checkpoint_hash)
    except Stage6RepositoryError as exc:
        if exc.reason not in {"stage6_head_missing", "closed_state_incomplete"}:
            raise

    state5 = recover_stage5_state(db, **actor)
    if state5.initial_graph is None or state5.active_graph is None:
        raise Stage6WorkerError("exam_stage6_incomplete", "stage5_graph_missing", "阶段 5 候选图尚未冻结", status_code=422)
    stage4_row = db.execute(
        "SELECT facts_payload_json FROM exam_generation_stage4_fact_receipts WHERE job_id=?", (job_id,),
    ).fetchone()
    if stage4_row is None:
        raise Stage6WorkerError("exam_stage6_incomplete", "stage4_facts_missing", "阶段 4 候选事实尚未冻结", status_code=422)
    stage4 = json.loads(stage4_row[0])
    contract_payload = state5.contract.to_dict()
    profile_hash = str(contract_payload["profile_hash"])

    initial_material = build_material_preheat_receipt(
        graph=state5.initial_graph, stage4=stage4, question_count=state5.contract.question_count,
    )
    head = _partial_head(db, job_id)
    if head is None:
        head = persist_material_receipt(db, job_id=job_id, receipt=initial_material, worker_id=worker_id, lease_attempt=lease_attempt)
        _step(stage_hook, "material")
    initial_windows = build_scoring_windows(graph=state5.initial_graph, question_count=state5.contract.question_count)
    initial_results, initial_failures = _score_windows(
        db, windows=initial_windows, material=initial_material, callback=score_callback,
        profile_hash=profile_hash, projection_identity_hash=state5.contract.projection_identity_hash,
        prompt_version=prompt_version, model_id=model_id, provider_capability=provider_capability,
        actor=actor, hook=stage_hook, transport_retry_limit=transport_retry_limit,
    )
    if initial_failures:
        failure = initial_failures[0]
        raise Stage6WorkerError("exam_stage6_scoring_failed", str(failure["reason"]), "初始评分窗口终止失败", status_code=502)
    initial_score = build_score_receipt(graph=state5.initial_graph, parsed_windows=initial_results)
    head = _partial_head(db, job_id)
    if head["active_graph_hash"] == state5.initial_graph.graph_hash and head["score_set_hash"] is None:
        head = persist_score_receipt(
            db,
            receipt=initial_score,
            expected_revision=int(head["revision"]),
            _legacy_fixture_capability=_LEGACY_SCORE_FIXTURE_WRITE_CAPABILITY,
            **actor,
        )
        _step(stage_hook, "score")

    score = initial_score
    material = initial_material
    active_results = list(initial_results)
    if state5.active_graph.graph_mode == "expanded":
        witness = locate_candidate_graph_deficit(state5.contract, state5.initial_graph)
        if witness is None:
            raise Stage6WorkerError("exam_stage6_contract_conflict", "expansion_witness_missing", "扩展图缺少可重算的初始不可行见证")
        material = build_material_preheat_receipt(
            graph=state5.active_graph, base_graph=state5.initial_graph, stage4=stage4,
            question_count=state5.contract.question_count,
        )
        head = _partial_head(db, job_id)
        if head["active_graph_hash"] == state5.initial_graph.graph_hash:
            head = persist_material_receipt(db, receipt=material, expected_revision=int(head["revision"]), **actor)
            _step(stage_hook, "material")
        affected = tuple(state5.active_graph.to_dict().get("expanded_slot_ids") or witness["deficient_slot_ids"])
        expansion_windows = build_scoring_windows(
            graph=state5.active_graph, base_graph=state5.initial_graph,
            question_count=state5.contract.question_count, phase="expansion", affected_slot_ids=affected,
        )
        expansion_results, expansion_failures = _score_windows(
            db, windows=expansion_windows, material=material, callback=score_callback,
            profile_hash=profile_hash, projection_identity_hash=state5.contract.projection_identity_hash,
            prompt_version=prompt_version, model_id=model_id, provider_capability=provider_capability,
            actor=actor, hook=stage_hook, transport_retry_limit=transport_retry_limit,
        )
        head, ledger, rejected = _append_missing_rejections(db, state5.active_graph.graph_hash, expansion_failures, actor)
        active_results.extend(expansion_results)
        score = build_score_receipt(
            graph=state5.active_graph, base_graph=state5.initial_graph,
            parsed_windows=active_results, rejected_edges=rejected,
            rejection_ledger_head_hash=ledger.head_hash if ledger else ZERO_HASH,
        )
        if head["score_set_hash"] is None:
            head = persist_score_receipt(
                db,
                receipt=score,
                expected_revision=int(head["revision"]),
                _legacy_fixture_capability=_LEGACY_SCORE_FIXTURE_WRITE_CAPABILITY,
                **actor,
            )
            _step(stage_hook, "score")

    state5 = recover_stage5_state(db, **actor)
    selection = state5.selection
    if selection is not None and (selection.graph_hash != state5.active_graph.graph_hash or selection.score_identity_hash != score.score_set_hash):
        # The only legal mismatch here is the base-selection -> expanded-graph
        # transition; it must close through an atomic repair receipt below.
        if not (state5.active_graph.graph_mode == "expanded" and selection.graph_hash == state5.initial_graph.graph_hash):
            raise Stage6WorkerError("exam_stage6_contract_conflict", "existing_selection_score_drift", "既有选择未绑定当前评分身份")
    try:
        if state5.active_graph.graph_mode == "expanded" and (selection is None or selection.graph_hash != state5.active_graph.graph_hash):
            witness = locate_candidate_graph_deficit(state5.contract, state5.initial_graph)
            repair = repair_global_selection(
                state5.contract, state5.initial_graph, score_receipt=score.to_stage5_dict(),
                witness=witness, expanded_graph=state5.active_graph,
            )
            persist_repair_result(db, result=repair, expected_revision=state5.head.revision, **actor)
            selection = repair.selection
        elif selection is None:
            selection = solve_global_selection(state5.contract, state5.active_graph, score_receipt=score.to_stage5_dict())
            persist_selection(db, selection=selection, expected_revision=state5.head.revision, **actor)
    except Stage5InfeasibleError as exc:
        persist_infeasible_witness(db, witness=exc.witness, **actor)
        return Stage6WorkerResult("infeasible", exc.code, exc.reason, exc.status_code, state5.active_graph.graph_hash, score.score_set_hash, witness=exc.witness)
    _step(stage_hook, "selection")

    head = _partial_head(db, job_id)
    if head["selection_hash"] is None:
        head = persist_selection_reference(db, selection_hash=selection.selection_hash, expected_revision=int(head["revision"]), **actor)
    _step(stage_hook, "selection_reference")
    durations = stage_durations_ms or {
        "material_preheat_ms": 0, "initial_scoring_ms": 0, "expansion_scoring_ms": 0,
        "structural_correction_ms": 0, "local_solve_ms": 0,
    }
    state5 = recover_stage5_state(db, **actor)
    checkpoint = _checkpoint(db, actor=actor, state5=state5, material=material, score=score, selection_hash=selection.selection_hash, durations=durations)
    head = _partial_head(db, job_id)
    if head["checkpoint_hash"] is None:
        head = persist_checkpoint_v3(db, checkpoint=checkpoint, expected_revision=int(head["revision"]), **actor)
    _step(stage_hook, "checkpoint")
    return Stage6WorkerResult("completed", "exam_stage6_completed", "stage6_scored", 200, state5.active_graph.graph_hash, score.score_set_hash, selection.selection_hash, checkpoint.checkpoint_hash)


def run_stage6_scoring_worker(
    db, *, job_id: str, worker_id: str, lease_attempt: int,
    score_callback: ScoreCallback | None, prompt_version: str, model_id: str,
    provider_capability: object = None, stage_hook: StepHook | None = None,
    stage_durations_ms: Mapping[str, int] | None = None,
    transport_retry_limit: int = 2,
    scoring_protocol: str = "legacy_score_read_only_v2",
    rubric_callback: ScoreCallback | None = None,
    utility_policy: Mapping[str, Any] | None = None,
) -> Stage6WorkerResult:
    """Read completed legacy Stage 6 state or fail before every new write.

    Candidate-base requests retain their existing E3/persistence fail-closed
    boundary.  The legacy identifier is now literal: only a fully closed
    historical receipt can be recovered; missing or partial state cannot be
    advanced and neither callback is invoked.
    """

    if scoring_protocol != "legacy_score_read_only_v2":
        return _run_legacy_stage6_scoring_fixture_worker(
            db,
            job_id=job_id,
            worker_id=worker_id,
            lease_attempt=lease_attempt,
            score_callback=score_callback,
            prompt_version=prompt_version,
            model_id=model_id,
            provider_capability=provider_capability,
            stage_hook=stage_hook,
            stage_durations_ms=stage_durations_ms,
            transport_retry_limit=transport_retry_limit,
            scoring_protocol=scoring_protocol,
            rubric_callback=rubric_callback,
            utility_policy=utility_policy,
        )

    actor = {"job_id": job_id, "worker_id": worker_id, "lease_attempt": lease_attempt}
    head = db.execute(
        f"SELECT score_set_hash,selection_hash,checkpoint_hash FROM {HEADS} "
        "WHERE job_id=?",
        (job_id,),
    ).fetchone()
    if (
        head is None
        or head["score_set_hash"] is None
        or head["selection_hash"] is None
        or head["checkpoint_hash"] is None
    ):
        raise Stage6WorkerError(
            "exam_stage6_legacy_write_disabled",
            "legacy_score_write_disabled",
            "旧版 score/penalty 只允许历史完整回执只读恢复，新写入已关闭",
            status_code=422,
        )
    try:
        closed = recover_stage6_state(db, **actor)
    except Stage6RepositoryError as exc:
        if exc.reason not in {"stage6_head_missing", "closed_state_incomplete"}:
            raise
        raise Stage6WorkerError(
            "exam_stage6_legacy_write_disabled",
            "legacy_score_write_disabled",
            "旧版 score/penalty 只允许历史完整回执只读恢复，新写入已关闭",
            status_code=422,
        ) from exc
    return Stage6WorkerResult(
        "completed",
        "exam_stage6_completed",
        "stage6_scored_read_only",
        200,
        closed.head.active_graph_hash or "",
        closed.head.score_set_hash,
        closed.head.selection_hash,
        closed.head.checkpoint_hash,
    )


def run_candidate_base_rubric_calibration(
    *, windows: Sequence[Any], material_receipt: Any, rubric_callback: ScoreCallback,
    visibility_receipt: Mapping[str, Any] | None = None,
    utility_policy: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Low-level, non-persistent candidate-base evaluation compatibility seam.

    It never writes Stage-6 tables, publication state, memory, rejection
    ledgers or selection checkpoints, but it does not implement R2-CAL batch
    identity, global cross-window de-duplication, physical-call budgeting or
    recovery.  Provider-free R2-CAL callers must use the separately versioned
    ``exam_generation_r2_calibration`` manifest/plan/artifact contracts.
    A complete visibility receipt is required before any callback is invoked.
    Supplying a policy derives a local calibration receipt; omitting it leaves
    utility explicitly blocked.  The normal production worker remains on its
    separately versioned legacy/released path until the policy is promoted.
    """

    return evaluate_candidate_base_rubric_windows(
        windows,
        material_receipt,
        rubric_callback,
        visibility_receipt=visibility_receipt,
        utility_policy=utility_policy,
    )


__all__ = ["Stage6WorkerError", "Stage6WorkerResult", "run_stage6_scoring_worker", "run_candidate_base_rubric_calibration"]
