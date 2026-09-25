"""Shared validation helpers for JSON APIs and mistake data."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime

from flask import Request


STATUS_VALUES = ("未掌握", "已掌握")
MISTAKE_REASONS = (
    "计算错误",
    "概念不清",
    "方法不会",
    "卡在技巧",
    "审题失误",
    "时间不够",
)
REVIEW_EVENT_TYPES = ("pass", "again", "assisted", "manual_master", "manual_reopen")


class ValidationError(ValueError):
    """A request value is invalid and should result in a 4xx response."""


def json_object(req: Request) -> dict:
    if not req.is_json:
        raise ValidationError("请求必须使用 application/json")
    value = req.get_json(silent=True)
    if not isinstance(value, dict):
        raise ValidationError("JSON 请求体必须是对象")
    return value


def text_value(value, label: str, *, maximum: int, required: bool = False) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValidationError(f"{label}必须是文本")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{label}不能为空")
    if len(value) > maximum:
        raise ValidationError(f"{label}不能超过 {maximum} 字")
    return value


def positive_int(value, label: str, *, optional: bool = False) -> int | None:
    if optional and value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{label}无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label}无效")
    if parsed <= 0:
        raise ValidationError(f"{label}无效")
    return parsed


def nonnegative_int(value, label: str, default: int = 0) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValidationError(f"{label}无效")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{label}无效")
    if parsed < 0:
        raise ValidationError(f"{label}不能为负数")
    return parsed


def date_value(value, label: str, *, optional: bool = False) -> str | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValidationError(f"{label}必须是 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise ValidationError(f"{label}不是有效日期")
    return value


def timestamp_value(value, label: str) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ValidationError(f"{label}不是有效时间")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        # Old exports may contain date-only event timestamps.
        date_value(value, label)
    return value


def status_value(value, default: str = "未掌握") -> str:
    value = default if value in (None, "") else value
    if value not in STATUS_VALUES:
        raise ValidationError("状态无效")
    return value


def reasons_value(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        requested = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        if not all(isinstance(part, str) for part in value):
            raise ValidationError("错因数组只能包含文本")
        requested = [part.strip() for part in value if part.strip()]
    else:
        raise ValidationError("错因必须是数组或逗号分隔文本")
    invalid = [reason for reason in requested if reason not in MISTAKE_REASONS]
    if invalid:
        raise ValidationError(f"不支持的错因：{invalid[0]}")
    selected = set(requested)
    return ",".join(reason for reason in MISTAKE_REASONS if reason in selected)


def subject_chapter(db, subject_id, chapter_id=None):
    sid = positive_int(subject_id, "科目编号")
    subject = db.execute("SELECT id,name FROM subjects WHERE id=?", (sid,)).fetchone()
    if not subject:
        raise ValidationError("科目不存在")
    cid = positive_int(chapter_id, "章节编号", optional=True)
    chapter = None
    if cid is not None:
        chapter = db.execute(
            "SELECT id,name,subject_id FROM chapters WHERE id=?", (cid,)
        ).fetchone()
        if not chapter:
            raise ValidationError("章节不存在")
        if chapter["subject_id"] != sid:
            raise ValidationError("所选章节不属于当前科目")
    return subject, chapter


def normalized_text(value) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.split()).casefold()


def mistake_fingerprint(subject, chapter, qnum, content, date_added) -> str:
    """Return the cross-entry identity for one dated mistake.

    ``subject`` and ``chapter`` are display names, not database identifiers;
    callers must pass the resolved names so manual, batch, import, and exam
    return paths produce the same five-part identity.
    """
    identity = [
        normalized_text(subject),
        normalized_text(chapter),
        normalized_text(qnum),
        normalized_text(content),
        date_added or "",
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def mistake_content_fingerprint(qnum, content, *, version: int = 1) -> str:
    """Return the date- and catalog-independent content identity.

    This three-part fingerprint supports search/index migrations and must not
    replace :func:`mistake_fingerprint` for entry-level deduplication.
    """
    identity = [version, normalized_text(qnum), normalized_text(content)]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
