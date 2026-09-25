"""Pure Stage 7 quality review and controlled single-item adaptation.

No database, worker, provider, or legacy checkpoint is used here.  Provider
calls are injected callbacks so the complete protocol can be tested offline.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage4_candidate_facts import (
    CANDIDATE_FACTS_CONTRACT_VERSION,
    FrozenCandidateFacts,
)
from .exam_generation_stage4_difficulty import (
    DIFFICULTY_RESOLUTION_CONTRACT_VERSION,
    DifficultyResolutionResult,
    EffectiveDifficultyOutcome,
)
from .exam_generation_stage5_candidate_selection import (
    CANDIDATE_SELECTION_VERSION,
    CandidateStage5Selection,
)
from .exam_generation_stage5_solver import SELECTION_VERSION, Stage5Selection
from .semantic_index import SemanticIndexContractError, semantic_content_hash


STAGE7_VERSION = "past-v3-stage7-quality-adaptation-v1"
LEGACY_PRE_RECEIPT_VERSION = "past-v3-stage7-pre-adaptation-receipt-v1"
PRE_RECEIPT_VERSION = "past-v3-stage7-pre-adaptation-receipt-v2"
LEGACY_QUALITY_PROTOCOL_VERSION = "past-v3-stage7-quality-protocol-v1"
QUALITY_PROTOCOL_VERSION = "past-v3-stage7-quality-protocol-v2"
QUALITY_PROMPT_VERSION = "past-v3-stage7-formal-original-quality-prompt-v1"
FORMAL_ORIGINAL_QUALITY_POLICY_VERSION = (
    "past-v3-stage7-formal-original-quality-policy-v1"
)
ADAPTATION_PROTOCOL_VERSION = "past-v3-stage7-adaptation-protocol-v1"
VERIFICATION_PROTOCOL_VERSION = "past-v3-stage7-hidden-verification-protocol-v1"
LEGACY_FINAL_RECEIPT_VERSION = "past-v3-stage7-final-receipt-v1"
FINAL_RECEIPT_VERSION = "past-v3-stage7-final-receipt-v2"
LOCAL_EVIDENCE_VERSION = "past-v3-stage7-local-answer-evidence-v1"
GLOBAL_CONSTRAINT_EVIDENCE_VERSION = "past-v3-stage7-global-constraint-evidence-v1"
DECLARATION = (
    "改编前最终候选卷严格满足 exact-slot 难度结构；若触发最终单题改编，"
    "则该题为难度结构审查后的受控例外，不再重新进行难度判定。"
)

_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_LEGACY_QUALITY_CODES = frozenset(
    {"none", "wording_clarity", "pedagogical_quality", "rendering_risk"}
)
_QUALITY_CODES = frozenset({"none", "rendering_risk"})
_VERIFY_CODES = frozenset({"none", "not_solvable", "meaning_incomplete", "result_invalid", "not_publishable"})
_SNAPSHOT_KEYS = frozenset({"schema_version", "content", "figure_references", "options", "conditions"})
_DIFFICULTIES = frozenset({"简单", "中等", "困难"})
class Stage7ContractError(RuntimeError):
    def __init__(self, code: str, reason: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.reason = reason
        self.status_code = status_code
        self.retryable = False


class Stage7Cancellation(RuntimeError):
    pass


class Stage7LeaseLost(RuntimeError):
    pass


class Stage7DeadlineExceeded(RuntimeError):
    pass


def formal_original_quality_policy() -> dict[str, Any]:
    """Return the immutable semantic boundary for formal source questions."""

    return {
        "version": FORMAL_ORIGINAL_QUALITY_POLICY_VERSION,
        "prompt_version": QUALITY_PROMPT_VERSION,
        "source_trust": "formal_original_statement_trusted",
        "trusted_facts": ["statement_expression_correct", "solvable"],
        "hard_veto_checks": ["objective_rendering_failure"],
        "non_review_dimensions": [
            "language_style",
            "pedagogical_value",
            "difficulty",
            "solvability",
        ],
    }


@dataclass(frozen=True, slots=True)
class Stage7Budget:
    mode: str = "observe"
    structural_calls_remaining: int = 3
    remaining_seconds: float | None = None
    formal_adaptation_min_remaining_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class Stage7Result:
    pre_adaptation_hash: str
    final_exam_hash: str
    adapted_position: int | None
    single_item_fallback: bool
    final_items: tuple[Mapping[str, Any], ...]
    pre_adaptation_receipt: Mapping[str, Any]
    final_receipt: Mapping[str, Any]


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise Stage7ContractError("exam_stage7_contract_invalid", "non_canonical_json", "阶段 7 数据不是规范 JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _strict_json(raw: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(raw, str) or not raw or len(raw) > 2_000_000:
        raise Stage7ContractError("exam_stage7_schema_invalid", f"{field}_not_json_text", "模型响应必须是有界 JSON 文本", status_code=422)

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise Stage7ContractError("exam_stage7_schema_invalid", f"{field}_duplicate_key", "模型响应包含重复 JSON 键", status_code=422)
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except Stage7ContractError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise Stage7ContractError("exam_stage7_schema_invalid", f"{field}_invalid_json", "模型响应不是严格 JSON", status_code=422) from exc
    if not isinstance(value, Mapping):
        raise Stage7ContractError("exam_stage7_schema_invalid", f"{field}_not_object", "模型响应必须是对象", status_code=422)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str] | frozenset[str], reason: str) -> None:
    if set(value) != set(expected):
        raise Stage7ContractError("exam_stage7_schema_invalid", reason, "模型响应字段不符合阶段 7 白名单", status_code=422)


def _text(value: Any, field: str, maximum: int = 500_000) -> str:
    if not isinstance(value, str):
        raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_invalid", "阶段 7 文本字段无效")
    result = unicodedata.normalize("NFC", value).strip()
    if not result or len(result) > maximum:
        raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_invalid", "阶段 7 文本字段无效")
    return result


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_invalid", "阶段 7 哈希字段无效")
    return value


def _snapshot(raw: Any, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_invalid_json", "冻结题面快照无效") from exc
    if not isinstance(value, Mapping) or set(value) != _SNAPSHOT_KEYS or value.get("schema_version") != "semantic-candidate-content-snapshot-v1":
        raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_shape_invalid", "冻结题面快照结构无效")
    result = {"schema_version": value["schema_version"], "content": _text(value.get("content"), f"{field}.content")}
    for key in ("figure_references", "options", "conditions"):
        seq = value.get(key)
        if isinstance(seq, (str, bytes)) or not isinstance(seq, Sequence) or len(seq) > 500:
            raise Stage7ContractError("exam_stage7_contract_invalid", f"{field}_{key}_invalid", "冻结题面结构列表无效")
        result[key] = [_text(item, f"{field}.{key}", 20_000) for item in seq]
    return result


def _verify_selection(
    selection: Stage5Selection | CandidateStage5Selection,
) -> dict[str, Any]:
    if isinstance(selection, CandidateStage5Selection):
        if selection.contract_version != CANDIDATE_SELECTION_VERSION:
            raise Stage7ContractError("exam_stage7_contract_invalid", "selection_type_invalid", "阶段 5 candidate 选择合同无效")
        payload = selection.to_dict()
        keys = (
            "contract_version", "solver_version", "stage3_contract_hash",
            "candidate_facts_hash", "projection_identity_hash", "profile_hash",
            "exact_slot_hash", "graph_hash", "constraint_hash",
            "rubric_set_receipt_hash", "utility_receipt_hash",
            "parameter_set_hash", "policy_hash", "rejection_ledger_head_hash",
            "assignments",
        )
    elif isinstance(selection, Stage5Selection):
        if selection.contract_version != SELECTION_VERSION:
            raise Stage7ContractError("exam_stage7_contract_invalid", "selection_type_invalid", "阶段 5 选择合同无效")
        payload = selection.to_dict()
        keys = (
            "contract_version", "solver_version", "stage3_contract_hash", "candidate_facts_hash",
            "projection_identity_hash", "profile_hash", "exact_slot_hash", "graph_hash",
            "constraint_hash", "score_identity_hash", "assignments",
        )
    else:
        raise Stage7ContractError("exam_stage7_contract_invalid", "selection_type_invalid", "阶段 5 选择合同无效")
    try:
        identity = {key: payload[key] for key in keys}
    except KeyError as exc:
        raise Stage7ContractError("exam_stage7_contract_invalid", "selection_shape_invalid", "阶段 5 选择字段缺失") from exc
    if (
        payload.get("selection_hash") != _digest(identity)
        or payload.get("selection_hash") != selection.selection_hash
        or payload.get("total_utility") != selection.total_utility
        or tuple(payload.get("assignments", ())) != selection.assignments
    ):
        raise Stage7ContractError("exam_stage7_hash_mismatch", "selection_hash_mismatch", "阶段 5 选择哈希不匹配")
    return payload


def _verify_facts(facts: FrozenCandidateFacts, expected_hash: str) -> dict[str, Any]:
    if not isinstance(facts, FrozenCandidateFacts) or facts.contract_version != CANDIDATE_FACTS_CONTRACT_VERSION:
        raise Stage7ContractError("exam_stage7_contract_invalid", "candidate_facts_type_invalid", "阶段 4 冻结候选合同无效")
    payload = facts.to_dict()
    actual = _digest(payload)
    if actual != facts.candidate_facts_hash or actual != _hash(expected_hash, "candidate_facts_hash"):
        raise Stage7ContractError("exam_stage7_hash_mismatch", "candidate_facts_hash_mismatch", "阶段 4 冻结候选哈希不匹配")
    if tuple(payload.get("candidates", ())) != facts.candidates:
        raise Stage7ContractError("exam_stage7_hash_mismatch", "candidate_facts_payload_mismatch", "阶段 4 冻结候选载荷漂移")
    return payload


def _verify_difficulty_results(
    results: Sequence[DifficultyResolutionResult],
    *,
    candidate_facts_hash: str,
    known_candidate_ids: set[str],
) -> tuple[dict[str, tuple[str, str]], tuple[dict[str, str], ...]]:
    """Verify complete immutable v42 results, never an outcomes-only summary."""

    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence) or len(results) > 10_000:
        raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_results_invalid", "阶段 4 难度解析结果集合无效")
    payload_fields = {
        "contract_version", "candidate_facts_hash", "component_hash", "input_facts_hash",
        "classification_binding", "verification_binding", "classification_response_hash",
        "verification_response_hash", "outcomes",
    }
    outcome_fields = {
        "candidate_id", "status", "effective_difficulty", "reason",
        "classification_confidence", "verification_confidence", "evidence_hash",
    }
    resolved: dict[str, tuple[str, str]] = {}
    identities: list[dict[str, str]] = []
    seen_results: set[str] = set()
    seen_candidates: set[str] = set()
    for result in results:
        if not isinstance(result, DifficultyResolutionResult):
            raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_result_type_invalid", "阶段 4 难度解析结果必须是完整合同")
        try:
            payload = result.to_dict()
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_result_payload_invalid", "阶段 4 难度解析载荷无效") from exc
        if not isinstance(payload, Mapping) or set(payload) != payload_fields:
            raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_result_shape_invalid", "阶段 4 难度解析载荷字段无效")
        result_hash = _hash(result.result_hash, "difficulty_result_hash")
        component_hash = _hash(result.component_hash, "difficulty_component_hash")
        input_facts_hash = _hash(result.input_facts_hash, "difficulty_input_facts_hash")
        if result_hash in seen_results:
            raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_result_duplicate", "阶段 4 难度解析结果重复")
        seen_results.add(result_hash)
        if (
            result.contract_version != DIFFICULTY_RESOLUTION_CONTRACT_VERSION
            or result.candidate_facts_hash != candidate_facts_hash
            or payload.get("contract_version") != result.contract_version
            or payload.get("candidate_facts_hash") != result.candidate_facts_hash
            or payload.get("component_hash") != component_hash
            or payload.get("input_facts_hash") != input_facts_hash
            or result._payload_json != _canonical(payload)
            or _digest(payload) != result_hash
        ):
            raise Stage7ContractError("exam_stage7_hash_mismatch", "difficulty_result_binding_mismatch", "阶段 4 难度解析结果未绑定当前候选事实或规范载荷")
        bindings: list[Mapping[str, Any]] = []
        for round_name in ("classification", "verification"):
            binding = payload.get(f"{round_name}_binding")
            if not isinstance(binding, Mapping) or set(binding) != {"model", "prompt_version", "rules_hash"}:
                raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_binding_shape_invalid", "阶段 4 难度解析轮次绑定无效")
            _text(binding.get("model"), f"{round_name}_model", 256)
            _text(binding.get("prompt_version"), f"{round_name}_prompt_version", 128)
            _hash(binding.get("rules_hash"), f"{round_name}_rules_hash")
            bindings.append(binding)
        if bindings[0]["rules_hash"] != bindings[1]["rules_hash"] or bindings[0]["prompt_version"] == bindings[1]["prompt_version"]:
            raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_round_binding_invalid", "阶段 4 难度解析双轮绑定无效")
        for field in ("classification_response_hash", "verification_response_hash"):
            if payload.get(field) is not None:
                _hash(payload[field], field)
        outcomes = payload.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes or tuple(outcomes) != tuple(item.to_dict() for item in result.outcomes):
            raise Stage7ContractError("exam_stage7_hash_mismatch", "difficulty_outcomes_payload_mismatch", "阶段 4 难度解析条目与完整载荷不一致")
        for outcome, raw in zip(result.outcomes, outcomes):
            if not isinstance(outcome, EffectiveDifficultyOutcome) or not isinstance(raw, Mapping) or set(raw) != outcome_fields:
                raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_outcome_type_invalid", "阶段 4 难度解析条目类型无效")
            candidate_id = _text(raw.get("candidate_id"), "difficulty_candidate_id")
            if candidate_id not in known_candidate_ids:
                raise Stage7ContractError("exam_stage7_hash_mismatch", "difficulty_outcome_candidate_mismatch", "阶段 4 难度解析条目跨越候选事实集合")
            if candidate_id in seen_candidates:
                raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_outcome_duplicate", "阶段 4 难度解析候选重复")
            seen_candidates.add(candidate_id)
            status, label = raw.get("status"), raw.get("effective_difficulty")
            confidences = (raw.get("classification_confidence"), raw.get("verification_confidence"))
            if (
                status not in {"resolved", "excluded", "conflict"}
                or (status == "resolved" and label not in _DIFFICULTIES)
                or (status != "resolved" and label is not None)
                or any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1) for value in confidences)
            ):
                raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_outcome_invalid", "阶段 4 难度解析条目值无效")
            _text(raw.get("reason"), "difficulty_outcome_reason", 128)
            _hash(raw.get("evidence_hash"), "difficulty_evidence_hash")
            if status == "resolved":
                resolved[candidate_id] = (label, result_hash)
        identities.append({"result_hash": result_hash, "component_hash": component_hash, "input_facts_hash": input_facts_hash})
    if identities != sorted(identities, key=lambda item: item["result_hash"]):
        raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_results_not_canonical", "阶段 4 难度解析结果顺序不规范")
    return resolved, tuple(identities)


def _verify_local_evidence(evidence: Mapping[str, Any], selection_hash: str, candidate_ids: set[str]) -> dict[str, bool]:
    if not isinstance(evidence, Mapping):
        raise Stage7ContractError("exam_stage7_contract_invalid", "local_evidence_invalid", "本地答案存在性证据无效")
    body = dict(evidence)
    evidence_hash = body.pop("evidence_hash", None)
    if set(body) != {"version", "selection_hash", "answer_exists"} or body.get("version") != LOCAL_EVIDENCE_VERSION or body.get("selection_hash") != selection_hash:
        raise Stage7ContractError("exam_stage7_contract_invalid", "local_evidence_shape_invalid", "本地答案存在性证据未绑定选择")
    answers = body.get("answer_exists")
    if not isinstance(answers, Mapping) or set(answers) != candidate_ids or any(type(value) is not bool for value in answers.values()):
        raise Stage7ContractError("exam_stage7_contract_invalid", "local_answer_evidence_invalid", "答案存在性必须逐候选显式给出")
    if _digest(body) != _hash(evidence_hash, "local_evidence_hash"):
        raise Stage7ContractError("exam_stage7_hash_mismatch", "local_evidence_hash_mismatch", "本地答案存在性证据哈希不匹配")
    return dict(answers)


def _is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _latex_delimiters_invalid(text: str) -> bool:
    math_delimiters = 0
    brace_depth = 0
    for index, character in enumerate(text):
        if character not in "${}" or _is_escaped(text, index):
            continue
        if character == "$":
            math_delimiters += 1
        elif character == "{":
            brace_depth += 1
        else:
            brace_depth -= 1
            if brace_depth < 0:
                return True
    return math_delimiters % 2 == 1 or brace_depth != 0


def _basic_content_codes(snapshot: Mapping[str, Any], answer_exists: bool | None = None) -> list[str]:
    joined = "\n".join([snapshot["content"], *snapshot["options"], *snapshot["conditions"]])
    codes: list[str] = []
    if answer_exists is False:
        codes.append("answer_missing")
    if len(snapshot["content"]) < 4 or snapshot["content"].endswith(("，", ",", ":", "：", ";", "；")):
        codes.append("meaning_incomplete")
    if re.search(r"(?:参考)?答案\s*[:：]|(?:标准)?解析\s*[:：]", joined, flags=re.IGNORECASE):
        codes.append("answer_leakage")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in joined):
        codes.append("unsafe_character")
    if re.search(r"<\s*(?:script|iframe|object|embed)\b|javascript\s*:", joined, flags=re.IGNORECASE):
        codes.append("unsafe_markup")
    if _latex_delimiters_invalid(joined):
        codes.append("latex_or_render_invalid")
    return codes


def _paper_hash(items: Sequence[Mapping[str, Any]]) -> str:
    return _digest({"version": STAGE7_VERSION, "items": list(items)})


def _call(callback: Callable[[str], str], request: Mapping[str, Any]) -> str:
    return callback(_canonical(request))


def _is_propagated(exc: BaseException) -> bool:
    return isinstance(
        exc,
        (
            Stage7Cancellation,
            Stage7LeaseLost,
            Stage7DeadlineExceeded,
            asyncio.CancelledError,
            KeyboardInterrupt,
            SystemExit,
        ),
    )


def _quality_response(
    raw: str,
    selection_hash: str,
    pre_hash: str,
    positions: tuple[int, ...],
    protocol_version: str,
    allowed_reason_codes: frozenset[str],
) -> dict[int, tuple[bool, tuple[str, ...]]]:
    value = _strict_json(raw, "quality_response")
    _exact_keys(value, {"version", "selection_hash", "pre_adaptation_hash", "results"}, "quality_response_fields_invalid")
    if value.get("version") != protocol_version or value.get("selection_hash") != selection_hash or value.get("pre_adaptation_hash") != pre_hash:
        raise Stage7ContractError("exam_stage7_schema_invalid", "quality_response_binding_invalid", "质量响应绑定无效", status_code=422)
    results = value.get("results")
    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence) or len(results) != len(positions):
        raise Stage7ContractError("exam_stage7_schema_invalid", "quality_results_count_invalid", "质量响应必须精确覆盖所有位置", status_code=422)
    parsed: dict[int, tuple[bool, tuple[str, ...]]] = {}
    for item in results:
        if not isinstance(item, Mapping):
            raise Stage7ContractError("exam_stage7_schema_invalid", "quality_result_invalid", "质量结果无效", status_code=422)
        _exact_keys(item, {"position", "passed", "reason_codes"}, "quality_result_fields_invalid")
        position, passed, codes = item.get("position"), item.get("passed"), item.get("reason_codes")
        if type(position) is not int or type(passed) is not bool or isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence):
            raise Stage7ContractError("exam_stage7_schema_invalid", "quality_result_types_invalid", "质量结果类型无效", status_code=422)
        code_tuple = tuple(codes)
        if not code_tuple or len(set(code_tuple)) != len(code_tuple) or any(code not in allowed_reason_codes for code in code_tuple):
            raise Stage7ContractError("exam_stage7_schema_invalid", "quality_reason_codes_invalid", "质量原因码无效", status_code=422)
        if (passed and code_tuple != ("none",)) or (not passed and "none" in code_tuple):
            raise Stage7ContractError("exam_stage7_schema_invalid", "quality_pass_codes_inconsistent", "质量布尔值与原因码矛盾", status_code=422)
        if position in parsed:
            raise Stage7ContractError("exam_stage7_schema_invalid", "quality_position_duplicate", "质量响应位置重复", status_code=422)
        parsed[position] = (passed, code_tuple)
    if set(parsed) != set(positions):
        raise Stage7ContractError("exam_stage7_schema_invalid", "quality_positions_invalid", "质量响应位置集合无效", status_code=422)
    return parsed


def run_stage7_quality_and_adaptation(
    *,
    selection: Stage5Selection | CandidateStage5Selection,
    candidate_facts: FrozenCandidateFacts,
    difficulty_resolution_results: Sequence[DifficultyResolutionResult],
    global_constraint_evidence: Mapping[str, Any],
    quality_callback: Callable[[str], str],
    local_answer_evidence: Mapping[str, Any] | None = None,
    adaptation_callback: Callable[[str], str] | None = None,
    verification_callback: Callable[[str], str] | None = None,
    budget: Stage7Budget = Stage7Budget(),
    quality_request_contract: Mapping[str, Any] | None = None,
) -> Stage7Result:
    """Review a complete selection and optionally adapt exactly one item."""

    if not isinstance(budget, Stage7Budget):
        raise Stage7ContractError("exam_stage7_contract_invalid", "budget_type_invalid", "阶段 7 预算合同无效")
    numeric_budget_fields = (budget.remaining_seconds, budget.formal_adaptation_min_remaining_seconds)
    if (
        budget.mode not in {"observe", "enforce"}
        or type(budget.structural_calls_remaining) is not int
        or budget.structural_calls_remaining < 0
        or any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            )
            for value in numeric_budget_fields
        )
    ):
        raise Stage7ContractError("exam_stage7_contract_invalid", "budget_fields_invalid", "阶段 7 预算字段无效")
    selection_payload = _verify_selection(selection)
    facts_payload = _verify_facts(candidate_facts, selection_payload["candidate_facts_hash"])
    assignments = list(selection_payload["assignments"])
    if not assignments:
        raise Stage7ContractError("exam_stage7_contract_invalid", "empty_selection", "阶段 7 不接受空试卷")
    if sorted(item.get("position") for item in assignments) != list(range(1, len(assignments) + 1)):
        raise Stage7ContractError("exam_stage7_contract_invalid", "selection_positions_invalid", "选择位置必须连续且唯一")
    candidate_ids = {item.get("candidate_id") for item in assignments}
    if None in candidate_ids or len(candidate_ids) != len(assignments):
        raise Stage7ContractError("exam_stage7_contract_invalid", "selection_candidate_ids_invalid", "选择候选身份无效")
    legacy_v1 = local_answer_evidence is not None
    answers = (
        _verify_local_evidence(local_answer_evidence, selection.selection_hash, candidate_ids)
        if legacy_v1 else {}
    )
    quality_protocol_version = LEGACY_QUALITY_PROTOCOL_VERSION if legacy_v1 else QUALITY_PROTOCOL_VERSION
    pre_receipt_version = LEGACY_PRE_RECEIPT_VERSION if legacy_v1 else PRE_RECEIPT_VERSION
    final_receipt_version = LEGACY_FINAL_RECEIPT_VERSION if legacy_v1 else FINAL_RECEIPT_VERSION
    evidence_body = dict(global_constraint_evidence) if isinstance(global_constraint_evidence, Mapping) else {}
    evidence_hash = evidence_body.pop("evidence_hash", None)
    if set(evidence_body) != {"version", "selection_hash", "constraint_hash", "passed"} or evidence_body.get("version") != GLOBAL_CONSTRAINT_EVIDENCE_VERSION or evidence_body.get("selection_hash") != selection.selection_hash or evidence_body.get("constraint_hash") != selection.constraint_hash or evidence_body.get("passed") is not True or _digest(evidence_body) != _hash(evidence_hash, "constraint_evidence_hash"):
        raise Stage7ContractError("exam_stage7_contract_invalid", "global_constraint_evidence_invalid", "全卷硬约束证据无效或未绑定选择")

    facts_by_id: dict[str, Mapping[str, Any]] = {}
    for fact in facts_payload.get("candidates", []):
        cid = fact.get("candidate_id")
        if cid in facts_by_id:
            raise Stage7ContractError("exam_stage7_contract_invalid", "candidate_fact_duplicate", "冻结候选身份重复")
        facts_by_id[cid] = fact
    effective_difficulties, difficulty_result_identities = _verify_difficulty_results(
        difficulty_resolution_results,
        candidate_facts_hash=candidate_facts.candidate_facts_hash,
        known_candidate_ids=set(facts_by_id),
    )
    used_difficulty_results: dict[str, set[str]] = {}
    items: list[dict[str, Any]] = []
    normalized_content: list[tuple[int, str]] = []
    local_issues: list[dict[str, Any]] = []
    for assignment in assignments:
        fact = facts_by_id.get(assignment["candidate_id"])
        if fact is None:
            raise Stage7ContractError("exam_stage7_contract_invalid", "selected_candidate_fact_missing", "选择引用的冻结候选不存在")
        difficulty = fact.get("difficulty_fact")
        expected = {
            "candidate_id": assignment["candidate_id"], "content_hash": assignment.get("content_hash"),
            "source_kind": "past_exam_other" if assignment.get("source_kind") == "past_exam" else assignment.get("source_kind"),
            "question_type_family": assignment.get("question_type_family"),
        }
        if any(fact.get(key) != value for key, value in expected.items()) or not isinstance(difficulty, Mapping):
            raise Stage7ContractError("exam_stage7_hash_mismatch", "selected_fact_or_exact_slot_mismatch", "来源、题型或难度冻结事实与选择不一致")
        if difficulty.get("resolved") is True:
            effective_label = difficulty.get("label")
        elif difficulty.get("resolved") is False:
            resolved = effective_difficulties.get(assignment["candidate_id"])
            if resolved is None:
                raise Stage7ContractError("exam_stage7_contract_invalid", "selected_effective_difficulty_missing", "原始未解析候选缺少完整阶段 4 难度解析证据")
            effective_label, used_result_hash = resolved
            used_difficulty_results.setdefault(used_result_hash, set()).add(assignment["candidate_id"])
        else:
            raise Stage7ContractError("exam_stage7_contract_invalid", "selected_difficulty_fact_invalid", "冻结候选难度事实状态无效")
        if effective_label != assignment.get("target_difficulty"):
            raise Stage7ContractError("exam_stage7_hash_mismatch", "selected_fact_or_exact_slot_mismatch", "来源、题型或有效难度与 exact-slot 不一致")
        snapshot = _snapshot(fact.get("content_snapshot"), f"candidate[{assignment['candidate_id']}]")
        raw_snapshot = fact.get("content_snapshot")
        try:
            replay_content_hash = semantic_content_hash(
                snapshot["content"],
                figure_references=tuple(snapshot["figure_references"]),
                options=tuple(snapshot["options"]),
                conditions=tuple(snapshot["conditions"]),
            )
        except SemanticIndexContractError as exc:
            raise Stage7ContractError(
                "exam_stage7_hash_mismatch",
                "selected_content_snapshot_unreplayable",
                "冻结结构化题面无法重放内容身份",
            ) from exc
        if (
            assignment.get("candidate_fact_hash") != _digest(fact)
            or not isinstance(raw_snapshot, str)
            or fact.get("content_snapshot_hash") != _digest(raw_snapshot)
            or replay_content_hash != assignment.get("content_hash")
        ):
            raise Stage7ContractError(
                "exam_stage7_hash_mismatch",
                "selected_content_or_fact_hash_mismatch",
                "选择引用的候选事实或结构化题面哈希不匹配",
            )
        codes = _basic_content_codes(snapshot, answers.get(assignment["candidate_id"]))
        if codes:
            local_issues.append({"position": assignment["position"], "reason_codes": codes})
        norm = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", snapshot["content"].casefold())
        normalized_content.append((assignment["position"], norm))
        items.append({"position": assignment["position"], "assignment": dict(assignment), "candidate_fact": dict(fact), "content_snapshot": snapshot})
    for left in range(len(items)):
        for right in range(left + 1, len(items)):
            if items[left]["assignment"]["content_hash"] == items[right]["assignment"]["content_hash"]:
                local_issues.append({"positions": [left + 1, right + 1], "reason_codes": ["exact_duplicate"]})
            elif min(len(normalized_content[left][1]), len(normalized_content[right][1])) >= 12 and SequenceMatcher(None, normalized_content[left][1], normalized_content[right][1]).ratio() >= 0.92:
                local_issues.append({"positions": [left + 1, right + 1], "reason_codes": ["near_duplicate"]})
    if local_issues:
        raise Stage7ContractError("exam_stage7_pre_adaptation_rejected", "local_hard_review_failed", _canonical(local_issues), status_code=422)

    all_result_hashes = {identity["result_hash"] for identity in difficulty_result_identities}
    if set(used_difficulty_results) != all_result_hashes:
        raise Stage7ContractError("exam_stage7_contract_invalid", "difficulty_result_unused", "阶段 7 不接受未被原始未解析选择使用的难度解析结果")
    full_difficulty_identity_hash = _digest({
        "candidate_facts_hash": candidate_facts.candidate_facts_hash,
        "results": list(difficulty_result_identities),
    })
    used_difficulty_identity_hash = _digest({
        "selection_hash": selection.selection_hash,
        "results": [
            {"result_hash": identity["result_hash"], "candidate_ids": sorted(used_difficulty_results[identity["result_hash"]])}
            for identity in difficulty_result_identities
        ],
    })

    pre_hash = _paper_hash(items)
    pre_body = {
        "version": pre_receipt_version, "selection_hash": selection.selection_hash,
        "candidate_facts_hash": candidate_facts.candidate_facts_hash,
        "effective_difficulty_results_identity_hash": full_difficulty_identity_hash,
        "used_effective_difficulty_results_identity_hash": used_difficulty_identity_hash,
        "constraint_evidence_hash": evidence_hash,
        "exact_slot_passed": True, "source_fact_consistency_passed": True,
        "global_constraints_passed": True, "duplicate_review_passed": True,
        "content_safety_review_passed": True,
        "pre_adaptation_hash": pre_hash,
    }
    if legacy_v1:
        pre_body["local_answer_evidence_hash"] = local_answer_evidence["evidence_hash"]
        pre_body["answer_presence_review_passed"] = True
    pre_receipt = {**pre_body, "receipt_hash": _digest(pre_body)}
    if budget.structural_calls_remaining < 1:
        raise Stage7ContractError("exam_stage7_resource_insufficient", "quality_call_budget_insufficient", "常规质量审查调用预算不足", status_code=422)

    if quality_request_contract is None:
        quality_codes = _LEGACY_QUALITY_CODES if legacy_v1 else _QUALITY_CODES
        quality_policy = None if legacy_v1 else formal_original_quality_policy()
    else:
        if not isinstance(quality_request_contract, Mapping) or set(
            quality_request_contract
        ) - {"allowed_reason_codes", "review_policy"}:
            raise Stage7ContractError(
                "exam_stage7_contract_invalid",
                "quality_request_contract_invalid",
                "质量审查请求合同无效",
            )
        raw_codes = quality_request_contract.get("allowed_reason_codes")
        if (
            isinstance(raw_codes, (str, bytes))
            or not isinstance(raw_codes, Sequence)
            or any(not isinstance(code, str) for code in raw_codes)
            or len(raw_codes) != len(set(raw_codes))
        ):
            raise Stage7ContractError(
                "exam_stage7_contract_invalid",
                "quality_request_contract_invalid",
                "质量审查原因码合同无效",
            )
        quality_codes = frozenset(raw_codes)
        quality_policy = quality_request_contract.get("review_policy")
        legacy_contract = quality_codes == _LEGACY_QUALITY_CODES and quality_policy is None
        current_contract = (
            quality_codes == _QUALITY_CODES
            and quality_policy == formal_original_quality_policy()
            and not legacy_v1
        )
        if not (legacy_contract or current_contract):
            raise Stage7ContractError(
                "exam_stage7_contract_invalid",
                "quality_request_contract_invalid",
                "质量审查请求合同不受支持",
            )

    quality_request = {
        "version": quality_protocol_version, "selection_hash": selection.selection_hash,
        "pre_adaptation_hash": pre_hash,
        "items": [{"position": item["position"], "candidate_id": item["assignment"]["candidate_id"], "source_kind": item["assignment"]["source_kind"], "question_type_family": item["assignment"]["question_type_family"], "content_snapshot": item["content_snapshot"]} for item in items],
        "allowed_reason_codes": sorted(quality_codes),
    }
    if quality_policy is not None:
        quality_request["review_policy"] = quality_policy
    quality = _quality_response(
        _call(quality_callback, quality_request),
        selection.selection_hash,
        pre_hash,
        tuple(range(1, len(items) + 1)),
        quality_protocol_version,
        quality_codes,
    )
    failed_positions = [position for position, result in quality.items() if not result[0]]
    if len(failed_positions) > 1:
        raise Stage7ContractError("exam_stage7_quality_rejected", "multiple_quality_failures", "常规质量审查发现多道题不合格", status_code=422)

    planned_position = failed_positions[0] if failed_positions else None
    skip_reason: str | None = None
    if planned_position is not None:
        if adaptation_callback is None or verification_callback is None or budget.structural_calls_remaining < 3:
            skip_reason = "structural_budget_insufficient"
        elif budget.mode == "enforce" and budget.formal_adaptation_min_remaining_seconds is not None:
            if budget.remaining_seconds is None or budget.remaining_seconds < budget.formal_adaptation_min_remaining_seconds:
                skip_reason = "formal_time_budget_insufficient"
    final_items = [dict(item) for item in items]
    adapted_position: int | None = None
    fallback = False
    verification_codes: tuple[str, ...] = ("none",)
    fallback_reason: str | None = None
    if planned_position is not None and skip_reason is None:
        index = planned_position - 1
        original = items[index]
        bind_hash = _digest({"position": planned_position, "candidate_id": original["assignment"]["candidate_id"], "pre_adaptation_hash": pre_hash})
        adapt_request = {
            "version": ADAPTATION_PROTOCOL_VERSION, "position": planned_position,
            "candidate_id": original["assignment"]["candidate_id"], "pre_adaptation_hash": pre_hash,
            "binding_hash": bind_hash, "content_snapshot": original["content_snapshot"],
            "instruction": "仅改善题面清晰性与成卷可用性；不得返回答案、解析、难度或推理。",
        }
        try:
            adapt_value = _strict_json(_call(adaptation_callback, adapt_request), "adaptation_response")
            _exact_keys(adapt_value, {"version", "position", "candidate_id", "pre_adaptation_hash", "binding_hash", "adapted_content"}, "adaptation_response_fields_invalid")
            if adapt_value.get("version") != ADAPTATION_PROTOCOL_VERSION or adapt_value.get("position") != planned_position or adapt_value.get("candidate_id") != original["assignment"]["candidate_id"] or adapt_value.get("pre_adaptation_hash") != pre_hash or adapt_value.get("binding_hash") != bind_hash:
                raise Stage7ContractError("exam_stage7_schema_invalid", "adaptation_response_binding_invalid", "改编响应绑定无效", status_code=422)
            adapted_text = _text(adapt_value.get("adapted_content"), "adapted_content")
            adapted_snapshot = json.loads(_canonical(original["content_snapshot"]))
            adapted_snapshot["content"] = adapted_text
            if _basic_content_codes(adapted_snapshot):
                raise Stage7ContractError(
                    "exam_stage7_schema_invalid",
                    "adapted_content_local_review_failed",
                    "改编正文未通过本地安全、完整性或渲染规则",
                    status_code=422,
                )
            adapted_snapshot_hash = _digest(adapted_snapshot)
            verify_request = {
                "version": VERIFICATION_PROTOCOL_VERSION, "position": planned_position,
                "candidate_id": original["assignment"]["candidate_id"], "pre_adaptation_hash": pre_hash,
                "adapted_content_hash": adapted_snapshot_hash, "adapted_content": adapted_snapshot,
                "checks": ["solvable", "meaning_complete", "candidate_result_valid", "renderable_and_publishable"],
                "allowed_reason_codes": sorted(_VERIFY_CODES),
            }
            verify_value = _strict_json(_call(verification_callback, verify_request), "verification_response")
            _exact_keys(verify_value, {"version", "position", "candidate_id", "pre_adaptation_hash", "adapted_content_hash", "passed", "reason_codes"}, "verification_response_fields_invalid")
            if verify_value.get("version") != VERIFICATION_PROTOCOL_VERSION or verify_value.get("position") != planned_position or verify_value.get("candidate_id") != original["assignment"]["candidate_id"] or verify_value.get("pre_adaptation_hash") != pre_hash or verify_value.get("adapted_content_hash") != adapted_snapshot_hash or type(verify_value.get("passed")) is not bool:
                raise Stage7ContractError("exam_stage7_schema_invalid", "verification_response_binding_invalid", "隐藏核验响应绑定无效", status_code=422)
            raw_codes = verify_value.get("reason_codes")
            if isinstance(raw_codes, (str, bytes)) or not isinstance(raw_codes, Sequence):
                raise Stage7ContractError("exam_stage7_schema_invalid", "verification_codes_invalid", "隐藏核验原因码无效", status_code=422)
            verification_codes = tuple(raw_codes)
            if not verification_codes or len(set(verification_codes)) != len(verification_codes) or any(code not in _VERIFY_CODES for code in verification_codes) or (verify_value["passed"] and verification_codes != ("none",)) or (not verify_value["passed"] and "none" in verification_codes):
                raise Stage7ContractError("exam_stage7_schema_invalid", "verification_codes_invalid", "隐藏核验原因码无效", status_code=422)
            if verify_value["passed"]:
                final_items[index] = {**original, "content_snapshot": adapted_snapshot, "adaptation": {"pre_content_snapshot_hash": _digest(original["content_snapshot"]), "adapted_content_hash": adapted_snapshot_hash}}
                adapted_position = planned_position
            else:
                fallback, fallback_reason = True, "adaptation_verification_failed"
        except BaseException as exc:
            if _is_propagated(exc):
                raise
            fallback = True
            if isinstance(exc, TimeoutError):
                fallback_reason = "adaptation_timeout"
            elif isinstance(exc, Stage7ContractError):
                fallback_reason = "adaptation_schema_invalid"
            else:
                fallback_reason = "adaptation_failed"

    final_hash = _paper_hash(final_items)
    final_body = {
        "version": final_receipt_version, "declaration": DECLARATION,
        "selection_hash": selection.selection_hash, "pre_adaptation_receipt_hash": pre_receipt["receipt_hash"],
        "pre_adaptation_hash": pre_hash, "quality_reviewed_positions": list(range(1, len(items) + 1)),
        "quality_reason_codes": {str(position): list(quality[position][1]) for position in sorted(quality)},
        "adaptation_planned_position": planned_position, "adaptation_triggered": adapted_position is not None or fallback,
        "controlled_exception_position": adapted_position, "adaptation_skip_reason": skip_reason,
        "verification_reason_codes": list(verification_codes), "single_item_fallback": fallback,
        "fallback_reason": fallback_reason, "difficulty_recheck_performed": False,
        "selection_changed": False, "other_items_changed": False, "final_exam_hash": final_hash,
    }
    final_receipt = {**final_body, "receipt_hash": _digest(final_body)}
    return Stage7Result(pre_hash, final_hash, adapted_position, fallback, tuple(final_items), pre_receipt, final_receipt)


__all__ = [
    "ADAPTATION_PROTOCOL_VERSION", "DECLARATION", "FINAL_RECEIPT_VERSION", "GLOBAL_CONSTRAINT_EVIDENCE_VERSION", "LOCAL_EVIDENCE_VERSION",
    "LEGACY_FINAL_RECEIPT_VERSION", "LEGACY_PRE_RECEIPT_VERSION", "LEGACY_QUALITY_PROTOCOL_VERSION",
    "FORMAL_ORIGINAL_QUALITY_POLICY_VERSION", "PRE_RECEIPT_VERSION", "QUALITY_PROMPT_VERSION", "QUALITY_PROTOCOL_VERSION", "STAGE7_VERSION", "VERIFICATION_PROTOCOL_VERSION", "formal_original_quality_policy",
    "Stage7Budget", "Stage7Cancellation", "Stage7ContractError", "Stage7DeadlineExceeded", "Stage7LeaseLost",
    "Stage7Result", "run_stage7_quality_and_adaptation",
]
