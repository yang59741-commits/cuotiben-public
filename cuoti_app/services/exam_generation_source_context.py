"""Serializable source-specific context contracts for exam generation stage 2."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .exam_contracts import (
    ExamGenerationConstraintError,
    validate_single_paper_count,
)
from .exam_coverage_policy import (
    MinimumCoverageTargets,
    preflight_minimum_coverage,
)
from .exam_local_repair import FrozenGenerationEvidence
from .exam_generation_mode_memory_authority import MemoryAuthorityError
from .exam_mistake_lineage_policy import MistakeLineageSlot
from .exam_source_flows import SourceFlowError, prepare_mistakes_flow
from .past_exams import (
    PastExamSourceError,
    reference_exam_section,
    reference_exam_structure,
    source_for_subject,
)


MODE_MEMORY_READ_SNAPSHOT_VERSION = "exam-mode-memory-read-snapshot-v1"
_MODE_MEMORY_READ_SOURCES = {
    "free": ("free", "mistakes"),
    "mistakes": ("mistakes",),
    "past": ("past", "mistakes"),
}
_MODE_MEMORY_ACTORS = {
    "free": "free_exam",
    "mistakes": "mistake_exam",
    "past": "past_exam",
}


@dataclass(frozen=True, slots=True)
class SourceContextFailure:
    body: Mapping[str, Any]
    status: int


def _constraint_failure(
    exc: ExamGenerationConstraintError,
) -> SourceContextFailure:
    """Preserve the stable constraint contract across every source adapter."""

    return SourceContextFailure(
        {
            "error": str(exc),
            "code": exc.code,
            "retryable": False,
            "hint": exc.hint,
            "actions": list(exc.actions),
            "details": dict(exc.details),
        },
        exc.status_code,
    )


@dataclass(frozen=True, slots=True)
class FreeSourceContext:
    subject_id: int
    subject: str
    chapters: tuple[str, ...]
    difficulty: str
    target: str
    question_count: int
    question_types: tuple[str, ...]
    note: str
    title: str
    exam_type: str
    system_prompt: str
    prompt: str
    memory_context: str
    difficulty_quota: Mapping[str, int]
    minimum_coverage: MinimumCoverageTargets
    allow_adaptation: bool


@dataclass(frozen=True, slots=True)
class FreeSourceContextOutcome:
    context: FreeSourceContext | None = None
    failure: SourceContextFailure | None = None


@dataclass(frozen=True, slots=True)
class MistakesSourceContext:
    mistakes: tuple[Mapping[str, Any], ...]
    subject_id: int
    subject: str
    chapters: tuple[str, ...]
    difficulty: str
    target: str
    question_count: int
    question_types: tuple[str, ...]
    note: str
    title: str
    exam_type: str
    system_prompt: str
    prompt: str
    frozen_evidence: FrozenGenerationEvidence
    minimum_coverage: MinimumCoverageTargets
    lineage_slots: tuple[MistakeLineageSlot, ...]
    public_id_by_internal_id: Mapping[int, str]
    allow_adaptation: bool
    memory_context: str = ""
    training_target: str | None = None


@dataclass(frozen=True, slots=True)
class MistakesSourceContextOutcome:
    context: MistakesSourceContext | None = None
    failure: SourceContextFailure | None = None


@dataclass(frozen=True, slots=True)
class PastSourceContext:
    subject_id: int
    subject: str
    chapters: tuple[str, ...]
    difficulty: str
    target: str
    question_count: int
    question_types: tuple[str, ...]
    note: str
    title: str
    exam_type: str
    system_prompt: str
    prompt: str
    memory_context: str
    past_source: Mapping[str, Any]
    institution: str
    paper_count: int
    allow_adaptation: bool
    reference_year: int
    reference_section: Mapping[str, Any]
    structure_contract: Mapping[str, Any]
    candidate_source: str
    source_quotas: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PastSourceContextOutcome:
    context: PastSourceContext | None = None
    failure: SourceContextFailure | None = None


def _serializable_value(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _serializable_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable_value(item) for item in value]
    return value


def _allow_adaptation(payload: Mapping[str, Any]) -> bool:
    value = payload.get("allow_adaptation", False)
    if not isinstance(value, bool):
        raise ValueError("改编选项格式无效")
    return value


def _plain_snapshot_value(value: Any) -> Any:
    """Keep task soft-input JSON deterministic without inventing identities."""

    if isinstance(value, Mapping):
        return {
            str(key): _plain_snapshot_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_plain_snapshot_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _capture_mode_memory_snapshot(
    memory: Any,
    *,
    mode: str,
    actor: str,
    filters: Mapping[str, Any],
) -> dict[str, Any]:
    """Read every permitted soft source at one task cutoff."""

    sources: dict[str, dict[str, Any]] = {}
    normalized_filters = _plain_snapshot_value(filters)
    for namespace in _MODE_MEMORY_READ_SOURCES[mode]:
        try:
            snapshot_reader = getattr(memory, "read_exam_inputs_snapshot", None)
            if callable(snapshot_reader):
                captured = snapshot_reader(
                    namespace, actor, dict(normalized_filters)
                )
                context = str(captured.get("context") or "")
                raw_recent_ids = captured.get("recent_question_ids") or ()
                raw_recent_fingerprints = (
                    captured.get("recent_question_fingerprints") or ()
                )
            else:
                context = str(
                    memory.read_context(namespace, actor, dict(normalized_filters)) or ""
                )
                raw_recent_ids = memory.recent_question_ids(namespace, actor)
                raw_recent_fingerprints = memory.recent_question_fingerprints(
                    namespace, actor
                )
            recent_ids = sorted(
                {str(value) for value in raw_recent_ids if str(value).strip()}
            )
            recent_fingerprints = sorted(
                {
                    str(value)
                    for value in raw_recent_fingerprints
                    if str(value).strip()
                }
            )
        except MemoryAuthorityError as exc:
            if not exc.reason.startswith("ai_memory_"):
                raise
            sources[namespace] = {
                "status": "omitted",
                "omission_reason": str(exc.reason),
            }
            continue
        sources[namespace] = {
            "status": "available",
            "context": context,
            "recent_question_ids": recent_ids,
            "recent_question_fingerprints": recent_fingerprints,
        }
    return {
        "version": MODE_MEMORY_READ_SNAPSHOT_VERSION,
        "mode": mode,
        "actor": actor,
        "allowed_sources": list(_MODE_MEMORY_READ_SOURCES[mode]),
        "filters": normalized_filters,
        "sources": sources,
    }


def _validate_mode_memory_snapshot(
    value: Any,
    *,
    mode: str,
    actor: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
    payload = dict(value)
    expected_sources = list(_MODE_MEMORY_READ_SOURCES[mode])
    if (
        set(payload)
        != {"version", "mode", "actor", "allowed_sources", "filters", "sources"}
        or payload.get("version") != MODE_MEMORY_READ_SNAPSHOT_VERSION
        or payload.get("mode") != mode
        or payload.get("actor") != actor
        or payload.get("allowed_sources") != expected_sources
        or not isinstance(payload.get("filters"), Mapping)
        or not isinstance(payload.get("sources"), Mapping)
        or set(payload["sources"]) != set(expected_sources)
    ):
        raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
    normalized_sources: dict[str, dict[str, Any]] = {}
    for namespace in expected_sources:
        raw = payload["sources"].get(namespace)
        if not isinstance(raw, Mapping):
            raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
        item = dict(raw)
        if item.get("status") == "omitted":
            if set(item) != {"status", "omission_reason"} or not isinstance(
                item.get("omission_reason"), str
            ):
                raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
        elif item.get("status") == "available":
            if (
                set(item)
                != {
                    "status",
                    "context",
                    "recent_question_ids",
                    "recent_question_fingerprints",
                }
                or not isinstance(item.get("context"), str)
                or not isinstance(item.get("recent_question_ids"), list)
                or not isinstance(item.get("recent_question_fingerprints"), list)
                or any(
                    not isinstance(entry, str) or not entry.strip()
                    for field in (
                        "recent_question_ids",
                        "recent_question_fingerprints",
                    )
                    for entry in item[field]
                )
            ):
                raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
        else:
            raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
        normalized_sources[namespace] = item
    payload["filters"] = _plain_snapshot_value(payload["filters"])
    payload["sources"] = normalized_sources
    return payload


def _omitted_mode_memory_snapshot(
    *, mode: str, actor: str, filters: Mapping[str, Any], reason: str
) -> dict[str, Any]:
    return {
        "version": MODE_MEMORY_READ_SNAPSHOT_VERSION,
        "mode": mode,
        "actor": actor,
        "allowed_sources": list(_MODE_MEMORY_READ_SOURCES[mode]),
        "filters": _plain_snapshot_value(filters),
        "sources": {
            namespace: {"status": "omitted", "omission_reason": reason}
            for namespace in _MODE_MEMORY_READ_SOURCES[mode]
        },
    }


class TaskFrozenModeMemory:
    """Read-through view that freezes all task memory inputs on first access."""

    def __init__(
        self,
        memory: Any,
        *,
        db: Any,
        mode: str,
        job_id: str | None,
    ) -> None:
        if mode not in _MODE_MEMORY_READ_SOURCES:
            raise ValueError("组卷模式无效")
        self._memory = memory
        self._db = db
        self._mode = mode
        self._actor = _MODE_MEMORY_ACTORS[mode]
        self._job_id = str(job_id or "").strip() or None
        self._snapshot: dict[str, Any] | None = None

    def _ensure_snapshot(self, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        normalized_filters = _plain_snapshot_value(filters or {})
        if self._snapshot is None:
            loader = lambda: _capture_mode_memory_snapshot(
                self._memory,
                mode=self._mode,
                actor=self._actor,
                filters=normalized_filters,
            )
            if self._job_id is None:
                payload = loader()
            else:
                from .exam_generation_soft_inputs import freeze_soft_input

                envelope = freeze_soft_input(
                    self._db,
                    job_id=self._job_id,
                    mode=self._mode,
                    input_key="mode_memory",
                    allowed_sources=_MODE_MEMORY_READ_SOURCES[self._mode],
                    loader=loader,
                )
                if not isinstance(envelope, Mapping):
                    raise MemoryAuthorityError("mode_memory_task_snapshot_invalid")
                payload = envelope.get("payload")
                if (
                    isinstance(payload, Mapping)
                    and payload.get("state") == "omitted"
                    and payload.get("reason") == "frozen_soft_input_unusable"
                ):
                    payload = _omitted_mode_memory_snapshot(
                        mode=self._mode,
                        actor=self._actor,
                        filters=normalized_filters,
                        reason="frozen_soft_input_unusable",
                    )
            if isinstance(payload, Mapping):
                stored_mode = payload.get("mode")
                stored_actor = payload.get("actor")
                if stored_mode is not None and stored_mode != self._mode:
                    raise MemoryAuthorityError("mode_memory_task_snapshot_mode_mismatch")
                if stored_actor is not None and stored_actor != self._actor:
                    raise MemoryAuthorityError("mode_memory_task_snapshot_actor_mismatch")
            try:
                self._snapshot = _validate_mode_memory_snapshot(
                    payload,
                    mode=self._mode,
                    actor=self._actor,
                )
            except MemoryAuthorityError as exc:
                if exc.reason != "mode_memory_task_snapshot_invalid":
                    raise
                self._snapshot = _omitted_mode_memory_snapshot(
                    mode=self._mode,
                    actor=self._actor,
                    filters=normalized_filters,
                    reason="frozen_soft_input_unusable",
                )
        return self._snapshot

    def _source(
        self,
        namespace: str,
        actor: str,
        filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if actor != self._actor or namespace not in _MODE_MEMORY_READ_SOURCES[self._mode]:
            raise MemoryAuthorityError("mode_memory_task_snapshot_read_forbidden")
        snapshot = self._ensure_snapshot(filters)
        if (
            filters is not None
            and snapshot["filters"] != _plain_snapshot_value(filters)
        ):
            return {
                "status": "omitted",
                "omission_reason": "frozen_soft_input_filter_mismatch",
            }
        return dict(snapshot["sources"][namespace])

    def read_context(
        self,
        namespace: str,
        actor: str,
        filters: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        combined = {**dict(filters or {}), **kwargs}
        source = self._source(namespace, actor, combined)
        return str(source.get("context") or "") if source["status"] == "available" else ""

    def recent_question_ids(self, namespace: str, actor: str) -> set[str]:
        source = self._source(namespace, actor)
        if source["status"] != "available":
            return set()
        return set(source["recent_question_ids"])

    def recent_question_fingerprints(self, namespace: str, actor: str) -> set[str]:
        source = self._source(namespace, actor)
        if source["status"] != "available":
            return set()
        return set(source["recent_question_fingerprints"])

    def __getattr__(self, name: str) -> Any:
        # Writes still use AIMemory's existing mode-local authorization.
        return getattr(self._memory, name)


def task_frozen_mode_memory(
    memory: Any,
    *,
    db: Any,
    mode: str,
    job_id: str | None,
) -> TaskFrozenModeMemory:
    return TaskFrozenModeMemory(memory, db=db, mode=mode, job_id=job_id)


def _read_mode_memory_context(
    memory: Any,
    *,
    mode: str,
    actor: str,
    filters: Mapping[str, Any],
) -> str:
    """Read mode history and daily-mistake preference as separate sources."""

    primary_namespace = {"free": "free", "mistakes": "mistakes", "past": "past"}[mode]
    read = read_optional_exam_memory_context
    primary = read(memory, primary_namespace, actor, filters)
    if mode == "mistakes":
        return primary
    mistake_preference = read(memory, "mistakes", actor, filters)
    if not mistake_preference or mistake_preference == primary:
        return primary
    if not primary:
        return mistake_preference
    if mode == "free":
        parts = (
            ("【每日错题偏好记忆】", mistake_preference),
            ("【自由组卷历史记忆】", primary),
        )
    else:
        parts = (
            ("【真题组卷历史软去重记忆】", primary),
            ("【每日错题低阶偏好记忆】", mistake_preference),
        )
    return "\n".join(value for pair in parts for value in pair)


def read_optional_exam_memory_context(
    memory: Any,
    namespace: str,
    actor: str,
    filters: Mapping[str, Any],
) -> str:
    """Omit unusable optional memory instead of treating it as evidence.

    AIMemory still validates its store strictly.  This orchestration boundary
    merely degrades read failures from that optional store to no soft input.
    Unrelated authority failures continue to propagate.
    """

    try:
        return str(memory.read_context(namespace, actor, dict(filters)) or "")
    except MemoryAuthorityError as exc:
        if exc.reason.startswith("ai_memory_"):
            return ""
        raise


def read_optional_recent_question_ids(
    memory: Any, namespace: str, actor: str
) -> set[str]:
    """Return no soft exclusions when optional memory is unusable."""

    try:
        return set(memory.recent_question_ids(namespace, actor))
    except MemoryAuthorityError as exc:
        if exc.reason.startswith("ai_memory_"):
            return set()
        raise


def read_optional_recent_question_fingerprints(
    memory: Any, namespace: str, actor: str
) -> set[str]:
    """Return no soft fingerprint evidence when optional memory is unusable."""

    try:
        return set(memory.recent_question_fingerprints(namespace, actor))
    except MemoryAuthorityError as exc:
        if exc.reason.startswith("ai_memory_"):
            return set()
        raise


def prepare_free_source_context(
    payload: Mapping[str, Any],
    *,
    db: Any,
    memory: Any,
    positive_int: Callable[[Any, str], int],
    exam_options: Callable[..., tuple[str, str, int, list[str], str]],
    difficulty_quota: Callable[[int, str], Mapping[str, int]],
) -> FreeSourceContextOutcome:
    """Resolve the free source only; candidate and semantic work starts later."""

    chapters = payload.get("chapters", [])
    if not isinstance(chapters, list) or not chapters:
        return FreeSourceContextOutcome(
            failure=SourceContextFailure(
                {"error": "请选择科目和至少一个章节"}, 400
            )
        )
    if len(chapters) > 50:
        return FreeSourceContextOutcome(
            failure=SourceContextFailure(
                {"error": "一次最多选择 50 个章节"}, 400
            )
        )
    try:
        subject_id = positive_int(payload.get("subject_id"), "科目 ID")
        chapter_ids = sorted(
            positive_int(value, "章节 ID") for value in chapters
        )
        if len(chapter_ids) != len(set(chapter_ids)):
            raise ValueError("章节 ID 不能重复")
        difficulty, target, question_count, question_types, note = exam_options(
            payload,
            default_count=5,
        )
        allow_adaptation = _allow_adaptation(payload)
    except ExamGenerationConstraintError as exc:
        return FreeSourceContextOutcome(failure=_constraint_failure(exc))
    except ValueError as exc:
        return FreeSourceContextOutcome(
            failure=SourceContextFailure({"error": str(exc)}, 400)
        )

    subject_row = db.execute(
        "SELECT name FROM subjects WHERE id=?", (subject_id,)
    ).fetchone()
    if not subject_row:
        return FreeSourceContextOutcome(
            failure=SourceContextFailure({"error": "科目不存在"}, 400)
        )
    subject = str(subject_row["name"])
    chapter_rows = db.execute(
        "SELECT id,name FROM chapters WHERE subject_id=? AND id IN ({})".format(
            ",".join("?" * len(chapter_ids))
        ),
        [subject_id, *chapter_ids],
    ).fetchall()
    chapter_map = {int(row["id"]): str(row["name"]) for row in chapter_rows}
    if len(chapter_map) != len(chapter_ids):
        return FreeSourceContextOutcome(
            failure=SourceContextFailure(
                {"error": "部分章节不存在或不属于所选科目"}, 400
            )
        )
    chapter_names = tuple(chapter_map[chapter_id] for chapter_id in chapter_ids)
    try:
        minimum_coverage = preflight_minimum_coverage(
            question_count,
            question_types=question_types,
            chapters=chapter_names,
        )
    except ExamGenerationConstraintError as exc:
        return FreeSourceContextOutcome(failure=_constraint_failure(exc))
    memory_context = _read_mode_memory_context(
        memory,
        mode="free",
        actor="free_exam",
        filters={
            "task": "exam_generation",
            "subject": subject,
            "chapters": list(chapter_names),
            "difficulty": difficulty,
        },
    )
    quota = dict(difficulty_quota(question_count, difficulty))
    return FreeSourceContextOutcome(
        context=FreeSourceContext(
            subject_id=subject_id,
            subject=subject,
            chapters=chapter_names,
            difficulty=difficulty,
            target=target,
            question_count=question_count,
            question_types=tuple(question_types),
            note=note,
            title=(
                f"{subject} - {'、'.join(chapter_names[:3])}"
                f"{'等' if len(chapter_names) > 3 else ''} ({difficulty})"
            ),
            exam_type="自由组卷",
            system_prompt="",
            prompt="",
            memory_context=str(memory_context),
            difficulty_quota=quota,
            minimum_coverage=minimum_coverage,
            allow_adaptation=allow_adaptation,
        )
    )


def prepare_mistakes_source_context(
    payload: Mapping[str, Any],
    *,
    db: Any,
    memory: Any,
    chapter_sources: Mapping[tuple[str, str], list[str]],
    tex_base: str,
    logger: Any,
    selection_seed: str,
) -> MistakesSourceContextOutcome:
    """Resolve and freeze mistake evidence without generating a paper."""

    try:
        allow_adaptation = _allow_adaptation(payload)
        flow = prepare_mistakes_flow(
            payload,
            db=db,
            memory=memory,
            chapter_sources=chapter_sources,
            tex_base=tex_base,
            logger=logger,
            selection_seed=selection_seed,
            read_memory_context=read_optional_exam_memory_context,
        )
    except ExamGenerationConstraintError as exc:
        return MistakesSourceContextOutcome(failure=_constraint_failure(exc))
    except (SourceFlowError, ValueError) as exc:
        return MistakesSourceContextOutcome(
            failure=SourceContextFailure({"error": str(exc)}, 400)
        )
    return MistakesSourceContextOutcome(
        context=MistakesSourceContext(
            mistakes=tuple(dict(item) for item in flow.mistakes),
            subject_id=flow.subject_id,
            subject=flow.subject,
            chapters=tuple(flow.chapters),
            difficulty=flow.difficulty,
            target=flow.target,
            question_count=flow.question_count,
            question_types=tuple(flow.question_types),
            note=flow.note,
            title=flow.title,
            exam_type=flow.exam_type,
            system_prompt=flow.system_prompt,
            prompt=flow.prompt,
            frozen_evidence=flow.frozen_evidence,
            minimum_coverage=flow.minimum_coverage,
            lineage_slots=flow.lineage_slots,
            public_id_by_internal_id=dict(flow.public_id_by_internal_id),
            allow_adaptation=allow_adaptation,
            memory_context=str(flow.memory_context),
            training_target=flow.training_target,
        )
    )


def prepare_past_source_context(
    payload: Mapping[str, Any],
    *,
    db: Any,
    memory: Any,
    config: Mapping[str, Any],
    positive_int: Callable[[Any, str], int],
    bounded_text: Callable[..., str],
    candidate_sources: frozenset[str] | set[str],
    candidate_quotas: Callable[..., Mapping[str, int]],
    source_error_response: Callable[..., tuple[Mapping[str, Any], int]],
) -> PastSourceContextOutcome:
    """Resolve official source identity and reference structure without AI work."""

    try:
        subject_id = positive_int(payload.get("subject_id"), "真题科目 ID")
        paper_count = validate_single_paper_count(payload.get("paper_count", 1))
        requested_institution = bounded_text(
            payload.get("institution", ""), "院校", 100
        )
        note = bounded_text(payload.get("note", ""), "补充要求", 2000)
        raw_candidate_source = payload.get("candidate_source", "ordinary")
        if not isinstance(raw_candidate_source, str):
            raise ValueError("真题候选来源选项无效")
        candidate_source = raw_candidate_source.strip()
        if candidate_source not in candidate_sources:
            raise ValueError("真题候选来源选项无效")
    except ExamGenerationConstraintError as exc:
        return PastSourceContextOutcome(failure=_constraint_failure(exc))
    except ValueError as exc:
        return PastSourceContextOutcome(
            failure=SourceContextFailure({"error": str(exc)}, 400)
        )

    subject_row = db.execute(
        "SELECT name FROM subjects WHERE id=?", (subject_id,)
    ).fetchone()
    if not subject_row:
        return PastSourceContextOutcome(
            failure=SourceContextFailure({"error": "科目不存在"}, 400)
        )
    subject = str(subject_row["name"])
    memory_context = _read_mode_memory_context(
        memory,
        mode="past",
        actor="past_exam",
        filters={"task": "exam_generation", "subject": subject},
    )
    past_directory = str(config.get("PAST_EXAM_DIR") or "").strip()
    if not past_directory:
        return PastSourceContextOutcome(
            failure=SourceContextFailure(
                {"error": "未配置真题 TeX 目录，无法进行真题组卷"}, 503
            )
        )
    try:
        raw_source = source_for_subject(
            past_directory,
            subject,
            requested_institution or None,
        )
    except PastExamSourceError as exc:
        body, status = source_error_response(exc, fallback_status=400)
        return PastSourceContextOutcome(
            failure=SourceContextFailure(dict(body), int(status))
        )
    institution = str(raw_source["institution"])
    try:
        reference_year = positive_int(
            payload.get("reference_year", raw_source["latest_year"]),
            "结构年份",
        )
        raw_reference_section = reference_exam_section(
            raw_source,
            reference_year=reference_year,
        )
        raw_structure_contract = reference_exam_structure(
            raw_source,
            reference_year=reference_year,
        )
        source_quotas = dict(
            candidate_quotas(
                candidate_source,
                int(raw_structure_contract["question_count"]),
                institution=institution,
                subject=subject,
                reference_year=reference_year,
            )
        )
    except PastExamSourceError as exc:
        body, status = source_error_response(exc, fallback_status=400)
        return PastSourceContextOutcome(
            failure=SourceContextFailure(dict(body), int(status))
        )
    except ValueError as exc:
        return PastSourceContextOutcome(
            failure=SourceContextFailure({"error": str(exc)}, 400)
        )
    try:
        allow_adaptation = _allow_adaptation(payload)
    except ValueError as exc:
        return PastSourceContextOutcome(
            failure=SourceContextFailure({"error": str(exc)}, 400)
        )

    structure_contract = _serializable_value(raw_structure_contract)
    reference_section = _serializable_value(raw_reference_section)
    past_source = _serializable_value(raw_source)
    question_count = int(structure_contract["question_count"])
    question_types = tuple(
        str(section["label"]) for section in structure_contract["sections"]
    )
    chapters = (f"{reference_year}年真题结构",)
    return PastSourceContextOutcome(
        context=PastSourceContext(
            subject_id=subject_id,
            subject=subject,
            chapters=chapters,
            difficulty="真题模拟",
            target="真题模拟",
            question_count=question_count,
            question_types=question_types,
            note=note,
            title=f"{institution} · {subject} - {reference_year}年结构真题模拟卷",
            exam_type="真题模拟组卷",
            system_prompt="",
            prompt="",
            memory_context=str(memory_context),
            past_source=past_source,
            institution=institution,
            paper_count=paper_count,
            allow_adaptation=allow_adaptation,
            reference_year=reference_year,
            reference_section=reference_section,
            structure_contract=structure_contract,
            candidate_source=candidate_source,
            source_quotas=source_quotas,
        )
    )


__all__ = [
    "MODE_MEMORY_READ_SNAPSHOT_VERSION",
    "FreeSourceContext",
    "FreeSourceContextOutcome",
    "MistakesSourceContext",
    "MistakesSourceContextOutcome",
    "PastSourceContext",
    "PastSourceContextOutcome",
    "SourceContextFailure",
    "prepare_free_source_context",
    "prepare_mistakes_source_context",
    "prepare_past_source_context",
    "read_optional_exam_memory_context",
    "read_optional_recent_question_fingerprints",
    "read_optional_recent_question_ids",
    "task_frozen_mode_memory",
]
