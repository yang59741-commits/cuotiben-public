"""Provider-free rendering attribution for D2 selected candidates.

The probe renders a frozen candidate twice with one canonical template and
then renders a known-good control.  It only attributes a failure to CONTENT
when both candidate renders fail, the control succeeds, and no explicit
renderer/environment error code was observed.  It never edits the frozen
snapshot, question source, or semantic card.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_v3_publish import _escape_latex
from .exam_paper_layout import render_question_content
from .exam_time_policy import ExamDeadlineExceeded
from .job_contracts import JobCancelled, JobLeaseLost
from .semantic_index import SemanticIndexContractError, semantic_content_hash
from .tex import TexCompileError, UnsafeLatexError, compile_latex_artifact, validate_latex_body


D2_RENDER_PROBE_VERSION = "past-v3-d2-render-probe-v1"
D2_RENDERING_SPEC_VERSION = "past-v3-d2-canonical-single-question-v1"
_REPEAT_REQUIREMENT = 2
_HASH_LENGTH = 64
_CONTROL_SNAPSHOT = {
    "schema_version": "semantic-candidate-content-snapshot-v1",
    "content": r"已知 \(1+1=2\)，求其值。",
    "figure_references": [],
    "options": [],
    "conditions": [],
}

# Exact renderer/process codes only.  Human-readable exception text is never
# used for attribution because it is localized and can contain candidate text.
_ENVIRONMENT_REASON_CODES = frozenset(
    {
        "XELATEX_NOT_FOUND",
        "XELATEX_COMMAND_INVALID",
        "TEX_TIMEOUT",
        "TEX_START_FAILED",
        "TEX_MISSING_FILE",
        "TEX_PDF_MISSING",
        "OUTPUT_DIRECTORY_UNAVAILABLE",
        "FILESYSTEM_PERMISSION_DENIED",
        "TEMPLATE_INVALID",
        "TEMPLATE_MISSING",
    }
)
_STABLE_CONTENT_FAILURE_CODES = frozenset(
    {
        "TEX_UNDEFINED_COMMAND",
        "TEX_ENVIRONMENT_MISMATCH",
        "TEX_BRACE_MISMATCH",
        "TEX_COMPILE_FAILED",
        "TEX_UNSAFE_BODY",
    }
)


class D2RenderProbeContractError(ValueError):
    """Raised when the frozen identity or injected probe contract is invalid."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise D2RenderProbeContractError("non_canonical_json") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity(value: Any, reason: str, *, hash_value: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise D2RenderProbeContractError(reason)
    normalized = value.strip()
    if len(normalized) > 512 or (
        hash_value
        and (
            len(normalized) != _HASH_LENGTH
            or any(char not in "0123456789abcdef" for char in normalized)
        )
    ):
        raise D2RenderProbeContractError(reason)
    return normalized


