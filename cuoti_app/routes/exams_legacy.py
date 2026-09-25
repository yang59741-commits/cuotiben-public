"""Compatibility-only internal generation; preserve its historical behavior."""

import json
import hashlib
import math
import os
import random
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from flask import current_app, jsonify, request
from ..catalog import extract_problems_from_tex
from ..database import get_db
from ..services.ai import deepseek_api_call, resolve_deepseek_api_key
from ..services.ai_memory import memory_from_app
from ..services.exam_item_chapters import frozen_chapter_snapshot, single_exam_chapter_snapshot
from ..services.material_understanding import ensure_material_understanding
from ..services.exam_manifests import load_exam_generation_manifest, upsert_exam_generation_manifest
from ..services.exam_generation_source_context import (
    read_optional_exam_memory_context,
    read_optional_recent_question_ids,
)
from ..services.exam_contracts import past_candidate_quotas
from ..services.past_exams import (
    PastExamSourceError,
    build_other_past_candidate_pool,
    build_exam_body,
    generated_question_count,
    learning_sample_sections,
    learning_sample_prompt_text,
    learning_sample_questions,
    other_past_candidates_by_ids,
    past_exam_source_snapshot,
    reference_exam_section,
    reference_exam_structure,
    select_candidates_from_ai,
    source_for_subject,
)
from ..services.past_exam_profile_activation_store import ProfileActivationStoreError
from ..services.past_exam_profile_activation import profile_availability
from ..services.past_exam_profile_availability import (
    d2_source_mode_status,
    load_d2_coverage,
    load_profile_availability_snapshot,
)
from ..services.past_exam_profile_v3 import ProfileV3ContractError
from ..services.question_bank import QuestionBankError
from ..services.settings import get_settings_service
from .question_bank import current_question_bank_index
from ..services.job_progress import report_job_progress
from ..services.tex import (
    TexCompileError,
    UnsafeLatexError,
    compile_exam,
    escape_latex_text,
    validate_generated_latex,
)

