"""Past-paper candidate ordering, selection and body generation."""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Mapping, Sequence

from .exam_selection import EXAM_AI_TIMEOUT_SECONDS
from .exam_tex import audit_question_difficulties
from .past_exam_flow import (
    PastGenerationInput,
    PastGenerationResult,
    PastSelectionValidationResult,
    generate_past_exam,
    validate_past_exam_selections,
)
from .past_exams import build_exam_body, generated_question_count
from .past_selection_shards import stable_json_hash


@dataclass(frozen=True, slots=True)
class PastPaperGenerationRequest:
    """Serializable inputs for selecting and assembling frozen past papers."""

    generation_job_id: str | None
    institution: str
    subject: str
    paper_count: int
    question_count: int
    reference_year: int
    structure_contract: Mapping[str, Any]
    section_slot_contract: Mapping[str, Any] | None
    past_analysis: Mapping[str, Any]
    material_understanding: Mapping[str, Any]
    candidate_pool: tuple[Mapping[str, Any], ...]
    paper_seeds: tuple[str, ...]
    paper_candidate_orders: tuple[tuple[str, ...], ...] | None
    candidate_source: str
    source_quotas: Mapping[str, int]
    chapter_plan: tuple[Mapping[str, Any], ...]
    difficulty_quota: Mapping[str, int]
    allow_adaptation: bool
    note: str
    past_source: Mapping[str, Any]
    testing: bool
    recent_question_ids: tuple[str, ...]
    recent_content_fingerprints: tuple[str, ...] = ()
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PastPaperGenerationResult:
    """Only the generated state consumed by later orchestration stages."""

    selections: tuple[tuple[Mapping[str, Any], ...], ...]
    diversity_audit: tuple[Mapping[str, Any], ...]
    latex_bodies: tuple[str, ...]
    selection_hashes: Mapping[int, str] = field(default_factory=dict)
    job_hash: str = ""
    context_hash: str = ""
    legacy_context_hash: str = ""


class PastPaperCandidateOrderError(RuntimeError):
    """A frozen paper order no longer matches the authoritative candidate pool."""


def _ordered_candidate_pools(
    request: PastPaperGenerationRequest,
) -> list[list[Mapping[str, Any]]]:
    candidate_ids = [
        str(item["candidate_id"]) for item in request.candidate_pool
    ]
    if len(set(candidate_ids)) != len(candidate_ids):
        raise PastPaperCandidateOrderError("冻结 paper 候选顺序损坏")
    if (
        request.paper_candidate_orders is not None
        and len(request.paper_candidate_orders) != request.paper_count
    ):
        raise PastPaperCandidateOrderError("冻结 paper 候选顺序损坏")
    if len(request.paper_seeds) != request.paper_count:
        raise PastPaperCandidateOrderError("冻结 paper 候选顺序损坏")

    pools: list[list[Mapping[str, Any]]] = []
    for paper_index in range(request.paper_count):
        frozen_order = (
            request.paper_candidate_orders[paper_index]
            if request.paper_candidate_orders is not None
            else None
        )
        if request.paper_candidate_orders is not None:
            by_id = dict(zip(candidate_ids, request.candidate_pool))
            if (
                not frozen_order
                or any(
                    not isinstance(candidate_id, str)
                    for candidate_id in frozen_order
                )
                or set(frozen_order) != set(candidate_ids)
                or len(frozen_order) != len(candidate_ids)
            ):
                raise PastPaperCandidateOrderError("冻结 paper 候选顺序损坏")
            prompt_candidates = [
                by_id[candidate_id] for candidate_id in frozen_order
            ]
        else:
            prompt_candidates = list(request.candidate_pool)
            random.Random(request.paper_seeds[paper_index]).shuffle(
                prompt_candidates
            )
        pools.append(prompt_candidates)
    return pools


def _legacy_selection_context_hash_from_pools(
    request: PastPaperGenerationRequest,
    paper_candidate_pools: Sequence[Sequence[Mapping[str, Any]]],
) -> str:
    legacy_job_hash = hashlib.sha256(
        str(request.generation_job_id or "anonymous").encode("utf-8")
    ).hexdigest()
    return stable_json_hash({
        "job_hash": legacy_job_hash,
        "paper_count": int(request.paper_count),
        "question_count": int(request.question_count),
        "candidate_orders": [
            [str(item.get("candidate_id") or "") for item in candidates]
            for candidates in paper_candidate_pools
        ],
        "source_quotas": dict(request.source_quotas),
        "chapter_plan": [dict(item) for item in request.chapter_plan],
        "structure_contract": request.structure_contract,
        "section_slot_contract": request.section_slot_contract,
    })


