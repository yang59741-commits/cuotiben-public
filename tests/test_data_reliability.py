from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from cuoti_app import create_app
from cuoti_app import database as database_module
from cuoti_app.database import DB_SCHEMA_VERSION, DatabaseSchemaError, init_db
from tests.migration_fixtures import create_historical_database


class DataReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "test.db"
        self.tex_dir = self.root / "tex"
        self.pdf_dir = self.root / "pdf"
        self.tex_dir.mkdir()
        self.pdf_dir.mkdir()
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.db_path),
                "TEX_BASE": str(self.tex_dir),
                "QUESTION_BANK_DATABASE": str(
                    self.root / "question_bank.sqlite3"
                ),
                "PDF_DIR": str(self.pdf_dir),
                "PAST_EXAM_DIR": str(self.root / "past"),
            }
        )
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def add_mistake(self, qnum="1", content="测试题目"):
        response = self.client.post(
            "/internal/legacy/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": qnum,
                "content": content,
                "reasons": ["计算错误"],
                "note": "备注",
                "date_added": "2026-07-26",
            },
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response.get_json()["id"]

    def row(self, mistake_id):
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        try:
            return dict(
                connection.execute(
                    "SELECT * FROM mistakes WHERE id=?", (mistake_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def add_v2_mistake(self):
        response = self.client.post(
            "/api/v2/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "V2-1",
                "content": "题目 $x^2$",
                "reasons": ["概念不清"],
                "note": "n" * 3000,
                "answer": "答案",
                "solution": "解析",
                "date_added": "2026-08-06",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def test_v2_identity_revision_idempotent_review_and_soft_delete(self):
        created = self.add_v2_mistake()
        uuid.UUID(created["public_id"])
        self.assertEqual(created["revision"], 1)
        self.assertEqual(created["reasons"], ["概念不清"])
        event_id = str(uuid.uuid4())
        first = self.client.post(
            f"/api/v2/mistakes/{created['public_id']}/review",
            json={"revision": 1, "client_event_id": event_id, "result": "again"},
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertFalse(first.get_json()["replayed"])
        replay = self.client.post(
            f"/api/v2/mistakes/{created['public_id']}/review",
            json={"revision": 1, "client_event_id": event_id, "result": "again"},
        )
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["replayed"])
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM review_events WHERE client_event_id=?",
                    (event_id,),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

        stale = self.client.put(
            f"/api/v2/mistakes/{created['public_id']}/reasons",
            json={"revision": 1, "reasons": ["方法不会"]},
        )
        self.assertEqual(stale.status_code, 409)
        deleted = self.client.delete(
            f"/api/v2/mistakes/{created['public_id']}", json={"revision": 2}
        )
        self.assertEqual(deleted.status_code, 200, deleted.get_data(as_text=True))
        listed = self.client.get("/api/v2/mistakes").get_json()["items"]
        self.assertFalse(any(item["public_id"] == created["public_id"] for item in listed))
        trash = self.client.get("/api/v2/trash/mistakes").get_json()["items"]
        self.assertTrue(any(item["public_id"] == created["public_id"] for item in trash))
        restored = self.client.post(
            f"/api/v2/mistakes/{created['public_id']}/restore",
            json={"revision": 3},
        )
        self.assertEqual(restored.status_code, 200)
        listed = self.client.get("/api/v2/mistakes").get_json()["items"]
        self.assertTrue(any(item["public_id"] == created["public_id"] for item in listed))

    def test_v2_note_update_add_change_clear_validation_and_search_refresh(self):
        created = self.add_v2_mistake()
        endpoint = f"/api/v2/mistakes/{created['public_id']}"
        added = self.client.patch(
            endpoint, json={"revision": 1, "note": "新关键词 alpha"}
        )
        self.assertEqual(added.status_code, 200, added.get_data(as_text=True))
        self.assertEqual(added.get_json()["revision"], 2)
        self.assertEqual(added.get_json()["note"], "新关键词 alpha")
        self.assertEqual(
            self.client.get("/api/v2/mistakes?search=alpha").get_json()["items"][0]["note"],
            "新关键词 alpha",
        )

        boundary = self.client.patch(
            endpoint, json={"revision": 2, "note": "边" * 20_000}
        )
        self.assertEqual(boundary.status_code, 200)
        too_long = self.client.patch(
            endpoint, json={"revision": 3, "note": "长" * 20_001}
        )
        self.assertEqual(too_long.status_code, 400)
        forbidden = self.client.patch(
            endpoint, json={"revision": 3, "note": "草稿", "status": "已掌握"}
        )
        self.assertEqual(forbidden.status_code, 400)
        cleared = self.client.patch(endpoint, json={"revision": 3, "note": ""})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.get_json()["note"], "")
        self.assertEqual(
            self.client.get("/api/v2/mistakes?search=alpha").get_json()["items"], []
        )
        missing = self.client.patch(
            f"/api/v2/mistakes/{uuid.uuid4()}", json={"revision": 1, "note": "x"}
        )
        self.assertEqual(missing.status_code, 404)

    def test_concurrent_note_updates_never_silently_overwrite(self):
        created = self.add_v2_mistake()
        endpoint = f"/api/v2/mistakes/{created['public_id']}"

        def submit(note):
            with self.app.test_client() as client:
                response = client.patch(endpoint, json={"revision": 1, "note": note})
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ("页面甲草稿", "页面乙草稿")))
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        conflict = next(payload for status, payload in results if status == 409)
        winner = next(payload for status, payload in results if status == 200)
        self.assertEqual(conflict["current_revision"], 2)
        self.assertEqual(conflict["current_note"], winner["note"])
        row = self.client.get("/api/v2/mistakes").get_json()["items"][0]
        self.assertEqual(row["revision"], 2)
        self.assertEqual(row["note"], winner["note"])

    def test_concurrent_duplicate_review_event_is_applied_once(self):
        created = self.add_v2_mistake()
        event_id = str(uuid.uuid4())

        def submit():
            with self.app.test_client() as client:
                response = client.post(
                    f"/api/v2/mistakes/{created['public_id']}/review",
                    json={
                        "revision": 1,
                        "client_event_id": event_id,
                        "result": "again",
                    },
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: submit(), range(2)))
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sorted(payload["replayed"] for _, payload in results), [False, True])
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM review_events WHERE client_event_id=?",
                    (event_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT review_count,lapse_count,revision FROM mistakes "
                    "WHERE public_id=?",
                    (created["public_id"],),
                ).fetchone(),
                (1, 1, 2),
            )
        finally:
            connection.close()

    def test_distinct_concurrent_events_surface_revision_then_retry_without_loss(self):
        created = self.add_v2_mistake()
        event_ids = [str(uuid.uuid4()), str(uuid.uuid4())]

        def submit(event_id, revision):
            with self.app.test_client() as client:
                response = client.post(
                    f"/api/v2/mistakes/{created['public_id']}/review",
                    json={
                        "revision": revision,
                        "client_event_id": event_id,
                        "result": "pass",
                    },
                )
                return response.status_code, response.get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            initial = list(executor.map(lambda event: submit(event, 1), event_ids))
        self.assertEqual(sorted(status for status, _ in initial), [200, 409])
        failed_index = next(index for index, result in enumerate(initial) if result[0] == 409)
        retried = submit(event_ids[failed_index], 2)
        self.assertEqual(retried[0], 200, retried[1])
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM review_events WHERE client_event_id IN (?,?)",
                    tuple(event_ids),
                ).fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT review_count,revision FROM mistakes WHERE public_id=?",
                    (created["public_id"],),
                ).fetchone(),
                (2, 3),
            )
        finally:
            connection.close()

    def test_soft_delete_is_excluded_everywhere_and_restore_reindexes(self):
        created = self.add_v2_mistake()
        query = "题目"
        self.assertTrue(self.client.get(f"/api/v2/mistakes?search={query}").get_json()["items"])
        deleted = self.client.delete(
            f"/api/v2/mistakes/{created['public_id']}", json={"revision": 1}
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get("/internal/legacy/stats").get_json()["total"], 0)
        self.assertEqual(self.client.get("/api/v2/reviews/due").get_json()["items"], [])
        self.assertEqual(self.client.get("/api/v2/exam-candidates").get_json()["items"], [])
        self.assertEqual(self.client.get("/internal/legacy/export").get_json()["mistakes"], [])
        self.assertEqual(
            self.client.get(f"/api/v2/mistakes?search={query}").get_json()["items"], []
        )
        restored = self.client.post(
            f"/api/v2/mistakes/{created['public_id']}/restore", json={"revision": 2}
        )
        self.assertEqual(restored.status_code, 200)
        self.assertTrue(self.client.get(f"/api/v2/mistakes?search={query}").get_json()["items"])

    def test_consecutive_review_schedule_and_events(self):
        mistake_id = self.add_mistake()
        today = datetime.now()

        again = self.client.put(
            f"/internal/legacy/mistakes/{mistake_id}/review", json={"result": "again"}
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(self.row(mistake_id)["lapse_count"], 1)
        self.assertEqual(self.row(mistake_id)["correct_streak"], 0)

        for streak, days in ((1, 5), (2, 10), (3, 15), (4, None)):
            response = self.client.put(
                f"/internal/legacy/mistakes/{mistake_id}/review", json={"result": "pass"}
            )
            payload = response.get_json()
            self.assertEqual(payload["correct_streak"], streak)
            expected = None if days is None else (today + timedelta(days=days)).strftime("%Y-%m-%d")
            self.assertEqual(payload["next_review"], expected)
            self.assertEqual(
                payload["status"], "已掌握" if streak == 4 else "未掌握"
            )

        connection = sqlite3.connect(self.db_path)
        try:
            events = connection.execute(
                "SELECT event_type FROM review_events WHERE mistake_id=? ORDER BY id",
                (mistake_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([event[0] for event in events], ["again", "pass", "pass", "pass", "pass"])

    def test_manual_toggle_records_events_and_reopens_today(self):
        created = self.add_v2_mistake()
        endpoint = f"/api/v2/mistakes/{created['public_id']}/toggle"
        mastered_response = self.client.put(endpoint, json={"revision": 1})
        self.assertEqual(mastered_response.status_code, 200)
        mastered = mastered_response.get_json()
        self.assertEqual(mastered["status"], "已掌握")
        self.assertEqual(mastered["correct_streak"], 0)
        self.assertEqual(mastered["revision"], 2)
        self.assertEqual(mastered["event_type"], "manual_master")
        conflict = self.client.put(endpoint, json={"revision": 1})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.get_json()["code"], "REVISION_CONFLICT")
        reopened = self.client.put(endpoint, json={"revision": 2}).get_json()
        self.assertEqual(reopened["status"], "未掌握")
        self.assertEqual(reopened["correct_streak"], 0)
        self.assertEqual(reopened["revision"], 3)
        self.assertEqual(reopened["event_type"], "manual_reopen")
        self.assertEqual(reopened["next_review"], (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d"))
        due_public_ids = {
            row["public_id"]
            for row in self.client.get("/api/v2/reviews/due").get_json()["items"]
        }
        self.assertNotIn(created["public_id"], due_public_ids)
        connection = sqlite3.connect(self.db_path)
        try:
            events = connection.execute(
                "SELECT event_type FROM review_events e JOIN mistakes m ON m.id=e.mistake_id "
                "WHERE m.public_id=? ORDER BY e.id", (created["public_id"],)
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in events], ["manual_master", "manual_reopen"])

    def import_with_preview(self, backup, mode="merge"):
        raw = json.dumps(backup, ensure_ascii=False).encode()
        preview = self.client.post(f"/api/v3/import/preview?mode={mode}", data=raw)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        return self.client.post(
            f"/api/v3/import/commit?mode={mode}", data=raw,
            headers={"X-Import-Preview-Token": preview.get_json()["preview_token"]},
        )

    def test_v2_export_merge_replace_and_atomic_validation(self):
        mistake_id = self.add_mistake()
        self.client.put(
            f"/internal/legacy/mistakes/{mistake_id}/review", json={"result": "pass"}
        )
        exported = self.client.get("/internal/legacy/export").get_json()
        self.assertEqual(exported["schema_version"], 2)
        self.assertEqual(len(exported["mistakes"]), 1)
        self.assertEqual(exported["mistakes"][0]["reviews"][0]["event_type"], "pass")
        self.assertNotIn("id", exported["mistakes"][0])

        merged = self.import_with_preview(exported).get_json()
        self.assertEqual(merged["inserted"], 0)
        self.assertEqual(merged["merged"], 1)

        invalid = json.loads(json.dumps(exported, ensure_ascii=False))
        invalid["mistakes"].append(
            {
                **invalid["mistakes"][0],
                "qnum": "2",
                "subject": "不存在",
            }
        )
        rejected = self.client.post("/api/v3/import/preview?mode=replace", json=invalid)
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(len(self.client.get("/internal/legacy/mistakes").get_json()), 1)

        replaced = self.import_with_preview(exported, mode="replace").get_json()
        self.assertEqual(replaced["inserted"], 1)
        self.assertTrue((Path(replaced["recovery_backup"]) / "cuoti.db").is_file())
        self.assertEqual(len(self.client.get("/internal/legacy/mistakes").get_json()), 1)

    def test_legacy_import_and_input_validation(self):
        legacy = [
            {
                "subject": "数学分析",
                "chapter": "引论",
                "qnum": "9",
                "content": "旧备份题目",
                "reasons": "概念不清",
                "note": "",
                "date_added": "2026-07-25",
                "status": "已掌握",
                "review_count": 5,
            }
        ]
        result = self.import_with_preview(legacy)
        self.assertEqual(result.status_code, 200)
        row = self.client.get("/internal/legacy/mistakes").get_json()[0]
        self.assertEqual(row["correct_streak"], 3)

        self.assertEqual(
            self.client.post("/internal/legacy/mistakes", data="{}").status_code, 400
        )
        self.assertEqual(
            self.client.post(
                "/internal/legacy/mistakes",
                json={
                    "subject_id": 1,
                    "chapter_id": 26,
                    "qnum": "x",
                    "date_added": "2026-07-26",
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.put("/internal/legacy/mistakes/999/review", json={"result": "pass"}).status_code,
            404,
        )
        too_large_import = self.client.post(
            "/internal/legacy/import",
            data=b"x" * (10 * 1024 * 1024 + 1),
            content_type="application/json",
        )
        self.assertEqual(too_large_import.status_code, 410)
        too_large_import.close()
        self.assertEqual(
            self.client.post(
                "/internal/jobs/ocr-pdf",
                data={"file": (io.BytesIO(b"legacy"), "legacy.doc")},
                content_type="multipart/form-data",
            ).status_code,
            400,
        )
        oversized_stream = io.BytesIO(b"x" * (20 * 1024 * 1024 + 1))
        oversized = self.client.post(
            "/internal/jobs/ocr-pdf",
            data={
                "file": (
                    oversized_stream,
                    "oversized.txt",
                )
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(oversized.status_code, 413)
        oversized.request.input_stream.close()
        oversized.close()
        oversized_stream.close()


class MigrationTests(unittest.TestCase):
    def test_v7_schema_adds_attachments_and_upgrades_to_current(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "partial-v7.db"
            create_historical_database(db_path, 7)
            create_app({"TESTING": True, "DATABASE": str(db_path)})
            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0],
                    DB_SCHEMA_VERSION,
                )
                self.assertTrue(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='attachments'"
                    ).fetchone()
                )
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()

    def test_newer_schema_is_rejected_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "newer.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE marker(value TEXT)")
            connection.execute("INSERT INTO marker VALUES('preserve-me')")
            connection.execute("PRAGMA user_version=99")
            connection.commit()
            connection.close()

            with self.assertRaises(DatabaseSchemaError):
                create_app({"TESTING": True, "DATABASE": str(db_path)})

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 99)
                self.assertEqual(
                    connection.execute("SELECT value FROM marker").fetchone()[0],
                    "preserve-me",
                )
                self.assertFalse(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='subjects'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_failed_migration_step_rolls_back_as_one_transaction(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "rollback.db"
            create_app({"TESTING": True, "DATABASE": str(db_path)})
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA user_version=6")
            connection.commit()
            connection.close()

            def broken_migration(connection):
                connection.execute("CREATE TABLE migration_probe(value TEXT)")
                connection.execute("INSERT INTO migration_probe VALUES('partial')")
                raise RuntimeError("injected migration failure")

            original = database_module.MIGRATIONS[6]
            database_module.MIGRATIONS[6] = (7, broken_migration)
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    create_app({"TESTING": True, "DATABASE": str(db_path)})
            finally:
                database_module.MIGRATIONS[6] = original

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 6)
                self.assertFalse(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='migration_probe'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_old_schema_migrates_idempotently_without_fake_events(self):
        with tempfile.TemporaryDirectory() as folder:
            db_path = Path(folder) / "legacy.db"
            connection = sqlite3.connect(db_path)
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
                    content TEXT
                );
                CREATE TABLE exams(
                    id INTEGER PRIMARY KEY,subject_id INTEGER,title TEXT,
                    config TEXT,content TEXT,created_at TEXT
                );
                INSERT INTO subjects VALUES(1,'数学分析');
                INSERT INTO chapters VALUES(1,1,'引论');
                INSERT INTO mistakes VALUES
                    (1,1,1,'1','','','2026-07-01','已掌握',3,NULL,NULL,'A'),
                    (2,1,1,'2','','','2026-07-02','未掌握',2,NULL,NULL,'B');
                """
            )
            connection.commit()
            connection.close()

            app = create_app({"TESTING": True, "DATABASE": str(db_path)})
            with app.app_context():
                init_db()
                init_db()

            connection = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(mistakes)")
                }
                streaks = connection.execute(
                    "SELECT correct_streak FROM mistakes ORDER BY id"
                ).fetchall()
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM review_events"
                ).fetchone()[0]
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
            self.assertTrue(
                {"correct_streak", "lapse_count", "fingerprint"} <= columns
            )
            self.assertEqual(streaks, [(3,), (0,)])
            self.assertEqual(event_count, 0)
            self.assertEqual(version, DB_SCHEMA_VERSION)
