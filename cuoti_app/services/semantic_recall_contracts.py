"""Versioned data contracts and algorithm constants for semantic recall."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping


RECALL_ALGORITHM_VERSION = "semantic-recall-v2"
RECALL_DICTIONARY_VERSION = "math-terms-v1"
RECALL_WEIGHTS_VERSION = "semantic-weights-v2"
RECALL_MODES = frozenset({"off", "shadow", "enforced"})

FIELD_WEIGHTS = {
    "chapter": 2.4,
    "knowledge_points": 2.2,
    "methods": 1.8,
    "semantic_keywords": 1.8,
    "question_type": 1.3,
    "characteristics": 1.1,
    "prerequisites": 0.8,
    "common_traps": 0.7,
    "source_constraints": 0.6,
    "difficulty": 0.5,
}
CARD_TERM_FIELDS = tuple(name for name in FIELD_WEIGHTS if name != "chapter")
DIFFICULTY_NEIGHBORS = {
    "简单": frozenset({"简单", "中等"}),
    "中等": frozenset({"简单", "中等", "困难"}),
    "困难": frozenset({"中等", "困难"}),
    "未知": frozenset({"未知"}),
}


class SemanticRecallError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def required_text(value: Any, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise SemanticRecallError("VALIDATION", f"{field_name} 必须是字符串")
    value = unicodedata.normalize("NFC", value).strip()
    if not value or len(value) > maximum:
        raise SemanticRecallError("VALIDATION", f"{field_name} 为空或过长")
    return value


def optional_text(value: Any, maximum: int) -> str:
    if value is None:
        return ""
    value = unicodedata.normalize("NFC", str(value)).strip()
    if len(value) > maximum:
        raise SemanticRecallError("VALIDATION", "文本过长")
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def timestamp(value: str) -> str:
    value = required_text(value, "timestamp", 128)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SemanticRecallError("VALIDATION", "timestamp 必须是 ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticRecallError("VALIDATION", "timestamp 必须包含时区")
    return value


@dataclass(frozen=True, slots=True)
class RecallRequest:
    subject: str
    chapters: tuple[str, ...]
    question_count: int
    note: str = ""
    question_types: tuple[str, ...] = ()
    difficulty: str = "未知"
    chapter_quotas: Mapping[str, int] = field(default_factory=dict)
    difficulty_quotas: Mapping[str, int] = field(default_factory=dict)
    recent_candidate_ids: frozenset[str] = frozenset()
    recent_similarity_groups: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        subject = required_text(self.subject, "subject", 320)
        chapters = tuple(
            dict.fromkeys(required_text(item, "chapter", 640) for item in self.chapters)
        )
        if not chapters:
            raise SemanticRecallError("VALIDATION", "chapters 不能为空")
        if (
            isinstance(self.question_count, bool)
            or not isinstance(self.question_count, int)
            or self.question_count < 1
        ):
            raise SemanticRecallError("VALIDATION", "question_count 必须是正整数")
        quotas: dict[str, int] = {}
        for chapter, count in dict(self.chapter_quotas).items():
            chapter = required_text(chapter, "chapter_quota", 640)
            if (
                chapter not in chapters
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise SemanticRecallError("VALIDATION", "chapter_quotas 无效")
            quotas[chapter] = count
        difficulty_quotas: dict[str, int] = {}
        for label, count in dict(self.difficulty_quotas).items():
            label = required_text(label, "difficulty_quota", 32)
            if (
                label not in DIFFICULTY_NEIGHBORS
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise SemanticRecallError("VALIDATION", "difficulty_quotas 无效")
            difficulty_quotas[label] = count
        object.__setattr__(self, "subject", subject)
        object.__setattr__(self, "chapters", chapters)
        object.__setattr__(self, "note", optional_text(self.note, 2_000))
        object.__setattr__(
            self,
            "question_types",
            tuple(
                dict.fromkeys(
                    optional_text(item, 160)
                    for item in self.question_types
                    if optional_text(item, 160)
                )
            ),
        )
        object.__setattr__(self, "chapter_quotas", quotas)
        object.__setattr__(self, "difficulty_quotas", difficulty_quotas)
        object.__setattr__(
            self,
            "recent_candidate_ids",
            frozenset(
                str(item).strip()
                for item in self.recent_candidate_ids
                if str(item).strip()
            ),
        )
        object.__setattr__(
            self,
            "recent_similarity_groups",
            frozenset(
                str(item).strip()
                for item in self.recent_similarity_groups
                if str(item).strip()
            ),
        )


@dataclass(frozen=True, slots=True)
class RecallExplanation:
    source_kind: str
    candidate_id: str
    coverage_group: str
    score: float
    matched_terms: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RecallCandidateDecision:
    source_kind: str
    candidate_id: str
    card_version_id: int | None
    content_hash: str | None
    coverage_group: str
    coverage_reason: str
    explanation: RecallExplanation


@dataclass(frozen=True, slots=True)
class RecallResult:
    projection_id: str
    semantic_snapshot_id: str
    source_snapshot: str
    projection_build_hash: str
    candidate_identities: tuple[tuple[str, str], ...]
    candidate_decisions: tuple[RecallCandidateDecision, ...]
    explanations: tuple[RecallExplanation, ...]
    coverage_counts: Mapping[str, int]
    coverage_reason_counts: Mapping[str, int]
    compatibility_counts: Mapping[str, int]
    eligible_hard_pool_count: int
    coverage_ratio: float
    partial_cache: bool
    partial_cache_reasons: tuple[str, ...]
    relaxed_quotas: tuple[str, ...]
    fallback_reason: str | None
    request_hash: str
    result_hash: str
    algorithm_version: str
    dictionary_version: str
    weights_version: str


@dataclass(frozen=True, slots=True)
class ProjectionBuildResult:
    projection_id: str
    snapshot_id: str
    item_count: int
    term_count: int
    build_hash: str
    fts_available: bool
    reused: bool


@dataclass(frozen=True, slots=True)
class ShadowRecallResult:
    effective_candidates: tuple[Mapping[str, Any], ...]
    recall: RecallResult
    shadow_run_id: str
    overlap_count: int
    added_count: int
    dropped_count: int


@dataclass(frozen=True, slots=True)
class SemanticRecallDecision:
    requested_mode: str
    effective_mode: str
    projection_id: str | None
    effective_candidates: tuple[Mapping[str, Any], ...]
    recall: RecallResult | None
    shadow_run_id: str | None
    fallback_category: str | None
    legacy_ids_hash: str
    hard_pool_hash: str
    hard_pool_count: int
    elapsed_ms: float
    effective_candidate_decisions: tuple[RecallCandidateDecision, ...]


__all__ = [
    "CARD_TERM_FIELDS",
    "DIFFICULTY_NEIGHBORS",
    "FIELD_WEIGHTS",
    "ProjectionBuildResult",
    "RECALL_ALGORITHM_VERSION",
    "RECALL_DICTIONARY_VERSION",
    "RECALL_MODES",
    "RECALL_WEIGHTS_VERSION",
    "RecallCandidateDecision",
    "RecallExplanation",
    "RecallRequest",
    "RecallResult",
    "SemanticRecallDecision",
    "SemanticRecallError",
    "ShadowRecallResult",
    "canonical_json",
    "optional_text",
    "required_text",
    "sha256",
    "timestamp",
]
