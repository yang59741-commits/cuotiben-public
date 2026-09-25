"""Stable, filter-bound cursor helpers for v2 list APIs."""

from __future__ import annotations

import base64
import hashlib
import json


class CursorError(ValueError):
    """Raised when a pagination cursor is invalid for the current request."""


def bounded_limit(value, *, default: int = 30, maximum: int = 100) -> int:
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CursorError("limit 必须是整数") from exc
    if parsed < 1 or parsed > maximum:
        raise CursorError(f"limit 必须在 1–{maximum} 之间")
    return parsed


def filter_digest(filters: dict) -> str:
    payload = json.dumps(
        filters, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def encode_cursor(*, filters: dict, snapshot: int, values: list) -> str:
    payload = {
        "v": 1,
        "f": filter_digest(filters),
        "s": int(snapshot),
        "p": values,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str | None, *, filters: dict) -> dict | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise CursorError("分页游标无效") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != 1
        or payload.get("f") != filter_digest(filters)
        or not isinstance(payload.get("s"), int)
        or payload["s"] < 0
        or not isinstance(payload.get("p"), list)
    ):
        raise CursorError("分页游标与当前筛选条件不匹配")
    return payload


def escaped_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace(
        "_", "\\_"
    ) + "%"

