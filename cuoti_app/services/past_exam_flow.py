"""Explicit past-exam selection and body assembly flow."""

from __future__ import annotations

import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .exam_contracts import ExamGenerationConstraintError, audit_difficulty_labels
from .exam_prompting import full_candidate_prompt_text
from .exam_repetition_policy import (
    normalize_question_content,
    question_content_fingerprint,
)
from .exam_selection import (
    build_position_only_section_slot_contract,
    build_section_slot_contract,
    validate_section_slot_selection,
)
from .past_selection_checkpoints import (
    SelectionCheckpointConflictError,
    SelectionCheckpointError,
    normalize_selection_checkpoint,
    selection_mapping_hash,
)
from .job_contracts import JobCancelled, JobLeaseLost
from .past_selection_shards import (
    MAX_SELECTION_SHARD_POSITIONS,
    candidate_matches_requirement as _candidate_matches_requirement,
    is_transient_selection_error as _is_transient_selection_error,
    parse_selection_shard as _parse_selection_shard,
    selection_candidate_window as _selection_candidate_window,
    selection_context_hash as _selection_context_hash,
    selection_position_requirements as _selection_position_requirements,
    stable_json_hash as _stable_json_hash,
)


def select_other_past_candidates(
    candidates,
    required_count,
    seed,
    *,
    token_budget=None,
):
    """Select a deterministic, institution-diverse past-exam prompt pool."""

    if required_count <= 0:
        return []
    # ``token_budget`` remains accepted for older internal callers, but the
    # remediation contract no longer treats cumulative estimates as a
    # business-failure boundary.  Single-call safety is enforced at AI I/O.
    usable = [
        dict(item)
        for item in candidates
        if str(item.get("source_kind") or "") == "past_exam"
    ]
    if len(usable) < required_count:
        raise ExamGenerationConstraintError(
            f"其他院校同科真题不足：需要{required_count}题，当前只有{len(usable)}题",
            code="past_candidate_pool_insufficient",
            hint="请改用普通题库、混合题源，或选择题量更少的结构年份",
            actions=("adjust_candidate_source", "adjust_reference_year"),
            details={"required": int(required_count), "available": len(usable)},
        )
    by_institution = {}
    for item in usable:
        by_institution.setdefault(
            str(item.get("source_institution") or ""), []
        ).append(item)
    ordered_groups = []
    for institution in sorted(by_institution):
        group = by_institution[institution]
        random.Random(f"{seed}:{institution}").shuffle(group)
        ordered_groups.append(group)
    diversified = []
    depth = 0
    while True:
        added = False
        for group in ordered_groups:
            if depth < len(group):
                diversified.append(group[depth])
                added = True
        if not added:
            break
        depth += 1
    target = min(len(diversified), max(required_count * 8, required_count * 4))
    selected = []
    for item in diversified:
        selected.append(item)
        if len(selected) >= target:
            break
    return selected


@dataclass(frozen=True)
class PastGenerationInput:
    institution: str
    subject: str
    paper_count: int
    question_count: int
    reference_year: int
    structure_contract: Mapping[str, Any]
    past_analysis: Mapping[str, Any]
    material_understanding: Mapping[str, Any]
    paper_candidate_pools: Sequence[Sequence[Mapping[str, Any]]]
    candidate_source: str
    source_quotas: Mapping[str, int]
    chapter_plan: Sequence[Mapping[str, Any]]
    difficulty_quota: Mapping[str, int]
    allow_adaptation: bool
    note: str
    assembly_source: Mapping[str, Any]
    selection_checkpoint: Mapping[str, Any]
    job_hash: str
    context_hash: str
    testing: bool = False
    recent_question_ids: Sequence[str] = ()
    candidate_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    section_slot_contract: Mapping[str, Any] | None = None
    recent_content_fingerprints: Sequence[str] = ()
    unmastered_content_fingerprints: Sequence[str] = ()
    legacy_selection_job_hash: str = ""
    selection_reasoning_effort: str | None = None


@dataclass(frozen=True)
class PastGenerationResult:
    selection_prompts: Sequence[str]
    selections: Sequence[Sequence[dict[str, Any]]]
    latex_bodies: Sequence[str]
    diversity_audit: Sequence[Mapping[str, Any]] = ()
    selection_hashes: Mapping[int, str] = field(default_factory=dict)
    job_hash: str = ""
    context_hash: str = ""
    legacy_context_hash: str = ""


@dataclass(frozen=True)
class PastSelectionValidationResult:
    selections: Sequence[Sequence[dict[str, Any]]]
    latex_bodies: Sequence[str]
    diversity_audit: Sequence[Mapping[str, Any]]
    selection_hashes: Mapping[int, str]


_DIVERSITY_FIELDS = (
    ("exact_content", "完全相同题目"),
    ("similarity_group", "真题相似簇"),
    ("profile_repetition_group", "真题画像重复组模板"),
    ("profile_avoid_reuse", "真题画像禁止复用模板"),
    ("knowledge_group", "画像知识组"),
    ("knowledge_point", "知识点"),
    ("question_type_knowledge", "题型与知识点组合"),
    ("question_type", "题型"),
)

# Exact content remains a hard constraint within one paper. Past-memory
# similarity fields become hard only at the whole-paper ratio threshold.
_HARD_INTRA_PAPER_FIELDS = (
    "exact_content",
    "similarity_group",
    "profile_repetition_group",
    "profile_avoid_reuse",
)

_ADVISORY_INTRA_PAPER_FIELDS = (
    "knowledge_group",
    "knowledge_point",
    "question_type_knowledge",
    "question_type",
)

_PAST_MEMORY_SIMILARITY_FIELDS = frozenset({
    "similarity_group",
    "profile_repetition_group",
    "profile_avoid_reuse",
})
# Existing legacy current-job threshold, retained unchanged by this repair.
# It controls when same-job similarity/profile repetition becomes a hard
# violation; cross-job history never enters this calculation.  The value is
# not presented as newly calibrated or reused as a semantic ranking weight.
PAST_MEMORY_REJECTION_THRESHOLD = 0.5
_CROSS_PAPER_HARD_FIELDS = (
    "exact_content",
    "similarity_group",
    "profile_repetition_group",
    "profile_avoid_reuse",
)


class DiversityConstraintViolation(ValueError):
    """One or more machine-readable hard diversity violations."""

    def __init__(self, violations: Sequence[Mapping[str, Any]]):
        self.violations = tuple(dict(item) for item in violations)
        self.failed_positions = tuple(sorted({
            int(position)
            for item in self.violations
            for position in (item.get("failed_positions") or ())
        }))
        messages = [str(item.get("message") or "").strip() for item in self.violations]
        super().__init__("；".join(message for message in messages if message))


def _trait_key(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", str(value or "").lower())


def _profile_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item) for item in value]
    return [str(value)] if value not in (None, "") else []


def _is_unmastered_candidate(
    state: PastGenerationInput,
    candidate: Mapping[str, Any],
) -> bool:
    if not normalize_question_content(candidate.get("content")):
        return False
    return (
        question_content_fingerprint(candidate.get("content"))
        in state.unmastered_content_fingerprints
    )


def _trait_intersects(values: Sequence[str] | frozenset[str], blocked: set[str]) -> bool:
    """Treat obvious normalized aliases (for example, suffix variants) as repeats."""

    for value in values:
        for prior in blocked:
            if value == prior:
                return True
            if len(value) >= 3 and len(prior) >= 3 and (
                value in prior or prior in value
            ):
                return True
    return False


