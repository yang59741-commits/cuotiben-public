"""Durable, redacted journal for one semantic provider request attempt.

The journal never calls a provider and never stores a prompt, question body, or
raw response.  A caller-owned insert helper exists so the next orchestration
stage can create the Token reservation and journal row in the same transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from ..transactions import immediate_transaction
from .semantic_index import (
    InvalidSemanticIndexTransition,
    SemanticProviderAttemptStatus,
    ValidatedSemanticCard,
    assert_provider_attempt_transition,
    validate_semantic_card,
)


class SemanticProviderAttemptError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _row_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return {
        description[0]: row[index]
        for index, description in enumerate(cursor.description)
    }


def _text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticProviderAttemptError("VALIDATION", f"{field} 不能为空")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise SemanticProviderAttemptError("VALIDATION", f"{field} 过长")
    return rendered


def _sha256(value: Any, field: str) -> str:
    rendered = _text(value, field, maximum=64).casefold()
    if len(rendered) != 64 or any(
        char not in "0123456789abcdef" for char in rendered
    ):
        raise SemanticProviderAttemptError(
            "VALIDATION", f"{field} 不是 SHA-256 摘要"
        )
    return rendered


def _positive_integer(value: Any, field: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticProviderAttemptError("VALIDATION", f"{field} 必须是正整数")
    if maximum is not None and value > maximum:
        raise SemanticProviderAttemptError("VALIDATION", f"{field} 超出允许范围")
    return value


def _usage(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticProviderAttemptError(
            "VALIDATION", "usage_total_tokens 必须是非负整数或空值"
        )
    return value


def _timestamp(value: Any) -> str:
    rendered = _text(value, "timestamp", maximum=128)
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise SemanticProviderAttemptError(
            "VALIDATION", "timestamp 必须是 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticProviderAttemptError(
            "VALIDATION", "timestamp 必须包含时区"
        )
    return rendered


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def get_provider_attempt(
    db: sqlite3.Connection, reservation_key: str
) -> dict[str, Any] | None:
    reservation_key = _sha256(reservation_key, "reservation_key")
    return _row_dict(
        db.execute(
            "SELECT * FROM semantic_index_provider_attempts "
            "WHERE reservation_key=?",
            (reservation_key,),
        )
    )


def insert_reserved_provider_attempt_in_transaction(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    run_id: str,
    semantic_question_id: int,
    item_attempt: int,
    provider_attempt: int,
    expected_content_hash: str,
    prompt_hash: str,
    request_fingerprint: str,
    timestamp: str,
) -> dict[str, Any]:
    """Insert/replay a reserved row inside the caller's Token transaction."""

    if not db.in_transaction:
        raise SemanticProviderAttemptError(
            "TRANSACTION_REQUIRED", "Provider 尝试必须与 Token 预留共用事务"
        )
    reservation_key = _sha256(reservation_key, "reservation_key")
    run_id = _text(run_id, "run_id")
    semantic_question_id = _positive_integer(
        semantic_question_id, "semantic_question_id"
    )
    item_attempt = _positive_integer(item_attempt, "item_attempt")
    provider_attempt = _positive_integer(
        provider_attempt, "provider_attempt", maximum=3
    )
    expected_content_hash = _sha256(
        expected_content_hash, "expected_content_hash"
    )
    prompt_hash = _sha256(prompt_hash, "prompt_hash")
    request_fingerprint = _sha256(request_fingerprint, "request_fingerprint")
    timestamp = _timestamp(timestamp)

    reservation = _row_dict(
        db.execute(
            "SELECT token_delta_reserved,metadata_json "
            "FROM semantic_index_token_events "
            "WHERE run_id=? AND reservation_key=? AND event_type='reservation'",
            (run_id, reservation_key),
        )
    )
    if reservation is None:
        raise SemanticProviderAttemptError(
            "RESERVATION_REQUIRED", "Provider attempt 缺少同事务 Token reservation"
        )
    try:
        reservation_identity = json.loads(str(reservation["metadata_json"]))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SemanticProviderAttemptError(
            "RESERVATION_CONFLICT", "Token reservation identity 已损坏"
        ) from exc
    if not isinstance(reservation_identity, dict):
        raise SemanticProviderAttemptError(
            "RESERVATION_CONFLICT", "Token reservation identity 不是对象"
        )
    identity_hash = hashlib.sha256(
        _canonical_json(reservation_identity).encode("utf-8")
    ).hexdigest()
    identity_fields = {
        "run_id": run_id,
        "semantic_question_id": semantic_question_id,
        "item_attempt": item_attempt,
        "provider_attempt": provider_attempt,
        "expected_content_hash": expected_content_hash,
        "prompt_hash": prompt_hash,
    }
    if (
        identity_hash != reservation_key
        or int(reservation["token_delta_reserved"]) < 1
        or any(
            reservation_identity.get(key) != value
            for key, value in identity_fields.items()
        )
    ):
        raise SemanticProviderAttemptError(
            "RESERVATION_CONFLICT",
            "Token reservation 与 Provider attempt 身份不一致",
        )

    expected = {
        "reservation_key": reservation_key,
        "run_id": run_id,
        "semantic_question_id": semantic_question_id,
        "item_attempt": item_attempt,
        "provider_attempt": provider_attempt,
        "expected_content_hash": expected_content_hash,
        "prompt_hash": prompt_hash,
        "request_fingerprint": request_fingerprint,
    }
    existing = get_provider_attempt(db, reservation_key)
    if existing is not None:
        if all(existing[key] == value for key, value in expected.items()):
            return existing
        raise SemanticProviderAttemptError(
            "ATTEMPT_CONFLICT", "reservation_key 已绑定其他 Provider attempt"
        )
    item = _row_dict(
        db.execute(
            "SELECT status,attempt_count,expected_content_hash "
            "FROM semantic_index_run_items "
            "WHERE run_id=? AND semantic_question_id=?",
            (run_id, semantic_question_id),
        )
    )
    if item is None:
        raise SemanticProviderAttemptError("ITEM_NOT_FOUND", "冻结 run item 不存在")
    if (
        item["status"] != "leased"
        or int(item["attempt_count"]) != item_attempt
        or str(item["expected_content_hash"]).casefold() != expected_content_hash
    ):
        raise SemanticProviderAttemptError(
            "LEASE_LOST", "冻结 run item 已不属于该 Provider attempt"
        )
    try:
        db.execute(
            "INSERT INTO semantic_index_provider_attempts("
            "reservation_key,run_id,semantic_question_id,item_attempt,"
            "provider_attempt,expected_content_hash,prompt_hash,"
            "request_fingerprint,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                reservation_key,
                run_id,
                semantic_question_id,
                item_attempt,
                provider_attempt,
                expected_content_hash,
                prompt_hash,
                request_fingerprint,
                SemanticProviderAttemptStatus.RESERVED.value,
                timestamp,
                timestamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SemanticProviderAttemptError(
            "ATTEMPT_CONFLICT",
            "同一题目租约的 Provider 子轮次已经存在",
        ) from exc
    return get_provider_attempt(db, reservation_key) or {}


def _transition_in_transaction(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    target: SemanticProviderAttemptStatus,
    timestamp: str,
    assignments: str,
    values: tuple[Any, ...],
) -> dict[str, Any]:
    if not db.in_transaction:
        raise SemanticProviderAttemptError(
            "TRANSACTION_REQUIRED", "Provider attempt 状态更新缺少事务"
        )
    reservation_key = _sha256(reservation_key, "reservation_key")
    timestamp = _timestamp(timestamp)
    current = get_provider_attempt(db, reservation_key)
    if current is None:
        raise SemanticProviderAttemptError(
            "ATTEMPT_NOT_FOUND", "Provider attempt 不存在"
        )
    try:
        assert_provider_attempt_transition(current["status"], target)
    except InvalidSemanticIndexTransition as exc:
        raise SemanticProviderAttemptError("INVALID_TRANSITION", str(exc)) from exc
    changed = db.execute(
        "UPDATE semantic_index_provider_attempts SET status=?,updated_at=?,"
        + assignments
        + " WHERE reservation_key=? AND status=?",
        (target.value, timestamp, *values, reservation_key, current["status"]),
    ).rowcount
    if changed != 1:
        raise SemanticProviderAttemptError(
            "ATTEMPT_CONFLICT", "Provider attempt 状态已被并发修改"
        )
    return get_provider_attempt(db, reservation_key) or {}


def _transition(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    target: SemanticProviderAttemptStatus,
    timestamp: str,
    assignments: str,
    values: tuple[Any, ...],
) -> dict[str, Any]:
    with immediate_transaction(db):
        return _transition_in_transaction(
            db,
            reservation_key=reservation_key,
            target=target,
            timestamp=timestamp,
            assignments=assignments,
            values=values,
        )


def mark_provider_attempt_dispatching(
    db: sqlite3.Connection, *, reservation_key: str, timestamp: str
) -> dict[str, Any]:
    reservation_key = _sha256(reservation_key, "reservation_key")
    timestamp = _timestamp(timestamp)
    with immediate_transaction(db):
        current = get_provider_attempt(db, reservation_key)
        if current is None:
            raise SemanticProviderAttemptError(
                "ATTEMPT_NOT_FOUND", "Provider attempt 不存在"
            )
        try:
            assert_provider_attempt_transition(
                current["status"], SemanticProviderAttemptStatus.DISPATCHING
            )
        except InvalidSemanticIndexTransition as exc:
            raise SemanticProviderAttemptError("INVALID_TRANSITION", str(exc)) from exc
        owned = db.execute(
            "SELECT 1 FROM semantic_index_run_items i "
            "JOIN semantic_index_runs r ON r.id=i.run_id "
            "WHERE i.run_id=? AND i.semantic_question_id=? "
            "AND r.status='running' AND i.status='leased' "
            "AND i.expected_content_hash=? AND i.attempt_count=? "
            "AND julianday(i.lease_expires_at)>julianday(?)",
            (
                current["run_id"],
                current["semantic_question_id"],
                current["expected_content_hash"],
                current["item_attempt"],
                timestamp,
            ),
        ).fetchone()
        if owned is None:
            raise SemanticProviderAttemptError(
                "LEASE_LOST", "Provider dispatch 前 run-item 租约已失效"
            )
        return _transition_in_transaction(
            db,
            reservation_key=reservation_key,
            target=SemanticProviderAttemptStatus.DISPATCHING,
            timestamp=timestamp,
            assignments="dispatched_at=?",
            values=(timestamp,),
        )


def record_valid_provider_response(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    card: ValidatedSemanticCard | Mapping[str, Any],
    usage_total_tokens: int | None,
    timestamp: str,
    provider_request_id_hash: str | None = None,
) -> dict[str, Any]:
    reservation_key = _sha256(reservation_key, "reservation_key")
    current = get_provider_attempt(db, reservation_key)
    if current is None:
        raise SemanticProviderAttemptError("ATTEMPT_NOT_FOUND", "Provider attempt 不存在")
    question = _row_dict(
        db.execute(
            "SELECT candidate_id FROM semantic_questions WHERE id=?",
            (current["semantic_question_id"],),
        )
    )
    if question is None:
        raise SemanticProviderAttemptError("QUESTION_NOT_FOUND", "语义题目不存在")
    validated = (
        card
        if isinstance(card, ValidatedSemanticCard)
        else validate_semantic_card(card, expected_candidate_id=question["candidate_id"])
    )
    if validated.candidate_id != question["candidate_id"]:
        raise SemanticProviderAttemptError(
            "CARD_ID_MISMATCH", "材料卡 candidate_id 与冻结题目不一致"
        )
    if validated.ignored_field_count:
        raise SemanticProviderAttemptError("CARD_INVALID", "材料卡包含未知字段")
    usage_total_tokens = _usage(usage_total_tokens)
    request_hash = (
        None
        if provider_request_id_hash is None
        else _sha256(provider_request_id_hash, "provider_request_id_hash")
    )
    payload = _canonical_json(validated.as_payload())
    return _transition(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.RESPONSE_VALID,
        timestamp=timestamp,
        assignments=(
            "usage_known=?,usage_total_tokens=?,provider_request_id_hash=?,"
            "validated_card_json=?,validated_card_hash=?,responded_at=?"
        ),
        values=(
            int(usage_total_tokens is not None),
            usage_total_tokens,
            request_hash,
            payload,
            validated.card_hash,
            _timestamp(timestamp),
        ),
    )


def record_invalid_provider_response(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    usage_total_tokens: int | None,
    timestamp: str,
    provider_request_id_hash: str | None = None,
) -> dict[str, Any]:
    usage_total_tokens = _usage(usage_total_tokens)
    request_hash = (
        None
        if provider_request_id_hash is None
        else _sha256(provider_request_id_hash, "provider_request_id_hash")
    )
    return _transition(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.RESPONSE_INVALID,
        timestamp=timestamp,
        assignments=(
            "usage_known=?,usage_total_tokens=?,provider_request_id_hash=?,"
            "error_code='INVALID_RESPONSE',"
            "error_summary='Provider 响应未通过永久材料卡契约',responded_at=?"
        ),
        values=(
            int(usage_total_tokens is not None),
            usage_total_tokens,
            request_hash,
            _timestamp(timestamp),
        ),
    )


def record_provider_failure(
    db: sqlite3.Connection,
    *,
    reservation_key: str,
    usage_total_tokens: int | None,
    timestamp: str,
    retryable: bool = False,
) -> dict[str, Any]:
    if not isinstance(retryable, bool):
        raise SemanticProviderAttemptError("VALIDATION", "retryable 必须是布尔值")
    usage_total_tokens = _usage(usage_total_tokens)
    code = "PROVIDER_TEMPORARY" if retryable else "PROVIDER_PERMANENT"
    summary = (
        "Provider 暂时不可用" if retryable else "Provider 配置或请求不可恢复"
    )
    return _transition(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.PROVIDER_FAILED,
        timestamp=timestamp,
        assignments=(
            "usage_known=?,usage_total_tokens=?,error_code=?,"
            "error_summary=?,responded_at=?"
        ),
        values=(
            int(usage_total_tokens is not None),
            usage_total_tokens,
            code,
            summary,
            _timestamp(timestamp),
        ),
    )


def mark_provider_outcome_unknown(
    db: sqlite3.Connection, *, reservation_key: str, timestamp: str
) -> dict[str, Any]:
    with immediate_transaction(db):
        return mark_provider_outcome_unknown_in_transaction(
            db, reservation_key=reservation_key, timestamp=timestamp
        )


def mark_provider_outcome_unknown_in_transaction(
    db: sqlite3.Connection, *, reservation_key: str, timestamp: str
) -> dict[str, Any]:
    row = _transition_in_transaction(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.OUTCOME_UNKNOWN,
        timestamp=timestamp,
        assignments=(
            "error_code='PROVIDER_OUTCOME_UNKNOWN',"
            "error_summary='Provider 是否执行无法确认，禁止自动重调'"
        ),
        values=(),
    )
    db.execute(
        "UPDATE semantic_index_runs SET status='paused_budget_review',"
        "token_stop_reason='provider_outcome_unknown',"
        "last_error_code='provider_outcome_unknown',"
        "last_error_summary='Provider 是否执行无法确认，禁止自动重调',"
        "updated_at=?,finished_at=NULL WHERE id=? AND status='running'",
        (_timestamp(timestamp), row["run_id"]),
    )
    return row


def mark_provider_attempt_abandoned_in_transaction(
    db: sqlite3.Connection, *, reservation_key: str, timestamp: str
) -> dict[str, Any]:
    if not db.in_transaction:
        raise SemanticProviderAttemptError(
            "TRANSACTION_REQUIRED", "abandoned 必须与零消耗结算共用事务"
        )
    reservation_key = _sha256(reservation_key, "reservation_key")
    current = get_provider_attempt(db, reservation_key)
    if current is None:
        raise SemanticProviderAttemptError("ATTEMPT_NOT_FOUND", "Provider attempt 不存在")
    zero_terminal = db.execute(
        "SELECT 1 FROM semantic_index_token_events WHERE run_id=? "
        "AND reservation_key=? AND event_type='settlement' AND actual_amount=0",
        (current["run_id"], reservation_key),
    ).fetchone()
    item = db.execute(
        "SELECT status FROM semantic_index_run_items "
        "WHERE run_id=? AND semantic_question_id=? AND attempt_count=?",
        (
            current["run_id"],
            current["semantic_question_id"],
            current["item_attempt"],
        ),
    ).fetchone()
    if zero_terminal is None:
        raise SemanticProviderAttemptError(
            "SETTLEMENT_REQUIRED", "abandoned 前缺少零消耗 Token 结算"
        )
    if item is not None and item[0] == "leased":
        raise SemanticProviderAttemptError(
            "CHECKPOINT_REQUIRED", "abandoned 前 run-item 尚未释放"
        )
    return _transition_in_transaction(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.ABANDONED,
        timestamp=timestamp,
        assignments=(
            "error_code='NOT_DISPATCHED',"
            "error_summary='Provider 未开始调用，reservation 已按零消耗释放'"
        ),
        values=(),
    )


def mark_provider_attempt_checkpointed_in_transaction(
    db: sqlite3.Connection, *, reservation_key: str, timestamp: str
) -> dict[str, Any]:
    if not db.in_transaction:
        raise SemanticProviderAttemptError(
            "TRANSACTION_REQUIRED",
            "checkpointed 必须与 Token 结算和材料卡 checkpoint 共用事务",
        )
    reservation_key = _sha256(reservation_key, "reservation_key")
    current = get_provider_attempt(db, reservation_key)
    if current is None:
        raise SemanticProviderAttemptError("ATTEMPT_NOT_FOUND", "Provider attempt 不存在")
    terminal = db.execute(
        "SELECT 1 FROM semantic_index_token_events WHERE run_id=? "
        "AND reservation_key=? AND event_type IN ('settlement','usage_unverified')",
        (current["run_id"], reservation_key),
    ).fetchone()
    if terminal is None:
        raise SemanticProviderAttemptError(
            "SETTLEMENT_REQUIRED", "checkpointed 前缺少 Token 结算事件"
        )
    item = _row_dict(
        db.execute(
            "SELECT card_version_id FROM semantic_index_run_items "
            "WHERE run_id=? AND semantic_question_id=? AND status='succeeded' "
            "AND attempt_count=? AND card_version_id IS NOT NULL",
            (
                current["run_id"],
                current["semantic_question_id"],
                current["item_attempt"],
            ),
        )
    )
    if item is None:
        raise SemanticProviderAttemptError(
            "CHECKPOINT_REQUIRED", "checkpointed 前材料卡 run-item 尚未成功"
        )
    card = _row_dict(
        db.execute(
            "SELECT c.semantic_question_id,c.content_hash,c.analysis_schema_version,"
            "c.prompt_version,c.model_role,c.model_name,c.reasoning_effort,"
            "c.card_hash,c.status,r.analysis_schema_version AS run_schema_version,"
            "r.prompt_version AS run_prompt_version,r.model_role AS run_model_role,"
            "r.model_name AS run_model_name,r.reasoning_effort AS run_effort "
            "FROM semantic_card_versions c JOIN semantic_index_runs r ON r.id=? "
            "WHERE c.id=?",
            (current["run_id"], item["card_version_id"]),
        )
    )
    if (
        card is None
        or int(card["semantic_question_id"]) != int(current["semantic_question_id"])
        or card["content_hash"] != current["expected_content_hash"]
        or card["card_hash"] != current["validated_card_hash"]
        or card["status"] != "valid"
        or int(card["analysis_schema_version"]) != int(card["run_schema_version"])
        or card["prompt_version"] != card["run_prompt_version"]
        or card["model_role"] != card["run_model_role"]
        or card["model_name"] != card["run_model_name"]
        or card["reasoning_effort"] != card["run_effort"]
    ):
        raise SemanticProviderAttemptError(
            "CHECKPOINT_CONFLICT", "run-item 材料卡与 Provider attempt 不一致"
        )
    return _transition_in_transaction(
        db,
        reservation_key=reservation_key,
        target=SemanticProviderAttemptStatus.CHECKPOINTED,
        timestamp=timestamp,
        assignments="checkpointed_at=?",
        values=(_timestamp(timestamp),),
    )
