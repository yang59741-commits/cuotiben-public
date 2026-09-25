"""TeX/result validation and bounded correction for generated exams."""

from __future__ import annotations

import json
import re

from .ai import DeepSeekConfigurationError, DeepSeekServiceError
from .exam_contracts import validate_difficulty_items
from .exam_selection import EXAM_AI_TIMEOUT_SECONDS
from .exam_prompting import build_exam_difficulty_review_messages
from .exam_paper_layout import knowledge_prelude_latex
from .exam_sources import parse_ai_json_object
from .job_contracts import JobError
from .tex import (
    escape_latex_text,
    normalize_generated_latex,
    validate_generated_latex,
)


class RawDraftValidationError(ValueError):
    """A single generated draft failed deterministic local validation."""


class DifficultyAuditUnavailableError(ValueError):
    """Both read-only difficulty audit responses failed their schema."""


def assemble_exam_document(title: str, summary: str, body: str) -> str:
    """Validate an exam body and wrap it in the stable printable document."""

    body = normalize_generated_latex(body)
    body = re.sub(
        r'\A(\s*\\section\*?\{(?:基础知识预备|预备知识)\})(.*?)(?=\\begin\{problem\}|\\section\*?\{|\Z)',
        lambda match: match[1] + '\n' + knowledge_prelude_latex(match[2]) + '\n\n',
        body, count=1, flags=re.DOTALL,
    )
    validate_generated_latex(body)
    latex_title = escape_latex_text(title)
    latex_summary = escape_latex_text(summary)
    return r'''\documentclass[12pt, a4paper, oneside]{ctexart}
\usepackage{amsmath, amsthm, amssymb, bm, mathrsfs}
\usepackage{mathtools, esint, extarrows, yhmath}
\usepackage{array, booktabs, cases, cancel}
\usepackage{color, framed, graphicx, hyperref}
\let\overparen\wideparen
\usepackage[top=2cm, bottom=2cm, left=2cm, right=2cm]{geometry}
\usepackage{enumitem}
\setlist{leftmargin=2em,itemsep=6pt}
\linespread{1.5}
\definecolor{shadecolor}{RGB}{241, 241, 255}
\newcounter{problemname}
\newenvironment{problem}{\begin{shaded}\stepcounter{problemname}\par\noindent\textbf{题目\arabic{problemname}. }}{\end{shaded}\par}
\newenvironment{solution}{\par\noindent\textbf{解答. }}{\par}
\newenvironment{note}{\par\noindent\textbf{题目\arabic{problemname}的注记. }}{\par}
\title{''' + latex_title + r'''}
\author{错题本自动生成}
\date{\today}
\begin{document}
\maketitle
\vspace{-0.5cm}
\begin{center}
''' + latex_summary + r'''
\end{center}
\vspace{0.5cm}

''' + body + r'''

\end{document}'''


def _safe_error_text(error):
    return str(error or "未知错误")[:2000]


def _parse_ai_json_object(raw):
    return parse_ai_json_object(
        raw,
        error_message="AI返回格式错误，未找到JSON对象",
    )


def _problem_environment_counts(latex):
    return (
        len(re.findall(r"\\begin\{problem\}", latex or "")),
        len(re.findall(r"\\end\{problem\}", latex or "")),
    )


_PROBLEM_ENVIRONMENT = re.compile(
    r"\\begin\{problem\}(.*?)\\end\{problem\}", re.DOTALL
)
_ADAPTED_SUFFIX = re.compile(r"(?:\[|［)\s*改编\s*(?:\]|］)\s*$")


def adapted_problem_count(latex: str) -> int:
    return sum(
        bool(_ADAPTED_SUFFIX.search(match.group(1)))
        for match in _PROBLEM_ENVIRONMENT.finditer(str(latex or ""))
    )


def _problem_exam_errors(body, question_count, max_adaptations):
    begin_count, end_count = _problem_environment_counts(body)
    errors = []
    if begin_count != question_count or end_count != question_count:
        errors.append(
            f"题量必须为 {question_count}，实际为 {begin_count} 个开始标记和 "
            f"{end_count} 个结束标记"
        )
    if max_adaptations is not None:
        adapted_count = adapted_problem_count(body)
        if adapted_count > max_adaptations:
            errors.append(
                f"改编题不得超过 {max_adaptations} 道，实际为 {adapted_count} 道"
            )
    return errors