def _snapshot(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise D2RenderProbeContractError("content_snapshot_invalid")
    expected = {
        "schema_version",
        "content",
        "figure_references",
        "options",
        "conditions",
    }
    if (
        set(value) != expected
        or value.get("schema_version")
        != "semantic-candidate-content-snapshot-v1"
        or not isinstance(value.get("content"), str)
        or not value["content"].strip()
    ):
        raise D2RenderProbeContractError("content_snapshot_invalid")
    result: dict[str, Any] = {
        "schema_version": value["schema_version"],
        # Do not strip or normalize mathematical input.  The semantic identity
        # function owns its normalization contract.
        "content": value["content"],
    }
    for field in ("figure_references", "options", "conditions"):
        rows = value.get(field)
        if (
            isinstance(rows, (str, bytes))
            or not isinstance(rows, Sequence)
            or len(rows) > 500
            or any(not isinstance(item, str) or not item.strip() for item in rows)
        ):
            raise D2RenderProbeContractError("content_snapshot_invalid")
        result[field] = list(rows)
    return result


def _content_hash(snapshot: Mapping[str, Any]) -> str:
    try:
        return semantic_content_hash(
            snapshot["content"],
            figure_references=tuple(snapshot["figure_references"]),
            options=tuple(snapshot["options"]),
            conditions=tuple(snapshot["conditions"]),
        )
    except SemanticIndexContractError as exc:
        raise D2RenderProbeContractError("content_snapshot_unreplayable") from exc


def canonical_single_question_template(snapshot: Mapping[str, Any]) -> str:
    """Build the production D2 template for one complete frozen snapshot."""

    frozen = _snapshot(snapshot)
    content = validate_latex_body(frozen["content"], label="D2 渲染探针题面")
    conditions = [
        validate_latex_body(item, label="D2 渲染探针条件")
        for item in frozen["conditions"]
    ]
    options = [
        validate_latex_body(item, label="D2 渲染探针选项")
        for item in frozen["options"]
    ]
    blocks = [content]
    blocks.extend(r"\par\textbf{条件：} " + item for item in conditions)
    blocks.extend(
        r"\par\textit{图示：} " + _escape_latex(item)
        for item in frozen["figure_references"]
    )
    if options:
        blocks.append(
            "\\begin{enumerate}\n"
            + "\n".join(r"\item " + item for item in options)
            + "\n\\end{enumerate}"
        )
    item = render_question_content("\n".join(blocks))
    return (
        "\\documentclass[UTF8]{ctexart}\n"
        "\\usepackage{amsmath,amssymb}\n"
        "\\begin{document}\n"
        "\\begin{enumerate}\n"
        "\\item "
        + item
        + "\n\\end{enumerate}\n"
        "\\end{document}\n"
    )


def make_production_compile_callback(
    *, xelatex_command: str, timeout_seconds: float = 60.0
) -> Callable[[Mapping[str, Any]], None]:
    """Create a callback backed by the application's isolated XeLaTeX API."""

    command = str(xelatex_command or "").strip()
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise D2RenderProbeContractError("timeout_invalid") from exc
    if not command:
        raise D2RenderProbeContractError("xelatex_command_invalid")
    if timeout < 1 or timeout > 600:
        raise D2RenderProbeContractError("timeout_invalid")

    def compile_request(request: Mapping[str, Any]) -> None:
        resolved = shutil.which(command)
        if resolved is None and not Path(command).is_file():
            raise TexCompileError(
                "未找到 XeLaTeX",
                code="XELATEX_NOT_FOUND",
                hint="请检查 XeLaTeX 配置",
            )
        working_directory = request.get("working_directory")
        latex_source = request.get("latex_source")
        stem = request.get("stem")
        if not isinstance(working_directory, str) or not isinstance(latex_source, str):
            raise D2RenderProbeContractError("compile_request_invalid")
        compile_latex_artifact(
            working_directory,
            command,
            str(stem),
            latex_source,
            timeout=min(timeout, max(1, int(request["timeout_seconds"]))),
            publish_source=False,
        )

    # This labels observed evidence; it is not a production authorization gate.
    setattr(compile_request, "_d2_render_environment", "production")
    return compile_request


def _lifecycle_exception(exc: BaseException) -> bool:
    return isinstance(exc, (JobCancelled, JobLeaseLost, ExamDeadlineExceeded))


def _outcome_from_value(value: Any) -> dict[str, Any]:
    if value is None or value is True:
        return {"outcome": "succeeded", "reason_code": "NONE"}
    if value is False:
        return {"outcome": "failed", "reason_code": "CALLBACK_REPORTED_FAILURE"}
    if not isinstance(value, Mapping):
        return {"outcome": "failed", "reason_code": "CALLBACK_RESULT_INVALID"}
    success = value.get("succeeded", value.get("success", value.get("ok")))
    code = value.get("reason_code", "NONE" if success is True else "CALLBACK_REPORTED_FAILURE")
    if type(success) is not bool or not isinstance(code, str) or not code.strip():
        return {"outcome": "failed", "reason_code": "CALLBACK_RESULT_INVALID"}
    return {
        "outcome": "succeeded" if success else "failed",
        "reason_code": "NONE" if success else code.strip(),
    }


def _invoke_compile(
    callback: Callable[[Mapping[str, Any]], Any], request: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        return _outcome_from_value(callback(request))
    except BaseException as exc:
        if _lifecycle_exception(exc):
            raise
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, TexCompileError):
            outcome = {"outcome": "failed", "reason_code": str(exc.code)}
            if exc.correlation_id:
                outcome["correlation_id"] = str(exc.correlation_id)
            return outcome
        if isinstance(exc, PermissionError):
            return {"outcome": "failed", "reason_code": "FILESYSTEM_PERMISSION_DENIED"}
        if isinstance(exc, OSError):
            return {"outcome": "failed", "reason_code": "OUTPUT_DIRECTORY_UNAVAILABLE"}
        return {
            "outcome": "failed",
            "reason_code": "CALLBACK_EXCEPTION",
            "exception_type": type(exc).__name__,
        }


def _environment_label(callback: Callable[[Mapping[str, Any]], Any] | None) -> str:
    if callback is None:
        return "unobserved"
    if getattr(callback, "_d2_render_environment", None) == "production":
        return "production"
    return "fixture"


def probe_rendering_risk(
    *,
    selection_hash: str,
    candidate_id: str,
    content_hash: str,
    content_snapshot: Mapping[str, Any],
    compile_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    control_callback: Callable[[Mapping[str, Any]], Any] | None = None,
    canonical_template_factory: Callable[[Mapping[str, Any]], str] | None = None,
    attempts: int = _REPEAT_REQUIREMENT,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Return a serializable, task-bound D2 rendering attribution receipt."""

    selection = _identity(selection_hash, "selection_hash_invalid", hash_value=True)
    candidate = _identity(candidate_id, "candidate_id_invalid")
    expected_content_hash = _identity(
        content_hash, "content_hash_invalid", hash_value=True
    )
    frozen = _snapshot(content_snapshot)
    if _content_hash(frozen) != expected_content_hash:
        raise D2RenderProbeContractError("content_hash_mismatch")
    if type(attempts) is not int or attempts < 1 or attempts > 5:
        raise D2RenderProbeContractError("attempts_invalid")
    try:
        timeout = float(timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise D2RenderProbeContractError("timeout_invalid") from exc
    if not 0 < timeout <= 600:
        raise D2RenderProbeContractError("timeout_invalid")

    factory = canonical_template_factory or canonical_single_question_template
    environment_label = _environment_label(compile_callback)
    base = {
        "version": D2_RENDER_PROBE_VERSION,
        "classification": "UNKNOWN",
        "candidate_id": candidate,
        "selection_hash": selection,
        "content_hash": expected_content_hash,
        # semantic_content_hash covers the complete snapshot payload (content,
        # figures, options and conditions); keep one identity at this boundary.
        "content_snapshot_hash": expected_content_hash,
        "canonical_template": True,
        "template_hash": None,
        "rendering_spec_hash": _digest(
            {
                "version": D2_RENDERING_SPEC_VERSION,
                "repeat_requirement": _REPEAT_REQUIREMENT,
            }
        ),
        "repeat_requirement": _REPEAT_REQUIREMENT,
        "reproduced_failure_count": 0,
        "candidate_attempts": [],
        "control": {"outcome": "not_run", "reason_code": "NOT_RUN"},
        "control_succeeded": False,
        "environment": {
            "observed": environment_label,
            "is_fixture": environment_label == "fixture",
            "reason_codes": [],
        },
        "disposition": "blocked",
    }
    if compile_callback is None:
        base["candidate_attempts"] = [
            {"attempt": 0, "outcome": "not_run", "reason_code": "PROBE_NOT_RUN"}
        ]
        return base

    try:
        control_latex = factory(_CONTROL_SNAPSHOT)
        if not isinstance(control_latex, str) or not control_latex.strip():
            raise D2RenderProbeContractError("template_invalid")
    except BaseException as exc:
        if _lifecycle_exception(exc) or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        base["classification"] = "TEMPLATE_OR_ENVIRONMENT"
        base["environment"]["reason_codes"] = ["TEMPLATE_INVALID"]
        base["disposition"] = "repair_template_or_environment"
        return base

    try:
        candidate_latex = factory(frozen)
        if not isinstance(candidate_latex, str) or not candidate_latex.strip():
            return base
    except UnsafeLatexError:
        candidate_latex = None
    except BaseException as exc:
        if _lifecycle_exception(exc) or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return base

    base["template_hash"] = _digest(
        {"candidate": candidate_latex, "control_shape": control_latex}
    )
    candidate_attempts: list[dict[str, Any]] = []
    environment_codes: set[str] = set()
    try:
        with TemporaryDirectory(prefix="cuoti-d2-render-probe-") as directory:
            for attempt in range(1, attempts + 1):
                if candidate_latex is None:
                    # Reproduce the renderer's body rejection without executing forbidden TeX.
                    try:
                        factory(frozen)
                    except UnsafeLatexError:
                        outcome = {"outcome": "failed", "reason_code": "TEX_UNSAFE_BODY", "stage": "body_validation"}
                    else:
                        return base
                else:
                    outcome = _invoke_compile(compile_callback, {
                        "version": D2_RENDER_PROBE_VERSION,
                        "kind": "candidate",
                        "attempt": attempt,
                        "candidate_id": candidate,
                        "selection_hash": selection,
                        "content_hash": expected_content_hash,
                        "template_hash": base["template_hash"],
                        "latex_source": candidate_latex,
                        "working_directory": directory,
                        "stem": f"d2_candidate_{attempt}",
                        "timeout_seconds": timeout,
                    })
                candidate_attempts.append({"attempt": attempt, **outcome})
                if outcome["reason_code"] in _ENVIRONMENT_REASON_CODES:
                    environment_codes.add(outcome["reason_code"])
                    break

            base["candidate_attempts"] = candidate_attempts
            base["reproduced_failure_count"] = sum(
                row["outcome"] == "failed" for row in candidate_attempts
            )
            if environment_codes:
                base["classification"] = "TEMPLATE_OR_ENVIRONMENT"
                base["environment"]["reason_codes"] = sorted(environment_codes)
                base["disposition"] = "repair_template_or_environment"
                return base
            if (
                len(candidate_attempts) < _REPEAT_REQUIREMENT
                or any(row["outcome"] != "failed" for row in candidate_attempts)
                or any(
                    row["reason_code"] not in _STABLE_CONTENT_FAILURE_CODES
                    for row in candidate_attempts
                )
                or len({row["reason_code"] for row in candidate_attempts}) != 1
            ):
                return base

            control_compiler = control_callback or compile_callback
            control_outcome = _invoke_compile(
                control_compiler,
                {
                    "version": D2_RENDER_PROBE_VERSION,
                    "kind": "control",
                    "attempt": 1,
                    "candidate_id": candidate,
                    "selection_hash": selection,
                    "content_hash": expected_content_hash,
                    "template_hash": base["template_hash"],
                    "latex_source": control_latex,
                    "working_directory": directory,
                    "stem": "d2_control_1",
                    "timeout_seconds": timeout,
                },
            )
            base["control"] = control_outcome
            base["control_succeeded"] = control_outcome["outcome"] == "succeeded"
            if not base["control_succeeded"]:
                code = control_outcome["reason_code"]
                base["classification"] = "TEMPLATE_OR_ENVIRONMENT"
                base["environment"]["reason_codes"] = [code]
                base["disposition"] = "repair_template_or_environment"
                return base
            base["classification"] = "CONTENT"
            base["disposition"] = "reject_task_local_content"
            return base
    except BaseException as exc:
        if _lifecycle_exception(exc) or isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        code = (
            "FILESYSTEM_PERMISSION_DENIED"
            if isinstance(exc, PermissionError)
            else "OUTPUT_DIRECTORY_UNAVAILABLE"
        )
        base["classification"] = "TEMPLATE_OR_ENVIRONMENT"
        base["environment"]["reason_codes"] = [code]
        base["disposition"] = "repair_template_or_environment"
        return base


__all__ = [
    "D2_RENDER_PROBE_VERSION",
    "D2_RENDERING_SPEC_VERSION",
    "D2RenderProbeContractError",
    "canonical_single_question_template",
    "make_production_compile_callback",
    "probe_rendering_risk",
]
