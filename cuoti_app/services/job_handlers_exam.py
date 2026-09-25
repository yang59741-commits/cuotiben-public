"""Exam and question-bank durable job handler factories."""

from __future__ import annotations

from ..database import get_db
from .exam_query import ExamRecompileError, recompile_published_exam
from .jobs import JobOutcome, PermanentJobError
from .question_bank import ensure_question_bank_index
from .semantic_index_refresh import maybe_submit_auto_semantic_refresh
from .tex import TexCompileError


def make_exam_domain_handlers(app, *, bounded_error_message):
    """Build handlers that publish deterministic exam-domain artifacts."""

    def exam_recompile_handler(context, payload):
        try:
            exam_id = int(payload.get("exam_id"))
        except (TypeError, ValueError) as exc:
            raise PermanentJobError("试卷 ID 无效", code="invalid_exam_id") from exc
        if exam_id <= 0:
            raise PermanentJobError("试卷 ID 无效", code="invalid_exam_id")
        try:
            with app.app_context():
                result = recompile_published_exam(
                    get_db(),
                    pdf_dir=app.config["PDF_DIR"],
                    command=app.config["XELATEX_COMMAND"],
                    exam_id=exam_id,
                    job_id=context.job_id,
                    timeout=app.config.get("XELATEX_TIMEOUT", 60),
                    runner=context.run_cancellable_process,
                    on_started=lambda: context.update_progress(
                        1, 3, "正在从冻结 LaTeX 源重新编译"
                    ),
                    on_compiled=lambda: context.update_progress(
                        2, 3, "正在发布新产物"
                    ),
                )
        except TexCompileError as exc:
            raise PermanentJobError(
                bounded_error_message(exc),
                code=getattr(exc, "code", "recompile_failed"),
            ) from exc
        except ExamRecompileError as exc:
            raise PermanentJobError(str(exc), code=exc.code) from exc
        if not result.idempotency_replayed:
            context.update_progress(3, 3, "重新编译已发布")
        return JobOutcome(
            result=result.job_payload(),
            exam_ids=(exam_id,),
            side_effect_committed=True,
        )

    def question_bank_rebuild_handler(context, payload):
        del payload
        context.update_progress(0, 3, "正在读取题库源文件")
        context.raise_if_cancelled()
        index = ensure_question_bank_index(
            app.config["TEX_BASE"],
            index_path=app.config.get("QUESTION_BANK_DATABASE")
            or app.config.get("QUESTION_BANK_INDEX"),
            catalog=app.extensions.get("catalog_service"),
            force=True,
            progress=context.update_progress,
        )
        auto_refresh = maybe_submit_auto_semantic_refresh(
            app,
            source_kind="ordinary",
            published_source_snapshot=index.snapshot_id,
        )
        context.update_progress(3, 3, "新索引已校验并原子发布")
        return JobOutcome(
            result={"snapshot_id": index.snapshot_id, "auto_refresh": auto_refresh},
            side_effect_committed=True,
        )

    return {
        "exam-recompile": exam_recompile_handler,
        "question-bank-rebuild": question_bank_rebuild_handler,
    }


__all__ = ["make_exam_domain_handlers"]
