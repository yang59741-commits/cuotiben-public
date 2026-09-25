"""Atomic local batch adjustment for an excessive easy-question ratio."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .exam_difficulty_adjustment_policy import (
    DIFFICULTY_ADJUSTMENT_POLICY_VERSION,
    EasyExcessAdjustmentPlan,
    plan_easy_excess_adjustment,
)
from .exam_quality_review import ExamQualityError, replace_exam_item_bodies
from .exam_repetition_policy import question_content_fingerprint


_SOURCE_MODES = frozenset({"past", "free", "mistakes"})
_ALL_DIFFICULTIES = frozenset({"简单", "中等", "困难"})
_HIGHER_DIFFICULTIES = frozenset({"中等", "困难"})
_BODY_FIELDS = ("content", "latex")


@dataclass(frozen=True, slots=True)
class ExamDifficultyAdjustmentResult:
    bodies: tuple[str, ...]
    papers: tuple[tuple[dict[str, Any], ...], ...]
    adjustment: Mapping[str, Any]
    warnings: tuple[Mapping[str, str], ...]


def _copied_papers(
    papers: Sequence[Sequence[Mapping[str, Any]]],
) -> tuple[tuple[dict[str, Any], ...], ...]:
    return tuple(
        tuple(deepcopy(dict(item)) for item in paper)
        for paper in papers
    )


def _body(item: Mapping[str, Any]) -> str:
    for field in _BODY_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ExamQualityError("ITEM_SCHEMA", "难度调整题位缺少正文")


def _text(item: Mapping[str, Any], *fields: str) -> str:
    for field in fields:
        value = item.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _mistake_lineage(item: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = item.get("source_mistake_ids")
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    values: list[str] = []
    for value in raw:
        if type(value) is not str or not value.strip():
            return None
        values.append(value.strip())
    if len(values) != len(set(values)):
        return None
    return tuple(sorted(values))


def _adjustment_audit(
    *,
    requested: bool,
    completed: bool,
    source_mode: str,
    requested_count: int = 0,
    replacements: Sequence[Mapping[str, Any]] = (),
    reason_code: str | None = None,
    advisory_codes: Sequence[str] = (),
) -> dict[str, Any]:
    frozen_replacements = [dict(value) for value in replacements]
    first = frozen_replacements[0] if frozen_replacements else {}
    return {
        "requested": bool(requested),
        "completed": bool(completed),
        "source_mode": source_mode,
        "paper_index": first.get("paper_index"),
        "position": first.get("position"),
        "from": first.get("from"),
        "to": first.get("to"),
        "reason_code": reason_code,
        "requested_count": int(requested_count),
        "replacement_count": len(frozen_replacements) if completed else 0,
        "replacements": frozen_replacements if completed else [],
        "atomic": True,
        "policy_version": DIFFICULTY_ADJUSTMENT_POLICY_VERSION,
        "advisory_codes": list(advisory_codes),
    }


def _warning() -> tuple[Mapping[str, str], ...]:
    return ({
        "code": "DIFFICULTY_ADJUSTMENT_NOT_APPLIED",
        "message": "简单题超额批量调整未完整匹配，已原样保留整卷。",
    },)


def _result_from_originals(
    *,
    bodies: Sequence[str],
    papers: Sequence[Sequence[Mapping[str, Any]]],
    adjustment: Mapping[str, Any],
    warned: bool,
) -> ExamDifficultyAdjustmentResult:
    return ExamDifficultyAdjustmentResult(
        bodies=tuple(str(value) for value in bodies),
        papers=_copied_papers(papers),
        adjustment=dict(adjustment),
        warnings=_warning() if warned else (),
    )


def _eligible_offsets(
    paper_count: int,
    raw_indexes: Sequence[int] | None,
) -> tuple[int, ...]:
    if raw_indexes is None:
        return tuple(range(paper_count))
    normalized: list[int] = []
    for value in raw_indexes:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 1
            or value > paper_count
        ):
            raise ExamQualityError(
                "DIFFICULTY_ADJUSTMENT_PAPER_INDEX",
                "难度调整卷号无效",
            )
        normalized.append(value - 1)
    if len(normalized) != len(set(normalized)):
        raise ExamQualityError(
            "DIFFICULTY_ADJUSTMENT_PAPER_INDEX",
            "难度调整卷号重复",
        )
    return tuple(normalized)


def _validated_paper_labels(
    paper: Sequence[Mapping[str, Any]],
) -> tuple[str, ...]:
    if not paper:
        raise ExamQualityError(
            "DIFFICULTY_LABEL_INVALID",
            "逐题难度标签不能为空",
        )
    labels: list[str] = []
    for item in paper:
        if not isinstance(item, Mapping):
            raise ExamQualityError(
                "DIFFICULTY_LABEL_INVALID",
                "逐题难度项目无效",
            )
        value = item.get("difficulty")
        if type(value) is not str or value not in _ALL_DIFFICULTIES:
            raise ExamQualityError(
                "DIFFICULTY_LABEL_INVALID",
                "逐题难度必须是简单、中等或困难",
            )
        labels.append(value)
    return tuple(labels)


def _used_candidate_ids(
    papers: Sequence[Sequence[Mapping[str, Any]]],
) -> set[str]:
    return {
        candidate_id
        for paper in papers
        for item in paper
        if (candidate_id := str(item.get("candidate_id") or "").strip())
    }


def _baseline_fingerprints(
    papers: Sequence[Sequence[Mapping[str, Any]]],
) -> set[str]:
    return {
        question_content_fingerprint(_body(item))
        for paper in papers
        for item in paper
    }


def _candidate_records(
    candidate_pool: Sequence[Mapping[str, Any]],
    *,
    used_ids: set[str],
    baseline_fingerprints: set[str],
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    for pool_index, candidate in enumerate(candidate_pool):
        if not isinstance(candidate, Mapping):
            continue
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        raw_difficulty = candidate.get("difficulty")
        if raw_difficulty is None:
            continue
        if (
            type(raw_difficulty) is not str
            or raw_difficulty not in _ALL_DIFFICULTIES
        ):
            raise ExamQualityError(
                "DIFFICULTY_LABEL_INVALID",
                "候选逐题难度必须是简单、中等或困难",
            )
        difficulty = raw_difficulty
        if difficulty not in _HIGHER_DIFFICULTIES:
            continue
        try:
            content = _body(candidate)
        except ExamQualityError:
            continue
        fingerprint = question_content_fingerprint(content)
        if (
            not candidate_id
            or candidate_id in used_ids
            or candidate_id in seen_ids
            or fingerprint in baseline_fingerprints
            or fingerprint in seen_fingerprints
        ):
            continue
        seen_ids.add(candidate_id)
        seen_fingerprints.add(fingerprint)
        records.append({
            "pool_index": pool_index,
            "candidate_id": candidate_id,
            "difficulty": difficulty,
            "content": content,
            "fingerprint": fingerprint,
            "item": candidate,
        })
    return tuple(records)


def _compatible(
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    source_mode: str,
) -> bool:
    if str(candidate.get("source_kind") or "ordinary").strip() != str(
        original.get("source_kind") or "ordinary"
    ).strip():
        return False
    if _text(candidate, "chapter") != _text(original, "chapter"):
        return False
    if _text(candidate, "question_type", "type") != _text(
        original, "question_type", "type"
    ):
        return False
    if source_mode == "mistakes":
        original_lineage = _mistake_lineage(original)
        return (
            original_lineage is not None
            and _mistake_lineage(candidate) == original_lineage
        )
    return True


def _match_complete_batch(
    *,
    papers: Sequence[Sequence[Mapping[str, Any]]],
    plans: Mapping[int, EasyExcessAdjustmentPlan],
    candidates: Sequence[Mapping[str, Any]],
    source_mode: str,
) -> tuple[tuple[int, int, int], ...] | None:
    """Find one deterministic all-paper assignment before any mutation."""

    requirements = {
        (paper_index, label): amount
        for paper_index, plan in plans.items()
        for label, amount in Counter(plan.target_labels).items()
        if amount
    }
    if not requirements:
        return ()
    available_candidates = set(range(len(candidates)))
    available_positions = {
        paper_index: {
            position
            for position, item in enumerate(papers[paper_index], start=1)
            if item["difficulty"] == "简单"
        }
        for paper_index in plans
    }
    assignments: list[tuple[int, int, int]] = []
    failed_states: set[tuple[object, ...]] = set()

    def compatible_pairs(
        paper_index: int,
        label: str,
    ) -> tuple[tuple[int, int], ...]:
        return tuple(
            (candidate_index, position)
            for candidate_index in sorted(available_candidates)
            if candidates[candidate_index]["difficulty"] == label
            for position in sorted(available_positions[paper_index])
            if _compatible(
                papers[paper_index][position - 1],
                candidates[candidate_index]["item"],
                source_mode=source_mode,
            )
        )

    def search() -> bool:
        remaining = sum(requirements.values())
        if remaining == 0:
            return True
        state = (
            tuple(sorted(requirements.items())),
            tuple(sorted(available_candidates)),
            tuple(
                (paper_index, tuple(sorted(positions)))
                for paper_index, positions in sorted(
                    available_positions.items()
                )
            ),
        )
        if state in failed_states:
            return False

        for paper_index in plans:
            paper_need = sum(
                amount
                for (needed_paper, _label), amount in requirements.items()
                if needed_paper == paper_index
            )
            if paper_need > len(available_positions[paper_index]):
                failed_states.add(state)
                return False
        for label in _HIGHER_DIFFICULTIES:
            label_need = sum(
                amount
                for (_paper_index, needed_label), amount in requirements.items()
                if needed_label == label
            )
            label_available = sum(
                candidates[index]["difficulty"] == label
                for index in available_candidates
            )
            if label_need > label_available:
                failed_states.add(state)
                return False

        choices: list[
            tuple[tuple[int, int, int, str], tuple[int, str], tuple[tuple[int, int], ...]]
        ] = []
        for key, amount in sorted(requirements.items()):
            if not amount:
                continue
            paper_index, label = key
            pairs = compatible_pairs(paper_index, label)
            candidate_count = len({candidate for candidate, _position in pairs})
            position_count = len({position for _candidate, position in pairs})
            if candidate_count < amount or position_count < amount:
                failed_states.add(state)
                return False
            choices.append((
                (len(pairs), candidate_count, paper_index, label),
                key,
                pairs,
            ))
        _score, key, pairs = min(choices, key=lambda value: value[0])
        paper_index, _label = key
        for candidate_index, position in pairs:
            requirements[key] -= 1
            available_candidates.remove(candidate_index)
            available_positions[paper_index].remove(position)
            assignments.append((paper_index, position, candidate_index))
            if search():
                return True
            assignments.pop()
            available_positions[paper_index].add(position)
            available_candidates.add(candidate_index)
            requirements[key] += 1
        failed_states.add(state)
        return False

    return tuple(sorted(assignments)) if search() else None


def _replacement_item(
    *,
    original: Mapping[str, Any],
    candidate: Mapping[str, Any],
    position: int,
    source_mode: str,
) -> dict[str, Any]:
    replacement = deepcopy(dict(candidate))
    for field in (
        "source_kind",
        "chapter",
        "question_type",
        "type",
        "section",
        "section_label",
        "section_index",
        "score",
        "points",
    ):
        if field in original:
            replacement[field] = deepcopy(original[field])
    if source_mode == "mistakes":
        for field in tuple(replacement):
            if field.startswith("source_"):
                replacement.pop(field, None)
        for field, value in original.items():
            if field.startswith("source_"):
                replacement[field] = deepcopy(value)
        if "lineage_policy_version" in original:
            replacement["lineage_policy_version"] = deepcopy(
                original["lineage_policy_version"]
            )
    replacement["position"] = position
    replacement["content_fingerprint"] = question_content_fingerprint(
        _body(replacement)
    )
    replacement["adapted"] = False
    replacement.pop("adapted_content", None)
    return replacement


def adjust_exam_difficulty_once(
    *,
    bodies: Sequence[str],
    papers: Sequence[Sequence[Mapping[str, Any]]],
    source_mode: str,
    overall_difficulty: str | None = None,
    candidate_pool: Sequence[Mapping[str, Any]] = (),
    unmastered_fingerprints: Sequence[str] = (),
    ai_call: Callable[..., str] | None = None,
    past_section_break_positions: Sequence[Sequence[int]] = (),
    eligible_paper_indexes: Sequence[int] | None = None,
) -> ExamDifficultyAdjustmentResult:
    """Apply the complete deterministic batch or leave every input unchanged."""

    # Retained compatibility parameters are deliberately inert: exact content
    # uniqueness has no unmastered exception inside one paper, and adjustment
    # may never synthesize a replacement through AI.
    _ = unmastered_fingerprints, ai_call
    normalized_source = str(source_mode or "").strip()
    if normalized_source not in _SOURCE_MODES:
        raise ExamQualityError(
            "DIFFICULTY_ADJUSTMENT_SOURCE",
            "难度调整来源无效",
        )
    if len(bodies) != len(papers):
        raise ExamQualityError(
            "ITEM_SCHEMA",
            "难度调整试卷数量不一致",
        )
    eligible_offsets = _eligible_offsets(len(papers), eligible_paper_indexes)
    baseline_audit = _adjustment_audit(
        requested=False,
        completed=False,
        source_mode=normalized_source,
    )
    if normalized_source == "past":
        return _result_from_originals(
            bodies=bodies,
            papers=papers,
            adjustment=baseline_audit,
            warned=False,
        )

    # Past exact-slot validation owns its per-item fact contract and this
    # whole-paper ratio adapter must not reinterpret or require those facts.
    # Free/mistake papers, including non-selected compatibility papers, still
    # fail closed on every invalid per-item label before planning.
    labels_by_paper = tuple(
        _validated_paper_labels(paper) for paper in papers
    )
    if not eligible_offsets:
        return _result_from_originals(
            bodies=bodies,
            papers=papers,
            adjustment=baseline_audit,
            warned=False,
        )

    plans: dict[int, EasyExcessAdjustmentPlan] = {}
    try:
        for paper_index in eligible_offsets:
            plans[paper_index] = plan_easy_excess_adjustment(
                labels_by_paper[paper_index],
                overall_difficulty,
            )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise ExamQualityError(
            "DIFFICULTY_LABEL_INVALID",
            "逐题难度或整体难度合同无效",
        ) from exc

    requested_plans = {
        paper_index: plan
        for paper_index, plan in plans.items()
        if plan.requested
    }
    requested_count = sum(
        plan.easy_excess for plan in requested_plans.values()
    )
    advisory_codes = tuple(
        code
        for plan in plans.values()
        for code in plan.advisory_codes
    )
    if requested_count == 0:
        return _result_from_originals(
            bodies=bodies,
            papers=papers,
            adjustment=_adjustment_audit(
                requested=False,
                completed=False,
                source_mode=normalized_source,
                advisory_codes=advisory_codes,
            ),
            warned=False,
        )

    records = _candidate_records(
        candidate_pool,
        used_ids=_used_candidate_ids(papers),
        baseline_fingerprints=_baseline_fingerprints(papers),
    )
    assignments = _match_complete_batch(
        papers=papers,
        plans=requested_plans,
        candidates=records,
        source_mode=normalized_source,
    )
    if assignments is None:
        return _result_from_originals(
            bodies=bodies,
            papers=papers,
            adjustment=_adjustment_audit(
                requested=True,
                completed=False,
                source_mode=normalized_source,
                requested_count=requested_count,
                reason_code="DIFFICULTY_BATCH_MATCHING_INFEASIBLE",
                advisory_codes=advisory_codes,
            ),
            warned=True,
        )

    final_papers = [
        [deepcopy(dict(item)) for item in paper]
        for paper in papers
    ]
    bodies_by_paper: dict[int, dict[int, str]] = {}
    replacement_audits: list[dict[str, Any]] = []
    for paper_index, position, candidate_index in assignments:
        record = records[candidate_index]
        replacement = _replacement_item(
            original=papers[paper_index][position - 1],
            candidate=record["item"],
            position=position,
            source_mode=normalized_source,
        )
        final_papers[paper_index][position - 1] = replacement
        bodies_by_paper.setdefault(paper_index, {})[position] = str(
            record["content"]
        )
        replacement_audits.append({
            "paper_index": paper_index + 1,
            "position": position,
            "from": "简单",
            "to": str(record["difficulty"]),
            "candidate_id": str(record["candidate_id"]),
        })

    final_bodies = [str(value) for value in bodies]
    try:
        for paper_index, replacements in bodies_by_paper.items():
            breaks = (
                tuple(past_section_break_positions[paper_index])
                if paper_index < len(past_section_break_positions)
                else ()
            )
            final_bodies[paper_index] = replace_exam_item_bodies(
                final_bodies[paper_index],
                replacements,
                past_section_break_positions=breaks,
            )
    except ExamQualityError:
        return _result_from_originals(
            bodies=bodies,
            papers=papers,
            adjustment=_adjustment_audit(
                requested=True,
                completed=False,
                source_mode=normalized_source,
                requested_count=requested_count,
                reason_code="DIFFICULTY_BATCH_RENDER_FAILED",
                advisory_codes=advisory_codes,
            ),
            warned=True,
        )

    return ExamDifficultyAdjustmentResult(
        bodies=tuple(final_bodies),
        papers=tuple(tuple(paper) for paper in final_papers),
        adjustment=_adjustment_audit(
            requested=True,
            completed=True,
            source_mode=normalized_source,
            requested_count=requested_count,
            replacements=replacement_audits,
            reason_code="easy_above_preferred_range",
            advisory_codes=advisory_codes,
        ),
        warnings=(),
    )


__all__ = [
    "ExamDifficultyAdjustmentResult",
    "adjust_exam_difficulty_once",
]
