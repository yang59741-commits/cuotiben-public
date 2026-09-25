"""Pure input contracts shared by exam generation and selection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .exam_difficulty_policy import (
    OVERALL_DIFFICULTY_VALUES,
    QUESTION_DIFFICULTY_VALUES,
    serialized_difficulty_targets,
)


DIFFICULTIES = set(OVERALL_DIFFICULTY_VALUES)
EXAM_TARGETS = {"日常练习", "考研训练"}
QUESTION_TYPES = {"选择题", "填空题", "计算题", "证明题", "综合题"}
QUESTION_DIFFICULTIES = set(QUESTION_DIFFICULTY_VALUES)
PAST_CANDIDATE_SOURCES = {"ordinary", "other_past", "mixed"}
MAX_MISTAKE_REFERENCES = 20
DIFFICULTY_AUDIT_SCHEMA_VERSION = "difficulty-audit-v1"


@dataclass(frozen=True)
class DifficultyAuditResult:
    """Serializable audit of actual labels against the preferred range."""

    question_count: int
    counts: Mapping[str, int]
    preferred_bounds: Mapping[str, int]
    within_preferred_range: bool
    requires_adjustment: bool
    reason_codes: tuple[str, ...]
    warning: str | None
    schema_version: str = DIFFICULTY_AUDIT_SCHEMA_VERSION

    def to_dict(self):
        return {
            "schema_version": self.schema_version,
            "question_count": self.question_count,
            "counts": dict(self.counts),
            "preferred_bounds": dict(self.preferred_bounds),
            "within_preferred_range": self.within_preferred_range,
            "requires_adjustment": self.requires_adjustment,
            "reason_codes": list(self.reason_codes),
            "warning": self.warning,
        }


class ExamGenerationConstraintError(ValueError):
    """A user-correctable generation constraint that must not be retried."""

    def __init__(
        self,
        message,
        *,
        code="exam_constraint_unsatisfied",
        hint="请调整组卷条件后重新提交",
        actions=(),
        details=None,
        status_code=422,
    ):
        super().__init__(message)
        self.code = str(code)
        self.status_code = int(status_code)
        self.hint = str(hint)
        self.actions = tuple(str(action) for action in actions if str(action))
        self.details = dict(details or {})

DIFFICULTY_GUIDANCE = {
    "基础": "以单一知识点、定义定理的直接应用和标准运算为主，避免偏题与技巧跳跃。",
    "中等": "以一至两个知识点的常规综合和典型变式为主，需要清晰的多步推理或计算。",
    "困难": "允许多个知识点综合与非直接套用，要求完整论证或较长推导，但不得超出所选章节。",
}
TARGET_GUIDANCE = {
    "日常练习": "用于日常巩固，题意清楚、梯度平稳，不故意追求偏怪技巧。",
    "考研训练": "采用研究生入学考试常见表述与区分度，但不把竞赛偏题当作高难题。",
}


def positive_int(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label}必须是正整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if parsed <= 0 or str(value).strip() != str(parsed):
        raise ValueError(f"{label}必须是正整数")
    return parsed


def validate_single_paper_count(value):
    """Accept only the integer value 1 for a newly submitted generation task."""

    if type(value) is not int or value != 1:
        raise ExamGenerationConstraintError(
            "当前版本每次组卷只能生成 1 份试卷",
            code="invalid_paper_count",
            hint="请删除份数参数或将其设为整数 1 后重新提交",
            actions=("resubmit_single_paper",),
            details={
                "expected": 1,
                "actual_type": type(value).__name__,
            },
            status_code=400,
        )
    return 1


def require_single_paper_sequence(items, *, label="试卷"):
    """Reject empty or multi-item write/output collections under the new contract."""

    if isinstance(items, (str, bytes)) or not isinstance(items, Sequence):
        actual_count = None
    else:
        actual_count = len(items)
    if actual_count != 1:
        raise ExamGenerationConstraintError(
            f"{label}数量必须恰好为 1",
            code="invalid_paper_count",
            hint="旧多卷未完成任务需要重新提交；历史已发布多卷仅可查看或导出",
            actions=("resubmit_single_paper",),
            details={"expected": 1, "actual_count": actual_count},
            status_code=400,
        )
    return items[0]


def bounded_text(value, label, maximum, *, required=False):
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


def validate_overall_difficulty(value):
    """Return the sole persisted overall label or a stable request error."""

    if type(value) is not str or value not in DIFFICULTIES:
        raise ExamGenerationConstraintError(
            "难度选项无效",
            code="invalid_difficulty_label",
            hint="整体难度只能选择基础、中等或困难",
            actions=("choose_supported_difficulty",),
            status_code=400,
        )
    return value


def parse_exam_options(data, *, default_count):
    difficulty = validate_overall_difficulty(data.get("difficulty", "中等"))
    target = data.get("target")
    if target in (None, ""):
        target = "日常练习"
    if target not in EXAM_TARGETS:
        raise ValueError("组卷场景选项无效")
    qcount = positive_int(data.get("question_count", default_count), "题量")
    if qcount > 20:
        raise ValueError("题量必须在 1–20 之间")
    types = data.get("types", ["计算题", "证明题"])
    if not isinstance(types, list) or not types or any(not isinstance(item, str) for item in types):
        raise ValueError("题型必须是非空文本列表")
    if len(types) != len(set(types)) or any(item not in QUESTION_TYPES for item in types):
        raise ValueError("题型选项无效或重复")
    note = bounded_text(data.get("note", ""), "补充要求", 2000)
    return difficulty, target, qcount, types, note


def difficulty_contract(difficulty, target):
    return (
        f"难度等级：{difficulty}。{DIFFICULTY_GUIDANCE[difficulty]}\n"
        f"使用场景：{target}。{TARGET_GUIDANCE[target]}"
    )


def past_candidate_quotas(
    candidate_source,
    question_count,
    *,
    institution=None,
    subject=None,
    reference_year=None,
):
    if candidate_source not in PAST_CANDIDATE_SOURCES:
        raise ValueError("真题候选来源选项无效")
    count = positive_int(question_count, "题量")
    if candidate_source == "ordinary":
        return {"ordinary": count}
    if candidate_source == "other_past":
        return {"past_exam": count}
    if count < 2:
        raise ValueError("混合题源至少需要 2 道题，请更换结构年份或候选来源")
    # Hunan Normal University's 2026 math-analysis paper has ten fill-blank
    # slots, while the ordinary pool has none.  Keep this as a narrowly scoped
    # target exception; every other mixed paper retains the historical rule.
    if (
        count == 17
        and institution == "湖南师范大学"
        and subject == "数学分析"
        and reference_year == 2026
    ):
        return {"ordinary": 7, "past_exam": 10}
    other_count = count // 2
    return {"ordinary": count - other_count, "past_exam": other_count}


def difficulty_quota(question_count, difficulty):
    count = positive_int(question_count, "题量")
    try:
        return serialized_difficulty_targets(count, difficulty)
    except ValueError as exc:
        raise ValueError("难度选项无效") from exc


def difficulty_preferred_bounds(question_count):
    """Return exact integer bounds for the preferred difficulty range."""

    count = positive_int(question_count, "题量")
    return {
        "max_easy": count if count < 5 else count * 3 // 10,
        "min_medium_plus_hard": 0 if count < 5 else (count * 7 + 9) // 10,
        "min_medium": (count + 4) // 5 if count >= 5 else 0,
        "min_hard": (count + 4) // 5 if count >= 5 else 0,
    }


def audit_difficulty_labels(
    labels: Sequence[str],
    question_count=None,
    *,
    overall_difficulty=None,
):
    """Audit actual labels without turning a soft target into rejection."""

    if isinstance(labels, (str, bytes)) or not isinstance(labels, Sequence):
        raise ValueError("逐题难度标签必须是数组")
    count = positive_int(
        len(labels) if question_count is None else question_count,
        "题量",
    )
    if len(labels) != count:
        raise ValueError(
            f"逐题难度结果数量不一致：应为{count}题，实际{len(labels)}题"
        )
    normalized = []
    for label in labels:
        if not isinstance(label, str) or label.strip() not in QUESTION_DIFFICULTIES:
            raise ValueError("逐题难度标签必须是简单、中等或困难")
        normalized.append(label.strip())
    counts = {
        label: sum(actual == label for actual in normalized)
        for label in ("简单", "中等", "困难")
    }
    if sum(counts.values()) != count:
        raise ValueError("逐题难度计数与总题量不一致")
    bounds = difficulty_preferred_bounds(count)
    reason_codes = []
    if count < 5 and overall_difficulty is not None:
        target = difficulty_quota(count, overall_difficulty)
        if counts != target:
            reason_codes.append("small_sample_target_deviation")
    if count >= 5 and counts["简单"] > bounds["max_easy"]:
        reason_codes.extend((
            "easy_above_preferred_range",
            "medium_hard_below_preferred_range",
        ))
    if count >= 5 and counts["中等"] < bounds["min_medium"]:
        reason_codes.append("medium_below_preferred_range")
    if count >= 5 and counts["困难"] < bounds["min_hard"]:
        reason_codes.append("hard_below_preferred_range")
    requires_adjustment = (
        count >= 5 and "easy_above_preferred_range" in reason_codes
    )
    return DifficultyAuditResult(
        question_count=count,
        counts=counts,
        preferred_bounds=bounds,
        within_preferred_range=not reason_codes,
        requires_adjustment=requires_adjustment,
        reason_codes=tuple(reason_codes),
        warning=(
            None
            if not reason_codes
            else "试卷难度分布未完全达到偏好区间，已保留当前试卷"
        ),
    )


def validate_difficulty_items(items, question_count, quota=None):
    """Validate position and label schema; quota remains a soft preference."""

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
        position = item.get("position", fallback)
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("逐题难度位置无效")
        label = str(item.get("difficulty") or "").strip()
        if position < 1 or position > question_count or position in positions:
            raise ValueError("逐题难度位置重复或越界")
        if label not in QUESTION_DIFFICULTIES:
            raise ValueError("逐题难度标签必须是简单、中等或困难")
        positions.add(position)
        labels.append((position, label))
    labels.sort()
    return [label for _, label in labels]


def validate_generation_job_id(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) != 32 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError("任务编号无效")
    return value


def validate_source(value):
    source = "free" if value is None else value
    if not isinstance(source, str) or source not in {"free", "mistakes", "past"}:
        raise ValueError("不支持的组卷来源")
    return source
