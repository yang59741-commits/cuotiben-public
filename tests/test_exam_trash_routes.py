from pathlib import Path

import pytest

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.routes.exams_legacy import _store_exam_items


@pytest.fixture
def app(tmp_path):
    return create_app({
        "TESTING": True, "DATABASE": str(tmp_path / "fixture.db"),
        "PDF_DIR": str(tmp_path / "pdfs"),
    })


def insert_exam(app, *, status="published"):
    with app.app_context():
        db = get_db()
        subject_id = db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()[0]
        body = r"\begin{problem}Find $x$ in $x+1=2$.\end{problem}"
        exam_id = db.execute(
            "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
            "VALUES(?,'Trash fixture','{}',?,'2026-09-06',?)",
            (subject_id, body, status),
        ).lastrowid
        _store_exam_items(db, exam_id, body, source="free")
        db.commit()
    for suffix, content in (("pdf", b"%PDF-fixture"), ("tex", body.encode())):
        (Path(app.config["PDF_DIR"]) / f"exam_{exam_id}.{suffix}").write_bytes(content)
    return exam_id


def test_trash_hides_public_routes_and_restore_keeps_original_rows_and_files(app):
    client = app.test_client()
    exam_id = insert_exam(app)
    detail = client.get(f"/api/v2/exams/{exam_id}").get_json()
    explanation = client.get(f"/api/v2/exams/{exam_id}/selection-explanation").get_json()
    before_files = {
        p.name: p.read_bytes() for p in Path(app.config["PDF_DIR"]).glob(f"exam_{exam_id}.*")
    }
    assert client.delete(f"/api/v2/exams/{exam_id}").get_json() == {
        "ok": True, "exam_id": exam_id,
    }
    assert client.get("/api/v2/exams").get_json()["items"] == []
    assert client.get("/internal/legacy/exams").get_json() == []
    for path in (f"/api/v2/exams/{exam_id}", f"/pdfs/exam_{exam_id}.pdf",
                 f"/api/v2/exams/{exam_id}/selection-explanation",
                 f"/api/v2/exams/{exam_id}/attempts"):
        assert client.get(path).status_code == 404, path
    trash = client.get("/api/v2/trash/exams").get_json()
    assert trash["total"] == 1
    assert trash["items"][0]["id"] == exam_id
    assert trash["items"][0]["deleted_at"]
    assert "content" not in trash["items"][0]
    with app.app_context():
        db = get_db()
        assert db.execute("SELECT COUNT(*) FROM exam_items WHERE exam_id=?", (exam_id,)).fetchone()[0] == 1
        assert db.execute("SELECT artifact_status FROM exams WHERE id=?", (exam_id,)).fetchone()[0] == "published"
    assert client.delete(f"/api/v2/exams/{exam_id}").status_code == 404
    assert client.post(f"/api/v2/trash/exams/{exam_id}/restore").get_json() == {
        "ok": True, "exam_id": exam_id,
    }
    assert client.post(f"/api/v2/trash/exams/{exam_id}/restore").status_code == 404
    assert client.get("/api/v2/trash/exams").get_json()["items"] == []
    assert client.get(f"/api/v2/exams/{exam_id}").get_json() == detail
    assert client.get(f"/api/v2/exams/{exam_id}/selection-explanation").get_json() == explanation
    response = client.get(f"/pdfs/exam_{exam_id}.pdf")
    assert response.status_code == 200
    response.close()
    assert before_files == {
        p.name: p.read_bytes() for p in Path(app.config["PDF_DIR"]).glob(f"exam_{exam_id}.*")
    }


def test_trash_pagination_is_separate_from_active_and_never_exposes_reservations(app):
    client = app.test_client()
    ids = [insert_exam(app) for _ in range(3)]
    reservation = insert_exam(app, status="publishing")
    assert client.delete(f"/api/v2/exams/{reservation}").status_code == 404
    assert client.post(f"/api/v2/trash/exams/{reservation}/restore").status_code == 404
    active_cursor = client.get("/api/v2/exams?limit=1").get_json()["next_cursor"]
    for exam_id in ids:
        assert client.delete(f"/api/v2/exams/{exam_id}").status_code == 200
    first = client.get("/api/v2/trash/exams?limit=1").get_json()
    assert first["total"] == 3 and first["has_more"]
    assert first["items"][0]["id"] == ids[-1]
    second = client.get("/api/v2/trash/exams", query_string={
        "limit": 1, "after": first["next_cursor"],
    }).get_json()
    assert second["items"][0]["id"] == ids[-2]
    for path, cursor in (("/api/v2/exams", first["next_cursor"]),
                         ("/api/v2/trash/exams", active_cursor)):
        assert client.get(path, query_string={"after": cursor}).status_code == 400
    assert client.get("/api/v2/trash/exams?subject_id=-1").status_code == 400
    assert client.get("/api/v2/trash/exams?date_from=invalid").status_code == 400