def generate_problem_exam(
    sys_prompt, prompt, question_count, *, ai_call, max_adaptations=None
):
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]
    body = ai_call(
        messages,
        temperature=0.7,
        timeout=EXAM_AI_TIMEOUT_SECONDS,
        task="exam_generation",
        task_policy="exam_generation",
    )
    try:
        body = normalize_generated_latex(body)
        validate_generated_latex(body)
        errors = _problem_exam_errors(body, question_count, max_adaptations)
    except Exception as exc:
        raise RawDraftValidationError("试卷草稿未通过本地格式校验") from exc
    if errors:
        raise RawDraftValidationError(
            "试卷草稿不符合题量或改编约束：" + "；".join(errors)
        )
    return body


def _difficulty_sequence(quota):
    return [
        *("简单" for _ in range(quota["简单"])),
        *("中等" for _ in range(quota["中等"])),
        *("困难" for _ in range(quota["困难"])),
    ]


def _parse_structured_free_exam(
    raw, question_count, quota, allowed_chapters=()
):
    parsed = _parse_ai_json_object(raw)
    chapter_set = {
        str(chapter).strip()
        for chapter in allowed_chapters
        if str(chapter).strip()
    }
    if not chapter_set:
        raise ValueError("自由组卷缺少可校验的所选章节")
    prelude = normalize_generated_latex(
        str(
            parsed.get("knowledge_prelude")
            or parsed.get("prelude")
            or ""
        )
    ).strip()
    questions = parsed.get("questions")
    if not isinstance(questions, list) or len(questions) != question_count:
        raise ValueError(
            f"AI生成题量不一致：需要{question_count}题，"
            f"实际{len(questions) if isinstance(questions, list) else 0}题"
        )
    items = []
    for fallback, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise ValueError("AI返回了无效题目项")
        content = normalize_generated_latex(
            str(item.get("latex") or item.get("content") or "")
        ).strip()
        if not content:
            raise ValueError(f"AI返回的第{fallback}题为空")
        chapter = item.get("chapter")
        if not isinstance(chapter, str) or not chapter.strip():
            raise ValueError(f"AI返回的第{fallback}题没有声明章节")
        chapter = chapter.strip()
        if chapter not in chapter_set:
            raise ValueError(f"AI返回的第{fallback}题章节不在所选范围内")
        references = item.get("reference_ids") or []
        if not isinstance(references, list) or any(
            not isinstance(value, str) for value in references
        ):
            raise ValueError("题目参考来源必须是题目ID列表")
        items.append(
            {
                "position": item.get("position", fallback),
                "chapter": chapter,
                "difficulty": item.get("difficulty"),
                "type": str(item.get("type") or "").strip(),
                "content": content,
                "reference_ids": references,
            }
        )
    labels = validate_difficulty_items(items, question_count, quota)
    body_parts = []
    if prelude:
        body_parts.extend([r"\section*{基础知识预备}", knowledge_prelude_latex(prelude), ""])
    for item in sorted(items, key=lambda value: int(value["position"])):
        body_parts.extend(
            [
                r"\begin{problem}",
                item["content"],
                r"\end{problem}",
                "",
            ]
        )
    body = "\n".join(body_parts).strip()
    validate_generated_latex(body)
    return body, items, labels


def _validate_free_reference_ids(items, candidate_ids):
    known_ids = set(candidate_ids)
    if not known_ids:
        raise ValueError("自由组卷没有可校验的候选题ID")
    for item in items:
        position = int(item["position"])
        references = list(item.get("reference_ids") or [])
        if not references:
            raise ValueError(f"第{position}题没有返回参考题ID")
        if len(references) != len(set(references)):
            raise ValueError(f"第{position}题返回了重复的参考题ID")
        unknown = sorted(set(references) - known_ids)
        if unknown:
            raise ValueError(
                f"第{position}题引用了候选清单外的题目ID："
                + "、".join(unknown[:3])
            )


