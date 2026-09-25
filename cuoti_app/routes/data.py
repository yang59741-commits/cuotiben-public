"""Versioned, atomic backup import and export routes."""

from __future__ import annotations

import json
import hashlib
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from ..database import get_db
from ..services.portable_backup import PortableBackupError, write_portable_backup
from ..services.portable_import import PortableImportError, parse_import
from ..services.review_schedule import SCHEDULE_VERSION
from ..services.import_planner import (
    ImportPlanError,
    build_import_plan,
    consume_preview,
    database_revision,
    inspect_preview,
    store_preview,
)
from ..services.import_recovery import create_replace_recovery
from ..services.attachments import AttachmentError, validate_image
from ..validation import (
    REVIEW_EVENT_TYPES,
    ValidationError,
    date_value,
    mistake_fingerprint,
    nonnegative_int,
    reasons_value,
    status_value,
    text_value,
    timestamp_value,
)

bp = Blueprint("data", __name__)

BACKUP_FORMAT_VERSION = 3
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_MISTAKES = 10_000


@bp.get("/api/v3/export")
def api_v3_export():
    import tempfile
    from pathlib import Path

    archive = tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024, mode="w+b")
    try:
        summary = write_portable_backup(
            get_db(), Path(current_app.config["ATTACHMENT_DIR"]), archive
        )
    except PortableBackupError as exc:
        archive.close()
        return jsonify({"error": str(exc), "code": exc.code}), exc.status_code
    archive.seek(0)
    response = send_file(
        archive,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"cuoti-portable-v{BACKUP_FORMAT_VERSION}.zip",
        max_age=0,
    )
    response.headers["X-Backup-Mistakes"] = str(summary["mistakes"])
    response.headers["X-Backup-Attachments"] = str(summary["attachments"])
    response.call_on_close(archive.close)
    return response


@bp.get('/internal/legacy/export')
def api_export():
    """Compatibility export used only by migration tests and offline conversion."""
    db = get_db()
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON m.subject_id=s.id "
            "LEFT JOIN chapters c ON m.chapter_id=c.id "
            "WHERE m.deleted_at IS NULL ORDER BY m.id"
        ).fetchall()
    ]
    for mistake in rows:
        mistake_id = mistake.pop('id')
        mistake.pop('subject_id', None)
        mistake.pop('chapter_id', None)
        mistake.pop('fingerprint', None)
        mistake.pop('answer', None)
        mistake.pop('solution', None)
        mistake['reviews'] = [
            {key: event[key] for key in ('event_type', 'reviewed_at', 'status_after', 'correct_streak')}
            for event in db.execute(
                "SELECT event_type,reviewed_at,status_after,correct_streak "
                "FROM review_events WHERE mistake_id=? ORDER BY id", (mistake_id,)
            ).fetchall()
        ]
    return jsonify({'schema_version': 2, 'exported_at': datetime.now().astimezone().isoformat(timespec='seconds'), 'mistakes': rows})


def _resolve_catalog(db):
    subjects = {
        row['name']: dict(row)
        for row in db.execute("SELECT id,name FROM subjects").fetchall()
    }
    chapters = {
        (row['subject'], row['name']): dict(row)
        for row in db.execute(
            "SELECT c.id,c.name,c.subject_id,s.name AS subject "
            "FROM chapters c JOIN subjects s ON s.id=c.subject_id"
        ).fetchall()
    }
    return subjects, chapters


def _validated_event(value, index, event_index):
    if not isinstance(value, dict):
        raise ValidationError(f"第 {index} 道题的第 {event_index} 条复习记录格式无效")
    event_type = value.get('event_type')
    if event_type not in REVIEW_EVENT_TYPES:
        raise ValidationError(f"第 {index} 道题的复习事件类型无效")
    reviewed_at = timestamp_value(
        value.get('reviewed_at'),
        f"第 {index} 道题的复习时间",
    )
    status_after = status_value(value.get('status_after'))
    correct_streak = nonnegative_int(
        value.get('correct_streak'),
        f"第 {index} 道题的连续正确次数",
    )
    return (event_type, reviewed_at, status_after, correct_streak)


