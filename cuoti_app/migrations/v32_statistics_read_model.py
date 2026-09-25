"""Schema 32: synchronous, resumable statistics review read model."""

from __future__ import annotations

import sqlite3


SCHEDULE_VERSION = "fixed-3-5-10-15-v1"
READ_MODEL_TABLES = (
    "statistics_review_daily",
    "statistics_review_cycle_days",
    "statistics_review_cycle_masteries",
    "statistics_read_model_state",
)


def _daily_refresh(ref: str) -> str:
    day = f"substr({ref}.reviewed_at,1,10)"
    return f"""
        DELETE FROM statistics_review_daily
        WHERE mistake_id={ref}.mistake_id AND review_day={day};
        INSERT INTO statistics_review_daily(
            mistake_id,review_day,all_events,review_count,pass_count,again_count,
            manual_changes,first_pass_order,last_again_order,first_mastered_order)
        SELECT e.mistake_id,substr(e.reviewed_at,1,10),COUNT(*),
            SUM(CASE WHEN e.event_type IN ('pass','again') THEN 1 ELSE 0 END),
            SUM(CASE WHEN e.event_type='pass' THEN 1 ELSE 0 END),
            SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END),
            SUM(CASE WHEN e.event_type IN ('manual_master','manual_reopen') THEN 1 ELSE 0 END),
            MIN(CASE WHEN e.event_type='pass' THEN
                e.reviewed_at || '#' || printf('%020d',e.id) END),
            MAX(CASE WHEN e.event_type='again' THEN
                e.reviewed_at || '#' || printf('%020d',e.id) END),
            MIN(CASE WHEN e.event_type='pass' AND e.status_after='已掌握' THEN
                e.reviewed_at || '#' || printf('%020d',e.id) END)
        FROM review_events e
        WHERE e.mistake_id={ref}.mistake_id
          AND substr(e.reviewed_at,1,10)={day}
        GROUP BY e.mistake_id,substr(e.reviewed_at,1,10);
    """


def _daily_insert(ref: str) -> str:
    order = f"{ref}.reviewed_at || '#' || printf('%020d',{ref}.id)"
    return f"""
        INSERT INTO statistics_review_daily(
            mistake_id,review_day,all_events,review_count,pass_count,again_count,
            manual_changes,first_pass_order,last_again_order,first_mastered_order)
        VALUES(
            {ref}.mistake_id,substr({ref}.reviewed_at,1,10),1,
            CASE WHEN {ref}.event_type IN ('pass','again') THEN 1 ELSE 0 END,
            CASE WHEN {ref}.event_type='pass' THEN 1 ELSE 0 END,
            CASE WHEN {ref}.event_type='again' THEN 1 ELSE 0 END,
            CASE WHEN {ref}.event_type IN ('manual_master','manual_reopen') THEN 1 ELSE 0 END,
            CASE WHEN {ref}.event_type='pass' THEN {order} END,
            CASE WHEN {ref}.event_type='again' THEN {order} END,
            CASE WHEN {ref}.event_type='pass' AND {ref}.status_after='已掌握'
                 THEN {order} END)
        ON CONFLICT(mistake_id,review_day) DO UPDATE SET
            all_events=all_events+1,
            review_count=review_count+excluded.review_count,
            pass_count=pass_count+excluded.pass_count,
            again_count=again_count+excluded.again_count,
            manual_changes=manual_changes+excluded.manual_changes,
            first_pass_order=CASE
                WHEN excluded.first_pass_order IS NULL THEN first_pass_order
                WHEN first_pass_order IS NULL OR excluded.first_pass_order<first_pass_order
                    THEN excluded.first_pass_order ELSE first_pass_order END,
            last_again_order=CASE
                WHEN excluded.last_again_order IS NULL THEN last_again_order
                WHEN last_again_order IS NULL OR excluded.last_again_order>last_again_order
                    THEN excluded.last_again_order ELSE last_again_order END,
            first_mastered_order=CASE
                WHEN excluded.first_mastered_order IS NULL THEN first_mastered_order
                WHEN first_mastered_order IS NULL
                     OR excluded.first_mastered_order<first_mastered_order
                    THEN excluded.first_mastered_order ELSE first_mastered_order END;
    """


