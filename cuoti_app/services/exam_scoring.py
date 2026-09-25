"""Explicit, fail-closed scoring semantics for published exams."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")


def _decimal_text(value: Any, *, label: str) -> str:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive finite decimal")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive finite decimal") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{label} must be a positive finite decimal")
    try:
        if not math.isfinite(float(number)):
            raise ValueError(f"{label} is outside the persistent numeric range")
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} is outside the persistent numeric range") from exc
    normalized = format(number.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _hash(value: Any, *, label: str) -> str:
    text = str(value or "")
    if _HASH_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a lowercase sha256 hash")
    return text


@dataclass(frozen=True, slots=True)
class ExamScoringProjection:
    mode: str
    total_points: str | None
    evidence_hash: str | None
    items: tuple[tuple[int, str, str], ...]
    projection_hash: str

    def item(self, position: int) -> tuple[str | None, str | None]:
        if self.mode != "verified":
            return None, None
        _position, points, evidence = self.items[position - 1]
        return points, evidence

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "total_points": self.total_points,
            "evidence_hash": self.evidence_hash,
            "items": [
                {"position": position, "points": points, "evidence_hash": evidence}
                for position, points, evidence in self.items
            ],
        }


def normalize_scoring_projection(
    value: Mapping[str, Any] | ExamScoringProjection | None,
    *,
    item_count: int,
) -> ExamScoringProjection:
    """Validate a complete scoring projection; omission explicitly means unscored."""

    if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count < 0:
        raise ValueError("scoring item count must not be negative")
    if isinstance(value, ExamScoringProjection):
        value = value.to_dict()
    if value is None:
        value = {"mode": "unscored", "total_points": None, "evidence_hash": None, "items": []}
    if not isinstance(value, Mapping):
        raise TypeError("scoring projection must be a mapping")
    expected_keys = {"mode", "total_points", "evidence_hash", "items"}
    if set(value) != expected_keys:
        raise ValueError("scoring projection fields do not match the contract")
    mode = str(value.get("mode") or "")
    if mode == "unscored":
        if value.get("total_points") is not None or value.get("evidence_hash") is not None:
            raise ValueError("unscored projection must not contain points or evidence")
        if list(value.get("items") or []):
            raise ValueError("unscored projection must not contain item points")
        normalized = {"mode": mode, "total_points": None, "evidence_hash": None, "items": []}
        raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return ExamScoringProjection(mode, None, None, (), hashlib.sha256(raw.encode()).hexdigest())
    if mode != "verified":
        raise ValueError("scoring mode must be unscored or verified")
    total = _decimal_text(value.get("total_points"), label="total points")
    evidence = _hash(value.get("evidence_hash"), label="scoring evidence")
    raw_items = value.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
        raise TypeError("verified scoring items must be a sequence")
    items: list[tuple[int, str, str]] = []
    for expected, raw_item in enumerate(raw_items, start=1):
        if not isinstance(raw_item, Mapping) or set(raw_item) != {
            "position", "points", "evidence_hash"
        }:
            raise ValueError("verified scoring item fields do not match the contract")
        if raw_item.get("position") != expected:
            raise ValueError("verified scoring positions must be continuous from one")
        points = _decimal_text(raw_item.get("points"), label="item points")
        item_evidence = _hash(raw_item.get("evidence_hash"), label="item scoring evidence")
        items.append((expected, points, item_evidence))
    if len(items) != item_count:
        raise ValueError("verified scoring item count does not match the paper")
    if sum((Decimal(points) for _, points, _ in items), Decimal(0)) != Decimal(total):
        raise ValueError("verified scoring total does not equal the exact item sum")
    normalized = {
        "mode": mode,
        "total_points": total,
        "evidence_hash": evidence,
        "items": [
            {"position": position, "points": points, "evidence_hash": item_evidence}
            for position, points, item_evidence in items
        ],
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return ExamScoringProjection(mode, total, evidence, tuple(items), hashlib.sha256(raw.encode()).hexdigest())


def public_points(mode: str, value: Any) -> float | None:
    """Never expose historical/unknown zeroes as authoritative scores."""

    if mode != "verified" or value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


__all__ = ["ExamScoringProjection", "normalize_scoring_projection", "public_points"]
