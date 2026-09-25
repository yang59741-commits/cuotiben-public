"""Exam catalog, detail, and deletion use cases shared by HTTP and jobs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..pagination import CursorError, decode_cursor, encode_cursor
from ..transactions import begin_immediate
from .exam_artifacts import recompile_exam
from .exam_scoring import public_points
from .exam_paper_layout import question_display_content


@dataclass(frozen=True)
class ExamListFilters:
    title: str = ""
    subject_id: int | None = None
    source: str = ""
    date_from: str = ""
    date_to: str = ""

    def cursor_values(self) -> dict[str, object]:
        return {
            "title": self.title,
            "subject_id": self.subject_id,
            "source": self.source,
            "date_from": self.date_from,
            "date_to": self.date_to,
        }


def _generation_timing(db, generation_job_id: str | None) -> dict | None:
    """Return only a completed public timing receipt for a generated exam.

    Historical exams may reference jobs created before timing receipts existed.
    Those rows deliberately keep the old detail shape instead of exposing an
    empty or partially accumulated timing object.
    """

    job_id = str(generation_job_id or "").strip()
    if not job_id:
        return None
    row = db.execute(
        "SELECT result_json,exam_timing_json FROM background_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    candidates = []
    try:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        if isinstance(result, dict):
            candidates.append(result.get("timing"))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    try:
        candidates.append(
            json.loads(row["exam_timing_json"])
            if row["exam_timing_json"]
            else None
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    for candidate in candidates:
        if (
            isinstance(candidate, dict)
            and candidate.get("schema_version") == "exam-timing-receipt-v1"
            and isinstance(candidate.get("total_elapsed_ms"), int)
        ):
            return candidate
    return None


def _generation_adaptation_audit(db, generation_job_id: str | None) -> dict | None:
    """Project only the public, non-solution part of the adaptation receipt."""

    job_id = str(generation_job_id or "").strip()
    if not job_id:
        return None
    row = db.execute(
        "SELECT result_json FROM background_jobs WHERE id=?",
        (job_id,),
    ).fetchone()
    if row is None or not row["result_json"]:
        return None
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict):
        return None
    receipt = result.get("adaptation")
    if (
        not isinstance(receipt, dict)
        or receipt.get("contract_version") != "exam-tail-adaptation-v2"
        or receipt.get("requested") is not True
    ):
        return None
    if receipt.get("completed") is True:
        position = receipt.get("controlled_exception_position")
        if (
            isinstance(position, bool)
            or not isinstance(position, int)
            or position < 1
            or receipt.get("difficulty_recheck_performed") is not False
            or receipt.get("single_item_fallback") is not False
            or receipt.get("verification_reason_codes") != ["none"]
        ):
            return None
        return {
            "status": "accepted",
            "controlled_exception_position": position,
            "difficulty_recheck_performed": False,
            "verification_status": "passed",
        }
    if (
        receipt.get("completed") is False
        and receipt.get("single_item_fallback") is True
    ):
        return {
            "status": "reverted",
            "controlled_exception_position": None,
        }
    return None

def exam_summary(row: dict) -> dict:
    try:
        config = json.loads(row.get("config") or "{}")
    except (TypeError, json.JSONDecodeError):
        config = {}
    if not isinstance(config, dict):
        config = {}
    chapters = config.get("chapters") or []
    if not isinstance(chapters, list):
        chapters = []
    return {
        "chapters": [str(value) for value in chapters],
        "question_count": int(
            config.get("question_count") or row.get("item_count") or 0
        ),
        "difficulty": str(config.get("difficulty") or ""),
        "candidate_source": str(config.get("candidate_source") or ""),
        "reference_year": config.get("reference_year"),
        "generation_mode": str(config.get("source") or "unknown"),
        "training_target": str(config.get("training_target") or ""),
        "training_target_counts": dict(
            (config.get("quality_review") or {}).get("training_target_counts") or {}
        ),
        "training_target_findings": list(
            (config.get("quality_review") or {}).get("training_target_findings") or []
        ),
    }


def serialize_exam(row: dict, *, include_content: bool = True) -> dict:
    item = dict(row)
    item["summary"] = exam_summary(item)
    item["pdf_url"] = f"/pdfs/exam_{item['id']}.pdf"
    if item.get("scoring_mode") != "verified":
        item["total_points"] = None
    if not include_content:
        item.pop("content", None)
    return item


def subject_ids_by_name(db) -> dict[str, int]:
    return {
        str(row["name"]): int(row["id"])
        for row in db.execute("SELECT id,name FROM subjects").fetchall()
    }


def list_legacy_exam_candidates(db, *, where, params) -> list[dict]:
    sql = (
        "SELECT m.*,s.name as subject,c.name as chapter FROM mistakes m "
        "LEFT JOIN subjects s ON m.subject_id=s.id "
        "LEFT JOIN chapters c ON m.chapter_id=c.id WHERE "
        + " AND ".join(where) + " ORDER BY m.id DESC LIMIT 100"
    )
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def list_exam_candidates(
    db,
    *,
    where: list[str],
    params: list[object],
    filters: dict,
    limit: int,
    after: str | None,
) -> dict:
    cursor = decode_cursor(after, filters=filters)
    if cursor is None:
        snapshot = db.execute(
            "SELECT COALESCE(MAX(m.id),0) AS n FROM mistakes m WHERE "
            + " AND ".join(where),
            params,
        ).fetchone()["n"]
        last_id = snapshot + 1
    else:
        snapshot = cursor["s"]
        if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
            raise CursorError("分页游标无效")
        last_id = cursor["p"][0]
    page_where = where + ["m.id<=?", "m.id<?"]
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT m.*,s.name AS subject,c.name AS chapter FROM mistakes m "
            "LEFT JOIN subjects s ON m.subject_id=s.id "
            "LEFT JOIN chapters c ON m.chapter_id=c.id WHERE "
            + " AND ".join(page_where)
            + " ORDER BY m.id DESC LIMIT ?",
            params + [snapshot, last_id, limit + 1],
        ).fetchall()
    ]
    has_more = len(rows) > limit
    items = rows[:limit]
    total = db.execute(
        "SELECT COUNT(*) AS n FROM mistakes m WHERE "
        + " AND ".join(where + ["m.id<=?"]),
        params + [snapshot],
    ).fetchone()["n"]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(
            filters=filters, snapshot=snapshot, values=[items[-1]["id"]]
        )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total": total,
    }


def _where_clause(filters: ExamListFilters, *, deleted: bool = False) -> tuple[str, list[object]]:
    where = [
        "e.artifact_status='published'",
        "e.deleted_at IS NOT NULL" if deleted else "e.deleted_at IS NULL",
    ]
    params: list[object] = []
    if filters.title:
        escaped = (
            filters.title.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        where.append("e.title LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")
    if filters.subject_id is not None:
        where.append("e.subject_id=?")
        params.append(filters.subject_id)
    if filters.source:
        where.append("COALESCE(json_extract(e.config,'$.source'),'')=?")
        params.append(filters.source)
    if filters.date_from:
        where.append("substr(e.created_at,1,10)>=?")
        params.append(filters.date_from)
    if filters.date_to:
        where.append("substr(e.created_at,1,10)<=?")
        params.append(filters.date_to)
    return " AND ".join(where), params


def list_exams(
    db,
    filters: ExamListFilters,
    *,
    limit: int,
    after: str | None,
) -> dict:
    """Return one immutable cursor page of published exam summaries."""
    cursor_filters = filters.cursor_values()
    cursor = decode_cursor(after, filters=cursor_filters)
    where_sql, params = _where_clause(filters)
    if cursor is None:
        snapshot = db.execute(
            f"SELECT COALESCE(MAX(e.id),0) AS n FROM exams e WHERE {where_sql}",
            params,
        ).fetchone()["n"]
        last_id = snapshot + 1
    else:
        snapshot = cursor["s"]
        if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
            raise CursorError("分页游标无效")
        last_id = cursor["p"][0]
    rows = [
        serialize_exam(dict(row), include_content=False)
        for row in db.execute(
            "SELECT e.*,s.name AS subject,(SELECT COUNT(*) FROM exam_items i "
            "WHERE i.exam_id=e.id) AS item_count FROM exams e "
            "LEFT JOIN subjects s ON e.subject_id=s.id "
            f"WHERE {where_sql} AND e.id<=? AND e.id<? "
            "ORDER BY e.id DESC LIMIT ?",
            (*params, snapshot, last_id, limit + 1),
        ).fetchall()
    ]
    has_more = len(rows) > limit
    items = rows[:limit]
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM exams e WHERE {where_sql} AND e.id<=?",
        (*params, snapshot),
    ).fetchone()["n"]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(
            filters=cursor_filters,
            snapshot=snapshot,
            values=[items[-1]["id"]],
        )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total": total,
    }


def list_deleted_exams(
    db,
    filters: ExamListFilters,
    *,
    limit: int,
    after: str | None,
) -> dict:
    """Return one cursor page of soft-deleted published exam metadata."""

    cursor_filters = {**filters.cursor_values(), "trash": "exams"}
    cursor = decode_cursor(after, filters=cursor_filters)
    where_sql, params = _where_clause(filters, deleted=True)
    if cursor is None:
        snapshot = db.execute(
            f"SELECT COALESCE(MAX(e.id),0) AS n FROM exams e WHERE {where_sql}",
            params,
        ).fetchone()["n"]
        last_id = snapshot + 1
    else:
        snapshot = cursor["s"]
        if len(cursor["p"]) != 1 or not isinstance(cursor["p"][0], int):
            raise CursorError("分页游标无效")
        last_id = cursor["p"][0]
    rows = [
        dict(row)
        for row in db.execute(
            "SELECT e.id,e.title,e.subject_id,e.created_at,e.deleted_at,"
            "e.artifact_status,e.config,s.name AS subject,"
            "(SELECT COUNT(*) FROM exam_items i WHERE i.exam_id=e.id) AS item_count "
            "FROM exams e LEFT JOIN subjects s ON e.subject_id=s.id "
            f"WHERE {where_sql} AND e.id<=? AND e.id<? "
            "ORDER BY e.id DESC LIMIT ?",
            (*params, snapshot, last_id, limit + 1),
        ).fetchall()
    ]
    has_more = len(rows) > limit
    items = rows[:limit]
    for item in items:
        item["summary"] = exam_summary(item)
    total = db.execute(
        f"SELECT COUNT(*) AS n FROM exams e WHERE {where_sql} AND e.id<=?",
        (*params, snapshot),
    ).fetchone()["n"]
    next_cursor = None
    if has_more and items:
        next_cursor = encode_cursor(
            filters=cursor_filters,
            snapshot=snapshot,
            values=[items[-1]["id"]],
        )
    return {
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "total": total,
    }


def get_exam_detail(db, exam_id: int) -> dict | None:
    row = db.execute(
        "SELECT e.*,s.name as subject,(SELECT COUNT(*) FROM exam_items i "
        "WHERE i.exam_id=e.id) AS item_count FROM exams e "
        "LEFT JOIN subjects s ON e.subject_id=s.id "
        "WHERE e.id=? AND e.artifact_status='published' AND e.deleted_at IS NULL",
        (exam_id,),
    ).fetchone()
    if row is None:
        return None
    result = serialize_exam(dict(row))
    timing = _generation_timing(db, result.get("generation_job_id"))
    if timing is not None:
        result["timing"] = timing
    adaptation_audit = _generation_adaptation_audit(
        db, result.get("generation_job_id")
    )
    if adaptation_audit is not None:
        result["adaptation_audit"] = adaptation_audit
    items = []
    for item_row in db.execute(
        "SELECT public_id,position,question_type,difficulty,content,content_hash,"
        "points,points_canonical,points_evidence_hash,source_kind,"
        "source_mistake_public_id,source_candidate_id,"
        "source_metadata_json FROM exam_items WHERE exam_id=? ORDER BY position",
        (exam_id,),
    ).fetchall():
        item = dict(item_row)
        item["points"] = public_points(result.get("scoring_mode"), item.get("points"))
        try:
            item["source_metadata"] = json.loads(
                item.pop("source_metadata_json")
            )
        except (TypeError, json.JSONDecodeError):
            item["source_metadata"] = {}
            item.pop("source_metadata_json", None)
        lineage_rows = db.execute(
            "SELECT mistake_public_id,source_lineage_hash,lineage_policy_version,"
            "content_fingerprint FROM exam_item_mistake_lineage "
            "WHERE exam_item_id=(SELECT id FROM exam_items WHERE public_id=?) "
            "ORDER BY source_order",
            (item["public_id"],),
        ).fetchall()
        if lineage_rows:
            item["source_mistake_ids"] = [
                str(lineage["mistake_public_id"]) for lineage in lineage_rows
            ]
            item["source_lineage_hash"] = str(lineage_rows[0]["source_lineage_hash"])
            item["lineage_policy_version"] = str(
                lineage_rows[0]["lineage_policy_version"]
            )
            item["content_fingerprint"] = str(lineage_rows[0]["content_fingerprint"])
            item["mistake_coverage_status"] = "authoritative"
        elif item.get("source_mistake_public_id"):
            # Historical read-only projection; never promoted into v47 authority.
            item["source_mistake_ids"] = [str(item["source_mistake_public_id"])]
            item["source_lineage_hash"] = None
            item["lineage_policy_version"] = None
            item["content_fingerprint"] = None
            item["mistake_coverage_status"] = "legacy_single"
        else:
            item["source_mistake_ids"] = []
            item["source_lineage_hash"] = None
            item["lineage_policy_version"] = None
            item["content_fingerprint"] = None
            item["mistake_coverage_status"] = "not_applicable"
        items.append(item)
    result["items"] = items
    authoritative = sum(
        item["mistake_coverage_status"] == "authoritative" for item in items
    )
    result["mistake_lineage"] = {
        "authoritative_item_count": authoritative,
        "item_count": len(items),
        "complete": bool(items) and authoritative == len(items),
        "source_count": len({
            source_id
            for item in items
            for source_id in item["source_mistake_ids"]
        }),
    }
    return result


def published_exam_exists(db, exam_id: int) -> bool:
    return (
        db.execute(
            "SELECT 1 FROM exams WHERE id=? AND artifact_status='published' "
            "AND deleted_at IS NULL",
            (exam_id,),
        ).fetchone()
        is not None
    )


def _soft_delete_timestamp(value: str | None = None) -> str:
    timestamp = value or datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if not isinstance(timestamp, str) or not timestamp.strip() or timestamp != timestamp.strip():
        raise ValueError("deleted_at must be non-empty text")
    return timestamp


def delete_published_exam(
    db,
    output_dir: str | Path | None,
    exam_id: int,
    *,
    memory=None,
    deleted_at: str | None = None,
) -> bool:
    """Soft-delete a published exam without changing files or related facts."""

    del output_dir, memory
    timestamp = _soft_delete_timestamp(deleted_at)
    try:
        cursor = db.execute(
            "UPDATE exams SET deleted_at=? WHERE id=? "
            "AND artifact_status='published' AND deleted_at IS NULL",
            (timestamp, exam_id),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return cursor.rowcount == 1


def restore_published_exam(db, exam_id: int) -> bool:
    """Restore one soft-deleted published exam under its original identity."""

    try:
        cursor = db.execute(
            "UPDATE exams SET deleted_at=NULL WHERE id=? "
            "AND artifact_status='published' AND deleted_at IS NOT NULL",
            (exam_id,),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return cursor.rowcount == 1


class ExamRecompileError(RuntimeError):
    code = "recompile_failed"


class ExamRecompileNotFound(ExamRecompileError):
    code = "exam_not_found"


class ExamRecompileUnavailable(ExamRecompileError):
    code = "recompile_unavailable"


class ExamRecompileConflict(ExamRecompileError):
    code = "recompile_conflict"


@dataclass(frozen=True, slots=True)
class ExamRecompileResult:
    exam_id: int
    artifact_revision: int
    latex_sha256: str | None
    pdf_sha256: str | None
    idempotency_replayed: bool = False

    def job_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "exam_id": self.exam_id,
            "artifact_revision": self.artifact_revision,
            "pdf_sha256": self.pdf_sha256,
        }
        if self.idempotency_replayed:
            payload["idempotency_replayed"] = True
        else:
            payload["latex_sha256"] = self.latex_sha256
        return payload


def recompile_published_exam(
    db,
    *,
    pdf_dir,
    command: str,
    exam_id: int,
    job_id: str,
    timeout: int = 60,
    runner=None,
    on_started=None,
    on_compiled=None,
) -> ExamRecompileResult:
    """Recompile one frozen source and publish files with its database revision."""

    row = db.execute(
        "SELECT id,artifact_status,latex_source,latex_sha256,recompile_available,"
        "artifact_revision,pdf_sha256,last_recompile_job_id FROM exams WHERE id=? "
        "AND deleted_at IS NULL",
        (exam_id,),
    ).fetchone()
    if not row or row["artifact_status"] != "published":
        raise ExamRecompileNotFound("试卷不存在或尚未发布")
    old_revision = int(row["artifact_revision"] or 1)
    if row["last_recompile_job_id"] == job_id:
        return ExamRecompileResult(
            exam_id=exam_id,
            artifact_revision=old_revision,
            latex_sha256=row["latex_sha256"],
            pdf_sha256=row["pdf_sha256"],
            idempotency_replayed=True,
        )
    if not row["recompile_available"] or not row["latex_source"]:
        raise ExamRecompileUnavailable("旧试卷缺少受控 LaTeX 源，无法重新编译")

    latex_source = str(row["latex_source"])
    if on_started is not None:
        on_started()

    def publish(hashes: dict[str, str]) -> None:
        begin_immediate(db)
        try:
            cursor = db.execute(
                "UPDATE exams SET latex_sha256=?,pdf_sha256=?,artifact_revision=?,"
                "recompile_available=1,last_recompile_job_id=? WHERE id=? "
                "AND artifact_status='published' AND artifact_revision=? "
                "AND latex_source=? AND deleted_at IS NULL",
                (
                    hashes["latex_sha256"],
                    hashes["pdf_sha256"],
                    old_revision + 1,
                    job_id,
                    exam_id,
                    old_revision,
                    latex_source,
                ),
            )
            if cursor.rowcount != 1:
                raise ExamRecompileConflict("试卷在重新编译期间发生变化")
            db.commit()
        except Exception:
            db.rollback()
            raise

    hashes = recompile_exam(
        str(pdf_dir),
        command,
        exam_id,
        latex_source,
        timeout=timeout,
        runner=runner,
        on_compiled=on_compiled,
        publish=publish,
        previous_hashes={
            ".tex": row["latex_sha256"],
            ".pdf": row["pdf_sha256"],
        },
    )
    return ExamRecompileResult(
        exam_id=exam_id,
        artifact_revision=old_revision + 1,
        latex_sha256=hashes["latex_sha256"],
        pdf_sha256=hashes["pdf_sha256"],
    )


# Historical HTTP views intentionally retain their existing fields and points.
def _serialize_http_exam(row, *, include_content=True):
    item = dict(row)
    item["summary"] = exam_summary(item)
    item["pdf_url"] = f"/pdfs/exam_{item['id']}.pdf"
    if not include_content:
        item.pop("content", None)
    return item


def list_legacy_http_exams(db):
    rows = db.execute(
        "SELECT e.*,s.name as subject,(SELECT COUNT(*) FROM exam_items i "
        "WHERE i.exam_id=e.id) AS item_count FROM exams e "
        "LEFT JOIN subjects s ON e.subject_id=s.id "
        "WHERE e.artifact_status='published' AND e.deleted_at IS NULL "
        "ORDER BY e.id DESC LIMIT 50"
    ).fetchall()
    return [_serialize_http_exam(dict(row)) for row in rows]


def get_http_exam_detail(db, exam_id: int) -> dict | None:
    r = db.execute(
        "SELECT e.*,s.name as subject,(SELECT COUNT(*) FROM exam_items i "
        "WHERE i.exam_id=e.id) AS item_count FROM exams e "
        "LEFT JOIN subjects s ON e.subject_id=s.id "
        "WHERE e.id=? AND e.artifact_status='published' AND e.deleted_at IS NULL",
        (exam_id,),
    ).fetchone()
    if not r:
        return None
    d = _serialize_http_exam(dict(r))
    items = []
    for row in db.execute(
        "SELECT public_id,position,question_type,difficulty,content,content_hash,"
        "points,source_kind,source_mistake_public_id,source_candidate_id,"
        "source_metadata_json FROM exam_items WHERE exam_id=? ORDER BY position",
        (exam_id,),
    ).fetchall():
        item = dict(row)
        try:
            item["source_metadata"] = json.loads(item.pop("source_metadata_json"))
        except (TypeError, json.JSONDecodeError):
            item["source_metadata"] = {}
            item.pop("source_metadata_json", None)
        if item.get('source_kind') in {'ordinary', 'past_exam_local', 'past_exam_other'}:
            item['display_content'] = question_display_content(
                item['content'], candidate_id=item.get('source_candidate_id')
            )
        items.append(item)
    d["items"] = items
    return d
