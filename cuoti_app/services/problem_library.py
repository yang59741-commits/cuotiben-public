"""Application services for writing classified problems into the TeX library."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from ..validation import ValidationError, subject_chapter, text_value
from .ai import DeepSeekConfigurationError
from .batch_mistakes import (
    SourceSplitReviewRequired,
    classify_known_problem_blocks,
    classify_problem_text,
    deterministic_source_blocks,
)
from .tex import append_problems, load_append_operation


class ProblemLibraryError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, code: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _ensure_fallback_file(tex_base: str | Path, target: str) -> None:
    root = Path(tex_base).resolve()
    path = (root / target).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValidationError("综合题目目标路径无效") from exc
    if path.exists():
        return
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=root
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\\documentclass{ctexart}\n\\begin{document}\n\\end{document}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def add_classified_problems(
    db,
    *,
    content: object,
    tex_base: str | Path,
    chapter_sources: dict,
    fallback_source,
    classifier,
    compile_check=None,
    source_job_id: str | None = None,
) -> dict:
    """Classify one text batch and atomically append all valid problem sources."""

    try:
        text = text_value(content, "题目内容", maximum=6000, required=True)
    except ValidationError as exc:
        raise ProblemLibraryError(str(exc), status_code=400, code="VALIDATION") from exc
    root = Path(tex_base)
    if not str(tex_base).strip():
        raise ProblemLibraryError(
            "未配置普通试题库目录，请设置 CUOTI_TEX_BASE",
            status_code=503,
            code="configuration",
        )
    if not root.is_dir():
        raise ProblemLibraryError(
            "普通试题库目录不存在，请检查 CUOTI_TEX_BASE",
            status_code=503,
            code="configuration",
        )
    if source_job_id is not None and not re.fullmatch(r"[0-9a-f]{32}", source_job_id):
        raise ProblemLibraryError(
            "任务编号无效", status_code=400, code="VALIDATION"
        )
    operation_input_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if source_job_id is not None:
        try:
            replay = load_append_operation(
                root, source_job_id, input_hash=operation_input_hash
            )
        except ValueError as exc:
            raise ProblemLibraryError(
                str(exc), status_code=409, code="operation_conflict"
            ) from exc
        if replay is not None:
            return {
                "ok": True,
                "count": len(replay),
                "saved": [dict(item["result"], num=int(item["num"])) for item in replay],
                "idempotency_replayed": True,
            }
    chapters = [
        dict(row)
        for row in db.execute(
            "SELECT c.id,c.name,c.subject_id,s.name as subject FROM chapters c "
            "JOIN subjects s ON c.subject_id=s.id"
        ).fetchall()
    ]
    chapter_list = "\n".join(
        f'  {chapter["id"]}: [{chapter["subject"]}] {chapter["name"]}'
        for chapter in chapters
    )
    try:
        source_blocks = deterministic_source_blocks(text)
        if len(source_blocks) > 100:
            raise ProblemLibraryError(
                "一次最多添加 100 道题", status_code=400, code="VALIDATION"
            )
        problems = (
            classify_known_problem_blocks(
                classifier,
                source_blocks,
                chapter_list,
            )
            if source_blocks
            else classify_problem_text(classifier, text, chapter_list, None)
        )
        if not isinstance(problems, list) or not problems:
            raise ProblemLibraryError(
                "No problems identified", status_code=400, code="VALIDATION"
            )
        if len(problems) > 100:
            raise ProblemLibraryError(
                "一次最多添加 100 道题", status_code=400, code="VALIDATION"
            )
        prepared = []
        for index, problem in enumerate(problems, start=1):
            if not isinstance(problem, dict):
                raise ValidationError(f"第 {index} 题格式无效")
            subject, chapter = subject_chapter(
                db,
                problem.get("subject_id"),
                None if problem.get("chapter_id") in (None, "", 0, "0") else problem.get("chapter_id"),
            )
            problem_content = text_value(
                problem.get("content"),
                f"第 {index} 题内容",
                maximum=100_000,
                required=True,
            )
            key = (subject["name"], chapter["name"]) if chapter else None
            tex_files = chapter_sources.get(key, []) if key else []
            target = tex_files[0] if tex_files else fallback_source(subject["name"])
            if not tex_files:
                _ensure_fallback_file(root, target)
            if not (root / target).is_file():
                raise ValidationError(f"第 {index} 题的目标文件不存在: {target}")
            prepared.append((problem, subject, chapter, problem_content, target))
        operation_metadata = [
            {
                "filename": target,
                "chapter": chapter["name"] if chapter else "综合",
                "sid": subject["id"],
                "cid": chapter["id"] if chapter else 0,
            }
            for problem, subject, chapter, _content, target in prepared
        ]
        assigned_numbers = append_problems(
            root,
            [(target, problem_content) for _, _, _, problem_content, target in prepared],
            compile_check=compile_check,
            operation_id=source_job_id,
            operation_input_hash=(operation_input_hash if source_job_id else None),
            operation_metadata=operation_metadata,
        )
    except ProblemLibraryError:
        raise
    except SourceSplitReviewRequired as exc:
        raise ProblemLibraryError(
            str(exc), status_code=409, code="split_review_required"
        ) from exc
    except DeepSeekConfigurationError as exc:
        raise ProblemLibraryError(
            str(exc), status_code=503, code="configuration"
        ) from exc
    except ValidationError as exc:
        raise ProblemLibraryError(str(exc), status_code=400, code="VALIDATION") from exc
    except Exception as exc:
        raise ProblemLibraryError(
            "批量题库写入失败，请检查服务配置或稍后重试",
            status_code=502,
            code="service_failure",
        ) from exc

    saved = [
        {
            "filename": target,
            "num": number,
            "chapter": chapter["name"] if chapter else "综合",
            "sid": subject["id"],
            "cid": chapter["id"] if chapter else 0,
        }
        for (problem, subject, chapter, _content, target), number in zip(
            prepared, assigned_numbers
        )
    ]
    return {"ok": True, "count": len(saved), "saved": saved}
