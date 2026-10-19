"""Deterministic, answer-free learning statistics and reminder rules."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from threading import RLock
from time import monotonic


MAX_RANGE_DAYS = 730
RULE_THRESHOLDS = {
    "weak_chapter_rate": 0.60,
    "weak_chapter_min_samples": 5,
    "again_rise_min_samples": 10,
    "again_rise_points": 0.15,
    "stale_subject_days": 30,
    "stale_subject_min_active": 3,
}

STATISTICS_CACHE_TTL_SECONDS = 3.0
_statistics_cache_lock = RLock()
_statistics_cache: dict[tuple, tuple[float, dict]] = {}
_reminders_cache: dict[tuple, tuple[float, dict]] = {}


class StatisticsValidationError(ValueError):
    pass


def _iso_day(value, label: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise StatisticsValidationError(f"{label}必须是 YYYY-MM-DD") from exc


def parse_scope(args, today: date | None = None) -> dict:
    today = today or datetime.now().astimezone().date()
    end = _iso_day(args.get("date_to") or today.isoformat(), "结束日期")
    start = _iso_day(
        args.get("date_from") or (end - timedelta(days=29)).isoformat(),
        "开始日期",
    )
    if start > end:
        raise StatisticsValidationError("开始日期不能晚于结束日期")
    days = (end - start).days + 1
    if days > MAX_RANGE_DAYS:
        raise StatisticsValidationError(f"统计日期范围最多 {MAX_RANGE_DAYS} 天")
    if end > today:
        raise StatisticsValidationError("结束日期不能晚于今天")

    result = {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "days": days,
        "subject_id": None,
        "chapter_id": None,
        "granularity": "day" if days <= 90 else ("week" if days <= 365 else "month"),
    }
    for key, label in (("subject_id", "科目"), ("chapter_id", "章节")):
        raw = args.get(key)
        if raw not in (None, ""):
            try:
                parsed = int(raw)
            except (TypeError, ValueError) as exc:
                raise StatisticsValidationError(f"{label}编号无效") from exc
            if parsed <= 0:
                raise StatisticsValidationError(f"{label}编号无效")
            result[key] = parsed
    return result


def _revisions(db) -> tuple[int, int]:
    row = db.execute(
        "SELECT learning_revision,statistics_revision FROM app_revisions WHERE id=1"
    ).fetchone()
    if row is None:
        return (0, 0)
    return (int(row[0] or 0), int(row[1] or 0))


def ensure_daily_snapshot(db, now: datetime | None = None) -> dict[str, int | bool]:
    now = now or datetime.now().astimezone()
    day = now.date().isoformat()
    captured_at = now.isoformat(timespec="seconds")
    learning_revision, _statistics_revision = _revisions(db)
    current = db.execute(
        "SELECT learning_revision FROM learning_daily_snapshots "
        "WHERE snapshot_day=? AND subject_id=0 AND chapter_id=0",
        (day,),
    ).fetchone()
    existing_groups = db.execute(
        "SELECT subject_id,chapter_id,learning_revision FROM learning_daily_snapshots "
        "WHERE snapshot_day=?", (day,),
    ).fetchall()
    if (current is not None and int(current[0] or 0) == learning_revision
            and all(int(row["learning_revision"] or 0) == learning_revision
                    for row in existing_groups)):
        return {"updated": False, "rows": 0, "learning_revision": learning_revision}
    rows = db.execute(
        "SELECT COALESCE(subject_id,0) AS subject_id,COALESCE(chapter_id,0) AS chapter_id,"
        "COUNT(*) AS total_count,SUM(CASE WHEN status='已掌握' THEN 1 ELSE 0 END) "
        "AS mastered_count,SUM(CASE WHEN status='未掌握' THEN 1 ELSE 0 END) "
        "AS active_count FROM mistakes WHERE deleted_at IS NULL "
        "GROUP BY COALESCE(subject_id,0),COALESCE(chapter_id,0)"
    ).fetchall()
    grouped: dict[tuple[int, int], list[int]] = {}
    for row in rows:
        subject_id, chapter_id = int(row["subject_id"] or 0), int(row["chapter_id"] or 0)
        values = [int(row["total_count"] or 0), int(row["mastered_count"] or 0), int(row["active_count"] or 0)]
        grouped[(subject_id, chapter_id)] = values
        aggregate = grouped.setdefault((subject_id, 0), [0, 0, 0])
        if chapter_id != 0:
            for index, value in enumerate(values):
                aggregate[index] += value
    overall = [sum(int(row[key] or 0) for row in rows) for key in ("total_count", "mastered_count", "active_count")]
    grouped[(0, 0)] = overall
    for row in existing_groups:
        grouped.setdefault((int(row["subject_id"]), int(row["chapter_id"])), [0, 0, 0])
    totals = [
        (
            day,
            subject_id,
            chapter_id,
            values[0],
            values[1],
            values[2],
            captured_at,
            learning_revision,
        )
        for (subject_id, chapter_id), values in grouped.items()
    ]
    db.executemany(
        "INSERT INTO learning_daily_snapshots(snapshot_day,subject_id,chapter_id,"
        "total_count,mastered_count,active_count,captured_at,estimated,learning_revision) "
        "VALUES(?,?,?,?,?,?,?,0,?) ON CONFLICT(snapshot_day,subject_id,chapter_id) "
        "DO UPDATE SET total_count=excluded.total_count,"
        "mastered_count=excluded.mastered_count,active_count=excluded.active_count,"
        "captured_at=excluded.captured_at,estimated=0,"
        "learning_revision=excluded.learning_revision",
        totals,
    )
    db.commit()
    return {
        "updated": True,
        "rows": len(totals),
        "learning_revision": learning_revision,
    }


def _database_cache_identity(db) -> tuple[str, str | int]:
    """Identify the request database so process-global TTL entries stay isolated."""

    try:
        rows = db.execute("PRAGMA database_list").fetchall()
        for row in rows:
            keys = row.keys() if hasattr(row, "keys") else ()
            name = row["name"] if "name" in keys else row[1]
            if name != "main":
                continue
            filename = row["file"] if "file" in keys else row[2]
            if filename:
                return ("file", str(Path(str(filename)).resolve()))
            return ("memory", id(db))
    except Exception:
        # A closed or non-SQLite test double cannot share a trustworthy cache
        # identity with another connection.
        pass
    return ("connection", id(db))


def _scope_cache_key(db, scope: dict) -> tuple:
    _learning_revision, statistics_revision = _revisions(db)
    return (_database_cache_identity(db), statistics_revision) + tuple(
        (key, scope.get(key))
        for key in ("date_from", "date_to", "subject_id", "chapter_id", "granularity")
    )


def invalidate_statistics_cache() -> None:
    """Drop request-independent cached aggregates after a learning write."""

    with _statistics_cache_lock:
        _statistics_cache.clear()
        _reminders_cache.clear()


def _cached_value(cache: dict, key: tuple, builder):
    now = monotonic()
    with _statistics_cache_lock:
        cached = cache.get(key)
        if cached and now - cached[0] < STATISTICS_CACHE_TTL_SECONDS:
            value = deepcopy(cached[1])
            value.setdefault("meta", {})
            value["meta"].update(
                {
                    "cached": True,
                    "cache_ttl_seconds": STATISTICS_CACHE_TTL_SECONDS,
                }
            )
            return value

    # Build outside the lock so a slow aggregate never blocks invalidation or
    # another scope.  The short TTL bounds the amount of duplicate work under
    # a burst while keeping the database connection request-owned.
    value = builder()
    value = deepcopy(value)
    value.setdefault("meta", {})
    value["meta"].update(
        {
            "cached": False,
            "cache_ttl_seconds": STATISTICS_CACHE_TTL_SECONDS,
        }
    )
    with _statistics_cache_lock:
        cache[key] = (monotonic(), value)
    return deepcopy(value)


def cached_statistics(db, scope: dict) -> dict:
    return _cached_value(
        _statistics_cache,
        _scope_cache_key(db, scope),
        lambda: build_statistics(db, scope),
    )


def cached_reminders(db, scope: dict) -> dict:
    return _cached_value(
        _reminders_cache,
        _scope_cache_key(db, scope),
        lambda: build_reminders(db, scope),
    )


def _mistake_scope(scope: dict, alias: str = "m") -> tuple[list[str], list]:
    clauses = [f"{alias}.deleted_at IS NULL"]
    params: list = []
    if scope["subject_id"]:
        clauses.append(f"{alias}.subject_id=?")
        params.append(scope["subject_id"])
    if scope["chapter_id"]:
        clauses.append(f"{alias}.chapter_id=?")
        params.append(scope["chapter_id"])
    return clauses, params


def _bucket(day_text: str, granularity: str) -> str:
    parsed = date.fromisoformat(day_text[:10])
    if granularity == "month":
        return parsed.strftime("%Y-%m")
    if granularity == "week":
        monday = parsed - timedelta(days=parsed.weekday())
        return monday.isoformat()
    return parsed.isoformat()


def _review_metrics_legacy(db, scope: dict, complete_since: str | None) -> tuple[dict, list[dict], list[dict], list[dict]]:
    clauses, params = _mistake_scope(scope)
    rows = db.execute(
        "SELECT e.id,e.mistake_id,e.event_type,e.reviewed_at,e.status_after,"
        "e.correct_streak,e.stage_before,e.stage_after,e.cycle_after,"
        "e.next_review_after,e.schedule_version,m.history_complete,m.history_complete_from "
        "FROM review_events e JOIN mistakes m ON m.id=e.mistake_id WHERE "
        + " AND ".join(clauses)
        + " AND substr(e.reviewed_at,1,10) BETWEEN ? AND ? "
        "ORDER BY e.reviewed_at,e.id",
        params + [scope["date_from"], scope["date_to"]],
    ).fetchall()
    normal = [row for row in rows if row["event_type"] in ("pass", "again")]
    # Pass/again rates only need a complete event stream inside the selected
    # range. From the feature enable date onward that stream is complete even
    # for a legacy mistake whose earlier mastery sequence is incomplete.
    complete = [
        row for row in normal
        if complete_since and row["reviewed_at"][:10] >= complete_since
    ]
    passed = sum(row["event_type"] == "pass" for row in complete)
    again = sum(row["event_type"] == "again" for row in complete)

    # Normal-mastery cycles are only reliable after the event stream became
    # complete. Bounding here avoids scanning pre-feature history for every
    # request while preserving every cycle that can contribute to the metric.
    complete_history_start = db.execute(
        "SELECT MIN(m.history_complete_from) FROM mistakes m WHERE "
        + " AND ".join(clauses)
        + " AND m.history_complete=1 AND m.history_complete_from IS NOT NULL",
        params,
    ).fetchone()[0]
    starts = [value for value in (complete_since, complete_history_start) if value]
    cycle_start = min(starts) if starts else scope["date_from"]
    all_complete = db.execute(
        "SELECT e.id,e.mistake_id,e.event_type,e.reviewed_at,e.status_after,"
        "e.correct_streak,e.stage_before,e.stage_after,e.cycle_after,"
        "e.next_review_after,e.schedule_version FROM review_events e "
        "JOIN mistakes m ON m.id=e.mistake_id "
        "WHERE " + " AND ".join(clauses)
        + " AND e.schedule_version=? AND substr(e.reviewed_at,1,10)>=? "
        "AND e.stage_before IS NOT NULL AND e.stage_after IS NOT NULL "
        "ORDER BY e.mistake_id,e.reviewed_at,e.id",
        params + [SCHEDULE_VERSION, cycle_start],
    ).fetchall()
    grouped: dict[tuple[int, int], list] = defaultdict(list)
    for row in all_complete:
        grouped[(int(row["mistake_id"]), int(row["cycle_after"] or 1))].append(row)
    forgotten: set[int] = set()
    mastery_attempts: list[int] = []
    reset_rows = [
        row for row in all_complete
        if row["event_type"] == "again"
        and scope["date_from"] <= row["reviewed_at"][:10] <= scope["date_to"]
    ]
    reset_events = len(reset_rows)
    reset_mistakes: set[int] = {int(row["mistake_id"]) for row in reset_rows}
    seen_pass_by_mistake: set[int] = set()
    for event in rows:
        mistake_id = int(event["mistake_id"])
        if event["event_type"] == "pass":
            seen_pass_by_mistake.add(mistake_id)
        elif (
            event["event_type"] == "again"
            and mistake_id in seen_pass_by_mistake
            and scope["date_from"] <= event["reviewed_at"][:10] <= scope["date_to"]
        ):
            forgotten.add(mistake_id)
    normally_mastered: set[int] = set()
    for (mistake_id, _cycle), events in grouped.items():
        review_count = 0
        for event in events:
            kind = event["event_type"]
            event_day = event["reviewed_at"][:10]
            if kind in {"pass", "again"}:
                review_count += 1
            if kind == "pass":
                if (
                    event["status_after"] == "已掌握"
                    and review_count > 0
                    and scope["date_from"] <= event_day <= scope["date_to"]
                ):
                    mastery_attempts.append(review_count)
                    normally_mastered.add(mistake_id)
                    break

    trend_map: dict[str, dict] = {}
    for row in rows:
        key = _bucket(row["reviewed_at"][:10], scope["granularity"])
        entry = trend_map.setdefault(
            key,
            {"period": key, "reviews": 0, "pass": 0, "again": 0,
             "first_mastered": 0, "manual_changes": 0},
        )
        kind = row["event_type"]
        if kind in ("pass", "again"):
            entry["reviews"] += 1
            entry[kind] += 1
        elif kind in ("manual_master", "manual_reopen"):
            entry["manual_changes"] += 1
    first_mastered_ids: set[int] = set()
    for row in rows:
        if row["event_type"] == "pass" and row["status_after"] == "已掌握":
            if row["mistake_id"] not in first_mastered_ids:
                trend_map[_bucket(row["reviewed_at"][:10], scope["granularity"])]["first_mastered"] += 1
                first_mastered_ids.add(row["mistake_id"])

    heat = defaultdict(lambda: {"reviews": 0, "pass": 0, "again": 0})
    for row in normal:
        day = row["reviewed_at"][:10]
        heat[day]["reviews"] += 1
        heat[day][row["event_type"]] += 1
    heatmap = [{"day": key, **heat[key]} for key in sorted(heat)]
    metric = {
        "review_sample": len(complete),
        "known_review_events": len(normal),
        "pass": passed,
        "again": again,
        "review_pass_rate": passed / len(complete) if complete else None,
        "again_rate": again / len(complete) if complete else None,
        "forgotten_mistakes": len(forgotten),
        "cycle_reset_events": reset_events,
        "cycle_reset_mistakes": len(reset_mistakes),
        "normal_cycle_mastered": len(normally_mastered),
        "mastery_sample": len(mastery_attempts),
        "average_reviews_to_mastery": (
            sum(mastery_attempts) / len(mastery_attempts) if mastery_attempts else None
        ),
    }
    return metric, [trend_map[key] for key in sorted(trend_map)], heatmap, [dict(row) for row in rows]


def _period_sql(day_sql: str, granularity: str) -> str:
    if granularity == "month":
        return f"substr({day_sql},1,7)"
    if granularity == "week":
        return (
            f"date({day_sql},'-' || "
            f"((CAST(strftime('%w',{day_sql}) AS INTEGER)+6)%7) || ' days')"
        )
    return day_sql


def _review_metrics_sql(db, scope: dict, complete_since: str | None) -> tuple[dict, list[dict], list[dict], dict]:
    """Produce the legacy contract with bounded SQL aggregates instead of row copies."""

    clauses, params = _mistake_scope(scope)
    day_sql = "substr(e.reviewed_at,1,10)"
    period_sql = _period_sql(day_sql, scope["granularity"])
    scoped_where = (
        " FROM review_events e JOIN mistakes m ON m.id=e.mistake_id WHERE "
        + " AND ".join(clauses)
        + f" AND {day_sql} BETWEEN ? AND ?"
    )
    scoped_params = params + [scope["date_from"], scope["date_to"]]
    daily_rows = db.execute(
        "SELECT " + period_sql + " AS period," + day_sql + " AS event_day,"
        "COUNT(*) AS all_events,"
        "SUM(CASE WHEN e.event_type IN ('pass','again') THEN 1 ELSE 0 END) AS reviews,"
        "SUM(CASE WHEN e.event_type='pass' THEN 1 ELSE 0 END) AS pass_count,"
        "SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END) AS again_count,"
        "SUM(CASE WHEN e.event_type IN ('manual_master','manual_reopen') THEN 1 ELSE 0 END) "
        "AS manual_changes" + scoped_where
        + " GROUP BY period,event_day ORDER BY period,event_day",
        scoped_params,
    ).fetchall()
    trend_map: dict[str, dict] = {}
    heatmap: list[dict] = []
    known_review_events = 0
    for row in daily_rows:
        period = str(row["period"])
        entry = trend_map.setdefault(
            period,
            {
                "period": period,
                "reviews": 0,
                "pass": 0,
                "again": 0,
                "first_mastered": 0,
                "manual_changes": 0,
            },
        )
        reviews = int(row["reviews"] or 0)
        passed = int(row["pass_count"] or 0)
        again = int(row["again_count"] or 0)
        entry["reviews"] += reviews
        entry["pass"] += passed
        entry["again"] += again
        entry["manual_changes"] += int(row["manual_changes"] or 0)
        known_review_events += reviews
        if reviews:
            heatmap.append(
                {
                    "day": str(row["event_day"]),
                    "reviews": reviews,
                    "pass": passed,
                    "again": again,
                }
            )

    first_mastered = db.execute(
        "WITH mastered AS (SELECT e.mistake_id,e.reviewed_at,e.id,"
        + period_sql
        + " AS period,ROW_NUMBER() OVER(PARTITION BY e.mistake_id "
        "ORDER BY e.reviewed_at,e.id) AS sequence"
        + scoped_where
        + " AND e.event_type='pass' AND e.status_after='已掌握') "
        "SELECT period,COUNT(*) AS n FROM mastered WHERE sequence=1 "
        "GROUP BY period ORDER BY period",
        scoped_params,
    ).fetchall()
    for row in first_mastered:
        trend_map[str(row["period"])]["first_mastered"] = int(row["n"] or 0)

    if complete_since:
        complete = db.execute(
            "SELECT SUM(CASE WHEN e.event_type='pass' THEN 1 ELSE 0 END) AS pass_count,"
            "SUM(CASE WHEN e.event_type='again' THEN 1 ELSE 0 END) AS again_count"
            + scoped_where
            + " AND e.event_type IN ('pass','again') AND "
            + day_sql
            + ">=?",
            scoped_params + [complete_since],
        ).fetchone()
        passed = int(complete["pass_count"] or 0)
        again = int(complete["again_count"] or 0)
    else:
        passed = again = 0
    review_sample = passed + again

    forgotten = int(
        db.execute(
            "WITH scoped AS (SELECT e.mistake_id,e.event_type,e.reviewed_at,e.id"
            + scoped_where
            + "),ordered AS (SELECT mistake_id,event_type,"
            "SUM(CASE WHEN event_type='pass' THEN 1 ELSE 0 END) OVER("
            "PARTITION BY mistake_id ORDER BY reviewed_at,id ROWS BETWEEN "
            "UNBOUNDED PRECEDING AND 1 PRECEDING) AS prior_pass FROM scoped) "
            "SELECT COUNT(DISTINCT mistake_id) FROM ordered "
            "WHERE event_type='again' AND prior_pass>0",
            scoped_params,
        ).fetchone()[0]
        or 0
    )

    complete_history_start = db.execute(
        "SELECT MIN(m.history_complete_from) FROM mistakes m WHERE "
        + " AND ".join(clauses)
        + " AND m.history_complete=1 AND m.history_complete_from IS NOT NULL",
        params,
    ).fetchone()[0]
    starts = [value for value in (complete_since, complete_history_start) if value]
    cycle_start = min(starts) if starts else scope["date_from"]
    cycle_where = (
        " FROM review_events e JOIN mistakes m ON m.id=e.mistake_id WHERE "
        + " AND ".join(clauses)
        + " AND e.schedule_version=? AND "
        + day_sql
        + ">=? AND e.stage_before IS NOT NULL AND e.stage_after IS NOT NULL"
    )
    cycle_params = params + [SCHEDULE_VERSION, cycle_start]
    reset = db.execute(
        "SELECT COUNT(*) AS reset_events,COUNT(DISTINCT e.mistake_id) AS reset_mistakes"
        + cycle_where
        + " AND e.event_type='again' AND "
        + day_sql
        + " BETWEEN ? AND ?",
        cycle_params + [scope["date_from"], scope["date_to"]],
    ).fetchone()
    mastery = db.execute(
        "WITH eligible AS (SELECT e.id,e.mistake_id,e.event_type,e.reviewed_at,"
        "e.status_after,COALESCE(e.cycle_after,1) AS review_cycle"
        + cycle_where
        + "),numbered AS (SELECT *,SUM(CASE WHEN event_type IN ('pass','again') "
        "THEN 1 ELSE 0 END) OVER(PARTITION BY mistake_id,review_cycle "
        "ORDER BY reviewed_at,id ROWS UNBOUNDED PRECEDING) AS review_count "
        "FROM eligible),mastered AS (SELECT *,ROW_NUMBER() OVER("
        "PARTITION BY mistake_id,review_cycle ORDER BY reviewed_at,id) AS sequence "
        "FROM numbered WHERE event_type='pass' AND status_after='已掌握' "
        "AND substr(reviewed_at,1,10) BETWEEN ? AND ?) "
        "SELECT COUNT(*) AS mastery_sample,COUNT(DISTINCT mistake_id) AS mastered_mistakes,"
        "AVG(review_count) AS average_reviews FROM mastered WHERE sequence=1",
        cycle_params + [scope["date_from"], scope["date_to"]],
    ).fetchone()
    mastery_sample = int(mastery["mastery_sample"] or 0)

    midpoint = max(1, scope["days"] // 2)
    split = (date.fromisoformat(scope["date_to"]) - timedelta(days=midpoint - 1)).isoformat()
    prior_pass = prior_again = recent_pass = recent_again = 0
    for row in daily_rows:
        target = "prior" if str(row["event_day"]) < split else "recent"
        if target == "prior":
            prior_pass += int(row["pass_count"] or 0)
            prior_again += int(row["again_count"] or 0)
        else:
            recent_pass += int(row["pass_count"] or 0)
            recent_again += int(row["again_count"] or 0)
    activity = {
        "split": split,
        "prior_pass": prior_pass,
        "prior_again": prior_again,
        "recent_pass": recent_pass,
        "recent_again": recent_again,
    }
    metric = {
        "review_sample": review_sample,
        "known_review_events": known_review_events,
        "pass": passed,
        "again": again,
        "review_pass_rate": passed / review_sample if review_sample else None,
        "again_rate": again / review_sample if review_sample else None,
        "forgotten_mistakes": forgotten,
        "cycle_reset_events": int(reset["reset_events"] or 0),
        "cycle_reset_mistakes": int(reset["reset_mistakes"] or 0),
        "normal_cycle_mastered": int(mastery["mastered_mistakes"] or 0),
        "mastery_sample": mastery_sample,
        "average_reviews_to_mastery": (
            float(mastery["average_reviews"]) if mastery_sample else None
        ),
    }
    return metric, [trend_map[key] for key in sorted(trend_map)], heatmap, activity


def _review_metrics_model(db, scope: dict, complete_since: str | None):
    clauses, params = _mistake_scope(scope)
    period_sql = _period_sql("d.review_day", scope["granularity"])
    daily_join = (
        "statistics_review_daily d JOIN mistakes m ON m.id=d.mistake_id"
        if scope["subject_id"] or scope["chapter_id"]
        else "statistics_review_daily d INDEXED BY idx_statistics_review_daily_day "
        "CROSS JOIN mistakes m ON m.id=d.mistake_id"
    )
    model_from = (
        " FROM " + daily_join + " WHERE "
        + " AND ".join(clauses)
        + " AND d.review_day BETWEEN ? AND ?"
    )
    model_params = params + [scope["date_from"], scope["date_to"]]
    daily_rows = db.execute(
        "SELECT " + period_sql + " AS period,d.review_day,"
        "SUM(d.all_events) AS all_events,SUM(d.review_count) AS reviews,"
        "SUM(d.pass_count) AS pass_count,SUM(d.again_count) AS again_count,"
        "SUM(d.manual_changes) AS manual_changes" + model_from
        + " GROUP BY period,d.review_day ORDER BY period,d.review_day",
        model_params,
    ).fetchall()
    trend_map: dict[str, dict] = {}
    heatmap: list[dict] = []
    known_review_events = passed = again = 0
    for row in daily_rows:
        period = str(row["period"])
        entry = trend_map.setdefault(
            period,
            {
                "period": period,
                "reviews": 0,
                "pass": 0,
                "again": 0,
                "first_mastered": 0,
                "manual_changes": 0,
            },
        )
        reviews = int(row["reviews"] or 0)
        pass_count = int(row["pass_count"] or 0)
        again_count = int(row["again_count"] or 0)
        entry["reviews"] += reviews
        entry["pass"] += pass_count
        entry["again"] += again_count
        entry["manual_changes"] += int(row["manual_changes"] or 0)
        known_review_events += reviews
        if complete_since and str(row["review_day"]) >= complete_since:
            passed += pass_count
            again += again_count
        if reviews:
            heatmap.append(
                {
                    "day": str(row["review_day"]),
                    "reviews": reviews,
                    "pass": pass_count,
                    "again": again_count,
                }
            )

    first_mastered = db.execute(
        "WITH candidates AS (SELECT d.mistake_id,d.first_mastered_order,"
        + period_sql
        + " AS period,ROW_NUMBER() OVER(PARTITION BY d.mistake_id "
        "ORDER BY d.first_mastered_order) AS sequence"
        + model_from
        + " AND d.first_mastered_order IS NOT NULL) "
        "SELECT period,COUNT(*) AS n FROM candidates WHERE sequence=1 "
        "GROUP BY period ORDER BY period",
        model_params,
    ).fetchall()
    for row in first_mastered:
        trend_map[str(row["period"])]["first_mastered"] = int(row["n"] or 0)

    forgotten = int(
        db.execute(
            "SELECT COUNT(*) FROM (SELECT d.mistake_id,"
            "MIN(d.first_pass_order) AS first_pass,MAX(d.last_again_order) AS last_again"
            + model_from
            + " GROUP BY d.mistake_id HAVING first_pass IS NOT NULL "
            "AND last_again IS NOT NULL AND first_pass<last_again)",
            model_params,
        ).fetchone()[0]
        or 0
    )
    complete_history_start = db.execute(
        "SELECT MIN(m.history_complete_from) FROM mistakes m WHERE "
        + " AND ".join(clauses)
        + " AND m.history_complete=1 AND m.history_complete_from IS NOT NULL",
        params,
    ).fetchone()[0]
    starts = [value for value in (complete_since, complete_history_start) if value]
    cycle_start = min(starts) if starts else scope["date_from"]
    cycle_join = (
        "statistics_review_cycle_days cd JOIN mistakes m ON m.id=cd.mistake_id"
        if scope["subject_id"] or scope["chapter_id"]
        else "statistics_review_cycle_days cd INDEXED BY idx_statistics_cycle_days_day "
        "CROSS JOIN mistakes m ON m.id=cd.mistake_id"
    )
    cycle_from = (
        " FROM " + cycle_join + " WHERE "
        + " AND ".join(clauses)
        + " AND cd.review_day BETWEEN ? AND ?"
    )
    reset = db.execute(
        "SELECT SUM(cd.again_count) AS reset_events,"
        "COUNT(DISTINCT CASE WHEN cd.again_count>0 THEN cd.mistake_id END) "
        "AS reset_mistakes" + cycle_from,
        params + [scope["date_from"], scope["date_to"]],
    ).fetchone()
    mastery_from = (
        " FROM statistics_review_cycle_masteries cm "
        "JOIN mistakes m ON m.id=cm.mistake_id WHERE "
        + " AND ".join(clauses)
        + " AND substr(cm.reviewed_at,1,10) BETWEEN ? AND ?"
    )
    mastery = db.execute(
        "WITH candidates AS (SELECT cm.mistake_id,cm.review_cycle,cm.reviewed_at,cm.event_id,"
        "cm.reviews_on_day_to_event + COALESCE((SELECT SUM(cd.review_count) "
        "FROM statistics_review_cycle_days cd WHERE cd.mistake_id=cm.mistake_id "
        "AND cd.review_cycle=cm.review_cycle AND cd.review_day>=? "
        "AND cd.review_day<substr(cm.reviewed_at,1,10)),0) AS review_count,"
        "ROW_NUMBER() OVER(PARTITION BY cm.mistake_id,cm.review_cycle "
        "ORDER BY cm.reviewed_at,cm.event_id) AS sequence"
        + mastery_from
        + ") SELECT COUNT(*) AS mastery_sample,COUNT(DISTINCT mistake_id) AS mastered_mistakes,"
        "AVG(review_count) AS average_reviews FROM candidates WHERE sequence=1",
        [cycle_start, *params, scope["date_from"], scope["date_to"]],
    ).fetchone()
    mastery_sample = int(mastery["mastery_sample"] or 0)
    midpoint = max(1, scope["days"] // 2)
    split = (date.fromisoformat(scope["date_to"]) - timedelta(days=midpoint - 1)).isoformat()
    prior_pass = prior_again = recent_pass = recent_again = 0
    for row in daily_rows:
        if str(row["review_day"]) < split:
            prior_pass += int(row["pass_count"] or 0)
            prior_again += int(row["again_count"] or 0)
        else:
            recent_pass += int(row["pass_count"] or 0)
            recent_again += int(row["again_count"] or 0)
    review_sample = passed + again
    metric = {
        "review_sample": review_sample,
        "known_review_events": known_review_events,
        "pass": passed,
        "again": again,
        "review_pass_rate": passed / review_sample if review_sample else None,
        "again_rate": again / review_sample if review_sample else None,
        "forgotten_mistakes": forgotten,
        "cycle_reset_events": int(reset["reset_events"] or 0),
        "cycle_reset_mistakes": int(reset["reset_mistakes"] or 0),
        "normal_cycle_mastered": int(mastery["mastered_mistakes"] or 0),
        "mastery_sample": mastery_sample,
        "average_reviews_to_mastery": (
            float(mastery["average_reviews"]) if mastery_sample else None
        ),
    }
    activity = {
        "split": split,
        "prior_pass": prior_pass,
        "prior_again": prior_again,
        "recent_pass": recent_pass,
        "recent_again": recent_again,
    }
    return metric, [trend_map[key] for key in sorted(trend_map)], heatmap, activity


def _review_metrics(db, scope: dict, complete_since: str | None):
    try:
        state = db.execute(
            "SELECT status FROM statistics_read_model_state WHERE id=1"
        ).fetchone()
    except Exception:
        state = None
    if state is not None and str(state[0]) == "complete":
        return _review_metrics_model(db, scope, complete_since)
    return _review_metrics_legacy(db, scope, complete_since)


def _exam_metrics_legacy(db, scope: dict) -> dict:
    # Chapter attribution is centralized so the fallback cannot drift from the
    # primary path when one published item has several mistake sources.
    return _exam_metrics(db, scope)


def _exam_metrics(db, scope: dict) -> dict:
    clauses = ["a.status='submitted'", "e.deleted_at IS NULL", "substr(a.submitted_at,1,10) BETWEEN ? AND ?"]
    params: list = [scope["date_from"], scope["date_to"]]
    if scope["subject_id"]:
        clauses.append("e.subject_id=?")
        params.append(scope["subject_id"])
    where = " AND ".join(clauses)
    attempts = db.execute(
        "SELECT COUNT(*) AS submitted_attempts,"
        "SUM(CASE WHEN CAST(a.max_score AS REAL)>0 THEN 1 ELSE 0 END) AS scored_attempts,"
        "AVG(CASE WHEN CAST(a.max_score AS REAL)>0 "
        "THEN CAST(COALESCE(a.total_score,0) AS REAL)/CAST(a.max_score AS REAL) END) "
        "AS average_score_rate FROM exam_attempts a "
        "JOIN exams e ON e.id=a.exam_id WHERE " + where,
        params,
    ).fetchone()
    submitted = int(attempts["submitted_attempts"] or 0)
    if not submitted:
        return {
            "submitted_attempts": 0,
            "answered_items": 0,
            "correct_items": 0,
            "accuracy": None,
            "scored_attempts": 0,
            "average_score_rate": None,
            "chapter_error_rates": [], "attempts_with_scoped_answers": 0,
            "paper_scope_chapter_applicable": False,
        }
    from .exam_item_chapters import resolve_exam_item_chapters
    item_rows = db.execute(
        "SELECT ai.id AS attempt_item_id,ai.attempt_id,ai.result_status,"
        "ei.id AS exam_item_id FROM exam_attempt_items ai "
        "JOIN exam_attempts a ON a.id=ai.attempt_id "
        "JOIN exams e ON e.id=a.exam_id JOIN exam_items ei ON ei.id=ai.exam_item_id "
        "WHERE ai.result_status IN ('correct','incorrect') AND " + where,
        params,
    ).fetchall()
    attribution = resolve_exam_item_chapters(
        db, [row["exam_item_id"] for row in item_rows]
    )
    scoped = []
    for row in item_rows:
        chapter = attribution[int(row["exam_item_id"])]
        if scope["chapter_id"] and chapter["chapter_id"] != scope["chapter_id"]:
            continue
        scoped.append((row, chapter))
    answered = len(scoped)
    correct = sum(row["result_status"] == "correct" for row, _chapter in scoped)
    grouped: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
    for row, chapter in scoped:
        key = (int(chapter["chapter_id"] or 0), str(chapter["chapter"] or "未归类"))
        grouped[key][0] += 1
        grouped[key][1] += row["result_status"] == "incorrect"
    chapter_rates = [
        {"chapter_id": key[0], "chapter": key[1], "answered": values[0],
         "incorrect": values[1], "error_rate": values[1] / values[0]}
        for key, values in sorted(grouped.items(), key=lambda item: (-item[1][1], item[0][1]))
    ]
    return {
        "submitted_attempts": submitted,
        "answered_items": answered,
        "correct_items": correct,
        "accuracy": correct / answered if answered else None,
        "attempts_with_scoped_answers": len({int(row["attempt_id"]) for row, _chapter in scoped}),
        "paper_scope_chapter_applicable": False,
        "scored_attempts": int(attempts["scored_attempts"] or 0),
        "average_score_rate": (
            float(attempts["average_score_rate"])
            if attempts["average_score_rate"] is not None else None
        ),
        "chapter_error_rates": chapter_rates,
    }


def _insights(
    db,
    scope: dict,
    summary: dict,
    chapters: list[dict],
    review_activity: list[dict] | dict,
) -> list[dict]:
    today = datetime.now().astimezone().date().isoformat()
    clauses, params = _mistake_scope(scope)
    overdue = db.execute(
        "SELECT COUNT(*) AS n FROM mistakes m WHERE " + " AND ".join(clauses)
        + " AND m.status='未掌握' AND m.next_review IS NOT NULL AND m.next_review<?",
        params + [today],
    ).fetchone()["n"]
    insights = [{
        "rule": "overdue_reviews", "status": "triggered" if overdue else "ok",
        "title": "逾期复习", "message": f"有 {overdue} 道题已逾期" if overdue else "当前没有逾期题目",
        "metric": {"overdue_count": overdue}, "range": {"as_of": today}, "sample_size": overdue,
    }]
    weak = [
        row for row in chapters
        if row["total"] >= RULE_THRESHOLDS["weak_chapter_min_samples"]
        and row["mastery_rate"] < RULE_THRESHOLDS["weak_chapter_rate"]
    ]
    insights.append({
        "rule": "weak_chapters",
        "status": "triggered" if weak else ("insufficient" if not any(row["total"] >= RULE_THRESHOLDS["weak_chapter_min_samples"] for row in chapters) else "ok"),
        "title": "当前低掌握率章节",
        "message": (f"{len(weak)} 个章节掌握率低于 {RULE_THRESHOLDS['weak_chapter_rate']:.0%}" if weak else "当前样本未显示低掌握率章节"),
        "metric": {"chapter_count": len(weak), "threshold": RULE_THRESHOLDS["weak_chapter_rate"]},
        "range": {"as_of": today}, "sample_size": sum(row["total"] for row in chapters),
    })
    midpoint = max(1, scope["days"] // 2)
    split = (date.fromisoformat(scope["date_to"]) - timedelta(days=midpoint - 1)).isoformat()
    if isinstance(review_activity, dict):
        split = str(review_activity["split"])
        prior_count = int(review_activity["prior_pass"]) + int(review_activity["prior_again"])
        recent_count = int(review_activity["recent_pass"]) + int(review_activity["recent_again"])
        prior_rate = (
            int(review_activity["prior_again"]) / prior_count if prior_count else None
        )
        recent_rate = (
            int(review_activity["recent_again"]) / recent_count if recent_count else None
        )
    else:
        normal = [
            row for row in review_activity if row["event_type"] in ("pass", "again")
        ]
        prior = [row for row in normal if row["reviewed_at"][:10] < split]
        recent = [row for row in normal if row["reviewed_at"][:10] >= split]
        prior_count, recent_count = len(prior), len(recent)
        prior_rate = (
            sum(row["event_type"] == "again" for row in prior) / prior_count
            if prior_count else None
        )
        recent_rate = (
            sum(row["event_type"] == "again" for row in recent) / recent_count
            if recent_count else None
        )
    enough = prior_count >= RULE_THRESHOLDS["again_rise_min_samples"] and recent_count >= RULE_THRESHOLDS["again_rise_min_samples"]
    rising = enough and recent_rate is not None and prior_rate is not None and recent_rate - prior_rate >= RULE_THRESHOLDS["again_rise_points"]
    insights.append({
        "rule": "again_rate_rising", "status": "triggered" if rising else ("ok" if enough else "insufficient"),
        "title": "再次答错比例", "message": "近期再次答错比例明显上升" if rising else ("样本不足以判断变化" if not enough else "近期再次答错比例稳定"),
        "metric": {"prior_rate": prior_rate, "recent_rate": recent_rate, "rise_threshold": RULE_THRESHOLDS["again_rise_points"]},
        "range": {"date_from": scope["date_from"], "date_to": scope["date_to"], "recent_from": split},
        "sample_size": prior_count + recent_count,
    })
    stale_cutoff = (datetime.now().astimezone().date() - timedelta(
        days=RULE_THRESHOLDS["stale_subject_days"]
    )).isoformat()
    stale_rows = db.execute(
        "SELECT s.id,s.name,COUNT(m.id) AS active_count,"
        "MAX(COALESCE(m.last_review,m.date_added)) AS last_activity "
        "FROM mistakes m JOIN subjects s ON s.id=m.subject_id WHERE "
        + " AND ".join(clauses)
        + " AND m.status='未掌握' GROUP BY s.id,s.name",
        params,
    ).fetchall()
    eligible = [row for row in stale_rows if int(row["active_count"] or 0) >= RULE_THRESHOLDS["stale_subject_min_active"]]
    stale = [row for row in eligible if not row["last_activity"] or str(row["last_activity"])[:10] < stale_cutoff]
    insights.append({
        "rule": "stale_subjects",
        "status": "triggered" if stale else ("ok" if eligible else "insufficient"),
        "title": "长期未做间隔复习的科目",
        "message": (f"{len(stale)} 个科目超过 {RULE_THRESHOLDS['stale_subject_days']} 天未做间隔复习" if stale else ("活跃题目样本不足以判断" if not eligible else "各科均有近期录入或间隔复习记录")),
        "metric": {"subject_count": len(stale), "days": RULE_THRESHOLDS["stale_subject_days"]},
        "range": {"as_of": today, "stale_before": stale_cutoff},
        "sample_size": sum(int(row["active_count"] or 0) for row in stale_rows),
    })
    return insights


def _build_statistics(db, scope: dict, review_builder, exam_builder) -> dict:
    # The request path owns no transaction.  A caller that does must retain its
    # commit boundary rather than having a statistics read commit its writes.
    if not db.in_transaction:
        ensure_daily_snapshot(db)
    metadata_row = db.execute("SELECT complete_since,enabled_at FROM statistics_metadata WHERE id=1").fetchone()
    metadata = dict(metadata_row) if metadata_row else {
        "complete_since": None,
        "enabled_at": None,
    }
    clauses, params = _mistake_scope(scope)
    current = db.execute(
        "SELECT COUNT(*) AS total,SUM(CASE WHEN m.status='已掌握' THEN 1 ELSE 0 END) "
        "AS mastered,SUM(CASE WHEN m.status='未掌握' THEN 1 ELSE 0 END) AS active "
        "FROM mistakes m WHERE " + " AND ".join(clauses), params,
    ).fetchone()
    total, mastered, active = (int(current[key] or 0) for key in ("total", "mastered", "active"))
    chapter_rows = db.execute(
        "SELECT c.id AS chapter_id,c.name AS chapter,s.id AS subject_id,s.name AS subject,"
        "COUNT(m.id) AS total,SUM(CASE WHEN m.status='已掌握' THEN 1 ELSE 0 END) AS mastered "
        "FROM mistakes m JOIN chapters c ON c.id=m.chapter_id JOIN subjects s ON s.id=m.subject_id "
        "WHERE " + " AND ".join(clauses) + " GROUP BY c.id,c.name,s.id,s.name ORDER BY c.display_order,c.id",
        params,
    ).fetchall()
    chapters = [
        {"chapter_id": row["chapter_id"], "chapter": row["chapter"], "subject_id": row["subject_id"],
         "subject": row["subject"], "total": int(row["total"]), "mastered": int(row["mastered"] or 0),
         "active": int(row["total"] - (row["mastered"] or 0)),
         "mastery_rate": float(row["mastered"] or 0) / int(row["total"])}
        for row in chapter_rows
    ]
    review, trend, heatmap, review_activity = review_builder(
        db, scope, metadata["complete_since"]
    )
    assisted = db.execute("SELECT substr(e.reviewed_at,1,10) AS day,COUNT(*) AS n FROM review_events e JOIN mistakes m ON m.id=e.mistake_id WHERE " + " AND ".join(clauses) + " AND e.event_type='assisted' AND substr(e.reviewed_at,1,10) BETWEEN ? AND ? GROUP BY day", params + [scope['date_from'], scope['date_to']]).fetchall()
    review['assisted_count'] = sum(row['n'] for row in assisted)
    review['practice_count'] = review['known_review_events'] + review['assisted_count']
    trend_by_period = {row['period']: row for row in trend}
    heat_by_day = {row['day']: row for row in heatmap}
    for row in assisted:
        period = _bucket(row['day'], scope['granularity'])
        entry = trend_by_period.setdefault(period, {'period':period, 'reviews':0, 'pass':0, 'again':0, 'first_mastered':0, 'manual_changes':0})
        entry['assisted'] = entry.get('assisted', 0) + row['n']
        entry['reviews'] += row['n']
        heat = heat_by_day.setdefault(row['day'], {'day':row['day'], 'reviews':0, 'pass':0, 'again':0})
        heat['assisted'] = row['n']
        heat['reviews'] += row['n']
    trend = sorted(trend_by_period.values(), key=lambda row: row['period'])
    heatmap = sorted(heat_by_day.values(), key=lambda row: row['day'])
    stage_rows = db.execute(
        "SELECT review_stage,COUNT(*) AS count FROM mistakes m WHERE "
        + " AND ".join(clauses)
        + " AND m.status='未掌握' GROUP BY review_stage ORDER BY review_stage",
        params,
    ).fetchall()
    stage_distribution = {str(index): 0 for index in range(4)}
    for row in stage_rows:
        stage_distribution[str(int(row["review_stage"] or 0))] = int(row["count"] or 0)
    exam = exam_builder(db, scope)
    reasons = db.execute(
        "SELECT r.reason_code AS reason,COUNT(*) AS count FROM mistake_reason_items r "
        "JOIN mistakes m ON m.id=r.mistake_id WHERE " + " AND ".join(clauses)
        + " AND m.date_added BETWEEN ? AND ? GROUP BY r.reason_code ORDER BY count DESC,r.reason_code",
        params + [scope["date_from"], scope["date_to"]],
    ).fetchall()
    recent_chapters = db.execute(
        "SELECT c.id AS chapter_id,c.name AS chapter,s.name AS subject,"
        "MAX(e.reviewed_at) AS last_reviewed_at,COUNT(*) AS review_count "
        "FROM review_events e JOIN mistakes m ON m.id=e.mistake_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id LEFT JOIN subjects s ON s.id=m.subject_id "
        "WHERE " + " AND ".join(clauses)
        + " AND substr(e.reviewed_at,1,10) BETWEEN ? AND ? "
        "GROUP BY c.id,c.name,s.name ORDER BY last_reviewed_at DESC LIMIT 8",
        params + [scope["date_from"], scope["date_to"]],
    ).fetchall()
    for row in trend:
        row["estimated"] = (
            not metadata["complete_since"]
            or row["period"] < metadata["complete_since"]
        )
    snapshot_where = ["snapshot_day BETWEEN ? AND ?"]
    snapshot_params: list = [scope["date_from"], scope["date_to"]]
    if scope["subject_id"]:
        snapshot_where.append("subject_id=?")
        snapshot_params.append(scope["subject_id"])
    else:
        snapshot_where.append("subject_id=0")
    if scope["chapter_id"]:
        snapshot_where.append("chapter_id=?")
        snapshot_params.append(scope["chapter_id"])
    else:
        snapshot_where.append("chapter_id=0")
    snapshots = db.execute(
        "SELECT snapshot_day,total_count,mastered_count,active_count,captured_at,"
        "estimated FROM learning_daily_snapshots WHERE "
        + " AND ".join(snapshot_where) + " ORDER BY snapshot_day",
        snapshot_params,
    ).fetchall()
    summary = {
        "total": total, "mastered": mastered, "active": active,
        "current_mastery_rate": mastered / total if total else None,
        **review,
    }
    completeness = {
        "statistics_complete_since": metadata["complete_since"],
        "enabled_at": metadata["enabled_at"],
        "range_is_complete": bool(
            metadata["complete_since"]
            and scope["date_from"] >= metadata["complete_since"]
        ),
        "historical_portion_estimated": bool(
            not metadata["complete_since"]
            or scope["date_from"] < metadata["complete_since"]
        ),
        "samples": {
            "current_mistakes": total,
            "known_review_events": review["known_review_events"],
            "complete_review_events": review["review_sample"],
            "normal_mastery_sequences": review["mastery_sample"],
            "submitted_exam_attempts": exam["submitted_attempts"],
            "answered_exam_items": exam["answered_items"],
        },
    }
    from .learning_evidence import learning_actions
    return {
        "scope": scope,
        "learning_actions": learning_actions(db, scope),
        "summary": summary,
        "review_cycle": {
            "schedule_version": SCHEDULE_VERSION,
            "stage_distribution": stage_distribution,
            "reset_events": review["cycle_reset_events"],
            "reset_mistakes": review["cycle_reset_mistakes"],
            "normal_mastered": review["normal_cycle_mastered"],
        },
        "chapter_rates": chapters,
        "review_trend": trend,
        "reason_breakdown": [dict(row) for row in reasons],
        "heatmap": heatmap,
        "daily_snapshots": [dict(row) for row in snapshots],
        "exam_attempts": exam,
        "recent_chapters": [dict(row) for row in recent_chapters],
        "completeness": completeness,
        "insights": _insights(db, scope, summary, chapters, review_activity),
        "formula_version": SCHEDULE_VERSION,
    }


def build_statistics(db, scope: dict) -> dict:
    return _build_statistics(db, scope, _review_metrics, _exam_metrics)


def build_statistics_legacy(db, scope: dict) -> dict:
    """Retained P4 fallback and deep-equivalence reference implementation."""

    return _build_statistics(
        db, scope, _review_metrics_legacy, _exam_metrics_legacy
    )


def build_reminders(db, scope: dict) -> dict:
    today = datetime.now().astimezone().date().isoformat()
    clauses, params = _mistake_scope(scope)
    due = db.execute(
        "SELECT COUNT(*) AS due_count,SUM(CASE WHEN next_review<? THEN 1 ELSE 0 END) "
        "AS overdue_count FROM mistakes m WHERE " + " AND ".join(clauses)
        + " AND m.status='未掌握' AND (m.next_review IS NULL OR m.next_review<=?)",
        [today, *params, today],
    ).fetchone()
    count = int(due["due_count"] or 0)
    return {
        "as_of": today,
        "due_count": count,
        "overdue_count": int(due["overdue_count"] or 0),
        "suggested_reminder_time": "19:30" if count else None,
        "delivery": "in_app_only",
        "message": f"今天有 {count} 道题待复习" if count else "今天没有待复习题目",
    }
from .review_schedule import SCHEDULE_VERSION
