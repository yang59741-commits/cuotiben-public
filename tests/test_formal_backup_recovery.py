import hashlib
import io
import json
import sqlite3
import socket
import subprocess
import sys
import urllib.request
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image

from cuoti_app import create_app
from cuoti_app.database import get_db, DB_SCHEMA_VERSION
from cuoti_app.routes.exams_legacy import _store_exam_items
from cuoti_app.services.formal_runtime import build_formal_runtime_overrides, database_service_guard, FormalRuntimeError
from tests.test_learning_evidence import session
from tools.create_completion_backup import create_backup, write_manifest
from tools.restore_completion_backup import restore_snapshot, verify_snapshot


@pytest.fixture
def formal_project(tmp_path):
    root = tmp_path / "project"
    runtime = root / "data/runtime"
    runtime.mkdir(parents=True)
    (root / "instance/semantic_index").mkdir(parents=True)
    (root / "data/cuoti.db").touch()
    with closing(sqlite3.connect(root / "instance/question_bank.sqlite3")) as db:
        db.execute("CREATE TABLE fixture_reference(id INTEGER PRIMARY KEY)")
        db.commit()
    (root / "instance/semantic_index/formal_past_exams.json").write_text("{}")
    (runtime / "settings.json").write_text(json.dumps({"ai": {"api_key": "fixture-secret"}}))
    (runtime / "d2-expansion-coverage.json").write_text('{"fixture":"coverage"}')
    (runtime / "profile_expansion_evidence").mkdir()
    (runtime / "profile_expansion_evidence/example.json").write_text('{"fixture":"evidence"}')
    (root / "app.py").write_text("# fixture source\n")
    config = build_formal_runtime_overrides(root)
    app = create_app({**config, "TESTING": True})
    created = app.test_client().post("/api/v2/mistakes", json={
        "subject_id": 1, "chapter_id": 1, "qnum": "Q1", "content": "x=1", "date_added": "2026-09-23"
    })
    assert created.status_code == 201
    with app.app_context():
        db = get_db()
        mistake = dict(db.execute("SELECT * FROM mistakes").fetchone())
    response = app.test_client().post(
        f"/api/v2/mistakes/{mistake['public_id']}/review",
        json={"revision": mistake["revision"], "client_event_id": str(uuid.uuid4()), "result": "again"},
    )
    assert response.status_code == 200
    with app.app_context():
        db = get_db()
        session(db)
        body = r"\begin{problem}x=1\end{problem}"
        pdf = b"%PDF-1.7\nfixture"
        cursor = db.execute(
            "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status,"
            "latex_source,latex_sha256,pdf_sha256,recompile_available) VALUES(1,'fixture','{}',?,'2026-09-23','published',?,?,?,1)",
            (body, body, hashlib.sha256(body.encode()).hexdigest(), hashlib.sha256(pdf).hexdigest()),
        )
        eid = cursor.lastrowid
        _store_exam_items(db, eid, body, source="free", structured_items=[{"position": 1, "type": "证明题", "difficulty": "中等"}])
        Path(config["PDF_DIR"]).mkdir(exist_ok=True)
        (Path(config["PDF_DIR"]) / f"exam_{eid}.pdf").write_bytes(pdf)
        (Path(config["PDF_DIR"]) / f"exam_{eid}.tex").write_text(body)
        image = io.BytesIO()
        Image.new("RGB", (1, 1), "white").save(image, format="PNG")
        attachment = image.getvalue()
        Path(config["ATTACHMENT_DIR"]).mkdir(exist_ok=True)
        (Path(config["ATTACHMENT_DIR"]) / "sample.png").write_bytes(attachment)
        db.execute(
            "INSERT INTO attachments(public_id,mistake_id,object_key,mime_type,extension,byte_size,width,height,sha256,sort_order,status,created_at) "
            "VALUES(?,?,'sample.png','image/png','.png',?,1,1,?,0,'published','2026-09-23')",
            (str(uuid.uuid4()), mistake["id"], len(attachment), hashlib.sha256(attachment).hexdigest()),
        )
        db.commit()
    return root


def business_rows(database):
    with closing(sqlite3.connect(database)) as db:
        return {name: db.execute(f"SELECT * FROM {name} ORDER BY id").fetchall() for name in (
            "mistakes", "review_events", "daily_review_rounds", "daily_review_items", "exams", "exam_items", "attachments"
        )}


