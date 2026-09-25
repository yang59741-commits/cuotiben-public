"""Durable, framework-independent coordination for local background jobs."""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping

from ..database import connect_database
from ..transactions import begin_immediate
from .job_contracts import (
    ACTIVE_STATUSES,
    JOB_KIND_POLICIES,
    JOB_LANES,
    JOB_METADATA_RETENTION_DAYS,
    OCR_RESULT_RETENTION_DAYS,
    STAGED_INPUT_FIELD,
    SUPPORTED_JOB_KINDS,
    TERMINAL_STATUSES,
    DeferredJobError,
    InvalidJobState,
    JobCancelled,
    JobConflict,
    JobError,
    JobLeaseLost,
    JobNotFound,
    JobOutcome,
    PermanentJobError,
    RetryableJobError,
    UnknownJobKind,
    canonical_payload as _canonical_payload,
    normalize_error_details as _normalize_error_details,
    timestamp as _timestamp,
    utcnow as _utcnow,
)
from .job_serialization import deserialize_job, publication_head_state
from .job_process import close_process_tree, start_owned_process, terminate_process_tree
from .job_worker import WorkerStopRequested, execute_next, run_worker_loop
from .exam_time_policy import (
    ExamDeadlineExceeded,
    PROGRESS_STAGE_NAMES,
    bounded_stage_timeout,
    build_timing_receipt,
    enforce_remaining_seconds,
    freeze_policy,
    merge_timing_accumulators,
    normalize_policy_spec,
)
from .exam_generation_profile_freeze import (
    freeze_exam_generation_profile_in_transaction,
    profile_freeze_database_error,
)
from .exam_generation_request_binding import (
    EXAM_GENERATION_INPUT_IDENTITY_VERSION,
    JOB_INPUT_IDENTITY_VERSION,
    ExamGenerationRequestIdentityError,
    attach_generation_request_identity,
    logical_request_payload,
    prepare_exam_generation_submission,
    verify_persisted_input,
)


logger = logging.getLogger(__name__)


Handler = Callable[["JobContext", dict[str, Any]], Any]
_registered_handlers: dict[str, Handler] = {}
_registry_lock = threading.Lock()


def register_job_handler(kind: str, handler: Handler, *, replace: bool = False) -> None:
    """Register one supported kind without importing any business route."""

    if kind not in SUPPORTED_JOB_KINDS:
        raise UnknownJobKind(f"不支持的任务类型: {kind}")
    if not callable(handler):
        raise TypeError("任务处理器必须可调用")
    with _registry_lock:
        if kind in _registered_handlers and not replace:
            raise JobConflict(f"任务类型 {kind} 已注册处理器")
        _registered_handlers[kind] = handler


def unregister_job_handler(kind: str) -> None:
    """Remove a handler, primarily for orderly shutdown and isolated tests."""

    with _registry_lock:
        _registered_handlers.pop(kind, None)


def registered_job_handlers() -> dict[str, Handler]:
    with _registry_lock:
        return dict(_registered_handlers)


@dataclass(slots=True)
class JobContext:
    coordinator: "JobCoordinator"
    job_id: str
    lease_attempt: int | None = None
    worker_stop_event: threading.Event | None = None
    receipt_only: bool = False
    _timing_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _timing_stage: str | None = field(default=None, init=False, repr=False)
    _timing_stage_started: datetime | None = field(
        default=None, init=False, repr=False
    )
    _timing_stage_elapsed_ms: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _timing_metrics: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _timing_finished: bool = field(default=False, init=False, repr=False)
    _deadline_job_snapshot: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )

    def _deadline_job(self) -> dict[str, Any]:
        # Deadline metadata is immutable after submission.  Cache it before
        # business code opens a write transaction so deadline fences never
        # introduce a nested SQLite read against the same database file.
        if self._deadline_job_snapshot is None:
            job = self.coordinator.get_job(self.job_id)
            if not isinstance(job, Mapping):
                job = {}
            raw_policy = job.get("time_policy")
            self._deadline_job_snapshot = {
                "kind": job.get("kind"),
                "deadline_at": job.get("deadline_at"),
                "time_policy": (
                    dict(raw_policy) if isinstance(raw_policy, Mapping) else {}
                ),
            }
        return self._deadline_job_snapshot

    def frozen_time_policy(self) -> dict[str, Any]:
        return dict(self._deadline_job().get("time_policy") or {})

    def heartbeat(self) -> None:
        cancel_requested = self.coordinator.heartbeat(
            self.job_id, lease_attempt=self.lease_attempt
        )
        if cancel_requested and not self.receipt_only:
            raise JobCancelled("任务已请求取消")

    def update_progress(
        self,
        current: int,
        total: int | None = None,
        message: str = "",
    ) -> None:
        stage = PROGRESS_STAGE_NAMES.get(int(current), f"stage_{int(current)}")
        self.raise_if_worker_stopping()
        self.raise_if_deadline_exceeded(stage=stage)
        cancel_requested = self.coordinator.update_progress(
            self.job_id,
            current=current,
            total=total,
            message=message,
            lease_attempt=self.lease_attempt,
        )
        if cancel_requested and not self.receipt_only:
            raise JobCancelled("任务已请求取消")
        self._transition_timing_stage(stage)
        self.raise_if_worker_stopping()

    def _transition_timing_stage(self, stage: str) -> None:
        now = self.coordinator.clock()
        with self._timing_lock:
            if self._timing_finished or stage == self._timing_stage:
                return
            if self._timing_stage is not None and self._timing_stage_started is not None:
                elapsed = max(
                    0,
                    round((now - self._timing_stage_started).total_seconds() * 1_000),
                )
                self._timing_stage_elapsed_ms[self._timing_stage] = (
                    self._timing_stage_elapsed_ms.get(self._timing_stage, 0)
                    + elapsed
                )
            self._timing_stage = stage
            self._timing_stage_started = now

    def add_exam_metric(self, name: str, amount: int = 1, *, maximum: bool = False) -> None:
        if name not in {
            "model_calls",
            "model_retry_count",
            "provider_calls",
            "candidate_count",
            "pdf_render_elapsed_ms",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "total_tokens",
            "usage_missing_calls",
        }:
            raise ValueError("未知组卷耗时指标")
        if type(amount) is not int or amount < 0:
            raise ValueError("组卷耗时指标必须是非负整数")
        with self._timing_lock:
            if maximum:
                self._timing_metrics[name] = max(
                    self._timing_metrics.get(name, 0), amount
                )
            else:
                self._timing_metrics[name] = self._timing_metrics.get(name, 0) + amount

    def snapshot_exam_metrics(self) -> dict[str, int]:
        """Return a read-only copy of current attempt telemetry."""

        with self._timing_lock:
            return dict(self._timing_metrics)

    def finish_exam_timing(self) -> dict[str, Any]:
        now = self.coordinator.clock()
        with self._timing_lock:
            if not self._timing_finished:
                if self._timing_stage is not None and self._timing_stage_started is not None:
                    elapsed = max(
                        0,
                        round((now - self._timing_stage_started).total_seconds() * 1_000),
                    )
                    self._timing_stage_elapsed_ms[self._timing_stage] = (
                        self._timing_stage_elapsed_ms.get(self._timing_stage, 0)
                        + elapsed
                    )
                self._timing_finished = True
            return {
                "stage_elapsed_ms": dict(self._timing_stage_elapsed_ms),
                "metrics": dict(self._timing_metrics),
                "failed_stage": self._timing_stage or "execution",
            }

    def raise_if_worker_stopping(self) -> None:
        if self.worker_stop_event is not None and self.worker_stop_event.is_set():
            raise WorkerStopRequested("后台任务 worker 正在停止")

    def is_cancel_requested(self) -> bool:
        self.raise_if_worker_stopping()
        cancel_requested = self.coordinator.is_cancel_requested(
            self.job_id, lease_attempt=self.lease_attempt
        )
        return bool(cancel_requested) and not self.receipt_only

    def assert_lease(self) -> None:
        self.coordinator.assert_lease(
            self.job_id, lease_attempt=self.lease_attempt
        )

    def raise_if_cancelled(self, *, stage: str = "execution") -> None:
        if self.is_cancel_requested():
            raise JobCancelled("任务已请求取消")
        self.raise_if_deadline_exceeded(stage=stage)

    def raise_if_deadline_exceeded(self, *, stage: str = "execution") -> None:
        if self.receipt_only:
            self.raise_if_worker_stopping()
            self.assert_lease()
            return
        enforce_remaining_seconds(
            self._deadline_job(),
            now=self.coordinator.clock(),
            stage=stage,
        )

    def bounded_timeout(
        self,
        requested_seconds: float | None,
        *,
        stage: str,
        reserve_seconds: float = 0.0,
    ) -> float | None:
        self.raise_if_worker_stopping()
        if self.receipt_only:
            self.assert_lease()
            return requested_seconds
        return bounded_stage_timeout(
            self._deadline_job(),
            requested_seconds,
            now=self.coordinator.clock(),
            stage=stage,
            reserve_seconds=reserve_seconds,
        )

    def run_cancellable_process(
        self,
        args,
        *,
        cwd: str | Path | None = None,
        timeout: float | None = None,
        check_interval: float = 0.2,
        stdin=None,
        stdout=None,
        stderr=None,
        creationflags: int = 0,
    ) -> subprocess.CompletedProcess:
        """Run one process while cooperatively enforcing cancellation and timeout."""

        self.raise_if_cancelled(stage="external_process")
        timeout = self.bounded_timeout(
            timeout,
            stage="external_process",
            reserve_seconds=0.0,
        )
        process = start_owned_process(
            args,
            cwd=str(cwd) if cwd is not None else None,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )
        started = self.coordinator.clock()
        interval = max(0.05, float(check_interval))
        completed = False
        try:
            while process.poll() is None:
                self.raise_if_cancelled(stage="external_process")
                if timeout is not None:
                    elapsed = (self.coordinator.clock() - started).total_seconds()
                    if elapsed >= timeout:
                        raise subprocess.TimeoutExpired(args, timeout)
                time.sleep(interval)
            completed = True
            return subprocess.CompletedProcess(args, process.returncode)
        finally:
            elapsed_ms = max(
                0,
                round((self.coordinator.clock() - started).total_seconds() * 1_000),
            )
            self.add_exam_metric("pdf_render_elapsed_ms", elapsed_ms)
            if completed:
                close_process_tree(process)
            else:
                terminate_process_tree(process)


