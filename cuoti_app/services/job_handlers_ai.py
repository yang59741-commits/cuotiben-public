"""AI, classification and batch job factories using existing business services."""

from __future__ import annotations
from ..database import get_db
from .jobs import JobOutcome, PermanentJobError
from .ai_memory import memory_from_app
from .problem_library import ProblemLibraryError, add_classified_problems
from .ai import DeepSeekConfigurationError, DeepSeekServiceError
from .ai_providers import CallbackCancellationToken
from .batch_mistakes import BatchMistakeError, mark_batch_mistakes
from .ai_tasks import (
    AITaskError,
    analyze_problem,
    batch_analyze,
    classify_problem,
    quick_classify,
)
from .daily_analysis import generate_daily_analysis

def make_job_ai_call(context, call_ai, *, exam_attempt):
    cancellation = CallbackCancellationToken(
        context.raise_if_cancelled
        if exam_attempt
        else context.is_cancel_requested
    )

    def job_ai_call(messages, **kwargs):
        kwargs.setdefault("freeze_id", context.job_id)
        kwargs.setdefault("cancellation", cancellation)
        if exam_attempt:
            kwargs.setdefault("attempt_guard", context.raise_if_cancelled)
            job_policy = context.frozen_time_policy()
            reserve_seconds = float(
                job_policy.get("final_reserve_ms") or 0
            ) / 1_000.0
            requested_timeout = kwargs.get("timeout")
            if requested_timeout is None:
                requested_timeout = 300
            kwargs["timeout"] = context.bounded_timeout(
                requested_timeout,
                stage=str(
                    kwargs.get("task_policy")
                    or kwargs.get("task")
                    or "ai_call"
                ),
                reserve_seconds=reserve_seconds,
            )

            upstream_usage_observer = kwargs.get("usage_observer")

            def observe_usage(fields):
                provider_calls = max(
                    0, int(fields.get("provider_calls") or 0)
                )
                attempts = max(0, int(fields.get("attempts") or 0))
                context.add_exam_metric("provider_calls", provider_calls)
                context.add_exam_metric(
                    "model_retry_count",
                    max(0, attempts - provider_calls)
                    + max(0, provider_calls - 1),
                )
                for field in (
                    "prompt_tokens",
                    "completion_tokens",
                    "reasoning_tokens",
                    "cached_tokens",
                    "total_tokens",
                ):
                    context.add_exam_metric(
                        field, max(0, int(fields.get(field) or 0))
                    )
                if fields.get("usage_missing"):
                    context.add_exam_metric("usage_missing_calls", 1)
                if callable(upstream_usage_observer):
                    upstream_usage_observer(fields)

            kwargs["usage_observer"] = observe_usage
            context.add_exam_metric("model_calls", 1)
        return call_ai(messages, **kwargs)

    return job_ai_call


