"""Past-exam semantic decision and final candidate freeze for stage P3.4."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .exam_generation_freeze import (
    build_candidate_freeze_payload,
    candidate_decisions,
    restore_frozen_past_candidates,
    validate_frozen_candidates,
)
from .exam_generation_past_candidates import (
    PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION,
    PAST_CROSS_JOB_HISTORY_AUDIT_MARKER,
    PAST_UNMASTERED_SOFT_EXEMPTION_MARKER,
    PastCandidatePoolResult,
)
from .exam_manifests import (
    ExamManifestError,
    exam_manifest_payload,
    freeze_exam_generation_manifest,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
)
from .semantic_recall import validate_frozen_recall_contract


@dataclass(frozen=True, slots=True)
class PastSemanticFreezeRequest:
    database: str
    generation_job_id: str | None
    past_exam_directory: str
    institution: str
    subject: str
    question_count: int
    note: str
    question_types: tuple[str, ...]
    difficulty: str
    paper_count: int
    question_bank_snapshot: str
    past_exam_snapshot_id: str
    learning_years: tuple[int, ...]
    learning_mode: str
    reference_year: int
    candidate_source: str
    source_quotas: Mapping[str, int]
    manual_profile_required: bool
    past_learning_candidates: tuple[Mapping[str, Any], ...]
    ai_task_freezes: Mapping[str, Any]
    semantic_settings_revision: int
    semantic_candidate_multiplier: int
    semantic_allow_partial_cache: bool
    recent_content_fingerprints: tuple[str, ...] = ()
    unmastered_content_fingerprints: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PastSemanticFreezeResult:
    """Serializable semantic winner state handed to final-material stage P3.5."""

    chapter_names: tuple[str, ...]
    chapter_quotas: Mapping[str, int]
    question_bank_coverage: Mapping[str, Any]
    ordinary_candidates: tuple[Mapping[str, Any], ...]
    other_candidates: tuple[Mapping[str, Any], ...]
    candidate_pool: tuple[Mapping[str, Any], ...]
    material_understanding: Mapping[str, Any]
    recent_past_ids: tuple[str, ...]
    past_analysis: Mapping[str, Any]
    chapter_plan: tuple[Mapping[str, Any], ...]
    difficulty_quota: Mapping[str, int] | None
    past_profile_status: str
    past_profile_origin: str
    past_profile_id: str | None
    frozen_manifest: Mapping[str, Any] | None
    saved_payload: Mapping[str, Any]
    versioned_manifest: bool
    staged_manifest: str | None
    recall_effective_mode: str
    fallback_reason: str | None
    paper_seeds: tuple[str, ...]
    unfrozen_candidate_decisions: tuple[Mapping[str, Any], ...] | None
    recent_content_fingerprints: tuple[str, ...] = ()
    unmastered_content_fingerprints: tuple[str, ...] = ()


def past_semantic_request_hash(request: PastSemanticFreezeRequest) -> str:
    """Return the canonical identity of every request field that shapes a freeze."""

    identity = {
        "institution": request.institution,
        "subject": request.subject,
        "question_count": request.question_count,
        "note": request.note,
        "question_types": list(request.question_types),
        "difficulty": request.difficulty,
        "paper_count": request.paper_count,
        "manual_profile_required": request.manual_profile_required,
        "ai_task_freezes": dict(request.ai_task_freezes),
        "semantic_settings_revision": request.semantic_settings_revision,
        "semantic_candidate_multiplier": request.semantic_candidate_multiplier,
        "semantic_allow_partial_cache": request.semantic_allow_partial_cache,
        "reference_year": request.reference_year,
        "candidate_source": request.candidate_source,
        "learning_years": list(request.learning_years),
        "learning_mode": request.learning_mode,
        "past_learning_candidates": [
            dict(item) for item in request.past_learning_candidates
        ],
        "source_quotas": dict(request.source_quotas),
        "recent_content_fingerprints": list(request.recent_content_fingerprints),
        "unmastered_content_fingerprints": list(
            request.unmastered_content_fingerprints
        ),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _corrupt_extension(message: str) -> None:
    raise ExamManifestError("MANIFEST_CORRUPT", message)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_PAST_MATERIAL_RECEIPT_FIELDS = frozenset({
    "schema_version",
    "complete",
    "cache_key",
    "snapshot",
    "total_count",
    "analyzed_count",
    "batch_count",
    "reused_count",
    "permanent_card_reused_count",
    "legacy_deep_read_count",
    "private_cache_reused_count",
    "missing_count",
    "chapters",
    "years",
    "sources",
    "profile_id",
    "profile_origin",
})
_PAST_MATERIAL_RECEIPT_MAX_ITEMS = 4_096
_PAST_MATERIAL_RECEIPT_MAX_COUNT = 1_000_000


def _material_receipt_error(error_code: str, message: str) -> None:
    raise ExamManifestError(error_code, message)


def validate_past_material_receipt(
    receipt: Mapping[str, Any],
    *,
    error_code: str = "MANIFEST_INVALID",
) -> dict[str, Any]:
    """Validate and copy a bounded, body-free past-material provenance receipt."""

    if not isinstance(receipt, Mapping) or set(receipt) != _PAST_MATERIAL_RECEIPT_FIELDS:
        _material_receipt_error(error_code, "真题材料回执字段缺失或越界")
    value = dict(receipt)
    if value["schema_version"] != 1 or not _is_int(value["schema_version"]):
        _material_receipt_error(error_code, "真题材料回执版本损坏")
    if not isinstance(value["complete"], bool):
        _material_receipt_error(error_code, "真题材料回执完成状态损坏")
    for key in ("cache_key", "snapshot"):
        item = value[key]
        if not isinstance(item, str) or not item.strip() or len(item) > 512:
            _material_receipt_error(error_code, f"真题材料回执字段 {key} 损坏")

    count_fields = (
        "total_count",
        "analyzed_count",
        "batch_count",
        "reused_count",
        "permanent_card_reused_count",
        "legacy_deep_read_count",
        "private_cache_reused_count",
        "missing_count",
    )
    for key in count_fields:
        count = value[key]
        if (
            not _is_int(count)
            or count < 0
            or count > _PAST_MATERIAL_RECEIPT_MAX_COUNT
        ):
            _material_receipt_error(error_code, f"真题材料回执计数 {key} 损坏")
    if value["analyzed_count"] + value["missing_count"] != value["total_count"]:
        _material_receipt_error(error_code, "真题材料回执分析计数不一致")
    if value["complete"] != (value["missing_count"] == 0):
        _material_receipt_error(error_code, "真题材料回执完成状态不一致")
    if value["reused_count"] > value["analyzed_count"]:
        _material_receipt_error(error_code, "真题材料回执复用计数不一致")
    if (
        value["permanent_card_reused_count"] > value["reused_count"]
        or value["private_cache_reused_count"] > value["reused_count"]
        or value["legacy_deep_read_count"] > value["total_count"]
    ):
        _material_receipt_error(error_code, "真题材料回执来源计数不一致")

    chapters = value["chapters"]
    if (
        not isinstance(chapters, list)
        or len(chapters) > _PAST_MATERIAL_RECEIPT_MAX_ITEMS
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 256
            for item in chapters
        )
        or len(chapters) != len(set(chapters))
    ):
        _material_receipt_error(error_code, "真题材料回执章节列表损坏")
    years = value["years"]
    if (
        not isinstance(years, list)
        or len(years) > _PAST_MATERIAL_RECEIPT_MAX_ITEMS
        or any(not _is_int(item) or item < 1 or item > 9_999 for item in years)
        or len(years) != len(set(years))
    ):
        _material_receipt_error(error_code, "真题材料回执年份列表损坏")

    sources = value["sources"]
    if not isinstance(sources, Mapping) or len(sources) > _PAST_MATERIAL_RECEIPT_MAX_ITEMS:
        _material_receipt_error(error_code, "真题材料回执来源映射损坏")
    source_total = 0
    normalized_sources: dict[str, dict[str, int]] = {}
    for source, metadata in sources.items():
        if (
            not isinstance(source, str)
            or not source.strip()
            or len(source) > 2_048
            or not isinstance(metadata, Mapping)
            or set(metadata) != {"count"}
        ):
            _material_receipt_error(error_code, "真题材料回执来源条目损坏")
        count = metadata.get("count")
        if (
            not _is_int(count)
            or count <= 0
            or count > _PAST_MATERIAL_RECEIPT_MAX_COUNT
        ):
            _material_receipt_error(error_code, "真题材料回执来源计数损坏")
        normalized_sources[source] = {"count": count}
        source_total += count
    if sources and source_total != value["total_count"]:
        _material_receipt_error(error_code, "真题材料回执来源总数不一致")

    for key in ("profile_id", "profile_origin"):
        item = value[key]
        if item is not None and (
            not isinstance(item, str) or not item.strip() or len(item) > 256
        ):
            _material_receipt_error(error_code, f"真题材料回执字段 {key} 损坏")

    value["chapters"] = list(chapters)
    value["years"] = list(years)
    value["sources"] = normalized_sources
    return value


def build_past_material_receipt(material: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce material understanding to bounded provenance without body content."""

    if not isinstance(material, Mapping):
        raise ExamManifestError("MANIFEST_INVALID", "真题材料理解结果必须是对象")
    raw_sources = material.get("sources")
    sources = (
        {
            source: {"count": metadata.get("count") if isinstance(metadata, Mapping) else None}
            for source, metadata in raw_sources.items()
        }
        if isinstance(raw_sources, Mapping)
        else raw_sources
    )
    permanent_reused = material.get("permanent_card_reused_count")
    if permanent_reused is None:
        permanent_reused = 0
    legacy_deep_read = material.get("legacy_deep_read_count")
    if legacy_deep_read is None:
        legacy_deep_read = material.get("total_count")
    private_cache_reused = material.get("private_cache_reused_count")
    if private_cache_reused is None:
        private_cache_reused = material.get("reused_count")
    receipt = {
        "schema_version": 1,
        "complete": material.get("complete"),
        "cache_key": material.get("cache_key"),
        "snapshot": material.get("snapshot"),
        "total_count": material.get("total_count"),
        "analyzed_count": material.get("analyzed_count"),
        "batch_count": material.get("batch_count", 0),
        "reused_count": material.get("reused_count", 0),
        "permanent_card_reused_count": permanent_reused,
        "legacy_deep_read_count": legacy_deep_read,
        "private_cache_reused_count": private_cache_reused,
        "missing_count": material.get("missing_count", 0),
        "chapters": (
            list(material["chapters"])
            if isinstance(material.get("chapters"), (list, tuple))
            else material.get("chapters")
        ),
        "years": (
            list(material["years"])
            if isinstance(material.get("years"), (list, tuple))
            else material.get("years")
        ),
        "sources": sources,
        "profile_id": material.get("profile_id"),
        "profile_origin": material.get("profile_origin"),
    }
    return validate_past_material_receipt(receipt)


