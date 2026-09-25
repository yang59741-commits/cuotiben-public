"""Material deep-reading and cache runtime for exam generation."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .material_understanding import (
    LOW_CONFIDENCE_THRESHOLD,
    MATERIAL_PROVENANCE_FIELDS,
    PROMPT_VERSION,
)
from .semantic_index import (
    CARD_ALLOWED_FIELDS,
    SEMANTIC_CARD_SCHEMA_VERSION,
    SemanticIndexContractError,
    semantic_content_hash,
    validate_semantic_card,
)


FROZEN_MATERIAL_SCHEMA_VERSION = 2


def _reject_duplicate_card_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise SemanticIndexContractError("永久材料卡包含重复 JSON 字段")
        value[key] = item
    return value


def understand_material(
    candidates,
    snapshot,
    kind,
    generation_job_id=None,
    *,
    testing=False,
    model=None,
    reasoning_effort="max",
    wire_api="chat_completions",
    cache_root=None,
    ensure_understanding=None,
    progress=None,
):
    if testing:
        chapters = sorted({str(item.get("chapter") or "未归类") for item in candidates})
        years = sorted(
            {int(item["year"]) for item in candidates if item.get("year") is not None}
        )
        cards = []
        seen: set[str] = set()
        for item in candidates:
            candidate_id = str(item.get("candidate_id") or "").strip()
            content = str(item.get("content") or "")
            if not candidate_id or candidate_id in seen or not content.strip():
                raise ValueError("测试材料候选身份或正文无效")
            seen.add(candidate_id)
            cards.append({
                "candidate_id": candidate_id,
                "knowledge_points": [],
                "question_type": "未知",
                "methods": [],
                "difficulty": "未知",
                "discrimination": "未知",
                "similarity_group": "",
                "characteristics": [],
                "prerequisites": [],
                "common_traps": [],
                "semantic_keywords": [],
                "source_constraints": [],
                "analysis_confidence": 0.0,
                "content_hash": semantic_content_hash(content),
                "chapter": str(item.get("chapter") or "未归类"),
                "source_file": str(item.get("source_file") or "未知来源"),
                "source_number": item.get("source_number"),
                **({"year": int(item["year"])} if item.get("year") is not None else {}),
            })
        return {
            "complete": True, "cache_key": "testing", "snapshot": snapshot,
            "total_count": len(candidates), "analyzed_count": len(candidates),
            "batch_count": 0, "reused_count": len(candidates),
            "missing_count": 0, "cards": cards,
            "chapters": chapters, "years": years, "sources": {},
            "summary": {
                "chapters": [],
                "global": {
                    "quota_guidance": [], "difficulty_gradient": [],
                    "evidence_ids": [],
                },
            },
        }
    if ensure_understanding is None or model is None or cache_root is None:
        raise ValueError("材料理解运行时未配置")
    return ensure_understanding(
        candidates=candidates,
        snapshot=snapshot,
        model=model,
        cache_dir=Path(cache_root) / kind,
        reasoning_effort=reasoning_effort,
        wire_api=wire_api,
        freeze_id=generation_job_id,
        progress=progress,
    )


def _bounded_unique(values, maximum=24):
    result = []
    seen = set()
    for raw in values:
        value = str(raw or "").strip()
        key = value.casefold()
        if not value or key in seen:
            continue
        seen.add(key)
        result.append(value)
        if len(result) >= maximum:
            break
    return result


def _evidence_suggestions(
    value: object,
    allowed_ids: set[str],
    *,
    maximum: int = 32,
) -> list[dict[str, Any]]:
    if maximum < 1 or not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
            continue
        text = " ".join(raw["text"].split())[:500]
        raw_ids = raw.get("evidence_ids")
        if not text or not isinstance(raw_ids, list):
            continue
        evidence_ids: list[str] = []
        for candidate_id in raw_ids:
            if (
                isinstance(candidate_id, str)
                and candidate_id in allowed_ids
                and candidate_id not in evidence_ids
            ):
                evidence_ids.append(candidate_id)
        if evidence_ids:
            result.append({"text": text, "evidence_ids": evidence_ids})
        if len(result) >= maximum:
            break
    return result


def _load_card_rows(db, card_version_ids: Sequence[int]) -> dict[int, Mapping[str, Any]]:
    rows: dict[int, Mapping[str, Any]] = {}
    unique = list(dict.fromkeys(card_version_ids))
    for start in range(0, len(unique), 500):
        batch = unique[start:start + 500]
        if not batch:
            continue
        cursor = db.execute(
            "SELECT c.id,c.content_hash,c.analysis_schema_version,c.prompt_version,"
            "c.card_json,c.status,q.source_kind,q.candidate_id "
            "FROM semantic_card_versions AS c "
            "JOIN semantic_questions AS q ON q.id=c.semantic_question_id "
            "WHERE c.id IN ("
            + ",".join("?" for _ in batch)
            + ")",
            batch,
        )
        columns = [item[0] for item in cursor.description]
        for row in cursor.fetchall():
            value = dict(zip(columns, row))
            rows[int(value["id"])] = value
    return rows


def _material_card(candidate: Mapping[str, Any], card) -> dict[str, Any]:
    payload = card.as_payload()
    payload.update({
        "content_hash": semantic_content_hash(str(candidate.get("content") or "")),
        "chapter": str(candidate.get("chapter") or "未归类"),
        "source_file": str(candidate.get("source_file") or "未知来源"),
        "source_number": candidate.get("source_number"),
    })
    if candidate.get("year") is not None:
        payload["year"] = int(candidate["year"])
    return payload


def _canonical_source_kind(candidate: Mapping[str, Any]) -> str:
    """Map the frozen legacy aliases without relaxing canonical membership."""
    explicit = str(candidate.get("source_kind") or "").strip().casefold()
    candidate_id = str(candidate.get("candidate_id") or "").strip().upper()
    if explicit in {"ordinary", "past_exam_local", "past_exam_other"}:
        return explicit
    if explicit == "past_exam" or not explicit:
        if candidate_id.startswith("PL"):
            return "past_exam_local"
        if candidate_id.startswith("PE"):
            return "past_exam_other"
        if not explicit:
            return "ordinary"
    raise ValueError("冻结候选 source_kind 无法规范化")


def _frozen_pool_identity(
    candidates: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
) -> None:
    if len(candidates) != len(decisions):
        raise ValueError("冻结候选与材料决策数量不一致")
    seen: set[str] = set()
    for candidate, decision in zip(candidates, decisions):
        if not isinstance(candidate, Mapping) or not isinstance(decision, Mapping):
            raise ValueError("冻结候选与材料决策必须是对象")
        candidate_id = candidate.get("candidate_id")
        decision_id = decision.get("candidate_id")
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id != candidate_id.strip()
        ):
            raise ValueError("冻结候选 candidate_id 无效")
        if not isinstance(decision_id, str) or decision_id != candidate_id:
            raise ValueError("冻结候选与材料决策身份不一致")
        if candidate_id in seen:
            raise ValueError("冻结候选包含重复 candidate_id")
        seen.add(candidate_id)
        source_kind = _canonical_source_kind(candidate)
        decision_kind = str(decision.get("source_kind") or "").strip()
        if not source_kind or source_kind != decision_kind:
            raise ValueError("冻结候选与材料决策来源身份不一致")


def _nonnegative_result_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


def _combined_summary(
    candidates: Sequence[Mapping[str, Any]],
    cards: Sequence[Mapping[str, Any]],
    legacy_summary: Mapping[str, Any] | None,
) -> dict[str, Any]:
    cards_by_id = {
        str(card.get("candidate_id") or ""): card
        for card in cards
        if isinstance(card, Mapping)
    }
    legacy_chapters: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    legacy_global: Mapping[str, Any] = {}
    if isinstance(legacy_summary, Mapping):
        for item in legacy_summary.get("chapters") or []:
            if isinstance(item, Mapping) and isinstance(item.get("chapter"), str):
                legacy_chapters[item["chapter"]].append(item)
        if isinstance(legacy_summary.get("global"), Mapping):
            legacy_global = legacy_summary["global"]
    chapters = []
    for chapter in sorted({str(item.get("chapter") or "未归类") for item in candidates}):
        chapter_candidates = [
            item for item in candidates
            if str(item.get("chapter") or "未归类") == chapter
        ]
        chapter_cards = [
            cards_by_id[str(item.get("candidate_id") or "")]
            for item in chapter_candidates
            if str(item.get("candidate_id") or "") in cards_by_id
        ]
        reliable_chapter_cards = [
            card for card in chapter_cards
            if float(card.get("analysis_confidence") or 0.0) >= LOW_CONFIDENCE_THRESHOLD
        ]
        soft_chapter_cards = [
            card for card in chapter_cards
            if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
        ]
        difficulty_distribution = Counter(
            str(card.get("difficulty") or "未知") for card in reliable_chapter_cards
        )
        question_type_distribution = Counter(
            str(card.get("question_type") or "未知") for card in reliable_chapter_cards
        )
        similarity_groups: dict[str, list[str]] = defaultdict(list)
        for card in chapter_cards:
            if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD:
                continue
            group = str(card.get("similarity_group") or "").strip()
            if group:
                similarity_groups[group].append(str(card.get("candidate_id") or ""))
        chapter_ids = [str(card.get("candidate_id") or "") for card in chapter_cards]
        recommendations: list[dict[str, Any]] = []
        for legacy in legacy_chapters.get(chapter, []):
            recommendations.extend(
                _evidence_suggestions(
                    legacy.get("recommendations"),
                    set(chapter_ids),
                    maximum=max(0, 32 - len(recommendations)),
                )
            )
        chapters.append({
            "chapter": chapter,
            "candidate_count": len(chapter_candidates),
            "valid_card_count": len(chapter_cards),
            "knowledge_points": _bounded_unique(
                point
                for card in chapter_cards
                for point in (card.get("knowledge_points") or [])
            ),
            "question_types": _bounded_unique(
                card.get("question_type") for card in chapter_cards
            ),
            "methods": _bounded_unique(
                method
                for card in chapter_cards
                for method in (card.get("methods") or [])
            ),
            "difficulty_distribution": dict(sorted(difficulty_distribution.items())),
            "question_type_distribution": dict(sorted(question_type_distribution.items())),
            "soft_hint_difficulty_distribution": dict(sorted(Counter(
                str(card.get("difficulty") or "未知") for card in soft_chapter_cards
            ).items())),
            "soft_hint_question_type_distribution": dict(sorted(Counter(
                str(card.get("question_type") or "未知") for card in soft_chapter_cards
            ).items())),
            "duplicate_groups": [
                {"group": group, "candidate_ids": ids}
                for group, ids in sorted(similarity_groups.items())
                if len(ids) > 1
            ],
            "low_confidence_ids": [
                str(card.get("candidate_id") or "")
                for card in chapter_cards
                if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
            ],
            "reliable_evidence_ids": [
                str(card.get("candidate_id") or "")
                for card in chapter_cards
                if float(card.get("analysis_confidence") or 0.0) >= LOW_CONFIDENCE_THRESHOLD
            ],
            "evidence_ids": chapter_ids,
            "recommendations": recommendations,
        })
    global_similarity_groups: dict[str, list[str]] = defaultdict(list)
    for card in cards:
        if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD:
            continue
        group = str(card.get("similarity_group") or "").strip()
        if group:
            global_similarity_groups[group].append(str(card.get("candidate_id") or ""))
    reliable_cards = [
        card for card in cards
        if float(card.get("analysis_confidence") or 0.0) >= LOW_CONFIDENCE_THRESHOLD
    ]
    soft_cards = [
        card for card in cards
        if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
    ]
    summary: dict[str, Any] = {
        "chapters": chapters,
        "global": {
            "candidate_count": len(candidates),
            "valid_card_count": len(cards),
            "quota_guidance": _evidence_suggestions(
                legacy_global.get("quota_guidance"), set(cards_by_id)
            ),
            "difficulty_gradient": _evidence_suggestions(
                legacy_global.get("difficulty_gradient"), set(cards_by_id)
            ),
            "difficulty_distribution": dict(sorted(Counter(
                str(card.get("difficulty") or "未知") for card in reliable_cards
            ).items())),
            "soft_hint_difficulty_distribution": dict(sorted(Counter(
                str(card.get("difficulty") or "未知") for card in soft_cards
            ).items())),
            "chapter_distribution": dict(sorted(Counter(
                str(card.get("chapter") or "未归类") for card in cards
            ).items())),
            "question_type_distribution": dict(sorted(Counter(
                str(card.get("question_type") or "未知") for card in reliable_cards
            ).items())),
            "soft_hint_question_type_distribution": dict(sorted(Counter(
                str(card.get("question_type") or "未知") for card in soft_cards
            ).items())),
            "duplicate_groups": [
                {"group": group, "candidate_ids": ids}
                for group, ids in sorted(global_similarity_groups.items())
                if len(ids) > 1
            ],
            "low_confidence_ids": [
                str(card.get("candidate_id") or "")
                for card in cards
                if float(card.get("analysis_confidence") or 0.0) < LOW_CONFIDENCE_THRESHOLD
            ],
            "reliable_evidence_ids": [
                str(card.get("candidate_id") or "")
                for card in cards
                if float(card.get("analysis_confidence") or 0.0) >= LOW_CONFIDENCE_THRESHOLD
            ],
            "evidence_ids": [str(card.get("candidate_id") or "") for card in cards],
        },
        "dossier": [
            {
                key: card.get(key)
                for key in (
                    "candidate_id", "knowledge_points", "question_type", "methods",
                    "difficulty", "common_traps", "source_constraints",
                    "analysis_confidence",
                )
            } | {
                "guidance_mode": (
                    "reliable_fact"
                    if float(card.get("analysis_confidence") or 0.0) >= LOW_CONFIDENCE_THRESHOLD
                    else "soft_hint"
                )
            }
            for card in cards
        ],
    }
    return summary


def understand_frozen_material(
    db,
    candidates: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    *,
    snapshot: str,
    kind: str,
    generation_job_id: str | None,
    deep_reader: Callable[[Sequence[Mapping[str, Any]], str, str, str | None], Mapping[str, Any]],
    summary_reader: Callable[..., Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reuse exact frozen cards and deep-read only the eligible final-pool remainder."""

    _frozen_pool_identity(candidates, decisions)
    card_ids = [
        int(decision["card_version_id"])
        for decision in decisions
        if isinstance(decision.get("card_version_id"), int)
        and not isinstance(decision.get("card_version_id"), bool)
        and int(decision["card_version_id"]) > 0
    ]
    rows = _load_card_rows(db, card_ids)
    permanent_cards: list[dict[str, Any]] = []
    deep_candidates: list[Mapping[str, Any]] = []
    skipped_ids: list[str] = []
    reusable_groups = {"A", "B"}
    deep_readable_d_reasons = {"content_drift", "invalid_card", "stale"}

    for candidate, decision in zip(candidates, decisions):
        candidate_id = str(candidate.get("candidate_id") or "").strip()
        source_kind = _canonical_source_kind(candidate)
        group = str(decision.get("coverage_group") or "C")
        reason = str(decision.get("coverage_reason") or "")
        raw_card_version_id = decision.get("card_version_id")
        row = rows.get(int(raw_card_version_id)) if (
            isinstance(raw_card_version_id, int)
            and not isinstance(raw_card_version_id, bool)
            and raw_card_version_id > 0
        ) else None
        reused = False
        if group in reusable_groups and row is not None:
            expected_card_hash = str(
                decision.get("card_content_hash")
                or decision.get("content_hash")
                or ""
            )
            try:
                raw_card = json.loads(
                    str(row["card_json"]),
                    object_pairs_hook=_reject_duplicate_card_keys,
                )
                card = validate_semantic_card(raw_card, expected_candidate_id=candidate_id)
                if card.ignored_field_count:
                    raise SemanticIndexContractError("永久材料卡包含未知字段")
                current_hash = semantic_content_hash(str(candidate.get("content") or ""))
                if (
                    str(row["status"]) == "valid"
                    and
                    int(row["analysis_schema_version"]) == SEMANTIC_CARD_SCHEMA_VERSION
                    and str(row["source_kind"]) == source_kind
                    and str(row["candidate_id"]) == candidate_id
                    and str(row["content_hash"]) == expected_card_hash
                    and current_hash == str(row["content_hash"])
                ):
                    permanent_cards.append(_material_card(candidate, card))
                    reused = True
            except (json.JSONDecodeError, SemanticIndexContractError, TypeError, ValueError):
                reused = False
        if reused:
            continue
        if group != "D" or reason in deep_readable_d_reasons:
            deep_candidates.append(candidate)
        else:
            skipped_ids.append(candidate_id)

    deep_result: Mapping[str, Any] = {}
    if deep_candidates:
        deep_result = deep_reader(
            deep_candidates,
            snapshot,
            kind,
            generation_job_id,
        )
        if not isinstance(deep_result, Mapping):
            raise ValueError("按需材料深读结果无效")
    expected_deep = {
        str(candidate.get("candidate_id") or ""): candidate
        for candidate in deep_candidates
    }
    raw_deep_cards = deep_result.get("cards")
    if not isinstance(raw_deep_cards, list):
        raw_deep_cards = []
    deep_id_counts = Counter(
        str(raw.get("candidate_id") or "")
        for raw in raw_deep_cards
        if isinstance(raw, Mapping)
    )
    deep_cards_by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_deep_cards:
        candidate_id = str(raw.get("candidate_id") or "") if isinstance(raw, Mapping) else ""
        candidate = expected_deep.get(candidate_id)
        if candidate is None or deep_id_counts[candidate_id] != 1:
            continue
        try:
            card = validate_semantic_card(raw, expected_candidate_id=candidate_id)
            if not set(raw).issubset(CARD_ALLOWED_FIELDS | MATERIAL_PROVENANCE_FIELDS):
                raise SemanticIndexContractError("按需材料卡包含未知字段")
            expected_hash = semantic_content_hash(str(candidate.get("content") or ""))
            supplied_hash = raw.get("content_hash")
            if not isinstance(supplied_hash, str) or supplied_hash != expected_hash:
                continue
            deep_cards_by_id[candidate_id] = _material_card(candidate, card)
        except (SemanticIndexContractError, TypeError, ValueError):
            continue
    deep_cards = [
        deep_cards_by_id[str(candidate.get("candidate_id") or "")]
        for candidate in deep_candidates
        if str(candidate.get("candidate_id") or "") in deep_cards_by_id
    ]
    analyzed_deep = len(deep_cards)
    private_reused = min(
        analyzed_deep,
        _nonnegative_result_count(deep_result.get("reused_count")),
    )
    all_cards_by_id = {
        str(card["candidate_id"]): card
        for card in [*permanent_cards, *deep_cards]
    }
    all_cards = [
        all_cards_by_id[str(candidate["candidate_id"]).strip()]
        for candidate in candidates
        if str(candidate["candidate_id"]).strip() in all_cards_by_id
    ]
    missing_count = len(candidates) - len(all_cards)
    summary_source = deep_result.get("summary") if deep_result else None
    # Frozen, validated cards are reusable across prompt revisions. Their
    # factual summary is local; optional AI suggestions require an explicit reader.
    if permanent_cards and all_cards and summary_reader is not None:
        summary_source = summary_reader(all_cards, freeze_id=generation_job_id)
        if not isinstance(summary_source, Mapping):
            raise ValueError("材料章节归纳结果无效")
    sources: dict[str, dict[str, Any]] = {}
    for item in candidates:
        source_file = str(item.get("source_file") or "未知来源")
        sources.setdefault(source_file, {"count": 0})["count"] += 1
    key_payload = {
        "schema_version": FROZEN_MATERIAL_SCHEMA_VERSION,
        "card_schema_version": SEMANTIC_CARD_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "snapshot": snapshot,
        "items": [
            {
                "source_kind": decision.get("source_kind"),
                "candidate_id": decision.get("candidate_id"),
                "candidate_content_hash": semantic_content_hash(
                    str(candidate.get("content") or "")
                ),
                "decision_content_hash": decision.get("content_hash"),
                "card_content_hash": decision.get("card_content_hash"),
                "card_version_id": decision.get("card_version_id"),
                "coverage_group": decision.get("coverage_group"),
                "coverage_reason": decision.get("coverage_reason"),
            }
            for candidate, decision in zip(candidates, decisions)
        ],
    }
    cache_key = "f4-" + hashlib.sha256(
        json.dumps(
            key_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": FROZEN_MATERIAL_SCHEMA_VERSION,
        "card_schema_version": SEMANTIC_CARD_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "complete": missing_count == 0,
        "cache_key": cache_key,
        "snapshot": snapshot,
        "total_count": len(candidates),
        "analyzed_count": len(all_cards),
        "batch_count": _nonnegative_result_count(deep_result.get("batch_count")),
        "reused_count": len(permanent_cards) + private_reused,
        "permanent_card_reused_count": len(permanent_cards),
        "legacy_deep_read_count": len(deep_candidates),
        "private_cache_reused_count": private_reused,
        "missing_count": missing_count,
        "skipped_ineligible_ids": skipped_ids,
        "chapters": sorted({str(item.get("chapter") or "未归类") for item in candidates}),
        "years": sorted({int(item["year"]) for item in candidates if item.get("year") is not None}),
        "sources": sources,
        "cards": all_cards,
        "summary": _combined_summary(
            candidates,
            all_cards,
            summary_source,
        ),
    }
