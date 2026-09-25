from __future__ import annotations

from tests.frontend_sources import script_source

from pathlib import Path
from types import SimpleNamespace

import pytest

from cuoti_app.services import exam_generation_d2_runtime as d2_runtime
from cuoti_app.services import exam_generation_past_v3_dependencies as dependencies
from cuoti_app.services.exam_generation_past_v3_dependencies import (
    PastV3ProductionAuthorities,
    build_past_v3_production_dependencies,
)
from cuoti_app.services.exam_generation_request import freeze_generation_request
from cuoti_app.services.exam_generation_request_binding import (
    attach_generation_request_identity,
    generation_request_hash,
    verify_generation_request_identity,
)
from cuoti_app.services.exam_generation_stage7_quality import Stage7Budget


ROOT = Path(__file__).resolve().parents[1]


def _freeze(payload):
    return freeze_generation_request(
        payload, generation_job_id="job", request_id=None
    )


def test_missing_and_explicit_false_remain_disabled_without_mutating_old_identity():
    old_payload = {"source": "past", "subject_id": 1}
    old_hash = generation_request_hash(old_payload)

    missing = _freeze(old_payload)
    explicit_false = _freeze({**old_payload, "priority_once": False})

    assert missing.priority_once is False
    assert explicit_false.priority_once is False
    assert "priority_once" not in missing.pending_memory_request["request"]
    assert explicit_false.pending_memory_request["request"]["priority_once"] is False
    assert generation_request_hash(old_payload) == old_hash


def test_true_priority_once_is_bound_by_the_strict_request_hash():
    base = {"source": "past", "subject_id": 1}
    enabled = {**base, "priority_once": True}

    assert _freeze(enabled).priority_once is True
    assert generation_request_hash(enabled) != generation_request_hash(base)
    stamped = attach_generation_request_identity(enabled)
    assert verify_generation_request_identity(stamped) == stamped[
        "generation_request_hash"
    ]


@pytest.mark.parametrize("value", [None, 0, 1, "true", [], {}])
def test_priority_once_rejects_non_boolean_values(value):
    with pytest.raises(ValueError, match="一次性优先选项格式无效"):
        _freeze({"source": "past", "priority_once": value})


@pytest.mark.parametrize("source", ["free", "mistakes"])
def test_priority_once_true_is_past_only(source):
    with pytest.raises(ValueError, match="一次性优先仅支持真题组卷"):
        _freeze({"source": source, "priority_once": True})


def test_normal_past_ui_only_submits_the_opt_in_when_checked():
    from tests.frontend_sources import template_source
    template = template_source()
    script = script_source("exam.js")

    assert 'type="checkbox" id="ep-priority-once"' in template
    assert "必须包含一次性优先题" in template
    assert "priority_once:false" not in script
    assert "if(document.getElementById('ep-priority-once').checked)" in script
    assert "payload.priority_once=true" in script


@pytest.mark.parametrize(
    ("remaining", "expected_limit"), [(3, 2), (1, 0), (0, 0), (-2, 0)]
)
def test_production_authorities_forward_opt_in_with_shared_structural_budget(
    monkeypatch, remaining, expected_limit
):
    contract = object()
    monkeypatch.setattr(
        dependencies,
        "persist_stage3_generation_contract",
        lambda *_args, **_kwargs: SimpleNamespace(contract=contract),
    )
    captured = {}

    def fake_run(_db, **kwargs):
        captured.update(kwargs)
        return "done"

    monkeypatch.setattr(d2_runtime, "run_d2_generation", fake_run)
    authorities = PastV3ProductionAuthorities(
        profile_context=object(),
        authority_registry_loader=lambda **_kwargs: {},
        recent_usage_snapshot_loader=lambda **_kwargs: {},
        score_callback=None,
        score_prompt_version=None,
        score_model_id=None,
        quality_callback=lambda *_args, **_kwargs: None,
        subject_id=1,
        title="test",
        artifact_dir=ROOT,
        artifact_callback=lambda *_args, **_kwargs: None,
        stage7_budget=Stage7Budget(structural_calls_remaining=remaining),
        priority_once=True,
    )
    built = build_past_v3_production_dependencies(authorities)

    assert built.d2_runner is not None
    assert built.d2_runner(
        object(), job_id="job", worker_id="worker", lease_attempt=1
    ) == "done"
    assert captured["priority_once"] is True
    assert captured["priority_reselection_limit"] == expected_limit
