"""Bounded local incident files shared by the service and launcher observers."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

from .launcher_runtime import SingleInstanceGuard, rotate_log

MAX_DUMP_BYTES = 96 * 1024 * 1024
MAX_EVENT_BYTES = 512 * 1024
MAX_STACK_BYTES = 8 * 1024 * 1024
MAX_CAPTURES = 4
MAX_CAPTURE_BYTES = 384 * 1024 * 1024
CONTEXT_RESERVE_BYTES = 12 * 1024 * 1024
MAX_CHANNELS = 8


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Journal:
    def __init__(self, path: Path, *, context=None, max_bytes=None, backups=2):
        self.path = path
        self.context = context or {}
        self.max_bytes = max_bytes
        self.backups = backups
        self.lock = threading.Lock()
        self.error = None

    def event(self, event: str, **details) -> None:
        record = {"time": timestamp(), **self.context, "event": event, **details}
        try:
            with self.lock:
                maximum = MAX_EVENT_BYTES if self.max_bytes is None else self.max_bytes
                rotate_log(self.path, max_bytes=maximum, backups=self.backups)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            # Losing diagnostics must not turn a successful operation into a failure.
            self.error = type(exc).__name__


def copy_evidence(source: Path, target: Path, limit: int, *, replace=False, tail=False) -> None:
    if source.is_file() and (replace or not target.exists()):
        with source.open("rb") as incoming:
            if tail:
                size = incoming.seek(0, os.SEEK_END)
                incoming.seek(max(0, size - limit))
            data = incoming.read(limit)
        with target.open("wb" if replace else "xb") as outgoing:
            outgoing.write(data)


def capture_directories(root: Path) -> list[Path]:
    paths = [root / "first-crash"] if (root / "first-crash").is_dir() else []
    incidents = root / "incidents"
    if incidents.is_dir():
        paths.extend(sorted(path for path in incidents.iterdir() if path.is_dir()))
    return paths


def capture_usage(root: Path) -> tuple[int, int]:
    paths = capture_directories(root)
    total = 0
    for path in paths:
        size = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
        receipt = read_json(path / "capture.json")
        if receipt.get("state") == "collecting":
            size = max(size, int(receipt.get("reserved_bytes", 0)))
        total += size
    return len(paths), total


def reserve_capture(root: Path, run: dict, details: dict, *, native: bool) -> dict:
    """Reserve space before dumping so two crashing processes cannot overbook it."""
    receipt = {**run, **details, "time": timestamp(), "state": "collecting"}
    guard = SingleInstanceGuard(root / "capture.lock")
    deadline = time.monotonic() + 1
    while not guard.acquire():
        if time.monotonic() >= deadline:
            return {**receipt, "dump_status": "skipped", "reason": "capture_busy"}
        time.sleep(0.02)
    try:
        for path in capture_directories(root):
            previous = read_json(path / "capture.json")
            if previous.get("run_id") == run["run_id"]:
                if native and previous.get("dump_status") == "not_requested" and previous.get("state") == "collecting":
                    # A Python error can be followed by a native failure during
                    # process teardown. Keep both in the same unfinished incident.
                    _count, used = capture_usage(root)
                    remaining = MAX_CAPTURE_BYTES - used + previous["reserved_bytes"] - CONTEXT_RESERVE_BYTES
                    previous.update(details)
                    if remaining >= 8 * 1024 * 1024:
                        limit = min(MAX_DUMP_BYTES, remaining)
                        previous.update(dump_status="collecting", limit_bytes=limit,
                                        reserved_bytes=limit + CONTEXT_RESERVE_BYTES)
                    else:
                        previous.update(dump_status="skipped", reason="capacity_reached")
                    write_json(path / "capture.json", previous)
                return previous
        count, used = capture_usage(root)
        remaining = MAX_CAPTURE_BYTES - used - CONTEXT_RESERVE_BYTES
        if count >= MAX_CAPTURES or remaining < (8 * 1024 * 1024 if native else 0):
            return {**receipt, "dump_status": "skipped", "reason": "capacity_reached",
                    "capture_count": count, "used_bytes": used}
        destination = root / "first-crash" if count == 0 else root / "incidents" / run["run_id"]
        destination.mkdir(parents=True, exist_ok=False)
        limit = min(MAX_DUMP_BYTES, remaining) if native else 0
        receipt.update(directory=destination.relative_to(root).as_posix(),
                       dump_status="collecting" if native else "not_requested",
                       limit_bytes=limit, reserved_bytes=limit + CONTEXT_RESERVE_BYTES)
        write_json(destination / "capture.json", receipt)
        return receipt
    finally:
        guard.release()


def snapshot_context(directory: Path, root: Path, receipt: dict, *, finished=False) -> dict:
    destination = root / receipt["directory"]
    # Only the observer for this run may finish its own incident, never an older one.
    saved = read_json(destination / "capture.json")
    if saved.get("run_id") != receipt["run_id"] or saved.get("state") == "frozen":
        return saved
    failures = []
    for name, limit in (("current-run.json", 16384), ("python-current.log", MAX_STACK_BYTES),
                        ("runtime.jsonl", MAX_EVENT_BYTES + 8192),
                        ("runtime.jsonl.1", MAX_EVENT_BYTES + 8192),
                        ("monitor.jsonl", MAX_EVENT_BYTES + 8192),
                        ("operations.jsonl", 256 * 1024 + 8192),
                        ("operations.jsonl.1", 256 * 1024 + 8192)):
        try:
            copy_evidence(directory / name, destination / name, limit,
                          replace=True, tail=name != "current-run.json")
        except OSError as exc:
            failures.append({"file": name, "reason": type(exc).__name__})
    receipt = {**receipt, "context_errors": failures}
    if finished:
        receipt.update(state="frozen", finished=timestamp())
    write_json(destination / "capture.json", receipt)
    return receipt


def publish_state(directory: Path, root: Path, name: str, value: dict) -> None:
    if read_json(directory / "current-run.json").get("run_id") != value.get("run_id"):
        return
    write_json(directory / name, value)
    # Keep the service's old status entry points. A retiring observer cannot
    # replace the status of a service that has already restarted.
    if directory != root and read_json(root / "current-run.json").get("run_id") == value.get("run_id"):
        write_json(root / name, value)
