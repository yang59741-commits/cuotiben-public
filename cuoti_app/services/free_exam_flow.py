"""Explicit free-exam prompt and structured-generation flow."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from .exam_contracts import difficulty_contract
from .exam_local_repair import FrozenGenerationEvidence, freeze_generation_evidence
from .exam_prompting import full_candidate_prompt_text


@dataclass(frozen=True)
class FreeGenerationInput:
    subject: str
    chapters: Sequence[str]
    question_count: int
    question_types: Sequence[str]
    difficulty: str
    target: str
    note: str
    candidates: Sequence[Mapping[str, Any]]
    question_bank_coverage: Mapping[str, Any]
    difficulty_quota: Mapping[str, int]
    material_understanding: Mapping[str, Any]
    memory_context: str = ""


@dataclass(frozen=True)
class FreeGenerationResult:
    system_prompt: str
    prompt: str
    latex_body: str
    structured_items: Sequence[dict[str, Any]]
    difficulty_labels: Sequence[str]
    frozen_evidence: FrozenGenerationEvidence


def freeze_free_generation_evidence(
    state: FreeGenerationInput,
) -> FrozenGenerationEvidence:
    """Freeze the complete candidate pool and generation constraints."""

    return freeze_generation_evidence(
        "free",
        expected_count=state.question_count,
        candidates=state.candidates,
        constraints={
            "subject": state.subject,
            "chapters": list(state.chapters),
            "question_types": list(state.question_types),
            "difficulty": state.difficulty,
            "target": state.target,
            "difficulty_quota": dict(state.difficulty_quota),
            "question_bank_coverage": dict(state.question_bank_coverage),
            "material_understanding": dict(state.material_understanding),
        },
    )


def build_free_prompts(state: FreeGenerationInput) -> tuple[str, str]:
    """Build the stable free-flow prompt without application/runtime objects."""

    difficulty_text = difficulty_contract(state.difficulty, state.target)
    prompt = f'''你是{state.subject}命题专家。请出一份规范的LaTeX试卷。

    科目={state.subject}，章节={"、".join(state.chapters)}，题量={state.question_count}题，题型={"、".join(state.question_types)}
    {difficulty_text}
    {"补充："+state.note if state.note else ""}

    【有效题库候选题】
    本地程序已经遍历所选章节的全部 {state.question_bank_coverage["total_questions"]} 道去重有效题，
    并依据章节覆盖、来源多样性和软排序选出以下完整候选。候选题没有字符截断：
    {full_candidate_prompt_text(state.candidates)}

    【候选池分批深读归纳】
    以下归纳来自全部 {state.material_understanding["analyzed_count"]} 道完整候选题的逐题分析卡；analysis_confidence 低于 0.6 的卡只能作为软提示，不能作为硬槽位或多样性事实；最终选择仍必须复核上方完整题干：
    {json.dumps(state.material_understanding["summary"], ensure_ascii=False)}

    【自由组卷历史记忆】
    {state.memory_context or '暂无历史记忆，请根据当前候选题和组卷参数生成。'}
    上述跨任务历史只用于软排序和审计：曾出现、曾选择、正文完全相同、题型、知识点或相似描述均不得成为候选删除或硬否决理由。
    仅当前这一次组卷结果内部不得出现正文完全相同的重复题；这项约束不得延续到其他组卷任务。
    如果历史记忆明确该题尚未掌握，可以提高其排序优先级，但不得由此改写任何硬合同。

    【难度分布软目标】
    {json.dumps(state.difficulty_quota, ensure_ascii=False)}
    上述数量只用于生成排序和分布偏好，不要求实际计数精确相等，也不得为了凑数虚标难度。
    优先使简单题不超过30%、中等与困难合计不少于70%；题量不少于5时，中等和困难各不少于20%。
    每道题必须如实标注实际难度；实际分布落在合理区间时无需再次调整。

    【输出格式】
    只返回 JSON 对象：
    {{
      "knowledge_prelude": "与本卷相关的3-5条核心定义、定理或公式，使用LaTeX正文",
      "questions": [
    {{
      "position": 1,
      "chapter": "{state.chapters[0] if state.chapters else '章节名'}",
      "type": "计算题",
      "difficulty": "中等",
      "latex": "完整题干，不含problem环境",
      "reference_ids": ["候选题ID"]
    }}
      ]
    }}
    questions 必须恰好 {state.question_count} 项，position 连续且不重复；
    每题 chapter 必填，且必须逐字取自此冻结章节数组：{json.dumps(list(state.chapters), ensure_ascii=False)}；
    不得输出 document 环境或 Markdown。'''
    system_prompt = (
        f'你是{state.subject}命题专家。根据有效题库候选生成新题，'
        '不得超出所选章节；每题必须输出冻结允许章节中的 chapter；'
        '必须严格遵守题型和结构化输出要求；难度数量仅为软目标，必须如实标注。'
    )
    return system_prompt, prompt


def generate_free_exam(
    state: FreeGenerationInput,
    *,
    generate_indexed: Callable[..., tuple[str, Sequence[dict[str, Any]], Sequence[str]]],
) -> FreeGenerationResult:
    """Run the provider adapter while keeping retry/validation in the existing helper."""

    system_prompt, prompt = build_free_prompts(state)
    frozen_evidence = freeze_free_generation_evidence(state)
    latex_body, structured_items, difficulty_labels = generate_indexed(
        system_prompt,
        prompt,
        state.question_count,
        dict(state.difficulty_quota),
        state.subject,
        [str(item["candidate_id"]) for item in state.candidates],
        list(state.question_types),
        list(state.chapters),
    )
    return FreeGenerationResult(
        system_prompt=system_prompt,
        prompt=prompt,
        latex_body=latex_body,
        structured_items=structured_items,
        difficulty_labels=difficulty_labels,
        frozen_evidence=frozen_evidence,
    )


__all__ = [
    "FreeGenerationInput",
    "FreeGenerationResult",
    "build_free_prompts",
    "freeze_free_generation_evidence",
    "generate_free_exam",
]
