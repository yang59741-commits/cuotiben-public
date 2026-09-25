"""Version 2 API contract for durable background jobs."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request, url_for

from ..errors import PDF_OCR_DISABLED_CODE, PDF_OCR_DISABLED_MESSAGE
from ..pagination import CursorError, bounded_limit
from ..database import get_db

from ..services.jobs import (
    JOB_KIND_POLICIES,
    SUPPORTED_JOB_KINDS,
    InvalidJobState,
    JobConflict,
    JobCoordinator,
    JobNotFound,
    TERMINAL_STATUSES,
    UnknownJobKind,
)
from ..services.exam_generation_profile_freeze import (
    ExamGenerationProfileFreezeError,
)
from ..services.past_exam_profile_availability import (
    D2_SOURCE_MODES,
    PROFILE_ABANDONED_CODE,
    d2_source_mode_status,
    load_d2_coverage,
)


bp = Blueprint("jobs", __name__)


class ImportPreviewRequired(ValueError):
    pass


def _coordinator() -> JobCoordinator:
    factory = current_app.config.get("JOB_COORDINATOR_FACTORY")
    if factory is not None:
        return factory()
    roots = current_app.config.get("JOB_STAGED_INPUT_ROOTS") or (
        current_app.config.get("JOB_INPUT_DIR"),
    )
    if isinstance(roots, str):
        roots = (roots,)
    return JobCoordinator(
        current_app.config["DATABASE"],
        staged_input_roots=tuple(roots),
    )


def _error(message: str, status: int, code: str):
    return jsonify({
        "error": message,
        "code": code,
        "retryable": status in {408, 425, 429, 500, 502, 503, 504},
        "correlation_id": getattr(g, "correlation_id", ""),
    }), status


def _abandoned_profile_submission_status(payload: object):
    """Mirror the source-page disposition for an unavailable past profile."""

    if not isinstance(payload, dict) or payload.get("source") != "past":
        return None
    institution = payload.get("institution")
    candidate_source = payload.get("candidate_source", "ordinary")
    reference_year = payload.get("reference_year")
    subject_id = payload.get("subject_id")
    if (
        not isinstance(institution, str)
        or not institution.strip()
        or not isinstance(candidate_source, str)
        or candidate_source not in D2_SOURCE_MODES
        or isinstance(reference_year, bool)
        or not isinstance(reference_year, int)
        or reference_year < 1
        or isinstance(subject_id, bool)
        or not isinstance(subject_id, int)
        or subject_id < 1
    ):
        return None
    try:
        row = get_db().execute(
            "SELECT name FROM subjects WHERE id=?", (subject_id,)
        ).fetchone()
        if row is None:
            return None
        coverage = load_d2_coverage(
            Path(current_app.config["DATABASE"]).parent
            / "runtime"
            / "d2-expansion-coverage.json"
        )
        status = d2_source_mode_status(
            coverage,
            institution=institution.strip(),
            subject=row["name"],
            reference_year=reference_year,
            mode=candidate_source,
            profile_available=False,
            source_available=True,
        )
        return status if status.get("reason_code") == PROFILE_ABANDONED_CODE else None
    except (OSError, TypeError, ValueError, KeyError):
        # A missing/old coverage projection must not weaken the normal freeze
        # contract or turn a submission error into a server error.
        return None


def _profile_freeze_error(
    exc: ExamGenerationProfileFreezeError,
    payload: object = None,
):
    abandoned = (
        _abandoned_profile_submission_status(payload)
        if exc.code == "unsupported_profile_combination"
        else None
    )
    if abandoned is not None:
        return jsonify({
            "error": abandoned.get("reason") or "画像建设已停止",
            "code": PROFILE_ABANDONED_CODE,
            "reason": PROFILE_ABANDONED_CODE,
            "retryable": False,
            "correlation_id": getattr(g, "correlation_id", ""),
        }), 422
    return jsonify({
        "error": str(exc),
        "code": exc.code,
        "reason": exc.reason,
        "retryable": exc.retryable,
        "correlation_id": getattr(g, "correlation_id", ""),
    }), exc.status_code


def _parse_json_object():
    if not request.is_json:
        return None, _error("请求体必须是 JSON 对象", 415, "unsupported_media_type")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return None, _error("请求体必须是 JSON 对象", 400, "invalid_request")
    return data, None


def _attach_staged_import_path(kind: str, payload):
    """Resolve the server-owned import staging path without trusting the client."""

    if kind != "data-import" or not isinstance(payload, dict):
        return payload
    staged_name = str(payload.get("staged_file") or "")
    if not staged_name:
        if payload.get("mode") == "replace":
            raise ImportPreviewRequired("替换导入必须先使用当前预览流程")
        return payload
    if not re.fullmatch(r"[0-9a-f]{64}\.import", staged_name):
        raise ValueError("导入暂存文件无效")
    root = Path(current_app.config["JOB_INPUT_DIR"]).resolve()
    staged_path = (root / staged_name).resolve()
    try:
        staged_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("导入暂存文件无效") from exc
    if not staged_path.is_file():
        raise ValueError("导入暂存文件不存在或已失效")
    return {**payload, "_staged_input_path": str(staged_path)}


def _stage_ocr_upload():
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return None, _error("请选择文件", 400, "file_required")
    suffix = Path(uploaded.filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".tex", ".txt"}:
        return None, _error("仅支持 PDF、DOCX、TeX 和 TXT", 400, "file_type")
    if suffix == ".pdf":
        return None, _error(
            PDF_OCR_DISABLED_MESSAGE,
            410,
            PDF_OCR_DISABLED_CODE,
        )
    root = Path(current_app.config["JOB_INPUT_DIR"])
    root.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    digest = hashlib.sha256()
    size = 0
    try:
        with tempfile.NamedTemporaryFile(dir=root, suffix=".upload", delete=False) as handle:
            temporary_path = Path(handle.name)
            while True:
                chunk = uploaded.stream.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > current_app.config["MAX_CONTENT_LENGTH"]:
                    return None, _error("文件过大", 413, "file_too_large")
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        final_name = digest.hexdigest() + suffix
        final_path = root / final_name
        if final_path.exists():
            temporary_path.unlink(missing_ok=True)
        else:
            os.replace(temporary_path, final_path)
        temporary_path = None
        return {
            "operation": "extract",
            "staged_file": final_name,
            "_staged_input_path": str(final_path.resolve()),
            "sha256": digest.hexdigest(),
            "original_name": Path(uploaded.filename).name[:200],
            "size": size,
        }, None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _job_response(job: dict, status: int = 200):
    response = jsonify(
        {
            "job": job,
            **job,
            "job_id": job["id"],
            "state": job["status"],
        }
    )
    response.status_code = status
    response.headers["Location"] = url_for("jobs.get_job", job_id=job["id"])
    return response


@bp.get("/api/v2/jobs/kinds")
def job_kinds():
    return jsonify({
        "kinds": sorted(SUPPORTED_JOB_KINDS),
        "policies": {
            kind: {"lane": lane, "priority": priority}
            for kind, (lane, priority) in sorted(JOB_KIND_POLICIES.items())
        },
    })


@bp.route("/api/v2/jobs", methods=["GET", "POST"])
def jobs_collection():
    coordinator = _coordinator()
    if request.method == "GET":
        try:
            limit = bounded_limit(request.args.get("limit"), default=50, maximum=200)
            requested_status = request.args.get("status") or None
            state = request.args.get("state") or None
            jobs = coordinator.list_jobs(
                status=requested_status,
                kind=request.args.get("kind") or None,
                limit=limit,
            )
            if state == "active":
                jobs = [
                    job
                    for job in jobs
                    if job["status"] in {"queued", "running", "cancel_requested"}
                ]
            elif state and state != requested_status:
                raise ValueError("任务状态筛选无效")
        except (CursorError, ValueError, UnknownJobKind) as exc:
            return _error(str(exc), 400, "invalid_request")
        return jsonify({"jobs": jobs})

    data, invalid = _parse_json_object()
    if invalid:
        return invalid
    kind = data.get("kind")
    payload = data.get("input", {})
    body_key = data.get("idempotency_key")
    header_key = request.headers.get("Idempotency-Key")
    if body_key and header_key and str(body_key).strip() != header_key.strip():
        return _error(
            "请求头与请求体中的幂等键不一致",
            400,
            "invalid_idempotency_key",
        )
    try:
        payload = _attach_staged_import_path(kind, payload)
        job, created = coordinator.submit(
            kind,
            payload,
            idempotency_key=header_key or body_key,
            max_attempts=data.get("max_attempts", 3),
        )
    except ImportPreviewRequired as exc:
        return _error(str(exc), 400, "import_preview_required")
    except UnknownJobKind as exc:
        return _error(str(exc), 400, "unknown_job_kind")
    except JobConflict as exc:
        return _error(str(exc), 409, "idempotency_conflict")
    except ExamGenerationProfileFreezeError as exc:
        return _profile_freeze_error(exc, payload)
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400, "invalid_request")
    response = _job_response(job, 202 if created else 200)
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return response


@bp.post("/api/v2/jobs/<kind>")
def create_job_by_kind(kind: str):
    if kind == "ocr" and request.files:
        payload, invalid = _stage_ocr_upload()
        if invalid:
            return invalid
        data = request.form.to_dict()
        body_key = data.get("idempotency_key")
    else:
        data, invalid = _parse_json_object()
        if invalid:
            return invalid
        payload = data.get("input", data.get("payload", data))
        body_key = data.get("idempotency_key")
        if payload is data:
            payload = {
                key: value
                for key, value in data.items()
                if key not in {"idempotency_key", "max_attempts"}
            }
    header_key = request.headers.get("Idempotency-Key")
    if body_key and header_key and str(body_key).strip() != header_key.strip():
        return _error("幂等键不一致", 400, "invalid_idempotency_key")
    try:
        payload = _attach_staged_import_path(kind, payload)
        job, created = _coordinator().submit(
            kind,
            payload,
            idempotency_key=header_key or body_key,
            max_attempts=data.get("max_attempts", 3),
        )
    except ImportPreviewRequired as exc:
        return _error(str(exc), 400, "import_preview_required")
    except UnknownJobKind as exc:
        return _error(str(exc), 400, "unknown_job_kind")
    except JobConflict as exc:
        return _error(str(exc), 409, "idempotency_conflict")
    except ExamGenerationProfileFreezeError as exc:
        return _profile_freeze_error(exc, payload)
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400, "invalid_request")
    response = _job_response(job, 202 if created else 200)
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return response


@bp.get("/api/v2/jobs/<job_id>")
def get_job(job_id: str):
    try:
        job = _coordinator().get_job(job_id)
    except JobNotFound as exc:
        return _error(str(exc), 404, "job_not_found")
    return _job_response(job)


@bp.get("/api/v2/jobs/<job_id>/result")
def get_job_result(job_id: str):
    try:
        job = _coordinator().get_job(job_id)
    except JobNotFound as exc:
        return _error(str(exc), 404, "job_not_found")
    if job["status"] == "succeeded":
        return jsonify({"job_id": job_id, "result": job["result"], "exam_ids": job["exam_ids"]})
    if job["status"] in TERMINAL_STATUSES:
        return _error("任务没有可用结果", 410, "job_result_unavailable")
    return _error("任务尚未完成", 409, "job_not_complete")


@bp.get("/api/v2/jobs/<job_id>/attempts")
def get_job_attempts(job_id: str):
    try:
        attempts = _coordinator().list_attempts(job_id)
    except JobNotFound as exc:
        return _error(str(exc), 404, "job_not_found")
    return jsonify({"job_id": job_id, "attempts": attempts})


@bp.post("/api/v2/jobs/<job_id>/cancel")
def cancel_job(job_id: str):
    try:
        job = _coordinator().request_cancel(job_id)
    except JobNotFound as exc:
        return _error(str(exc), 404, "job_not_found")
    return _job_response(job, 202 if job["status"] == "cancel_requested" else 200)


@bp.post("/api/v2/jobs/<job_id>/retry")
def retry_job(job_id: str):
    try:
        job = _coordinator().retry(job_id)
    except JobNotFound as exc:
        return _error(str(exc), 404, "job_not_found")
    except InvalidJobState as exc:
        return _error(str(exc), 409, "invalid_job_state")
    return _job_response(job, 202)
