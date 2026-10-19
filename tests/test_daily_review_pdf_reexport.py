"""Historical PDF exports must not become new practice sessions."""

import io
import json
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db, DB_SCHEMA_VERSION
from cuoti_app.services.daily_review import current_round, record_result
from cuoti_app.services.job_contracts import SUPPORTED_JOB_KINDS
from cuoti_app.services.job_handlers import register_app_job_handlers
from cuoti_app.services.jobs import JobCoordinator, unregister_job_handler
from cuoti_app.services.mistake_exports import (
    MistakeExportError, cleanup_exports, create_mistake_export,
)
from cuoti_app.services.tex import TexCompileError


def fake_compile(pdf_dir, _command, stem, _latex, **_kwargs):
    Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\nhistorical fixture")


@pytest.fixture
def book(tmp_path):
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    app = create_app({
        "TESTING": True, "DATABASE": str(tmp_path / "review.db"),
        "PDF_DIR": str(pdf_dir), "REGISTER_JOB_HANDLERS": False,
    })
    with app.app_context():
        db = get_db()
        subject = db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()[0]
        for i in range(3):
            db.execute(
                "INSERT INTO mistakes(public_id,subject_id,qnum,content,note,reasons,date_added,status) "
                "VALUES(?,?,?,?,?,?,?,'未掌握')",
                (uuid.uuid4().hex, subject, str(i + 1), f"原始题目 {i + 1} $x^2$", "原始提示", "概念不清", "2026-09-01"),
            )
        db.commit()
        original, _ = create_mistake_export(
            db, pdf_dir=pdf_dir, xelatex_command="xelatex", compiler=fake_compile,
            payload={"export_context": "daily_review", "selection": {"pdf_purpose": "self_test"}},
        )
        session_id = original["daily_review_round_id"]
        items = db.execute("SELECT id FROM daily_review_items ORDER BY id").fetchall()
        for item in items:
            record_result(db, round_id=session_id, item_id=item[0], result="correct", assistance="none")
    yield app, pdf_dir, original


def practice_state(db):
    return {
        table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY id")]
        for table in ("daily_review_rounds", "daily_review_items", "mistakes")
    }


def reexport(db, pdf_dir, session_id, **kwargs):
    return create_mistake_export(
        db, pdf_dir=pdf_dir, xelatex_command="xelatex",
        payload={"export_context": "daily_review", "daily_review_session_id": session_id,
                 **kwargs.pop("payload", {})}, compiler=kwargs.pop("compiler", fake_compile), **kwargs,
    )


def test_download_names_match_history_headers_and_archive_in_beijing_time(book):
    app, _, original = book
    with app.app_context():
        db = get_db()
        db.execute("UPDATE mistake_export_batches SET created_at='2026-10-15T16:30:00+00:00' WHERE id=?", (original['id'],))
        db.execute("UPDATE mistake_exports SET created_at='2026-10-15T16:30:00+00:00' WHERE batch_id=?", (original['id'],))
        db.commit()
    client = app.test_client()
    history = client.get('/api/v2/mistake-exports').get_json()['items'][0]
    stem = f"每日自主复习_20261016_第{original['round_number']}轮_批次{original['id']}"
    assert history['download_name'] == f'{stem}.zip'
    with client.get(history['download_url']) as response:
        assert response.status_code == 200
        assert f"filename*=UTF-8''{stem}.zip" in unquote(response.headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            for volume in history['volumes']:
                expected = f"{stem}_第{volume['volume_index']}册.pdf"
                assert volume['download_name'] == expected
                assert bundle.read(expected).startswith(b'%PDF')
            assert json.loads(bundle.read('manifest.json'))['download_name'] == history['download_name']
    for volume in history['volumes']:
        with client.get(volume['download_url']) as response:
            assert response.status_code == 200
            assert f"filename*=UTF-8''{volume['download_name']}" in unquote(response.headers['Content-Disposition'])


@pytest.mark.parametrize("layout", ["compact", "notes", "two_column"])
def test_expired_pdf_rebuild_uses_snapshots_and_preserves_completed_session(book, layout):
    app, pdf_dir, original = book
    with app.app_context():
        db = get_db()
        db.execute("UPDATE mistakes SET content='后来改动',note='后来提示',deleted_at='2026-09-25'")
        db.execute("UPDATE mistake_exports SET expires_at='2000-01-01'")
        db.execute("UPDATE mistake_export_batches SET expires_at='2000-01-01'")
        db.commit()
        cleanup_exports(db, pdf_dir)
        assert not list(pdf_dir.glob("*.pdf"))
        before = practice_state(db)
        with patch("cuoti_app.services.mistake_exports.reserve_daily_review", side_effect=AssertionError("must not select")):
            with patch("cuoti_app.services.mistake_exports.compile_latex_artifact", side_effect=fake_compile) as compiler:
                result, replayed = reexport(db, pdf_dir, original["daily_review_round_id"],
                                            payload={"layout": layout}, compiler=compiler)
        assert not replayed
        assert result["is_reexport"] is True
        assert result["source_session_id"] == original["daily_review_round_id"]
        assert result["completion_status"] == "completed"
        assert result["completed_count"] == 3
        assert result["layout"] == layout
        assert [v["content"] for v in result["items"]] == [v["content"] for v in original["items"]]
        latex = compiler.call_args.args[3]
        assert "后来改动" not in latex and "原始题目" in latex
        assert "原始提示" not in latex  # self-test purpose is retained
        assert practice_state(db) == before
        assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
    with app.test_client().get(result["download_url"]) as response:
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.data)) as bundle:
            assert bundle.read(result["volumes"][0]["download_name"]).startswith(b"%PDF")
        assert result["download_name"] != original["download_name"]
        assert f"_第{original['round_number']}轮_" in result["download_name"]
        assert f"_批次{result['id']}.zip" in result["download_name"]


