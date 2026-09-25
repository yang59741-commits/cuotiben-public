"""Thread-local bridge from business services/routes to durable job progress."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator


ProgressReporter = Callable[[int, int | None, str], None]
_reporter: ContextVar[ProgressReporter | None] = ContextVar(
    "cuoti_job_progress_reporter", default=None
)


@contextmanager
def use_job_progress(reporter: ProgressReporter) -> Iterator[None]:
    token = _reporter.set(reporter)
    try:
        yield
    finally:
        _reporter.reset(token)


def report_job_progress(current: int, total: int | None, message: str) -> None:
    reporter = _reporter.get()
    if reporter is not None:
        reporter(current, total, message)
