"""Selected-mistake PDF export and retained download history."""

from __future__ import annotations

import json
import hashlib
import tempfile
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, abort, current_app, jsonify, request, send_file

from ..database import get_db
from ..pagination import CursorError, bounded_limit, decode_cursor, encode_cursor
from ..services.mistake_exports import (
    MAX_EXPORT_MISTAKES,
    MAX_EXPORT_BATCH_MISTAKES,
    EXPORT_LAYOUTS,
    artifact_path,
    build_export_document,
    cleanup_exports,
    create_mistake_export,
    daily_review_download_stem,
    MistakeExportError,
    export_expiry,
    serialize_batch as serialize_batch_service,
    serialize_items,
    timestamp,
    utc_now,
)
from ..services.job_progress import report_job_progress
from ..services.job_results import (
    load_business_result,
    store_business_result,
    validated_job_id,
)
from ..services.tex import TexCompileError, UnsafeLatexError, compile_latex_artifact
from ..validation import ValidationError, json_object, positive_int


bp = Blueprint("mistake_exports", __name__)


def _export_prefix(row) -> str:
    context = (
        row["export_context"]
        if hasattr(row, "keys") and "export_context" in row.keys()
        else "selected_mistakes"
    )
    if context == "daily_review":
        return "每日自主复习"
    raw_snapshot = (
        row["filter_snapshot_json"]
        if hasattr(row, "keys") and "filter_snapshot_json" in row.keys()
        else "{}"
    )
    try:
        snapshot = json.loads(raw_snapshot or "{}")
    except (TypeError, json.JSONDecodeError):
        snapshot = {}
    if (snapshot.get("filters") or {}).get("review_due") is True:
        return "间隔复习"
    return "错题复习"


def _download_name(row) -> str:
    if row["export_context"] == "daily_review":
        return f"{daily_review_download_stem(row)}_第{int(row['volume_index'])}册.pdf"
    day = str(row["created_at"] or "")[:10].replace("-", "") or "export"
    return f"{_export_prefix(row)}_{day}_{int(row['id'])}.pdf"


def _serialize_batch(db, row) -> dict:
    return serialize_batch_service(db, row)


def _cleanup(*, include_pending: bool = False) -> None:
    try:
        cleanup_exports(
            get_db(),
            current_app.config["PDF_DIR"],
            include_pending=include_pending,
        )
    except Exception:
        current_app.logger.exception("错题 PDF 导出清理失败，将在后续请求重试")


def cleanup_on_startup() -> None:
    """Remove interrupted and expired exports before the server starts."""

    cleanup_exports(
        get_db(),
        current_app.config["PDF_DIR"],
        include_pending=True,
    )


