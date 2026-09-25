"""Pure exact-slot profile v3 contracts and legacy migration planning."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .exam_difficulty_policy import parse_question_difficulty


PROFILE_V3_SCHEMA_VERSION = 3
PROFILE_V3_CONTRACT_VERSION = "past-exam-exact-slot-v3"
PROFILE_V3_TYPE = "past_exam_exact_slot"
PROFILE_V3_READER_VERSIONS = frozenset({1, 2, 3})
PROFILE_V3_SCORING_MODES = frozenset({"verified", "unscored"})
PROFILE_V3_AUTHORITY_KINDS = frozenset({"human", "external_authority"})

_HEX_64 = re.compile(r"^[0-9a-fA-F]{64}$")
_QUESTION_TYPE_ALIASES = {
    "choice": "choice",
    "选择题": "choice",
    "fill_blank": "fill_blank",
    "填空题": "fill_blank",
    "calculation": "calculation",
    "计算题": "calculation",
    "proof": "proof",
    "证明题": "proof",
    "comprehensive": "comprehensive",
    "综合题": "comprehensive",
}
_PROFILE_KEYS = frozenset({
    "schema_version",
    "contract_version",
    "profile_type",
    "profile_id",
    "profile_hash",
    "identity",
    "provenance",
    "sections",
    "slots",
    "scoring",
})
_IDENTITY_KEYS = frozenset({
    "institution",
    "subject",
    "reference_year",
    "source_snapshot",
    "source_exam_id",
    "catalog_version",
    "course_version",
})
_PROVENANCE_KEYS = frozenset({
    "learning_years",
    "classification_version",
    "classifier_prompt_version",
    "classifier_model_profile",
    "normalization_version",
    "evidence_manifest_hash",
})
_SECTION_KEYS = frozenset({
    "section_id",
    "title",
    "points",
    "points_evidence_hash",
})
_SLOT_KEYS = frozenset({
    "position",
    "section_id",
    "question_type_family",
    "target_difficulty",
    "points",
    "evidence",
})
_SCORING_KEYS = frozenset({"mode", "total_points", "evidence_hash"})
_EVIDENCE_KEYS = frozenset({"question_type", "difficulty", "points"})
_ATTESTATION_KEYS = frozenset({
    "profile_id",
    "profile_hash",
    "authority_kind",
    "authority_ref_hash",
    "confirmed_at",
})


class ProfileV3ContractError(ValueError):
    """Stable contract failure for profile, exact-slot and scoring inputs."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class LegacyProfileDiagnostic:
    schema_version: int
    prompt_version: str
    source_hash: str
    identity: tuple[tuple[str, Any], ...]
    generation_eligible: bool
    disposition: str
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        identity = dict(self.identity)
        if isinstance(identity.get("learning_years"), tuple):
            identity["learning_years"] = list(identity["learning_years"])
        return {
            "schema_version": self.schema_version,
            "prompt_version": self.prompt_version,
            "source_hash": self.source_hash,
            "identity": identity,
            "generation_eligible": self.generation_eligible,
            "disposition": self.disposition,
            "reason_codes": list(self.reason_codes),
        }


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "画像内容无法规范序列化",
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是对象",
        )
    return value


def _exact_keys(
    value: Mapping[str, Any],
    allowed: frozenset[str],
    label: str,
) -> None:
    actual = set(value)
    if actual != set(allowed):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}字段集合无效",
            details={
                "missing": sorted(set(allowed) - actual),
                "unknown": sorted(str(item) for item in actual - set(allowed)),
            },
        )


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}不能为空",
        )
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是正整数",
        )
    return value


def _hash(value: object, label: str) -> str:
    if type(value) is not str or _HEX_64.fullmatch(value) is None:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是 64 位哈希",
        )
    return value.lower()


def _optional_hash(value: object, label: str) -> str | None:
    return None if value is None else _hash(value, label)


def _decimal_points(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是正数",
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是有限正数",
        )
    try:
        normalized = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是正数",
        ) from exc
    if not normalized.is_finite() or normalized <= 0:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            f"{label}必须是有限正数",
        )
    return normalized


def _json_points(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral() else float(value)


def normalize_question_type_family(value: object) -> str:
    if type(value) is not str or value.strip() not in _QUESTION_TYPE_ALIASES:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "逐槽题型族无效",
        )
    return _QUESTION_TYPE_ALIASES[value.strip()]


