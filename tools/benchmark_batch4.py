"""Repeatable micro-benchmarks for batch-four query and memory hot paths."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cuoti_app.services.ai_memory import AIMemory


def statistics_scan(events: int) -> tuple[float, float, int, int]:
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE mistakes(id INTEGER PRIMARY KEY,history_complete INTEGER,history_complete_from TEXT)")
    db.execute("CREATE TABLE review_events(id INTEGER PRIMARY KEY,mistake_id INTEGER,event_type TEXT,reviewed_at TEXT,schedule_version TEXT,stage_before INTEGER,stage_after INTEGER)")
    db.execute("CREATE INDEX review_events_date ON review_events(reviewed_at)")
    db.execute("INSERT INTO mistakes VALUES(1,1,'2025-01-01')")
    start = date(2020, 1, 1)
    db.executemany(
        "INSERT INTO review_events(mistake_id,event_type,reviewed_at,schedule_version,stage_before,stage_after) VALUES(1,'pass',?,'fixed-3-5-10-15-v1',0,1)",
        [((start + timedelta(days=index % 2200)).isoformat(),) for index in range(events)],
    )
    db.commit()
    legacy_sql = "SELECT * FROM review_events WHERE schedule_version=? AND stage_before IS NOT NULL ORDER BY mistake_id,reviewed_at,id"
    bounded_sql = "SELECT * FROM review_events WHERE schedule_version=? AND stage_before IS NOT NULL AND substr(reviewed_at,1,10)>=? ORDER BY mistake_id,reviewed_at,id"
    before = time.perf_counter(); legacy = db.execute(legacy_sql, ("fixed-3-5-10-15-v1",)).fetchall(); legacy_seconds = time.perf_counter() - before
    before = time.perf_counter(); bounded = db.execute(bounded_sql, ("fixed-3-5-10-15-v1", "2025-01-01")).fetchall(); bounded_seconds = time.perf_counter() - before
    db.close()
    return legacy_seconds, bounded_seconds, len(legacy), len(bounded)


def memory_append(count: int, *, batch: bool = False) -> float:
    with tempfile.TemporaryDirectory() as folder:
        memory = AIMemory(Path(folder))
        events = [
            {
                "event_id": f"bench-{index}",
                "event_type": "result",
                "subject": "数学分析",
            }
            for index in range(count)
        ]
        before = time.perf_counter()
        if batch:
            memory.append_events("free", "free_exam", events, day="2026-08-08")
        else:
            for event in events:
                memory.append_event("free", "free_exam", event, day="2026-08-08")
        return time.perf_counter() - before


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def benchmark(events: int, memory_events: int, iterations: int) -> dict:
    statistics_scan(min(events, 10_000))
    memory_append(min(memory_events, 100))
    memory_append(min(memory_events, 100), batch=True)
    legacy_samples = []
    bounded_samples = []
    append_samples = []
    batch_append_samples = []
    legacy_rows = bounded_rows = 0
    for _ in range(iterations):
        legacy, bounded, legacy_rows, bounded_rows = statistics_scan(events)
        legacy_samples.append(legacy * 1000)
        bounded_samples.append(bounded * 1000)
        append_samples.append(memory_append(memory_events) * 1000)
        batch_append_samples.append(memory_append(memory_events, batch=True) * 1000)
    return {
        "events": events,
        "memory_events": memory_events,
        "iterations": iterations,
        "statistics": {
            "legacy_rows": legacy_rows,
            "bounded_rows": bounded_rows,
            "legacy_p50_ms": round(percentile(legacy_samples, 0.50), 3),
            "legacy_p95_ms": round(percentile(legacy_samples, 0.95), 3),
            "bounded_p50_ms": round(percentile(bounded_samples, 0.50), 3),
            "bounded_p95_ms": round(percentile(bounded_samples, 0.95), 3),
        },
        "memory_append": {
            "p50_ms": round(percentile(append_samples, 0.50), 3),
            "p95_ms": round(percentile(append_samples, 0.95), 3),
        },
        "memory_append_batch": {
            "p50_ms": round(percentile(batch_append_samples, 0.50), 3),
            "p95_ms": round(percentile(batch_append_samples, 0.95), 3),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--memory-events", type=int, default=1_000)
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.iterations < 3 or args.iterations > 30:
        parser.error("iterations must be between 3 and 30")
    result = benchmark(args.events, args.memory_events, args.iterations)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    statistics = result["statistics"]
    memory = result["memory_append"]
    memory_batch = result["memory_append_batch"]
    print(
        f"statistics legacy_p50_ms={statistics['legacy_p50_ms']:.3f} "
        f"legacy_p95_ms={statistics['legacy_p95_ms']:.3f} "
        f"rows={statistics['legacy_rows']}"
    )
    print(
        f"statistics bounded_p50_ms={statistics['bounded_p50_ms']:.3f} "
        f"bounded_p95_ms={statistics['bounded_p95_ms']:.3f} "
        f"rows={statistics['bounded_rows']}"
    )
    print(
        f"memory append {result['memory_events']} events "
        f"p50_ms={memory['p50_ms']:.3f} p95_ms={memory['p95_ms']:.3f}"
    )
    print(
        f"memory batch {result['memory_events']} events "
        f"p50_ms={memory_batch['p50_ms']:.3f} "
        f"p95_ms={memory_batch['p95_ms']:.3f}"
    )


if __name__ == "__main__":
    main()
