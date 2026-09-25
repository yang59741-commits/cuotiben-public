from pathlib import Path

import pytest

from cuoti_app.services.formal_runtime import (
    FormalRuntimeError,
    build_formal_runtime_overrides,
    resolve_formal_runtime,
)
from cuoti_app.services.job_handlers import stop_job_worker
from cuoti_app.services.release_workers import start_release_workers


def _create_required_inputs(root: Path) -> None:
    (root / "data").mkdir(parents=True)
    (root / "data" / "cuoti.db").touch()
    (root / "instance" / "semantic_index").mkdir(parents=True)
    (root / "instance" / "question_bank.sqlite3").touch()
    (root / "instance" / "semantic_index" / "formal_past_exams.json").touch()


def test_formal_overrides_keep_all_mutable_paths_under_data_runtime(tmp_path):
    _create_required_inputs(tmp_path)

    overrides = build_formal_runtime_overrides(tmp_path)
    runtime = tmp_path.resolve() / "data" / "runtime"

    assert overrides["DATABASE"] == str(tmp_path.resolve() / "data" / "cuoti.db")
    for key, relative in {
        "PDF_DIR": "pdfs",
        "ATTACHMENT_DIR": "attachments",
        "AI_MEMORY_DIR": "memory",
        "JOB_INPUT_DIR": "job_inputs",
        "PAST_EXAM_PROFILE_DIR": "past_exam_profiles",
        "AI_USAGE_PATH": "ai-usage.jsonl",
        "SETTINGS_PATH": "settings.json",
        "CATALOG_PATH": "catalog.json",
    }.items():
        assert overrides[key] == str(runtime / relative)


def test_formal_overrides_reuse_current_reference_assets_and_runner_contract(tmp_path):
    _create_required_inputs(tmp_path)

    overrides = build_formal_runtime_overrides(tmp_path)

    assert overrides["QUESTION_BANK_DATABASE"] == str(
        tmp_path.resolve() / "instance" / "question_bank.sqlite3"
    )
    assert overrides["SEMANTIC_FORMAL_ARTIFACT"] == str(
        tmp_path.resolve()
        / "instance"
        / "semantic_index"
        / "formal_past_exams.json"
    )
    assert overrides["SEMANTIC_HOME_INSTITUTION"] == ""
    assert overrides["SEMANTIC_RECALL_ALGORITHM_VERSION"] == "semantic-recall-v2"
    assert overrides["JOB_WORKER_ENABLED"] is True


def test_missing_formal_database_never_falls_back_to_legacy_root_database(tmp_path):
    (tmp_path / "cuoti.db").touch()
    (tmp_path / "instance" / "semantic_index").mkdir(parents=True)
    (tmp_path / "instance" / "question_bank.sqlite3").touch()
    (tmp_path / "instance" / "semantic_index" / "formal_past_exams.json").touch()

    with pytest.raises(FormalRuntimeError) as caught:
        resolve_formal_runtime(tmp_path)

    assert caught.value.code == "formal_database_missing"
    assert caught.value.path == tmp_path.resolve() / "data" / "cuoti.db"


@pytest.mark.parametrize(
    ("missing", "expected_code"),
    [
        ("question_bank.sqlite3", "question_bank_missing"),
        ("formal_past_exams.json", "formal_semantic_artifact_missing"),
    ],
)
def test_missing_reference_asset_is_reported_explicitly(
    tmp_path, missing, expected_code
):
    _create_required_inputs(tmp_path)
    target = (
        tmp_path / "instance" / missing
        if missing == "question_bank.sqlite3"
        else tmp_path / "instance" / "semantic_index" / missing
    )
    target.unlink()

    with pytest.raises(FormalRuntimeError) as caught:
        resolve_formal_runtime(tmp_path)

    assert caught.value.code == expected_code


def test_release_workers_stop_without_retention_deleting_terminal_jobs(tmp_path):
    from cuoti_app import create_app
    from cuoti_app.database import get_db

    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "fixture.db"),
            "JOB_INPUT_DIR": str(tmp_path / "job_inputs"),
            "JOB_FAST_WORKERS": 1,
            "JOB_HEAVY_WORKERS": 1,
        }
    )
    with app.app_context():
        db = get_db()
        db.execute(
            "INSERT INTO background_jobs("
            "id,kind,status,input_hash,available_at,created_at,updated_at,finished_at"
            ") VALUES(?,?,?,?,?,?,?,?)",
            (
                "historical-terminal-job",
                "test",
                "failed",
                "fixture-input-hash",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
                "2020-01-01T00:00:00+00:00",
            ),
        )
        db.commit()

    start_release_workers(app)
    stop_job_worker(app)

    with app.app_context():
        row = get_db().execute(
            "SELECT status FROM background_jobs WHERE id=?",
            ("historical-terminal-job",),
        ).fetchone()
    assert row is not None
    assert row["status"] == "failed"
