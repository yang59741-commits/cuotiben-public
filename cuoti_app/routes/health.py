"""Stable runtime health and application identity endpoint."""

import os

from flask import Blueprint, current_app, jsonify, request

from ..services.health import collect_runtime_health


bp = Blueprint("health", __name__)


@bp.get("/api/v2/health")
def health():
    force = request.args.get("refresh") == "1"
    payload = collect_runtime_health(current_app._get_current_object(), force=force)
    if current_app.config.get("SERVICE_INSTANCE_ID"):
        payload["service_instance_id"] = current_app.config["SERVICE_INSTANCE_ID"]
        payload["service_pid"] = os.getpid()
    response = jsonify(payload)
    response.status_code = 503 if payload["overall_status"] == "fatal" else 200
    response.headers["Cache-Control"] = "no-store"
    return response