def _string_id_list(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        _corrupt_extension(f"冻结真题扩展字段 {key} 损坏")
    return list(value)


def _fingerprint_list(payload: Mapping[str, Any], key: str) -> list[str]:
    values = _string_id_list(payload, key)
    if len(values) != len(set(values)) or any(
        len(value) not in {32, 64}
        or any(
            character not in "0123456789abcdef"
            for character in value.casefold()
        )
        for value in values
    ):
        _corrupt_extension(f"冻结真题扩展字段 {key} 损坏")
    return values


def validate_past_semantic_winner_extension(
    payload: Mapping[str, Any],
    request: PastSemanticFreezeRequest,
) -> None:
    """Fail closed on incomplete or request-incompatible past winner state."""

    request_hash = payload.get("past_semantic_request_hash")
    if (
        not isinstance(request_hash, str)
        or len(request_hash) != 64
        or any(character not in "0123456789abcdef" for character in request_hash)
    ):
        _corrupt_extension("冻结真题请求哈希缺失或损坏")
    if request_hash != past_semantic_request_hash(request):
        raise ExamManifestError("MANIFEST_CONFLICT", "冻结真题语义请求已变化")

    analysis = payload.get("past_analysis")
    if not isinstance(analysis, Mapping):
        _corrupt_extension("冻结真题分析缺失或损坏")

    chapter_plan = payload.get("chapter_plan")
    if not isinstance(chapter_plan, list) or not chapter_plan:
        _corrupt_extension("冻结真题章节计划缺失或损坏")
    chapter_names: list[str] = []
    chapter_total = 0
    for item in chapter_plan:
        if not isinstance(item, Mapping):
            _corrupt_extension("冻结真题章节计划项损坏")
        chapter = item.get("chapter")
        question_count = item.get("question_count")
        if not isinstance(chapter, str) or not chapter.strip():
            _corrupt_extension("冻结真题章节计划名称损坏")
        if not _is_int(question_count) or question_count <= 0:
            _corrupt_extension("冻结真题章节计划题量损坏")
        chapter_names.append(chapter)
        chapter_total += question_count
    if len(set(chapter_names)) != len(chapter_names):
        _corrupt_extension("冻结真题章节计划包含重复章节")
    if chapter_total != request.question_count:
        _corrupt_extension("冻结真题章节计划总题量损坏")

    difficulty_quota = payload.get("difficulty_quota")
    if not isinstance(difficulty_quota, Mapping):
        _corrupt_extension("冻结真题难度配额缺失或损坏")
    if any(
        not isinstance(key, str)
        or not key.strip()
        or not _is_int(value)
        or value < 0
        for key, value in difficulty_quota.items()
    ):
        _corrupt_extension("冻结真题难度配额损坏")
    # Non-empty legacy evidence quotas remain readable, but are no longer an
    # execution constraint.  New exact-slot manifests deliberately freeze an
    # empty aggregate quota.
    if difficulty_quota and sum(difficulty_quota.values()) != request.question_count:
        _corrupt_extension("冻结真题难度配额总题量损坏")

    profile = payload.get("past_profile")
    if not isinstance(profile, Mapping):
        _corrupt_extension("冻结真题画像缺失或损坏")
    for key in ("status", "origin", "source_snapshot"):
        if not isinstance(profile.get(key), str) or not profile[key].strip():
            _corrupt_extension(f"冻结真题画像字段 {key} 损坏")
    profile_id = profile.get("profile_id")
    if profile_id is not None and not isinstance(profile_id, str):
        _corrupt_extension("冻结真题画像字段 profile_id 损坏")
    if profile["source_snapshot"] != request.past_exam_snapshot_id:
        raise ExamManifestError("MANIFEST_CONFLICT", "冻结真题画像来源快照已变化")

    receipt = validate_past_material_receipt(
        payload.get("past_material_receipt"),
        error_code="MANIFEST_CORRUPT",
    )
    expected_material_snapshot = (
        f"{request.past_exam_snapshot_id}:"
        f"{','.join(map(str, request.learning_years))}"
    )
    if receipt["snapshot"] != expected_material_snapshot:
        _corrupt_extension("冻结真题材料回执快照不一致")

    membership_policy = payload.get("past_candidate_membership_policy_version")
    history_marker = payload.get("past_history_signal_audit_marker")
    unmastered_soft_marker = payload.get(
        "past_unmastered_soft_exemption_marker"
    )
    if membership_policy is not None or history_marker is not None:
        if (
            membership_policy != PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
            or history_marker != PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
        ):
            _corrupt_extension("冻结真题历史软信号策略标记损坏")
        if "recent_content_fingerprints" not in payload:
            _corrupt_extension("冻结真题历史题面审计快照缺失")
    if unmastered_soft_marker is not None:
        if (
            unmastered_soft_marker != PAST_UNMASTERED_SOFT_EXEMPTION_MARKER
            or membership_policy != PAST_CANDIDATE_MEMBERSHIP_POLICY_VERSION
            or history_marker != PAST_CROSS_JOB_HISTORY_AUDIT_MARKER
        ):
            _corrupt_extension("冻结真题未掌握软信号策略标记损坏")
        if "unmastered_content_fingerprints" not in payload:
            _corrupt_extension("冻结真题未掌握快照缺失")
        decision_recent_ids = payload.get("decision_recent_past_ids")
        if not isinstance(decision_recent_ids, list) or any(
            not isinstance(item, str) or not item.strip()
            for item in decision_recent_ids
        ):
            _corrupt_extension("冻结真题派生近期软信号字段损坏")

    recent_ids = payload.get("recent_question_ids")
    if not isinstance(recent_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in recent_ids
    ):
        _corrupt_extension("冻结真题近期题目字段损坏")
    for key, expected_values in (
        ("recent_content_fingerprints", request.recent_content_fingerprints),
        (
            "unmastered_content_fingerprints",
            request.unmastered_content_fingerprints,
        ),
    ):
        if key in payload:
            values = _fingerprint_list(payload, key)
            if values != list(expected_values):
                raise ExamManifestError(
                    "MANIFEST_CONFLICT", "冻结真题判重状态已变化"
                )
        elif expected_values:
            raise ExamManifestError(
                "MANIFEST_CONFLICT", "冻结真题判重状态缺失"
            )
    _string_id_list(payload, "ordinary_candidate_ids")
    _string_id_list(payload, "other_past_candidate_ids")

    ai_task_freezes = payload.get("ai_task_freezes")
    if not isinstance(ai_task_freezes, Mapping):
        _corrupt_extension("冻结真题 AI 策略字段缺失或损坏")

    settings = (
        ("semantic_settings_revision", request.semantic_settings_revision, _is_int),
        (
            "semantic_candidate_multiplier",
            request.semantic_candidate_multiplier,
            _is_int,
        ),
        (
            "semantic_allow_partial_cache",
            request.semantic_allow_partial_cache,
            lambda value: isinstance(value, bool),
        ),
    )
    for key, expected, type_checker in settings:
        value = payload.get(key)
        if not type_checker(value):
            _corrupt_extension(f"冻结真题语义设置字段 {key} 损坏")
        if value != expected:
            raise ExamManifestError("MANIFEST_CONFLICT", "冻结真题语义设置已变化")

    reference_year = payload.get("reference_year")
    learning_years = payload.get("learning_years")
    source_quotas = payload.get("source_quotas")
    if not _is_int(reference_year):
        _corrupt_extension("冻结真题结构年份字段损坏")
    if not isinstance(payload.get("candidate_source"), str):
        _corrupt_extension("冻结真题候选题源字段损坏")
    if not isinstance(learning_years, list) or any(
        not _is_int(year) for year in learning_years
    ):
        _corrupt_extension("冻结真题学习年份字段损坏")
    if not isinstance(payload.get("learning_mode"), str):
        _corrupt_extension("冻结真题学习模式字段损坏")
    if not isinstance(source_quotas, Mapping) or any(
        not isinstance(key, str)
        or not key.strip()
        or not _is_int(value)
        or value < 0
        for key, value in source_quotas.items()
    ):
        _corrupt_extension("冻结真题题源配额字段损坏")

    expected = {
        "reference_year": request.reference_year,
        "candidate_source": request.candidate_source,
        "learning_years": list(request.learning_years),
        "learning_mode": request.learning_mode,
        "source_quotas": dict(request.source_quotas),
        "ai_task_freezes": dict(request.ai_task_freezes),
    }
    if any(payload.get(key) != value for key, value in expected.items()):
        raise ExamManifestError("MANIFEST_CONFLICT", "冻结真题扩展请求已变化")


def _source_splits(candidates):
    ordinary = []
    other = []
    for item in candidates:
        candidate = dict(item)
        source_kind = str(candidate.get("source_kind") or "")
        if source_kind == "ordinary":
            ordinary.append(candidate)
        elif source_kind in {"past_exam", "past_exam_other"}:
            other.append(candidate)
    return ordinary, other


def _paper_seeds(payload: Mapping[str, Any], paper_count: int) -> list[str]:
    raw = payload.get("paper_seeds")
    seeds = list(raw) if isinstance(raw, list) else []
    if len(seeds) != paper_count:
        raise ExamManifestError("MANIFEST_CORRUPT", "冻结 paper seed 数量不一致")
    return [str(seed) for seed in seeds]


def _validate_winner_request(
    loaded: Mapping[str, Any],
    payload: Mapping[str, Any],
    request: PastSemanticFreezeRequest,
) -> None:
    if str(loaded.get("job_id") or "") != str(request.generation_job_id or ""):
        raise ExamManifestError("MANIFEST_CONFLICT", "冻结组卷清单对应的任务已变化")
    if str(loaded.get("snapshot_id") or "") != request.question_bank_snapshot:
        raise ExamManifestError("MANIFEST_CONFLICT", "组卷清单对应的题库快照已变化")
    if payload.get("source") != "past":
        raise ExamManifestError("MANIFEST_CONFLICT", "同一任务已有不同来源的冻结组卷决定")
    if payload.get("question_bank_snapshot") != request.question_bank_snapshot:
        raise ExamManifestError("MANIFEST_CONFLICT", "同一任务已有不同题库快照的冻结组卷决定")
    if payload.get("past_exam_snapshot") != request.past_exam_snapshot_id:
        raise ExamManifestError("MANIFEST_CONFLICT", "同一任务已有不同真题快照的冻结组卷决定")
    validate_past_semantic_winner_extension(payload, request)


def _winner_state(
    pool: PastCandidatePoolResult,
    request: PastSemanticFreezeRequest,
    payload: Mapping[str, Any],
    *,
    bank_index: Any,
    bank_scope: Any,
    frozen_candidate_restorer: Callable[..., list[Mapping[str, Any]]],
    frozen_candidate_validator: Callable[..., None],
    restore_candidates: bool,
):
    chapter_plan = [dict(item) for item in payload["chapter_plan"]]
    chapter_names = [str(item["chapter"]) for item in chapter_plan]
    chapter_quotas = {
        str(item["chapter"]): int(item["question_count"])
        for item in chapter_plan
    }
    coverage = dict(
        bank_scope.coverage(
            bank_index,
            subject=request.subject,
            chapters=chapter_names,
        )
    )

    if restore_candidates:
        candidates = [
            dict(item)
            for item in frozen_candidate_restorer(
                payload,
                bank_index=bank_index,
                subject=request.subject,
                chapters=chapter_names,
                other_pool=[dict(item) for item in pool.other_pool],
                other_directory=request.past_exam_directory,
                institution=request.institution,
            )
        ]
    else:
        candidates = [dict(item) for item in pool.candidate_pool]
    frozen_candidate_validator(payload, candidates)
    ordinary, other = _source_splits(candidates)
    if len(ordinary) + len(other) != len(candidates):
        _corrupt_extension("冻结真题候选题源类型损坏")
    ordinary_ids = [str(item["candidate_id"]) for item in ordinary]
    other_ids = [str(item["candidate_id"]) for item in other]
    if ordinary_ids != payload["ordinary_candidate_ids"]:
        _corrupt_extension("冻结普通题候选顺序损坏")
    if other_ids != payload["other_past_candidate_ids"]:
        _corrupt_extension("冻结其他真题候选顺序损坏")

    analysis = dict(payload["past_analysis"])
    difficulty_quota = dict(payload["difficulty_quota"])
    winner_profile = payload["past_profile"]
    profile_status = str(winner_profile["status"])
    profile_origin = str(winner_profile["origin"])
    winner_profile_id = winner_profile.get("profile_id")
    profile_id = str(winner_profile_id) if winner_profile_id is not None else None
    material = dict(payload["past_material_receipt"])
    recent_ids = set(payload["recent_question_ids"])
    return {
        "chapter_names": chapter_names,
        "chapter_quotas": chapter_quotas,
        "question_bank_coverage": coverage,
        "ordinary_candidates": ordinary,
        "other_candidates": other,
        "candidate_pool": candidates,
        "material_understanding": material,
        "recent_past_ids": recent_ids,
        "recent_content_fingerprints": set(
            payload.get("recent_content_fingerprints") or []
        ),
        "unmastered_content_fingerprints": set(
            payload.get("unmastered_content_fingerprints") or []
        ),
        "past_analysis": analysis,
        "chapter_plan": chapter_plan,
        "difficulty_quota": difficulty_quota,
        "past_profile_status": profile_status,
        "past_profile_origin": profile_origin,
        "past_profile_id": profile_id,
    }


def adopt_past_semantic_winner(
    pool: PastCandidatePoolResult,
    request: PastSemanticFreezeRequest,
    winner_manifest: Mapping[str, Any],
    *,
    db: Any,
    bank_index: Any,
    bank_scope: Any,
    manifest_payload_reader: Callable[..., Mapping[str, Any]] = exam_manifest_payload,
    versioned_manifest_checker: Callable[..., bool] = is_versioned_exam_manifest,
    frozen_candidate_validator: Callable[..., None] = validate_frozen_candidates,
    frozen_candidate_restorer: Callable[..., list[Mapping[str, Any]]] = (
        restore_frozen_past_candidates
    ),
    frozen_recall_validator: Callable[..., None] = validate_frozen_recall_contract,
) -> PastSemanticFreezeResult:
    """Validate and fully rehydrate an authoritative semantic freeze winner."""

    if not versioned_manifest_checker(winner_manifest):
        raise ExamManifestError("MANIFEST_CORRUPT", "冻结组卷清单缺失或未完成")
    payload = dict(manifest_payload_reader(winner_manifest))
    _validate_winner_request(winner_manifest, payload, request)
    frozen_recall_validator(db, payload)
    state = _winner_state(
        pool,
        request,
        payload,
        bank_index=bank_index,
        bank_scope=bank_scope,
        frozen_candidate_restorer=frozen_candidate_restorer,
        frozen_candidate_validator=frozen_candidate_validator,
        restore_candidates=True,
    )
    fallback_reason = payload.get("fallback_reason")
    if fallback_reason is not None:
        fallback_reason = str(fallback_reason)

    return PastSemanticFreezeResult(
        chapter_names=tuple(state["chapter_names"]),
        chapter_quotas=dict(state["chapter_quotas"]),
        question_bank_coverage=dict(state["question_bank_coverage"]),
        ordinary_candidates=tuple(
            dict(item) for item in state["ordinary_candidates"]
        ),
        other_candidates=tuple(dict(item) for item in state["other_candidates"]),
        candidate_pool=tuple(dict(item) for item in state["candidate_pool"]),
        material_understanding=dict(state["material_understanding"]),
        recent_past_ids=tuple(sorted(state["recent_past_ids"])),
        past_analysis=dict(state["past_analysis"]),
        chapter_plan=tuple(dict(item) for item in state["chapter_plan"]),
        difficulty_quota=(
            dict(state["difficulty_quota"])
            if state["difficulty_quota"] is not None
            else None
        ),
        past_profile_status=str(state["past_profile_status"]),
        past_profile_origin=str(state["past_profile_origin"]),
        past_profile_id=state["past_profile_id"],
        frozen_manifest=dict(winner_manifest),
        saved_payload=payload,
        versioned_manifest=True,
        staged_manifest=None,
        recall_effective_mode="frozen",
        fallback_reason=fallback_reason,
        paper_seeds=tuple(_paper_seeds(payload, request.paper_count)),
        unfrozen_candidate_decisions=None,
        recent_content_fingerprints=tuple(
            sorted(state["recent_content_fingerprints"])
        ),
        unmastered_content_fingerprints=tuple(
            sorted(state["unmastered_content_fingerprints"])
        ),
    )


def prepare_past_semantic_freeze(
    pool: PastCandidatePoolResult,
    request: PastSemanticFreezeRequest,
    *,
    db: Any,
    bank_index: Any,
    bank_scope: Any,
    has_background_job: Callable[[Any, str | None], bool],
    semantic_recall_decision: Callable[..., Any] | None,
    semantic_recall_shadow: Callable[..., Any] | None,
    logger: Any,
    manifest_freezer: Callable[..., Mapping[str, Any]] = freeze_exam_generation_manifest,
    manifest_loader: Callable[..., Mapping[str, Any] | None] = load_exam_generation_manifest,
    manifest_payload_reader: Callable[..., Mapping[str, Any]] = exam_manifest_payload,
    versioned_manifest_checker: Callable[..., bool] = is_versioned_exam_manifest,
    freeze_payload_builder: Callable[..., Mapping[str, Any]] = build_candidate_freeze_payload,
    frozen_candidate_validator: Callable[..., None] = validate_frozen_candidates,
    frozen_candidate_restorer: Callable[..., list[Mapping[str, Any]]] = (
        restore_frozen_past_candidates
    ),
    frozen_recall_validator: Callable[..., None] = validate_frozen_recall_contract,
    candidate_decision_serializer: Callable[..., list[Mapping[str, Any]]] = (
        candidate_decisions
    ),
) -> PastSemanticFreezeResult:
    """Decide past recall and freeze a first-winner candidate state."""

    frozen_manifest = (
        dict(pool.frozen_manifest) if pool.frozen_manifest is not None else None
    )
    saved_payload = dict(pool.saved_payload)
    versioned_manifest = bool(pool.versioned_manifest)
    staged_manifest = pool.staged_manifest
    decision = None
    winner_adopted = False

    if versioned_manifest:
        if frozen_manifest is None:
            raise ExamManifestError("MANIFEST_CORRUPT", "冻结组卷清单缺失")
        _validate_winner_request(frozen_manifest, saved_payload, request)
        frozen_recall_validator(db, saved_payload)
        state = _winner_state(
            pool,
            request,
            saved_payload,
            bank_index=bank_index,
            bank_scope=bank_scope,
            frozen_candidate_restorer=frozen_candidate_restorer,
            frozen_candidate_validator=frozen_candidate_validator,
            restore_candidates=False,
        )
        paper_seeds = _paper_seeds(saved_payload, request.paper_count)
        recall_effective_mode = "frozen"
    else:
        candidates = [dict(item) for item in pool.candidate_pool]

        def load_past_semantic_hard_pool():
            semantic_pool = []
            if pool.ordinary_required:
                semantic_pool.extend(
                    {
                        **dict(item),
                        "source_kind": "ordinary",
                    }
                    for item in bank_scope.candidates(
                        bank_index,
                        request.subject,
                        list(pool.chapter_names),
                    )
                )
            semantic_pool.extend(dict(item) for item in pool.other_pool)
            return semantic_pool

        # This is the already-derived, checkpoint-frozen input to the existing
        # semantic recent-ID soft seam.  Mastery is intentionally not read or
        # re-applied here, so retry cannot drift and no weight changes.
        soft_recent_candidate_ids = set(pool.decision_recent_past_ids)

        decision_kwargs = {
            "subject": request.subject,
            "chapters": list(pool.chapter_names),
            "question_count": request.question_count,
            "note": request.note,
            "question_types": list(request.question_types),
            "difficulty": request.difficulty,
            "chapter_quotas": dict(pool.chapter_quotas),
            # Aggregate difficulty evidence may be retained in the manifest,
            # but past recall must not filter or boost candidates by a free-
            # mode ratio.  Per-position profile facts are enforced downstream.
            # Exact-slot profiles own past-paper difficulty.  An empty mapping
            # keeps the generic recall contract valid without enabling its
            # aggregate difficulty filter or bonus.
            "difficulty_quotas": {},
            "hard_pool_loader": load_past_semantic_hard_pool,
            "legacy_candidates": candidates,
            "recent_candidate_ids": soft_recent_candidate_ids,
            "required_source_counts": {
                "ordinary": pool.ordinary_required,
                "past_exam_other": pool.other_required,
            },
        }
        if semantic_recall_decision is None:
            if semantic_recall_shadow is not None:
                try:
                    semantic_recall_shadow(
                        **{
                            key: value
                            for key, value in decision_kwargs.items()
                            if key
                            not in {"recent_candidate_ids", "required_source_counts"}
                        }
                    )
                except Exception:
                    logger.exception("semantic recall shadow failed")
            recall_effective_mode = "shadow"
        else:
            decision = semantic_recall_decision(**decision_kwargs)
            if decision.fallback_category:
                logger.warning(
                    "semantic recall fallback category=%s projection_id=%s legacy_ids_hash=%s",
                    decision.fallback_category,
                    decision.projection_id,
                    decision.legacy_ids_hash,
                )
            candidates = [dict(item) for item in decision.effective_candidates]
            recall_effective_mode = str(decision.effective_mode)

        paper_seeds = [
            f"{request.generation_job_id or request.question_bank_snapshot}:{paper_index}"
            for paper_index in range(1, request.paper_count + 1)
        ]
        ordinary, other = _source_splits(candidates)
        state = {
            "chapter_names": list(pool.chapter_names),
            "chapter_quotas": dict(pool.chapter_quotas),
            "question_bank_coverage": dict(pool.question_bank_coverage),
            "ordinary_candidates": ordinary,
            "other_candidates": other,
            "candidate_pool": candidates,
            "material_understanding": dict(pool.material_understanding),
            "recent_past_ids": set(pool.recent_past_ids),
            "recent_content_fingerprints": set(
                request.recent_content_fingerprints
            ),
            "unmastered_content_fingerprints": set(
                request.unmastered_content_fingerprints
            ),
            "past_analysis": dict(pool.past_analysis),
            "chapter_plan": [dict(item) for item in pool.chapter_plan],
            "difficulty_quota": (
                dict(pool.difficulty_quota)
                if pool.difficulty_quota is not None
                else None
            ),
            "past_profile_status": pool.past_profile_status,
            "past_profile_origin": pool.past_profile_origin,
            "past_profile_id": pool.past_profile_id,
        }

        if has_background_job(db, request.generation_job_id):
            freeze_payload = dict(
                freeze_payload_builder(
                    source="past",
                    decision=decision,
                    candidates=candidates,
                    question_bank_snapshot=request.question_bank_snapshot,
                    past_snapshot=request.past_exam_snapshot_id,
                    paper_seeds=paper_seeds,
                    semantic_settings_revision=request.semantic_settings_revision,
                    semantic_candidate_multiplier=request.semantic_candidate_multiplier,
                    semantic_allow_partial_cache=request.semantic_allow_partial_cache,
                    extra={
                        "past_analysis": dict(pool.past_analysis),
                        "chapter_plan": [dict(item) for item in pool.chapter_plan],
                        "difficulty_quota": dict(pool.difficulty_quota or {}),
                        "past_profile": {
                            "status": pool.past_profile_status,
                            "profile_id": pool.past_profile_id,
                            "origin": pool.past_profile_origin,
                            "source_snapshot": request.past_exam_snapshot_id,
                        },
                        "past_material_receipt": build_past_material_receipt(
                            pool.material_understanding
                        ),
                        "recent_question_ids": sorted(pool.recent_past_ids),
                        "recent_content_fingerprints": list(
                            request.recent_content_fingerprints
                        ),
                        "unmastered_content_fingerprints": list(
                            request.unmastered_content_fingerprints
                        ),
                        "ai_task_freezes": dict(request.ai_task_freezes),
                        "ordinary_candidate_ids": [
                            str(item["candidate_id"]) for item in ordinary
                        ],
                        "other_past_candidate_ids": [
                            str(item["candidate_id"]) for item in other
                        ],
                        "learning_years": list(request.learning_years),
                        "learning_mode": request.learning_mode,
                        "reference_year": request.reference_year,
                        "candidate_source": request.candidate_source,
                        "source_quotas": dict(request.source_quotas),
                        "past_semantic_request_hash": (
                            past_semantic_request_hash(request)
                        ),
                    },
                )
            )
            if pool.membership_policy_version is not None:
                freeze_payload["past_candidate_membership_policy_version"] = (
                    pool.membership_policy_version
                )
            if pool.history_signal_audit_marker is not None:
                freeze_payload["past_history_signal_audit_marker"] = (
                    pool.history_signal_audit_marker
                )
            if pool.unmastered_soft_exemption_marker is not None:
                # Marker + IDs are explainable freeze-identity inputs: they
                # replay the exact existing soft-seam request and never alter
                # membership, hard constraints, or a numeric weight.
                freeze_payload["past_unmastered_soft_exemption_marker"] = (
                    pool.unmastered_soft_exemption_marker
                )
                freeze_payload["decision_recent_past_ids"] = list(
                    pool.decision_recent_past_ids
                )
            validate_past_semantic_winner_extension(freeze_payload, request)
            conflict = False
            try:
                frozen_manifest = dict(
                    manifest_freezer(
                        request.database,
                        request.generation_job_id,
                        request.question_bank_snapshot,
                        freeze_payload,
                    )
                )
            except ExamManifestError as error:
                if error.code != "MANIFEST_CONFLICT":
                    raise
                loaded = manifest_loader(
                    request.database,
                    request.generation_job_id,
                )
                if not versioned_manifest_checker(loaded):
                    raise
                frozen_manifest = dict(loaded)
                conflict = True

            saved_payload = dict(manifest_payload_reader(frozen_manifest))
            versioned_manifest = bool(versioned_manifest_checker(frozen_manifest))
            if not versioned_manifest:
                raise ExamManifestError(
                    "MANIFEST_WRITE_FAILED",
                    "冻结组卷清单未持久化",
                )
            staged_manifest = None
            if conflict or saved_payload != freeze_payload:
                _validate_winner_request(frozen_manifest, saved_payload, request)
                frozen_recall_validator(db, saved_payload)
                state = _winner_state(
                    pool,
                    request,
                    saved_payload,
                    bank_index=bank_index,
                    bank_scope=bank_scope,
                    frozen_candidate_restorer=frozen_candidate_restorer,
                    frozen_candidate_validator=frozen_candidate_validator,
                    restore_candidates=True,
                )
                paper_seeds = _paper_seeds(saved_payload, request.paper_count)
                winner_adopted = True
                recall_effective_mode = "frozen"

    unfrozen_decisions = None
    if request.generation_job_id is None and decision is not None:
        unfrozen_decisions = tuple(
            dict(item)
            for item in candidate_decision_serializer(
                decision,
                state["candidate_pool"],
            )
        )
    fallback_reason = saved_payload.get("fallback_reason")
    if not versioned_manifest and not winner_adopted:
        fallback_reason = (
            str(decision.fallback_category)
            if decision is not None and decision.fallback_category is not None
            else fallback_reason
        )
    if fallback_reason is not None:
        fallback_reason = str(fallback_reason)

    return PastSemanticFreezeResult(
        chapter_names=tuple(state["chapter_names"]),
        chapter_quotas=dict(state["chapter_quotas"]),
        question_bank_coverage=dict(state["question_bank_coverage"]),
        ordinary_candidates=tuple(
            dict(item) for item in state["ordinary_candidates"]
        ),
        other_candidates=tuple(dict(item) for item in state["other_candidates"]),
        candidate_pool=tuple(dict(item) for item in state["candidate_pool"]),
        material_understanding=dict(state["material_understanding"]),
        recent_past_ids=tuple(sorted(state["recent_past_ids"])),
        past_analysis=dict(state["past_analysis"]),
        chapter_plan=tuple(dict(item) for item in state["chapter_plan"]),
        difficulty_quota=(
            dict(state["difficulty_quota"])
            if state["difficulty_quota"] is not None
            else None
        ),
        past_profile_status=str(state["past_profile_status"]),
        past_profile_origin=str(state["past_profile_origin"]),
        past_profile_id=state["past_profile_id"],
        frozen_manifest=(
            dict(frozen_manifest) if frozen_manifest is not None else None
        ),
        saved_payload=dict(saved_payload),
        versioned_manifest=versioned_manifest,
        staged_manifest=staged_manifest,
        recall_effective_mode=recall_effective_mode,
        fallback_reason=fallback_reason,
        paper_seeds=tuple(paper_seeds),
        unfrozen_candidate_decisions=unfrozen_decisions,
        recent_content_fingerprints=tuple(
            sorted(state["recent_content_fingerprints"])
        ),
        unmastered_content_fingerprints=tuple(
            sorted(state["unmastered_content_fingerprints"])
        ),
    )


__all__ = [
    "PastSemanticFreezeRequest",
    "PastSemanticFreezeResult",
    "adopt_past_semantic_winner",
    "build_past_material_receipt",
    "past_semantic_request_hash",
    "prepare_past_semantic_freeze",
    "validate_past_material_receipt",
    "validate_past_semantic_winner_extension",
]
