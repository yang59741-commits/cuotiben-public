"""Answer-free exam quality review and targeted-position contracts."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass
from numbers import Real
from typing import Any, Callable, Iterable, Mapping, Sequence

from .exam_prompting import (
    build_exam_quality_review_messages,
    build_targeted_item_repair_messages,
)
from .exam_sources import parse_ai_json_object
from .tex import (
    UNSAFE_LATEX_PATTERN,
    UnsafeLatexError,
    normalize_generated_latex,
    validate_generated_latex,
)


REVIEW_PROMPT_VERSION = "exam-quality-review-v1"
REVIEW_SCHEMA_VERSION = "exam-quality-review-v1"
TRAINING_REVIEW_PROMPT_VERSION = "exam-quality-review-training-v2"
TRAINING_REVIEW_SCHEMA_VERSION = "exam-quality-review-training-v2"
REVIEW_MAX_ATTEMPTS = 2
ALLOWED_DIFFICULTIES = frozenset({"简单", "中等", "困难", "未知"})
ALLOWED_ISSUES = frozenset({
    "chapter_mismatch", "question_type_mismatch", "difficulty_mismatch",
    "section_slot_mismatch", "reference_ungrounded", "near_duplicate",
    "adaptation_scope_invalid", "answer_leak", "latex_invalid",
    "forbidden_latex_command",
    "training_target_mismatch",
})
MODEL_REVIEW_ISSUES = frozenset({
    "question_type_mismatch", "difficulty_mismatch",
    "reference_ungrounded", "near_duplicate",
    "training_target_mismatch",
})
FORBIDDEN_REVIEW_FIELDS = frozenset({
    "well_posed", "sufficient_conditions", "solvable", "solution", "answer",
    "reference_answer", "reasoning", "chain_of_thought", "analysis",
    "scratchpad",
})
REVIEW_ITEM_FIELDS = frozenset({
    "position", "actual_chapter", "actual_question_type", "actual_difficulty",
    "section_slot_match", "reference_grounded", "duplicate_group",
    "duplicate_risk", "adaptation_scope_valid", "answer_leak", "issues",
    "confidence",
})
TRAINING_REVIEW_ITEM_FIELDS = REVIEW_ITEM_FIELDS | {
    "training_target_status", "training_target_reason",
}
REFERENCE_REVIEW_FIELDS = (
    "candidate_id", "knowledge_points", "question_type", "methods",
    "difficulty", "traps", "source_constraints", "confidence", "content",
    "chapter", "source_kind", "source_file", "source_number",
    "source_institution", "source_year", "source_section",
)
_ANSWER_LEAK = re.compile(
    r"\\begin\s*\{(?:solution|answer)\}|"
    r"\\(?:sub)*section\*?\s*\{\s*(?:参考)?(?:答案|解答|解析)\s*\}|"
    r"(?:^|[\n\r])\s*(?:参考)?(?:答案|解答|解析)\s*[：:]",
    re.IGNORECASE,
)
_PROBLEM = re.compile(
    r"(\\begin\s*\{problem\})(.*?)(\\end\s*\{problem\})", re.DOTALL,
)
_PAST_HEADING = re.compile(
    r"(\\subsection\*?\s*\{\s*题目\s*([1-9]\d*)\s*\})(.*?)"
    r"(?=\\subsection\*?\s*\{\s*题目\s*[1-9]\d*\s*\}|\Z)",
    re.DOTALL,
)
_PAST_ADAPTATION_MARKER = re.compile(
    r"\\textbf\s*\{\s*(?:\[|［)\s*小幅改编\s*(?:\]|］)\s*\}"
)
_PAST_SECTION_SUFFIX = re.compile(
    r"(?m)^[^\S\r\n]*\\noindent[^\S\r\n]*\\textbf[^\S\r\n]*"
    r"\{[^\r\n]*\}[^\S\r\n]*(?:(?:\r\n|\r|\n)[^\S\r\n]*)*\Z"
)
_ADAPTED_SUFFIX = re.compile(r"(?:\[|［)\s*改编\s*(?:\]|］)\s*$")
_REPAIR_WRAPPER = re.compile(
    r"\\begin\s*\{problem\}|\\end\s*\{problem\}|"
    r"\\subsection\*?\s*\{\s*题目\s*[1-9]\d*\s*\}",
    re.IGNORECASE,
)
_DUPLICATE_GROUP = re.compile(r"[A-Za-z0-9_.:-]{0,80}\Z")
_SAFE_REVIEW_VALIDATION_MESSAGES = frozenset({
    "题目 position 必须是正整数",
    "题目 position 必须连续且唯一",
    "试卷审核未返回有效 JSON",
    "试卷审核包含禁止的求解类字段",
    "试卷审核顶层字段无效",
    "试卷审核题目数量无效",
    "试卷审核题目字段无效",
    "试卷审核章节不在允许目录",
    "试卷审核题型不在允许目录",
    "试卷审核难度无效",
    "试卷审核 section_slot_match 必须是布尔值",
    "试卷审核 reference_grounded 必须是布尔值",
    "试卷审核 adaptation_scope_valid 必须是布尔值",
    "试卷审核 answer_leak 必须是布尔值",
    "试卷审核重复组无效",
    "试卷审核 duplicate_risk 必须是数值",
    "试卷审核 duplicate_risk 超出范围",
    "试卷审核 confidence 必须是数值",
    "试卷审核 confidence 超出范围",
    "试卷审核理由码无效",
    "模型审核未保持本地确定性检查结果",
    "试卷审核输出未通过本地 Schema",
})


class ExamQualityError(ValueError):
    """Stable fail-closed error which never includes question or model text."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        failed_positions: Sequence[int] = (),
        issue_codes: Sequence[str] = (),
        schema_error_code: str | None = None,
        schema_error_reason: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.failed_positions = tuple(
            sorted({int(value) for value in failed_positions})
        )
        self.issue_codes = tuple(
            sorted({str(value) for value in issue_codes if str(value) in ALLOWED_ISSUES})
        )
        self.schema_error_code = str(schema_error_code or "").strip() or None
        self.schema_error_reason = str(schema_error_reason or "").strip() or None


