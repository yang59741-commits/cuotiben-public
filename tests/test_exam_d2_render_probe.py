from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cuoti_app.services.exam_generation_d2_render_probe import (
    D2RenderProbeContractError,
    canonical_single_question_template,
    make_production_compile_callback,
    probe_rendering_risk,
)
from cuoti_app.services.tex import TexCompileError
from cuoti_app.services.semantic_index import semantic_content_hash


SELECTION_HASH = "a" * 64


def _snapshot(content: str = r"设 \(x_1=\frac{a}{b}\)，求 \(x_1^2\)。"):
    return {
        "schema_version": "semantic-candidate-content-snapshot-v1",
        "content": content,
        "figure_references": ["图 1（示意）"],
        "options": [r"\(x_1\)", r"\(x_1^2\)"],
        "conditions": [r"\(b\ne0\)"],
    }


def _hash(snapshot):
    return semantic_content_hash(
        snapshot["content"],
        figure_references=snapshot["figure_references"],
        options=snapshot["options"],
        conditions=snapshot["conditions"],
    )


def _probe(snapshot, callback, **kwargs):
    return probe_rendering_risk(
        selection_hash=SELECTION_HASH,
        candidate_id="candidate-1",
        content_hash=_hash(snapshot),
        content_snapshot=snapshot,
        compile_callback=callback,
        **kwargs,
    )


def test_repeated_candidate_failure_and_good_control_attributes_content():
    seen = []

    def compile_callback(request):
        seen.append(request)
        if request["kind"] == "control":
            return {"ok": True}
        return {"ok": False, "reason_code": "TEX_BRACE_MISMATCH"}

    snapshot = _snapshot()
    receipt = _probe(snapshot, compile_callback)

    assert receipt["classification"] == "CONTENT"
    assert receipt["disposition"] == "reject_task_local_content"
    assert receipt["reproduced_failure_count"] == 2
    assert [row["attempt"] for row in receipt["candidate_attempts"]] == [1, 2]
    assert receipt["control_succeeded"] is True
    assert receipt["environment"] == {
        "observed": "fixture",
        "is_fixture": True,
        "reason_codes": [],
    }
    assert len({row["template_hash"] for row in seen}) == 1
    assert all(row["content_hash"] == _hash(snapshot) for row in seen)


@pytest.mark.parametrize(
    "reason_code",
    [
        "XELATEX_NOT_FOUND",
        "TEX_TIMEOUT",
        "TEX_MISSING_FILE",
        "FILESYSTEM_PERMISSION_DENIED",
    ],
)
def test_explicit_environment_code_never_attributes_content(reason_code):
    def compile_callback(_request):
        return {"ok": False, "reason_code": reason_code}

    receipt = _probe(_snapshot(), compile_callback)
    assert receipt["classification"] == "TEMPLATE_OR_ENVIRONMENT"
    assert receipt["control_succeeded"] is False
    assert receipt["environment"]["reason_codes"] == [reason_code]


def test_repeat_shortfall_and_control_failure_do_not_attribute_content():
    failure = lambda _request: {"ok": False, "reason_code": "TEX_COMPILE_FAILED"}
    short = _probe(_snapshot(), failure, attempts=1)
    assert short["classification"] == "UNKNOWN"
    assert short["reproduced_failure_count"] == 1
    assert short["control"]["outcome"] == "not_run"

    def control_failure(request):
        if request["kind"] == "control":
            return {"ok": False, "reason_code": "TEX_COMPILE_FAILED"}
        return {"ok": False, "reason_code": "TEX_UNDEFINED_COMMAND"}

    receipt = _probe(_snapshot(), control_failure)
    assert receipt["classification"] == "TEMPLATE_OR_ENVIRONMENT"
    assert receipt["control_succeeded"] is False
    assert receipt["environment"]["reason_codes"] == ["TEX_COMPILE_FAILED"]