def legacy_selection_context_hash(
    request: PastPaperGenerationRequest,
) -> str:
    """Reproduce the v1 checkpoint identity from fully frozen inputs."""

    return _legacy_selection_context_hash_from_pools(
        request, _ordered_candidate_pools(request)
    )


def _past_generation_input(
    request: PastPaperGenerationRequest,
    paper_candidate_pools: Sequence[Sequence[Mapping[str, Any]]],
    *,
    selection_checkpoint: Mapping[str, Any],
    job_hash: str,
    context_hash: str,
) -> PastGenerationInput:
    assembly_source = dict(request.past_source)
    assembly_source["sections"] = request.structure_contract["sections"]
    legacy_job_hash = hashlib.sha256(
        str(request.generation_job_id or "anonymous").encode("utf-8")
    ).hexdigest()
    return PastGenerationInput(
        institution=request.institution,
        subject=request.subject,
        paper_count=request.paper_count,
        question_count=request.question_count,
        reference_year=request.reference_year,
        structure_contract=request.structure_contract,
        section_slot_contract=request.section_slot_contract,
        past_analysis=request.past_analysis,
        material_understanding=request.material_understanding,
        paper_candidate_pools=paper_candidate_pools,
        candidate_source=request.candidate_source,
        source_quotas=request.source_quotas,
        chapter_plan=list(request.chapter_plan),
        # Past-paper execution is exact-slot only.  Aggregate evidence remains
        # available in characterization receipts, not as an execution quota.
        difficulty_quota={},
        allow_adaptation=request.allow_adaptation,
        note=request.note,
        assembly_source=assembly_source,
        selection_checkpoint=selection_checkpoint,
        job_hash=job_hash,
        context_hash=context_hash,
        testing=request.testing,
        recent_question_ids=tuple(sorted(request.recent_question_ids)),
        recent_content_fingerprints=frozenset(
            request.recent_content_fingerprints
        ),
        unmastered_content_fingerprints=frozenset(
            request.unmastered_content_fingerprints
        ),
        candidate_profiles={
            str(card.get("candidate_id")): card
            for card in (request.material_understanding.get("cards") or [])
            if isinstance(card, dict) and card.get("candidate_id")
        },
        legacy_selection_job_hash=legacy_job_hash,
    )


def _validate_selection_callback(
    request: PastPaperGenerationRequest,
    selection_validator: Callable[..., Sequence[Mapping[str, Any]]],
    raw: Any,
    candidates: Sequence[Mapping[str, Any]],
    *,
    audit: bool,
) -> Sequence[Mapping[str, Any]]:
    kwargs: dict[str, Any] = {
        "allow_adaptation": False,
        "subject": request.subject,
        "enforce_chapter_plan": request.candidate_source == "ordinary",
        "source_quotas": request.source_quotas,
        "target_institution": request.institution,
    }
    # The legacy whole-paper difficulty auditor could overwrite labels after
    # exact-slot validation.  Past selections are always checked by the frozen
    # section-slot contract instead.
    kwargs["audit_questions"] = None
    return selection_validator(
        raw,
        candidates,
        request.question_count,
        list(request.chapter_plan),
        {},
        **kwargs,
    )


def validate_past_paper_selections(
    request: PastPaperGenerationRequest,
    selections: Sequence[Sequence[Mapping[str, Any]]],
    *,
    selection_checkpoint: Mapping[str, Any],
    job_hash: str,
    context_hash: str,
    selection_validator: Callable[..., Sequence[Mapping[str, Any]]],
    exam_body_builder: Callable[..., str] = build_exam_body,
    question_count_reader: Callable[[str], int] = generated_question_count,
) -> PastSelectionValidationResult:
    """Revalidate complete replacements without provider or storage effects."""

    paper_candidate_pools = _ordered_candidate_pools(request)
    state = _past_generation_input(
        request,
        paper_candidate_pools,
        selection_checkpoint=selection_checkpoint,
        job_hash=job_hash,
        context_hash=context_hash,
    )
    return validate_past_exam_selections(
        state,
        selections,
        validate_selection=lambda raw, candidates: _validate_selection_callback(
            request,
            selection_validator,
            raw,
            candidates,
            audit=False,
        ),
        build_exam_body=exam_body_builder,
        generated_question_count=question_count_reader,
    )


