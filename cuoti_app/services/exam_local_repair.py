"""Provider-free contracts for one-shot, position-local exam repair."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from .ai import DeepSeekConfigurationError, DeepSeekServiceError
from .exam_quality_review import ALLOWED_ISSUES
from .job_contracts import JobError


EVIDENCE_SCHEMA_VERSION = "exam-local-repair-evidence-v1"
REPAIR_CONTRACT_VERSION = "exam-local-repair-v1"
REPAIR_REASON_CODES = frozenset(ALLOWED_ISSUES)
_BODY_FIELDS = frozenset({"content", "latex"})
_LABEL_SPLIT = re.compile(r"[、,，;；/|\n]+")
_LABEL_SPACE = re.compile(r"\s+")
_FORBIDDEN_EVIDENCE_KEYS = frozenset({
    "answer", "solution", "reference_answer", "reasoning", "chain_of_thought",
    "prompt", "raw_prompt", "raw_response", "api_key", "credential",
    "credential_ref",
})


class LocalItemRepairError(ValueError):
    """Fail-closed error with a stable, content-free reason code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failed_positions: Sequence[int] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.failed_positions = tuple(sorted({int(value) for value in failed_positions}))


@dataclass(frozen=True, slots=True)
class FrozenGenerationEvidence:
    source: str
    expected_count: int
    canonical_payload: str
    sha256: str
    schema_version: str = EVIDENCE_SCHEMA_VERSION

    def payload(self) -> dict[str, Any]:
        value = json.loads(self.canonical_payload)
        if not isinstance(value, dict):
            raise LocalItemRepairError("EVIDENCE_INVALID", "冻结证据结构无效")
        return value

    def verify(self) -> None:
        actual = hashlib.sha256(self.canonical_payload.encode("utf-8")).hexdigest()
        if actual != self.sha256:
            raise LocalItemRepairError("EVIDENCE_CHANGED", "冻结证据摘要不一致")
        payload = self.payload()
        if (
            payload.get("schema_version") != self.schema_version
            or payload.get("source") != self.source
            or payload.get("expected_count") != self.expected_count
        ):
            raise LocalItemRepairError("EVIDENCE_CHANGED", "冻结证据合同不一致")

    def safe_summary(self) -> dict[str, Any]:
        payload = self.payload()
        return {
            "schema_version": self.schema_version,
            "source": self.source,
            "expected_count": self.expected_count,
            "source_record_count": len(payload.get("source_records") or []),
            "candidate_count": len(payload.get("candidates") or []),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class LocalRepairResult:
    items: tuple[dict[str, Any], ...]
    repaired_positions: tuple[int, ...]
    repair_attempts: tuple[tuple[int, int], ...]
    rereview_count: int
    review: Any
    contract_version: str = REPAIR_CONTRACT_VERSION

    def body_repairs(self) -> dict[int, str]:
        return {
            position: str(
                self.items[position - 1].get("latex")
                or self.items[position - 1].get("content")
                or ""
            )
            for position in self.repaired_positions
        }

    def safe_receipt(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "repaired_positions": list(self.repaired_positions),
            "repair_attempts": [list(value) for value in self.repair_attempts],
            "rereview_count": self.rereview_count,
            "passed": True,
        }


@dataclass(frozen=True, slots=True)
class ScopedAdaptationResult:
    items: tuple[dict[str, Any], ...]
    accepted_positions: tuple[int, ...]
    fallback_positions: tuple[int, ...]
    max_adaptations: int
    reason_codes: tuple[tuple[int, str], ...]


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _privacy_safe_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _privacy_safe_evidence(item)
            for key, item in value.items()
            if str(key).casefold() not in _FORBIDDEN_EVIDENCE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_privacy_safe_evidence(item) for item in value]
    return value


