"""Read-only discovery and structure extraction for historical exam TeX files."""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from ..utils import QUESTION_SUBSECTION_PATTERN

from .ai_budgeting import estimate_text_tokens
from .catalog import load_default_catalog
from .tex import UNSAFE_LATEX_PATTERN, normalize_generated_latex


_YEAR_HEADING = re.compile(
    r'(?:\\noindent\s*)?\\textbf\{[^}\n]*?'
    r'(?P<year>(?:19|20)\d{2})\s*年[^}\n]*?'
    r'(?:真题|考试初试)[^}\n]*\}',
)
_QUESTION_HEADING = QUESTION_SUBSECTION_PATTERN
_STANDALONE_YEAR = re.compile(r'(?<!\d)(?P<year>(?:19|20)\d{2})\s*年')
_ARABIC_QUESTION_LINE = re.compile(
    r'^\s*(?P<number>\d{1,3})\s*[、．.。)]'
)
_ARABIC_ITEM = re.compile(
    r'^\s*\\item\s*\[\s*(?:'
    r'[（(]\s*(?P<paren_number>\d{1,3})\s*[、．.。]?\s*[）)]'
    r'|(?P<plain_number>\d{1,3})\s*[、．.。)]?'
    r')\s*\]'
)
_UNTRUSTED_NUMERIC_ITEM = re.compile(
    r'^\s*\\item\s*\[\s*(?:\d|[（(]\s*\d)'
)
_CHINESE_NUMBER_LINE = re.compile(
    r'^\s*(?P<number>[一二三四五六七八九十百]+)\s*[、．.。)]'
)
_REVIEWED_BOUNDARY_LINE = re.compile(
    r'^\s*原题边界[“"](?P<number>\d{1,3}|[一二三四五六七八九十百]+)\s*[、．.。)]?[”"]\s*[:：]'
)
_PAREN_SUBQUESTION = re.compile(r'^\s*[（(]\s*\d+\s*[）)]')
_SECTION_HEADING = re.compile(
    r'(?m)^[ \t]*(?:'
    r'(?:\\noindent\s*)?\\textbf\{'
    r'(?P<bold_label>[一二三四五六七八九十]+[、．.。][^}]*)\}'
    r'|\\section\*?\{'
    r'(?P<section_label>[一二三四五六七八九十]+[、．.。][^}]*)\}'
    r'|\\item\s*\[\s*'
    r'(?P<item_number>[一二三四五六七八九十]+\s*[、．.。])\s*\]'
    r'(?P<item_label>[^\r\n]*)'
    r'|(?P<plain_label>[一二三四五六七八九十]+\s*[、．.。]'
    r'[^\r\n]{0,120}(?:题|部分)[^\r\n]{0,80}?)(?:\\\\)?\s*$'
    r')'
)
_INSTITUTION_ALIASES = {
    '大工': '大连理工大学',
    '大连理工': '大连理工大学',
    '大连理工大学': '大连理工大学',
    '湖师大': '湖南师范大学',
    '湖南师大': '湖南师范大学',
    '湖南师范大学': '湖南师范大学',
    '北大': '北京大学',
    '清华': '清华大学',
    '复旦': '复旦大学',
}

_PRIORITY_INSTITUTIONS = ('大连理工大学', '湖南师范大学')
_AUDITED_COMPLETE_COUNTS = {
    ('大连理工大学', '高等代数', 2022): 3,
    ('大连理工大学', '数学分析', 2023): 3,
}
_AUDITED_SECTION_LABELS = {
    **{
        ('大连理工大学', '数学分析', year): ('简答题', '计算题', '证明题')
        for year in (2024, 2025, 2026)
    },
    **{
        ('湖南师范大学', '数学分析', year): ('填空题', '计算题', '证明题')
        for year in (2024, 2025, 2026)
    },
}
_AUDITED_STRUCTURE_WARNINGS = {
    ('吉林大学', '高等代数', 2024): (
        '原卷从第三部分开始且前两部分缺失，已保留现状，禁止自动补题'
    ),
    ('郑州大学', '高等代数', 2026): (
        '原卷填空题标题标注6题但仅提供5题，已保留残缺警告'
    ),
    ('浙江大学', '高等代数', 2026): (
        '原卷仅提供5题，且只有第一部分填空题，已保留残缺警告'
    ),
}
_AUDITED_SEQUENTIAL_NUMBERING = frozenset({
    # The first three top-level list items precede imported markers whose
    # printed numbering restarts; preserve their order as the source slots.
    ('华中师范大学', '数学分析', 2026),
})
_LIST_TOKEN = re.compile(
    r'\\(?P<kind>begin|end)\{(?P<environment>enumerate|itemize|description)\}'
    r'|(?P<item>\\item\b)'
)
_LIST_ENVIRONMENTS = frozenset({'enumerate', 'itemize', 'description'})


