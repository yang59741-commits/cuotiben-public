from datetime import datetime
import sqlite3

import pytest

from cuoti_app.database import get_db
from cuoti_app.routes import attempts as attempt_routes
from cuoti_app.services import attempts as attempt_service
from cuoti_app.services.attempts import AttemptError
from cuoti_app.services.exam_item_repair import inspect_missing_past_exam_items
from cuoti_app.services.statistics import (
    cached_statistics,
    invalidate_statistics_cache,
    parse_scope,
)
from tests import test_exam_attempts as attempt_fixtures


def _fixture():
    fixture = attempt_fixtures.ExamAttemptTests(methodName="runTest")
    fixture.setUp()
    with fixture.app.app_context():
        db = get_db()
        assert "deleted_at" in {
            str(row[1]) for row in db.execute("PRAGMA table_info(exams)").fetchall()
        }
    return fixture


def _set_deleted(fixture, value):
    with fixture.app.app_context():
        db = get_db()
        db.execute("UPDATE exams SET deleted_at=? WHERE id=?", (value, fixture.exam_id))
        db.commit()


def test_attempt_routes_hide_trashed_exam_and_restore_existing_attempt():
    fixture = _fixture()
    try:
        created = fixture.client.post(
            f"/api/v2/exams/{fixture.exam_id}/attempts", json={}
        )
        assert created.status_code == 201
        attempt = created.get_json()
        item = attempt["items"][0]
        submitted = fixture.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={
                "items": [{
                    "public_id": item["public_id"],
                    "result_status": "incorrect",
                    "score": 0,
                }],
                "max_score": 10,
                "submit": True,
            },
        )
        assert submitted.status_code == 200

        _set_deleted(fixture, "2026-09-06T12:00:00+00:00")
        assert fixture.client.get(
            f"/api/v2/exams/{fixture.exam_id}/attempts"
        ).status_code == 404
        assert fixture.client.post(
            f"/api/v2/exams/{fixture.exam_id}/attempts", json={}
        ).status_code == 404
        for method in (fixture.client.get, fixture.client.patch, fixture.client.delete):
            kwargs = {"json": {"note": "hidden"}} if method == fixture.client.patch else {}
            response = method(
                f"/api/v2/exam-attempts/{attempt['public_id']}", **kwargs
            )
            assert response.status_code == 404
        assert fixture.client.post(
            f"/api/v2/exam-attempt-items/{item['public_id']}/return-mistake"
        ).status_code == 404

        with fixture.app.app_context():
            db = get_db()
            assert db.execute(
                "SELECT status FROM exam_attempts WHERE public_id=?",
                (attempt["public_id"],),
            ).fetchone()[0] == "submitted"
            assert db.execute(
                "SELECT COUNT(*) FROM mistakes WHERE source_exam_attempt_item_id IS NOT NULL"
            ).fetchone()[0] == 0

        _set_deleted(fixture, None)
        assert fixture.client.get(
            f"/api/v2/exam-attempts/{attempt['public_id']}"
        ).status_code == 200
        assert fixture.client.post(
            f"/api/v2/exam-attempt-items/{item['public_id']}/return-mistake"
        ).status_code == 201
    finally:
        fixture.tearDown()


