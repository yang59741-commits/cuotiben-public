"""Publication coordination helpers for generated exams."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable, Sequence

from ..utils import file_sha256
from .exam_manifests import (
    ExamManifestError,
    append_exam_generation_receipt,
    is_versioned_exam_manifest,
    load_exam_generation_manifest,
)
from .job_contracts import JobError


class ExamReceiptRecoveryPending(RuntimeError):
    """A published exam exists but its durable receipt still needs recovery."""


def _replay_batch_is_complete(rows: Sequence[Any], pdf_dir: str | Path) -> bool:
    expected_sizes = set()
    batch_indices = []
    for row in rows:
        try:
            config = json.loads(row["config"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(config, dict):
            return False
        expected = config.get("batch_size", 1)
        if isinstance(expected, bool) or not isinstance(expected, int):
            return False
        if expected < 1 or expected > 20:
            return False
        expected_sizes.add(expected)
        index = row["batch_index"]
        if isinstance(index, bool) or not isinstance(index, int):
            return False
        batch_indices.append(index)
    if len(expected_sizes) != 1:
        return False
    expected = next(iter(expected_sizes))
    if len(rows) != expected or batch_indices != list(range(1, expected + 1)):
        return False
    if any(row["artifact_status"] != "published" for row in rows):
        return False
    root = Path(pdf_dir)
    for row in rows:
        pdf_path = root / f"exam_{row['id']}.pdf"
        try:
            if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
                return False
            keys = row.keys() if hasattr(row, "keys") else ()
            expected_hash = row["pdf_sha256"] if "pdf_sha256" in keys else None
            if expected_hash and file_sha256(pdf_path) != expected_hash:
                return False
        except OSError:
            return False
    return True


def confirm_exam_publication_receipt(
    database_path: str | Path,
    generation_job_id: str | None,
    ordered_exam_ids: Sequence[int],
    *,
    receipt_writer: Callable[..., Any] = append_exam_generation_receipt,
) -> None:
    """Confirm a published batch without owning or rolling back its outputs."""

    if not generation_job_id:
        return
    try:
        loaded = load_exam_generation_manifest(database_path, generation_job_id)
        if is_versioned_exam_manifest(loaded):
            receipt_writer(
                database_path,
                generation_job_id,
                {
                    "status": "published",
                    "exam_ids": [int(exam_id) for exam_id in ordered_exam_ids],
                },
            )
    except JobError:
        raise
    except Exception as exc:
        raise ExamReceiptRecoveryPending("执行收据仍待恢复") from exc


def publish_exam_outputs(
    db,
    config,
    *,
    subject_id: int,
    source: str,
    generation_job_id: str | None,
    drafts: Sequence[Any],
    reference_mistakes: Sequence[dict[str, Any]],
    publish_batch: Callable[..., list[dict[str, Any]]],
    publish_kwargs: dict[str, Any],
    receipt_writer: Callable[..., Any] = append_exam_generation_receipt,
) -> list[dict[str, Any]]:
    """Coordinate draft validation, atomic publication and receipt recovery."""

    results = publish_batch(
        db,
        subject_id=subject_id,
        source=source,
        generation_job_id=generation_job_id,
        drafts=drafts,
        reference_mistakes=reference_mistakes,
        **publish_kwargs,
    )
    confirm_exam_publication_receipt(
        config["DATABASE"],
        generation_job_id,
        tuple(int(result["id"]) for result in results),
        receipt_writer=receipt_writer,
    )
    return results


def replay_published_job(db, config, generation_job_id):
    """Replay only a complete batch; startup recovery owns hard-crash residue."""

    if not generation_job_id:
        return None
    existing = [
        dict(row)
        for row in db.execute(
            "SELECT e.*,s.name AS subject FROM exams e "
            "LEFT JOIN subjects s ON e.subject_id=s.id "
            "WHERE e.generation_job_id=? "
            "ORDER BY e.batch_index,e.id",
            (generation_job_id,),
        ).fetchall()
    ]
    if not existing:
        return None
    if not _replay_batch_is_complete(existing, config["PDF_DIR"]):
        raise ExamReceiptRecoveryPending("试卷发布批次仍待恢复")
    loaded = load_exam_generation_manifest(config["DATABASE"], generation_job_id)
    if is_versioned_exam_manifest(loaded):
        try:
            append_exam_generation_receipt(
                config["DATABASE"],
                generation_job_id,
                {
                    "status": "published",
                    "exam_ids": [int(row["id"]) for row in existing],
                },
            )
        except ExamManifestError as exc:
            if exc.code != "MANIFEST_WRITE_FAILED":
                raise
            raise ExamReceiptRecoveryPending("执行收据仍待恢复") from exc
        except JobError:
            raise
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise ExamReceiptRecoveryPending("执行收据仍待恢复") from exc
    results = [
        {
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "pdf_url": f"/pdfs/exam_{row['id']}.pdf",
            "created_at": row["created_at"],
        }
        for row in existing
    ]
    response = dict(results[0])
    response["count"] = len(results)
    response["exams"] = results
    response["idempotency_replayed"] = True
    return response