def semantic_card_question_family(value: object) -> str:
    """Map existing semantic labels without rewriting cards or frozen profiles."""
    if type(value) is str and value.strip() in {'辨析题', '构造题', '判断题', '讨论题', '问答题', '简答题', '应用题'}:
        return 'comprehensive'
    return normalize_question_type_family(value)


def _normalize_identity(value: object) -> dict[str, Any]:
    identity = _mapping(value, "画像身份")
    _exact_keys(identity, _IDENTITY_KEYS, "画像身份")
    return {
        "institution": _text(identity.get("institution"), "院校"),
        "subject": _text(identity.get("subject"), "学科"),
        "reference_year": _positive_int(
            identity.get("reference_year"), "参考年份"
        ),
        "source_snapshot": _hash(
            identity.get("source_snapshot"), "来源快照"
        ),
        "source_exam_id": _text(
            identity.get("source_exam_id"), "来源试卷身份"
        ),
        "catalog_version": _text(
            identity.get("catalog_version"), "目录版本"
        ),
        "course_version": _text(
            identity.get("course_version"), "课程版本"
        ),
    }


def _normalize_provenance(value: object) -> dict[str, Any]:
    provenance = _mapping(value, "画像来源证据")
    _exact_keys(provenance, _PROVENANCE_KEYS, "画像来源证据")
    raw_years = provenance.get("learning_years")
    if (
        isinstance(raw_years, (str, bytes))
        or not isinstance(raw_years, Sequence)
        or not raw_years
    ):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "学习年份必须是非空数组",
        )
    years = tuple(_positive_int(value, "学习年份") for value in raw_years)
    if len(years) != len(set(years)):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "学习年份不得重复",
        )
    return {
        "learning_years": sorted(years),
        "classification_version": _text(
            provenance.get("classification_version"), "分类版本"
        ),
        "classifier_prompt_version": _text(
            provenance.get("classifier_prompt_version"), "分类提示词版本"
        ),
        "classifier_model_profile": _text(
            provenance.get("classifier_model_profile"), "分类模型身份"
        ),
        "normalization_version": _text(
            provenance.get("normalization_version"), "规范化版本"
        ),
        "evidence_manifest_hash": _hash(
            provenance.get("evidence_manifest_hash"), "证据清单哈希"
        ),
    }


def _normalize_sections(value: object) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "画像分区必须是非空数组",
        )
    sections: list[dict[str, Any]] = []
    indexes: dict[str, int] = {}
    for index, raw in enumerate(value):
        section = _mapping(raw, "画像分区")
        _exact_keys(section, _SECTION_KEYS, "画像分区")
        section_id = _text(section.get("section_id"), "分区身份")
        if section_id in indexes:
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "分区身份不得重复",
            )
        indexes[section_id] = index
        sections.append({
            "section_id": section_id,
            "title": _text(section.get("title"), "分区标题"),
            "points": section.get("points"),
            "points_evidence_hash": section.get("points_evidence_hash"),
        })
    return sections, indexes


def _normalize_slots(
    value: object,
    section_indexes: Mapping[str, int],
) -> list[dict[str, Any]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "逐槽合同必须是非空数组",
        )
    slots: list[dict[str, Any]] = []
    positions: list[int] = []
    for raw in value:
        slot = _mapping(raw, "逐槽合同")
        _exact_keys(slot, _SLOT_KEYS, "逐槽合同")
        position = _positive_int(slot.get("position"), "题位")
        section_id = _text(slot.get("section_id"), "题位分区")
        if section_id not in section_indexes:
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "题位引用了不存在的分区",
                details={"position": position, "section_id": section_id},
            )
        try:
            difficulty = parse_question_difficulty(
                slot.get("target_difficulty")
            ).value
        except ValueError as exc:
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "题位目标难度无效",
                details={"position": position},
            ) from exc
        evidence = _mapping(slot.get("evidence"), "题位证据")
        _exact_keys(evidence, _EVIDENCE_KEYS, "题位证据")
        positions.append(position)
        slots.append({
            "position": position,
            "section_id": section_id,
            "question_type_family": normalize_question_type_family(
                slot.get("question_type_family")
            ),
            "target_difficulty": difficulty,
            "points": slot.get("points"),
            "evidence": {
                "question_type": _hash(
                    evidence.get("question_type"), "题型证据哈希"
                ),
                "difficulty": _hash(
                    evidence.get("difficulty"), "难度证据哈希"
                ),
                "points": evidence.get("points"),
            },
        })
    if sorted(positions) != list(range(1, len(slots) + 1)):
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "逐槽 position 必须从 1 连续且不得重复",
        )
    return sorted(slots, key=lambda item: item["position"])


