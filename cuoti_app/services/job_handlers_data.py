"""Data-import durable job handler factory."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..database import get_db
from ..validation import ValidationError
from .import_planner import ImportPlanError
from .jobs import JobOutcome, PermanentJobError
from .job_results import load_business_result
from .legacy_import import LegacyImportError, import_legacy_backup
from .portable_import import PortableImportError
from .portable_import_commit import commit_portable_import


def make_data_import_handler(app, *, job_error_from_response):
    def data_import_handler(context, payload):
        staged_name = str(payload.get("staged_file") or "")
        if staged_name:
            if not re.fullmatch(r"[0-9a-f]{64}\.import", staged_name):
                raise PermanentJobError(
                    "导入暂存文件无效", code="import_file_invalid"
                )
            mode = str(payload.get("mode") or "merge")
            preview_token = str(payload.get("preview_token") or "")
            resolutions = payload.get("resolutions") or {}
            root = Path(app.config["JOB_INPUT_DIR"]).resolve()
            path = (root / staged_name).resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise PermanentJobError(
                    "导入暂存文件无效", code="import_file_invalid"
                ) from exc
            if (
                not path.is_file()
                or mode not in {"merge", "replace"}
                or not preview_token
                or not isinstance(resolutions, dict)
            ):
                raise PermanentJobError("导入任务输入无效", code="invalid_import")
            context.update_progress(0, 4, "正在校验预览 token 与暂存文件")
            try:
                with app.app_context():
                    body = commit_portable_import(
                        get_db(),
                        raw=path,
                        mode=mode,
                        preview_token=preview_token,
                        resolutions=resolutions,
                        database_path=app.config["DATABASE"],
                        attachment_dir=app.config["ATTACHMENT_DIR"],
                        recovery_dir=Path(app.config["DATABASE"]).resolve().parent
                        / "备份",
                        warn=app.logger.warning,
                    )
            except PortableImportError as exc:
                raise job_error_from_response(
                    {"error": str(exc), "code": exc.code}, exc.status_code
                ) from exc
            except ImportPlanError as exc:
                raise job_error_from_response(
                    {"error": str(exc), "code": exc.code}, 409
                ) from exc
            except (ValidationError, ValueError) as exc:
                raise job_error_from_response(
                    {"error": str(exc), "code": "VALIDATION"}, 400
                ) from exc
            except Exception as exc:
                raise job_error_from_response(
                    {
                        "error": "导入提交失败，数据与附件均未更改",
                        "code": "IMPORT_FAILED",
                    },
                    500,
                ) from exc
            context.update_progress(4, 4, "导入已提交")
            path.unlink(missing_ok=True)
            return JobOutcome(result=body, side_effect_committed=True)

        backup = payload.get("backup")
        mode = str(payload.get("mode") or "merge")
        if mode == "replace":
            with app.app_context():
                replay = load_business_result(get_db(), context.job_id, "data-import")
            if replay is not None:
                context.update_progress(4, 4, "导入已提交")
                return JobOutcome(
                    result={**replay, "idempotency_replayed": True},
                    side_effect_committed=True,
                )
            raise PermanentJobError(
                "替换导入必须重新使用当前预览流程", code="import_preview_required"
            )
        if not isinstance(backup, (dict, list)) or mode not in {"merge", "replace"}:
            raise PermanentJobError("导入任务输入无效", code="invalid_import")
        context.update_progress(0, 4, "正在校验导入文件")
        raw = json.dumps(backup, ensure_ascii=False).encode("utf-8")
        try:
            with app.app_context():
                result = import_legacy_backup(
                    get_db(),
                    raw=raw,
                    mode=mode,
                    source_job_id=context.job_id,
                    progress=context.update_progress,
                )
        except LegacyImportError as exc:
            raise job_error_from_response(
                {"error": str(exc)}, exc.status_code
            ) from exc
        context.update_progress(4, 4, "导入已提交")
        return JobOutcome(
            result=result.response_payload(), side_effect_committed=True
        )

    return data_import_handler


__all__ = ["make_data_import_handler"]
