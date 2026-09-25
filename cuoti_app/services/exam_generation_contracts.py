"""Stable input/output contracts for exam-generation orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_publish import ExamPublicationFence


@dataclass(frozen=True)
class ExamGenerationResult:
    body: dict[str, Any]
    status: int = 200


@dataclass(frozen=True)
class ExamGenerationRuntime:
    db: Any
    config: Mapping[str, Any]
    memory: Any
    logger: Any
    request_id: str | None
    validated_past_selection: Callable[..., Any]
    generate_problem_exam: Callable[..., Any]
    generate_indexed_free_exam: Callable[..., Any]
    require_deepseek_config: Callable[[], Any]
    question_bank_index: Callable[[], Any]
    require_question_bank_snapshot: Callable[..., Any]
    require_past_exam_snapshot: Callable[..., Any]
    indexed_candidates: Callable[..., Any]
    understand_material: Callable[..., Any]
    remove_exam_artifacts: Callable[..., Any]
    safe_error_text: Callable[..., str]
    has_background_job: Callable[..., bool]
    parse_ai_json_object: Callable[..., Any]
    store_exam_items: Callable[..., Any]
    compile_exam: Callable[..., Any]
    progress: Callable[[int, int, str], None]
    ai_call: Callable[..., str]
    chapter_sources: Mapping[tuple[str, str], list[str]]
    app_context: Callable[[], Any]
    semantic_recall_decision: Callable[..., Any] | None = None
    semantic_recall_shadow: Callable[..., Any] | None = None
    semantic_settings_revision: int = 0
    semantic_candidate_multiplier: int = 4
    semantic_allow_partial_cache: bool = False
    ai_task_freeze_snapshot: Mapping[str, Any] | None = None
    observe_metric: Callable[[str, int], None] = lambda _name, _value: None
    past_attempt_resolver: Callable[..., Any] | None = None
    allow_legacy_past_characterization: bool = False
    publication_fence: ExamPublicationFence | None = None


__all__ = ["ExamGenerationResult", "ExamGenerationRuntime"]
