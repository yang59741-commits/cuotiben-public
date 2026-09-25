"""Durable post-commit delivery of published exam memory events."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable

from ..migrations.v54_exam_generation_memory_delivery import (
    MEMORY_EVENT_DELIVERY_HEADS,
    MEMORY_EVENT_OUTBOX,
)


_ACTORS = {
    "free": "free_exam",
    "mistakes": "mistake_exam",
    "past": "past_exam",
}


class GenerationMemoryDeliveryError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class GenerationMemoryDelivery:
    operation_id: str
    exam_id: int
    mode: str
    actor: str
    event_day: str
    published_at: str
    events: tuple[dict[str, Any], ...]
    state: str
    revision: int
    delivered_at: str | None


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise GenerationMemoryDeliveryError("memory_delivery_events_invalid") from exc


def _text(value: Any, reason: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GenerationMemoryDeliveryError(reason)
    return value


def _day(value: Any) -> str:
    raw = _text(value, "memory_delivery_event_day_invalid")
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise GenerationMemoryDeliveryError("memory_delivery_event_day_invalid") from exc


def _timestamp(value: str | None = None) -> str:
    return _text(
        value or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "memory_delivery_timestamp_invalid",
    )


def _events(
    values: Sequence[Mapping[str, Any]],
    *,
    operation_id: str,
    exam_id: int,
    published_at: str,
) -> tuple[dict[str, Any], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise GenerationMemoryDeliveryError("memory_delivery_events_invalid")
    events = tuple(dict(value) for value in values if isinstance(value, Mapping))
    if len(events) != len(values) or len(events) != 2:
        raise GenerationMemoryDeliveryError("memory_delivery_events_invalid")
    kinds = [event.get("event_type") for event in events]
    if kinds.count("request") != 1 or kinds.count("result") != 1:
        raise GenerationMemoryDeliveryError("memory_delivery_event_types_invalid")
    event_ids = [str(event.get("event_id") or "") for event in events]
    if any(not value for value in event_ids) or len(set(event_ids)) != len(event_ids):
        raise GenerationMemoryDeliveryError("memory_delivery_event_identity_invalid")
    for event in events:
        if event.get("operation_id") != operation_id:
            raise GenerationMemoryDeliveryError("memory_delivery_operation_mismatch")
        event["occurred_at"] = published_at
    result = next(event for event in events if event["event_type"] == "result")
    result_body = result.get("result")
    if not isinstance(result_body, Mapping) or result_body.get("exam_id") != exam_id:
        raise GenerationMemoryDeliveryError("memory_delivery_exam_binding_invalid")
    if result_body.get("source") not in (None, "") and result_body.get("source") not in _ACTORS:
        raise GenerationMemoryDeliveryError("memory_delivery_result_mode_invalid")
    _canonical(events)
    return events


def _load_events(raw: Any) -> tuple[dict[str, Any], ...]:
    try:
        values = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError) as exc:
        raise GenerationMemoryDeliveryError("memory_delivery_payload_invalid") from exc
    if not isinstance(values, list) or not all(
        isinstance(value, Mapping) for value in values
    ):
        raise GenerationMemoryDeliveryError("memory_delivery_payload_invalid")
    return tuple(dict(value) for value in values)


def _published_exam(db: Any, exam_id: int, operation_id: str) -> None:
    row = db.execute(
        "SELECT artifact_status,generation_job_id FROM exams WHERE id=?", (exam_id,)
    ).fetchone()
    if row is None or str(row[0] or "") != "published":
        raise GenerationMemoryDeliveryError("memory_delivery_exam_not_published")
    generation_job_id = str(row[1] or "").strip()
    if generation_job_id and generation_job_id != operation_id:
        raise GenerationMemoryDeliveryError("memory_delivery_exam_operation_mismatch")


def stage_generation_memory_delivery(
    db: Any,
    *,
    operation_id: str,
    exam_id: int,
    mode: str,
    actor: str,
    event_day: str,
    published_at: str,
    events: Sequence[Mapping[str, Any]],
) -> str:
    """Stage an immutable envelope in the caller-owned publication transaction."""

    if not getattr(db, "in_transaction", False):
        raise GenerationMemoryDeliveryError("memory_delivery_transaction_required")
    operation = _text(operation_id, "memory_delivery_operation_invalid")
    if type(exam_id) is not int or exam_id <= 0:
        raise GenerationMemoryDeliveryError("memory_delivery_exam_id_invalid")
    if mode not in _ACTORS or actor != _ACTORS[mode]:
        raise GenerationMemoryDeliveryError("memory_delivery_route_invalid")
    fixed_day = _day(event_day)
    committed_at = _timestamp(published_at)
    if committed_at[:10] != fixed_day:
        raise GenerationMemoryDeliveryError("memory_delivery_publication_day_mismatch")
    frozen_events = _events(
        events,
        operation_id=operation,
        exam_id=exam_id,
        published_at=committed_at,
    )
    frozen_result = next(
        event for event in frozen_events if event["event_type"] == "result"
    )
    if frozen_result.get("result", {}).get("source") not in (None, "", mode):
        raise GenerationMemoryDeliveryError("memory_delivery_result_mode_invalid")
    encoded = _canonical(frozen_events)
    _published_exam(db, exam_id, operation)
    existing = db.execute(
        f"SELECT exam_id,mode,actor,event_day,published_at,events_json "
        f"FROM {MEMORY_EVENT_OUTBOX} WHERE operation_id=?",
        (operation,),
    ).fetchone()
    if existing is not None:
        existing_events = _load_events(existing[5])
        actual = tuple(existing[:5])
        expected = (exam_id, mode, actor, fixed_day, committed_at)
        if actual != expected:
            raise GenerationMemoryDeliveryError("memory_delivery_stage_conflict")
        checked_existing = _events(
            existing_events,
            operation_id=operation,
            exam_id=exam_id,
            published_at=committed_at,
        )
        if checked_existing != frozen_events:
            raise GenerationMemoryDeliveryError("memory_delivery_stage_conflict")
        head = db.execute(
            f"SELECT state,revision,delivered_at FROM {MEMORY_EVENT_DELIVERY_HEADS} "
            "WHERE operation_id=?",
            (operation,),
        ).fetchone()
        valid_head = head is not None and (
            tuple(head) == ("pending", 1, None)
            or (head[0] == "delivered" and head[1] == 2 and head[2] is not None)
        )
        if not valid_head:
            raise GenerationMemoryDeliveryError("memory_delivery_head_drift")
        return operation
    timestamp = committed_at
    db.execute(
        f"INSERT INTO {MEMORY_EVENT_OUTBOX}("
        "operation_id,exam_id,mode,actor,event_day,published_at,events_json,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (operation, exam_id, mode, actor, fixed_day, committed_at, encoded, timestamp),
    )
    db.execute(
        f"INSERT INTO {MEMORY_EVENT_DELIVERY_HEADS}("
        "operation_id,state,revision,delivered_at,updated_at"
        ") VALUES(?,'pending',1,NULL,?)",
        (operation, timestamp),
    )
    return operation


def recover_generation_memory_delivery(
    db: Any, *, operation_id: str
) -> GenerationMemoryDelivery:
    operation = _text(operation_id, "memory_delivery_operation_invalid")
    row = db.execute(
        f"SELECT o.operation_id,o.exam_id,o.mode,o.actor,o.event_day,o.published_at,"
        "o.events_json,h.state,h.revision,h.delivered_at "
        f"FROM {MEMORY_EVENT_OUTBOX} o JOIN {MEMORY_EVENT_DELIVERY_HEADS} h "
        "ON h.operation_id=o.operation_id WHERE o.operation_id=?",
        (operation,),
    ).fetchone()
    if row is None:
        raise GenerationMemoryDeliveryError("memory_delivery_missing")
    events = _load_events(row[6])
    checked = _events(
        events,
        operation_id=str(row[0]),
        exam_id=int(row[1]),
        published_at=str(row[5]),
    )
    if (
        checked != events
        or _ACTORS.get(str(row[2])) != row[3]
        or str(row[5])[:10] != _day(row[4])
        or next(event for event in checked if event["event_type"] == "result")
        .get("result", {})
        .get("source")
        not in (None, "", str(row[2]))
    ):
        raise GenerationMemoryDeliveryError("memory_delivery_row_drift")
    state, revision, delivered_at = str(row[7]), int(row[8]), row[9]
    if (state, revision, delivered_at is None) not in {
        ("pending", 1, True),
        ("delivered", 2, False),
    }:
        raise GenerationMemoryDeliveryError("memory_delivery_head_drift")
    return GenerationMemoryDelivery(
        operation_id=str(row[0]),
        exam_id=int(row[1]),
        mode=str(row[2]),
        actor=str(row[3]),
        event_day=_day(row[4]),
        published_at=str(row[5]),
        events=checked,
        state=state,
        revision=revision,
        delivered_at=None if delivered_at is None else str(delivered_at),
    )


def discard_pending_generation_memory_delivery(
    db: Any, *, operation_id: str
) -> bool:
    """Discard only an uncommitted publication's pending envelope."""

    if not getattr(db, "in_transaction", False):
        raise GenerationMemoryDeliveryError("memory_delivery_transaction_required")
    operation = _text(operation_id, "memory_delivery_operation_invalid")
    row = db.execute(
        f"SELECT state FROM {MEMORY_EVENT_DELIVERY_HEADS} WHERE operation_id=?",
        (operation,),
    ).fetchone()
    if row is None:
        return False
    if str(row[0]) != "pending":
        raise GenerationMemoryDeliveryError("memory_delivery_discard_delivered_forbidden")
    db.execute(
        f"DELETE FROM {MEMORY_EVENT_DELIVERY_HEADS} "
        "WHERE operation_id=? AND state='pending' AND revision=1",
        (operation,),
    )
    deleted = db.execute(
        f"DELETE FROM {MEMORY_EVENT_OUTBOX} WHERE operation_id=?", (operation,)
    )
    if deleted.rowcount != 1:
        raise GenerationMemoryDeliveryError("memory_delivery_discard_conflict")
    return True