def _profile_repetition_group(
    profile: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> str:
    allowed = {
        str(value).strip()
        for value in (analysis.get("avoid_repetition_groups") or [])
        if str(value).strip()
    }
    patterns = analysis.get("question_type_patterns") or []
    question_type = _trait_key(profile.get("question_type")).removesuffix("题")
    haystack = _trait_key(" ".join(
        [
            str(profile.get("chapter") or ""),
            *_profile_values(profile.get("knowledge_points")),
            *_profile_values(profile.get("methods")),
            *_profile_values(profile.get("semantic_keywords")),
        ]
    ))
    matches = []
    for index, pattern in enumerate(patterns):
        if not isinstance(pattern, Mapping):
            continue
        group = str(pattern.get("avoid_group") or "").strip()
        if not group or (allowed and group not in allowed):
            continue
        label = str(pattern.get("type") or "").strip()
        family, _, topic_text = label.partition("-")
        family_key = _trait_key(family).removesuffix("题")
        if family_key and question_type and not (
            family_key in question_type or question_type in family_key
        ):
            continue
        topics = [
            _trait_key(value)
            for value in re.split(r"[/、|]", topic_text)
            if _trait_key(value)
        ]
        score = sum(topic in haystack for topic in topics)
        matches.append((score, -index, group))
    if not matches:
        return ""
    matches.sort(reverse=True)
    if matches[0][0] or len(matches) == 1:
        return matches[0][2]
    return ""


def _knowledge_group_traits(
    profile: Mapping[str, Any],
    analysis: Mapping[str, Any],
) -> tuple[frozenset[str], frozenset[str]]:
    haystack = _trait_key(" ".join(
        [
            str(profile.get("chapter") or ""),
            *_profile_values(profile.get("knowledge_points")),
            *_profile_values(profile.get("methods")),
            *_profile_values(profile.get("semantic_keywords")),
        ]
    ))
    groups: set[str] = set()
    avoid_reuse: set[str] = set()
    for item in analysis.get("knowledge_groups") or []:
        if not isinstance(item, Mapping):
            continue
        group = _trait_key(item.get("group"))
        chapters = [_trait_key(value) for value in _profile_values(item.get("chapters"))]
        if group and any(chapter and chapter in haystack for chapter in chapters):
            groups.add(group)
        for template in _profile_values(item.get("avoid_reuse")):
            template_key = _trait_key(template)
            comparable = template_key.removeprefix("同一")
            if group and comparable and comparable in haystack:
                avoid_reuse.add(f"{group}:{comparable}")
    return frozenset(groups), frozenset(avoid_reuse)


def _candidate_traits(
    candidate: Mapping[str, Any] | str,
    profiles: Mapping[str, Mapping[str, Any]],
    analysis: Mapping[str, Any],
    unmastered_fingerprints: Sequence[str] = (),
) -> dict[str, frozenset[str]]:
    # Retained in the private call contract for checkpoint-compatible callers.
    # Per the 2026-08-29 decision, mastery history cannot erase any trait used
    # by current-job hard rules, so it intentionally does not shape this map.
    del unmastered_fingerprints
    candidate_data = candidate if isinstance(candidate, Mapping) else {
        "candidate_id": str(candidate)
    }
    candidate_id = str(candidate_data.get("candidate_id") or "")
    raw = profiles.get(candidate_id)
    has_profile = isinstance(raw, Mapping)
    profile = dict(candidate_data)
    if has_profile:
        profile.update(raw)
    # Similarity and learned-profile reuse rules describe past-exam memory.
    # Source provenance comes from the frozen candidate, never the AI card.
    is_past_exam = (
        str(candidate_data.get("source_kind") or "ordinary") == "past_exam"
    )
    knowledge_values = _profile_values(profile.get("knowledge_points"))
    if not knowledge_values:
        knowledge_values = _profile_values(profile.get("chapter"))
    if not knowledge_values:
        knowledge_values = ["未知知识点"]
    knowledge = tuple(dict.fromkeys(
        key
        for key in (
            _trait_key(value) for value in knowledge_values
        )
        if key
    ))
    question_type = _trait_key(profile.get("question_type")) or "未知题型"
    exact_content = normalize_question_content(candidate_data.get("content"))
    exact_content_hash = (
        question_content_fingerprint(candidate_data.get("content"))
        if exact_content
        else ""
    )
    similarity = _trait_key(profile.get("similarity_group"))
    if is_past_exam and not similarity:
        similarity = f"fallback:{question_type}:{knowledge[0]}"
    repetition_group = _profile_repetition_group(profile, analysis)
    knowledge_groups, avoid_reuse = _knowledge_group_traits(profile, analysis)
    return {
        "exact_content": (
            frozenset((exact_content_hash,))
            if exact_content_hash
            else frozenset()
        ),
        "similarity_group": (
            frozenset((similarity,))
            if is_past_exam and similarity
            else frozenset()
        ),
        "profile_repetition_group": frozenset(
            f"{repetition_group}:{point}" for point in knowledge
        ) if (
            is_past_exam
            and repetition_group
            and knowledge
        ) else frozenset(),
        "profile_avoid_reuse": (
            avoid_reuse if is_past_exam else frozenset()
        ),
        "knowledge_group": knowledge_groups,
        "knowledge_point": frozenset(knowledge),
        "question_type_knowledge": frozenset(
            f"{question_type}:{point}" for point in knowledge
        ) if question_type and knowledge else frozenset(),
        "question_type": frozenset((question_type,)),
    }


def _diversity_buckets(
    state: PastGenerationInput,
) -> tuple[str | None, tuple[tuple[str, int], ...]]:
    field_name = "chapter" if state.candidate_source == "ordinary" else "source_kind"
    source = (
        (
            (str(item.get("chapter") or ""), int(item.get("question_count") or 0))
            for item in state.chapter_plan
        )
        if field_name == "chapter"
        else (
            (str(source_kind), int(required))
            for source_kind, required in state.source_quotas.items()
        )
    )
    combined: dict[str, int] = {}
    for value, required in source:
        if value and required > 0:
            combined[value] = combined.get(value, 0) + required
    if not combined:
        return None, ()
    return field_name, tuple(combined.items())


def _lexicographic_novel_counts(
    state: PastGenerationInput,
    candidates: Sequence[Mapping[str, Any]],
    checks: Sequence[tuple[str, set[str]]],
) -> dict[str, int]:
    """Maximize all active diversity dimensions in their declared priority order."""

    if not checks:
        return {}
    base = state.question_count + 1
    weights = {
        field_name: base ** (len(checks) - index - 1)
        for index, (field_name, _blocked) in enumerate(checks)
    }
    ranked = []
    for index, item in enumerate(candidates):
        traits = _candidate_traits(
            item,
            state.candidate_profiles,
            state.past_analysis,
            state.unmastered_content_fingerprints,
        )
        vector: dict[str, int] = {}
        for field_name, blocked in checks:
            if field_name == "candidate_id":
                is_novel = str(item.get("candidate_id") or "") not in blocked
            else:
                is_novel = not _trait_intersects(traits[field_name], blocked)
            vector[field_name] = int(is_novel)
        score = sum(weights[field_name] * value for field_name, value in vector.items())
        ranked.append((item, vector, score, index))

    def stable_rank(record):
        item, _vector, score, index = record
        return (
            -score,
            str(item.get("candidate_id") or ""),
            str(item.get("source_file") or ""),
            str(item.get("source_number") or ""),
            index,
        )

    bucket_field, requirements = _diversity_buckets(state)
    witness = []
    if bucket_field is None:
        witness = sorted(ranked, key=stable_rank)[:state.question_count]
    else:
        for bucket_value, required in requirements:
            bucket = [
                record
                for record in ranked
                if str(
                    record[0].get(bucket_field)
                    or ("ordinary" if bucket_field == "source_kind" else "")
                ) == bucket_value
            ]
            witness.extend(sorted(bucket, key=stable_rank)[:required])

    counts = {field_name: 0 for field_name, _blocked in checks}
    for _item, vector, _score, _index in witness:
        for field_name, value in vector.items():
            counts[field_name] += value
    return counts


def _intra_repeat_counts(
    state: PastGenerationInput,
    selected: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for field_name in fields:
        seen: set[str] = set()
        repeated = 0
        for item in selected:
            values = _candidate_traits(
                item,
                state.candidate_profiles,
                state.past_analysis,
                state.unmastered_content_fingerprints,
            ).get(field_name, frozenset())
            if _trait_intersects(values, seen):
                repeated += 1
            seen.update(values)
        counts[field_name] = repeated
    return counts


def _intra_paper_witness(
    state: PastGenerationInput,
    candidates: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Build one deterministic, quota-aware selection for joint hard limits."""

    records = []
    for index, item in enumerate(candidates):
        records.append(
            (
                item,
                _candidate_traits(
                    item,
                    state.candidate_profiles,
                    state.past_analysis,
                    state.unmastered_content_fingerprints,
                ),
                index,
            )
        )
    selected: list[Mapping[str, Any]] = []
    selected_ids: set[str] = set()
    seen = {field_name: set() for field_name in _HARD_INTRA_PAPER_FIELDS}

    def stable_key(record):
        item, traits, index = record
        repeat_vector = tuple(
            int(_trait_intersects(traits[field_name], seen[field_name]))
            for field_name in _HARD_INTRA_PAPER_FIELDS
        )
        return (
            repeat_vector,
            str(item.get("candidate_id") or ""),
            str(item.get("source_file") or ""),
            str(item.get("source_number") or ""),
            index,
        )

    def take(pool, required):
        available = list(pool)
        for _ in range(max(0, int(required))):
            available = [
                record
                for record in available
                if str(record[0].get("candidate_id") or "") not in selected_ids
            ]
            if not available:
                return
            item, traits, _index = min(available, key=stable_key)
            candidate_id = str(item.get("candidate_id") or "")
            selected.append(item)
            selected_ids.add(candidate_id)
            for field_name in _HARD_INTRA_PAPER_FIELDS:
                seen[field_name].update(traits[field_name])

    bucket_field, requirements = _diversity_buckets(state)
    if bucket_field is not None:
        for bucket_value, required in requirements:
            take(
                [
                    record
                    for record in records
                    if str(
                        record[0].get(bucket_field)
                        or ("ordinary" if bucket_field == "source_kind" else "")
                    )
                    == bucket_value
                ],
                required,
            )
    take(records, state.question_count - len(selected))
    return selected[: state.question_count]


def _intra_paper_repeat_limits(
    state: PastGenerationInput,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, int]:
    """Return jointly achievable hard limits from one real candidate witness."""

    active_fields = tuple(
        field_name
        for field_name in _HARD_INTRA_PAPER_FIELDS
        if any(
            _candidate_traits(
                item,
                state.candidate_profiles,
                state.past_analysis,
                state.unmastered_content_fingerprints,
            )[field_name]
            for item in candidates
        )
    )
    if not active_fields:
        return {}
    witness = _intra_paper_witness(state, candidates)
    if len(witness) < state.question_count:
        return {
            field_name: state.question_count
            for field_name in active_fields
        }
    limits = _intra_repeat_counts(state, witness, active_fields)
    if "exact_content" in limits:
        limits["exact_content"] = 0
    return limits


def _diversity_policy(
    state: PastGenerationInput,
    candidates: Sequence[Mapping[str, Any]],
    used_ids: set[str],
    used_traits: Mapping[str, set[str]],
) -> dict[str, Any]:
    enforced: list[str] = []
    relaxed: list[str] = []
    partial: list[str] = []
    forbidden: dict[str, list[str]] = {}
    repeat_limits: dict[str, int] = {}
    checks = [
        (
            "exact_content",
            set(used_traits.get("exact_content") or ()),
        ),
        ("candidate_id", set(used_ids)),
    ]
    checks.extend(
        (field_name, set(used_traits.get(field_name) or ()))
        for field_name in _CROSS_PAPER_HARD_FIELDS
        if field_name != "exact_content"
    )
    active_checks = [
        (field_name, prior) for field_name, prior in checks if prior
    ]
    novel_counts = _lexicographic_novel_counts(
        state, candidates, active_checks
    )
    for field_name, prior in active_checks:
        max_novel = min(
            state.question_count, int(novel_counts.get(field_name, 0))
        )
        max_repeated = max(0, state.question_count - max_novel)
        forbidden[field_name] = sorted(prior)
        repeat_limits[field_name] = max_repeated
        if max_repeated == 0:
            enforced.append(field_name)
        else:
            relaxed.append(field_name)
            if max_novel:
                partial.append(field_name)
    intra_repeat_limits = _intra_paper_repeat_limits(state, candidates)
    intra_enforced = [
        field_name
        for field_name, limit in intra_repeat_limits.items()
        if limit == 0
    ]
    intra_relaxed = [
        field_name
        for field_name, limit in intra_repeat_limits.items()
        if limit > 0
    ]
    return {
        "enforced": enforced,
        "relaxed": relaxed,
        "partial": partial,
        "forbidden": forbidden,
        "repeat_limits": repeat_limits,
        "intra_repeat_limits": intra_repeat_limits,
        "intra_enforced": intra_enforced,
        "intra_relaxed": intra_relaxed,
        "intra_advisory": list(_ADVISORY_INTRA_PAPER_FIELDS),
    }


def _validate_diversity(
    state: PastGenerationInput,
    selected: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
) -> None:
    violations: list[dict[str, Any]] = []
    forbidden = policy.get("forbidden") or {}
    repeat_limits = policy.get("repeat_limits") or {}
    blocked_ids = set(forbidden.get("candidate_id") or ())
    repeated_id_items = [
        (int(item.get("position") or offset), str(item.get("candidate_id") or ""))
        for offset, item in enumerate(selected, start=1)
        if (
            str(item.get("candidate_id") or "") in blocked_ids
        )
    ]
    allowed_ids = int(repeat_limits.get("candidate_id", len(selected)))
    if len(repeated_id_items) > allowed_ids:
        violations.append(
            {
                "scope": "current_generation_job",
                "field": "candidate_id",
                "actual": len(repeated_id_items),
                "allowed": allowed_ids,
                "failed_positions": [
                    position
                    for position, _candidate_id in repeated_id_items[allowed_ids:]
                ],
                "message": (
                    f"本批其他试卷候选题ID重复{len(repeated_id_items)}题"
                    f"（最多允许{allowed_ids}题）"
                ),
            }
        )
    labels = dict(_DIVERSITY_FIELDS)
    for field_name, blocked_values in forbidden.items():
        if field_name == "candidate_id":
            continue
        blocked = set(blocked_values)
        repeated_positions = [
            int(item.get("position") or offset)
            for offset, item in enumerate(selected, start=1)
            if _trait_intersects(
                _candidate_traits(
                    item,
                    state.candidate_profiles,
                    state.past_analysis,
                    state.unmastered_content_fingerprints,
                )[field_name],
                blocked,
            )
        ]
        repeated_count = len(repeated_positions)
        allowed = int(repeat_limits.get(field_name, len(selected)))
        repeated_ratio = repeated_count / len(selected) if selected else 0.0
        below_past_memory_threshold = (
            field_name in _PAST_MEMORY_SIMILARITY_FIELDS
            and repeated_ratio < PAST_MEMORY_REJECTION_THRESHOLD
        )
        if repeated_count > allowed and not below_past_memory_threshold:
            violations.append(
                {
                    "scope": "cross_paper",
                    "field": field_name,
                    "actual": repeated_count,
                    "allowed": allowed,
                    "failed_positions": repeated_positions[allowed:],
                    "message": (
                        f"与本批其他试卷{labels[field_name]}重复{repeated_count}题"
                        f"（最多允许{allowed}题，占整卷{repeated_ratio:.1%}）"
                    ),
                }
            )
    intra_labels = dict(_DIVERSITY_FIELDS)
    intra_counts = _intra_repeat_counts(
        state,
        selected,
        tuple((policy.get("intra_repeat_limits") or {}).keys()),
    )
    for field_name, allowed_raw in (policy.get("intra_repeat_limits") or {}).items():
        allowed = int(allowed_raw)
        repeated_count = intra_counts.get(field_name, 0)
        repeated_ratio = repeated_count / len(selected) if selected else 0.0
        below_past_memory_threshold = (
            field_name in _PAST_MEMORY_SIMILARITY_FIELDS
            and repeated_ratio < PAST_MEMORY_REJECTION_THRESHOLD
        )
        if repeated_count > allowed and not below_past_memory_threshold:
            seen: set[str] = set()
            repeated_positions: list[int] = []
            for offset, item in enumerate(selected, start=1):
                values = _candidate_traits(
                    item,
                    state.candidate_profiles,
                    state.past_analysis,
                    state.unmastered_content_fingerprints,
                ).get(field_name, frozenset())
                if _trait_intersects(values, seen):
                    repeated_positions.append(
                        int(item.get("position") or offset)
                    )
                seen.update(values)
            violations.append(
                {
                    "scope": "intra_paper",
                    "field": field_name,
                    "actual": repeated_count,
                    "allowed": allowed,
                    "failed_positions": repeated_positions[allowed:],
                    "message": (
                        f"本卷{intra_labels.get(field_name, field_name)}重复"
                        f"{repeated_count}题（最多允许{allowed}题，"
                        f"占整卷{repeated_ratio:.1%}）"
                    ),
                }
            )
    if violations:
        raise DiversityConstraintViolation(violations)


def _record_selected_traits(
    state: PastGenerationInput,
    selected: Sequence[Mapping[str, Any]],
    used_traits: dict[str, set[str]],
) -> dict[str, list[str]]:
    selected_traits = {field: set() for field, _label in _DIVERSITY_FIELDS}
    for item in selected:
        traits = _candidate_traits(
            item,
            state.candidate_profiles,
            state.past_analysis,
            state.unmastered_content_fingerprints,
        )
        for field_name, values in traits.items():
            selected_traits[field_name].update(values)
            used_traits[field_name].update(values)
    return {key: sorted(values) for key, values in selected_traits.items()}


def _candidate_matches_frozen_position(
    candidate: Mapping[str, Any],
    requirement: Mapping[str, str],
    rejected_candidate_ids_by_position: Mapping[str, Sequence[str]],
    position: int,
) -> bool:
    candidate_id = str(candidate.get("candidate_id") or "")
    return (
        bool(candidate_id)
        and candidate_id
        not in rejected_candidate_ids_by_position.get(str(position), ())
        and _candidate_matches_requirement(candidate, requirement)
    )


def _resolve_section_slot_contract(
    state: PastGenerationInput,
) -> Mapping[str, Any]:
    stored_present = (
        isinstance(state.past_analysis, Mapping)
        and "section_slot_contract" in state.past_analysis
    )
    stored = (
        state.past_analysis.get("section_slot_contract")
        if stored_present
        else None
    )
    formal_present = state.section_slot_contract is not None or stored_present
    formal = state.section_slot_contract
    if formal is None and stored_present:
        formal = stored
    if formal_present:
        if not isinstance(formal, Mapping):
            raise ValueError("板块题位合同格式无效")
        declared_count = formal.get("question_count")
        if (
            isinstance(declared_count, bool)
            or not isinstance(declared_count, int)
            or declared_count != state.question_count
        ):
            raise ValueError("板块题位合同题量与冻结题量不一致")
        return formal
    slot_contract = build_section_slot_contract(state.structure_contract)
    if slot_contract.get("question_count") != state.question_count:
        slot_contract = build_position_only_section_slot_contract(
            state.question_count
        )
    return slot_contract


def _normalize_flow_checkpoint(
    state: PastGenerationInput,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    if len(state.paper_candidate_pools) != state.paper_count:
        raise SelectionCheckpointConflictError(
            "frozen candidate pools do not match the paper count"
        )
    normalized = normalize_selection_checkpoint(payload)
    job_hash = str(state.job_hash or "").strip().casefold()
    context_hash = str(state.context_hash or "").strip().casefold()
    if (
        normalized["job_hash"] != job_hash
        or normalized["context_hash"] != context_hash
    ):
        raise SelectionCheckpointConflictError(
            "selection checkpoint identity does not match the frozen flow"
        )
    papers = {
        int(item["paper_index"]): item for item in normalized["papers"]
    }
    if set(papers) != set(range(1, state.paper_count + 1)):
        raise SelectionCheckpointConflictError(
            "selection checkpoint papers do not match the frozen flow"
        )
    expected_positions = list(range(1, state.question_count + 1))
    if any(
        paper["expected_positions"] != expected_positions
        for paper in papers.values()
    ):
        raise SelectionCheckpointConflictError(
            "selection checkpoint positions do not match the frozen structure"
        )
    return normalized


def _checkpoint_paper(
    checkpoint: Mapping[str, Any], paper_index: int
) -> Mapping[str, Any]:
    for paper in checkpoint["papers"]:
        if int(paper["paper_index"]) == paper_index:
            return paper
    raise SelectionCheckpointConflictError(
        "selection checkpoint paper does not exist"
    )


def _canonical_frozen_selection(
    state: PastGenerationInput,
    selected: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    position_requirements: Mapping[int, Mapping[str, str]],
    rejected_candidate_ids_by_position: Mapping[str, Sequence[str]],
) -> list[dict[str, Any]]:
    if len(selected) != state.question_count:
        raise ValueError("整卷校验未返回冻结题量")
    candidate_map = {
        str(item.get("candidate_id") or ""): item for item in candidates
    }
    ordered: dict[int, dict[str, Any]] = {}
    seen_ids: set[str] = set()
    for item in selected:
        try:
            position = int(item.get("position"))
        except (TypeError, ValueError) as exc:
            raise ValueError("整卷校验返回了无效题位") from exc
        candidate_id = str(item.get("candidate_id") or "")
        frozen_candidate = candidate_map.get(candidate_id)
        if (
            frozen_candidate is None
            or position not in position_requirements
            or position in ordered
            or candidate_id in seen_ids
            or not _candidate_matches_frozen_position(
                frozen_candidate,
                position_requirements[position],
                rejected_candidate_ids_by_position,
                position,
            )
        ):
            raise ValueError("整卷校验返回了不合法的冻结候选题位映射")
        canonical = dict(frozen_candidate)
        canonical["position"] = position
        selection_reason = str(
            item.get("selection_reason") or item.get("reason") or ""
        ).strip()
        if selection_reason:
            canonical["selection_reason"] = selection_reason
        ordered[position] = canonical
        seen_ids.add(candidate_id)
    expected = set(range(1, state.question_count + 1))
    if set(ordered) != expected:
        raise ValueError("整卷校验未完整覆盖冻结题位")
    return [ordered[position] for position in sorted(ordered)]


def _selection_validation_payload(
    selected: Sequence[Mapping[str, Any]],
) -> str:
    return json.dumps({
        "selected": [
            {
                "candidate_id": str(item.get("candidate_id") or ""),
                "position": int(item["position"]),
                "difficulty": str(item.get("difficulty") or ""),
                "reason": str(item.get("selection_reason") or ""),
                "adapted_content": "",
            }
            for item in selected
        ]
    }, ensure_ascii=False)


def _validate_past_paper_selection(
    state: PastGenerationInput,
    selected: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
    *,
    position_requirements: Mapping[int, Mapping[str, str]],
    rejected_candidate_ids_by_position: Mapping[str, Sequence[str]],
    slot_contract: Mapping[str, Any],
    policy: Mapping[str, Any],
    validate_selection: Callable[
        [str, Sequence[Mapping[str, Any]]], Sequence[Mapping[str, Any]]
    ] | None,
    build_exam_body: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], str
    ],
    generated_question_count: Callable[[str], int],
) -> tuple[list[dict[str, Any]], Sequence[Mapping[str, Any]], str]:
    canonical = _canonical_frozen_selection(
        state,
        selected,
        candidates,
        position_requirements,
        rejected_candidate_ids_by_position,
    )
    if validate_selection is not None:
        expected_assignment = {
            int(item["position"]): str(item.get("candidate_id") or "")
            for item in canonical
        }
        validated = list(
            validate_selection(_selection_validation_payload(canonical), candidates)
        )
        canonical = _canonical_frozen_selection(
            state,
            validated,
            candidates,
            position_requirements,
            rejected_candidate_ids_by_position,
        )
        if {
            int(item["position"]): str(item.get("candidate_id") or "")
            for item in canonical
        } != expected_assignment:
            raise ValueError("整卷校验不得改变冻结候选题位映射")
    slot_decisions = validate_section_slot_selection(
        canonical, slot_contract, state.candidate_profiles
    )
    _validate_diversity(state, canonical, policy)
    body = build_exam_body(state.assembly_source, canonical)
    actual_count = generated_question_count(body)
    if actual_count != state.question_count:
        raise ValueError(
            f"生成题数与{state.reference_year}年结构不一致："
            f"应为{state.question_count}题，实际{actual_count}题，未保存"
        )
    return canonical, slot_decisions, body


def _record_paper_validation_audit(
    state: PastGenerationInput,
    *,
    paper_index: int,
    selected: Sequence[Mapping[str, Any]],
    policy: Mapping[str, Any],
    slot_contract: Mapping[str, Any],
    slot_decisions: Sequence[Mapping[str, Any]],
    used_ids: set[str],
    used_traits: dict[str, set[str]],
) -> dict[str, Any]:
    prior_advisory_traits = {
        field_name: set(used_traits.get(field_name) or ())
        for field_name in _ADVISORY_INTRA_PAPER_FIELDS
    }
    used_ids.update(str(item.get("candidate_id")) for item in selected)
    selected_traits = _record_selected_traits(state, selected, used_traits)
    advisory_counts = _intra_repeat_counts(
        state, selected, _ADVISORY_INTRA_PAPER_FIELDS
    )
    past_memory_counts = _intra_repeat_counts(
        state, selected, tuple(_PAST_MEMORY_SIMILARITY_FIELDS)
    )
    cross_advisory_counts = {
        field_name: sum(
            _trait_intersects(
                _candidate_traits(
                    item,
                    state.candidate_profiles,
                    state.past_analysis,
                    state.unmastered_content_fingerprints,
                )[field_name],
                prior,
            )
            for item in selected
        )
        for field_name, prior in prior_advisory_traits.items()
    }
    selected_unmastered_ids = sorted(
        str(item.get("candidate_id") or "")
        for item in selected
        if _is_unmastered_candidate(state, item)
    )
    # User decision (2026-08-29): these overlaps are audit evidence only.
    # They are deliberately calculated after validation and never feed the
    # forbidden sets, repeat limits, checkpoint rejection, or reselection.
    recent_ids = {
        str(value) for value in state.recent_question_ids if str(value)
    }
    history_soft_exemption_ids = sorted(
        set(selected_unmastered_ids) & recent_ids
    )
    recent_fingerprints = {
        str(value)
        for value in state.recent_content_fingerprints
        if str(value)
    }
    history_id_overlaps = sorted({
        str(item.get("candidate_id") or "")
        for item in selected
        if str(item.get("candidate_id") or "") in recent_ids
    })
    history_content_overlaps = sorted({
        fingerprint
        for item in selected
        for fingerprint in (
            question_content_fingerprint(item.get("content")),
        )
        if normalize_question_content(item.get("content"))
        and fingerprint in recent_fingerprints
    })
    return {
        "paper_index": paper_index,
        "enforced": list(policy["enforced"]),
        "relaxed": list(policy["relaxed"]),
        "partial": list(policy["partial"]),
        "repeat_limits": dict(policy["repeat_limits"]),
        "intra_repeat_limits": dict(policy.get("intra_repeat_limits") or {}),
        "intra_enforced": list(policy.get("intra_enforced") or []),
        "intra_relaxed": list(policy.get("intra_relaxed") or []),
        "intra_advisory": list(policy.get("intra_advisory") or []),
        "intra_advisory_counts": advisory_counts,
        "cross_advisory_counts": cross_advisory_counts,
        "past_memory_rejection_threshold": PAST_MEMORY_REJECTION_THRESHOLD,
        "intra_past_memory_counts": past_memory_counts,
        "intra_past_memory_ratios": {
            field_name: count / len(selected) if selected else 0.0
            for field_name, count in past_memory_counts.items()
        },
        "recent_question_ids": sorted(
            str(value) for value in state.recent_question_ids if str(value)
        ),
        "history_overlap_counts": {
            "candidate_id": len(history_id_overlaps),
            "exact_content": len(history_content_overlaps),
        },
        "history_overlap_candidate_ids": history_id_overlaps,
        "history_overlap_exact_content_fingerprints": history_content_overlaps,
        "selected_candidate_ids": [
            str(item.get("candidate_id") or "") for item in selected
        ],
        # Compatibility key retained for readers of the old audit shape.  It
        # is now always empty because unmastered status grants no current-job
        # hard exemption.
        "unmastered_exempt_candidate_ids": [],
        "selected_unmastered_candidate_ids": selected_unmastered_ids,
        "unmastered_history_soft_exemption_candidate_ids": (
            history_soft_exemption_ids
        ),
        "unmastered_exemption_scope": "cross_job_history_soft_signal_only",
        "selected_traits": selected_traits,
        "section_slot_contract_version": slot_contract["schema_version"],
        "section_slot_compatibility_mode": slot_contract.get(
            "compatibility_mode"
        ),
        "section_slot_decisions": slot_decisions,
    }


def validate_past_exam_selections(
    state: PastGenerationInput,
    selections: Sequence[Sequence[Mapping[str, Any]]],
    *,
    validate_selection: Callable[
        [str, Sequence[Mapping[str, Any]]], Sequence[Mapping[str, Any]]
    ] | None,
    build_exam_body: Callable[
        [Mapping[str, Any], Sequence[Mapping[str, Any]]], str
    ],
    generated_question_count: Callable[[str], int],
) -> PastSelectionValidationResult:
    """Pure deterministic validation for complete frozen past-paper selections."""

    if len(selections) != state.paper_count:
        raise ValueError("完整选题卷数与冻结卷数不一致")
    checkpoint = _normalize_flow_checkpoint(
        state, state.selection_checkpoint
    )
    slot_contract = _resolve_section_slot_contract(state)
    position_requirements = _selection_position_requirements(state)
    used_ids: set[str] = set()
    used_traits = {field: set() for field, _label in _DIVERSITY_FIELDS}
    validated_selections: list[Sequence[dict[str, Any]]] = []
    bodies: list[str] = []
    audits: list[Mapping[str, Any]] = []
    selection_hashes: dict[int, str] = {}
    for paper_index, (selected, candidates) in enumerate(
        zip(selections, state.paper_candidate_pools), start=1
    ):
        policy = _diversity_policy(state, candidates, used_ids, used_traits)
        paper = _checkpoint_paper(checkpoint, paper_index)
        canonical, slot_decisions, body = _validate_past_paper_selection(
            state,
            selected,
            candidates,
            position_requirements=position_requirements,
            rejected_candidate_ids_by_position=paper[
                "rejected_candidate_ids_by_position"
            ],
            slot_contract=slot_contract,
            policy=policy,
            validate_selection=validate_selection,
            build_exam_body=build_exam_body,
            generated_question_count=generated_question_count,
        )
        validated_selections.append(canonical)
        bodies.append(body)
        selection_hashes[paper_index] = selection_mapping_hash({
            int(item["position"]): str(item.get("candidate_id") or "")
            for item in canonical
        })
        audits.append(_record_paper_validation_audit(
            state,
            paper_index=paper_index,
            selected=canonical,
            policy=policy,
            slot_contract=slot_contract,
            slot_decisions=slot_decisions,
            used_ids=used_ids,
            used_traits=used_traits,
        ))
    return PastSelectionValidationResult(
        selections=validated_selections,
        latex_bodies=bodies,
        diversity_audit=audits,
        selection_hashes=selection_hashes,
    )


def generate_past_exam(
    state: PastGenerationInput,
    *,
    request_selection: Callable[[str], str],
    validate_selection: Callable[[str, Sequence[Mapping[str, Any]]], Sequence[dict[str, Any]]],
    validate_selection_without_audit: Callable[[str, Sequence[Mapping[str, Any]]], Sequence[dict[str, Any]]] | None = None,
    audit_difficulties: Callable[[Sequence[str]], Sequence[str]] | None = None,
    build_exam_body: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], str],
    generated_question_count: Callable[[str], int],
    upsert_selection_shard: Callable[..., Mapping[str, Any]],
    mark_selection_validated: Callable[..., Mapping[str, Any]],
    reject_selection_candidates: Callable[..., Mapping[str, Any]],
    clear_selection_paper: Callable[..., Mapping[str, Any]],
) -> PastGenerationResult:
    """Select and assemble each paper; publication remains outside this flow."""

    # Kept in the signature only for legacy callers/checkpoint replays.  A
    # whole-paper difficulty audit is not an authority for past exact slots and
    # must never overwrite their frozen candidate facts.
    audit_difficulties = None

    slot_contract = _resolve_section_slot_contract(state)

    source_rule = (
        "必须严格满足候选来源数量："
        + json.dumps(state.source_quotas, ensure_ascii=False)
        + "。ordinary 表示普通题库，past_exam 表示其他院校同科真题；"
        + f"past_exam 不得来自目标院校 {state.institution}。"
    )
    chapter_rule = (
        "必须严格遵守章节题量计划："
        + json.dumps(state.chapter_plan, ensure_ascii=False)
        if state.candidate_source == "ordinary"
        else (
            "章节题量计划用于普通题库候选范围和整体知识分布指导；"
            "他校真题保留原卷板块，不得伪造普通题库章节。"
        )
    )
    prompts: list[str] = []
    intra_limits_by_paper = [
        _intra_paper_repeat_limits(state, candidates)
        for candidates in state.paper_candidate_pools
    ]
    unmastered_candidate_ids = {
        str(item.get("candidate_id") or "")
        for candidates in state.paper_candidate_pools
        for item in candidates
        if _is_unmastered_candidate(state, item)
    }
    recent_ids = {
        str(value) for value in state.recent_question_ids if str(value)
    }
    profile_lines: list[dict[str, Any]] = []
    for candidate_id, profile in state.candidate_profiles.items():
        if not isinstance(profile, Mapping):
            continue
        topics = [str(item) for item in (profile.get("knowledge_points") or [])[:4]]
        question_type = str(profile.get("question_type") or "")
        similarity_group = str(profile.get("similarity_group") or "")
        if topics or question_type or similarity_group:
            profile_lines.append({
                "candidate_id": str(candidate_id),
                "question_type": question_type,
                "knowledge_points": topics,
                "similarity_group": similarity_group,
            })
    profiles_by_id = {
        str(item["candidate_id"]): item for item in profile_lines
    }
    position_requirements = _selection_position_requirements(state)
    job_hash = str(state.job_hash or "").strip().casefold()
    context_hash = str(state.context_hash or "").strip().casefold()
    expected_positions = list(range(1, state.question_count + 1))
    checkpoint = _normalize_flow_checkpoint(state, state.selection_checkpoint)

    def paper_checkpoint(paper_index: int) -> dict[str, Any]:
        return dict(_checkpoint_paper(checkpoint, paper_index))

    def accept_callback_checkpoint(payload: Mapping[str, Any]) -> None:
        nonlocal checkpoint
        checkpoint = _normalize_flow_checkpoint(state, payload)

    def persist_shard(record: Mapping[str, Any]) -> None:
        paper_index = int(record["paper_index"])
        shard = {
            key: value for key, value in record.items() if key != "paper_index"
        }
        accept_callback_checkpoint(
            upsert_selection_shard(
                paper_index=paper_index,
                shard=shard,
            )
        )
        persisted = next(
            (
                item
                for item in paper_checkpoint(paper_index)["shards"]
                if int(item["shard_index"]) == int(shard["shard_index"])
            ),
            None,
        )
        if persisted != shard:
            raise SelectionCheckpointConflictError(
                "selection shard callback did not persist the requested shard"
            )

    def shard_prompt(
        *,
        paper_index: int,
        positions: Sequence[int],
        candidates: Sequence[Mapping[str, Any]],
        selected_ids: Sequence[str],
        correction: str = "",
    ) -> str:
        intra_limits = intra_limits_by_paper[paper_index - 1]
        diversity_context = json.dumps(
            [
                profiles_by_id[candidate_id]
                for candidate_id in (
                    str(item.get("candidate_id") or "") for item in candidates
                )
                if candidate_id in profiles_by_id
            ],
            ensure_ascii=False,
        )
        requirements = {
            str(position): position_requirements[position]
            for position in positions
        }
        rejected = {
            str(position): list(candidate_ids)
            for position, candidate_ids in paper_checkpoint(paper_index)[
                "rejected_candidate_ids_by_position"
            ].items()
            if int(position) in positions
        }
        correction_rule = (
            "\n\n【上次选题未通过校验】\n"
            + str(correction)[:500]
            + "\n只重选本小片，不得改变已完成题位。"
            if correction
            else ""
        )
        return f'''你是{state.institution}{state.subject}考研组卷专家。
    请只完成第 {paper_index} 份模拟卷的题位小片 {list(positions)}（本次共 {state.paper_count} 份），只能从本窗口候选题选择，不得生成或改写题目。

    【不可改变的所选结构年份】
    {json.dumps(state.structure_contract, ensure_ascii=False)}

    【板块题位合同】
    {json.dumps(slot_contract, ensure_ascii=False)}
    板块标题只用于定位 position 范围，不能替代候选卡中的实际题型；高置信卡不匹配显式题型族或知识组时必须拒绝，难度带不匹配只作排序提示，低置信卡只能提示并要求深读完整题干。

    【AI 结构分析】
    {json.dumps(state.past_analysis, ensure_ascii=False)}

    【全部候选材料的分批深读归纳】
    已理解 {state.material_understanding["analyzed_count"]} 道有效候选；analysis_confidence 低于 0.6 的卡只能作为软提示，不能作为硬槽位或多样性事实；以下全局归纳用于召回，最终选择必须复核后面的完整题干：
    {json.dumps(state.material_understanding["summary"], ensure_ascii=False)}

    【本小片冻结题源与普通章节要求】
    {json.dumps(requirements, ensure_ascii=False)}

    【本小片逐题位已拒绝候选】
    {json.dumps(rejected, ensure_ascii=False)}

    【本小片候选题（普通题库或其他院校同科真题）】
    {full_candidate_prompt_text(candidates)}

    【筛选要求】
    1. 必须以候选题 ID 为来源，不得凭空生成题目。
    2. 恰好选择 {len(positions)} 个互不重复的候选题，仅覆盖题位 {list(positions)}。
    3. 每个冻结题位恰好出现一次，并满足本小片冻结题源与普通章节要求；题型、难度梯度和知识点比例贴近 {state.reference_year} 年真题。
    4. 优先保证覆盖面与真题命题特色，避免同一章节或同类题过度集中。
    5. 已完成题位的候选 ID 为 {json.dumps(list(selected_ids), ensure_ascii=False)}，不得重复选择。
    6. 当前任务（同一 generation job，含同卷和多卷）已选择的候选 ID 与完全相同题面属于硬约束，必须避免；未掌握题也不得豁免当前任务的 ID、题面、相似簇或画像等任何既有硬规则。跨任务历史 ID {json.dumps(sorted(recent_ids), ensure_ascii=False)} 仅用于软排序与审计，不得据此判定失败或触发重选；未掌握题 {json.dumps(sorted(unmastered_candidate_ids), ensure_ascii=False)} 只免除这项跨任务历史软避让。
       候选材料画像如下，请优先选择不同的相似簇、题型和知识点组合：{diversity_context}
    7. 选题阶段一律不允许改编；所有 adapted_content 必须为空字符串。
    8. {chapter_rule}
    9. {source_rule}
    10. 真题难度只服从上面的逐题位合同；不得套用自由/错题模式的整卷比例，也不得为凑比例改写候选事实。
    11. 完全相同的题目不得重复。仅 source_kind=past_exam 的真题候选参与真题相似簇、真题画像重复和真题画像禁复用判断；题型相似本身不得作为弃用依据；上述真题记忆字段的重复题数严格低于整卷题数 50% 时不得判定失败。各字段本地可行重复数：{json.dumps(intra_limits, ensure_ascii=False)}。
    12. 知识组、知识点及“题型+知识点”允许合理重复，但应尽量分散并扩大覆盖面；这些指标只用于审计，不会单独判定失败。
    {f"13. 用户补充要求：{state.note}" if state.note else ""}
    {correction_rule}

    只返回 JSON，不要输出 Markdown：
    {{"selected":[{{"candidate_id":"题目ID","position":1,"difficulty":"中等","reason":"匹配原因","adapted_content":""}}]}}'''

    selections: list[Sequence[dict[str, Any]]] = []
    latex_bodies: list[str] = []
    selection_hashes: dict[int, str] = {}
    used_ids: set[str] = set()
    used_traits = {field: set() for field, _label in _DIVERSITY_FIELDS}
    diversity_audit = []
    # Selection and cross-paper diversity are order-dependent, while
    # difficulty review is not. Defer the latter without skipping any
    # deterministic validation for restored papers.
    validator = validate_selection_without_audit or validate_selection

    def failed_positions_from(error: BaseException) -> tuple[int, ...]:
        raw_positions = getattr(error, "failed_positions", ())
        details = getattr(error, "details", None)
        if not raw_positions and isinstance(details, Mapping):
            raw_positions = details.get("failed_positions") or ()
        positions: set[int] = set()
        if isinstance(raw_positions, (list, tuple, set, frozenset)):
            for raw_position in raw_positions:
                try:
                    position = int(raw_position)
                except (TypeError, ValueError):
                    continue
                if 1 <= position <= state.question_count:
                    positions.add(position)
        return tuple(sorted(positions))

    for index, candidates in enumerate(state.paper_candidate_pools):
        paper_index = index + 1
        policy = _diversity_policy(state, candidates, used_ids, used_traits)
        candidate_map = {
            str(item.get("candidate_id") or ""): item for item in candidates
        }

        def restore_complete_shards() -> dict[int, dict[str, Any]]:
            paper = paper_checkpoint(paper_index)
            rejected = paper["rejected_candidate_ids_by_position"]
            restored: dict[int, dict[str, Any]] = {}
            for record in paper["shards"]:
                if record["status"] != "complete":
                    continue
                for position, candidate_id in zip(
                    record["positions"], record["candidate_ids"]
                ):
                    position = int(position)
                    candidate_id = str(candidate_id)
                    candidate = candidate_map.get(candidate_id)
                    if candidate is None:
                        raise SelectionCheckpointConflictError(
                            "selection checkpoint references a candidate outside the frozen pool"
                        )
                    if not _candidate_matches_frozen_position(
                        candidate,
                        position_requirements[position],
                        rejected,
                        position,
                    ):
                        raise SelectionCheckpointConflictError(
                            "selection checkpoint candidate violates a frozen position"
                        )
                    item = dict(candidate)
                    item["position"] = position
                    restored[position] = item
            return restored

        def request_leaf(
            positions: Sequence[int],
            *,
            shard_index: int | None = None,
            correction: str = "",
            allow_validation_retry: bool = True,
        ) -> None:
            frozen_positions = tuple(int(value) for value in positions)
            if not frozen_positions:
                return
            selected_ids = {
                str(item.get("candidate_id") or "")
                for item in selected_by_position.values()
            }
            rejected = paper_checkpoint(paper_index)[
                "rejected_candidate_ids_by_position"
            ]
            window = _selection_candidate_window(
                state,
                candidates,
                frozen_positions,
                selected_ids,
                rejected_candidate_ids_by_position=rejected,
            )
            prompt = shard_prompt(
                paper_index=paper_index,
                positions=frozen_positions,
                candidates=window,
                selected_ids=sorted(selected_ids),
                correction=correction,
            )
            prompts.append(prompt)
            effort = state.selection_reasoning_effort
            try:
                if effort:
                    raw = request_selection(prompt, reasoning_effort=effort)
                else:
                    raw = request_selection(prompt)
                leaf_selection = _parse_selection_shard(
                    raw,
                    window,
                    frozen_positions,
                    position_requirements=position_requirements,
                    rejected_candidate_ids_by_position=rejected,
                )
            except Exception as exc:
                if isinstance(exc, (JobCancelled, JobLeaseLost)):
                    raise
                if _is_transient_selection_error(exc):
                    if len(frozen_positions) > 1:
                        midpoint = max(1, len(frozen_positions) // 2)
                        request_leaf(
                            frozen_positions[:midpoint],
                            shard_index=shard_index,
                        )
                        request_leaf(frozen_positions[midpoint:])
                        return
                    medium_prompt = shard_prompt(
                        paper_index=paper_index,
                        positions=frozen_positions,
                        candidates=window,
                        selected_ids=sorted(selected_ids),
                        correction=(
                            "上游暂态失败，仅对这个未完成单题片降低思考强度后重试"
                        ),
                    )
                    prompts.append(medium_prompt)
                    raw = request_selection(
                        medium_prompt, reasoning_effort="medium"
                    )
                    leaf_selection = _parse_selection_shard(
                        raw,
                        window,
                        frozen_positions,
                        position_requirements=position_requirements,
                        rejected_candidate_ids_by_position=rejected,
                    )
                    effort = "medium"
                elif not state.testing and allow_validation_retry:
                    request_leaf(
                        frozen_positions,
                        shard_index=shard_index,
                        correction=(
                            str(exc)
                            + "。请重新选择本小片全部题目，并严格满足章节、"
                            "跨试卷去重及其他硬性约束"
                        ),
                        allow_validation_retry=False,
                    )
                    return
                else:
                    if (
                        isinstance(exc, ValueError)
                        and not state.testing
                        and not allow_validation_retry
                    ):
                        raise ExamGenerationConstraintError(
                            "候选题无法同时满足本卷结构与硬性多样性约束",
                            code="past_selection_constraint",
                            hint="请调整候选题源，或选择题量更少的结构年份后重试",
                            actions=(
                                "adjust_candidate_source",
                                "adjust_reference_year",
                            ),
                            details={
                                "paper_index": paper_index,
                                "positions": list(frozen_positions),
                                "validation_error": str(exc)[:500],
                            },
                        ) from exc
                    raise
            for item in leaf_selection:
                selected_by_position[int(item["position"])] = item
            candidate_ids = [
                str(selected_by_position[position].get("candidate_id") or "")
                for position in frozen_positions
            ]
            request_hash = _stable_json_hash({
                "context_hash": context_hash,
                "paper_index": paper_index,
                "positions": list(frozen_positions),
                "window_candidate_ids": [
                    str(item.get("candidate_id") or "") for item in window
                ],
                "rejected_candidate_ids_by_position": {
                    str(position): list(rejected.get(str(position), ()))
                    for position in frozen_positions
                    if rejected.get(str(position))
                },
                "effort": effort or "high",
            })
            persisted_shard_index = shard_index
            if persisted_shard_index is None:
                preferred = min(frozen_positions)
                occupied = {
                    int(item["shard_index"])
                    for item in paper_checkpoint(paper_index)["shards"]
                }
                if preferred not in occupied:
                    persisted_shard_index = preferred
                else:
                    persisted_shard_index = max(
                        [state.question_count, *occupied]
                    ) + 1
            persist_shard({
                "paper_index": paper_index,
                "shard_index": int(persisted_shard_index),
                "positions": list(frozen_positions),
                "candidate_ids": candidate_ids,
                "status": "complete",
                "effort": effort or "high",
                "request_hash": request_hash,
            })

        def fill_pending_positions(*, correction: str = "") -> None:
            pending_records = [
                record
                for record in paper_checkpoint(paper_index)["shards"]
                if record["status"] != "complete"
            ]
            requested: set[int] = set()
            for record in pending_records:
                positions = [
                    int(position)
                    for position in record["positions"]
                    if int(position) not in selected_by_position
                ]
                if not positions:
                    continue
                request_leaf(
                    positions,
                    shard_index=int(record["shard_index"]),
                    correction=correction,
                )
                requested.update(positions)
            pending_positions = [
                position
                for position in expected_positions
                if (
                    position not in selected_by_position
                    and position not in requested
                )
            ]
            for offset in range(
                0, len(pending_positions), MAX_SELECTION_SHARD_POSITIONS
            ):
                request_leaf(
                    pending_positions[
                        offset:offset + MAX_SELECTION_SHARD_POSITIONS
                    ],
                    correction=correction,
                )

        def validate_whole_paper():
            assembled = [
                selected_by_position[position]
                for position in expected_positions
            ]
            return _validate_past_paper_selection(
                state,
                assembled,
                candidates,
                position_requirements=position_requirements,
                rejected_candidate_ids_by_position=paper_checkpoint(
                    paper_index
                )["rejected_candidate_ids_by_position"],
                slot_contract=slot_contract,
                policy=policy,
                validate_selection=validator,
                build_exam_body=build_exam_body,
                generated_question_count=generated_question_count,
            )

        selected_by_position = restore_complete_shards()
        fill_pending_positions()
        try:
            selected, slot_decisions, body = validate_whole_paper()
        except Exception as selection_error:
            if isinstance(selection_error, (JobCancelled, JobLeaseLost)):
                raise
            if state.testing:
                raise
            failed_positions = failed_positions_from(selection_error)
            correction = (
                str(selection_error)
                + "。请重新选择本卷失效题位，并严格满足章节、跨试卷去重及其他硬性约束；"
                + "本卷启用的跨卷硬约束如下："
                + json.dumps(policy["forbidden"], ensure_ascii=False)
                + "；跨卷各项最多允许重复题数："
                + json.dumps(policy["repeat_limits"], ensure_ascii=False)
                + "；本卷各项最多允许重复题数："
                + json.dumps(
                    policy.get("intra_repeat_limits") or {}, ensure_ascii=False
                )
            )
            if failed_positions:
                rejected_candidates = {
                    str(position): [
                        str(
                            selected_by_position[position].get("candidate_id")
                            or ""
                        )
                    ]
                    for position in failed_positions
                }
                accept_callback_checkpoint(
                    reject_selection_candidates(
                        paper_index=paper_index,
                        rejected_candidate_ids_by_position=rejected_candidates,
                    )
                )
            else:
                accept_callback_checkpoint(
                    clear_selection_paper(paper_index=paper_index)
                )
            selected_by_position = restore_complete_shards()
            try:
                fill_pending_positions(correction=correction)
                selected, slot_decisions, body = validate_whole_paper()
            except Exception as corrected_error:
                if isinstance(
                    corrected_error,
                    (JobCancelled, JobLeaseLost, SelectionCheckpointError),
                ):
                    raise
                constraint_details = {
                    "paper_index": paper_index,
                    "validation_error": str(corrected_error)[:500],
                }
                if isinstance(corrected_error, DiversityConstraintViolation):
                    constraint_details["violations"] = [
                        dict(item) for item in corrected_error.violations
                    ]
                raise ExamGenerationConstraintError(
                    "候选题无法同时满足本卷结构与硬性多样性约束",
                    code="past_selection_constraint",
                    hint="请调整候选题源，或选择题量更少的结构年份后重试",
                    actions=("adjust_candidate_source", "adjust_reference_year"),
                    details=constraint_details,
                ) from corrected_error

        validation_status = paper_checkpoint(paper_index)["validation_status"]
        if validation_status == "provisional":
            accept_callback_checkpoint(
                mark_selection_validated(paper_index=paper_index)
            )
            if paper_checkpoint(paper_index)["validation_status"] != "selection_validated":
                raise SelectionCheckpointConflictError(
                    "selection validation callback did not advance the paper"
                )
        selection_hash = paper_checkpoint(paper_index)["selection_hash"]
        if not isinstance(selection_hash, str) or not selection_hash:
            raise SelectionCheckpointConflictError(
                "validated selection checkpoint is missing its selection hash"
            )
        reviewed_selection_hash = selection_mapping_hash({
            int(item["position"]): str(item.get("candidate_id") or "")
            for item in selected
        })
        if selection_hash != reviewed_selection_hash:
            raise SelectionCheckpointConflictError(
                "validated selection hash does not match the reviewed paper"
            )
        selection_hashes[paper_index] = selection_hash

        selections.append(selected)
        latex_bodies.append(body)
        diversity_audit.append(_record_paper_validation_audit(
            state,
            paper_index=paper_index,
            selected=selected,
            policy=policy,
            slot_contract=slot_contract,
            slot_decisions=slot_decisions,
            used_ids=used_ids,
            used_traits=used_traits,
        ))
    if audit_difficulties is not None and selections:
        def review_paper(review_input):
            paper_index, selected = review_input
            try:
                labels = list(
                    audit_difficulties(
                        [str(item.get("content") or "") for item in selected]
                    )
                )
                if len(labels) != len(selected):
                    raise ValueError(
                        f"复核数量应为{len(selected)}，实际为{len(labels)}"
                    )
                allowed = {"简单", "中等", "困难"}
                if any(str(label) not in allowed for label in labels):
                    raise ValueError("复核结果包含无效难度标签")
                unknown_quota = set(state.difficulty_quota) - allowed
                if unknown_quota:
                    raise ValueError(
                        "难度配额包含未知标签："
                        + ",".join(sorted(str(item) for item in unknown_quota))
                    )
                return [str(label) for label in labels]
            except ExamGenerationConstraintError:
                raise
            except (TypeError, ValueError) as exc:
                raise ExamGenerationConstraintError(
                    f"第{paper_index}份试卷的难度复核结果无效",
                    code="past_difficulty_review_invalid",
                    hint="请稍后重试；若持续失败请减少试卷份数",
                    actions=("adjust_candidate_source", "adjust_reference_year"),
                    details={
                        "paper_index": paper_index,
                        "validation_error": str(exc)[:500],
                    },
                ) from exc

        workers = max(1, min(3, len(selections)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            audited = list(
                executor.map(review_paper, enumerate(selections, start=1))
            )
        for paper_index, (selected, labels) in enumerate(
            zip(selections, audited), start=1
        ):
            if len(labels) != len(selected):
                raise ExamGenerationConstraintError(
                    f"第{paper_index}份试卷的难度复核数量不一致",
                    code="past_difficulty_review_invalid",
                    hint="请稍后重试；若持续失败请减少试卷份数",
                    actions=("adjust_candidate_source", "adjust_reference_year"),
                    details={"paper_index": paper_index},
                )
            for item, label in zip(selected, labels):
                item["difficulty"] = label
            audit = audit_difficulty_labels(labels)
            diversity_audit[paper_index - 1]["difficulty_audit"] = (
                audit.to_dict()
            )
    return PastGenerationResult(
        selection_prompts=prompts,
        selections=selections,
        latex_bodies=latex_bodies,
        diversity_audit=diversity_audit,
        selection_hashes=dict(sorted(selection_hashes.items())),
        job_hash=job_hash,
        context_hash=context_hash,
        legacy_context_hash=_selection_context_hash(state),
    )


__all__ = [
    "DiversityConstraintViolation",
    "PAST_MEMORY_REJECTION_THRESHOLD",
    "PastGenerationInput",
    "PastGenerationResult",
    "PastSelectionValidationResult",
    "generate_past_exam",
    "select_other_past_candidates",
    "validate_past_exam_selections",
]