def _normalize_scoring(
    sections: list[dict[str, Any]],
    slots: list[dict[str, Any]],
    value: object,
) -> dict[str, Any]:
    scoring = _mapping(value, "分值合同")
    _exact_keys(scoring, _SCORING_KEYS, "分值合同")
    mode = scoring.get("mode")
    if type(mode) is not str or mode not in PROFILE_V3_SCORING_MODES:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "分值模式无效",
        )
    if mode == "unscored":
        if scoring.get("total_points") is not None or scoring.get("evidence_hash") is not None:
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "未计分卷不得保存总分或分值证据",
            )
        if any(
            slot.get("points") is not None
            or slot["evidence"].get("points") is not None
            for slot in slots
        ) or any(
            section.get("points") is not None
            or section.get("points_evidence_hash") is not None
            for section in sections
        ):
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "未计分卷不得保留部分分值",
            )
        return {"mode": "unscored", "total_points": None, "evidence_hash": None}

    total = _decimal_points(scoring.get("total_points"), "试卷总分")
    evidence_hash = _hash(scoring.get("evidence_hash"), "整卷分值证据哈希")
    slot_totals: dict[str, Decimal] = {
        section["section_id"]: Decimal(0) for section in sections
    }
    for slot in slots:
        points = _decimal_points(slot.get("points"), "题位分值")
        slot["points"] = _json_points(points)
        slot["evidence"]["points"] = _hash(
            slot["evidence"].get("points"), "题位分值证据哈希"
        )
        slot_totals[slot["section_id"]] += points
    section_sum = Decimal(0)
    for section in sections:
        points = _decimal_points(section.get("points"), "分区分值")
        if points != slot_totals[section["section_id"]]:
            raise ProfileV3ContractError(
                "scoring_schema_invalid",
                "分区分值与所属题位之和不一致",
                details={"section_id": section["section_id"]},
            )
        section["points"] = _json_points(points)
        section["points_evidence_hash"] = _hash(
            section.get("points_evidence_hash"), "分区分值证据哈希"
        )
        section_sum += points
    if section_sum != total:
        raise ProfileV3ContractError(
            "scoring_schema_invalid",
            "逐槽与分区分值之和必须等于总分",
        )
    return {
        "mode": "verified",
        "total_points": _json_points(total),
        "evidence_hash": evidence_hash,
    }


def validate_profile_v3(payload: object) -> dict[str, Any]:
    """Return canonical immutable profile content or fail closed."""

    profile = _mapping(payload, "画像 v3")
    _exact_keys(profile, _PROFILE_KEYS, "画像 v3")
    if (
        type(profile.get("schema_version")) is not int
        or profile.get("schema_version") != PROFILE_V3_SCHEMA_VERSION
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像不是 schema v3",
        )
    if profile.get("contract_version") != PROFILE_V3_CONTRACT_VERSION:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像合同版本无效",
        )
    if profile.get("profile_type") != PROFILE_V3_TYPE:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像类型无效",
        )
    identity = _normalize_identity(profile.get("identity"))
    provenance = _normalize_provenance(profile.get("provenance"))
    sections, section_indexes = _normalize_sections(profile.get("sections"))
    slots = _normalize_slots(profile.get("slots"), section_indexes)
    scoring = _normalize_scoring(sections, slots, profile.get("scoring"))
    content = {
        "schema_version": PROFILE_V3_SCHEMA_VERSION,
        "contract_version": PROFILE_V3_CONTRACT_VERSION,
        "profile_type": PROFILE_V3_TYPE,
        "identity": identity,
        "provenance": provenance,
        "sections": sections,
        "slots": slots,
        "scoring": scoring,
    }
    profile_hash = _digest(content)
    profile_id = f"ppv3:{profile_hash}"
    supplied_id = profile.get("profile_id")
    supplied_hash = profile.get("profile_hash")
    if supplied_id not in (None, profile_id) or supplied_hash not in (
        None,
        profile_hash,
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像内容身份或哈希不一致",
        )
    return {
        **content,
        "profile_id": profile_id,
        "profile_hash": profile_hash,
    }


