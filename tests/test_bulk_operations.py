from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db


class BulkOperationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({"TESTING": True, "DATABASE": str(root / "db.sqlite3")})
        self.client = self.app.test_client()
        self.ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        with self.app.app_context():
            db = get_db()
            subject = db.execute("SELECT id FROM subjects WHERE name='数学分析'").fetchone()[0]
            chapter = db.execute("SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1", (subject,)).fetchone()[0]
            self.target_chapter = db.execute("SELECT id FROM chapters WHERE subject_id=? AND id<>? ORDER BY id LIMIT 1", (subject, chapter)).fetchone()[0]
            for position, public_id in enumerate(self.ids):
                db.execute(
                    "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,date_added,status,content,updated_at) "
                    "VALUES(?,?,?,?,?,'未掌握',?,?)",
                    (public_id, subject, chapter, str(position), "2026-08-06", "内容", "2026-08-06T10:00:00+08:00"),
                )
            db.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def _preview(self, operation):
        response = self.client.post("/api/v2/mistakes/bulk/preview", json=operation)
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def test_atomic_operation_and_network_replay_apply_once(self):
        operation = {"action": "move_chapter", "public_ids": self.ids, "payload": {"chapter_id": self.target_chapter}}
        preview = self._preview(operation)
        operation_id = str(uuid.uuid4())
        request = {"operation_id": operation_id, "preview_token": preview["preview_token"], "mode": "atomic", "operation": operation}
        first = self.client.post("/api/v2/mistakes/bulk/commit", json=request)
        self.assertEqual(first.status_code, 200, first.get_json())
        replay = self.client.post("/api/v2/mistakes/bulk/commit", json=request)
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        changed_mode = self.client.post(
            "/api/v2/mistakes/bulk/commit",
            json={**request, "mode": "best_effort"},
        )
        self.assertEqual(changed_mode.status_code, 409)
        self.assertEqual(changed_mode.get_json()["code"], "OPERATION_CONFLICT")
        with self.app.app_context():
            rows = get_db().execute("SELECT chapter_id,revision FROM mistakes ORDER BY id").fetchall()
            self.assertTrue(all(row["chapter_id"] == self.target_chapter for row in rows))
            self.assertTrue(all(row["revision"] == 2 for row in rows))

    def test_atomic_conflict_rolls_back_and_best_effort_reports_retryable(self):
        missing = str(uuid.uuid4())
        operation = {"action": "trash", "public_ids": [self.ids[0], missing], "payload": {}}
        preview = self._preview(operation)
        atomic = self.client.post("/api/v2/mistakes/bulk/commit", json={
            "operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "mode": "atomic", "operation": operation,
        })
        self.assertEqual(atomic.status_code, 409)
        with self.app.app_context():
            self.assertIsNone(get_db().execute("SELECT deleted_at FROM mistakes WHERE public_id=?", (self.ids[0],)).fetchone()[0])
        preview = self._preview(operation)
        best = self.client.post("/api/v2/mistakes/bulk/commit", json={
            "operation_id": str(uuid.uuid4()), "preview_token": preview["preview_token"],
            "mode": "best_effort", "operation": operation,
        })
        self.assertEqual(best.status_code, 200, best.get_json())
        self.assertEqual(best.get_json()["succeeded"], [self.ids[0]])
        self.assertEqual(best.get_json()["retryable"], [missing])


if __name__ == "__main__":
    unittest.main()
