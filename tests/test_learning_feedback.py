from __future__ import annotations

import hashlib
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.routes.exams_legacy import _store_exam_items


class LearningFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({"TESTING": True, "DATABASE": str(root / "data.db"),
                               "PDF_DIR": str(root / "pdfs")})
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            self.subject_id = int(db.execute(
                "SELECT id FROM subjects WHERE name='数学分析'"
            ).fetchone()[0])
            chapters = db.execute(
                "SELECT id,name FROM chapters WHERE subject_id=? ORDER BY id LIMIT 2",
                (self.subject_id,),
            ).fetchall()
            self.chapter_a = (int(chapters[0]["id"]), str(chapters[0]["name"]))
            self.chapter_b = (int(chapters[1]["id"]), str(chapters[1]["name"]))

    def tearDown(self):
        self.temporary.cleanup()

    def _exam(self, structured):
        body = "\n".join(
            rf"\begin{{problem}}题目 {index}\end{{problem}}"
            for index in range(1, len(structured) + 1)
        )
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
                "VALUES(?,'反馈测试','{}',?,?,'published')",
                (self.subject_id, body, datetime.now().astimezone().isoformat()),
            )
            exam_id = int(cursor.lastrowid)
            _store_exam_items(db, exam_id, body, source="free", structured_items=structured)
            db.commit()
        return exam_id

    def test_chapter_scope_counts_items_once_and_hides_paper_scope(self):
        exam_id = self._exam([
            {"position": 1, "chapter": self.chapter_a[1]},
            {"position": 2, "chapter": self.chapter_b[1]},
        ])
        attempt = self.client.post(f"/api/v2/exams/{exam_id}/attempts", json={}).get_json()
        response = self.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={"items": [
                {"public_id": attempt["items"][0]["public_id"], "result_status": "correct", "score": 8},
                {"public_id": attempt["items"][1]["public_id"], "result_status": "incorrect", "score": 2},
            ], "max_score": 10, "submit": True},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["items"][0]["assistance"], "unknown")
        stats = self.client.get("/api/v2/stats", query_string={
            "subject_id": self.subject_id, "chapter_id": self.chapter_a[0]
        }).get_json()["exam_attempts"]
        self.assertEqual(stats["submitted_attempts"], 1)
        self.assertEqual(stats["attempts_with_scoped_answers"], 1)
        self.assertEqual(stats["answered_items"], 1)
        self.assertEqual(stats["accuracy"], 1)
        self.assertFalse(stats["paper_scope_chapter_applicable"])
        self.assertEqual(stats["average_score_rate"], 1)

    def test_multi_lineage_is_one_multi_chapter_item(self):
        exam_id = self._exam([{"position": 1}])
        with self.app.app_context():
            db = get_db()
            exam_item_id = int(db.execute(
                "SELECT id FROM exam_items WHERE exam_id=?", (exam_id,)
            ).fetchone()[0])
            now = datetime.now().astimezone().isoformat()
            public_ids = []
            for chapter_id in (self.chapter_a[0], self.chapter_b[0]):
                public_id = str(uuid.uuid4())
                public_ids.append(public_id)
                db.execute(
                    "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (public_id, self.subject_id, chapter_id, "1", "来源题", now[:10], now),
                )
            digest = hashlib.sha256(b"lineage").hexdigest()
            for order, public_id in enumerate(public_ids, start=1):
                db.execute(
                    "INSERT INTO exam_item_mistake_lineage(exam_item_id,source_order,mistake_public_id,"
                    "source_lineage_hash,lineage_policy_version,content_fingerprint,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (exam_item_id, order, public_id, digest, "test-v1", digest, now),
                )
            db.commit()
        attempt = self.client.post(f"/api/v2/exams/{exam_id}/attempts", json={}).get_json()
        self.client.patch(f"/api/v2/exam-attempts/{attempt['public_id']}", json={
            "items": [{"public_id": attempt["items"][0]["public_id"],
                       "result_status": "incorrect", "score": 0}],
            "max_score": 1, "submit": True,
        })
        stats = self.client.get("/api/v2/stats").get_json()["exam_attempts"]
        self.assertEqual(stats["answered_items"], 1)
        multi = [row for row in stats["chapter_error_rates"] if row["chapter"] == "多章节"]
        self.assertEqual(len(multi), 1)
        self.assertEqual(multi[0]["answered"], 1)

    def test_return_copies_selected_feedback_and_replay_does_not_rewrite(self):
        exam_id = self._exam([{"position": 1, "chapter": self.chapter_a[1]}])
        attempt = self.client.post(f"/api/v2/exams/{exam_id}/attempts", json={}).get_json()
        item = attempt["items"][0]
        self.client.patch(f"/api/v2/exam-attempts/{attempt['public_id']}", json={
            "items": [{"public_id": item["public_id"], "result_status": "incorrect",
                       "assistance": "none", "score": 0, "comment": "先检查定义域"}],
            "max_score": 1, "submit": True,
        })
        returned = self.client.post(
            f"/api/v2/exam-attempt-items/{item['public_id']}/return-mistake",
            json={"chapter_id": self.chapter_b[0], "reasons": ["审题失误"],
                  "copy_comment": True},
        )
        saved = self.client.get(f"/api/v2/exam-attempts/{attempt['public_id']}").get_json()
        self.assertEqual(saved["items"][0]["assistance"], "none")
        self.assertEqual(returned.status_code, 201, returned.get_json())
        mistake_public_id = returned.get_json()["mistake_public_id"]
        replay = self.client.post(
            f"/api/v2/exam-attempt-items/{item['public_id']}/return-mistake",
            json={"chapter_id": self.chapter_a[0], "reasons": ["计算错误"],
                  "copy_comment": False},
        )
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        with self.app.app_context():
            db = get_db()
            row = db.execute(
                "SELECT id,chapter_id,note,reasons FROM mistakes WHERE public_id=?",
                (mistake_public_id,),
            ).fetchone()
            self.assertEqual((row["chapter_id"], row["note"], row["reasons"]),
                             (self.chapter_b[0], "先检查定义域", "审题失误"))
            reasons = db.execute(
                "SELECT reason_code FROM mistake_reason_items WHERE mistake_id=?",
                (row["id"],),
            ).fetchall()
            self.assertEqual([value[0] for value in reasons], ["审题失误"])


if __name__ == "__main__":
    unittest.main()
