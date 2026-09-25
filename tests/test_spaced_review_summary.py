"""Spaced-review counts do not require the full statistics dashboard."""
from datetime import date, timedelta
from uuid import uuid4

from cuoti_app import create_app
from cuoti_app.database import get_db


def test_due_summary_scope_pagination_and_saved_results(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "fixture.db"),
                      "PDF_DIR": str(tmp_path / "pdfs")})
    today = date.today().isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()
    with app.app_context():
        db = get_db()
        chapters = db.execute("SELECT id,subject_id FROM chapters ORDER BY id LIMIT 2").fetchall()
        chapter, other = chapters
        identities = [str(uuid4()) for _ in range(7)]
        cases = [
            (chapter[0], "未掌握", today, None, 3),
            (chapter[0], "未掌握", today, None, 0),
            (chapter[0], "未掌握", today, None, 0),
            (chapter[0], "未掌握", future, None, 0),
            (chapter[0], "已掌握", None, None, 3),
            (chapter[0], "未掌握", today, today, 0),
            (other[0], "未掌握", today, None, 0),
        ]
        for uid, (cid, status, due, deleted, stage) in zip(identities, cases):
            db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                       "date_added,status,next_review,deleted_at,review_stage) "
                       "VALUES(?,?,?,'synthetic','synthetic',?,?,?,?,?)",
                       (uid, chapter[1], cid, today, status, due, deleted, stage))
        db.commit()
    client = app.test_client()
    scope = {"subject_id": chapter[1], "chapter_id": chapter[0], "limit": 2}
    first = client.get("/api/v2/reviews/due", query_string=scope).get_json()
    assert first["total"] == 3
    assert first["summary"] == {"active": 4, "mastered": 1}
    assert first["has_more"]
    second = client.get("/api/v2/reviews/due", query_string={**scope, "after": first["next_cursor"]}).get_json()
    assert second["total"] == 3 and second["summary"] == first["summary"]
    assert len(second["items"]) == 1
    for index, (result, assistance) in enumerate([("pass", "none"), ("again", "none"), ("pass", "hint")]):
        event_id = str(uuid4())
        body = {"revision": 1, "result": result, "assistance": assistance, "client_event_id": event_id}
        saved = client.post(f"/api/v2/mistakes/{identities[index]}/review", json=body)
        assert saved.status_code == 200
        replay = client.post(f"/api/v2/mistakes/{identities[index]}/review", json=body)
        assert replay.status_code == 200 and replay.get_json()["replayed"]
        page = client.get("/api/v2/reviews/due", query_string=scope).get_json()
        assert page["total"] == 2 - index
        assert page["summary"] == {"active": 3, "mastered": 2}
    empty = client.get("/api/v2/reviews/due", query_string={"subject_id": 999999}).get_json()
    assert empty["total"] == 0 and empty["summary"] == {"active": 0, "mastered": 0}


def test_due_snapshot_count_does_not_absorb_later_insert(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "fixture.db"),
                      "PDF_DIR": str(tmp_path / "pdfs")})
    with app.app_context():
        db = get_db()
        for i in range(3):
            db.execute("INSERT INTO mistakes(qnum,content,date_added,status,next_review) "
                       "VALUES(?,'synthetic','2026-01-01','未掌握','2026-01-01')", (str(i),))
        db.commit()
    client = app.test_client()
    first = client.get('/api/v2/reviews/due?limit=1').get_json()
    with app.app_context():
        db = get_db()
        db.execute("INSERT INTO mistakes(qnum,content,date_added,status,next_review) "
                   "VALUES('later','synthetic','2026-01-01','未掌握','2026-01-01')")
        db.commit()
    page = client.get('/api/v2/reviews/due', query_string={"limit": 1, "after": first['next_cursor']}).get_json()
    assert page['total'] == 3
    assert page['summary'] == {'active': 4, 'mastered': 0}
