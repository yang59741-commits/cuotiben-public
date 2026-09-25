"""Benchmark canonical statistics over deterministic synthetic review events."""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.migrations.v32_statistics_read_model import migrate_v31_to_v32
from cuoti_app.services.statistics import (
    build_statistics,
    build_statistics_legacy,
    parse_scope,
)
from cuoti_app.services.statistics_read_model import (
    activate_statistics_read_model,
    backfill_statistics_read_model,
)


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _stable_contract(value: dict) -> dict:
    result = deepcopy(value)
    for row in result.get("daily_snapshots", []):
        if "captured_at" in row:
            row["captured_at"] = "<captured-at>"
    return result


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def _populate(db: sqlite3.Connection, events: int, mistakes: int, today) -> None:
    subject_id = int(db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()[0])
    chapter_id = int(
        db.execute(
            "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
            (subject_id,),
        ).fetchone()[0]
    )
    complete_since = (today - timedelta(days=729)).isoformat()
    db.executemany(
        "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added,"
        "status,history_complete,history_complete_from,updated_at) "
        "VALUES(?,?,?,?,?,?,'未掌握',1,?,?)",
        (
            (
                f"benchmark-mistake-{index}", subject_id, chapter_id, str(index),
                "synthetic", complete_since, complete_since, complete_since,
            )
            for index in range(mistakes)
        ),
    )
    mistake_ids = [
        int(row[0])
        for row in db.execute(
            "SELECT id FROM mistakes WHERE public_id LIKE 'benchmark-mistake-%' ORDER BY id"
        ).fetchall()
    ]
    schedule = "fixed-3-5-10-15-v1"
    db.executemany(
        "INSERT INTO review_events(mistake_id,event_type,reviewed_at,status_after,"
        "correct_streak,created_at,lapse_count_after,stage_before,stage_after,"
        "cycle_after,schedule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            (
                mistake_ids[index % mistakes],
                "again" if index % 5 == 0 else "pass",
                (
                    datetime.combine(
                        today - timedelta(days=index % 700), datetime.min.time()
                    ).astimezone().replace(hour=index % 24)
                ).isoformat(timespec="seconds"),
                "已掌握" if index % 20 == 19 else "未掌握",
                index % 4,
                complete_since,
                index % 7,
                index % 4,
                0 if index % 5 == 0 else min(3, index % 4 + 1),
                index // max(1, mistakes * 4) + 1,
                schedule,
            )
            for index in range(events)
        ),
    )
    db.execute(
        "UPDATE statistics_metadata SET complete_since=?,enabled_at=? WHERE id=1",
        (complete_since, complete_since),
    )
    db.commit()


def _write_overhead(events: int, today) -> dict:
    def connection(with_model: bool) -> sqlite3.Connection:
        db = sqlite3.connect(":memory:")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript(
            "CREATE TABLE mistakes(id INTEGER PRIMARY KEY);"
            "CREATE TABLE review_events("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,mistake_id INTEGER NOT NULL,"
            "event_type TEXT NOT NULL,reviewed_at TEXT NOT NULL,status_after TEXT NOT NULL,"
            "stage_before INTEGER,stage_after INTEGER,cycle_after INTEGER,"
            "schedule_version TEXT,FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE);"
            "CREATE INDEX idx_benchmark_review_mistake_date "
            "ON review_events(mistake_id,reviewed_at);"
        )
        db.executemany("INSERT INTO mistakes(id) VALUES(?)", ((i,) for i in range(1, 101)))
        if with_model:
            migrate_v31_to_v32(db)
        db.commit()
        return db

    rows = [
        (
            index % 100 + 1,
            "again" if index % 5 == 0 else "pass",
            (
                datetime.combine(
                    today - timedelta(days=index % 700), datetime.min.time()
                ).astimezone().replace(hour=index % 24)
            ).isoformat(timespec="seconds"),
            "已掌握" if index % 20 == 19 else "未掌握",
            index % 4,
            0 if index % 5 == 0 else min(3, index % 4 + 1),
            index // 400 + 1,
            "fixed-3-5-10-15-v1",
        )
        for index in range(events)
    ]
    sql = (
        "INSERT INTO review_events(mistake_id,event_type,reviewed_at,status_after,"
        "stage_before,stage_after,cycle_after,schedule_version) VALUES(?,?,?,?,?,?,?,?)"
    )
    elapsed = {}
    for label, with_model in (("baseline", False), ("read_model", True)):
        db = connection(with_model)
        started = time.perf_counter()
        db.executemany(sql, rows)
        db.commit()
        elapsed[label] = (time.perf_counter() - started) * 1000
        db.close()
    return {
        "events": events,
        "baseline_ms": round(elapsed["baseline"], 3),
        "read_model_ms": round(elapsed["read_model"], 3),
        "amplification_ratio": round(elapsed["read_model"] / elapsed["baseline"], 6),
    }


