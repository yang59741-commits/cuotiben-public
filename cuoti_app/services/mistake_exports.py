"""Create and retain PDF snapshots of selected mistakes."""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..pagination import bounded_limit, decode_cursor, encode_cursor
from ..transactions import begin_immediate
from ..validation import ValidationError, positive_int
from .daily_review import (
    DailyReviewError,
    attach_export_batch,
    load_daily_review_pdf_plan,
    publish_daily_review,
    release_daily_review,
    reserve_daily_review,
)
from .job_results import load_business_result, store_business_result, validated_job_id
from .daily_tips import build_daily_tips_latex, sample_daily_tips
from .tex import (
    TexCompileError,
    UnsafeLatexError,
    compile_latex_artifact,
    escape_latex_text,
    validate_latex_body,
)


EXPORT_RETENTION_DAYS = 30
MAX_EXPORT_MISTAKES = 100
MAX_EXPORT_BATCH_MISTAKES = 1000
EXPORT_LAYOUTS = frozenset({"compact", "notes", "two_column"})


class MistakeExportError(RuntimeError):
    def __init__(self, message: str, *, status_code: int, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def utc_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def timestamp(value: datetime) -> str:
    return utc_now(value).isoformat(timespec="seconds")


def export_expiry(created_at: datetime) -> datetime:
    return utc_now(created_at) + timedelta(days=EXPORT_RETENTION_DAYS)


def artifact_path(pdf_dir: str | Path, export_id: int) -> Path:
    if not isinstance(export_id, int) or isinstance(export_id, bool) or export_id <= 0:
        raise ValueError("错题导出 ID 无效")
    return Path(pdf_dir) / f"mistake_export_{export_id}.pdf"


def list_export_history(db, *, limit_value, after, now: str) -> dict:
    filters = {"retention_days": 30}
    limit = bounded_limit(limit_value, default=20)
    cursor = decode_cursor(after, filters=filters)
    if cursor is None:
        snapshot = db.execute(
            "SELECT COALESCE(MAX(id),0) AS n FROM mistake_export_batches "
            "WHERE artifact_status='published' AND expires_at>?",
            (now,),
        ).fetchone()["n"]
        last_id = snapshot + 1
    else:
        snapshot = cursor["s"]
        if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
            from ..pagination import CursorError

            raise CursorError("分页游标无效")
        last_id = cursor["p"][0]
    rows = db.execute(
        "SELECT * FROM mistake_export_batches WHERE artifact_status='published' "
        "AND expires_at>? AND id<=? AND id<? ORDER BY id DESC LIMIT ?",
        (now, snapshot, last_id, limit + 1),
    ).fetchall()
    has_more = len(rows) > limit
    items = rows[:limit]
    total = db.execute(
        "SELECT COUNT(*) AS n FROM mistake_export_batches WHERE artifact_status='published' "
        "AND expires_at>? AND id<=?",
        (now, snapshot),
    ).fetchone()["n"]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(
            filters=filters, snapshot=snapshot, values=[int(items[-1]["id"])]
        )
    return {
        "items": [serialize_batch(db, row) for row in items],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total": total,
    }


def published_export_batch(db, batch_id: int, *, now: str):
    batch = db.execute(
        "SELECT * FROM mistake_export_batches WHERE id=? AND artifact_status='published' "
        "AND expires_at>?",
        (batch_id, now),
    ).fetchone()
    if batch is None:
        return None, []
    volumes = db.execute(
        "SELECT * FROM mistake_exports WHERE batch_id=? AND artifact_status='published' "
        "ORDER BY volume_index",
        (batch_id,),
    ).fetchall()
    return batch, volumes


def published_export(db, export_id: int, *, now: str):
    return db.execute(
        "SELECT e.*,b.export_context,b.filter_snapshot_json FROM mistake_exports e "
        "JOIN mistake_export_batches b ON b.id=e.batch_id "
        "WHERE e.id=? AND e.artifact_status='published' AND e.expires_at>?",
        (export_id, now),
    ).fetchone()


def _display_value(value: object, fallback: str = "未填写") -> str:
    text = str(value or "").strip()
    return text or fallback


def build_export_document(
    mistakes: list[dict],
    *,
    created_at: datetime,
    layout: str = "compact",
    export_context: str = "selected_mistakes",
    review_due: bool = False,
    daily_tips: tuple[dict, ...] | list[dict] = (),
    practice_mode: str | None = None,
    pdf_purpose: str | None = None,
) -> tuple[str, list[dict]]:
    """Build a complete review booklet and its compact history snapshot."""

    if not mistakes:
        raise ValueError("至少选择一道错题")
    if len(mistakes) > MAX_EXPORT_MISTAKES:
        raise ValueError(f"一次最多导出 {MAX_EXPORT_MISTAKES} 道错题")
    if layout not in EXPORT_LAYOUTS:
        raise ValueError("PDF 布局无效")
    if export_context not in {"selected_mistakes", "daily_review"}:
        raise ValueError("PDF 导出场景无效")
    if review_due and export_context != "selected_mistakes":
        raise ValueError("间隔复习 PDF 场景无效")

    summaries = []
    sections = []
    for position, mistake in enumerate(mistakes, start=1):
        mistake_id = int(mistake["id"])
        subject = _display_value(mistake.get("subject"), "其他")
        chapter = _display_value(mistake.get("chapter"), "未分章节")
        qnum = _display_value(mistake.get("qnum"), "未填写")
        date_added = _display_value(mistake.get("date_added"), "未知日期")
        reasons = _display_value(mistake.get("reasons"))
        note = _display_value(mistake.get("note"))
        raw_content = str(mistake.get("content") or "").strip()
        if raw_content:
            content = validate_latex_body(
                raw_content,
                label=f"第 {position} 道错题正文",
                max_length=100_000,
            )
        else:
            content = r"\textit{题目内容为空}"

        summaries.append(
            {
                "mistake_id": mistake_id,
                "mistake_public_id": mistake.get("public_id"),
                "subject": subject,
                "chapter": chapter,
                "qnum": qnum,
                "date_added": date_added,
                "reasons": reasons,
                "note": note,
                "content": raw_content,
                "content_hash": hashlib.sha256(raw_content.encode("utf-8")).hexdigest(),
            }
        )
        heading = escape_latex_text(f"{position}. {subject} · {chapter}")
        type_name = str((mistake.get('classification') or {}).get('type_name') or '')
        if practice_mode == 'focused' and type_name and pdf_purpose != 'self_test':
            heading += ' · ' + escape_latex_text(type_name)
        annotations = '' if pdf_purpose == 'self_test' or (practice_mode == 'mixed' and pdf_purpose != 'guided') else (
            r"\Needspace{4\baselineskip}" + "\n" + r"\textbf{错因：}" + escape_latex_text(reasons)
            + r"\par\nopagebreak[4]\smallskip" + "\n" + r"\textbf{备注：}" + escape_latex_text(note)
            + r"\par\nopagebreak[4]\smallskip"
        )
        sections.append(
            r"\subsection*{" + heading + "}\n"
            r"\textcolor{MetaText}{\small "
            r"\textbf{题号：}" + escape_latex_text(qnum)
            + r"\quad\textbf{录入日期：}"
            + escape_latex_text(date_added)
            + r"}\par\medskip"
            + "\n"
            + r"\textbf{题目}\par\smallskip"
            + "\n"
            + content
            + r"\par\medskip"
            + "\n"
            + annotations + r"\hrule"
            + (r"\par\vspace{4cm}\hrule\vspace{0.8em}" if layout == "notes" else r"\vspace{0.8em}")
            + "\n"
        )

    local_created = utc_now(created_at).astimezone()
    generated_text = escape_latex_text(
        local_created.strftime("%Y-%m-%d %H:%M:%S")
    )
    count_text = escape_latex_text(f"共 {len(mistakes)} 道错题")
    document_options = "10pt, a4paper, twocolumn" if layout == "two_column" else "12pt, a4paper, oneside"
    document_title = (
        "每日自主复习册"
        if export_context == "daily_review"
        else ("间隔复习册" if review_due else "错题复习册")
    )
    daily_tip_section = build_daily_tips_latex(daily_tips)
    if daily_tip_section:
        daily_tip_section += "\n\n"
    latex = "\\documentclass[" + document_options + r"""]{ctexart}
\usepackage{amsmath, amsthm, amssymb, bm, mathrsfs}
\usepackage[top=2cm, bottom=2cm, left=2cm, right=2cm]{geometry}
\usepackage{xcolor}
\usepackage{enumitem}
\usepackage{needspace}
\usepackage{fancyhdr}
\definecolor{Accent}{RGB}{107,78,52}
\definecolor{MetaText}{RGB}{117,99,85}
\setlist{leftmargin=2em,itemsep=4pt,topsep=4pt}
\linespread{1.35}
\setlength{\parindent}{0pt}
\raggedbottom
\sloppy
\title{\textcolor{Accent}{""" + escape_latex_text(document_title) + r"""}}
\author{错题本}
\date{}
\begin{document}
\pagestyle{fancy}
\fancyhf{}
\fancyfoot[C]{\thepage}
\renewcommand{\headrulewidth}{0pt}
\maketitle
\vspace{-1.2cm}
\begin{center}
\textcolor{MetaText}{生成时间：""" + generated_text + r"""\quad """ + count_text + r"""}
\end{center}
\vspace{0.5cm}

""" + daily_tip_section + "\n".join(sections) + r"""
\end{document}
"""
    return latex, summaries


def serialize_items(items: list[dict]) -> str:
    return json.dumps(items, ensure_ascii=False, separators=(",", ":"))


def parse_items(raw: object) -> list[dict]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def daily_review_download_stem(row) -> str:
    try:
        snapshot = json.loads(row["filter_snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot = {}
    created_at = datetime.fromisoformat(row["created_at"])
    day = created_at.astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    round_number = snapshot.get("daily_review_round_number")
    round_label = f"_第{int(round_number)}轮" if round_number is not None else ""
    batch_id = row["batch_id"] if "batch_id" in row.keys() else row["id"]
    return f"每日自主复习_{day}{round_label}_批次{int(batch_id)}"


def serialize_export(row) -> dict:
    day = str(row["created_at"] or "")[:10].replace("-", "") or "export"
    export_id = int(row["id"])
    return {
        "id": export_id,
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "item_count": int(row["item_count"]),
        "items": parse_items(row["items_json"]),
        "download_url": f"/mistake-exports/{export_id}.pdf",
        "download_name": f"错题复习_{day}_{export_id}.pdf",
        "volume_index": int(row["volume_index"] or 1),
        "layout": row["layout"] or "compact",
    }


def serialize_batch(db, row) -> dict:
    volumes = [
        serialize_export(volume)
        for volume in db.execute(
            "SELECT * FROM mistake_exports WHERE batch_id=? AND artifact_status='published' "
            "ORDER BY volume_index",
            (row["id"],),
        ).fetchall()
    ]
    items = [
        dict(item)
        for item in db.execute(
            "SELECT i.mistake_public_id,i.subject,i.chapter,i.qnum,i.date_added,"
            "i.reasons,i.note,i.content,i.content_hash,e.volume_index,i.position "
            "FROM mistake_export_items i JOIN mistake_exports e ON e.id=i.export_id "
            "WHERE e.batch_id=? ORDER BY e.volume_index,i.position",
            (row["id"],),
        ).fetchall()
    ]
    batch_id = int(row["id"])
    export_context = row["export_context"] or "selected_mistakes"
    try:
        filter_snapshot = json.loads(row["filter_snapshot_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        filter_snapshot = {}
    snapshot_filters = filter_snapshot.get("filters") or {}
    review_due = export_context == "selected_mistakes" and snapshot_filters.get(
        "review_due"
    ) is True
    prefix = (
        "每日自主复习"
        if export_context == "daily_review"
        else ("间隔复习" if review_due else "错题复习")
    )
    if export_context == "daily_review" or review_due:
        for volume in volumes:
            volume["download_name"] = f"{prefix}_第{int(volume['volume_index'])}册.pdf"
    result = {
        "id": batch_id,
        "public_id": row["public_id"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "item_count": int(row["total_count"]),
        "volume_count": int(row["volume_count"]),
        "layout": row["layout"],
        "export_context": export_context,
        "context_label": prefix,
        "resolved_review_date": snapshot_filters.get("review_date"),
        "filter_snapshot": filter_snapshot,
        "items": items,
        "volumes": volumes,
        "download_url": f"/mistake-export-batches/{batch_id}.zip",
        "download_name": f"{prefix}批次_{batch_id}.zip",
    }
    if export_context == "daily_review":
        result.update(
            {
                "completion_status": "not_tracked",
                "completed_count": None,
            }
        )
        reexport_session_id = filter_snapshot.get("reexport_session_id")
        if reexport_session_id is not None:
            round_row = db.execute(
                "SELECT * FROM daily_review_rounds WHERE id=?", (reexport_session_id,)
            ).fetchone()
            result.update({
                "is_reexport": True,
                "source_session_id": reexport_session_id,
                "legacy_live_content": bool(filter_snapshot.get("legacy_live_content")),
            })
        else:
            round_row = db.execute(
                "SELECT r.* FROM daily_review_rounds r "
                "JOIN daily_review_items i ON i.round_id=r.id "
                "WHERE i.export_batch_id=? ORDER BY r.id DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        if round_row is not None:
            scope = "round_id" if reexport_session_id is not None else "export_batch_id"
            result_count = db.execute(
                "SELECT COUNT(*) AS total,"
                "SUM(CASE WHEN completion_result IS NOT NULL THEN 1 ELSE 0 END) AS completed "
                f"FROM daily_review_items WHERE {scope}=? AND status='published'",
                (reexport_session_id if reexport_session_id is not None else batch_id,),
            ).fetchone()
            completed = int(result_count["completed"] or 0)
            total = int(result_count["total"] or 0)
            result.update(
                {
                    "daily_review_round_id": int(round_row["id"]),
                    "round_number": int(round_row["round_number"]),
                    "completion_status": (
                        "completed" if total and completed == total else "in_progress"
                    ),
                    "completed_count": completed,
                }
            )
    if export_context == "daily_review":
        stem = daily_review_download_stem(row)
        result["download_name"] = f"{stem}.zip"
        for volume in volumes:
            volume["download_name"] = f"{stem}_第{volume['volume_index']}册.pdf"
    return result


def _validated_export_payload(
    payload: dict,
) -> tuple[list[int] | None, dict, str, str | None, str]:
    export_context = str(payload.get("export_context") or "selected_mistakes")
    if export_context not in {"selected_mistakes", "daily_review"}:
        raise ValidationError("导出场景无效")
    if "daily_review_session_id" in payload:
        if export_context != "daily_review":
            raise ValidationError("历史批次导出必须使用每日自主复习场景")
        session_id = payload["daily_review_session_id"]
        if isinstance(session_id, bool) or not isinstance(session_id, int) or session_id <= 0:
            raise ValidationError("历史批次编号无效")
        if "selection" in payload or "pdf_purpose" in payload:
            raise ValidationError("历史批次导出沿用原题目与用途，不接受重新选题或修改用途")
    raw_ids = payload.get("mistake_ids")
    filters = payload.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValidationError("filters 必须是对象")
    review_due = filters.get("review_due")
    if "review_due" in filters and review_due is not True:
        raise ValidationError("间隔复习筛选标记无效")
    ids = None
    if export_context == "daily_review":
        if raw_ids is not None:
            raise ValidationError("每日自主复习导出不接受显式错题 ID")
        if filters:
            raise ValidationError("每日自主复习由轮询规则自动选题，不接受筛选条件")
    elif review_due:
        if raw_ids is not None:
            raise ValidationError("间隔复习导出不接受显式错题 ID")
        unexpected = set(filters) - {"review_due", "subject_id", "chapter_id"}
        if unexpected:
            raise ValidationError("间隔复习导出只接受科目和章节筛选")
    elif raw_ids is not None:
        if not isinstance(raw_ids, list) or not raw_ids:
            raise ValidationError("至少选择一道错题")
        if len(raw_ids) > MAX_EXPORT_BATCH_MISTAKES:
            raise ValidationError(f"一次最多导出 {MAX_EXPORT_BATCH_MISTAKES} 道错题")
        ids = [positive_int(value, "错题 ID") for value in raw_ids]
        if len(set(ids)) != len(ids):
            raise ValidationError("错题 ID 不能重复")
    elif not filters:
        raise ValidationError("请选择错题或提供筛选条件")
    layout = str(payload.get("layout") or "compact")
    if layout not in EXPORT_LAYOUTS:
        raise ValidationError("PDF 布局无效")
    normalized_filters = {}
    for key in ("subject_id", "chapter_id"):
        if filters.get(key) not in (None, ""):
            normalized_filters[key] = positive_int(filters[key], key)
    if review_due:
        normalized_filters["review_due"] = True
        normalized_filters["review_date"] = datetime.now().astimezone().date().isoformat()
    else:
        status = str(filters.get("status") or "").strip()
        if status:
            if status not in {"未掌握", "已掌握"}:
                raise ValidationError("状态筛选无效")
            normalized_filters["status"] = status
        for key in ("date_from", "date_to"):
            value = str(filters.get(key) or "").strip()
            if value:
                if len(value) != 10:
                    raise ValidationError("日期筛选无效")
                normalized_filters[key] = value
        tags = filters.get("tags") or []
        if tags:
            if not isinstance(tags, list) or len(tags) > 50:
                raise ValidationError("标签筛选无效")
            normalized_filters["tags"] = [
                str(tag).strip().casefold() for tag in tags if str(tag).strip()
            ]
    return (
        ids,
        normalized_filters,
        layout,
        validated_job_id(payload.get("_job_id")),
        export_context,
    )


def _selected_rows(db, ids, filters):
    where = ["m.deleted_at IS NULL"]
    params = []
    if ids is not None:
        where.append("m.id IN ({})".format(",".join("?" for _ in ids)))
        params.extend(ids)
    for key in ("subject_id", "chapter_id", "status"):
        if key in filters:
            where.append(f"m.{key}=?")
            params.append(filters[key])
    if filters.get("review_due"):
        where.extend(
            (
                "m.status='未掌握'",
                "(m.next_review IS NULL OR m.next_review<=?)",
            )
        )
        params.append(filters["review_date"])
    if filters.get("date_from"):
        where.append("m.date_added>=?")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("m.date_added<=?")
        params.append(filters["date_to"])
    for tag in filters.get("tags") or []:
        where.append(
            "EXISTS(SELECT 1 FROM mistake_tags mt JOIN tags t ON t.id=mt.tag_id "
            "WHERE mt.mistake_id=m.id AND t.normalized_name=?)"
        )
        params.append(tag)
    order = (
        "m.review_cycle DESC,COALESCE(m.next_review,''),m.id"
        if filters.get("review_due")
        else "m.date_added DESC,m.id DESC"
    )
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON s.id=m.subject_id LEFT JOIN chapters c ON c.id=m.chapter_id "
            "WHERE " + " AND ".join(where) + f" ORDER BY {order} LIMIT ?",
            (*params, MAX_EXPORT_BATCH_MISTAKES + 1),
        ).fetchall()
    ]
    if len(rows) > MAX_EXPORT_BATCH_MISTAKES:
        raise ValidationError(f"筛选结果超过 {MAX_EXPORT_BATCH_MISTAKES} 道，请缩小范围")
    if ids is not None and len(rows) != len(ids):
        raise ValidationError("部分所选错题不存在，请刷新列表后重试")
    if not rows:
        raise ValidationError("筛选结果没有可导出的错题")
    return rows


def _discard_failed_batch(
    db,
    pdf_dir: str | Path,
    batch_id: int,
    export_ids: list[int],
    warn,
    *,
    daily_round_id: int | None = None,
    daily_state: str = "failed",
    daily_source_job_id: str | None = None,
) -> None:
    for export_id in export_ids:
        path = artifact_path(pdf_dir, export_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            warn("无法删除失败的错题导出文件 %s", path.name)
    try:
        begin_immediate(db)
        db.execute("DELETE FROM mistake_exports WHERE batch_id=?", (batch_id,))
        db.execute("DELETE FROM mistake_export_batches WHERE id=?", (batch_id,))
        if daily_round_id is not None:
            release_daily_review(
                db,
                daily_round_id,
                state=daily_state,
                source_job_id=daily_source_job_id,
            )
        db.commit()
    except Exception:
        db.rollback()
        warn("无法删除失败的错题导出记录 batch_id=%s", batch_id)


def create_mistake_export(
    db,
    *,
    pdf_dir: str | Path,
    xelatex_command: str,
    payload: dict,
    timeout: int = 60,
    progress: Callable[[int, int, str], None] | None = None,
    compiler=None,
    runner=None,
    cancel_check: Callable[[], None] | None = None,
    cancelled_errors: tuple[type[BaseException], ...] = (),
    warn: Callable[..., None] | None = None,
) -> tuple[dict, bool]:
    """Freeze, compile, publish, and idempotently record one export batch."""

    progress = progress or (lambda _current, _total, _message: None)
    cancel_check = cancel_check or (lambda: None)
    warn = warn or (lambda *_args: None)
    compiler = compiler or compile_latex_artifact
    daily_plan = None
    daily_round_id: int | None = None
    reexport_session_id = payload.get("daily_review_session_id")

    def release_pending_daily(state: str = "failed") -> None:
        if daily_round_id is None:
            return
        if db.in_transaction:
            db.rollback()
        try:
            release_daily_review(
                db,
                daily_round_id,
                state=state,
                source_job_id=source_job_id,
            )
            db.commit()
        except Exception:
            if db.in_transaction:
                db.rollback()
            warn("无法释放每日复习占位 round_id=%s", daily_round_id)

    try:
        mistake_ids, filters, layout, source_job_id, export_context = _validated_export_payload(payload)
        pdf_purpose = payload.get('pdf_purpose') or (payload.get('selection') or {}).get('pdf_purpose')
        if pdf_purpose not in (None, 'self_test', 'guided'):
            raise ValidationError('PDF 用途无效')
        replay = load_business_result(db, source_job_id, "mistake-export")
    except ValidationError as exc:
        raise MistakeExportError(str(exc), status_code=400, code="VALIDATION") from exc
    except ValueError as exc:
        raise MistakeExportError(str(exc), status_code=409, code="OPERATION_CONFLICT") from exc
    if replay is not None:
        return {**replay, "idempotency_replayed": True}, True

    cancel_check()
    progress(1, 4, "输入校验完成")
    try:
        if export_context == "daily_review":
            if reexport_session_id is not None:
                daily_plan = load_daily_review_pdf_plan(db, reexport_session_id)
                pdf_purpose = (daily_plan.selection or {}).get("pdf_purpose")
            else:
                # BEGIN IMMEDIATE prevents new sessions from reserving the
                # same mistake in one coverage round.
                daily_plan = reserve_daily_review(db, source_job_id=source_job_id, selection=payload.get('selection'))
                daily_round_id = daily_plan.round_id
            rows = []
            for plan_row in daily_plan.rows:
                document_row = dict(plan_row)
                document_row["daily_review_item_id"] = int(document_row["id"])
                document_row["id"] = int(document_row["mistake_id"])
                rows.append(document_row)
        else:
            rows = _selected_rows(db, mistake_ids, filters)
        created = datetime.now(timezone.utc)
        daily_tips = sample_daily_tips(
            db,
            selection_key=source_job_id or uuid.uuid4().hex,
        )
        volumes = [
            rows[index : index + MAX_EXPORT_MISTAKES]
            for index in range(0, len(rows), MAX_EXPORT_MISTAKES)
        ]
        documents = [
            build_export_document(
                volume,
                created_at=created,
                layout=layout,
                export_context=export_context,
                review_due=bool(filters.get("review_due")),
                daily_tips=daily_tips,
                practice_mode=daily_plan.practice_mode if daily_plan else None,
                pdf_purpose=pdf_purpose,
            )
            for volume in volumes
        ]
    except DailyReviewError as exc:
        raise MistakeExportError(
            str(exc), status_code=exc.status, code=exc.code
        ) from exc
    except ValidationError as exc:
        raise MistakeExportError(str(exc), status_code=400, code="VALIDATION") from exc
    except (ValueError, UnsafeLatexError) as exc:
        release_pending_daily("failed")
        raise MistakeExportError(str(exc), status_code=400, code="UNSAFE_LATEX") from exc
    except BaseException as exc:
        release_pending_daily(
            "cancelled"
            if cancelled_errors and isinstance(exc, cancelled_errors)
            else "failed"
        )
        if cancelled_errors and isinstance(exc, cancelled_errors):
            raise
        raise MistakeExportError("冻结每日复习内容失败", status_code=500) from exc
    try:
        progress(2, 4, "导出内容已冻结")
        cancel_check()
    except BaseException as exc:
        release_pending_daily(
            "cancelled"
            if cancelled_errors and isinstance(exc, cancelled_errors)
            else "failed"
        )
        if cancelled_errors and isinstance(exc, cancelled_errors):
            raise
        raise

    created_at = timestamp(created)
    expires_at = timestamp(export_expiry(created))
    export_ids: list[int] = []
    try:
        snapshot = {
            "export_context": export_context,
            "filters": filters,
            "explicit_ids": mistake_ids,
            "daily_tips": daily_tips,
            "pdf_purpose": pdf_purpose,
            "resolved": [
                {
                    "public_id": row.get("public_id"),
                    "content_hash": hashlib.sha256(
                        str(row.get("content") or "").encode()
                    ).hexdigest(),
                }
                for row in rows
            ],
        }
        if daily_plan is not None:
            snapshot["daily_review_round_id"] = daily_plan.round_id
            snapshot["daily_review_round_number"] = daily_plan.round_number
            snapshot["daily_review_item_ids"] = list(daily_plan.item_ids)
            snapshot['practice_mode'] = daily_plan.practice_mode
            snapshot['selection'] = daily_plan.selection
        if reexport_session_id is not None:
            # Link the new artifact without replacing the original items' export
            # association or entering the reservation/publication lifecycle.
            snapshot["reexport_session_id"] = reexport_session_id
            snapshot["legacy_live_content"] = any(row.get("legacy_live_content") for row in rows)
        batch_cursor = db.execute(
            "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,total_count,"
            "volume_count,layout,filter_snapshot_json,source_job_id,artifact_status,"
            "export_context) VALUES(?,?,?,?,?,?,?,?, 'pending',?)",
            (
                str(uuid.uuid4()), created_at, expires_at, len(rows), len(volumes), layout,
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), source_job_id,
                export_context,
            ),
        )
        batch_id = int(batch_cursor.lastrowid)
        for volume_index, (_latex, summaries) in enumerate(documents, start=1):
            cursor = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,"
                "artifact_status,batch_id,volume_index,layout) VALUES(?,?,?,?, 'pending',?,?,?)",
                (
                    created_at, expires_at, len(summaries), serialize_items(summaries),
                    batch_id, volume_index, layout,
                ),
            )
            export_id = int(cursor.lastrowid)
            export_ids.append(export_id)
            db.executemany(
                "INSERT INTO mistake_export_items(export_id,position,mistake_public_id,"
                "subject,chapter,qnum,date_added,reasons,note,content,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        export_id, position, item.get("mistake_public_id"), item["subject"],
                        item["chapter"], item["qnum"], item["date_added"], item["reasons"],
                        item["note"], item["content"], item["content_hash"],
                    )
                    for position, item in enumerate(summaries, start=1)
                ],
            )
        if daily_round_id is not None:
            attach_export_batch(
                db,
                daily_round_id,
                batch_id,
                source_job_id=source_job_id,
                now=created,
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        release_pending_daily("failed")
        raise MistakeExportError(
            "创建错题导出记录失败，数据未更改", status_code=500
        ) from exc

    try:
        progress(3, 4, "正在编译 PDF")

        def compile_volume(entry):
            (latex, _summaries), export_id = entry
            cancel_check()
            compiler(
                pdf_dir,
                xelatex_command,
                f"mistake_export_{export_id}",
                latex,
                timeout=timeout,
                runner=runner,
                publish_source=False,
            )
            cancel_check()
            return export_id

        entries = list(zip(documents, export_ids))
        with ThreadPoolExecutor(max_workers=min(2, len(entries))) as executor:
            futures = [
                executor.submit(copy_context().run, compile_volume, entry)
                for entry in entries
            ]
            for completed, future in enumerate(as_completed(futures), start=1):
                future.result()
                cancel_check()
                progress(
                    3,
                    4,
                    f"已编译 {completed}/{len(export_ids)} 册",
                )
        cancel_check()
        db.execute(
            "UPDATE mistake_exports SET artifact_status='published' WHERE batch_id=?",
            (batch_id,),
        )
        db.execute(
            "UPDATE mistake_export_batches SET artifact_status='published' WHERE id=?",
            (batch_id,),
        )
        if daily_round_id is not None:
            publish_daily_review(
                db,
                daily_round_id,
                batch_id,
                source_job_id=source_job_id,
                now=created,
            )
        batch_row = db.execute(
            "SELECT * FROM mistake_export_batches WHERE id=?", (batch_id,)
        ).fetchone()
        result = serialize_batch(db, batch_row)
        store_business_result(db, source_job_id, "mistake-export", result)
        db.commit()
    except (TexCompileError, UnsafeLatexError) as exc:
        db.rollback()
        _discard_failed_batch(
            db,
            pdf_dir,
            batch_id,
            export_ids,
            warn,
            daily_round_id=daily_round_id,
            daily_state="failed",
            daily_source_job_id=source_job_id,
        )
        raise MistakeExportError(
            f"PDF 生成失败：{str(exc)[:2000]}", status_code=500
        ) from exc
    except Exception as exc:
        db.rollback()
        daily_state = (
            "cancelled"
            if cancelled_errors and isinstance(exc, cancelled_errors)
            else "failed"
        )
        _discard_failed_batch(
            db,
            pdf_dir,
            batch_id,
            export_ids,
            warn,
            daily_round_id=daily_round_id,
            daily_state=daily_state,
            daily_source_job_id=source_job_id,
        )
        if cancelled_errors and isinstance(exc, cancelled_errors):
            raise
        raise MistakeExportError("PDF 生成失败，未保留导出记录", status_code=500) from exc

    return result, False


def cleanup_exports(
    db,
    pdf_dir: str | Path,
    *,
    now: datetime | None = None,
    include_pending: bool = False,
) -> dict[str, int]:
    """Remove expired exports with reversible file staging."""

    cutoff = timestamp(utc_now(now))
    where = "(artifact_status='published' AND expires_at<=?)"
    params: list[object] = [cutoff]
    if include_pending:
        where += " OR artifact_status='pending'"
    rows = db.execute(
        f"SELECT id,batch_id FROM mistake_exports WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    if not rows:
        return {"deleted_records": 0, "deleted_files": 0}

    output_dir = Path(pdf_dir).resolve()
    staging_dir = (output_dir / f".delete-mistake-exports-{uuid.uuid4().hex}").resolve()
    try:
        staging_dir.relative_to(output_dir)
    except ValueError as exc:
        raise OSError("错题导出清理目录无效") from exc

    moved: list[tuple[Path, Path]] = []
    export_ids = [int(row["id"]) for row in rows]
    batch_ids = sorted({int(row["batch_id"]) for row in rows if row["batch_id"]})
    interrupted_daily_round_ids: list[int] = []
    if include_pending and batch_ids:
        batch_placeholders = ",".join("?" for _ in batch_ids)
        interrupted_daily_round_ids = [
            int(row["round_id"])
            for row in db.execute(
                "SELECT DISTINCT i.round_id FROM daily_review_items i "
                "JOIN mistake_export_batches b ON b.id=i.export_batch_id "
                f"WHERE b.id IN ({batch_placeholders}) "
                "AND b.artifact_status='pending' AND i.status='reserved'",
                batch_ids,
            ).fetchall()
        ]
    try:
        begin_immediate(db)
        for export_id in export_ids:
            source = artifact_path(output_dir, export_id).resolve()
            try:
                source.relative_to(output_dir)
            except ValueError as exc:
                raise OSError("错题导出文件路径无效") from exc
            if not source.is_file():
                continue
            staging_dir.mkdir(parents=True, exist_ok=True)
            destination = staging_dir / source.name
            os.replace(source, destination)
            moved.append((source, destination))
        for round_id in interrupted_daily_round_ids:
            release_daily_review(db, round_id, state="failed")
        placeholders = ",".join("?" for _ in export_ids)
        db.execute(
            f"DELETE FROM mistake_exports WHERE id IN ({placeholders})",
            export_ids,
        )
        if batch_ids:
            batch_placeholders = ",".join("?" for _ in batch_ids)
            db.execute(
                f"DELETE FROM mistake_export_batches WHERE id IN ({batch_placeholders})",
                batch_ids,
            )
        db.commit()
    except Exception:
        db.rollback()
        for source, destination in reversed(moved):
            if destination.exists():
                os.replace(destination, source)
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        raise

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    return {
        "deleted_records": len(export_ids),
        "deleted_files": len(moved),
    }
