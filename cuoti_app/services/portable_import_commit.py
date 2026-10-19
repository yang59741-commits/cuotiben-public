"""Atomic portable v3 import commit shared by HTTP and jobs."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from ..transactions import immediate_transaction
from .attachments import AttachmentError, validate_image
from .import_planner import ImportPlanError, build_import_plan, consume_preview, inspect_preview
from .import_recovery import create_replace_recovery
from .import_validation import parse_and_validate_import
from .portable_import import ImportSource, PortableImportError
from .job_results import load_business_result, store_business_result, validated_job_id


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
            "schedule_version,assistance,evidence_version,reviewed_on,content_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mistake_id, event["public_id"], event["client_event_id"],
                event["event_type"], event["reviewed_at"], event["created_at"],
                event["status_after"], event["correct_streak"],
                event["lapse_count_after"], event["source_import_id"],
                event["stage_before"], event["stage_after"], event["cycle_after"],
                event["next_review_after"], event["schedule_version"],
                event.get('assistance', 'unknown'), event.get('evidence_version', 1),
                event.get('reviewed_on'), event.get('content_fingerprint'),
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
        "SELECT event_type,reviewed_at,status_after,correct_streak,lapse_count_after,stage_after,"
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
    reviews = [row for row in rows if row["event_type"] in {"pass", "again", "assisted"}]
    db.execute(
        "UPDATE mistakes SET history_complete=1,history_complete_from=?,review_count=?,"
        "last_review=?,status=?,correct_streak=?,lapse_count=?,review_stage=?,"
        "review_cycle=?,next_review=?,schedule_version=? WHERE id=?",
        (
            item["history_complete_from"], len(reviews),
            str(reviews[-1]["reviewed_at"])[:10] if reviews else None,
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


def _restore_daily_analysis(db, mistake_id: int, item: dict) -> bool:
    """Import a validated card only when the local mistake has no cache."""

    analysis = item.get("daily_analysis")
    if not analysis or db.execute(
        "SELECT 1 FROM daily_analysis_items WHERE mistake_id=?", (mistake_id,)
    ).fetchone():
        return False
    db.execute(
        "INSERT INTO daily_analysis_items("
        "mistake_id,analysis_date,analysis_schema_version,prompt_version,source_hash,"
        "analysis_json,source_job_id,model_role,model_name,generated_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            mistake_id, analysis["analysis_date"], analysis["analysis_schema_version"],
            analysis["prompt_version"], analysis["source_hash"],
            json.dumps(analysis["analysis"], ensure_ascii=False, separators=(",", ":")),
            analysis["source_job_id"], analysis["model_role"], analysis["model_name"],
            analysis["generated_at"], analysis["updated_at"],
        ),
    )
    return True


def _restore_classification(db, mistake_id, item):
    from . import mistake_classification as service
    incoming = item.get('classification')
    if not incoming or db.execute('SELECT 1 FROM mistake_classifications WHERE mistake_id=?', (mistake_id,)).fetchone():
        return
    public_id = db.execute('SELECT public_id FROM mistakes WHERE id=?', (mistake_id,)).fetchone()[0]
    row = service.mistake_row(db, public_id)
    code = incoming['primary_type_code']
    definition = incoming['question_type']
    local = db.execute('SELECT * FROM question_types WHERE code=?', (code,)).fetchone()
    reason = ''
    if local and (local['subject'] != definition['subject'] or local['name'] != definition['name']):
        reason = '导入题型定义与本地目录不同'
    elif not local:
        same = db.execute('SELECT code FROM question_types WHERE subject=? AND name=?', (definition['subject'], definition['name'])).fetchone()
        if same:
            code = same['code']
        else:
            code = service.update_type(db, code, definition)
    value = {**incoming, 'primary_type_code': code}
    if incoming['source_hash'] != service.source_hash(row):
        reason = '导入分类的题干或科目章节与当前错题不同'
    try:
        normalized = service.validate_classification(db, row, value, check_chapter=False)
    except service.ClassificationError as exc:
        reason = str(exc)
    if reason:
        db.execute('INSERT INTO classification_suggestions(mistake_id,source_hash,base_revision,payload_json,reason,created_at) VALUES(?,?,0,?,?,?)',
                   (mistake_id, service.source_hash(row), service.dumps(value), reason, service.now()))
    else:
        service._save(db, row, normalized, revision=0, locked=incoming['locked'], source='import')


def _commit_daily_tips(db, daily_tips: list[dict], plan: dict) -> dict[str, int]:
    counts = {"added": 0, "merged": 0, "rekeyed": 0}
    for item, planned in zip(daily_tips, plan.get("items") or []):
        action = planned["action"]
        if action == "merge":
            counts["merged"] += 1
            continue
        target_public_id = str(planned["target_public_id"])
        db.execute(
            "INSERT INTO daily_tips(public_id,content,content_hash,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                target_public_id,
                item["content"],
                item["content_hash"],
                item["revision"],
                item["created_at"],
                item["updated_at"],
            ),
        )
        counts["added"] += 1
        counts["rekeyed"] += int(action == "rekey")
    return counts


def _publish_import_attachments(
    db, parsed, validated, target_ids, root: Path, published=None
):
    root.mkdir(parents=True, exist_ok=True)
    if published is None:
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


def commit_portable_import(
    db,
    *,
    raw: ImportSource,
    mode: str,
    preview_token: str,
    resolutions: dict,
    database_path: str | Path,
    attachment_dir: str | Path,
    recovery_dir: str | Path,
    warn=None,
    source_job_id: str | None = None,
) -> dict[str, object]:
    """Consume one preview and atomically publish its rows and attachments."""

    source_job_id = validated_job_id(source_job_id)
    replay = load_business_result(db, source_job_id, "data-import")
    if replay is not None:
        return {**replay, "idempotency_replayed": True}
    if not isinstance(resolutions, dict):
        raise ValueError("冲突选择格式无效")
    published = []
    parsed, validated = parse_and_validate_import(raw, db)
    recovery_path = None
    old_attachment_keys = []
    try:
        if mode == "replace":
            inspect_preview(
                db,
                token=preview_token,
                input_sha256=parsed.input_sha256,
                mode=mode,
            )
            recovery_path = create_replace_recovery(
                database_path, attachment_dir, recovery_dir
            )
            old_attachment_keys = [
                row[0]
                for row in db.execute(
                    "SELECT object_key FROM attachments WHERE status='published'"
                )
            ]
        with immediate_transaction(db):
            replay = load_business_result(db, source_job_id, "data-import")
            if replay is not None:
                return {**replay, "idempotency_replayed": True}
            stored_plan = consume_preview(
                db,
                token=preview_token,
                input_sha256=parsed.input_sha256,
                mode=mode,
            )
            current_plan = build_import_plan(
                db, validated, mode, parsed.daily_tips
            )
            if current_plan != stored_plan:
                raise ImportPlanError("PLAN_CHANGED", "导入计划已变化，请重新预览")
            if mode == "replace":
                db.execute("DELETE FROM mistakes")
                db.execute("DELETE FROM daily_tips")
            inserted = merged = review_events = analysis_caches = 0
            target_ids = []
            for item, planned in zip(validated, current_plan["items"]):
                if planned["action"] in {"ambiguous", "duplicate_input"}:
                    raise ImportPlanError(
                        "UNRESOLVED_CONFLICT", "存在歧义或输入内重复，无法提交"
                    )
                if mode == "replace" or planned["action"] == "add":
                    target_id = _insert_imported_mistake(db, item)
                    inserted += 1
                else:
                    allowed = set(resolutions.get(str(planned["position"]), []))
                    if not allowed.issubset(set(planned["conflict_fields"])):
                        raise ImportPlanError(
                            "INVALID_RESOLUTION", "冲突选择包含无效字段"
                        )
                    target_id = planned["local_id"]
                    review_events += _merge_imported_mistake(
                        db, target_id, item, allowed
                    )
                    merged += 1
                target_ids.append(target_id)
                analysis_caches += int(_restore_daily_analysis(db, target_id, item))
                _restore_classification(db, target_id, item)
            _publish_import_attachments(
                db,
                parsed,
                validated,
                target_ids,
                Path(attachment_dir),
                published=published,
            )
            daily_tip_counts = _commit_daily_tips(
                db,
                parsed.daily_tips,
                current_plan.get("daily_tips") or {},
            )
            result = {
                "ok": True,
                "inserted": inserted,
                "merged": merged,
                "review_events_added": review_events,
                "daily_analysis_caches_added": analysis_caches,
                "daily_tips_added": daily_tip_counts["added"],
                "daily_tips_merged": daily_tip_counts["merged"],
                "daily_tips_rekeyed": daily_tip_counts["rekeyed"],
                "input_sha256": parsed.input_sha256,
                "recovery_backup": str(recovery_path) if recovery_path else None,
            }
            store_business_result(db, source_job_id, "data-import", result)
    except Exception:
        for path in published:
            path.unlink(missing_ok=True)
        raise
    if mode == "replace":
        live_keys = {row[0] for row in db.execute("SELECT object_key FROM attachments")}
        attachment_root = Path(attachment_dir)
        for key in old_attachment_keys:
            if key not in live_keys:
                try:
                    (attachment_root / key).unlink(missing_ok=True)
                except OSError:
                    if warn is not None:
                        warn("old imported attachment cleanup failed")
    return result
