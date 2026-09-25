"""API routes for daily self-review sessions and recommendations."""

from __future__ import annotations

import uuid

from flask import Blueprint, current_app, jsonify, request, url_for

from ..database import get_db
from ..pagination import bounded_limit
from ..services.daily_review import (
    DailyReviewError,
    current_round,
    get_round,
    list_rounds,
    record_result,
    recommendations,
    validate_selection,
    history_page,
    load_daily_review_pdf_plan,
    type_summary,
)
from ..services.jobs import (
    JobConflict,
    JobCoordinator,
    UnknownJobKind,
)
from ..services.learning_evidence import remediation_status


bp = Blueprint("daily_review", __name__)


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


def _job_response(job: dict, status: int = 202):
    body = {"job": job, **job, "job_id": job["id"], "state": job["status"]}
    response = jsonify(body)
    response.status_code = status
    response.headers["Location"] = url_for("jobs.get_job", job_id=job["id"])
    return response


def _error(message: str, status: int, code: str):
    return jsonify({"error": message, "code": code}), status


def _json_body() -> dict:
    if not request.is_json:
        raise DailyReviewError("请求体必须是 JSON 对象", code="INVALID_REQUEST", status=415)
    value = request.get_json(silent=True)
    if not isinstance(value, dict):
        raise DailyReviewError("请求体必须是 JSON 对象", code="INVALID_REQUEST")
    return value


def _result_key(body: dict) -> str | None:
    key = request.headers.get("Idempotency-Key") or body.get("idempotency_key") or body.get("client_event_id")
    if key in (None, ""):
        return None
    key = str(key).strip()
    if not 1 <= len(key) <= 200:
        raise DailyReviewError("幂等键长度无效", code="INVALID_IDEMPOTENCY_KEY")
    return key


@bp.get("/api/v2/daily-review/current")
def daily_review_current():
    subject_id = request.args.get("subject_id")
    try:
        subject = int(subject_id) if subject_id not in (None, "") else None
        if subject is not None and subject <= 0:
            raise ValueError
    except ValueError:
        return _error("科目编号无效", 400, "INVALID_REQUEST")
    value = current_round(get_db(), subject_id=subject, filters=request.args)
    return jsonify(value or {"session": None, "round": None, "items": []})


@bp.get("/api/v2/daily-review/sessions")
def daily_review_history():
    try:
        limit = bounded_limit(request.args.get("limit"), default=20, maximum=100)
    except ValueError as exc:
        return _error(str(exc), 400, "INVALID_REQUEST")
    try:
        return jsonify(history_page(get_db(), limit=limit, after=request.args.get('after')))
    except ValueError:
        return _error('历史分页参数无效', 400, 'INVALID_REQUEST')


@bp.get("/api/v2/daily-review/sessions/<int:round_id>")
def daily_review_session(round_id: int):
    value = get_round(get_db(), round_id, subject_id=request.args.get('subject_id', type=int), filters=request.args)
    if value is None:
        return _error("每日复习轮次不存在", 404, "ROUND_NOT_FOUND")
    return jsonify(value)


@bp.post("/api/v2/daily-review/generate")
def daily_review_generate():
    try:
        body = _json_body()
        layout = str(body.get("layout") or "compact")
        if layout not in {"compact", "notes", "two_column"}:
            raise DailyReviewError("PDF 布局无效", code="INVALID_LAYOUT")
        request_id = str(body.get("request_id") or body.get("generation_id") or uuid.uuid4().hex)
        if len(request_id) > 200:
            raise DailyReviewError("生成请求标识过长", code="INVALID_REQUEST")
        idempotency_key = request.headers.get("Idempotency-Key") or body.get("idempotency_key") or request_id
        payload = {
            "export_context": "daily_review",
            "layout": layout,
            "daily_review_request_id": request_id,
            "selection": validate_selection(get_db(), {key: body[key] for key in ('practice_mode','primary_type_code','subject_id','chapter_id','count','include_mastered','practice_kind','source_item_ids','pdf_purpose') if key in body}),
        }
        job, created = _coordinator().submit(
            "mistake-export",
            payload,
            idempotency_key=str(idempotency_key),
            max_attempts=int(body.get("max_attempts", 3)),
        )
    except DailyReviewError as exc:
        return _error(str(exc), exc.status, exc.code)
    except (UnknownJobKind, JobConflict, ValueError, TypeError) as exc:
        return _error(str(exc), 409 if isinstance(exc, JobConflict) else 400, "INVALID_REQUEST")
    response = _job_response(job, 202 if created else 200)
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return response