def _safe_review_validation_failure(
    error: ExamQualityError,
) -> tuple[str, str]:
    """Return only whitelisted local validation metadata for retries/errors."""

    reason = str(error)
    if reason not in _SAFE_REVIEW_VALIDATION_MESSAGES:
        return "REVIEW_SCHEMA", "试卷审核输出未通过本地 Schema"
    code = str(error.code or "REVIEW_SCHEMA").strip().upper()
    if code not in {"POSITION", "REVIEW_FORBIDDEN_FIELD", "REVIEW_SCHEMA"}:
        code = "REVIEW_SCHEMA"
    return code, reason


@dataclass(frozen=True, slots=True)
class ExamQualityReview:
    items: tuple[dict[str, Any], ...]
    passed: bool
    failed_positions: tuple[int, ...]
    same_model_review: bool
    prompt_version: str = REVIEW_PROMPT_VERSION
    schema_version: str = REVIEW_SCHEMA_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed_positions": list(self.failed_positions),
            "same_model_review": self.same_model_review,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "items": [dict(item) for item in self.items],
        }


def _content(item: Mapping[str, Any]) -> str:
    return str(item.get("content") or item.get("latex") or "")


def normalized_question_text(value: object) -> str:
    """Normalize formatting while retaining the mathematical tokens."""

    text = re.sub(r"(?m)%[^\n\r]*", " ", str(value or "")).casefold()
    text = re.sub(r"\\(?:begin|end)\s*\{[^{}]+\}", " ", text)
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def normalized_question_hash(value: object) -> str:
    return hashlib.sha256(
        normalized_question_text(value).encode("utf-8")
    ).hexdigest()


def _ngrams(value: str, size: int = 5) -> set[str]:
    if len(value) <= size:
        return {value} if value else set()
    return {
        value[index:index + size]
        for index in range(len(value) - size + 1)
    }


