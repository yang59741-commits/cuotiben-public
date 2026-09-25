from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.routes.exams_legacy import _store_exam_items
from cuoti_app.validation import mistake_fingerprint


class ExamAttemptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(root / "data.db"),
            "PDF_DIR": str(root / "pdfs"),
        })
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute(
                "SELECT id FROM subjects WHERE name='数学分析'"
            ).fetchone()[0]
            self.chapter_id, self.chapter_name = db.execute(
                "SELECT id,name FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
                (subject_id,),
            ).fetchone()
            cursor = db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
                "VALUES(?,'无答案作答测试','{}',?,'2026-08-06 10:00','published')",
                (
                    subject_id,
                    r"\begin{problem}作答题目快照\end{problem}",
                ),
            )
            self.exam_id = cursor.lastrowid
            db.execute(
                "UPDATE exams SET config=? WHERE id=?",
                (f'{{"chapters":[{self.chapter_id}]}}', self.exam_id),
            )
            _store_exam_items(
                db,
                self.exam_id,
                r"\begin{problem}作答题目快照\end{problem}",
                source="free",
            )
            db.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def test_detail_adds_native_display_text_without_rewriting_frozen_content(self):
        original = '37．（清华大学，2002 年）设 A=0。'
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE exam_items SET content=? WHERE exam_id=?',
                       (original, self.exam_id))
            db.commit()
            for kind in ('ordinary', 'past_exam_local', 'past_exam_other', 'free', 'mistakes'):
                db.execute('UPDATE exam_items SET source_kind=? WHERE exam_id=?',
                           (kind, self.exam_id))
                db.commit()
                before = tuple(db.execute('SELECT * FROM exam_items WHERE exam_id=?',
                                          (self.exam_id,)).fetchone())
                response = self.client.get(f'/api/v2/exams/{self.exam_id}')
                self.assertEqual(response.status_code, 200)
                item = response.get_json()['items'][0]
                self.assertEqual(item['content'], original)
                if kind in {'ordinary', 'past_exam_local', 'past_exam_other'}:
                    self.assertEqual(item['display_content'], original.removeprefix('37．'))
                else:
                    self.assertNotIn('display_content', item)
                after = tuple(db.execute('SELECT * FROM exam_items WHERE exam_id=?',
                                         (self.exam_id,)).fetchone())
                self.assertEqual(after, before)

    def test_native_attempt_create_read_and_list_share_display_only_cleanup(self):
        from cuoti_app.services.reviewed_source_labels import REVIEWED_SOURCE_LABELS

        candidate_id, (prefix, stem) = next(iter(REVIEWED_SOURCE_LABELS.items()))
        original = prefix + stem + ' END'
        with self.app.app_context():
            db = get_db()
            db.execute('UPDATE exam_items SET source_kind=?,source_candidate_id=?,content=? WHERE exam_id=?',
                       ('ordinary', candidate_id, original, self.exam_id))
            db.commit()
            before = tuple(db.execute('SELECT * FROM exam_items WHERE exam_id=?',
                                      (self.exam_id,)).fetchone())
        created = self.client.post(f'/api/v2/exams/{self.exam_id}/attempts', json={})
        self.assertEqual(created.status_code, 201)
        attempt = created.get_json()
        read = self.client.get(f"/api/v2/exam-attempts/{attempt['public_id']}").get_json()
        listed = self.client.get(f'/api/v2/exams/{self.exam_id}/attempts').get_json()['items'][0]
        for payload in (attempt, read, listed):
            self.assertEqual(payload['items'][0]['content'], original)
            self.assertEqual(payload['items'][0]['display_content'], stem + ' END')
        with self.app.app_context():
            after = tuple(get_db().execute('SELECT * FROM exam_items WHERE exam_id=?',
                                          (self.exam_id,)).fetchone())
            self.assertEqual(after, before)

    def test_multiple_attempts_manual_scoring_and_idempotent_return(self):
        first = self.client.post(f"/api/v2/exams/{self.exam_id}/attempts", json={})
        second = self.client.post(f"/api/v2/exams/{self.exam_id}/attempts", json={})
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(second.status_code, 201, second.get_json())
        self.assertNotEqual(first.get_json()["public_id"], second.get_json()["public_id"])
        attempt = first.get_json()
        attempt_item = attempt["items"][0]
        submitted = self.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={
                "items": [{
                    "public_id": attempt_item["public_id"],
                    "result_status": "incorrect",
                    "score": 2,
                    "comment": "手工批注",
                }],
                "max_score": 10,
                "duration_seconds": 120,
                "submit": True,
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.get_json())
        self.assertEqual(submitted.get_json()["status"], "submitted")
        self.assertEqual(submitted.get_json()["total_score"], 2)
        self.assertNotIn("answer", str(submitted.get_json()).lower())
        rejected_edit = self.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}", json={"note": "late"}
        )
        self.assertEqual(rejected_edit.status_code, 409)

        returned = self.client.post(
            f"/api/v2/exam-attempt-items/{attempt_item['public_id']}/return-mistake"
        )
        replay = self.client.post(
            f"/api/v2/exam-attempt-items/{attempt_item['public_id']}/return-mistake"
        )
        self.assertEqual(returned.status_code, 201, returned.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        self.assertEqual(
            returned.get_json()["mistake_public_id"],
            replay.get_json()["mistake_public_id"],
        )
        with self.app.app_context():
            db = get_db()
            mistakes = db.execute(
                "SELECT public_id,content,source_kind,source_ref,chapter_id,qnum,"
                "date_added,fingerprint FROM mistakes "
                "WHERE source_exam_attempt_item_id IS NOT NULL"
            ).fetchall()
            self.assertEqual(len(mistakes), 1)
            self.assertEqual(mistakes[0]["content"], "作答题目快照")
            self.assertEqual(mistakes[0]["source_kind"], "exam_attempt")
            self.assertEqual(mistakes[0]["chapter_id"], self.chapter_id)
            self.assertEqual(
                mistakes[0]["fingerprint"],
                mistake_fingerprint(
                    "数学分析", self.chapter_name, mistakes[0]["qnum"],
                    mistakes[0]["content"], mistakes[0]["date_added"],
                ),
            )
            for table in ("exam_attempts", "exam_attempt_items"):
                columns = {
                    row[1] for row in db.execute(f"PRAGMA table_info({table})")
                }
                self.assertNotIn("answer", columns)
                self.assertNotIn("solution", columns)

    def test_only_submitted_incorrect_item_can_return(self):
        attempt = self.client.post(
            f"/api/v2/exams/{self.exam_id}/attempts", json={}
        ).get_json()
        item_id = attempt["items"][0]["public_id"]
        response = self.client.post(
            f"/api/v2/exam-attempt-items/{item_id}/return-mistake"
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("先提交", response.get_json()["error"])

    def test_attempts_can_be_deleted_and_returned_mistakes_are_preserved(self):
        draft = self.client.post(
            f"/api/v2/exams/{self.exam_id}/attempts", json={}
        ).get_json()
        deleted = self.client.delete(f"/api/v2/exam-attempts/{draft['public_id']}")
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        self.assertTrue(deleted.get_json()["ok"])
        self.assertEqual(
            self.client.get(f"/api/v2/exam-attempts/{draft['public_id']}").status_code,
            404,
        )

        attempt = self.client.post(
            f"/api/v2/exams/{self.exam_id}/attempts", json={}
        ).get_json()
        item = attempt["items"][0]
        submitted = self.client.patch(
            f"/api/v2/exam-attempts/{attempt['public_id']}",
            json={
                "items": [{
                    "public_id": item["public_id"],
                    "result_status": "incorrect",
                    "score": 0,
                    "comment": "",
                }],
                "max_score": 10,
                "submit": True,
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.get_json())
        returned = self.client.post(
            f"/api/v2/exam-attempt-items/{item['public_id']}/return-mistake"
        )
        mistake_id = returned.get_json()["mistake_public_id"]
        deleted = self.client.delete(
            f"/api/v2/exam-attempts/{attempt['public_id']}"
        )
        self.assertEqual(deleted.status_code, 200, deleted.get_json())
        with self.app.app_context():
            row = get_db().execute(
                "SELECT source_exam_attempt_item_id FROM mistakes WHERE public_id=?",
                (mistake_id,),
            ).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["source_exam_attempt_item_id"])

    def test_delete_attempt_validates_identifier_and_missing_record(self):
        self.assertEqual(
            self.client.delete("/api/v2/exam-attempts/not-a-uuid").status_code,
            400,
        )
        self.assertEqual(
            self.client.delete(
                "/api/v2/exam-attempts/00000000-0000-0000-0000-000000000000"
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
