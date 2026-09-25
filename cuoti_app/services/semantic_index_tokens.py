"""Atomic persistent token ledger for the first full semantic build.

The module owns only short SQLite transactions. It neither calls a provider
nor estimates prompts; callers must reserve the complete worst-case charge
before any external request starts.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..transactions import immediate_transaction
from .semantic_index import (
    FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
    FirstFullBuildTokenLedger,
    TokenBudgetError,
    TokenBudgetExtension,
)
from .semantic_index_checkpoints import SemanticRunItemLease


class SemanticTokenLedgerError(RuntimeError):
    def __init__(
        self, code: str, message: str, *, requested_amount: int | None = None
    ):
        super().__init__(message)
        self.code = code
        self.requested_amount = requested_amount


@dataclass(frozen=True, slots=True)
class TokenLedgerState:
    consumed: int
    reserved: int
    consumed_unverified: int
    current_limit: int
    extension_index: int

    @property
    def remaining(self) -> int:
        return self.current_limit - self.consumed - self.reserved


@dataclass(frozen=True, slots=True)
class TokenReservationResult:
    reservation_key: str
    reservation: int
    state: str
    replayed: bool
    ledger: TokenLedgerState


@dataclass(frozen=True, slots=True)
class TokenSettlementResult:
    reservation_key: str
    event_type: str
    charged_tokens: int
    replayed: bool
    ledger: TokenLedgerState


@dataclass(frozen=True, slots=True)
class TokenExtensionResult:
    status: str
    extension: TokenBudgetExtension | None
    replayed: bool
    ledger: TokenLedgerState


@dataclass(frozen=True, slots=True)
class TokenHardStopResult:
    stopped: bool
    replayed: bool
    ledger: TokenLedgerState


def _text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticTokenLedgerError("VALIDATION", f"{field} 不能为空")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise SemanticTokenLedgerError("VALIDATION", f"{field} 过长")
    return rendered


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticTokenLedgerError("VALIDATION", f"{field} 必须是正整数")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticTokenLedgerError("VALIDATION", f"{field} 必须是非负整数")
    return value


def _sha256(value: Any, field: str) -> str:
    rendered = _text(value, field, maximum=64).casefold()
    if len(rendered) != 64 or any(char not in "0123456789abcdef" for char in rendered):
        raise SemanticTokenLedgerError("VALIDATION", f"{field} 不是 SHA-256 摘要")
    return rendered


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _timestamp(value: Any) -> str:
    rendered = _text(value, "timestamp", maximum=128)
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise SemanticTokenLedgerError(
            "VALIDATION", "timestamp 必须是 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticTokenLedgerError("VALIDATION", "timestamp 必须包含时区")
    return rendered


def _row_dict(cursor: Any) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        description[0]: row[index]
        for index, description in enumerate(cursor.description)
    }


def _run(db: Any, run_id: str) -> dict[str, Any]:
    row = _row_dict(
        db.execute("SELECT * FROM semantic_index_runs WHERE id=?", (run_id,))
    )
    if row is None:
        raise SemanticTokenLedgerError("RUN_NOT_FOUND", "semantic run 不存在")
    if row["run_kind"] not in {"first_full", "rebuild"}:
        raise SemanticTokenLedgerError(
            "RUN_KIND_NOT_BUDGETED", "该 Token 账本只适用于首次全库或显式重建 run"
        )
    if int(row["token_initial_limit"]) != FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT:
        raise SemanticTokenLedgerError("LEDGER_CORRUPT", "初始额度不是 300 万")
    if int(row["token_hard_limit"]) != FIRST_FULL_BUILD_HARD_TOKEN_LIMIT:
        raise SemanticTokenLedgerError("LEDGER_CORRUPT", "硬上限不是 500 万")
    return row


def _domain_ledger(row: dict[str, Any]) -> FirstFullBuildTokenLedger:
    try:
        return FirstFullBuildTokenLedger(
            current_limit=int(row["token_current_limit"]),
            extension_index=int(row["token_extension_index"]),
            consumed=int(row["token_consumed"]),
            reserved=int(row["token_reserved"]),
            consumed_unverified=int(row["token_consumed_unverified"]),
        )
    except (TokenBudgetError, TypeError, ValueError) as exc:
        raise SemanticTokenLedgerError(
            "LEDGER_CORRUPT", "持久化 Token 余额违反领域契约"
        ) from exc


def _state(ledger: FirstFullBuildTokenLedger) -> TokenLedgerState:
    return TokenLedgerState(
        consumed=ledger.consumed,
        reserved=ledger.reserved,
        consumed_unverified=ledger.consumed_unverified,
        current_limit=ledger.current_limit,
        extension_index=ledger.extension_index,
    )


def _update_run_ledger(
    db: Any,
    *,
    run_id: str,
    ledger: FirstFullBuildTokenLedger,
    timestamp: str,
    extension: TokenBudgetExtension | None = None,
) -> None:
    next_extension = ledger.next_nominal_extension
    changed = db.execute(
        "UPDATE semantic_index_runs SET token_current_limit=?,token_consumed=?,"
        "token_reserved=?,token_consumed_unverified=?,token_remaining=?,"
        "token_extension_index=?,token_next_extension=?,updated_at=?,"
        "token_last_extended_at=CASE WHEN ? IS NULL THEN token_last_extended_at ELSE ? END "
        "WHERE id=?",
        (
            ledger.current_limit,
            ledger.consumed,
            ledger.reserved,
            ledger.consumed_unverified,
            ledger.available,
            ledger.extension_index,
            next_extension,
            timestamp,
            None if extension is None else extension.extension_index,
            timestamp,
            run_id,
        ),
    ).rowcount
    if changed != 1:
        raise SemanticTokenLedgerError("RUN_NOT_FOUND", "semantic run 不存在")


def _insert_event(
    db: Any,
    *,
    run_id: str,
    event_type: str,
    timestamp: str,
    ledger: FirstFullBuildTokenLedger,
    reservation_key: str | None = None,
    extension: TokenBudgetExtension | None = None,
    actual_amount: int | None = None,
    delta_consumed: int = 0,
    delta_reserved: int = 0,
    delta_unverified: int = 0,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO semantic_index_token_events("
        "run_id,event_type,reservation_key,extension_index,previous_limit,"
        "nominal_amount,actual_amount,new_limit,token_delta_consumed,"
        "token_delta_reserved,token_delta_unverified,consumed_after,reserved_after,"
        "unverified_after,current_limit_after,metadata_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            event_type,
            reservation_key,
            None if extension is None else extension.extension_index,
            None if extension is None else extension.previous_limit,
            None if extension is None else extension.nominal_amount,
            actual_amount if extension is None else extension.actual_amount,
            None if extension is None else extension.new_limit,
            delta_consumed,
            delta_reserved,
            delta_unverified,
            ledger.consumed,
            ledger.reserved,
            ledger.consumed_unverified,
            ledger.current_limit,
            None if metadata is None else _canonical_json(metadata),
            timestamp,
        ),
    )


def _reservation_events(db: Any, run_id: str, reservation_key: str) -> list[dict[str, Any]]:
    cursor = db.execute(
        "SELECT * FROM semantic_index_token_events WHERE run_id=? "
        "AND reservation_key=? ORDER BY id",
        (run_id, reservation_key),
    )
    columns = [description[0] for description in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _validate_reservation_history(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    reservations = [event for event in events if event["event_type"] == "reservation"]
    terminals = [
        event
        for event in events
        if event["event_type"] in {"settlement", "usage_unverified", "reconciliation"}
    ]
    if len(reservations) > 1 or len(terminals) > 2:
        raise SemanticTokenLedgerError("LEDGER_CORRUPT", "reservation 审计事件重复")
    if terminals and not reservations:
        raise SemanticTokenLedgerError("LEDGER_CORRUPT", "结算事件缺少 reservation")
    if len(terminals) == 2 and [event["event_type"] for event in terminals] != [
        "usage_unverified",
        "reconciliation",
    ]:
        raise SemanticTokenLedgerError("LEDGER_CORRUPT", "reservation 终态事件冲突")
    return (reservations[0] if reservations else None, terminals[-1] if terminals else None)


def read_token_ledger(db: Any, *, run_id: str) -> TokenLedgerState:
    run_id = _text(run_id, "run_id")
    return _state(_domain_ledger(_run(db, run_id)))


def reserve_tokens(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    provider_attempt: int,
    prompt_hash: str,
    estimated_prompt_tokens: int,
    max_completion_tokens: int,
    safety_margin: int,
    timestamp: str,
    _caller_owned: bool = False,
) -> TokenReservationResult:
    if not isinstance(lease, SemanticRunItemLease):
        raise SemanticTokenLedgerError("VALIDATION", "lease 类型无效")
    provider_attempt = _positive_integer(provider_attempt, "provider_attempt")
    if provider_attempt > 3:
        raise SemanticTokenLedgerError("VALIDATION", "provider_attempt 超过定向补齐上限")
    prompt_hash = _sha256(prompt_hash, "prompt_hash")
    estimated_prompt_tokens = _positive_integer(
        estimated_prompt_tokens, "estimated_prompt_tokens"
    )
    max_completion_tokens = _positive_integer(
        max_completion_tokens, "max_completion_tokens"
    )
    safety_margin = _nonnegative_integer(safety_margin, "safety_margin")
    amount = estimated_prompt_tokens + max_completion_tokens + safety_margin
    timestamp = _timestamp(timestamp)
    if _caller_owned and not db.in_transaction:
        raise SemanticTokenLedgerError(
            "TRANSACTION_REQUIRED", "caller-owned Token reservation 缺少事务"
        )
    transaction = nullcontext() if _caller_owned else immediate_transaction(db)
    with transaction:
        row = _run(db, lease.run_id)
        if row["status"] != "running":
            raise SemanticTokenLedgerError("RUN_NOT_RUNNING", "run 当前不能预留 Token")
        owned = db.execute(
            "SELECT 1 FROM semantic_index_run_items WHERE run_id=? "
            "AND semantic_question_id=? AND status='leased' AND lease_owner=? "
            "AND expected_content_hash=? AND attempt_count=? "
            "AND julianday(lease_expires_at)>julianday(?)",
            (
                lease.run_id,
                lease.semantic_question_id,
                lease.lease_owner,
                lease.expected_content_hash,
                lease.attempt_count,
                timestamp,
            ),
        ).fetchone()
        if owned is None:
            raise SemanticTokenLedgerError("LEASE_LOST", "run item 租约已失效")
        identity = {
            "version": 1,
            "run_id": lease.run_id,
            "semantic_question_id": lease.semantic_question_id,
            "item_attempt": lease.attempt_count,
            "provider_attempt": provider_attempt,
            "expected_content_hash": lease.expected_content_hash,
            "analysis_schema_version": int(row["analysis_schema_version"]),
            "prompt_version": str(row["prompt_version"]),
            "model_role": str(row["model_role"]),
            "model_name": str(row["model_name"]),
            "reasoning_effort": str(row["reasoning_effort"]),
            "prompt_hash": prompt_hash,
            "estimated_prompt_tokens": estimated_prompt_tokens,
            "max_completion_tokens": max_completion_tokens,
            "safety_margin": safety_margin,
        }
        reservation_key = hashlib.sha256(
            _canonical_json(identity).encode("utf-8")
        ).hexdigest()
        ledger = _domain_ledger(row)
        reservation, terminal = _validate_reservation_history(
            _reservation_events(db, lease.run_id, reservation_key)
        )
        if reservation is not None:
            original = int(reservation["token_delta_reserved"])
            if original != amount:
                raise SemanticTokenLedgerError(
                    "RESERVATION_CONFLICT", "同一 reservation_key 的额度不一致"
                )
            return TokenReservationResult(
                reservation_key,
                original,
                "reserved" if terminal is None else str(terminal["event_type"]),
                True,
                _state(ledger),
            )
        try:
            updated = ledger.reserve(amount)
        except TokenBudgetError as exc:
            code = (
                "TOKEN_HARD_LIMIT"
                if ledger.current_limit == FIRST_FULL_BUILD_HARD_TOKEN_LIMIT
                else "TOKEN_EXTENSION_REQUIRED"
            )
            raise SemanticTokenLedgerError(
                code, str(exc), requested_amount=amount
            ) from exc
        _update_run_ledger(
            db, run_id=lease.run_id, ledger=updated, timestamp=timestamp
        )
        _insert_event(
            db,
            run_id=lease.run_id,
            event_type="reservation",
            reservation_key=reservation_key,
            timestamp=timestamp,
            ledger=updated,
            delta_reserved=amount,
            metadata=identity,
        )
        return TokenReservationResult(
            reservation_key, amount, "reserved", False, _state(updated)
        )


def reserve_tokens_in_transaction(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    provider_attempt: int,
    prompt_hash: str,
    estimated_prompt_tokens: int,
    max_completion_tokens: int,
    safety_margin: int,
    timestamp: str,
) -> TokenReservationResult:
    return reserve_tokens(
        db,
        lease=lease,
        provider_attempt=provider_attempt,
        prompt_hash=prompt_hash,
        estimated_prompt_tokens=estimated_prompt_tokens,
        max_completion_tokens=max_completion_tokens,
        safety_margin=safety_margin,
        timestamp=timestamp,
        _caller_owned=True,
    )


def settle_tokens(
    db: Any,
    *,
    run_id: str,
    reservation_key: str,
    actual_tokens: int | None,
    timestamp: str,
    _caller_owned: bool = False,
) -> TokenSettlementResult:
    run_id = _text(run_id, "run_id")
    reservation_key = _sha256(reservation_key, "reservation_key")
    if actual_tokens is not None:
        actual_tokens = _nonnegative_integer(actual_tokens, "actual_tokens")
    timestamp = _timestamp(timestamp)
    if _caller_owned and not db.in_transaction:
        raise SemanticTokenLedgerError(
            "TRANSACTION_REQUIRED", "caller-owned Token settlement 缺少事务"
        )
    transaction = nullcontext() if _caller_owned else immediate_transaction(db)
    with transaction:
        row = _run(db, run_id)
        ledger = _domain_ledger(row)
        reservation, terminal = _validate_reservation_history(
            _reservation_events(db, run_id, reservation_key)
        )
        if reservation is None:
            raise SemanticTokenLedgerError(
                "RESERVATION_NOT_FOUND", "结算前没有对应 Token 预留"
            )
        amount = int(reservation["token_delta_reserved"])
        expected_type = "settlement" if actual_tokens is not None else "usage_unverified"
        if terminal is not None:
            if terminal["event_type"] == "reconciliation":
                recorded = int(terminal["actual_amount"])
                if actual_tokens != recorded:
                    raise SemanticTokenLedgerError(
                        "SETTLEMENT_CONFLICT", "reservation 已按不同 usage 完成对账"
                    )
            elif terminal["event_type"] != expected_type:
                overage_replay = False
                if (
                    terminal["event_type"] == "usage_unverified"
                    and actual_tokens is not None
                    and terminal["metadata_json"]
                ):
                    try:
                        metadata = json.loads(str(terminal["metadata_json"]))
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise SemanticTokenLedgerError(
                            "LEDGER_CORRUPT", "usage 异常审计 metadata 已损坏"
                        ) from exc
                    overage_replay = (
                        metadata.get("overage") is True
                        and metadata.get("reported_actual_tokens") == actual_tokens
                    )
                if not overage_replay:
                    raise SemanticTokenLedgerError(
                        "SETTLEMENT_CONFLICT", "reservation 已按不同 usage 状态结算"
                    )
            elif expected_type == "settlement" and int(terminal["actual_amount"]) != actual_tokens:
                raise SemanticTokenLedgerError(
                    "SETTLEMENT_CONFLICT", "reservation 已按不同 usage 结算"
                )
            charged = (
                int(terminal["actual_amount"])
                if terminal["event_type"] in {"settlement", "reconciliation"}
                else amount
            )
            return TokenSettlementResult(
                reservation_key,
                str(terminal["event_type"]),
                charged,
                True,
                _state(ledger),
            )
        if actual_tokens is not None and actual_tokens > amount:
            try:
                updated = FirstFullBuildTokenLedger(
                    current_limit=ledger.current_limit,
                    extension_index=ledger.extension_index,
                    consumed=ledger.consumed + actual_tokens,
                    reserved=ledger.reserved - amount,
                    consumed_unverified=ledger.consumed_unverified,
                )
                overage_fits = True
            except TokenBudgetError:
                try:
                    updated = ledger.settle(amount, None)
                except TokenBudgetError as fallback_exc:
                    raise SemanticTokenLedgerError(
                        "SETTLEMENT_INVALID", str(fallback_exc)
                    ) from fallback_exc
                overage_fits = False
            _update_run_ledger(
                db, run_id=run_id, ledger=updated, timestamp=timestamp
            )
            db.execute(
                "UPDATE semantic_index_runs SET status='paused_budget_review',"
                "token_stop_reason='usage_exceeds_reservation',"
                "last_error_code='usage_exceeds_reservation',"
                "last_error_summary='Provider usage 超过请求前最坏情况预留',"
                "updated_at=? WHERE id=? AND status='running'",
                (timestamp, run_id),
            )
            _insert_event(
                db,
                run_id=run_id,
                event_type="settlement" if overage_fits else "usage_unverified",
                reservation_key=reservation_key,
                timestamp=timestamp,
                ledger=updated,
                actual_amount=actual_tokens if overage_fits else None,
                delta_consumed=actual_tokens if overage_fits else amount,
                delta_reserved=-amount,
                delta_unverified=0 if overage_fits else amount,
                metadata={
                    "reported_actual_tokens": actual_tokens,
                    "overage": True,
                    "accounting": "actual" if overage_fits else "reservation_unverified",
                },
            )
            return TokenSettlementResult(
                reservation_key,
                "settlement" if overage_fits else "usage_unverified",
                actual_tokens if overage_fits else amount,
                False,
                _state(updated),
            )
        try:
            updated = ledger.settle(amount, actual_tokens)
        except TokenBudgetError as exc:
            raise SemanticTokenLedgerError("SETTLEMENT_INVALID", str(exc)) from exc
        charged = amount if actual_tokens is None else actual_tokens
        _update_run_ledger(
            db, run_id=run_id, ledger=updated, timestamp=timestamp
        )
        _insert_event(
            db,
            run_id=run_id,
            event_type=expected_type,
            reservation_key=reservation_key,
            timestamp=timestamp,
            ledger=updated,
            actual_amount=actual_tokens,
            delta_consumed=charged,
            delta_reserved=-amount,
            delta_unverified=amount if actual_tokens is None else 0,
        )
        return TokenSettlementResult(
            reservation_key, expected_type, charged, False, _state(updated)
        )


def settle_tokens_in_transaction(
    db: Any,
    *,
    run_id: str,
    reservation_key: str,
    actual_tokens: int | None,
    timestamp: str,
) -> TokenSettlementResult:
    return settle_tokens(
        db,
        run_id=run_id,
        reservation_key=reservation_key,
        actual_tokens=actual_tokens,
        timestamp=timestamp,
        _caller_owned=True,
    )


def reconcile_unverified_tokens(
    db: Any,
    *,
    run_id: str,
    reservation_key: str,
    actual_tokens: int,
    timestamp: str,
) -> TokenSettlementResult:
    run_id = _text(run_id, "run_id")
    reservation_key = _sha256(reservation_key, "reservation_key")
    actual_tokens = _nonnegative_integer(actual_tokens, "actual_tokens")
    timestamp = _timestamp(timestamp)
    with immediate_transaction(db):
        row = _run(db, run_id)
        ledger = _domain_ledger(row)
        events = _reservation_events(db, run_id, reservation_key)
        reservation, terminal = _validate_reservation_history(events)
        if reservation is None:
            raise SemanticTokenLedgerError(
                "RESERVATION_NOT_FOUND", "对账前没有对应 Token 预留"
            )
        amount = int(reservation["token_delta_reserved"])
        if terminal is None or terminal["event_type"] == "settlement":
            raise SemanticTokenLedgerError(
                "RECONCILIATION_NOT_ALLOWED", "只有未知 usage 可向下对账"
            )
        if terminal["event_type"] == "reconciliation":
            if int(terminal["actual_amount"]) != actual_tokens:
                raise SemanticTokenLedgerError(
                    "SETTLEMENT_CONFLICT", "reservation 已按不同 usage 完成对账"
                )
            return TokenSettlementResult(
                reservation_key,
                "reconciliation",
                actual_tokens,
                True,
                _state(ledger),
            )
        try:
            updated = ledger.reconcile_unverified(amount, actual_tokens)
        except TokenBudgetError as exc:
            raise SemanticTokenLedgerError("RECONCILIATION_INVALID", str(exc)) from exc
        _update_run_ledger(
            db, run_id=run_id, ledger=updated, timestamp=timestamp
        )
        _insert_event(
            db,
            run_id=run_id,
            event_type="reconciliation",
            reservation_key=reservation_key,
            timestamp=timestamp,
            ledger=updated,
            actual_amount=actual_tokens,
            delta_consumed=actual_tokens - amount,
            delta_unverified=-amount,
        )
        return TokenSettlementResult(
            reservation_key, "reconciliation", actual_tokens, False, _state(updated)
        )


def extend_token_limit_after_review(
    db: Any,
    *,
    run_id: str,
    extension_index: int,
    review_passed: bool,
    minimum_reservation: int = 1,
    timestamp: str,
) -> TokenExtensionResult:
    run_id = _text(run_id, "run_id")
    extension_index = _positive_integer(extension_index, "extension_index")
    if not isinstance(review_passed, bool):
        raise SemanticTokenLedgerError("VALIDATION", "review_passed 必须是布尔值")
    minimum_reservation = _positive_integer(
        minimum_reservation, "minimum_reservation"
    )
    timestamp = _timestamp(timestamp)
    with immediate_transaction(db):
        row = _run(db, run_id)
        ledger = _domain_ledger(row)
        existing = _row_dict(
            db.execute(
                "SELECT * FROM semantic_index_token_events WHERE run_id=? "
                "AND event_type='extension' AND extension_index=? ORDER BY id",
                (run_id, extension_index),
            )
        )
        if existing is not None:
            extension = TokenBudgetExtension(
                extension_index=extension_index,
                previous_limit=int(existing["previous_limit"]),
                nominal_amount=int(existing["nominal_amount"]),
                actual_amount=int(existing["actual_amount"]),
                new_limit=int(existing["new_limit"]),
            )
            return TokenExtensionResult("extended", extension, True, _state(ledger))
        if row["status"] != "running":
            raise SemanticTokenLedgerError("RUN_NOT_RUNNING", "run 当前不能追加额度")
        if extension_index != ledger.extension_index + 1:
            raise SemanticTokenLedgerError("EXTENSION_CONFLICT", "额度追加序号不连续")
        if ledger.reserved or ledger.available >= minimum_reservation:
            raise SemanticTokenLedgerError(
                "EXTENSION_NOT_READY", "必须先结清在途预留且当前余额不足下一次预留"
            )
        pending = int(
            db.execute(
                "SELECT COUNT(*) FROM semantic_index_run_items WHERE run_id=? "
                "AND status NOT IN ('succeeded','failed','cancelled','skipped')",
                (run_id,),
            ).fetchone()[0]
        )
        if pending == 0:
            raise SemanticTokenLedgerError("EXTENSION_NOT_NEEDED", "run 已无待处理题目")
        failed = int(
            db.execute(
                "SELECT COUNT(*) FROM semantic_index_run_items WHERE run_id=? "
                "AND status='failed'",
                (run_id,),
            ).fetchone()[0]
        )
        if not review_passed or failed:
            db.execute(
                "UPDATE semantic_index_runs SET status='paused_budget_review',"
                "token_stop_reason='extension_review_required',updated_at=? WHERE id=?",
                (timestamp, run_id),
            )
            return TokenExtensionResult(
                "paused_budget_review", None, False, _state(ledger)
            )
        try:
            updated, extension = ledger.extend()
        except TokenBudgetError as exc:
            raise SemanticTokenLedgerError("TOKEN_HARD_LIMIT", str(exc)) from exc
        _update_run_ledger(
            db,
            run_id=run_id,
            ledger=updated,
            timestamp=timestamp,
            extension=extension,
        )
        _insert_event(
            db,
            run_id=run_id,
            event_type="extension",
            timestamp=timestamp,
            ledger=updated,
            extension=extension,
        )
        return TokenExtensionResult("extended", extension, False, _state(updated))


def hard_stop_if_minimum_cannot_fit(
    db: Any,
    *,
    run_id: str,
    minimum_reservation: int,
    timestamp: str,
) -> TokenHardStopResult:
    run_id = _text(run_id, "run_id")
    minimum_reservation = _positive_integer(
        minimum_reservation, "minimum_reservation"
    )
    timestamp = _timestamp(timestamp)
    with immediate_transaction(db):
        row = _run(db, run_id)
        ledger = _domain_ledger(row)
        existing = _row_dict(
            db.execute(
                "SELECT * FROM semantic_index_token_events WHERE run_id=? "
                "AND event_type='hard_stop' ORDER BY id",
                (run_id,),
            )
        )
        if existing is not None:
            return TokenHardStopResult(True, True, _state(ledger))
        if ledger.current_limit < FIRST_FULL_BUILD_HARD_TOKEN_LIMIT:
            return TokenHardStopResult(False, False, _state(ledger))
        if ledger.available >= minimum_reservation:
            return TokenHardStopResult(False, False, _state(ledger))
        if ledger.reserved:
            raise SemanticTokenLedgerError(
                "TOKEN_REQUESTS_IN_FLIGHT", "必须等待在途 Token 预留完成结算"
            )
        pending = int(
            db.execute(
                "SELECT COUNT(*) FROM semantic_index_run_items WHERE run_id=? "
                "AND status NOT IN ('succeeded','failed','cancelled','skipped')",
                (run_id,),
            ).fetchone()[0]
        )
        if pending == 0:
            return TokenHardStopResult(False, False, _state(ledger))
        if row["status"] != "running":
            raise SemanticTokenLedgerError("RUN_NOT_RUNNING", "run 当前不能进入硬停止")
        db.execute(
            "UPDATE semantic_index_runs SET status='paused_token_hard_limit',"
            "token_stop_reason='first_full_build_hard_limit',updated_at=? WHERE id=?",
            (timestamp, run_id),
        )
        _insert_event(
            db,
            run_id=run_id,
            event_type="hard_stop",
            timestamp=timestamp,
            ledger=ledger,
            metadata={
                "reason": "first_full_build_hard_limit",
                "minimum_reservation": minimum_reservation,
            },
        )
        return TokenHardStopResult(True, False, _state(ledger))
