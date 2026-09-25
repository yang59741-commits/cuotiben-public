from __future__ import annotations

from tests.frontend_sources import script_source

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from cuoti_app import create_app
from cuoti_app.database import init_db
from cuoti_app.services.past_exam_profile_availability import (
    _legacy_year_decision, d2_source_mode_status,
    overlay_legacy_source_availability,
)


def test_reviewed_year_reason_uses_bound_evidence_without_gating_availability(tmp_path):
    combination = dict(institution='A', subject='math', reference_year=2025)
    resolved = dict(activation_id='active', entry_revision=1,
                    profile={'provenance': {'evidence_manifest_hash': 'a' * 64,
                                            'learning_years': [2024, 2025]}})
    evidence = tmp_path / ('a' * 64 + '.json')
    with (
        patch('cuoti_app.services.past_exam_profile_availability.profile_availability',
              return_value={'available': True}),
        patch('cuoti_app.services.past_exam_profile_availability.resolve_selectable_profile',
              return_value=resolved),
    ):
        assert _legacy_year_decision({}, {}, combination, tmp_path)['available']
        evidence.write_text(json.dumps({'review': combination | {
            'year_selection_reason': 'Latest source contains only a fragment.'}}), encoding='utf-8')
        result = _legacy_year_decision({}, {}, combination, tmp_path)
        assert result['year_selection_reason'] == 'Latest source contains only a fragment.'
        assert result['learning_years'] == [2024, 2025]
        assert result['available']
        evidence.write_text(json.dumps({'review': combination | {
            'reference_year': 2024, 'year_selection_reason': 'Wrong year'}}), encoding='utf-8')
        result = _legacy_year_decision({}, {}, combination, tmp_path)
        assert result['year_selection_reason'] is None
        assert result['available']


def _write_past_exam(root: Path, institution: str, subject: str) -> None:
    directory = root / institution / subject
    directory.mkdir(parents=True)
    (directory / "2026真题.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        f"\\section*{{{institution}2026年{subject}试卷}}\n"
        "一、计算测试题。\n二、证明测试结论。\n"
        "\\end{document}\n",
        encoding="utf-8",
    )


@pytest.fixture
def app_fixture(tmp_path):
    past_dir = tmp_path / "past"
    _write_past_exam(past_dir, "湖南师范大学", "高等代数")
    _write_past_exam(past_dir, "大连理工大学", "高等代数")
    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "fixture.db"),
            "PAST_EXAM_DIR": str(past_dir),
            "SETTINGS_PATH": str(tmp_path / "settings.json"),
            "JOB_INPUT_DIR": str(tmp_path / "job-input"),
        }
    )
    with app.app_context():
        init_db()
    return app


def test_sources_intersect_tex_with_exact_active_profile(app_fixture):
    registry = object()
    artifacts = object()

    def availability(_registry, _artifacts, combination):
        active = combination["institution"] == "湖南师范大学"
        return {
            "usable": active,
            "available": active,
            "code": None if active else "unsupported_profile_combination",
            "reason": None if active else "missing_active_profile_v3",
            "retryable": False,
            "combination_key": "active-key" if active else "missing-key",
            "profile_id": "ppv3:active" if active else None,
        }

    def resolve(_registry, _artifacts, combination):
        assert combination == {
            "institution": "湖南师范大学",
            "subject": "高等代数",
            "reference_year": 2026,
        }
        return {"activation_id": "activation-active", "entry_revision": 3}

    with (
        patch(
            "cuoti_app.routes.exams_candidates.load_profile_availability_snapshot",
            return_value={"registry": registry, "artifact_map": artifacts},
        ),
        patch(
            "cuoti_app.services.past_exam_profile_availability.profile_availability",
            side_effect=availability,
        ),
        patch(
            "cuoti_app.services.past_exam_profile_availability.resolve_selectable_profile",
            side_effect=resolve,
        ),
    ):
        response = app_fixture.test_client().get("/api/past-exam-sources")

    assert response.status_code == 200, response.get_json()
    by_institution = {
        item["institution"]: item for item in response.get_json()
    }
    active = by_institution["湖南师范大学"]
    inactive = by_institution["大连理工大学"]

    assert active["available_years"] == [2026]
    assert active["available"] is True
    assert active["year_structures"][0]["source_available"] is True
    assert active["year_structures"][0]["profile_selectable"] is True
    assert active["year_structures"][0]["profile_availability"][
        "activation_id"
    ] == "activation-active"
    modes = active["year_structures"][0]["source_modes"]
    assert set(modes) == {"ordinary", "other_past", "mixed"}
    assert all(item["available"] is True for item in modes.values())
    assert all(item["acceptance_status"] == "not_run" for item in modes.values())

    assert inactive["source_available_years"] == [2026]
    assert inactive["available_years"] == []
    assert inactive["available"] is False
    assert inactive["availability_reason"] == "missing_active_profile_v3"
    assert inactive["year_structures"][0]["source_available"] is True
    assert inactive["year_structures"][0]["available"] is False
    assert inactive["year_structures"][0]["availability_reason"] == (
        "missing_active_profile_v3"
    )
    assert all(
        item["available"] is False
        for item in inactive["year_structures"][0]["source_modes"].values()
    )


