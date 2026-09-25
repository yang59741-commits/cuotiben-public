from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask

from cuoti_app import create_app
from cuoti_app.database import DB_SCHEMA_VERSION, init_db
from cuoti_app.routes.jobs import bp as jobs_blueprint
from cuoti_app.services.jobs import (
    SUPPORTED_JOB_KINDS,
    JobConflict,
    JobCoordinator,
    JobOutcome,
    RetryableJobError,
    register_job_handler,
    unregister_job_handler,
)


class MutableClock:
    def __init__(self):
        self.value = datetime(2026, 7, 27, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class JobTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "jobs.db"
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, DATABASE=str(self.database))
        with self.app.app_context():
            init_db()
        self.app.config["JOB_COORDINATOR_FACTORY"] = lambda: JobCoordinator(
            self.database
        )
        self.app.register_blueprint(jobs_blueprint)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add_exam(self):
        with self.connection() as connection:
            cursor = connection.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at) "
                "VALUES(NULL,'job exam','{}','body','2026-07-27 08:00')"
            )
            return cursor.lastrowid


class DatabaseMigrationTests(JobTestCase):
    def test_application_factory_migrates_before_first_job_request(self):
        database = self.root / "factory-legacy.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE preserved(id INTEGER PRIMARY KEY,value TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO preserved(value) VALUES('keep-me')")
            connection.commit()
        app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(database),
                "SETTINGS_PATH": str(self.root / "factory-settings.json"),
            }
        )
        response = app.test_client().get("/api/v2/jobs?state=active")
        self.assertEqual(response.status_code, 200)
        with closing(sqlite3.connect(database)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertTrue(
                {
                    "background_jobs", "job_inputs", "job_exam_results",
                    "job_attempts", "job_business_results",
                } <= tables
            )
            self.assertEqual(
                connection.execute("SELECT value FROM preserved").fetchone()[0],
                "keep-me",
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], DB_SCHEMA_VERSION)

    def test_job_schema_indexes_and_optional_fts_are_idempotent(self):
        with self.app.app_context():
            init_db()
        with self.connection() as connection:
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            self.assertTrue(
                {"background_jobs", "job_inputs", "job_exam_results"} <= tables
            )
            self.assertTrue(
                {
                    "idx_background_jobs_claim",
                    "idx_background_jobs_lane_claim",
                    "idx_background_jobs_lease",
                    "idx_job_attempts_job",
                    "idx_review_events_date_status",
                    "idx_review_events_day_status",
                    "idx_mistakes_date_status",
                }
                <= indexes
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], DB_SCHEMA_VERSION)

            if "mistakes_fts" in tables:
                cursor = connection.execute(
                    "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,"
                    "note,date_added,status,next_review,fingerprint) "
                    "VALUES(1,1,'1','函数极限测试','','','2026-07-27',"
                    "'未掌握','2026-07-27','fts-one')"
                )
                mistake_id = cursor.lastrowid
                self.assertEqual(
                    connection.execute(
                        "SELECT rowid FROM mistakes_fts WHERE mistakes_fts MATCH ?",
                        ('"函数极限"',),
                    ).fetchone()[0],
                    mistake_id,
                )
                connection.execute(
                    "UPDATE mistakes SET content='矩阵变换测试' WHERE id=?",
                    (mistake_id,),
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT rowid FROM mistakes_fts WHERE mistakes_fts MATCH ?",
                        ('"函数极限"',),
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT rowid FROM mistakes_fts WHERE mistakes_fts MATCH ?",
                        ('"矩阵变换"',),
                    ).fetchone()[0],
                    mistake_id,
                )
                connection.execute("DELETE FROM mistakes WHERE id=?", (mistake_id,))
                self.assertIsNone(
                    connection.execute(
                        "SELECT rowid FROM mistakes_fts WHERE rowid=?", (mistake_id,)
                    ).fetchone()
                )

    def test_existing_v2_database_migrates_without_losing_rows(self):
        legacy_path = self.root / "legacy-v2.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE subjects(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL);
                CREATE TABLE chapters(
                    id INTEGER PRIMARY KEY,subject_id INTEGER NOT NULL,name TEXT NOT NULL
                );
                CREATE TABLE mistakes(
                    id INTEGER PRIMARY KEY,subject_id INTEGER,chapter_id INTEGER,
                    qnum TEXT NOT NULL,reasons TEXT,note TEXT,date_added TEXT NOT NULL,
                    status TEXT,review_count INTEGER,last_review TEXT,next_review TEXT,
                    content TEXT,correct_streak INTEGER NOT NULL DEFAULT 0,
                    lapse_count INTEGER NOT NULL DEFAULT 0,fingerprint TEXT
                );
                CREATE TABLE exams(
                    id INTEGER PRIMARY KEY,subject_id INTEGER,title TEXT,
                    config TEXT,content TEXT,created_at TEXT
                );
                CREATE TABLE review_events(
                    id INTEGER PRIMARY KEY,mistake_id INTEGER,event_type TEXT,
                    reviewed_at TEXT,status_after TEXT,correct_streak INTEGER
                );
                INSERT INTO subjects VALUES(1,'数学分析');
                INSERT INTO chapters VALUES(1,1,'引论');
                INSERT INTO mistakes VALUES(
                    1,1,1,'1','','','2026-07-01','未掌握',0,NULL,
                    '2026-07-01','legacy',0,0,'legacy-fingerprint'
                );
                PRAGMA user_version=2;
                """
            )
            connection.commit()
        legacy_app = Flask("legacy-migration")
        legacy_app.config["DATABASE"] = str(legacy_path)
        with legacy_app.app_context():
            init_db()
            init_db()
        with closing(sqlite3.connect(legacy_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0], 1)
            self.assertEqual(
                connection.execute("SELECT content FROM mistakes WHERE id=1").fetchone()[0],
                "legacy",
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()[0],
                0,
            )
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], DB_SCHEMA_VERSION)


class JobCoordinatorTests(JobTestCase):
    def test_supported_kinds_can_register_without_business_imports(self):
        coordinator = JobCoordinator(self.database)
        for kind in SUPPORTED_JOB_KINDS:
            coordinator.register_handler(kind, lambda _context, payload: payload)
        self.assertEqual(set(coordinator.handlers), set(SUPPORTED_JOB_KINDS))

        register_job_handler("analysis", lambda _context, payload: payload)
        try:
            self.assertIn("analysis", JobCoordinator(self.database).handlers)
        finally:
            unregister_job_handler("analysis")

    def test_submission_is_idempotent_and_keeps_input_separate(self):
        coordinator = JobCoordinator(self.database)
        first, created = coordinator.submit(
            "analysis",
            {"problem": "x", "options": {"b": 2, "a": 1}},
            idempotency_key="analysis-1",
        )
        replayed, replay_created = coordinator.submit(
            "analysis",
            {"options": {"a": 1, "b": 2}, "problem": "x"},
            idempotency_key="analysis-1",
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first["id"], replayed["id"])
        self.assertEqual(coordinator.get_input(first["id"])["problem"], "x")
        self.assertNotIn("input", first)
        with self.assertRaises(JobConflict):
            coordinator.submit(
                "analysis",
                {"problem": "different"},
                idempotency_key="analysis-1",
            )

    def test_concurrent_idempotent_submission_creates_one_job(self):
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def submit_once():
            try:
                barrier.wait()
                results.append(
                    JobCoordinator(self.database).submit(
                        "ocr",
                        {"file_token": "temporary"},
                        idempotency_key="concurrent-ocr",
                    )
                )
            except Exception as exc:  # Captured for an assertion on the main thread.
                failures.append(exc)

        workers = [threading.Thread(target=submit_once) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(len(results), 2)
        self.assertEqual({job["id"] for job, _created in results}, {results[0][0]["id"]})
        self.assertEqual(sorted(created for _job, created in results), [False, True])

    def test_success_persists_progress_result_and_exam_links(self):
        exam_id = self.add_exam()

        def handler(context, payload):
            context.update_progress(1, 2, "分析完成")
            context.update_progress(2, 2, "试卷完成")
            return JobOutcome({"echo": payload["value"]}, (exam_id,))

        coordinator = JobCoordinator(
            self.database, handlers={"exam-generation": handler}
        )
        submitted, _created = coordinator.submit(
            "exam-generation", {"value": 7}, max_attempts=2
        )
        finished = coordinator.run_once()
        self.assertEqual(finished["id"], submitted["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["attempts"], 1)
        self.assertEqual(finished["progress"], {"current": 2, "total": 2, "message": "试卷完成"})
        self.assertEqual(set(finished["result"]), {"echo", "timing"})
        self.assertEqual(finished["result"]["echo"], 7)
        self.assertEqual(finished["result"]["timing"]["schema_version"], "exam-timing-receipt-v1")
        self.assertEqual(finished["exam_ids"], [exam_id])

    def test_queued_and_running_cancellation_are_idempotent(self):
        queued = JobCoordinator(self.database)
        submitted, _created = queued.submit("ocr", {"file_token": "temporary"})
        first = queued.request_cancel(submitted["id"])
        second = queued.request_cancel(submitted["id"])
        self.assertEqual(first["status"], "cancelled")
        self.assertEqual(second["status"], "cancelled")
        self.assertIsNone(queued.run_once())

        def cancelling_handler(context, _payload):
            context.coordinator.request_cancel(context.job_id)
            context.update_progress(1, 1, "不应提交")
            return {"unexpected": True}

        running = JobCoordinator(
            self.database, handlers={"classification": cancelling_handler}
        )
        job, _created = running.submit("classification", {"text": "x"})
        finished = running.run_once()
        self.assertEqual(finished["id"], job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(finished["result"])

    def test_retryable_failure_and_manual_retry(self):
        calls = {"count": 0}

        def flaky_handler(_context, _payload):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RetryableJobError("稍后重试", delay_seconds=0)
            return {"ok": True}

        coordinator = JobCoordinator(
            self.database, handlers={"analysis": flaky_handler}
        )
        job, _created = coordinator.submit("analysis", {}, max_attempts=2)
        self.assertEqual(coordinator.run_once()["status"], "queued")
        succeeded = coordinator.run_once()
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["attempts"], 2)

        failed_coordinator = JobCoordinator(
            self.database,
            handlers={"ocr": lambda _context, _payload: (_ for _ in ()).throw(RuntimeError())},
        )
        failed_job, _created = failed_coordinator.submit("ocr", {})
        failed = failed_coordinator.run_once()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "handler_error")
        self.assertEqual(failed["error"]["message"], "任务执行失败")
        retried = failed_coordinator.retry(failed_job["id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(retried["attempts"], 1)
        self.assertEqual(retried["attempts_in_cycle"], 0)
        self.assertEqual(retried["manual_retry_count"], 1)
        attempts = failed_coordinator.list_attempts(failed_job["id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "failed")
        self.assertEqual(attempts[0]["correlation_id"], failed["error"]["correlation_id"])

    def test_fast_and_heavy_lanes_run_independently(self):
        heavy_started = threading.Event()
        release_heavy = threading.Event()

        def heavy_handler(_context, _payload):
            heavy_started.set()
            release_heavy.wait(2)
            return {"heavy": True}

        heavy = JobCoordinator(
            self.database,
            lane="heavy",
            handlers={"exam-generation": heavy_handler},
        )
        fast = JobCoordinator(
            self.database,
            lane="fast",
            handlers={"classification": lambda _context, _payload: {"fast": True}},
        )
        heavy.submit("exam-generation", {})
        fast_job, _created = fast.submit("classification", {"problem": "x"})
        worker = threading.Thread(target=heavy.run_once)
        worker.start()
        self.assertTrue(heavy_started.wait(1))
        completed = fast.run_once()
        self.assertEqual(completed["id"], fast_job["id"])
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(worker.is_alive())
        release_heavy.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_wait_aging_prevents_low_priority_starvation(self):
        clock = MutableClock()
        coordinator = JobCoordinator(
            self.database,
            lane="heavy",
            clock=clock,
            aging_seconds=10,
        )
        old_job, _created = coordinator.submit("memory-maintenance", {})
        clock.advance(250)
        coordinator.submit("mistake-export", {"mistake_ids": [1]})
        claimed = coordinator.claim_next()
        self.assertEqual(claimed["id"], old_job["id"])

    def test_unknown_error_has_logged_correlation_without_client_traceback(self):
        def broken(_context, _payload):
            raise RuntimeError("private-stack-detail")

        coordinator = JobCoordinator(
            self.database, handlers={"analysis": broken}
        )
        coordinator.submit("analysis", {})
        with self.assertLogs("cuoti_app.services.jobs", level="ERROR") as captured:
            failed = coordinator.run_once()
        correlation_id = failed["error"]["correlation_id"]
        self.assertRegex(correlation_id, r"^[0-9a-f]{32}$")
        self.assertEqual(failed["error"]["category"], "internal")
        self.assertFalse(failed["error"]["retryable"])
        self.assertNotIn("private-stack-detail", failed["error"]["message"])
        self.assertIn(correlation_id, "\n".join(captured.output))
        self.assertIn("private-stack-detail", "\n".join(captured.output))

    def test_cancel_race_keeps_committed_business_result_successful(self):
        def committed(context, _payload):
            context.coordinator.request_cancel(context.job_id)
            return JobOutcome(
                result={"published": True}, side_effect_committed=True
            )

        coordinator = JobCoordinator(
            self.database, handlers={"mistake-export": committed}
        )
        job, _created = coordinator.submit("mistake-export", {"mistake_ids": [1]})
        finished = coordinator.run_once()
        self.assertEqual(finished["id"], job["id"])
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"], {"published": True})

    def test_d2_completed_publication_finishes_pending_job_without_resetting_attempts(self):
        exam_ids = []

        def recover_completed(_context, _payload):
            return JobOutcome(
                result={"status": "completed", "id": exam_ids[0]},
                exam_ids=(exam_ids[0],), side_effect_committed=True,
            )

        coordinator = JobCoordinator(
            self.database, worker_id="d2-worker",
            handlers={"exam-generation": recover_completed},
        )
        job, _created = coordinator.submit(
            "exam-generation",
            {
                "source": "free", "subject_id": 1, "chapters": [1],
                "difficulty": "中等", "question_count": 1,
                "types": ["计算题"], "note": "",
            },
        )
        with self.connection() as connection:
            exam_id = connection.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,"
                "generation_job_id,artifact_status) VALUES(1,'D2','{}','body',?,?,'published')",
                ("2026-09-05T00:00:00+00:00", job["id"]),
            ).lastrowid
            exam_ids.append(exam_id)
            h = "a" * 64
            connection.execute(
                "INSERT INTO exam_generation_d2_quality_publications("
                "job_id,state_version,state,selection_hash,objective_hash,"
                "candidate_facts_hash,stage3_contract_hash,authority_hash,authority_json,"
                "quality_request_hash,quality_request_json,quality_response_hash,"
                "quality_response_json,draft_hash,request_hash,request_json,artifact_hash,"
                "exam_id,publication_hash,publication_json,receipt_hash,revision,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job["id"], "past-v3-d2-quality-publication-state-v1", "completed",
                    h, h, h, h, h, "{}", h, "{}", h, "{}", h, h, "{}", h,
                    exam_id, h, "{}", h, 6,
                    "2026-09-05T00:00:00+00:00", "2026-09-05T00:00:01+00:00",
                ),
            )
            connection.execute(
                "UPDATE background_jobs SET status='published_pending_receipt',attempts=470 "
                "WHERE id=?",
                (job["id"],),
            )
            connection.execute(
                "INSERT INTO job_exam_results(job_id,exam_id,position) VALUES(?,?,1)",
                (job["id"], exam_id),
            )
            connection.execute(
                "INSERT INTO job_business_results(job_id,kind,result_json,committed_at) "
                "VALUES(?,'exam-generation','{\"state\":\"published_pending_receipt\"}',?)",
                (job["id"], "2026-09-05T00:00:00+00:00"),
            )
        finished = coordinator.run_once()
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["publication_state"], "completed")
        self.assertEqual(finished["attempts"], 471)
        with self.connection() as connection:
            attempt = connection.execute(
                "SELECT status FROM job_attempts WHERE job_id=? AND attempt_number=471",
                (job["id"],),
            ).fetchone()
            self.assertEqual(attempt["status"], "succeeded")

    def test_expired_lease_recovers_and_heartbeat_extends_it(self):
        clock = MutableClock()
        first = JobCoordinator(
            self.database,
            worker_id="worker-one",
            lease_seconds=5,
            clock=clock,
        )
        job, _created = first.submit("batch-mistakes", {"items": [1]})
        claimed = first.claim_next()
        self.assertEqual(claimed["id"], job["id"])
        self.assertFalse(first.heartbeat(job["id"]))
        clock.advance(6)
        second = JobCoordinator(
            self.database,
            worker_id="worker-two",
            lease_seconds=5,
            clock=clock,
            handlers={"batch-mistakes": lambda _context, _payload: {"saved": 1}},
        )
        self.assertEqual(second.recover_stale_jobs(), 1)
        recovered = second.run_once()
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["attempts"], 2)

    def test_worker_heartbeats_automatically_during_blocking_handler(self):
        started = threading.Event()

        def blocking_handler(_context, _payload):
            started.set()
            time.sleep(1.4)
            return {"ok": True}

        first = JobCoordinator(
            self.database,
            worker_id="blocking-worker",
            lease_seconds=1,
            handlers={"analysis": blocking_handler},
        )
        job, _created = first.submit("analysis", {})
        worker = threading.Thread(target=first.run_once)
        worker.start()
        self.assertTrue(started.wait(1))
        time.sleep(1.1)
        second = JobCoordinator(
            self.database, worker_id="competing-worker", lease_seconds=1
        )
        self.assertEqual(second.recover_stale_jobs(), 0)
        self.assertIsNone(second.claim_next())
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first.get_job(job["id"])["status"], "succeeded")

    def test_retention_purges_ocr_result_before_terminal_metadata(self):
        clock = MutableClock()
        coordinator = JobCoordinator(
            self.database,
            clock=clock,
            handlers={
                "ocr": lambda _context, _payload: {"text": "temporary OCR text"},
                "analysis": lambda _context, _payload: {"summary": "old"},
            },
        )
        ocr_job, _created = coordinator.submit("ocr", {})
        coordinator.run_once()
        ordinary_job, _created = coordinator.submit("analysis", {})
        coordinator.run_once()
        eight_days_ago = (clock.value - timedelta(days=8)).isoformat(
            timespec="microseconds"
        )
        thirty_one_days_ago = (clock.value - timedelta(days=31)).isoformat(
            timespec="microseconds"
        )
        with self.connection() as connection:
            connection.execute(
                "UPDATE background_jobs SET finished_at=? WHERE id=?",
                (eight_days_ago, ocr_job["id"]),
            )
            connection.execute(
                "UPDATE background_jobs SET finished_at=? WHERE id=?",
                (thirty_one_days_ago, ordinary_job["id"]),
            )
        cleaned = coordinator.cleanup_retention()
        self.assertEqual(cleaned["purged_ocr_results"], 1)
        self.assertEqual(cleaned["deleted_jobs"], 1)
        retained_ocr = coordinator.get_job(ocr_job["id"])
        self.assertEqual(retained_ocr["status"], "succeeded")
        self.assertIsNone(retained_ocr["result"])
        with self.connection() as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM background_jobs WHERE id=?", (ordinary_job["id"],)
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM job_inputs WHERE job_id=?", (ordinary_job["id"],)
                ).fetchone()
            )

    def test_queued_ocr_cancel_deletes_only_approved_staged_files(self):
        staged_root = self.root / "ocr-staging"
        staged_root.mkdir()
        staged_file = staged_root / "upload.pdf"
        staged_file.write_bytes(b"temporary")
        outside_file = self.root / "must-remain.txt"
        outside_file.write_text("keep", encoding="utf-8")
        coordinator = JobCoordinator(
            self.database, staged_input_roots=(staged_root,)
        )
        job, _created = coordinator.submit(
            "ocr",
            {
                "_staged_input_path": [str(staged_file), str(outside_file)],
                "display_name": "upload.pdf",
            },
        )
        cancelled = coordinator.request_cancel(job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse(staged_file.exists())
        self.assertTrue(outside_file.exists())
        self.assertEqual(coordinator.cleanup_staged_input(job["id"]), 0)

    def test_ocr_terminal_completion_cleans_staged_input(self):
        staged_root = self.root / "ocr-complete"
        staged_root.mkdir()
        staged_file = staged_root / "upload.pdf"
        staged_file.write_bytes(b"temporary")
        coordinator = JobCoordinator(
            self.database,
            staged_input_roots=(staged_root,),
            handlers={"ocr": lambda _context, _payload: {"text": "done"}},
        )
        coordinator.submit(
            "ocr", {"_staged_input_path": str(staged_file)}
        )
        self.assertEqual(coordinator.run_once()["status"], "succeeded")
        self.assertFalse(staged_file.exists())

    def test_cancellable_process_is_terminated_by_running_job_cancel(self):
        process_started = threading.Event()

        def process_handler(context, _payload):
            process_started.set()
            context.run_cancellable_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout=15,
                check_interval=0.05,
            )
            return {"unexpected": True}

        coordinator = JobCoordinator(
            self.database,
            handlers={"exam-generation": process_handler},
        )
        job, _created = coordinator.submit("exam-generation", {})
        worker = threading.Thread(target=coordinator.run_once)
        worker.start()
        self.assertTrue(process_started.wait(1))
        time.sleep(0.15)
        requested = coordinator.request_cancel(job["id"])
        self.assertEqual(requested["status"], "cancel_requested")
        worker.join(3)
        self.assertFalse(worker.is_alive())
        finished = coordinator.get_job(job["id"])
        self.assertEqual(finished["status"], "cancelled")
        self.assertIsNone(finished["result"])


class JobsApiTests(JobTestCase):
    def test_v2_submit_replay_list_cancel_retry_and_errors(self):
        submitted = self.client.post(
            "/api/v2/jobs",
            json={"kind": "analysis", "input": {"problem": "x"}},
            headers={"Idempotency-Key": "api-analysis-1"},
        )
        self.assertEqual(submitted.status_code, 202)
        self.assertEqual(submitted.headers["Idempotency-Replayed"], "false")
        job = submitted.get_json()["job"]
        self.assertEqual(job["status"], "queued")
        self.assertTrue(submitted.headers["Location"].endswith(job["id"]))

        replayed = self.client.post(
            "/api/v2/jobs",
            json={"kind": "analysis", "input": {"problem": "x"}},
            headers={"Idempotency-Key": "api-analysis-1"},
        )
        self.assertEqual(replayed.status_code, 200)
        self.assertEqual(replayed.headers["Idempotency-Replayed"], "true")
        self.assertEqual(replayed.get_json()["job"]["id"], job["id"])

        conflict = self.client.post(
            "/api/v2/jobs",
            json={"kind": "analysis", "input": {"problem": "other"}},
            headers={"Idempotency-Key": "api-analysis-1"},
        )
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "idempotency_conflict")
        self.assertIsInstance(conflict.get_json()["error"], str)
        self.assertIn("retryable", conflict.get_json())
        self.assertIn("correlation_id", conflict.get_json())

        jobs = self.client.get("/api/v2/jobs?kind=analysis&status=queued").get_json()
        self.assertEqual([item["id"] for item in jobs["jobs"]], [job["id"]])
        kinds = self.client.get("/api/v2/jobs/kinds").get_json()["kinds"]
        self.assertEqual(set(kinds), set(SUPPORTED_JOB_KINDS))
        policies = self.client.get("/api/v2/jobs/kinds").get_json()["policies"]
        self.assertEqual(policies["classification"]["lane"], "fast")
        self.assertEqual(policies["exam-generation"]["lane"], "heavy")

        cancelled = self.client.post(f"/api/v2/jobs/{job['id']}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.get_json()["job"]["status"], "cancelled")
        retried = self.client.post(f"/api/v2/jobs/{job['id']}/retry")
        self.assertEqual(retried.status_code, 202)
        self.assertEqual(retried.get_json()["job"]["status"], "queued")
        attempts = self.client.get(f"/api/v2/jobs/{job['id']}/attempts")
        self.assertEqual(attempts.status_code, 200)
        self.assertEqual(attempts.get_json()["attempts"], [])

        missing = self.client.get("/api/v2/jobs/missing")
        self.assertEqual(missing.status_code, 404)
        invalid = self.client.post(
            "/api/v2/jobs", json={"kind": "unsupported", "input": {}}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "unknown_job_kind")


if __name__ == "__main__":
    unittest.main()
