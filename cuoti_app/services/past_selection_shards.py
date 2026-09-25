"""Bounded candidate windows and strict parsing for past-paper selection shards."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .exam_contracts import ExamGenerationConstraintError


MAX_SELECTION_SHARD_POSITIONS = 4
MAX_SELECTION_SHARD_CANDIDATES = 16


def stable_json_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def selection_context_hash(state: Any) -> str:
    return stable_json_hash({
        # v1 embedded the job identity inside a second, selection-context
        # digest.  Keep this calculation available for the explicit v1 -> v2
        # migration boundary, but never use it as the v2 context identity.
        "job_hash": str(
            getattr(state, "legacy_selection_job_hash", "")
            or getattr(state, "job_hash", "")
            or getattr(state, "selection_checkpoint_job_hash", "")
        ),
        "paper_count": int(state.paper_count),
        "question_count": int(state.question_count),
        "candidate_orders": [
            [str(item.get("candidate_id") or "") for item in candidates]
            for candidates in state.paper_candidate_pools
        ],
        "source_quotas": dict(state.source_quotas),
        "chapter_plan": [dict(item) for item in state.chapter_plan],
        "structure_contract": state.structure_contract,
        "section_slot_contract": state.section_slot_contract,
    })


def _quota_labels(
    quotas: Mapping[str, Any], *, count: int, fallback: str
) -> list[str]:
    labels: list[str] = []
    for label, raw_count in quotas.items():
        try:
            amount = max(0, int(raw_count))
        except (TypeError, ValueError):
            amount = 0
        labels.extend([str(label)] * amount)
    if len(labels) < count:
        labels.extend([fallback] * (count - len(labels)))
    return labels[:count]


def selection_position_requirements(state: Any) -> dict[int, dict[str, str]]:
    fallback_source = (
        "ordinary" if state.candidate_source == "ordinary" else "past_exam"
    )
    source_labels = _quota_labels(
        state.source_quotas,
        count=state.question_count,
        fallback=fallback_source,
    )
    chapter_quotas = {
        str(item.get("chapter") or ""): item.get("question_count", 0)
        for item in state.chapter_plan
        if isinstance(item, Mapping) and str(item.get("chapter") or "")
    }
    chapter_labels = _quota_labels(
        chapter_quotas,
        count=sum(label == "ordinary" for label in source_labels),
        fallback="",
    )
    chapter_iter = iter(chapter_labels)
    return {
        position: {
            "source_kind": source_kind,
            "chapter": next(chapter_iter, "") if source_kind == "ordinary" else "",
        }
        for position, source_kind in enumerate(source_labels, start=1)
    }


def candidate_matches_requirement(
    candidate: Mapping[str, Any], requirement: Mapping[str, str]
) -> bool:
    source_kind = str(candidate.get("source_kind") or "").strip()
    if source_kind not in {"ordinary", "past_exam"}:
        return False
    required_source = str(requirement.get("source_kind") or "")
    if required_source and source_kind != required_source:
        return False
    required_chapter = str(requirement.get("chapter") or "")
    return not required_chapter or str(candidate.get("chapter") or "") == required_chapter


def selection_candidate_window(
    state: Any,
    candidates: Sequence[Mapping[str, Any]],
    positions: Sequence[int],
    selected_ids: set[str],
    *,
    rejected_candidate_ids_by_position: Mapping[str | int, Sequence[str]] | None = None,
) -> list[Mapping[str, Any]]:
    if not positions or len(positions) > MAX_SELECTION_SHARD_POSITIONS:
        raise ExamGenerationConstraintError(
            "选题小片题位数量超出限制",
            code="past_selection_shard_limit",
        )
    requirements = selection_position_requirements(state)
    rejected = {
        int(position): {str(candidate_id) for candidate_id in candidate_ids}
        for position, candidate_ids in (
            rejected_candidate_ids_by_position or {}
        ).items()
    }
    available = [
        item
        for item in candidates
        if str(item.get("candidate_id") or "") not in selected_ids
    ]
    chosen: list[Mapping[str, Any]] = []
    chosen_ids: set[str] = set()
    for position in positions:
        eligible = [
            item
            for item in available
            if (
                str(item.get("candidate_id") or "")
                not in rejected.get(int(position), set())
                and candidate_matches_requirement(item, requirements[position])
            )
        ]
        if not eligible:
            requirement = requirements[position]
            raise ExamGenerationConstraintError(
                f"第{position}题位没有满足冻结题源与章节配额的候选题",
                code="past_selection_window_insufficient",
                hint="请调整候选题源，或选择题量更少的结构年份后重试",
                actions=("adjust_candidate_source", "adjust_reference_year"),
                details={
                    "position": int(position),
                    "source_kind": requirement["source_kind"],
                    "chapter": requirement["chapter"],
                },
            )
        for item in eligible[:4]:
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id not in chosen_ids:
                chosen.append(item)
                chosen_ids.add(candidate_id)
    def has_distinct_assignment(offset: int, used: set[str]) -> bool:
        if offset >= len(positions):
            return True
        requirement = requirements[int(positions[offset])]
        for item in chosen:
            candidate_id = str(item.get("candidate_id") or "")
            if (
                candidate_id in used
                or candidate_id in rejected.get(int(positions[offset]), set())
                or not candidate_matches_requirement(item, requirement)
            ):
                continue
            used.add(candidate_id)
            if has_distinct_assignment(offset + 1, used):
                return True
            used.remove(candidate_id)
        return False

    if len(chosen) < len(positions) or not has_distinct_assignment(0, set()):
        raise ExamGenerationConstraintError(
            "当前小片没有足够的互异候选题",
            code="past_selection_window_insufficient",
            hint="请调整候选题源，或选择题量更少的结构年份后重试",
            actions=("adjust_candidate_source", "adjust_reference_year"),
            details={"required": len(positions), "available": len(chosen)},
        )
    for item in available:
        if len(chosen) >= MAX_SELECTION_SHARD_CANDIDATES:
            break
        candidate_id = str(item.get("candidate_id") or "")
        if candidate_id in chosen_ids:
            continue
        if any(
            candidate_id not in rejected.get(int(position), set())
            and candidate_matches_requirement(item, requirements[position])
            for position in positions
        ):
            chosen.append(item)
            chosen_ids.add(candidate_id)
    return chosen[:MAX_SELECTION_SHARD_CANDIDATES]


def parse_selection_shard(
    raw: str,
    candidates: Sequence[Mapping[str, Any]],
    positions: Sequence[int],
    *,
    position_requirements: Mapping[int, Mapping[str, str]] | None = None,
    rejected_candidate_ids_by_position: Mapping[str | int, Sequence[str]] | None = None,
) -> list[dict[str, Any]]:
    if not positions or len(positions) > MAX_SELECTION_SHARD_POSITIONS:
        raise ValueError("选题小片题位数量超出限制")
    if len(candidates) > MAX_SELECTION_SHARD_CANDIDATES:
        raise ValueError("选题小片候选数量超出限制")
    text = str(raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines.pop()
        text = "\n".join(lines).strip()
    try:
        payload = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("选题小片未返回有效 JSON") from exc
    selected = payload.get("selected") if isinstance(payload, Mapping) else None
    if not isinstance(selected, list) or len(selected) != len(positions):
        raise ValueError(f"选题小片必须恰好返回{len(positions)}题")
    allowed_positions = set(int(value) for value in positions)
    candidate_map = {
        str(item.get("candidate_id") or ""): item for item in candidates
    }
    rejected = {
        int(position): {str(candidate_id) for candidate_id in candidate_ids}
        for position, candidate_ids in (
            rejected_candidate_ids_by_position or {}
        ).items()
    }
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_positions: set[int] = set()
    for item in selected:
        if not isinstance(item, Mapping):
            raise ValueError("选题小片包含无效选题项")
        candidate_id = str(item.get("candidate_id") or "").strip()
        try:
            position = int(item.get("position"))
        except (TypeError, ValueError) as exc:
            raise ValueError("选题小片包含无效题位") from exc
        if candidate_id not in candidate_map:
            raise ValueError("选题小片选择了窗口之外的候选题")
        if candidate_id in seen_ids or position in seen_positions:
            raise ValueError("选题小片返回了重复候选或题位")
        if position not in allowed_positions:
            raise ValueError("选题小片返回了窗口之外的题位")
        if candidate_id in rejected.get(position, set()):
            raise ValueError("选题小片复用了该题位已拒绝的候选题")
        if position_requirements is not None:
            requirement = position_requirements.get(position)
            if requirement is None:
                raise ValueError("选题小片缺少冻结题位要求")
            if not candidate_matches_requirement(
                candidate_map[candidate_id], requirement
            ):
                raise ValueError("选题小片候选题不满足冻结题位要求")
        if str(item.get("adapted_content") or "").strip():
            raise ValueError("真题选题阶段不允许返回改编正文")
        selected_candidate = dict(candidate_map[candidate_id])
        selected_candidate["position"] = position
        selected_candidate["selection_reason"] = str(item.get("reason") or "").strip()
        if str(item.get("difficulty") or "").strip():
            selected_candidate["difficulty"] = str(item["difficulty"]).strip()
        result.append(selected_candidate)
        seen_ids.add(candidate_id)
        seen_positions.add(position)
    if seen_positions != allowed_positions:
        raise ValueError("选题小片未覆盖全部冻结题位")
    return sorted(result, key=lambda item: int(item["position"]))


def is_transient_selection_error(exc: BaseException) -> bool:
    return bool(getattr(exc, "retryable", False))


__all__ = [
    "MAX_SELECTION_SHARD_CANDIDATES",
    "MAX_SELECTION_SHARD_POSITIONS",
    "candidate_matches_requirement",
    "is_transient_selection_error",
    "parse_selection_shard",
    "selection_candidate_window",
    "selection_context_hash",
    "selection_position_requirements",
    "stable_json_hash",
]
