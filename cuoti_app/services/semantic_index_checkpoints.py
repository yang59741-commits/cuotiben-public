"""Crash-safe, provider-free checkpoints for semantic-index run items.

This stage owns only short SQLite transactions around run/item state.  Source
content is re-read from a validated last-good bundle outside the transaction;
no provider call, token accounting, or card persistence lives here.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta
import sqlite3
from typing import Any, Iterable, Mapping

from ..transactions import immediate_transaction
from .semantic_index import (
    SemanticRunItemStatus,
    ValidatedSemanticCard,
    assert_run_item_transition,
    assert_run_transition,
    semantic_content_hash,
)
from .semantic_index_sources import SemanticSourceBundle
from .semantic_index_repository import SemanticIndexRepository, StoredCard


class SemanticCheckpointError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticRunItemLease:
    run_id: str
    semantic_question_id: int
    source_kind: str
    candidate_id: str
    expected_content_hash: str
    change_kind: str
    lease_owner: str
    lease_expires_at: str
    attempt_count: int
    run_source_snapshot: str


@dataclass(frozen=True, slots=True)
class SourceVerification:
    lease: SemanticRunItemLease
    bundle_source_snapshot: str
    source_snapshot_changed: bool
    found: bool
    actual_content_hash: str | None

    @property
    def matches(self) -> bool:
        return self.found and self.actual_content_hash == self.lease.expected_content_hash


_ACTIVE_RUN_STATUSES = frozenset({"running"})
_CLAIMABLE_ITEM_STATUSES = ("pending", "retry_wait")
_TERMINAL_ITEM_STATUSES = ("succeeded", "failed", "cancelled", "skipped")


def _text(value: Any, field: str, *, maximum: int = 2_048) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticCheckpointError("VALIDATION", f"{field} 不能为空")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise SemanticCheckpointError("VALIDATION", f"{field} 超过 {maximum} 字符")
    return rendered


def _timestamp(value: Any) -> tuple[str, datetime]:
    rendered = _text(value, "timestamp", maximum=128)
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise SemanticCheckpointError("VALIDATION", "timestamp 必须是 ISO-8601 时间") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticCheckpointError("VALIDATION", "timestamp 必须包含时区")
    return rendered, parsed


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticCheckpointError("VALIDATION", f"{field} 必须是正整数")
    return value


def _run_row(db: Any, run_id: str) -> Any:
    row = db.execute("SELECT * FROM semantic_index_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise SemanticCheckpointError("RUN_NOT_FOUND", "semantic run 不存在")
    return row


def refresh_run_summary_in_transaction(db: Any, run_id: str, timestamp: str) -> str:
    if not db.in_transaction:
        raise SemanticCheckpointError(
            "TRANSACTION_REQUIRED", "run summary 刷新缺少调用者事务"
        )
    counts = db.execute(
        "SELECT COUNT(*) AS total,"
        "SUM(CASE WHEN status IN ('succeeded','failed','cancelled','skipped') THEN 1 ELSE 0 END) AS terminal,"
        "SUM(CASE WHEN status='succeeded' THEN 1 ELSE 0 END) AS completed,"
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed "
        "FROM semantic_index_run_items WHERE run_id=?",
        (run_id,),
    ).fetchone()
    total = int(counts["total"])
    terminal = int(counts["terminal"] or 0)
    completed = int(counts["completed"] or 0)
    failed = int(counts["failed"] or 0)
    run = _run_row(db, run_id)
    status = str(run["status"])
    finished_at = run["finished_at"]
    if status == "running" and terminal == total:
        target = "failed" if failed else "succeeded"
        assert_run_transition(status, target)
        status = target
        finished_at = timestamp
    db.execute(
        "UPDATE semantic_index_runs SET completed_count=?,failed_count=?,status=?,"
        "updated_at=?,finished_at=? WHERE id=?",
        (completed, failed, status, timestamp, finished_at, run_id),
    )
    return status


def activate_run(db: Any, *, run_id: str, timestamp: str) -> str:
    """Start a queued run or resume a normally paused run."""

    run_id = _text(run_id, "run_id", maximum=256)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        run = _run_row(db, run_id)
        current = str(run["status"])
        if current == "paused":
            assert_run_transition(current, "queued")
            db.execute(
                "UPDATE semantic_index_runs SET status='queued',updated_at=? WHERE id=?",
                (timestamp, run_id),
            )
            current = "queued"
        if current != "queued":
            raise SemanticCheckpointError("RUN_NOT_ACTIVATABLE", "run 当前不能启动或恢复")
        assert_run_transition(current, "running")
        db.execute(
            "UPDATE semantic_index_runs SET status='running',"
            "started_at=COALESCE(started_at,?),updated_at=?,finished_at=NULL WHERE id=?",
            (timestamp, timestamp, run_id),
        )
        return refresh_run_summary_in_transaction(db, run_id, timestamp)


def pause_run(db: Any, *, run_id: str, timestamp: str) -> None:
    run_id = _text(run_id, "run_id", maximum=256)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        run = _run_row(db, run_id)
        assert_run_transition(run["status"], "paused")
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(semantic_index_runs)")
        }
        if {"control_requested", "control_requested_at"} <= columns:
            db.execute(
                "UPDATE semantic_index_runs SET status='paused',control_requested=NULL,"
                "control_requested_at=NULL,updated_at=? WHERE id=?",
                (timestamp, run_id),
            )
        else:
            db.execute(
                "UPDATE semantic_index_runs SET status='paused',updated_at=? WHERE id=?",
                (timestamp, run_id),
            )


def cancel_run(db: Any, *, run_id: str, timestamp: str) -> None:
    """Cancel a run only after its coordinator has joined all active slots."""

    run_id = _text(run_id, "run_id", maximum=256)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        run = _run_row(db, run_id)
        current = str(run["status"])
        if current == "cancelled":
            return
        assert_run_transition(current, "cancelled")
        db.execute(
            "UPDATE semantic_index_run_items SET status='cancelled',lease_owner=NULL,"
            "lease_expires_at=NULL,updated_at=? WHERE run_id=? AND status IN "
            "('pending','leased','retry_wait')",
            (timestamp, run_id),
        )
        columns = {
            str(row[1]) for row in db.execute("PRAGMA table_info(semantic_index_runs)")
        }
        if {"control_requested", "control_requested_at"} <= columns:
            db.execute(
                "UPDATE semantic_index_runs SET status='cancelled',"
                "control_requested=NULL,control_requested_at=NULL,updated_at=?,"
                "finished_at=? WHERE id=?",
                (timestamp, timestamp, run_id),
            )
        else:
            db.execute(
                "UPDATE semantic_index_runs SET status='cancelled',updated_at=?,"
                "finished_at=? WHERE id=?",
                (timestamp, timestamp, run_id),
            )


def recover_expired_leases(db: Any, *, run_id: str, timestamp: str) -> int:
    """Return expired work to retry_wait without consuming another attempt."""

    run_id = _text(run_id, "run_id", maximum=256)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        _run_row(db, run_id)
        rows = db.execute(
            "SELECT semantic_question_id FROM semantic_index_run_items "
            "WHERE run_id=? AND status='leased' AND lease_expires_at IS NOT NULL "
            "AND julianday(lease_expires_at)<=julianday(?)",
            (run_id, timestamp),
        ).fetchall()
        for _row in rows:
            assert_run_item_transition("leased", "retry_wait")
        if rows:
            db.execute(
                "UPDATE semantic_index_run_items SET status='retry_wait',lease_owner=NULL,"
                "lease_expires_at=NULL,available_at=?,error_code='lease_expired',"
                "error_summary='处理进程中断，租约过期后等待重试',updated_at=? "
                "WHERE run_id=? AND status='leased' AND lease_expires_at IS NOT NULL "
                "AND julianday(lease_expires_at)<=julianday(?)",
                (timestamp, timestamp, run_id, timestamp),
            )
        refresh_run_summary_in_transaction(db, run_id, timestamp)
        return len(rows)


def release_unstarted_leases(db: Any, *, run_id: str, timestamp: str) -> int:
    """Release only leases that have never crossed the Provider-attempt fence."""

    run_id = _text(run_id, "run_id", maximum=256)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        _run_row(db, run_id)
        rows = db.execute(
            "SELECT ri.semantic_question_id FROM semantic_index_run_items ri "
            "WHERE ri.run_id=? AND ri.status='leased' AND NOT EXISTS ("
            "SELECT 1 FROM semantic_index_provider_attempts pa WHERE pa.run_id=ri.run_id "
            "AND pa.semantic_question_id=ri.semantic_question_id "
            "AND pa.item_attempt=ri.attempt_count)",
            (run_id,),
        ).fetchall()
        for _row in rows:
            assert_run_item_transition("leased", "retry_wait")
        if rows:
            db.execute(
                "UPDATE semantic_index_run_items SET status='retry_wait',"
                "lease_owner=NULL,lease_expires_at=NULL,available_at=?,updated_at=? "
                "WHERE run_id=? AND status='leased' AND NOT EXISTS ("
                "SELECT 1 FROM semantic_index_provider_attempts pa "
                "WHERE pa.run_id=semantic_index_run_items.run_id "
                "AND pa.semantic_question_id=semantic_index_run_items.semantic_question_id "
                "AND pa.item_attempt=semantic_index_run_items.attempt_count)",
                (timestamp, timestamp, run_id),
            )
        return len(rows)


def degrade_run_to_single_slot(
    db: Any, *, run_id: str, reason: str, timestamp: str
) -> bool:
    """Persist a conservative one-slot downgrade at a completed batch boundary."""

    run_id = _text(run_id, "run_id", maximum=256)
    reason = _text(reason, "reason", maximum=128)
    timestamp, _ = _timestamp(timestamp)
    with immediate_transaction(db):
        _run_row(db, run_id)
        return bool(
            db.execute(
                "UPDATE semantic_index_runs SET effective_concurrency=1,"
                "degrade_reason=?,updated_at=? WHERE id=? AND effective_concurrency=2",
                (reason, timestamp, run_id),
            ).rowcount
        )


def claim_next_item(
    db: Any,
    *,
    run_id: str,
    lease_owner: str,
    lease_seconds: int,
    timestamp: str,
) -> SemanticRunItemLease | None:
    """Claim one due item while honoring the frozen run concurrency limit."""

    run_id = _text(run_id, "run_id", maximum=256)
    lease_owner = _text(lease_owner, "lease_owner", maximum=256)
    lease_seconds = _positive_integer(lease_seconds, "lease_seconds")
    timestamp, now = _timestamp(timestamp)
    lease_expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
    with immediate_transaction(db):
        run = _run_row(db, run_id)
        if run["status"] not in _ACTIVE_RUN_STATUSES:
            raise SemanticCheckpointError("RUN_NOT_RUNNING", "只有 running run 可以领取题目")
        db.execute(
            "UPDATE semantic_index_run_items SET status='retry_wait',lease_owner=NULL,"
            "lease_expires_at=NULL,available_at=?,error_code='lease_expired',"
            "error_summary='处理进程中断，租约过期后等待重试',updated_at=? "
            "WHERE run_id=? AND status='leased' AND lease_expires_at IS NOT NULL "
            "AND julianday(lease_expires_at)<=julianday(?)",
            (timestamp, timestamp, run_id, timestamp),
        )
        active_leases = int(
            db.execute(
                "SELECT COUNT(*) FROM semantic_index_run_items "
                "WHERE run_id=? AND status='leased'",
                (run_id,),
            ).fetchone()[0]
        )
        if active_leases >= int(run["effective_concurrency"]):
            return None
        row = db.execute(
            "SELECT ri.*,q.source_kind,q.candidate_id FROM semantic_index_run_items ri "
            "JOIN semantic_questions q ON q.id=ri.semantic_question_id "
            "WHERE ri.run_id=? AND ri.status IN ('pending','retry_wait') "
            "AND julianday(ri.available_at)<=julianday(?) "
            "ORDER BY ri.available_at,ri.semantic_question_id LIMIT 1",
            (run_id, timestamp),
        ).fetchone()
        if row is None:
            refresh_run_summary_in_transaction(db, run_id, timestamp)
            return None
        assert_run_item_transition(row["status"], SemanticRunItemStatus.LEASED)
        try:
            changed = db.execute(
                "UPDATE semantic_index_run_items SET status='leased',lease_owner=?,"
                "lease_expires_at=?,attempt_count=attempt_count+1,error_code=NULL,"
                "error_summary=NULL,updated_at=? WHERE run_id=? AND semantic_question_id=? "
                "AND status=? AND expected_content_hash=?",
                (
                    lease_owner,
                    lease_expires_at,
                    timestamp,
                    run_id,
                    row["semantic_question_id"],
                    row["status"],
                    row["expected_content_hash"],
                ),
            ).rowcount
        except sqlite3.IntegrityError as exc:
            raise SemanticCheckpointError("CLAIM_CONFLICT", "run item 领取竞争失败") from exc
        if changed != 1:
            raise SemanticCheckpointError("CLAIM_CONFLICT", "run item 已被其他处理者领取")
        return SemanticRunItemLease(
            run_id=run_id,
            semantic_question_id=int(row["semantic_question_id"]),
            source_kind=str(row["source_kind"]),
            candidate_id=str(row["candidate_id"]),
            expected_content_hash=str(row["expected_content_hash"]),
            change_kind=str(row["change_kind"]),
            lease_owner=lease_owner,
            lease_expires_at=lease_expires_at,
            attempt_count=int(row["attempt_count"]) + 1,
            run_source_snapshot=str(run["source_snapshot"]),
        )


def heartbeat_item(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    lease_seconds: int,
    timestamp: str,
) -> str:
    lease_seconds = _positive_integer(lease_seconds, "lease_seconds")
    timestamp, now = _timestamp(timestamp)
    expires = (now + timedelta(seconds=lease_seconds)).isoformat()
    with immediate_transaction(db):
        changed = db.execute(
            "UPDATE semantic_index_run_items SET lease_expires_at=?,updated_at=? "
            "WHERE run_id=? AND semantic_question_id=? AND status='leased' "
            "AND lease_owner=? AND expected_content_hash=? "
            "AND attempt_count=? "
            "AND julianday(lease_expires_at)>julianday(?)",
            (
                expires,
                timestamp,
                lease.run_id,
                lease.semantic_question_id,
                lease.lease_owner,
                lease.expected_content_hash,
                lease.attempt_count,
                timestamp,
            ),
        ).rowcount
        if changed != 1:
            raise SemanticCheckpointError("LEASE_LOST", "run item 租约已失效")
    return expires


def verify_leased_sources(
    bundle: SemanticSourceBundle,
    leases: Iterable[SemanticRunItemLease],
) -> tuple[SourceVerification, ...]:
    """Re-read full source content by frozen identity and recompute its hash."""

    by_identity: dict[tuple[str, str], Any] = {}
    for candidate in bundle.candidates:
        identity = (candidate.source_kind, candidate.candidate_id)
        if identity in by_identity:
            raise SemanticCheckpointError("SOURCE_DUPLICATE", "last-good 来源包含重复题目身份")
        by_identity[identity] = candidate
    results = []
    for lease in leases:
        candidate = by_identity.get((lease.source_kind, lease.candidate_id))
        found = candidate is not None and not candidate.excluded
        actual_hash = None if not found else semantic_content_hash(
            candidate.content,
            figure_references=candidate.figure_references,
            options=candidate.options,
            conditions=candidate.conditions,
        )
        results.append(
            SourceVerification(
                lease=lease,
                bundle_source_snapshot=bundle.source_snapshot,
                source_snapshot_changed=bundle.source_snapshot != lease.run_source_snapshot,
                found=found,
                actual_content_hash=actual_hash,
            )
        )
    return tuple(results)


def _checkpoint_leased(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    target: str,
    timestamp: str,
    available_at: str,
    error_code: str,
    error_summary: str,
    _caller_owned: bool = False,
    _refresh_summary: bool = True,
) -> str:
    timestamp, _ = _timestamp(timestamp)
    available_at, _ = _timestamp(available_at)
    error_code = _text(error_code, "error_code", maximum=128)
    error_summary = _text(error_summary, "error_summary", maximum=1_024)
    assert_run_item_transition("leased", target)
    if _caller_owned and not db.in_transaction:
        raise SemanticCheckpointError(
            "TRANSACTION_REQUIRED", "caller-owned run-item checkpoint 缺少事务"
        )
    transaction = nullcontext() if _caller_owned else immediate_transaction(db)
    with transaction:
        run = _run_row(db, lease.run_id)
        if run["status"] != "running":
            raise SemanticCheckpointError("RUN_NOT_RUNNING", "run 已不再接受 checkpoint")
        changed = db.execute(
            "UPDATE semantic_index_run_items SET status=?,lease_owner=NULL,"
            "lease_expires_at=NULL,available_at=?,error_code=?,error_summary=?,updated_at=? "
            "WHERE run_id=? AND semantic_question_id=? AND status='leased' "
            "AND lease_owner=? AND expected_content_hash=? "
            "AND attempt_count=? "
            "AND julianday(lease_expires_at)>julianday(?)",
            (
                target,
                available_at,
                error_code,
                error_summary,
                timestamp,
                lease.run_id,
                lease.semantic_question_id,
                lease.lease_owner,
                lease.expected_content_hash,
                lease.attempt_count,
                timestamp,
            ),
        ).rowcount
        if changed != 1:
            raise SemanticCheckpointError("LEASE_LOST", "迟到或失效 checkpoint 已丢弃")
        if _refresh_summary:
            return refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
        return str(_run_row(db, lease.run_id)["status"])


def checkpoint_source_changed(
    db: Any, *, verification: SourceVerification, timestamp: str
) -> str:
    if verification.matches:
        raise SemanticCheckpointError("SOURCE_STILL_MATCHES", "内容未变化，不能标记 skipped")
    detail = "题目已从 last-good 来源消失" if not verification.found else "题目内容 hash 已变化"
    return _checkpoint_leased(
        db,
        lease=verification.lease,
        target="skipped",
        timestamp=timestamp,
        available_at=timestamp,
        error_code="question_content_changed",
        error_summary=detail,
    )


def checkpoint_success(
    db: Any,
    *,
    verification: SourceVerification,
    card: ValidatedSemanticCard | Mapping[str, Any],
    timestamp: str,
    provider_request_id_hash: str | None = None,
    _caller_owned: bool = False,
    _refresh_summary: bool = True,
) -> StoredCard:
    """Atomically publish one valid card and bind its exact leased item."""

    if not verification.matches:
        raise SemanticCheckpointError(
            "SOURCE_CONTENT_CHANGED", "来源题干未通过冻结 content hash 复核"
        )
    timestamp, _ = _timestamp(timestamp)
    lease = verification.lease
    if provider_request_id_hash is not None:
        provider_request_id_hash = _text(
            provider_request_id_hash, "provider_request_id_hash", maximum=64
        ).lower()
        if len(provider_request_id_hash) != 64 or any(
            char not in "0123456789abcdef" for char in provider_request_id_hash
        ):
            raise SemanticCheckpointError(
                "VALIDATION", "provider_request_id_hash 不是 SHA-256 摘要"
            )
    if _caller_owned and not db.in_transaction:
        raise SemanticCheckpointError(
            "TRANSACTION_REQUIRED", "caller-owned success checkpoint 缺少事务"
        )
    transaction = nullcontext() if _caller_owned else immediate_transaction(db)
    with transaction:
        run = _run_row(db, lease.run_id)
        if run["status"] != "running":
            raise SemanticCheckpointError("RUN_NOT_RUNNING", "run 已不再接受 checkpoint")
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
            raise SemanticCheckpointError("LEASE_LOST", "迟到或失效 checkpoint 已丢弃")
        stored = SemanticIndexRepository(db)._store_valid_card_in_transaction(
            question_id=lease.semantic_question_id,
            expected_hash=lease.expected_content_hash,
            card=card,
            analysis_schema_version=int(run["analysis_schema_version"]),
            prompt_version=str(run["prompt_version"]),
            model_role=str(run["model_role"]),
            model_name=str(run["model_name"]),
            reasoning_effort=str(run["reasoning_effort"]),
            timestamp=timestamp,
            provider_request_id_hash=provider_request_id_hash,
        )
        assert_run_item_transition("leased", "succeeded")
        changed = db.execute(
            "UPDATE semantic_index_run_items SET status='succeeded',card_version_id=?,"
            "lease_owner=NULL,lease_expires_at=NULL,error_code=NULL,error_summary=NULL,"
            "updated_at=? WHERE run_id=? AND semantic_question_id=? AND status='leased' "
            "AND lease_owner=? AND expected_content_hash=? "
            "AND attempt_count=? "
            "AND julianday(lease_expires_at)>julianday(?)",
            (
                stored.card_version_id,
                timestamp,
                lease.run_id,
                lease.semantic_question_id,
                lease.lease_owner,
                lease.expected_content_hash,
                lease.attempt_count,
                timestamp,
            ),
        ).rowcount
        if changed != 1:
            raise SemanticCheckpointError("LEASE_LOST", "迟到或失效 checkpoint 已丢弃")
        if _refresh_summary:
            refresh_run_summary_in_transaction(db, lease.run_id, timestamp)
        return stored


def checkpoint_success_in_transaction(
    db: Any,
    *,
    verification: SourceVerification,
    card: ValidatedSemanticCard | Mapping[str, Any],
    timestamp: str,
    provider_request_id_hash: str | None = None,
) -> StoredCard:
    return checkpoint_success(
        db,
        verification=verification,
        card=card,
        timestamp=timestamp,
        provider_request_id_hash=provider_request_id_hash,
        _caller_owned=True,
        _refresh_summary=False,
    )


def checkpoint_failure(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    retryable: bool,
    timestamp: str,
    available_at: str | None,
    error_code: str,
    error_summary: str,
) -> str:
    if not isinstance(retryable, bool):
        raise SemanticCheckpointError("VALIDATION", "retryable 必须是布尔值")
    if retryable and available_at is None:
        raise SemanticCheckpointError("VALIDATION", "可重试失败必须给出 available_at")
    target = "retry_wait" if retryable else "failed"
    return _checkpoint_leased(
        db,
        lease=lease,
        target=target,
        timestamp=timestamp,
        available_at=available_at or timestamp,
        error_code=error_code,
        error_summary=error_summary,
    )


def checkpoint_failure_in_transaction(
    db: Any,
    *,
    lease: SemanticRunItemLease,
    retryable: bool,
    timestamp: str,
    available_at: str | None,
    error_code: str,
    error_summary: str,
) -> str:
    if not isinstance(retryable, bool):
        raise SemanticCheckpointError("VALIDATION", "retryable 必须是布尔值")
    if retryable and available_at is None:
        raise SemanticCheckpointError("VALIDATION", "可重试失败必须给出 available_at")
    return _checkpoint_leased(
        db,
        lease=lease,
        target="retry_wait" if retryable else "failed",
        timestamp=timestamp,
        available_at=available_at or timestamp,
        error_code=error_code,
        error_summary=error_summary,
        _caller_owned=True,
        _refresh_summary=False,
    )


def checkpoint_source_changed_in_transaction(
    db: Any, *, verification: SourceVerification, timestamp: str
) -> str:
    if verification.matches:
        raise SemanticCheckpointError("SOURCE_STILL_MATCHES", "内容未变化，不能标记 skipped")
    detail = "题目已从 last-good 来源消失" if not verification.found else "题目内容 hash 已变化"
    return _checkpoint_leased(
        db,
        lease=verification.lease,
        target="skipped",
        timestamp=timestamp,
        available_at=timestamp,
        error_code="question_content_changed",
        error_summary=detail,
        _caller_owned=True,
        _refresh_summary=False,
    )


def checkpoint_cancelled_in_transaction(
    db: Any, *, lease: SemanticRunItemLease, timestamp: str
) -> str:
    return _checkpoint_leased(
        db,
        lease=lease,
        target="cancelled",
        timestamp=timestamp,
        available_at=timestamp,
        error_code="cancelled",
        error_summary="用户已请求取消语义材料卡处理",
        _caller_owned=True,
        _refresh_summary=False,
    )