def test_attempt_service_entry_points_reject_trashed_exam_then_restore():
    fixture = _fixture()
    try:
        with fixture.app.app_context():
            db = get_db()
            attempt_id, _ = attempt_service.create_or_resume_attempt(
                db,
                fixture.exam_id,
                note="",
                idempotency_key=None,
                timestamp="2026-09-06T12:00:00+00:00",
            )
            row = db.execute(
                "SELECT public_id FROM exam_attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            attempt_public_id = row[0]
            item_public_id = db.execute(
                "SELECT public_id FROM exam_attempt_items WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()[0]
            db.execute(
                "UPDATE exam_attempts SET status='submitted' WHERE id=?", (attempt_id,)
            )
            db.execute(
                "UPDATE exam_attempt_items SET result_status='incorrect' WHERE attempt_id=?",
                (attempt_id,),
            )
            db.execute(
                "UPDATE exams SET deleted_at='2026-09-06T12:01:00+00:00' WHERE id=?",
                (fixture.exam_id,),
            )
            db.commit()

            assert not attempt_service.published_exam_exists(db, fixture.exam_id)
            assert attempt_service.find_attempt(db, attempt_public_id) is None
            assert attempt_service.find_attempt_by_id(db, attempt_id) is None
            with pytest.raises(AttemptError) as caught:
                attempt_service.list_attempts(
                    db, fixture.exam_id, limit=20, after=None, include_items=True
                )
            assert caught.value.code == "NOT_FOUND"
            for callback in (
                lambda: attempt_service.create_or_resume_attempt(
                    db,
                    fixture.exam_id,
                    note="",
                    idempotency_key=None,
                    timestamp="2026-09-06T12:02:00+00:00",
                ),
                lambda: attempt_service.update_attempt(
                    db, attempt_public_id, {}, timestamp="2026-09-06T12:02:00+00:00"
                ),
                lambda: attempt_service.return_attempt_item(
                    db, item_public_id, now=datetime.fromisoformat("2026-09-06T12:02:00+00:00")
                ),
                lambda: attempt_service.delete_attempt(db, attempt_id),
            ):
                with pytest.raises(AttemptError) as caught:
                    callback()
                assert caught.value.code == "NOT_FOUND"
            assert db.execute(
                "SELECT COUNT(*) FROM exam_attempts WHERE id=?", (attempt_id,)
            ).fetchone()[0] == 1

            db.execute("UPDATE exams SET deleted_at=NULL WHERE id=?", (fixture.exam_id,))
            db.commit()
            assert attempt_service.published_exam_exists(db, fixture.exam_id)
            assert attempt_service.find_attempt(db, attempt_public_id) is not None
    finally:
        fixture.tearDown()


def test_statistics_and_item_repair_exclude_trashed_exam_then_restore():
    fixture = _fixture()
    try:
        invalidate_statistics_cache()
        attempt = fixture.client.post(
            f"/api/v2/exams/{fixture.exam_id}/attempts", json={}
        ).get_json()
        fixture.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={
                "items": [{
                    "public_id": attempt["items"][0]["public_id"],
                    "result_status": "correct",
                    "score": 1,
                }],
                "max_score": 1,
                "submit": True,
            },
        )
        assert fixture.client.get("/api/v2/stats").get_json()["exam_attempts"][
            "submitted_attempts"
        ] == 1

        with fixture.app.app_context():
            db = get_db()
            scope = parse_scope({})
            assert cached_statistics(db, scope)["exam_attempts"][
                "submitted_attempts"
            ] == 1
            repair_exam_id = db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
                "VALUES(?,'repair','{\"source\":\"past\",\"question_count\":1}',"
                "'\\begin{problem}repair\\end{problem}','2026-09-06','published')",
                (db.execute("SELECT subject_id FROM exams WHERE id=?", (fixture.exam_id,)).fetchone()[0],),
            ).lastrowid
            db.commit()
            assert [row["exam_id"] for row in inspect_missing_past_exam_items(
                db, exam_ids=[repair_exam_id]
            )] == [repair_exam_id]

            db.execute(
                "UPDATE exams SET deleted_at='2026-09-06T12:03:00+00:00' "
                "WHERE id IN (?,?)", (fixture.exam_id, repair_exam_id),
            )
            db.commit()
            assert inspect_missing_past_exam_items(db, exam_ids=[repair_exam_id]) == []
            assert cached_statistics(db, scope)["exam_attempts"][
                "submitted_attempts"
            ] == 0

        assert fixture.client.get("/api/v2/stats").get_json()["exam_attempts"][
            "submitted_attempts"
        ] == 0

        with fixture.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE exams SET deleted_at=NULL WHERE id IN (?,?)",
                (fixture.exam_id, repair_exam_id),
            )
            db.commit()
            assert [row["exam_id"] for row in inspect_missing_past_exam_items(
                db, exam_ids=[repair_exam_id]
            )] == [repair_exam_id]
            assert cached_statistics(db, scope)["exam_attempts"][
                "submitted_attempts"
            ] == 1
        assert fixture.client.get("/api/v2/stats").get_json()["exam_attempts"][
            "submitted_attempts"
        ] == 1
    finally:
        fixture.tearDown()