def _validated_request() -> tuple[list[int] | None, dict, str, str | None]:
    data = json_object(request)
    raw_ids = data.get("mistake_ids")
    filters = data.get("filters") or {}
    if not isinstance(filters, dict):
        raise ValidationError("filters 必须是对象")
    review_due = filters.get("review_due")
    if "review_due" in filters and review_due is not True:
        raise ValidationError("间隔复习筛选标记无效")
    ids = None
    if review_due:
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
    layout = str(data.get("layout") or "compact")
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
    return ids, normalized_filters, layout, validated_job_id(data.get("_job_id"))


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
        where.append("m.date_added>=?"); params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("m.date_added<=?"); params.append(filters["date_to"])
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
        dict(row) for row in db.execute(
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


def _discard_failed_export(db, export_id: int) -> None:
    path = artifact_path(current_app.config["PDF_DIR"], export_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        current_app.logger.warning("无法删除失败的错题导出文件 %s", path.name)
    try:
        db.execute("DELETE FROM mistake_exports WHERE id=?", (export_id,))
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("无法删除失败的错题导出记录 %s", export_id)


@bp.post("/internal/jobs/mistake-exports")
def api_create_mistake_export():
    _cleanup()
    raw_payload = request.get_json(silent=True)
    if isinstance(raw_payload, dict) and raw_payload.get("export_context") == "daily_review":
        try:
            result, _replayed = create_mistake_export(
                get_db(),
                pdf_dir=current_app.config["PDF_DIR"],
                xelatex_command=current_app.config["XELATEX_COMMAND"],
                payload=raw_payload,
                timeout=current_app.config.get("XELATEX_TIMEOUT", 60),
                compiler=compile_latex_artifact,
            )
            return jsonify(result), 200 if result.get("idempotency_replayed") else 201
        except MistakeExportError as exc:
            return jsonify({"error": str(exc), "code": exc.code or "EXPORT_FAILED"}), exc.status_code
    try:
        mistake_ids, filters, layout, source_job_id = _validated_request()
    except (ValidationError, ValueError) as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400

    db = get_db()
    try:
        replay = load_business_result(db, source_job_id, "mistake-export")
    except ValueError as exc:
        return jsonify({"error": str(exc), "code": "OPERATION_CONFLICT"}), 409
    if replay is not None:
        return jsonify({**replay, "idempotency_replayed": True}), 200
    report_job_progress(1, 4, "输入校验完成")
    try:
        rows = _selected_rows(db, mistake_ids, filters)
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400

    created = datetime.now(timezone.utc)
    volumes = [rows[index:index + MAX_EXPORT_MISTAKES] for index in range(0, len(rows), MAX_EXPORT_MISTAKES)]
    documents = []
    try:
        for volume in volumes:
            documents.append(
                build_export_document(
                    volume,
                    created_at=created,
                    layout=layout,
                    review_due=bool(filters.get("review_due")),
                )
            )
    except (ValueError, UnsafeLatexError) as exc:
        return jsonify({"error": str(exc), "code": "UNSAFE_LATEX"}), 400
    report_job_progress(2, 4, "导出内容已冻结")

    created_at = timestamp(created)
    expires_at = timestamp(export_expiry(created))
    export_ids = []
    try:
        snapshot = {
            "filters": filters,
            "explicit_ids": mistake_ids,
            "resolved": [
                {"public_id": row.get("public_id"), "content_hash": hashlib.sha256(str(row.get("content") or "").encode()).hexdigest()}
                for row in rows
            ],
        }
        batch_cursor = db.execute(
            "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,total_count,"
            "volume_count,layout,filter_snapshot_json,source_job_id,artifact_status) "
            "VALUES(?,?,?,?,?,?,?,?, 'pending')",
            (str(uuid.uuid4()), created_at, expires_at, len(rows), len(volumes), layout,
             json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), source_job_id),
        )
        batch_id = int(batch_cursor.lastrowid)
        for volume_index, (_latex, summaries) in enumerate(documents, start=1):
            cursor = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,"
                "artifact_status,batch_id,volume_index,layout) VALUES(?,?,?,?, 'pending',?,?,?)",
                (created_at, expires_at, len(summaries), serialize_items(summaries), batch_id, volume_index, layout),
            )
            export_id = int(cursor.lastrowid); export_ids.append(export_id)
            db.executemany(
                "INSERT INTO mistake_export_items(export_id,position,mistake_public_id,"
                "subject,chapter,qnum,date_added,reasons,note,content,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (export_id, position, item.get("mistake_public_id"), item["subject"], item["chapter"],
                     item["qnum"], item["date_added"], item["reasons"], item["note"], item["content"], item["content_hash"])
                    for position, item in enumerate(summaries, start=1)
                ],
            )
        db.commit()
    except Exception:
        db.rollback()
        current_app.logger.exception("无法创建错题导出记录")
        return jsonify({"error": "创建错题导出记录失败，数据未更改"}), 500

    try:
        report_job_progress(3, 4, "正在编译 PDF")
        pdf_dir = current_app.config["PDF_DIR"]
        xelatex_command = current_app.config["XELATEX_COMMAND"]
        timeout = current_app.config.get("XELATEX_TIMEOUT", 60)

        def compile_volume(entry):
            (latex, _summaries), export_id = entry
            compile_latex_artifact(
                pdf_dir, xelatex_command, f"mistake_export_{export_id}", latex,
                timeout=timeout, publish_source=False,
            )
            return export_id

        entries = list(zip(documents, export_ids))
        with ThreadPoolExecutor(max_workers=min(2, len(entries))) as executor:
            futures = [
                executor.submit(copy_context().run, compile_volume, entry)
                for entry in entries
            ]
            for completed, future in enumerate(as_completed(futures), start=1):
                future.result()
                report_job_progress(
                    2 + completed, 2 + len(export_ids),
                    f"已编译 {completed}/{len(export_ids)} 册",
                )
        db.execute(
            "UPDATE mistake_exports SET artifact_status='published' WHERE batch_id=?", (batch_id,)
        )
        db.execute(
            "UPDATE mistake_export_batches SET artifact_status='published' WHERE id=?", (batch_id,)
        )
        batch_row = db.execute("SELECT * FROM mistake_export_batches WHERE id=?", (batch_id,)).fetchone()
        result = _serialize_batch(db, batch_row)
        store_business_result(db, source_job_id, "mistake-export", result)
        db.commit()
    except (TexCompileError, UnsafeLatexError) as exc:
        db.rollback()
        for export_id in export_ids:
            _discard_failed_export(db, export_id)
        db.execute("DELETE FROM mistake_export_batches WHERE id=?", (batch_id,)); db.commit()
        return jsonify({"error": f"PDF 生成失败：{str(exc)[:2000]}"}), 500
    except Exception:
        db.rollback()
        for export_id in export_ids:
            _discard_failed_export(db, export_id)
        db.execute("DELETE FROM mistake_export_batches WHERE id=?", (batch_id,)); db.commit()
        current_app.logger.exception("错题 PDF 导出失败")
        return jsonify({"error": "PDF 生成失败，未保留导出记录"}), 500

    report_job_progress(2 + len(export_ids), 2 + len(export_ids), "全部分册已发布")
    return jsonify(result), 201


