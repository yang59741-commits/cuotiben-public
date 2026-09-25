"""Pure request identities for the remediation generation pipeline.

The three identities deliberately have different owners:

* ``background_jobs.input_hash`` is the normalized logical request used for
  idempotency before server-owned facts are frozen;
* ``job_inputs.payload_hash`` is the exact canonical persisted input;
* ``generation_request_hash`` excludes execution policy while retaining every
  frozen fact that can affect the requested paper.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping

from .exam_contracts import validate_single_paper_count
from .exam_remediation_contracts import SINGLE_PAPER_CONTRACT_VERSION
from .job_contracts import canonical_payload


GENERATION_REQUEST_HASH_VERSION = "exam-generation-request-hash-v1"
JOB_INPUT_IDENTITY_VERSION = "job-input-v1"
EXAM_GENERATION_INPUT_IDENTITY_VERSION = "exam-generation-input-v1"
LEGACY_JOB_INPUT_IDENTITY_VERSION = "legacy-job-input-v37"
JOB_INPUT_IDENTITY_VERSIONS = frozenset(
    {
        JOB_INPUT_IDENTITY_VERSION,
        EXAM_GENERATION_INPUT_IDENTITY_VERSION,
        LEGACY_JOB_INPUT_IDENTITY_VERSION,
    }
)

_SERVER_OWNED_IDENTITY_FIELDS = frozenset(
    {
        "generation_request_hash",
        "generation_request_hash_version",
        "job_input_hash",
        "logical_request_hash",
        "binding_hash",
    }
)
FORBIDDEN_EXAM_RUNTIME_IDENTITY_FIELDS = frozenset(
    {
        "_job_id",
        "generation_job_id",
        "request_id",
        "worker_id",
        "lease_attempt",
        "attempt",
        "attempts",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "available_at",
        "heartbeat_at",
        "deadline_at",
        "runtime_config",
        "provider",
        "model",
        "endpoint",
        "api_key",
        "reasoning_effort",
        "context_window",
        "max_output_tokens",
        "manifest",
        "selection_checkpoint",
        "checkpoint",
        "artifact_path",
        "pdf_dir",
        "source_dir",
        "callback_url",
    }
)
_EXECUTION_ONLY_FIELDS = frozenset({"exam_time_policy"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class ExamGenerationRequestIdentityError(ValueError):
    """The persisted or caller-supplied request identity is not trustworthy."""

    def __init__(self, reason: str) -> None:
        super().__init__("组卷任务冻结输入完整性校验失败")
        self.code = "exam_request_identity_invalid"
        self.reason = str(reason)


def prepare_exam_generation_submission(payload: object) -> dict[str, Any]:
    """Normalize the single-paper request before any identity is calculated."""

    if not isinstance(payload, Mapping):
        raise ValueError("任务输入必须是 JSON 对象")
    normalized = dict(payload)
    if _SERVER_OWNED_IDENTITY_FIELDS.intersection(normalized):
        raise ExamGenerationRequestIdentityError(
            "client_owned_request_identity_forbidden"
        )
    if FORBIDDEN_EXAM_RUNTIME_IDENTITY_FIELDS.intersection(normalized):
        raise ExamGenerationRequestIdentityError(
            "client_owned_runtime_identity_forbidden"
        )
    validate_single_paper_count(normalized.get("paper_count", 1))
    normalized["paper_count"] = 1
    normalized["single_paper_contract_version"] = SINGLE_PAPER_CONTRACT_VERSION
    return normalized


def logical_request_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return caller intent without server-frozen execution policy."""

    normalized = dict(payload)
    normalized.pop("exam_time_policy", None)
    return normalized


def logical_request_hash(payload: Mapping[str, Any]) -> str:
    return canonical_payload(logical_request_payload(payload))[1]


def generation_request_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Project facts that define the requested paper, not its execution clock."""

    projected = {
        key: value
        for key, value in dict(payload).items()
        if key not in _EXECUTION_ONLY_FIELDS
        and key != "generation_request_hash"
    }
    return projected


def generation_request_hash(payload: Mapping[str, Any]) -> str:
    return canonical_payload(generation_request_projection(payload))[1]


def attach_generation_request_identity(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Attach a self-verifying semantic request identity before input hashing."""

    if _SERVER_OWNED_IDENTITY_FIELDS.intersection(payload):
        raise ExamGenerationRequestIdentityError(
            "request_identity_already_present"
        )
    normalized = dict(payload)
    normalized["generation_request_hash_version"] = (
        GENERATION_REQUEST_HASH_VERSION
    )
    normalized["generation_request_hash"] = generation_request_hash(normalized)
    return normalized


