"""Versioned statistics and reminder endpoints."""

from flask import Blueprint, jsonify, request

from ..database import get_db
from ..services.statistics import (
    StatisticsValidationError,
    build_reminders,
    build_statistics,
    parse_scope,
)


bp = Blueprint("statistics_v2", __name__)


def _scope_or_error():
    try:
        return parse_scope(request.args), None
    except StatisticsValidationError as exc:
        return None, (jsonify({"error": str(exc), "code": "VALIDATION"}), 400)


@bp.get("/api/v2/stats")
def api_v2_stats():
    scope, error = _scope_or_error()
    if error:
        return error
    return jsonify(build_statistics(get_db(), scope))


@bp.get("/api/v2/stats/reminders")
def api_v2_stats_reminders():
    scope, error = _scope_or_error()
    if error:
        return error
    return jsonify(build_reminders(get_db(), scope))