@bp.route("/api/v2/mistake-exports")
def api_mistake_export_history():
    _cleanup()
    db = get_db()
    filters = {"retention_days": 30}
    now = timestamp(utc_now())
    try:
        limit = bounded_limit(request.args.get("limit"), default=20)
        cursor = decode_cursor(request.args.get("after"), filters=filters)
    except CursorError as exc:
        return jsonify({"error": str(exc), "code": "INVALID_CURSOR"}), 400

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
            return jsonify({"error": "分页游标无效", "code": "INVALID_CURSOR"}), 400
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
            filters=filters,
            snapshot=snapshot,
            values=[int(items[-1]["id"])],
        )
    return jsonify(
        {
            "items": [_serialize_batch(db, row) for row in items],
            "next_cursor": next_cursor,
            "has_more": has_more,
            "total": total,
        }
    )


@bp.route("/mistake-export-batches/<int:batch_id>.zip")
def download_mistake_export_batch(batch_id: int):
    _cleanup()
    now = timestamp(utc_now())
    db = get_db()
    batch = db.execute(
        "SELECT * FROM mistake_export_batches WHERE id=? AND artifact_status='published' "
        "AND expires_at>?",
        (batch_id, now),
    ).fetchone()
    if not batch:
        abort(404)
    volumes = db.execute(
        "SELECT * FROM mistake_exports WHERE batch_id=? AND artifact_status='published' "
        "ORDER BY volume_index", (batch_id,)
    ).fetchall()
    prefix = _export_prefix(batch)
    payload = _serialize_batch(db, batch)
    download_names = {volume["id"]: volume["download_name"] for volume in payload["volumes"]}
    handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    output_path = Path(handle.name)
    handle.close()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for volume in volumes:
            path = artifact_path(current_app.config["PDF_DIR"], int(volume["id"]))
            if not path.is_file():
                abort(404)
            name = (download_names[int(volume["id"])] if batch["export_context"] == "daily_review"
                    else f"{prefix}_第{int(volume['volume_index'])}册.pdf")
            archive.write(path, name)
        archive.writestr(
            "manifest.json",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
    response = send_file(
        output_path, mimetype="application/zip", as_attachment=True,
        download_name=payload["download_name"],
    )
    response.call_on_close(lambda: output_path.unlink(missing_ok=True))
    return response


@bp.route("/mistake-exports/<int:export_id>.pdf")
def download_mistake_export(export_id: int):
    _cleanup()
    now = timestamp(utc_now())
    row = get_db().execute(
        "SELECT e.*,b.export_context,b.filter_snapshot_json FROM mistake_exports e "
        "LEFT JOIN mistake_export_batches b ON b.id=e.batch_id "
        "WHERE e.id=? AND e.artifact_status='published' AND e.expires_at>?",
        (export_id, now),
    ).fetchone()
    if not row:
        abort(404)
    path = artifact_path(current_app.config["PDF_DIR"], export_id).resolve()
    output_dir = Path(current_app.config["PDF_DIR"]).resolve()
    try:
        path.relative_to(output_dir)
    except ValueError:
        abort(404)
    if not path.is_file():
        abort(404)
    preview = request.args.get("preview") == "1"
    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=not preview,
        download_name=_download_name(row),
        conditional=True,
    )