def _validated_mistake(item, index, subjects, chapters):
    if not isinstance(item, dict):
        raise ValidationError(f"第 {index} 道题格式无效")
    subject_name = text_value(
        item.get('subject'), f"第 {index} 道题的科目", maximum=100, required=True
    )
    subject = subjects.get(subject_name)
    if not subject:
        raise ValidationError(f"第 {index} 道题的科目不存在")

    chapter_name = text_value(
        item.get('chapter'), f"第 {index} 道题的章节", maximum=100
    )
    chapter = None
    if chapter_name:
        chapter = chapters.get((subject_name, chapter_name))
        if not chapter:
            raise ValidationError(f"第 {index} 道题的章节不属于该科目")

    qnum = text_value(item.get('qnum'), f"第 {index} 道题的题号", maximum=80)
    content = text_value(
        item.get('content'), f"第 {index} 道题的内容", maximum=100_000
    )
    reasons = reasons_value(item.get('reasons'))
    note = text_value(item.get('note'), f"第 {index} 道题的备注", maximum=20_000)
    date_added = date_value(item.get('date_added'), f"第 {index} 道题的日期")
    status = status_value(item.get('status'))
    review_count = nonnegative_int(
        item.get('review_count'), f"第 {index} 道题的复习次数"
    )
    last_review = date_value(
        item.get('last_review'), f"第 {index} 道题的上次复习日期", optional=True
    )
    next_review = date_value(
        item.get('next_review'), f"第 {index} 道题的下次复习日期", optional=True
    )
    correct_streak = nonnegative_int(
        item.get('correct_streak'),
        f"第 {index} 道题的连续正确次数",
        default=3 if status == '已掌握' else 0,
    )
    if status == '已掌握':
        correct_streak = max(correct_streak, 3)
    lapse_count = nonnegative_int(
        item.get('lapse_count'), f"第 {index} 道题的遗忘次数"
    )

    raw_reviews = item.get('reviews', [])
    if raw_reviews is None:
        raw_reviews = []
    if not isinstance(raw_reviews, list):
        raise ValidationError(f"第 {index} 道题的复习历史必须是数组")
    if len(raw_reviews) > 100_000:
        raise ValidationError(f"第 {index} 道题的复习历史过多")
    reviews = [
        _validated_event(event, index, event_index)
        for event_index, event in enumerate(raw_reviews, start=1)
    ]
    review_records = []
    for raw_event, values in zip(raw_reviews, reviews):
        event_public_id = str(raw_event.get("public_id") or "").strip()
        if event_public_id:
            try:
                event_public_id = str(uuid.UUID(event_public_id))
            except ValueError as exc:
                raise ValidationError(
                    f"第 {index} 道题的复习事件 public_id 无效"
                ) from exc
        client_event_id = str(raw_event.get("client_event_id") or "").strip()
        if len(client_event_id) > 100:
            raise ValidationError(f"第 {index} 道题的复习事件标识过长")
        review_records.append({
            "public_id": event_public_id or None,
            "client_event_id": client_event_id or None,
            "event_type": values[0], "reviewed_at": values[1],
            "status_after": values[2], "correct_streak": values[3],
            "lapse_count_after": nonnegative_int(
                raw_event.get("lapse_count_after"), "复习后的遗忘次数"
            ),
            "created_at": str(raw_event.get("created_at") or values[1]),
            "source_import_id": str(raw_event.get("source_import_id") or "") or None,
            "stage_before": (
                nonnegative_int(raw_event.get("stage_before"), "复习前阶段")
                if raw_event.get("stage_before") is not None else None
            ),
            "stage_after": (
                nonnegative_int(raw_event.get("stage_after"), "复习后阶段")
                if raw_event.get("stage_after") is not None else None
            ),
            "cycle_after": (
                nonnegative_int(raw_event.get("cycle_after"), "复习后循环")
                if raw_event.get("cycle_after") is not None else None
            ),
            "next_review_after": date_value(
                raw_event.get("next_review_after"), "复习后的下次日期", optional=True
            ),
            "schedule_version": text_value(
                raw_event.get("schedule_version"), "复习规则版本", maximum=80
            ) or None,
        })
    fingerprint = mistake_fingerprint(
        subject_name, chapter_name, qnum, content, date_added
    )
    values = (
        subject['id'],
        chapter['id'] if chapter else None,
        qnum,
        content,
        reasons,
        note,
        date_added,
        status,
        review_count,
        last_review,
        next_review,
        correct_streak,
        lapse_count,
        fingerprint,
    )
    public_id = str(item.get("public_id") or "").strip()
    if public_id:
        try:
            public_id = str(uuid.UUID(public_id))
        except ValueError as exc:
            raise ValidationError(f"第 {index} 道题的 public_id 无效") from exc
    tags = item.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 100 or any(
        not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 80 for tag in tags
    ):
        raise ValidationError(f"第 {index} 道题的标签无效")
    modern = {
        "source_kind": text_value(item.get("source_kind"), f"第 {index} 道题的来源类型", maximum=50),
        "source_ref": text_value(item.get("source_ref"), f"第 {index} 道题的来源", maximum=500),
        "difficulty": text_value(item.get("difficulty"), f"第 {index} 道题的难度", maximum=50),
    }
    review_stage = nonnegative_int(
        item.get("review_stage"), f"第 {index} 道题的复习阶段",
        default=min(correct_streak, 3),
    )
    if review_stage > 3:
        raise ValidationError(f"第 {index} 道题的复习阶段必须为 0 到 3")
    review_cycle = nonnegative_int(
        item.get("review_cycle"), f"第 {index} 道题的复习循环", default=1
    )
    if review_cycle < 1:
        raise ValidationError(f"第 {index} 道题的复习循环必须从 1 开始")
    schedule_version = text_value(
        item.get("schedule_version") or SCHEDULE_VERSION,
        f"第 {index} 道题的复习规则版本", maximum=80, required=True,
    )
    return {
        'values': values, 'fingerprint': fingerprint, 'reviews': reviews,
        "review_records": review_records,
        "public_id": public_id or None,
        "subject_id": subject["id"], "chapter_id": chapter["id"] if chapter else None,
        "subject": subject_name, "chapter": chapter_name, "qnum": qnum,
        "content": content, "reasons": reasons, "note": note,
        "date_added": date_added, "status": status, "review_count": review_count,
        "last_review": last_review, "next_review": next_review,
        "correct_streak": correct_streak, "lapse_count": lapse_count,
        "review_stage": review_stage, "review_cycle": review_cycle,
        "schedule_version": schedule_version,
        "tags": list(dict.fromkeys(tag.strip() for tag in tags)),
        "history_complete": bool(item.get("history_complete", False)),
        "history_complete_from": str(item.get("history_complete_from") or date_added),
        "updated_at": str(item.get("updated_at") or date_added),
        "attachments": item.get("attachments") or [],
        **modern,
    }