def profile_combination_key(profile_or_identity: Mapping[str, Any]) -> str:
    profile_or_identity = _mapping(profile_or_identity, "画像组合")
    identity = profile_or_identity.get("identity", profile_or_identity)
    identity = _mapping(identity, "画像组合")
    scope = {
        "institution": _text(identity.get("institution"), "院校"),
        "subject": _text(identity.get("subject"), "学科"),
        "reference_year": _positive_int(
            identity.get("reference_year"), "参考年份"
        ),
    }
    return f"profile-combination:{_digest(scope)}"


def validate_profile_attestation(
    profile: Mapping[str, Any],
    attestation: object,
) -> dict[str, Any]:
    normalized_profile = validate_profile_v3(profile)
    value = _mapping(attestation, "画像确认")
    _exact_keys(value, _ATTESTATION_KEYS, "画像确认")
    if (
        value.get("profile_id") != normalized_profile["profile_id"]
        or value.get("profile_hash") != normalized_profile["profile_hash"]
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像确认与内容身份不一致",
        )
    authority_kind = value.get("authority_kind")
    if authority_kind not in PROFILE_V3_AUTHORITY_KINDS:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像确认来源无效",
        )
    confirmed_at = _text(value.get("confirmed_at"), "确认时间")
    try:
        parsed = datetime.fromisoformat(confirmed_at)
    except ValueError as exc:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "确认时间无效",
        ) from exc
    if parsed.tzinfo is None:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "确认时间必须带时区",
        )
    return {
        "profile_id": normalized_profile["profile_id"],
        "profile_hash": normalized_profile["profile_hash"],
        "authority_kind": authority_kind,
        "authority_ref_hash": _hash(
            value.get("authority_ref_hash"), "确认回执哈希"
        ),
        "confirmed_at": parsed.isoformat(),
    }


def inspect_legacy_profile(payload: object) -> LegacyProfileDiagnostic:
    value = _mapping(payload, "旧画像")
    raw_schema = value.get("schema_version")
    if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "旧画像 schema 版本无效",
        )
    if raw_schema not in {1, 2}:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "只支持查看或迁移 v1/v2 旧画像",
        )
    identity = _mapping(value.get("identity"), "旧画像身份")
    required = (
        "institution",
        "subject",
        "reference_year",
        "source_snapshot",
        "learning_years",
    )
    raw_years = identity.get("learning_years")
    identity_validity = {
        "institution": (
            type(identity.get("institution")) is str
            and bool(identity.get("institution").strip())
        ),
        "subject": (
            type(identity.get("subject")) is str
            and bool(identity.get("subject").strip())
        ),
        "reference_year": (
            type(identity.get("reference_year")) is int
            and identity.get("reference_year") > 0
        ),
        "source_snapshot": (
            type(identity.get("source_snapshot")) is str
            and _HEX_64.fullmatch(identity.get("source_snapshot")) is not None
        ),
    }
    valid_years = (
        isinstance(raw_years, Sequence)
        and not isinstance(raw_years, (str, bytes))
        and bool(raw_years)
        and all(type(year) is int and year > 0 for year in raw_years)
        and len(raw_years) == len(set(raw_years))
    )
    frozen_years: tuple[int, ...] | str = (
        tuple(raw_years)
        if valid_years
        else _canonical(raw_years)
    )
    normalized_identity = tuple(
        (
            key,
            (
                frozen_years
                if key == "learning_years"
                else (
                    identity.get(key)
                    if identity_validity[key]
                    else _canonical(identity.get(key))
                )
            ),
        )
        for key in required
    )
    reason_codes = [
        "legacy_missing_exact_slots",
        "legacy_missing_slot_difficulty_evidence",
        "legacy_requires_new_attestation",
    ]
    if not valid_years:
        reason_codes.append("legacy_invalid_learning_years")
    reason_codes.extend(
        f"legacy_invalid_{key}"
        for key in (
            "institution",
            "subject",
            "reference_year",
            "source_snapshot",
        )
        if not identity_validity[key]
    )
    return LegacyProfileDiagnostic(
        schema_version=raw_schema,
        prompt_version=str(identity.get("prompt_version") or ""),
        source_hash=_digest(value),
        identity=normalized_identity,
        generation_eligible=False,
        disposition="requires_authoritative_slot_supplement",
        reason_codes=tuple(reason_codes),
    )


