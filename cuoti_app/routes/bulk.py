"""Previewed and idempotent bulk mistake operations."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from ..transactions import begin_immediate

from ..database import get_db
from ..services.import_planner import database_revision
from ..validation import ValidationError, reasons_value


bp = Blueprint("bulk", __name__)
_ACTIONS = {"move_chapter", "set_reasons", "add_tags", "trash"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(data: dict) -> tuple[str, str]:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode()).hexdigest()


def _validate_request(data) -> dict:
    if not isinstance(data, dict):
        raise ValidationError("请求体必须是对象")
    action = data.get("action")
    if action not in _ACTIONS:
        raise ValidationError("批量操作类型无效")
    ids = data.get("public_ids")
    if not isinstance(ids, list) or not ids or len(ids) > 1000:
        raise ValidationError("public_ids 必须包含 1–1000 项")
    normalized = []
    for value in ids:
        try:
            normalized.append(str(uuid.UUID(str(value))))
        except ValueError as exc:
            raise ValidationError("public_ids 包含无效 UUID") from exc
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValidationError("payload 必须是对象")
    validated = {"action": action, "public_ids": list(dict.fromkeys(normalized)), "payload": {}}
    if action == "move_chapter":
        try:
            validated["payload"]["chapter_id"] = int(payload.get("chapter_id"))
        except (TypeError, ValueError) as exc:
            raise ValidationError("章节 ID 无效") from exc
    elif action == "set_reasons":
        validated["payload"]["reasons"] = reasons_value(payload.get("reasons"))
    elif action == "add_tags":
        tags = payload.get("tags")
        if not isinstance(tags, list) or not tags or len(tags) > 50:
            raise ValidationError("标签必须是 1–50 项数组")
        cleaned = [str(tag).strip() for tag in tags]
        if any(not tag or len(tag) > 80 for tag in cleaned):
            raise ValidationError("标签为空或过长")
        validated["payload"]["tags"] = list(dict.fromkeys(cleaned))
    return validated


def _build_plan(db, spec: dict) -> dict:
    placeholders = ",".join("?" for _ in spec["public_ids"])
    rows = db.execute(
        f"SELECT id,public_id,subject_id,chapter_id,deleted_at FROM mistakes "
        f"WHERE public_id IN ({placeholders})",
        spec["public_ids"],
    ).fetchall()
    by_id = {row["public_id"]: dict(row) for row in rows}
    conflicts = []
    target_chapter = None
    if spec["action"] == "move_chapter":
        target_chapter = db.execute(
            "SELECT id,subject_id,name FROM chapters WHERE id=?",
            (spec["payload"]["chapter_id"],),
        ).fetchone()
        if not target_chapter:
            raise ValidationError("目标章节不存在")
    items = []
    for public_id in spec["public_ids"]:
        row = by_id.get(public_id)
        conflict = None
        if row is None:
            conflict = "not_found"
        elif row["deleted_at"] is not None and spec["action"] != "trash":
            conflict = "in_trash"
        elif spec["action"] == "trash" and row["deleted_at"] is not None:
            conflict = "already_in_trash"
        elif target_chapter is not None and row["subject_id"] != target_chapter["subject_id"]:
            conflict = "cross_subject_chapter"
        if conflict:
            conflicts.append({"public_id": public_id, "reason": conflict})
        items.append({"public_id": public_id, "database_id": row["id"] if row else None, "conflict": conflict})
    return {
        "action": spec["action"], "payload": spec["payload"], "items": items,
        "requested": len(spec["public_ids"]),
        "affected": sum(item["conflict"] is None for item in items),
        "conflicts": conflicts,
    }


@bp.post("/api/v2/mistakes/bulk/preview")
def preview_bulk_operation():
    try:
        spec = _validate_request(request.get_json(silent=True))
        canonical, input_hash = _canonical(spec)
        db = get_db()
        plan = _build_plan(db, spec)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(timezone.utc)
        db.execute("DELETE FROM bulk_previews WHERE expires_at<? OR used_at IS NOT NULL", (now.isoformat(),))
        db.execute(
            "INSERT INTO bulk_previews(token_hash,input_hash,database_revision,plan_json,"
            "expires_at,created_at) VALUES(?,?,?,?,?,?)",
            (token_hash, input_hash, database_revision(db), json.dumps(plan, ensure_ascii=False, sort_keys=True),
             (now + timedelta(minutes=20)).isoformat(), now.isoformat()),
        )
        db.commit()
        return jsonify({"preview_token": token, "input_hash": input_hash, "plan": plan, "expires_in_seconds": 1200})
    except ValidationError as exc:
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400


def _apply_one(db, action: str, payload: dict, database_id: int):
    now = _now()
    if action == "move_chapter":
        db.execute("UPDATE mistakes SET chapter_id=?,revision=revision+1,updated_at=? WHERE id=?", (payload["chapter_id"], now, database_id))
    elif action == "set_reasons":
        db.execute("UPDATE mistakes SET reasons=?,revision=revision+1,updated_at=? WHERE id=?", (payload["reasons"], now, database_id))
        db.execute("DELETE FROM mistake_reason_items WHERE mistake_id=?", (database_id,))
        db.executemany(
            "INSERT INTO mistake_reason_items(mistake_id,reason_code) VALUES(?,?)",
            [(database_id, reason) for reason in payload["reasons"].split(",") if reason],
        )
    elif action == "add_tags":
        db.executemany(
            "INSERT OR IGNORE INTO mistake_tags(mistake_id,tag_id) VALUES(?,?)",
            [(database_id, tag_id) for tag_id in payload["_tag_ids"]],
        )
        db.execute("UPDATE mistakes SET revision=revision+1,updated_at=? WHERE id=?", (now, database_id))
    else:
        db.execute("UPDATE mistakes SET deleted_at=?,revision=revision+1,updated_at=? WHERE id=?", (now, now, database_id))


@bp.post("/api/v2/mistakes/bulk/commit")
def commit_bulk_operation():
    data = request.get_json(silent=True)
    try:
        if not isinstance(data, dict):
            raise ValidationError("请求体必须是对象")
        operation_id = str(uuid.UUID(str(data.get("operation_id"))))
        mode = data.get("mode", "atomic")
        if mode not in {"atomic", "best_effort"}:
            raise ValidationError("mode 必须是 atomic 或 best_effort")
        spec = _validate_request(data.get("operation"))
        _canonical_value, preview_input_hash = _canonical(spec)
        _operation_value, operation_hash = _canonical(
            {"mode": mode, "operation": spec}
        )
        db = get_db()
        replay = db.execute("SELECT input_hash,result_json FROM bulk_operations WHERE operation_id=?", (operation_id,)).fetchone()
        if replay:
            if replay["input_hash"] != operation_hash:
                return jsonify({"error": "operation_id 已用于不同操作", "code": "OPERATION_CONFLICT"}), 409
            return jsonify({**json.loads(replay["result_json"]), "idempotency_replayed": True})
        token_hash = hashlib.sha256(str(data.get("preview_token") or "").encode()).hexdigest()
        begin_immediate(db)
        preview = db.execute("SELECT * FROM bulk_previews WHERE token_hash=?", (token_hash,)).fetchone()
        if not preview or preview["used_at"] is not None or preview["expires_at"] <= _now():
            raise ValidationError("预览 token 无效、已使用或已过期")
        if preview["input_hash"] != preview_input_hash or preview["database_revision"] != database_revision(db):
            db.rollback()
            return jsonify({"error": "操作或数据库已变化，请重新预览", "code": "PREVIEW_STALE"}), 409
        plan = _build_plan(db, spec)
        if json.loads(preview["plan_json"]) != plan:
            db.rollback()
            return jsonify({"error": "批量计划已变化，请重新预览", "code": "PREVIEW_STALE"}), 409
        if mode == "atomic" and plan["conflicts"]:
            db.rollback()
            return jsonify({"error": "原子模式存在冲突，未修改任何记录", "code": "BULK_CONFLICTS", "conflicts": plan["conflicts"]}), 409
        succeeded = []
        failed = list(plan["conflicts"])
        retryable = [item["public_id"] for item in plan["conflicts"]]
        if spec["action"] == "add_tags":
            tags = [(tag, tag.casefold()) for tag in spec["payload"]["tags"]]
            db.executemany(
                "INSERT OR IGNORE INTO tags(name,normalized_name) VALUES(?,?)", tags
            )
            normalized = [item[1] for item in tags]
            placeholders = ",".join("?" for _ in normalized)
            tag_ids = [
                row["id"] for row in db.execute(
                    f"SELECT id FROM tags WHERE normalized_name IN ({placeholders})",
                    normalized,
                ).fetchall()
            ]
            spec["payload"] = {**spec["payload"], "_tag_ids": tag_ids}
        for item in plan["items"]:
            if item["conflict"]:
                continue
            try:
                if mode == "best_effort":
                    db.execute("SAVEPOINT bulk_item")
                _apply_one(db, spec["action"], spec["payload"], item["database_id"])
                if mode == "best_effort":
                    db.execute("RELEASE bulk_item")
                succeeded.append(item["public_id"])
            except Exception as exc:
                if mode != "best_effort":
                    raise
                db.execute("ROLLBACK TO bulk_item")
                db.execute("RELEASE bulk_item")
                failed.append({"public_id": item["public_id"], "reason": type(exc).__name__})
                retryable.append(item["public_id"])
        result = {"ok": not failed, "operation_id": operation_id, "mode": mode, "succeeded": succeeded, "failed": failed, "retryable": retryable}
        db.execute("UPDATE bulk_previews SET used_at=? WHERE token_hash=?", (_now(), token_hash))
        db.execute(
            "INSERT INTO bulk_operations(operation_id,input_hash,status,result_json,created_at) VALUES(?,?,'succeeded',?,?)",
            (operation_id, operation_hash, json.dumps(result, ensure_ascii=False), _now()),
        )
        db.commit()
        return jsonify(result)
    except (ValidationError, ValueError) as exc:
        get_db().rollback()
        return jsonify({"error": str(exc), "code": "VALIDATION"}), 400
    except Exception:
        get_db().rollback()
        raise
