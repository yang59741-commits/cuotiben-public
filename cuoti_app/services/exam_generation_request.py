"""Immutable request identity for one exam-generation operation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .exam_contracts import validate_source
from .exam_generation_memory import pending_request_event


D2_SCORING_PROTOCOL = "past-v3-stage6-candidate-base-d2-v1"


@dataclass(frozen=True, slots=True)
class FrozenGenerationRequest:
    source: str
    allow_adaptation: bool
    memory_type: str
    memory_actor: str
    operation_id: str
    pending_memory_request: Mapping[str, Any]
    # Optional explicit Stage-6 protocol marker.  Omitted or legacy means
    # historical read-only recovery only: a new/incomplete strict task is
    # blocked and can never create score/penalty.  It is never silently
    # upgraded to candidate-base or downgraded to a legacy writer.
    stage6_scoring_protocol: str | None = None
    priority_once: bool = False


def freeze_generation_request(
    payload: Mapping[str, Any],
    *,
    generation_job_id: str | None,
    request_id: str | None,
) -> FrozenGenerationRequest:
    source = validate_source(payload.get("source", "free"))
    allow_adaptation = payload.get("allow_adaptation", False)
    if not isinstance(allow_adaptation, bool):
        raise ValueError("改编选项格式无效")
    priority_once = payload.get("priority_once", False)
    if type(priority_once) is not bool:
        raise ValueError("一次性优先选项格式无效")
    if priority_once and source != "past":
        raise ValueError("一次性优先仅支持真题组卷")
    memory_actor = {
        "free": "free_exam",
        "mistakes": "mistake_exam",
        "past": "past_exam",
    }[source]
    operation_id = generation_job_id or request_id or uuid.uuid4().hex
    stage6_protocol = payload.get("stage6_scoring_protocol")
    if stage6_protocol is not None and stage6_protocol not in {
        "legacy_score_read_only_v2",
        "candidate_base_rubric_v1",
        D2_SCORING_PROTOCOL,
    }:
        raise ValueError("Stage-6 评分协议标识无效")
    return FrozenGenerationRequest(
        source=source,
        allow_adaptation=allow_adaptation,
        memory_type=source,
        memory_actor=memory_actor,
        operation_id=operation_id,
        pending_memory_request=pending_request_event(payload, operation_id),
        stage6_scoring_protocol=stage6_protocol,
        priority_once=priority_once,
    )


def attach_current_scoring_protocol(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Stamp new past jobs without changing already-persisted requests."""
    result = dict(payload)
    if result.get("source") == "past" and result.get("stage6_scoring_protocol") is None:
        result["stage6_scoring_protocol"] = D2_SCORING_PROTOCOL
    return result


__all__ = ["D2_SCORING_PROTOCOL", "FrozenGenerationRequest", "freeze_generation_request", "attach_current_scoring_protocol"]
