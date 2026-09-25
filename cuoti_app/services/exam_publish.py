"""Database and artifact publication for generated exam batches."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from ..transactions import immediate_transaction
from .exam_sources import remove_exam_artifacts
from .exam_drafts import FlowOutcome
from .job_contracts import (
    JobCancelled,
    JobLeaseLost,
    PermanentJobError,
    WorkerStopRequested,
)
from .tex import TexCompileError
from .exam_scoring import ExamScoringProjection, normalize_scoring_projection
from .exam_snapshots import extract_exam_item_snapshots
from .exam_generation_memory_delivery import (
    GenerationMemoryDeliveryError,
    discard_pending_generation_memory_delivery,
)


_PUBLISHED_EXAM_RESULT_FIELDS = (
    "id",
    "title",
    "content",
    "pdf_url",
    "created_at",
)


@dataclass(frozen=True, slots=True)
class PublishedExamResult(Mapping[str, Any]):
    id: int
    title: str
    content: str
    pdf_url: str
    created_at: str

    def __post_init__(self) -> None:
        if isinstance(self.id, bool) or not isinstance(self.id, int) or self.id <= 0:
            raise ValueError("published exam id must be a positive integer")
        for field_name in _PUBLISHED_EXAM_RESULT_FIELDS[1:]:
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"published exam {field_name} must be a string")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PublishedExamResult":
        if not isinstance(value, Mapping):
            raise TypeError("published exam result must be a mapping")
        fields = set(value)
        required = set(_PUBLISHED_EXAM_RESULT_FIELDS)
        if fields != required:
            missing = sorted(required - fields)
            unexpected = sorted(fields - required)
            raise ValueError(
                "published exam result schema mismatch: "
                f"missing={missing}, unexpected={unexpected}"
            )
        return cls(**{name: value[name] for name in _PUBLISHED_EXAM_RESULT_FIELDS})

    def __getitem__(self, key: str) -> Any:
        if key not in _PUBLISHED_EXAM_RESULT_FIELDS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(_PUBLISHED_EXAM_RESULT_FIELDS)

    def __len__(self) -> int:
        return len(_PUBLISHED_EXAM_RESULT_FIELDS)

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in _PUBLISHED_EXAM_RESULT_FIELDS}


@dataclass(frozen=True, slots=True)
class PublishedExamBatch:
    results: tuple[PublishedExamResult, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.results, tuple):
            raise TypeError("published exam results must be a tuple")
        if not all(isinstance(result, PublishedExamResult) for result in self.results):
            raise TypeError("published exam results must use PublishedExamResult")
        if len(set(self.exam_ids)) != len(self.exam_ids):
            raise ValueError("published exam ids must be unique")

    @property
    def exam_ids(self) -> tuple[int, ...]:
        return tuple(result.id for result in self.results)

    @classmethod
    def from_mappings(
        cls,
        results: Sequence[Mapping[str, Any]],
        *,
        expected_exam_ids: Sequence[int],
    ) -> "PublishedExamBatch":
        frozen_results = tuple(
            result
            if isinstance(result, PublishedExamResult)
            else PublishedExamResult.from_mapping(result)
            for result in results
        )
        published = cls(results=frozen_results)
        expected = tuple(expected_exam_ids)
        if published.exam_ids != expected:
            raise ValueError(
                "published exam result ids do not match the committed batch"
            )
        return published


@dataclass(frozen=True, slots=True)
class ExamPublicationFence:
    """Ephemeral worker authority; never part of the generation identity."""

    job_id: str
    worker_id: str
    lease_attempt: int

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise ValueError("publication fence job id must not be empty")
        if not isinstance(self.worker_id, str) or not self.worker_id.strip():
            raise ValueError("publication fence worker id must not be empty")
        if (
            isinstance(self.lease_attempt, bool)
            or not isinstance(self.lease_attempt, int)
            or self.lease_attempt < 1
        ):
            raise ValueError("publication fence lease attempt must be positive")


def _publication_fence_error(reason: str, message: str) -> PermanentJobError:
    return PermanentJobError(
        message,
        code="exam_publication_binding_fenced",
        category="contract",
        details={"reason": reason},
    )


def _assert_worker_publication_fence(
    db,
    *,
    generation_job_id: str | None,
    publication_fence: ExamPublicationFence | None,
) -> None:
    if generation_job_id is None:
        if publication_fence is not None:
            raise JobLeaseLost("当前 worker attempt 的发布任务身份不一致")
        return
    if (
        publication_fence is None
        or publication_fence.job_id != generation_job_id
    ):
        raise JobLeaseLost("当前 worker attempt 不持有该任务发布权限")
    row = db.execute(
        "SELECT kind,status,cancel_requested,lease_owner,attempts "
        "FROM background_jobs WHERE id=?",
        (generation_job_id,),
    ).fetchone()
    if row is None:
        raise JobLeaseLost("当前 worker attempt 不持有该任务发布权限")
    if (
        str(row["kind"] or "") != "exam-generation"
        or str(row["lease_owner"] or "") != publication_fence.worker_id
        or int(row["attempts"] or 0) != publication_fence.lease_attempt
    ):
        raise JobLeaseLost("当前 worker attempt 不持有该任务发布权限")
    if bool(row["cancel_requested"]) or row["status"] == "cancel_requested":
        raise JobCancelled("任务已请求取消")
    if str(row["status"] or "") != "running":
        raise JobLeaseLost("当前 worker attempt 不持有该任务发布权限")
    binding = db.execute(
        "SELECT binding_hash FROM exam_generation_attempt_bindings WHERE job_id=?",
        (generation_job_id,),
    ).fetchone()
    if binding is not None:
        raise _publication_fence_error(
            "legacy_publisher_blocked_by_attempt_binding",
            "当前真题任务已建立 v3 原生绑定，旧发布链已被阻止",
        )


def _assert_initial_publication_slot(db, generation_job_id: str | None) -> None:
    if generation_job_id is None:
        return
    residue = db.execute(
        "SELECT EXISTS(SELECT 1 FROM exams WHERE generation_job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_exam_results WHERE job_id=?) "
        "OR EXISTS(SELECT 1 FROM job_business_results WHERE job_id=?)",
        (generation_job_id, generation_job_id, generation_job_id),
    ).fetchone()
    if bool(residue[0]):
        raise PermanentJobError(
            "当前任务已有发布事实，不能重复发布",
            code="publication_failed",
            category="contract",
            details={"reason": "publication_residue_detected"},
        )


def _assert_final_publication_slot(
    db,
    *,
    generation_job_id: str | None,
    exam_id: int,
) -> None:
    row = db.execute(
        "SELECT generation_job_id,artifact_status FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    if row is None or str(row["artifact_status"] or "") != "publishing":
        raise PermanentJobError(
            "试卷发布暂存状态已发生冲突",
            code="publication_failed",
            category="contract",
            details={"reason": "publishing_exam_state_conflict"},
        )
    actual_job_id = row["generation_job_id"]
    if actual_job_id != generation_job_id:
        raise PermanentJobError(
            "试卷发布任务身份已发生冲突",
            code="publication_failed",
            category="contract",
            details={"reason": "publishing_exam_job_mismatch"},
        )


@dataclass(frozen=True)
class ExamPaperDraft:
    title: str
    body: str
    config: dict[str, Any]
    latex_source: str
    structured_items: Sequence[dict[str, Any]] = ()
    scoring: Mapping[str, Any] | ExamScoringProjection | None = None

    def to_flow_outcome(self, *, source: str) -> FlowOutcome:
        """Adapt the legacy facade draft to the pure source-flow contract."""

        return FlowOutcome(
            source=source,
            title=self.title,
            latex_body=self.body,
            latex_source=self.latex_source,
            config=self.config,
            structured_items=self.structured_items,
        )


def compensate_exam_publication(
    db,
    ordered_exam_ids: Sequence[int],
    *,
    pdf_dir: str,
    warn: Callable[..., None] | None = None,
    memory_delivery_operation_id: str | None = None,
) -> None:
    """Best-effort removal of one ordered published batch and its artifacts."""

    created_ids = tuple(ordered_exam_ids)

    def safe_warn(*args, **kwargs) -> None:
        if warn is None:
            return
        try:
            warn(*args, **kwargs)
        except Exception:
            pass

    try:
        db.rollback()
    except Exception:
        safe_warn("failed to roll back exam publication transaction", exc_info=True)
    if not created_ids:
        return
    try:
        db.execute("BEGIN IMMEDIATE")
        if memory_delivery_operation_id is not None:
            discard_pending_generation_memory_delivery(
                db,
                operation_id=memory_delivery_operation_id,
            )
        placeholders = ",".join("?" for _ in created_ids)
        db.execute(f"DELETE FROM exams WHERE id IN ({placeholders})", created_ids)
        db.commit()
    except GenerationMemoryDeliveryError as exc:
        try:
            db.rollback()
        except Exception:
            pass
        # A delivered envelope proves that publication crossed the durable
        # success boundary.  Compensation must never erase that exam/history.
        if exc.reason == "memory_delivery_discard_delivered_forbidden":
            safe_warn(
                "refused to compensate a delivered exam memory publication",
                exc_info=True,
            )
            return
        safe_warn("failed to discard pending exam memory delivery", exc_info=True)
        return
    except Exception:
        try:
            db.rollback()
        except Exception:
            safe_warn(
                "failed to roll back exam compensation transaction",
                exc_info=True,
            )
        safe_warn("failed to delete compensated exam rows", exc_info=True)
        return
    try:
        remove_exam_artifacts(created_ids, pdf_dir=pdf_dir, warn=safe_warn)
    except Exception:
        safe_warn("failed to remove compensated exam artifacts", exc_info=True)


def publish_exam_batch_result(
    db,
    *,
    subject_id: int,
    source: str,
    generation_job_id: str | None,
    drafts: Sequence[ExamPaperDraft],
    reference_mistakes: Sequence[dict[str, Any]],
    pdf_dir: str,
    xelatex_command: str,
    xelatex_timeout: int,
    testing: bool,
    compile_exam: Callable[..., None],
    store_exam_items: Callable[..., None],
    progress: Callable[[int, int, str], None],
    created_at: str,
    warn: Callable[..., None] | None = None,
    publication_fence: ExamPublicationFence | None = None,
    stage_published: Callable[[Any, PublishedExamResult], None] | None = None,
    memory_delivery_operation_id: str | None = None,
) -> PublishedExamBatch:
    """Publish a batch and compensate every row/file created on failure."""

    created_ids: list[int] = []
    results: list[dict[str, Any]] = []
    try:
        for paper_index, draft in enumerate(drafts, start=1):
            flow_draft = draft.to_flow_outcome(source=source)
            snapshot_count = len(extract_exam_item_snapshots(flow_draft.latex_body))
            if snapshot_count < 1:
                raise ValueError("试卷没有可发布的结构化题目快照")
            scoring = normalize_scoring_projection(
                draft.scoring,
                item_count=snapshot_count,
            )
            # Progress callbacks are also the durable deadline/cancellation
            # fence.  Run it before creating even a temporary publishing row.
            progress(6, 7, f"正在编译第 {paper_index}/{len(drafts)} 份试卷")
            latex_sha256 = hashlib.sha256(
                flow_draft.latex_source.encode("utf-8")
            ).hexdigest()
            with immediate_transaction(db):
                _assert_worker_publication_fence(
                    db,
                    generation_job_id=generation_job_id,
                    publication_fence=publication_fence,
                )
                _assert_initial_publication_slot(db, generation_job_id)
                db.execute(
                    "INSERT INTO exams(subject_id,title,config,content,created_at,"
                    "generation_job_id,batch_index,artifact_status,latex_source,"
                    "latex_sha256,artifact_revision,recompile_available,scoring_mode,"
                    "total_points,scoring_evidence_hash,scoring_projection_hash) "
                    "VALUES(?,?,?,?,?,?,?,'publishing',?,?,1,0,?,?,?,?)",
                    (
                        subject_id, flow_draft.title,
                        json.dumps(flow_draft.config, ensure_ascii=False),
                        flow_draft.latex_body, created_at, generation_job_id,
                        paper_index, flow_draft.latex_source, latex_sha256,
                        scoring.mode, scoring.total_points,
                        scoring.evidence_hash, scoring.projection_hash,
                    ),
                )
                exam_id = int(
                    db.execute("SELECT last_insert_rowid()").fetchone()[0]
                )
                store_exam_items(
                    db, exam_id, flow_draft.latex_body, source=source,
                    structured_items=flow_draft.structured_items,
                    reference_mistakes=(
                        reference_mistakes if source == "mistakes" else ()
                    ),
                    scoring_projection=scoring,
                )
            created_ids.append(exam_id)
            compile_exam(
                pdf_dir, xelatex_command, exam_id, flow_draft.latex_source,
                timeout=xelatex_timeout,
            )
            pdf_path = Path(pdf_dir) / f"exam_{exam_id}.pdf"
            if pdf_path.is_file():
                pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            elif testing:
                pdf_sha256 = None
            else:
                raise TexCompileError("XeLaTeX 未发布试卷 PDF")
            progress(6, 7, "正在原子发布试卷产物")
            published_result = PublishedExamResult(
                id=exam_id,
                title=flow_draft.title,
                content=flow_draft.latex_body,
                pdf_url=f"/pdfs/exam_{exam_id}.pdf",
                created_at=created_at,
            )
            with immediate_transaction(db):
                _assert_worker_publication_fence(
                    db,
                    generation_job_id=generation_job_id,
                    publication_fence=publication_fence,
                )
                _assert_final_publication_slot(
                    db,
                    generation_job_id=generation_job_id,
                    exam_id=exam_id,
                )
                updated = db.execute(
                    "UPDATE exams SET artifact_status='published',pdf_sha256=?,"
                    "recompile_available=1 WHERE id=? AND artifact_status='publishing'",
                    (pdf_sha256, exam_id),
                )
                if updated.rowcount != 1:
                    raise PermanentJobError(
                        "试卷发布暂存状态已发生冲突",
                        code="publication_failed",
                        category="contract",
                        details={"reason": "publishing_exam_update_conflict"},
                    )
                if stage_published is not None:
                    stage_published(db, published_result)
            results.append(published_result.to_dict())
        published = PublishedExamBatch.from_mappings(
            results,
            expected_exam_ids=created_ids,
        )
        progress(7, 7, "正在确认试卷发布回执")
        return published
    except (WorkerStopRequested, KeyboardInterrupt, SystemExit):
        compensate_exam_publication(
            db,
            created_ids,
            pdf_dir=pdf_dir,
            warn=warn,
            memory_delivery_operation_id=memory_delivery_operation_id,
        )
        raise
    except Exception:
        compensate_exam_publication(
            db,
            created_ids,
            pdf_dir=pdf_dir,
            warn=warn,
            memory_delivery_operation_id=memory_delivery_operation_id,
        )
        raise


def publish_exam_batch(
    db,
    *,
    subject_id: int,
    source: str,
    generation_job_id: str | None,
    drafts: Sequence[ExamPaperDraft],
    reference_mistakes: Sequence[dict[str, Any]],
    pdf_dir: str,
    xelatex_command: str,
    xelatex_timeout: int,
    testing: bool,
    compile_exam: Callable[..., None],
    store_exam_items: Callable[..., None],
    progress: Callable[[int, int, str], None],
    created_at: str,
    warn: Callable[..., None] | None = None,
    publication_fence: ExamPublicationFence | None = None,
) -> list[dict[str, Any]]:
    """Compatibility facade returning the legacy mutable result list."""

    published = publish_exam_batch_result(
        db,
        subject_id=subject_id,
        source=source,
        generation_job_id=generation_job_id,
        drafts=drafts,
        reference_mistakes=reference_mistakes,
        pdf_dir=pdf_dir,
        xelatex_command=xelatex_command,
        xelatex_timeout=xelatex_timeout,
        testing=testing,
        compile_exam=compile_exam,
        store_exam_items=store_exam_items,
        progress=progress,
        created_at=created_at,
        warn=warn,
        publication_fence=publication_fence,
    )
    return [result.to_dict() for result in published.results]
