"""Versioned mistake attachment API."""

from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_from_directory

from ..database import get_db
from ..services.attachments import (
    AttachmentError,
    MAX_FILE_BYTES,
    publish_attachment,
    purge_deleted_mistake,
)
from ..validation import ValidationError, positive_int
bp = Blueprint("attachments", __name__)


def _uuid_value(value, label: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise ValidationError(f"{label}必须是 UUID")


def _error(exc: AttachmentError):
    return jsonify({"error": str(exc), "code": exc.code}), exc.status_code


@bp.route("/api/v2/mistakes/<public_id>/attachments", methods=["GET", "POST"])
def mistake_attachments(public_id):
    try:
        public_id = _uuid_value(public_id, "错题标识")
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    db = get_db()
    if request.method == "GET":
        rows = db.execute(
            "SELECT a.public_id,a.mime_type,a.extension,a.byte_size,a.width,a.height,"
            "a.sha256,a.sort_order,a.created_at FROM attachments a "
            "JOIN mistakes m ON m.id=a.mistake_id WHERE m.public_id=? "
            "AND m.deleted_at IS NULL AND a.status='published' ORDER BY a.sort_order,a.id",
            (public_id,),
        ).fetchall()
        return jsonify({"items": [dict(row) for row in rows]})
    try:
        revision = positive_int(request.form.get("revision"), "版本号")
        upload = request.files.get("file")
        if upload is None:
            raise AttachmentError("VALIDATION", "请选择附件")
        data = upload.stream.read(MAX_FILE_BYTES + 1)
        result = publish_attachment(
            db,
            Path(current_app.config["ATTACHMENT_DIR"]),
            public_id,
            revision,
            data,
        )
        return jsonify(result), 201
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except AttachmentError as exc:
        return _error(exc)


@bp.get("/api/v2/attachments/<attachment_id>/content")
def attachment_content(attachment_id):
    try:
        attachment_id = _uuid_value(attachment_id, "附件标识")
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    row = get_db().execute(
        "SELECT a.object_key,a.mime_type FROM attachments a "
        "JOIN mistakes m ON m.id=a.mistake_id WHERE a.public_id=? "
        "AND a.status='published' AND m.deleted_at IS NULL",
        (attachment_id,),
    ).fetchone()
    if not row:
        return jsonify({"error": "附件不存在", "code": "NOT_FOUND"}), 404
    return send_from_directory(
        current_app.config["ATTACHMENT_DIR"], row["object_key"], mimetype=row["mime_type"]
    )


@bp.delete("/api/v2/trash/mistakes/<public_id>")
def purge_mistake(public_id):
    try:
        public_id = _uuid_value(public_id, "错题标识")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("confirm") is not True:
            raise ValidationError("永久删除必须明确确认")
        revision = positive_int(payload.get("revision"), "版本号")
        purge_deleted_mistake(
            get_db(),
            Path(current_app.config["ATTACHMENT_DIR"]),
            public_id,
            revision,
        )
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except AttachmentError as exc:
        return _error(exc)
    return jsonify({"ok": True, "public_id": public_id, "permanently_deleted": True})
