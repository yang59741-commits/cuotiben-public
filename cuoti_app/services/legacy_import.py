"""Legacy backup import use case shared by HTTP and durable jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..transactions import begin_immediate
from ..validation import ValidationError
from .import_validation import resolve_import_catalog, validate_import_mistake
from .job_results import load_business_result, store_business_result


MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_IMPORT_MISTAKES = 10_000


class LegacyImportError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    payload: dict[str, object]
    idempotency_replayed: bool = False

    def response_payload(self) -> dict[str, object]:
        if not self.idempotency_replayed:
            return dict(self.payload)
        return {**self.payload, "idempotency_replayed": True}


def _parse_legacy_backup(raw: bytes) -> list[dict]:
    if len(raw) > MAX_IMPORT_BYTES:
        raise LegacyImportError("导入文件不能超过 10 MB", 413)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError("导入文件不是有效的 UTF-8 JSON") from exc
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict):
        if parsed.get("schema_version") != 2:
            raise LegacyImportError("不支持的备份版本")
        items = parsed.get("mistakes")
        if not isinstance(items, list):
            raise LegacyImportError("备份中的 mistakes 必须是数组")
    else:
        raise LegacyImportError("备份必须是旧版数组或版本化对象")
    if len(items) > MAX_IMPORT_MISTAKES:
        raise LegacyImportError("一次最多导入 10000 道题", 413)
    return items


def import_legacy_backup(
    db,
    *,
    raw: bytes,
    mode: str,
    source_job_id: str | None = None,
    progress=None,
) -> LegacyImportResult:
    """Validate and atomically merge/replace one legacy JSON backup."""

    items = _parse_legacy_backup(raw)
    if mode not in {"merge", "replace"}:
        raise LegacyImportError("mode 必须是 merge 或 replace")
    try:
        replay = load_business_result(db, source_job_id, "data-import")
    except ValueError as exc:
        raise LegacyImportError(str(exc), 409) from exc
    if replay is not None:
        return LegacyImportResult(replay, idempotency_replayed=True)
    if progress is not None:
        progress(1, 4, "导入文件解析完成")
    subjects, chapters = resolve_import_catalog(db)
    try:
        validated = [
            validate_import_mistake(item, index, subjects, chapters)
            for index, item in enumerate(items, start=1)
        ]
    except ValidationError as exc:
        raise LegacyImportError(str(exc)) from exc
    if progress is not None:
        progress(2, 4, "导入内容校验完成")

    try:
        begin_immediate(db)
        existing = {
            row["fingerprint"]
            for row in db.execute(
                "SELECT fingerprint FROM mistakes WHERE fingerprint IS NOT NULL"
            ).fetchall()
        }
        old_count = db.execute("SELECT COUNT(*) AS n FROM mistakes").fetchone()["n"]
        if progress is not None:
            progress(3, 4, "正在原子写入")
        if mode == "replace":
            db.execute("DELETE FROM mistakes")
        inserted = skipped = 0
        seen = set()
        for mistake in validated:
            fingerprint = mistake["fingerprint"]
            if fingerprint in seen or (mode == "merge" and fingerprint in existing):
                skipped += 1
                continue
            seen.add(fingerprint)
            cursor = db.execute(
                "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,"
                "note,date_added,status,review_count,last_review,next_review,"
                "correct_streak,lapse_count,fingerprint,review_stage,review_cycle,"
                "schedule_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                mistake["values"]
                + (
                    mistake["review_stage"],
                    mistake["review_cycle"],
                    mistake["schedule_version"],
                ),
            )
            for event in mistake["reviews"]:
                db.execute(
                    "INSERT INTO review_events(mistake_id,event_type,reviewed_at,"
                    "status_after,correct_streak) VALUES(?,?,?,?,?)",
                    (cursor.lastrowid, *event),
                )
            inserted += 1
        result = {
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "replaced": old_count if mode == "replace" else 0,
        }
        store_business_result(db, source_job_id, "data-import", result)
        db.commit()
    except LegacyImportError:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise LegacyImportError("导入写入失败，数据未更改", 500) from exc
    if progress is not None:
        progress(4, 4, "导入已提交")
    return LegacyImportResult(result)