def benchmark(events: int = 100_000, iterations: int = 7) -> dict:
    if events < 1 or iterations < 1:
        raise ValueError("events and iterations must be positive")
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(root / "statistics.sqlite3"),
                "PDF_DIR": str(root / "pdfs"),
                "ATTACHMENT_DIR": str(root / "attachments"),
                "SETTINGS_PATH": str(root / "settings.json"),
                "TEX_BASE": "",
                "PAST_EXAM_DIR": "",
                "DEEPSEEK_API_KEY": "",
                "JOB_WORKER_ENABLED": False,
                "REGISTER_JOB_HANDLERS": False,
            }
        )
        today = datetime.now().astimezone().date()
        scope = parse_scope(
            {
                "date_from": (today - timedelta(days=729)).isoformat(),
                "date_to": today.isoformat(),
            },
            today=today,
        )
        with app.app_context():
            db = get_db()
            _populate(db, events, min(500, max(10, events // 500)), today)
            backfill_started = time.perf_counter()
            backfill_statistics_read_model(db, batch_size=500)
            activate_statistics_read_model(db)
            backfill_ms = (time.perf_counter() - backfill_started) * 1000
            optimized = build_statistics(db, scope)
            legacy = build_statistics_legacy(db, scope)
            if optimized != legacy:
                raise RuntimeError("optimized statistics differ from the legacy contract")
            optimized_samples: list[float] = []
            legacy_samples: list[float] = []
            payload = optimized
            for _ in range(iterations):
                started = time.perf_counter()
                payload = build_statistics(db, scope)
                optimized_samples.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                legacy_payload = build_statistics_legacy(db, scope)
                legacy_samples.append((time.perf_counter() - started) * 1000)
                if legacy_payload != payload:
                    raise RuntimeError("statistics outputs diverged during benchmark")
            tracemalloc.start()
            measured = build_statistics(db, scope)
            _current, optimized_peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            tracemalloc.start()
            legacy_measured = build_statistics_legacy(db, scope)
            _current, legacy_peak_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if measured != payload or legacy_measured != payload:
                raise RuntimeError("statistics output changed between identical reads")
            legacy_plan = [
                str(row[3])
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT e.id FROM review_events e "
                    "JOIN mistakes m ON m.id=e.mistake_id "
                    "WHERE m.deleted_at IS NULL "
                    "AND substr(e.reviewed_at,1,10) BETWEEN ? AND ? "
                    "ORDER BY e.reviewed_at,e.id",
                    (scope["date_from"], scope["date_to"]),
                ).fetchall()
            ]
            model_plan = [
                str(row[3])
                for row in db.execute(
                    "EXPLAIN QUERY PLAN SELECT d.review_day,SUM(d.review_count) "
                    "FROM statistics_review_daily d INDEXED BY idx_statistics_review_daily_day "
                    "CROSS JOIN mistakes m ON m.id=d.mistake_id "
                    "WHERE m.deleted_at IS NULL AND d.review_day BETWEEN ? AND ? "
                    "GROUP BY d.review_day ORDER BY d.review_day",
                    (scope["date_from"], scope["date_to"]),
                ).fetchall()
            ]
            write_overhead = _write_overhead(min(events, 10_000), today)
        return {
            "events": events,
            "iterations": iterations,
            "p50_ms": round(_percentile(optimized_samples, 0.50), 3),
            "p95_ms": round(_percentile(optimized_samples, 0.95), 3),
            "peak_bytes": optimized_peak_bytes,
            "legacy": {
                "p50_ms": round(_percentile(legacy_samples, 0.50), 3),
                "p95_ms": round(_percentile(legacy_samples, 0.95), 3),
                "peak_bytes": legacy_peak_bytes,
            },
            "p95_reduction_ratio": round(
                1 - _percentile(optimized_samples, 0.95)
                / _percentile(legacy_samples, 0.95),
                6,
            ),
            "peak_reduction_ratio": round(
                1 - optimized_peak_bytes / legacy_peak_bytes, 6
            ),
            "backfill_ms": round(backfill_ms, 3),
            "canonical_json_sha256": _digest(_stable_contract(payload)),
            "summary_sha256": _digest(payload["summary"]),
            "query_plan": model_plan,
            "legacy_query_plan": legacy_plan,
            "write_overhead": write_overhead,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if not 3 <= args.iterations <= 30:
        parser.error("iterations must be between 3 and 30")
    result = benchmark(args.events, args.iterations)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
