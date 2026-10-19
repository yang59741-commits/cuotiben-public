from __future__ import annotations

import io
import json
import random
import sqlite3
import tempfile
import threading
import unittest
import uuid
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.database import connect_database, get_db
from cuoti_app.services.daily_review import (
    attach_export_batch,
    choose_mistakes,
    publish_daily_review,
    record_result,
    recommendations,
    reserve_daily_review,
)
from cuoti_app.services.mistake_exports import cleanup_exports, create_mistake_export
from cuoti_app.services.job_contracts import SUPPORTED_JOB_KINDS
from cuoti_app.services.job_handlers import register_app_job_handlers
from cuoti_app.services.jobs import JobCoordinator, unregister_job_handler
from cuoti_app.services.tex import TexCompileError


class SyntheticCancellation(RuntimeError):
    pass


class DailyReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "daily-review.db"
        self.pdf_dir = self.root / "pdfs"
        self.pdf_dir.mkdir()
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.database),
                "PDF_DIR": str(self.pdf_dir),
                "XELATEX_COMMAND": "xelatex",
                "REGISTER_JOB_HANDLERS": False,
            }
        )
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            subjects = {
                row["name"]: int(row["id"])
                for row in db.execute(
                    "SELECT id,name FROM subjects WHERE name IN ('数学分析','高等代数')"
                )
            }
            self.math_id = subjects["数学分析"]
            self.algebra_id = subjects["高等代数"]
            self.chapters = {
                subject_id: int(
                    db.execute(
                        "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
                        (subject_id,),
                    ).fetchone()["id"]
                )
                for subject_id in (self.math_id, self.algebra_id)
            }

    def tearDown(self):
        self.temporary.cleanup()

    def add_mistake(
        self,
        index: int,
        *,
        subject_id: int | None = None,
        date_added: str | None = None,
        status: str = "未掌握",
        deleted: bool = False,
    ) -> int:
        subject_id = subject_id or (
            self.math_id if index % 2 == 0 else self.algebra_id
        )
        date_added = date_added or f"2026-01-{index + 1:02d}"
        with self.app.app_context():
            db = get_db()
            mistake_id = db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "reasons,note,date_added,status,correct_streak,lapse_count,next_review,"
                "updated_at,deleted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    uuid.uuid4().hex,
                    subject_id,
                    self.chapters[subject_id],
                    f"DR-{index + 1}",
                    f"每日复习题目正文 {index + 1}",
                    "概念不清",
                    f"备注 {index + 1}",
                    date_added,
                    status,
                    3 if status == "已掌握" else 0,
                    index % 3,
                    "2026-09-20",
                    "2026-09-14T12:00:00+08:00",
                    "2026-09-14T13:00:00+08:00" if deleted else None,
                ),
            ).lastrowid
            db.commit()
            return int(mistake_id)

    def add_export_batch(self, db) -> int:
        return int(
            db.execute(
                "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
                "total_count,volume_count,layout,filter_snapshot_json,artifact_status,"
                "export_context) VALUES(?,?,?,1,1,'compact','{}','published','daily_review')",
                (
                    uuid.uuid4().hex,
                    "2026-09-14T00:00:00+00:00",
                    # Keep the fixture unexpired regardless of the run date.
                    (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
                ),
            ).lastrowid
        )

    def publish_plan(self, db, plan) -> int:
        batch_id = self.add_export_batch(db)
        attach_export_batch(db, plan.round_id, batch_id)
        publish_daily_review(db, plan.round_id, batch_id)
        db.commit()
        return batch_id

    def fake_compile(self, pdf_dir, _command, stem, _latex, **_kwargs):
        Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\ndaily review")

    def queued_export_job_id(self) -> str:
        job, _created = JobCoordinator(self.database).submit(
            "mistake-export",
            {"export_context": "daily_review"},
            idempotency_key=uuid.uuid4().hex,
        )
        return str(job["id"])

    def seed(self, count: int, *, start: int = 0) -> list[int]:
        return [self.add_mistake(start + index) for index in range(count)]

    def test_first_round_is_earliest_mixed_and_includes_mastered(self):
        deleted_id = self.add_mistake(
            99,
            subject_id=self.math_id,
            date_added="2010-01-01",
            deleted=True,
        )
        expected = []
        for index in range(12):
            expected.append(
                self.add_mistake(
                    index,
                    date_added=f"2020-01-{index + 1:02d}",
                    status="已掌握" if index == 0 else "未掌握",
                )
            )
        with self.app.app_context():
            plan = reserve_daily_review(
                get_db(), count=5, rng=random.Random(41)
            )
            self.assertEqual(plan.round_number, 1)
            self.assertEqual(plan.generation_mode, "earliest")
            self.assertEqual(list(plan.mistake_ids), expected[:5])
            self.assertNotIn(deleted_id, plan.mistake_ids)
            self.assertIn(expected[0], plan.mistake_ids)
            self.assertEqual(
                {row["subject"] for row in plan.rows},
                {"数学分析", "高等代数"},
            )

    def test_same_date_order_is_randomized_and_size_stays_five_to_ten(self):
        rows = [
            {
                "id": index,
                "date_added": "2026-09-14",
                "subject": "数学分析" if index % 2 else "高等代数",
            }
            for index in range(1, 26)
        ]
        first = choose_mistakes(rows, generation_mode="earliest", rng=random.Random(1))
        second = choose_mistakes(rows, generation_mode="earliest", rng=random.Random(2))
        self.assertGreaterEqual(len(first), 5)
        self.assertLessEqual(len(first), 10)
        self.assertNotEqual([row["id"] for row in first], [row["id"] for row in second])
        self.assertEqual({row["subject"] for row in first}, {"数学分析", "高等代数"})

    def test_new_mistakes_finish_current_round_before_random_next_round(self):
        original = self.seed(10)
        with self.app.app_context():
            db = get_db()
            first = reserve_daily_review(db, count=5, rng=random.Random(3))
            self.publish_plan(db, first)
        added = self.add_mistake(50, date_added="2026-09-14")
        with self.app.app_context():
            db = get_db()
            second = reserve_daily_review(db, count=10, rng=random.Random(4))
            self.assertEqual(second.round_number, 1)
            self.assertIn(added, second.mistake_ids)
            self.publish_plan(db, second)
            covered = db.execute(
                "SELECT mistake_id FROM daily_review_items WHERE round_number=1"
            ).fetchall()
            self.assertEqual({int(row[0]) for row in covered}, {*original, added})
            third = reserve_daily_review(db, count=5, rng=random.Random(5))
            self.assertEqual(third.round_number, 2)
            self.assertEqual(third.generation_mode, "random")
            self.assertEqual(len(third.mistake_ids), 5)
            self.assertEqual(len(set(third.mistake_ids)), 5)
            self.assertEqual(
                {row["subject"] for row in third.rows},
                {"数学分析", "高等代数"},
            )

    def test_concurrent_reservations_do_not_overlap(self):
        self.seed(20)
        barrier = threading.Barrier(2)
        plans = []
        errors = []

        def reserve(index: int):
            try:
                db = connect_database(self.database, role="request")
                try:
                    barrier.wait(timeout=5)
                    plans.append(
                        reserve_daily_review(
                            db,
                            source_job_id=uuid.uuid4().hex,
                            count=5,
                            rng=random.Random(index),
                        )
                    )
                finally:
                    db.close()
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(plans), 2)
        self.assertEqual({plan.round_number for plan in plans}, {1})
        self.assertTrue(set(plans[0].mistake_ids).isdisjoint(plans[1].mistake_ids))

    def test_daily_export_reuses_pipeline_history_and_job_idempotency(self):
        discarded = self.add_mistake(90, date_added="2019-01-01")
        with self.app.app_context():
            db = get_db()
            db.execute("DELETE FROM mistakes WHERE id=?", (discarded,))
            db.commit()
        self.seed(12)
        job_id = self.queued_export_job_id()
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=self.fake_compile,
        ) as compiler:
            first = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"export_context": "daily_review", "_job_id": job_id},
            )
            replay = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"export_context": "daily_review", "_job_id": job_id},
            )
        self.assertEqual(first.status_code, 201, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        payload = first.get_json()
        self.assertEqual(replay.get_json()["id"], payload["id"])
        self.assertTrue(replay.get_json()["idempotency_replayed"])
        self.assertEqual(compiler.call_count, 1)
        self.assertEqual(payload["export_context"], "daily_review")
        self.assertEqual(payload["context_label"], "每日自主复习")
        self.assertEqual(payload["round_number"], 1)
        self.assertGreaterEqual(payload["item_count"], 5)
        self.assertLessEqual(payload["item_count"], 10)
        self.assertEqual(payload["completion_status"], "in_progress")
        with self.app.app_context():
            selected_ids = {
                int(row[0])
                for row in get_db().execute(
                    "SELECT mistake_id FROM daily_review_items WHERE round_id=?",
                    (payload["daily_review_round_id"],),
                )
            }
        snapshot_ids = {
            int(item["mistake_id"])
            for volume in payload["volumes"]
            for item in volume["items"]
        }
        self.assertEqual(snapshot_ids, selected_ids)
        self.assertNotIn(1, snapshot_ids)

        history = self.client.get("/api/v2/mistake-exports").get_json()["items"]
        self.assertEqual(history[0]["round_number"], 1)
        self.assertEqual(history[0]["completed_count"], 0)
        archive = self.client.get(payload["download_url"])
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            self.assertIn(payload["volumes"][0]["download_name"], bundle.namelist())
            self.assertEqual(json.loads(bundle.read("manifest.json"))["download_name"], payload["download_name"])
        day = datetime.fromisoformat(payload["created_at"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        stem = f"每日自主复习_{day}_第1轮_批次{payload['id']}"
        self.assertEqual(payload["download_name"], f"{stem}.zip")
        self.assertEqual(payload["volumes"][0]["download_name"], f"{stem}_第1册.pdf")
        with self.app.app_context():
            statuses = {
                row[0]
                for row in get_db().execute("SELECT status FROM daily_review_items")
            }
            self.assertEqual(statuses, {"published"})

    def test_daily_export_rejects_manual_ids_and_filters(self):
        mistake_id = self.add_mistake(1)
        for payload in (
            {"export_context": "daily_review", "mistake_ids": [mistake_id]},
            {"export_context": "daily_review", "filters": {"subject_id": self.math_id}},
        ):
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/internal/jobs/mistake-exports", json=payload
                )
                self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM daily_review_rounds").fetchone()[0],
                0,
            )

    def test_v58_daily_export_history_remains_readable_without_backfill(self):
        with self.app.app_context():
            db = get_db()
            batch_id = self.add_export_batch(db)
            db.commit()
        history = self.client.get("/api/v2/mistake-exports").get_json()["items"]
        legacy = next(item for item in history if item["id"] == batch_id)
        self.assertEqual(legacy["export_context"], "daily_review")
        self.assertEqual(legacy["completion_status"], "not_tracked")
        self.assertIsNone(legacy["completed_count"])
        self.assertNotIn("round_number", legacy)

    def test_compile_failure_and_cancellation_release_reservations(self):
        self.seed(10)
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=TexCompileError("synthetic failure"),
        ):
            failed = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"export_context": "daily_review", "_job_id": self.queued_export_job_id()},
            )
        self.assertEqual(failed.status_code, 500)
        with self.app.app_context():
            db = get_db()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_review_items").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM daily_review_rounds").fetchone()[0], "failed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mistake_export_batches").fetchone()[0], 0)

        calls = 0

        def cancel_after_reservation():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SyntheticCancellation("cancelled")

        with self.app.app_context():
            db = get_db()
            with self.assertRaises(SyntheticCancellation):
                create_mistake_export(
                    db,
                    pdf_dir=self.pdf_dir,
                    xelatex_command="xelatex",
                    payload={"export_context": "daily_review", "_job_id": uuid.uuid4().hex},
                    compiler=self.fake_compile,
                    cancel_check=cancel_after_reservation,
                    cancelled_errors=(SyntheticCancellation,),
                )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_review_items").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM daily_review_rounds ORDER BY id DESC LIMIT 1").fetchone()[0],
                "cancelled",
            )

    def test_startup_cleanup_releases_interrupted_pending_batch(self):
        self.seed(6)
        with self.app.app_context():
            db = get_db()
            plan = reserve_daily_review(db, count=5, rng=random.Random(9))
            batch_id = db.execute(
                "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
                "total_count,volume_count,layout,filter_snapshot_json,artifact_status,"
                "export_context) VALUES(?,?,?,5,1,'compact','{}','pending','daily_review')",
                (
                    uuid.uuid4().hex,
                    "2026-09-14T00:00:00+00:00",
                    "2026-10-14T00:00:00+00:00",
                ),
            ).lastrowid
            export_id = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,"
                "artifact_status,batch_id,volume_index,layout) "
                "VALUES(?,?,5,'[]','pending',?,1,'compact')",
                (
                    "2026-09-14T00:00:00+00:00",
                    "2026-10-14T00:00:00+00:00",
                    batch_id,
                ),
            ).lastrowid
            attach_export_batch(db, plan.round_id, batch_id)
            db.commit()
            (self.pdf_dir / f"mistake_export_{export_id}.pdf").write_bytes(b"partial")
            cleanup_exports(db, self.pdf_dir, include_pending=True)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_review_items").fetchone()[0], 0)
            self.assertEqual(
                db.execute("SELECT status FROM daily_review_rounds WHERE id=?", (plan.round_id,)).fetchone()[0],
                "failed",
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mistake_export_batches").fetchone()[0], 0)
            self.assertFalse((self.pdf_dir / f"mistake_export_{export_id}.pdf").exists())

    def test_results_are_idempotent_and_do_not_change_normal_review_state(self):
        mistake_ids = self.seed(5)
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=self.fake_compile,
        ):
            export = self.client.post(
                "/internal/jobs/mistake-exports",
                json={
                    "export_context": "daily_review",
                    "_job_id": self.queued_export_job_id(),
                },
            )
        self.assertEqual(export.status_code, 201)
        session = self.client.get("/api/v2/daily-review/current").get_json()
        item = session["items"][0]
        with self.app.app_context():
            db = get_db()
            before = tuple(
                db.execute(
                    "SELECT status,review_count,correct_streak,lapse_count,last_review,"
                    "next_review,review_stage,review_cycle FROM mistakes WHERE id=?",
                    (item["mistake_id"],),
                ).fetchone()
            )
            event_count = db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0]

        key = uuid.uuid4().hex
        endpoint = (
            f"/api/v2/daily-review/sessions/{session['session_id']}"
            f"/items/{item['id']}/result"
        )
        first = self.client.post(endpoint, json={"result": "again"}, headers={"Idempotency-Key": key})
        replay = self.client.post(endpoint, json={"result": "again"}, headers={"Idempotency-Key": key})
        conflict = self.client.post(endpoint, json={"result": "correct"})
        self.assertEqual(first.status_code, 200)
        self.assertFalse(first.get_json()["replayed"])
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["replayed"])
        self.assertEqual(conflict.status_code, 409)

        for remaining in session["items"][1:]:
            response = self.client.post(
                f"/api/v2/daily-review/sessions/{session['session_id']}"
                f"/items/{remaining['id']}/result",
                json={"result": "incomplete"},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
            self.assertEqual(response.status_code, 200)
        current = self.client.get("/api/v2/daily-review/current").get_json()
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["completed_count"], len(session["items"]))

        with self.app.app_context():
            db = get_db()
            after = tuple(
                db.execute(
                    "SELECT status,review_count,correct_streak,lapse_count,last_review,"
                    "next_review,review_stage,review_cycle FROM mistakes WHERE id=?",
                    (item["mistake_id"],),
                ).fetchone()
            )
            self.assertEqual(after, before)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM review_events").fetchone()[0], event_count)
            self.assertEqual(
                db.execute(
                    "SELECT COUNT(*) FROM daily_review_items WHERE mistake_id=? "
                    "AND completion_result='again'",
                    (item["mistake_id"],),
                ).fetchone()[0],
                1,
            )
        suggested = self.client.get("/api/v2/daily-review/recommendations").get_json()
        self.assertEqual(suggested["items"][0]["id"], item["mistake_id"])

    def test_recommendations_sort_by_consolidation_due_date_and_filter(self):
        mistake_ids = self.seed(5)
        with self.app.app_context():
            db = get_db()
            first = reserve_daily_review(db, count=5, rng=random.Random(7))
            self.publish_plan(db, first)
            first_by_mistake = dict(zip(first.mistake_ids, first.item_ids))
            record_result(
                db,
                round_id=first.round_id,
                item_id=first_by_mistake[mistake_ids[0]],
                result="again",
                now=datetime(2026, 9, 10, tzinfo=timezone.utc),
            )
            record_result(
                db,
                round_id=first.round_id,
                item_id=first_by_mistake[mistake_ids[1]],
                result="again",
                now=datetime(2026, 9, 11, tzinfo=timezone.utc),
            )
            record_result(
                db,
                round_id=first.round_id,
                item_id=first_by_mistake[mistake_ids[2]],
                result="again",
                now=datetime(2026, 9, 14, tzinfo=timezone.utc),
            )
            second = reserve_daily_review(db, count=5, rng=random.Random(8))
            self.publish_plan(db, second)
            second_by_mistake = dict(zip(second.mistake_ids, second.item_ids))
            record_result(
                db,
                round_id=second.round_id,
                item_id=second_by_mistake[mistake_ids[0]],
                result="again",
                now=datetime(2026, 9, 12, tzinfo=timezone.utc),
            )
            record_result(
                db,
                round_id=second.round_id,
                item_id=second_by_mistake[mistake_ids[1]],
                result="again",
                now=datetime(2026, 9, 13, tzinfo=timezone.utc),
            )
            ranked = recommendations(db)["items"]
            self.assertEqual(
                [item["id"] for item in ranked[:3]],
                [mistake_ids[0], mistake_ids[1], mistake_ids[2]],
            )
            self.assertEqual([item["again_count"] for item in ranked[:3]], [2, 2, 1])
            db.execute("UPDATE mistakes SET status='已掌握' WHERE id=?", (mistake_ids[1],))
            db.execute(
                "UPDATE mistakes SET deleted_at='2026-09-15T00:00:00+08:00' WHERE id=?",
                (mistake_ids[2],),
            )
            db.commit()
            filtered = recommendations(db)["items"]
            self.assertEqual([item["id"] for item in filtered], [mistake_ids[0]])
            subject_filtered = recommendations(
                db, subject_id=self.algebra_id
            )["items"]
            self.assertTrue(
                all(int(item["subject_id"]) == self.algebra_id for item in subject_filtered)
            )

    def test_generate_route_replays_existing_job_submission(self):
        request_id = uuid.uuid4().hex
        first = self.client.post(
            "/api/v2/daily-review/generate",
            json={"request_id": request_id},
            headers={"Idempotency-Key": request_id},
        )
        replay = self.client.post(
            "/api/v2/daily-review/generate",
            json={"request_id": request_id},
            headers={"Idempotency-Key": request_id},
        )
        self.assertEqual(first.status_code, 202, first.get_json())
        self.assertEqual(replay.status_code, 200, replay.get_json())
        self.assertEqual(first.get_json()["job_id"], replay.get_json()["job_id"])
        self.assertEqual(replay.headers["Idempotency-Replayed"], "true")

    def test_registered_mistake_export_worker_publishes_daily_session(self):
        self.seed(8)
        register_app_job_handlers(self.app)
        try:
            coordinator = JobCoordinator(self.database, lease_seconds=2)
            job, _created = coordinator.submit(
                "mistake-export",
                {"export_context": "daily_review"},
                idempotency_key=uuid.uuid4().hex,
            )
            with patch(
                "cuoti_app.services.mistake_exports.compile_latex_artifact",
                side_effect=self.fake_compile,
            ) as compiler:
                completed = coordinator.run_once()
            self.assertEqual(completed["id"], job["id"])
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(completed["result"]["export_context"], "daily_review")
            self.assertEqual(completed["result"]["round_number"], 1)
            self.assertEqual(compiler.call_count, 1)
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM daily_review_items WHERE status='published'"
                    ).fetchone()[0],
                    completed["result"]["item_count"],
                )
        finally:
            for kind in SUPPORTED_JOB_KINDS:
                unregister_job_handler(kind)


if __name__ == "__main__":
    unittest.main()