def freeze_generation_evidence(
    source: str,
    *,
    expected_count: int,
    source_records: Sequence[Mapping[str, Any]] = (),
    candidates: Sequence[Mapping[str, Any]] = (),
    constraints: Mapping[str, Any] | None = None,
) -> FrozenGenerationEvidence:
    """Freeze complete, untruncated source evidence for local repair."""

    source = str(source or "").strip()
    if source not in {"free", "mistakes"}:
        raise LocalItemRepairError("EVIDENCE_INVALID", "冻结证据来源无效")
    if isinstance(expected_count, bool) or int(expected_count) <= 0:
        raise LocalItemRepairError("EVIDENCE_INVALID", "冻结题量无效")
    if any(not isinstance(item, Mapping) for item in source_records) or any(
        not isinstance(item, Mapping) for item in candidates
    ):
        raise LocalItemRepairError("EVIDENCE_INVALID", "冻结证据条目无效")
    candidate_values = [
        dict(_privacy_safe_evidence(dict(item))) for item in candidates
    ]
    candidate_ids = [
        str(item.get("candidate_id") or "").strip() for item in candidate_values
    ]
    if source == "free" and not candidate_values:
        raise LocalItemRepairError("EVIDENCE_INVALID", "自由组卷冻结候选不能为空")
    if (
        any(not value for value in candidate_ids)
        or len(candidate_ids) != len(set(candidate_ids))
        or any(not str(item.get("content") or "").strip() for item in candidate_values)
    ):
        raise LocalItemRepairError("EVIDENCE_INVALID", "冻结候选身份缺失或重复")
    payload = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "source": source,
        "expected_count": int(expected_count),
        "source_records": [
            dict(_privacy_safe_evidence(dict(item))) for item in source_records
        ],
        "candidates": candidate_values,
        "constraints": dict(_privacy_safe_evidence(dict(constraints or {}))),
    }
    canonical = _canonical(payload)
    return FrozenGenerationEvidence(
        source=source,
        expected_count=int(expected_count),
        canonical_payload=canonical,
        sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def exact_adaptation_limit(question_count: int) -> int:
    if isinstance(question_count, bool) or int(question_count) <= 0:
        raise LocalItemRepairError("ADAPTATION_LIMIT_INVALID", "改编题量合同无效")
    return int(question_count) // 3


def _positioned_items(
    items: Sequence[Mapping[str, Any]], expected_count: int
) -> list[dict[str, Any]]:
    if len(items) != expected_count:
        raise LocalItemRepairError("QUESTION_COUNT_CHANGED", "局部修复题量发生变化")
    result = [deepcopy(dict(item)) for item in items]
    positions = []
    for fallback, item in enumerate(result, start=1):
        raw_position = item.get("position", fallback)
        if isinstance(raw_position, bool):
            raise LocalItemRepairError("POSITION_CHANGED", "局部修复题位无效")
        try:
            positions.append(int(raw_position))
        except (TypeError, ValueError) as exc:
            raise LocalItemRepairError("POSITION_CHANGED", "局部修复题位无效") from exc
    if positions != list(range(1, expected_count + 1)):
        raise LocalItemRepairError("POSITION_CHANGED", "局部修复题位必须连续且唯一")
    return result


def _body_fields(item: Mapping[str, Any]) -> tuple[str, ...]:
    fields = tuple(field for field in ("latex", "content") if field in item)
    if not fields:
        raise LocalItemRepairError("ITEM_BODY_INVALID", "题位缺少可修复正文")
    return fields


def _validate_frozen_sources(
    items: Sequence[Mapping[str, Any]], payload: Mapping[str, Any]
) -> None:
    known_ids = {
        str(item.get(key) or "").strip()
        for item in [
            *(payload.get("candidates") or []),
            *(payload.get("source_records") or []),
        ]
        if isinstance(item, Mapping)
        for key in ("candidate_id", "public_id", "id")
        if str(item.get(key) or "").strip()
    }
    for item in items:
        references = item.get("reference_ids")
        if references is not None:
            if (
                not isinstance(references, list)
                or any(not isinstance(value, str) or not value.strip() for value in references)
                or len(references) != len(set(map(str, references)))
                or (known_ids and set(map(str, references)) - known_ids)
            ):
                raise LocalItemRepairError("FROZEN_SOURCE_INVALID", "题位引用不属于冻结证据")
        source_id = str(
            item.get("candidate_id") or item.get("source_candidate_id") or ""
        ).strip()
        if source_id and known_ids and source_id not in known_ids:
            raise LocalItemRepairError("FROZEN_SOURCE_INVALID", "题位来源不属于冻结证据")


def _apply_repair_value(
    original: Mapping[str, Any], value: object
) -> dict[str, Any]:
    body_fields = _body_fields(original)
    if isinstance(value, str):
        body = value.strip()
        proposed = deepcopy(dict(original))
        for field in body_fields:
            proposed[field] = body
    elif isinstance(value, Mapping):
        if not set(value).intersection(body_fields):
            raise LocalItemRepairError("ITEM_BODY_INVALID", "局部修复未返回正文")
        if any(field in value and field not in body_fields for field in _BODY_FIELDS):
            raise LocalItemRepairError("ITEM_BODY_INVALID", "局部修复正文槽位无效")
        unexpected = set(value) - set(original) - _BODY_FIELDS
        if unexpected:
            raise LocalItemRepairError(
                "FROZEN_FIELD_CHANGED", "局部修复返回了未冻结字段"
            )
        proposed = deepcopy(dict(original))
        for key, item_value in value.items():
            proposed[str(key)] = deepcopy(item_value)
        bodies = {
            str(proposed.get(field) or "").strip()
            for field in body_fields
        }
        if len(bodies) != 1:
            raise LocalItemRepairError("ITEM_BODY_INVALID", "局部修复正文不一致")
        body = next(iter(bodies))
    else:
        raise LocalItemRepairError("ITEM_BODY_INVALID", "局部修复正文类型无效")
    if not body:
        raise LocalItemRepairError("ITEM_BODY_INVALID", "局部修复正文不能为空")
    for key, original_value in original.items():
        if key not in _BODY_FIELDS and proposed.get(key) != original_value:
            raise LocalItemRepairError(
                "FROZEN_FIELD_CHANGED", "局部修复改变了冻结字段"
            )
    return proposed


def _review_outcome(review: Any) -> tuple[bool, tuple[int, ...]]:
    if isinstance(review, Mapping):
        passed = review.get("passed")
        raw_failed = review.get("failed_positions") or ()
    else:
        passed = getattr(review, "passed", None)
        raw_failed = getattr(review, "failed_positions", ())
    if not isinstance(passed, bool) or not isinstance(raw_failed, (list, tuple)):
        raise LocalItemRepairError("REREVIEW_INVALID", "局部复审合同无效")
    try:
        failed = tuple(sorted({int(value) for value in raw_failed}))
    except (TypeError, ValueError) as exc:
        raise LocalItemRepairError("REREVIEW_INVALID", "局部复审题位无效") from exc
    return passed, failed


def repair_failed_positions_once(
    items: Sequence[Mapping[str, Any]],
    *,
    failed_positions: Sequence[int],
    issues_by_position: Mapping[int, Sequence[str]],
    frozen_evidence: FrozenGenerationEvidence,
    repair_one: Callable[[Mapping[str, Any], tuple[str, ...], Mapping[str, Any]], object],
    review_once: Callable[[Sequence[Mapping[str, Any]]], Any],
) -> LocalRepairResult:
    """Repair each failed position once, then review the whole frozen paper once."""

    frozen_evidence.verify()
    originals = _positioned_items(items, frozen_evidence.expected_count)
    evidence_payload = frozen_evidence.payload()
    _validate_frozen_sources(originals, evidence_payload)
    try:
        failed = tuple(sorted({int(value) for value in failed_positions}))
    except (TypeError, ValueError) as exc:
        raise LocalItemRepairError("FAILED_POSITION_INVALID", "失败题位无效") from exc
    if any(position < 1 or position > len(originals) for position in failed):
        raise LocalItemRepairError("FAILED_POSITION_INVALID", "失败题位超出冻结题量")
    if not failed:
        return LocalRepairResult(tuple(originals), (), (), 0, None)
    repaired = deepcopy(originals)
    attempts = []
    for position in failed:
        raw_issues = issues_by_position.get(position, ())
        if not raw_issues and isinstance(issues_by_position, Mapping):
            raw_issues = issues_by_position.get(str(position), ())  # type: ignore[arg-type]
        if not isinstance(raw_issues, (list, tuple, set)):
            raise LocalItemRepairError(
                "REPAIR_REASON_INVALID",
                "局部修复理由码无效",
                failed_positions=(position,),
            )
        issues = tuple(sorted({str(value) for value in raw_issues}))
        if not issues or set(issues) - REPAIR_REASON_CODES:
            raise LocalItemRepairError(
                "REPAIR_REASON_INVALID",
                "局部修复理由码无效",
                failed_positions=(position,),
            )
        try:
            proposal = repair_one(deepcopy(originals[position - 1]), issues, evidence_payload)
            repaired[position - 1] = _apply_repair_value(
                originals[position - 1], proposal
            )
        except (
            DeepSeekConfigurationError,
            DeepSeekServiceError,
            JobError,
            LocalItemRepairError,
        ):
            raise
        except Exception as exc:
            raise LocalItemRepairError(
                "REPAIR_ATTEMPT_FAILED",
                "局部题位修复失败",
                failed_positions=(position,),
            ) from exc
        attempts.append((position, 1))
    # Passed positions are byte-for-byte object snapshots; only failed bodies
    # may differ and every other field remains frozen by _apply_repair_value.
    for position, original in enumerate(originals, start=1):
        if position not in failed and repaired[position - 1] != original:
            raise LocalItemRepairError(
                "PASSED_ITEM_CHANGED", "已通过题位发生变化", failed_positions=(position,)
            )
    try:
        review = review_once(tuple(deepcopy(repaired)))
    except (DeepSeekConfigurationError, DeepSeekServiceError, JobError):
        raise
    except Exception as exc:
        raise LocalItemRepairError(
            "REREVIEW_FAILED", "局部修复复审失败", failed_positions=failed
        ) from exc
    passed, remaining = _review_outcome(review)
    if not passed or remaining:
        raise LocalItemRepairError(
            "QUALITY_REPAIR_FAILED",
            "局部修复复审仍未通过",
            failed_positions=remaining or failed,
        )
    return LocalRepairResult(
        items=tuple(repaired),
        repaired_positions=failed,
        repair_attempts=tuple(attempts),
        rereview_count=1,
        review=review,
    )


def repair_from_quality_review_once(
    items: Sequence[Mapping[str, Any]],
    initial_review: Any,
    *,
    frozen_evidence: FrozenGenerationEvidence,
    repair_one: Callable[[Mapping[str, Any], tuple[str, ...], Mapping[str, Any]], object],
    review_once: Callable[[Sequence[Mapping[str, Any]]], Any],
) -> LocalRepairResult:
    """Adapter for ``ExamQualityReview`` without depending on provider code."""

    if isinstance(initial_review, Mapping):
        failed = initial_review.get("failed_positions") or ()
        review_items = initial_review.get("items") or ()
    else:
        failed = getattr(initial_review, "failed_positions", ())
        review_items = getattr(initial_review, "items", ())
    if not isinstance(failed, (list, tuple)) or not isinstance(review_items, (list, tuple)):
        raise LocalItemRepairError("INITIAL_REVIEW_INVALID", "初次审核合同无效")
    issues_by_position: dict[int, tuple[str, ...]] = {}
    for fallback, item in enumerate(review_items, start=1):
        if not isinstance(item, Mapping):
            raise LocalItemRepairError("INITIAL_REVIEW_INVALID", "初次审核题位无效")
        try:
            position = int(item.get("position", fallback))
        except (TypeError, ValueError) as exc:
            raise LocalItemRepairError("INITIAL_REVIEW_INVALID", "初次审核题位无效") from exc
        raw_issues = item.get("issues") or ()
        if not isinstance(raw_issues, (list, tuple)):
            raise LocalItemRepairError("INITIAL_REVIEW_INVALID", "初次审核理由无效")
        issues_by_position[position] = tuple(str(value) for value in raw_issues)
    return repair_failed_positions_once(
        items,
        failed_positions=failed,
        issues_by_position=issues_by_position,
        frozen_evidence=frozen_evidence,
        repair_one=repair_one,
        review_once=review_once,
    )


def _labels(value: object) -> tuple[str, ...]:
    raw_values = value if isinstance(value, (list, tuple, set)) else [value]
    labels = set()
    for raw in raw_values:
        for part in _LABEL_SPLIT.split(str(raw or "")):
            normalized = _LABEL_SPACE.sub("", part).casefold().strip()
            if normalized:
                labels.add(normalized)
    return tuple(sorted(labels))


def _first_labels(item: Mapping[str, Any], fields: Sequence[str]) -> tuple[str, ...]:
    for field in fields:
        labels = _labels(item.get(field))
        if labels:
            return labels
    return ()


def adaptation_signature(item: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        _first_labels(item, ("knowledge_points", "concepts", "key_concepts", "knowledge_point")),
        _first_labels(item, ("question_type", "type", "target_question_type")),
        _first_labels(item, ("method_tags", "methods", "method")),
    )


def adaptation_within_scope(
    original: Mapping[str, Any], adapted: Mapping[str, Any]
) -> bool:
    before = adaptation_signature(original)
    after = adaptation_signature(adapted)
    return all(before[index] and before[index] == after[index] for index in range(3))


def apply_scoped_adaptations(
    originals: Sequence[Mapping[str, Any]],
    proposals: Sequence[Mapping[str, Any]],
    *,
    max_adaptations: int,
) -> ScopedAdaptationResult:
    """Apply in-scope adaptations; deterministically fall back when labels drift."""

    if isinstance(max_adaptations, bool) or int(max_adaptations) < 0:
        raise LocalItemRepairError("ADAPTATION_LIMIT_INVALID", "改编上限无效")
    items = [deepcopy(dict(item)) for item in originals]
    proposal_map: dict[int, Mapping[str, Any]] = {}
    for proposal in proposals:
        if not isinstance(proposal, Mapping):
            raise LocalItemRepairError("ADAPTATION_POSITION_INVALID", "改编条目无效")
        try:
            position = int(proposal.get("position"))
        except (TypeError, ValueError) as exc:
            raise LocalItemRepairError("ADAPTATION_POSITION_INVALID", "改编题位无效") from exc
        if position in proposal_map or position < 1 or position > len(items):
            raise LocalItemRepairError("ADAPTATION_POSITION_INVALID", "改编题位重复或越界")
        proposal_map[position] = proposal
    requested = [
        position for position, proposal in proposal_map.items()
        if str(proposal.get("adapted_content") or "").strip()
    ]
    if len(requested) > int(max_adaptations):
        raise LocalItemRepairError("ADAPTATION_LIMIT_EXCEEDED", "改编数量超过精确上限")
    accepted = []
    fallback = []
    reasons = []
    for position in sorted(requested):
        original = items[position - 1]
        proposal = proposal_map[position]
        original_candidate = str(original.get("candidate_id") or original.get("source_candidate_id") or "")
        proposed_candidate = str(proposal.get("candidate_id") or proposal.get("source_candidate_id") or "")
        if proposed_candidate and original_candidate and proposed_candidate != original_candidate:
            raise LocalItemRepairError("FROZEN_SOURCE_CHANGED", "改编改变了冻结候选来源")
        if not adaptation_within_scope(original, proposal):
            fallback.append(position)
            reasons.append((position, "adaptation_scope_invalid"))
            continue
        body = str(proposal.get("adapted_content") or "").strip()
        target_field = "content" if "content" in original else "latex"
        if target_field not in original:
            raise LocalItemRepairError("ITEM_BODY_INVALID", "改编原题缺少正文")
        original[target_field] = body
        if "adapted" in original:
            original["adapted"] = True
        accepted.append(position)
    return ScopedAdaptationResult(
        items=tuple(items),
        accepted_positions=tuple(accepted),
        fallback_positions=tuple(fallback),
        max_adaptations=int(max_adaptations),
        reason_codes=tuple(reasons),
    )


__all__ = [
    "EVIDENCE_SCHEMA_VERSION",
    "REPAIR_CONTRACT_VERSION",
    "REPAIR_REASON_CODES",
    "FrozenGenerationEvidence",
    "LocalItemRepairError",
    "LocalRepairResult",
    "ScopedAdaptationResult",
    "adaptation_signature",
    "adaptation_within_scope",
    "apply_scoped_adaptations",
    "exact_adaptation_limit",
    "freeze_generation_evidence",
    "repair_from_quality_review_once",
    "repair_failed_positions_once",
]