def generate_past_papers(
    request: PastPaperGenerationRequest,
    *,
    app_context: Callable[[], ContextManager[Any]],
    ai_call: Callable[..., Any],
    progress: Callable[[int, int, str], Any],
    selection_validator: Callable[..., Sequence[Mapping[str, Any]]],
    past_exam_generator: Callable[..., PastGenerationResult] = generate_past_exam,
    difficulty_auditor: Callable[..., Sequence[str]] = (
        audit_question_difficulties
    ),
    exam_body_builder: Callable[..., str] = build_exam_body,
    question_count_reader: Callable[[str], int] = generated_question_count,
    selection_checkpoint: Mapping[str, Any],
    job_hash: str,
    context_hash: str,
    upsert_selection_shard: Callable[..., Mapping[str, Any]],
    mark_selection_validated: Callable[..., Mapping[str, Any]],
    reject_selection_candidates: Callable[..., Mapping[str, Any]],
    clear_selection_paper: Callable[..., Mapping[str, Any]],
) -> PastPaperGenerationResult:
    """Select and assemble past papers without retaining provider dependencies."""

    paper_candidate_pools = _ordered_candidate_pools(request)
    legacy_context_hash = _legacy_selection_context_hash_from_pools(
        request, paper_candidate_pools
    )

    def request_selection(
        selection_prompt: str,
        *,
        reasoning_effort: str | None = None,
    ) -> Any:
        provider_kwargs: dict[str, Any] = {
            "temperature": 0.2,
            "timeout": EXAM_AI_TIMEOUT_SECONDS,
            "task": "exam_generation",
            "task_policy": "candidate_selection",
            "freeze_id": request.generation_job_id,
        }
        if reasoning_effort is not None:
            provider_kwargs["reasoning_effort"] = reasoning_effort
        with app_context():
            return ai_call(
                [
                    {
                        "role": "system",
                        "content": (
                            f"你是{request.institution}{request.subject}考研选题专家。"
                            "只能从给定候选ID中选择，不得生成新题，必须只返回JSON。"
                        ),
                    },
                    {"role": "user", "content": selection_prompt},
                ],
                **provider_kwargs,
            )

    def validate_selection(
        raw: Any,
        candidates: Sequence[Mapping[str, Any]],
        *,
        audit: bool = True,
    ) -> Sequence[Mapping[str, Any]]:
        return _validate_selection_callback(
            request,
            selection_validator,
            raw,
            candidates,
            audit=audit,
        )

    def validate_selection_without_audit(
        raw: Any,
        candidates: Sequence[Mapping[str, Any]],
    ) -> Sequence[Mapping[str, Any]]:
        return validate_selection(raw, candidates, audit=False)

    progress(3, 7, "正在进行 AI 候选选择")
    flow_result = past_exam_generator(
        _past_generation_input(
            request,
            paper_candidate_pools,
            selection_checkpoint=selection_checkpoint,
            job_hash=job_hash,
            context_hash=context_hash,
        ),
        request_selection=request_selection,
        validate_selection=validate_selection,
        validate_selection_without_audit=validate_selection_without_audit,
        audit_difficulties=None,
        build_exam_body=exam_body_builder,
        generated_question_count=question_count_reader,
        upsert_selection_shard=upsert_selection_shard,
        mark_selection_validated=mark_selection_validated,
        reject_selection_candidates=reject_selection_candidates,
        clear_selection_paper=clear_selection_paper,
    )
    return PastPaperGenerationResult(
        selections=tuple(
            tuple(dict(item) for item in selection)
            for selection in flow_result.selections
        ),
        diversity_audit=tuple(
            dict(item) for item in flow_result.diversity_audit
        ),
        latex_bodies=tuple(str(body) for body in flow_result.latex_bodies),
        selection_hashes={
            int(paper_index): str(selection_hash)
            for paper_index, selection_hash in flow_result.selection_hashes.items()
        },
        job_hash=str(flow_result.job_hash),
        context_hash=str(flow_result.context_hash),
        legacy_context_hash=str(
            flow_result.legacy_context_hash or legacy_context_hash
        ),
    )


__all__ = [
    "PastPaperCandidateOrderError",
    "PastPaperGenerationRequest",
    "PastPaperGenerationResult",
    "generate_past_papers",
    "legacy_selection_context_hash",
    "validate_past_paper_selections",
]
