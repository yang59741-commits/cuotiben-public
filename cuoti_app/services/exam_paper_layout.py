"""Deterministic presentation cleanup for generated exam papers.

The helpers operate on screen and LaTeX presentation copies only.
Candidate snapshots and their semantic hashes remain authoritative.
"""

from __future__ import annotations

import re

from .reviewed_source_labels import REVIEWED_SOURCE_LABELS
from .tex import _LATEX_ENVIRONMENT, _MATH_ENVIRONMENTS


# These packages are already used by the standard exam document. Native D2
# papers also need them for source option labels and ordinary math notation.
D2_DOCUMENT_PACKAGES = r"\usepackage{amsmath,amssymb,bm,mathrsfs,esint,enumitem}"


_TRAILING_EXERCISE_HEADING = re.compile(
    r"(?:\r?\n)[ \t]*\\textbf\s*\{\s*习题\s*\d+"
    r"(?:\s*[.．]\s*\d+)*\s*\}[ \t]*(?:\r?\n)*\Z"
)
# This source heading also bounds an existing exclusion range. Keep it in the
# source and remove it only from the reviewed card's presentation copy.
_CANDIDATE_TRAILING_HEADING = {
    "QB52D4323016A58F659AEB5504": (r"设函数 \( u=u(x, y, z) \)", "单元练习7．5"),
    "QBD6EC10793D5A55269513CFD8": (r"设函数 \( u=u(x, y, z) \)", "单元练习7．5"),
}
_LEADING_EXAMPLE_NUMBER = re.compile(
    r"\A\s*【\s*例\s*\d+(?:\s*[.．]\s*\d+)*\s*】\s*"
)
_LEADING_SOURCED_NUMBER = re.compile(
    r"\A\s*\d{1,4}\s*[.．、]\s*"
    r"(?=[（(][\u3400-\u9fff]{2,24}\s*[,，]\s*"
    r"(?:19|20)\d{2}\s*年[^）)\r\n]*[）)])"
)
# Keep this fingerprint narrow: the leading 80 was verified as the source-book
# item number, while unverified bare leading numbers remain part of the content.
_LEADING_CONFIRMED_SOURCE_NUMBER = re.compile(
    r"\A\s*80\s*[.．、]\s*"
    r"(?=已知\s*4\s*维线性空间\s*\\\(\s*V\s*\\\)\s*"
    r"的线性变换\s*\\\(\s*\\sigma\s*\\\))"
)
_CANDIDATE_SOURCE_NUMBER = {
    # Current cards after reviewed heading cleanup. Original source neighbors
    # are 21/22/23 and 4/5, respectively; these are not mathematical constants.
    "QB92C0711EA623DB384B4BDFFB": re.compile(
        r"\A\s*22\s*[.．、]\s*"
        r"(?=设\s*\\\(\s*A\s*,\s*B\s*\\\)\s*都是)"
    ),
    "QB47D2E658424EFF208E9B2BA9": re.compile(
        r"\A\s*5\s*[.．、]\s*(?=由紧性定理证明聚点定理)"
    ),
    "QB0E0444995F25ECFC31F4029D": re.compile(
        r"\A\s*18\s*[.．、]\s*"
        r"(?=设\s*\\\(\s*n\s*\\\)\s*阶方阵\s*\\\(\s*A\s*\\\)\s*"
        r"和\s*\\\(\s*B\s*\\\)\s*有相同的特征多项式)"
    ),
    "QB329994F26723BC33C6E1D063": re.compile(
        r"\A\s*41\s*[.．、]\s*"
        r"(?=设\s*\\\(\s*\\sigma\s*\\\)\s*是\s*2\s*维欧氏空间\s*"
        r"\\\(\s*V\s*\\\)\s*的一个旋转)"
    ),
}
_DIRECT_PRODUCT_CANDIDATE_ID = "QB2C9BE69EFFDCAE915841983C"
_DIRECT_PRODUCT_DEFINITIONS = re.compile(
    r"(?P<prefix>规定)\s*\\\((?P<addition>.*?)\\\)\s*，\s*"
    r"(?P<tag_one>（1）)\s*\\\((?P<scalar>.*?)\\\)\s*。\s*"
    r"(?P<tag_two>（2）)(?P<question_one>（1）)\s*证明：",
    re.DOTALL,
)
_DIRECT_PRODUCT_ADDITION = (
    r"\left(\boldsymbol{\alpha}_{1},\boldsymbol{\alpha}_{2}\right)"
    r"+\left(\boldsymbol{\beta}_{1},\boldsymbol{\beta}_{2}\right)="
    r"\left(\boldsymbol{\alpha}_{1}+\boldsymbol{\beta}_{1},"
    r"\boldsymbol{\alpha}_{2}+\boldsymbol{\beta}_{2}\right)"
)
_DIRECT_PRODUCT_SCALAR = (
    r"k\left(\boldsymbol{\alpha}_{1},\boldsymbol{\alpha}_{2}\right)="
    r"\left(k\boldsymbol{\alpha}_{1},k\boldsymbol{\alpha}_{2}\right)"
)
_INLINE_MATH = re.compile(
    r"\\\((?P<body>(?:[^\\]|\\(?!\))[^\r\n])*)\\\)"
)
_ADJACENT_SCALABLE_FACTORS = re.compile(
    r"(?P<left>\\right\s*\))(?P<space>\s*)"
    r"(?P<right>\\left\s*\()"
)
_SUBQUESTION_MARKER = re.compile(r"（\s*(\d{1,2})\s*）")
_KNOWLEDGE_POINT_MARKER = re.compile(r"(?:[（(]\s*)?(\d{1,2})(?:\s*[）)]|[.．、](?!\d))\s*")
_SUBQUESTION_BOUNDARY = frozenset("。；;:：．.]）)")


