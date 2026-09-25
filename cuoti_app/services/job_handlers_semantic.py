"""Semantic-index durable job handler factory."""

from __future__ import annotations

from ..database import connect_database, get_db
from .jobs import JobOutcome, PermanentJobError
from .semantic_index_loop import (
    SemanticDispatchRuntime,
    SemanticIndexLoopError,
    run_semantic_dispatch_coordinator,
)
from .semantic_index_provider import (
    SemanticProviderAdapterError,
    build_semantic_provider_runtime,
)
from .semantic_index_refresh import (
    SemanticIndexRefreshError,
    load_app_semantic_source_bundle,
    normalize_semantic_refresh_request,
    prepare_semantic_index_refresh,
    semantic_analysis_profile_from_app,
)


def make_semantic_index_refresh_handler(app):
    def semantic_index_refresh_handler(context, payload):
        context.update_progress(0, 3, "正在校验语义索引刷新身份")
        try:
            with app.app_context():
                profile = semantic_analysis_profile_from_app(app)
                request = normalize_semantic_refresh_request(payload, profile=profile)
                context.update_progress(1, 3, "正在读取已发布 last-good 来源")
                result = prepare_semantic_index_refresh(
                    get_db(),
                    request=request,
                    profile=profile,
                    bundle_loader=lambda: load_app_semantic_source_bundle(app),
                    raise_if_cancelled=context.raise_if_cancelled,
                )
                runtime = app.extensions.get("semantic_index_synthetic_runtime")
                if runtime is None:
                    runtime = build_semantic_provider_runtime(app, profile=profile)
                if runtime is not None:
                    if not isinstance(runtime, SemanticDispatchRuntime):
                        raise SemanticIndexLoopError(
                            "SYNTHETIC_RUNTIME_INVALID",
                            "semantic synthetic runtime 类型无效",
                        )

                    def semantic_connection_factory():
                        return connect_database(
                            app.config["DATABASE"], role="background"
                        )

                    def foreground_exam_requested():
                        connection = semantic_connection_factory()
                        try:
                            return connection.execute(
                                "SELECT 1 FROM background_jobs WHERE kind='exam-generation' "
                                "AND status IN ('queued','running') LIMIT 1"
                            ).fetchone() is not None
                        finally:
                            connection.close()

                    result = run_semantic_dispatch_coordinator(
                        connection_factory=semantic_connection_factory,
                        run_id=request.run_id,
                        bundle_loader=lambda: load_app_semantic_source_bundle(app),
                        runtime=runtime,
                        lease_owner_prefix=f"job:{context.job_id}",
                        heartbeat=lambda: context.coordinator.heartbeat(
                            context.job_id,
                            lease_attempt=context.lease_attempt,
                        ),
                        cancel_requested=context.is_cancel_requested,
                        yield_requested=foreground_exam_requested,
                        progress=lambda current, total, message: (
                            context.coordinator.update_progress(
                                context.job_id,
                                current=current,
                                total=total,
                                message=message,
                                lease_attempt=context.lease_attempt,
                            )
                        ),
                        resume_paused=request.operation == "resume",
                    )
        except SemanticIndexRefreshError as exc:
            raise PermanentJobError(
                str(exc), code=exc.code.lower(), category="business"
            ) from exc
        except SemanticIndexLoopError as exc:
            raise PermanentJobError(
                str(exc), code=exc.code.lower(), category="business"
            ) from exc
        except SemanticProviderAdapterError as exc:
            raise PermanentJobError(
                str(exc), code=exc.code.lower(), category="configuration"
            ) from exc
        final_message = (
            "语义索引材料卡 dispatch 已安全收口"
            if result.get("phase") == "dispatched"
            else "语义索引刷新清单已冻结"
        )
        context.update_progress(3, 3, final_message)
        return JobOutcome(result=result, side_effect_committed=True)

    return semantic_index_refresh_handler


__all__ = ["make_semantic_index_refresh_handler"]
