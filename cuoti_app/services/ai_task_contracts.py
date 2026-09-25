"""Central, versioned contracts for AI task execution.

The registry intentionally contains transport-independent policy only. Domain
services remain responsible for validating their business payloads before any
result is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass


_EFFORT_ORDER = {"low": 0, "medium": 1, "high": 2, "max": 3}


@dataclass(frozen=True, slots=True)
class AITaskContract:
    name: str
    first_output_tokens: int
    retry_output_tokens: int
    reasoning_effort: str
    json_output: bool = False
    thinking: bool = True
    default_model_role: str = "quality"
    prompt_version: str = "legacy-v1"
    schema_version: str = "legacy-v1"
    max_logical_calls: int = 2

    def legacy_policy(self) -> dict:
        """Return the historical dict shape during the staged migration."""

        policy = {
            "first": self.first_output_tokens,
            "retry": self.retry_output_tokens,
            "effort": self.reasoning_effort,
            "thinking": self.thinking,
            "model_role": self.default_model_role,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "max_logical_calls": self.max_logical_calls,
        }
        if self.json_output:
            policy["json"] = True
        return policy


TASK_CONTRACTS = {
    "generic_text": AITaskContract(
        "generic_text", 64_000, 128_000, "high"
    ),
    "classification_single": AITaskContract(
        "classification_single", 16_000, 32_000, "high",
        thinking=False, default_model_role="fast",
    ),
    "classification_batch": AITaskContract(
        "classification_batch", 64_000, 128_000, "high",
        thinking=False, default_model_role="fast",
    ),
    "classification_object": AITaskContract(
        "classification_object", 16_000, 32_000, "high",
        json_output=True, thinking=False, default_model_role="fast",
        prompt_version="classification-v2", schema_version="classification-v2",
    ),
    "classification_batch_object": AITaskContract(
        "classification_batch_object", 64_000, 128_000, "high",
        json_output=True, thinking=False, default_model_role="fast",
        prompt_version="classification-batch-v2",
        schema_version="classification-batch-v2",
    ),
    "problem_analysis": AITaskContract(
        "problem_analysis", 64_000, 128_000, "high", json_output=True,
        prompt_version="problem-analysis-v2", schema_version="problem-analysis-v2",
    ),
    "daily_analysis": AITaskContract(
        "daily_analysis", 128_000, 256_000, "high", json_output=True,
        prompt_version="daily-analysis-v2", schema_version="daily-analysis-v1",
    ),
    "exam_generation": AITaskContract(
        "exam_generation", 192_000, 384_000, "max"
    ),
    "structured_exam": AITaskContract(
        "structured_exam", 192_000, 384_000, "max", json_output=True
    ),
    "past_structure": AITaskContract(
        "past_structure", 192_000, 384_000, "max", json_output=True
    ),
    "candidate_selection": AITaskContract(
        "candidate_selection", 128_000, 256_000, "max", json_output=True
    ),
    "difficulty_review": AITaskContract(
        "difficulty_review", 64_000, 128_000, "max", json_output=True,
        prompt_version="exam-difficulty-review-v1",
        schema_version="exam-difficulty-review-v1",
    ),
    "exam_difficulty_review": AITaskContract(
        "exam_difficulty_review", 64_000, 128_000, "max", json_output=True,
        prompt_version="exam-difficulty-review-v1",
        schema_version="exam-difficulty-review-v1",
    ),
    "exam_quality_review": AITaskContract(
        "exam_quality_review", 128_000, 256_000, "max", json_output=True,
        prompt_version="exam-quality-review-v1",
        schema_version="exam-quality-review-v1",
    ),
    "exam_item_repair": AITaskContract(
        "exam_item_repair", 128_000, 256_000, "max",
        prompt_version="exam-item-repair-v1",
        schema_version="exam-item-repair-v1",
    ),
    "material_analysis": AITaskContract(
        "material_analysis", 128_000, 256_000, "max", json_output=True,
        prompt_version="material-cards-v4", schema_version="material-cards-v1",
    ),
    "material_analysis_shard": AITaskContract(
        "material_analysis_shard", 32_000, 64_000, "max", json_output=True,
        prompt_version="material-cards-v4", schema_version="material-cards-v1",
    ),
    "material_analysis_summary": AITaskContract(
        "material_analysis_summary", 16_000, 32_000, "max", json_output=True,
        prompt_version="material-summary-v1", schema_version="material-summary-v1",
    ),
    "document_ocr": AITaskContract(
        "document_ocr", 16_000, 32_000, "low",
        thinking=False, default_model_role="fast",
    ),
}


TASK_DEFAULT_POLICIES = {
    "classification": "classification_single",
    "problem_analysis": "problem_analysis",
    "daily_analysis": "daily_analysis",
    "exam_generation": "exam_generation",
    "ocr": "document_ocr",
}


def contract_for(task: str | None, task_policy: str | None) -> AITaskContract:
    name = str(task_policy or TASK_DEFAULT_POLICIES.get(task) or "generic_text")
    try:
        return TASK_CONTRACTS[name]
    except KeyError as exc:
        raise KeyError(name) from exc


def capped_reasoning_effort(
    requested: str | None,
    *,
    contract_effort: str,
    profile_cap: str | None,
) -> str:
    """Resolve task effort while treating a profile value as an upper bound."""

    desired = str(requested or contract_effort or "high").strip().casefold()
    if desired not in _EFFORT_ORDER:
        desired = contract_effort
    cap = str(profile_cap or "").strip().casefold()
    if cap not in _EFFORT_ORDER:
        return desired
    return min((desired, cap), key=_EFFORT_ORDER.__getitem__)


# Compatibility export for callers/tests while tasks migrate to contracts.
AI_TASK_POLICIES = {
    name: contract.legacy_policy() for name, contract in TASK_CONTRACTS.items()
}