@pytest.mark.parametrize("kind", ["private-full", "private-no-secrets"])
def test_current_schema_formal_snapshot_restores_current_layout_and_history(formal_project, tmp_path, kind):
    root = formal_project
    before = business_rows(root / "data/cuoti.db")
    snapshot = create_backup(root, snapshot_kind=kind, include_material_understanding=False)
    assert not (snapshot / "cuoti.db").exists()
    assert not list(snapshot.rglob("*.service.lock"))
    result = restore_snapshot(snapshot, tmp_path / "restored")
    assert result["runtime_layout"] == "formal"
    assert result["sqlite"]["database"]["user_version"] == DB_SCHEMA_VERSION
    restored = tmp_path / "restored"
    assert business_rows(restored / "data/cuoti.db") == before
    assert (restored / "data/runtime/d2-expansion-coverage.json").read_bytes() == (root / "data/runtime/d2-expansion-coverage.json").read_bytes()
    assert (restored / "data/runtime/profile_expansion_evidence/example.json").exists()
    settings = restored / "data/runtime/settings.json"
    if kind == "private-full":
        assert settings.read_bytes() == (root / "data/runtime/settings.json").read_bytes()
    else:
        assert not settings.exists()
        assert "fixture-secret" not in (restored / "data/runtime/settings.sanitized.json").read_text()
    app = create_app({**build_formal_runtime_overrides(restored), "TESTING": True})
    client = app.test_client()
    assert client.get("/api/subjects").status_code == 200
    mistakes = client.get("/api/v2/mistakes")
    assert mistakes.status_code == 200
    assert len(mistakes.get_json()["items"]) == len(before["mistakes"])
    assert mistakes.get_json()["items"][0]["content"] == "x=1"
    assert client.get("/api/v2/exams/1").status_code == 200
    response = client.get("/pdfs/exam_1.pdf")
    assert response.status_code == 200
    response.close()
    assert business_rows(restored / "data/cuoti.db") == before


def test_formal_backup_refuses_running_database_before_copy(formal_project):
    with database_service_guard(formal_project / "data/cuoti.db"), patch("tools.create_completion_backup.copy_project_files") as copy:
        with pytest.raises(FormalRuntimeError, match="database_service_already_running"):
            create_backup(formal_project)
        copy.assert_not_called()


def test_default_formal_layout_never_falls_back_to_old_root_database(tmp_path):
    (tmp_path / "cuoti.db").touch()
    with pytest.raises(FormalRuntimeError, match="formal_database_missing"):
        create_backup(tmp_path)


@pytest.mark.parametrize("version", [1, 2])
def test_old_snapshot_without_layout_metadata_remains_readable(tmp_path, version):
    from tests.test_completion_backup import make_database
    database = tmp_path / "legacy.db"
    make_database(database)
    snapshot = create_backup(tmp_path, config={"DATABASE": str(database)}, layout="legacy")
    payload = json.loads((snapshot / "SNAPSHOT.json").read_text())
    payload["schema_version"] = version
    payload.pop("runtime_layout")
    (snapshot / "SNAPSHOT.json").write_text(json.dumps(payload))
    write_manifest(snapshot)
    assert verify_snapshot(snapshot)["runtime_layout"] == "legacy"
    assert restore_snapshot(snapshot, tmp_path / "restored")["sqlite"]["database"]["user_version"] == 6


def test_source_server_uses_fixture_and_holds_shared_lock(formal_project):
    import app as desktop

    root = formal_project
    (root / "data/runtime/settings.json").write_text(json.dumps({"ai": {
        "active_profile": "deepseek", "profiles": {"deepseek": {
            "provider": "openai_compatible", "base_url": "https://api.example.test/v1/chat/completions",
            "quality_model": "deepseek-flash", "fast_model": "deepseek-flash",
            "credential_ref": "fixture-no-credential",
        }}
    }}))
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    command = [sys.executable, "-X", "utf8", "-B", "tools/run_q1c2_release_app.py",
               "--database", str(root / "data/cuoti.db"), "--runtime-root", str(root / "data/runtime"),
               "--source-settings", str(root / "data/runtime/settings.json"),
               "--source-catalog", str(root / "data/runtime/catalog.json"),
               "--question-bank", str(root / "instance/question_bank.sqlite3"),
               "--formal-artifact", str(root / "instance/semantic_index/formal_past_exams.json"),
               "--port", str(port), "--no-browser"]
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        line = process.stdout.readline()
        if not line:
            raise AssertionError(process.stderr.read())
        started = json.loads(line)
        assert started["status"] == "started"
        with urllib.request.urlopen(started["url"] + "/api/subjects", timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())[0]["name"] == "数学分析"
        with patch.object(desktop, "DATA_DIR", root), patch.object(desktop, "get_application") as create:
            with pytest.raises(FormalRuntimeError, match="database_service_already_running"):
                desktop.run(open_browser=False)
            create.assert_not_called()
    finally:
        process.terminate()
        process.communicate(timeout=10)
    with database_service_guard(root / "data/cuoti.db"):
        pass
