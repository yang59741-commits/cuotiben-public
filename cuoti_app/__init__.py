
"""Flask application factory for the mistake-review system."""

from __future__ import annotations

def create_app(config_overrides: dict | None = None):
    # Imports stay local so the standalone environment checker can report a
    # missing Flask installation instead of failing while importing config.
    from flask import Flask

    from .config import INSTANCE_DIR, build_config
    from .database import get_db, init_app as init_database_app, init_db
    from .errors import init_error_contract
    from .paths import RESOURCE_DIR
    from .routes import (
        ai,
        attempts,
        attachments,
        data,
        daily_tips,
        daily_review,
        classifications,
        exams,
        health,
        jobs,
        library,
        mistake_exports,
        mistakes,
        pages,
        question_bank,
        bulk,
        settings,
        statistics,
    )
    from .services.job_handlers import register_app_job_handlers
    from .services.attachments import reconcile_attachments
    from .services.exam_artifacts import reconcile_exam_artifacts, reconcile_exam_deletions
    from .services.settings import get_settings_service
    from .services.statistics import ensure_daily_snapshot
    from .services.catalog import CatalogService

    INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
    app = Flask(
        __name__,
        instance_path=str(INSTANCE_DIR),
        template_folder=str(RESOURCE_DIR / "cuoti_app" / "templates"),
        static_folder=str(RESOURCE_DIR / "cuoti_app" / "static"),
    )
    app.config.from_mapping(build_config(config_overrides))
    app.json.ensure_ascii = False
    app.extensions["catalog_service"] = CatalogService(
        app.config["CATALOG_PATH"]
    ).ensure()
    init_error_contract(app)
    init_database_app(app)
    with app.app_context():
        init_db()
        ensure_daily_snapshot(get_db())
        reconcile_attachments(app.config["DATABASE"], app.config["ATTACHMENT_DIR"])
        reconcile_exam_deletions(app.config["DATABASE"], app.config["PDF_DIR"])
        reconcile_exam_artifacts(app.config["DATABASE"], app.config["PDF_DIR"])
        get_settings_service(app).remove_obsolete_local_fields(
            "mathpix_app_id", "mathpix_app_key"
        )
    for blueprint in (
        pages.bp,
        health.bp,
        attachments.bp,
        attempts.bp,
        mistakes.bp,
        statistics.bp,
        daily_tips.bp,
        daily_review.bp,
        classifications.bp,
        mistake_exports.bp,
        data.bp,
        ai.bp,
        exams.bp,
        library.bp,
        question_bank.bp,
        bulk.bp,
        settings.bp,
        jobs.bp,
    ):
        app.register_blueprint(blueprint)
    if not app.config.get("TESTING") or app.config.get("REGISTER_JOB_HANDLERS"):
        register_app_job_handlers(app)
    return app