def near_duplicate_ratio(left: object, right: object) -> float:
    left_terms = _ngrams(normalized_question_text(left))
    right_terms = _ngrams(normalized_question_text(right))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _strict_position(value: object, *, expected: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExamQualityError("POSITION", "题目 position 必须是正整数")
    if expected is not None and value != expected:
        raise ExamQualityError("POSITION", "题目 position 必须连续且唯一")
    return value


def _string_set(values: Iterable[str]) -> set[str]:
    return {str(value).strip() for value in values if str(value).strip()}


def _slot_issues(
    item: Mapping[str, Any],
    position: int,
    contracts: Mapping[int, Mapping[str, Any]],
) -> set[str]:
    contract = contracts.get(position)
    if not isinstance(contract, Mapping):
        return set()
    chapter = str(item.get("chapter") or item.get("target_chapter") or "").strip()
    question_type = str(
        item.get("type") or item.get("question_type")
        or item.get("target_question_type") or ""
    ).strip()
    difficulty = str(
        item.get("difficulty") or item.get("target_difficulty") or ""
    ).strip()
    section = str(item.get("section") or item.get("section_label") or "").strip()
    allowed_chapters = _string_set(contract.get("allowed_chapters") or ())
    allowed_types = _string_set(contract.get("allowed_question_types") or ())
    allowed_difficulties = _string_set(
        contract.get("allowed_difficulties")
        or contract.get("difficulty_band") or ()
    )
    expected_section = str(contract.get("section") or "").strip()
    mismatch = (
        (allowed_chapters and chapter not in allowed_chapters)
        or (allowed_types and question_type not in allowed_types)
        or (allowed_difficulties and difficulty not in allowed_difficulties)
        or (expected_section and section != expected_section)
    )
    return {"section_slot_mismatch"} if mismatch else set()


def deterministic_exam_issues(
    items: Sequence[Mapping[str, Any]],
    *,
    expected_count: int,
    candidate_ids: Iterable[str] = (),
    allowed_question_types: Iterable[str] = (),
    allowed_chapters: Iterable[str] = (),
    prior_question_texts: Sequence[str] = (),
    duplicate_threshold: float = 0.88,
    section_slot_contracts: Mapping[int, Mapping[str, Any]] | None = None,
    max_adaptations: int | None = None,
) -> dict[int, list[str]]:
    """Run answer-free structure, safety, identity, and duplicate checks."""

    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise ExamQualityError("QUESTION_COUNT", "冻结题量必须是正整数")
    if len(items) != expected_count:
        raise ExamQualityError("QUESTION_COUNT", "试卷题量与冻结要求不一致")
    if (
        not isinstance(duplicate_threshold, Real)
        or isinstance(duplicate_threshold, bool)
        or not 0 < float(duplicate_threshold) <= 1
    ):
        raise ExamQualityError("DUPLICATE_THRESHOLD", "近重复阈值无效")
    if max_adaptations is not None and (
        isinstance(max_adaptations, bool)
        or not isinstance(max_adaptations, int)
        or max_adaptations < 0
    ):
        raise ExamQualityError("ADAPTATION_LIMIT", "改编题上限无效")

    known_ids = _string_set(candidate_ids)
    allowed_types = _string_set(allowed_question_types)
    allowed_chapter_set = _string_set(allowed_chapters)
    contracts = section_slot_contracts or {}
    issues: dict[int, set[str]] = {
        position: set() for position in range(1, expected_count + 1)
    }
    contents: list[str] = []
    adapted_positions: list[int] = []

    for expected_position, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ExamQualityError("ITEM_SCHEMA", "试卷题目项必须是对象")
        position = _strict_position(item.get("position"), expected=expected_position)
        content = _content(item)
        contents.append(content)
        if not content.strip():
            issues[position].add("latex_invalid")
        else:
            try:
                validate_generated_latex(content)
            except UnsafeLatexError:
                issues[position].add(
                    "forbidden_latex_command"
                    if UNSAFE_LATEX_PATTERN.search(content)
                    else "latex_invalid"
                )
        if _ANSWER_LEAK.search(content):
            issues[position].add("answer_leak")

        question_type = str(
            item.get("type") or item.get("question_type")
            or item.get("target_question_type") or ""
        ).strip()
        chapter = str(
            item.get("chapter") or item.get("target_chapter") or ""
        ).strip()
        if allowed_types and question_type and question_type not in allowed_types:
            issues[position].add("question_type_mismatch")
        if allowed_chapter_set and chapter and chapter not in allowed_chapter_set:
            issues[position].add("chapter_mismatch")
        issues[position].update(_slot_issues(item, position, contracts))

        candidate_id = str(item.get("candidate_id") or "").strip()
        if known_ids and candidate_id and candidate_id not in known_ids:
            issues[position].add("reference_ungrounded")
        raw_references = item.get("reference_ids")
        references = (
            [candidate_id]
            if raw_references is None and candidate_id
            else (raw_references or [])
        )
        valid_references = (
            isinstance(references, list)
            and all(isinstance(value, str) and value.strip() for value in references)
            and len(references) == len(set(references))
        )
        if not valid_references or (
            known_ids and (not references or _string_set(references) - known_ids)
        ):
            issues[position].add("reference_ungrounded")

        adapted = bool(item.get("adapted")) or bool(
            _ADAPTED_SUFFIX.search(content.strip())
        )
        if adapted:
            adapted_positions.append(position)
        if item.get("adaptation_scope_valid") is False:
            issues[position].add("adaptation_scope_invalid")

    if max_adaptations is not None and len(adapted_positions) > max_adaptations:
        for position in adapted_positions[max_adaptations:]:
            issues[position].add("adaptation_scope_invalid")

    previous_hashes: dict[str, int] = {}
    for index, content in enumerate(contents, start=1):
        normalized = normalized_question_text(content)
        digest = normalized_question_hash(content)
        if normalized and digest in previous_hashes:
            issues[index].add("near_duplicate")
        elif normalized:
            previous_hashes[digest] = index
        if "near_duplicate" not in issues[index]:
            for prior in contents[:index - 1]:
                if near_duplicate_ratio(content, prior) >= float(duplicate_threshold):
                    issues[index].add("near_duplicate")
                    break
        if "near_duplicate" not in issues[index]:
            for prior in prior_question_texts:
                if near_duplicate_ratio(content, prior) >= float(duplicate_threshold):
                    issues[index].add("near_duplicate")
                    break
    return {
        position: sorted(values)
        for position, values in issues.items()
        if values
    }


def extract_review_items(
    body: str,
    *,
    expected_count: int,
) -> tuple[dict[str, Any], ...]:
    """Extract consecutive review items from problem or past-heading TeX."""

    if (
        isinstance(expected_count, bool)
        or not isinstance(expected_count, int)
        or expected_count < 1
    ):
        raise ExamQualityError("QUESTION_COUNT", "冻结题量必须是正整数")
    text = str(body or "")
    problem_matches = list(_PROBLEM.finditer(text))
    extracted: list[dict[str, Any]] = []
    if problem_matches:
        extracted = [
            {"position": position, "content": match.group(2).strip()}
            for position, match in enumerate(problem_matches, start=1)
        ]
    else:
        for match in _PAST_HEADING.finditer(text):
            position = _strict_position(int(match.group(2)))
            extracted.append({
                "position": position,
                "content": match.group(3).strip(),
            })
    if len(extracted) != expected_count:
        raise ExamQualityError("QUESTION_COUNT", "试卷题量与冻结要求不一致")
    for expected_position, item in enumerate(extracted, start=1):
        _strict_position(item["position"], expected=expected_position)
        if not item["content"]:
            raise ExamQualityError(
                "ITEM_SCHEMA", "试卷包含空题干",
                failed_positions=(expected_position,),
            )
    return tuple(extracted)


def _forbidden_fields(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().casefold()
            if normalized in FORBIDDEN_REVIEW_FIELDS:
                found.add(normalized)
            found.update(_forbidden_fields(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_forbidden_fields(child))
    return found


def _unit_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ExamQualityError("REVIEW_SCHEMA", f"试卷审核 {label} 必须是数值")
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ExamQualityError("REVIEW_SCHEMA", f"试卷审核 {label} 超出范围")
    return number


def validate_review_payload(
    raw: object,
    *,
    items: Sequence[Mapping[str, Any]],
    allowed_chapters: Iterable[str],
    allowed_question_types: Iterable[str],
    deterministic_issues: Mapping[int, Sequence[str]] | None = None,
    same_model_review: bool = False,
    training_target: str | None = None,
) -> ExamQualityReview:
    try:
        parsed = (
            parse_ai_json_object(str(raw), error_message="试卷审核未返回JSON")
            if isinstance(raw, str) else raw
        )
    except ValueError as exc:
        raise ExamQualityError(
            "REVIEW_SCHEMA", "试卷审核未返回有效 JSON"
        ) from exc
    if _forbidden_fields(parsed):
        raise ExamQualityError(
            "REVIEW_FORBIDDEN_FIELD", "试卷审核包含禁止的求解类字段"
        )
    if not isinstance(parsed, Mapping) or set(parsed) != {"items"}:
        raise ExamQualityError("REVIEW_SCHEMA", "试卷审核顶层字段无效")
    values = parsed.get("items")
    if not isinstance(values, list) or len(values) != len(items):
        raise ExamQualityError("REVIEW_SCHEMA", "试卷审核题目数量无效")
    chapters = _string_set(allowed_chapters)
    question_types = _string_set(allowed_question_types)
    local_issues = {
        int(position): set(map(str, reasons))
        for position, reasons in (deterministic_issues or {}).items()
    }
    validated: list[dict[str, Any]] = []
    failed: set[int] = set()

    for expected_position, value in enumerate(values, start=1):
        expected_fields = TRAINING_REVIEW_ITEM_FIELDS if training_target else REVIEW_ITEM_FIELDS
        if not isinstance(value, Mapping) or set(value) != expected_fields:
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核题目字段无效")
        _strict_position(value.get("position"), expected=expected_position)
        actual_chapter = str(value.get("actual_chapter") or "").strip()
        actual_type = str(value.get("actual_question_type") or "").strip()
        actual_difficulty = str(value.get("actual_difficulty") or "").strip()
        if not actual_chapter or (chapters and actual_chapter not in chapters):
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核章节不在允许目录")
        if not actual_type or (question_types and actual_type not in question_types):
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核题型不在允许目录")
        if actual_difficulty not in ALLOWED_DIFFICULTIES:
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核难度无效")
        for field in (
            "section_slot_match", "reference_grounded",
            "adaptation_scope_valid", "answer_leak",
        ):
            if not isinstance(value.get(field), bool):
                raise ExamQualityError(
                    "REVIEW_SCHEMA", f"试卷审核 {field} 必须是布尔值"
                )
        duplicate_group = value.get("duplicate_group")
        if duplicate_group is not None and not isinstance(duplicate_group, str):
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核重复组无效")
        duplicate_group = str(duplicate_group or "").strip()
        if not _DUPLICATE_GROUP.fullmatch(duplicate_group):
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核重复组无效")
        duplicate_risk = _unit_number(value.get("duplicate_risk"), "duplicate_risk")
        confidence = _unit_number(value.get("confidence"), "confidence")
        raw_issues = value.get("issues")
        if (
            not isinstance(raw_issues, list)
            or len(raw_issues) != len(set(map(str, raw_issues)))
            or any(
                not isinstance(issue, str) or issue not in MODEL_REVIEW_ISSUES
                for issue in raw_issues
            )
        ):
            raise ExamQualityError("REVIEW_SCHEMA", "试卷审核理由码无效")
        training_status = training_reason = None
        if training_target:
            training_status = str(value.get("training_target_status") or "").strip()
            training_reason = str(value.get("training_target_reason") or "").strip()
            if training_status not in {"match", "mismatch", "unknown"}:
                raise ExamQualityError("REVIEW_SCHEMA", "试卷审核训练目标状态无效")
            if len(training_reason) > 200 or (training_status == "mismatch" and not training_reason):
                raise ExamQualityError("REVIEW_SCHEMA", "试卷审核训练目标依据无效")

        deterministic = set(local_issues.get(expected_position, ()))
        expected_slot_match = "section_slot_mismatch" not in deterministic
        expected_adaptation_valid = "adaptation_scope_invalid" not in deterministic
        expected_answer_leak = "answer_leak" in deterministic
        if (
            value["section_slot_match"] is not expected_slot_match
            or value["adaptation_scope_valid"] is not expected_adaptation_valid
            or value["answer_leak"] is not expected_answer_leak
        ):
            raise ExamQualityError(
                "REVIEW_SCHEMA", "模型审核未保持本地确定性检查结果"
            )

        all_issues = deterministic | set(raw_issues)
        # The explicit status, not a speculative issue label, owns this check.
        all_issues.discard("training_target_mismatch")
        if training_status == "mismatch":
            all_issues.add("training_target_mismatch")
        if not value["reference_grounded"]:
            all_issues.add("reference_ungrounded")
        if duplicate_risk >= 0.75:
            all_issues.add("near_duplicate")
        target = items[expected_position - 1]
        target_chapter = str(
            target.get("target_chapter") or target.get("chapter") or ""
        ).strip()
        target_type = str(
            target.get("target_question_type") or target.get("type")
            or target.get("question_type") or ""
        ).strip()
        target_difficulty = str(
            target.get("target_difficulty") or target.get("difficulty") or ""
        ).strip()
        if target_chapter and actual_chapter != target_chapter:
            all_issues.add("chapter_mismatch")
        if target_type and actual_type != target_type:
            all_issues.add("question_type_mismatch")
        if target_difficulty and actual_difficulty != target_difficulty:
            all_issues.add("difficulty_mismatch")
        if all_issues:
            failed.add(expected_position)
        validated_item = {
            "position": expected_position,
            "actual_chapter": actual_chapter,
            "actual_question_type": actual_type,
            "actual_difficulty": actual_difficulty,
            "section_slot_match": expected_slot_match,
            "reference_grounded": (
                bool(value["reference_grounded"])
                and "reference_ungrounded" not in deterministic
            ),
            "duplicate_group": duplicate_group,
            "duplicate_risk": duplicate_risk,
            "adaptation_scope_valid": expected_adaptation_valid,
            "answer_leak": expected_answer_leak,
            "issues": sorted(all_issues),
            "confidence": confidence,
        }
        if training_target:
            validated_item.update(
                training_target_status=training_status,
                training_target_reason=training_reason,
            )
        validated.append(validated_item)
    positions = tuple(sorted(failed))
    return ExamQualityReview(
        tuple(validated), not positions, positions, bool(same_model_review),
        TRAINING_REVIEW_PROMPT_VERSION if training_target else REVIEW_PROMPT_VERSION,
        TRAINING_REVIEW_SCHEMA_VERSION if training_target else REVIEW_SCHEMA_VERSION,
    )


def review_exam_items(
    items: Sequence[Mapping[str, Any]],
    *,
    subject: str,
    allowed_chapters: Sequence[str],
    allowed_question_types: Sequence[str],
    candidate_ids: Sequence[str],
    ai_call: Callable[..., str],
    same_model_review: bool,
    prior_question_texts: Sequence[str] = (),
    expected_count: int | None = None,
    section_slot_contracts: Mapping[int, Mapping[str, Any]] | None = None,
    max_adaptations: int | None = None,
    reference_evidence: Mapping[str, object],
    training_target: str | None = None,
) -> ExamQualityReview:
    """Call only the review policy; invalid output retries review once."""

    frozen_count = len(items) if expected_count is None else expected_count
    deterministic = deterministic_exam_issues(
        items,
        expected_count=frozen_count,
        candidate_ids=candidate_ids,
        allowed_question_types=allowed_question_types,
        allowed_chapters=allowed_chapters,
        prior_question_texts=prior_question_texts,
        section_slot_contracts=section_slot_contracts,
        max_adaptations=max_adaptations,
    )
    safe_items = []
    known_ids = _string_set(candidate_ids)
    for index, item in enumerate(items, start=1):
        position = _strict_position(item.get("position"), expected=index)
        candidate_id = str(item.get("candidate_id") or "").strip()
        raw_reference_ids = item.get("reference_ids")
        reference_ids = (
            list(raw_reference_ids)
            if isinstance(raw_reference_ids, list)
            else []
        )
        if not reference_ids and candidate_id:
            reference_ids = [candidate_id]
        if (
            not reference_ids
            or any(
                not isinstance(reference_id, str)
                or not reference_id.strip()
                or reference_id not in known_ids
                for reference_id in reference_ids
            )
        ):
            raise ExamQualityError(
                "REFERENCE_ID",
                "题目引用不属于冻结候选",
                failed_positions=(position,),
            )
        reference_contexts = []
        for reference_id in reference_ids:
            raw_reference = reference_evidence.get(str(reference_id))
            if isinstance(raw_reference, Mapping):
                context = {
                    key: raw_reference.get(key)
                    for key in REFERENCE_REVIEW_FIELDS
                    if raw_reference.get(key) not in (None, "", [], {})
                }
                context["candidate_id"] = str(reference_id)
                if not context.get("content") and raw_reference.get("latex"):
                    context["content"] = str(raw_reference["latex"])
            elif isinstance(raw_reference, str) and raw_reference.strip():
                context = {
                    "candidate_id": str(reference_id),
                    "content": raw_reference,
                }
            else:
                raise ExamQualityError(
                    "REFERENCE_EVIDENCE",
                    "冻结候选正文缺失",
                    failed_positions=(position,),
                )
            if not isinstance(context.get("content"), str) or not context["content"].strip():
                raise ExamQualityError(
                    "REFERENCE_EVIDENCE",
                    "冻结候选正文缺失",
                    failed_positions=(position,),
                )
            reference_contexts.append(context)
        safe_items.append({
            "position": position,
            "content": _content(item),
            "reference_ids": reference_ids,
            "reference_contexts": reference_contexts,
            "target_chapter": str(
                item.get("target_chapter") or item.get("chapter") or ""
            ),
            "target_question_type": str(
                item.get("target_question_type") or item.get("type")
                or item.get("question_type") or ""
            ),
            "local_issues": list(deterministic.get(position, ())),
        })
    base_messages = build_exam_quality_review_messages(
        subject=subject,
        allowed_chapters=allowed_chapters,
        allowed_question_types=allowed_question_types,
        items=safe_items,
        prompt_version=(TRAINING_REVIEW_PROMPT_VERSION if training_target else REVIEW_PROMPT_VERSION),
        training_target=training_target,
    )
    last_error: ExamQualityError | None = None
    last_schema_code = "REVIEW_SCHEMA"
    last_schema_reason = "试卷审核输出未通过本地 Schema"
    for attempt in range(REVIEW_MAX_ATTEMPTS):
        messages = list(base_messages)
        if attempt:
            messages.append({
                "role": "user",
                "content": (
                    "上次审核输出未通过本地 Schema。仅重新执行审核，"
                    "不要重新生成或改写题目；仍只返回规定 JSON。"
                    f"本地校验原因：{last_schema_reason}。"
                ),
            })
        raw = ai_call(
            messages,
            temperature=0.0,
            task="exam_quality_review",
            task_policy="exam_quality_review",
        )
        try:
            return validate_review_payload(
                raw,
                items=items,
                allowed_chapters=allowed_chapters,
                allowed_question_types=allowed_question_types,
                deterministic_issues=deterministic,
                same_model_review=same_model_review,
                training_target=training_target,
            )
        except ExamQualityError as exc:
            last_error = exc
            last_schema_code, last_schema_reason = (
                _safe_review_validation_failure(exc)
            )
        except (TypeError, ValueError):
            last_error = ExamQualityError(
                "REVIEW_SCHEMA", "试卷审核输出未通过本地 Schema"
            )
            last_schema_code, last_schema_reason = (
                "REVIEW_SCHEMA", "试卷审核输出未通过本地 Schema"
            )
    raise ExamQualityError(
        "REVIEW_INVALID_AFTER_RETRY",
        "试卷审核连续两次未通过本地 Schema",
        schema_error_code=last_schema_code,
        schema_error_reason=last_schema_reason,
    ) from last_error


def build_targeted_repair_plan(
    items: Sequence[Mapping[str, Any]],
    review: ExamQualityReview,
) -> tuple[dict[str, Any], ...]:
    """Return only failed positions and their frozen, non-solution contract."""

    by_position: dict[int, Mapping[str, Any]] = {}
    for expected, item in enumerate(items, start=1):
        if not isinstance(item, Mapping):
            raise ExamQualityError("ITEM_SCHEMA", "试卷题目项必须是对象")
        position = _strict_position(item.get("position"), expected=expected)
        by_position[position] = item
    review_by_position = {int(item["position"]): item for item in review.items}
    if set(review.failed_positions) - set(by_position):
        raise ExamQualityError("REPAIR_PLAN", "失败题位不属于当前冻结试卷")
    plan = []
    for position in review.failed_positions:
        item = by_position[position]
        reviewed = review_by_position.get(position) or {}
        plan.append({
            "position": position,
            "issues": tuple(reviewed.get("issues") or ()),
            "content": _content(item),
            "candidate_id": str(item.get("candidate_id") or ""),
            "reference_ids": tuple(item.get("reference_ids") or ()),
            "target_chapter": str(
                item.get("target_chapter") or item.get("chapter") or ""
            ),
            "target_question_type": str(
                item.get("target_question_type") or item.get("type")
                or item.get("question_type") or ""
            ),
            "target_difficulty": str(
                item.get("target_difficulty") or item.get("difficulty") or ""
            ),
            "max_rewrite_attempts": 1,
            "max_rereview_attempts": 1,
            "training_target_status": reviewed.get("training_target_status"),
            "training_target_reason": reviewed.get("training_target_reason"),
        })
    return tuple(plan)


def apply_targeted_item_repairs(
    items: Sequence[Mapping[str, Any]],
    repairs: Mapping[int, str],
    *,
    allowed_positions: Iterable[int],
) -> tuple[dict[str, Any], ...]:
    """Apply body-only repairs while preserving every frozen item field."""

    allowed = {int(position) for position in allowed_positions}
    normalized_repairs: dict[int, str] = {}
    for raw_position, raw_content in repairs.items():
        position = int(raw_position)
        if position not in allowed:
            raise ExamQualityError("REPAIR_SCOPE", "局部修复包含未失败题位")
        repaired = normalize_generated_latex(raw_content).strip()
        validate_generated_latex(repaired)
        if _ANSWER_LEAK.search(repaired):
            raise ExamQualityError(
                "ANSWER_LEAK", "局部修复返回答案性文本",
                failed_positions=(position,),
            )
        normalized_repairs[position] = repaired
    result = []
    seen = set()
    for expected, item in enumerate(items, start=1):
        position = _strict_position(item.get("position"), expected=expected)
        seen.add(position)
        copied = dict(item)
        if position in normalized_repairs:
            field = "content" if "content" in copied or "latex" not in copied else "latex"
            copied[field] = normalized_repairs[position]
        result.append(copied)
    if set(normalized_repairs) - seen:
        raise ExamQualityError("REPAIR_SCOPE", "局部修复题位不存在")
    return tuple(result)


def request_targeted_item_repair(
    item: Mapping[str, Any],
    issues: Sequence[str],
    evidence: Mapping[str, object] | Sequence[object] | None,
    *,
    ai_call: Callable[..., str],
) -> str:
    """Request exactly one repair attempt for exactly one failed position."""

    if not isinstance(item, Mapping):
        raise ExamQualityError("ITEM_SCHEMA", "局部修复题目必须是对象")
    _strict_position(item.get("position"))
    controlled_issues = tuple(str(issue) for issue in issues)
    if (
        not controlled_issues
        or len(controlled_issues) != len(set(controlled_issues))
        or any(issue not in ALLOWED_ISSUES for issue in controlled_issues)
    ):
        raise ExamQualityError("REPAIR_SCHEMA", "局部修复理由码无效")
    messages = build_targeted_item_repair_messages(
        item=item,
        issues=controlled_issues,
        evidence=evidence,
    )
    raw = ai_call(
        messages,
        temperature=0.2,
        task="exam_generation",
        task_policy="exam_item_repair",
    )
    repaired = normalize_generated_latex(raw).strip()
    if not repaired or _REPAIR_WRAPPER.search(repaired):
        raise ExamQualityError(
            "REPAIR_SCHEMA", "局部修复必须只返回单题题干正文"
        )
    validate_generated_latex(repaired)
    if _ANSWER_LEAK.search(repaired):
        raise ExamQualityError(
            "ANSWER_LEAK", "局部修复返回答案性文本",
            failed_positions=(int(item["position"]),),
        )
    return repaired


def replace_exam_item_bodies(
    body: str,
    repairs: Mapping[int, str],
    *,
    past_section_break_positions: Iterable[int] = (),
) -> str:
    """Replace only numbered bodies in an already generated TeX fragment."""

    replacements = {
        int(position): normalize_generated_latex(value).strip()
        for position, value in repairs.items()
    }
    for position, value in replacements.items():
        if position < 1:
            raise ExamQualityError("REPAIR_SCOPE", "局部修复题位无效")
        validate_generated_latex(value)
        if _ANSWER_LEAK.search(value):
            raise ExamQualityError("ANSWER_LEAK", "局部修复返回答案性文本")
    seen: set[int] = set()
    section_break_positions = set(past_section_break_positions)
    problem_match = _PROBLEM.search(body)
    past_match = _PAST_HEADING.search(body)
    if problem_match and (
        past_match is None or problem_match.start() < past_match.start()
    ):
        position = 0

        def replace_problem(match: re.Match[str]) -> str:
            nonlocal position
            position += 1
            seen.add(position)
            if position not in replacements:
                return match.group(0)
            return (
                match.group(1)
                + replacements[position]
                + match.group(3)
            )

        rendered = _PROBLEM.sub(replace_problem, body)
    else:

        def replace_past(match: re.Match[str]) -> str:
            position = int(match.group(2))
            seen.add(position)
            if position not in replacements:
                return match.group(0)
            value = replacements[position]
            if _PAST_ADAPTATION_MARKER.search(value):
                raise ExamQualityError(
                    "REPAIR_SCHEMA", "局部修复不得包含改编标记"
                )

            original = match.group(3)
            section_suffix = ""
            if position in section_break_positions:
                section_match = _PAST_SECTION_SUFFIX.search(original)
                if section_match:
                    section_suffix = section_match.group(0)
                    original = original[:section_match.start()]

            content_start = len(original) - len(original.lstrip())
            prefix = original[:content_start]
            content = original[content_start:]
            marker = _PAST_ADAPTATION_MARKER.match(content)
            if marker:
                prefix += marker.group(0)
                content = content[marker.end():]
                marker_spacing = len(content) - len(content.lstrip())
                prefix += content[:marker_spacing]
                content = content[marker_spacing:]
            content_end = len(content.rstrip())
            suffix = content[content_end:] + section_suffix
            return match.group(1) + prefix + value + suffix

        rendered = _PAST_HEADING.sub(replace_past, body)
    if set(replacements) - seen:
        raise ExamQualityError("REPAIR_SCOPE", "局部修复题位不存在")
    validate_generated_latex(rendered)
    return rendered


def largest_remainder_difficulty_quota(
    cards: Sequence[Mapping[str, Any]],
    question_count: int,
    *,
    minimum_coverage: float = 0.6,
) -> tuple[dict[str, int] | None, dict[str, Any]]:
    """Compatibility helper currently consumed by the coordinator D draft."""

    valid = [
        card for card in cards
        if str(card.get("difficulty") or "") in {"简单", "中等", "困难"}
        and float(card.get("analysis_confidence") or 0.0) >= 0.6
    ]
    coverage = len(valid) / max(1, len(cards))
    if not cards or coverage < minimum_coverage:
        return None, {
            "coverage": round(coverage, 4),
            "fallback_reason": "insufficient_high_confidence_cards",
        }
    counts = Counter(str(card["difficulty"]) for card in valid)
    exact = {
        key: question_count * counts.get(key, 0) / len(valid)
        for key in ("简单", "中等", "困难")
    }
    quota = {key: int(value) for key, value in exact.items()}
    remainder = question_count - sum(quota.values())
    for key in sorted(
        exact, key=lambda name: (-(exact[name] - quota[name]), name)
    )[:remainder]:
        quota[key] += 1
    return quota, {
        "coverage": round(coverage, 4),
        "evidence_count": len(valid),
        "fallback_reason": None,
    }


__all__ = [
    "ALLOWED_ISSUES", "ExamQualityError", "ExamQualityReview",
    "apply_targeted_item_repairs", "build_targeted_repair_plan",
    "deterministic_exam_issues", "extract_review_items",
    "largest_remainder_difficulty_quota",
    "near_duplicate_ratio", "normalized_question_hash",
    "normalized_question_text", "replace_exam_item_bodies",
    "request_targeted_item_repair", "review_exam_items",
    "validate_review_payload",
]