def _parsed_and_validated(raw: bytes, db):
    from ..services.import_validation import parse_and_validate_import
    return parse_and_validate_import(raw, db)


@bp.post("/api/v3/import/preview")
def api_v3_import_preview():
    mode = request.args.get("mode", "merge")
    raw = request.get_data(cache=False)
    try:
        db = get_db()
        parsed, validated = _parsed_and_validated(raw, db)
        plan = build_import_plan(db, validated, mode, parsed.daily_tips)
        token = store_preview(
            db, input_sha256=parsed.input_sha256, mode=mode, plan=plan
        )
        return jsonify({
            "input_sha256": parsed.input_sha256,
            "database_revision": database_revision(db),
            "format_version": parsed.format_version,
            "legacy": parsed.legacy,
            "preview_token": token,
            "expires_in_seconds": 20 * 60,
            "plan": plan,
        })
    except PortableImportError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), exc.status_code
    except ImportPlanError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), 400
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


def _sync_import_sets(db, mistake_id: int, item: dict) -> None:
    reasons = {
        part.strip() for part in str(item["reasons"] or "").split(",") if part.strip()
    }
    reasons.update(
        row[0] for row in db.execute(
            "SELECT reason_code FROM mistake_reason_items WHERE mistake_id=?",
            (mistake_id,),
        )
    )
    db.executemany(
        "INSERT OR IGNORE INTO mistake_reason_items(mistake_id,reason_code) VALUES(?,?)",
        [(mistake_id, reason) for reason in sorted(reasons)],
    )
    db.execute(
        "UPDATE mistakes SET reasons=? WHERE id=?",
        (",".join(sorted(reasons)), mistake_id),
    )
    for tag in item["tags"]:
        normalized = tag.casefold()
        db.execute(
            "INSERT OR IGNORE INTO tags(name,normalized_name) VALUES(?,?)",
            (tag, normalized),
        )
        tag_id = db.execute(
            "SELECT id FROM tags WHERE normalized_name=?", (normalized,)
        ).fetchone()[0]
        db.execute(
            "INSERT OR IGNORE INTO mistake_tags(mistake_id,tag_id) VALUES(?,?)",
            (mistake_id, tag_id),
        )


def _merge_review_records(db, mistake_id: int, item: dict) -> int:
    inserted = 0
    for event in item["review_records"]:
        if event["public_id"] and db.execute(
            "SELECT 1 FROM review_events WHERE public_id=?", (event["public_id"],)
        ).fetchone():
            continue
        db.execute(
            "INSERT INTO review_events(mistake_id,public_id,client_event_id,event_type,"
            "reviewed_at,created_at,status_after,correct_streak,lapse_count_after,"
            "source_import_id,stage_before,stage_after,cycle_after,next_review_after,"
            "schedule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mistake_id, event["public_id"], event["client_event_id"],
                event["event_type"], event["reviewed_at"], event["created_at"],
                event["status_after"], event["correct_streak"],
                event["lapse_count_after"], event["source_import_id"],
                event["stage_before"], event["stage_after"], event["cycle_after"],
                event["next_review_after"], event["schedule_version"],
            ),
        )
        inserted += 1
    return inserted


