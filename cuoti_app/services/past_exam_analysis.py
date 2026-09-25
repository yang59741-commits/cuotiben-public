"""Validation contracts for frozen past-exam analysis and chapter plans."""

from __future__ import annotations

import re

from .exam_contracts import positive_int

def validate_chapter_plan(
    raw_plan,
    available_chapters,
    question_count,
    *,
    prefer_chapter_name=False,
):
    if not isinstance(raw_plan, list) or not raw_plan:
        raise ValueError("真题分析未返回章节题量计划")
    by_id = {int(item["id"]): item for item in available_chapters}
    by_name = {str(item["name"]): item for item in available_chapters}
    normalized = []
    used = set()
    total = 0
    for item in raw_plan:
        if not isinstance(item, dict):
            raise ValueError("真题章节计划格式无效")
        raw_name = str(item.get("chapter") or item.get("name") or "").strip()
        name_candidate = by_name.get(raw_name) if raw_name else None
        id_candidate = None
        try:
            raw_id = item.get("chapter_id")
            if raw_id not in (None, ""):
                id_candidate = by_id.get(int(raw_id))
        except (TypeError, ValueError):
            id_candidate = None
        # Verified manual portraits survive ordinary-bank rebuilds, while
        # chapter IDs are local to one database.  Rebind their stable chapter
        # name to the current bank instead of accepting a stale, conflicting ID.
        candidate = id_candidate or name_candidate
        if prefer_chapter_name and name_candidate is not None:
            candidate = name_candidate
        if candidate is None:
            raise ValueError("真题章节计划包含未知、超纲或无题章节")
        chapter_id = int(candidate["id"])
        if chapter_id in used:
            raise ValueError("真题章节计划包含重复章节")
        count = positive_int(item.get("question_count"), "章节题量")
        available = int(candidate.get("question_count") or 0)
        if available < count:
            raise ValueError(
                f"{candidate['name']}可用题目不足：计划{count}题，当前{available}题"
            )
        normalized.append(
            {
                "chapter_id": chapter_id,
                "chapter": str(candidate["name"]),
                "question_count": count,
                "reason": str(item.get("reason") or "").strip(),
            }
        )
        used.add(chapter_id)
        total += count
    if total != question_count:
        raise ValueError(
            f"真题章节题量总和必须为{question_count}，当前为{total}"
        )
    return normalized


def analysis_section_key(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def validate_past_analysis_contract(raw_analysis, learning_years, structure_contract):
    if not isinstance(raw_analysis, dict):
        raise ValueError("真题分析格式无效")

    expected_years = [int(year) for year in learning_years]
    raw_years = raw_analysis.get("learned_years")
    if not isinstance(raw_years, list):
        raise ValueError("真题分析未返回学习年份")
    try:
        actual_years = [int(year) for year in raw_years]
    except (TypeError, ValueError):
        raise ValueError("真题分析的学习年份格式无效") from None
    if actual_years != expected_years:
        raise ValueError(
            "真题分析学习年份与冻结年份不一致："
            f"应为{expected_years}，实际为{actual_years}"
        )

    normalized = dict(raw_analysis)
    normalized["learned_years"] = actual_years
    for field, label in (
        ("structure_summary", "结构概述"),
        ("difficulty_progression", "难度变化"),
    ):
        value = str(raw_analysis.get(field) or "").strip()
        if not value:
            raise ValueError(f"真题分析未返回{label}")
        normalized[field] = value

    for field, label in (
        ("style_rules", "命题风格规则"),
        ("topic_distribution", "知识点分布"),
    ):
        values = raw_analysis.get(field)
        if not isinstance(values, list):
            raise ValueError(f"真题分析未返回{label}")
        cleaned = [
            value.strip()
            for value in values
            if isinstance(value, str) and value.strip()
        ]
        if not cleaned:
            raise ValueError(f"真题分析未返回{label}")
        normalized[field] = cleaned

    raw_sections = raw_analysis.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise ValueError("真题分析未返回板块分析")
    analyzed_sections = []
    analyzed_keys = []
    for item in raw_sections:
        if not isinstance(item, dict):
            raise ValueError("真题分析板块格式无效")
        title = str(item.get("title") or item.get("label") or "").strip()
        if not title:
            raise ValueError("真题分析板块缺少标题")
        key = analysis_section_key(title)
        if not key:
            raise ValueError("真题分析板块标题无效")
        analyzed_sections.append(dict(item, title=title))
        analyzed_keys.append(key)

    expected_labels = [
        str(item.get("label") or "").strip()
        for item in structure_contract.get("sections") or []
        if str(item.get("label") or "").strip()
    ]
    if not expected_labels:
        raise ValueError("所选结构年份没有可校验的板块")
    missing_labels = []
    unused_analyzed = set(range(len(analyzed_keys)))
    for label in expected_labels:
        expected_key = analysis_section_key(label)
        matching_index = next(
            (
                index
                for index in sorted(unused_analyzed)
                if expected_key in analyzed_keys[index]
                or analyzed_keys[index] in expected_key
            ),
            None,
        )
        if matching_index is None:
            missing_labels.append(label)
        else:
            unused_analyzed.remove(matching_index)
    if missing_labels:
        raise ValueError(
            "真题分析板块未覆盖所选结构：" + "、".join(missing_labels)
        )
    normalized["sections"] = analyzed_sections
    return normalized


def _testing_chapter_plan(available_chapters, question_count):
    usable = [
        item for item in available_chapters if int(item.get("question_count") or 0)
    ][:question_count]
    if not usable:
        raise ValueError("有效题库没有可用于真题模拟的章节")
    plan = []
    remaining = question_count
    for index, chapter in enumerate(usable):
        slots = len(usable) - index
        count = min(
            int(chapter["question_count"]),
            max(1, remaining // slots),
        )
        if remaining - count < slots - 1:
            count = remaining - (slots - 1)
        if count <= 0:
            break
        plan.append(
            {
                "chapter_id": int(chapter["id"]),
                "chapter": str(chapter["name"]),
                "question_count": count,
                "reason": "测试兼容计划",
            }
        )
        remaining -= count
        if remaining == 0:
            break
    if remaining:
        for item in plan:
            chapter = next(
                value
                for value in usable
                if int(value["id"]) == item["chapter_id"]
            )
            spare = int(chapter["question_count"]) - item["question_count"]
            take = min(spare, remaining)
            item["question_count"] += take
            remaining -= take
            if not remaining:
                break
    if remaining:
        raise ValueError("测试题库题目不足")
    return plan


def validate_past_analysis_result(
    raw_analysis,
    learning_years,
    structure_contract,
    available_chapters,
    question_count,
    *,
    allow_testing_plan_fallback=False,
    prefer_chapter_name=False,
):
    analysis = validate_past_analysis_contract(
        raw_analysis,
        learning_years,
        structure_contract,
    )
    try:
        plan = validate_chapter_plan(
            analysis.get("chapter_plan"),
            available_chapters,
            question_count,
            prefer_chapter_name=prefer_chapter_name,
        )
    except ValueError:
        if not allow_testing_plan_fallback:
            raise
        plan = _testing_chapter_plan(available_chapters, question_count)
    analysis["chapter_plan"] = plan
    return analysis, plan
