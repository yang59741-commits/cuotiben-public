"""Crash-conservative single-item semantic dispatch orchestration.

This module is provider-agnostic.  The caller injects both a synthetic invoker
and a worst-case Token cost estimator.  It deliberately supports one leased
item at a time and never creates workers or enables two-lane execution.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from ..transactions import immediate_transaction
from .semantic_index import (
    SEMANTIC_CARD_SCHEMA_VERSION,
    SemanticRunItemStatus,
    assert_run_item_transition,
    validate_semantic_card,
)
from .semantic_index_checkpoints import (
    SemanticRunItemLease,
    SourceVerification,
    checkpoint_cancelled_in_transaction,
    checkpoint_failure,
    checkpoint_failure_in_transaction,
    checkpoint_source_changed,
    checkpoint_source_changed_in_transaction,
    checkpoint_success_in_transaction,
    refresh_run_summary_in_transaction,
    verify_leased_sources,
)
from .semantic_index_material import (
    AmbiguousSemanticProviderError,
    MAX_MISSING_FILL_ATTEMPTS,
    PROMPT_VERSION,
    PermanentSemanticProviderError,
    SemanticCardInvoker,
    SemanticCardProviderRequest,
    SemanticCardProviderResponse,
    SemanticMaterialExecutionError,
    TemporarySemanticProviderError,
    build_permanent_card_request,
    candidate_for_lease,
    coerce_provider_response,
    hash_provider_request_id,
    parse_permanent_card_response,
)
from .semantic_index_provider_attempts import (
    SemanticProviderAttemptError,
    get_provider_attempt,
    insert_reserved_provider_attempt_in_transaction,
    mark_provider_attempt_abandoned_in_transaction,
    mark_provider_attempt_checkpointed_in_transaction,
    mark_provider_attempt_dispatching,
    mark_provider_outcome_unknown_in_transaction,
    record_invalid_provider_response,
    record_provider_failure,
    record_valid_provider_response,
)
from .semantic_index_sources import SemanticSourceBundle
from .semantic_index_tokens import (
    TokenReservationResult,
    reserve_tokens_in_transaction,
    settle_tokens,
    settle_tokens_in_transaction,
)


class SemanticDispatchError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticProviderTokenCost:
    estimated_prompt_tokens: int
    max_completion_tokens: int
    safety_margin: int


@dataclass(frozen=True, slots=True)
class SemanticDispatchResult:
    candidate_id: str
    status: str
    provider_attempts: int
    reservation_key: str | None = None
    card_version_id: int | None = None


SemanticTokenCostEstimator = Callable[
    [SemanticCardProviderRequest], SemanticProviderTokenCost
]
CancelCheck = Callable[[], bool]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _request_hashes(
    request: SemanticCardProviderRequest,
    cost: SemanticProviderTokenCost,
) -> tuple[str, str]:
    prompt_payload = {
        "messages": [dict(message) for message in request.messages],
    }
    prompt_hash = hashlib.sha256(
        _canonical_json(prompt_payload).encode("utf-8")
    ).hexdigest()
    fingerprint = hashlib.sha256(
        _canonical_json(
            {
                "version": 1,
                "candidate_id": request.candidate_id,
                "attempt": request.attempt,
                "targeted_fill": request.targeted_fill,
                "coverage_token_budget": request.coverage_token_budget,
                "prompt_hash": prompt_hash,
                "estimated_prompt_tokens": cost.estimated_prompt_tokens,
                "max_completion_tokens": cost.max_completion_tokens,
                "safety_margin": cost.safety_margin,
            }
        ).encode("utf-8")
    ).hexdigest()
    return prompt_hash, fingerprint


def _cost_for_request(
    estimate_cost: SemanticTokenCostEstimator,
    request: SemanticCardProviderRequest,
) -> SemanticProviderTokenCost:
    if not callable(estimate_cost):
        raise SemanticDispatchError(
            "TOKEN_COST_UNAVAILABLE", "未配置最坏情况 Token 费用估算器"
        )
    cost = estimate_cost(request)
    if not isinstance(cost, SemanticProviderTokenCost):
        raise SemanticDispatchError(
            "TOKEN_COST_INVALID", "Token 费用估算器返回类型无效"
        )
    return cost


def _reserve_attempt_pair(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    request: SemanticCardProviderRequest,
    cost: SemanticProviderTokenCost,
    prompt_hash: str,
    request_fingerprint: str,
    timestamp: str,
) -> tuple[TokenReservationResult, dict[str, Any]]:
    with immediate_transaction(db):
        reservation = reserve_tokens_in_transaction(
            db,
            lease=lease,
            provider_attempt=request.attempt,
            prompt_hash=prompt_hash,
            estimated_prompt_tokens=cost.estimated_prompt_tokens,
            max_completion_tokens=cost.max_completion_tokens,
            safety_margin=cost.safety_margin,
            timestamp=timestamp,
        )
        if reservation.state != "reserved":
            raise SemanticDispatchError(
                "RESERVATION_ALREADY_TERMINAL",
                "Provider attempt 不能绑定已经结算的 reservation",
            )
        attempt = insert_reserved_provider_attempt_in_transaction(
            db,
            reservation_key=reservation.reservation_key,
            run_id=lease.run_id,
            semantic_question_id=lease.semantic_question_id,
            item_attempt=lease.attempt_count,
            provider_attempt=request.attempt,
            expected_content_hash=lease.expected_content_hash,
            prompt_hash=prompt_hash,
            request_fingerprint=request_fingerprint,
            timestamp=timestamp,
        )
        return reservation, attempt


def _attempt_rows(db: Any, lease: SemanticRunItemLease) -> list[dict[str, Any]]:
    cursor = db.execute(
        "SELECT * FROM semantic_index_provider_attempts WHERE run_id=? "
        "AND semantic_question_id=? AND item_attempt=? ORDER BY provider_attempt",
        (lease.run_id, lease.semantic_question_id, lease.attempt_count),
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _item_state(db: Any, lease: SemanticRunItemLease) -> dict[str, Any] | None:
    cursor = db.execute(
        "SELECT status,attempt_count,card_version_id,error_code FROM "
        "semantic_index_run_items WHERE run_id=? AND semantic_question_id=?",
        (lease.run_id, lease.semantic_question_id),
    )
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        description[0]: row[index]
        for index, description in enumerate(cursor.description)
    }


def _has_terminal_token_event(db: Any, attempt: dict[str, Any]) -> bool:
    return db.execute(
        "SELECT 1 FROM semantic_index_token_events WHERE run_id=? "
        "AND reservation_key=? AND event_type IN "
        "('settlement','usage_unverified','reconciliation')",
        (attempt["run_id"], attempt["reservation_key"]),
    ).fetchone() is not None


def _result_from_terminal_item(
    lease: SemanticRunItemLease, item: dict[str, Any]
) -> SemanticDispatchResult:
    return SemanticDispatchResult(
        candidate_id=lease.candidate_id,
        status=str(item["status"]),
        provider_attempts=0,
        card_version_id=item["card_version_id"],
    )


def _finalize_success(
    db: Any,
    *,
    attempt: dict[str, Any],
    verification: SourceVerification,
    timestamp: str,
) -> SemanticDispatchResult:
    try:
        card_payload = json.loads(str(attempt["validated_card_json"]))
        card = validate_semantic_card(
            card_payload, expected_candidate_id=verification.lease.candidate_id
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticDispatchError(
            "JOURNAL_CARD_CORRUPT", "Provider attempt 的已验证材料卡损坏"
        ) from exc
    with immediate_transaction(db):
        stored = checkpoint_success_in_transaction(
            db,
            verification=verification,
            card=card,
            timestamp=timestamp,
            provider_request_id_hash=attempt["provider_request_id_hash"],
        )
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=(
                int(attempt["usage_total_tokens"])
                if int(attempt["usage_known"])
                else None
            ),
            timestamp=timestamp,
        )
        mark_provider_attempt_checkpointed_in_transaction(
            db,
            reservation_key=attempt["reservation_key"],
            timestamp=timestamp,
        )
        refresh_run_summary_in_transaction(db, attempt["run_id"], timestamp)
    return SemanticDispatchResult(
        verification.lease.candidate_id,
        "succeeded",
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
        stored.card_version_id,
    )


def _finalize_failure(
    db: Any,
    *,
    attempt: dict[str, Any],
    lease: SemanticRunItemLease,
    retryable: bool,
    timestamp: str,
    retry_available_at: str,
    error_code: str,
    error_summary: str,
) -> SemanticDispatchResult:
    with immediate_transaction(db):
        checkpoint_failure_in_transaction(
            db,
            lease=lease,
            retryable=retryable,
            timestamp=timestamp,
            available_at=retry_available_at if retryable else None,
            error_code=error_code,
            error_summary=error_summary,
        )
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=(
                int(attempt["usage_total_tokens"])
                if int(attempt["usage_known"])
                else None
            ),
            timestamp=timestamp,
        )
        refresh_run_summary_in_transaction(db, attempt["run_id"], timestamp)
    return SemanticDispatchResult(
        lease.candidate_id,
        "retry_wait" if retryable else "failed",
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
    )


def _abandon_reserved(
    db: Any,
    *,
    attempt: dict[str, Any],
    verification: SourceVerification,
    timestamp: str,
    cancelled: bool,
) -> SemanticDispatchResult:
    with immediate_transaction(db):
        if cancelled:
            checkpoint_cancelled_in_transaction(
                db, lease=verification.lease, timestamp=timestamp
            )
            status = "cancelled"
        else:
            checkpoint_source_changed_in_transaction(
                db, verification=verification, timestamp=timestamp
            )
            status = "skipped"
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=0,
            timestamp=timestamp,
        )
        mark_provider_attempt_abandoned_in_transaction(
            db,
            reservation_key=attempt["reservation_key"],
            timestamp=timestamp,
        )
        refresh_run_summary_in_transaction(db, attempt["run_id"], timestamp)
    return SemanticDispatchResult(
        verification.lease.candidate_id,
        status,
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
    )


def _abandon_reserved_failure(
    db: Any,
    *,
    attempt: dict[str, Any],
    lease: SemanticRunItemLease,
    timestamp: str,
    error_code: str,
    error_summary: str,
) -> SemanticDispatchResult:
    with immediate_transaction(db):
        checkpoint_failure_in_transaction(
            db,
            lease=lease,
            retryable=False,
            timestamp=timestamp,
            available_at=None,
            error_code=error_code,
            error_summary=error_summary,
        )
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=0,
            timestamp=timestamp,
        )
        mark_provider_attempt_abandoned_in_transaction(
            db,
            reservation_key=attempt["reservation_key"],
            timestamp=timestamp,
        )
        refresh_run_summary_in_transaction(db, attempt["run_id"], timestamp)
    return SemanticDispatchResult(
        lease.candidate_id,
        "failed",
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
    )


def _abandon_expired_reservation(
    db: Any,
    *,
    attempt: dict[str, Any],
    lease: SemanticRunItemLease,
    timestamp: str,
) -> SemanticDispatchResult:
    with immediate_transaction(db):
        item = db.execute(
            "SELECT status,attempt_count,lease_expires_at FROM "
            "semantic_index_run_items WHERE run_id=? AND semantic_question_id=?",
            (lease.run_id, lease.semantic_question_id),
        ).fetchone()
        if (
            item is not None
            and item["status"] == "leased"
            and int(item["attempt_count"]) == lease.attempt_count
        ):
            changed = db.execute(
                "UPDATE semantic_index_run_items SET status='retry_wait',"
                "lease_owner=NULL,lease_expires_at=NULL,available_at=?,"
                "error_code='lease_expired_before_dispatch',"
                "error_summary='Provider dispatch 前租约已到期',updated_at=? "
                "WHERE run_id=? AND semantic_question_id=? AND status='leased' "
                "AND attempt_count=? AND julianday(lease_expires_at)<=julianday(?)",
                (
                    timestamp,
                    timestamp,
                    lease.run_id,
                    lease.semantic_question_id,
                    lease.attempt_count,
                    timestamp,
                ),
            ).rowcount
            if changed != 1:
                raise SemanticDispatchError(
                    "LEASE_LOST", "Provider dispatch 被拒绝，但租约尚不能安全回收"
                )
            assert_run_item_transition("leased", "retry_wait")
        settle_tokens_in_transaction(
            db,
            run_id=lease.run_id,
            reservation_key=attempt["reservation_key"],
            actual_tokens=0,
            timestamp=timestamp,
        )
        mark_provider_attempt_abandoned_in_transaction(
            db,
            reservation_key=attempt["reservation_key"],
            timestamp=timestamp,
        )
        refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
    return SemanticDispatchResult(
        lease.candidate_id,
        "retry_wait",
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
    )


def _pause_unknown(
    db: Any, *, attempt: dict[str, Any], candidate_id: str, timestamp: str
) -> SemanticDispatchResult:
    with immediate_transaction(db):
        row = get_provider_attempt(db, attempt["reservation_key"])
        if row is None:
            raise SemanticDispatchError("ATTEMPT_NOT_FOUND", "Provider attempt 不存在")
        if row["status"] == "dispatching":
            mark_provider_outcome_unknown_in_transaction(
                db,
                reservation_key=attempt["reservation_key"],
                timestamp=timestamp,
            )
        elif row["status"] != "outcome_unknown":
            raise SemanticDispatchError(
                "ATTEMPT_STATE_CONFLICT", "只有 dispatching 可进入 outcome_unknown"
            )
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=None,
            timestamp=timestamp,
        )
        db.execute(
            "UPDATE semantic_index_runs SET status='paused_budget_review',"
            "token_stop_reason='provider_outcome_unknown',"
            "last_error_code='provider_outcome_unknown',"
            "last_error_summary='Provider 是否执行无法确认，禁止自动重调',"
            "updated_at=?,finished_at=NULL WHERE id=? "
            "AND status IN ('running','paused_budget_review')",
            (timestamp, attempt["run_id"]),
        )
    return SemanticDispatchResult(
        candidate_id=candidate_id,
        status="outcome_unknown",
        provider_attempts=int(attempt["provider_attempt"]),
        reservation_key=attempt["reservation_key"],
    )


def _resolve_stale_attempt(
    db: Any,
    *,
    attempt: dict[str, Any],
    lease: SemanticRunItemLease,
    timestamp: str,
) -> SemanticDispatchResult:
    status = str(attempt["status"])
    if status in {"dispatching", "outcome_unknown"}:
        return _pause_unknown(
            db,
            attempt=attempt,
            candidate_id=lease.candidate_id,
            timestamp=timestamp,
        )
    with immediate_transaction(db):
        if status == "reserved":
            actual_tokens: int | None = 0
        elif status in {"response_valid", "response_invalid", "provider_failed"}:
            actual_tokens = (
                int(attempt["usage_total_tokens"])
                if int(attempt["usage_known"])
                else None
            )
        else:
            return SemanticDispatchResult(
                lease.candidate_id,
                "lease_lost",
                int(attempt["provider_attempt"]),
                attempt["reservation_key"],
            )
        settle_tokens_in_transaction(
            db,
            run_id=attempt["run_id"],
            reservation_key=attempt["reservation_key"],
            actual_tokens=actual_tokens,
            timestamp=timestamp,
        )
        if status == "reserved":
            mark_provider_attempt_abandoned_in_transaction(
                db,
                reservation_key=attempt["reservation_key"],
                timestamp=timestamp,
            )
        else:
            db.execute(
                "UPDATE semantic_index_runs SET status='paused_budget_review',"
                "token_stop_reason='stale_provider_attempt',"
                "last_error_code='stale_provider_attempt',"
                "last_error_summary='旧租约 Provider 响应不能写入新 attempt',"
                "updated_at=?,finished_at=NULL WHERE id=? AND status='running'",
                (timestamp, attempt["run_id"]),
            )
    return SemanticDispatchResult(
        lease.candidate_id,
        "lease_lost" if status == "reserved" else "stale_response_paused",
        int(attempt["provider_attempt"]),
        attempt["reservation_key"],
    )


def execute_synthetic_semantic_item(
    db: Any,
    *,
    bundle: SemanticSourceBundle,
    lease: SemanticRunItemLease,
    invoke_card: SemanticCardInvoker,
    estimate_cost: SemanticTokenCostEstimator,
    timestamp: str,
    retry_available_at: str,
    cancel_requested: CancelCheck | None = None,
    max_provider_attempts: int = MAX_MISSING_FILL_ATTEMPTS + 1,
) -> SemanticDispatchResult:
    """Advance one leased item without ever repeating an ambiguous Provider call."""

    if not callable(invoke_card):
        raise SemanticDispatchError("PROVIDER_UNAVAILABLE", "未配置材料卡调用器")
    if (
        isinstance(max_provider_attempts, bool)
        or not isinstance(max_provider_attempts, int)
        or max_provider_attempts < 1
        or max_provider_attempts > MAX_MISSING_FILL_ATTEMPTS + 1
    ):
        raise SemanticDispatchError(
            "VALIDATION", "max_provider_attempts 超出支持范围"
        )
    item = _item_state(db, lease)
    if item is None:
        raise SemanticDispatchError("ITEM_NOT_FOUND", "冻结 run-item 不存在")
    attempts = _attempt_rows(db, lease)
    latest = attempts[-1] if attempts else None
    if str(item["status"]) != SemanticRunItemStatus.LEASED.value:
        if (
            latest is not None
            and latest["status"] not in {"abandoned", "checkpointed"}
            and not _has_terminal_token_event(db, latest)
        ):
            return _resolve_stale_attempt(
                db, attempt=latest, lease=lease, timestamp=timestamp
            )
        return _result_from_terminal_item(lease, item)
    if int(item["attempt_count"]) != lease.attempt_count:
        if latest is not None:
            return _resolve_stale_attempt(
                db, attempt=latest, lease=lease, timestamp=timestamp
            )
        raise SemanticDispatchError("LEASE_LOST", "run-item 已进入新的租约 attempt")

    if latest is not None and latest["status"] == "checkpointed":
        item = _item_state(db, lease) or item
        return _result_from_terminal_item(lease, item)
    if latest is not None and latest["status"] in {"dispatching", "outcome_unknown"}:
        return _pause_unknown(
            db,
            attempt=latest,
            candidate_id=lease.candidate_id,
            timestamp=timestamp,
        )

    run = db.execute(
        "SELECT status,analysis_schema_version,prompt_version FROM semantic_index_runs "
        "WHERE id=?",
        (lease.run_id,),
    ).fetchone()
    if run is None:
        raise SemanticDispatchError("RUN_NOT_FOUND", "semantic run 不存在")
    if (
        int(run["analysis_schema_version"]) != SEMANTIC_CARD_SCHEMA_VERSION
        or str(run["prompt_version"]) != PROMPT_VERSION
    ):
        if latest is not None and latest["status"] == "reserved":
            return _abandon_reserved_failure(
                db,
                attempt=latest,
                lease=lease,
                timestamp=timestamp,
                error_code="analysis_profile_incompatible",
                error_summary="材料卡分析契约版本与冻结 run 不一致",
            )
        if latest is not None and latest["status"] in {
            "response_valid",
            "response_invalid",
            "provider_failed",
        }:
            return _finalize_failure(
                db,
                attempt=latest,
                lease=lease,
                retryable=False,
                timestamp=timestamp,
                retry_available_at=retry_available_at,
                error_code="analysis_profile_incompatible",
                error_summary="材料卡分析契约版本与冻结 run 不一致",
            )
        checkpoint_failure(
            db,
            lease=lease,
            retryable=False,
            timestamp=timestamp,
            available_at=None,
            error_code="analysis_profile_incompatible",
            error_summary="材料卡分析契约版本与冻结 run 不一致",
        )
        return SemanticDispatchResult(lease.candidate_id, "failed", 0)

    verification = verify_leased_sources(bundle, [lease])[0]
    candidate = candidate_for_lease(bundle, lease)
    if not verification.matches and latest is None:
        checkpoint_source_changed(db, verification=verification, timestamp=timestamp)
        return SemanticDispatchResult(lease.candidate_id, "skipped", 0)
    if candidate is None and verification.matches:
        raise SemanticDispatchError("SOURCE_MISSING", "已验证题目无法从来源取回")

    while True:
        attempts = _attempt_rows(db, lease)
        latest = attempts[-1] if attempts else None
        if latest is not None:
            status = str(latest["status"])
            if status == "response_valid":
                if not verification.matches:
                    with immediate_transaction(db):
                        checkpoint_source_changed_in_transaction(
                            db, verification=verification, timestamp=timestamp
                        )
                        settle_tokens_in_transaction(
                            db,
                            run_id=lease.run_id,
                            reservation_key=latest["reservation_key"],
                            actual_tokens=(
                                int(latest["usage_total_tokens"])
                                if int(latest["usage_known"])
                                else None
                            ),
                            timestamp=timestamp,
                        )
                        refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
                    return SemanticDispatchResult(
                        lease.candidate_id,
                        "skipped",
                        int(latest["provider_attempt"]),
                        latest["reservation_key"],
                    )
                return _finalize_success(
                    db,
                    attempt=latest,
                    verification=verification,
                    timestamp=timestamp,
                )
            if status == "provider_failed":
                retryable = latest["error_code"] == "PROVIDER_TEMPORARY"
                return _finalize_failure(
                    db,
                    attempt=latest,
                    lease=lease,
                    retryable=retryable,
                    timestamp=timestamp,
                    retry_available_at=retry_available_at,
                    error_code=(
                        "provider_temporary" if retryable else "provider_permanent"
                    ),
                    error_summary=(
                        "材料卡 Provider 暂时不可用"
                        if retryable
                        else "材料卡 Provider 配置或请求不可恢复"
                    ),
                )
            if status == "response_invalid":
                if int(latest["provider_attempt"]) >= max_provider_attempts:
                    return _finalize_failure(
                        db,
                        attempt=latest,
                        lease=lease,
                        retryable=False,
                        timestamp=timestamp,
                        retry_available_at=retry_available_at,
                        error_code="invalid_provider_response",
                        error_summary=(
                            "材料卡无效，且本次运行禁止追加 Provider 请求"
                            if max_provider_attempts == 1
                            else "材料卡定向补全后仍未通过严格校验"
                        ),
                    )
                settle_tokens(
                    db,
                    run_id=lease.run_id,
                    reservation_key=latest["reservation_key"],
                    actual_tokens=(
                        int(latest["usage_total_tokens"])
                        if int(latest["usage_known"])
                        else None
                    ),
                    timestamp=timestamp,
                )
                run_status = db.execute(
                    "SELECT status FROM semantic_index_runs WHERE id=?",
                    (lease.run_id,),
                ).fetchone()[0]
                if run_status != "running":
                    return SemanticDispatchResult(
                        lease.candidate_id,
                        str(run_status),
                        int(latest["provider_attempt"]),
                        latest["reservation_key"],
                    )
                if cancel_requested is not None and cancel_requested():
                    with immediate_transaction(db):
                        checkpoint_cancelled_in_transaction(
                            db, lease=lease, timestamp=timestamp
                        )
                        refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
                    return SemanticDispatchResult(
                        lease.candidate_id,
                        "cancelled",
                        int(latest["provider_attempt"]),
                        latest["reservation_key"],
                    )
                next_attempt = int(latest["provider_attempt"]) + 1
            elif status == "reserved":
                cancelled = bool(cancel_requested and cancel_requested())
                if not verification.matches or cancelled:
                    return _abandon_reserved(
                        db,
                        attempt=latest,
                        verification=verification,
                        timestamp=timestamp,
                        cancelled=cancelled,
                    )
                next_attempt = int(latest["provider_attempt"])
            elif status in {"abandoned", "checkpointed"}:
                return _result_from_terminal_item(lease, _item_state(db, lease) or item)
            else:
                raise SemanticDispatchError(
                    "ATTEMPT_STATE_UNSUPPORTED",
                    f"Provider attempt 状态无法继续：{status}",
                )
        else:
            if cancel_requested is not None and cancel_requested():
                with immediate_transaction(db):
                    checkpoint_cancelled_in_transaction(
                        db, lease=lease, timestamp=timestamp
                    )
                    refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
                return SemanticDispatchResult(lease.candidate_id, "cancelled", 0)
            next_attempt = 1

        if candidate is None:
            raise SemanticDispatchError("SOURCE_MISSING", "无法构造材料卡请求")
        request = build_permanent_card_request(candidate, attempt=next_attempt)
        cost = _cost_for_request(estimate_cost, request)
        prompt_hash, fingerprint = _request_hashes(request, cost)
        if latest is None or int(latest["provider_attempt"]) != next_attempt:
            _reservation, latest = _reserve_attempt_pair(
                db,
                lease=lease,
                request=request,
                cost=cost,
                prompt_hash=prompt_hash,
                request_fingerprint=fingerprint,
                timestamp=timestamp,
            )
        elif (
            latest["prompt_hash"] != prompt_hash
            or latest["request_fingerprint"] != fingerprint
        ):
            return _abandon_reserved_failure(
                db,
                attempt=latest,
                lease=lease,
                timestamp=timestamp,
                error_code="request_fingerprint_changed",
                error_summary="已预留 Provider 请求与当前构造结果不一致",
            )

        try:
            mark_provider_attempt_dispatching(
                db, reservation_key=latest["reservation_key"], timestamp=timestamp
            )
        except SemanticProviderAttemptError as exc:
            if exc.code != "LEASE_LOST":
                raise
            return _abandon_expired_reservation(
                db, attempt=latest, lease=lease, timestamp=timestamp
            )
        response: SemanticCardProviderResponse | None = None
        request_hash: str | None = None
        try:
            response = coerce_provider_response(invoke_card(request))
            usage = response.usage_total_tokens
            if usage is not None and (
                isinstance(usage, bool) or not isinstance(usage, int) or usage < 0
            ):
                raise SemanticMaterialExecutionError(
                    "INVALID_PROVIDER_RESPONSE", "Provider usage 无效"
                )
            request_hash = hash_provider_request_id(response.request_id)
            card = parse_permanent_card_response(
                response.raw_text, expected_candidate_id=lease.candidate_id
            )
        except AmbiguousSemanticProviderError:
            return _pause_unknown(
                db,
                attempt=latest,
                candidate_id=lease.candidate_id,
                timestamp=timestamp,
            )
        except TemporarySemanticProviderError:
            record_provider_failure(
                db,
                reservation_key=latest["reservation_key"],
                usage_total_tokens=None,
                timestamp=timestamp,
                retryable=True,
            )
        except PermanentSemanticProviderError:
            record_provider_failure(
                db,
                reservation_key=latest["reservation_key"],
                usage_total_tokens=None,
                timestamp=timestamp,
                retryable=False,
            )
        except SemanticMaterialExecutionError:
            record_invalid_provider_response(
                db,
                reservation_key=latest["reservation_key"],
                usage_total_tokens=(
                    response.usage_total_tokens
                    if "response" in locals()
                    and isinstance(response, SemanticCardProviderResponse)
                    and isinstance(response.usage_total_tokens, int)
                    and not isinstance(response.usage_total_tokens, bool)
                    and response.usage_total_tokens >= 0
                    else None
                ),
                timestamp=timestamp,
                provider_request_id_hash=(
                    request_hash
                ),
            )
        except Exception:
            return _pause_unknown(
                db,
                attempt=latest,
                candidate_id=lease.candidate_id,
                timestamp=timestamp,
            )
        else:
            record_valid_provider_response(
                db,
                reservation_key=latest["reservation_key"],
                card=card,
                usage_total_tokens=usage,
                timestamp=timestamp,
                provider_request_id_hash=request_hash,
            )
