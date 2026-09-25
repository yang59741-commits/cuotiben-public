"""Pure TeX parsing and exclusion rules for question-bank ingestion."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any

from ..utils import QUESTION_SUBSECTION_PATTERN
from .catalog import load_default_catalog


SUBSECTION_HEADING = QUESTION_SUBSECTION_PATTERN
NUMBERED_HEADING = re.compile(r"(?m)^[ \t]*(?P<number>\d+)[．.](?!\d)[ \t]*")
COMMENT = re.compile(r"(?<!\\)%[^\r\n]*")
CJK = re.compile(r"[\u3400-\u9fff]")

# Removing reviewed heading residue must not drop an already callable short
# theorem. Explicit source exclusions still take precedence over this exception.
_REVIEWED_SHORT_QUESTIONS = {
    ("chapterB3_18_多元函数极限.tex", 17): "证明：连续映射把紧集映为紧集．",
    ("chapterB3_17_高维空间点集.tex", 12): "5．由紧性定理证明聚点定理。",
}


@dataclass(frozen=True)
class Candidate:
    source_number: int
    source_ordinal: int
    marker: str
    start: int
    content: str


def normalized_content(content: str) -> str:
    return re.sub(r"\s+", "", content)


def content_digest(content: str) -> str:
    return hashlib.sha256(normalized_content(content).encode("utf-8")).hexdigest()


def stable_question_id(content_hash: str) -> str:
    return f"QB{content_hash[:24].upper()}"


def estimate_tokens(content: str) -> int:
    cjk_count = len(CJK.findall(content))
    non_cjk_count = max(0, len(content) - cjk_count)
    return max(1, cjk_count + math.ceil(non_cjk_count / 4))


def clean_content(content: str) -> str:
    content = re.split(r"\\end\{document\}", content, maxsplit=1)[0]
    content = COMMENT.sub("", content)
    return content.strip()


def parse_candidates(text: str) -> list[Candidate]:
    matches = list(SUBSECTION_HEADING.finditer(text))
    marker = "subsection"
    if not matches:
        matches = list(NUMBERED_HEADING.finditer(text))
        marker = "numbered_line"

    candidates = []
    for ordinal, match in enumerate(matches, start=1):
        end = matches[ordinal].start() if ordinal < len(matches) else len(text)
        content = clean_content(text[match.end():end])
        candidates.append(
            Candidate(
                source_number=int(match.group("number")),
                source_ordinal=ordinal,
                marker=marker,
                start=match.start(),
                content=content,
            )
        )
    return candidates


def find_range(
    text: str,
    start_pattern: str,
    end_pattern: str | None,
    reason: str,
) -> tuple[int, int, str] | None:
    start = re.search(start_pattern, text)
    if not start:
        return None
    if end_pattern is None:
        end_position = len(text)
    else:
        end = re.search(end_pattern, text[start.end():])
        end_position = start.end() + end.start() if end else len(text)
    return (start.start(), end_position, reason)


def catalog_exclusions(catalog: Any, kind: str, filename: str) -> list[dict]:
    catalog = catalog or load_default_catalog()
    return [
        rule
        for rule in catalog.exclusions
        if rule.get("kind") == kind and rule.get("source_file") == filename
    ]


def whole_file_exclusion(filename: str, catalog: Any = None) -> str | None:
    rules = catalog_exclusions(catalog, "whole_file", filename)
    return str(rules[0]["reason"]) if rules else None


def number_exclusion(
    filename: str,
    number: int,
    catalog: Any = None,
) -> str | None:
    rules = catalog_exclusions(catalog, "question_numbers", filename)
    if rules:
        for rule in rules:
            if number in rule["numbers"]:
                return str(rule["reason"])
    return None


def excluded_ranges(
    filename: str,
    text: str,
    catalog: Any = None,
) -> list[tuple[int, int, str]]:
    configured = catalog_exclusions(catalog, "range", filename)
    if not configured:
        return []
    return [
        item
        for item in (
            find_range(
                text,
                str(rule["start_pattern"]),
                (
                    str(rule["end_pattern"])
                    if rule.get("end_pattern") is not None
                    else None
                ),
                str(rule["reason"]),
            )
            for rule in configured
        )
        if item is not None
    ]


def candidate_exclusion_reason(
    filename: str,
    candidate: Candidate,
    ranges: list[tuple[int, int, str]],
    catalog: Any = None,
) -> str | None:
    explicit = number_exclusion(filename, candidate.source_number, catalog)
    if explicit:
        return explicit
    for start, end, reason in ranges:
        if start <= candidate.start < end:
            return reason
    if len(normalized_content(candidate.content)) < 25:
        reviewed = _REVIEWED_SHORT_QUESTIONS.get((filename, candidate.source_number))
        if reviewed is not None and normalized_content(candidate.content) == normalized_content(reviewed):
            return None
        return "excluded_too_short"
    return None


__all__ = [
    "Candidate",
    "candidate_exclusion_reason",
    "catalog_exclusions",
    "clean_content",
    "content_digest",
    "estimate_tokens",
    "excluded_ranges",
    "find_range",
    "normalized_content",
    "number_exclusion",
    "parse_candidates",
    "stable_question_id",
    "whole_file_exclusion",
]
