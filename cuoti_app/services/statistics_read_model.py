"""Resumable builders and activation controls for the P4 statistics read model."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..transactions import immediate_transaction


class StatisticsReadModelError(RuntimeError):
    pass


def read_model_status(db) -> dict:
    row = db.execute(
        "SELECT status,last_mistake_id,processed_mistakes,completed_at,updated_at "
        "FROM statistics_read_model_state WHERE id=1"
    ).fetchone()
    if row is None:
        raise StatisticsReadModelError("statistics read-model state is missing")
    return {
        "status": str(row[0]),
        "last_mistake_id": int(row[1]),
        "processed_mistakes": int(row[2]),
        "completed_at": row[3],
        "updated_at": str(row[4]),
    }


def _refresh_mistakes(db, mistake_ids: list[int]) -> None:
    if not mistake_ids:
        return
    marks = ",".join("?" for _ in mistake_ids)
    for table in (
        "statistics_review_daily",
        "statistics_review_cycle_days",
        "statistics_review_cycle_masteries",
    ):
        db.execute(f"DELETE FROM {table} WHERE mistake_id IN ({marks})", mistake_ids)
    db.execute(
        "INSERT INTO statistics_review_daily("
        "mistake_id,review_day,all_events,review_count,pass_count,again_count,"
        "manual_changes,first_pass_order,last_again_order,first_mastered_order) "
        "SELECT e.mistake_id,substr(e.reviewed_at,1,10),COUNT(*),"
        "SUM(CASE WHEN e.event_type IN ('pass','again') THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN e.event_type='pass' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN e.event_type IN ('manual_master','manual_reopen') THEN 1 ELSE 0 END),"
        "MIN(CASE WHEN e.event_type='pass' THEN "
        "e.reviewed_at || '#' || printf('%020d',e.id) END),"
        "MAX(CASE WHEN e.event_type='again' THEN "
        "e.reviewed_at || '#' || printf('%020d',e.id) END),"
        "MIN(CASE WHEN e.event_type='pass' AND e.status_after='已掌握' THEN "
        "e.reviewed_at || '#' || printf('%020d',e.id) END) "
        f"FROM review_events e WHERE e.mistake_id IN ({marks}) "
        "GROUP BY e.mistake_id,substr(e.reviewed_at,1,10)",
        mistake_ids,
    )
    eligible = (
        "e.schedule_version='fixed-3-5-10-15-v1' "
        "AND e.stage_before IS NOT NULL AND e.stage_after IS NOT NULL"
    )
    db.execute(
        "INSERT INTO statistics_review_cycle_days("
        "mistake_id,review_cycle,review_day,review_count,again_count) "
        "SELECT e.mistake_id,COALESCE(e.cycle_after,1),substr(e.reviewed_at,1,10),"
        "SUM(CASE WHEN e.event_type IN ('pass','again') THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END) "
        f"FROM review_events e WHERE e.mistake_id IN ({marks}) AND {eligible} "
        "GROUP BY e.mistake_id,COALESCE(e.cycle_after,1),substr(e.reviewed_at,1,10)",
        mistake_ids,
    )
    db.execute(
        "INSERT INTO statistics_review_cycle_masteries("
        "mistake_id,review_cycle,event_id,reviewed_at,reviews_on_day_to_event) "
        "SELECT e.mistake_id,COALESCE(e.cycle_after,1),e.id,e.reviewed_at,"
        "(SELECT COUNT(*) FROM review_events prior "
        "WHERE prior.mistake_id=e.mistake_id "
        "AND COALESCE(prior.cycle_after,1)=COALESCE(e.cycle_after,1) "
        "AND substr(prior.reviewed_at,1,10)=substr(e.reviewed_at,1,10) "
        "AND prior.schedule_version='fixed-3-5-10-15-v1' "
        "AND prior.stage_before IS NOT NULL AND prior.stage_after IS NOT NULL "
        "AND prior.event_type IN ('pass','again') "
        "AND (prior.reviewed_at<e.reviewed_at "
        "OR (prior.reviewed_at=e.reviewed_at AND prior.id<=e.id))) "
        f"FROM review_events e WHERE e.mistake_id IN ({marks}) AND {eligible} "
        "AND e.event_type='pass' AND e.status_after='已掌握'",
        mistake_ids,
    )


def backfill_statistics_read_model(
    db,
    *,
    batch_size: int = 250,
    progress: Callable[[dict[str, int]], None] | None = None,
) -> dict:
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    state = read_model_status(db)
    if state["status"] == "complete":
        return {**state, "reused": True}
    cursor = state["last_mistake_id"]
    processed = state["processed_mistakes"]
    with immediate_transaction(db):
        db.execute(
            "UPDATE statistics_read_model_state SET status='running',updated_at=? WHERE id=1",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),),
        )
    while True:
        ids = [
            int(row[0])
            for row in db.execute(
                "SELECT id FROM mistakes WHERE id>? ORDER BY id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
        ]
        if not ids:
            break
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with immediate_transaction(db):
            _refresh_mistakes(db, ids)
            cursor = ids[-1]
            processed += len(ids)
            db.execute(
                "UPDATE statistics_read_model_state SET last_mistake_id=?,"
                "processed_mistakes=?,updated_at=? WHERE id=1",
                (cursor, processed, now),
            )
        if progress:
            progress({"processed_mistakes": processed, "cursor": cursor})
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with immediate_transaction(db):
        db.execute(
            "UPDATE statistics_read_model_state SET status='verifying',updated_at=? WHERE id=1",
            (now,),
        )
    return {**read_model_status(db), "reused": False}


def activate_statistics_read_model(db) -> dict:
    state = read_model_status(db)
    if state["status"] not in {"verifying", "complete"}:
        raise StatisticsReadModelError("statistics read model has not finished backfill")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with immediate_transaction(db):
        db.execute(
            "UPDATE statistics_read_model_state SET status='complete',"
            "completed_at=COALESCE(completed_at,?),updated_at=? WHERE id=1",
            (now, now),
        )
    return read_model_status(db)
