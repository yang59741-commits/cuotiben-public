"""Unified, privacy-safe performance runner over synthetic data only."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuoti_app.database import DB_SCHEMA_VERSION
from tools.benchmark_batch4 import benchmark as benchmark_batch4
from tools.benchmark_search_100k import benchmark as benchmark_search
from tools.benchmark_statistics_100k import benchmark as benchmark_statistics


RUNNER_VERSION = 1
SCENARIOS = ("search", "statistics", "statistics-memory")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if len(value) == 40 else None


def environment_summary() -> dict[str, Any]:
    cpu_count = os.cpu_count()
    return {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": int(cpu_count) if cpu_count is not None else None,
    }


def build_report(
    scenarios: list[str],
    *,
    search_iterations: int = 10,
    events: int = 100_000,
    memory_events: int = 10,
    iterations: int = 10,
) -> dict[str, Any]:
    unknown = sorted(set(scenarios) - set(SCENARIOS))
    if unknown:
        raise ValueError(f"unknown scenarios: {', '.join(unknown)}")
    inputs = {
        "scenarios": scenarios,
        "search_iterations": search_iterations,
        "events": events,
        "memory_events": memory_events,
        "iterations": iterations,
        "data_source": "synthetic",
    }
    results: dict[str, Any] = {}
    if "search" in scenarios:
        results["search"] = benchmark_search(search_iterations)
    if "statistics" in scenarios:
        results["statistics"] = benchmark_statistics(events, iterations)
    if "statistics-memory" in scenarios:
        results["statistics-memory"] = benchmark_batch4(
            events, memory_events, iterations
        )
    environment = environment_summary()
    return {
        "runner_version": RUNNER_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": _commit(),
        "schema_version": DB_SCHEMA_VERSION,
        "input_summary_hash": _digest(inputs),
        "environment": environment,
        "environment_hash": _digest(environment),
        "inputs": inputs,
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run synthetic performance scenarios and emit one JSON report."
    )
    parser.add_argument(
        "--scenario",
        action="append",
        choices=SCENARIOS,
        dest="scenarios",
        help="Scenario to run; repeat to select more than one (default: all).",
    )
    parser.add_argument("--search-iterations", type=int, default=10)
    parser.add_argument("--events", type=int, default=100_000)
    parser.add_argument("--memory-events", type=int, default=10)
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 3 <= args.search_iterations <= 200:
        parser.error("search iterations must be between 3 and 200")
    if not 3 <= args.iterations <= 30:
        parser.error("iterations must be between 3 and 30")
    if args.events < 1 or args.memory_events < 1:
        parser.error("event counts must be positive")
    report = build_report(
        list(args.scenarios or SCENARIOS),
        search_iterations=args.search_iterations,
        events=args.events,
        memory_events=args.memory_events,
        iterations=args.iterations,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = args.output.expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        os.replace(temporary, output)
    print(rendered, end="")


if __name__ == "__main__":
    main()