def make_ai_handlers(app, *, call_ai, job_error_from_response):
    def make_handler(kind):
        def handler(context, payload):
            if kind == "analysis":
                inferred = "day" if payload.get("date") else "problem"
            elif kind == "classification":
                inferred = "add-problems"
            else:
                inferred = "mark"
            operation = str(payload.get("operation") or inferred)
            job_ai_call = make_job_ai_call(context, call_ai, exam_attempt=False)
            if kind == 'classification' and operation == 'backfill':
                from .mistake_classification import generate_classifications, ClassificationError
                try:
                    with app.app_context():
                        result = generate_classifications(get_db(), payload.get('targets'), ai_call=job_ai_call,
                            job_id=context.job_id, progress=context.update_progress, cancellation=context.raise_if_cancelled)
                    return JobOutcome(result=result)
                except ClassificationError as exc:
                    raise job_error_from_response({'error': str(exc), 'code': 'classification'}, exc.status) from exc

            if operation == "day" and kind == "analysis":
                context.update_progress(0, 4, "正在校验输入")

                def remember(event_type, event_payload):
                    try:
                        memory_from_app(app).append_event(
                            "mistakes",
                            "mistake_analysis",
                            {"event_type": event_type, **event_payload},
                        )
                    except Exception:
                        app.logger.exception("failed to write mistake AI memory")

                try:
                    with app.app_context():
                        body = generate_daily_analysis(
                            get_db(),
                            raw_date=payload.get("date"),
                            generation_scope=payload.get("generation_scope") or "missing",
                            mistake_public_id=payload.get("mistake_public_id"),
                            job_id=context.job_id,
                            ai_call=job_ai_call,
                            remember=remember,
                        )
                except AITaskError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": exc.code}, exc.status_code
                    ) from exc
                except DeepSeekConfigurationError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": "configuration"}, 503
                    ) from exc
                except Exception as exc:
                    message = (
                        f"当日分析失败：{exc}"
                        if isinstance(exc, DeepSeekServiceError)
                        else "当日分析失败，请检查 AI 服务配置或稍后重试"
                    )
                    raise job_error_from_response({"error": message}, 502) from exc
                context.update_progress(4, 4, "已完成")
                return JobOutcome(result=body)
            ai_operations = {
                ("analysis", "problem"): (analyze_problem, "错题分析"),
                ("analysis", "batch"): (batch_analyze, "批量拆题"),
                ("classification", "quick"): (quick_classify, "章节分类"),
                ("classification", "problem"): (classify_problem, "题目分类"),
            }
            selected_ai = ai_operations.get((kind, operation))
            if selected_ai:
                task, action = selected_ai
                context.update_progress(0, 4, "正在校验输入")

                def remember_analysis(event_type, event_payload):
                    try:
                        memory_from_app(app).append_event(
                            "mistakes",
                            "mistake_analysis",
                            {"event_type": event_type, **event_payload},
                        )
                    except Exception:
                        app.logger.exception("failed to write mistake AI memory")

                kwargs = {"payload": payload, "ai_call": job_ai_call}
                if task in {quick_classify, batch_analyze, classify_problem}:
                    kwargs["db"] = None
                if task is analyze_problem:
                    kwargs["remember"] = remember_analysis
                try:
                    with app.app_context():
                        if "db" in kwargs:
                            kwargs["db"] = get_db()
                        body = task(**kwargs)
                except AITaskError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": exc.code}, exc.status_code
                    ) from exc
                except DeepSeekConfigurationError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": "configuration"}, 503
                    ) from exc
                except Exception as exc:
                    message = (
                        f"{action}失败：{exc}"
                        if isinstance(exc, DeepSeekServiceError)
                        else f"{action}失败，请检查 AI 服务配置或稍后重试"
                    )
                    raise job_error_from_response({"error": message}, 502) from exc
                context.update_progress(4, 4, "已完成")
                return JobOutcome(result=body)
            if operation == "add-problems" and kind in {
                "classification",
                "batch-mistakes",
            }:
                context.update_progress(0, 4, "正在校验输入")
                catalog = app.extensions["catalog_service"]
                try:
                    with app.app_context():
                        body = add_classified_problems(
                            get_db(),
                            content=payload.get("content"),
                            tex_base=app.config.get("TEX_BASE") or "",
                            chapter_sources=catalog.chapter_sources,
                            fallback_source=catalog.fallback_source,
                            classifier=lambda messages: job_ai_call(
                                messages,
                                temperature=0.1,
                                timeout=90,
                                task="classification",
                                task_policy="classification_batch",
                            ),
                            compile_check=app.config.get(
                                "LIBRARY_MINIMAL_COMPILE_CHECK"
                            ),
                            source_job_id=context.job_id,
                        )
                except ProblemLibraryError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": exc.code}, exc.status_code
                    ) from exc
                context.update_progress(4, 4, "已完成")
                return JobOutcome(result=body, side_effect_committed=True)
            if operation == "mark" and kind == "batch-mistakes":
                request_payload = {
                    key: value for key, value in payload.items() if key != "operation"
                }
                request_payload["_job_id"] = context.job_id
                context.update_progress(0, 4, "正在校验输入")

                def classify(messages):
                    with app.app_context():
                        return job_ai_call(
                            messages,
                            temperature=0.0,
                            timeout=app.config["BATCH_CLASSIFY_TIMEOUT"],
                            task="classification",
                            task_policy="classification_batch_object",
                            max_attempts=1,
                        )

                try:
                    with app.app_context():
                        body = mark_batch_mistakes(
                            get_db(), payload=request_payload, classifier=classify
                        )
                except BatchMistakeError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": exc.code}, exc.status_code
                    ) from exc
                except DeepSeekConfigurationError as exc:
                    raise job_error_from_response(
                        {"error": str(exc), "code": "configuration"}, 503
                    ) from exc
                except Exception as exc:
                    raise job_error_from_response(
                        {"error": "批量归类失败，请检查服务配置或稍后重试"}, 502
                    ) from exc
                context.update_progress(4, 4, "已完成")
                return JobOutcome(result=body, side_effect_committed=True)
            raise PermanentJobError(
                "任务操作类型无效", code="invalid_operation", category="input",
            )
        return handler
    return {kind: make_handler(kind) for kind in ("analysis", "classification", "batch-mistakes")}
