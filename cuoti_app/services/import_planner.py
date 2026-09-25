"""Deterministic preview plans for versioned imports."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

PREVIEW_TTL_MINUTES = 20
SCALAR_FIELDS = (
    "qnum", "content", "note", "source_kind", "source_ref",
    "difficulty", "date_added",
)


class ImportPlanError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def database_revision(db) -> str:
    payload = {
        "classifications": list(db.execute('SELECT COUNT(*),COALESCE(SUM(revision),0) FROM mistake_classifications').fetchone()),
        "question_types": list(db.execute('SELECT COUNT(*),COALESCE(SUM(revision),0) FROM question_types').fetchone()),
        "mistakes": list(db.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0),COALESCE(MAX(revision),0),"
            "COALESCE(MAX(updated_at),'') FROM mistakes"
        ).fetchone()),
        "reviews": list(db.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0),COALESCE(MAX(created_at),'') FROM review_events"
        ).fetchone()),
        "attachments": list(db.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0),COALESCE(MAX(created_at),'') FROM attachments"
        ).fetchone()),
        "daily_tips": list(db.execute(
            "SELECT COUNT(*),COALESCE(MAX(id),0),COALESCE(MAX(revision),0),"
            "COALESCE(MAX(updated_at),'') FROM daily_tips"
        ).fetchone()),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _daily_tip_plan(db, daily_tips: list[dict], mode: str) -> dict:
    existing = [
        dict(row)
        for row in db.execute(
            "SELECT public_id,content_hash FROM daily_tips ORDER BY public_id"
        ).fetchall()
    ]
    by_hash = {row["content_hash"]: row for row in existing}
    occupied_ids = {str(row["public_id"]) for row in existing}
    items = []
    counts = {"add": 0, "merge": 0, "rekey": 0}
    namespace = uuid.UUID("3d59804c-37ed-4b21-a3c4-c6a27c017fb5")
    for position, item in enumerate(daily_tips, start=1):
        incoming_id = str(item["public_id"])
        content_hash = str(item["content_hash"])
        if mode == "merge" and content_hash in by_hash:
            action = "merge"
            target_id = str(by_hash[content_hash]["public_id"])
        elif mode == "merge" and incoming_id in occupied_ids:
            action = "rekey"
            salt = 0
            while True:
                target_id = str(
                    uuid.uuid5(namespace, f"{incoming_id}:{content_hash}:{salt}")
                )
                if target_id not in occupied_ids:
                    break
                salt += 1
            occupied_ids.add(target_id)
        else:
            action = "add"
            target_id = incoming_id
            occupied_ids.add(target_id)
        counts[action] += 1
        items.append(
            {
                "position": position,
                "action": action,
                "public_id": incoming_id,
                "target_public_id": target_id,
                "content_hash": content_hash,
            }
        )
    return {
        "counts": counts,
        "current": len(existing),
        "would_remove": len(existing) if mode == "replace" else 0,
        "items": items,
    }


def build_import_plan(
    db,
    mistakes: list[dict],
    mode: str,
    daily_tips: list[dict] | None = None,
) -> dict:
    if mode not in {"merge", "replace"}:
        raise ImportPlanError("INVALID_MODE", "mode 必须是 merge 或 replace")
    existing_rows = [
        dict(row) for row in db.execute(
            "SELECT id,public_id,fingerprint,qnum,content,note,source_kind,source_ref,"
            "difficulty,date_added FROM mistakes"
        )
    ]
    by_public = {
        row["public_id"]: row for row in existing_rows if row["public_id"] is not None
    }
    by_fingerprint: dict[str, list[dict]] = {}
    for row in existing_rows:
        if row["fingerprint"] is not None:
            by_fingerprint.setdefault(row["fingerprint"], []).append(row)
    existing_reasons: dict[int, set[str]] = defaultdict(set)
    for row in db.execute("SELECT mistake_id,reason_code FROM mistake_reason_items"):
        existing_reasons[int(row["mistake_id"])].add(row["reason_code"])
    existing_tags: dict[int, set[str]] = defaultdict(set)
    for row in db.execute(
        "SELECT mt.mistake_id,t.normalized_name FROM mistake_tags mt "
        "JOIN tags t ON t.id=mt.tag_id"
    ):
        existing_tags[int(row["mistake_id"])].add(row["normalized_name"])
    existing_events: dict[int, set[str]] = defaultdict(set)
    for row in db.execute(
        "SELECT mistake_id,public_id FROM review_events WHERE public_id IS NOT NULL"
    ):
        existing_events[int(row["mistake_id"])].add(row["public_id"])
    existing_attachments: dict[int, set[str]] = defaultdict(set)
    for row in db.execute("SELECT mistake_id,sha256 FROM attachments"):
        existing_attachments[int(row["mistake_id"])].add(row["sha256"])
    items = []
    seen_identity: set[tuple[str, str]] = set()
    counts = {"add": 0, "merge": 0, "ambiguous": 0, "conflict": 0, "duplicate_input": 0}
    for position, imported in enumerate(mistakes, start=1):
        public_id = str(imported.get("public_id") or "").strip()
        fingerprint = imported["fingerprint"]
        identity = ("public_id", public_id) if public_id else ("fingerprint", fingerprint)
        if identity in seen_identity:
            action = "duplicate_input"
            matches = []
        else:
            seen_identity.add(identity)
            if public_id:
                matches = [by_public[public_id]] if public_id in by_public else []
            else:
                matches = by_fingerprint.get(fingerprint, [])
            action = "add" if not matches else ("merge" if len(matches) == 1 else "ambiguous")
        conflicts = []
        fills = []
        local_id = matches[0]["id"] if len(matches) == 1 else None
        relation_additions = {
            "classification": int(bool(imported.get('classification'))),
            "reasons": len([part for part in str(imported.get("reasons") or "").split(",") if part]),
            "tags": len(imported.get("tags") or []),
            "review_events": len(imported.get("review_records") or []),
            "attachments": len(imported.get("attachments") or []),
        }
        if local_id:
            local = matches[0]
            for field in SCALAR_FIELDS:
                incoming = imported.get(field)
                current = local.get(field)
                if incoming in (None, "") or incoming == current:
                    continue
                if current in (None, ""):
                    fills.append(field)
                else:
                    conflicts.append(field)
            if conflicts:
                counts["conflict"] += 1
            relation_additions = {
                "classification": int(bool(imported.get('classification')) and not db.execute('SELECT 1 FROM mistake_classifications WHERE mistake_id=?', (local_id,)).fetchone()),
                "reasons": len({part for part in str(imported.get("reasons") or "").split(",") if part} - existing_reasons[local_id]),
                "tags": len({tag.casefold() for tag in imported.get("tags") or []} - existing_tags[local_id]),
                "review_events": sum(
                    not event.get("public_id") or event["public_id"] not in existing_events[local_id]
                    for event in imported.get("review_records") or []
                ),
                "attachments": sum(
                    str(item.get("sha256") or "") not in existing_attachments[local_id]
                    for item in imported.get("attachments") or []
                ),
            }
        counts[action] += 1
        items.append({
            "position": position,
            "identity": {"public_id": public_id or None, "fingerprint": fingerprint},
            "action": action,
            "local_id": local_id,
            "candidate_ids": [row["id"] for row in matches],
            "fill_empty_fields": fills,
            "conflict_fields": conflicts,
            "relation_additions": relation_additions,
            "warnings": (["旧格式仅按 fingerprint 匹配，无法可靠识别重复复习事件"] if not public_id else []),
        })
    old_count = db.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0]
    return {
        "mode": mode,
        "counts": counts,
        "current_mistakes": old_count,
        "would_remove": old_count if mode == "replace" else 0,
        "items": items,
        "requires_confirmation": mode == "replace" or bool(counts["conflict"] or counts["ambiguous"]),
        "daily_tips": _daily_tip_plan(db, list(daily_tips or []), mode),
    }


def store_preview(db, *, input_sha256: str, mode: str, plan: dict) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = _now()
    db.execute(
        "DELETE FROM import_previews WHERE expires_at<? OR used_at IS NOT NULL",
        (now.isoformat(),),
    )
    db.execute(
        "INSERT INTO import_previews(token_hash,input_sha256,database_revision,mode,"
        "plan_json,expires_at,created_at) VALUES(?,?,?,?,?,?,?)",
        (
            token_hash, input_sha256, database_revision(db), mode,
            json.dumps(plan, ensure_ascii=False, sort_keys=True),
            (now + timedelta(minutes=PREVIEW_TTL_MINUTES)).isoformat(), now.isoformat(),
        ),
    )
    db.commit()
    return token


def consume_preview(db, *, token: str, input_sha256: str, mode: str) -> dict:
    if not db.in_transaction:
        raise RuntimeError("consume_preview requires a caller-owned transaction")
    token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
    row = db.execute("SELECT * FROM import_previews WHERE token_hash=?", (token_hash,)).fetchone()
    if not row:
        raise ImportPlanError("PREVIEW_TOKEN_INVALID", "预览 token 无效或已使用")
    if row["used_at"] is not None or row["expires_at"] <= _now().isoformat():
        raise ImportPlanError("PREVIEW_TOKEN_EXPIRED", "预览 token 已过期或已使用")
    if row["input_sha256"] != input_sha256 or row["mode"] != mode:
        raise ImportPlanError("IMPORT_CHANGED", "导入文件或模式已变化，请重新预览")
    if row["database_revision"] != database_revision(db):
        raise ImportPlanError("DATABASE_CHANGED", "本地数据已变化，请重新预览")
    db.execute("UPDATE import_previews SET used_at=? WHERE token_hash=?", (_now().isoformat(), token_hash))
    return json.loads(row["plan_json"])


def inspect_preview(db, *, token: str, input_sha256: str, mode: str) -> dict:
    """Validate a token without consuming it, for pre-commit recovery creation."""

    token_hash = hashlib.sha256(str(token or "").encode()).hexdigest()
    row = db.execute("SELECT * FROM import_previews WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or row["used_at"] is not None:
        raise ImportPlanError("PREVIEW_TOKEN_INVALID", "预览 token 无效或已使用")
    if row["expires_at"] <= _now().isoformat():
        raise ImportPlanError("PREVIEW_TOKEN_EXPIRED", "预览 token 已过期")
    if row["input_sha256"] != input_sha256 or row["mode"] != mode:
        raise ImportPlanError("IMPORT_CHANGED", "导入文件或模式已变化，请重新预览")
    if row["database_revision"] != database_revision(db):
        raise ImportPlanError("DATABASE_CHANGED", "本地数据已变化，请重新预览")
    return json.loads(row["plan_json"])