def _rebuild_review_snapshot(db, mistake_id: int, item: dict) -> None:
    if not item["history_complete"]:
        db.execute(
            "UPDATE mistakes SET history_complete_from=COALESCE(history_complete_from,?) "
            "WHERE id=? AND history_complete=0",
            (item["history_complete_from"], mistake_id),
        )
        return
    rows = db.execute(
        "SELECT reviewed_at,status_after,correct_streak,lapse_count_after,stage_after,"
        "cycle_after,next_review_after,schedule_version "
        "FROM review_events WHERE mistake_id=? ORDER BY reviewed_at,id",
        (mistake_id,),
    ).fetchall()
    if not rows:
        db.execute(
            "UPDATE mistakes SET history_complete=1,history_complete_from=? WHERE id=?",
            (item["history_complete_from"], mistake_id),
        )
        return
    last = rows[-1]
    db.execute(
        "UPDATE mistakes SET history_complete=1,history_complete_from=?,review_count=?,"
        "last_review=?,status=?,correct_streak=?,lapse_count=?,review_stage=?,"
        "review_cycle=?,next_review=?,schedule_version=? WHERE id=?",
        (
            item["history_complete_from"], len(rows), str(last["reviewed_at"])[:10],
            last["status_after"], last["correct_streak"], last["lapse_count_after"],
            last["stage_after"] if last["stage_after"] is not None else item["review_stage"],
            last["cycle_after"] if last["cycle_after"] is not None else item["review_cycle"],
            last["next_review_after"] if last["schedule_version"] else item["next_review"],
            last["schedule_version"] or item["schedule_version"],
            mistake_id,
        ),
    )


def _insert_imported_mistake(db, item: dict) -> int:
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    cursor = db.execute(
        "INSERT INTO mistakes(public_id,revision,updated_at,history_complete,history_complete_from,subject_id,"
        "chapter_id,qnum,content,reasons,note,date_added,status,review_count,last_review,"
        "next_review,correct_streak,lapse_count,fingerprint,source_kind,"
        "source_ref,difficulty,review_stage,review_cycle,schedule_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            item["public_id"], 1, now, int(item["history_complete"]), item["history_complete_from"],
            item["subject_id"], item["chapter_id"], item["qnum"], item["content"],
            item["reasons"], item["note"], item["date_added"], item["status"],
            item["review_count"], item["last_review"], item["next_review"],
            item["correct_streak"], item["lapse_count"], item["fingerprint"],
            item["source_kind"], item["source_ref"],
            item["difficulty"],
            item["review_stage"], item["review_cycle"], item["schedule_version"],
        ),
    )
    _sync_import_sets(db, cursor.lastrowid, item)
    _merge_review_records(db, cursor.lastrowid, item)
    _rebuild_review_snapshot(db, cursor.lastrowid, item)
    return cursor.lastrowid


