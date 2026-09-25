"""Local AI configuration routes with write-only credentials."""

from __future__ import annotations

from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request

from ..database import get_db
from ..services.ai_providers import AIProviderError
from ..services.ai_telemetry import usage_summary
from ..services.ai_memory import MEMORY_TYPES, memory_from_app
from ..services.catalog import CatalogError
from ..services.jobs import JobConflict, JobCoordinator
from ..services.semantic_cleanup import (
    build_cleanup_preview,
)
from ..services.semantic_index_control import (
    SemanticIndexControlError,
    request_semantic_run_control,
    semantic_resume_payload,
)
from ..services.semantic_operations import collect_semantic_operations_overview
from ..services.semantic_operations_contract import ERROR_STATUS, error_envelope
from ..services.semantic_index_refresh import (
    SemanticIndexRefreshError,
    active_semantic_refresh_keys,
    load_app_semantic_source_bundle,
    prepare_semantic_refresh_submission,
    semantic_analysis_profile_from_app,
)
from ..services.settings import SettingsError, get_settings_service


bp = Blueprint("settings", __name__)


@bp.get("/api/v2/settings/semantic-index/overview")
def semantic_index_overview():
    response = jsonify(
        collect_semantic_operations_overview(
            current_app._get_current_object(), get_db()
        )
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _semantic_error(code: str, message: str):
    status = ERROR_STATUS[code]
    return (
        jsonify(
            error_envelope(
                code, message, request.headers.get("X-Correlation-ID", "")
            )
        ),
        status,
    )


def _semantic_write_payload():
    if not request.is_json:
        return None, _semantic_error(
            "semantic_json_required", "请求体必须是 JSON 对象"
        )
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return None, _semantic_error(
            "semantic_validation", "请求体必须是 JSON 对象"
        )
    origin = request.headers.get("Origin")
    if origin:
        expected = urlsplit(request.host_url)
        supplied = urlsplit(origin)
        if (supplied.scheme, supplied.netloc) != (
            expected.scheme,
            expected.netloc,
        ):
            return None, _semantic_error(
                "semantic_origin_rejected", "请求来源不受信任"
            )
    if request.headers.get("X-Cuoti-CSRF") != "same-origin":
        return None, _semantic_error(
            "semantic_csrf_rejected", "缺少同源写入标记"
        )
    return payload, None


def _revision_matches(payload):
    overview = collect_semantic_operations_overview(
        current_app._get_current_object(), get_db()
    )
    expected = payload.get("version")
    if isinstance(expected, bool) or not isinstance(expected, int):
        return overview, _semantic_error(
            "semantic_validation", "version 必须是整数"
        )
    if expected != overview["settings_revision"]:
        return overview, _semantic_error(
            "semantic_settings_conflict", "设置已更新，请刷新后重试"
        )
    return overview, None


@bp.put("/api/v2/settings/semantic-index")
def update_semantic_index_settings():
    payload, invalid = _semantic_write_payload()
    if invalid:
        return invalid
    try:
        result = get_settings_service(
            current_app._get_current_object()
        ).update_semantic_index_settings(payload)
        return jsonify(result)
    except SettingsError as exc:
        code = (
            "semantic_settings_conflict"
            if exc.code == "SETTINGS_CONFLICT"
            else "semantic_capability_denied"
            if exc.status_code == 409
            else "semantic_validation"
        )
        return _semantic_error(code, str(exc))


def _semantic_coordinator():
    return JobCoordinator(
        current_app.config["DATABASE"],
        staged_input_roots=(current_app.config.get("JOB_INPUT_DIR"),),
    )


@bp.post("/api/v2/settings/semantic-index/actions/<action>")
def semantic_index_action(action: str):
    if action not in {"refresh", "pause", "resume", "rebuild"}:
        return _semantic_error("semantic_not_found", "运维操作不存在")
    payload, invalid = _semantic_write_payload()
    if invalid:
        return invalid
    overview, invalid = _revision_matches(payload)
    if invalid:
        return invalid
    capability = overview["capabilities"][action]
    semantic_replay_candidate = (
        action in {"refresh", "rebuild"}
        and capability.get("reason") == "active_job_conflict"
        and overview["job_state"]["semantic_active"]
        and not overview["job_state"]["exam_generation_active"]
    )
    if not capability["enabled"] and not semantic_replay_candidate:
        return _semantic_error(
            "semantic_capability_denied",
            capability["reason"] or "操作当前不可用",
        )
    if action == "rebuild" and payload.get("confirm") is not True:
        return _semantic_error(
            "semantic_confirmation_required", "重建需要明确确认"
        )
    try:
        if action in {"pause", "resume"}:
            run_id = str(payload.get("run_id") or "").strip()
            if action == "pause":
                state = request_semantic_run_control(
                    get_db(), run_id=run_id, action="pause"
                )
                refreshed = collect_semantic_operations_overview(
                    current_app._get_current_object(), get_db()
                )
                return (
                    jsonify(
                        {
                            "run_id": run_id,
                            "status": state["status"],
                            "capabilities": refreshed["capabilities"],
                        }
                    ),
                    202,
                )
            profile = semantic_analysis_profile_from_app(current_app)
            submission = semantic_resume_payload(
                get_db(), run_id=run_id, profile=profile
            )
        else:
            bundle = load_app_semantic_source_bundle(current_app)
            profile = semantic_analysis_profile_from_app(current_app)
            submission = {
                "operation": "prepare",
                "run_kind": "rebuild" if action == "rebuild" else "incremental",
                "expected_source_snapshot": bundle.source_snapshot,
                "analysis_profile_id": profile.profile_id,
                "analysis_fingerprint": profile.fingerprint,
            }
        normalized, key = prepare_semantic_refresh_submission(
            current_app, submission
        )
        if semantic_replay_candidate:
            active_keys = active_semantic_refresh_keys(get_db())
            if key not in active_keys:
                return _semantic_error(
                    "semantic_active_job_conflict",
                    "已有不同的语义索引任务正在运行",
                )
        job, created = _semantic_coordinator().submit(
            "semantic-index-refresh", normalized, idempotency_key=key
        )
        refreshed = collect_semantic_operations_overview(
            current_app._get_current_object(), get_db()
        )
        response = jsonify(
            {
                "job_id": job["id"],
                "run_id": (
                    None
                    if action not in {"pause", "resume"}
                    else payload.get("run_id")
                ),
                "status": job["status"],
                "idempotency_replayed": not created,
                "capabilities": refreshed["capabilities"],
            }
        )
        response.headers["Idempotency-Replayed"] = (
            "false" if created else "true"
        )
        return response, 202 if created else 200
    except JobConflict as exc:
        return _semantic_error("semantic_idempotency_conflict", str(exc))
    except (SemanticIndexControlError, SemanticIndexRefreshError) as exc:
        code = (
            "semantic_not_found"
            if getattr(exc, "code", "") == "RUN_NOT_FOUND"
            else "semantic_capability_denied"
        )
        return _semantic_error(code, str(exc))


@bp.get("/api/v2/settings/semantic-index/cleanup-preview")
def semantic_index_cleanup_preview():
    overview = collect_semantic_operations_overview(
        current_app._get_current_object(), get_db()
    )
    preview = build_cleanup_preview(
        get_db(), retention=overview["settings"]["retention"]
    )
    response = jsonify(preview)
    response.headers["Cache-Control"] = "no-store"
    return response


def _memory_type(payload):
    memory_type = str((payload or {}).get("memory_type") or "").strip()
    if memory_type not in MEMORY_TYPES:
        raise ValueError("记忆类型必须是 free、mistakes 或 past")
    return memory_type


@bp.get("/api/v2/settings/ai/usage")
def ai_usage_statistics():
    return jsonify(usage_summary(current_app._get_current_object()))


@bp.get("/api/v2/settings/ai/memory")
def ai_memory_status():
    return jsonify(memory_from_app(current_app).status())


@bp.put("/api/v2/settings/ai/memory/pause")
def ai_memory_pause():
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        return jsonify(memory_from_app(current_app).set_paused(_memory_type(payload), bool(payload.get("paused"))))
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


@bp.delete("/api/v2/settings/ai/memory")
def ai_memory_clear():
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        memory_type = _memory_type(payload)
        if payload.get("confirm") is not True:
            raise ValueError("清空记忆需要明确确认")
        memory_from_app(current_app).clear(memory_type)
        return jsonify({"cleared": True, "memory_type": memory_type})
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


@bp.post("/api/v2/settings/ai/memory/rebuild")
def ai_memory_rebuild():
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        memory_type = _memory_type(payload)
        return jsonify({"memory_type": memory_type, **memory_from_app(current_app).rebuild(memory_type)})
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


@bp.patch("/api/v2/settings/ai/memory/profile")
def ai_memory_profile():
    payload = request.get_json(silent=True) if request.is_json else {}
    try:
        memory_type = _memory_type(payload)
        key = str(payload.get("key") or "").strip()
        if not key:
            raise ValueError("画像条目不能为空")
        memory = memory_from_app(current_app)
        if payload.get("action"):
            entry = memory.command_profile(
                memory_type,
                key,
                action=str(payload.get("action") or "").strip(),
                value=(
                    str(payload.get("value"))
                    if payload.get("value") is not None
                    else None
                ),
            )
        else:
            entry = memory.update_profile(
                memory_type,
                key,
                confirmed=(
                    payload.get("confirmed") if "confirmed" in payload else None
                ),
                correction=(
                    str(payload.get("correction"))
                    if "correction" in payload
                    else None
                ),
            )
        return jsonify({"memory_type": memory_type, "entry": entry})
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except KeyError as exc:
        return jsonify({"error": str(exc).strip("'"), "code": "NOT_FOUND"}), 404


@bp.get("/api/v2/settings/ai/memory/profile")
def ai_memory_profile_list():
    try:
        memory_type = _memory_type(request.args)
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("page_size", 20))
        return jsonify(
            memory_from_app(current_app).list_profile(
                memory_type,
                page=page,
                page_size=page_size,
                status=str(request.args.get("status") or "").strip(),
                entry_type=str(request.args.get("entry_type") or "").strip(),
            )
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


@bp.get("/api/v2/catalog")
def catalog_configuration():
    return jsonify(current_app.extensions["catalog_service"].public_catalog())


@bp.post("/api/v2/catalog/sync")
def sync_catalog_configuration():
    """Reload validated config and perform the deliberately add-only sync."""

    try:
        catalog = current_app.extensions["catalog_service"].reload()
        db = get_db()
        result = catalog.sync_database(db)
        db.commit()
        return jsonify({"catalog": catalog.public_catalog(), "sync": result})
    except CatalogError as exc:
        return jsonify({"error": str(exc), "code": "CATALOG_INVALID"}), 400


def _settings_error(error: SettingsError):
    return jsonify({"error": str(error), "code": error.code}), error.status_code


def _provider_error(error: AIProviderError):
    status = 503 if error.code in {"CONFIGURATION", "NETWORK", "RATE_LIMITED"} else 502
    if error.code == "AUTHENTICATION":
        status = 401
    return (
        jsonify(
            {
                "error": str(error),
                "code": error.code,
                "retryable": error.retryable,
            }
        ),
        status,
    )


@bp.route("/api/v2/settings/ai", methods=["GET", "PUT"])
def ai_settings():
    service = get_settings_service(current_app._get_current_object())
    if request.method == "GET":
        try:
            return jsonify(service.public_ai_settings())
        except SettingsError as exc:
            return _settings_error(exc)

    if not request.is_json:
        return jsonify({"error": "请求体必须是 JSON 对象", "code": "VALIDATION"}), 415
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象", "code": "VALIDATION"}), 400
    try:
        return jsonify(service.update_ai_settings(payload))
    except SettingsError as exc:
        return _settings_error(exc)


@bp.route("/api/v2/settings/ai/test", methods=["POST"])
def test_ai_settings():
    service = get_settings_service(current_app._get_current_object())
    try:
        return jsonify(service.test_ai())
    except SettingsError as exc:
        return _settings_error(exc)
    except AIProviderError as exc:
        return _provider_error(exc)


@bp.post("/api/v2/settings/ai/models")
def available_ai_models():
    """Discover models visible to the active profile's saved credential."""

    try:
        return jsonify(
            get_settings_service(
                current_app._get_current_object()
            ).available_models()
        )
    except SettingsError as exc:
        return _settings_error(exc)
    except AIProviderError as exc:
        return _provider_error(exc)


@bp.route("/api/v2/settings/ai/migrate-legacy", methods=["POST"])
def migrate_legacy_ai_key():
    service = get_settings_service(current_app._get_current_object())
    try:
        return jsonify(service.migrate_legacy_key())
    except SettingsError as exc:
        return _settings_error(exc)


@bp.post("/api/v2/settings/ai/reset")
def reset_ai_settings():
    payload = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象", "code": "VALIDATION"}), 400
    try:
        return jsonify(
            get_settings_service(current_app._get_current_object()).reset_ai_settings(
                payload
            )
        )
    except SettingsError as exc:
        return _settings_error(exc)


@bp.delete("/api/v2/settings/ai/credential")
def delete_ai_credential():
    payload = request.get_json(silent=True) if request.is_json else {}
    if not isinstance(payload, dict):
        return jsonify({"error": "请求体必须是 JSON 对象", "code": "VALIDATION"}), 400
    try:
        return jsonify(
            get_settings_service(current_app._get_current_object()).clear_ai_credential(
                payload
            )
        )
    except SettingsError as exc:
        return _settings_error(exc)