def test_http_submission_worker_and_idempotent_replay(book):
    app, pdf_dir, original = book
    client = app.test_client()
    url = f"/api/v2/daily-review/sessions/{original['daily_review_round_id']}/pdf"
    headers = {"Idempotency-Key": "historical-click-1"}
    first = client.post(url, json={"layout": "notes"}, headers=headers)
    again = client.post(url, json={"layout": "notes"}, headers=headers)
    assert first.status_code == 202
    assert again.status_code == 200
    assert first.get_json()["job_id"] == again.get_json()["job_id"]
    assert again.headers["Idempotency-Replayed"] == "true"
    assert client.post(url, json={"layout": "compact"}, headers=headers).status_code == 409
    register_app_job_handlers(app)
    try:
        coordinator = JobCoordinator(app.config["DATABASE"])
        with patch("cuoti_app.services.mistake_exports.compile_latex_artifact", side_effect=fake_compile) as compiler:
            completed = coordinator.run_once()
            assert completed["status"] == "succeeded", completed
            result = completed["result"]
            with app.app_context():
                before = practice_state(get_db())
                replay, reused = reexport(get_db(), pdf_dir, original["daily_review_round_id"],
                                          payload={"layout": "notes", "_job_id": completed["id"]}, compiler=compiler)
                assert reused and replay["id"] == result["id"]
                assert practice_state(get_db()) == before
            assert compiler.call_count == 1
        assert client.get(f"/api/v2/jobs/{completed['id']}").get_json()["status"] == "succeeded"
        response = client.get(f"/api/v2/jobs/{completed['id']}/result")
        assert response.status_code == 200
        assert response.get_json()["result"]["is_reexport"] is True
        with client.get(result["volumes"][0]["download_url"]) as pdf:
            assert pdf.status_code == 200 and pdf.data.startswith(b"%PDF")
        later = client.post(url, json={"request_id": "historical-click-2"})
        assert later.status_code == 202 and later.get_json()["job_id"] != completed["id"]
    finally:
        for kind in SUPPORTED_JOB_KINDS:
            unregister_job_handler(kind)


@pytest.mark.parametrize("body,headers,status", [
    ({"layout": "unknown"}, {}, 400),
    ({"layout": []}, {}, 400),
    ({"pdf_purpose": "guided"}, {}, 400),
    ({"selection": {}}, {}, 400),
    ({"request_id": "a"}, {"Idempotency-Key": "b"}, 400),
    ({"request_id": "x" * 201}, {}, 400),
])
def test_reexport_request_rejects_invalid_options(book, body, headers, status):
    app, _, original = book
    response = app.test_client().post(
        f"/api/v2/daily-review/sessions/{original['daily_review_round_id']}/pdf", json=body, headers=headers,
    )
    assert response.status_code == status
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0] == 0


@pytest.mark.parametrize("damage,code", [
    ("missing", "ROUND_NOT_FOUND"),
    ("unpublished", "DAILY_REVIEW_CONFLICT"),
    ("deleted_item", "DAILY_REVIEW_SNAPSHOT_UNAVAILABLE"),
    ("missing_snapshot", "DAILY_REVIEW_SNAPSHOT_UNAVAILABLE"),
])
def test_unavailable_history_is_not_silently_reselected(book, damage, code):
    app, pdf_dir, original = book
    session_id = original["daily_review_round_id"]
    with app.app_context():
        db = get_db()
        if damage == "missing":
            session_id += 100
        elif damage == "unpublished":
            db.execute("UPDATE daily_review_rounds SET status='failed'")
        elif damage == "deleted_item":
            db.execute("DELETE FROM daily_review_items WHERE id=(SELECT MIN(id) FROM daily_review_items)")
        else:
            db.execute("UPDATE daily_review_items SET snapshot_json='{}'")
        db.commit()
        before = practice_state(db)
        with pytest.raises(MistakeExportError) as exc:
            reexport(db, pdf_dir, session_id)
        assert exc.value.code == code
        assert practice_state(db) == before
    response = app.test_client().post(f"/api/v2/daily-review/sessions/{session_id}/pdf", json={})
    assert response.status_code == (404 if damage == "missing" else 409)
    assert response.get_json()["code"] == code