def test_unsupported_past_submission_keeps_freeze_error_and_creates_no_job(
    app_fixture,
):
    database = app_fixture.config["DATABASE"]
    with sqlite3.connect(database) as db:
        subject_id = db.execute(
            "SELECT id FROM subjects WHERE name='高等代数'"
        ).fetchone()[0]
        before = db.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0]

    provider = "cuoti_app.services.ai.deepseek_api_call"
    with patch(provider) as provider_call:
        response = app_fixture.test_client().post(
            "/api/v2/jobs/exam-generation",
            json={
                "source": "past",
                "subject_id": subject_id,
                "institution": "大连理工大学",
                "reference_year": 2026,
            },
            headers={"Idempotency-Key": "unsupported-dalian-2026"},
        )

    assert response.status_code == 422, response.get_json()
    assert response.get_json()["code"] == "unsupported_profile_combination"
    assert response.get_json()["reason"] == "missing_active_profile_v3"
    assert response.get_json()["retryable"] is False
    provider_call.assert_not_called()
    with sqlite3.connect(database) as db:
        after = db.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0]
    assert after == before


def test_abandoned_past_submission_uses_source_disposition_and_creates_no_job(
    app_fixture,
):
    past_dir = Path(app_fixture.config["PAST_EXAM_DIR"])
    _write_past_exam(past_dir, "测试院校", "高等代数")
    coverage_dir = Path(app_fixture.config["DATABASE"]).parent / "runtime"
    coverage_dir.mkdir(parents=True, exist_ok=True)
    (coverage_dir / "d2-expansion-coverage.json").write_text(
        json.dumps({
            "targets": [{
                "institution": "测试院校",
                "subject": "高等代数",
                "proposed_reference_year": 2026,
                "profile_status": "abandoned",
                "abandonment_reference_year": 2026,
                "profile_abandonment": {
                    "abandonment_reason": "missing_valid_card",
                    "candidate_retention": "preserved",
                },
            }],
        }),
        encoding="utf-8",
    )
    database = app_fixture.config["DATABASE"]
    with sqlite3.connect(database) as db:
        subject_id = db.execute(
            "SELECT id FROM subjects WHERE name='高等代数'"
        ).fetchone()[0]
        before = db.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0]
    response = app_fixture.test_client().post(
        "/api/v2/jobs",
        json={
            "kind": "exam-generation",
            "input": {
                "source": "past",
                "subject_id": subject_id,
                "institution": "测试院校",
                "reference_year": 2026,
                "candidate_source": "mixed",
            },
            "idempotency_key": "abandoned-profile-2026",
        },
    )
    assert response.status_code == 422, response.get_json()
    assert response.get_json()["code"] == "profile_abandoned"
    assert response.get_json()["reason"] == "profile_abandoned"
    with sqlite3.connect(database) as db:
        after = db.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0]
    assert after == before


def test_internal_past_adapter_rejects_inactive_year_before_ai(app_fixture):
    """The retained internal adapter must not bypass the active-v3 gate."""

    database = app_fixture.config["DATABASE"]
    with sqlite3.connect(database) as db:
        subject_id = db.execute(
            "SELECT id FROM subjects WHERE name='高等代数'"
        ).fetchone()[0]

    app_fixture.config.update(TESTING=False, DEEPSEEK_API_KEY="fixture-key")
    decision = {
        "available": False,
        "code": "unsupported_profile_combination",
        "reason": "missing_active_profile_v3",
    }
    with (
        patch(
            "cuoti_app.routes.exams_legacy._require_deepseek_config",
            return_value=None,
        ),
        patch(
            "cuoti_app.routes.exams_legacy.load_profile_availability_snapshot",
            return_value={"registry": object(), "artifact_map": object()},
        ),
        patch(
            "cuoti_app.routes.exams_legacy.profile_availability",
            return_value=decision,
        ),
        patch("cuoti_app.routes.exams_legacy.deepseek_api_call") as provider_call,
    ):
        response = app_fixture.test_client().post(
            "/internal/jobs/generate-exam",
            json={
                "source": "past",
                "subject_id": subject_id,
                "institution": "大连理工大学",
                "reference_year": 2026,
            },
        )

    assert response.status_code == 422, response.get_json()
    assert response.get_json()["code"] == "unsupported_profile_combination"
    assert response.get_json()["reason"] == "missing_active_profile_v3"
    provider_call.assert_not_called()