class PastExamSourceError(ValueError):
    """Raised when configured TeX sources cannot provide a usable exam.

    The extra fields let the source screen explain which file and phase needs
    attention while keeping ``str(exc)`` compatible with existing callers.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "past_source_error",
        status_code: int = 422,
        retryable: bool = False,
        hint: str = "",
        phase: str = "source",
        file: str | None = None,
        year: int | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status_code = int(status_code)
        self.retryable = bool(retryable)
        self.hint = str(hint or "")
        self.phase = str(phase or "source")
        self.file = file
        self.year = year
        self.details = dict(details or {})

    def as_issue(self) -> dict:
        return {
            "file": self.file,
            "year": self.year,
            "phase": self.phase,
            "code": self.code,
            "reason": str(self),
            "retryable": self.retryable,
            "hint": self.hint,
            "details": dict(self.details),
        }


_RAW_UNICODE_MATH = frozenset(
    "∫∬∭∮∯∰∑∏√∞≤≥≠≈≡∂∇πθφλμσΣΔ"
)
_MATH_ENVIRONMENTS = frozenset(
    {
        "math",
        "displaymath",
        "equation",
        "equation*",
        "align",
        "align*",
        "gather",
        "gather*",
        "multline",
        "multline*",
        "split",
        "cases",
        "matrix",
        "pmatrix",
        "bmatrix",
        "vmatrix",
        "Vmatrix",
    }
)


def _validate_adapted_math_markup(content: str) -> None:
    raw_symbols = sorted(set(content) & _RAW_UNICODE_MATH)
    if raw_symbols:
        raise PastExamSourceError(
            "AI 服务返回的改编题含裸 Unicode 数学符号："
            + "".join(raw_symbols)
            + "；必须改用 LaTeX 命令并放入数学环境"
        )

    math_stack: list[str] = []
    index = 0
    while index < len(content):
        if content.startswith(r"\(", index):
            math_stack.append(r"\(")
            index += 2
            continue
        if content.startswith(r"\)", index):
            if math_stack and math_stack[-1] == r"\(":
                math_stack.pop()
            index += 2
            continue
        if content.startswith(r"\[", index):
            math_stack.append(r"\[")
            index += 2
            continue
        if content.startswith(r"\]", index):
            if math_stack and math_stack[-1] == r"\[":
                math_stack.pop()
            index += 2
            continue
        environment = re.match(r"\\(begin|end)\{([^}]+)\}", content[index:])
        if environment:
            action, name = environment.groups()
            if name in _MATH_ENVIRONMENTS:
                marker = f"env:{name}"
                if action == "begin":
                    math_stack.append(marker)
                elif marker in math_stack:
                    reverse_index = len(math_stack) - 1 - math_stack[::-1].index(marker)
                    math_stack.pop(reverse_index)
            index += environment.end()
            continue
        if content[index] == "$" and (
            index == 0 or content[index - 1] != "\\"
        ):
            marker = "$$" if content.startswith("$$", index) else "$"
            if math_stack and math_stack[-1] == marker:
                math_stack.pop()
            else:
                math_stack.append(marker)
            index += len(marker)
            continue
        if (
            content[index] in "_^"
            and (index == 0 or content[index - 1] != "\\")
            and not math_stack
        ):
            raise PastExamSourceError(
                "AI 服务返回的改编题有数学环境外的 "
                f"{content[index]}；必须用 \\(...\\) 或 \\[...\\] 包裹公式"
            )
        if content[index] == "\\":
            command = re.match(r"\\[A-Za-z@]+|\\.", content[index:])
            if command:
                index += command.end()
                continue
        index += 1


def _subject_from_name(filename: str) -> str | None:
    if '数分' in filename or '数学分析' in filename:
        return '数学分析'
    if '高代' in filename or '高等代数' in filename:
        return '高等代数'
    return None


def _subject_from_path(base: Path, path: Path) -> str | None:
    for part in reversed(path.relative_to(base).parts):
        subject = _subject_from_name(part)
        if subject:
            return subject
    return None


def normalize_institution_name(name: str) -> str:
    cleaned = re.sub(r'\s+', '', str(name or '')).strip('_-')
    return _INSTITUTION_ALIASES.get(cleaned, cleaned)


def _institution_from_path(base: Path, path: Path) -> str:
    relative = path.relative_to(base)
    if len(relative.parts) > 1:
        return normalize_institution_name(relative.parts[0])
    stem = path.stem
    for alias, institution in _INSTITUTION_ALIASES.items():
        if alias in stem:
            return institution
    candidate = re.sub(r'^chapter[^_]*_', '', stem, flags=re.IGNORECASE)
    candidate = re.sub(r'(数学分析|数分|高等代数|高代|真题|试卷)', '', candidate)
    candidate = re.sub(r'[_\-\s]+', '', candidate)
    return normalize_institution_name(candidate) or '其他院校'


def iter_official_tex_files(directory: Path):
    for path in sorted(directory.rglob('*.tex')):
        relative_parts = path.relative_to(directory).parts
        lowered_parts = [part.lower() for part in relative_parts]
        transient_parts = {'.tmp', 'tmp', '.staging', 'staging', '.building', 'building'}
        transient_suffixes = ('.tmp.tex', '.partial.tex', '.staging.tex', '.building.tex')
        if any(
            marker in part
            for part in lowered_parts
            for marker in ('backup', '模拟')
        ) or any(
            part in transient_parts or part.startswith('.')
            for part in lowered_parts[:-1]
        ) or (
            lowered_parts[-1].startswith('.')
            or lowered_parts[-1].endswith(transient_suffixes)
        ):
            continue
        if path.is_file() and _subject_from_path(directory, path):
            yield path


# Compatibility alias retained for the import tool and older audit runners.
# The public iterator above is the single implementation of the filtering
# policy; callers using the historical private name must see the same stream.
_official_tex_files = iter_official_tex_files


def _year_sections(text: str):
    matches = list(_YEAR_HEADING.finditer(text))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section = text[match.start():end]
        section = re.sub(r'\\end\{document\}\s*$', '', section).strip()
        sections.append({'year': int(match.group('year')), 'text': section})
    return sections


def _document_body(text: str) -> str:
    body = text
    if r'\begin{document}' in body:
        body = body.split(r'\begin{document}', 1)[1]
    if r'\end{document}' in body:
        body = body.split(r'\end{document}', 1)[0]
    return body.strip()


def _infer_standalone_year(path: Path, text: str) -> int | None:
    match = _STANDALONE_YEAR.search(path.stem)
    if match:
        return int(match.group('year'))
    match = _STANDALONE_YEAR.search(text[:1200])
    return int(match.group('year')) if match else None


def _standalone_question_anchors(body: str) -> list[tuple[int, str]]:
    """Find main questions in the raw single-year TeX format.

    Numeric items at the first list level are main questions; parenthesized
    items remain subquestions. When a file uses Chinese numerals, each
    substantive top-level heading is treated as one main question.
    """
    return [
        (item['line'], item['number'])
        for item in _main_question_anchors(body)
        if item['kind'] != 'subsection'
    ]


_CHINESE_ITEM = re.compile(
    r'^\s*\\item\s*\[\s*(?P<number>[一二三四五六七八九十百]+)'
    r'\s*[、．.。)]?\s*\]'
)
_EMPTY_ITEM_CHINESE = re.compile(
    r'^\s*\\item\s*\[\s*\]\s*'
    r'(?P<number>[一二三四五六七八九十百]+)\s*[、．.。)]'
)
_SECTION_TYPE_TEXT = re.compile(
    r'^\s*(?:'
    r'计算(?:与|和|及|、)?证明(?:题|部分)?(?:\s|[、．.。:：；，,（(]|$)'
    r'|'
    r'(?:填空|选择|判断|辨析|是非|计算|证明|解答|简答|综合|混合|问答|应用|基础|大题|小题)'
    r'(?:题|部分)(?:\s|[、．.。:：；，,（(]|$)'
    r'|(?:填空|选择|判断|计算|证明|解答|简答|回答)'
    r'(?:下列|以下)?(?:各)?(?:题|问题)(?:\s|[、．.。:：；，,（(]|$)'
    r')'
)
_SCORE_SECTION_TEXT = re.compile(
    r'^\s*[（(]\s*\d+\s*分\s*[）)]\s*'
    r'[^\r\n]{0,48}(?:必答|选答|任选)'
)
_SCORED_TYPE_SECTION_TEXT = re.compile(
    r'^\s*[（(]\s*\d+\s*分\s*[）)]\s*'
    r'(?:填空|选择|判断|是非|计算|证明|解答|简答|综合|混合|问答)题'
)
_NUMBERED_SCORE_SECTION_TEXT = re.compile(
    r'^\s*[（(]\s*第\s*\d+(?:\s*[、，,]\s*\d+)*\s*题\s*(?:每题\s*)?\d+\s*分'
    r'(?:\s*[，,；;]\s*第\s*\d+(?:\s*[、，,]\s*\d+)*\s*题\s*(?:每题\s*)?\d+\s*分)*'
    r'\s*[）)]\s*(?:\\\\)?\s*$'
)
_AUDITED_DALIAN_2003_NESTED_BODY_SHA256 = (
    '67823b093da12bee79c50d509ea200344ed90ae2ae355e877ac9317939b90e26'
)


def _normalize_inline_question_markers(body: str) -> str:
    """Put every imported subsection marker on its own logical line.

    The parser still returns content from the original string; this logical
    normalization only makes multiple markers on one physical line visible to
    the line-oriented boundary state machine.
    """

    normalized = re.sub(
        r'(?<!\n)(?=\\subsection\*?\{\s*题目\s*\d+\s*\})',
        '\n',
        body,
    )
    return normalized.lstrip('\n')


def _chinese_number_value(value: str) -> int | None:
    rendered = str(value or '').strip()
    digits = {
        '一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
        '六': 6, '七': 7, '八': 8, '九': 9,
    }
    if rendered == '十':
        return 10
    if rendered == '百':
        return 100
    if '十' in rendered:
        head, tail = rendered.split('十', 1)
        tens = digits.get(head, 1 if not head else 0)
        ones = digits.get(tail, 0 if not tail else -1)
        if tens > 0 and ones >= 0:
            return tens * 10 + ones
        return None
    return digits.get(rendered)


def _numeric_label(value: str) -> int | None:
    rendered = str(value or '').strip()
    return int(rendered) if rendered.isdigit() else _chinese_number_value(rendered)


def _confirmed_section_heading(line: str) -> re.Match | None:
    """Return only headings that are demonstrably section labels.

    ``_SECTION_HEADING`` is intentionally broad for legacy discovery and can
    match a real question such as ``一．本题求矩阵的秩``.  Boundary extraction
    must use a narrower predicate so substantive main questions are never
    discarded as metadata.
    """

    match = _SECTION_HEADING.match(line)
    if match is None:
        return None
    label = (
        match.group('bold_label')
        or match.group('section_label')
        or match.group('plain_label')
        or match.group('item_label')
        or ''
    )
    label = re.sub(
        r'^\s*[一二三四五六七八九十百]+\s*[、．.。)]\s*',
        '',
        label,
    )
    type_match = (
        _SECTION_TYPE_TEXT.match(label)
        or _SCORE_SECTION_TEXT.match(label)
        or _SCORED_TYPE_SECTION_TEXT.match(label)
        or _NUMBERED_SCORE_SECTION_TEXT.match(label)
    )
    if type_match is None:
        # A bare bold ordinal is used by some papers as a real partition
        # delimiter; the following score note and question anchor supply the
        # meaning that the label itself omits.
        return match if not label.strip() and (
            match.group('bold_label') or match.group('section_label')
        ) else None
    remainder = label[type_match.end():].strip()
    # ``一、证明题：设...`` is a question stem/title, not a partition.  Real
    # headings may carry only punctuation, points, or short scoring notes.
    colon = re.search(r'[：:]\s*(?P<tail>.*)', label)
    if colon:
        tail = colon.group('tail').strip()
        metadata = re.match(
            r'^(?:每题|共\s*\d+\s*分|以下各题|下列各题|本部分|'
            r'任选|选答|计分|答题说明)',
            tail,
        )
        if tail and metadata is None:
            return None
    return match


def _next_substantive_lines(lines: list[str]) -> list[str]:
    result = [''] * len(lines)
    following = ''
    structural = re.compile(
        r'^\s*\\(?:begin|end)\{(?:itemize|enumerate|description)\}\s*$'
    )
    for index in range(len(lines) - 1, -1, -1):
        result[index] = following
        candidate = re.split(r'(?<!\\)%', lines[index], maxsplit=1)[0].strip()
        if candidate and not structural.match(candidate):
            following = lines[index]
    return result


def _section_evidence(lines: list[str], next_substantive: list[str]) -> list[bool]:
    """Tell whether each type-looking line precedes a real question anchor.

    A partition label is followed (possibly after score notes) by a marker or
    printed-number anchor before the next type-looking line.  Conversely,
    ``一．计算题（15分）`` followed directly by its mathematical stem is the
    question title itself.  The reverse pass keeps this contextual decision
    linear in the paper size.
    """

    depth = 0
    depths = []
    for line in lines:
        depths.append(depth)
        for token in re.finditer(
            r'\\(?P<kind>begin|end)\{(?:itemize|enumerate|description)\}',
            line,
        ):
            if token.group('kind') == 'begin':
                depth += 1
            else:
                depth = max(0, depth - 1)

    evidence = [False] * len(lines)
    following_relevant: int | None = None
    note = re.compile(
        r'^\s*(?:每题|各题|以下各题|下列各题|本部分|本大题|计分|共\s*\d+\s*分)'
    )
    structural = re.compile(
        r'^\s*\\(?:begin|end)\{(?:itemize|enumerate|description)\}\s*$'
    )

    def is_question_signal(index: int | None) -> bool:
        if index is None:
            return False
        candidate_line = lines[index]
        marker = _QUESTION_HEADING.search(candidate_line)
        # Any following marker is sufficient evidence that the preceding
        # type line is a partition.  The main scanner separately decides
        # whether that marker is a nested import residue.
        if marker:
            return True
        if depths[index] == 0:
            return bool(
                _ARABIC_QUESTION_LINE.match(candidate_line)
                or (
                    _CHINESE_NUMBER_LINE.match(candidate_line)
                    and _confirmed_section_heading(candidate_line) is None
                )
            )
        if depths[index] == 1:
            return bool(
                _ARABIC_ITEM.match(candidate_line)
                or _CHINESE_ITEM.match(candidate_line)
            )
        return False

    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        candidate = _confirmed_section_heading(line)
        evidence[index] = is_question_signal(following_relevant)
        if candidate is not None:
            following_relevant = index
            continue
        stripped = re.split(r'(?<!\\)%', line, maxsplit=1)[0].strip()
        if not stripped or structural.match(line) or note.match(line):
            continue
        following_relevant = index
    return evidence


_STRICT_SUBQUESTION_PREFIX = re.compile(
    r'^\s*(?:'
    r'[（(]\s*(?P<plain_number>\d+)\s*[）)](?!\s*分)'
    r'|\\item\s*\[\s*[（(]\s*(?P<item_number>\d+)\s*[）)]\s*\]'
    r')'
)
_PLAIN_SUBQUESTION_PREFIX = re.compile(
    r'^\s*(?P<number>\d+)\s*[、．.。)]\s*'
    r'(?:[（(]\s*\d+\s*分\s*[）)]\s*)?\S'
)
_SCORED_PARENT_STEM = re.compile(r'^\s*[（(]\s*\d+\s*分\s*[）)]\s*\S')
_EMPTY_MATH_ANSWER = re.compile(r'=\s*(?:\\[)\]]|\${1,2})\s*[．.。]?\s*$')


def _marker_fragment(lines: list[str], start: int, end: int) -> str:
    fragment = '\n'.join(lines[start:end])
    return _QUESTION_HEADING.sub('', fragment).strip()


def _has_self_contained_parent_stem(lines: list[str], start: int, end: int) -> bool:
    """Recognize a scored stem or an explicit scored question group."""

    fragment = _marker_fragment(lines, start, end)
    first = next((line.strip() for line in fragment.splitlines() if line.strip()), '')
    first = re.sub(
        r'^\s*(?:\d+|[一二三四五六七八九十百]+)\s*[、．.。)]\s*',
        '',
        first,
    )
    scored_group = re.fullmatch(
        r'[（(]\s*\d+\s*分\s*[）)]\s*解答(?:如下|下列)问题\s*[：:]?\s*(?:\\\\)?',
        first,
    )
    return bool(_SCORED_PARENT_STEM.match(first)) and (
        len(re.sub(r'\s+', '', fragment)) >= 18 or scored_group is not None
    )


def _split_subquestion_continuation(
    lines: list[str],
    parent_start: int,
    marker_line: int,
    next_marker_line: int,
) -> bool:
    """Recognize one audited marker inserted inside a ``(1)/(2)`` pair."""

    parent = _marker_fragment(lines, parent_start, marker_line)
    parent_first = next(
        (line.strip() for line in parent.splitlines() if line.strip()), ''
    )
    if not re.match(r'^\s*[（(]\s*1\s*[）)]', parent_first):
        return False
    continuation = _marker_fragment(lines, marker_line, next_marker_line)
    substantive = [line.strip() for line in continuation.splitlines() if line.strip()]
    return bool(
        substantive
        and not _STRICT_SUBQUESTION_PREFIX.match(substantive[0])
        and any(re.match(r'^\s*[（(]\s*2\s*[）)]', line) for line in substantive[:4])
    )


def _non_question_reason(content: str) -> str:
    rendered = _QUESTION_HEADING.sub('', content)
    rendered = re.sub(r'\\(?:section|subsection|chapter)\*?\s*\{([^{}]*)\}', r'\1', rendered)
    rendered = re.sub(r'^\s*\\item\s*\[[^]]*\]\s*', '', rendered)
    rendered = re.sub(
        r'^\s*(?:\d+|[一二三四五六七八九十百]+)\s*[、．.。)]\s*',
        '',
        rendered,
    )
    plain = re.sub(r'\\\\|[。．.：:\s]', '', rendered)
    if re.fullmatch(r'(?:未知|忘了|想不起来了?|作废)', plain):
        return 'source_placeholder_or_recall_fragment'
    if plain in {'高阶无穷小问题', '求对偶基'}:
        return 'context_free_fragment'
    if plain in {
        '一个关于什么矩阵存在性的问题',
        '求一曲线旋转得到的旋转体的体积',
        '已知实矩阵常微分方程部分',
        '求矩阵的特征多项式与特征值',
        '最小二乘法Delta',
        '最小二乘法Δ',
    }:
        return 'audited_missing_object_fragment'
    if re.fullmatch(r'最小二乘法\\item\[\]\\\(\\Delta\\\)', plain):
        return 'audited_missing_object_fragment'
    if re.fullmatch(r'10分，共30分[）)]?', plain):
        return 'truncated_score_instruction'
    if re.fullmatch(
        r'(?:共\d+题[,，])?(?:每题|每小题)\d+分[,，]共\d+分',
        plain,
    ):
        return 'score_instruction_only'
    if re.fullmatch(
        r'(?:混合题|问答题|高等代数|数学分析|解答如下问题|'
        r'每小题\d+分|每小题[一二三四五六七八九十]+分)',
        plain,
    ):
        return 'section_or_instruction_only'
    return ''


def _annotate_non_questions(body: str, anchors: list[dict]) -> None:
    lines = body.splitlines()
    for index, anchor in enumerate(anchors):
        end_line = int(
            anchor.get('end_line')
            or (anchors[index + 1]['line'] if index + 1 < len(anchors) else len(lines))
        )
        reason = _non_question_reason(
            '\n'.join(lines[int(anchor['line']):end_line])
        )
        if reason:
            anchor['non_question_reason'] = reason
            continue
        raw_fragment = '\n'.join(lines[int(anchor['line']):end_line])
        if any(
            issue.startswith('列表环境交叉')
            for issue in _list_fragment_issues(raw_fragment)
        ):
            anchor['non_question_reason'] = 'unusable_source_fragment'
            continue
        cleaned = _clean_anchor_content(lines, anchor, end_line)
        if not _usable_past_problem(cleaned):
            anchor['non_question_reason'] = 'unusable_source_fragment'
            continue
        if '填空' in str(anchor.get('section') or ''):
            fragment = _QUESTION_HEADING.sub('', '\n'.join(
                lines[int(anchor['line']):end_line]
            ))
            fragment = re.sub(
                r'^\s*(?:\d+|[一二三四五六七八九十百]+)\s*[、．.。)]\s*',
                '',
                fragment,
            )
            has_blank = bool(re.search(r'\\_\s*\\_|=\s*(?:\\_|$)', fragment)
                             or _EMPTY_MATH_ANSWER.search(fragment))
            explicit_task = bool(re.search(r'(?:^|[）)]\s*)(?:求|已知|设|证明|计算|判断|讨论)', fragment))
            if explicit_task and not has_blank:
                anchor['section'] = '试题（题型未标明）'


def _clean_anchor_content(lines: list[str], anchor: dict, end_line: int) -> str:
    problem_lines = lines[int(anchor['line']):int(end_line)]
    content = '\n'.join(problem_lines)
    content = _QUESTION_HEADING.sub('', content).strip()
    content = re.sub(
        r'\\(?:section|chapter)\*?\s*\{([^{}]*)\}',
        r'\1',
        content,
    ).strip()
    problem_lines = content.splitlines()
    if problem_lines:
        problem_lines[0] = _strip_question_line(problem_lines[0])
    content = '\n'.join(problem_lines).strip()
    content = re.sub(
        r'\\end\{(?:enumerate|itemize|document)\}\s*$',
        '',
        content,
    ).strip()
    return _sanitize_extracted_problem(content)


def _semantic_body_fingerprint(body: str) -> str:
    semantic_lines = []
    for line in body.splitlines():
        line = re.split(r'(?<!\\)%', line, maxsplit=1)[0].strip()
        if line:
            semantic_lines.append(line)
    return hashlib.sha256('\n'.join(semantic_lines).encode('utf-8')).hexdigest()


def _main_question_anchors(body: str) -> list[dict]:
    """Return one boundary for every recoverable top-level main question.

    Normalised imports are not uniformly normalised: a subsection can mark a
    real main question, can merely precede its printed number, or can have been
    inserted immediately before a nested ``\\item``.  Treating subsections as
    the only authority therefore both joins independent questions and splits a
    parent stem from its children.  This scanner combines both sources of
    evidence while keeping list depth authoritative for nested subquestions.
    """
    # Keep the scanner correct when called directly as well as through the
    # public extractors.  The normalization is idempotent for already-split
    # markers and remains a single linear substitution pass.
    body = _normalize_inline_question_markers(body)
    allow_nested_promotion = (
        _semantic_body_fingerprint(body)
        == _AUDITED_DALIAN_2003_NESTED_BODY_SHA256
    )
    lines = body.splitlines()
    next_substantive = _next_substantive_lines(lines)
    section_evidence = _section_evidence(lines, next_substantive)
    next_marker_line = [len(lines)] * len(lines)
    following_marker = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        next_marker_line[index] = following_marker
        if _QUESTION_HEADING.search(lines[index]):
            following_marker = index
    marker_starts_with_one: dict[int, bool] = {}
    marker_segment_has_two: dict[int, bool] = {}
    for index, line in enumerate(lines):
        marker = _QUESTION_HEADING.search(line)
        if marker is None:
            continue
        first = line[marker.end():].strip() or next_substantive[index]
        marker_starts_with_one[index] = bool(
            re.match(r'^\s*[（(]\s*1\s*[）)]', first)
        )
        substantive = []
        for cursor in range(index + 1, next_marker_line[index]):
            candidate = lines[cursor].strip()
            if candidate:
                substantive.append(candidate)
                if len(substantive) == 4:
                    break
        marker_segment_has_two[index] = any(
            re.match(r'^\s*[（(]\s*2\s*[）)]', candidate)
            for candidate in substantive
        )
    anchors: list[dict] = []
    list_roles: list[str] = []
    substantive_since_anchor = False
    previous_line_blank = True
    current_section = ''
    current_section_number: int | None = None
    section_epoch = 0
    source_has_markers = bool(_QUESTION_HEADING.search(body))
    child_numbering = False
    marker_parent_value: int | None = None
    last_printed_main_number: int | None = None
    promoted_item_depth: int | None = None
    promoted_sequence_seen = False
    blocked_item_depths: set[int] = set()
    parent_stem_cache: dict[int, bool] = {}
    plain_child_next: dict[int, int] = {}

    for line_number, line in enumerate(lines):
        stripped = line.strip()
        depth = len(list_roles)
        subsection = _QUESTION_HEADING.search(line)
        section_match = _confirmed_section_heading(line)
        raw_match = None
        raw_kind = ''

        section_allowed = depth == 0 or (
            depth == 1 and list_roles[-1] == 'outer'
        ) or promoted_item_depth == depth
        # A type-looking line immediately after an imported marker is that
        # question's printed title (for example ``一．计算题（15分）``), not a
        # new partition.  This decision needs scanner context and must not be
        # repeated later by a context-free classifier.
        marker_title = (
            section_match is not None
            and anchors
            and anchors[-1]['kind'] == 'subsection'
            and not substantive_since_anchor
        )
        if marker_title:
            if section_evidence[line_number]:
                # Some imports put one marker before an entire typed section.
                # The empty marker is metadata residue, not a main question.
                anchors.pop()
                substantive_since_anchor = False
                child_numbering = False
                marker_parent_value = None
            else:
                anchors[-1]['section'] = _section_heading_label(section_match)
                section_epoch += 1
                anchors[-1]['section_epoch'] = section_epoch
                current_section = ''
                current_section_number = None
                section_match = None
        elif (
            section_match is not None
            and not section_evidence[line_number]
            and promoted_item_depth != depth
        ):
            section_match = None
        if section_match is not None and section_allowed:
            if anchors and anchors[-1].get('end_line') is None:
                anchors[-1]['end_line'] = line_number
            current_section = _section_heading_label(section_match)
            prefix = re.match(
                r'^\s*(?:\\(?:section|subsection)\*?\{|\\textbf\{)?'
                r'(?P<number>[一二三四五六七八九十百]+)',
                current_section,
            )
            current_section_number = (
                _chinese_number_value(prefix.group('number')) if prefix else None
            )
            section_epoch += 1
            substantive_since_anchor = False

        if depth == 0 and section_match is None:
            raw_match = _ARABIC_QUESTION_LINE.match(line)
            raw_kind = 'arabic'
            if raw_match is None:
                raw_match = _CHINESE_NUMBER_LINE.match(line)
                raw_kind = 'chinese'
            if raw_match is None:
                raw_match = _REVIEWED_BOUNDARY_LINE.match(line)
                if raw_match is not None:
                    raw_kind = 'arabic' if raw_match.group('number').isdigit() else 'chinese'
        elif (
            depth == 1
            and list_roles[-1] == 'outer'
            and section_match is None
        ):
            raw_match = _ARABIC_ITEM.match(line)
            raw_kind = 'item'
            if raw_match is None:
                raw_match = _CHINESE_ITEM.match(line)
                raw_kind = 'chinese_item'
            if raw_match is None and _UNTRUSTED_NUMERIC_ITEM.match(line):
                raise PastExamSourceError(
                    '主问题的 \\item[...] 数字标签不完整或括号不匹配',
                    code='past_item_label_invalid',
                    phase='structure',
                    hint='修正完整数字标签后重新扫描',
                )

        elif allow_nested_promotion and depth >= 2 and section_match is None:
            nested_item = _ARABIC_ITEM.match(line)
            if nested_item is not None:
                nested_number = (
                    nested_item.group('paren_number')
                    or nested_item.group('plain_number')
                )
                nested_value = _numeric_label(nested_number)
                if (
                    last_printed_main_number is not None
                    and nested_value == last_printed_main_number + 1
                    and nested_value >= 4
                    and depth not in blocked_item_depths
                    and (promoted_item_depth is None or promoted_item_depth == depth)
                ):
                    raw_match = nested_item
                    raw_kind = 'item'
                    promoted_item_depth = depth
                    promoted_sequence_seen = True
                elif promoted_item_depth != depth:
                    blocked_item_depths.add(depth)

        if raw_match is None and section_match is None:
            chinese_item = _CHINESE_ITEM.match(line) if depth == 1 else None
            if (chinese_item is not None and anchors
                    and re.fullmatch(r'[一二三四五六七八九十百]+', str(anchors[-1]['raw_number']))
                    and last_printed_main_number is not None
                    and _numeric_label(chinese_item.group('number')) == last_printed_main_number + 1):
                # Imported lists can wrap the next Chinese-numbered main question.
                raw_match = chinese_item
                raw_kind = 'chinese_item'
            empty_item_chinese = _EMPTY_ITEM_CHINESE.match(line)
            if empty_item_chinese is not None:
                empty_value = _numeric_label(empty_item_chinese.group('number'))
                if (
                    last_printed_main_number is not None
                    and empty_value == last_printed_main_number + 1
                ):
                    raw_match = empty_item_chinese
                    raw_kind = 'chinese'

        next_content_line = ''
        if subsection:
            inline_remainder = line[subsection.end():].strip()
            next_content_line = inline_remainder or next_substantive[line_number]
        nested_subsection = bool(
            subsection
            and depth
            and list_roles[-1] == 'nested'
            and re.match(r'^\s*\\item\b', next_content_line)
        )
        parent_start = int(anchors[-1]['line']) if anchors else -1
        parent_merge_eligible = bool(
            anchors
            and anchors[-1].get('end_line') is None
            and int(anchors[-1].get('section_epoch') or 0) == section_epoch
        )
        parent_is_self_contained = False
        if parent_merge_eligible and subsection and not nested_subsection:
            if parent_start not in parent_stem_cache:
                parent_stem_cache[parent_start] = _has_self_contained_parent_stem(
                    lines, parent_start, line_number
                )
            parent_is_self_contained = parent_stem_cache[parent_start]
        merged_subsection = bool(
            subsection
            and not nested_subsection
            and parent_merge_eligible
            and (
                _STRICT_SUBQUESTION_PREFIX.match(next_content_line)
                or (
                    _PLAIN_SUBQUESTION_PREFIX.match(next_content_line)
                    and anchors[-1]['kind'] in {'chinese', 'chinese_item'}
                )
            )
            and parent_is_self_contained
            and '填空' not in str(anchors[-1].get('section') or '')
        )
        # Reviewed imports may split one multi-part problem across subsection
        # wrappers and annotate the continuation explicitly (for example,
        # ``原题边界“（3）”``).  The marker is an import boundary, not a new
        # top-level question; keep its body with the preceding parent.
        explicit_continuation = bool(
            subsection
            and not nested_subsection
            and parent_merge_eligible
            and anchors[-1]['kind'] == 'subsection'
            and parent_is_self_contained
            and '填空' not in str(anchors[-1].get('section') or '')
            and re.match(r'^原题边界[“"]\s*[（(]\s*[2-9]\s*[）)]', next_content_line)
        )
        if explicit_continuation:
            merged_subsection = True
        plain_match = _PLAIN_SUBQUESTION_PREFIX.match(next_content_line)
        if merged_subsection and plain_match is not None:
            plain_number = int(plain_match.group('number'))
            expected_plain = plain_child_next.get(parent_start, 1)
            merged_subsection = plain_number == expected_plain
            if merged_subsection:
                plain_child_next[parent_start] = expected_plain + 1
        if (
            subsection
            and not nested_subsection
            and not merged_subsection
            and parent_merge_eligible
            and anchors[-1]['kind'] == 'subsection'
        ):
            merged_subsection = bool(
                marker_starts_with_one.get(int(anchors[-1]['line']))
                and not _STRICT_SUBQUESTION_PREFIX.match(next_content_line)
                and marker_segment_has_two.get(line_number)
            )
        redundant_marker = bool(
            subsection and not nested_subsection and parent_merge_eligible
            and anchors[-1]['kind'] == 'subsection' and not substantive_since_anchor
        )
        if redundant_marker:
            # Keep the outer imported number, but discard its empty wrapper.
            anchors[-1]['line'] = line_number
        elif subsection and not nested_subsection and not merged_subsection:
            marker_section = current_section
            if (
                '填空' in marker_section
                and _SCORED_PARENT_STEM.match(next_content_line)
            ):
                marker_section = '试题（题型未标明）'
                current_section = marker_section
                current_section_number = None
                section_epoch += 1
            anchors.append(
                {
                    'line': line_number,
                    'number': subsection.group('number'),
                    'raw_number': subsection.group('number'),
                    'marker_number': subsection.group('number'),
                    'kind': 'subsection',
                    'section': marker_section,
                    'section_epoch': section_epoch,
                }
            )
            substantive_since_anchor = bool(
                line[subsection.end():].strip()
            )
            child_numbering = False
            marker_parent_value = None
        elif merged_subsection:
            substantive_since_anchor = True
            if _PLAIN_SUBQUESTION_PREFIX.match(next_content_line):
                child_numbering = True

        if raw_match is not None:
            if raw_kind == 'item':
                number = (
                    raw_match.group('paren_number')
                    or raw_match.group('plain_number')
                )
            else:
                number = raw_match.group('number')

            raw_value = _numeric_label(number)
            coalesces_marker = bool(
                anchors
                and anchors[-1]['kind'] == 'subsection'
                and not substantive_since_anchor
            )
            if (
                raw_kind in {'chinese', 'chinese_item'}
                and current_section
                and current_section_number is not None
                and raw_value is not None
                and raw_value > current_section_number
            ):
                previous_section = current_section
                current_section = (
                    '试题（题型未标明）'
                    if '填空' in previous_section
                    else ''
                )
                current_section_number = None
                section_epoch += 1
                if coalesces_marker:
                    anchors[-1]['section'] = current_section
                    anchors[-1]['section_epoch'] = section_epoch

            if raw_kind in {'chinese', 'chinese_item'}:
                child_numbering = False
                marker_parent_value = None
            elif (
                raw_kind == 'arabic'
                and marker_parent_value is not None
                and raw_value is not None
                and raw_value <= marker_parent_value
            ):
                child_numbering = True
            unmarked_scored_child = False
            if (raw_kind == 'arabic' and subsection is None and parent_merge_eligible
                    and anchors[-1]['kind'] in {'chinese', 'chinese_item'}
                    and raw_value == plain_child_next.get(parent_start, 1)):
                if parent_start not in parent_stem_cache:
                    parent_stem_cache[parent_start] = _has_self_contained_parent_stem(lines, parent_start, line_number)
                unmarked_scored_child = parent_stem_cache[parent_start]
                if unmarked_scored_child:
                    plain_child_next[parent_start] = raw_value + 1
            ignore_child_number = (child_numbering and raw_kind == 'arabic') or unmarked_scored_child

            # The printed number immediately following a subsection belongs
            # to that same question.  Consecutive unseparated Arabic lines in
            # such a marked block remain ordinary in-question enumeration;
            # blank-separated or Chinese headings are independent boundaries.
            compact_marked_enumeration = bool(
                raw_kind == 'arabic'
                and anchors
                and anchors[-1]['kind'] == 'subsection'
                and substantive_since_anchor
                and not previous_line_blank
            )
            if (
                not ignore_child_number
                and not coalesces_marker
                and not compact_marked_enumeration
            ):
                anchors.append(
                    {
                        'line': line_number,
                        'number': number,
                        'raw_number': number,
                        'kind': raw_kind,
                        'section': current_section,
                        'section_epoch': section_epoch,
                    }
                )
                marker_parent_value = None
            elif coalesces_marker:
                marker_value = _numeric_label(anchors[-1].get('marker_number'))
                if (
                    raw_kind in {'chinese', 'chinese_item'}
                    and last_printed_main_number is not None
                    and raw_value == last_printed_main_number + 1
                    and any(_numeric_label(a.get('marker_number')) == marker_value for a in anchors[:-1])
                ):
                    anchors[-1]['marker_number'] = str(raw_value)
                promoted_recovery = bool(
                    promoted_sequence_seen
                    and raw_value is not None
                    and last_printed_main_number is not None
                    and raw_value == last_printed_main_number + 1
                    and marker_value != raw_value
                )
                anchors[-1]['printed_number'] = number
                anchors[-1]['raw_number'] = number
                if promoted_recovery:
                    anchors[-1].pop('marker_number', None)
                    anchors[-1]['kind'] = raw_kind
                if current_section:
                    anchors[-1]['section'] = current_section
                    anchors[-1]['section_epoch'] = section_epoch
                marker_parent_value = raw_value
            if not ignore_child_number and raw_value is not None:
                last_printed_main_number = raw_value
            substantive_since_anchor = True

        for token in re.finditer(
            r'\\(?P<kind>begin|end)\{(?:itemize|enumerate|description)\}',
            line,
        ):
            if token.group('kind') == 'begin':
                marker_line = next_marker_line[line_number]
                next_item = (
                    _ARABIC_ITEM.match(next_substantive[marker_line])
                    if marker_line < len(lines) else None
                )
                parent_fragment = (
                    _marker_fragment(lines, int(anchors[-1]['line']), line_number)
                    if anchors else ''
                )
                # Imports also wrap independent marked questions in a list.
                # Shared stems and parenthesized children keep their nesting.
                marked_outer = bool(
                    not list_roles
                    and _QUESTION_HEADING.fullmatch(next_substantive[line_number].strip())
                    and next_item is not None
                    and next_item.group('plain_number')
                    and not re.search(r'[:：]\s*$', parent_fragment)
                    and not (
                        anchors and _has_self_contained_parent_stem(
                            lines, int(anchors[-1]['line']), line_number
                        )
                    )
                )
                list_roles.append(
                    'nested'
                    if anchors and substantive_since_anchor and not marked_outer
                    else 'outer'
                )
                blocked_item_depths.discard(len(list_roles))
            elif list_roles:
                blocked_item_depths.discard(len(list_roles))
                if promoted_item_depth == len(list_roles):
                    promoted_item_depth = None
                list_roles.pop()

        marker_only = bool(subsection and not line[subsection.end():].strip())
        structural_only = bool(
            re.fullmatch(
                r'\s*\\(?:begin|end)\{(?:itemize|enumerate)\}\s*',
                line,
            )
        )
        if (
            stripped
            and section_match is None
            and not marker_only
            and not structural_only
            and raw_match is None
        ):
            substantive_since_anchor = substantive_since_anchor or bool(anchors)
        previous_line_blank = not stripped

    _annotate_non_questions(body, anchors)
    has_markers = source_has_markers
    if has_markers:
        used_numbers = {
            value
            for item in anchors
            if item.get('marker_number') is not None
            for value in [_numeric_label(item.get('marker_number'))]
            if value is not None
        }
        next_free = 1
        for ordinal, item in enumerate(anchors, 1):
            item['ordinal'] = ordinal
            marker_value = _numeric_label(item.get('marker_number'))
            if marker_value is not None:
                item['number'] = str(marker_value)
                continue
            raw_value = _numeric_label(item.get('raw_number'))
            if (
                item.get('kind') in {'chinese', 'chinese_item'}
                and raw_value is not None
                and raw_value not in used_numbers
            ):
                used_numbers.add(raw_value)
                item['number'] = str(raw_value)
                continue
            while next_free in used_numbers:
                next_free += 1
            assigned = next_free
            used_numbers.add(assigned)
            item['number'] = str(assigned)

        raw_values = [
            _numeric_label(item.get('raw_number')) for item in anchors
        ]
        has_implicit_reset = any(
            current is not None and previous is not None and current <= previous
            for previous, current in zip(raw_values, raw_values[1:])
        )
        if has_implicit_reset:
            part = 1
            previous = None
            for item, raw_value in zip(anchors, raw_values):
                if (
                    previous is not None
                    and raw_value is not None
                    and raw_value <= previous
                ):
                    part += 1
                if not item.get('section'):
                    item['section'] = f'第{part}部分'
                previous = raw_value if raw_value is not None else previous
    else:
        part = 1
        previous = None
        for ordinal, item in enumerate(anchors, 1):
            raw_value = _numeric_label(item.get('raw_number') or item['number'])
            if previous is not None and raw_value is not None and raw_value <= previous:
                part += 1
            item['ordinal'] = ordinal
            item['section'] = item.get('section') or f'第{part}部分'
            previous = raw_value if raw_value is not None else previous
    return anchors


def _extract_standalone_structure(
    body: str,
    anchors: list[dict] | None = None,
) -> dict:
    anchors = list(anchors if anchors is not None else _main_question_anchors(body))
    eligible = [item for item in anchors if not item.get('non_question_reason')]
    excluded = [
        {
            'source_number': (
                int(item['number'])
                if str(item.get('number') or '').isdigit()
                else int(item.get('ordinal') or index + 1)
            ),
            'raw_source_number': str(item.get('raw_number') or ''),
            'marker_number': None,
            'reason': str(item['non_question_reason']),
            'line': int(item['line']),
        }
        for index, item in enumerate(anchors)
        if item.get('non_question_reason')
    ]
    numbers = [
        int(item['number']) if str(item.get('number') or '').isdigit() else index
        for index, item in enumerate(eligible, 1)
    ]
    sections = []
    start = 0
    previous = None
    for index, number in enumerate(numbers):
        if previous is not None and number <= previous:
            sections.append({
                'label': f'第{len(sections) + 1}部分',
                'question_count': index - start,
                'question_numbers': numbers[start:index],
            })
            start = index
        previous = number
    if numbers:
        sections.append({
            'label': f'第{len(sections) + 1}部分',
            'question_count': len(numbers) - start,
            'question_numbers': numbers[start:],
        })
    return {
        'question_count': len(numbers),
        'question_numbers': numbers,
        'source_had_markers': False,
        'excluded_questions': excluded,
        'sections': sections,
    }


def _clean_heading(label: str) -> str:
    return re.sub(r'\s+', ' ', label).strip()


def _section_heading_label(match: re.Match) -> str:
    label = (
        match.group('bold_label')
        or match.group('section_label')
        or match.group('plain_label')
    )
    if label:
        return _clean_heading(label)
    return _clean_heading(
        f"{match.group('item_number') or ''}"
        f"{(match.group('item_label') or '').lstrip()}"
    )


def _marked_numbering_sections(section_text: str, question_matches: list) -> list[dict]:
    """Recover unnamed blocks from the source number that follows each marker.

    Imported markers remain authoritative.  We inspect only the leading label
    inside each already-delimited main question, so nested subquestions cannot
    increase the main-question count.
    """
    raw_numbers = []
    global_numbers = [int(match.group(1)) for match in question_matches]
    item = re.compile(r'^\s*\\item\s*\[\s*(?P<number>\d{1,3})\s*[、．.。)]?')
    for index, match in enumerate(question_matches):
        end = question_matches[index + 1].start() if index + 1 < len(question_matches) else len(section_text)
        leading = section_text[match.end():end]
        number = None
        for line in leading.splitlines()[:8]:
            found = _ARABIC_QUESTION_LINE.match(line) or item.match(line)
            if found:
                number = int(found.group('number'))
                break
        if number is None:
            return []
        raw_numbers.append(number)
    if not any(
        raw_numbers[index] <= raw_numbers[index - 1]
        for index in range(1, len(raw_numbers))
    ):
        return []
    sections = []
    start = 0
    for index in range(1, len(raw_numbers)):
        if raw_numbers[index] <= raw_numbers[index - 1]:
            sections.append({
                'label': f'第{len(sections) + 1}部分',
                'question_count': index - start,
                'question_numbers': global_numbers[start:index],
            })
            start = index
    sections.append({
        'label': f'第{len(sections) + 1}部分',
        'question_count': len(raw_numbers) - start,
        'question_numbers': global_numbers[start:],
    })
    return sections


def extract_exam_structure(
    section_text: str,
    anchors: list[dict] | None = None,
) -> dict:
    """Extract only top-level exam questions from one yearly paper.

    Imported markers and raw printed numbering are reconciled by the same
    boundary scanner used for extraction.  This keeps structure counts aligned
    with candidates even when a normalised paper has missing or nested markers.
    """
    body = _normalize_inline_question_markers(_document_body(section_text))
    anchors = list(anchors) if anchors is not None else _main_question_anchors(body)
    if not anchors:
        return {'question_count': 0, 'question_numbers': [], 'sections': []}
    has_markers = bool(_QUESTION_HEADING.search(body))
    if not has_markers:
        return _extract_standalone_structure(body, anchors)

    eligible_anchors = [
        item for item in anchors if not item.get('non_question_reason')
    ]
    excluded_questions = [
        {
            'source_number': int(item['number']),
            'raw_source_number': str(item.get('raw_number') or ''),
            'marker_number': (
                str(item.get('marker_number'))
                if item.get('marker_number') is not None
                else None
            ),
            'reason': str(item['non_question_reason']),
            'line': int(item['line']),
        }
        for item in anchors
        if item.get('non_question_reason')
    ]
    numbers = [int(item['number']) for item in eligible_anchors]
    retained_marker_counts: dict[str, int] = {}
    for item in eligible_anchors:
        marker_number = item.get('marker_number')
        if marker_number is not None:
            key = str(marker_number)
            retained_marker_counts[key] = retained_marker_counts.get(key, 0) + 1
    ignored_marker_numbers = []
    for match in _QUESTION_HEADING.finditer(body):
        key = str(match.group('number'))
        if retained_marker_counts.get(key, 0):
            retained_marker_counts[key] -= 1
        else:
            ignored_marker_numbers.append(key)
    sections = []
    grouped: list[list[dict]] = []
    for anchor in eligible_anchors:
        key = (anchor.get('section_epoch'), anchor.get('section') or '')
        if not grouped or grouped[-1][0].get('_section_key') != key:
            grouped.append([])
        anchor = {**anchor, '_section_key': key}
        grouped[-1].append(anchor)
    for index, group in enumerate(grouped, 1):
        raw_values = [
            _numeric_label(item.get('raw_number')) for item in group
        ]
        implicit_reset = any(
            current is not None and previous is not None and current <= previous
            for previous, current in zip(raw_values, raw_values[1:])
        )
        label = str(
            group[0].get('section')
            or (f'第{index}部分' if implicit_reset or len(grouped) > 1 else '试题')
        )
        group_numbers = [int(item['number']) for item in group]
        sections.append(
            {
                'label': label,
                'question_count': len(group_numbers),
                'question_numbers': group_numbers,
                'raw_question_numbers': [
                    str(item.get('raw_number') or '') for item in group
                ],
            }
        )
    return {
        'question_count': len(eligible_anchors),
        'question_numbers': numbers,
        'source_had_markers': bool(_QUESTION_HEADING.search(body)),
        'raw_question_numbers': [
            str(item.get('raw_number') or '') for item in anchors
        ],
        'marker_question_numbers': [
            str(item.get('marker_number'))
            for item in anchors
            if item.get('marker_number') is not None
        ],
        'ignored_marker_numbers': ignored_marker_numbers,
        'excluded_questions': excluded_questions,
        'marker_question_ordinals': [
            {
                'marker_number': str(item.get('marker_number')),
                'ordinal': int(item['ordinal']),
            }
            for item in anchors
            if item.get('marker_number') is not None
        ],
        'sections': sections,
    }


def _structure_warnings(structure: dict, text: str) -> list[str]:
    numbers = list(structure.get('question_numbers') or [])
    warnings = []
    if not numbers:
        return ['无法识别主问题，疑似空白或格式残缺']
    duplicate = False
    nonconsecutive = False
    if structure.get('source_had_markers'):
        number_groups = [numbers]
    else:
        number_groups = [
            list(section.get('question_numbers') or [])
            for section in (structure.get('sections') or [])
        ] or [numbers]
    for group_index, group in enumerate(number_groups):
        numeric = [
            value
            for value in (_numeric_label(item) for item in group)
            if value is not None
        ]
        if len(numeric) != len(set(numeric)):
            duplicate = True
        if numeric:
            raw_labels = [str(value).strip() for value in structure.get(
                'raw_question_numbers', []
            )]
            primary_chinese_sequence = bool(
                structure.get('source_had_markers')
                and len(raw_labels) == len(numbers)
                and raw_labels
                and all(not value.isdigit() for value in raw_labels)
                and [
                    _numeric_label(value) for value in raw_labels
                ] == list(range(1, len(raw_labels) + 1))
            )
            ignored = (
                [
                    value
                    for value in (
                        _numeric_label(item)
                        for item in structure.get('ignored_marker_numbers') or []
                    )
                    if value is not None
                ]
                + [
                    int(item['source_number'])
                    for item in structure.get('excluded_questions') or []
                    if item.get('source_number') is not None
                ]
                if structure.get('source_had_markers')
                and group_index == 0
                and not primary_chinese_sequence
                else []
            )
            coverage = sorted(set(numeric + ignored))
            if coverage != list(range(min(coverage), max(coverage) + 1)):
                nonconsecutive = True
    if duplicate:
        warnings.append('主问题编号重复，疑似格式残缺')
    if nonconsecutive:
        warnings.append('主问题编号不连续，疑似缺题')
    excluded_count = len(structure.get('excluded_questions') or [])
    if excluded_count:
        warnings.append(f'已排除 {excluded_count} 个非题目残片或标题说明')
    if len(numbers) == 1:
        warnings.append('仅识别到 1 道主问题，请核对原卷是否完整')
    if text.count(r'\begin{document}') > 1 or text.count(r'\end{document}') > 1:
        warnings.append('文档边界重复，疑似合并文件格式异常')
    return warnings


def _apply_audited_sequential_numbering(anchors: list[dict], section: dict) -> list[dict]:
    key = (
        str(section.get('_source_institution') or ''),
        str(section.get('_source_subject') or ''),
        int(section.get('year') or 0),
    )
    if key not in _AUDITED_SEQUENTIAL_NUMBERING:
        return anchors
    number = 0
    for anchor in anchors:
        if anchor.get('non_question_reason'):
            continue
        number += 1
        anchor['number'] = str(number)
    return anchors


def _prepare_exam_section(section: dict) -> dict:
    prepared = dict(section)
    anchors = _apply_audited_sequential_numbering(
        _main_question_anchors(prepared['text']), prepared
    )
    structure = prepared.get('structure')
    if (
        not structure
        or (
            str(prepared.get('_source_institution') or ''),
            str(prepared.get('_source_subject') or ''),
            int(prepared.get('year') or 0),
        ) in _AUDITED_SEQUENTIAL_NUMBERING
    ):
        structure = extract_exam_structure(prepared['text'], anchors=anchors)
    warnings = _structure_warnings(structure, prepared['text'])
    prepared['structure'] = structure
    prepared['available'] = bool(structure.get('question_count'))
    prepared['warnings'] = warnings
    prepared['warning'] = '；'.join(warnings) if warnings else ''
    return prepared


def _default_structure_eligible(section: dict) -> bool:
    """Whether a usable paper is reliable enough to become the default year.

    Warning years remain selectable and visible.  Only automatic defaulting is
    conservative, so a newly imported fragment cannot silently replace the
    newest structurally complete paper.
    """
    if not section.get('available'):
        return False
    warnings = '；'.join(section.get('warnings') or [])
    severe_markers = (
        '无法识别主问题',
        '主问题编号重复',
        '主问题编号不连续',
        '仅识别到 1 道主问题',
        '明显少于历年典型题量',
        '文档边界重复',
        '原卷从第三部分开始',
        '仅提供5题',
    )
    return not any(marker in warnings for marker in severe_markers)


def discover_past_exam_sources(
    directory: str,
    *,
    issues: list[dict] | None = None,
) -> list[dict]:
    base = Path(directory)
    if not base.exists() or not base.is_dir():
        raise PastExamSourceError(
            '真题 TeX 目录不存在或不可访问',
            code='past_source_unavailable',
            status_code=503,
            retryable=True,
            hint='确认目录存在且应用具有读取权限后重新扫描',
            phase='discovery',
        )

    grouped = {}
    issues = issues if issues is not None else []
    for path in iter_official_tex_files(base):
        relative_path = path.relative_to(base).as_posix()
        try:
            text = path.read_text(encoding='utf-8')
        except UnicodeDecodeError as exc:
            issues.append(
                {
                    'file': relative_path,
                    'year': _infer_standalone_year(path, ''),
                    'phase': 'encoding',
                    'code': 'past_source_encoding',
                    'reason': '文件不是有效 UTF-8 编码，无法安全解析',
                    'retryable': False,
                    'hint': '将文件转换为 UTF-8 后点击“修复后重新扫描”',
                    'details': {'encoding_error': str(exc)},
                }
            )
            continue
        except OSError as exc:
            issues.append(
                {
                    'file': relative_path,
                    'year': _infer_standalone_year(path, ''),
                    'phase': 'read',
                    'code': 'past_source_read_error',
                    'reason': '文件读取失败',
                    'retryable': True,
                    'hint': '确认文件未被占用后重试扫描',
                    'details': {'error': str(exc)},
                }
            )
            continue
        years = _year_sections(text)
        if not years:
            year = _infer_standalone_year(path, text)
            body = _document_body(text)
            # Normalised files use subsection markers.  They must be checked
            # before raw body numbering so nested (1), (2), ... items cannot
            # become main questions.
            structure = extract_exam_structure(body)
            if year is not None:
                years = [{
                    'year': year,
                    'text': body,
                    'structure': structure,
                    'source_format': 'standalone',
                }]
        if not years:
            issues.append(
                {
                    'file': relative_path,
                    'year': _infer_standalone_year(path, text),
                    'phase': 'structure',
                    'code': 'past_structure_incomplete',
                    'reason': '未识别到有效的年份或主问题结构',
                    'retryable': False,
                    'hint': '补齐年份标题和题目结构后重新扫描',
                    'details': {},
                }
            )
            continue
        years = [
            {
                **item,
                'path': path,
                'relative_path': relative_path,
            }
            for item in years
        ]
        institution = _institution_from_path(base, path)
        subject = _subject_from_path(base, path)
        group = grouped.setdefault(
            (institution, subject),
            {
                'institution': institution,
                'subject': subject,
                'paths': [],
                'exam_sections': [],
            },
        )
        group['paths'].append(path)
        group['exam_sections'].extend(years)

    sources = []
    for group in grouped.values():
        sections_by_year = {}
        for section in group['exam_sections']:
            section = {
                **section,
                '_source_institution': group['institution'],
                '_source_subject': group['subject'],
            }
            section = _prepare_exam_section(section)
            audited_labels = _AUDITED_SECTION_LABELS.get(
                (group['institution'], group['subject'], section['year'])
            )
            structure_sections = section['structure'].get('sections') or []
            if audited_labels and len(audited_labels) == len(structure_sections):
                section['structure']['sections'] = [
                    {**item, 'label': audited_labels[index]}
                    for index, item in enumerate(structure_sections)
                ]
            audited_warning = _AUDITED_STRUCTURE_WARNINGS.get(
                (group['institution'], group['subject'], section['year'])
            )
            if audited_warning and audited_warning not in section['warnings']:
                section['warnings'].append(audited_warning)
                section['warning'] = '；'.join(section['warnings'])
            existing = sections_by_year.get(section['year'])
            if existing is None or len(section['text']) > len(existing['text']):
                sections_by_year[section['year']] = section
        all_years = sorted(sections_by_year.values(), key=lambda item: item['year'])
        usable_counts = sorted(
            int(item['structure']['question_count'])
            for item in all_years
            if item.get('available')
        )
        if len(usable_counts) >= 3:
            middle = len(usable_counts) // 2
            median_count = (
                usable_counts[middle]
                if len(usable_counts) % 2
                else (usable_counts[middle - 1] + usable_counts[middle]) / 2
            )
            for item in all_years:
                count = int(item['structure']['question_count'])
                audited_count = _AUDITED_COMPLETE_COUNTS.get(
                    (group['institution'], group['subject'], item['year'])
                )
                if (
                    item.get('available')
                    and count * 2 < median_count
                    and audited_count != count
                ):
                    warning = (
                        f'仅识别到 {count} 道主问题，明显少于历年典型题量，'
                        '请核对原卷是否完整'
                    )
                    if warning not in item['warnings']:
                        item['warnings'].append(warning)
                    item['warning'] = '；'.join(item['warnings'])
        years = [item for item in all_years if item['available']]
        if not years:
            for item in all_years:
                issues.append(
                    {
                        'file': str(item.get('relative_path') or ''),
                        'year': item.get('year'),
                        'phase': 'structure',
                        'code': 'past_structure_incomplete',
                        'reason': str(item.get('warning') or '未识别到可用主问题'),
                        'retryable': False,
                        'hint': '修复题目环境或主问题编号后重新扫描',
                        'details': {'warnings': list(item.get('warnings') or [])},
                    }
                )
            continue
        default_years = [
            item for item in years if _default_structure_eligible(item)
        ]
        latest = (default_years or years)[-1]
        structure = latest['structure']
        learning = years[max(0, len(years) - 3):]
        history_sample = '\n\n'.join(
            f"【{item['year']}年节选】\n{item['text'][:2200]}"
            for item in learning
        )
        sources.append(
            {
                'institution': group['institution'],
                'subject': group['subject'],
                'filename': (
                    group['paths'][0].name
                    if len(group['paths']) == 1
                    else f"{len(group['paths'])} 个 TeX 文件"
                ),
                'filenames': [path.name for path in group['paths']],
                'paths': group['paths'],
                'available_years': sorted({item['year'] for item in years}, reverse=True),
                'latest_year': latest['year'],
                'latest_available_year': years[-1]['year'],
                'latest_text': latest['text'],
                'exam_sections': years,
                'all_exam_sections': all_years,
                'history_sample': history_sample,
                **structure,
            }
        )
    return sorted(
        sources,
        key=lambda item: (
            _PRIORITY_INSTITUTIONS.index(item['institution'])
            if item['institution'] in _PRIORITY_INSTITUTIONS
            else len(_PRIORITY_INSTITUTIONS),
            item['institution'],
            item['subject'],
        ),
    )


def scan_past_exam_sources(directory: str) -> dict:
    """Return usable sources and per-file diagnostics in one deterministic scan."""

    issues: list[dict] = []
    try:
        sources = discover_past_exam_sources(directory, issues=issues)
    except PastExamSourceError as exc:
        issues.append(exc.as_issue())
        sources = []
    return {
        'sources': sources,
        'issues': issues,
    }


def public_source(source: dict, subject_id: int) -> dict:
    available_count = len(source.get('exam_sections') or [])
    learning_mode = 'all' if available_count <= 3 else 'deterministic'
    year_structures = []
    for item in sorted(
        source.get('all_exam_sections') or source.get('exam_sections') or [],
        key=lambda value: value['year'],
        reverse=True,
    ):
        structure = item.get('structure') or extract_exam_structure(item['text'])
        year_structures.append(
            {
                'year': item['year'],
                'filename': str(item.get('relative_path') or item.get('path') or ''),
                'question_count': structure['question_count'],
                'sections': structure['sections'],
                'available': bool(item.get('available', structure['question_count'])),
                'default_eligible': _default_structure_eligible(item),
                'warning': str(item.get('warning') or ''),
                'warnings': list(item.get('warnings') or []),
            }
        )
    return {
        'institution': source['institution'],
        'subject_id': subject_id,
        'subject': source['subject'],
        'filename': source['filename'],
        'filenames': source['filenames'],
        'latest_year': source['latest_year'],
        'latest_available_year': source.get(
            'latest_available_year', source['latest_year']
        ),
        'available_years': source['available_years'],
        'question_count': source['question_count'],
        'sections': source['sections'],
        'learning_sample_size': min(3, available_count),
        'learning_mode': learning_mode,
        'year_structures': year_structures,
    }


def source_for_subject(
    directory: str,
    subject: str,
    institution: str | None = None,
) -> dict:
    sources = [
        source
        for source in discover_past_exam_sources(directory)
        if source['subject'] == subject
        and len(source.get('exam_sections') or []) >= 1
    ]
    if institution:
        normalized = normalize_institution_name(institution)
        sources = [
            source
            for source in sources
            if source['institution'] == normalized
        ]
    if len(sources) == 1:
        return sources[0]
    if len(sources) > 1 and not institution:
        raise PastExamSourceError(
            f'{subject}存在多个院校来源，请选择院校',
            code='past_source_institution_required',
            status_code=400,
            retryable=False,
            hint='请选择目标院校后重新提交',
            phase='selection',
        )
    target = f'{institution}{subject}' if institution else subject
    raise PastExamSourceError(f'未找到{target}可用的正式真题 TeX')


def reference_exam_section(source: dict, reference_year: int | None = None) -> dict:
    """Return the requested usable yearly paper, defaulting to the newest."""
    sections = list(
        {
            item['year']: item
            for item in (source.get('exam_sections') or [])
            if item.get('available', True)
        }.values()
    )
    sections.sort(key=lambda item: item['year'])
    if not sections:
        raise PastExamSourceError('没有可用的真题结构年份')
    if reference_year in (None, ''):
        return sections[-1]
    try:
        requested = int(reference_year)
    except (TypeError, ValueError):
        raise PastExamSourceError('结构年份无效') from None
    for item in sections:
        if item['year'] == requested:
            return item
    raise PastExamSourceError(f'{requested}年真题不可用或不存在')


def reference_exam_structure(source: dict, reference_year: int | None = None) -> dict:
    """Return a public structure contract for one reference year."""
    section = reference_exam_section(source, reference_year)
    structure = section.get('structure') or extract_exam_structure(section['text'])
    return {
        'reference_year': section['year'],
        'question_count': structure['question_count'],
        'question_numbers': structure['question_numbers'],
        'sections': structure['sections'],
    }


def sample_exam_sections(
    source: dict,
    count: int = 3,
    rng=None,
    *,
    reference_year: int | None = None,
) -> list[dict]:
    """Choose the reference paper and its nearest usable predecessors.

    ``rng`` remains accepted for source compatibility but is intentionally not
    used.  Retries for the same reference year must receive the same papers.
    """
    if reference_year is None and isinstance(rng, (int, str)):
        reference_year, rng = rng, None
    sections = list(
        {
            item['year']: item
            for item in (source.get('exam_sections') or [])
            if item.get('available', True)
        }.values()
    )
    sections.sort(key=lambda item: item['year'])
    reference = reference_exam_section(source, reference_year)
    eligible = [item for item in sections if item['year'] <= reference['year']]
    if len(eligible) < count:
        raise PastExamSourceError(
            f'参考年份及此前可用真题不足：需要学习{count}份，当前只有{len(eligible)}份'
        )
    return eligible[-count:]


def learning_sample_sections(
    source: dict,
    rng=None,
    *,
    reference_year: int | None = None,
) -> tuple[list[dict], str]:
    """Return the reference year plus at most its two valid predecessors."""
    if reference_year is None and isinstance(rng, (int, str)):
        reference_year, rng = rng, None
    sections = list(
        {
            item['year']: item
            for item in (source.get('exam_sections') or [])
            if item.get('available', True)
        }.values()
    )
    sections.sort(key=lambda item: item['year'])
    reference = reference_exam_section(source, reference_year)
    eligible = [item for item in sections if item['year'] <= reference['year']]
    samples = eligible[-3:]
    mode = 'all' if len(samples) == len(sections) else 'deterministic'
    return samples, mode


def learning_sample_prompt_text(samples: list[dict]) -> str:
    return '\n\n'.join(
        f"===== {item['year']}年真题 =====\n{item['text']}"
        for item in samples
    )


def learning_sample_questions(samples: list[dict]) -> list[dict]:
    """Return every full main-question body from the fixed at-most-three years."""
    questions=[]
    for sample in samples:
        year=int(sample["year"])
        for problem in extract_yearly_problems(sample):
            content=problem["content"]
            questions.append({
                "candidate_id": f"PAST-{year}-{int(problem['source_number']):03d}",
                "year": year,
                "chapter": problem.get("source_section") or "未分板块",
                "source_file": str(sample.get("filename") or f"{year}年真题"),
                "source_number": int(problem["source_number"]),
                "source_ordinal": len(questions)+1,
                "content": content,
                "char_count": len(content),
                "token_estimate": estimate_text_tokens(content),
            })
    return questions


def _extract_bank_problems(path: Path) -> list[dict]:
    text = path.read_text(encoding='utf-8', errors='replace')
    matches = list(_QUESTION_HEADING.finditer(text))
    problems = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[match.end():end].strip()
        content = re.split(r'\\end\{document\}', content, maxsplit=1)[0].strip()
        content = re.split(
            r'\n\\textbf\{[^}\n]*(?:练习题|思考题)[^}\n]*\}',
            content,
            maxsplit=1,
        )[0].strip()
        if len(content) < 25:
            continue
        problems.append(
            {
                'source_number': int(match.group(1)),
                'content': content,
            }
        )
    return problems


def build_subject_candidate_pool(
    tex_base: str,
    subject: str,
    *,
    per_chapter: int = 5,
    limit: int = 60,
    catalog=None,
) -> list[dict]:
    base = Path(tex_base)
    if not base.exists() or not base.is_dir():
        raise PastExamSourceError('普通试题库目录不存在或不可访问')

    by_chapter = {}
    seen_content = set()
    mapping = (catalog or load_default_catalog()).chapter_sources
    for (mapped_subject, chapter), filenames in mapping.items():
        if mapped_subject != subject:
            continue
        chapter_candidates = []
        for filename in filenames:
            path = base / filename
            if not path.exists() or not path.is_file():
                continue
            for problem in _extract_bank_problems(path):
                normalized = re.sub(r'\s+', '', problem['content'])
                if normalized in seen_content:
                    continue
                seen_content.add(normalized)
                chapter_candidates.append(
                    {
                        'chapter': chapter,
                        'source_file': filename,
                        **problem,
                    }
                )
                if len(chapter_candidates) >= per_chapter:
                    break
            if len(chapter_candidates) >= per_chapter:
                break
        if chapter_candidates:
            by_chapter[chapter] = chapter_candidates

    pool = []
    depth = 0
    while len(pool) < limit:
        added = False
        for chapter in by_chapter:
            candidates = by_chapter[chapter]
            if depth >= len(candidates):
                continue
            pool.append(candidates[depth])
            added = True
            if len(pool) >= limit:
                break
        if not added:
            break
        depth += 1

    if not pool:
        raise PastExamSourceError(f'普通试题库中没有可用的{subject}题目')
    for index, candidate in enumerate(pool, start=1):
        candidate['candidate_id'] = f'Q{index:03d}'
    return pool


def _balanced_tex_braces(content: str) -> bool:
    depth = 0
    for raw_line in content.splitlines():
        line = re.split(r'(?<!\\)%', raw_line, maxsplit=1)[0]
        index = 0
        while index < len(line):
            char = line[index]
            if char in '{}':
                backslashes = 0
                cursor = index - 1
                while cursor >= 0 and line[cursor] == '\\':
                    backslashes += 1
                    cursor -= 1
                if backslashes % 2 == 0:
                    depth += 1 if char == '{' else -1
                    if depth < 0:
                        return False
            index += 1
    return depth == 0


def _usable_past_problem(content: str) -> bool:
    compact = re.sub(r'\s+', '', content)
    if (
        len(compact) < 8
        and not _short_actionable_problem(compact)
    ) or '\x00' in content or '\ufffd' in content:
        return False
    if UNSAFE_LATEX_PATTERN.search(content):
        return False
    if re.search(r'\\(?:section|subsection|chapter)\*?\s*\{', content):
        return False
    return _balanced_tex_braces(content) and not _list_fragment_issues(content)


def _short_actionable_problem(compact: str) -> bool:
    """Admit only short prompts that still name a concrete task and object."""

    if re.fullmatch(
        r'(?:第)?(?:\d+|[一二三四五六七八九十百]+)'
        r'(?:(?:道)?题(?:正文)?|道主问题)[。．.]?',
        compact,
    ):
        return True
    match = re.match(r'^(?:证明|判断|求|计算)[：:]?(?P<object>.+?)[。．.]?$', compact)
    if match is None:
        return False
    target = match.group('object').rstrip('。．.')
    if re.search(r'定理|结论|矩阵|极限|积分|方程|级数|多项式', target):
        return True
    return bool(re.search(r'(?:\\[A-Za-z]+|\d+\s*[=<>+\-*/]\s*\d+)', target))


def _list_fragment_issues(content: str) -> list[str]:
    """Return list-environment errors that make a sliced question unsafe.

    A formal paper may wrap several main questions in one ``itemize``.  Once
    those questions are sliced at ``\\subsection{题目N}``, a fragment can
    inherit only the opening or closing half of that wrapper.  Concatenating
    such fragments used to leak list state into the following question and
    make a perfectly valid ``\\item`` fail during final compilation.
    """
    stack = []
    issues = []
    for match in _LIST_TOKEN.finditer(content or ''):
        if match.group('item'):
            if not stack:
                issues.append('游离 \\item')
            continue
        environment = match.group('environment')
        if match.group('kind') == 'begin':
            stack.append(environment)
        elif not stack:
            issues.append(f'多余 \\end{{{environment}}}')
        elif stack[-1] != environment:
            issues.append(
                f'列表环境交叉：{stack[-1]} / {environment}'
            )
        else:
            stack.pop()
    issues.extend(f'缺少 \\end{{{environment}}}' for environment in stack)
    return issues


def _normalize_list_fragment(content: str) -> str:
    """Make a single extracted question independent of surrounding lists."""
    text = str(content or '')
    stack = []
    removals = []
    append_environments = []
    for match in _LIST_TOKEN.finditer(text):
        if match.group('item'):
            if stack:
                stack[-1]['items'] += 1
            continue
        environment = match.group('environment')
        if match.group('kind') == 'begin':
            stack.append(
                {
                    'environment': environment,
                    'start': match.start(),
                    'end': match.end(),
                    'items': 0,
                }
            )
            continue
        if stack and stack[-1]['environment'] == environment:
            stack.pop()
        else:
            # The opener belongs to an earlier sliced question.
            removals.append((match.start(), match.end()))

    for entry in stack:
        if entry['items']:
            append_environments.append(entry['environment'])
        else:
            # An empty opener is the outer wrapper of the original paper, not
            # part of this standalone question.
            removals.append((entry['start'], entry['end']))

    for start, end in sorted(removals, reverse=True):
        text = text[:start] + text[end:]
    if append_environments:
        text = text.rstrip() + ''.join(
            f'\n\\end{{{environment}}}'
            for environment in reversed(append_environments)
        )

    # OCR/imported fragments occasionally retain numbered subitems but lose
    # both list delimiters.  Wrap them once so they remain valid LaTeX.
    stack = []
    first_orphan = None
    for match in _LIST_TOKEN.finditer(text):
        if match.group('item'):
            if not stack and first_orphan is None:
                first_orphan = match.start()
            continue
        environment = match.group('environment')
        if match.group('kind') == 'begin':
            stack.append(environment)
        elif stack and stack[-1] == environment:
            stack.pop()
    if first_orphan is not None:
        text = (
            text[:first_orphan]
            + '\\begin{itemize}\n'
            + text[first_orphan:].rstrip()
            + '\n\\end{itemize}'
        )
    return text.strip()


def _section_for_question(structure: dict, number: int) -> str:
    for section in structure.get('sections') or []:
        if number in (section.get('question_numbers') or []):
            return str(section.get('label') or '').strip()
    return ''


def _strip_question_line(line: str) -> str:
    empty_item_chinese = _EMPTY_ITEM_CHINESE.match(line)
    if empty_item_chinese:
        return line[empty_item_chinese.end():].lstrip()
    for pattern in (_ARABIC_QUESTION_LINE, _CHINESE_NUMBER_LINE, _CHINESE_ITEM):
        match = pattern.match(line)
        if match:
            return line[match.end():].lstrip()
    match = _ARABIC_ITEM.match(line)
    if match:
        return line[match.end():].lstrip()
    if _UNTRUSTED_NUMERIC_ITEM.match(line):
        raise PastExamSourceError(
            '主问题的 \\item[...] 数字标签不完整或括号不匹配',
            code='past_item_label_invalid',
            phase='extraction',
            hint='修正完整数字标签后重新扫描',
        )
    generic_item = re.match(r'^\s*\\item\b\s*', line)
    if generic_item:
        return line[generic_item.end():].lstrip()
    return line


def _sanitize_extracted_problem(content: str) -> str:
    lines = content.splitlines()
    if lines:
        lines[0] = _strip_question_line(lines[0])
    sanitized = '\n'.join(lines).strip()
    for environment in ('enumerate', 'itemize'):
        begin_count = len(re.findall(rf'\\begin\{{{environment}\}}', sanitized))
        end_count = len(re.findall(rf'\\end\{{{environment}\}}', sanitized))
        if end_count > begin_count:
            sanitized = re.sub(
                rf'\\end\{{{environment}\}}\s*$',
                '',
                sanitized,
                count=1,
            ).strip()
    return _normalize_list_fragment(sanitized)


def _candidate_source_section(anchor: dict, structure: dict, content: str, number: int) -> str:
    """Apply the audited candidate-local fill-section metadata correction."""

    section = (
        str(anchor.get('section') or '').strip()
        or _section_for_question(structure, number)
    )
    if '填空' not in section:
        return section
    has_blank = bool(
        re.search(
            r'\\_|_{2,}|＿{2,}|=\s*(?:\\_|_{2,}|＿{2,}|$)',
            content,
        )
        or _EMPTY_MATH_ANSWER.search(content)
    )
    explicit_task = bool(
        re.search(r'(?:^|[。；;：:，,\n）)])\s*(?:求|证明|判断|计算|讨论)', content)
    )
    if explicit_task and not has_blank:
        return '试题（题型未标明）'
    return section


def extract_yearly_problems(section: dict) -> list[dict]:
    text = _normalize_inline_question_markers(_document_body(section['text']))
    structure = section.get('structure') or extract_exam_structure(text)
    problems = []
    lines = text.splitlines()
    anchors = _apply_audited_sequential_numbering(
        _main_question_anchors(text), section
    )
    for index, anchor in enumerate(anchors):
        if anchor.get('non_question_reason'):
            continue
        line_number = anchor['line']
        end_line = int(
            anchor.get('end_line')
            or (anchors[index + 1]['line'] if index + 1 < len(anchors) else len(lines))
        )
        problem_lines = lines[line_number:end_line]
        if not problem_lines:
            continue
        content = _clean_anchor_content(lines, anchor, end_line)
        rendered_number = str(anchor.get('number') or '')
        number = int(rendered_number) if rendered_number.isdigit() else index + 1
        if _usable_past_problem(content):
            problems.append(
                {
                    'source_number': number,
                    'source_ordinal': int(anchor.get('ordinal') or index + 1),
                    'raw_source_number': str(
                        anchor.get('raw_number') or rendered_number
                    ),
                    'source_section': _candidate_source_section(
                        anchor, structure, content, number
                    ),
                    'content': content,
                }
            )
    return problems


def canonical_problem_content(content: str) -> str:
    without_comments = '\n'.join(
        re.split(r'(?<!\\)%', line, maxsplit=1)[0]
        for line in content.splitlines()
    )
    return re.sub(r'\s+', '', without_comments).strip()


def past_exam_source_snapshot(directory: str, subject: str | None = None) -> str:
    """Hash formal TeX paths and bytes without mutating or exposing them."""
    base = Path(directory)
    if not base.exists() or not base.is_dir():
        raise PastExamSourceError('真题 TeX 目录不存在或不可访问')
    digest = hashlib.sha256()
    for path in iter_official_tex_files(base):
        if subject and _subject_from_path(base, path) != subject:
            continue
        relative = path.relative_to(base).as_posix().encode('utf-8')
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, 'big'))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, 'big'))
        digest.update(payload)
    return digest.hexdigest().upper()


def build_other_past_candidate_pool(
    directory: str,
    subject: str,
    target_institution: str,
    *,
    limit: int | None = None,
) -> list[dict]:
    """Build a deduplicated, read-only pool from other schools' same subject."""
    normalized_target = normalize_institution_name(target_institution)
    candidates = []
    seen_content = set()
    seen_ids = set()
    for source in discover_past_exam_sources(directory):
        if source['subject'] != subject:
            continue
        if normalize_institution_name(source['institution']) == normalized_target:
            continue
        for section in sorted(source.get('exam_sections') or [], key=lambda item: item['year']):
            for problem in extract_yearly_problems(section):
                canonical = canonical_problem_content(problem['content'])
                if not canonical or canonical in seen_content:
                    continue
                candidate_id = 'PE' + hashlib.sha256(
                    f'{subject}\0{canonical}'.encode('utf-8')
                ).hexdigest()[:24].upper()
                if candidate_id in seen_ids:
                    continue
                relative_path = str(section.get('relative_path') or '')
                candidate = {
                    'candidate_id': candidate_id,
                    'content': problem['content'],
                    'source_kind': 'past_exam',
                    'chapter': problem['source_section'],
                    'source_file': relative_path,
                    'relative_path': relative_path,
                    'source_number': problem['source_number'],
                    'original_question_number': problem['source_number'],
                    'source_institution': source['institution'],
                    'source_subject': subject,
                    'source_year': section['year'],
                    'source_section': problem['source_section'],
                }
                candidates.append(candidate)
                seen_content.add(canonical)
                seen_ids.add(candidate_id)
    candidates.sort(
        key=lambda item: (
            item['source_institution'],
            item['source_year'],
            item['source_file'],
            item['source_number'],
            item['candidate_id'],
        )
    )
    if limit is not None:
        if limit < 1:
            return []
        candidates = candidates[:limit]
    if not candidates:
        raise PastExamSourceError(f'其他院校真题中没有可用的{subject}题目')
    return candidates


