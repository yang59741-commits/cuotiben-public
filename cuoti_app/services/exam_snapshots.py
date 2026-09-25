"""Deterministic, answer-free item extraction from frozen exam bodies."""

from __future__ import annotations

import re


class ExamSnapshotError(ValueError):
    """The frozen exam body cannot produce trustworthy item snapshots."""


_PROBLEM_BEGIN = re.compile(r"\\begin\s*\{problem\}")
_PROBLEM_END = re.compile(r"\\end\s*\{problem\}")
_PROBLEM_ENVIRONMENT = re.compile(
    r"\\begin\s*\{problem\}(.*?)\\end\s*\{problem\}", re.DOTALL
)
_PAST_QUESTION_HEADING = re.compile(
    r"\\subsection\*?\s*\{\s*题目\s*([1-9]\d*)\s*\}"
)
_TRAILING_SECTION_LABEL = re.compile(
    r"(?:\r?\n[ \t]*)*"
    r"\\noindent[ \t]*\\textbf[ \t]*\{[^{}\r\n]*\}"
    r"[ \t]*(?:\r?\n[ \t]*)*\Z"
)


def _nonempty_snapshots(values: list[str]) -> list[str]:
    snapshots = [value.strip() for value in values]
    if any(not value for value in snapshots):
        raise ExamSnapshotError("试卷包含空题目快照")
    return snapshots


def _problem_environment_snapshots(body: str) -> list[str] | None:
    begin_count = len(_PROBLEM_BEGIN.findall(body))
    end_count = len(_PROBLEM_END.findall(body))
    if not begin_count and not end_count:
        return None
    matches = list(_PROBLEM_ENVIRONMENT.finditer(body))
    if begin_count != end_count or len(matches) != begin_count:
        raise ExamSnapshotError("试卷 problem 环境不完整")
    return _nonempty_snapshots([match.group(1) for match in matches])


def _past_heading_snapshots(body: str) -> list[str]:
    matches = list(_PAST_QUESTION_HEADING.finditer(body))
    if not matches:
        return []
    numbers = [int(match.group(1)) for match in matches]
    if numbers != list(range(1, len(matches) + 1)):
        raise ExamSnapshotError("真题题号必须从 1 开始连续排列")
    values: list[str] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[match.end():end]
        if index + 1 < len(matches):
            content = _TRAILING_SECTION_LABEL.sub("", content)
        values.append(content)
    return _nonempty_snapshots(values)


def extract_exam_item_snapshots(latex_body: object) -> list[str]:
    """Extract ordered item bodies from either supported frozen exam format."""

    body = str(latex_body or "")
    problem_snapshots = _problem_environment_snapshots(body)
    if problem_snapshots is not None:
        return problem_snapshots
    return _past_heading_snapshots(body)