def test_ai_signal_without_real_reproduction_remains_unknown():
    snapshot = _snapshot()
    receipt = probe_rendering_risk(
        selection_hash=SELECTION_HASH,
        candidate_id="candidate-1",
        content_hash=_hash(snapshot),
        content_snapshot=snapshot,
    )
    assert receipt["classification"] == "UNKNOWN"
    assert receipt["candidate_attempts"][0]["reason_code"] == "PROBE_NOT_RUN"


def test_different_failure_codes_are_not_stable_content_evidence():
    def compile_callback(request):
        assert request["kind"] == "candidate"
        code = "TEX_BRACE_MISMATCH" if request["attempt"] == 1 else "TEX_UNDEFINED_COMMAND"
        return {"ok": False, "reason_code": code}
    receipt = _probe(_snapshot(), compile_callback)
    assert receipt["classification"] == "UNKNOWN"
    assert receipt["control"]["outcome"] == "not_run"


def test_shared_template_failure_is_environment_not_content():
    def broken_template(_snapshot):
        raise ValueError("missing common template")
    receipt = _probe(_snapshot(), lambda _: pytest.fail("invalid template must not compile"),
                     canonical_template_factory=broken_template)
    assert receipt["classification"] == "TEMPLATE_OR_ENVIRONMENT"
    assert receipt["environment"]["reason_codes"] == ["TEMPLATE_INVALID"]


def test_template_keeps_complete_math_snapshot_without_mutation():
    snapshot = _snapshot(
        "  " + r"若 \(A_{n+1}=\left(\frac{x}{y}\right)^2\)，求 \(A_{n+1}\)。" + "  "
    )
    original = {key: list(value) if isinstance(value, list) else value for key, value in snapshot.items()}
    latex = canonical_single_question_template(snapshot)
    assert snapshot == original
    assert snapshot["content"].strip() in latex
    assert snapshot["conditions"][0] in latex
    assert all(option in latex for option in snapshot["options"])
    assert "图 1（示意）" in latex

    seen_source = []

    def success(request):
        seen_source.append(request["latex_source"])
        return {"ok": True}

    receipt = _probe(snapshot, success)
    assert receipt["classification"] == "UNKNOWN"
    assert seen_source[0] == latex
    assert receipt["content_snapshot_hash"] == _hash(snapshot)


def test_hash_binding_rejects_changed_auxiliary_content():
    snapshot = _snapshot()
    stale_hash = _hash(snapshot)
    snapshot["conditions"].append(r"\(a>0\)")
    with pytest.raises(D2RenderProbeContractError) as raised:
        probe_rendering_risk(
            selection_hash=SELECTION_HASH,
            candidate_id="candidate-1",
            content_hash=stale_hash,
            content_snapshot=snapshot,
        )
    assert raised.value.reason == "content_hash_mismatch"


@patch(
    "cuoti_app.services.exam_generation_d2_render_probe.shutil.which",
    return_value="xelatex",
)
@patch("cuoti_app.services.exam_generation_d2_render_probe.compile_latex_artifact")
def test_production_factory_uses_existing_isolated_compiler_and_cleans_probe_dir(
    compile_artifact, _which
):
    directories = []

    def compile_side_effect(directory, command, stem, latex, **kwargs):
        directories.append(directory)
        assert command == "xelatex"
        assert kwargs == {"timeout": 17, "publish_source": False}
        assert latex.startswith(r"\documentclass[UTF8]{ctexart}")
        if stem.startswith("d2_candidate_"):
            raise TexCompileError("candidate failed", code="TEX_BRACE_MISMATCH")

    compile_artifact.side_effect = compile_side_effect
    callback = make_production_compile_callback(
        xelatex_command="xelatex", timeout_seconds=17
    )
    receipt = _probe(_snapshot(), callback)

    assert receipt["classification"] == "CONTENT"
    assert receipt["environment"]["observed"] == "production"
    assert compile_artifact.call_count == 3
    assert len(set(directories)) == 1
    assert not Path(directories[0]).exists()