def _cycle_refresh(ref: str) -> str:
    day = f"substr({ref}.reviewed_at,1,10)"
    cycle = f"COALESCE({ref}.cycle_after,1)"
    eligible = (
        f"e.schedule_version='{SCHEDULE_VERSION}' "
        "AND e.stage_before IS NOT NULL AND e.stage_after IS NOT NULL"
    )
    same_cycle_day = (
        f"e.mistake_id={ref}.mistake_id AND COALESCE(e.cycle_after,1)={cycle} "
        f"AND substr(e.reviewed_at,1,10)={day}"
    )
    return f"""
        DELETE FROM statistics_review_cycle_days
        WHERE mistake_id={ref}.mistake_id AND review_cycle={cycle} AND review_day={day};
        INSERT INTO statistics_review_cycle_days(
            mistake_id,review_cycle,review_day,review_count,again_count)
        SELECT e.mistake_id,COALESCE(e.cycle_after,1),substr(e.reviewed_at,1,10),
            SUM(CASE WHEN e.event_type IN ('pass','again') THEN 1 ELSE 0 END),
            SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END)
        FROM review_events e WHERE {same_cycle_day} AND {eligible}
        GROUP BY e.mistake_id,COALESCE(e.cycle_after,1),substr(e.reviewed_at,1,10);
        DELETE FROM statistics_review_cycle_masteries
        WHERE mistake_id={ref}.mistake_id AND review_cycle={cycle}
          AND substr(reviewed_at,1,10)={day};
        INSERT OR REPLACE INTO statistics_review_cycle_masteries(
            mistake_id,review_cycle,event_id,reviewed_at,reviews_on_day_to_event)
        SELECT e.mistake_id,COALESCE(e.cycle_after,1),e.id,e.reviewed_at,
            (SELECT COUNT(*) FROM review_events prior
             WHERE prior.mistake_id=e.mistake_id
               AND COALESCE(prior.cycle_after,1)=COALESCE(e.cycle_after,1)
               AND substr(prior.reviewed_at,1,10)=substr(e.reviewed_at,1,10)
               AND prior.schedule_version='{SCHEDULE_VERSION}'
               AND prior.stage_before IS NOT NULL AND prior.stage_after IS NOT NULL
               AND prior.event_type IN ('pass','again')
               AND (prior.reviewed_at<e.reviewed_at
                    OR (prior.reviewed_at=e.reviewed_at AND prior.id<=e.id)))
        FROM review_events e WHERE {same_cycle_day} AND {eligible}
          AND e.event_type='pass' AND e.status_after='已掌握';
    """


def _cycle_insert(ref: str) -> str:
    day = f"substr({ref}.reviewed_at,1,10)"
    cycle = f"COALESCE({ref}.cycle_after,1)"
    eligible = (
        f"{ref}.schedule_version='{SCHEDULE_VERSION}' "
        f"AND {ref}.stage_before IS NOT NULL AND {ref}.stage_after IS NOT NULL"
    )
    return f"""
        INSERT INTO statistics_review_cycle_days(
            mistake_id,review_cycle,review_day,review_count,again_count)
        SELECT {ref}.mistake_id,{cycle},{day},
            CASE WHEN {ref}.event_type IN ('pass','again') THEN 1 ELSE 0 END,
            CASE WHEN {ref}.event_type='again' THEN 1 ELSE 0 END
        WHERE {eligible}
        ON CONFLICT(mistake_id,review_cycle,review_day) DO UPDATE SET
            review_count=review_count+excluded.review_count,
            again_count=again_count+excluded.again_count;
        INSERT OR REPLACE INTO statistics_review_cycle_masteries(
            mistake_id,review_cycle,event_id,reviewed_at,reviews_on_day_to_event)
        SELECT {ref}.mistake_id,{cycle},{ref}.id,{ref}.reviewed_at,
            (SELECT COUNT(*) FROM review_events prior
             WHERE prior.mistake_id={ref}.mistake_id
               AND COALESCE(prior.cycle_after,1)={cycle}
               AND substr(prior.reviewed_at,1,10)={day}
               AND prior.schedule_version='{SCHEDULE_VERSION}'
               AND prior.stage_before IS NOT NULL AND prior.stage_after IS NOT NULL
               AND prior.event_type IN ('pass','again')
               AND (prior.reviewed_at<{ref}.reviewed_at
                    OR (prior.reviewed_at={ref}.reviewed_at AND prior.id<={ref}.id)))
        WHERE {eligible} AND {ref}.event_type='pass' AND {ref}.status_after='已掌握';
    """


def _trigger(name: str, operation: str, body: str) -> str:
    return f"CREATE TRIGGER IF NOT EXISTS {name} AFTER {operation} ON review_events BEGIN {body} END"


SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS statistics_review_daily(
        mistake_id INTEGER NOT NULL,
        review_day TEXT NOT NULL,
        all_events INTEGER NOT NULL CHECK(all_events>=0),
        review_count INTEGER NOT NULL CHECK(review_count>=0),
        pass_count INTEGER NOT NULL CHECK(pass_count>=0),
        again_count INTEGER NOT NULL CHECK(again_count>=0),
        manual_changes INTEGER NOT NULL CHECK(manual_changes>=0),
        first_pass_order TEXT,
        last_again_order TEXT,
        first_mastered_order TEXT,
        PRIMARY KEY(mistake_id,review_day),
        FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS statistics_review_cycle_days(
        mistake_id INTEGER NOT NULL,
        review_cycle INTEGER NOT NULL,
        review_day TEXT NOT NULL,
        review_count INTEGER NOT NULL CHECK(review_count>=0),
        again_count INTEGER NOT NULL CHECK(again_count>=0),
        PRIMARY KEY(mistake_id,review_cycle,review_day),
        FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS statistics_review_cycle_masteries(
        mistake_id INTEGER NOT NULL,
        review_cycle INTEGER NOT NULL,
        event_id INTEGER PRIMARY KEY,
        reviewed_at TEXT NOT NULL,
        reviews_on_day_to_event INTEGER NOT NULL CHECK(reviews_on_day_to_event>=0),
        FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE,
        FOREIGN KEY(event_id) REFERENCES review_events(id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS statistics_read_model_state(
        id INTEGER PRIMARY KEY CHECK(id=1),
        status TEXT NOT NULL CHECK(status IN ('pending','running','verifying','complete','failed')),
        last_mistake_id INTEGER NOT NULL DEFAULT 0,
        processed_mistakes INTEGER NOT NULL DEFAULT 0,
        completed_at TEXT,
        updated_at TEXT NOT NULL
    )""",
    "INSERT OR IGNORE INTO statistics_read_model_state("
    "id,status,last_mistake_id,processed_mistakes,updated_at) "
    "VALUES(1,'pending',0,0,datetime('now'))",
    "CREATE INDEX IF NOT EXISTS idx_statistics_review_daily_day "
    "ON statistics_review_daily(review_day,mistake_id)",
    "CREATE INDEX IF NOT EXISTS idx_statistics_cycle_days_day "
    "ON statistics_review_cycle_days(review_day,mistake_id,review_cycle)",
    "CREATE INDEX IF NOT EXISTS idx_statistics_masteries_date "
    "ON statistics_review_cycle_masteries(reviewed_at,mistake_id,review_cycle)",
    _trigger(
        "statistics_review_events_insert",
        "INSERT",
        _daily_insert("NEW") + _cycle_insert("NEW"),
    ),
    _trigger(
        "statistics_review_events_delete",
        "DELETE",
        _daily_refresh("OLD") + _cycle_refresh("OLD"),
    ),
    _trigger(
        "statistics_review_events_update",
        "UPDATE OF mistake_id,event_type,reviewed_at,status_after,stage_before,"
        "stage_after,cycle_after,schedule_version",
        _daily_refresh("OLD") + _cycle_refresh("OLD")
        + _daily_refresh("NEW") + _cycle_refresh("NEW"),
    ),
)


REQUIRED_TRIGGERS = {
    "statistics_review_events_insert",
    "statistics_review_events_delete",
    "statistics_review_events_update",
}
REQUIRED_INDEXES = {
    "idx_statistics_review_daily_day",
    "idx_statistics_cycle_days_day",
    "idx_statistics_masteries_date",
}


def migrate_v31_to_v32(db: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        db.execute(statement)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    objects = {
        (str(row[0]), str(row[1]))
        for row in db.execute(
            "SELECT type,name FROM sqlite_master "
            "WHERE type IN ('table','index','trigger')"
        ).fetchall()
    }
    issues = [
        f"版本 32 缺少表 {name}"
        for name in READ_MODEL_TABLES
        if ("table", name) not in objects
    ]
    issues.extend(
        f"版本 32 缺少索引 {name}"
        for name in sorted(REQUIRED_INDEXES)
        if ("index", name) not in objects
    )
    issues.extend(
        f"版本 32 缺少触发器 {name}"
        for name in sorted(REQUIRED_TRIGGERS)
        if ("trigger", name) not in objects
    )
    return issues
