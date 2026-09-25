"""Read and fingerprint explicitly allowlisted TeX question-bank sources."""

from __future__ import annotations

import hashlib
import json
import random
import re
from pathlib import Path
from typing import Any

from ..utils import QUESTION_SUBSECTION_SPLIT_PATTERN, file_sha256
from .catalog import load_default_catalog
from .question_bank_contracts import PARSER_VERSION, SCHEMA_VERSION


def extract_problems_from_tex(filepath: str | Path, count: int = 3, *, seed=None):
    """Extract legacy reference-problem fragments from one TeX source."""

    try:
        content = Path(filepath).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    content = re.sub(r"(?<!\\)%.*", "", content)
    parts = QUESTION_SUBSECTION_SPLIT_PATTERN.split(content)
    if len(parts) < 2:
        parts = re.split(r"\n(?=\d+[．.])", content)
    problems = []
    for part in parts[1:]:
        part = part.strip()
        if len(part) > 30:
            problems.append(part[:600])
    if len(problems) <= count:
        return problems
    stable_seed = f"{seed or 'default'}|{Path(filepath).as_posix()}"
    return random.Random(stable_seed).sample(problems, count)


def whitelist_entries(catalog: Any = None) -> list[tuple[str, str, str]]:
    entries = []
    seen = set()
    mapping = (catalog or load_default_catalog()).chapter_sources
    for (subject, chapter), filenames in mapping.items():
        for filename in filenames:
            key = (subject, chapter, filename)
            if key in seen:
                continue
            seen.add(key)
            entries.append(key)
    return entries


def catalog_semantic_digest(catalog: Any = None) -> str:
    resolved = catalog or load_default_catalog()
    existing = getattr(resolved, "semantic_digest", None)
    if isinstance(existing, str) and existing.strip():
        return existing.strip().lower()
    mapping = getattr(resolved, "chapter_sources", {})
    payload = {
        "chapter_sources": [
            [str(subject), str(chapter), [str(name) for name in filenames]]
            for (subject, chapter), filenames in sorted(
                mapping.items(),
                key=lambda item: (str(item[0][0]), str(item[0][1])),
            )
        ],
        "exclusions": list(getattr(resolved, "exclusions", ()) or ()),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def manifest_snapshot_id(
    rows: list[dict],
    *,
    catalog_semantic_digest: str,
) -> str:
    snapshot_payload = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "catalog_semantic_digest": catalog_semantic_digest,
        "sources": rows,
    }
    return hashlib.sha256(
        json.dumps(
            snapshot_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def source_manifest(tex_base: Path, catalog: Any = None) -> tuple[list[dict], str]:
    rows = []
    for subject, chapter, filename in whitelist_entries(catalog):
        path = tex_base / filename
        if path.is_file():
            digest = file_sha256(path)
            size = path.stat().st_size
            status = "available"
        else:
            digest = ""
            size = 0
            status = "missing"
        rows.append(
            {
                "subject": subject,
                "chapter": chapter,
                "source_file": filename,
                "sha256": digest,
                "size": size,
                "status": status,
            }
        )
    rows.sort(key=lambda row: (row["subject"], row["chapter"], row["source_file"]))
    snapshot_id = manifest_snapshot_id(
        rows,
        catalog_semantic_digest=catalog_semantic_digest(catalog),
    )
    return rows, snapshot_id


__all__ = [
    "catalog_semantic_digest",
    "extract_problems_from_tex",
    "manifest_snapshot_id",
    "source_manifest",
    "whitelist_entries",
]
