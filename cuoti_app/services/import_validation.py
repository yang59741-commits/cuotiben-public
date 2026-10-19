"""Shared parsing and validation for versioned and legacy imports."""

from __future__ import annotations

import uuid
import re
from dataclasses import replace

from .portable_import import parse_import
from .daily_tips import DailyTipError, daily_tip_hash, normalize_daily_tip
from .review_schedule import SCHEDULE_VERSION
from .learning_evidence import assistance_value
from ..validation import (
    REVIEW_EVENT_TYPES,
    ValidationError,
    date_value,
    mistake_fingerprint,
    nonnegative_int,
    positive_int,
    reasons_value,
    status_value,
    text_value,
    timestamp_value,
)


DAILY_ANALYSIS_ASSESSMENTS = frozenset({"正确", "部分正确", "错误", "无备注"})


def validate_import_daily_tip(item, index):
    if not isinstance(item, dict):
        raise ValidationError(f"第 {index} 条每日提示格式无效")
    try:
        content = normalize_daily_tip(item.get("content"))
    except DailyTipError as exc:
        raise ValidationError(f"第 {index} 条每日提示无效：{exc}") from exc
    public_id = str(item.get("public_id") or "").strip()
    try:
        public_id = str(uuid.UUID(public_id))
    except ValueError as exc:
        raise ValidationError(f"第 {index} 条每日提示的 public_id 无效") from exc
    expected_hash = daily_tip_hash(content)
    supplied_hash = str(item.get("content_hash") or "").strip()
    if supplied_hash != expected_hash:
        raise ValidationError(f"第 {index} 条每日提示的内容哈希无效")
    revision = positive_int(item.get("revision"), f"第 {index} 条每日提示的版本号")
    created_at = timestamp_value(
        item.get("created_at"), f"第 {index} 条每日提示的创建时间"
    )
    updated_at = timestamp_value(
        item.get("updated_at"), f"第 {index} 条每日提示的更新时间"
    )
    return {
        "public_id": public_id,
        "content": content,
        "content_hash": expected_hash,
        "revision": revision,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _validated_classification(value):
    if value is None:
        return None
    from .mistake_classification import labels, ClassificationError
    from . import solution_methods
    try:
        if not isinstance(value, dict) or not isinstance(value.get('question_type'), dict):
            raise ClassificationError('分类备份格式无效')
        kind = value['question_type']
        if not isinstance(value.get('locked'), (bool, int)) or value.get('locked') not in (True, False, 0, 1):
            raise ClassificationError('分类锁定标记无效')
        result = {
            'primary_type_code': text_value(value.get('primary_type_code'), '题型代码', maximum=120, required=True),
            'source_hash': text_value(value.get('source_hash'), '分类输入指纹', maximum=64, required=True),
            'knowledge_points': labels(value.get('knowledge_points', []), 5),
            'methods': labels(value.get('methods', []), 3), 'locked': bool(value['locked']),
            'question_type': {
                'name': text_value(kind.get('name'), '题型名称', maximum=60, required=True),
                'subject': text_value(kind.get('subject'), '题型科目', maximum=100, required=True),
                'definition': text_value(kind.get('definition'), '题型定义', maximum=500, required=True),
                'chapters': labels(kind.get('chapters', []), 35),
                'aliases': labels(kind.get('aliases', []), 30),
                'active': bool(kind.get('active', True)),
            },
        }
        if value.get('method_annotations') is not None:
            result['method_annotations'] = solution_methods.normalize_annotations(
                result['methods'], value['method_annotations'], subject=result['question_type']['subject'])
        return result
    except (ClassificationError, solution_methods.MethodError) as exc:
        raise ValidationError(str(exc)) from exc


def _validated_daily_analysis(value, index):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValidationError(f"第 {index} 道题的每日分析缓存格式无效")
    analysis = value.get("analysis")
    if not isinstance(analysis, dict):
        raise ValidationError(f"第 {index} 道题的每日分析内容无效")
    assessment = text_value(
        analysis.get("note_assessment"),
        f"第 {index} 道题的备注判断",
        maximum=20,
        required=True,
    )
    if assessment not in DAILY_ANALYSIS_ASSESSMENTS:
        raise ValidationError(f"第 {index} 道题的备注判断无效")
    normalized_analysis = {"note_assessment": assessment}
    for field, label in (
        ("correction", "必要纠正"), ("hint", "解题提示"),
        ("exam_pattern", "题型与命题模式"),
    ):
        normalized_analysis[field] = text_value(
            analysis.get(field), f"第 {index} 道题的{label}", maximum=20_000
        )
    source_hash = text_value(
        value.get("source_hash"), f"第 {index} 道题的分析来源哈希",
        maximum=64, required=True,
    )
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValidationError(f"第 {index} 道题的分析来源哈希无效")
    schema_version = nonnegative_int(
        value.get("analysis_schema_version"), f"第 {index} 道题的分析格式版本"
    )
    prompt_version = nonnegative_int(
        value.get("prompt_version"), f"第 {index} 道题的分析提示版本"
    )
    if schema_version < 1 or prompt_version < 1:
        raise ValidationError(f"第 {index} 道题的每日分析版本无效")
    generated_at = timestamp_value(
        value.get("generated_at"), f"第 {index} 道题的分析生成时间"
    )
    updated_at = timestamp_value(
        value.get("updated_at") or generated_at, f"第 {index} 道题的分析更新时间"
    )
    return {
        "analysis_date": date_value(
            value.get("analysis_date"), f"第 {index} 道题的分析日期"
        ),
        "analysis_schema_version": schema_version,
        "prompt_version": prompt_version,
        "source_hash": source_hash,
        "analysis": normalized_analysis,
        "source_job_id": text_value(
            value.get("source_job_id"), f"第 {index} 道题的分析任务", maximum=100
        ) or None,
        "model_role": text_value(
            value.get("model_role"), f"第 {index} 道题的模型角色", maximum=80
        ) or None,
        "model_name": text_value(
            value.get("model_name"), f"第 {index} 道题的模型名称", maximum=200
        ) or None,
        "generated_at": generated_at,
        "updated_at": updated_at,
    }


def resolve_import_catalog(db):
    subjects = {
        row['name']: dict(row)
        for row in db.execute("SELECT id,name FROM subjects").fetchall()
    }
    chapters = {
        (row['subject'], row['name']): dict(row)
        for row in db.execute(
            "SELECT c.id,c.name,c.subject_id,s.name AS subject "
            "FROM chapters c JOIN subjects s ON s.id=c.subject_id"
        ).fetchall()
    }
    return subjects, chapters


def _validated_event(value, index, event_index):
    if not isinstance(value, dict):
        raise ValidationError(f"第 {index} 道题的第 {event_index} 条复习记录格式无效")
    event_type = value.get('event_type')
    if event_type not in REVIEW_EVENT_TYPES:
        raise ValidationError(f"第 {index} 道题的复习事件类型无效")
    reviewed_at = timestamp_value(
        value.get('reviewed_at'),
        f"第 {index} 道题的复习时间",
    )
    status_after = status_value(value.get('status_after'))
    correct_streak = nonnegative_int(
        value.get('correct_streak'),
        f"第 {index} 道题的连续正确次数",
    )
    return (event_type, reviewed_at, status_after, correct_streak)


def validate_import_mistake(item, index, subjects, chapters):
    if not isinstance(item, dict):
        raise ValidationError(f"第 {index} 道题格式无效")
    subject_name = text_value(
        item.get('subject'), f"第 {index} 道题的科目", maximum=100, required=True
    )
    subject = subjects.get(subject_name)
    if not subject:
        raise ValidationError(f"第 {index} 道题的科目不存在")

    chapter_name = text_value(
        item.get('chapter'), f"第 {index} 道题的章节", maximum=100
    )
    chapter = None
    if chapter_name:
        chapter = chapters.get((subject_name, chapter_name))
        if not chapter:
            raise ValidationError(f"第 {index} 道题的章节不属于该科目")

    qnum = text_value(item.get('qnum'), f"第 {index} 道题的题号", maximum=80)
    content = text_value(
        item.get('content'), f"第 {index} 道题的内容", maximum=100_000
    )
    reasons = reasons_value(item.get('reasons'))
    note = text_value(item.get('note'), f"第 {index} 道题的备注", maximum=20_000)
    date_added = date_value(item.get('date_added'), f"第 {index} 道题的日期")
    status = status_value(item.get('status'))
    review_count = nonnegative_int(
        item.get('review_count'), f"第 {index} 道题的复习次数"
    )
    last_review = date_value(
        item.get('last_review'), f"第 {index} 道题的上次复习日期", optional=True
    )
    next_review = date_value(
        item.get('next_review'), f"第 {index} 道题的下次复习日期", optional=True
    )
    correct_streak = nonnegative_int(
        item.get('correct_streak'),
        f"第 {index} 道题的连续正确次数",
        default=3 if status == '已掌握' else 0,
    )
    lapse_count = nonnegative_int(
        item.get('lapse_count'), f"第 {index} 道题的遗忘次数"
    )

    raw_reviews = item.get('reviews', [])
    if raw_reviews is None:
        raw_reviews = []
    if not isinstance(raw_reviews, list):
        raise ValidationError(f"第 {index} 道题的复习历史必须是数组")
    if len(raw_reviews) > 100_000:
        raise ValidationError(f"第 {index} 道题的复习历史过多")
    reviews = [
        _validated_event(event, index, event_index)
        for event_index, event in enumerate(raw_reviews, start=1)
    ]
    review_records = []
    for raw_event, values in zip(raw_reviews, reviews):
        event_public_id = str(raw_event.get("public_id") or "").strip()
        if event_public_id:
            try:
                event_public_id = str(uuid.UUID(event_public_id))
            except ValueError as exc:
                raise ValidationError(
                    f"第 {index} 道题的复习事件 public_id 无效"
                ) from exc
        client_event_id = str(raw_event.get("client_event_id") or "").strip()
        if len(client_event_id) > 100:
            raise ValidationError(f"第 {index} 道题的复习事件标识过长")
        review_records.append({
            "public_id": event_public_id or None,
            "client_event_id": client_event_id or None,
            "event_type": values[0], "reviewed_at": values[1],
            "status_after": values[2], "correct_streak": values[3],
            "lapse_count_after": nonnegative_int(
                raw_event.get("lapse_count_after"), "复习后的遗忘次数"
            ),
            "created_at": str(raw_event.get("created_at") or values[1]),
            "source_import_id": str(raw_event.get("source_import_id") or "") or None,
            "stage_before": (
                nonnegative_int(raw_event.get("stage_before"), "复习前阶段")
                if raw_event.get("stage_before") is not None else None
            ),
            "stage_after": (
                nonnegative_int(raw_event.get("stage_after"), "复习后阶段")
                if raw_event.get("stage_after") is not None else None
            ),
            "cycle_after": (
                nonnegative_int(raw_event.get("cycle_after"), "复习后循环")
                if raw_event.get("cycle_after") is not None else None
            ),
            "next_review_after": date_value(
                raw_event.get("next_review_after"), "复习后的下次日期", optional=True
            ),
            "schedule_version": text_value(
                raw_event.get("schedule_version"), "复习规则版本", maximum=80
            ) or None,
            "assistance": assistance_value(raw_event.get('assistance')),
            "evidence_version": positive_int(raw_event.get('evidence_version', 1), '学习证据版本'),
            "reviewed_on": date_value(raw_event.get('reviewed_on'), '作答日期', optional=True),
            "content_fingerprint": text_value(raw_event.get('content_fingerprint'), '题面身份', maximum=64) or None,
        })
    fingerprint = mistake_fingerprint(
        subject_name, chapter_name, qnum, content, date_added
    )
    values = (
        subject['id'],
        chapter['id'] if chapter else None,
        qnum,
        content,
        reasons,
        note,
        date_added,
        status,
        review_count,
        last_review,
        next_review,
        correct_streak,
        lapse_count,
        fingerprint,
    )
    public_id = str(item.get("public_id") or "").strip()
    if public_id:
        try:
            public_id = str(uuid.UUID(public_id))
        except ValueError as exc:
            raise ValidationError(f"第 {index} 道题的 public_id 无效") from exc
    tags = item.get("tags") or []
    if not isinstance(tags, list) or len(tags) > 100 or any(
        not isinstance(tag, str) or not tag.strip() or len(tag.strip()) > 80 for tag in tags
    ):
        raise ValidationError(f"第 {index} 道题的标签无效")
    modern = {
        "source_kind": text_value(item.get("source_kind"), f"第 {index} 道题的来源类型", maximum=50),
        "source_ref": text_value(item.get("source_ref"), f"第 {index} 道题的来源", maximum=500),
        "difficulty": text_value(item.get("difficulty"), f"第 {index} 道题的难度", maximum=50),
    }
    review_stage = nonnegative_int(
        item.get("review_stage"), f"第 {index} 道题的复习阶段",
        default=min(correct_streak, 3),
    )
    if review_stage > 3:
        raise ValidationError(f"第 {index} 道题的复习阶段必须为 0 到 3")
    review_cycle = nonnegative_int(
        item.get("review_cycle"), f"第 {index} 道题的复习循环", default=1
    )
    if review_cycle < 1:
        raise ValidationError(f"第 {index} 道题的复习循环必须从 1 开始")
    schedule_version = text_value(
        item.get("schedule_version") or SCHEDULE_VERSION,
        f"第 {index} 道题的复习规则版本", maximum=80, required=True,
    )
    return {
        'values': values, 'fingerprint': fingerprint, 'reviews': reviews,
        "review_records": review_records,
        "public_id": public_id or None,
        "subject_id": subject["id"], "chapter_id": chapter["id"] if chapter else None,
        "subject": subject_name, "chapter": chapter_name, "qnum": qnum,
        "content": content, "reasons": reasons, "note": note,
        "date_added": date_added, "status": status, "review_count": review_count,
        "last_review": last_review, "next_review": next_review,
        "correct_streak": correct_streak, "lapse_count": lapse_count,
        "review_stage": review_stage, "review_cycle": review_cycle,
        "schedule_version": schedule_version,
        "tags": list(dict.fromkeys(tag.strip() for tag in tags)),
        "history_complete": bool(item.get("history_complete", False)),
        "history_complete_from": str(item.get("history_complete_from") or date_added),
        "updated_at": str(item.get("updated_at") or date_added),
        "attachments": item.get("attachments") or [],
        "daily_analysis": _validated_daily_analysis(item.get("daily_analysis"), index),
        "classification": _validated_classification(item.get('classification')),
        **modern,
    }


def parse_and_validate_import(source, db, *, expected_sha256: str | None = None):
    parsed = parse_import(source, expected_sha256=expected_sha256)
    subjects, chapters = resolve_import_catalog(db)
    validated = [
        validate_import_mistake(item, index, subjects, chapters)
        for index, item in enumerate(parsed.mistakes, start=1)
    ]
    validated_daily_tips = [
        validate_import_daily_tip(item, index)
        for index, item in enumerate(parsed.daily_tips, start=1)
    ]
    hashes = [item["content_hash"] for item in validated_daily_tips]
    public_ids = [item["public_id"] for item in validated_daily_tips]
    if len(hashes) != len(set(hashes)):
        raise ValidationError("备份中包含内容重复的每日提示")
    if len(public_ids) != len(set(public_ids)):
        raise ValidationError("备份中包含标识重复的每日提示")
    parsed = replace(parsed, daily_tips=validated_daily_tips)
    return parsed, validated

