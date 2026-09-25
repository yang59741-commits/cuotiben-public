"""Caller-transaction primitives for immutable published exam items."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime

from .exam_snapshots import extract_exam_item_snapshots
from .exam_scoring import ExamScoringProjection, normalize_scoring_projection
from .exam_repetition_policy import question_content_fingerprint
from .exam_item_chapters import frozen_chapter_snapshot, single_exam_chapter_snapshot


_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _problem_snapshots(latex_body):
    return extract_exam_item_snapshots(latex_body)


def store_exam_items_in_transaction(
    db,
    exam_id,
    latex_body,
    *,
    source,
    structured_items=(),
    reference_mistakes=(),
    scoring_projection: ExamScoringProjection | None = None,
):
    """Insert immutable question snapshots without commit or rollback.

    The publication caller owns the surrounding transaction and compensation.
    """

    if not db.in_transaction:
        raise RuntimeError("试题快照写入缺少调用者事务")
    snapshots = _problem_snapshots(latex_body)
    if not snapshots:
        raise ValueError("试卷没有可发布的结构化题目快照")
    legacy_unknown = scoring_projection is None and db.execute(
        "SELECT scoring_mode FROM exams WHERE id=?", (exam_id,)
    ).fetchone()[0] == "legacy_unknown"
    scoring = None if legacy_unknown else normalize_scoring_projection(
        scoring_projection, item_count=len(snapshots)
    )
    structured_values = list(structured_items)
    if structured_values:
        positions = []
        for item in structured_values:
            if not isinstance(item, dict):
                raise ValueError("结构化题目必须是对象")
            try:
                positions.append(int(item.get("position")))
            except (TypeError, ValueError) as exc:
                raise ValueError("结构化题目 position 无效") from exc
        if positions != list(range(1, len(structured_values) + 1)):
            raise ValueError("结构化题目 position 必须从 1 开始连续排列")
        if len(structured_values) != len(snapshots):
            raise ValueError(
                "结构化题目数量与发布正文不一致："
                f"正文 {len(snapshots)} 题，结构化数据 {len(structured_values)} 题"
            )
    structured_by_position = {
        int(item.get("position")): item
        for item in structured_values
    }
    lineage_by_position: dict[int, tuple[tuple[str, ...], str, str, str]] = {}
    if source == "mistakes":
        if not structured_values:
            raise ValueError("错题组卷缺少逐题来源谱系")
        for position, structured in structured_by_position.items():
            raw_ids = structured.get("source_mistake_ids")
            if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
                raise ValueError(f"第{position}题 source_mistake_ids 必须是非空数组")
            source_ids = tuple(str(value).strip() for value in raw_ids)
            if any(not value for value in source_ids) or len(set(source_ids)) != len(source_ids):
                raise ValueError(f"第{position}题 source_mistake_ids 无效或重复")
            lineage_hash = str(structured.get("source_lineage_hash") or "").strip()
            policy_version = str(structured.get("lineage_policy_version") or "").strip()
            fingerprint = str(structured.get("content_fingerprint") or "").strip()
            if _HEX64.fullmatch(lineage_hash) is None:
                raise ValueError(f"第{position}题 source_lineage_hash 无效")
            if not policy_version:
                raise ValueError(f"第{position}题 lineage_policy_version 缺失")
            if _HEX64.fullmatch(fingerprint) is None:
                raise ValueError(f"第{position}题 content_fingerprint 无效")
            structured_content = str(structured.get("content") or "")
            if question_content_fingerprint(structured_content) != fingerprint:
                raise ValueError(f"第{position}题 content_fingerprint 与结构化正文不一致")
            if question_content_fingerprint(snapshots[position - 1]) != fingerprint:
                raise ValueError(f"第{position}题 content_fingerprint 与实际发布正文不一致")
            missing = [
                value for value in source_ids
                if db.execute(
                    "SELECT 1 FROM mistakes WHERE public_id=?", (value,)
                ).fetchone() is None
            ]
            if missing:
                raise ValueError(f"第{position}题引用了不存在的错题 public_id")
            lineage_by_position[position] = (
                source_ids, lineage_hash, policy_version, fingerprint
            )
    mistake_refs = [
        {
            "public_id": item.get("public_id"),
            "qnum": item.get("qnum"),
            "chapter": item.get("chapter"),
        }
        for item in reference_mistakes
    ]
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    for position, content in enumerate(snapshots, start=1):
        structured = structured_by_position.get(position, {})
        reference_ids = list(structured.get("reference_ids") or [])
        source_candidate_id = structured.get("candidate_id")
        if not source_candidate_id and len(reference_ids) == 1:
            source_candidate_id = reference_ids[0]
        source_kind = str(structured.get("source_kind") or "")
        if not source_kind:
            source_kind = {
                "free": "ai_generated",
                "mistakes": "ai_generated_from_mistakes",
                "past": "question_bank",
            }.get(source, "unknown")
        metadata = {
            "reference_candidate_ids": reference_ids,
            "reference_mistakes": mistake_refs,
            "source_file": structured.get("source_file"),
            "source_number": structured.get("source_number"),
            "source_institution": structured.get("source_institution"),
            "source_year": structured.get("source_year"),
            "adapted": bool(structured.get("adapted")),
        }
        if str(structured.get("chapter") or "").strip():
            metadata["chapter_snapshot"] = frozen_chapter_snapshot(
                db, exam_id=exam_id, chapter_name=structured.get("chapter"),
                evidence="structured_item",
            )
        else:
            chapter_snapshot = single_exam_chapter_snapshot(db, exam_id=exam_id)
            if chapter_snapshot:
                metadata["chapter_snapshot"] = chapter_snapshot
        if isinstance(structured.get("review_markers"), list):
            metadata["review_markers"] = [
                dict(marker)
                for marker in structured["review_markers"]
                if isinstance(marker, dict)
            ]
        if scoring is None:
            points_text, points_evidence_hash, points = None, None, 0
        else:
            points_text, points_evidence_hash = scoring.item(position)
            points = float(points_text) if points_text is not None else None
        cursor = db.execute(
            "INSERT INTO exam_items(public_id,exam_id,position,question_type,"
            "difficulty,content,content_hash,points,points_canonical,"
            "points_evidence_hash,source_kind,"
            "source_mistake_public_id,source_candidate_id,source_metadata_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), exam_id, position,
                str(structured.get("type") or ""),
                str(structured.get("difficulty") or ""), content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(), points,
                points_text, points_evidence_hash, source_kind,
                None, str(source_candidate_id) if source_candidate_id else None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), created_at,
            ),
        )
        lineage = lineage_by_position.get(position)
        if lineage is not None:
            source_ids, lineage_hash, policy_version, fingerprint = lineage
            for source_order, mistake_public_id in enumerate(source_ids, start=1):
                db.execute(
                    "INSERT INTO exam_item_mistake_lineage("
                    "exam_item_id,source_order,mistake_public_id,source_lineage_hash,"
                    "lineage_policy_version,content_fingerprint,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        int(cursor.lastrowid), source_order, mistake_public_id,
                        lineage_hash, policy_version, fingerprint, created_at,
                    ),
                )
            # Compatibility-only projection.  New readers use the v47 table.
            db.execute(
                "UPDATE exam_items SET source_mistake_public_id=? WHERE id=?",
                (source_ids[0], int(cursor.lastrowid)),
            )
