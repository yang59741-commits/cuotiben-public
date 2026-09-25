"""Stable contracts and version constants for the question-bank index."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..paths import INSTANCE_DIR
from .error_codes import QUESTION_BANK_BUILD_FAILED, QUESTION_BANK_BUILDING


SCHEMA_VERSION = 2
PARSER_VERSION = "2026-09-06.2"
DEFAULT_INDEX_PATH = INSTANCE_DIR / "question_bank.sqlite3"


class QuestionBankError(RuntimeError):
    """Raised when the derived question-bank index cannot be used safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "QUESTION_BANK_ERROR",
        status_code: int = 503,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
        job: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(str(message))
        self.code = str(code or "QUESTION_BANK_ERROR")[:80]
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.details = dict(details) if isinstance(details, Mapping) else {}
        self.job = dict(job) if isinstance(job, Mapping) else None


def resolve_index_path(index_path: str | Path | None) -> Path:
    return Path(index_path) if index_path else DEFAULT_INDEX_PATH


__all__ = [
    "DEFAULT_INDEX_PATH",
    "PARSER_VERSION",
    "QUESTION_BANK_BUILDING",
    "QUESTION_BANK_BUILD_FAILED",
    "QuestionBankError",
    "SCHEMA_VERSION",
    "resolve_index_path",
]
