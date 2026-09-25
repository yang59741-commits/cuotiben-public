"""Durable, single-lane orchestration for permanent-card dispatch.

The loop remains Provider-neutral.  Production installs an adapter only behind
an explicit default-off gate; tests can inject the same runtime contract.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .material_understanding import MAX_MISSING_FILL_ATTEMPTS
from .semantic_index_checkpoints import (
    SemanticRunItemLease,
    activate_run,
    cancel_run,
    claim_next_item,
    degrade_run_to_single_slot,
    heartbeat_item,
    pause_run,
    recover_expired_leases,
    release_unstarted_leases,
)
from .semantic_index_dispatch import (
    SemanticCardInvoker,
    SemanticDispatchResult,
    SemanticTokenCostEstimator,
    execute_synthetic_semantic_item,
)
from .semantic_index_sources import SemanticSourceBundle
from .semantic_index_tokens import (
    SemanticTokenLedgerError,
    extend_token_limit_after_review,
    hard_stop_if_minimum_cannot_fit,
)


class SemanticIndexLoopError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticDispatchRuntime:
    invoke_card: SemanticCardInvoker
    estimate_cost: SemanticTokenCostEstimator
    extension_review: Callable[[str, int, int], bool]
    lease_seconds: int = 300
    retry_delay_seconds: int = 60
    max_provider_attempts_per_item: int = MAX_MISSING_FILL_ATTEMPTS + 1
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

    def __post_init__(self) -> None:
        if not callable(self.invoke_card) or not callable(self.estimate_cost):
            raise SemanticIndexLoopError(
                "SYNTHETIC_RUNTIME_INVALID", "synthetic runtime 缺少调用器或费用估算器"
            )
        if not callable(self.extension_review) or not callable(self.now):
            raise SemanticIndexLoopError(
                "SYNTHETIC_RUNTIME_INVALID", "synthetic runtime 回调无效"
            )
        if isinstance(self.lease_seconds, bool) or self.lease_seconds < 1:
            raise SemanticIndexLoopError(
                "SYNTHETIC_RUNTIME_INVALID", "item lease 必须是正整数"
            )
        if isinstance(self.retry_delay_seconds, bool) or self.retry_delay_seconds < 1:
            raise SemanticIndexLoopError(
                "SYNTHETIC_RUNTIME_INVALID", "重试延迟必须是正整数"
            )
        if (
            isinstance(self.max_provider_attempts_per_item, bool)
            or not isinstance(self.max_provider_attempts_per_item, int)
            or self.max_provider_attempts_per_item < 1
            or self.max_provider_attempts_per_item > MAX_MISSING_FILL_ATTEMPTS + 1
        ):
            raise SemanticIndexLoopError(
                "SYNTHETIC_RUNTIME_INVALID",
                "每题 Provider 请求上限超出支持范围",
            )


# Compatibility name retained for B7c tests and explicit synthetic injection.
SyntheticSemanticRuntime = SemanticDispatchRuntime


def _timestamp(runtime: SemanticDispatchRuntime) -> tuple[datetime, str]:
    now = runtime.now()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise SemanticIndexLoopError(
            "SYNTHETIC_RUNTIME_INVALID", "runtime 时钟必须返回带时区 datetime"
        )
    return now, now.isoformat()


def _lease_from_row(row: Any, *, fallback_owner: str, timestamp: str) -> SemanticRunItemLease:
    current_attempt = int(row["current_attempt"])
    item_attempt = int(row["item_attempt"] or current_attempt)
    current = item_attempt == current_attempt and str(row["item_status"]) == "leased"
    owner = str(row["lease_owner"] or fallback_owner) if current else fallback_owner
    expires = str(row["lease_expires_at"] or timestamp) if current else timestamp
    return SemanticRunItemLease(
        run_id=str(row["run_id"]),
        semantic_question_id=int(row["semantic_question_id"]),
        source_kind=str(row["source_kind"]),
        candidate_id=str(row["candidate_id"]),
        expected_content_hash=str(row["expected_content_hash"]),
        change_kind=str(row["change_kind"]),
        lease_owner=owner,
        lease_expires_at=expires,
        attempt_count=item_attempt,
        run_source_snapshot=str(row["source_snapshot"]),
    )


def _recoverable_lease(
    db: Any, *, run_id: str, fallback_owner: str, timestamp: str
) -> SemanticRunItemLease | None:
    # Provider attempts always take priority over new claims.  This is the
    # durable fence that prevents crash recovery from starting another item.
    concurrency = int(
        db.execute(
            "SELECT effective_concurrency FROM semantic_index_runs WHERE id=?",
            (run_id,),
        ).fetchone()[0]
    )
    row = db.execute(
        "SELECT pa.run_id,pa.semantic_question_id,pa.item_attempt,"
        "pa.expected_content_hash,ri.status AS item_status,"
        "ri.attempt_count AS current_attempt,ri.change_kind,ri.lease_owner,"
        "ri.lease_expires_at,q.source_kind,q.candidate_id,r.source_snapshot "
        "FROM semantic_index_provider_attempts pa "
        "JOIN semantic_index_run_items ri ON ri.run_id=pa.run_id "
        "AND ri.semantic_question_id=pa.semantic_question_id "
        "JOIN semantic_questions q ON q.id=pa.semantic_question_id "
        "JOIN semantic_index_runs r ON r.id=pa.run_id "
        "WHERE pa.run_id=? AND (?=1 OR ri.lease_owner=?) AND pa.status IN "
        "('reserved','dispatching','response_valid','response_invalid',"
        "'provider_failed','outcome_unknown') "
        "ORDER BY pa.created_at,pa.provider_attempt LIMIT 1",
        (run_id, concurrency, fallback_owner),
    ).fetchone()
    if row is not None:
        return _lease_from_row(row, fallback_owner=fallback_owner, timestamp=timestamp)
    row = db.execute(
        "SELECT ri.run_id,ri.semantic_question_id,NULL AS item_attempt,"
        "ri.expected_content_hash,ri.status AS item_status,"
        "ri.attempt_count AS current_attempt,ri.change_kind,ri.lease_owner,"
        "ri.lease_expires_at,q.source_kind,q.candidate_id,r.source_snapshot "
        "FROM semantic_index_run_items ri "
        "JOIN semantic_questions q ON q.id=ri.semantic_question_id "
        "JOIN semantic_index_runs r ON r.id=ri.run_id "
        "WHERE ri.run_id=? AND ri.status='leased' "
        "AND (?=1 OR ri.lease_owner=?) "
        "ORDER BY ri.semantic_question_id LIMIT 1",
        (run_id, concurrency, fallback_owner),
    ).fetchone()
    return None if row is None else _lease_from_row(
        row, fallback_owner=fallback_owner, timestamp=timestamp
    )


def _run_state(db: Any, run_id: str) -> Any:
    row = db.execute(
        "SELECT status,pending_count,completed_count,failed_count,"
        "token_extension_index FROM semantic_index_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise SemanticIndexLoopError("RUN_NOT_FOUND", "semantic run 不存在")
    return row


def run_semantic_dispatch_loop(
    db: Any,
    *,
    run_id: str,
    bundle_loader: Callable[[], SemanticSourceBundle],
    runtime: SemanticDispatchRuntime,
    lease_owner: str,
    heartbeat: Callable[[], bool | None],
    cancel_requested: Callable[[], bool],
    progress: Callable[[int, int, str], None] | None = None,
    item_limit: int | None = None,
    activate_queued: bool = True,
) -> dict[str, Any]:
    """Drain one run serially, recovering durable attempts before every claim."""

    if not isinstance(runtime, SemanticDispatchRuntime):
        raise SemanticIndexLoopError(
            "SYNTHETIC_RUNTIME_INVALID", "synthetic runtime 类型无效"
        )
    if item_limit is not None and (isinstance(item_limit, bool) or item_limit < 1):
        raise SemanticIndexLoopError("VALIDATION", "item_limit 必须是正整数")
    state = _run_state(db, run_id)
    if str(state["status"]) == "queued" and activate_queued:
        _now, stamp = _timestamp(runtime)
        activate_run(db, run_id=run_id, timestamp=stamp)

    last: SemanticDispatchResult | None = None
    processed = 0
    while True:
        cancelled = bool(cancel_requested())
        # Fences a worker that lost the outer durable-job lease.  A returned
        # cancellation bit is handled at the safe between-items boundary.
        cancelled = bool(heartbeat()) or cancelled
        state = _run_state(db, run_id)
        status = str(state["status"])
        if status != "running":
            return {
                "run_id": run_id,
                "semantic_run_status": status,
                "phase": "dispatched",
                "completed_count": int(state["completed_count"]),
                "failed_count": int(state["failed_count"]),
                "last_item_status": None if last is None else last.status,
            }
        now, stamp = _timestamp(runtime)
        lease = _recoverable_lease(
            db, run_id=run_id, fallback_owner=lease_owner, timestamp=stamp
        )
        if lease is None and cancelled:
            pause_run(db, run_id=run_id, timestamp=stamp)
            continue
        if lease is None:
            lease = claim_next_item(
                db,
                run_id=run_id,
                lease_owner=lease_owner,
                lease_seconds=runtime.lease_seconds,
                timestamp=stamp,
            )
            if lease is None:
                waiting = int(
                    db.execute(
                        "SELECT COUNT(*) FROM semantic_index_run_items "
                        "WHERE run_id=? AND status NOT IN "
                        "('succeeded','failed','cancelled','skipped')",
                        (run_id,),
                    ).fetchone()[0]
                )
                if waiting:
                    if item_limit is not None:
                        return {
                            "run_id": run_id,
                            "semantic_run_status": status,
                            "phase": "slot_idle",
                            "completed_count": int(state["completed_count"]),
                            "failed_count": int(state["failed_count"]),
                            "last_item_status": None if last is None else last.status,
                        }
                    pause_run(db, run_id=run_id, timestamp=stamp)
                continue
        else:
            item = db.execute(
                "SELECT status,attempt_count FROM semantic_index_run_items "
                "WHERE run_id=? AND semantic_question_id=?",
                (lease.run_id, lease.semantic_question_id),
            ).fetchone()
            if (
                item is not None
                and str(item["status"]) == "leased"
                and int(item["attempt_count"]) == lease.attempt_count
            ):
                try:
                    lease_expiry = datetime.fromisoformat(lease.lease_expires_at)
                except ValueError as exc:
                    raise SemanticIndexLoopError(
                        "LEASE_CORRUPT", "run-item 租约时间无效"
                    ) from exc
                if lease_expiry <= now:
                    unresolved = db.execute(
                        "SELECT 1 FROM semantic_index_provider_attempts "
                        "WHERE run_id=? AND semantic_question_id=? AND item_attempt=? "
                        "AND status IN ('reserved','dispatching','response_valid',"
                        "'response_invalid','provider_failed','outcome_unknown')",
                        (lease.run_id, lease.semantic_question_id, lease.attempt_count),
                    ).fetchone()
                    if unresolved is None:
                        recover_expired_leases(db, run_id=run_id, timestamp=stamp)
                        continue
                else:
                    expires = heartbeat_item(
                        db,
                        lease=lease,
                        lease_seconds=runtime.lease_seconds,
                        timestamp=stamp,
                    )
                    lease = replace(lease, lease_expires_at=expires)
        retry_at = (now + timedelta(seconds=runtime.retry_delay_seconds)).isoformat()
        # Re-open the current last-good artifact before every item.  The B7b
        # verifier then checks the frozen identity and content hash.
        bundle = bundle_loader()
        try:
            last = execute_synthetic_semantic_item(
                db,
                bundle=bundle,
                lease=lease,
                invoke_card=runtime.invoke_card,
                estimate_cost=runtime.estimate_cost,
                timestamp=stamp,
                retry_available_at=retry_at,
                max_provider_attempts=runtime.max_provider_attempts_per_item,
                # Cancellation is honored only between items.  Once a
                # reservation exists the item is conservatively finalized.
                cancel_requested=None,
            )
        except SemanticTokenLedgerError as exc:
            requested = exc.requested_amount
            if requested is None:
                raise
            if exc.code == "TOKEN_EXTENSION_REQUIRED":
                row = _run_state(db, run_id)
                next_index = int(row["token_extension_index"]) + 1
                passed = bool(runtime.extension_review(run_id, next_index, requested))
                try:
                    extended = extend_token_limit_after_review(
                        db,
                        run_id=run_id,
                        extension_index=next_index,
                        review_passed=passed,
                        minimum_reservation=requested,
                        timestamp=stamp,
                    )
                except SemanticTokenLedgerError as extension_exc:
                    if item_limit is not None and extension_exc.code in {
                        "EXTENSION_NOT_READY",
                        "EXTENSION_CONFLICT",
                    }:
                        return {
                            "run_id": run_id,
                            "semantic_run_status": "running",
                            "phase": "token_barrier_wait",
                            "completed_count": int(state["completed_count"]),
                            "failed_count": int(state["failed_count"]),
                            "last_item_status": None,
                        }
                    raise
                if extended.status == "extended":
                    continue
            elif exc.code == "TOKEN_HARD_LIMIT":
                try:
                    hard_stop_if_minimum_cannot_fit(
                        db,
                        run_id=run_id,
                        minimum_reservation=requested,
                        timestamp=stamp,
                    )
                except SemanticTokenLedgerError as stop_exc:
                    if item_limit is not None and stop_exc.code == "TOKEN_REQUESTS_IN_FLIGHT":
                        return {
                            "run_id": run_id,
                            "semantic_run_status": "running",
                            "phase": "token_barrier_wait",
                            "completed_count": int(state["completed_count"]),
                            "failed_count": int(state["failed_count"]),
                            "last_item_status": None,
                        }
                    raise
            else:
                raise
            continue
        heartbeat()
        state = _run_state(db, run_id)
        if progress is not None:
            progress(
                int(state["completed_count"]),
                int(state["pending_count"]),
                f"永久材料卡：{last.candidate_id} / {last.status}",
            )
        processed += 1
        if item_limit is not None and processed >= item_limit:
            return {
                "run_id": run_id,
                "semantic_run_status": str(state["status"]),
                "phase": "batch_checkpointed",
                "completed_count": int(state["completed_count"]),
                "failed_count": int(state["failed_count"]),
                "last_item_status": last.status,
            }


def run_semantic_dispatch_coordinator(
    *,
    connection_factory: Callable[[], Any],
    run_id: str,
    bundle_loader: Callable[[], SemanticSourceBundle],
    runtime: SemanticDispatchRuntime,
    lease_owner_prefix: str,
    heartbeat: Callable[[], bool | None],
    cancel_requested: Callable[[], bool],
    yield_requested: Callable[[], bool],
    progress: Callable[[int, int, str], None] | None = None,
    resume_paused: bool = False,
) -> dict[str, Any]:
    """Drain bounded one-item batches with one or two durable lease slots."""

    control = connection_factory()
    try:
        state = _run_state(control, run_id)
        initial_status = str(state["status"])
        if initial_status == "queued" or (initial_status == "paused" and resume_paused):
            _now, stamp = _timestamp(runtime)
            activate_run(control, run_id=run_id, timestamp=stamp)
    finally:
        control.close()

    while True:
        control = connection_factory()
        try:
            row = control.execute(
                "SELECT status,pending_count,completed_count,failed_count,"
                "effective_concurrency,control_requested FROM semantic_index_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise SemanticIndexLoopError("RUN_NOT_FOUND", "semantic run 不存在")
            status = str(row["status"])
            requested_control = str(row["control_requested"] or "")
            should_cancel = requested_control == "cancel" or bool(cancel_requested())
            should_cancel = bool(heartbeat()) or should_cancel
            if should_cancel and status not in {"succeeded", "failed", "cancelled"}:
                _now, stamp = _timestamp(runtime)
                cancel_run(control, run_id=run_id, timestamp=stamp)
                continue
            if status != "running":
                return {
                    "run_id": run_id,
                    "semantic_run_status": status,
                    "phase": "dispatched",
                    "completed_count": int(row["completed_count"]),
                    "failed_count": int(row["failed_count"]),
                    "max_concurrency": int(row["effective_concurrency"]),
                }
            if requested_control == "pause" or bool(yield_requested()):
                _now, stamp = _timestamp(runtime)
                release_unstarted_leases(control, run_id=run_id, timestamp=stamp)
                pause_run(control, run_id=run_id, timestamp=stamp)
                continue
            concurrency = int(row["effective_concurrency"])
        finally:
            control.close()

        def run_slot(slot: int) -> dict[str, Any]:
            connection = connection_factory()
            try:
                return run_semantic_dispatch_loop(
                    connection,
                    run_id=run_id,
                    bundle_loader=bundle_loader,
                    runtime=runtime,
                    lease_owner=f"{lease_owner_prefix}:slot:{slot}",
                    heartbeat=lambda: False,
                    cancel_requested=lambda: False,
                    item_limit=1,
                    activate_queued=False,
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="semantic-card-slot"
        ) as executor:
            results = list(executor.map(run_slot, range(1, concurrency + 1)))

        control = connection_factory()
        try:
            if concurrency == 2 and any(
                result.get("last_item_status") == "retry_wait"
                for result in results
            ):
                _now, stamp = _timestamp(runtime)
                degrade_run_to_single_slot(
                    control,
                    run_id=run_id,
                    reason="provider_temporary_failure",
                    timestamp=stamp,
                )
            state = _run_state(control, run_id)
            if progress is not None:
                progress(
                    int(state["completed_count"]),
                    int(state["pending_count"]),
                    "永久材料卡批次已提交",
                )
            if all(
                result.get("phase") in {"slot_idle", "token_barrier_wait"}
                for result in results
            ):
                if any(
                    result.get("phase") == "token_barrier_wait" for result in results
                ):
                    continue
                _now, stamp = _timestamp(runtime)
                pause_run(control, run_id=run_id, timestamp=stamp)
        finally:
            control.close()


# Compatibility entry point retained for synthetic B7c callers.
run_synthetic_semantic_loop = run_semantic_dispatch_loop
