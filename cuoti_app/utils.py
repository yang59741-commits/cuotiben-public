"""Small shared utilities with no Flask or database side effects."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


QUESTION_SUBSECTION_PATTERN = re.compile(
    r"\\subsection\*?\{\s*题目\s*(?P<number>\d+)\s*\}"
)
QUESTION_SUBSECTION_SPLIT_PATTERN = re.compile(
    r"\\subsection\*?\{\s*题目\s*\d+\s*\}"
)


def file_sha256(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Return the SHA-256 digest for a file without loading it into memory."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def subsection_numbers(text: str) -> list[int]:
    """Return numbered ``\\subsection{题目N}`` headings in source order."""
    return [int(match.group("number")) for match in QUESTION_SUBSECTION_PATTERN.finditer(text)]
