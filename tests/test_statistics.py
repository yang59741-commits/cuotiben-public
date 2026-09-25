from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import DB_SCHEMA_VERSION, get_db
from cuoti_app.routes.exams_legacy import _store_exam_items


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(root / "data.db"),
            "PDF_DIR": str(root / "pdfs"),
        })
        self.client = self.app.test_client()
        self.today = datetime.now().astimezone().date()
        with self.app.app_context():
            db = get_db()
            self.subject_id = db.execute(
                "SELECT id FROM subjects ORDER BY id LIMIT 1"
            ).fetchone()[0]
            self.chapter_id = db.execute(
                "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
                (self.subject_id,),
            ).fetchone()[0]

    def tearDown(self):
        self.temporary.cleanup()

    def _mistake(self, *, complete=True, status="未掌握", deleted=False):
        with self.app.app_context():
            db = get_db()
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            cursor = db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "date_added,status,history_complete,history_complete_from,deleted_at,"
                "updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    str(uuid.uuid4()), self.subject_id, self.chapter_id, "1", "题目",
                    self.today.isoformat(), status, int(complete),
                    (self.today - timedelta(days=30)).isoformat() if complete else None,
                    now if deleted else None, now,
                ),
            )
            db.commit()
            return cursor.lastrowid

    def _event(self, mistake_id, kind, day_offset=0, status="未掌握", streak=0,
               stage_before=None, stage_after=None, cycle=1):
        with self.app.app_context():
            db = get_db()
            stamp = datetime.combine(
                self.today + timedelta(days=day_offset), datetime.min.time()
            ).astimezone().replace(hour=10).isoformat(timespec="seconds")
            db.execute(
                "INSERT INTO review_events(public_id,mistake_id,event_type,reviewed_at,"
                "created_at,status_after,correct_streak,lapse_count_after,stage_before,"
                "stage_after,cycle_after,schedule_version) VALUES(?,?,?,?,?,?,?,0,?,?,?,?)",
                (str(uuid.uuid4()), mistake_id, kind, stamp, stamp, status, streak,
                 stage_before, stage_after, cycle,
                 "fixed-3-5-10-15-v1" if stage_before is not None else None),
            )
            db.commit()

    def test_formula_completeness_snapshots_and_soft_delete(self):
        normal = self._mistake(status="已掌握")
        incomplete = self._mistake(complete=False)
        self._mistake(status="已掌握", deleted=True)
        self._event(normal, "pass", -3, streak=1, stage_before=0, stage_after=1)
        self._event(normal, "pass", -2, streak=2, stage_before=1, stage_after=2)
        self._event(normal, "pass", -1, streak=3, stage_before=2, stage_after=3)
        self._event(normal, "pass", 0, status="已掌握", streak=4, stage_before=3, stage_after=3)
        self._event(incomplete, "again", 0)

        response = self.client.get(
            "/api/v2/stats",
            query_string={
                "date_from": (self.today - timedelta(days=5)).isoformat(),
                "date_to": self.today.isoformat(),
                "subject_id": self.subject_id,
                "chapter_id": self.chapter_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertEqual(data["summary"]["total"], 2)
        self.assertEqual(data["summary"]["mastered"], 1)
        self.assertEqual(data["summary"]["review_sample"], 2)
        self.assertEqual(data["summary"]["pass"], 1)
        self.assertEqual(data["summary"]["again"], 1)
        self.assertEqual(data["summary"]["average_reviews_to_mastery"], 4)
        self.assertEqual(data["completeness"]["samples"]["known_review_events"], 5)
        self.assertEqual(data["review_cycle"]["normal_mastered"], 1)
        self.assertFalse(data["completeness"]["range_is_complete"])
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], DB_SCHEMA_VERSION)
            snapshot = db.execute(
                "SELECT total_count,mastered_count,estimated FROM learning_daily_snapshots "
                "WHERE snapshot_day=? AND subject_id=? AND chapter_id=?",
                (self.today.isoformat(), self.subject_id, self.chapter_id),
            ).fetchone()
            self.assertEqual(tuple(snapshot), (2, 1, 0))

    def test_manual_events_are_separate_and_forgotten_is_traced(self):
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE statistics_metadata SET complete_since=? WHERE id=1",
                ((self.today - timedelta(days=30)).isoformat(),),
            )
            db.commit()
        mistake = self._mistake()
        self._event(mistake, "pass", -2, streak=1)
        self._event(mistake, "again", -1)
        self._event(mistake, "manual_master", 0, status="已掌握", streak=3)
        data = self.client.get(
            "/api/v2/stats",
            query_string={"date_from": (self.today - timedelta(days=3)).isoformat()},
        ).get_json()
        self.assertEqual(data["summary"]["review_sample"], 2)
        self.assertEqual(data["summary"]["forgotten_mistakes"], 1)
        trend = data["review_trend"]
        self.assertEqual(sum(row["manual_changes"] for row in trend), 1)
        self.assertEqual(sum(row["reviews"] for row in trend), 2)

    def test_only_submitted_exam_attempts_are_counted(self):
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
                "VALUES(?,'统计试卷','{}',? ,?,'published')",
                (self.subject_id, r"\begin{problem}题目\end{problem}", self.today.isoformat()),
            )
            exam_id = cursor.lastrowid
            _store_exam_items(db, exam_id, r"\begin{problem}题目\end{problem}", source="free")
            db.commit()
        draft = self.client.post(f"/api/v2/exams/{exam_id}/attempts", json={}).get_json()
        submitted = self.client.post(f"/api/v2/exams/{exam_id}/attempts", json={}).get_json()
        self.client.patch(
            f"/api/v2/exam-attempts/{submitted['public_id']}",
            json={
                "items": [{"public_id": submitted["items"][0]["public_id"], "result_status": "correct", "score": 8}],
                "max_score": 10, "submit": True,
            },
        )
        data = self.client.get("/api/v2/stats").get_json()["exam_attempts"]
        self.assertEqual(data["submitted_attempts"], 1)
        self.assertEqual(data["answered_items"], 1)
        self.assertEqual(data["accuracy"], 1)
        self.assertEqual(data["average_score_rate"], 0.8)
        self.assertNotIn(draft["public_id"], str(data))

    def test_range_cap_and_reminders(self):
        self._mistake()
        too_wide = self.client.get(
            "/api/v2/stats?date_from=2020-01-01&date_to=2026-01-01"
        )
        self.assertEqual(too_wide.status_code, 400)
        reminder = self.client.get("/api/v2/stats/reminders").get_json()
        self.assertEqual(reminder["due_count"], 1)
        self.assertEqual(reminder["delivery"], "in_app_only")
        dated = self.client.get(
            "/api/v2/stats/reminders",
            query_string={
                "date_from": (self.today - timedelta(days=180)).isoformat(),
                "date_to": (self.today - timedelta(days=150)).isoformat(),
            },
        ).get_json()
        self.assertEqual(dated["due_count"], 1)
        scoped = self.client.get(
            "/api/v2/stats/reminders",
            query_string={
                "subject_id": self.subject_id,
                "chapter_id": self.chapter_id,
            },
        ).get_json()
        self.assertEqual(scoped["due_count"], 1)
        scoped = self.client.get(
            "/api/v2/stats/reminders", query_string={"chapter_id": 999999}
        ).get_json()
        self.assertEqual(scoped["due_count"], 0)

    def test_missing_statistics_metadata_is_reported_as_incomplete(self):
        mistake_id = self._mistake()
        self._event(mistake_id, "pass", 0)
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM statistics_metadata")
            db.commit()
        response = self.client.get("/api/v2/stats")
        self.assertEqual(response.status_code, 200, response.get_json())
        data = response.get_json()
        self.assertIsNone(data["completeness"]["statistics_complete_since"])
        self.assertFalse(data["completeness"]["range_is_complete"])
        self.assertTrue(data["completeness"]["historical_portion_estimated"])
        self.assertEqual(data["summary"]["review_sample"], 0)

    def test_mistake_api_ignores_answer_and_solution_fields(self):
        created = self.client.post(
            "/api/v2/mistakes",
            json={
                "subject_id": self.subject_id,
                "chapter_id": self.chapter_id,
                "qnum": "无答案",
                "content": "只保存题目",
                "date_added": self.today.isoformat(),
                "answer": "不应保存",
                "solution": "也不应保存",
            },
        )
        self.assertEqual(created.status_code, 201, created.get_json())
        self.assertNotIn("answer", created.get_json())
        self.assertNotIn("solution", created.get_json())
        with self.app.app_context():
            row = get_db().execute(
                "SELECT answer,solution FROM mistakes WHERE public_id=?",
                (created.get_json()["public_id"],),
            ).fetchone()
            self.assertEqual(tuple(row), ("", ""))


if __name__ == "__main__":
    unittest.main()
