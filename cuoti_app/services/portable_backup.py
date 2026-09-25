"""Portable v5 ZIP export with mistakes, attachments, and daily tips."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from ..utils import file_sha256
from ..transactions import immediate_transaction


PORTABLE_BACKUP_FORMAT_VERSION = 7


class PortableBackupError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _safe_object_key(value: object) -> str:
    key = str(value or "")
    if not key or key in {".", ".."} or Path(key).name != key:
        raise PortableBackupError("ATTACHMENT_PATH", "附件对象键不安全，无法生成备份")
    return key


def write_portable_backup(db, attachment_root: Path, output) -> dict[str, int]:
    """Write one coherent portable ZIP to a seekable binary stream."""

    attachment_root = attachment_root.resolve()
    with immediate_transaction(db):
        mistakes = db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON s.id=m.subject_id "
            "LEFT JOIN chapters c ON c.id=m.chapter_id "
            "WHERE m.deleted_at IS NULL ORDER BY m.public_id"
        ).fetchall()
        exported = []
        attachment_files: list[tuple[Path, str, int]] = []
        for row in mistakes:
            item = {
                key: row[key]
                for key in (
                    "public_id", "revision", "updated_at", "history_complete",
                    "history_complete_from",
                    "content_fingerprint", "content_fingerprint_version", "subject",
                    "chapter", "qnum", "content", "note",
                    "source_kind", "source_ref", "difficulty", "date_added", "status",
                    "review_count", "last_review", "next_review", "correct_streak",
                    "lapse_count", "review_stage", "review_cycle", "schedule_version",
                )
            }
            item["reasons"] = [
                value["reason_code"]
                for value in db.execute(
                    "SELECT reason_code FROM mistake_reason_items WHERE mistake_id=? "
                    "ORDER BY reason_code",
                    (row["id"],),
                ).fetchall()
            ]
            item["tags"] = [
                value["name"]
                for value in db.execute(
                    "SELECT t.name FROM mistake_tags mt JOIN tags t ON t.id=mt.tag_id "
                    "WHERE mt.mistake_id=? ORDER BY t.normalized_name",
                    (row["id"],),
                ).fetchall()
            ]
            item["reviews"] = [
                dict(value)
                for value in db.execute(
                    "SELECT public_id,client_event_id,event_type,reviewed_at,created_at,"
                    "status_after,correct_streak,lapse_count_after,source_import_id "
                    ",stage_before,stage_after,cycle_after,next_review_after,schedule_version,assistance,evidence_version,reviewed_on,content_fingerprint "
                    "FROM review_events WHERE mistake_id=? ORDER BY created_at,id",
                    (row["id"],),
                ).fetchall()
            ]
            analysis = db.execute(
                "SELECT analysis_date,analysis_schema_version,prompt_version,source_hash,"
                "analysis_json,source_job_id,model_role,model_name,generated_at,updated_at "
                "FROM daily_analysis_items WHERE mistake_id=?",
                (row["id"],),
            ).fetchone()
            item["daily_analysis"] = None
            from .mistake_classification import get_classification, type_dict
            typed = get_classification(db, row)
            item['classification'] = None
            if typed['revision']:
                definition = db.execute('SELECT * FROM question_types WHERE code=?', (typed['primary_type_code'],)).fetchone()
                item['classification'] = {key: typed[key] for key in ('primary_type_code', 'knowledge_points', 'methods', 'source_hash', 'locked')}
                item['classification']['question_type'] = type_dict(definition) if definition else None
            if analysis:
                item["daily_analysis"] = {
                    "analysis_date": analysis["analysis_date"],
                    "analysis_schema_version": analysis["analysis_schema_version"],
                    "prompt_version": analysis["prompt_version"],
                    "source_hash": analysis["source_hash"],
                    "analysis": json.loads(analysis["analysis_json"]),
                    "source_job_id": analysis["source_job_id"],
                    "model_role": analysis["model_role"],
                    "model_name": analysis["model_name"],
                    "generated_at": analysis["generated_at"],
                    "updated_at": analysis["updated_at"],
                }
            item["attachments"] = []
            for attachment in db.execute(
                "SELECT public_id,object_key,mime_type,extension,byte_size,width,height,"
                "sha256,sort_order,created_at FROM attachments WHERE mistake_id=? "
                "AND status='published' ORDER BY sort_order,id",
                (row["id"],),
            ).fetchall():
                key = _safe_object_key(attachment["object_key"])
                path = (attachment_root / key).resolve()
                try:
                    path.relative_to(attachment_root)
                except ValueError as exc:
                    raise PortableBackupError(
                        "ATTACHMENT_PATH", "附件路径超出存储目录，无法生成备份"
                    ) from exc
                if not path.is_file():
                    raise PortableBackupError(
                        "ATTACHMENT_MISSING", "附件文件缺失，备份未生成"
                    )
                size = path.stat().st_size
                digest = file_sha256(path)
                if size != attachment["byte_size"] or digest != attachment["sha256"]:
                    raise PortableBackupError(
                        "ATTACHMENT_INTEGRITY", "附件大小或哈希校验失败，备份未生成"
                    )
                archive_path = f"attachments/{key}"
                metadata = {
                    field: attachment[field]
                    for field in (
                        "public_id", "mime_type", "extension", "byte_size", "width",
                        "height", "sha256", "sort_order", "created_at",
                    )
                }
                metadata["archive_path"] = archive_path
                item["attachments"].append(metadata)
                attachment_files.append((path, archive_path, size))
            exported.append(item)

        daily_tips = [
            dict(row)
            for row in db.execute(
                "SELECT public_id,content,content_hash,revision,created_at,updated_at "
                "FROM daily_tips ORDER BY content_hash,public_id"
            ).fetchall()
        ]

        manifest = {
            "format": "cuoti-portable-backup",
            "format_version": PORTABLE_BACKUP_FORMAT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "mistakes": exported,
            "daily_tips": daily_tips,
        }
        with zipfile.ZipFile(
            output, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            )
            for path, archive_path, _size in attachment_files:
                archive.write(path, archive_path)
        output.flush()
        os.fsync(output.fileno())
    return {
        "mistakes": len(exported),
        "attachments": len(attachment_files),
        "daily_tips": len(daily_tips),
    }