def other_past_candidates_by_ids(
    directory: str,
    subject: str,
    target_institution: str,
    candidate_ids: list[str],
) -> list[dict]:
    """Rebuild a frozen other-school pool and restore IDs in manifest order."""
    requested = [str(value) for value in candidate_ids]
    if len(requested) != len(set(requested)):
        raise PastExamSourceError('冻结真题候选清单包含重复 ID')
    candidate_map = {
        item['candidate_id']: item
        for item in build_other_past_candidate_pool(
            directory,
            subject,
            target_institution,
        )
    }
    missing = [candidate_id for candidate_id in requested if candidate_id not in candidate_map]
    if missing:
        raise PastExamSourceError('真题候选池已变化，缺少冻结题目：' + '、'.join(missing))
    return [candidate_map[candidate_id] for candidate_id in requested]


# Short alias retained for route code that treats this as the whole formal
# source snapshot rather than a candidate-specific value.
past_exam_snapshot = past_exam_source_snapshot


def candidate_prompt_text(candidates: list[dict]) -> str:
    return '\n\n'.join(
        (
            f"【{candidate['candidate_id']}｜{candidate['chapter']}｜"
            f"{candidate['source_file']}#题目{candidate['source_number']}】\n"
            f"{candidate['content']}"
        )
        for candidate in candidates
    )


