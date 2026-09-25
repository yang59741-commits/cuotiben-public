"""Read-only visibility endpoints for the derived question-bank index."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..database import get_db
from ..services.question_bank import (
    QuestionBankError,
    ensure_question_bank_index,
    open_last_good_question_bank_index,
    question_bank_status,
)
from ..services.jobs import JobCoordinator


bp = Blueprint("question_bank", __name__)


def _index_path():
    return current_app.config.get("QUESTION_BANK_DATABASE") or current_app.config.get(
        "QUESTION_BANK_INDEX"
    )


def _rebuild_job(status: dict, *, force: bool = False):
    coordinator = JobCoordinator(current_app.config["DATABASE"])
    key = f"question-bank:{status['source_snapshot']}"
    if force:
        key += f":manual:{request.headers.get('Idempotency-Key') or 'default'}"
    job, _created = coordinator.submit(
        "question-bank-rebuild",
        {"requested_source_snapshot": status["source_snapshot"]},
        idempotency_key=key,
        max_attempts=2,
    )
    return job


def current_question_bank_index():
    tex_base = current_app.config["TEX_BASE"]
    catalog = current_app.extensions["catalog_service"]
    status = question_bank_status(tex_base, index_path=_index_path(), catalog=catalog)
    last_good = open_last_good_question_bank_index(_index_path())
    if status["stale"]:
        _rebuild_job(status)
    if last_good is not None:
        return last_good
    if current_app.config.get("TESTING"):
        return ensure_question_bank_index(
            tex_base, index_path=_index_path(), catalog=catalog
        )
    raise QuestionBankError("题库索引正在后台构建，请稍后重试")


@bp.get("/api/v2/question-bank/status")
def api_question_bank_status():
    try:
        status = question_bank_status(
            current_app.config["TEX_BASE"],
            index_path=_index_path(),
            catalog=current_app.extensions.get("catalog_service"),
        )
        row = get_db().execute(
            "SELECT id,status,error_message,updated_at FROM background_jobs "
            "WHERE kind='question-bank-rebuild' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        latest = dict(row) if row else None
        status["rebuilding"] = bool(
            latest and latest["status"] in {"queued", "running", "cancel_requested"}
        )
        status["error"] = (
            latest.get("error_message")
            if latest and latest["status"] == "failed"
            else None
        )
        status["latest_job"] = latest
        return jsonify(status)
    except QuestionBankError as exc:
        return jsonify({"error": str(exc)}), 503


@bp.post("/api/v2/question-bank/rebuild")
def api_rebuild_question_bank():
    try:
        status = question_bank_status(
            current_app.config["TEX_BASE"],
            index_path=_index_path(),
            catalog=current_app.extensions.get("catalog_service"),
        )
        job = _rebuild_job(status, force=True)
        return jsonify({"job": job, "job_id": job["id"]}), 202
    except QuestionBankError as exc:
        return jsonify({"error": str(exc)}), 503


def _positive_id(raw, label):
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if value < 1:
        raise ValueError(f"{label}必须是正整数")
    return value


@bp.get("/api/v2/question-bank/coverage")
def api_question_bank_coverage():
    tex_base = str(current_app.config.get("TEX_BASE") or "").strip()
    if not tex_base:
        return jsonify({"error": "未配置普通试题库目录"}), 503
    db = get_db()
    subject = str(request.args.get("subject") or "").strip() or None
    resolved_subject_id = None
    chapters = []
    for value in request.args.getlist("chapter"):
        chapters.extend(
            item.strip() for item in value.split(",") if item.strip()
        )
    raw_subject_id = request.args.get("subject_id")
    raw_chapter_ids = request.args.getlist("chapter_ids")
    try:
        if raw_subject_id not in (None, ""):
            resolved_subject_id = _positive_id(raw_subject_id, "科目 ID")
            subject_row = db.execute(
                "SELECT id,name FROM subjects WHERE id=?",
                (resolved_subject_id,),
            ).fetchone()
            if not subject_row:
                raise ValueError("科目不存在")
            if subject and subject != subject_row["name"]:
                raise ValueError("科目 ID 与科目名称不一致")
            subject = subject_row["name"]

        chapter_id_values = []
        for raw_value in raw_chapter_ids:
            chapter_id_values.extend(
                _positive_id(item.strip(), "章节 ID")
                for item in raw_value.split(",")
                if item.strip()
            )
        chapter_id_values = list(dict.fromkeys(chapter_id_values))
        if len(chapter_id_values) > 50:
            raise ValueError("一次最多查询 50 个章节")
        if chapter_id_values:
            if resolved_subject_id is None:
                raise ValueError("使用 chapter_ids 时必须同时提供 subject_id")
            rows = db.execute(
                "SELECT id,name,subject_id FROM chapters WHERE id IN ({})".format(
                    ",".join("?" for _ in chapter_id_values)
                ),
                chapter_id_values,
            ).fetchall()
            by_id = {row["id"]: row for row in rows}
            if len(by_id) != len(chapter_id_values):
                raise ValueError("部分章节不存在")
            if any(
                row["subject_id"] != resolved_subject_id
                for row in by_id.values()
            ):
                raise ValueError("部分章节不属于所选科目")
            chapters = [by_id[chapter_id]["name"] for chapter_id in chapter_id_values]
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    try:
        index = current_question_bank_index()
        result = index.coverage(subject=subject, chapters=chapters)
        chapter_catalog = {
            (row["subject"], row["name"]): {
                "chapter_id": row["id"],
                "subject_id": row["subject_id"],
            }
            for row in db.execute(
                "SELECT c.id,c.name,c.subject_id,s.name AS subject "
                "FROM chapters c JOIN subjects s ON s.id=c.subject_id"
            ).fetchall()
        }
        for item in result["chapters"]:
            identifiers = chapter_catalog.get(
                (item["subject"], item["chapter"]), {}
            )
            item.update(identifiers)
        if resolved_subject_id is not None:
            result["subject_id"] = resolved_subject_id
        return jsonify(result)
    except QuestionBankError as exc:
        return jsonify({"error": str(exc)}), 503