def test_d2_mode_status_refines_only_the_reported_capacity_conflict():
    coverage = {
        "targets": [{
            "institution": "湖南师范大学", "subject": "数学分析",
            "active_reference_year": 2026,
            "source_modes": {
                "ordinary": {
                    "source_type_preflight": "infeasible",
                    "source_type_witness": {"reason": "source_type_minimum_exceeds_quota"},
                },
                "other_past": {"source_type_preflight": "source_types_passed",
                               "solver_probe": {"status": "not_completed"}},
                "mixed": {"source_type_preflight": "source_types_passed",
                           "solver_probe": {"status": "passed"}},
            },
        }]
    }
    ordinary = d2_source_mode_status(
        coverage, institution="湖南师范大学", subject="数学分析", reference_year=2026,
        mode="ordinary", profile_available=True, source_available=True,
    )
    assert ordinary["available"] is False
    assert ordinary["status"] == "infeasible"
    other = d2_source_mode_status(
        coverage, institution="湖南师范大学", subject="数学分析", reference_year=2026,
        mode="other_past", profile_available=True, source_available=True,
    )
    assert other["available"] is True
    assert other["status"] == "not_completed"
    assert other["acceptance_status"] == "local_preflight"
    mixed = d2_source_mode_status(
        coverage, institution="湖南师范大学", subject="数学分析", reference_year=2026,
        mode="mixed", profile_available=True, source_available=True,
    )
    assert mixed["available"] is True and mixed["acceptance_status"] == "local_preflight"


def test_d2_mode_status_exposes_real_acceptance_over_an_unfinished_probe():
    coverage = {
        "targets": [{
            "institution": "大连理工大学", "subject": "高等代数",
            "active_reference_year": 2026,
            "source_modes": {
                "other_past": {
                    "source_type_preflight": "source_types_passed",
                    "solver_probe": {"status": "not_completed"},
                    "real_acceptance_status": "real_accepted_v4",
                    "real_exam_id": 73,
                    "real_job_id": "job-73",
                },
            },
        }]
    }
    status = d2_source_mode_status(
        coverage, institution="大连理工大学", subject="高等代数", reference_year=2026,
        mode="other_past", profile_available=True, source_available=True,
    )
    assert status["available"] is True
    assert status["status"] == "not_completed"
    assert status["acceptance_status"] == "real_accepted_v4"
    assert status["reason"] is None
    assert status["real_exam_id"] == 73
    assert status["real_job_id"] == "job-73"


def test_abandoned_non_priority_profile_is_explicitly_not_applicable():
    coverage = {
        "targets": [{
            "institution": "A", "subject": "math",
            "proposed_reference_year": 2026,
            "profile_status": "abandoned",
            "profile_abandonment": {
                "abandonment_reason": "missing_valid_card",
                "candidate_retention": "preserved",
            },
        }]
    }
    status = d2_source_mode_status(
        coverage, institution="A", subject="math", reference_year=2026,
        mode="mixed", profile_available=False, source_available=True,
    )
    assert status["available"] is False
    assert status["status"] == "not_applicable"
    assert status["acceptance_status"] == "not_applicable"
    assert status["reason_code"] == "profile_abandoned"
    assert status["profile_abandonment"]["candidate_retention"] == "preserved"

    # A stale disposition can never hide either priority institution.
    priority = d2_source_mode_status(
        coverage | {"targets": [{**coverage["targets"][0], "institution": "湖南师范大学"}]},
        institution="湖南师范大学", subject="math", reference_year=2026,
        mode="mixed", profile_available=False, source_available=True,
    )
    assert priority["status"] == "not_run"
    assert priority["reason"] == "missing_active_profile_v3"


def test_overlay_exposes_abandoned_profile_without_changing_source_cards():
    source = {
        "institution": "A", "subject": "math", "available_years": [2026],
        "year_structures": [{"year": 2026, "available": True, "question_count": 8}],
    }
    coverage = {
        "targets": [{
            "institution": "A", "subject": "math",
            "proposed_reference_year": 2026,
            "profile_status": "abandoned",
            "profile_abandonment": {
                "abandonment_reason": "formal_reference_slot_gap",
                "candidate_retention": "preserved",
            },
        }]
    }
    with patch(
        "cuoti_app.services.past_exam_profile_availability._legacy_year_decision",
        return_value={"available": False, "code": "unsupported_profile_combination",
                      "reason": "missing_active_profile_v3"},
    ):
        result = overlay_legacy_source_availability(source, {}, {}, d2_coverage=coverage)
    structure = result["year_structures"][0]
    assert result["profile_establishment"] == "abandoned"
    assert result["profile_availability"]["code"] == "profile_abandoned"
    assert result["availability_reason"] == "profile_abandoned"
    assert structure["profile_establishment"] == "abandoned"
    assert structure["candidate_retention"] == "preserved"
    assert structure["usable"] is False


