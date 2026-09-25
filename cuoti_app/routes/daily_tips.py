"""Daily-card content and independent daily-tip CRUD API."""

from __future__ import annotations

import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..catalog import MATH_WORDS, QUOTES
from ..database import get_db
from ..services.daily_tips import (
    DailyTipError,
    create_daily_tip,
    daily_tip_for_date,
    delete_daily_tip,
    list_daily_tips,
    update_daily_tip,
)
from ..validation import ValidationError, json_object, positive_int


bp = Blueprint("daily_tips", __name__)


def _uuid_value(value: object) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("每日提示标识必须是 UUID") from exc


def _error_response(exc: DailyTipError):
    body = {"error": str(exc), "code": exc.code}
    body.update(exc.details)
    return jsonify(body), exc.status_code


@bp.get("/api/daily")
def api_daily():
    today = datetime.now().date()
    seed = today.year * 10000 + today.month * 100 + today.day
    word = MATH_WORDS[seed % len(MATH_WORDS)]
    quote = QUOTES[seed % len(QUOTES)]
    return jsonify(
        {
            "word": word[0],
            "word_desc": word[1],
            "quote": quote,
            "tip": daily_tip_for_date(get_db(), today),
        }
    )


@bp.route("/api/v2/daily-tips", methods=["GET", "POST"])
def daily_tip_collection():
    if request.method == "GET":
        try:
            limit = positive_int(request.args.get("limit") or 20, "每页数量")
            before_id = positive_int(
                request.args.get("before_id"), "分页位置", optional=True
            )
            return jsonify(
                list_daily_tips(get_db(), limit=limit, before_id=before_id)
            )
        except ValidationError as exc:
            return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
        except DailyTipError as exc:
            return _error_response(exc)

    try:
        payload = json_object(request)
        result = create_daily_tip(get_db(), payload.get("content"))
        return jsonify(result), 201
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except DailyTipError as exc:
        return _error_response(exc)


@bp.route("/api/v2/daily-tips/<public_id>", methods=["PATCH", "DELETE"])
def daily_tip_item(public_id: str):
    try:
        public_id = _uuid_value(public_id)
        payload = json_object(request)
        revision = positive_int(payload.get("revision"), "版本号")
        if request.method == "PATCH":
            return jsonify(
                update_daily_tip(
                    get_db(),
                    public_id,
                    content=payload.get("content"),
                    revision=revision,
                )
            )
        delete_daily_tip(get_db(), public_id, revision=revision)
        return jsonify({"ok": True, "public_id": public_id})
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except DailyTipError as exc:
        return _error_response(exc)
