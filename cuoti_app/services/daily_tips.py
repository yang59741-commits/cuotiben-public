"""Independent daily-tip use cases and publication helpers."""

from __future__ import annotations

import hashlib
import random
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from typing import Mapping, Sequence

from ..transactions import immediate_transaction
from .tex import UnsafeLatexError, validate_latex_body


MAX_DAILY_TIP_LENGTH = 2_000
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
PUBLICATION_TIP_LIMIT = 5

_STRUCTURAL_COMMAND = re.compile(
    r"\\(?:begin|end|section|subsection|subsubsection|chapter|part|title|author|"
    r"date|maketitle|item|setcounter|addtocounter|newpage|clearpage|pagebreak)\b",
    re.IGNORECASE,
)
_UNESCAPED_PERCENT = re.compile(r"(?<!\\)%")


class DailyTipError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = dict(details or {})


def normalize_daily_tip(content: object) -> str:
    if not isinstance(content, str):
        raise DailyTipError("VALIDATION", "每日提示必须是文本", status_code=400)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise DailyTipError("VALIDATION", "每日提示不能为空", status_code=400)
    if len(normalized) > MAX_DAILY_TIP_LENGTH:
        raise DailyTipError(
            "VALIDATION",
            f"每日提示不能超过 {MAX_DAILY_TIP_LENGTH} 个字符",
            status_code=400,
        )
    try:
        validate_latex_body(
            normalized,
            label="每日提示",
            max_length=MAX_DAILY_TIP_LENGTH,
        )
    except UnsafeLatexError as exc:
        raise DailyTipError("VALIDATION", str(exc), status_code=400) from exc
    if _STRUCTURAL_COMMAND.search(normalized):
        raise DailyTipError(
            "VALIDATION",
            "每日提示不能包含章节、列表或分页等结构命令",
            status_code=400,
        )
    if _UNESCAPED_PERCENT.search(normalized):
        raise DailyTipError(
            "VALIDATION",
            "百分号请写成 \\%",
            status_code=400,
        )
    return normalized