def _strip_presentation_labels(content: str, *, candidate_id: str | None) -> str:
    """Remove source-only labels whose shape is unambiguous at paper time."""

    rendered = _TRAILING_EXERCISE_HEADING.sub("", str(content or "")).strip()
    trailing = _CANDIDATE_TRAILING_HEADING.get(candidate_id or "")
    if trailing is not None:
        stem, heading = trailing
        if rendered.startswith(stem) and rendered.endswith("\n" + heading):
            rendered = rendered[:-len(heading)].rstrip()
    rendered = _LEADING_EXAMPLE_NUMBER.sub("", rendered, count=1)
    rendered = _LEADING_SOURCED_NUMBER.sub("", rendered, count=1)
    rendered = _LEADING_CONFIRMED_SOURCE_NUMBER.sub("", rendered, count=1)
    reviewed_label = REVIEWED_SOURCE_LABELS.get(candidate_id or "")
    if reviewed_label is not None:
        prefix, stem = reviewed_label
        if rendered.startswith(prefix + stem):
            rendered = rendered[len(prefix):]
    candidate_number = _CANDIDATE_SOURCE_NUMBER.get(candidate_id or "")
    if candidate_number is not None:
        rendered = candidate_number.sub("", rendered, count=1)
    return rendered.strip()


def question_display_content(content: str, *, candidate_id: str | None = None) -> str:
    """Separate source labels for screen text without changing frozen content."""
    return _wrap_reviewed_field_conditions(
        _strip_presentation_labels(content, candidate_id=candidate_id), candidate_id=candidate_id
    )


def _wrap_reviewed_field_conditions(content: str, *, candidate_id: str | None) -> str:
    """Keep the reviewed denominator condition inside its existing array."""
    if candidate_id != "QBAC330D3A2A06CE5AA2728C35" or not content.startswith("令\n\\[\nF=\\left\\{"):
        return content
    original = r"0 \leq i \leq n, 0 \leq j \leq m,\quad b_{0}+b_{1}e+\cdots+b_{m}e^{m}\neq 0"
    wrapped = r"0 \leq i \leq n, 0 \leq j \leq m," + "\\\\\n" + r"b_{0}+b_{1}e+\cdots+b_{m}e^{m}\neq 0"
    return content.replace(original, wrapped) if content.count(original) == 1 else content


def _allow_inline_product_wraps(content: str) -> str:
    """Add semantic-neutral line-break opportunities between large factors."""

    pieces = []
    index = 0
    while index < len(content):
        end = index + 1
        if content[index] == "%":
            newline = content.find("\n", index)
            end = len(content) if newline < 0 else newline + 1
        elif content.startswith(r"\verb", index):
            delimiter_at = index + len(r"\verb")
            if content[delimiter_at:delimiter_at + 1] == "*":
                delimiter_at += 1
            if delimiter_at < len(content) and not content[delimiter_at].isspace():
                close = content.find(content[delimiter_at], delimiter_at + 1)
                if close >= 0:
                    end = close + 1
        elif content[index] == "\\":
            match = _INLINE_MATH.match(content, index)
            if match is not None:
                body = _ADJACENT_SCALABLE_FACTORS.sub(
                    r"\g<left>\g<space>\\allowbreak\g<right>",
                    match.group("body"),
                )
                pieces.append(r"\(" + body + r"\)")
                index = match.end()
                continue
            end = min(index + 2, len(content))
        pieces.append(content[index:end])
        index = end
    return "".join(pieces)


