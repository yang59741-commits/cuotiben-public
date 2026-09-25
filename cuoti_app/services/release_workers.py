"""Background workers that preserve formal-release job history."""

from __future__ import annotations

import threading
from typing import Any

from .jobs import JobCoordinator


class ReleaseJobCoordinator(JobCoordinator):
    """Keep historical terminal jobs intact in the formal runtime."""

    def cleanup_retention(self, *args: Any, **kwargs: Any) -> dict[str, int]:
        del args, kwargs
        return {"jobs": 0, "inputs": 0}


def run_release_worker(
    database: str,
    stop_event: threading.Event,
    *,
    lane: str,
    staged_input_root: str,
    aging_seconds: int,
) -> None:
    coordinator = ReleaseJobCoordinator(
        database,
        lane=lane,
        staged_input_roots=(staged_input_root,),
        aging_seconds=aging_seconds,
    )
    coordinator.run_forever(stop_event, poll_interval=0.25)


def start_release_workers(app: Any) -> dict[str, Any]:
    """Start normal job lanes with retention disabled for formal history."""

    stop_event = threading.Event()
    threads: list[threading.Thread] = []
    for lane, count in (
        ("fast", int(app.config.get("JOB_FAST_WORKERS", 1))),
        ("heavy", int(app.config.get("JOB_HEAVY_WORKERS", 1))),
    ):
        for index in range(count):
            thread = threading.Thread(
                target=run_release_worker,
                kwargs={
                    "database": app.config["DATABASE"],
                    "stop_event": stop_event,
                    "lane": lane,
                    "staged_input_root": app.config["JOB_INPUT_DIR"],
                    "aging_seconds": int(app.config.get("JOB_AGING_SECONDS", 300)),
                },
                name=f"cuoti-release-job-{lane}-{index + 1}",
                daemon=True,
            )
            thread.start()
            threads.append(thread)
    handle = {"threads": threads, "thread": threads[0], "stop_event": stop_event}
    app.extensions["job_worker"] = handle
    return handle


__all__ = [
    "ReleaseJobCoordinator",
    "run_release_worker",
    "start_release_workers",
]