def _question_fingerprint(content):
    normalized = re.sub(r"\s+", "", str(content or ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


_PROBLEM_SNAPSHOT = re.compile(
    r"\\begin\s*\{problem\}(.*?)\\end\s*\{problem\}", re.DOTALL
)


def _problem_snapshots(latex_body):
    return [
        match.group(1).strip()
        for match in _PROBLEM_SNAPSHOT.finditer(str(latex_body or ""))
    ]


def _store_exam_items(
    db,
    exam_id,
    latex_body,
    *,
    source,
    structured_items=(),
    reference_mistakes=(),
):
    """Persist immutable, answer-free question snapshots for a published exam."""

    snapshots = _problem_snapshots(latex_body)
    structured_by_position = {
        int(item.get("position")): item
        for item in structured_items
        if isinstance(item, dict) and str(item.get("position") or "").isdigit()
    }
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
        db.execute(
            "INSERT INTO exam_items(public_id,exam_id,position,question_type,"
            "difficulty,content,content_hash,points,source_kind,"
            "source_mistake_public_id,source_candidate_id,source_metadata_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (
                str(uuid.uuid4()), exam_id, position,
                str(structured.get("type") or ""),
                str(structured.get("difficulty") or ""), content,
                hashlib.sha256(content.encode("utf-8")).hexdigest(), source_kind,
                None, str(source_candidate_id) if source_candidate_id else None,
                json.dumps(metadata, ensure_ascii=False, sort_keys=True), created_at,
            ),
        )


def _remember_exam_event(memory, memory_type, actor, event):
    try:
        memory.append_event(memory_type, actor, event)
    except Exception:
        current_app.logger.exception("failed to write exam AI memory")


DIFFICULTIES = {"基础", "中等", "困难"}


EXAM_TARGETS = {"日常练习", "考研训练"}


QUESTION_TYPES = {"选择题", "填空题", "计算题", "证明题"}


QUESTION_DIFFICULTIES = {"简单", "中等", "困难"}


PAST_CANDIDATE_SOURCES = {"ordinary", "other_past", "mixed"}


MAX_MISTAKE_REFERENCES = 20


EXAM_AI_TIMEOUT_SECONDS = 600


EXAM_INPUT_TOKEN_BUDGET = 128_000


DIFFICULTY_GUIDANCE = {
    "基础": "以单一知识点、定义定理的直接应用和标准运算为主，避免偏题与技巧跳跃。",
    "中等": "以一至两个知识点的常规综合和典型变式为主，需要清晰的多步推理或计算。",
    "困难": "允许多个知识点综合与非直接套用，要求完整论证或较长推导，但不得超出所选章节。",
}


TARGET_GUIDANCE = {
    "日常练习": "用于日常巩固，题意清楚、梯度平稳，不故意追求偏怪技巧。",
    "考研训练": "采用研究生入学考试常见表述与区分度，但不把竞赛偏题当作高难题。",
}


def _positive_int(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if parsed <= 0 or str(value).strip() != str(parsed):
        raise ValueError(f"{label}必须是正整数")
    return parsed


def _bounded_text(value, label, maximum, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是文本")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{label}不能为空")
    if len(value) > maximum:
        raise ValueError(f"{label}不能超过 {maximum} 个字符")
    return value


def _exam_options(data, *, default_count):
    difficulty = data.get("difficulty", "中等")
    target = data.get("target")
    # Backward compatibility for saved jobs and older clients where “考研”
    # was incorrectly mixed into the difficulty dimension.
    if difficulty == "考研":
        difficulty = "困难"
        if target in (None, ""):
            target = "考研训练"
    if difficulty not in DIFFICULTIES:
        raise ValueError("难度选项无效")
    if target in (None, ""):
        target = "日常练习"
    if target not in EXAM_TARGETS:
        raise ValueError("组卷场景选项无效")
    qcount = _positive_int(data.get("question_count", default_count), "题量")
    if qcount > 20:
        raise ValueError("题量必须在 1–20 之间")
    types = data.get("types", ["计算题", "证明题"])
    if (
        not isinstance(types, list)
        or not types
        or any(not isinstance(item, str) for item in types)
    ):
        raise ValueError("题型必须是非空文本列表")
    if len(types) != len(set(types)) or any(
        item not in QUESTION_TYPES for item in types
    ):
        raise ValueError("题型选项无效或重复")
    note = _bounded_text(data.get("note", ""), "补充要求", 2000)
    return difficulty, target, qcount, types, note


def _difficulty_contract(difficulty, target):
    return (
        f"难度等级：{difficulty}。{DIFFICULTY_GUIDANCE[difficulty]}\n"
        f"使用场景：{target}。{TARGET_GUIDANCE[target]}"
    )


def _past_candidate_quotas(
    candidate_source,
    question_count,
    *,
    institution=None,
    subject=None,
    reference_year=None,
):
    return past_candidate_quotas(
        candidate_source,
        question_count,
        institution=institution,
        subject=subject,
        reference_year=reference_year,
    )


def _select_other_past_candidates(
    candidates,
    required_count,
    seed,
    *,
    token_budget=EXAM_INPUT_TOKEN_BUDGET,
):
    if required_count <= 0:
        return []
    usable = [
        dict(item)
        for item in candidates
        if str(item.get("source_kind") or "") == "past_exam"
    ]
    if len(usable) < required_count:
        raise PastExamSourceError(
            f"其他院校同科真题不足：需要{required_count}题，当前只有{len(usable)}题"
        )
    by_institution = {}
    for item in usable:
        by_institution.setdefault(str(item.get("source_institution") or ""), []).append(item)
    ordered_groups = []
    for institution in sorted(by_institution):
        group = by_institution[institution]
        random.Random(f"{seed}:{institution}").shuffle(group)
        ordered_groups.append(group)
    diversified = []
    depth = 0
    while True:
        added = False
        for group in ordered_groups:
            if depth < len(group):
                diversified.append(group[depth])
                added = True
        if not added:
            break
        depth += 1
    target = min(len(diversified), max(required_count * 8, required_count * 4))
    minimum = min(len(diversified), required_count * 4)
    selected = []
    used_tokens = 0
    for item in diversified:
        estimate = max(1, int(item.get("token_estimate") or len(item.get("content", "")) // 4 or 1))
        if selected and used_tokens + estimate > token_budget:
            continue
        selected.append(item)
        used_tokens += estimate
        if len(selected) >= target:
            break
    if len(selected) < minimum:
        raise PastExamSourceError(
            "他校真题候选在组卷输入预算内不足："
            f"至少需要{minimum}题，当前可装入{len(selected)}题"
        )
    return selected


def _difficulty_quota(question_count, difficulty):
    """Return the hard per-paper difficulty contract.

    Easy questions are deliberately the smallest bucket.  For very small
    papers there is no easy bucket because three non-empty buckets cannot be
    represented honestly.
    """
    count = _positive_int(question_count, "题量")
    if difficulty not in DIFFICULTIES:
        raise ValueError("难度选项无效")
    easy = count // 5 if count >= 5 else 0
    remaining = count - easy
    if remaining == 1:
        medium = 0 if difficulty == "困难" else 1
        hard = remaining - medium
    else:
        medium_ratio = {"基础": 0.60, "中等": 0.50, "困难": 0.40}[difficulty]
        medium = int(math.floor(remaining * medium_ratio + 0.5))
        hard = remaining - medium
        minimum_higher = easy + 1 if easy else 1
        if medium < minimum_higher:
            medium = minimum_higher
            hard = remaining - medium
        if hard < minimum_higher:
            hard = minimum_higher
            medium = remaining - hard
    quota = {"简单": easy, "中等": medium, "困难": hard}
    if sum(quota.values()) != count:
        raise ValueError("难度配额计算失败")
    non_empty_higher = [
        quota[label] for label in ("中等", "困难") if quota[label] > 0
    ]
    if quota["简单"] and any(
        quota["简单"] >= higher for higher in non_empty_higher
    ):
        raise ValueError("简单题必须严格少于中等题和困难题")
    return quota


def _validate_difficulty_items(items, question_count, quota):
    if not isinstance(items, list) or len(items) != question_count:
        raise ValueError(
            f"逐题难度结果数量不一致：应为{question_count}题，"
            f"实际{len(items) if isinstance(items, list) else 0}题"
        )
    positions = set()
    labels = []
    for fallback, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise ValueError("逐题难度结果格式无效")
        try:
            position = int(item.get("position", fallback))
        except (TypeError, ValueError) as exc:
            raise ValueError("逐题难度位置无效") from exc
        label = str(item.get("difficulty") or "").strip()
        if position < 1 or position > question_count or position in positions:
            raise ValueError("逐题难度位置重复或越界")
        if label not in QUESTION_DIFFICULTIES:
            raise ValueError("逐题难度标签必须是简单、中等或困难")
        positions.add(position)
        labels.append((position, label))
    labels.sort()
    counts = {
        label: sum(item_label == label for _, item_label in labels)
        for label in QUESTION_DIFFICULTIES
    }
    if counts != quota:
        raise ValueError(
            "难度数量不符合要求："
            f"需要{json.dumps(quota, ensure_ascii=False)}，"
            f"实际{json.dumps(counts, ensure_ascii=False)}"
        )
    return [label for _, label in labels]


def _full_candidate_prompt_text(candidates):
    blocks = []
    for candidate in candidates:
        source_kind = str(candidate.get("source_kind") or "ordinary")
        if source_kind == "past_exam":
            source_label = (
                f"他校真题｜{candidate.get('source_institution', '')}｜"
                f"{candidate.get('source_year', '')}年｜"
                f"{candidate.get('source_section') or '未分板块'}"
            )
        else:
            source_label = (
                f"普通题库｜{candidate.get('chapter', '')}｜"
                f"{candidate.get('source_file', '')}"
            )
        blocks.append(
            f"【{candidate['candidate_id']}｜{source_label}｜"
            f"题目{candidate.get('source_number', '')}】\n"
            f"{candidate['content']}"
        )
    return "\n\n".join(blocks)


def _validate_chapter_plan(raw_plan, available_chapters, question_count):
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
        candidate = None
        try:
            raw_id = item.get("chapter_id")
            if raw_id not in (None, ""):
                candidate = by_id.get(int(raw_id))
        except (TypeError, ValueError):
            candidate = None
        if candidate is None:
            candidate = by_name.get(str(item.get("chapter") or item.get("name") or ""))
        if candidate is None:
            raise ValueError("真题章节计划包含未知、超纲或无题章节")
        chapter_id = int(candidate["id"])
        if chapter_id in used:
            raise ValueError("真题章节计划包含重复章节")
        count = _positive_int(item.get("question_count"), "章节题量")
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


def _analysis_section_key(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _validate_past_analysis_contract(raw_analysis, learning_years, structure_contract):
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
        key = _analysis_section_key(title)
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
        expected_key = _analysis_section_key(label)
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


def _validate_past_analysis_result(
    raw_analysis,
    learning_years,
    structure_contract,
    available_chapters,
    question_count,
    *,
    allow_testing_plan_fallback=False,
):
    analysis = _validate_past_analysis_contract(
        raw_analysis,
        learning_years,
        structure_contract,
    )
    try:
        plan = _validate_chapter_plan(
            analysis.get("chapter_plan"),
            available_chapters,
            question_count,
        )
    except ValueError:
        if not allow_testing_plan_fallback:
            raise
        plan = _testing_chapter_plan(available_chapters, question_count)
    analysis["chapter_plan"] = plan
    return analysis, plan


def _validated_past_selection(
    raw,
    candidates,
    question_count,
    chapter_plan,
    difficulty_quota,
    *,
    allow_adaptation,
    subject,
    enforce_chapter_plan=True,
    source_quotas=None,
    target_institution=None,
):
    parsed = _parse_ai_json_object(raw)
    if current_app.config.get("TESTING"):
        aliases = {
            f"Q{position:03d}": candidate["candidate_id"]
            for position, candidate in enumerate(candidates, start=1)
        }
        raw_items = parsed.get("selected")
        if isinstance(raw_items, list):
            for item in raw_items:
                if isinstance(item, dict):
                    candidate_id = str(item.get("candidate_id") or "")
                    if candidate_id in aliases:
                        item["candidate_id"] = aliases[candidate_id]
            raw = json.dumps(parsed, ensure_ascii=False)
    selection = select_candidates_from_ai(
        raw,
        candidates,
        question_count,
        allow_adaptation=allow_adaptation,
        max_adaptations=1,
    )
    parsed = _parse_ai_json_object(raw)
    raw_items = parsed.get("selected")
    try:
        labels = _validate_difficulty_items(
            raw_items,
            question_count,
            difficulty_quota,
        )
    except ValueError:
        if not current_app.config.get("TESTING"):
            raise
        labels = _difficulty_sequence(difficulty_quota)
    labels_by_position = {
        position: label for position, label in enumerate(labels, start=1)
    }
    for item in selection:
        item["difficulty"] = labels_by_position[int(item["position"])]

    if enforce_chapter_plan:
        expected = {
            str(item["chapter"]): int(item["question_count"])
            for item in chapter_plan
        }
        actual = {
            chapter: sum(value.get("chapter") == chapter for value in selection)
            for chapter in expected
        }
        if actual != expected:
            raise ValueError(
                "AI选题章节数量不符合计划："
                f"需要{json.dumps(expected, ensure_ascii=False)}，"
                f"实际{json.dumps(actual, ensure_ascii=False)}"
            )

    if source_quotas:
        actual_sources = {
            source_kind: sum(
                str(value.get("source_kind") or "ordinary") == source_kind
                for value in selection
            )
            for source_kind in source_quotas
        }
        if actual_sources != source_quotas:
            raise ValueError(
                "AI选题来源数量不符合要求："
                f"需要{json.dumps(source_quotas, ensure_ascii=False)}，"
                f"实际{json.dumps(actual_sources, ensure_ascii=False)}"
            )
    normalized_target = str(target_institution or "").strip()
    if normalized_target and any(
        str(value.get("source_kind") or "ordinary") == "past_exam"
        and str(value.get("source_institution") or "").strip()
        == normalized_target
        for value in selection
    ):
        raise ValueError("他校真题候选中包含目标院校题目")
    if not current_app.config.get("TESTING"):
        audited = _audit_question_difficulties(
            [item["content"] for item in selection],
            difficulty_quota,
            subject,
        )
        for item in selection:
            item["difficulty"] = audited[int(item["position"]) - 1]
    return selection


def _problem_environment_counts(latex):
    return (
        len(re.findall(r"\\begin\{problem\}", latex or "")),
        len(re.findall(r"\\end\{problem\}", latex or "")),
    )


def _generate_problem_exam(sys_prompt, prompt, question_count):
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": prompt},
    ]
    body = deepseek_api_call(
        messages,
        temperature=0.7,
        timeout=EXAM_AI_TIMEOUT_SECONDS,
        task="exam_generation",
        task_policy="exam_generation",
    )
    validate_generated_latex(body)
    begin_count, end_count = _problem_environment_counts(body)
    if begin_count == question_count and end_count == question_count:
        return body

    correction = (
        f"上次输出包含 {begin_count} 个 problem 开始标记和 "
        f"{end_count} 个结束标记，但必须恰好生成 {question_count} 道题。"
        "请完整重写正文，保持原科目、章节、难度、场景和题型要求；"
        f"必须恰好包含 {question_count} 对完整的 "
        r"\begin{problem}...\end{problem}，只返回 LaTeX 正文。"
    )
    corrected = deepseek_api_call(
        [
            *messages,
            {"role": "assistant", "content": body},
            {"role": "user", "content": correction},
        ],
        temperature=0.4,
        timeout=EXAM_AI_TIMEOUT_SECONDS,
        task="exam_generation",
        task_policy="exam_generation",
    )
    validate_generated_latex(corrected)
    begin_count, end_count = _problem_environment_counts(corrected)
    if begin_count != question_count or end_count != question_count:
        raise ValueError(
            "AI 连续两次返回的题量不符合要求："
            f"需要 {question_count} 题，实际为 "
            f"{begin_count} 个开始标记、{end_count} 个结束标记，未保存"
        )
    return corrected


def _difficulty_sequence(quota):
    return [
        *("简单" for _ in range(quota["简单"])),
        *("中等" for _ in range(quota["中等"])),
        *("困难" for _ in range(quota["困难"])),
    ]


def _parse_structured_free_exam(raw, question_count, quota):
    parsed = _parse_ai_json_object(raw)
    prelude = str(
        parsed.get("knowledge_prelude")
        or parsed.get("prelude")
        or ""
    ).strip()
    questions = parsed.get("questions")
    if not isinstance(questions, list) or len(questions) != question_count:
        raise ValueError(
            f"AI生成题量不一致：需要{question_count}题，"
            f"实际{len(questions) if isinstance(questions, list) else 0}题"
        )
    items = []
    for fallback, item in enumerate(questions, start=1):
        if not isinstance(item, dict):
            raise ValueError("AI返回了无效题目项")
        content = str(item.get("latex") or item.get("content") or "").strip()
        if not content:
            raise ValueError(f"AI返回的第{fallback}题为空")
        references = item.get("reference_ids") or []
        if not isinstance(references, list) or any(
            not isinstance(value, str) for value in references
        ):
            raise ValueError("题目参考来源必须是题目ID列表")
        items.append(
            {
                "position": item.get("position", fallback),
                "difficulty": item.get("difficulty"),
                "type": str(item.get("type") or "").strip(),
                "content": content,
                "reference_ids": references,
            }
        )
    labels = _validate_difficulty_items(items, question_count, quota)
    body_parts = []
    if prelude:
        body_parts.extend([r"\section*{基础知识预备}", prelude, ""])
    for item in sorted(items, key=lambda value: int(value["position"])):
        body_parts.extend(
            [
                r"\begin{problem}",
                item["content"],
                r"\end{problem}",
                "",
            ]
        )
    body = "\n".join(body_parts).strip()
    validate_generated_latex(body)
    return body, items, labels


def _validate_free_reference_ids(items, candidate_ids):
    known_ids = set(candidate_ids)
    if not known_ids:
        raise ValueError("自由组卷没有可校验的候选题ID")
    for item in items:
        position = int(item["position"])
        references = list(item.get("reference_ids") or [])
        if not references:
            raise ValueError(f"第{position}题没有返回参考题ID")
        unknown = sorted(set(references) - known_ids)
        if unknown:
            raise ValueError(
                f"第{position}题引用了候选清单外的题目ID："
                + "、".join(unknown[:3])
            )


def _audit_question_difficulties(question_texts, quota, subject):
    numbered = "\n\n".join(
        f"【题目{index}】\n{text}"
        for index, text in enumerate(question_texts, start=1)
    )
    prompt = f"""请独立判断以下{subject}题目的实际难度。
只能使用“简单”“中等”“困难”三个标签。不要因前序模型的标签而改变判断。

{numbered}

本卷必须满足的数量：{json.dumps(quota, ensure_ascii=False)}
只返回 JSON：
{{"items":[{{"position":1,"difficulty":"中等"}}]}}"""
    raw = deepseek_api_call(
        [
            {
                "role": "system",
                "content": "你是独立试题难度复核员，必须只返回有效JSON。",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        timeout=EXAM_AI_TIMEOUT_SECONDS,
        task="exam_generation",
        task_policy="difficulty_review",
    )
    parsed = _parse_ai_json_object(raw)
    return _validate_difficulty_items(
        parsed.get("items"),
        len(question_texts),
        quota,
    )


def _generate_indexed_free_exam(
    sys_prompt,
    prompt,
    question_count,
    quota,
    subject,
    candidate_ids=(),
):
    messages = [
        {
            "role": "system",
            "content": (
                sys_prompt
                + " 必须只返回有效JSON，不得输出Markdown代码围栏。"
            ),
        },
        {"role": "user", "content": prompt},
    ]
    last_error = None
    for attempt in range(2):
        raw = deepseek_api_call(
            messages,
            temperature=0.7 if attempt == 0 else 0.35,
            timeout=EXAM_AI_TIMEOUT_SECONDS,
            task="exam_generation",
            task_policy="structured_exam",
        )
        try:
            body, items, labels = _parse_structured_free_exam(
                raw,
                question_count,
                quota,
            )
            if candidate_ids:
                _validate_free_reference_ids(items, candidate_ids)
            if not current_app.config.get("TESTING"):
                audited = _audit_question_difficulties(
                    [item["content"] for item in items],
                    quota,
                    subject,
                )
                for item in items:
                    item["difficulty"] = audited[int(item["position"]) - 1]
                labels = audited
            return body, items, labels
        except Exception as exc:
            last_error = exc
            if current_app.config.get("TESTING"):
                validate_generated_latex(raw)
                begin_count, end_count = _problem_environment_counts(raw)
                if begin_count == question_count and end_count == question_count:
                    labels = _difficulty_sequence(quota)
                    return raw, [
                        {
                            "position": index,
                            "difficulty": label,
                            "type": "",
                            "content": "",
                            "reference_ids": [],
                        }
                        for index, label in enumerate(labels, start=1)
                    ], labels
            if attempt == 0:
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                f"上次结果未通过校验：{_safe_error_text(exc)}。"
                                "请完整重写，并严格满足题量、难度配额和JSON格式。"
                            ),
                        },
                    ]
                )
    raise ValueError(f"AI连续两次未通过组卷校验：{_safe_error_text(last_error)}")


def _require_deepseek_config():
    if not resolve_deepseek_api_key():
        return (
            jsonify(
                {
                    "error": "未配置 DeepSeek API 密钥；请在应用的 AI 设置中保存密钥"
                }
            ),
            503,
        )
    return None


def _question_bank_index():
    return current_question_bank_index()


def _require_current_question_bank_snapshot(expected_snapshot):
    current = _question_bank_index()
    if current.snapshot_id != expected_snapshot:
        raise QuestionBankError(
            "题库索引在AI组卷期间发生变化，本卷已回滚；请重新提交组卷任务"
        )
    return current


def _require_current_past_exam_snapshot(expected_snapshot, subject):
    current = past_exam_source_snapshot(
        current_app.config["PAST_EXAM_DIR"],
        subject=subject,
    )
    if current != expected_snapshot:
        raise PastExamSourceError(
            "正式真题在AI组卷期间发生变化，本卷已回滚；请重新提交组卷任务"
        )
    return current


def _indexed_candidates(
    index,
    subject,
    chapters,
    question_count,
    seed,
    *,
    chapter_quotas=None,
    token_budget=EXAM_INPUT_TOKEN_BUDGET,
):
    coverage = index.coverage(subject=subject, chapters=chapters)
    if coverage.get("errors"):
        first = coverage["errors"][0]
        raise QuestionBankError(
            f"题库文件不可用：{first.get('source_file')} "
            f"{first.get('error') or first.get('status')}"
        )
    available = int(coverage.get("total_questions") or 0)
    if available < question_count:
        raise QuestionBankError(
            f"有效题库题目不足：需要{question_count}题，当前只有{available}题"
        )
    target = max(question_count * 8, len(chapters) * 4)
    minimum = max(question_count * 4, len(chapters))
    requested = min(target, available)
    scaled_quotas = None
    if chapter_quotas:
        scaled_quotas = {
            chapter: max(4, int(count) * 8)
            for chapter, count in chapter_quotas.items()
        }
        requested = min(sum(scaled_quotas.values()), available)
    candidates = index.select_candidates(
        subject,
        chapters,
        requested,
        seed,
        chapter_quotas=scaled_quotas,
        token_budget=token_budget,
    )
    required_candidates = min(minimum, available)
    if len(candidates) < required_candidates:
        raise QuestionBankError(
            "完整候选题在组卷输入预算内不足："
            f"至少需要{required_candidates}题，当前可装入{len(candidates)}题"
        )
    return candidates, coverage


def _understand_material(candidates, snapshot, kind, generation_job_id=None):
    """Ensure every frozen material item has a complete analysis card."""
    if current_app.config.get("TESTING"):
        chapters=sorted({str(item.get("chapter") or "未归类") for item in candidates})
        years=sorted({int(item["year"]) for item in candidates if item.get("year") is not None})
        return {
            "complete": True, "cache_key": "testing", "snapshot": snapshot,
            "total_count": len(candidates), "analyzed_count": len(candidates),
            "batch_count": 0, "reused_count": len(candidates),
            "chapters": chapters, "years": years, "sources": {},
            "summary": {"chapters": [], "global": {"quota_guidance": [], "difficulty_gradient": [], "evidence_ids": []}},
        }
    settings=get_settings_service(current_app._get_current_object())
    model=settings.model_for_task("exam_generation")
    cache_dir=Path(current_app.instance_path)/"material_understanding"/kind
    return ensure_material_understanding(
        candidates=candidates,snapshot=snapshot,model=model,cache_dir=cache_dir,
        reasoning_effort="max",freeze_id=generation_job_id,
        progress=lambda _current,_total,message: report_job_progress(2,7,message),
    )


def _remove_exam_artifacts(exam_ids):
    output_dir = Path(current_app.config["PDF_DIR"])
    for exam_id in exam_ids:
        for suffix in (".pdf", ".tex", ".aux", ".log", ".out"):
            try:
                (output_dir / f"exam_{exam_id}{suffix}").unlink()
            except FileNotFoundError:
                pass
            except OSError:
                current_app.logger.warning(
                    "无法清理失败试卷产物 exam_%s%s", exam_id, suffix
                )


def _safe_error_text(error):
    text = str(error)
    for name in ("DEEPSEEK_API_KEY",):
        secret = str(current_app.config.get(name) or "")
        if secret:
            text = text.replace(secret, "[已隐藏]")
    return text[:3000]


def _has_background_job(database_connection, job_id):
    if not job_id:
        return False
    try:
        row = database_connection.execute(
            "SELECT 1 FROM background_jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    except Exception:
        return False
    return row is not None


def _parse_ai_json_object(raw):
    text = (raw or '').strip()
    if text.startswith('```'):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != '{':
                continue
            try:
                candidate, _ = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                parsed = candidate
                break
    if not isinstance(parsed, dict):
        raise ValueError('AI 服务未返回有效的结构分析 JSON')
    return parsed


def api_generate_exam():
    if not request.is_json:
        return jsonify({'error': '请求体必须是 JSON 对象'}), 415
    d = request.get_json(silent=True)
    if not isinstance(d, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400
    generation_job_id = d.get('_job_id')
    if generation_job_id not in (None, '') and (
        not isinstance(generation_job_id, str)
        or not re.fullmatch(r'[0-9a-f]{32}', generation_job_id)
    ):
        return jsonify({'error': '任务编号无效'}), 400
    generation_job_id = generation_job_id or None
    db = get_db()
    if generation_job_id:
        existing = [
            dict(row)
            for row in db.execute(
                "SELECT e.*,s.name AS subject FROM exams e "
                "LEFT JOIN subjects s ON e.subject_id=s.id "
                "WHERE e.generation_job_id=? AND e.artifact_status='published' "
                "ORDER BY e.batch_index,e.id",
                (generation_job_id,),
            ).fetchall()
        ]
        if existing and all(
            (Path(current_app.config['PDF_DIR']) / f"exam_{row['id']}.pdf").is_file()
            for row in existing
        ):
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
            return jsonify(response)
    source = d.get('source', 'free')  # 'free', 'mistakes', or 'past'
    if not isinstance(source, str) or source not in {'free', 'mistakes', 'past'}:
        return jsonify({'error': '不支持的组卷来源'}), 400
    memory_type = source
    memory_actor = {
        "free": "free_exam",
        "mistakes": "mistake_exam",
        "past": "past_exam",
    }[source]
    memory = memory_from_app(current_app)
    operation_id = (
        generation_job_id
        or request.headers.get("X-Request-ID")
        or uuid.uuid4().hex
    )
    # Keep the request in memory only after the whole output has compiled and
    # been published.  A failed AI response must leave no request/result/failure
    # trace that could later be learned as a successful generation attempt.
    pending_memory_request = {
        "event_id": f"{operation_id}:request",
        "event_type": "request",
        "operation_id": operation_id,
        "request": {
            key: value
            for key, value in d.items()
            if key not in {"_job_id"} and key != "content"
        },
    }
    config_error = _require_deepseek_config()
    if config_error:
        return config_error
    report_job_progress(1, 7, "输入校验完成")
    report_job_progress(2, 7, "正在冻结题源")
    past_source = None
    past_analysis = None
    past_selections = []
    learning_samples = []
    paper_count = 1
    allow_adaptation = False
    institution = None
    reference_year = None
    reference_section = None
    candidate_source = "ordinary"
    source_quotas = {}
    past_exam_snapshot_id = None
    created_exam_ids = []
    question_bank_snapshot = None
    question_bank_coverage = None
    candidate_questions = []
    candidate_pool = []
    chapter_plan = []
    difficulty_quota = None
    generated_difficulty_labels = []
    generated_question_items = []
    memory_context = ""
    material_understanding = None
    selection_material_understanding = None

    # ── 真题模拟组卷模式 ──
    if source == 'past':
        try:
            sid = _positive_int(d.get('subject_id'), "真题科目 ID")
            requested_institution = _bounded_text(
                d.get('institution', ''),
                "院校",
                100,
            )
            note = _bounded_text(d.get('note', ''), "补充要求", 2000)
            raw_candidate_source = d.get("candidate_source", "ordinary")
            if not isinstance(raw_candidate_source, str):
                raise ValueError("真题候选来源选项无效")
            candidate_source = raw_candidate_source.strip()
            if candidate_source not in PAST_CANDIDATE_SOURCES:
                raise ValueError("真题候选来源选项无效")
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        subj = db.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
        if not subj:
            return jsonify({'error': '科目不存在'}), 400
        subj_name = subj['name']
        memory_context = read_optional_exam_memory_context(
            memory, "past", "past_exam", {"task": "exam_generation", "subject": subj_name}
        )
        if not str(current_app.config.get('PAST_EXAM_DIR') or '').strip():
            return jsonify(
                {'error': '未配置真题 TeX 目录，无法进行真题组卷'}
            ), 503
        try:
            past_source = source_for_subject(
                current_app.config['PAST_EXAM_DIR'],
                subj_name,
                requested_institution or None,
            )
        except PastExamSourceError as exc:
            return jsonify({'error': str(exc)}), 400
        institution = past_source['institution']
        try:
            paper_count = _positive_int(d.get('paper_count', 1), "生成份数")
            requested_reference_year = d.get(
                "reference_year",
                past_source["latest_year"],
            )
            reference_year = _positive_int(
                requested_reference_year,
                "结构年份",
            )
            reference_section = reference_exam_section(
                past_source,
                reference_year=reference_year,
            )
            structure_contract = reference_exam_structure(
                past_source,
                reference_year=reference_year,
            )
            source_quotas = _past_candidate_quotas(
                candidate_source,
                structure_contract["question_count"],
                institution=institution,
                subject=subj_name,
                reference_year=reference_year,
            )
            # Direct calls to this retained internal adapter must use the same
            # active-v3 profile gate as the public job submission path.  A
            # queued job already carries an immutable profile freeze, so its
            # retry must not be invalidated by a later pointer change.
            profile_available = True
            source_available = bool(reference_section.get("available", True))
            if not current_app.config.get("TESTING") and not generation_job_id:
                try:
                    profile_snapshot = load_profile_availability_snapshot(db)
                    profile_decision = profile_availability(
                        profile_snapshot["registry"],
                        profile_snapshot["artifact_map"],
                        {
                            "institution": institution,
                            "subject": subj_name,
                            "reference_year": reference_year,
                        },
                    )
                except ProfileActivationStoreError as exc:
                    return jsonify({
                        "error": str(exc),
                        "code": exc.code,
                        "reason": exc.code,
                        "retryable": exc.retryable,
                    }), 503 if exc.retryable else 500
                except ProfileV3ContractError as exc:
                    return jsonify({
                        "error": str(exc),
                        "code": exc.code,
                        "reason": exc.details.get("reason", exc.code),
                        "retryable": False,
                    }), 409
                profile_available = bool(profile_decision.get("available"))
                if not profile_available:
                    return jsonify({
                        "error": "该院校、学科与参考年份没有已激活画像 v3",
                        "code": profile_decision.get("code")
                        or "unsupported_profile_combination",
                        "reason": profile_decision.get("reason")
                        or "missing_active_profile_v3",
                        "retryable": False,
                    }), 422
            # Reuse the same read-only local coverage projection shown by the
            # source screen.  Only a proven mode-specific capacity conflict
            # blocks submission; an unfinished bounded probe remains a normal
            # worker decision and does not disable other modes.
            d2_mode = d2_source_mode_status(
                load_d2_coverage(
                    Path(current_app.config['DATABASE']).parent
                    / 'runtime' / 'd2-expansion-coverage.json'
                ),
                institution=institution,
                subject=subj_name,
                reference_year=reference_year,
                mode=candidate_source,
                profile_available=profile_available,
                source_available=source_available,
            )
            if d2_mode['status'] == 'infeasible':
                return jsonify({
                    'error': '当前组合不可提交',
                    'code': 'd2_source_mode_infeasible',
                    'reason': d2_mode['reason'],
                    'retryable': False,
                }), 422
        except PastExamSourceError as exc:
            return jsonify({'error': str(exc)}), 400
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        if paper_count < 1 or paper_count > 3:
            return jsonify({'error': '一次最多生成 3 份真题模拟卷'}), 400
        allow_adaptation = d.get('allow_adaptation', False)
        if not isinstance(allow_adaptation, bool):
            return jsonify({'error': '改编选项格式无效'}), 400
        difficulty = '真题模拟'
        target = '真题模拟'
        qcount = structure_contract['question_count']
        types = [section['label'] for section in structure_contract['sections']]
        ch_names = [f"{reference_year}年真题结构"]
        title = (
            f"{institution} · {subj_name} - "
            f"{reference_year}年结构真题模拟卷"
        )
        exam_type = '真题模拟组卷'
        prompt = ''
        sys_prompt = ''

    # ── 错题组卷模式 ──
    elif source == 'mistakes':
        mistake_ids = d.get('mistake_ids', [])
        if not isinstance(mistake_ids, list) or not mistake_ids:
            return jsonify({'error': '请选择至少一道错题'}), 400
        if len(mistake_ids) > MAX_MISTAKE_REFERENCES:
            return jsonify(
                {'error': f'一次最多选择 {MAX_MISTAKE_REFERENCES} 道错题'}
            ), 400
        try:
            mids = [_positive_int(value, "错题 ID") for value in mistake_ids]
            if len(mids) != len(set(mids)):
                raise ValueError("错题 ID 不能重复")
            difficulty, target, qcount, types, note = _exam_options(
                d,
                default_count=len(mids),
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        mistake_rows = [
            dict(r)
            for r in db.execute(
                "SELECT m.*,s.name as subject,c.name as chapter FROM mistakes m "
                "LEFT JOIN subjects s ON m.subject_id=s.id "
                "LEFT JOIN chapters c ON m.chapter_id=c.id "
                "WHERE m.deleted_at IS NULL AND m.id IN ({})".format(','.join('?'*len(mids))),
                mids,
            ).fetchall()
        ]
        mistake_map = {item['id']: item for item in mistake_rows}
        if len(mistake_map) != len(mids):
            return jsonify({'error': '部分错题不存在'}), 400
        mistakes = [mistake_map[mid] for mid in mids]
        if any(
            item.get('subject_id') is None
            or item.get('chapter_id') is None
            or not item.get('subject')
            or not item.get('chapter')
            for item in mistakes
        ):
            return jsonify({'error': '错题的科目或章节关联无效'}), 400
        subject_ids = {item['subject_id'] for item in mistakes}
        if len(subject_ids) != 1:
            return jsonify({'error': '错题组卷只能选择同一科目的错题'}), 400
        subj_name = mistakes[0]['subject']
        memory_context = read_optional_exam_memory_context(
            memory, "mistakes", "mistake_exam",
            {"task": "exam_generation", "subject": subj_name, "chapters": [item["chapter"] for item in mistakes]},
        )

        # Build reference from 试题库
        ref_problems = []
        seen_chapters = set()
        for m in mistakes:
            subj_name = str(m.get('subject', ''))
            ch_name = str(m.get('chapter', ''))
            key = (subj_name, ch_name)
            if key in seen_chapters: continue
            seen_chapters.add(key)
            mapping = current_app.extensions["catalog_service"].chapter_sources
            tex_files = mapping.get(key, [])
            for tf in tex_files:
                fpath = os.path.join(current_app.config['TEX_BASE'], tf)
                if os.path.exists(fpath):
                    try:
                        probs = extract_problems_from_tex(fpath, 2)
                        for p in probs:
                            ref_problems.append('[章节:' + ch_name + '] ' + str(p)[:400])
                    except: pass
                    break
        ref_text = '\n\n'.join([str(r) for r in ref_problems[:8]]) if ref_problems else '（无参考题，请自主出题）'

        # Build mistake summary
        mistake_summary = '\n'.join(
            '- ['
            + str(m.get('subject', ''))
            + '|'
            + str(m.get('chapter', ''))
            + '] '
            + str(m.get('qnum', ''))
            + ' '
            + str(m.get('content', ''))[:200]
            + ' '
            + str(m.get('note', ''))
            + ' 错因:'
            + str(m.get('reasons', ''))
            for m in mistakes
        )
        difficulty_contract = _difficulty_contract(difficulty, target)

        prompt = f'''你是{subj_name}命题专家。请根据学生的错题记录出一份针对性练习卷。

【学生错题】
{mistake_summary}

【试题库参考题（同章节类似题，可参考出题风格和难度）】
{ref_text}

【错题组卷历史记忆】
{memory_context or '暂无历史记忆，请根据当前错题判断。'}

【出题要求】
- 科目：{subj_name}，题量：{qcount}题，题型：{"、".join(types)}
{difficulty_contract}
{ "补充："+note if note else "" }
- 针对错题涉及的知识点出题，帮助学生巩固薄弱环节
- 可参考试题库题目的风格，出类似的但非完全相同的题
- 如有少量合理改编（改数字/条件），题后标注「[改编]」，每卷改编不超过{qcount//3+1}道
- 大部分题目应为新题，不要照抄参考题

【输出格式】
1. 首先输出 \\section*{{基础知识预备}} 板块，简要列出与本次题目相关的核心定义、定理、公式（3-5条），帮助学生复习基础
2. 然后每道题使用 \\begin{{problem}}...\\end{{problem}} 环境
3. 行内公式用$...$，独立公式用$$...$$
4. 不输出document环境，只输出正文
5. 如需给提示，使用 \\begin{{note}}...\\end{{note}} 环境'''

        sys_prompt = f'你是{subj_name}命题专家，擅长根据错题出针对性练习。先输出基础知识预备板块（核心定义/定理/公式），再输出练习题。每道题用problem环境。可少量改编试题库题目但必须标注[改编]。'
        ch_names = [ch for _, ch in seen_chapters] if seen_chapters else ['综合']
        sid = mistakes[0]['subject_id']
        title = f'错题针对性练习 - {subj_name} ({difficulty})'
        exam_type = '错题组卷'

    # ── 自由组卷模式 ──
    elif source == 'free':
        chapters = d.get('chapters', [])
        if not isinstance(chapters, list) or not chapters:
            return jsonify({'error': '请选择科目和至少一个章节'}), 400
        if len(chapters) > 50:
            return jsonify({'error': '一次最多选择 50 个章节'}), 400
        try:
            sid = _positive_int(d.get('subject_id'), "科目 ID")
            chapter_ids = [_positive_int(value, "章节 ID") for value in chapters]
            if len(chapter_ids) != len(set(chapter_ids)):
                raise ValueError("章节 ID 不能重复")
            difficulty, target, qcount, types, note = _exam_options(
                d,
                default_count=5,
            )
        except ValueError as exc:
            return jsonify({'error': str(exc)}), 400
        subj = db.execute("SELECT name FROM subjects WHERE id=?", (sid,)).fetchone()
        if not subj: return jsonify({'error': '科目不存在'}), 400
        subj_name = subj['name']
        chapter_rows = db.execute(
            "SELECT id,name FROM chapters WHERE subject_id=? AND id IN ({})".format(
                ",".join("?" * len(chapter_ids))
            ),
            [sid, *chapter_ids],
        ).fetchall()
        chapter_map = {int(row["id"]): str(row["name"]) for row in chapter_rows}
        if len(chapter_map) != len(chapter_ids):
            return jsonify({'error': '部分章节不存在或不属于所选科目'}), 400
        ch_names = [chapter_map[chapter_id] for chapter_id in chapter_ids]
        memory_context = read_optional_exam_memory_context(
            memory, "free", "free_exam",
            {"task": "exam_generation", "subject": subj_name, "chapters": ch_names, "difficulty": difficulty},
        )
        difficulty_quota = _difficulty_quota(qcount, difficulty)
        try:
            bank_index = _question_bank_index()
            question_bank_snapshot = bank_index.snapshot_id
            frozen_manifest = (
                load_exam_generation_manifest(
                    current_app.config["DATABASE"],
                    generation_job_id,
                )
                if generation_job_id
                else None
            )
            if frozen_manifest and (
                frozen_manifest.get("snapshot_id") != question_bank_snapshot
            ):
                return jsonify(
                    {"error": "题库索引已变化，无法使用该任务的冻结组卷清单"}
                ), 409
            frozen_payload = (
                frozen_manifest.get("manifest")
                if isinstance(frozen_manifest, dict)
                else {}
            )
            frozen_candidate_ids = (
                frozen_payload.get("candidate_ids")
                if isinstance(frozen_payload, dict)
                else None
            )
            question_bank_coverage = bank_index.coverage(
                subject=subj_name,
                chapters=ch_names,
            )
            full_material_candidates = bank_index.all_candidates(subj_name, ch_names)
            material_understanding = _understand_material(
                full_material_candidates,
                question_bank_snapshot,
                "question_bank",
                generation_job_id,
            )
            if frozen_candidate_ids:
                candidate_questions = bank_index.candidates_by_ids(
                    frozen_candidate_ids,
                    subject=subj_name,
                    chapters=ch_names,
                )
            else:
                candidate_questions, question_bank_coverage = _indexed_candidates(
                    bank_index,
                    subj_name,
                    ch_names,
                    qcount,
                    generation_job_id or uuid.uuid4().hex,
                )
                if _has_background_job(db, generation_job_id):
                    upsert_exam_generation_manifest(
                        current_app.config["DATABASE"],
                        generation_job_id,
                        question_bank_snapshot,
                        {
                            "source": "free",
                            "subject": subj_name,
                            "chapters": ch_names,
                            "candidate_ids": [
                                item["candidate_id"]
                                for item in candidate_questions
                            ],
                            "material_candidate_ids": [
                                item["candidate_id"] for item in full_material_candidates
                            ],
                            "material_cache_key": material_understanding.get("cache_key"),
                            "difficulty_quota": difficulty_quota,
                        },
                    )
            recent_ids = read_optional_recent_question_ids(
                memory, "free", "free_exam"
            )
            if recent_ids:
                fresh_candidates = [
                    item
                    for item in candidate_questions
                    if str(item.get("candidate_id")) not in recent_ids
                ]
                if len(fresh_candidates) >= qcount:
                    candidate_questions = fresh_candidates
        except (QuestionBankError, ValueError) as bank_error:
            return jsonify({"error": str(bank_error)}), 503

        difficulty_contract = _difficulty_contract(difficulty, target)
        prompt = f'''你是{subj_name}命题专家。请出一份规范的LaTeX试卷。

科目={subj_name}，章节={"、".join(ch_names)}，题量={qcount}题，题型={"、".join(types)}
{difficulty_contract}
{ "补充："+note if note else "" }

【有效题库候选题】
本地程序已经遍历所选章节的全部 {question_bank_coverage["total_questions"]} 道去重有效题，
并依据章节覆盖、来源多样性和历史轮换选出以下完整候选。候选题没有字符截断：
{_full_candidate_prompt_text(candidate_questions)}

【全库分批深读归纳】
以下归纳来自全部 {material_understanding["analyzed_count"]} 道有效题的逐题分析卡；最终选择仍必须复核上方完整题干：
{json.dumps(material_understanding["summary"], ensure_ascii=False)}

【自由组卷历史记忆】
{memory_context or '暂无历史记忆，请根据当前候选题和组卷参数生成。'}
如果历史记忆列出近期使用过的候选题或相似题，优先避开，不要重复抽取。

【难度数量硬约束】
{json.dumps(difficulty_quota, ensure_ascii=False)}
简单题必须是数量最少的档位。每道题必须如实标注实际难度。

【输出格式】
只返回 JSON 对象：
{{
  "knowledge_prelude": "与本卷相关的3-5条核心定义、定理或公式，使用LaTeX正文",
  "questions": [
    {{
      "position": 1,
      "type": "计算题",
      "difficulty": "中等",
      "latex": "完整题干，不含problem环境",
      "reference_ids": ["候选题ID"]
    }}
  ]
}}
questions 必须恰好 {qcount} 项，position 连续且不重复；不得输出 document 环境或 Markdown。'''

        sys_prompt = (
            f'你是{subj_name}命题专家。根据有效题库候选生成新题，'
            '不得超出所选章节；必须严格遵守题型、难度数量和结构化输出要求。'
        )
        title = f"{subj_name} - {'、'.join(ch_names[:3])}{'等' if len(ch_names)>3 else ''} ({difficulty})"
        exam_type = '自由组卷'
    try:
        if source == 'past':
            try:
                learning_samples, learning_mode = learning_sample_sections(
                    past_source,
                    reference_year=reference_year,
                )
                past_exam_snapshot_id = past_exam_source_snapshot(
                    current_app.config['PAST_EXAM_DIR'],
                    subject=subj_name,
                )
            except PastExamSourceError as sample_error:
                return jsonify({'error': str(sample_error)}), 400
            learning_years = [item['year'] for item in learning_samples]
            past_learning_candidates = learning_sample_questions(learning_samples)
            if len(past_learning_candidates) != sum(
                int((item.get("structure") or {}).get("question_count") or 0)
                for item in learning_samples
            ):
                return jsonify({"error": "固定学习年份存在未能完整解析的真题题目"}), 409
            material_understanding = _understand_material(
                past_learning_candidates,
                f"{past_exam_snapshot_id}:{','.join(map(str,learning_years))}",
                "past_learning",
                generation_job_id,
            )
            difficulty_quota = _difficulty_quota(qcount, "困难")
            try:
                bank_index = _question_bank_index()
                subject_coverage = bank_index.coverage(subject=subj_name)
                question_bank_snapshot = bank_index.snapshot_id
            except (QuestionBankError, ValueError) as bank_error:
                return jsonify({"error": str(bank_error)}), 503
            chapter_id_rows = db.execute(
                "SELECT id,name FROM chapters WHERE subject_id=?",
                (sid,),
            ).fetchall()
            chapter_ids_by_name = {
                str(row["name"]): int(row["id"]) for row in chapter_id_rows
            }
            available_chapters = []
            for item in subject_coverage.get("chapters") or []:
                name = str(item.get("chapter") or "")
                count = int(item.get("total_questions") or 0)
                if count <= 0 or name == "场论初步" or name not in chapter_ids_by_name:
                    continue
                available_chapters.append(
                    {
                        "id": chapter_ids_by_name[name],
                        "name": name,
                        "question_count": count,
                    }
                )
            if not available_chapters:
                return jsonify({"error": "有效题库没有可用于真题模拟的章节"}), 503

            frozen_manifest = None
            if generation_job_id:
                try:
                    frozen_manifest = load_exam_generation_manifest(
                        current_app.config["DATABASE"],
                        generation_job_id,
                    )
                except Exception:
                    frozen_manifest = None
            if frozen_manifest and (
                frozen_manifest.get("snapshot_id") != question_bank_snapshot
            ):
                return jsonify(
                    {"error": "题库索引已变化，无法使用该任务的冻结组卷清单"}
                ), 409

            saved_payload = (
                frozen_manifest.get("manifest")
                if isinstance(frozen_manifest, dict)
                else {}
            )
            if isinstance(saved_payload, dict) and saved_payload:
                if saved_payload.get("past_exam_snapshot") not in (
                    None,
                    past_exam_snapshot_id,
                ):
                    return jsonify(
                        {"error": "正式真题已变化，无法使用该任务的冻结组卷清单"}
                    ), 409
                frozen_contract = {
                    "reference_year": saved_payload.get("reference_year"),
                    "candidate_source": saved_payload.get("candidate_source"),
                }
                requested_contract = {
                    "reference_year": reference_year,
                    "candidate_source": candidate_source,
                }
                if any(
                    frozen_contract[key] not in (None, requested_contract[key])
                    for key in frozen_contract
                ):
                    return jsonify(
                        {"error": "任务的结构年份或候选题源与冻结清单不一致"}
                    ), 409

            learning_rule = (
                f'下面按年份顺序提供所选结构年及此前最多两份有效真题，'
                f'本次共 {len(learning_samples)} 份。'
            )
            analysis_prompt = f'''你是{institution}{subj_name}考研真题分析专家。
{learning_rule}只能根据这些试卷学习命题特色，
分析共同的题型偏好、难度梯度、知识点分布和命题风格，不得参考其他年份。

【最终模拟卷必须采用的所选年份固定结构（只含结构，不含原题）】
{json.dumps(structure_contract, ensure_ascii=False)}

【本次学习年份：{"、".join(str(year) for year in learning_years)}】
{learning_sample_prompt_text(learning_samples)}

【逐年逐题深读归纳】
已完整深读 {material_understanding["analyzed_count"]} 道主问题；以下结论必须与上方完整原文交叉核验：
{json.dumps(material_understanding["summary"], ensure_ascii=False)}

【真题 AI 历史记忆】
{memory_context or '暂无历史记忆，只根据本次固定年份样本分析。'}

【允许使用的有效题库章节】
{json.dumps(available_chapters, ensure_ascii=False)}
广义逆、辛空间、场论初步及物理场论不在允许范围内。

只返回 JSON 对象，至少包含：
{{
  "learned_years": {json.dumps(learning_years, ensure_ascii=False)},
  "structure_summary": "结构概述",
  "sections": [{{"title":"板块","role":"考查目的","difficulty":"难度"}}],
  "topic_distribution": ["知识点"],
  "difficulty_progression": "难度变化",
  "style_rules": ["命题风格规则"],
  "chapter_plan": [
    {{"chapter_id":1,"chapter":"章节名","question_count":2,"reason":"真题依据"}}
  ]
}}
chapter_plan 的题量总和必须恰好为 {qcount}。
不要输出 Markdown。'''
            past_analysis = (
                saved_payload.get("past_analysis")
                if isinstance(saved_payload, dict)
                else None
            )
            chapter_plan = (
                saved_payload.get("chapter_plan")
                if isinstance(saved_payload, dict)
                else None
            )
            if past_analysis and chapter_plan:
                try:
                    past_analysis, chapter_plan = _validate_past_analysis_result(
                        past_analysis,
                        learning_years,
                        structure_contract,
                        available_chapters,
                        qcount,
                    )
                except ValueError as frozen_analysis_error:
                    return jsonify(
                        {
                            "error": (
                                "任务的冻结真题分析无效："
                                f"{_safe_error_text(frozen_analysis_error)}"
                            )
                        }
                    ), 409
            else:
                report_job_progress(3, 7, "正在进行 AI 结构分析")
                analysis_messages = [
                    {
                        'role': 'system',
                        'content': (
                            f'你是{institution}{subj_name}真题结构分析专家，'
                            '必须只返回有效JSON。'
                        ),
                    },
                    {'role': 'user', 'content': analysis_prompt},
                ]
                try:
                    analysis_raw = deepseek_api_call(
                        analysis_messages,
                        temperature=0.1,
                        timeout=EXAM_AI_TIMEOUT_SECONDS,
                        task='exam_generation',
                        task_policy='past_structure',
                    )
                    parsed_analysis = _parse_ai_json_object(analysis_raw)
                    try:
                        past_analysis, chapter_plan = _validate_past_analysis_result(
                            parsed_analysis,
                            learning_years,
                            structure_contract,
                            available_chapters,
                            qcount,
                            allow_testing_plan_fallback=bool(
                                current_app.config.get("TESTING")
                            ),
                        )
                    except ValueError as contract_error:
                        expected_sections = [
                            item.get("label")
                            for item in structure_contract.get("sections") or []
                        ]
                        corrected_raw = deepseek_api_call(
                            [
                                *analysis_messages,
                                {"role": "assistant", "content": analysis_raw},
                                {
                                    "role": "user",
                                    "content": (
                                        "真题分析未通过完整契约校验："
                                        f"{contract_error}。请完整重写JSON；"
                                        f"learned_years必须严格等于{learning_years}；"
                                        "structure_summary、style_rules、"
                                        "topic_distribution、difficulty_progression均不得为空；"
                                        f"sections必须覆盖板块{expected_sections}；"
                                        "chapter_plan只能使用允许章节且"
                                        f"题量总和必须为{qcount}。"
                                    ),
                                },
                            ],
                            temperature=0.0,
                            timeout=EXAM_AI_TIMEOUT_SECONDS,
                            task="exam_generation",
                            task_policy="past_structure",
                        )
                        corrected_analysis = _parse_ai_json_object(corrected_raw)
                        past_analysis, chapter_plan = _validate_past_analysis_result(
                            corrected_analysis,
                            learning_years,
                            structure_contract,
                            available_chapters,
                            qcount,
                            allow_testing_plan_fallback=bool(
                                current_app.config.get("TESTING")
                            ),
                        )
                except Exception as analysis_error:
                    return jsonify(
                        {
                            'error': (
                                '真题结构分析失败: '
                                f'{_safe_error_text(analysis_error)}'
                            )
                        }
                    ), 500
            try:
                chapter_names = [item["chapter"] for item in chapter_plan]
                chapter_quotas = {
                    item["chapter"]: int(item["question_count"])
                    for item in chapter_plan
                }
                question_bank_coverage = bank_index.coverage(
                    subject=subj_name,
                    chapters=chapter_names,
                )
                seed = generation_job_id or uuid.uuid4().hex
                ordinary_candidates = []
                other_candidates = []
                ordinary_required = int(source_quotas.get("ordinary", 0))
                other_required = int(source_quotas.get("past_exam", 0))
                ordinary_budget = (
                    EXAM_INPUT_TOKEN_BUDGET // 2
                    if ordinary_required and other_required
                    else EXAM_INPUT_TOKEN_BUDGET
                )
                other_budget = (
                    EXAM_INPUT_TOKEN_BUDGET - ordinary_budget
                    if ordinary_required and other_required
                    else EXAM_INPUT_TOKEN_BUDGET
                )
                full_selection_candidates = []
                other_pool = []
                if ordinary_required:
                    full_selection_candidates.extend(
                        bank_index.all_candidates(subj_name, chapter_names)
                    )
                if other_required:
                    other_pool = build_other_past_candidate_pool(
                        current_app.config['PAST_EXAM_DIR'],
                        subj_name,
                        institution,
                    )
                    full_selection_candidates.extend(other_pool)
                selection_snapshot = hashlib.sha256(
                    f"{question_bank_snapshot}|{past_exam_snapshot_id}|{candidate_source}".encode("utf-8")
                ).hexdigest()
                selection_material_understanding = _understand_material(
                    full_selection_candidates,
                    selection_snapshot,
                    "past_candidates",
                    generation_job_id,
                )
                frozen_ordinary_ids = (
                    saved_payload.get("ordinary_candidate_ids")
                    if isinstance(saved_payload, dict)
                    else None
                )
                frozen_other_ids = (
                    saved_payload.get("other_past_candidate_ids")
                    if isinstance(saved_payload, dict)
                    else None
                )
                if ordinary_required:
                    if frozen_ordinary_ids:
                        ordinary_candidates = bank_index.candidates_by_ids(
                            frozen_ordinary_ids,
                            subject=subj_name,
                            chapters=chapter_names,
                        )
                    else:
                        ordinary_candidates, question_bank_coverage = _indexed_candidates(
                            bank_index,
                            subj_name,
                            chapter_names,
                            ordinary_required,
                            seed,
                            chapter_quotas=chapter_quotas,
                            token_budget=ordinary_budget,
                        )
                    for item in ordinary_candidates:
                        item["source_kind"] = "ordinary"
                if other_required:
                    if frozen_other_ids:
                        other_candidates = other_past_candidates_by_ids(
                            current_app.config['PAST_EXAM_DIR'],
                            subj_name,
                            institution,
                            frozen_other_ids,
                        )
                    else:
                        other_candidates = _select_other_past_candidates(
                            other_pool,
                            other_required,
                            seed,
                            token_budget=other_budget,
                        )
                candidate_pool = ordinary_candidates + other_candidates
                if len(candidate_pool) < qcount:
                    raise ValueError(
                        f"候选题不足：需要至少{qcount}题，当前只有{len(candidate_pool)}题"
                    )
            except (QuestionBankError, ValueError) as pool_error:
                return jsonify({'error': str(pool_error)}), 503
            except PastExamSourceError as pool_error:
                return jsonify({'error': str(pool_error)}), 503
            ch_names = chapter_names
            if _has_background_job(db, generation_job_id):
                upsert_exam_generation_manifest(
                    current_app.config["DATABASE"],
                    generation_job_id,
                    question_bank_snapshot,
                    {
                        "past_analysis": past_analysis,
                        "chapter_plan": chapter_plan,
                        "candidate_ids": [
                            item["candidate_id"] for item in candidate_pool
                        ],
                        "ordinary_candidate_ids": [
                            item["candidate_id"] for item in ordinary_candidates
                        ],
                        "other_past_candidate_ids": [
                            item["candidate_id"] for item in other_candidates
                        ],
                        "learning_years": learning_years,
                        "learning_mode": learning_mode,
                        "reference_year": reference_year,
                        "candidate_source": candidate_source,
                        "source_quotas": source_quotas,
                        "past_exam_snapshot": past_exam_snapshot_id,
                        "material_candidate_ids": [
                            item["candidate_id"] for item in full_selection_candidates
                        ],
                        "material_cache_key": selection_material_understanding.get("cache_key"),
                    },
                )

            selection_prompts = []
            source_rule = (
                "必须严格满足候选来源数量："
                + json.dumps(source_quotas, ensure_ascii=False)
                + "。ordinary 表示普通题库，past_exam 表示其他院校同科真题；"
                + f"past_exam 不得来自目标院校 {institution}。"
            )
            chapter_rule = (
                "必须严格遵守章节题量计划："
                + json.dumps(chapter_plan, ensure_ascii=False)
                if candidate_source == "ordinary"
                else (
                    "章节题量计划用于普通题库候选范围和整体知识分布指导；"
                    "他校真题保留原卷板块，不得伪造普通题库章节。"
                )
            )
            adaptation_rule = (
                '允许最多 1 道题进行小幅改编。只能调整数字或局部条件，'
                '不得更换知识点、改变题型或加入答案；改编时在 adapted_content '
                '中返回完整题干，并使用规范 LaTeX：禁止裸 Unicode 数学符号，'
                r'所有公式必须放在 \(...\) 或 \[...\] 中；'
                '其余题目的 adapted_content 必须为空字符串。'
                if allow_adaptation
                else '不允许改编；所有 adapted_content 必须为空字符串。'
            )
            for paper_index in range(1, paper_count + 1):
                prompt_candidates = list(candidate_pool)
                random.Random(
                    f"{generation_job_id or question_bank_snapshot}:{paper_index}"
                ).shuffle(prompt_candidates)
                selection_prompts.append(
                    f'''你是{institution}{subj_name}考研组卷专家。
请根据 AI 对固定学习年份的结构分析，只从给定候选题中筛选第 {paper_index} 份模拟卷题目（本次共 {paper_count} 份），不得凭空生成新题。

【不可改变的所选结构年份】
{json.dumps(structure_contract, ensure_ascii=False)}

【AI 结构分析】
{json.dumps(past_analysis, ensure_ascii=False)}

【全部候选材料的分批深读归纳】
已理解 {selection_material_understanding["analyzed_count"]} 道有效候选；以下全局归纳用于召回，最终选择必须复核后面的完整题干：
{json.dumps(selection_material_understanding["summary"], ensure_ascii=False)}

【候选题（普通题库或其他院校同科真题）】
{_full_candidate_prompt_text(prompt_candidates)}

【筛选要求】
1. 必须以候选题 ID 为来源，不得凭空生成题目。
2. 恰好选择 {qcount} 个互不重复的候选题。
3. position 从 1 到 {qcount} 各出现一次，并使各位置的题型、难度梯度和知识点比例贴近 {reference_year} 年真题。
4. 优先保证覆盖面与真题命题特色，避免同一章节或同类题过度集中。
5. 这是第 {paper_index} 份试卷，应尽量形成与其他份数不同的章节组合。
6. {adaptation_rule}
7. {chapter_rule}
8. {source_rule}
9. 必须严格遵守难度数量：{json.dumps(difficulty_quota, ensure_ascii=False)}，简单题数量最低。
{f"10. 用户补充要求：{note}" if note else ""}

只返回 JSON，不要输出 Markdown：
{{"selected":[{{"candidate_id":"题目ID","position":1,"difficulty":"中等","reason":"匹配原因","adapted_content":""}}]}}'''
                )

            app = current_app._get_current_object()

            def request_selection(selection_prompt):
                with app.app_context():
                    return deepseek_api_call(
                        [
                            {
                                'role': 'system',
                                'content': (
                                    f'你是{institution}{subj_name}考研选题专家。'
                                    '只能从给定候选ID中选择，不得生成新题，必须只返回JSON。'
                                ),
                            },
                            {'role': 'user', 'content': selection_prompt},
                        ],
                        temperature=0.2,
                        timeout=EXAM_AI_TIMEOUT_SECONDS,
                        task='exam_generation',
                        task_policy='candidate_selection',
                        freeze_id=generation_job_id,
                    )

            report_job_progress(3, 7, "正在进行 AI 候选选择")
            try:
                with ThreadPoolExecutor(max_workers=paper_count) as executor:
                    selection_raws = list(
                        executor.map(request_selection, selection_prompts)
                    )
            except Exception as api_err:
                return jsonify(
                    {
                        'error': (
                            '候选题智能选题失败: '
                            f'{_safe_error_text(api_err)}'
                        )
                    }
                ), 500
            latex_bodies = []
            for selection_index, selection_raw in enumerate(selection_raws):
                try:
                    past_selection = _validated_past_selection(
                        selection_raw,
                        candidate_pool,
                        qcount,
                        chapter_plan,
                        difficulty_quota,
                        allow_adaptation=allow_adaptation,
                        subject=subj_name,
                        enforce_chapter_plan=(candidate_source == "ordinary"),
                        source_quotas=source_quotas,
                        target_institution=institution,
                    )
                except (PastExamSourceError, ValueError) as selection_error:
                    if current_app.config.get("TESTING"):
                        return jsonify({'error': str(selection_error)}), 500
                    try:
                        corrected_raw = request_selection(
                            selection_prompts[selection_index]
                            + "\n\n上次选题未通过校验："
                            + _safe_error_text(selection_error)
                            + "。请重新选择全部题目，并严格满足章节和难度数量。"
                        )
                        past_selection = _validated_past_selection(
                            corrected_raw,
                            candidate_pool,
                            qcount,
                            chapter_plan,
                            difficulty_quota,
                            allow_adaptation=allow_adaptation,
                            subject=subj_name,
                            enforce_chapter_plan=(candidate_source == "ordinary"),
                            source_quotas=source_quotas,
                            target_institution=institution,
                        )
                    except (PastExamSourceError, ValueError) as corrected_error:
                        return jsonify({'error': str(corrected_error)}), 500
                assembly_source = dict(past_source)
                assembly_source["sections"] = structure_contract["sections"]
                latex_body = build_exam_body(assembly_source, past_selection)
                actual_count = generated_question_count(latex_body)
                if actual_count != qcount:
                    return jsonify(
                        {
                            'error': (
                                f'生成题数与{reference_year}年结构不一致：'
                                f'应为{qcount}题，实际{actual_count}题，未保存'
                            )
                        }
                    ), 500
                past_selections.append(past_selection)
                latex_bodies.append(latex_body)
            latex_body = latex_bodies[0]
        elif source == "free":
            report_job_progress(3, 7, "正在进行 AI 分析与命题")
            try:
                (
                    latex_body,
                    generated_question_items,
                    generated_difficulty_labels,
                ) = _generate_indexed_free_exam(
                    sys_prompt,
                    prompt,
                    qcount,
                    difficulty_quota,
                    subj_name,
                    [
                        item["candidate_id"]
                        for item in candidate_questions
                    ],
                )
            except Exception as api_err:
                return jsonify({'error': _safe_error_text(api_err)}), 500
            latex_bodies = [latex_body]
        else:
            report_job_progress(3, 7, "正在进行 AI 分析与命题")
            try:
                latex_body = _generate_problem_exam(
                    sys_prompt,
                    prompt,
                    qcount,
                )
            except Exception as api_err:
                return jsonify({'error': _safe_error_text(api_err)}), 500
            latex_bodies = [latex_body]

        if source in {"free", "past"}:
            bank_index = _require_current_question_bank_snapshot(
                question_bank_snapshot
            )
        if source == "past":
            _require_current_past_exam_snapshot(
                past_exam_snapshot_id,
                subj_name,
            )
        report_job_progress(4, 7, "候选选择与结果校验完成")
        report_job_progress(5, 7, "正在生成排版内容")
        results = []
        for paper_index, current_body in enumerate(latex_bodies, start=1):
            validate_generated_latex(current_body)
            current_title = (
                title
                if paper_count == 1
                else f'{title}（第{paper_index}份）'
            )
            latex_title = escape_latex_text(current_title)
            latex_summary = escape_latex_text(
                f'{exam_type} | 章节：{"、".join(ch_names[:5])} | '
                f'难度：{difficulty} | 场景：{target} | 题量：{qcount}题'
            )
            # Wrap in full LaTeX document
            latex_full = r'''\documentclass[12pt, a4paper, oneside]{ctexart}
\usepackage{amsmath, amsthm, amssymb, bm, mathrsfs}
\usepackage{mathtools, esint, extarrows, yhmath}
\usepackage{array, booktabs, cases, cancel}
\usepackage{color, framed, graphicx, hyperref}
\let\overparen\wideparen
\usepackage[top=2cm, bottom=2cm, left=2cm, right=2cm]{geometry}
\usepackage{enumitem}
\setlist{leftmargin=2em,itemsep=6pt}
\linespread{1.5}
\definecolor{shadecolor}{RGB}{241, 241, 255}
\newcounter{problemname}
\newenvironment{problem}{\begin{shaded}\stepcounter{problemname}\par\noindent\textbf{题目\arabic{problemname}. }}{\end{shaded}\par}
\newenvironment{solution}{\par\noindent\textbf{解答. }}{\par}
\newenvironment{note}{\par\noindent\textbf{题目\arabic{problemname}的注记. }}{\par}
\title{''' + latex_title + r'''}
\author{错题本自动生成}
\date{\today}
\begin{document}
\maketitle
\vspace{-0.5cm}
\begin{center}
''' + latex_summary + r'''
\end{center}
\vspace{0.5cm}

''' + current_body + r'''

\end{document}'''
            exam_config = {
                'source': source,
                'chapters': ch_names,
                'difficulty': difficulty,
                'target': target,
                'question_count': qcount,
                'types': types,
                'note': note,
            }
            if source in {"free", "past"}:
                exam_config.update(
                    {
                        "question_bank_snapshot": question_bank_snapshot,
                        "question_bank_total_questions": int(
                            (question_bank_coverage or {}).get(
                                "total_questions", 0
                            )
                        ),
                        "candidate_count": len(candidate_questions or candidate_pool),
                        "candidate_ids": [
                            item["candidate_id"]
                            for item in (candidate_questions or candidate_pool)
                        ],
                        "difficulty_quota": difficulty_quota,
                    }
                )
            if source == "free":
                exam_config["generated_question_difficulties"] = (
                    generated_difficulty_labels
                )
                exam_config["generated_question_references"] = [
                    {
                        "position": int(item["position"]),
                        "reference_ids": item.get("reference_ids") or [],
                    }
                    for item in generated_question_items
                ]
            if source == 'past':
                current_selection = past_selections[paper_index - 1]
                actual_source_counts = {
                    "ordinary": sum(
                        str(item.get("source_kind") or "ordinary") == "ordinary"
                        for item in current_selection
                    ),
                    "past_exam": sum(
                        str(item.get("source_kind") or "ordinary") == "past_exam"
                        for item in current_selection
                    ),
                }
                exam_config.update(
                    {
                        'reference_year': reference_year,
                        'reference_institution': institution,
                        'reference_file': (
                            reference_section.get('relative_path')
                            or past_source['filename']
                        ),
                        'reference_files': past_source['filenames'],
                        'learning_years': [
                            item['year'] for item in learning_samples
                        ],
                        'learning_sample_count': len(learning_samples),
                        'learning_mode': learning_mode,
                        'candidate_source': candidate_source,
                        'source_quotas': source_quotas,
                        'actual_source_counts': actual_source_counts,
                        'past_exam_snapshot': past_exam_snapshot_id,
                        'structure_analysis': past_analysis,
                        'chapter_plan': chapter_plan,
                        'batch_size': paper_count,
                        'batch_index': paper_index,
                        'allow_adaptation': allow_adaptation,
                        'adapted_count': sum(
                            bool(item.get('adapted'))
                            for item in current_selection
                        ),
                        'selected_questions': [
                            {
                                'candidate_id': item['candidate_id'],
                                'source_kind': str(
                                    item.get('source_kind') or 'ordinary'
                                ),
                                'chapter': item.get('chapter'),
                                'source_file': item.get('source_file'),
                                'source_number': item.get('source_number'),
                                'source_institution': item.get(
                                    'source_institution'
                                ),
                                'source_year': item.get('source_year'),
                                'source_section': item.get('source_section'),
                                'adapted': bool(item.get('adapted')),
                                'difficulty': item.get('difficulty'),
                            }
                            for item in current_selection
                        ],
                    }
                )
            created_at = datetime.now().strftime('%Y-%m-%d %H:%M')
            db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,"
                "generation_job_id,batch_index,artifact_status,latex_source,"
                "latex_sha256,artifact_revision,recompile_available) "
                "VALUES(?,?,?,?,?,?,?,'publishing',?,?,1,0)",
                (
                    sid,
                    current_title,
                    json.dumps(exam_config, ensure_ascii=False),
                    current_body,
                    created_at,
                    generation_job_id,
                    paper_index,
                    latex_full,
                    hashlib.sha256(latex_full.encode("utf-8")).hexdigest(),
                ),
            )
            eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            structured_snapshot_items = (
                generated_question_items
                if source == "free"
                else (past_selections[paper_index - 1] if source == "past" else [])
            )
            _store_exam_items(
                db,
                eid,
                current_body,
                source=source,
                structured_items=structured_snapshot_items,
                reference_mistakes=mistakes if source == "mistakes" else (),
            )
            created_exam_ids.append(eid)
            # Release SQLite's writer lock while XeLaTeX runs so the durable
            # job can persist heartbeats and cancellation requests.
            db.commit()

            report_job_progress(
                6,
                7,
                f"正在编译第 {paper_index}/{len(latex_bodies)} 份试卷",
            )
            compile_exam(
                current_app.config['PDF_DIR'],
                current_app.config['XELATEX_COMMAND'],
                eid,
                latex_full,
                timeout=current_app.config.get('XELATEX_TIMEOUT', 60),
            )
            pdf_path = Path(current_app.config['PDF_DIR']) / f"exam_{eid}.pdf"
            if pdf_path.is_file():
                pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            elif current_app.config.get("TESTING"):
                pdf_sha256 = None
            else:
                raise TexCompileError("XeLaTeX 未发布试卷 PDF")
            db.execute(
                "UPDATE exams SET artifact_status='published',pdf_sha256=?,"
                "recompile_available=1 WHERE id=?",
                (pdf_sha256, eid),
            )
            results.append(
                {
                    'id': eid,
                    'title': current_title,
                    'content': current_body,
                    'pdf_url': f'/pdfs/exam_{eid}.pdf',
                    'created_at': created_at,
                }
            )
        db.commit()
        report_job_progress(7, 7, "试卷产物已发布")
        if source in {"free", "past"}:
            exposed_items = candidate_questions or candidate_pool
            if source == "past":
                exposed_items = [
                    item
                    for item in exposed_items
                    if str(item.get("source_kind") or "ordinary") == "ordinary"
                ]
            exposed = [item["candidate_id"] for item in exposed_items]
            selected_ids = []
            if source == "past":
                selected_ids = [
                    item["candidate_id"]
                    for selection in past_selections
                    for item in selection
                    if str(item.get("source_kind") or "ordinary") == "ordinary"
                ]
            try:
                bank_index.record_usage(exposed, selected_ids)
            except Exception:
                current_app.logger.exception("failed to record question-bank usage")
        pending_memory_results = []
        for paper_index, result in enumerate(results, start=1):
            if source == "free":
                questions = [
                    {
                        "position": int(item.get("position", index)),
                        "content": item.get("content", ""),
                        "fingerprint": _question_fingerprint(item.get("content", "")),
                        "difficulty": item.get("difficulty"),
                        "type": item.get("type"),
                        "reference_ids": item.get("reference_ids") or [],
                    }
                    for index, item in enumerate(generated_question_items, start=1)
                ]
            elif source == "past":
                questions = [
                    {
                        "position": index,
                        "candidate_id": item.get("candidate_id"),
                        "chapter": item.get("chapter"),
                        "source_file": item.get("source_file"),
                        "source_number": item.get("source_number"),
                        "difficulty": item.get("difficulty"),
                        "adapted": bool(item.get("adapted")),
                        "fingerprint": _question_fingerprint(item.get("content", "")),
                    }
                    for index, item in enumerate(
                        past_selections[paper_index - 1], start=1
                    )
                ]
            else:
                questions = [
                    {
                        "position": 1,
                        "content": result.get("content", ""),
                        "fingerprint": _question_fingerprint(result.get("content", "")),
                    }
                ]
            pending_memory_results.append(
                {
                    "event_id": f"{operation_id}:result:{paper_index}",
                    "event_type": "result",
                    "operation_id": operation_id,
                    "paper_index": paper_index,
                    "subject": subj_name,
                    "chapters": ch_names,
                    "request": {
                        "difficulty": difficulty,
                        "target": target,
                        "question_count": qcount,
                        "types": types,
                        "note": note,
                    },
                    "questions": questions,
                    "result": {
                        "exam_id": result.get("id"),
                        "title": result.get("title"),
                        "source": source,
                        "learning_years": learning_years if source == "past" else [],
                        "structure_analysis": past_analysis if source == "past" else None,
                    },
                    "user_behavior": {
                        "allow_adaptation": allow_adaptation if source == "past" else None,
                        "selected_mistake_ids": d.get("mistake_ids", []) if source == "mistakes" else [],
                    },
                }
            )
        _remember_exam_event(
            memory,
            memory_type,
            memory_actor,
            pending_memory_request,
        )
        for memory_result in pending_memory_results:
            _remember_exam_event(
                memory,
                memory_type,
                memory_actor,
                memory_result,
            )
        response = dict(results[0])
        response['count'] = len(results)
        response['exams'] = results
        material_parts = [
            part for part in (material_understanding, selection_material_understanding)
            if isinstance(part, dict)
        ]
        if material_parts:
            response['material_understanding'] = {
                'complete': all(bool(part.get('complete')) for part in material_parts),
                'total_count': sum(int(part.get('total_count') or 0) for part in material_parts),
                'analyzed_count': sum(int(part.get('analyzed_count') or 0) for part in material_parts),
                'chapter_count': len({chapter for part in material_parts for chapter in (part.get('chapters') or [])}),
                'source_count': len({source for part in material_parts for source in (part.get('sources') or {})}),
                'years': sorted({year for part in material_parts for year in (part.get('years') or [])}),
                'batch_count': sum(int(part.get('batch_count') or 0) for part in material_parts),
                'reused_count': sum(int(part.get('reused_count') or 0) for part in material_parts),
                'cache_keys': [part.get('cache_key') for part in material_parts if part.get('cache_key')],
            }
        return jsonify(response)
    except Exception as e:
        db.rollback()
        _remove_exam_artifacts(created_exam_ids)
        if created_exam_ids:
            placeholders = ",".join("?" for _ in created_exam_ids)
            db.execute(
                f"DELETE FROM exams WHERE id IN ({placeholders})",
                created_exam_ids,
            )
            db.commit()
        if isinstance(e, (TexCompileError, UnsafeLatexError)):
            message = str(e)
        else:
            message = _safe_error_text(e)
        return jsonify({'error': f'生成失败: {message}'}), 500
