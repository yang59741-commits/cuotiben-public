"""Validated attachment staging, atomic publication, and crash reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from ..database import connect_database
from ..transactions import immediate_transaction


MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 30 * 1024 * 1024
MAX_ATTACHMENTS = 6
MAX_PIXELS = 40_000_000
FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}


class AttachmentError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def list_published_attachments(db, mistake_public_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT a.public_id,a.mime_type,a.extension,a.byte_size,a.width,a.height,"
        "a.sha256,a.sort_order,a.created_at FROM attachments a "
        "JOIN mistakes m ON m.id=a.mistake_id WHERE m.public_id=? "
        "AND m.deleted_at IS NULL AND a.status='published' ORDER BY a.sort_order,a.id",
        (mistake_public_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def published_attachment_content(db, attachment_public_id: str):
    return db.execute(
        "SELECT a.object_key,a.mime_type FROM attachments a "
        "JOIN mistakes m ON m.id=a.mistake_id WHERE a.public_id=? "
        "AND a.status='published' AND m.deleted_at IS NULL",
        (attachment_public_id,),
    ).fetchone()


def validate_image(data: bytes) -> tuple[str, str, int, int, str]:
    if not data or len(data) > MAX_FILE_BYTES:
        raise AttachmentError("ATTACHMENT_SIZE", "附件必须在 1 字节到 10 MB 之间")
    try:
        import io

        with Image.open(io.BytesIO(data)) as image:
            image_format = str(image.format or "").upper()
            if image_format not in FORMATS:
                raise AttachmentError("ATTACHMENT_TYPE", "仅支持 JPEG、PNG 或 WebP 图片")
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                raise AttachmentError("ATTACHMENT_PIXELS", "图片像素尺寸无效或过大")
            image.verify()
    except AttachmentError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise AttachmentError("ATTACHMENT_TYPE", "文件内容不是有效图片") from exc
    mime, extension = FORMATS[image_format]
    return mime, extension, width, height, hashlib.sha256(data).hexdigest()


def publish_attachment(db, root: Path, mistake, revision: int, data: bytes) -> dict:
    mime, extension, width, height, digest = validate_image(data)
    root.mkdir(parents=True, exist_ok=True)
    public_id = str(uuid.uuid4())
    object_key = f"{public_id}{extension}"
    staged = root / f".{object_key}.staged"
    final = root / object_key
    pending_committed = False
    mistake_id = None
    previous_updated_at = None
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb", dir=root, prefix=f".{object_key}.", suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, staged)
        with immediate_transaction(db):
            current = db.execute(
                "SELECT id,revision,updated_at FROM mistakes "
                "WHERE public_id=? AND deleted_at IS NULL",
                (mistake,),
            ).fetchone()
            if not current:
                raise AttachmentError("NOT_FOUND", "错题不存在", status_code=404)
            if current["revision"] != revision:
                raise AttachmentError(
                    "REVISION_CONFLICT", "错题已被其他操作更新", status_code=409
                )
            usage = db.execute(
                "SELECT COUNT(*) AS n,COALESCE(SUM(byte_size),0) AS bytes "
                "FROM attachments WHERE mistake_id=? "
                "AND status IN ('staged','published')",
                (current["id"],),
            ).fetchone()
            if (
                usage["n"] >= MAX_ATTACHMENTS
                or usage["bytes"] + len(data) > MAX_TOTAL_BYTES
            ):
                raise AttachmentError(
                    "ATTACHMENT_LIMIT", "单题最多 6 个附件且合计不超过 30 MB"
                )
            now = datetime.now(timezone.utc).isoformat()
            sort_order = int(usage["n"])
            mistake_id = current["id"]
            previous_updated_at = current["updated_at"]
            db.execute(
                "INSERT INTO attachments(public_id,mistake_id,object_key,mime_type,"
                "extension,byte_size,width,height,sha256,sort_order,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?, 'staged',?)",
                (
                    public_id, mistake_id, object_key, mime, extension, len(data),
                    width, height, digest, sort_order, now,
                ),
            )
            db.execute(
                "UPDATE mistakes SET revision=revision+1,updated_at=? WHERE id=?",
                (now, mistake_id),
            )
        pending_committed = True
        os.replace(staged, final)
        with db:
            changed = db.execute(
                "UPDATE attachments SET status='published' "
                "WHERE public_id=? AND status='staged'",
                (public_id,),
            )
            if changed.rowcount != 1:
                raise RuntimeError("attachment publication state was lost")
    except Exception:
        if pending_committed:
            with immediate_transaction(db):
                removed = db.execute(
                    "DELETE FROM attachments WHERE public_id=?", (public_id,)
                ).rowcount
                if removed:
                    db.execute(
                        "UPDATE mistakes SET revision=?,updated_at=? "
                        "WHERE id=? AND revision=?",
                        (revision, previous_updated_at, mistake_id, revision + 1),
                    )
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
    return {
        "public_id": public_id,
        "mime_type": mime,
        "extension": extension,
        "byte_size": len(data),
        "width": width,
        "height": height,
        "sha256": digest,
        "sort_order": sort_order,
        "revision": revision + 1,
    }


def purge_deleted_mistake(db, root: Path, public_id: str, revision: int) -> None:
    """Permanently delete a tombstone while staging attachment files for recovery."""

    root.mkdir(parents=True, exist_ok=True)
    operation = str(uuid.uuid4())
    staging = root / f".purge-{operation}"
    moved: list[tuple[Path, Path]] = []
    try:
        with immediate_transaction(db):
            mistake = db.execute(
                "SELECT id,revision FROM mistakes "
                "WHERE public_id=? AND deleted_at IS NOT NULL",
                (public_id,),
            ).fetchone()
            if not mistake:
                raise AttachmentError(
                    "NOT_FOUND", "回收站记录不存在", status_code=404
                )
            if mistake["revision"] != revision:
                raise AttachmentError(
                    "REVISION_CONFLICT", "错题已被其他操作更新", status_code=409
                )
            rows = db.execute(
                "SELECT object_key FROM attachments WHERE mistake_id=?",
                (mistake["id"],),
            ).fetchall()
            keys = [str(row["object_key"]) for row in rows]
            if any(
                Path(key).name != key or key in {"", ".", ".."} for key in keys
            ):
                raise AttachmentError("ATTACHMENT_PATH", "附件存储键无效")
            staging.mkdir()
            (staging / "manifest.json").write_text(
                json.dumps(
                    {"public_id": public_id, "object_keys": keys},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            for key in keys:
                source = root / key
                target = staging / key
                if source.is_file():
                    os.replace(source, target)
                    moved.append((target, source))
            db.execute("DELETE FROM mistakes WHERE id=?", (mistake["id"],))
    except Exception:
        for source, target in reversed(moved):
            if source.is_file() and not target.exists():
                os.replace(source, target)
        if staging.exists():
            shutil.rmtree(staging)
        raise
    shutil.rmtree(staging)


def _reconcile_purge_directories(connection, root: Path) -> tuple[int, int]:
    restored = removed = 0
    for staging in sorted(root.glob(".purge-*")):
        if not staging.is_dir():
            continue
        try:
            manifest = json.loads((staging / "manifest.json").read_text(encoding="utf-8"))
            public_id = str(manifest["public_id"])
            keys = [str(item) for item in manifest.get("object_keys", [])]
            if any(Path(key).name != key or key in {"", ".", ".."} for key in keys):
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        exists = connection.execute(
            "SELECT 1 FROM mistakes WHERE public_id=?", (public_id,)
        ).fetchone()
        if exists:
            for key in keys:
                source = staging / key
                target = root / key
                if source.is_file() and not target.exists():
                    os.replace(source, target)
            restored += 1
        else:
            removed += 1
        shutil.rmtree(staging)
    return restored, removed


def reconcile_attachments(database: str, attachment_dir: str) -> dict[str, int]:
    root = Path(attachment_dir)
    root.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database, role="maintenance")
    repaired = removed = 0
    try:
        purge_restored, purge_removed = _reconcile_purge_directories(connection, root)
        rows = connection.execute(
            "SELECT public_id,object_key FROM attachments WHERE status='staged'"
        ).fetchall()
        with connection:
            for row in rows:
                final = root / row["object_key"]
                staged = root / f".{row['object_key']}.staged"
                if final.is_file():
                    connection.execute(
                        "UPDATE attachments SET status='published' WHERE public_id=?",
                        (row["public_id"],),
                    )
                    repaired += 1
                elif staged.is_file():
                    os.replace(staged, final)
                    connection.execute(
                        "UPDATE attachments SET status='published' WHERE public_id=?",
                        (row["public_id"],),
                    )
                    repaired += 1
                else:
                    connection.execute(
                        "DELETE FROM attachments WHERE public_id=?",
                        (row["public_id"],),
                    )
                    removed += 1
    finally:
        connection.close()
    return {
        "repaired": repaired,
        "removed": removed,
        "purge_restored": purge_restored,
        "purge_removed": purge_removed,
    }