def read_profile_artifact(payload: object) -> dict[str, Any]:
    value = _mapping(payload, "画像文件")
    schema = value.get("schema_version")
    if type(schema) is not int or schema not in PROFILE_V3_READER_VERSIONS:
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "画像 schema 版本不受支持",
        )
    if schema in {1, 2}:
        return {
            "read_mode": "legacy_diagnostic",
            "generation_eligible": False,
            "diagnostic": inspect_legacy_profile(value).to_dict(),
        }
    return {
        "read_mode": "v3_inactive",
        "generation_eligible": False,
        "profile": validate_profile_v3(value),
    }


def migrate_legacy_profile_to_v3_draft(
    legacy_payload: object,
    authoritative_supplement: object,
) -> dict[str, Any]:
    """Build an inactive v3 draft only from an explicit slot supplement."""

    diagnostic = inspect_legacy_profile(legacy_payload)
    legacy = _mapping(legacy_payload, "旧画像")
    legacy_identity = _mapping(legacy.get("identity"), "旧画像身份")
    legacy_identity_validity = {
        "institution": (
            type(legacy_identity.get("institution")) is str
            and bool(legacy_identity.get("institution").strip())
        ),
        "subject": (
            type(legacy_identity.get("subject")) is str
            and bool(legacy_identity.get("subject").strip())
        ),
        "reference_year": (
            type(legacy_identity.get("reference_year")) is int
            and legacy_identity.get("reference_year") > 0
        ),
        "source_snapshot": (
            type(legacy_identity.get("source_snapshot")) is str
            and _HEX_64.fullmatch(legacy_identity.get("source_snapshot"))
            is not None
        ),
    }
    for field, valid in legacy_identity_validity.items():
        if not valid:
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "旧画像身份无效，不能迁移",
                details={"field": field},
            )
    supplement = _mapping(authoritative_supplement, "权威逐槽补充")
    required_keys = frozenset({
        "identity",
        "provenance",
        "sections",
        "slots",
        "scoring",
    })
    _exact_keys(supplement, required_keys, "权威逐槽补充")
    identity = dict(_mapping(supplement.get("identity"), "补充身份"))
    for key in ("institution", "subject", "reference_year", "source_snapshot"):
        if identity.get(key) != legacy_identity.get(key):
            raise ProfileV3ContractError(
                "profile_v3_not_verified",
                "权威补充与旧画像身份不一致",
                details={"field": key},
            )
    profile = validate_profile_v3({
        "schema_version": PROFILE_V3_SCHEMA_VERSION,
        "contract_version": PROFILE_V3_CONTRACT_VERSION,
        "profile_type": PROFILE_V3_TYPE,
        "profile_id": None,
        "profile_hash": None,
        "identity": identity,
        "provenance": supplement.get("provenance"),
        "sections": supplement.get("sections"),
        "slots": supplement.get("slots"),
        "scoring": supplement.get("scoring"),
    })
    legacy_years_value = dict(diagnostic.identity).get("learning_years")
    if not isinstance(legacy_years_value, tuple):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "旧画像学习年份无效，不能迁移",
            details={"field": "learning_years"},
        )
    legacy_years = legacy_years_value
    if tuple(profile["provenance"]["learning_years"]) != tuple(
        sorted(legacy_years)
    ):
        raise ProfileV3ContractError(
            "profile_v3_not_verified",
            "权威补充与旧画像学习年份不一致",
            details={"field": "learning_years"},
        )
    return {
        "profile": profile,
        "generation_eligible": False,
        "migration": {
            "source_schema_version": diagnostic.schema_version,
            "source_hash": diagnostic.source_hash,
            "disposition": "draft_requires_attestation_and_activation",
        },
    }


__all__ = [
    "LegacyProfileDiagnostic",
    "PROFILE_V3_AUTHORITY_KINDS",
    "PROFILE_V3_CONTRACT_VERSION",
    "PROFILE_V3_READER_VERSIONS",
    "PROFILE_V3_SCHEMA_VERSION",
    "PROFILE_V3_SCORING_MODES",
    "PROFILE_V3_TYPE",
    "ProfileV3ContractError",
    "inspect_legacy_profile",
    "migrate_legacy_profile_to_v3_draft",
    "normalize_question_type_family",
    "semantic_card_question_family",
    "profile_combination_key",
    "read_profile_artifact",
    "validate_profile_attestation",
    "validate_profile_v3",
]
