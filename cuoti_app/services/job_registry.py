"""Single validated registration boundary for all durable job handlers."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .job_contracts import SUPPORTED_JOB_KINDS
from .jobs import register_job_handler


def register_handler_map(handlers: Mapping[str, Callable]) -> None:
    observed = set(handlers)
    expected = set(SUPPORTED_JOB_KINDS)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        raise RuntimeError(
            f"job handler registry mismatch: missing={missing}, extra={extra}"
        )
    for kind, handler in handlers.items():
        register_job_handler(kind, handler, replace=True)


__all__ = ["register_handler_map"]
