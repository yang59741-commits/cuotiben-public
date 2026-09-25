"""Immutable and legacy chapter attribution for published exam items."""

from __future__ import annotations

import json
import sqlite3


SNAPSHOT_VERSION = "exam-item-chapter-v1"


def frozen_chapter_snapshot(db, *, exam_id: int, chapter_name: object, evidence: str,
                            fact_hash: object = None) -> dict:
    name = str(chapter_name or "").strip()
    result = {"version": SNAPSHOT_VERSION, "status": "unknown", "chapter_id": None,
              "chapter_name": name, "evidence": str(evidence)}
    if fact_hash:
        result["fact_hash"] = str(fact_hash)
    if not name:
        return result
    row = db.execute(
        "SELECT c.id,c.name FROM chapters c JOIN exams e ON e.subject_id=c.subject_id "
        "WHERE e.id=? AND c.name=?", (exam_id, name),
    ).fetchone()
    if row:
        result.update(status="resolved", chapter_id=int(row["id"]), chapter_name=str(row["name"]))
    return result


def native_frozen_chapter_snapshot(db, *, exam_id: int, job_id: str,
                                   candidate_id: str) -> dict:
    try:
        row = db.execute(
            "SELECT chapter,fact_hash FROM exam_generation_stage4_candidate_facts "
            "WHERE job_id=? AND candidate_id=?", (job_id, candidate_id),
        ).fetchone()
    except sqlite3.Error:
        row = None
    return frozen_chapter_snapshot(
        db, exam_id=exam_id, chapter_name=row["chapter"] if row else None,
        evidence="stage4_candidate_fact", fact_hash=row["fact_hash"] if row else None,
    )


def single_exam_chapter_snapshot(db, *, exam_id: int) -> dict | None:
    row = db.execute("SELECT config FROM exams WHERE id=?", (exam_id,)).fetchone()
    try:
        config = json.loads(row["config"] or "{}") if row else {}
    except (TypeError, ValueError):
        return None
    raw = config.get("chapters") or config.get("chapter_ids") or []
    if isinstance(raw, (str, int)):
        raw = [raw]
    ids = [int(value) for value in raw if str(value).isdigit()]
    if len(set(ids)) != 1:
        return None
    chapter = db.execute(
        "SELECT c.name FROM chapters c JOIN exams e ON e.subject_id=c.subject_id "
        "WHERE e.id=? AND c.id=?", (exam_id, ids[0]),
    ).fetchone()
    if not chapter:
        return None
    return frozen_chapter_snapshot(
        db, exam_id=exam_id, chapter_name=chapter["name"],
        evidence="single_chapter_exam_scope",
    )


def resolve_exam_item_chapters(db, exam_item_ids) -> dict[int, dict]:
    ids = sorted({int(value) for value in exam_item_ids})
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    items = db.execute(
        f"SELECT id,source_mistake_public_id,source_metadata_json FROM exam_items WHERE id IN ({marks})",
        ids,
    ).fetchall()
    result: dict[int, dict] = {}
    unresolved: list[int] = []
    for row in items:
        item_id = int(row["id"])
        try:
            metadata = json.loads(row["source_metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        snapshot = metadata.get("chapter_snapshot") if isinstance(metadata, dict) else None
        if isinstance(snapshot, dict) and snapshot.get("version") == SNAPSHOT_VERSION:
            chapter_id = snapshot.get("chapter_id")
            resolved = snapshot.get("status") == "resolved" and chapter_id
            result[item_id] = {
                "status": "resolved" if resolved else "unknown",
                "chapter_id": int(chapter_id) if resolved else None,
                "chapter": str(snapshot.get("chapter_name") or ("" if resolved else "未归类")),
                "candidates": ([{"id": int(chapter_id), "name": str(snapshot.get("chapter_name") or ""),
                                  "evidence": "frozen_snapshot"}] if resolved else []),
                "evidence": "frozen_snapshot",
            }
        else:
            unresolved.append(item_id)
    if not unresolved:
        return result
    legacy_marks = ",".join("?" for _ in unresolved)
    rows = db.execute(
        "SELECT eml.exam_item_id,c.id AS chapter_id,c.name AS chapter "
        "FROM exam_item_mistake_lineage eml LEFT JOIN mistakes m ON m.public_id=eml.mistake_public_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id "
        f"WHERE eml.exam_item_id IN ({legacy_marks}) ORDER BY eml.exam_item_id,eml.source_order",
        unresolved,
    ).fetchall()
    by_item: dict[int, list] = {value: [] for value in unresolved}
    for row in rows:
        by_item[int(row["exam_item_id"])].append(row)
    item_by_id = {int(row["id"]): row for row in items}
    for item_id in unresolved:
        lineage = by_item[item_id]
        if not lineage and item_by_id[item_id]["source_mistake_public_id"]:
            lineage = db.execute(
                "SELECT c.id AS chapter_id,c.name AS chapter FROM mistakes m "
                "LEFT JOIN chapters c ON c.id=m.chapter_id WHERE m.public_id=?",
                (item_by_id[item_id]["source_mistake_public_id"],),
            ).fetchall()
        candidates = sorted({(int(row["chapter_id"]), str(row["chapter"]))
                             for row in lineage if row["chapter_id"] is not None})
        has_unknown = any(row["chapter_id"] is None for row in lineage)
        if len(candidates) == 1 and not has_unknown:
            chapter_id, name = candidates[0]
            result[item_id] = {"status": "legacy_current_source", "chapter_id": chapter_id,
                               "chapter": name,
                               "candidates": [{"id": chapter_id, "name": name,
                                               "evidence": "legacy_current_source"}],
                               "evidence": "legacy_current_source"}
        elif candidates or lineage:
            result[item_id] = {"status": "multi", "chapter_id": None, "chapter": "多章节",
                               "candidates": [{"id": value, "name": name,
                                               "evidence": "legacy_current_source"}
                                              for value, name in candidates],
                               "evidence": "legacy_current_source"}
        else:
            result[item_id] = {"status": "unknown", "chapter_id": None,
                               "chapter": "未归类", "candidates": [], "evidence": "none"}
    return result
