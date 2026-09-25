"""Pure Stage 4 contracts for strict projection preflight and candidate freezing.

The module deliberately has no database, worker or provider integration.  A
caller must supply both the frozen candidate rows and the independently read
current identities; any missing identity or drift fails closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .past_exam_profile_v3 import ProfileV3ContractError, normalize_question_type_family


CANDIDATE_FACTS_CONTRACT_VERSION = "past-v3-stage4-candidate-facts-v1"
PROJECTION_IDENTITY_VERSION = "past-v3-stage4-projection-identity-v1"
DIFFICULTY_FACT_VERSION = "past-v3-stage4-difficulty-fact-v1"
DIFFICULTY_THRESHOLD = 0.6
DIFFICULTY_LABELS = frozenset({"简单", "中等", "困难"})
SUPPORTED_SOURCE_KINDS = frozenset({"ordinary", "past_exam_other", "past_exam_local"})

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROJECTION_FIELDS = (
    "identity_version",
    "projection_id",
    "semantic_snapshot_id",
    "source_snapshot",
    "algorithm_version",
    "dictionary_version",
    "weights_version",
    "projection_build_hash",
    "item_count",
)
_CANDIDATE_REQUIRED_FIELDS = frozenset(
    {
        "source_kind",
        "candidate_id",
        "subject",
        "question_type_family",
        "content_hash",
        "card_version_id",
        "card_hash",
        "projection_document_hash",
        "content_snapshot",
        "difficulty",
        "difficulty_confidence",
    }
)
_CANDIDATE_ALLOWED_FIELDS = _CANDIDATE_REQUIRED_FIELDS | frozenset(
    {"chapter", "source_exam_id", "section_id", "source_institution"}
)
_LIVE_STATE_FIELDS = frozenset(
    {"content_hash", "card_version_id", "card_hash", "projection_document_hash"}
)


class Stage4CandidateFactsError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.retryable = False


@dataclass(frozen=True, slots=True)
class DifficultyFact:
    resolved: bool
    label: str | None
    confidence: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": DIFFICULTY_FACT_VERSION,
            "resolved": self.resolved,
            "label": self.label,
            "confidence": self.confidence,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class FrozenCandidateFacts:
    contract_version: str
    projection_identity_hash: str
    candidate_facts_hash: str
    target_institution: str
    candidates: tuple[Mapping[str, Any], ...]
    unresolved_candidate_ids: tuple[str, ...]
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved",
            "candidate_fact_not_canonical_json",
            "冻结候选事实不是可规范化的 JSON",
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def build_projection_document_hash(value: Mapping[str, Any]) -> str:
    """Hash the exact immutable semantic-recall document projection."""

    fields = (
        "projection_id", "semantic_question_id", "card_version_id", "source_kind",
        "candidate_id", "subject", "chapter", "question_type", "difficulty",
        "similarity_group", "source_year", "source_file_key", "confidence",
        "content_hash", "normalized_text_hash",
    )
    if not isinstance(value, Mapping) or any(field not in value for field in fields):
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved",
            "projection_document_identity_incomplete",
            "语义投影文档身份不完整",
        )
    return _digest({field: value[field] for field in fields})


def _text(value: object, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str):
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved", f"{field}_missing", "冻结候选事实字段缺失"
        )
    rendered = unicodedata.normalize("NFC", value).strip()
    if not rendered or len(rendered) > maximum:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved", f"{field}_invalid", "冻结候选事实字段无效"
        )
    return rendered


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved", f"{field}_invalid", "冻结候选事实哈希无效"
        )
    return value


def evaluate_difficulty_fact(label: object, confidence: object) -> DifficultyFact:
    """Apply the only authoritative Stage 4 difficulty availability rule."""

    if label is None or (isinstance(label, str) and not label.strip()):
        return DifficultyFact(False, None, None, "difficulty_label_missing")
    if not isinstance(label, str):
        return DifficultyFact(False, None, None, "difficulty_label_invalid")
    normalized = unicodedata.normalize("NFC", label).strip()
    if normalized not in DIFFICULTY_LABELS:
        return DifficultyFact(False, None, None, "difficulty_label_invalid")
    if confidence is None:
        return DifficultyFact(False, None, None, "difficulty_confidence_missing")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return DifficultyFact(False, None, None, "difficulty_confidence_invalid")
    numeric = float(confidence)
    if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        return DifficultyFact(False, None, None, "difficulty_confidence_invalid")
    if numeric < DIFFICULTY_THRESHOLD:
        return DifficultyFact(False, None, numeric, "difficulty_confidence_below_threshold")
    return DifficultyFact(True, normalized, numeric, "difficulty_resolved")


def validate_projection_identity(
    projection: Mapping[str, Any], *, expected: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """Validate the full frozen identity; there is intentionally no fallback."""

    if not isinstance(projection, Mapping) or not isinstance(expected, Mapping):
        raise Stage4CandidateFactsError(
            "semantic_projection_unavailable",
            "projection_identity_missing",
            "正式真题组卷缺少冻结语义投影",
        )
    if set(projection) != set(_PROJECTION_FIELDS) or set(expected) != set(_PROJECTION_FIELDS):
        raise Stage4CandidateFactsError(
            "semantic_projection_unavailable",
            "projection_identity_incomplete",
            "冻结语义投影身份字段不完整",
        )
    normalized: dict[str, Any] = {}
    for field in _PROJECTION_FIELDS:
        if field == "item_count":
            value = projection.get(field)
            expected_value = expected.get(field)
            if type(value) is not int or value < 0 or type(expected_value) is not int or expected_value < 0:
                raise Stage4CandidateFactsError(
                    "semantic_projection_unavailable",
                    "projection_item_count_invalid",
                    "冻结语义投影条目计数无效",
                )
            if value != expected_value:
                raise Stage4CandidateFactsError(
                    "semantic_projection_unavailable",
                    "projection_item_count_drift",
                    "冻结语义投影条目计数与当前预检不一致",
                )
            normalized[field] = value
            continue
        value = _text(projection.get(field), f"projection_{field}")
        expected_value = _text(expected.get(field), f"expected_projection_{field}")
        if field == "identity_version" and value != PROJECTION_IDENTITY_VERSION:
            raise Stage4CandidateFactsError(
                "semantic_projection_unavailable",
                "projection_identity_version_unsupported",
                "冻结语义投影身份版本不受支持",
            )
        if field == "projection_build_hash":
            _hash(value, field)
            _hash(expected_value, f"expected_{field}")
        if value.lower() in {"unavailable", "legacy", "none"}:
            raise Stage4CandidateFactsError(
                "semantic_projection_unavailable",
                "projection_fallback_forbidden",
                "正式真题组卷禁止使用不可用或旧投影回退",
            )
        if value != expected_value:
            raise Stage4CandidateFactsError(
                "semantic_projection_unavailable",
                f"projection_{field}_drift",
                "冻结语义投影身份与当前预检不一致",
            )
        normalized[field] = value
    return normalized, _digest(normalized)


def _live_identity(value: object, *, identity: tuple[str, str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _LIVE_STATE_FIELDS:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved",
            "candidate_live_identity_incomplete",
            f"候选 {identity[1]} 的当前身份不完整",
        )
    card_version_id = value.get("card_version_id")
    if isinstance(card_version_id, bool) or not isinstance(card_version_id, int) or card_version_id < 1:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved", "card_version_id_invalid", "候选卡片版本无效"
        )
    return {
        "content_hash": _hash(value.get("content_hash"), "live_content_hash"),
        "card_version_id": card_version_id,
        "card_hash": _hash(value.get("card_hash"), "live_card_hash"),
        "projection_document_hash": _hash(
            value.get("projection_document_hash"), "live_projection_document_hash"
        ),
    }


def freeze_candidate_facts(
    *,
    projection: Mapping[str, Any],
    expected_projection: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    current_candidate_identities: Mapping[tuple[str, str], Mapping[str, Any]],
    target_institution: str,
) -> FrozenCandidateFacts:
    """Freeze stable candidate facts after strict projection/card drift checks."""

    projection_identity, projection_hash = validate_projection_identity(
        projection, expected=expected_projection
    )
    target = _text(target_institution, "target_institution", maximum=320)
    if not isinstance(current_candidate_identities, Mapping):
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved",
            "candidate_live_identity_set_invalid",
            "当前候选身份集合无效",
        )
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved", "candidate_rows_invalid", "冻结候选集合无效"
        )
    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    candidate_ids: set[str] = set()
    for raw in candidates:
        if (
            not isinstance(raw, Mapping)
            or not _CANDIDATE_REQUIRED_FIELDS.issubset(raw)
            or not set(raw).issubset(_CANDIDATE_ALLOWED_FIELDS)
        ):
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved",
                "candidate_fact_fields_incomplete",
                "冻结候选事实字段不完整",
            )
        source_kind = _text(raw.get("source_kind"), "source_kind", maximum=64)
        if source_kind not in SUPPORTED_SOURCE_KINDS:
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved", "source_kind_unsupported", "候选来源类型不受支持"
            )
        candidate_id = _text(raw.get("candidate_id"), "candidate_id", maximum=256)
        identity = (source_kind, candidate_id)
        if identity in identities:
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved", "candidate_identity_duplicate", "冻结候选身份重复"
            )
        identities.add(identity)
        if candidate_id in candidate_ids:
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved",
                "candidate_id_not_globally_unique",
                "冻结候选 ID 必须在解析批次内唯一",
            )
        candidate_ids.add(candidate_id)
        live = _live_identity(current_candidate_identities.get(identity), identity=identity)
        card_version_id = raw.get("card_version_id")
        if isinstance(card_version_id, bool) or not isinstance(card_version_id, int) or card_version_id < 1:
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved", "card_version_id_invalid", "候选卡片版本无效"
            )
        frozen_identity = {
            "content_hash": _hash(raw.get("content_hash"), "content_hash"),
            "card_version_id": card_version_id,
            "card_hash": _hash(raw.get("card_hash"), "card_hash"),
            "projection_document_hash": _hash(
                raw.get("projection_document_hash"), "projection_document_hash"
            ),
        }
        for field, value in frozen_identity.items():
            if live[field] != value:
                raise Stage4CandidateFactsError(
                    "candidate_fact_unresolved",
                    f"candidate_{field}_drift",
                    f"候选 {candidate_id} 的内容、卡片或投影身份已漂移",
                )
        chapter: str | None
        source_exam_id: str | None
        section_id: str | None
        source_institution: str | None
        if source_kind == "ordinary":
            if any(raw.get(field) not in (None, "") for field in (
                "source_exam_id", "section_id", "source_institution"
            )):
                raise Stage4CandidateFactsError(
                    "candidate_fact_unresolved",
                    "ordinary_source_provenance_conflict",
                    "普通题库候选不得携带外校真题来源字段",
                )
            chapter = _text(raw.get("chapter"), "chapter", maximum=640)
            source_exam_id = None
            section_id = None
            source_institution = None
        else:
            if raw.get("chapter") not in (None, ""):
                raise Stage4CandidateFactsError(
                    "candidate_fact_unresolved",
                    "past_exam_textbook_chapter_forbidden",
                    "外校真题候选不得伪造教材章节",
                )
            chapter = None
            source_exam_id = _text(raw.get("source_exam_id"), "source_exam_id", maximum=512)
            section_id = _text(raw.get("section_id"), "section_id", maximum=512)
            source_institution = _text(
                raw.get("source_institution"), "source_institution", maximum=320
            )
            if source_institution.casefold() == target.casefold():
                raise Stage4CandidateFactsError(
                    "candidate_fact_unresolved",
                    "target_institution_candidate_forbidden",
                    "正式真题候选不得来自目标院校",
                )
        difficulty = evaluate_difficulty_fact(
            raw.get("difficulty"), raw.get("difficulty_confidence")
        )
        try:
            question_type_family = normalize_question_type_family(
                raw.get("question_type_family")
            )
        except ProfileV3ContractError as exc:
            raise Stage4CandidateFactsError(
                "candidate_fact_unresolved",
                "question_type_family_invalid",
                "候选题型族无法规范化",
            ) from exc
        content_snapshot = _text(
            raw.get("content_snapshot"), "content_snapshot", maximum=500_000
        )
        normalized.append(
            {
                "source_kind": source_kind,
                "candidate_id": candidate_id,
                "subject": _text(raw.get("subject"), "subject", maximum=320),
                "question_type_family": question_type_family,
                "chapter": chapter,
                "source_exam_id": source_exam_id,
                "section_id": section_id,
                "source_institution": source_institution,
                "content_snapshot": content_snapshot,
                "content_snapshot_hash": _digest(content_snapshot),
                **frozen_identity,
                "difficulty_fact": difficulty.to_dict(),
            }
        )
    if set(current_candidate_identities) != identities:
        raise Stage4CandidateFactsError(
            "candidate_fact_unresolved",
            "candidate_live_identity_set_mismatch",
            "冻结候选集合与当前身份集合不一致",
        )
    normalized.sort(key=lambda item: (item["source_kind"], item["candidate_id"]))
    unresolved = tuple(
        item["candidate_id"]
        for item in normalized
        if not item["difficulty_fact"]["resolved"]
    )
    payload = {
        "contract_version": CANDIDATE_FACTS_CONTRACT_VERSION,
        "projection_identity": projection_identity,
        "projection_identity_hash": projection_hash,
        "target_institution": target,
        "candidates": normalized,
        "unresolved_candidate_ids": list(unresolved),
    }
    payload_json = _canonical_json(payload)
    facts_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return FrozenCandidateFacts(
        CANDIDATE_FACTS_CONTRACT_VERSION,
        projection_hash,
        facts_hash,
        target,
        tuple(normalized),
        unresolved,
        payload_json,
    )


__all__ = [
    "CANDIDATE_FACTS_CONTRACT_VERSION",
    "DIFFICULTY_FACT_VERSION",
    "DIFFICULTY_LABELS",
    "DIFFICULTY_THRESHOLD",
    "DifficultyFact",
    "FrozenCandidateFacts",
    "PROJECTION_IDENTITY_VERSION",
    "Stage4CandidateFactsError",
    "build_projection_document_hash",
    "evaluate_difficulty_fact",
    "freeze_candidate_facts",
    "validate_projection_identity",
]