def select_candidates_from_ai(
    raw: str,
    candidates: list[dict],
    count: int,
    *,
    allow_adaptation: bool = False,
    max_adaptations: int = 1,
) -> list[dict]:
    text = (raw or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != '{':
                continue
            try:
                candidate_object, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(candidate_object, dict):
                parsed = candidate_object
                break
    selected = parsed.get('selected') if isinstance(parsed, dict) else None
    if not isinstance(selected, list) or len(selected) != count:
        actual = len(selected) if isinstance(selected, list) else 0
        raise PastExamSourceError(
            f'AI 服务选题数量不一致：应为{count}题，实际{actual}题'
        )

    candidate_map = {item['candidate_id']: item for item in candidates}
    ordered = [None] * count
    used = set()
    adaptation_count = 0
    for fallback_position, item in enumerate(selected, start=1):
        if not isinstance(item, dict):
            raise PastExamSourceError('AI 服务返回了无效选题项')
        candidate_id = str(item.get('candidate_id') or '').strip()
        try:
            position = int(item.get('position', fallback_position))
        except (TypeError, ValueError):
            raise PastExamSourceError('AI 服务返回了无效题目位置')
        if candidate_id not in candidate_map:
            raise PastExamSourceError(f'AI 服务选择了未知题目 {candidate_id}')
        if candidate_id in used:
            raise PastExamSourceError(f'AI 服务重复选择题目 {candidate_id}')
        if position < 1 or position > count or ordered[position - 1] is not None:
            raise PastExamSourceError('AI 服务返回了重复或越界的位置')
        selected_candidate = dict(candidate_map[candidate_id])
        selected_candidate['position'] = position
        selected_candidate['selection_reason'] = str(item.get('reason') or '').strip()
        adapted_content = normalize_generated_latex(
            str(item.get('adapted_content') or '')
        ).strip()
        if adapted_content:
            if not allow_adaptation:
                raise PastExamSourceError('当前未允许改编，但 DeepSeek 返回了改编题目')
            _validate_adapted_math_markup(adapted_content)
            adaptation_count += 1
            if adaptation_count > max_adaptations:
                raise PastExamSourceError(
                    f'AI 服务改编题目过多：每份最多{max_adaptations}题'
                )
            blocked_commands = (
                r'\documentclass',
                r'\begin{document}',
                r'\end{document}',
                r'\section',
                r'\subsection',
                r'\begin{solution}',
            )
            if any(command in adapted_content for command in blocked_commands):
                raise PastExamSourceError('AI 服务返回的改编题包含禁用结构命令')
            original_length = len(selected_candidate['content'])
            if len(adapted_content) < 25 or len(adapted_content) > max(
                600,
                original_length * 2 + 300,
            ):
                raise PastExamSourceError('AI 服务返回的改编题长度异常')
            selected_candidate['content'] = adapted_content
        selected_candidate['adapted'] = bool(adapted_content)
        ordered[position - 1] = selected_candidate
        used.add(candidate_id)
    return ordered


def build_exam_body(source: dict, selected: list[dict]) -> str:
    body = []
    cursor = 0
    question_number = 1
    for section in source['sections']:
        body.append(f"\\noindent\\textbf{{{section['label']}}}")
        body.append('')
        for _ in range(section['question_count']):
            candidate = selected[cursor]
            body.append(f"\\subsection{{题目{question_number}}}")
            body.append('')
            if candidate.get('adapted'):
                body.append(r'\textbf{[小幅改编]}')
                body.append('')
            candidate_content = _normalize_list_fragment(candidate['content'])
            list_issues = _list_fragment_issues(candidate_content)
            if list_issues:
                raise PastExamSourceError(
                    f"候选题 {candidate.get('candidate_id') or question_number} "
                    f"的列表结构无效：{list_issues[0]}"
                )
            body.append(candidate_content)
            body.append('')
            cursor += 1
            question_number += 1
    return '\n'.join(body).strip()


def clean_generated_latex(raw: str) -> str:
    text = (raw or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    if r'\begin{document}' in text:
        text = text.split(r'\begin{document}', 1)[1]
    if r'\end{document}' in text:
        text = text.split(r'\end{document}', 1)[0]
    return text.strip()


def generated_question_count(latex: str) -> int:
    subsection_count = len(_QUESTION_HEADING.findall(latex))
    problem_count = len(re.findall(r'\\begin\{problem\}', latex))
    return max(subsection_count, problem_count)