def _log_failure(logger: Any, message: str) -> None:
    if logger is None:
        return
    try:
        logger.exception(message)
    except Exception:
        pass


def dispatch_generation_memory_delivery(
    db: Any,
    *,
    operation_id: str,
    memory: Any,
    logger: Any = None,
    delivered_at: str | None = None,
    fault_injector: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Deliver once after commit; a failed delivery always remains pending."""

    if getattr(db, "in_transaction", False):
        raise GenerationMemoryDeliveryError("memory_delivery_nested_dispatch")
    delivery = recover_generation_memory_delivery(db, operation_id=operation_id)
    if delivery.state == "delivered":
        return {"state": "delivered", "revision": 2, "idempotent_replay": True}
    try:
        normalized = memory.append_events(
            delivery.mode,
            delivery.actor,
            list(delivery.events),
            day=delivery.event_day,
            verify_duplicate_payload=True,
        )
        if len(normalized) != len(delivery.events):
            raise GenerationMemoryDeliveryError("memory_delivery_append_incomplete")
        if any(event.get("memory_paused") is True for event in normalized):
            return {
                "state": "pending",
                "revision": 1,
                "reason": "memory_paused",
            }
        if fault_injector is not None:
            fault_injector("after_memory_fsync_before_delivery_cas")
        timestamp = _timestamp(delivered_at)
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute(
            f"UPDATE {MEMORY_EVENT_DELIVERY_HEADS} SET "
            "state='delivered',revision=revision+1,delivered_at=?,updated_at=? "
            "WHERE operation_id=? AND state='pending' AND revision=1",
            (timestamp, timestamp, delivery.operation_id),
        )
        if updated.rowcount != 1:
            current = db.execute(
                f"SELECT state,revision FROM {MEMORY_EVENT_DELIVERY_HEADS} "
                "WHERE operation_id=?",
                (delivery.operation_id,),
            ).fetchone()
            if current is not None and tuple(current) == ("delivered", 2):
                db.commit()
                return {
                    "state": "delivered",
                    "revision": 2,
                    "idempotent_replay": True,
                }
            raise GenerationMemoryDeliveryError("memory_delivery_cas_lost")
        db.commit()
    except BaseException:
        if getattr(db, "in_transaction", False):
            db.rollback()
        _log_failure(logger, "failed to deliver published exam AI memory")
        raise
    return {"state": "delivered", "revision": 2, "idempotent_replay": False}


__all__ = [
    "GenerationMemoryDelivery",
    "GenerationMemoryDeliveryError",
    "discard_pending_generation_memory_delivery",
    "dispatch_generation_memory_delivery",
    "recover_generation_memory_delivery",
    "stage_generation_memory_delivery",
]
