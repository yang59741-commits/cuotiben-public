"""Pure selection contracts and immutable exam item snapshots."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Mapping, Sequence

from .exam_contracts import (
    DIFFICULTIES,
    DIFFICULTY_GUIDANCE,
    EXAM_TARGETS,
    MAX_MISTAKE_REFERENCES,
    PAST_CANDIDATE_SOURCES,
    QUESTION_TYPES,
    TARGET_GUIDANCE,
    positive_int,
)

from .past_exams import PastExamSourceError
from .past_exam_analysis import analysis_section_key


DIFFICULTY_EVIDENCE_SCHEMA_VERSION = "past-difficulty-evidence-v1"
SECTION_SLOT_SCHEMA_VERSION = "section-slot-v1"
_EVIDENCE_DIFFICULTIES = ("简单", "中等", "困难")
_SECTION_PROFILE_KEYS = (
    "allowed_question_types", "question_types", "question_type_families"
)


def _normalized_string_list(value, *, maximum=32):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    seen = set()
    for raw in value:
        if not isinstance(raw, str):
            continue
        item = " ".join(raw.split())[:160]
        key = analysis_section_key(item)
        if item and key and key not in seen:
            seen.add(key)
            result.append(item)
        if len(result) >= maximum:
            break
    return result


def largest_remainder_difficulty_quota(
    cards,
    question_count,
    *,
    minimum_coverage=0.6,
    minimum_evidence_count=3,
    confidence_threshold=0.6,
    learning_years=None,
):
    """Derive a reproducible quota from validated frozen learning-card evidence."""

    count = positive_int(question_count, "试卷题量")
    if (
        isinstance(minimum_coverage, bool)
        or not isinstance(minimum_coverage, (int, float))
        or not 0 <= float(minimum_coverage) <= 1
    ):
        raise ValueError("最低证据覆盖率必须在 0 到 1 之间")
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not 0 <= float(confidence_threshold) <= 1
    ):
        raise ValueError("永久卡置信度阈值必须在 0 到 1 之间")
    minimum_count = positive_int(minimum_evidence_count, "最低证据卡数量")
    raw_values = list(cards) if isinstance(cards, Sequence) and not isinstance(cards, (str, bytes)) else []
    frozen_years = None
    if learning_years is not None:
        if not isinstance(learning_years, Sequence) or isinstance(learning_years, (str, bytes)):
            raise ValueError("固定学习年份必须是数组")
        try:
            frozen_years = tuple(dict.fromkeys(int(year) for year in learning_years))
        except (TypeError, ValueError) as exc:
            raise ValueError("固定学习年份无效") from exc
        if not frozen_years:
            raise ValueError("固定学习年份不能为空")
    def card_year(raw):
        if not isinstance(raw, Mapping) or isinstance(raw.get("year"), bool):
            return None
        try:
            return int(raw["year"]) if raw.get("year") is not None else None
        except (TypeError, ValueError):
            return None
    values = [
        raw for raw in raw_values
        if frozen_years is None
        or card_year(raw) in frozen_years
    ]
    eligible = []
    id_counts = Counter(
        raw.get("candidate_id")
        for raw in values
        if isinstance(raw, Mapping)
        and isinstance(raw.get("candidate_id"), str)
        and raw.get("candidate_id").strip()
    )
    for raw in values:
        if not isinstance(raw, Mapping):
            continue
        candidate_id = raw.get("candidate_id")
        difficulty = raw.get("difficulty")
        confidence = raw.get("analysis_confidence")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or id_counts[candidate_id] != 1
            or difficulty not in _EVIDENCE_DIFFICULTIES
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(float(confidence))
            or float(confidence) < float(confidence_threshold)
        ):
            continue
        eligible.append((candidate_id, str(difficulty), float(confidence)))
    total_count = len(values)
    evidence_count = len(eligible)
    coverage = evidence_count / total_count if total_count else 0.0
    difficulty_counts = Counter(item[1] for item in eligible)
    metadata = {
        "schema_version": DIFFICULTY_EVIDENCE_SCHEMA_VERSION,
        "question_count": count,
        "learning_years": list(frozen_years or sorted({
            year for year in (card_year(raw) for raw in values) if year is not None
        })),
        "total_card_count": total_count,
        "evidence_count": evidence_count,
        "coverage": round(coverage, 8),
        "coverage_ratio": round(coverage, 8),
        "confidence": round(
            sum(item[2] for item in eligible) / evidence_count, 8
        ) if evidence_count else 0.0,
        "confidence_threshold": float(confidence_threshold),
        "minimum_coverage": float(minimum_coverage),
        "minimum_evidence_count": minimum_count,
        "difficulty_counts": {
            label: int(difficulty_counts.get(label, 0))
            for label in _EVIDENCE_DIFFICULTIES
        },
        "quota": None,
        "mode": "fallback",
        "fallback_reason": None,
    }
    if not values:
        metadata["fallback_reason"] = "no_learning_cards"
        return None, metadata
    if evidence_count < minimum_count:
        metadata["fallback_reason"] = "insufficient_high_confidence_cards"
        return None, metadata
    if coverage < float(minimum_coverage):
        metadata["fallback_reason"] = "insufficient_evidence_coverage"
        return None, metadata

    numerators = {
        label: count * difficulty_counts.get(label, 0)
        for label in _EVIDENCE_DIFFICULTIES
    }
    quota = {
        label: numerators[label] // evidence_count
        for label in _EVIDENCE_DIFFICULTIES
    }
    remaining = count - sum(quota.values())
    order = {label: index for index, label in enumerate(_EVIDENCE_DIFFICULTIES)}
    ranked = sorted(
        _EVIDENCE_DIFFICULTIES,
        key=lambda label: (
            -(numerators[label] % evidence_count),
            order[label],
        ),
    )
    for label in ranked[:remaining]:
        quota[label] += 1
    metadata.update({"quota": dict(quota), "mode": "evidence"})
    return quota, metadata


def build_section_slot_contract(
    structure_contract,
    *,
    section_profiles=None,
    evidence_cards=None,
    confidence_threshold=0.6,
):
    """Freeze position ranges and only explicit semantic slot restrictions."""

    if not isinstance(structure_contract, Mapping):
        raise ValueError("真题结构合同无效")
    if (
        isinstance(confidence_threshold, bool)
        or not isinstance(confidence_threshold, (int, float))
        or not 0 <= float(confidence_threshold) <= 1
    ):
        raise ValueError("板块题位置信度阈值无效")
    sections = structure_contract.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ValueError("真题结构合同缺少板块")
    profiles = section_profiles if isinstance(section_profiles, Mapping) else {}
    cards = list(evidence_cards) if isinstance(evidence_cards, Sequence) and not isinstance(evidence_cards, (str, bytes)) else []
    card_id_counts = Counter(
        card.get("candidate_id")
        for card in cards
        if isinstance(card, Mapping) and isinstance(card.get("candidate_id"), str)
    )
    slots = []
    next_position = 1
    for index, raw in enumerate(sections, start=1):
        if not isinstance(raw, Mapping):
            raise ValueError("真题结构板块必须是对象")
        label = str(raw.get("label") or raw.get("title") or "").strip()
        if not label:
            raise ValueError("真题结构板块缺少标题")
        section_count = positive_int(raw.get("question_count"), "板块题量")
        profile = profiles.get(label)
        profile = profile if isinstance(profile, Mapping) else {}
        question_numbers = {
            str(value).strip()
            for value in raw.get("question_numbers") or []
            if str(value).strip()
        }
        section_cards = []
        soft_hint_count = 0
        for card in cards:
            if not isinstance(card, Mapping):
                continue
            candidate_id = card.get("candidate_id")
            source_number = str(card.get("source_number") or "").strip()
            confidence = card.get("analysis_confidence")
            if (
                not isinstance(candidate_id, str)
                or card_id_counts[candidate_id] != 1
            ):
                continue
            if question_numbers:
                if source_number not in question_numbers:
                    continue
            elif source_number.isdigit() and not (
                next_position <= int(source_number) <= next_position + section_count - 1
            ):
                continue
            else:
                continue
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(float(confidence))
            ):
                continue
            if float(confidence) < float(confidence_threshold):
                soft_hint_count += 1
                continue
            section_cards.append(card)
        allowed_types = []
        for key in _SECTION_PROFILE_KEYS:
            allowed_types = _normalized_string_list(
                profile.get(key) if key in profile else raw.get(key)
            )
            if allowed_types:
                break
        if not allowed_types:
            allowed_types = _normalized_string_list([
                str(card.get("question_type") or "") for card in section_cards
            ])
        allowed_difficulties = _normalized_string_list(
            profile.get("allowed_difficulties")
            if "allowed_difficulties" in profile
            else raw.get("allowed_difficulties")
        )
        if any(value not in _EVIDENCE_DIFFICULTIES for value in allowed_difficulties):
            raise ValueError("板块难度带包含未知难度")
        if not allowed_difficulties:
            allowed_difficulties = _normalized_string_list([
                str(card.get("difficulty") or "") for card in section_cards
                if str(card.get("difficulty") or "") in _EVIDENCE_DIFFICULTIES
            ])
        knowledge_groups = _normalized_string_list(
            profile.get("knowledge_groups")
            if "knowledge_groups" in profile
            else raw.get("knowledge_groups")
        )
        if not knowledge_groups:
            knowledge_groups = _normalized_string_list([
                str(point)
                for card in section_cards
                for point in (card.get("knowledge_points") or [])
            ])
        slots.append({
            "slot_id": f"section-{index}",
            "section_label": label,
            "position_start": next_position,
            "position_end": next_position + section_count - 1,
            "allowed_question_type_families": allowed_types,
            "knowledge_groups": knowledge_groups,
            "allowed_difficulties": allowed_difficulties,
            "title_is_advisory_only": True,
            "high_confidence_evidence_count": len(section_cards),
            "low_confidence_soft_hint_count": soft_hint_count,
            "requires_deep_read": bool(soft_hint_count),
            "constraint_source": (
                "explicit_profile" if profile else
                "permanent_card_evidence" if section_cards else
                "position_only"
            ),
        })
        next_position += section_count
    declared_count = structure_contract.get("question_count")
    if declared_count is not None and int(declared_count) != next_position - 1:
        raise ValueError("真题结构题量与板块位置范围不一致")
    return {
        "schema_version": SECTION_SLOT_SCHEMA_VERSION,
        "question_count": next_position - 1,
        "confidence_threshold": float(confidence_threshold),
        "slots": slots,
    }


def build_position_only_section_slot_contract(question_count):
    """Legacy adapter: cover positions without inferring semantics from titles."""

    count = positive_int(question_count, "试卷题量")
    return {
        "schema_version": SECTION_SLOT_SCHEMA_VERSION,
        "question_count": count,
        "confidence_threshold": 0.6,
        "compatibility_mode": "legacy_position_only",
        "slots": [{
            "slot_id": "legacy-position-range",
            "section_label": "旧合同未映射板块",
            "position_start": 1,
            "position_end": count,
            "allowed_question_type_families": [],
            "knowledge_groups": [],
            "allowed_difficulties": [],
            "title_is_advisory_only": True,
            "high_confidence_evidence_count": 0,
            "low_confidence_soft_hint_count": 0,
            "requires_deep_read": True,
            "constraint_source": "legacy_position_only",
        }],
    }


def evaluate_section_slot_candidate(contract, position, card):
    """Return a hard decision only when a high-confidence card supports it."""

    if not isinstance(contract, Mapping) or contract.get("schema_version") != SECTION_SLOT_SCHEMA_VERSION:
        raise ValueError("板块题位合同版本无效")
    position = positive_int(position, "题目 position")
    matches = [
        slot for slot in (contract.get("slots") or [])
        if isinstance(slot, Mapping)
        and int(slot.get("position_start") or 0) <= position <= int(slot.get("position_end") or 0)
    ]
    if len(matches) != 1:
        raise ValueError("题目 position 未唯一落入板块题位")
    slot = matches[0]
    if not isinstance(card, Mapping):
        return {"hard_match": None, "needs_deep_read": True, "reason_codes": ["card_missing"], "slot": dict(slot)}
    confidence = card.get("analysis_confidence")
    threshold = float(contract.get("confidence_threshold") or 0.6)
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or float(confidence) < threshold
    ):
        return {"hard_match": None, "needs_deep_read": True, "reason_codes": ["low_confidence_soft_hint"], "slot": dict(slot)}
    hard_reasons = []
    soft_reasons = []
    allowed_types = {
        analysis_section_key(value).removesuffix("题")
        for value in slot.get("allowed_question_type_families") or []
    }
    actual_type = analysis_section_key(card.get("question_type")).removesuffix("题")
    if allowed_types and not actual_type:
        return {"hard_match": None, "needs_deep_read": True, "reason_codes": ["question_type_missing"], "slot": dict(slot)}
    if allowed_types and not any(
        actual_type == allowed or actual_type in allowed or allowed in actual_type
        for allowed in allowed_types
    ):
        hard_reasons.append("question_type_family_mismatch")
    allowed_difficulties = set(slot.get("allowed_difficulties") or [])
    actual_difficulty = str(card.get("difficulty") or "")
    if allowed_difficulties and actual_difficulty not in _EVIDENCE_DIFFICULTIES:
        return {"hard_match": None, "needs_deep_read": True, "reason_codes": ["difficulty_missing"], "slot": dict(slot)}
    if allowed_difficulties and actual_difficulty not in allowed_difficulties:
        soft_reasons.append("difficulty_band_mismatch")
    groups = [analysis_section_key(value) for value in slot.get("knowledge_groups") or []]
    evidence_text = analysis_section_key(" ".join(
        [
            *[str(value) for value in card.get("knowledge_points") or []],
            *[str(value) for value in card.get("methods") or []],
            *[str(value) for value in card.get("semantic_keywords") or []],
        ]
    ))
    if groups and not evidence_text:
        return {"hard_match": None, "needs_deep_read": True, "reason_codes": ["knowledge_group_missing"], "slot": dict(slot)}
    if groups and not any(group and group in evidence_text for group in groups):
        hard_reasons.append("knowledge_group_mismatch")
    return {
        "hard_match": not hard_reasons,
        "needs_deep_read": False,
        "reason_codes": [*hard_reasons, *soft_reasons],
        "slot": dict(slot),
    }


def validate_section_slot_selection(selection, contract, candidate_profiles):
    decisions = []
    profiles = candidate_profiles if isinstance(candidate_profiles, Mapping) else {}
    for item in selection:
        candidate_id = str(item.get("candidate_id") or "")
        decision = evaluate_section_slot_candidate(
            contract,
            item.get("position"),
            profiles.get(candidate_id),
        )
        if decision["hard_match"] is False:
            raise ValueError(
                f"候选题 {candidate_id} 不符合 position={item.get('position')} 的板块题位合同："
                + ",".join(decision["reason_codes"])
            )
        decisions.append({"candidate_id": candidate_id, "position": int(item["position"]), **decision})
    return decisions


def validated_past_selection(
    raw,
    candidates,
    question_count,
    chapter_plan,
    difficulty_quota,
    *,
    allow_adaptation,
    subject,
    parser,
    selector,
    testing=False,
    audit_questions=None,
    enforce_chapter_plan=True,
    source_quotas=None,
    target_institution=None,
):
    """Validate an AI selection against chapter, source and difficulty contracts."""

    parsed = parser(raw)
    if testing:
        aliases = {
            f"Q{position:03d}": candidate["candidate_id"]
            for position, candidate in enumerate(candidates, start=1)
        }
        raw_items = parsed.get("selected")
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    candidate_id = str(item.get("candidate_id") or "")
                    if candidate_id in aliases:
                        item["candidate_id"] = aliases[candidate_id]
            raw = json.dumps(parsed, ensure_ascii=False)
    selection = selector(
        raw, candidates, question_count,
        allow_adaptation=allow_adaptation, max_adaptations=1,
    )
    # Keep the legacy parameters for caller compatibility, but never let an
    # aggregate quota or an AI audit rewrite frozen past-paper difficulty.

    if enforce_chapter_plan:
        expected = {
            str(item["chapter"]): int(item["question_count"])
            for item in chapter_plan
        }
        actual = {
            chapter: sum(value.get("chapter") == chapter for value in selection)
            for chapter in expected
        }
        if actual != expected:
            raise ValueError(
                "AI选题章节数量不符合计划："
                f"需要{json.dumps(expected, ensure_ascii=False)}，"
                f"实际{json.dumps(actual, ensure_ascii=False)}"
            )
    if source_quotas:
        actual_sources = {
            kind: sum(
                str(value.get("source_kind") or "ordinary") == kind
                for value in selection
            )
            for kind in source_quotas
        }
        if actual_sources != source_quotas:
            raise ValueError(
                "AI选题来源数量不符合要求："
                f"需要{json.dumps(source_quotas, ensure_ascii=False)}，"
                f"实际{json.dumps(actual_sources, ensure_ascii=False)}"
            )
    target = str(target_institution or "").strip()
    if target and any(
        str(value.get("source_kind") or "ordinary") == "past_exam"
        and str(value.get("source_institution") or "").strip() == target
        for value in selection
    ):
        raise ValueError("他校真题候选中包含目标院校题目")
    return selection



EXAM_AI_TIMEOUT_SECONDS = 600
_PDF_NAME = re.compile(r"exam_([1-9]\d*)\.pdf")