def test_abandoned_latest_year_does_not_leak_to_an_older_fallback_year():
    coverage = {
        "targets": [{
            "institution": "A", "subject": "math",
            "latest_source_year": 2026,
            "proposed_reference_year": 2025,
            "active_reference_year": None,
            "profile_status": "abandoned",
            "abandonment_reference_year": 2026,
            "profile_abandonment": {"abandonment_reason": "formal_reference_slot_gap"},
        }]
    }
    latest = d2_source_mode_status(
        coverage, institution="A", subject="math", reference_year=2026,
        mode="mixed", profile_available=False, source_available=True,
    )
    older = d2_source_mode_status(
        coverage, institution="A", subject="math", reference_year=2025,
        mode="mixed", profile_available=False, source_available=True,
    )
    assert latest["reason_code"] == "profile_abandoned"
    assert older["reason"] == "missing_active_profile_v3"


def test_source_card_contract_distinguishes_unavailable_mode_reason():
    """The card must expose mode availability, not only probe acceptance."""
    source = script_source("exam.js")
    assert "modeInfo&&modeInfo.available===false" in source
    assert "不可用：${escapeHTML(String(modeInfo.reason||'该题源方式当前不可用'))}" in source


def test_structure_actual_year_is_not_overwritten_by_latest_active_year():
    source = {
        "institution": "A", "subject": "math", "available_years": [2026, 2025],
        "year_structures": [
            {"year": 2026, "available": True},
            {"year": 2025, "available": True},
        ],
    }

    def decision(_registry, _artifacts, combination, _evidence_directory):
        active = combination["reference_year"] == 2026
        return {"available": active, "reason": None if active else "missing_active_profile_v3"}

    with patch(
        "cuoti_app.services.past_exam_profile_availability._legacy_year_decision",
        side_effect=decision,
    ):
        result = overlay_legacy_source_availability(source, {}, {})

    assert result["actual_reference_year"] == 2026
    assert {
        row["year"]: row["actual_reference_year"]
        for row in result["year_structures"]
    } == {2026: 2026, 2025: 2025}


def test_d2_mode_status_stays_bound_to_each_year_when_profile_uses_fallback():
    source = {
        "institution": "A", "subject": "math", "available_years": [2026, 2025],
        "year_structures": [
            {"year": 2026, "available": True},
            {"year": 2025, "available": True},
        ],
    }
    coverage = {
        "targets": [{
            "institution": "A", "subject": "math",
            "active_reference_year": 2025,
            "source_modes": {
                "ordinary": {
                    "source_type_preflight": "source_types_passed",
                    "solver_probe": {"status": "passed"},
                },
                "other_past": {
                    "source_type_preflight": "source_types_passed",
                    "solver_probe": {"status": "not_completed"},
                },
                "mixed": {
                    "source_type_preflight": "infeasible",
                    "source_type_witness": {
                        "reason": "source_type_minimum_exceeds_quota",
                    },
                },
            },
        }],
    }

    def decision(_registry, _artifacts, combination, _evidence_directory):
        active = combination["reference_year"] == 2025
        return {
            "available": active,
            "reason": None if active else "missing_active_profile_v3",
            "year_selection_reason": "2026 片段，采用 2025 完整结构" if active else None,
        }

    with patch(
        "cuoti_app.services.past_exam_profile_availability._legacy_year_decision",
        side_effect=decision,
    ):
        result = overlay_legacy_source_availability(
            source, {}, {}, d2_coverage=coverage
        )

    by_year = {row["year"]: row for row in result["year_structures"]}
    assert by_year[2026]["actual_reference_year"] == 2026
    assert by_year[2026]["usable"] is False
    assert all(not mode["available"] for mode in by_year[2026]["source_modes"].values())
    assert by_year[2025]["actual_reference_year"] == 2025
    assert by_year[2025]["usable"] is True
    assert by_year[2025]["source_modes"]["ordinary"]["status"] == "passed"
    assert by_year[2025]["source_modes"]["other_past"]["status"] == "not_completed"
    assert by_year[2025]["source_modes"]["mixed"]["available"] is False
    assert by_year[2025]["source_modes"]["mixed"]["reason"] == (
        "当前题源池无法满足该方式的题型与题源配额要求。"
    )