def _merge_imported_mistake(db, mistake_id: int, item: dict, allowed_fields: set[str]) -> int:
    local = dict(db.execute("SELECT * FROM mistakes WHERE id=?", (mistake_id,)).fetchone())
    updates = {}
    for field in (
        "qnum", "content", "note", "source_kind", "source_ref",
        "difficulty", "date_added",
    ):
        incoming = item[field]
        if incoming not in (None, "") and (local.get(field) in (None, "") or field in allowed_fields):
            updates[field] = incoming
    if updates:
        updates["revision"] = int(local["revision"]) + 1
        updates["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        assignments = ",".join(f"{field}=?" for field in updates)
        db.execute(
            f"UPDATE mistakes SET {assignments} WHERE id=?",
            [*updates.values(), mistake_id],
        )
    _sync_import_sets(db, mistake_id, item)
    inserted = _merge_review_records(db, mistake_id, item)
    _rebuild_review_snapshot(db, mistake_id, item)
    return inserted


def _publish_import_attachments(db, parsed, validated, target_ids, root: Path):
    root.mkdir(parents=True, exist_ok=True)
    published = []
    for item, mistake_id in zip(validated, target_ids):
        existing = {
            row[0] for row in db.execute(
                "SELECT sha256 FROM attachments WHERE mistake_id=?", (mistake_id,)
            )
        }
        existing_usage = db.execute(
            "SELECT COUNT(*),COALESCE(SUM(byte_size),0) FROM attachments WHERE mistake_id=?",
            (mistake_id,),
        ).fetchone()
        pending = [
            metadata for metadata in item["attachments"]
            if str(metadata.get("sha256") or "") not in existing
        ]
        if existing_usage[0] + len(pending) > 6:
            raise PortableImportError("ATTACHMENT_LIMIT", "单题附件数量超过 6 个")
        pending_bytes = sum(int(metadata.get("byte_size") or 0) for metadata in pending)
        if existing_usage[1] + pending_bytes > 30 * 1024 * 1024:
            raise PortableImportError("ATTACHMENT_LIMIT", "单题附件总量超过 30 MB")
        for metadata in pending:
            digest = str(metadata.get("sha256") or "")
            body = parsed.attachments.get(str(metadata.get("archive_path") or ""))
            if body is None or hashlib.sha256(body).hexdigest() != digest:
                raise PortableImportError("ATTACHMENT_HASH", "附件正文与清单不一致")
            try:
                mime, extension, width, height, verified_digest = validate_image(body)
            except AttachmentError as exc:
                raise PortableImportError(exc.code, str(exc)) from exc
            if verified_digest != digest or metadata.get("mime_type") != mime:
                raise PortableImportError("ATTACHMENT_METADATA", "附件类型元数据与文件不一致")
            object_key = f"{uuid.uuid4().hex}{extension}"
            final = root / object_key
            temporary = root / f".{object_key}.import"
            temporary.write_bytes(body)
            os.replace(temporary, final)
            published.append(final)
            db.execute(
                "INSERT INTO attachments(public_id,mistake_id,object_key,mime_type,extension,"
                "byte_size,width,height,sha256,sort_order,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'published',?)",
                (
                    metadata.get("public_id") or str(uuid.uuid4()), mistake_id, object_key,
                    mime, extension, len(body), width,
                    height, digest, metadata.get("sort_order", 0),
                    metadata.get("created_at") or datetime.now().astimezone().isoformat(),
                ),
            )
    return published


@bp.post("/api/v3/import/commit")
def api_v3_import_commit():
    mode = request.args.get("mode", "merge")
    token = request.headers.get("X-Import-Preview-Token") or ""
    raw = request.get_data(cache=False)
    try:
        from ..services.portable_import_commit import commit_portable_import
        return jsonify(commit_portable_import(
            get_db(), raw=raw, mode=mode, preview_token=token,
            resolutions=json.loads(request.headers.get('X-Import-Resolutions') or '{}'),
            database_path=current_app.config['DATABASE'], attachment_dir=current_app.config['ATTACHMENT_DIR'],
            recovery_dir=Path(current_app.config['DATABASE']).resolve().parent / '备份',
            warn=current_app.logger.warning,
        ))
    except (PortableImportError, ImportPlanError) as exc:
        get_db().rollback()
        return jsonify({"error": str(exc), "code": exc.code}), 409 if isinstance(exc, ImportPlanError) else exc.status_code
    except (ValidationError, ValueError, json.JSONDecodeError) as exc:
        get_db().rollback()
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except Exception:
        get_db().rollback()
        current_app.logger.exception("portable import commit failed")
        return jsonify({"error": "导入提交失败，数据与附件均未更改", "code": "IMPORT_FAILED"}), 500


@bp.post("/api/v3/import/stage")
def api_v3_import_stage():
    """Stage a confirmed immutable input for the heavy-lane commit job."""

    mode = request.args.get("mode", "merge")
    token = request.headers.get("X-Import-Preview-Token") or ""
    raw = request.get_data(cache=False)
    try:
        db = get_db()
        parsed, _validated = _parsed_and_validated(raw, db)
        inspect_preview(db, token=token, input_sha256=parsed.input_sha256, mode=mode)
        root = Path(current_app.config["JOB_INPUT_DIR"])
        root.mkdir(parents=True, exist_ok=True)
        name = parsed.input_sha256 + ".import"
        destination = root / name
        if not destination.exists():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{name}.", suffix=".tmp", dir=root
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, destination)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
        return jsonify({
            "staged_file": name,
            "input_sha256": parsed.input_sha256,
            "size": len(raw),
        })
    except PortableImportError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), exc.status_code
    except (ImportPlanError, ValidationError) as exc:
        return jsonify({"error": str(exc), "code": getattr(exc, "code", "VALIDATION")}), 409


@bp.post('/internal/legacy/import')
def api_import():
    return jsonify({
        'error': '旧导入入口已停用，请使用当前导入预览流程',
        'code': 'legacy_import_disabled',
    }), 410
