"""Deterministic, versioned source lineage for mistake-exam question slots."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Mapping, Sequence


MISTAKE_LINEAGE_POLICY_VERSION = "mistake-lineage-v1"


class MistakeLineagePolicyError(ValueError):
    """Raised when frozen mistake facts cannot produce an honest slot plan."""


@dataclass(frozen=True, slots=True)
class MistakeLineageSlot:
    position: int
    target_chapter: str
    target_question_type: str
    source_mistake_ids: tuple[str, ...]
    source_lineage_hash: str
    policy_version: str = MISTAKE_LINEAGE_POLICY_VERSION

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: object) -> str:
    return str(value or "").strip()


def _integer(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recent_key(value: object) -> float:
    text = _text(value)
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0.0


def _weakness_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    status = _text(record.get("status"))
    mastered = status in {"已掌握", "mastered", "resolved"}
    return (
        1 if mastered else 0,
        -_integer(record.get("lapse_count")),
        _integer(record.get("correct_streak")),
        -_integer(record.get("review_count")),
        -_recent_key(record.get("date_added") or record.get("updated_at")),
        _text(record.get("public_id")),
    )


def _freeze_record(record: Mapping[str, Any]) -> dict[str, Any]:
    public_id = _text(record.get("public_id"))
    internal_id = record.get("id")
    chapter = _text(record.get("chapter") or record.get("chapter_name"))
    content = _text(record.get("content"))
    if not public_id:
        raise MistakeLineagePolicyError("冻结错题缺少稳定 public_id")
    if isinstance(internal_id, bool):
        raise MistakeLineagePolicyError(f"冻结错题 {public_id} 内部 id 必须是正整数")
    try:
        internal_id = int(internal_id)
    except (TypeError, ValueError) as exc:
        raise MistakeLineagePolicyError(
            f"冻结错题 {public_id} 内部 id 必须是正整数"
        ) from exc
    if internal_id < 1:
        raise MistakeLineagePolicyError(f"冻结错题 {public_id} 内部 id 必须是正整数")
    if not chapter:
        raise MistakeLineagePolicyError(f"冻结错题 {public_id} 缺少章节")
    if not content:
        raise MistakeLineagePolicyError(f"冻结错题 {public_id} 缺少正文")
    return {
        "public_id": public_id,
        "id": internal_id,
        "chapter": chapter,
        "date_added": _text(record.get("date_added")),
        "updated_at": _text(record.get("updated_at")),
        "status": _text(record.get("status")),
        "lapse_count": _integer(record.get("lapse_count")),
        "correct_streak": _integer(record.get("correct_streak")),
        "review_count": _integer(record.get("review_count")),
        "fingerprint": _text(record.get("fingerprint")),
        "content_fingerprint": _text(record.get("content_fingerprint")),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def build_mistake_lineage_slots(
    frozen_mistakes: Sequence[Mapping[str, Any]],
    *,
    question_count: int,
    allowed_question_types: Sequence[str],
) -> tuple[MistakeLineageSlot, ...]:
    """Allocate honest source lineage before generation.

    Every selected mistake receives one slot first. Extra slots are assigned by
    current coverage count, then weakness, recency and stable public id.  The
    type allocator likewise fills the least-covered allowed type first.
    """

    if isinstance(question_count, bool) or not isinstance(question_count, int):
        raise MistakeLineagePolicyError("题量必须是整数")
    if question_count <= 0:
        raise MistakeLineagePolicyError("题量必须大于 0")
    if not frozen_mistakes:
        raise MistakeLineagePolicyError("至少需要一条冻结错题")
    types = tuple(sorted({_text(value) for value in allowed_question_types if _text(value)}))
    if not types:
        raise MistakeLineagePolicyError("至少需要一种允许题型")

    records: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    seen: set[str] = set()
    for source in frozen_mistakes:
        if not isinstance(source, Mapping):
            raise MistakeLineagePolicyError("冻结错题记录必须是对象")
        frozen = _freeze_record(source)
        public_id = frozen["public_id"]
        if public_id in seen:
            raise MistakeLineagePolicyError(f"冻结错题 public_id 重复：{public_id}")
        seen.add(public_id)
        records.append((source, frozen))
    if question_count < len(records):
        raise MistakeLineagePolicyError("题量小于所选错题数，无法保证逐条覆盖")

    ordered = sorted(records, key=lambda pair: _weakness_key(pair[0]))
    source_coverage = {frozen["public_id"]: 0 for _, frozen in ordered}
    type_coverage = {value: 0 for value in types}
    allocations: list[tuple[Mapping[str, Any], dict[str, Any]]] = []
    for pair in ordered:
        allocations.append(pair)
        source_coverage[pair[1]["public_id"]] += 1
    while len(allocations) < question_count:
        pair = min(
            ordered,
            key=lambda value: (
                source_coverage[value[1]["public_id"]],
                *_weakness_key(value[0]),
            ),
        )
        allocations.append(pair)
        source_coverage[pair[1]["public_id"]] += 1

    slots = []
    for position, (_, frozen) in enumerate(allocations, start=1):
        question_type = min(types, key=lambda value: (type_coverage[value], value))
        type_coverage[question_type] += 1
        lineage_fact = {
            "policy_version": MISTAKE_LINEAGE_POLICY_VERSION,
            "position": position,
            "target_chapter": frozen["chapter"],
            "target_question_type": question_type,
            "source_mistake_ids": [frozen["public_id"]],
            "frozen_source": frozen,
        }
        slots.append(MistakeLineageSlot(
            position=position,
            target_chapter=frozen["chapter"],
            target_question_type=question_type,
            source_mistake_ids=(frozen["public_id"],),
            source_lineage_hash=_canonical_hash(lineage_fact),
        ))
    return tuple(slots)


def validate_mistake_lineage_slots(
    frozen_mistakes: Sequence[Mapping[str, Any]],
    slots: Sequence[MistakeLineageSlot],
) -> tuple[MistakeLineageSlot, ...]:
    """Rebuild a supplied plan from source facts and require exact equality."""

    ordered = tuple(sorted(slots, key=lambda slot: slot.position))
    types = tuple(sorted({slot.target_question_type for slot in ordered}))
    expected = build_mistake_lineage_slots(
        frozen_mistakes,
        question_count=len(ordered),
        allowed_question_types=types,
    )
    if ordered != expected:
        raise MistakeLineagePolicyError("错题题位谱系与冻结错题事实不一致")
    return ordered


__all__ = [
    "MISTAKE_LINEAGE_POLICY_VERSION",
    "MistakeLineagePolicyError",
    "MistakeLineageSlot",
    "build_mistake_lineage_slots",
    "validate_mistake_lineage_slots",
]
