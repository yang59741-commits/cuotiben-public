"""Document extraction and export durable job handler factories."""

from __future__ import annotations

import re
from pathlib import Path

from ..database import get_db
from .ai import DeepSeekConfigurationError, DeepSeekServiceError, deepseek_api_call
from .ai_providers import CallbackCancellationToken
from .jobs import JobCancelled, JobOutcome, PermanentJobError, RetryableJobError
from .mistake_exports import MistakeExportError, cleanup_exports, create_mistake_export
from .ocr import LocalDocumentExtractionError, extract_local_document


def make_document_handlers(app, *, job_error_from_response):
    def mistake_export_handler(context, payload):
        request_payload = {
            key: value for key, value in payload.items() if key != "operation"
        }
        request_payload["_job_id"] = context.job_id
        context.update_progress(0, 4, "正在校验输入")
        try:
            with app.app_context():
                try:
                    cleanup_exports(get_db(), app.config["PDF_DIR"])
                except Exception:
                    app.logger.exception("错题 PDF 导出清理失败，将在后续请求重试")
                result, _replayed = create_mistake_export(
                    get_db(),
                    pdf_dir=app.config["PDF_DIR"],
                    xelatex_command=app.config["XELATEX_COMMAND"],
                    payload=request_payload,
                    timeout=app.config.get("XELATEX_TIMEOUT", 60),
                    progress=context.update_progress,
                    runner=context.run_cancellable_process,
                    cancel_check=context.raise_if_cancelled,
                    cancelled_errors=(JobCancelled,),
                    warn=app.logger.warning,
                )
        except MistakeExportError as exc:
            raise job_error_from_response(
                {"error": str(exc), "code": exc.code}, exc.status_code
            ) from exc
        context.coordinator.update_progress(
            context.job_id,
            current=4,
            total=4,
            message="全部分册已发布",
            lease_attempt=context.lease_attempt,
        )
        return JobOutcome(result=result, side_effect_committed=True)

    def ocr_handler(context, payload):
        name = str(payload.get("staged_file") or "")
        if not re.fullmatch(r"[0-9a-f]{64}\.(?:pdf|docx|tex|txt)", name):
            raise PermanentJobError("OCR 暂存文件无效", code="ocr_file_invalid")
        root = Path(app.config["JOB_INPUT_DIR"]).resolve()
        path = (root / name).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise PermanentJobError(
                "OCR 暂存文件无效", code="ocr_file_invalid"
            ) from exc
        if not path.is_file():
            raise PermanentJobError("OCR 暂存文件不存在", code="ocr_file_missing")
        context.update_progress(1, 4, "输入校验完成")
        is_pdf = name.endswith(".pdf")
        progress_state = {"total": 4}
        cancellation = CallbackCancellationToken(context.is_cancel_requested)

        def report_progress(completed, total, message):
            progress_state["total"] = total
            context.update_progress(completed, total, message)

        context.update_progress(
            1,
            4,
            "PDF 已暂存，正在本地分页渲染" if is_pdf else "文档已暂存，正在本地提取",
        )
        try:
            with app.app_context():
                body = extract_local_document(
                    path,
                    filename=str(payload.get("original_name") or name),
                    pdf_recognizer=lambda messages, _page, _total: deepseek_api_call(
                        messages,
                        temperature=0.0,
                        timeout=300,
                        model=app.config["DEEPSEEK_VISION_MODEL"],
                        max_attempts=3,
                        task="ocr",
                        thinking={"type": "disabled"},
                        reasoning_effort="low",
                        freeze_id=context.job_id,
                        cancellation=cancellation,
                    ),
                    progress=report_progress,
                    raise_if_cancelled=context.raise_if_cancelled,
                )
        except LocalDocumentExtractionError as exc:
            raise job_error_from_response(
                {"error": str(exc), "code": exc.code}, exc.status_code
            ) from exc
        except DeepSeekConfigurationError as exc:
            raise PermanentJobError(
                str(exc), code="vision_ocr_configuration", category="configuration"
            ) from exc
        except DeepSeekServiceError as exc:
            if exc.retryable:
                raise RetryableJobError(
                    "DeepSeek 视觉识别暂时不可用，请稍后重试",
                    code="vision_ocr_provider",
                    delay_seconds=2,
                ) from exc
            raise PermanentJobError(
                "DeepSeek 视觉识别请求被拒绝，请检查模型与 Provider 配置",
                code="vision_ocr_provider",
                category="provider",
            ) from exc
        final_total = int(progress_state["total"])
        context.update_progress(
            final_total,
            final_total,
            "DeepSeek 视觉识别已完成" if is_pdf else "本地提取已完成",
        )
        path.unlink(missing_ok=True)
        return JobOutcome(result=body)

    return {"mistake-export": mistake_export_handler, "ocr": ocr_handler}


__all__ = ["make_document_handlers"]