class Cancelled(RuntimeError):
    pass


@pytest.mark.parametrize("failure", [TexCompileError("broken compiler"), Cancelled("cancelled")])
def test_failed_or_cancelled_reexport_leaves_original_intact(book, failure):
    app, pdf_dir, original = book
    with app.app_context():
        db = get_db()
        before = practice_state(db)
        files = {p.name: p.read_bytes() for p in pdf_dir.glob("*.pdf")}

        def fail(*args, **kwargs):
            fake_compile(*args, **kwargs)
            raise failure

        with pytest.raises(Cancelled if isinstance(failure, Cancelled) else MistakeExportError):
            reexport(db, pdf_dir, original["daily_review_round_id"], compiler=fail, cancelled_errors=(Cancelled,))
        assert practice_state(db) == before
        assert {p.name: p.read_bytes() for p in pdf_dir.glob("*.pdf")} == files
        assert db.execute("SELECT COUNT(*) FROM mistake_export_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM mistake_exports").fetchone()[0] == 1


def test_reexport_keeps_guided_purpose_and_does_not_replace_latest_session(book):
    app, pdf_dir, original = book
    with app.app_context():
        db = get_db()
        original_id = original["daily_review_round_id"]
        db.execute("UPDATE daily_review_rounds SET selection_json=? WHERE id=?",
                   (json.dumps({"pdf_purpose": "guided"}), original_id))
        db.execute("UPDATE daily_review_items SET snapshot_json=json_set(snapshot_json,'$.legacy_live_content',json('true')) WHERE round_id=?", (original_id,))
        db.commit()
        newer, _ = create_mistake_export(db, pdf_dir=pdf_dir, xelatex_command="xelatex",
                                         payload={"export_context": "daily_review"}, compiler=fake_compile)
        before = practice_state(db)
        with patch("cuoti_app.services.mistake_exports.compile_latex_artifact", side_effect=fake_compile) as compiler:
            result, _ = reexport(db, pdf_dir, original_id, compiler=compiler)
        assert "原始提示" in compiler.call_args.args[3]
        assert result["filter_snapshot"]["pdf_purpose"] == "guided"
        assert result["legacy_live_content"] is True
        assert current_round(db)["id"] == newer["daily_review_round_id"]
        assert practice_state(db) == before


def test_generic_export_payload_cannot_bypass_history_rules(book):
    app, pdf_dir, original = book
    with app.app_context():
        db = get_db()
        before = practice_state(db)
        for overrides in (
            {"daily_review_session_id": True}, {"daily_review_session_id": None},
            {"daily_review_session_id": 1.5}, {"selection": {}},
            {"pdf_purpose": "guided"}, {"export_context": "selected_mistakes"},
        ):
            with pytest.raises(MistakeExportError) as exc:
                reexport(db, pdf_dir, original["daily_review_round_id"], payload=overrides)
            assert exc.value.status_code == 400
        assert practice_state(db) == before


def test_worker_rechecks_snapshot_after_submission(book):
    app, _, original = book
    response = app.test_client().post(
        f"/api/v2/daily-review/sessions/{original['daily_review_round_id']}/pdf", json={},
    )
    assert response.status_code == 202
    with app.app_context():
        db = get_db()
        db.execute("DELETE FROM daily_review_items WHERE id=(SELECT MIN(id) FROM daily_review_items)")
        db.commit()
        before = practice_state(db)
    register_app_job_handlers(app)
    try:
        with patch("cuoti_app.services.mistake_exports.compile_latex_artifact") as compiler:
            completed = JobCoordinator(app.config["DATABASE"]).run_once()
            assert completed["status"] == "failed", completed
            assert completed["error"]["code"].upper() == "DAILY_REVIEW_SNAPSHOT_UNAVAILABLE"
            compiler.assert_not_called()
        with app.app_context():
            assert practice_state(get_db()) == before
    finally:
        for kind in SUPPORTED_JOB_KINDS:
            unregister_job_handler(kind)


def test_failed_export_insert_rolls_back_without_changing_session(book):
    app, pdf_dir, original = book
    with app.app_context():
        db = get_db()
        before = practice_state(db)
        db.execute("CREATE TRIGGER reject_export BEFORE INSERT ON mistake_export_items BEGIN SELECT RAISE(ABORT,'test disk constraint'); END")
        db.commit()
        with pytest.raises(MistakeExportError):
            reexport(db, pdf_dir, original["daily_review_round_id"])
        assert not db.in_transaction
        assert practice_state(db) == before
        assert db.execute("SELECT COUNT(*) FROM mistake_export_batches").fetchone()[0] == 1