def _text_mode_subquestion_markers(
    latex: str, *, marker_pattern=_SUBQUESTION_MARKER, allow_space=False,
) -> list[tuple[int, int, int]]:
    """Find boundary-like Chinese subquestion markers outside protected TeX."""

    markers: list[tuple[int, int, int]] = []
    math_modes: list[str] = []
    brace_depth = 0
    index = 0
    length = len(latex)
    while index < length:
        char = latex[index]
        if char == "%":
            newline = latex.find("\n", index + 1)
            index = length if newline < 0 else newline + 1
            continue
        if char == "\\":
            environment = _LATEX_ENVIRONMENT.match(latex, index)
            if environment is not None:
                name = environment.group("name")
                marker = f"environment:{name}"
                if name in _MATH_ENVIRONMENTS:
                    if environment.group("action") == "begin":
                        math_modes.append(marker)
                    elif marker in math_modes:
                        reverse_index = math_modes[::-1].index(marker)
                        del math_modes[len(math_modes) - reverse_index - 1]
                index = environment.end()
                continue
            if latex.startswith(r"\(", index):
                math_modes.append(r"\(")
                index += 2
                continue
            if latex.startswith(r"\)", index):
                if r"\(" in math_modes:
                    reverse_index = math_modes[::-1].index(r"\(")
                    del math_modes[len(math_modes) - reverse_index - 1]
                index += 2
                continue
            if latex.startswith(r"\[", index):
                math_modes.append(r"\[")
                index += 2
                continue
            if latex.startswith(r"\]", index):
                if r"\[" in math_modes:
                    reverse_index = math_modes[::-1].index(r"\[")
                    del math_modes[len(math_modes) - reverse_index - 1]
                index += 2
                continue
            if latex.startswith(r"\verb", index):
                cursor = index + len(r"\verb")
                if cursor < length and latex[cursor] == "*":
                    cursor += 1
                if cursor < length and not latex[cursor].isspace():
                    delimiter = latex[cursor]
                    closing = latex.find(delimiter, cursor + 1)
                    if closing >= 0:
                        index = closing + 1
                        continue
            index += 2
            continue
        if char == "$":
            mode = "$$" if index + 1 < length and latex[index + 1] == "$" else "$"
            if math_modes and math_modes[-1] == mode:
                math_modes.pop()
            else:
                math_modes.append(mode)
            index += len(mode)
            continue
        if char == "{":
            brace_depth += 1
            index += 1
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            index += 1
            continue
        if not math_modes and brace_depth == 0:
            match = marker_pattern.match(latex, index)
            if match is not None:
                prefix = latex[:index]
                previous = prefix.rstrip()
                whitespace = prefix[len(previous) :]
                at_boundary = (
                    not previous
                    or "\n" in whitespace
                    or (allow_space and bool(whitespace))
                    or previous[-1] in _SUBQUESTION_BOUNDARY
                )
                if at_boundary:
                    markers.append((match.start(), match.end(), int(match.group(1))))
                index = match.end()
                continue
        index += 1
    return markers


def knowledge_prelude_latex(content: str) -> str:
    """Lay out explicit numbered points without splitting formulas or prose."""
    content = str(content or '').strip()
    if re.search(r'\\begin\{(?:enumerate|itemize|description)\}', content):
        return content
    markers = _text_mode_subquestion_markers(
        content, marker_pattern=_KNOWLEDGE_POINT_MARKER, allow_space=True,
    )
    if (len(markers) < 2 or content[:markers[0][0]].strip()
            or [number for _, _, number in markers] != list(range(1, len(markers) + 1))):
        return content
    points = [content[end:markers[index + 1][0] if index + 1 < len(markers) else len(content)].strip()
              for index, (_, end, _) in enumerate(markers)]
    if any(not point for point in points):
        return content
    return '\n'.join([r'\begin{enumerate}', *(r'\item ' + point for point in points), r'\end{enumerate}'])


def _layout_subquestions(content: str) -> str:
    markers = _text_mode_subquestion_markers(content)
    if len(markers) < 2 or [item[2] for item in markers] != list(
        range(1, len(markers) + 1)
    ):
        return content

    rendered = content
    for start, _end, _number in reversed(markers):
        whitespace_start = start
        while whitespace_start > 0 and rendered[whitespace_start - 1].isspace():
            whitespace_start -= 1
        rendered = (
            rendered[:whitespace_start].rstrip()
            + "\n\\par\\smallskip\\noindent "
            + rendered[start:]
        )
    return rendered.strip()


def _compact_tex(value: str) -> str:
    return re.sub(r"\s+", "", value)


def _layout_direct_product_definitions(content: str, *, candidate_id: str | None) -> str:
    if candidate_id != _DIRECT_PRODUCT_CANDIDATE_ID:
        return content
    match = _DIRECT_PRODUCT_DEFINITIONS.search(content)
    if match is None:
        return content
    if (
        _compact_tex(match.group("addition")) != _DIRECT_PRODUCT_ADDITION
        or _compact_tex(match.group("scalar")) != _DIRECT_PRODUCT_SCALAR
    ):
        return content
    text_marker_starts = {
        start for start, _end, _number in _text_mode_subquestion_markers(content)
    }
    if match.start("question_one") not in text_marker_starts:
        return content
    replacement = (
        match.group("prefix")
        + "\n\\[\n"
        + match.group("addition").strip()
        + " \\tag{1}\n\\]\n\\[\n"
        + match.group("scalar").strip()
        + " \\tag{2}\n\\]\n（1）证明："
    )
    return content[:match.start()] + replacement + content[match.end():]


def render_question_content(content: str, *, candidate_id: str | None = None) -> str:
    """Return a paper-only rendering of one canonical question snapshot."""

    rendered = question_display_content(content, candidate_id=candidate_id)
    rendered = _layout_direct_product_definitions(
        rendered, candidate_id=candidate_id
    )
    rendered = _layout_subquestions(rendered)
    return _allow_inline_product_wraps(rendered)


__all__ = ["render_question_content", "question_display_content"]
