"""Mistake-exam training-target vocabulary and prompt contracts."""

from __future__ import annotations


TRAINING_TARGETS = {
    "general": "综合巩固：围绕原错题核心知识与方法形成合理的新练习。",
    "concept": "概念辨析：突出定义、定理适用范围或相近概念的辨别。",
    "method_selection": "方法选择：题面应要求学习者在多种可行思路中识别合适方法；存在多种解法不构成不匹配。",
    "condition_check": "条件核对：突出条件的识别、比较、边界或使用时机，不要求审核者判断充分性或求解。",
    "transfer": "迁移应用：保持核心知识关联，同时改变表述、情境或组合方式。",
}


def validate_training_target(value: object, *, explicit: bool) -> str | None:
    if not explicit:
        return None
    target = str(value or "").strip()
    if target not in TRAINING_TARGETS:
        raise ValueError("训练目标无效")
    return target


def training_target_contract(target: str) -> dict[str, str]:
    if target not in TRAINING_TARGETS:
        raise ValueError("训练目标无效")
    return {"code": target, "definition": TRAINING_TARGETS[target]}


__all__ = ["TRAINING_TARGETS", "training_target_contract", "validate_training_target"]
