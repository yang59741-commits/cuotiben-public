"""Versioned local semantic recall and shadow-only comparison.

The service never calls a model.  A published semantic snapshot is projected
into immutable SQLite documents and terms, then a caller-supplied hard pool is
ranked without admitting any outside identity.  Stage E callers keep using the
legacy candidate order; only hashes, counts, and bounded quota labels are
persisted for shadow audit.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

from ..transactions import immediate_transaction
from .ai_budgeting import estimate_text_tokens
from .semantic_index import SemanticIndexContractError, semantic_content_hash
from .semantic_recall_contracts import (
    DIFFICULTY_NEIGHBORS as _DIFFICULTY_NEIGHBORS,
    ProjectionBuildResult,
    RECALL_ALGORITHM_VERSION,
    RECALL_DICTIONARY_VERSION,
    RECALL_WEIGHTS_VERSION,
    RecallCandidateDecision,
    RecallExplanation,
    RecallRequest,
    RecallResult,
    SemanticRecallDecision,
    SemanticRecallError,
    ShadowRecallResult,
    canonical_json as _canonical_json,
    required_text as _required_text,
    sha256 as _sha256,
    timestamp as _timestamp,
)
from .semantic_recall_terms import (
    canonical_concept,
    canonical_concepts,
    normalize as _normalize,
    normalized_terms,
)
from .semantic_recall_serialization import (
    recall_contract_payload as _serialize_recall_contract,
)
from .semantic_recall_projection import (
    build_recall_projection as _build_recall_projection,
    ensure_optional_fts as _projection_ensure_optional_fts,
    latest_published_projection as _latest_published_projection,
    load_projection as _projection_load,
    load_terms as _projection_load_terms,
    migrate_shared_terms as _projection_migrate_shared_terms,
    projection_rows as _projection_source_rows,
    question_states as _projection_question_states,
    source_file_key as _projection_source_file_key,
)


RECALL_MODES = frozenset({"off", "shadow", "enforced"})
RECALL_V3_ALGORITHM_VERSION = "semantic-recall-v3"
RECALL_V3_DICTIONARY_VERSION = "math-concepts-v2"
RECALL_V3_WEIGHTS_VERSION = "semantic-weights-v3"
RECALL_V3_BUDGET_VERSION = "semantic-candidate-budget-v1"
RECALL_V2_BUDGET_VERSION = "semantic-candidate-budget-legacy-v2"
V3_MAX_NOTE_CONCEPTS = 24
_V3_SEMANTIC_FIELD_WEIGHTS = {
    "knowledge_points": 2.2,
    "methods": 1.8,
    "semantic_keywords": 1.8,
    "characteristics": 1.1,
    "prerequisites": 0.8,
    "common_traps": 0.7,
    "source_constraints": 0.6,
}
_HEX = frozenset("0123456789abcdef")


def _source_file_key(value: Any) -> str:
    return _projection_source_file_key(value)


def _candidate_source_identity(candidate: Mapping[str, Any]) -> tuple[str, str | None]:
    explicit = str(candidate.get("source_kind") or "").strip().casefold()
    candidate_id = str(candidate.get("candidate_id") or "").strip().upper()
    if explicit in {"ordinary", "past_exam_local", "past_exam_other"}:
        return explicit, None
    if explicit == "past_exam" or not explicit:
        if candidate_id.startswith("PL"):
            return "past_exam_local", "prefix_inferred"
        if candidate_id.startswith("PE"):
            return "past_exam_other", "prefix_inferred"
        if not explicit:
            return "ordinary", "default_ordinary"
    raise SemanticRecallError("SOURCE_KIND_INVALID", "source_kind 无法确定或不受支持")


def _candidate_source_kind(candidate: Mapping[str, Any]) -> str:
    return _candidate_source_identity(candidate)[0]


def _candidate_content_hash(candidate: Mapping[str, Any]) -> str | None:
    supplied = str(candidate.get("content_hash") or "").strip().casefold()
    if len(supplied) == 64 and all(char in _HEX for char in supplied):
        return supplied
    content = candidate.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        return semantic_content_hash(content)
    except SemanticIndexContractError:
        return None


def _ensure_optional_fts(db: sqlite3.Connection) -> bool:
    return _projection_ensure_optional_fts(db)


def _projection_rows(db: sqlite3.Connection, snapshot_id: str) -> list[sqlite3.Row | tuple]:
    return _projection_source_rows(db, snapshot_id)


def _maybe_migrate_shared_terms(db: sqlite3.Connection, projection_id: str) -> None:
    _projection_migrate_shared_terms(db, projection_id)


def build_recall_projection(
    db: sqlite3.Connection,
    *,
    snapshot_id: str,
    timestamp: str,
    algorithm_version: str = RECALL_ALGORITHM_VERSION,
    dictionary_version: str = RECALL_DICTIONARY_VERSION,
    weights_version: str = RECALL_WEIGHTS_VERSION,
) -> ProjectionBuildResult:
    return _build_recall_projection(
        db,
        snapshot_id=snapshot_id,
        timestamp=timestamp,
        algorithm_version=algorithm_version,
        dictionary_version=dictionary_version,
        weights_version=weights_version,
        ensure_fts=_ensure_optional_fts,
        load_rows=_projection_rows,
        migrate_terms=_maybe_migrate_shared_terms,
    )


def build_v3_recall_projection(
    db: sqlite3.Connection,
    *,
    snapshot_id: str,
    timestamp: str,
) -> ProjectionBuildResult:
    """Build a side-by-side v3 projection without changing the v2 default."""

    return build_recall_projection(
        db,
        snapshot_id=snapshot_id,
        timestamp=timestamp,
        algorithm_version=RECALL_V3_ALGORITHM_VERSION,
        dictionary_version=RECALL_V3_DICTIONARY_VERSION,
        weights_version=RECALL_V3_WEIGHTS_VERSION,
    )


def _load_projection(
    db: sqlite3.Connection,
    projection_id: str,
    identities: Sequence[tuple[str, str]] | None = None,
) -> tuple[sqlite3.Row | tuple, dict[tuple[str, str], dict[str, Any]]]:
    return _projection_load(db, projection_id, identities)


def _load_terms(
    db: sqlite3.Connection,
    projection_id: str,
    question_ids: Sequence[int],
    query_terms: Sequence[str] | None = None,
) -> dict[int, list[tuple[str, str, float]]]:
    return _projection_load_terms(db, projection_id, question_ids, query_terms)


def _question_states(
    db: sqlite3.Connection,
    identities: Sequence[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], tuple[bool, bool]]:
    return _projection_question_states(db, identities)


def _recall_request_hash(
    request: RecallRequest,
    hard_pool: Sequence[Mapping[str, Any]],
    *,
    projection_id: str,
    projection: Sequence[Any],
    budget_version: str,
    result_limit: int,
    source_minimums: Mapping[str, int],
) -> str:
    return _sha256({
        "subject": request.subject,
        "chapters": request.chapters,
        "question_count": request.question_count,
        "note_terms": normalized_terms(request.note),
        "question_types": request.question_types,
        "difficulty": request.difficulty,
        "chapter_quotas": dict(request.chapter_quotas),
        "difficulty_quotas": dict(request.difficulty_quotas),
        "recent_candidate_ids": sorted(request.recent_candidate_ids),
        "recent_similarity_groups": sorted(request.recent_similarity_groups),
        "hard_pool_content_hash": semantic_hard_pool_hash(hard_pool),
        "hard_pool_count": len(hard_pool),
        "projection": {
            "projection_id": projection_id,
            "snapshot_id": str(projection[0]),
            "source_snapshot": str(projection[1]),
            "algorithm_version": str(projection[2]),
            "dictionary_version": str(projection[3]),
            "weights_version": str(projection[4]),
            "build_hash": str(projection[6]),
        },
        "budget_version": budget_version,
        "result_limit": result_limit,
        "source_minimums": dict(sorted(source_minimums.items())),
    })


def recall_contract_payload(result: RecallResult, *, generated_at: str) -> dict[str, Any]:
    return _serialize_recall_contract(result, generated_at=generated_at)


def _bounded_metric(value: Any, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = float(value)
    if not math.isfinite(value):
        return default
    return max(0.0, min(1.0, value))


def calibrated_analysis_confidence(
    reported_confidence: Any,
    *,
    field_completeness: Any,
    repeat_consistency: Any = None,
    manual_label_confidence: Any = None,
) -> float:
    """Calibrate untrusted model confidence with independently observable signals."""

    reported = _bounded_metric(reported_confidence, 0.0) * 0.75
    completeness = _bounded_metric(field_completeness, 0.0)
    consistency = _bounded_metric(repeat_consistency, 0.5)
    manual = _bounded_metric(manual_label_confidence, 0.5)
    return round(
        0.35 * reported + 0.30 * completeness + 0.20 * consistency + 0.15 * manual,
        8,
    )


def _concepts_by_field(
    terms: Sequence[tuple[str, str, float]],
) -> dict[str, frozenset[str]]:
    result: dict[str, set[str]] = {}
    for field_name, term, _weight in terms:
        concept = canonical_concept(term)
        if concept:
            result.setdefault(str(field_name), set()).add(concept)
    return {name: frozenset(values) for name, values in result.items()}


def deterministic_similarity_group(
    values: Mapping[str, Any] | Sequence[tuple[str, str, float]],
) -> str:
    """Derive a stable group from structured semantics, never free-text group labels."""

    if isinstance(values, Mapping):
        fields = {
            name: frozenset(
                concept
                for raw in (
                    values.get(name)
                    if isinstance(values.get(name), (list, tuple, set, frozenset))
                    else (values.get(name),)
                )
                for concept in canonical_concepts(raw)
            )
            for name in ("knowledge_points", "methods", "question_type")
        }
    else:
        projected = _concepts_by_field(values)
        fields = {
            name: projected.get(name, frozenset())
            for name in ("knowledge_points", "methods", "question_type")
        }
    payload = {
        name: tuple(sorted(concepts))[:8]
        for name, concepts in fields.items()
    }
    if not any(payload.values()):
        return ""
    return "semantic-v3-" + _sha256(payload)[:20]


def _weighted_field_jaccard(
    query_concepts: frozenset[str],
    candidate_fields: Mapping[str, frozenset[str]],
) -> float:
    if not query_concepts:
        return 0.0
    weighted_score = 0.0
    available_weight = 0.0
    for field_name, weight in _V3_SEMANTIC_FIELD_WEIGHTS.items():
        concepts = candidate_fields.get(field_name, frozenset())
        if not concepts:
            continue
        union = query_concepts | concepts
        weighted_score += weight * (len(query_concepts & concepts) / len(union))
        available_weight += weight
    return weighted_score / available_weight if available_weight else 0.0


def _v3_score_candidate(
    request: RecallRequest,
    candidate: Mapping[str, Any],
    document: Mapping[str, Any] | None,
    coverage_group: str,
    terms: Sequence[tuple[str, str, float]],
    *,
    similarity_group: str,
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    by_field = _concepts_by_field(terms)
    chapter_query = frozenset(
        concept for value in request.chapters for concept in canonical_concepts(value)
    )
    type_query = frozenset(
        concept for value in request.question_types for concept in canonical_concepts(value)
    )
    note_query = frozenset(
        canonical_concepts(request.note, limit=V3_MAX_NOTE_CONCEPTS)
    )
    chapter_terms = by_field.get("chapter", frozenset())
    type_terms = by_field.get("question_type", frozenset())
    chapter_score = (
        len(chapter_query & chapter_terms) / len(chapter_query | chapter_terms)
        if chapter_query and chapter_terms else 0.0
    )
    type_score = (
        1.0 if not type_query else
        len(type_query & type_terms) / len(type_query | type_terms)
        if type_terms else 0.0
    )
    query_score = _weighted_field_jaccard(note_query, by_field)
    candidate_difficulty = str(
        (document or {}).get("difficulty") or candidate.get("difficulty") or "未知"
    )
    allowed_difficulties = _DIFFICULTY_NEIGHBORS.get(
        request.difficulty, frozenset({request.difficulty})
    )
    difficulty_score = (
        1.0 if candidate_difficulty == request.difficulty
        else 0.55 if candidate_difficulty in allowed_difficulties else 0.0
    )
    completeness = sum(
        bool(by_field.get(name)) for name in _V3_SEMANTIC_FIELD_WEIGHTS
    ) / len(_V3_SEMANTIC_FIELD_WEIGHTS)
    confidence = calibrated_analysis_confidence(
        (document or {}).get("confidence"),
        field_completeness=completeness,
        repeat_consistency=(
            candidate.get("repeat_analysis_consistency")
            if "repeat_analysis_consistency" in candidate
            else candidate.get("analysis_consistency")
        ),
        manual_label_confidence=(
            candidate.get("manual_label_confidence")
            if "manual_label_confidence" in candidate
            else 1.0 if candidate.get("manual_label_verified") is True else None
        ),
    )
    coverage_score = {"A": 1.0, "B": 0.6, "C": 0.2}.get(coverage_group, 0.0)
    exposed_count = max(0, int(candidate.get("exposed_count") or 0))
    selected_count = max(0, int(candidate.get("selected_count") or 0))
    low_usage_score = 1.0 / (1.0 + selected_count + 0.25 * exposed_count)
    score = (
        0.24 * chapter_score + 0.38 * query_score + 0.10 * type_score
        + 0.10 * difficulty_score + 0.05 * confidence + 0.07 * coverage_score
        + 0.06 * low_usage_score
    )
    matched = sorted(
        (chapter_query | type_query | note_query)
        & frozenset(concept for concepts in by_field.values() for concept in concepts)
    )
    reasons = [
        f"章节匹配={chapter_score:.4f}",
        f"字段归一语义匹配={query_score:.4f}",
        f"题型匹配={type_score:.4f}",
        f"校准置信度={confidence:.4f}",
        f"卡覆盖={coverage_group}",
        f"有效note概念={len(note_query)}/{V3_MAX_NOTE_CONCEPTS}",
    ]
    candidate_id = str(candidate.get("candidate_id") or "")
    if candidate_id in request.recent_candidate_ids:
        score -= 0.18
        reasons.append("近期已用惩罚")
    if similarity_group and similarity_group in request.recent_similarity_groups:
        score -= 0.12
        reasons.append("近期相似组惩罚")
    return max(0.0, min(1.0, score)), tuple(matched[:12]), tuple(reasons)


def _score_candidate(
    request: RecallRequest,
    candidate: Mapping[str, Any],
    document: Mapping[str, Any] | None,
    coverage_group: str,
    terms: Sequence[tuple[str, str, float]],
    *,
    algorithm_version: str,
    weights_version: str,
    similarity_group: str = "",
) -> tuple[float, tuple[str, ...], tuple[str, ...]]:
    if (
        algorithm_version == RECALL_V3_ALGORITHM_VERSION
        and weights_version == RECALL_V3_WEIGHTS_VERSION
    ):
        return _v3_score_candidate(
            request,
            candidate,
            document,
            coverage_group,
            terms,
            similarity_group=similarity_group,
        )
    query_terms = set()
    for value in (*request.chapters, *request.question_types, request.note):
        query_terms.update(normalized_terms(value))
    matched = sorted({term for _field, term, _weight in terms if term in query_terms})
    available_weight = sum(weight for _field, term, weight in terms if term in query_terms)
    query_score = min(1.0, available_weight / max(3.0, len(query_terms) * 0.7)) if query_terms else 0.0
    exact_query_terms = {
        _normalize(value)
        for value in (*request.chapters, *request.question_types, request.note)
        if _normalize(value)
    }
    candidate_terms = {term for _field, term, _weight in terms}
    exact_matches = sorted(exact_query_terms & candidate_terms)
    note_term = _normalize(request.note)
    exact_note_match = bool(note_term and note_term in candidate_terms)
    if algorithm_version == "semantic-recall-v2" and weights_version == "semantic-weights-v2" and exact_note_match:
        query_score = max(query_score, 1.0)
    chapter = str((document or candidate).get("chapter") or candidate.get("chapter") or "")
    chapter_score = 1.0 if chapter in request.chapters else 0.0
    question_type = str((document or {}).get("question_type") or candidate.get("question_type") or "")
    type_score = 1.0 if not request.question_types else float(question_type in request.question_types)
    candidate_difficulty = str((document or {}).get("difficulty") or candidate.get("difficulty") or "未知")
    allowed_difficulties = _DIFFICULTY_NEIGHBORS.get(request.difficulty, frozenset({request.difficulty}))
    difficulty_score = 1.0 if candidate_difficulty == request.difficulty else (0.55 if candidate_difficulty in allowed_difficulties else 0.0)
    confidence = float((document or {}).get("confidence") or 0.0)
    coverage_score = {"A": 1.0, "B": 0.6, "C": 0.2}.get(coverage_group, 0.0)
    exposed_count = max(0, int(candidate.get("exposed_count") or 0))
    selected_count = max(0, int(candidate.get("selected_count") or 0))
    low_usage_score = 1.0 / (1.0 + selected_count + 0.25 * exposed_count)
    score = (
        0.24 * chapter_score + 0.34 * query_score + 0.10 * type_score
        + 0.10 * difficulty_score + 0.08 * confidence + 0.08 * coverage_score
        + 0.06 * low_usage_score
    )
    if algorithm_version == "semantic-recall-v2" and weights_version == "semantic-weights-v2" and exact_note_match:
        score = min(1.0, score + 0.12)
    reasons = [
        f"章节匹配={chapter_score:.2f}", f"语义匹配={query_score:.2f}",
        f"卡覆盖={coverage_group}", f"低使用增益={low_usage_score:.2f}",
    ]
    if exact_matches:
        reasons.append("完整语义词匹配")
    if exact_note_match:
        reasons.append("note完整词匹配")
    candidate_id = str(candidate.get("candidate_id") or "")
    similarity_group = similarity_group or str(
        (document or {}).get("similarity_group") or ""
    )
    if candidate_id in request.recent_candidate_ids:
        score -= 0.18
        reasons.append("近期已用惩罚")
    if similarity_group and similarity_group in request.recent_similarity_groups:
        score -= 0.12
        reasons.append("近期相似组惩罚")
    return max(0.0, min(1.0, score)), tuple(matched[:12]), tuple(reasons)


def recall_candidates(
    db: sqlite3.Connection,
    *,
    projection_id: str,
    request: RecallRequest,
    hard_pool: Sequence[Mapping[str, Any]],
    result_limit: int | None = None,
    source_minimums: Mapping[str, int] | None = None,
    budget_version: str | None = None,
) -> RecallResult:
    projection_id = _required_text(projection_id, "projection_id", 256)
    pool_identities = tuple(
        (_candidate_source_kind(item), str(item.get("candidate_id") or "").strip())
        for item in hard_pool
        if isinstance(item, Mapping)
    )
    projection, documents = _load_projection(db, projection_id, pool_identities)
    is_v3 = (
        str(projection[2]) == RECALL_V3_ALGORITHM_VERSION
        and str(projection[4]) == RECALL_V3_WEIGHTS_VERSION
    )
    if request.recent_candidate_ids and not is_v3:
        inferred_groups = {
            str(document.get("similarity_group") or "").strip()
            for document in documents.values()
            if str(document.get("candidate_id") or "") in request.recent_candidate_ids
        }
        inferred_groups.discard("")
        if not inferred_groups <= request.recent_similarity_groups:
            request = replace(
                request,
                recent_similarity_groups=(
                    request.recent_similarity_groups | frozenset(inferred_groups)
                ),
            )
    limit = request.question_count if result_limit is None else result_limit
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise SemanticRecallError("VALIDATION", "result_limit 必须是正整数")
    budget_version = _required_text(
        budget_version or (
            RECALL_V3_BUDGET_VERSION if is_v3 else RECALL_V2_BUDGET_VERSION
        ),
        "budget_version",
        128,
    )
    source_minimums = dict(source_minimums or {})
    for source_kind, count in source_minimums.items():
        if source_kind not in {"ordinary", "past_exam_local", "past_exam_other"}:
            raise SemanticRecallError("VALIDATION", "source_minimums 来源无效")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise SemanticRecallError("VALIDATION", "source_minimums 数量无效")
    if sum(source_minimums.values()) > limit:
        raise SemanticRecallError(
            "HARD_CONSTRAINT_CONFLICT", "来源最低数量超过召回候选上限"
        )
    coverage = {name: 0 for name in "ABCD"}
    coverage_reasons: dict[str, int] = {}
    compatibility_counts: dict[str, int] = {}
    prepared: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    question_states = _question_states(db, pool_identities)
    matching_question_ids: list[int] = []
    for index, raw in enumerate(hard_pool):
        if not isinstance(raw, Mapping):
            raise SemanticRecallError("VALIDATION", "hard_pool 项必须是对象")
        candidate_id = _required_text(raw.get("candidate_id"), "candidate_id", 256)
        source_kind, compatibility = _candidate_source_identity(raw)
        if compatibility:
            compatibility_counts[compatibility] = compatibility_counts.get(compatibility, 0) + 1
        identity = (source_kind, candidate_id)
        document = documents.get(identity)
        group = "C"
        reason = "missing_card"
        candidate_hash = _candidate_content_hash(raw)
        state = question_states.get(identity)
        if state is not None and state[1]:
            group = "D"
            reason = "excluded"
        elif state is not None and not state[0]:
            group = "D"
            reason = "inactive"
        elif identity in seen:
            group = "D"
            reason = "duplicate_identity"
        elif str(raw.get("subject") or raw.get("source_subject") or request.subject).strip() != request.subject:
            group = "D"
            reason = "scope_mismatch"
        elif source_kind == "ordinary" and str(raw.get("chapter") or "").strip() not in request.chapters:
            group = "D"
            reason = "scope_mismatch"
        elif document is not None and candidate_hash is not None and candidate_hash != str(document["content_hash"]):
            group = "D"
            reason = "content_drift"
        elif document is not None and (
            str(document.get("card_status")) != "valid"
            or int(document.get("current_card_version_id") or 0) != int(document["card_version_id"])
        ):
            group = "D"
            reason = "invalid_card"
        elif document is not None and candidate_hash is None:
            group = "B"
            reason = "caller_hash_missing"
        elif document is not None:
            group = "A"
            reason = "valid_current_card"
        seen.add(identity)
        coverage[group] += 1
        coverage_reasons[reason] = coverage_reasons.get(reason, 0) + 1
        item = {"index": index, "raw": raw, "identity": identity, "document": document, "group": group, "reason": reason}
        prepared.append(item)
        if document is not None and group in {"A", "B"}:
            matching_question_ids.append(int(document["semantic_question_id"]))
    requested_terms: set[str] = set()
    for value in (*request.chapters, *request.question_types, request.note):
        requested_terms.update(normalized_terms(value))
    terms_by_question = _load_terms(
        db,
        projection_id,
        matching_question_ids,
        None if is_v3 else tuple(sorted(requested_terms)),
    )
    for item in prepared:
        document = item["document"]
        terms = (
            terms_by_question.get(int(document["semantic_question_id"]), [])
            if document else ()
        )
        item["similarity_group"] = (
            deterministic_similarity_group(terms)
            if is_v3
            else str((document or {}).get("similarity_group") or "")
        )
    if request.recent_candidate_ids and is_v3:
        inferred_groups = {
            str(item["similarity_group"])
            for item in prepared
            if item["identity"][1] in request.recent_candidate_ids
            and item["similarity_group"]
        }
        if not inferred_groups <= request.recent_similarity_groups:
            request = replace(
                request,
                recent_similarity_groups=(
                    request.recent_similarity_groups | frozenset(inferred_groups)
                ),
            )
    ranked: list[dict[str, Any]] = []
    for item in prepared:
        if item["group"] == "D":
            continue
        document = item["document"]
        terms = terms_by_question.get(int(document["semantic_question_id"]), []) if document else ()
        score, matched, reasons = _score_candidate(
            request, item["raw"], document, item["group"], terms,
            algorithm_version=str(projection[2]), weights_version=str(projection[4]),
            similarity_group=str(item["similarity_group"]),
        )
        ranked.append({**item, "score": score, "matched": matched, "reasons": reasons})
    ranked.sort(key=lambda item: (-item["score"], item["identity"][0], item["identity"][1], item["index"]))

    selected: list[dict[str, Any]] = []
    relaxed: list[str] = []
    remaining = list(ranked)
    similarity_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    year_counts: dict[int, int] = {}
    chapter_counts: dict[str, int] = {}
    type_counts: dict[str, int] = {}
    difficulty_counts: dict[str, int] = {}
    selected_source_counts: dict[str, int] = {}
    quotas = dict(request.chapter_quotas)
    requested_types = set(request.question_types)
    available_types = {
        str((item["document"] or item["raw"]).get("question_type") or "")
        for item in ranked
    }
    enforce_types = bool(requested_types) and requested_types <= available_types and len(requested_types) <= limit
    unmet_types = set(requested_types) if enforce_types else set()
    if requested_types and not enforce_types:
        relaxed.append("question_type")
    difficulty_quotas = dict(request.difficulty_quotas)
    enforce_difficulty = bool(difficulty_quotas)
    while remaining and len(selected) < min(limit, len(ranked)):
        eligible = []
        for item in remaining:
            source_kind = item["identity"][0]
            unmet_source_count = sum(
                max(0, required - selected_source_counts.get(kind, 0))
                for kind, required in source_minimums.items()
            )
            remaining_slots = min(limit, len(ranked)) - len(selected)
            if (
                remaining_slots <= unmet_source_count
                and selected_source_counts.get(source_kind, 0)
                >= source_minimums.get(source_kind, 0)
            ):
                continue
            document = item["document"] or item["raw"]
            chapter = str(document.get("chapter") or item["raw"].get("chapter") or "")
            quota = quotas.get(chapter)
            if quota is not None and chapter_counts.get(chapter, 0) >= quota:
                continue
            question_type = str(document.get("question_type") or item["raw"].get("question_type") or "")
            if enforce_types and question_type not in requested_types:
                continue
            if enforce_types and unmet_types and question_type not in unmet_types:
                continue
            candidate_difficulty = str(document.get("difficulty") or item["raw"].get("difficulty") or "未知")
            if enforce_difficulty and difficulty_counts.get(candidate_difficulty, 0) >= difficulty_quotas.get(candidate_difficulty, 0):
                continue
            group = str(item.get("similarity_group") or "")
            source_key = str(document.get("source_file_key") or _source_file_key(item["raw"].get("source_file")))
            source_year = document.get("source_year") or item["raw"].get("source_year")
            diversity_penalty = min(0.24, 0.14 * similarity_counts.get(group, 0)) if group else 0.0
            diversity_penalty += min(0.12, 0.04 * source_counts.get(source_key, 0)) if source_key else 0.0
            if isinstance(source_year, int) and not isinstance(source_year, bool):
                diversity_penalty += min(0.10, 0.03 * year_counts.get(source_year, 0))
            type_bonus = 0.08 if question_type in requested_types and type_counts.get(question_type, 0) == 0 else 0.0
            difficulty_bonus = 0.06 if difficulty_quotas.get(candidate_difficulty, 0) > difficulty_counts.get(candidate_difficulty, 0) else 0.0
            final_score = item["score"] + type_bonus + difficulty_bonus - diversity_penalty
            eligible.append(
                (
                    final_score,
                    item,
                    type_bonus,
                    difficulty_bonus,
                    diversity_penalty,
                )
            )
        if not eligible:
            if enforce_difficulty:
                enforce_difficulty = False
                relaxed.append("difficulty_quota")
                continue
            if enforce_types:
                enforce_types = False
                relaxed.append("question_type")
                continue
            if quotas:
                quotas = {}
                relaxed.append("chapter_quota_redistribution")
                continue
            break
        eligible.sort(key=lambda pair: (-pair[0], pair[1]["identity"][0], pair[1]["identity"][1], pair[1]["index"]))
        chosen_score, chosen, type_bonus, quota_bonus, diversity_penalty = eligible[0]
        chosen["final_score"] = chosen_score
        chosen["reasons"] = chosen["reasons"] + (
            f"最终分={chosen_score:.8f}",
            f"题型增益={type_bonus:.8f}",
            f"配额增益={quota_bonus:.8f}",
            f"多样性惩罚={diversity_penalty:.8f}",
        )
        selected.append(chosen)
        remaining.remove(chosen)
        selected_source = chosen["identity"][0]
        selected_source_counts[selected_source] = (
            selected_source_counts.get(selected_source, 0) + 1
        )
        document = chosen["document"] or chosen["raw"]
        chapter = str(document.get("chapter") or chosen["raw"].get("chapter") or "")
        group = str(chosen.get("similarity_group") or "")
        source_key = str(document.get("source_file_key") or _source_file_key(chosen["raw"].get("source_file")))
        source_year = document.get("source_year") or chosen["raw"].get("source_year")
        question_type = str(document.get("question_type") or chosen["raw"].get("question_type") or "")
        candidate_difficulty = str(document.get("difficulty") or chosen["raw"].get("difficulty") or "未知")
        chapter_counts[chapter] = chapter_counts.get(chapter, 0) + 1
        if question_type:
            type_counts[question_type] = type_counts.get(question_type, 0) + 1
            unmet_types.discard(question_type)
        difficulty_counts[candidate_difficulty] = difficulty_counts.get(candidate_difficulty, 0) + 1
        if group:
            similarity_counts[group] = similarity_counts.get(group, 0) + 1
        if source_key:
            source_counts[source_key] = source_counts.get(source_key, 0) + 1
        if isinstance(source_year, int) and not isinstance(source_year, bool):
            year_counts[source_year] = year_counts.get(source_year, 0) + 1
    explanations = tuple(
        RecallExplanation(
            item["identity"][0],
            item["identity"][1],
            item["group"],
            round(item["final_score"], 8),
            item["matched"],
            item["reasons"],
        )
        for item in selected
    )
    decisions = tuple(
        RecallCandidateDecision(
            item["identity"][0], item["identity"][1],
            int(item["document"]["card_version_id"]) if item["document"] else None,
            str(item["document"]["content_hash"]) if item["document"] else _candidate_content_hash(item["raw"]),
            item["group"], item["reason"], explanations[index],
        )
        for index, item in enumerate(selected)
    )
    partial_reasons = tuple(
        reason for reason in ("caller_hash_missing", "missing_card", "content_drift", "invalid_card")
        if coverage_reasons.get(reason, 0) > 0
    )
    eligible_count = sum(
        count for reason, count in coverage_reasons.items()
        if reason not in {"excluded", "inactive", "duplicate_identity", "scope_mismatch"}
    )
    covered_count = coverage["A"] + coverage["B"]
    request_hash = _recall_request_hash(
        request,
        hard_pool,
        projection_id=projection_id,
        projection=projection,
        budget_version=budget_version,
        result_limit=limit,
        source_minimums=source_minimums,
    )
    result_hash = _sha256({
        "projection_id": projection_id,
        "request_hash": request_hash,
        "candidate_identities": tuple(item["identity"] for item in selected),
        "coverage_counts": coverage,
        "coverage_reason_counts": coverage_reasons,
        "source_minimums": source_minimums,
        "relaxed_quotas": relaxed,
        "final_scores": tuple(
            (item["identity"], round(item["final_score"], 8))
            for item in selected
        ),
    })
    if any(
        selected_source_counts.get(source_kind, 0) < required
        for source_kind, required in source_minimums.items()
    ):
        raise SemanticRecallError(
            "HARD_CONSTRAINT_CONFLICT", "召回结果无法满足来源最低数量"
        )
    if len(selected) < request.question_count:
        raise SemanticRecallError(
            "INSUFFICIENT_CANDIDATES",
            f"硬过滤范围内候选不足：需要 {request.question_count}，当前 {len(selected)}",
        )
    return RecallResult(
        projection_id=projection_id,
        semantic_snapshot_id=str(projection[0]),
        source_snapshot=str(projection[1]),
        projection_build_hash=str(projection[6]),
        candidate_identities=tuple(item["identity"] for item in selected),
        candidate_decisions=decisions,
        explanations=explanations,
        coverage_counts=coverage,
        coverage_reason_counts=dict(sorted(coverage_reasons.items())),
        compatibility_counts=dict(sorted(compatibility_counts.items())),
        eligible_hard_pool_count=eligible_count,
        coverage_ratio=round(covered_count / eligible_count, 8) if eligible_count else 1.0,
        partial_cache=bool(partial_reasons),
        partial_cache_reasons=partial_reasons,
        relaxed_quotas=tuple(relaxed),
        fallback_reason=None,
        request_hash=request_hash,
        result_hash=result_hash,
        algorithm_version=str(projection[2]),
        dictionary_version=str(projection[3]),
        weights_version=str(projection[4]),
    )


def _candidate_token_estimate(candidate: Mapping[str, Any]) -> int:
    raw = candidate.get("token_estimate")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return estimate_text_tokens(candidate.get("content")) or 1


def _candidate_byte_estimate(candidate: Mapping[str, Any]) -> int:
    return len(str(candidate.get("content") or "").encode("utf-8"))


def _legacy_identity_hash(candidates: Sequence[Mapping[str, Any]]) -> str:
    return _sha256(
        tuple(
            (_candidate_source_kind(item), str(item.get("candidate_id") or "").strip())
            for item in candidates
        )
    )


def semantic_candidate_identity(candidate: Mapping[str, Any]) -> tuple[str, str]:
    return _candidate_source_kind(candidate), _required_text(
        candidate.get("candidate_id"), "candidate_id", 256
    )


def semantic_candidate_content_hash(candidate: Mapping[str, Any]) -> str | None:
    return _candidate_content_hash(candidate)


def semantic_hard_pool_hash(candidates: Sequence[Mapping[str, Any]]) -> str:
    return _sha256([
        {
            "source_kind": _candidate_source_kind(item),
            "candidate_id": str(item.get("candidate_id") or "").strip(),
            "content_hash": _candidate_content_hash(item),
        }
        for item in candidates
    ])


def _effective_candidate_decisions(
    db: sqlite3.Connection,
    *,
    projection_id: str | None,
    candidates: Sequence[Mapping[str, Any]],
    recall: RecallResult | None,
) -> tuple[RecallCandidateDecision, ...]:
    recalled = {
        (item.source_kind, item.candidate_id): item
        for item in (recall.candidate_decisions if recall is not None else ())
    }
    identities = tuple(semantic_candidate_identity(item) for item in candidates)
    documents: dict[tuple[str, str], dict[str, Any]] = {}
    if projection_id is not None:
        try:
            _projection, documents = _load_projection(db, projection_id, identities)
        except (SemanticRecallError, sqlite3.DatabaseError):
            documents = {}
    decisions: list[RecallCandidateDecision] = []
    for candidate, identity in zip(candidates, identities):
        existing = recalled.get(identity)
        if existing is not None:
            decisions.append(existing)
            continue
        document = documents.get(identity)
        candidate_hash = _candidate_content_hash(candidate)
        if document is not None and bool(document.get("excluded")):
            group, card_version_id, content_hash, reason = (
                "D", int(document["card_version_id"]),
                str(document["content_hash"]), "excluded",
            )
        elif document is not None and not bool(document.get("active")):
            group, card_version_id, content_hash, reason = (
                "D", int(document["card_version_id"]),
                str(document["content_hash"]), "inactive",
            )
        elif document is not None and (
            str(document.get("card_status")) != "valid"
            or int(document.get("current_card_version_id") or 0)
            != int(document["card_version_id"])
        ):
            group, card_version_id, content_hash, reason = (
                "D", int(document["card_version_id"]),
                str(document["content_hash"]), "invalid_card",
            )
        elif document is None:
            group, card_version_id, content_hash, reason = (
                "C", None, candidate_hash, "missing_card"
            )
        elif candidate_hash is not None and candidate_hash != str(document["content_hash"]):
            group, card_version_id, content_hash, reason = (
                "D", int(document["card_version_id"]),
                str(document["content_hash"]), "content_drift",
            )
        else:
            group = "A" if candidate_hash is not None else "B"
            card_version_id = int(document["card_version_id"])
            content_hash = str(document["content_hash"])
            reason = "valid_current_card" if group == "A" else "caller_hash_missing"
        explanation = RecallExplanation(
            identity[0], identity[1], group, 0.0, (), (reason, "effective_candidate"),
        )
        decisions.append(RecallCandidateDecision(
            identity[0], identity[1], card_version_id, content_hash, group, reason,
            explanation,
        ))
    return tuple(decisions)


def _budgeted_recalled_candidates(
    recalled: Sequence[Mapping[str, Any]],
    *,
    target_count: int,
    minimum_count: int,
    token_budget: int | None,
    byte_budget: int | None,
    required_source_counts: Mapping[str, int],
) -> tuple[Mapping[str, Any], ...]:
    required_identities: set[tuple[str, str]] = set()
    for source_kind, required in required_source_counts.items():
        source_candidates = [
            candidate
            for candidate in recalled
            if _candidate_source_kind(candidate) == source_kind
        ]
        if len(source_candidates) < required:
            raise SemanticRecallError(
                "HARD_CONSTRAINT_CONFLICT",
                "召回候选无法满足冻结来源数量约束",
            )
        required_identities.update(
            (_candidate_source_kind(candidate), str(candidate.get("candidate_id") or ""))
            for candidate in source_candidates[:required]
        )
    ordered = list(recalled)
    selected: list[Mapping[str, Any]] = []
    used_tokens = 0
    used_bytes = 0
    for candidate in ordered:
        if len(selected) >= target_count:
            break
        identity = (
            _candidate_source_kind(candidate),
            str(candidate.get("candidate_id") or ""),
        )
        remaining_slots = target_count - len(selected)
        remaining_required = sum(
            1
            for required_identity in required_identities
            if required_identity not in {
                (
                    _candidate_source_kind(item),
                    str(item.get("candidate_id") or ""),
                )
                for item in selected
            }
        )
        if identity not in required_identities and remaining_slots <= remaining_required:
            continue
        token_estimate = _candidate_token_estimate(candidate)
        byte_estimate = _candidate_byte_estimate(candidate)
        if token_budget is not None and selected and used_tokens + token_estimate > token_budget:
            continue
        if byte_budget is not None and selected and used_bytes + byte_estimate > byte_budget:
            continue
        selected.append(candidate)
        used_tokens += token_estimate
        used_bytes += byte_estimate
    if len(selected) < minimum_count:
        raise SemanticRecallError(
            "CANDIDATE_BUDGET_INSUFFICIENT",
            f"召回候选在输入预算内不足：至少需要 {minimum_count}，当前 {len(selected)}",
        )
    source_counts: dict[str, int] = {}
    for candidate in selected:
        source_kind = _candidate_source_kind(candidate)
        source_counts[source_kind] = source_counts.get(source_kind, 0) + 1
    for source_kind, required in required_source_counts.items():
        if source_counts.get(source_kind, 0) < required:
            raise SemanticRecallError(
                "HARD_CONSTRAINT_CONFLICT",
                "召回候选无法满足冻结来源数量约束",
            )
    return tuple(selected)


def decide_semantic_recall(
    db: sqlite3.Connection,
    *,
    mode: str,
    projection_id: str | None,
    request: RecallRequest,
    hard_pool_loader,
    legacy_candidates: Sequence[Mapping[str, Any]],
    timestamp: str,
    token_budget: int | None = None,
    byte_budget: int | None = None,
    minimum_candidates: int | None = None,
    required_source_counts: Mapping[str, int] | None = None,
    projection_fallback_category: str | None = None,
    candidate_multiplier: int | None = None,
    allow_partial_cache: bool | None = None,
    budget_version: str | None = None,
) -> SemanticRecallDecision:
    """Compute provider-free recall and explicitly choose the effective pool."""

    started = time.perf_counter()
    requested_mode = str(mode or "").strip().casefold()
    if requested_mode not in RECALL_MODES:
        raise SemanticRecallError("RECALL_MODE_INVALID", "语义召回模式无效")
    legacy = tuple(legacy_candidates)
    legacy_hash = _legacy_identity_hash(legacy)
    if candidate_multiplier is not None and (
        isinstance(candidate_multiplier, bool)
        or not isinstance(candidate_multiplier, int)
        or not 1 <= candidate_multiplier <= 10
    ):
        raise SemanticRecallError("VALIDATION", "candidate_multiplier 必须在 1 到 10 之间")

    def result(
        *,
        effective_mode: str,
        candidates: Sequence[Mapping[str, Any]],
        recall: RecallResult | None = None,
        shadow_run_id: str | None = None,
        fallback_category: str | None = None,
        hard_pool_count: int = 0,
        hard_pool_hash: str | None = None,
    ) -> SemanticRecallDecision:
        effective = tuple(candidates)
        return SemanticRecallDecision(
            requested_mode=requested_mode,
            effective_mode=effective_mode,
            projection_id=projection_id,
            effective_candidates=effective,
            recall=recall,
            shadow_run_id=shadow_run_id,
            fallback_category=fallback_category,
            legacy_ids_hash=legacy_hash,
            hard_pool_hash=hard_pool_hash or legacy_hash,
            hard_pool_count=hard_pool_count,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            effective_candidate_decisions=_effective_candidate_decisions(
                db,
                projection_id=projection_id,
                candidates=effective,
                recall=recall,
            ),
        )

    if requested_mode == "off":
        return result(effective_mode="off", candidates=legacy)
    if projection_id is None:
        return result(
            effective_mode="off",
            candidates=legacy,
            fallback_category=(
                projection_fallback_category or "projection_unavailable"
            ),
        )

    try:
        hard_pool = tuple(hard_pool_loader())
    except Exception as exc:
        if requested_mode == "shadow":
            return result(
                effective_mode="off",
                candidates=legacy,
                fallback_category="hard_pool_read_error",
            )
        raise SemanticRecallError(
            "HARD_POOL_READ_ERROR", "无法读取冻结的完整硬过滤候选池"
        ) from exc

    try:
        if requested_mode == "shadow":
            shadow = shadow_recall(
                db,
                projection_id=projection_id,
                request=request,
                hard_pool=hard_pool,
                legacy_candidates=legacy,
                timestamp=timestamp,
                result_limit=(
                    min(len(legacy), request.question_count * candidate_multiplier)
                    if candidate_multiplier is not None
                    else None
                ),
                budget_version=budget_version,
            )
            return result(
                effective_mode="shadow",
                candidates=legacy,
                recall=shadow.recall,
                shadow_run_id=shadow.shadow_run_id,
                hard_pool_count=len(hard_pool),
                hard_pool_hash=semantic_hard_pool_hash(hard_pool),
            )

        target_count = (
            len(legacy)
            if candidate_multiplier is None
            else min(len(legacy), max(request.question_count, request.question_count * candidate_multiplier))
        )
        if target_count < request.question_count:
            raise SemanticRecallError(
                "HARD_CONSTRAINT_CONFLICT", "legacy 冻结候选少于请求题量"
            )
        recall = recall_candidates(
            db,
            projection_id=projection_id,
            request=request,
            hard_pool=hard_pool,
            result_limit=max(1, target_count),
            source_minimums=required_source_counts,
            budget_version=budget_version,
        )
        conflict_reasons = {
            "duplicate_identity",
            "scope_mismatch",
        }
        if recall.coverage_reason_counts.get("content_drift", 0):
            raise SemanticRecallError(
                "SNAPSHOT_DRIFT", "硬过滤池内容与冻结语义 snapshot 不一致"
            )
        if recall.coverage_reason_counts.get("invalid_card", 0):
            raise SemanticRecallError(
                "FROZEN_DATA_CORRUPT", "冻结材料卡状态或版本损坏"
            )
        if any(recall.coverage_reason_counts.get(name, 0) for name in conflict_reasons):
            raise SemanticRecallError(
                "HARD_CONSTRAINT_CONFLICT", "完整硬过滤池包含不允许的身份或范围"
            )
        if allow_partial_cache is False and recall.partial_cache:
            raise SemanticRecallError(
                "PARTIAL_CACHE_NOT_ALLOWED",
                "当前语义 snapshot 不是完整覆盖，已拒绝 enforced 召回",
            )
        by_identity = {
            (_candidate_source_kind(item), str(item.get("candidate_id") or "").strip()): item
            for item in hard_pool
        }
        recalled = tuple(by_identity[identity] for identity in recall.candidate_identities)
        minimum = (
            max(request.question_count * 4, len(request.chapters))
            if candidate_multiplier is None and minimum_candidates is None
            else max(request.question_count, request.question_count * candidate_multiplier)
            if minimum_candidates is None
            else minimum_candidates
        )
        minimum = min(target_count, max(request.question_count, int(minimum)))
        effective = _budgeted_recalled_candidates(
            recalled,
            target_count=target_count,
            minimum_count=minimum,
            token_budget=token_budget,
            byte_budget=byte_budget,
            required_source_counts=dict(required_source_counts or {}),
        )
        return result(
            effective_mode="enforced",
            candidates=effective,
            recall=recall,
            hard_pool_count=len(hard_pool),
            hard_pool_hash=semantic_hard_pool_hash(hard_pool),
        )
    except SemanticRecallError as exc:
        if requested_mode == "shadow":
            return result(
                effective_mode="off",
                candidates=legacy,
                fallback_category=exc.code.casefold(),
                hard_pool_count=len(hard_pool),
                hard_pool_hash=semantic_hard_pool_hash(hard_pool),
            )
        if exc.code in {"PROJECTION_NOT_FOUND"}:
            return result(
                effective_mode="off",
                candidates=legacy,
                fallback_category="projection_read_error",
                hard_pool_count=len(hard_pool),
                hard_pool_hash=semantic_hard_pool_hash(hard_pool),
            )
        raise
    except sqlite3.DatabaseError:
        return result(
            effective_mode="off",
            candidates=legacy,
            fallback_category="projection_read_error",
            hard_pool_count=len(hard_pool),
            hard_pool_hash=semantic_hard_pool_hash(hard_pool),
        )


def shadow_recall(
    db: sqlite3.Connection,
    *,
    projection_id: str,
    request: RecallRequest,
    hard_pool: Sequence[Mapping[str, Any]],
    legacy_candidates: Sequence[Mapping[str, Any]],
    timestamp: str,
    result_limit: int | None = None,
    budget_version: str | None = None,
) -> ShadowRecallResult:
    timestamp = _timestamp(timestamp)
    legacy = tuple(legacy_candidates)
    recall = recall_candidates(
        db,
        projection_id=projection_id,
        request=request,
        hard_pool=hard_pool,
        result_limit=max(1, result_limit if result_limit is not None else len(legacy)),
        budget_version=budget_version,
    )
    legacy_identities = tuple((_candidate_source_kind(item), str(item.get("candidate_id") or "").strip()) for item in legacy)
    recalled = recall.candidate_identities
    legacy_set = set(legacy_identities)
    recalled_set = set(recalled)
    overlap = len(legacy_set & recalled_set)
    added = len(recalled_set - legacy_set)
    dropped = len(legacy_set - recalled_set)
    request_hash = recall.request_hash
    shadow_run_id = "shadow-" + uuid.uuid4().hex
    with immediate_transaction(db):
        db.execute(
            "INSERT INTO semantic_recall_shadow_runs("
            "id,request_hash,source_snapshot,projection_id,algorithm_version,"
            "dictionary_version,weights_version,hard_pool_count,a_count,b_count,c_count,"
            "d_count,legacy_ids_hash,recalled_ids_hash,overlap_count,added_count,"
            "dropped_count,partial_cache,relaxed_quotas_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                shadow_run_id, request_hash, recall.source_snapshot, recall.projection_id,
                recall.algorithm_version, recall.dictionary_version, recall.weights_version,
                len(hard_pool), recall.coverage_counts["A"], recall.coverage_counts["B"],
                recall.coverage_counts["C"], recall.coverage_counts["D"],
                _sha256(legacy_identities), _sha256(recalled), overlap, added, dropped,
                int(recall.partial_cache), _canonical_json(recall.relaxed_quotas), timestamp,
            ),
        )
    return ShadowRecallResult(legacy, recall, shadow_run_id, overlap, added, dropped)


def latest_published_projection(
    db: sqlite3.Connection, *, algorithm_version: str | None = None
) -> str | None:
    return _latest_published_projection(db, algorithm_version=algorithm_version)


def validate_frozen_recall_contract(
    db: sqlite3.Connection,
    freeze_payload: Mapping[str, Any],
) -> None:
    """Reject missing or changed projection/card rows without consulting latest."""

    projection_id = str(freeze_payload.get("projection_id") or "")
    if projection_id == "unavailable":
        return
    projection = db.execute(
        "SELECT p.snapshot_id,p.build_hash,p.algorithm_version,p.dictionary_version,"
        "p.weights_version FROM semantic_recall_projection_versions p WHERE p.id=?",
        (projection_id,),
    ).fetchone()
    if projection is None:
        raise SemanticRecallError("FROZEN_DATA_CORRUPT", "冻结 projection 行不存在")
    expected = (
        str(freeze_payload.get("semantic_snapshot_id") or ""),
        str(freeze_payload.get("projection_build_hash") or ""),
        str(freeze_payload.get("recall_algorithm_version") or ""),
        str(freeze_payload.get("dictionary_version") or ""),
        str(freeze_payload.get("weights_version") or ""),
    )
    if tuple(str(item) for item in projection) != expected:
        raise SemanticRecallError("FROZEN_DATA_CORRUPT", "冻结 projection 元数据不一致")
    decisions = freeze_payload.get("candidate_decisions")
    if not isinstance(decisions, list):
        raise SemanticRecallError("FROZEN_DATA_CORRUPT", "冻结候选决策缺失")
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise SemanticRecallError("FROZEN_DATA_CORRUPT", "冻结候选决策损坏")
        card_version_id = decision.get("card_version_id")
        if card_version_id is None:
            continue
        row = db.execute(
            "SELECT d.card_version_id,d.content_hash,c.content_hash "
            "FROM semantic_recall_documents d "
            "JOIN semantic_card_versions c ON c.id=d.card_version_id "
            "WHERE d.projection_id=? AND d.source_kind=? AND d.candidate_id=?",
            (
                projection_id,
                decision.get("source_kind"),
                decision.get("candidate_id"),
            ),
        ).fetchone()
        expected_card_hash = decision.get("card_content_hash")
        if expected_card_hash is None:
            expected_card_hash = decision.get("content_hash")
        if row is None or (
            int(row[0]) != int(card_version_id)
            or str(row[1]) != str(expected_card_hash)
            or str(row[2]) != str(expected_card_hash)
        ):
            raise SemanticRecallError("FROZEN_DATA_CORRUPT", "冻结 card version 不存在或损坏")