def verify_generation_request_identity(payload: Mapping[str, Any]) -> str | None:
    """Verify new inputs while allowing explicitly legacy rows to remain readable."""

    has_version = "generation_request_hash_version" in payload
    has_hash = "generation_request_hash" in payload
    if not has_version and not has_hash:
        return None
    if not has_version or not has_hash:
        raise ExamGenerationRequestIdentityError(
            "generation_request_identity_incomplete"
        )
    if payload.get("generation_request_hash_version") != (
        GENERATION_REQUEST_HASH_VERSION
    ):
        raise ExamGenerationRequestIdentityError(
            "generation_request_hash_version_unsupported"
        )
    actual = payload.get("generation_request_hash")
    if not isinstance(actual, str) or not _SHA256_RE.fullmatch(actual):
        raise ExamGenerationRequestIdentityError(
            "generation_request_hash_malformed"
        )
    expected = generation_request_hash(payload)
    if not hmac.compare_digest(actual, expected):
        raise ExamGenerationRequestIdentityError(
            "generation_request_hash_mismatch"
        )
    return actual


def canonical_persisted_input(
    payload_json: object,
) -> tuple[dict[str, Any], str, str]:
    """Parse one strict JSON object and return object, canonical bytes and hash."""

    if not isinstance(payload_json, str):
        raise ExamGenerationRequestIdentityError("job_input_json_not_text")

    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            payload_json,
            parse_constant=reject_constant,
            object_pairs_hook=unique_object,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExamGenerationRequestIdentityError("job_input_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise ExamGenerationRequestIdentityError("job_input_not_object")
    try:
        canonical, digest = canonical_payload(parsed)
    except ValueError as exc:
        raise ExamGenerationRequestIdentityError("job_input_json_invalid") from exc
    return parsed, canonical, digest


def verify_persisted_input(
    payload_json: object,
    payload_hash: object,
    *,
    identity_version: object,
    job_kind: object,
    job_status: object | None = None,
    allow_legacy_exam_read: bool = False,
) -> dict[str, Any]:
    """Verify canonical storage bytes and the database-owned identity class."""

    parsed, canonical, expected = canonical_persisted_input(payload_json)
    if payload_json != canonical:
        raise ExamGenerationRequestIdentityError("job_input_not_canonical")
    if not isinstance(payload_hash, str) or not _SHA256_RE.fullmatch(payload_hash):
        raise ExamGenerationRequestIdentityError("job_input_hash_malformed")
    if not hmac.compare_digest(payload_hash, expected):
        raise ExamGenerationRequestIdentityError("job_input_hash_mismatch")
    if identity_version not in JOB_INPUT_IDENTITY_VERSIONS:
        raise ExamGenerationRequestIdentityError(
            "job_input_identity_version_unsupported"
        )
    kind = str(job_kind or "")
    if identity_version == EXAM_GENERATION_INPUT_IDENTITY_VERSION:
        if kind != "exam-generation":
            raise ExamGenerationRequestIdentityError(
                "exam_input_identity_kind_mismatch"
            )
        if verify_generation_request_identity(parsed) is None:
            raise ExamGenerationRequestIdentityError(
                "generation_request_identity_missing"
            )
    elif identity_version == JOB_INPUT_IDENTITY_VERSION:
        if kind == "exam-generation":
            raise ExamGenerationRequestIdentityError(
                "exam_input_identity_class_mismatch"
            )
    elif kind == "exam-generation" and not (
        allow_legacy_exam_read and str(job_status or "") == "succeeded"
    ):
        paper_count = parsed.get("paper_count", 1)
        if type(paper_count) is int and paper_count > 1:
            raise ExamGenerationRequestIdentityError(
                "legacy_multi_paper_checkpoint_unsupported"
            )
        raise ExamGenerationRequestIdentityError(
            "legacy_exam_checkpoint_migration_pending"
        )
    return parsed


def payload_hash_for_canonical_json(payload_json: str) -> str:
    """Hash already-canonical persisted bytes without interpreting their content."""

    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


__all__ = [
    "GENERATION_REQUEST_HASH_VERSION",
    "EXAM_GENERATION_INPUT_IDENTITY_VERSION",
    "FORBIDDEN_EXAM_RUNTIME_IDENTITY_FIELDS",
    "JOB_INPUT_IDENTITY_VERSION",
    "JOB_INPUT_IDENTITY_VERSIONS",
    "LEGACY_JOB_INPUT_IDENTITY_VERSION",
    "ExamGenerationRequestIdentityError",
    "attach_generation_request_identity",
    "canonical_persisted_input",
    "generation_request_hash",
    "generation_request_projection",
    "logical_request_hash",
    "logical_request_payload",
    "payload_hash_for_canonical_json",
    "prepare_exam_generation_submission",
    "verify_generation_request_identity",
    "verify_persisted_input",
]