def test_attempt_route_rechecks_active_exam_after_acquiring_write_lock(monkeypatch):
    fixture = _fixture()
    other = sqlite3.connect(fixture.app.config["DATABASE"])
    original_begin = attempt_routes.begin_immediate

    def delete_then_begin(db):
        other.execute(
            "UPDATE exams SET deleted_at='2026-09-06T12:04:00+00:00' WHERE id=?",
            (fixture.exam_id,),
        )
        other.commit()
        original_begin(db)

    try:
        monkeypatch.setattr(attempt_routes, "begin_immediate", delete_then_begin)
        response = fixture.client.post(
            f"/api/v2/exams/{fixture.exam_id}/attempts", json={"note": "must not persist"}
        )
        assert response.status_code == 404
        with fixture.app.app_context():
            assert get_db().execute(
                "SELECT COUNT(*) FROM exam_attempts WHERE exam_id=?", (fixture.exam_id,)
            ).fetchone()[0] == 0

        other.execute("UPDATE exams SET deleted_at=NULL WHERE id=?", (fixture.exam_id,))
        other.commit()
        monkeypatch.setattr(attempt_routes, "begin_immediate", original_begin)
        attempt = fixture.client.post(
            f"/api/v2/exams/{fixture.exam_id}/attempts", json={"note": "original"}
        ).get_json()

        monkeypatch.setattr(attempt_routes, "begin_immediate", delete_then_begin)
        response = fixture.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={"note": "must not persist"},
        )
        assert response.status_code == 404
        with fixture.app.app_context():
            assert get_db().execute(
                "SELECT note FROM exam_attempts WHERE public_id=?",
                (attempt["public_id"],),
            ).fetchone()[0] == "original"

        other.execute("UPDATE exams SET deleted_at=NULL WHERE id=?", (fixture.exam_id,))
        other.commit()
        monkeypatch.setattr(attempt_routes, "begin_immediate", original_begin)
        submitted = fixture.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={
                "items": [{
                    "public_id": attempt["items"][0]["public_id"],
                    "result_status": "incorrect",
                    "score": 0,
                }],
                "max_score": 1,
                "submit": True,
            },
        )
        assert submitted.status_code == 200

        monkeypatch.setattr(attempt_routes, "begin_immediate", delete_then_begin)
        response = fixture.client.post(
            f"/api/v2/exam-attempt-items/{attempt['items'][0]['public_id']}/return-mistake"
        )
        assert response.status_code == 404
        with fixture.app.app_context():
            db = get_db()
            assert db.execute(
                "SELECT COUNT(*) FROM mistakes WHERE source_exam_attempt_item_id IS NOT NULL"
            ).fetchone()[0] == 0

        other.execute("UPDATE exams SET deleted_at=NULL WHERE id=?", (fixture.exam_id,))
        other.commit()
        monkeypatch.setattr(attempt_routes, "begin_immediate", original_begin)
        assert fixture.client.get(
            f"/api/v2/exam-attempts/{attempt['public_id']}"
        ).status_code == 200
    finally:
        other.close()
        fixture.tearDown()


def test_statistics_read_does_not_commit_callers_transaction():
    fixture = _fixture()
    try:
        invalidate_statistics_cache()
        with fixture.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE subjects SET name=? WHERE id=(SELECT MIN(id) FROM subjects)",
                ("uncommitted",),
            )
            assert db.in_transaction
            cached_statistics(db, parse_scope({}))
            assert db.in_transaction
            db.rollback()
            assert db.execute(
                "SELECT name FROM subjects WHERE id=(SELECT MIN(id) FROM subjects)"
            ).fetchone()[0] != "uncommitted"
    finally:
        fixture.tearDown()
