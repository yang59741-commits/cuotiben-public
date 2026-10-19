"""Current staged imports must recover the committed business fact."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cuoti_app.database import get_db
from cuoti_app.services.jobs import JobCoordinator, WorkerStopRequested, JobCancelled, DeferredJobError
from cuoti_app.services.job_results import load_business_result
from tests.test_review_fixes import app


def stage_import(app, mode="merge", *, max_attempts=3):
    public_id = str(uuid.uuid4())
    raw = json.dumps({"format": "cuoti-portable-backup", "format_version": 3,
        "mistakes": [{"public_id": public_id, "subject": "数学分析", "chapter": "引论",
            "qnum": "staged-recovery", "content": "synthetic import", "date_added": "2026-09-20",
            "status": "未掌握", "history_complete": True, "reviews": [], "attachments": []}]}).encode()
    client = app.test_client()
    preview = client.post(f"/api/v3/import/preview?mode={mode}", data=raw)
    assert preview.status_code == 200, preview.json
    token = preview.json["preview_token"]
    staged = client.post(f"/api/v3/import/stage?mode={mode}", data=raw,
                         headers={"X-Import-Preview-Token": token})
    assert staged.status_code == 200, staged.json
    submitted = client.post("/api/v2/jobs/data-import", json={"input": {
        **staged.json, "preview_token": token, "mode": mode}, "max_attempts": max_attempts},
        headers={"Idempotency-Key": str(uuid.uuid4())})
    assert submitted.status_code == 202, submitted.json
    return submitted.json["job_id"], public_id, Path(app.config["JOB_INPUT_DIR"]) / staged.json["staged_file"]


@pytest.mark.parametrize("mode", ["merge", "replace"])
@pytest.mark.parametrize("boundary", ["restart", "missing_input", "cancel", "exhausted"])
def test_staged_commit_survives_interruption(app, mode, boundary):
    job_id, public_id, staged_path = stage_import(app, mode, max_attempts=1 if boundary == "exhausted" else 3)
    clock = [datetime.now(timezone.utc)]
    coordinator = JobCoordinator(app.config["DATABASE"], lease_seconds=1,
        clock=lambda: clock[0], staged_input_roots=(app.config["JOB_INPUT_DIR"],))
    original = coordinator.handlers["data-import"]

    def interrupted(context, payload):
        original(context, payload)
        raise WorkerStopRequested()

    coordinator.register_handler("data-import", interrupted)
    assert coordinator.run_once()["status"] == "running"
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM mistakes WHERE public_id=?", (public_id,)).fetchone()[0] == 1
        assert load_business_result(db, job_id, "data-import")["inserted"] == 1
    if boundary == "missing_input":
        staged_path.unlink(missing_ok=True)
    if boundary == "cancel":
        coordinator.request_cancel(job_id)
    coordinator.register_handler("data-import", original)
    clock[0] += timedelta(seconds=3)
    coordinator.run_once()
    done = coordinator.get_job(job_id)
    assert done["status"] == "succeeded", done
    assert done["result"]["inserted"] == 1
    assert not staged_path.exists()
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM mistakes WHERE public_id=?", (public_id,)).fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM job_business_results WHERE job_id=?", (job_id,)).fetchone()[0] == 1
    if mode == "replace":
        assert len(list((Path(app.config["DATABASE"]).parent / "备份").glob("导入替换前自动备份_*"))) == 1


def test_staged_import_rolls_back_when_receipt_cannot_be_saved(app, monkeypatch):
    from cuoti_app.services import portable_import_commit as service
    job_id, public_id, _ = stage_import(app)

    def fail(*args):
        raise RuntimeError("receipt write interrupted")

    monkeypatch.setattr(service, "store_business_result", fail)
    coordinator = JobCoordinator(app.config["DATABASE"], staged_input_roots=(app.config["JOB_INPUT_DIR"],))
    assert coordinator.run_once()["status"] == "failed"
    with app.app_context():
        db = get_db()
        assert not db.execute("SELECT 1 FROM mistakes WHERE public_id=?", (public_id,)).fetchone()
        assert load_business_result(db, job_id, "data-import") is None
        assert db.execute("SELECT used_at FROM import_previews").fetchone()[0] is None


@pytest.mark.parametrize('error', [RuntimeError('after commit'), JobCancelled(), DeferredJobError()])
def test_committed_import_wins_over_late_handler_failure(app, error):
    job_id, public_id, staged_path = stage_import(app)
    coordinator = JobCoordinator(app.config['DATABASE'], staged_input_roots=(app.config['JOB_INPUT_DIR'],))
    original = coordinator.handlers['data-import']
    def fail_after_commit(context, payload):
        original(context, payload)
        raise error
    coordinator.register_handler('data-import', fail_after_commit)
    result = coordinator.run_once()
    assert result['id'] == job_id and result['status'] == 'succeeded', result
    assert result['result']['inserted'] == 1 and not staged_path.exists()
    with app.app_context():
        assert get_db().execute('SELECT COUNT(*) FROM mistakes WHERE public_id=?', (public_id,)).fetchone()[0] == 1


def test_same_second_edit_invalidates_preview_below_max_revision(app, monkeypatch):
    from cuoti_app.routes import mistakes
    client = app.test_client()
    def add(qnum):
        response = client.post("/api/v2/mistakes", json={"subject_id": 1, "chapter_id": 1,
            "qnum": qnum, "content": qnum, "date_added": "2026-09-20", "note": "local"})
        assert response.status_code == 201, response.json
        return response.json
    high, item = add("high"), add("target")
    fixed = datetime.now().replace(microsecond=0)
    class Clock:
        @staticmethod
        def now(): return fixed
    monkeypatch.setattr(mistakes, "datetime", Clock)
    with app.app_context():
        db = get_db()
        db.execute("UPDATE mistakes SET revision=100 WHERE public_id=?", (high["public_id"],))
        db.commit()
    url = f"/api/v2/mistakes/{item['public_id']}"
    assert client.patch(url, json={"revision": 1, "note": "before"}).status_code == 200
    raw = json.dumps({"format": "cuoti-portable-backup", "format_version": 3, "mistakes": [{
        "public_id": item["public_id"], "subject": "数学分析", "chapter": "引论",
        "qnum": "target", "content": "target", "date_added": "2026-09-20",
        "status": "未掌握", "note": "backup", "reviews": [], "attachments": []}]}).encode()
    preview = client.post("/api/v3/import/preview", data=raw)
    assert preview.status_code == 200, preview.json
    assert client.patch(url, json={"revision": 2, "note": "new user edit"}).status_code == 200
    commit = client.post("/api/v3/import/commit", data=raw, headers={
        "X-Import-Preview-Token": preview.json["preview_token"],
        "X-Import-Resolutions": json.dumps({"1": ["note"]})})
    assert commit.status_code == 409, commit.json
    assert commit.json["code"] == "DATABASE_CHANGED"
    with app.app_context():
        assert get_db().execute("SELECT note FROM mistakes WHERE public_id=?", (item["public_id"],)).fetchone()[0] == "new user edit"