class JobCoordinator:
    """Coordinate durable jobs using short SQLite transactions and leases."""

    def __init__(
        self,
        database: str | Path,
        *,
        worker_id: str | None = None,
        lease_seconds: float = 30,
        clock: Callable[[], datetime] = _utcnow,
        handlers: Mapping[str, Handler] | None = None,
        staged_input_roots: tuple[str | Path, ...] = (),
        lane: str | None = None,
        aging_seconds: float = 300,
        outcome_observer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.database = str(database)
        self.worker_id = worker_id or f"worker-{uuid.uuid4().hex}"
        self.lease_seconds = max(1.0, float(lease_seconds))
        self.clock = clock
        if lane is not None and lane not in JOB_LANES:
            raise ValueError("lane 必须是 fast 或 heavy")
        self.lane = lane
        self.aging_seconds = max(1.0, float(aging_seconds))
        self.outcome_observer = outcome_observer
        self._worker_stop_event: threading.Event | None = None
        self.staged_input_roots = tuple(
            Path(root).resolve() for root in staged_input_roots
        )
        self.handlers = registered_job_handlers()
        if handlers:
            for kind, handler in handlers.items():
                self.register_handler(kind, handler)

    def register_handler(self, kind: str, handler: Handler) -> None:
        if kind not in SUPPORTED_JOB_KINDS:
            raise UnknownJobKind(f"不支持的任务类型: {kind}")
        if not callable(handler):
            raise TypeError("任务处理器必须可调用")
        self.handlers[kind] = handler

    def _notify_terminal(self, job: dict[str, Any]) -> None:
        if not self.outcome_observer or job.get("status") not in TERMINAL_STATUSES:
            return
        try:
            self.outcome_observer(job)
        except Exception:
            logger.exception("background job outcome observer failed")

    def _connect(self) -> sqlite3.Connection:
        return connect_database(self.database, role="background", timeout=5)

    @contextmanager
    def _connection(self):
        db = self._connect()
        try:
            with db:
                yield db
        finally:
            db.close()

    def _now(self) -> tuple[datetime, str]:
        value = self.clock()
        return value, _timestamp(value)

    @staticmethod
    def _deserialize_job(
        db: sqlite3.Connection,
        row: sqlite3.Row,
        exam_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        return deserialize_job(db, row, exam_ids)

    @staticmethod
    def _publication_head_state(
        db: sqlite3.Connection, job_id: str
    ) -> str | None:
        """Return the native publication authority across v46 and D2."""

        return publication_head_state(db, job_id)

    @classmethod
    def _publication_is_committed(
        cls, db: sqlite3.Connection, job_id: str
    ) -> bool:
        return cls._publication_head_state(db, job_id) in {
            "published_pending_receipt", "completed"
        }

    @staticmethod
    def _has_generation_checkpoint(
        db: sqlite3.Connection, job_id: str
    ) -> bool:
        """Recognize only native immutable Stage 3-7 job-scoped facts."""

        tables = (
            "exam_generation_stage3_contract_receipts",
            "exam_generation_stage4_fact_receipts",
            "exam_generation_stage5_heads",
            "exam_generation_stage6_heads",
            "exam_generation_stage7_heads",
        )
        try:
            existing = {
                str(row["name"])
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name LIKE 'exam_generation_stage%'"
                ).fetchall()
            }
            return any(
                table in existing
                and db.execute(
                    f"SELECT 1 FROM {table} WHERE job_id=? LIMIT 1", (job_id,)
                ).fetchone()
                is not None
                for table in tables
            )
        except sqlite3.DatabaseError:
            return False

    def _keep_publication_pending(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        now: str,
        available_at: str | None = None,
        timing_json: str | None = None,
        attempt_error_code: str = "receipt_pending",
    ) -> None:
        """Release a lease without allowing a committed publication to regress."""

        db.execute(
            "UPDATE background_jobs SET status='published_pending_receipt',"
            "cancel_requested=0,lease_owner=NULL,lease_expires_at=NULL,"
            "heartbeat_at=NULL,error_code='receipt_pending',"
            "error_message='发布已完成，正在恢复发布回执',"
            "error_category='dependency',error_retryable=1,"
            "error_correlation_id=NULL,error_details_json=NULL,"
            "exam_timing_json=COALESCE(?,exam_timing_json),"
            "available_at=?,updated_at=?,finished_at=NULL "
            "WHERE id=? AND attempts=?",
            (
                timing_json,
                available_at or now,
                now,
                row["id"],
                int(row["attempts"]),
            ),
        )
        db.execute(
            "UPDATE job_attempts SET status='interrupted',finished_at=?,error_code=? "
            "WHERE job_id=? AND attempt_number=? AND status='running'",
            (now, attempt_error_code, row["id"], int(row["attempts"])),
        )

    @staticmethod
    def _stored_exam_timing(row: sqlite3.Row) -> dict[str, Any]:
        if "exam_timing_json" not in row.keys() or not row["exam_timing_json"]:
            return {}
        try:
            value = json.loads(row["exam_timing_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _final_exam_timing(
        self,
        db: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        finished_at: str,
        attempt_timing: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        accumulated = merge_timing_accumulators(
            self._stored_exam_timing(row), attempt_timing
        )
        if row["kind"] != "exam-generation":
            return accumulated, None
        policy = self._deserialize_job(db, row, [])["time_policy"] or {
            "mode": "observe",
            "version": "legacy-unversioned",
            "target_elapsed_ms": 1_200_000,
            "final_reserve_ms": 90_000,
            "deadline_at": None,
        }
        receipt = build_timing_receipt(
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=finished_at,
            time_policy=policy,
            accumulator=accumulated,
        )
        return accumulated, receipt

    def submit(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> tuple[dict[str, Any], bool]:
        if kind not in SUPPORTED_JOB_KINDS:
            raise UnknownJobKind(f"不支持的任务类型: {kind}")
        if not isinstance(payload, Mapping):
            raise ValueError("任务输入必须是 JSON 对象")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须在 1 到 10 之间")
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key).strip()
            if not idempotency_key or len(idempotency_key) > 200:
                raise ValueError("幂等键长度必须在 1 到 200 之间")
        # The logical input hash excludes the absolute deadline timestamp so
        # an idempotent replay can find the original job without manufacturing
        # a new deadline.  The frozen timestamp is added only after the replay
        # lookup has proved this is a new row.
        submission_payload = dict(payload)
        if kind == "exam-generation":
            submission_payload = prepare_exam_generation_submission(
                submission_payload
            )
            # The server-owned policy is frozen metadata, not caller intent.
            # Excluding it makes a replay return the original frozen policy
            # even if administrators have since activated a new version.
            logical_payload = logical_request_payload(submission_payload)
        else:
            logical_payload = submission_payload
        payload_json, logical_request_hash = _canonical_payload(logical_payload)
        lane, priority = JOB_KIND_POLICIES[kind]
        _now_value, now = self._now()
        job_id = uuid.uuid4().hex
        with self._connection() as db:
            try:
                begin_immediate(db)
            except sqlite3.DatabaseError as exc:
                if (
                    kind == "exam-generation"
                    and str(payload.get("source") or "free") == "past"
                ):
                    raise profile_freeze_database_error(exc) from exc
                raise
            if idempotency_key is not None:
                existing = db.execute(
                    "SELECT * FROM background_jobs WHERE kind=? AND idempotency_key=?",
                    (kind, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["input_hash"] != logical_request_hash:
                        raise JobConflict("同一幂等键已用于不同任务输入")
                    input_row = db.execute(
                        "SELECT payload_json,payload_hash,identity_version "
                        "FROM job_inputs "
                        "WHERE job_id=?",
                        (existing["id"],),
                    ).fetchone()
                    try:
                        if input_row is None:
                            raise ExamGenerationRequestIdentityError(
                                "job_input_missing"
                            )
                        verify_persisted_input(
                            input_row["payload_json"],
                            input_row["payload_hash"],
                            identity_version=input_row["identity_version"],
                            job_kind=kind,
                            job_status=existing["status"],
                            allow_legacy_exam_read=(
                                existing["status"] == "succeeded"
                            ),
                        )
                    except ExamGenerationRequestIdentityError as exc:
                        raise JobConflict(
                            "已有任务冻结输入完整性校验失败"
                        ) from exc
                    return self._deserialize_job(db, existing), False
            deadline_at = None
            deadline_policy_mode = None
            deadline_policy_version = None
            if kind == "exam-generation":
                normalized_payload = freeze_exam_generation_profile_in_transaction(
                    db,
                    submission_payload,
                )
                policy = freeze_policy(
                    normalize_policy_spec(normalized_payload.get("exam_time_policy")),
                    _now_value,
                )
                normalized_payload["exam_time_policy"] = policy.to_dict()
                from .exam_generation_request import attach_current_scoring_protocol

                normalized_payload = attach_current_scoring_protocol(normalized_payload)
                normalized_payload = attach_generation_request_identity(
                    normalized_payload
                )
                payload_json, payload_hash = _canonical_payload(normalized_payload)
                deadline_at = policy.deadline_at
                deadline_policy_mode = policy.spec.mode
                deadline_policy_version = policy.spec.version
            else:
                payload_hash = logical_request_hash
            identity_version = (
                EXAM_GENERATION_INPUT_IDENTITY_VERSION
                if kind == "exam-generation"
                else JOB_INPUT_IDENTITY_VERSION
            )
            db.execute(
                "INSERT INTO background_jobs("
                "id,kind,status,idempotency_key,input_hash,lane,priority,max_attempts,"
                "available_at,created_at,updated_at,deadline_at,"
                "deadline_policy_mode,deadline_policy_version) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    kind,
                    "queued",
                    idempotency_key,
                    logical_request_hash,
                    lane,
                    priority,
                    max_attempts,
                    now,
                    now,
                    now,
                    deadline_at,
                    deadline_policy_mode,
                    deadline_policy_version,
                ),
            )
            db.execute(
                "INSERT INTO job_inputs("
                "job_id,payload_json,payload_hash,identity_version,created_at) "
                "VALUES(?,?,?,?,?)",
                (job_id, payload_json, payload_hash, identity_version, now),
            )
            row = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._deserialize_job(db, row), True

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            return self._deserialize_job(db, row)

    def list_attempts(self, job_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            if not db.execute(
                "SELECT 1 FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone():
                raise JobNotFound("任务不存在")
            return [
                {
                    "attempt_number": int(row["attempt_number"]),
                    "manual_retry_number": int(row["manual_retry_number"]),
                    "lane": row["lane"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "status": row["status"],
                    "error_code": row["error_code"],
                    "correlation_id": row["error_correlation_id"],
                }
                for row in db.execute(
                    "SELECT * FROM job_attempts WHERE job_id=? "
                    "ORDER BY attempt_number", (job_id,)
                ).fetchall()
            ]

    def list_jobs(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
        active: bool = False,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("limit 必须在 1 到 200 之间")
        where: list[str] = []
        params: list[Any] = []
        if status:
            if status not in {
                "queued",
                "checkpointed",
                "running",
                "cancel_requested",
                "published_pending_receipt",
                "cancelled",
                "succeeded",
                "failed",
            }:
                raise ValueError("任务状态无效")
            where.append("status=?")
            params.append(status)
        if kind:
            if kind not in SUPPORTED_JOB_KINDS:
                raise UnknownJobKind(f"不支持的任务类型: {kind}")
            where.append("kind=?")
            params.append(kind)
        if active:
            # Apply the collection's active-state filter before LIMIT.  The
            # route uses this for polling; filtering after a limited query can
            # hide real active jobs behind recent terminal history.
            placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
            where.append(f"status IN ({placeholders})")
            params.extend(sorted(ACTIVE_STATUSES))
        sql = "SELECT * FROM background_jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC,id DESC LIMIT ?"
        params.append(limit)
        with self._connection() as db:
            rows = db.execute(sql, params).fetchall()
            exam_ids_by_job: dict[str, list[int]] = {row["id"]: [] for row in rows}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                for item in db.execute(
                    f"SELECT job_id,exam_id FROM job_exam_results WHERE job_id IN ({placeholders}) "
                    "ORDER BY job_id,position",
                    [row["id"] for row in rows],
                ).fetchall():
                    exam_ids_by_job[item["job_id"]].append(item["exam_id"])
            return [
                self._deserialize_job(db, row, exam_ids_by_job[row["id"]])
                for row in rows
            ]

    def get_input(self, job_id: str) -> dict[str, Any]:
        with self._connection() as db:
            row = db.execute(
                "SELECT i.payload_json,i.payload_hash,i.identity_version,"
                "j.kind,j.status "
                "FROM job_inputs i JOIN background_jobs j ON j.id=i.job_id "
                "WHERE i.job_id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            try:
                return verify_persisted_input(
                    row["payload_json"],
                    row["payload_hash"],
                    identity_version=row["identity_version"],
                    job_kind=row["kind"],
                    job_status=row["status"],
                )
            except ExamGenerationRequestIdentityError as exc:
                raise self._persisted_input_error(exc) from exc

    @staticmethod
    def _persisted_input_error(
        exc: ExamGenerationRequestIdentityError,
    ) -> PermanentJobError:
        if exc.reason == "legacy_multi_paper_checkpoint_unsupported":
            return PermanentJobError(
                "旧多卷未完成任务不能恢复，请重新提交单卷任务",
                code="legacy_multi_paper_checkpoint_unsupported",
                category="contract",
                details={
                    "reason": exc.reason,
                    "actions": ["resubmit_single_paper"],
                },
            )
        if exc.reason == "legacy_exam_checkpoint_migration_pending":
            return PermanentJobError(
                "旧组卷任务尚未进入白名单检查点迁移路径",
                code="legacy_exam_checkpoint_migration_pending",
                category="dependency",
                details={
                    "reason": exc.reason,
                    "actions": ["resubmit_after_upgrade"],
                },
            )
        return PermanentJobError(
            "任务冻结输入完整性校验失败",
            code="job_input_hash_mismatch",
            category="contract",
            details={"reason": exc.reason},
        )

    def get_input_identity(self, job_id: str) -> dict[str, str | None]:
        """Return verified internal identities without exposing input content."""

        with self._connection() as db:
            row = db.execute(
                "SELECT j.kind,j.status,j.input_hash,i.payload_json,"
                "i.payload_hash,i.identity_version "
                "FROM background_jobs j JOIN job_inputs i ON i.job_id=j.id "
                "WHERE j.id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            try:
                payload = verify_persisted_input(
                    row["payload_json"],
                    row["payload_hash"],
                    identity_version=row["identity_version"],
                    job_kind=row["kind"],
                    job_status=row["status"],
                    allow_legacy_exam_read=row["status"] == "succeeded",
                )
            except ExamGenerationRequestIdentityError as exc:
                raise self._persisted_input_error(exc) from exc
            generation_hash = (
                payload.get("generation_request_hash")
                if row["kind"] == "exam-generation"
                else None
            )
            return {
                "logical_request_hash": str(row["input_hash"]),
                "job_input_hash": str(row["payload_hash"]),
                "generation_request_hash": (
                    str(generation_hash) if generation_hash is not None else None
                ),
            }

    def get_idempotency_key(self, job_id: str) -> str | None:
        """Read a job's submission key for internal deduplication only."""

        with self._connection() as db:
            row = db.execute(
                "SELECT idempotency_key FROM background_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            return row["idempotency_key"]

    @staticmethod
    def _staged_values(payload: Mapping[str, Any]) -> list[str]:
        raw = payload.get(STAGED_INPUT_FIELD)
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            return raw
        return []

    def _cleanup_staged_payload(self, payload: Mapping[str, Any]) -> int:
        deleted = 0
        if not self.staged_input_roots:
            return deleted
        for raw in self._staged_values(payload):
            candidate = Path(raw)
            if not candidate.is_absolute():
                continue
            try:
                resolved = candidate.resolve(strict=False)
                allowed = any(
                    resolved != root and root in resolved.parents
                    for root in self.staged_input_roots
                )
                if not allowed or not (candidate.is_file() or candidate.is_symlink()):
                    continue
                candidate.unlink()
                deleted += 1
            except OSError:
                continue
        return deleted

    def cleanup_staged_input(self, job_id: str) -> int:
        """Delete only explicitly registered files under approved staging roots."""

        return self._cleanup_staged_payload(self.get_input(job_id))

    def cleanup_retention(self) -> dict[str, int]:
        """Apply OCR result and terminal metadata retention without recursive deletion."""

        now_value, now = self._now()
        result_cutoff = _timestamp(
            now_value - timedelta(days=OCR_RESULT_RETENTION_DAYS)
        )
        metadata_cutoff = _timestamp(
            now_value - timedelta(days=JOB_METADATA_RETENTION_DAYS)
        )
        staged_payloads: list[dict[str, Any]] = []
        with self._connection() as db:
            begin_immediate(db)
            for row in db.execute(
                "SELECT i.payload_json FROM background_jobs j "
                "JOIN job_inputs i ON i.job_id=j.id "
                "WHERE j.status='cancelled'"
            ).fetchall():
                try:
                    staged_payloads.append(json.loads(row["payload_json"]))
                except (TypeError, json.JSONDecodeError):
                    continue
            purged_results = db.execute(
                "UPDATE background_jobs SET result_json=NULL,updated_at=? "
                "WHERE kind='ocr' AND status='succeeded' AND result_json IS NOT NULL "
                "AND finished_at IS NOT NULL AND finished_at<=?",
                (now, result_cutoff),
            ).rowcount
            deleted_jobs = db.execute(
                "DELETE FROM background_jobs WHERE status IN ('cancelled','succeeded','failed') "
                "AND finished_at IS NOT NULL AND finished_at<=?",
                (metadata_cutoff,),
            ).rowcount
        deleted_staged_files = sum(
            self._cleanup_staged_payload(payload) for payload in staged_payloads
        )
        return {
            "purged_ocr_results": purged_results,
            "deleted_jobs": deleted_jobs,
            "deleted_staged_files": deleted_staged_files,
        }

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        _now_value, now = self._now()
        with self._connection() as db:
            begin_immediate(db)
            row = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            # Once the native publication transaction commits, cancellation
            # can no longer describe reality.  Leave both the lease and the
            # durable cancellation fields untouched; the receipt recovery
            # attempt is the only remaining work.
            if (
                row["status"] == "published_pending_receipt"
                or self._publication_is_committed(db, job_id)
            ):
                return self._deserialize_job(db, row)
            if row["status"] in {"queued", "checkpointed"}:
                db.execute(
                    "UPDATE background_jobs SET status='cancelled',cancel_requested=1,"
                    "error_code=NULL,error_message=NULL,error_category=NULL,"
                    "error_retryable=NULL,error_correlation_id=NULL,error_details_json=NULL,"
                    "updated_at=?,finished_at=? WHERE id=?",
                    (now, now, job_id),
                )
            elif row["status"] in {"running", "cancel_requested"}:
                db.execute(
                    "UPDATE background_jobs SET status='cancel_requested',"
                    "cancel_requested=1,updated_at=? WHERE id=?",
                    (now, job_id),
                )
            updated = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            job = self._deserialize_job(db, updated)
        if job["kind"] == "ocr" and job["status"] == "cancelled":
            self.cleanup_staged_input(job_id)
        return job

    def retry(self, job_id: str) -> dict[str, Any]:
        _now_value, now = self._now()
        with self._connection() as db:
            begin_immediate(db)
            row = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            if row["status"] in {
                "running", "cancel_requested", "checkpointed", "published_pending_receipt"
            }:
                raise InvalidJobState("运行中的任务不能重试")
            if self._publication_is_committed(db, job_id):
                raise InvalidJobState("发布已提交的任务不能重试")
            if row["status"] == "succeeded":
                raise InvalidJobState("已成功的任务不能重试")
            if row["kind"] == "ocr" and row["status"] == "cancelled":
                raise InvalidJobState("已取消的 OCR 输入已清理，请重新上传文件")
            input_row = db.execute(
                "SELECT payload_json,payload_hash,identity_version "
                "FROM job_inputs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            try:
                if input_row is None:
                    raise ExamGenerationRequestIdentityError("job_input_missing")
                verify_persisted_input(
                    input_row["payload_json"],
                    input_row["payload_hash"],
                    identity_version=input_row["identity_version"],
                    job_kind=row["kind"],
                    job_status=row["status"],
                )
            except ExamGenerationRequestIdentityError as exc:
                raise InvalidJobState(
                    "任务冻结输入未通过完整性或恢复资格校验"
                ) from exc
            if row["status"] == "queued":
                return self._deserialize_job(db, row)
            if row["status"] == "failed" and row["error_category"] in {
                "input",
                "contract",
            }:
                raise InvalidJobState(
                    "输入或冻结合同错误不能重试，请修正后重新提交任务"
                )
            db.execute("DELETE FROM job_exam_results WHERE job_id=?", (job_id,))
            db.execute(
                "UPDATE background_jobs SET status='queued',attempts_in_cycle=0,"
                "manual_retry_count=manual_retry_count+1,"
                "progress_current=0,progress_total=NULL,progress_message='',"
                "result_json=NULL,error_code=NULL,error_message=NULL,"
                "error_category=NULL,error_retryable=NULL,error_correlation_id=NULL,"
                "error_details_json=NULL,"
                "cancel_requested=0,lease_owner=NULL,lease_expires_at=NULL,"
                "heartbeat_at=NULL,available_at=?,updated_at=?,"
                "started_at=CASE WHEN kind='exam-generation' THEN started_at ELSE NULL END,"
                "finished_at=NULL WHERE id=?",
                (now, now, job_id),
            )
            updated = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return self._deserialize_job(db, updated)

    def recover_stale_jobs(self) -> int:
        _now_value, now = self._now()
        recovered = 0
        deadline_terminal_ids: list[str] = []
        with self._connection() as db:
            begin_immediate(db)
            rows = db.execute(
                "SELECT * "
                "FROM background_jobs WHERE status IN "
                "('running','cancel_requested','published_pending_receipt') "
                "AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
                (now,),
            ).fetchall()
            for row in rows:
                if (
                    row["status"] == "published_pending_receipt"
                    or self._publication_is_committed(db, str(row["id"]))
                ):
                    self._keep_publication_pending(db, row, now=now)
                    recovered += 1
                    continue
                deadline_expired = (
                    row["kind"] == "exam-generation"
                    and row["deadline_policy_mode"] == "enforce"
                    and row["deadline_at"] is not None
                    and str(row["deadline_at"]) <= now
                    and not row["cancel_requested"]
                    and row["status"] != "cancel_requested"
                )
                if deadline_expired:
                    accumulated, receipt = self._final_exam_timing(
                        db, row, finished_at=now, attempt_timing=None
                    )
                    details_json = json.dumps(
                        {
                            "details": {
                                "failed_stage": "recovery",
                                "deadline_at": row["deadline_at"],
                                "policy_version": row["deadline_policy_version"],
                                "timing": receipt,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    timing_json = json.dumps(
                        accumulated,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    db.execute(
                        "UPDATE background_jobs SET status='failed',"
                        "error_code='exam_deadline_exceeded',"
                        "error_message='组卷任务在执行进程恢复时已达到正式截止时间',"
                        "error_category='business',error_retryable=0,"
                        "error_correlation_id=NULL,error_details_json=?,"
                        "exam_timing_json=?,lease_owner=NULL,lease_expires_at=NULL,"
                        "heartbeat_at=NULL,available_at=?,updated_at=?,finished_at=? "
                        "WHERE id=?",
                        (details_json, timing_json, now, now, now, row["id"]),
                    )
                    db.execute(
                        "UPDATE job_attempts SET status='interrupted',finished_at=?,"
                        "error_code='exam_deadline_exceeded' "
                        "WHERE job_id=? AND status='running'",
                        (now, row["id"]),
                    )
                    deadline_terminal_ids.append(str(row["id"]))
                    recovered += 1
                    continue
                if row["cancel_requested"] or row["status"] == "cancel_requested":
                    status, code, message, finished = (
                        "cancelled",
                        None,
                        None,
                        now,
                    )
                elif row["attempts_in_cycle"] >= row["max_attempts"]:
                    status, code, message, finished = (
                        "failed",
                        "lease_expired",
                        "任务执行进程中断且已达到最大尝试次数",
                        now,
                    )
                else:
                    status = (
                        "checkpointed"
                        if row["kind"] == "exam-generation"
                        and self._has_generation_checkpoint(db, str(row["id"]))
                        else "queued"
                    )
                    code, message, finished = None, None, None
                details_json = None
                timing_json = None
                if (
                    status == "failed"
                    and code == "lease_expired"
                    and row["kind"] == "exam-generation"
                ):
                    accumulated, receipt = self._final_exam_timing(
                        db, row, finished_at=now, attempt_timing=None
                    )
                    details_json = json.dumps(
                        {
                            "details": {
                                "failed_stage": "recovery",
                                "timing": receipt,
                            }
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    timing_json = json.dumps(
                        accumulated,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                db.execute(
                    "UPDATE background_jobs SET status=?,error_code=?,error_message=?,"
                    "error_category=?,error_retryable=?,error_correlation_id=NULL,"
                    "error_details_json=?,exam_timing_json=COALESCE(?,exam_timing_json),"
                    "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                    "available_at=?,updated_at=?,finished_at=? WHERE id=?",
                    (
                        status,
                        code,
                        message,
                        "internal" if code else None,
                        0 if status == "failed" else (
                            1 if status in {"queued", "checkpointed"} else None
                        ),
                        details_json,
                        timing_json,
                        now,
                        now,
                        finished,
                        row["id"],
                    ),
                )
                db.execute(
                    "UPDATE job_attempts SET status=?,finished_at=?,error_code=? "
                    "WHERE job_id=? AND status='running'",
                    (
                        "cancelled" if status == "cancelled" else "interrupted",
                        now,
                        code or (
                            "lease_expired"
                            if status in {"queued", "checkpointed"}
                            else None
                        ),
                        row["id"],
                    ),
                )
                recovered += 1
        for job_id in deadline_terminal_ids:
            self._notify_terminal(self.get_job(job_id))
        return recovered

    def _expire_queued_exam_deadlines(self) -> int:
        """Fail queued enforce-mode exams before any handler or provider call."""

        _now_value, now = self._now()
        expired_ids: list[str] = []
        with self._connection() as db:
            begin_immediate(db)
            rows = db.execute(
                "SELECT * FROM background_jobs WHERE kind='exam-generation' "
                "AND status='queued' AND deadline_policy_mode='enforce' "
                "AND deadline_at IS NOT NULL AND deadline_at<=? ORDER BY created_at,id",
                (now,),
            ).fetchall()
            for row in rows:
                accumulated, receipt = self._final_exam_timing(
                    db, row, finished_at=now, attempt_timing=None
                )
                details_json = json.dumps(
                    {
                        "details": {
                            "failed_stage": "queue",
                            "deadline_at": row["deadline_at"],
                            "policy_version": row["deadline_policy_version"],
                            "timing": receipt,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                timing_json = json.dumps(
                    accumulated,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                updated = db.execute(
                    "UPDATE background_jobs SET status='failed',"
                    "error_code='exam_deadline_exceeded',"
                    "error_message='组卷任务在队列中达到正式截止时间',"
                    "error_category='business',error_retryable=0,"
                    "error_correlation_id=NULL,error_details_json=?,"
                    "exam_timing_json=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND status='queued'",
                    (details_json, timing_json, now, now, row["id"]),
                )
                if updated.rowcount == 1:
                    expired_ids.append(str(row["id"]))
        for job_id in expired_ids:
            self._notify_terminal(self.get_job(job_id))
        return len(expired_ids)

    def claim_next(self) -> dict[str, Any] | None:
        self.recover_stale_jobs()
        self._expire_queued_exam_deadlines()
        now_value, now = self._now()
        lease_expires = _timestamp(now_value + timedelta(seconds=self.lease_seconds))
        with self._connection() as db:
            begin_immediate(db)
            sql = (
                "SELECT * FROM background_jobs WHERE "
                "(status='published_pending_receipt' OR "
                "(status IN ('queued','checkpointed') AND cancel_requested=0 "
                "AND attempts_in_cycle<max_attempts)) AND available_at<=? "
            )
            params: list[Any] = [now]
            if self.lane is not None:
                sql += "AND lane=? "
                params.append(self.lane)
            sql += (
                "ORDER BY CASE WHEN status='published_pending_receipt' THEN 0 ELSE 1 END,"
                "(priority + CAST(MAX(0,(julianday(?) - "
                "julianday(created_at))*86400)/? AS INTEGER)) DESC,"
                "available_at,created_at,id LIMIT 1"
            )
            params.extend((now, self.aging_seconds))
            row = db.execute(sql, params).fetchone()
            if not row:
                return None
            pending_receipt = row["status"] == "published_pending_receipt"
            updated = db.execute(
                "UPDATE background_jobs SET status=?,attempts=attempts+1,"
                "attempts_in_cycle=attempts_in_cycle+?,cancel_requested=0,"
                "lease_owner=?,lease_expires_at=?,heartbeat_at=?,updated_at=?,"
                "started_at=COALESCE(started_at,?),error_code=NULL,error_message=NULL,"
                "error_category=NULL,error_retryable=NULL,error_correlation_id=NULL,"
                "error_details_json=NULL "
                "WHERE id=? AND status=?",
                (
                    "published_pending_receipt" if pending_receipt else "running",
                    0 if pending_receipt else 1,
                    self.worker_id,
                    lease_expires,
                    now,
                    now,
                    now,
                    row["id"],
                    row["status"],
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = db.execute(
                "SELECT * FROM background_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            db.execute(
                "INSERT INTO job_attempts(job_id,attempt_number,manual_retry_number,"
                "worker_id,lane,started_at,status) VALUES(?,?,?,?,?,?,'running')",
                (
                    row["id"],
                    int(claimed["attempts"]),
                    int(claimed["manual_retry_count"]),
                    self.worker_id,
                    claimed["lane"],
                    now,
                ),
            )
            return self._deserialize_job(db, claimed)

    @staticmethod
    def _lease_matches(row, worker_id: str, lease_attempt: int | None) -> bool:
        return (
            row["lease_owner"] == worker_id
            and row["status"] in {
                "running", "checkpointed", "cancel_requested", "published_pending_receipt"
            }
            and (
                lease_attempt is None
                or int(row["attempts"]) == int(lease_attempt)
            )
        )

    def assert_lease(self, job_id: str, *, lease_attempt: int | None = None) -> None:
        with self._connection() as db:
            row = db.execute(
                "SELECT status,lease_owner,attempts FROM background_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            if not self._lease_matches(row, self.worker_id, lease_attempt):
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")

    def heartbeat(
        self, job_id: str, *, lease_attempt: int | None = None
    ) -> bool:
        now_value, now = self._now()
        lease_expires = _timestamp(now_value + timedelta(seconds=self.lease_seconds))
        with self._connection() as db:
            row = db.execute(
                "SELECT status,cancel_requested,lease_owner,attempts "
                "FROM background_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            if not self._lease_matches(row, self.worker_id, lease_attempt):
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            parameters: list[Any] = [now, lease_expires, now, job_id, self.worker_id]
            attempt_sql = ""
            if lease_attempt is not None:
                attempt_sql = " AND attempts=?"
                parameters.append(int(lease_attempt))
            updated = db.execute(
                "UPDATE background_jobs SET heartbeat_at=?,lease_expires_at=?,"
                "updated_at=? WHERE id=? AND lease_owner=? "
                "AND status IN ('running','checkpointed','cancel_requested',"
                "'published_pending_receipt')" + attempt_sql,
                parameters,
            )
            if updated.rowcount != 1:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            return bool(row["cancel_requested"])

    def update_progress(
        self,
        job_id: str,
        *,
        current: int,
        total: int | None = None,
        message: str = "",
        lease_attempt: int | None = None,
    ) -> bool:
        if not isinstance(current, int) or current < 0:
            raise ValueError("current 必须是非负整数")
        if total is not None and (
            not isinstance(total, int) or total < 0 or current > total
        ):
            raise ValueError("total 必须是不小于 current 的非负整数")
        if len(message) > 500:
            raise ValueError("进度消息不能超过 500 个字符")
        now_value, now = self._now()
        lease_expires = _timestamp(now_value + timedelta(seconds=self.lease_seconds))
        with self._connection() as db:
            row = db.execute(
                "SELECT status,cancel_requested,lease_owner,attempts "
                "FROM background_jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            if not self._lease_matches(row, self.worker_id, lease_attempt):
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            parameters: list[Any] = [
                current, total, message, now, lease_expires, now, job_id,
                self.worker_id,
            ]
            attempt_sql = ""
            if lease_attempt is not None:
                attempt_sql = " AND attempts=?"
                parameters.append(int(lease_attempt))
            updated = db.execute(
                "UPDATE background_jobs SET progress_current=?,progress_total=?,"
                "progress_message=?,heartbeat_at=?,lease_expires_at=?,updated_at=? "
                "WHERE id=? AND lease_owner=? "
                "AND status IN ('running','checkpointed','cancel_requested',"
                "'published_pending_receipt')" + attempt_sql,
                parameters,
            )
            if updated.rowcount != 1:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            return bool(row["cancel_requested"])

    def is_cancel_requested(
        self, job_id: str, *, lease_attempt: int | None = None
    ) -> bool:
        with self._connection() as db:
            row = db.execute(
                "SELECT status,cancel_requested,lease_owner,attempts "
                "FROM background_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFound("任务不存在")
            if lease_attempt is not None and not self._lease_matches(
                row, self.worker_id, lease_attempt
            ):
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            return bool(row["cancel_requested"])

    def _finish_cancelled(
        self,
        job_id: str,
        *,
        lease_attempt: int | None = None,
        timing: Mapping[str, Any] | None = None,
    ) -> None:
        _now_value, now = self._now()
        with self._connection() as db:
            where = "WHERE id=? AND lease_owner=?"
            lookup_params: list[Any] = [job_id, self.worker_id]
            if lease_attempt is not None:
                where += " AND attempts=?"
                lookup_params.append(int(lease_attempt))
            row = db.execute(
                "SELECT * FROM background_jobs " + where, lookup_params
            ).fetchone()
            if not row:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            accumulated, _receipt = self._final_exam_timing(
                db, row, finished_at=now, attempt_timing=timing
            )
            timing_json = json.dumps(
                accumulated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if (
                row["status"] == "published_pending_receipt"
                or self._publication_is_committed(db, job_id)
            ):
                self._keep_publication_pending(
                    db, row, now=now, timing_json=timing_json
                )
                return
            parameters: list[Any] = [timing_json, now, now, job_id, self.worker_id]
            attempt_sql = ""
            if lease_attempt is not None:
                attempt_sql = " AND attempts=?"
                parameters.append(int(lease_attempt))
            updated = db.execute(
                "UPDATE background_jobs SET status='cancelled',cancel_requested=1,"
                    "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                    "error_code=NULL,error_message=NULL,error_category=NULL,"
                    "error_retryable=NULL,error_correlation_id=NULL,error_details_json=NULL,"
                    "exam_timing_json=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND lease_owner=?" + attempt_sql,
                parameters,
            )
            if updated.rowcount != 1:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            attempt_parameters: list[Any] = [now, job_id, self.worker_id]
            attempt_filter = ""
            if lease_attempt is not None:
                attempt_filter = " AND attempt_number=?"
                attempt_parameters.append(int(lease_attempt))
            db.execute(
                "UPDATE job_attempts SET status='cancelled',finished_at=? "
                "WHERE job_id=? AND worker_id=? AND status='running'" + attempt_filter,
                attempt_parameters,
            )

    def _finish_deferred(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        delay_seconds: float = 1.0,
        details: Mapping[str, Any] | None = None,
        timing: Mapping[str, Any] | None = None,
        lease_attempt: int | None = None,
    ) -> None:
        """Requeue a dependency wait without consuming the retry cycle."""

        normalized_details = _normalize_error_details(details)
        details_json = (
            json.dumps(normalized_details, ensure_ascii=False, separators=(",", ":"))
            if normalized_details
            else None
        )
        now_value, now = self._now()
        available_at = _timestamp(
            now_value + timedelta(seconds=max(0.1, float(delay_seconds)))
        )
        with self._connection() as db:
            begin_immediate(db)
            where = "WHERE id=? AND lease_owner=?"
            parameters: list[Any] = [job_id, self.worker_id]
            if lease_attempt is not None:
                where += " AND attempts=?"
                parameters.append(int(lease_attempt))
            row = db.execute(
                "SELECT * FROM background_jobs " + where,
                parameters,
            ).fetchone()
            if not row:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            accumulated, _receipt = self._final_exam_timing(
                db, row, finished_at=now, attempt_timing=timing
            )
            timing_json = json.dumps(
                accumulated, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if (
                row["status"] == "published_pending_receipt"
                or self._publication_is_committed(db, job_id)
            ):
                self._keep_publication_pending(
                    db,
                    row,
                    now=now,
                    available_at=available_at,
                    timing_json=timing_json,
                )
                return
            if row["cancel_requested"]:
                cancel_params: list[Any] = [
                    timing_json, now, now, job_id, self.worker_id
                ]
                cancel_filter = ""
                if lease_attempt is not None:
                    cancel_filter = " AND attempts=?"
                    cancel_params.append(int(lease_attempt))
                db.execute(
                    "UPDATE background_jobs SET status='cancelled',"
                    "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                    "error_code=NULL,error_message=NULL,error_category=NULL,"
                    "error_retryable=NULL,error_correlation_id=NULL,error_details_json=NULL,"
                    "exam_timing_json=?,updated_at=?,finished_at=? "
                    "WHERE id=? AND lease_owner=?"
                    + cancel_filter,
                    cancel_params,
                )
                attempt_params: list[Any] = [now, job_id, self.worker_id]
                attempt_filter = ""
                if lease_attempt is not None:
                    attempt_filter = " AND attempt_number=?"
                    attempt_params.append(int(lease_attempt))
                db.execute(
                    "UPDATE job_attempts SET status='cancelled',finished_at=? "
                    "WHERE job_id=? AND worker_id=? AND status='running'"
                    + attempt_filter,
                    attempt_params,
                )
                return
            # claim_next increments attempts_in_cycle before invoking the
            # handler; put that counter back so dependency waits do not spend
            # the normal AI retry budget.
            cycle_attempts = max(0, int(row["attempts_in_cycle"]) - 1)
            deferred_status = (
                "checkpointed"
                if row["kind"] == "exam-generation"
                and self._has_generation_checkpoint(db, job_id)
                else "queued"
            )
            update_params: list[Any] = [
                deferred_status,
                code,
                message,
                "dependency",
                1,
                details_json,
                cycle_attempts,
                timing_json,
                available_at,
                now,
                job_id,
                self.worker_id,
            ]
            attempt_filter = ""
            if lease_attempt is not None:
                attempt_filter = " AND attempts=?"
                update_params.append(int(lease_attempt))
            updated = db.execute(
                "UPDATE background_jobs SET status=?,error_code=?,"
                "error_message=?,error_category=?,error_retryable=?,"
                "error_correlation_id=NULL,error_details_json=?,"
                "attempts_in_cycle=?,lease_owner=NULL,lease_expires_at=NULL,"
                "heartbeat_at=NULL,exam_timing_json=?,available_at=?,updated_at=?,"
                "finished_at=NULL "
                "WHERE id=? AND lease_owner=?" + attempt_filter,
                update_params,
            )
            if updated.rowcount != 1:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            attempt_params: list[Any] = [now, code, job_id, self.worker_id]
            attempt_filter = ""
            if lease_attempt is not None:
                attempt_filter = " AND attempt_number=?"
                attempt_params.append(int(lease_attempt))
            db.execute(
                "UPDATE job_attempts SET status='retrying',finished_at=?,"
                "error_code=? WHERE job_id=? AND worker_id=? AND status='running'"
                + attempt_filter,
                attempt_params,
            )

    def _finish_failure(
        self,
        job_id: str,
        *,
        code: str,
        message: str,
        retry_delay: float | None = None,
        category: str = "business",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
        timing: Mapping[str, Any] | None = None,
        lease_attempt: int | None = None,
    ) -> None:
        normalized_details = _normalize_error_details(details)
        now_value, now = self._now()
        with self._connection() as db:
            begin_immediate(db)
            row = db.execute(
                "SELECT * FROM background_jobs "
                "WHERE id=? AND lease_owner=?"
                + (" AND attempts=?" if lease_attempt is not None else ""),
                (
                    (job_id, self.worker_id, int(lease_attempt))
                    if lease_attempt is not None
                    else (job_id, self.worker_id)
                ),
            ).fetchone()
            if not row:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            accumulated_timing, timing_receipt = self._final_exam_timing(
                db, row, finished_at=now, attempt_timing=timing
            )
            timing_json = json.dumps(
                accumulated_timing,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if (
                row["status"] == "published_pending_receipt"
                or self._publication_is_committed(db, job_id)
            ):
                pending_available_at = now
                if retry_delay is not None:
                    pending_available_at = _timestamp(
                        now_value
                        + timedelta(seconds=max(0.0, float(retry_delay)))
                    )
                self._keep_publication_pending(
                    db,
                    row,
                    now=now,
                    available_at=pending_available_at,
                    timing_json=timing_json,
                )
                return
            if row["cancel_requested"]:
                status = "cancelled"
                available_at = now
                finished_at = now
                code = None
                message = None
            elif retry_delay is not None and row["attempts_in_cycle"] < row["max_attempts"]:
                status = "queued"
                available_at = _timestamp(
                    now_value + timedelta(seconds=max(0.0, retry_delay))
                )
                finished_at = None
            else:
                status = "failed"
                available_at = now
                finished_at = now
            if code and timing_receipt is not None and status == "failed":
                public_details = normalized_details.get("details")
                public_details = (
                    dict(public_details) if isinstance(public_details, Mapping) else {}
                )
                public_details.setdefault(
                    "failed_stage",
                    str(
                        timing.get("failed_stage") or "execution"
                        if isinstance(timing, Mapping)
                        else "execution"
                    ),
                )
                public_details["timing"] = timing_receipt
                normalized_details["details"] = public_details
            error_details_json = (
                json.dumps(
                    normalized_details,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                if normalized_details and code
                else None
            )
            db.execute(
                "UPDATE background_jobs SET status=?,error_code=?,error_message=?,"
                "error_category=?,error_retryable=?,error_correlation_id=?,"
                "error_details_json=?,exam_timing_json=?,"
                "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                "available_at=?,updated_at=?,finished_at=? WHERE id=?",
                (
                    status,
                    code,
                    message,
                    category if code else None,
                    int(retryable) if code else None,
                    correlation_id if code else None,
                    error_details_json if code else None,
                    timing_json,
                    available_at,
                    now,
                    finished_at,
                    job_id,
                ),
            )
            db.execute(
                "UPDATE job_attempts SET status=?,finished_at=?,error_code=?,"
                "error_correlation_id=? WHERE job_id=? AND worker_id=? "
                "AND status='running'"
                + (" AND attempt_number=?" if lease_attempt is not None else ""),
                (
                    "cancelled" if status == "cancelled" else (
                        "retrying" if status == "queued" else "failed"
                    ),
                    now,
                    code,
                    correlation_id,
                    job_id,
                    self.worker_id,
                    *((int(lease_attempt),) if lease_attempt is not None else ()),
                ),
            )

    def _finish_success(
        self,
        job_id: str,
        outcome: JobOutcome,
        *,
        lease_attempt: int | None = None,
    ) -> None:
        _now_value, now = self._now()
        with self._connection() as db:
            begin_immediate(db)
            row = db.execute(
                "SELECT * FROM background_jobs "
                "WHERE id=? AND lease_owner=?"
                + (" AND attempts=?" if lease_attempt is not None else ""),
                (
                    (job_id, self.worker_id, int(lease_attempt))
                    if lease_attempt is not None
                    else (job_id, self.worker_id)
                ),
            ).fetchone()
            if not row:
                raise JobLeaseLost("当前 worker attempt 不持有该任务租约")
            publication_state = self._publication_head_state(db, job_id)
            publication_committed = publication_state in {
                "published_pending_receipt", "completed"
            }
            if publication_state == "published_pending_receipt" or (
                row["status"] == "published_pending_receipt"
                and publication_state != "completed"
            ):
                self._keep_publication_pending(db, row, now=now)
                return
            if publication_state == "request_frozen":
                db.execute(
                    "UPDATE background_jobs SET status='checkpointed',"
                    "attempts_in_cycle=MAX(0,attempts_in_cycle-1),"
                    "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                    "error_code='publication_not_committed',"
                    "error_message='发布请求已冻结但尚无已提交发布事实',"
                    "error_category='dependency',error_retryable=1,"
                    "error_correlation_id=NULL,error_details_json=NULL,"
                    "available_at=?,updated_at=?,finished_at=NULL "
                    "WHERE id=? AND attempts=?",
                    (now, now, job_id, int(row["attempts"])),
                )
                db.execute(
                    "UPDATE job_attempts SET status='interrupted',finished_at=?,"
                    "error_code='publication_not_committed' WHERE job_id=? "
                    "AND attempt_number=? AND status='running'",
                    (now, job_id, int(row["attempts"])),
                )
                return
            if (
                row["kind"] == "exam-generation"
                and row["deadline_policy_mode"] == "enforce"
                and row["deadline_at"] is not None
                and str(row["deadline_at"]) <= now
                and not publication_committed
            ):
                raise ExamDeadlineExceeded(
                    stage="receipt_and_finalization",
                    deadline_at=str(row["deadline_at"]),
                    policy_version=str(row["deadline_policy_version"] or ""),
                    now=_now_value,
                )
            accumulated_timing, timing_receipt = self._final_exam_timing(
                db,
                row,
                finished_at=now,
                attempt_timing=outcome.timing,
            )
            timing_json = json.dumps(
                accumulated_timing,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if (
                row["cancel_requested"] or row["status"] == "cancel_requested"
            ) and not outcome.side_effect_committed and not publication_committed:
                db.execute(
                    "UPDATE background_jobs SET status='cancelled',"
                    "error_code=NULL,error_message=NULL,error_category=NULL,"
                    "error_retryable=NULL,error_correlation_id=NULL,error_details_json=NULL,"
                    "lease_owner=NULL,lease_expires_at=NULL,heartbeat_at=NULL,"
                    "exam_timing_json=?,updated_at=?,finished_at=? WHERE id=?",
                    (timing_json, now, now, job_id),
                )
                db.execute(
                    "UPDATE job_attempts SET status='cancelled',finished_at=? "
                    "WHERE job_id=? AND worker_id=? AND status='running'"
                    + (" AND attempt_number=?" if lease_attempt is not None else ""),
                    (
                        (now, job_id, self.worker_id, int(lease_attempt))
                        if lease_attempt is not None
                        else (now, job_id, self.worker_id)
                    ),
                )
                return
            result_value = outcome.result
            if timing_receipt is not None:
                if not isinstance(result_value, Mapping):
                    raise ValueError("组卷任务结果必须是 JSON 对象")
                result_value = {**dict(result_value), "timing": timing_receipt}
            try:
                result_json = json.dumps(result_value, ensure_ascii=False)
            except (TypeError, ValueError) as exc:
                raise ValueError("任务结果必须可序列化为 JSON") from exc
            db.execute("DELETE FROM job_exam_results WHERE job_id=?", (job_id,))
            for position, exam_id in enumerate(outcome.exam_ids, start=1):
                db.execute(
                    "INSERT INTO job_exam_results(job_id,exam_id,position) VALUES(?,?,?)",
                    (job_id, int(exam_id), position),
                )
            db.execute(
                "UPDATE background_jobs SET status='succeeded',result_json=?,"
                "error_code=NULL,error_message=NULL,error_category=NULL,"
                "error_retryable=NULL,error_correlation_id=NULL,error_details_json=NULL,"
                "lease_owner=NULL,exam_timing_json=?,"
                "lease_expires_at=NULL,heartbeat_at=NULL,updated_at=?,finished_at=? "
                "WHERE id=?",
                (result_json, timing_json, now, now, job_id),
            )
            if outcome.side_effect_committed:
                db.execute(
                    "INSERT INTO job_business_results(job_id,kind,result_json,committed_at) "
                    "SELECT id,kind,?,? FROM background_jobs WHERE id=? "
                    "ON CONFLICT(job_id) DO UPDATE SET result_json=excluded.result_json,"
                    "committed_at=excluded.committed_at",
                    (result_json, now, job_id),
                )
            db.execute(
                "UPDATE job_attempts SET status='succeeded',finished_at=? "
                "WHERE job_id=? AND worker_id=? AND status='running'"
                + (" AND attempt_number=?" if lease_attempt is not None else ""),
                (
                    (now, job_id, self.worker_id, int(lease_attempt))
                    if lease_attempt is not None
                    else (now, job_id, self.worker_id)
                ),
            )

    def run_once(self) -> dict[str, Any] | None:
        return execute_next(
            self,
            lambda coordinator, job_id, lease_attempt, receipt_only: JobContext(
                coordinator,
                job_id,
                lease_attempt,
                worker_stop_event=self._worker_stop_event,
                receipt_only=receipt_only,
            ),
        )

    def run_forever(
        self,
        stop_event: threading.Event,
        *,
        poll_interval: float = 0.25,
        retention_interval: float = 3600,
    ) -> None:
        previous_stop_event = self._worker_stop_event
        self._worker_stop_event = stop_event
        try:
            run_worker_loop(
                self,
                stop_event,
                poll_interval=poll_interval,
                retention_interval=retention_interval,
            )
        finally:
            self._worker_stop_event = previous_stop_event


def run_registered_worker(
    database: str | Path,
    stop_event: threading.Event,
    *,
    poll_interval: float = 0.25,
    lease_seconds: float = 30,
    retention_interval: float = 3600,
    staged_input_roots: tuple[str | Path, ...] = (),
    lane: str | None = None,
    aging_seconds: float = 300,
    outcome_observer: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    """Convenience entry point for the application's eventual worker thread."""

    coordinator = JobCoordinator(
        database,
        lease_seconds=lease_seconds,
        staged_input_roots=staged_input_roots,
        lane=lane,
        aging_seconds=aging_seconds,
        outcome_observer=outcome_observer,
    )
    coordinator.run_forever(
        stop_event,
        poll_interval=poll_interval,
        retention_interval=retention_interval,
    )
