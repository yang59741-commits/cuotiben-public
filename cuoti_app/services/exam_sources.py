"""Source snapshots, candidate freezing, and material understanding for exams."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .exam_contracts import ExamGenerationConstraintError
from .past_exams import PastExamSourceError
from .question_bank import QuestionBankError


class BackgroundJobLookupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def require_current_question_bank_snapshot(expected_snapshot, *, index_loader):
    current = index_loader()
    if current.snapshot_id != expected_snapshot:
        raise QuestionBankError(
            "题库索引在AI组卷期间发生变化，本卷已回滚；请重新提交组卷任务"
        )
    return current


def require_current_past_exam_snapshot(
    expected_snapshot, subject, *, snapshot_loader
):
    current = snapshot_loader(subject)
    if current != expected_snapshot:
        raise PastExamSourceError(
            "正式真题在AI组卷期间发生变化，本卷已回滚；请重新提交组卷任务"
        )
    return current


def indexed_candidates(
    index,
    subject,
    chapters,
    question_count,
    seed,
    *,
    chapter_quotas=None,
    token_budget=None,
    candidate_multiplier=4,
    coverage_result=None,
):
    if isinstance(candidate_multiplier, bool) or not isinstance(candidate_multiplier, int) or not 1 <= candidate_multiplier <= 10:
        raise QuestionBankError("候选倍数必须在 1 到 10 之间")
    # Generation callers often need coverage for validation before selecting
    # candidates.  Reuse that immutable-snapshot result when supplied instead
    # of opening and scanning the index a second time.
    coverage = (
        coverage_result
        if coverage_result is not None
        else index.coverage(subject=subject, chapters=chapters)
    )
    if coverage.get("errors"):
        first = coverage["errors"][0]
        raise QuestionBankError(
            f"题库文件不可用：{first.get('source_file')} "
            f"{first.get('error') or first.get('status')}"
        )
    available = int(coverage.get("total_questions") or 0)
    if available < question_count:
        raise ExamGenerationConstraintError(
            f"有效题库题目不足：需要{question_count}题，当前只有{available}题",
            code="candidate_pool_insufficient",
            hint="请减少题量、调整章节，或重建题库后再试",
            actions=("reduce_question_count", "adjust_chapters", "rebuild_question_bank"),
            details={"required": int(question_count), "available": available},
        )
    source_multiplier = max(8, candidate_multiplier)
    target = max(question_count * source_multiplier, len(chapters) * 4)
    minimum = max(question_count * 4, len(chapters))
    requested = min(target, available)
    scaled_quotas = None
    if chapter_quotas:
        scaled_quotas = {
            chapter: max(4, int(count) * source_multiplier)
            for chapter, count in chapter_quotas.items()
        }
        requested = min(sum(scaled_quotas.values()), available)
    candidates = index.select_candidates(
        subject,
        chapters,
        requested,
        seed,
        chapter_quotas=scaled_quotas,
        token_budget=token_budget,
    )
    required = min(minimum, available)
    if len(candidates) < required:
        raise ExamGenerationConstraintError(
            "完整候选题数量不足："
            f"至少需要{required}题，当前只有{len(candidates)}题",
            code="candidate_pool_insufficient",
            hint="请减少题量、调整章节，或重建题库后再试",
            actions=("reduce_question_count", "adjust_chapters"),
            details={"required": required, "available": len(candidates)},
        )
    return candidates, coverage


def remove_exam_artifacts(exam_ids, *, pdf_dir, warn=None):
    warn = warn or (lambda *_args: None)
    output_dir = Path(pdf_dir)
    for exam_id in exam_ids:
        for suffix in (".pdf", ".tex", ".aux", ".log", ".out"):
            try:
                (output_dir / f"exam_{exam_id}{suffix}").unlink()
            except FileNotFoundError:
                pass
            except OSError:
                warn("无法清理失败试卷产物 exam_%s%s", exam_id, suffix)


def safe_error_text(error, *, secrets=()):
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "[已隐藏]")
    return text[:3000]


def has_background_job(database_connection, job_id):
    if not job_id:
        return False
    try:
        row = database_connection.execute(
            "SELECT 1 FROM background_jobs WHERE id=?", (job_id,)
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise BackgroundJobLookupError(
            "BACKGROUND_JOB_LOOKUP_FAILED",
            "无法确认后台任务状态，请稍后重试",
        ) from exc
    return row is not None


def parse_ai_json_object(
    raw,
    *,
    error_message="AI 服务未返回有效的结构分析 JSON",
):
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        raise ValueError(error_message)
    return parsed
