"""Publication, receipt error mapping and usage audit stage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_contracts import require_single_paper_sequence
from .exam_publication import (
    ExamReceiptRecoveryPending,
    confirm_exam_publication_receipt,
)
from .exam_publish import (
    ExamPaperDraft,
    ExamPublicationFence,
    PublishedExamBatch,
    PublishedExamResult,
    publish_exam_batch_result,
)
from .job_contracts import JobError
from .exam_generation_memory import append_success_events
from .exam_generation_memory_delivery import dispatch_generation_memory_delivery
from .exam_scoring import ExamScoringProjection, normalize_scoring_projection
from .exam_snapshots import extract_exam_item_snapshots


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def _load_canonical_json(
    encoded: str,
    *,
    label: str,
    expected_type: type,
) -> Any:
    if not isinstance(encoded, str):
        raise TypeError(f"{label} JSON must be a string")
    try:
        value = json.loads(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} JSON is invalid") from exc
    if not isinstance(value, expected_type):
        raise TypeError(f"{label} JSON has the wrong shape")
    if _canonical_json(value, label=label) != encoded:
        raise ValueError(f"{label} JSON must use canonical encoding")
    return value


@dataclass(frozen=True, slots=True)
class GenerationPaperDraft:
    title: str
    body: str
    config_json: str
    latex_source: str
    structured_items_json: str
    scoring_json: str = '{"evidence_hash":null,"items":[],"mode":"unscored","total_points":null}'

    def __post_init__(self) -> None:
        for field_name in ("title", "body", "latex_source"):
            if not isinstance(getattr(self, field_name), str):
                raise TypeError(f"generation draft {field_name} must be a string")
        _load_canonical_json(
            self.config_json,
            label="generation draft config",
            expected_type=dict,
        )
        items = _load_canonical_json(
            self.structured_items_json,
            label="generation draft structured items",
            expected_type=list,
        )
        if not all(isinstance(item, dict) for item in items):
            raise TypeError("generation draft structured items must contain objects")
        _load_canonical_json(
            self.scoring_json,
            label="generation draft scoring",
            expected_type=dict,
        )

    @classmethod
    def from_exam_draft(cls, draft: ExamPaperDraft) -> "GenerationPaperDraft":
        if not isinstance(draft, ExamPaperDraft):
            raise TypeError("generation draft source must be ExamPaperDraft")
        scoring = normalize_scoring_projection(
            draft.scoring,
            item_count=len(extract_exam_item_snapshots(draft.body)),
        )
        return cls(
            title=draft.title,
            body=draft.body,
            config_json=_canonical_json(
                draft.config,
                label="generation draft config",
            ),
            latex_source=draft.latex_source,
            structured_items_json=_canonical_json(
                list(draft.structured_items),
                label="generation draft structured items",
            ),
            scoring_json=_canonical_json(
                scoring.to_dict(),
                label="generation draft scoring",
            ),
        )

    def to_exam_draft(self) -> ExamPaperDraft:
        scoring = json.loads(self.scoring_json)
        if scoring == {
            "mode": "unscored", "total_points": None,
            "evidence_hash": None, "items": [],
        }:
            # Preserve the legacy facade value while the frozen request still
            # carries an explicit unscored identity.
            scoring = None
        return ExamPaperDraft(
            title=self.title,
            body=self.body,
            config=json.loads(self.config_json),
            latex_source=self.latex_source,
            structured_items=json.loads(self.structured_items_json),
            scoring=scoring,
        )


@dataclass(frozen=True, slots=True)
class GenerationReferenceMistake:
    payload_json: str

    def __post_init__(self) -> None:
        _load_canonical_json(
            self.payload_json,
            label="generation reference mistake",
            expected_type=dict,
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "GenerationReferenceMistake":
        if not isinstance(value, Mapping):
            raise TypeError("generation reference mistake must be a mapping")
        return cls(
            payload_json=_canonical_json(
                dict(value),
                label="generation reference mistake",
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.payload_json)


@dataclass(frozen=True, slots=True)
class GenerationPublicationRequest:
    subject_id: int
    source: str
    generation_job_id: str | None
    drafts: tuple[GenerationPaperDraft, ...]
    reference_mistakes: tuple[GenerationReferenceMistake, ...]
    created_at: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.subject_id, bool)
            or not isinstance(self.subject_id, int)
            or self.subject_id <= 0
        ):
            raise ValueError("publication subject id must be a positive integer")
        if self.source not in {"free", "mistakes", "past"}:
            raise ValueError("publication source is invalid")
        if self.generation_job_id is not None:
            if not isinstance(self.generation_job_id, str):
                raise TypeError("generation job id must be a string or None")
            if not self.generation_job_id.strip():
                raise ValueError("generation job id must not be empty")
        if not isinstance(self.drafts, tuple) or not all(
            isinstance(draft, GenerationPaperDraft) for draft in self.drafts
        ):
            raise TypeError("publication drafts must be frozen GenerationPaperDraft values")
        require_single_paper_sequence(self.drafts, label="待发布试卷")
        if not isinstance(self.reference_mistakes, tuple) or not all(
            isinstance(item, GenerationReferenceMistake)
            for item in self.reference_mistakes
        ):
            raise TypeError(
                "publication references must be frozen GenerationReferenceMistake values"
            )
        if not isinstance(self.created_at, str):
            raise TypeError("publication created_at must be a string")
        if not self.created_at.strip():
            raise ValueError("publication created_at must not be empty")

    @classmethod
    def from_exam_drafts(
        cls,
        *,
        subject_id: int,
        source: str,
        generation_job_id: str | None,
        drafts: Sequence[ExamPaperDraft],
        reference_mistakes: Sequence[Mapping[str, Any]],
        created_at: str,
    ) -> "GenerationPublicationRequest":
        return cls(
            subject_id=subject_id,
            source=source,
            generation_job_id=generation_job_id,
            drafts=tuple(
                GenerationPaperDraft.from_exam_draft(draft) for draft in drafts
            ),
            reference_mistakes=tuple(
                GenerationReferenceMistake.from_mapping(item)
                for item in reference_mistakes
            ),
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class ReceiptRecoveryRequired:
    body: Mapping[str, Any]
    status: int = 503


@dataclass(frozen=True)
class PublicationStageOutcome:
    results: Any = None
    error_body: Mapping[str, Any] | None = None
    error_status: int | None = None


def publish_generation_batch(
    request: GenerationPublicationRequest,
    *,
    db,
    pdf_dir: str,
    xelatex_command: str,
    xelatex_timeout: int,
    testing: bool,
    compile_exam: Callable[..., None],
    store_exam_items: Callable[..., None],
    progress: Callable[[int, int, str], None],
    warn: Callable[..., None] | None,
    publication_fence: ExamPublicationFence | None = None,
    stage_published: Callable[[Any, PublishedExamResult], None] | None = None,
    memory_delivery_operation_id: str | None = None,
    publish_batch: Callable[..., PublishedExamBatch] = publish_exam_batch_result,
) -> PublishedExamBatch:
    """Publish one frozen request using explicit runtime dependencies."""

    publish_arguments = dict(
        subject_id=request.subject_id,
        source=request.source,
        generation_job_id=request.generation_job_id,
        drafts=tuple(draft.to_exam_draft() for draft in request.drafts),
        reference_mistakes=tuple(
            item.to_dict() for item in request.reference_mistakes
        ),
        pdf_dir=pdf_dir,
        xelatex_command=xelatex_command,
        xelatex_timeout=xelatex_timeout,
        testing=testing,
        compile_exam=compile_exam,
        store_exam_items=store_exam_items,
        progress=progress,
        created_at=request.created_at,
        warn=warn,
    )
    if publication_fence is not None:
        publish_arguments["publication_fence"] = publication_fence
    if stage_published is not None:
        publish_arguments["stage_published"] = stage_published
    if memory_delivery_operation_id is not None:
        publish_arguments["memory_delivery_operation_id"] = (
            memory_delivery_operation_id
        )
    return publish_batch(db, **publish_arguments)


def confirm_generation_receipt(
    published: PublishedExamBatch,
    generation_job_id: str | None,
    *,
    database_path: str,
    receipt_writer: Callable[..., Any],
    logger,
) -> ReceiptRecoveryRequired | None:
    """Map ordinary receipt failures while preserving control exceptions."""

    if not generation_job_id:
        return None
    try:
        confirm_exam_publication_receipt(
            database_path,
            generation_job_id,
            published.exam_ids,
            receipt_writer=receipt_writer,
        )
    except JobError:
        raise
    except Exception:
        try:
            logger.exception("failed to append exam manifest receipt")
        except Exception:
            pass
        return ReceiptRecoveryRequired(
            body={
                "error": "试卷已发布，正在确认幂等收据，请稍后重试",
                "code": "receipt_recovery_pending",
            }
        )
    return None


def publish_generation_outputs(
    db,
    config,
    *,
    subject_id,
    source: str,
    generation_job_id,
    drafts,
    reference_mistakes,
    publish_batch,
    receipt_writer,
    publish_kwargs,
    publisher,
    logger,
) -> PublicationStageOutcome:
    """Run the existing atomic publisher and map only receipt recovery errors."""

    try:
        results = publisher(
            db,
            config,
            subject_id=subject_id,
            source=source,
            generation_job_id=generation_job_id,
            drafts=drafts,
            reference_mistakes=reference_mistakes,
            publish_batch=publish_batch,
            receipt_writer=receipt_writer,
            publish_kwargs=publish_kwargs,
        )
    except ExamReceiptRecoveryPending:
        logger.exception("failed to append exam manifest receipt")
        return PublicationStageOutcome(
            error_body={
                "error": "试卷已发布，正在确认幂等收据，请稍后重试",
                "code": "receipt_recovery_pending",
            },
            error_status=503,
        )
    return PublicationStageOutcome(results=results)


def record_question_bank_usage(
    bank_index,
    *,
    source: str,
    candidate_questions: Sequence[Mapping[str, Any]],
    candidate_pool: Sequence[Mapping[str, Any]],
    past_selections: Sequence[Sequence[Mapping[str, Any]]],
    generated_question_items: Sequence[Mapping[str, Any]] = (),
    logger,
) -> None:
    """Record post-publication exposure/selection without failing the result."""

    if source not in {"free", "past"}:
        return
    try:
        exposed_items = candidate_questions or candidate_pool
        if source == "past":
            exposed_items = [
                item
                for item in exposed_items
                if str(item.get("source_kind") or "ordinary") == "ordinary"
            ]
        exposed = [item["candidate_id"] for item in exposed_items]
        selected_ids = []
        if source == "free":
            allowed = {
                str(item.get("candidate_id") or "") for item in exposed_items
            }
            selected_ids = list(dict.fromkeys(
                str(reference_id)
                for item in generated_question_items
                for reference_id in (item.get("reference_ids") or [])
                if str(reference_id) in allowed
            ))
        elif source == "past":
            selected_ids = [
                item["candidate_id"]
                for selection in past_selections
                for item in selection
                if str(item.get("source_kind") or "ordinary") == "ordinary"
            ]
        bank_index.record_usage(exposed, selected_ids)
    except JobError:
        raise
    except Exception:
        try:
            logger.exception("failed to record question-bank usage")
        except Exception:
            pass


def finalize_generation_publication(
    published: PublishedExamBatch,
    generation_job_id: str | None,
    *,
    db,
    database_path: str,
    source: str,
    operation_id: str,
    response: dict[str, Any],
    receipt_writer: Callable[..., Any],
    bank_index,
    candidate_questions: Sequence[Mapping[str, Any]],
    candidate_pool: Sequence[Mapping[str, Any]],
    past_selections: Sequence[Sequence[Mapping[str, Any]]],
    generated_question_items: Sequence[Mapping[str, Any]],
    memory,
    memory_type: str,
    memory_actor: str,
    memory_events: Sequence[Mapping[str, Any]],
    logger,
) -> PublicationStageOutcome:
    """Finish a durable publication without compensating its rows or files.

    Receipt recovery takes precedence over usage and memory delivery. Free and
    mistake papers deliver their transactional outbox; legacy past papers keep
    their existing best-effort event append behavior.
    """

    recovery = confirm_generation_receipt(
        published,
        generation_job_id,
        database_path=database_path,
        receipt_writer=receipt_writer,
        logger=logger,
    )
    if recovery is not None:
        return PublicationStageOutcome(
            error_body=recovery.body, error_status=recovery.status,
        )
    record_question_bank_usage(
        bank_index if source in {"free", "past"} else None,
        source=source,
        candidate_questions=candidate_questions,
        candidate_pool=candidate_pool,
        past_selections=past_selections,
        generated_question_items=generated_question_items,
        logger=logger,
    )
    if source in {"free", "mistakes"}:
        # Delivery failures remain retryable after commit; the outbox owns retry.
        try:
            delivery = dispatch_generation_memory_delivery(
                db, operation_id=operation_id, memory=memory, logger=logger,
            )
        except Exception:
            delivery = {"state": "pending"}
        if delivery.get("state") != "delivered":
            return PublicationStageOutcome(
                error_body={
                    "error": "试卷已发布，生成记忆仍待交付，请稍后重试",
                    "code": "memory_delivery_pending",
                    "memory_delivery": "pending",
                    "retryable": True,
                },
                error_status=503,
            )
        response["memory_delivery"] = "delivered"
    elif source == "past":
        append_success_events(
            memory, memory_type, memory_actor, memory_events, logger=logger,
        )
    return PublicationStageOutcome(results=response)


__all__ = [
    "GenerationPaperDraft",
    "GenerationPublicationRequest",
    "GenerationReferenceMistake",
    "PublicationStageOutcome",
    "ReceiptRecoveryRequired",
    "confirm_generation_receipt",
    "finalize_generation_publication",
    "publish_generation_batch",
    "publish_generation_outputs",
    "record_question_bank_usage",
]