def daily_tip_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def serialize_daily_tip(row) -> dict:
    return {
        "public_id": str(row["public_id"]),
        "content": str(row["content"]),
        "revision": int(row["revision"]),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def list_daily_tips(
    db,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
) -> dict:
    if limit < 1 or limit > MAX_PAGE_SIZE:
        raise DailyTipError(
            "VALIDATION",
            f"limit 必须在 1 到 {MAX_PAGE_SIZE} 之间",
            status_code=400,
        )
    if before_id is not None and before_id < 1:
        raise DailyTipError("VALIDATION", "before_id 无效", status_code=400)
    params: list[object] = []
    where = ""
    if before_id is not None:
        where = "WHERE id<?"
        params.append(before_id)
    rows = db.execute(
        "SELECT id,public_id,content,content_hash,revision,created_at,updated_at "
        f"FROM daily_tips {where} ORDER BY id DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    visible = rows[:limit]
    return {
        "items": [serialize_daily_tip(row) for row in visible],
        "total": int(db.execute("SELECT COUNT(*) FROM daily_tips").fetchone()[0]),
        "next_before_id": int(visible[-1]["id"]) if has_more and visible else None,
    }


def create_daily_tip(db, content: object) -> dict:
    normalized = normalize_daily_tip(content)
    public_id = str(uuid.uuid4())
    now = _timestamp()
    try:
        with immediate_transaction(db):
            db.execute(
                "INSERT INTO daily_tips(public_id,content,content_hash,revision,created_at,updated_at) "
                "VALUES(?,?,?,1,?,?)",
                (public_id, normalized, daily_tip_hash(normalized), now, now),
            )
            row = db.execute(
                "SELECT public_id,content,content_hash,revision,created_at,updated_at "
                "FROM daily_tips WHERE public_id=?",
                (public_id,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise DailyTipError(
            "DAILY_TIP_DUPLICATE", "相同的每日提示已经存在", status_code=409
        ) from exc
    return serialize_daily_tip(row)


def _daily_tip_row(db, public_id: str):
    return db.execute(
        "SELECT public_id,content,content_hash,revision,created_at,updated_at "
        "FROM daily_tips WHERE public_id=?",
        (public_id,),
    ).fetchone()


def update_daily_tip(
    db,
    public_id: str,
    *,
    content: object,
    revision: int,
) -> dict:
    normalized = normalize_daily_tip(content)
    now = _timestamp()
    try:
        with immediate_transaction(db):
            cursor = db.execute(
                "UPDATE daily_tips SET content=?,content_hash=?,revision=revision+1,updated_at=? "
                "WHERE public_id=? AND revision=?",
                (normalized, daily_tip_hash(normalized), now, public_id, revision),
            )
            if cursor.rowcount != 1:
                latest = _daily_tip_row(db, public_id)
                if latest is None:
                    raise DailyTipError(
                        "DAILY_TIP_NOT_FOUND", "每日提示不存在", status_code=404
                    )
                raise DailyTipError(
                    "REVISION_CONFLICT",
                    "每日提示已被更新，请载入最新内容后重试",
                    status_code=409,
                    details={
                        "current_revision": int(latest["revision"]),
                        "latest": serialize_daily_tip(latest),
                    },
                )
            row = _daily_tip_row(db, public_id)
    except sqlite3.IntegrityError as exc:
        raise DailyTipError(
            "DAILY_TIP_DUPLICATE", "相同的每日提示已经存在", status_code=409
        ) from exc
    return serialize_daily_tip(row)


def delete_daily_tip(db, public_id: str, *, revision: int) -> None:
    with immediate_transaction(db):
        cursor = db.execute(
            "DELETE FROM daily_tips WHERE public_id=? AND revision=?",
            (public_id, revision),
        )
        if cursor.rowcount == 1:
            return
        latest = _daily_tip_row(db, public_id)
        if latest is None:
            raise DailyTipError(
                "DAILY_TIP_NOT_FOUND", "每日提示不存在", status_code=404
            )
        raise DailyTipError(
            "REVISION_CONFLICT",
            "每日提示已被更新，请刷新后重试",
            status_code=409,
            details={
                "current_revision": int(latest["revision"]),
                "latest": serialize_daily_tip(latest),
            },
        )


def daily_tip_for_date(db, day: date) -> str:
    rows = db.execute(
        "SELECT content FROM daily_tips ORDER BY content_hash,public_id"
    ).fetchall()
    if not rows:
        return ""
    digest = hashlib.sha256(day.isoformat().encode("ascii")).digest()
    index = int.from_bytes(digest[:8], "big") % len(rows)
    return str(rows[index]["content"])


def sample_daily_tips(
    db,
    *,
    limit: int = PUBLICATION_TIP_LIMIT,
    selection_key: object | None = None,
) -> list[dict]:
    if limit < 0:
        raise ValueError("limit must be non-negative")
    rows = db.execute(
        "SELECT public_id,content,content_hash,revision,created_at,updated_at "
        "FROM daily_tips ORDER BY content_hash,public_id"
    ).fetchall()
    if not rows or limit == 0:
        return []
    if len(rows) <= limit:
        selected = list(rows)
    else:
        identity = "|".join(str(row["content_hash"]) for row in rows)
        seed_source = f"{selection_key or uuid.uuid4()}|{identity}|{limit}"
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest(), 16)
        selected = random.Random(seed).sample(list(rows), limit)
    return [
        {
            "public_id": str(row["public_id"]),
            "content": str(row["content"]),
            "content_hash": str(row["content_hash"]),
        }
        for row in selected
    ]


def build_daily_tips_latex(tips: Sequence[Mapping[str, object]]) -> str:
    contents = [normalize_daily_tip(item.get("content")) for item in tips]
    if not contents:
        return ""
    items = "\n".join(f"\\item {content}" for content in contents)
    return "\n".join(
        (r"\section*{每日提示}", r"\begin{enumerate}", items, r"\end{enumerate}")
    )


def insert_daily_tips_before_first_problem(
    body: str,
    tips: Sequence[Mapping[str, object]],
) -> str:
    section = build_daily_tips_latex(tips)
    if not section:
        return body
    marker = r"\begin{problem}"
    index = body.find(marker)
    if index < 0:
        return f"{section}\n{body}"
    prefix = body[:index].rstrip()
    suffix = body[index:].lstrip()
    return f"{prefix}\n\n{section}\n\n{suffix}" if prefix else f"{section}\n\n{suffix}"


__all__ = [
    "DailyTipError",
    "MAX_DAILY_TIP_LENGTH",
    "build_daily_tips_latex",
    "create_daily_tip",
    "daily_tip_for_date",
    "delete_daily_tip",
    "insert_daily_tips_before_first_problem",
    "list_daily_tips",
    "normalize_daily_tip",
    "sample_daily_tips",
    "serialize_daily_tip",
    "update_daily_tip",
]