def audit_question_difficulties(question_texts, quota, subject, *, ai_call):
    messages = build_exam_difficulty_review_messages(
        subject=subject,
        question_texts=question_texts,
        quota=quota,
    )
    last_error = None
    for attempt in range(2):
        review_messages = list(messages)
        if attempt:
            review_messages.append({
                "role": "user",
                "content": (
                    "上次难度审核未通过本地 Schema。仅重新审核难度，"
                    "不要重新生成、改写或解答题目；只返回规定 JSON。"
                ),
            })
        raw = ai_call(
            review_messages,
            temperature=0.0,
            timeout=EXAM_AI_TIMEOUT_SECONDS,
            task="exam_difficulty_review",
            task_policy="exam_difficulty_review",
        )
        try:
            parsed = _parse_ai_json_object(raw)
            if set(parsed) != {"items"}:
                raise ValueError("独立难度审核顶层字段无效")
            items = parsed.get("items")
            if not isinstance(items, list):
                raise ValueError("独立难度审核题目字段无效")
            for expected_position, item in enumerate(items, start=1):
                if not isinstance(item, dict) or set(item) != {"position", "difficulty"}:
                    raise ValueError("独立难度审核题目字段无效")
                if (
                    isinstance(item.get("position"), bool)
                    or not isinstance(item.get("position"), int)
                    or item["position"] != expected_position
                    or not isinstance(item.get("difficulty"), str)
                ):
                    raise ValueError("独立难度审核题目类型无效")
            return validate_difficulty_items(
                items,
                len(question_texts),
                quota,
            )
        except (TypeError, ValueError) as exc:
            last_error = exc
    raise DifficultyAuditUnavailableError(
        "独立难度审核连续两次未通过本地 Schema，未重新生成试卷"
    ) from last_error


def generate_indexed_free_exam(
    sys_prompt,
    prompt,
    question_count,
    quota,
    subject,
    candidate_ids=(),
    allowed_question_types=(),
    allowed_chapters=(),
    *,
    ai_call,
    testing=False,
):
    messages = [
        {
            "role": "system",
            "content": (
                sys_prompt
                + " 必须只返回有效JSON，不得输出Markdown代码围栏。"
                " knowledge_prelude 和每个 latex 字段都必须是可直接嵌入"
                " TeX 正文的内容；所有数学表达式必须置于数学模式内，"
                "禁止在数学模式外出现未转义的 ^ 或 _。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    raw = ai_call(
        messages,
        temperature=0.7,
        timeout=EXAM_AI_TIMEOUT_SECONDS,
        task="exam_generation",
        task_policy="structured_exam",
    )
    try:
        body, items, labels = _parse_structured_free_exam(
            raw,
            question_count,
            quota,
            allowed_chapters,
        )
        if candidate_ids:
            _validate_free_reference_ids(items, candidate_ids)
        allowed_types = set(allowed_question_types)
        if allowed_types:
            for item in items:
                if item["type"] not in allowed_types:
                    raise ValueError(
                        f"第{int(item['position'])}题的题型不在所选范围内"
                    )
        items = sorted(items, key=lambda item: int(item["position"]))
        if not testing:
            audited = audit_question_difficulties(
                [item["content"] for item in items],
                quota,
                subject,
                ai_call=ai_call,
            )
            for item in items:
                item["difficulty"] = audited[int(item["position"]) - 1]
            labels = audited
        return body, items, labels
    except (DeepSeekConfigurationError, DeepSeekServiceError, JobError):
        raise
    except DifficultyAuditUnavailableError:
        raise
    except Exception as exc:
        if testing:
            try:
                validate_generated_latex(raw)
                begin_count, end_count = _problem_environment_counts(raw)
                if begin_count == question_count and end_count == question_count:
                    labels = _difficulty_sequence(quota)
                    contents = tuple(
                        normalize_generated_latex(match.group(1)).strip()
                        for match in _PROBLEM_ENVIRONMENT.finditer(raw)
                    )
                    if len(contents) == question_count and all(contents):
                        return raw, [
                            {
                                "position": index,
                                "chapter": "",
                                "difficulty": label,
                                "type": "",
                                "content": contents[index - 1],
                                "reference_ids": [],
                            }
                            for index, label in enumerate(labels, start=1)
                        ], labels
            except Exception:
                pass
        raise RawDraftValidationError(
            f"试卷草稿未通过本地组卷校验：{_safe_error_text(exc)}"
        ) from exc