@bp.post("/api/v2/daily-review/sessions/<int:round_id>/pdf")
def daily_review_session_pdf(round_id: int):
    try:
        body = _json_body()
        if set(body) - {"layout", "request_id", "idempotency_key"}:
            raise DailyReviewError("历史批次导出只接受版式和请求标识", code="INVALID_REQUEST")
        layout = body.get("layout", "compact")
        if not isinstance(layout, str) or layout not in {"compact", "notes", "two_column"}:
            raise DailyReviewError("PDF 布局无效", code="INVALID_LAYOUT")
        header_key = request.headers.get("Idempotency-Key")
        body_key = body.get("idempotency_key") or body.get("request_id")
        if header_key and body_key and str(header_key).strip() != str(body_key).strip():
            raise DailyReviewError("幂等键不一致", code="INVALID_IDEMPOTENCY_KEY")
        key = str(header_key or body_key or uuid.uuid4().hex).strip()
        if not 1 <= len(key) <= 200:
            raise DailyReviewError("请求标识长度无效", code="INVALID_IDEMPOTENCY_KEY")
        load_daily_review_pdf_plan(get_db(), round_id)
        job, created = _coordinator().submit(
            "mistake-export",
            {"export_context": "daily_review", "daily_review_session_id": round_id, "layout": layout},
            idempotency_key=key,
        )
    except DailyReviewError as exc:
        return _error(str(exc), exc.status, exc.code)
    except JobConflict as exc:
        return _error(str(exc), 409, "idempotency_conflict")
    response = _job_response(job, 202 if created else 200)
    response.headers["Idempotency-Replayed"] = "false" if created else "true"
    return response


@bp.get('/api/v2/daily-review/type-summary')
def daily_review_type_summary():
    return jsonify({'items': type_summary(get_db(), session_id=request.args.get('session_id', type=int))})


def _record(round_id: int, item_id: int | None = None, mistake_id: int | None = None):
    try:
        body = _json_body()
        key = _result_key(body)
        result = body.get("result")
        value = record_result(
            get_db(),
            round_id=round_id,
            item_id=item_id,
            mistake_id=mistake_id,
            result=result,
            idempotency_key=key,
            assistance=body.get('assistance', 'unknown'),
        )
        return jsonify(value)
    except DailyReviewError as exc:
        return _error(str(exc), exc.status, exc.code)
    except (TypeError, ValueError) as exc:
        return _error(str(exc), 400, "INVALID_REQUEST")


@bp.post("/api/v2/daily-review/sessions/<int:round_id>/items/<int:item_id>/result")
def daily_review_item_result(round_id: int, item_id: int):
    return _record(round_id, item_id=item_id)


@bp.get("/api/v2/daily-review/recommendations")
def daily_review_recommendations():
    try:
        limit = bounded_limit(request.args.get("limit"), default=10, maximum=10)
        subject_id = int(request.args["subject_id"]) if request.args.get("subject_id") else None
        chapter_id = int(request.args["chapter_id"]) if request.args.get("chapter_id") else None
        if subject_id is not None and subject_id <= 0:
            raise ValueError
        if chapter_id is not None and chapter_id <= 0:
            raise ValueError
    except ValueError as exc:
        return _error("推荐筛选条件无效", 400, "INVALID_REQUEST")
    return jsonify(recommendations(get_db(), limit=limit, subject_id=subject_id, chapter_id=chapter_id))


@bp.get('/api/v2/daily-review/remediation')
def daily_remediation():
    return jsonify(remediation_status(get_db()))


__all__ = ["bp"]
