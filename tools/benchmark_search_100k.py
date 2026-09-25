"""Reproducible 100k-row search benchmark; creates only a temporary database."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import tempfile
import time
from pathlib import Path


def timed(connection, sql, params):
    started = time.perf_counter()
    rows = connection.execute(sql, params).fetchall()
    return (time.perf_counter() - started) * 1000, len(rows)


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * quantile) - 1)]


def benchmark(iterations: int = 30) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "search-100k.sqlite3"
        db = sqlite3.connect(path)
        db.executescript(
            "CREATE TABLE mistakes("
            "id INTEGER PRIMARY KEY,qnum TEXT,note TEXT,content TEXT,"
            "deleted_at TEXT,updated_at TEXT);"
            "CREATE INDEX idx_mistakes_active_updated "
            "ON mistakes(deleted_at,updated_at,id);"
            "CREATE INDEX idx_mistakes_active_id "
            "ON mistakes(deleted_at,id DESC);"
            "CREATE VIRTUAL TABLE mistakes_fts USING fts5("
            "qnum,note,content,content='mistakes',content_rowid='id',tokenize='trigram');"
        )
        db.executemany(
            "INSERT INTO mistakes(qnum,note,content,deleted_at,updated_at) "
            "VALUES(?,?,?,NULL,?)",
            (
                (
                    str(index),
                    "极限定理重点" if index % 997 == 0 else "普通备注",
                    f"题目正文 {index} 数学分析",
                    f"2026-08-{index % 28 + 1:02d}T12:00:00",
                )
                for index in range(100_000)
            ),
        )
        db.execute("INSERT INTO mistakes_fts(mistakes_fts) VALUES('rebuild')")
        db.commit()
        like_sql = (
            "SELECT id FROM mistakes WHERE qnum LIKE ? OR note LIKE ? OR content LIKE ? "
            "ORDER BY CASE WHEN qnum=? THEN 0 WHEN qnum LIKE ? THEN 1 "
            "WHEN note LIKE ? THEN 2 ELSE 3 END,id DESC LIMIT 30"
        )
        like_params = (
            "%极限定理%", "%极限定理%", "%极限定理%", "极限定理",
            "极限定理%", "%极限定理%",
        )
        fts_sql = (
            "SELECT m.id FROM mistakes m WHERE m.id IN ("
            "SELECT rowid FROM mistakes_fts WHERE mistakes_fts MATCH ?) "
            "AND (m.qnum LIKE ? OR m.note LIKE ? OR m.content LIKE ?) "
            "ORDER BY CASE WHEN m.qnum=? THEN 0 WHEN m.qnum LIKE ? THEN 1 "
            "WHEN m.note LIKE ? THEN 2 ELSE 3 END,m.id DESC LIMIT 30"
        )
        fts_params = (
            '"极限定理"', "%极限定理%", "%极限定理%", "%极限定理%",
            "极限定理", "极限定理%", "%极限定理%",
        )
        list_sql = (
            "SELECT id FROM mistakes WHERE deleted_at IS NULL "
            "AND id<=? AND id<? ORDER BY id DESC LIMIT ?"
        )
        list_previous_sql = (
            "SELECT id FROM mistakes INDEXED BY idx_mistakes_active_updated "
            "WHERE deleted_at IS NULL AND id<=? AND id<? "
            "ORDER BY id DESC LIMIT ?"
        )
        list_params = (100_000, 100_001, 31)
        for _ in range(3):
            timed(db, like_sql, like_params)
            timed(db, fts_sql, fts_params)
            timed(db, list_previous_sql, list_params)
            timed(db, list_sql, list_params)
        like_samples = []
        fts_samples = []
        list_previous_samples = []
        list_samples = []
        like_rows = fts_rows = list_previous_rows = list_rows = 0
        for _ in range(iterations):
            like_ms, like_rows = timed(db, like_sql, like_params)
            fts_ms, fts_rows = timed(db, fts_sql, fts_params)
            list_previous_ms, list_previous_rows = timed(
                db, list_previous_sql, list_params
            )
            list_ms, list_rows = timed(db, list_sql, list_params)
            like_samples.append(like_ms)
            fts_samples.append(fts_ms)
            list_previous_samples.append(list_previous_ms)
            list_samples.append(list_ms)
        result = {
            "rows": 100_000,
            "iterations": iterations,
            "like": {
                "p50_ms": round(percentile(like_samples, 0.50), 3),
                "p95_ms": round(percentile(like_samples, 0.95), 3),
                "results": like_rows,
                "plan": [
                    row[3]
                    for row in db.execute(
                        "EXPLAIN QUERY PLAN " + like_sql, like_params
                    ).fetchall()
                ],
            },
            "fts": {
                "p50_ms": round(percentile(fts_samples, 0.50), 3),
                "p95_ms": round(percentile(fts_samples, 0.95), 3),
                "results": fts_rows,
                "plan": [
                    row[3]
                    for row in db.execute(
                        "EXPLAIN QUERY PLAN " + fts_sql, fts_params
                    ).fetchall()
                ],
            },
            "list_previous_index": {
                "p50_ms": round(percentile(list_previous_samples, 0.50), 3),
                "p95_ms": round(percentile(list_previous_samples, 0.95), 3),
                "results": list_previous_rows,
                "plan": [
                    row[3]
                    for row in db.execute(
                        "EXPLAIN QUERY PLAN " + list_previous_sql, list_params
                    ).fetchall()
                ],
            },
            "list": {
                "p50_ms": round(percentile(list_samples, 0.50), 3),
                "p95_ms": round(percentile(list_samples, 0.95), 3),
                "results": list_rows,
                "plan": [
                    row[3]
                    for row in db.execute(
                        "EXPLAIN QUERY PLAN " + list_sql, list_params
                    ).fetchall()
                ],
            },
        }
        db.close()
        return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.iterations < 3 or args.iterations > 200:
        parser.error("iterations must be between 3 and 200")
    result = benchmark(args.iterations)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return
    print(f"rows={result['rows']} iterations={result['iterations']}")
    for name in ("like", "fts", "list_previous_index", "list"):
        item = result[name]
        print(
            f"{name}_p50_ms={item['p50_ms']:.3f} "
            f"{name}_p95_ms={item['p95_ms']:.3f} results={item['results']}"
        )
        print(f"{name}_plan={' | '.join(item['plan'])}")


if __name__ == "__main__":
    main()
