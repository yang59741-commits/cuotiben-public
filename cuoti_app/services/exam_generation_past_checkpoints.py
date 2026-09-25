"""Frozen context and atomic persistence boundary for past-paper selection."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .exam_generation_past_papers import (
    PastPaperCandidateOrderError,
    PastPaperGenerationRequest,
    legacy_selection_context_hash,
)
from .exam_repetition_policy import question_content_fingerprint
from .past_selection_checkpoints import (
    SELECTION_CHECKPOINT_SCHEMA_VERSION,
    UPSTREAM_PAPER_CHANGED_REASON,
    SelectionCheckpointConflictError,
    clear_selection_checkpoint_paper,
    downgrade_following_selection_checkpoint_papers,
    load_selection_checkpoint,
    migrate_selection_checkpoint_v1_to_v2,
    normalize_selection_checkpoint,
    reject_selection_checkpoint_candidates,
    replace_validated_selection_checkpoint_candidate,
    selection_checkpoint_context_hash,
    transition_selection_checkpoint_paper,
    upsert_selection_checkpoint_shard,
    write_selection_checkpoint,
)
from .past_selection_shards import selection_position_requirements


_REQUIRED_CANDIDATE_FIELDS = (
    "candidate_id",
    "source_kind",
    "chapter",
    "source_file",
    "source_number",
    "content",
)
_OPTIONAL_CANDIDATE_FACT_FIELDS = (
    "source_ordinal",
    "source_institution",
    "source_subject",
    "source_year",
    "source_section",
)


def _conflict(message: str) -> SelectionCheckpointConflictError:
    return SelectionCheckpointConflictError(message)


def _required_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise _conflict(f"frozen candidate {field} is invalid")
    normalized = value.strip()
    if not allow_empty and not normalized:
        raise _conflict(f"frozen candidate {field} is missing")
    return normalized


def _candidate_facts(
    request: PastPaperGenerationRequest,
) -> dict[str, dict[str, Any]]:
    if not isinstance(request.candidate_pool, tuple) or not request.candidate_pool:
        raise _conflict("frozen candidate pool is missing")
    facts: dict[str, dict[str, Any]] = {}
    for candidate in request.candidate_pool:
        if not isinstance(candidate, Mapping):
            raise _conflict("frozen candidate is invalid")
        if any(field not in candidate for field in _REQUIRED_CANDIDATE_FIELDS):
            raise _conflict("frozen candidate fact contract is incomplete")
        candidate_id = _required_text(candidate["candidate_id"], "candidate_id")
        if candidate_id in facts:
            raise _conflict("frozen candidate IDs are duplicated")
        source_kind = _required_text(candidate["source_kind"], "source_kind")
        if source_kind not in {"ordinary", "past_exam"}:
            raise _conflict("frozen candidate source_kind is invalid")
        chapter = _required_text(
            candidate["chapter"], "chapter", allow_empty=True
        )
        source_file = _required_text(candidate["source_file"], "source_file")
        source_number = candidate["source_number"]
        if (
            isinstance(source_number, bool)
            or not isinstance(source_number, int)
            or source_number < 1
        ):
            raise _conflict("frozen candidate source_number is invalid")
        content = candidate["content"]
        if not isinstance(content, str) or not content.strip():
            raise _conflict("frozen candidate content is missing")
        fact: dict[str, Any] = {
            "source_kind": source_kind,
            "chapter": chapter,
            "source_file": source_file,
            "source_number": source_number,
            "content_fingerprint": question_content_fingerprint(content),
        }
        for field in _OPTIONAL_CANDIDATE_FACT_FIELDS:
            if field in candidate and candidate[field] is not None:
                fact[field] = candidate[field]
        facts[candidate_id] = fact
    return facts


@dataclass(frozen=True, slots=True)
class PastSelectionCheckpointContext:
    job_hash: str
    legacy_context_hash: str
    context_hash: str
    expected_positions_by_paper: Mapping[int, tuple[int, ...]]
    frozen_requirements_by_paper: Mapping[
        int, Mapping[int, Mapping[str, str]]
    ]
    candidate_facts_by_id: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_request(
        cls,
        request: PastPaperGenerationRequest,
        checkpoint_identity: str,
    ) -> "PastSelectionCheckpointContext":
        if (
            not isinstance(checkpoint_identity, str)
            or not checkpoint_identity.strip()
        ):
            raise _conflict("checkpoint identity is missing")
        for field, value in (
            ("paper_count", request.paper_count),
            ("question_count", request.question_count),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise _conflict(f"frozen {field} is invalid")
        job_hash = hashlib.sha256(
            checkpoint_identity.encode("utf-8")
        ).hexdigest()
        facts = _candidate_facts(request)
        try:
            legacy_hash = legacy_selection_context_hash(request)
        except PastPaperCandidateOrderError as exc:
            raise _conflict("frozen candidate order is invalid") from exc
        requirements = selection_position_requirements(request)
        expected = {
            paper_index: tuple(range(1, request.question_count + 1))
            for paper_index in range(1, request.paper_count + 1)
        }
        frozen_requirements = {
            paper_index: {
                position: dict(requirements[position])
                for position in expected[paper_index]
            }
            for paper_index in expected
        }
        context_hash = selection_checkpoint_context_hash(
            expected_positions_by_paper=expected,
            frozen_requirements_by_paper=frozen_requirements,
            candidate_facts_by_id=facts,
        )
        return cls(
            job_hash=job_hash,
            legacy_context_hash=legacy_hash,
            context_hash=context_hash,
            expected_positions_by_paper=expected,
            frozen_requirements_by_paper=frozen_requirements,
            candidate_facts_by_id=facts,
        )


def _validate_checkpoint_context(
    checkpoint: Mapping[str, Any],
    context: PastSelectionCheckpointContext,
) -> dict[str, Any]:
    normalized = normalize_selection_checkpoint(checkpoint)
    if (
        normalized["job_hash"] != context.job_hash
        or normalized["context_hash"] != context.context_hash
    ):
        raise _conflict("selection checkpoint identity does not match")
    papers = {
        int(paper["paper_index"]): paper for paper in normalized["papers"]
    }
    if set(papers) != set(context.expected_positions_by_paper):
        raise _conflict("selection checkpoint paper set does not match")
    for paper_index, expected_positions in context.expected_positions_by_paper.items():
        if papers[paper_index]["expected_positions"] != list(expected_positions):
            raise _conflict("selection checkpoint expected positions do not match")
    return normalized


def _initial_checkpoint(
    context: PastSelectionCheckpointContext,
) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_CHECKPOINT_SCHEMA_VERSION,
        "job_hash": context.job_hash,
        "context_hash": context.context_hash,
        "papers": [
            {
                "paper_index": paper_index,
                "expected_positions": list(expected_positions),
                "validation_status": "provisional",
                "selection_hash": None,
                "rejected_candidate_ids_by_position": {},
                "shards": [],
            }
            for paper_index, expected_positions in sorted(
                context.expected_positions_by_paper.items()
            )
        ],
    }


@dataclass(slots=True)
class PastSelectionCheckpointSession:
    path: Path
    context: PastSelectionCheckpointContext
    checkpoint: dict[str, Any]

    @classmethod
    def open(
        cls,
        path: str | Path,
        context: PastSelectionCheckpointContext,
    ) -> "PastSelectionCheckpointSession":
        checkpoint_path = Path(path)
        try:
            checkpoint = load_selection_checkpoint(checkpoint_path)
        except SelectionCheckpointConflictError:
            checkpoint = migrate_selection_checkpoint_v1_to_v2(
                checkpoint_path,
                job_hash=context.job_hash,
                legacy_context_hash=context.legacy_context_hash,
                expected_positions_by_paper=(
                    context.expected_positions_by_paper
                ),
                frozen_requirements_by_paper=(
                    context.frozen_requirements_by_paper
                ),
                candidate_facts_by_id=context.candidate_facts_by_id,
            )
        if checkpoint is None:
            checkpoint = write_selection_checkpoint(
                checkpoint_path, _initial_checkpoint(context)
            )
        return cls(
            path=checkpoint_path,
            context=context,
            checkpoint=_validate_checkpoint_context(checkpoint, context),
        )

    def _accept(self, checkpoint: Mapping[str, Any]) -> dict[str, Any]:
        self.checkpoint = _validate_checkpoint_context(
            checkpoint, self.context
        )
        return self.checkpoint

    def refresh(self) -> dict[str, Any]:
        checkpoint = load_selection_checkpoint(self.path)
        if checkpoint is None:
            raise _conflict("selection checkpoint no longer exists")
        return self._accept(checkpoint)

    def upsert_selection_shard(
        self, *, paper_index: int, shard: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._accept(upsert_selection_checkpoint_shard(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
            shard=shard,
        ))

    def mark_selection_validated(
        self, *, paper_index: int
    ) -> dict[str, Any]:
        return self._accept(transition_selection_checkpoint_paper(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
            target_status="selection_validated",
        ))

    def reject_selection_candidates(
        self,
        *,
        paper_index: int,
        rejected_candidate_ids_by_position: Mapping[str | int, list[str]],
    ) -> dict[str, Any]:
        return self._accept(reject_selection_checkpoint_candidates(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
            rejected_candidate_ids_by_position=(
                rejected_candidate_ids_by_position
            ),
        ))

    def clear_selection_paper(
        self, *, paper_index: int
    ) -> dict[str, Any]:
        return self._accept(clear_selection_checkpoint_paper(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
        ))

    def mark_quality_validated(
        self, *, paper_index: int, reviewed_selection_hash: str
    ) -> dict[str, Any]:
        return self._accept(transition_selection_checkpoint_paper(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
            target_status="quality_validated",
            reviewed_selection_hash=reviewed_selection_hash,
        ))

    def replace_validated_candidate(
        self,
        *,
        paper_index: int,
        position: int,
        expected_candidate_id: str,
        replacement_candidate_id: str,
        validated_selection_hash: str,
    ) -> dict[str, Any]:
        return self._accept(replace_validated_selection_checkpoint_candidate(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            paper_index=paper_index,
            position=position,
            expected_candidate_id=expected_candidate_id,
            replacement_candidate_id=replacement_candidate_id,
            validated_selection_hash=validated_selection_hash,
        ))

    def downgrade_following(
        self, *, changed_paper_index: int
    ) -> dict[str, Any]:
        return self._accept(downgrade_following_selection_checkpoint_papers(
            self.path,
            job_hash=self.context.job_hash,
            context_hash=self.context.context_hash,
            changed_paper_index=changed_paper_index,
            reason=UPSTREAM_PAPER_CHANGED_REASON,
        ))


__all__ = [
    "PastSelectionCheckpointContext",
    "PastSelectionCheckpointSession",
]
