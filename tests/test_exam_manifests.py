from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from flask import Flask

from cuoti_app.database import DB_SCHEMA_VERSION, init_db
from cuoti_app.services.exam_manifests import (
    load_exam_generation_manifest,
    upsert_exam_generation_manifest,
)


class ExamManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "manifest.db"
        app = Flask("exam-manifest-tests")
        app.config["DATABASE"] = str(self.database)
        with app.app_context():
            init_db()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "INSERT INTO background_jobs("
                "id,kind,status,input_hash,available_at,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    "job-1",
                    "exam-generation",
                    "queued",
                    "hash",
                    "2026-07-30T00:00:00+00:00",
                    "2026-07-30T00:00:00+00:00",
                    "2026-07-30T00:00:00+00:00",
                ),
            )
            connection.commit()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_schema_is_version_six_and_manifest_upsert_is_stable(self):
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], DB_SCHEMA_VERSION)

        first = upsert_exam_generation_manifest(
            self.database,
            "job-1",
            "snapshot-a",
            {
                "candidate_ids": ["q-2", "q-1"],
                "chapter_plan": [{"chapter_id": 3, "count": 2}],
            },
        )
        second = upsert_exam_generation_manifest(
            self.database,
            "job-1",
            "snapshot-b",
            {"candidate_ids": ["q-3"], "difficulty_quota": {"困难": 1}},
        )
        loaded = load_exam_generation_manifest(self.database, "job-1")

        self.assertEqual(first["created_at"], second["created_at"])
        self.assertEqual(loaded, second)
        self.assertEqual(loaded["snapshot_id"], "snapshot-b")
        self.assertEqual(loaded["manifest"]["candidate_ids"], ["q-3"])

    def test_manifest_follows_job_foreign_key_lifecycle(self):
        with self.assertRaises(sqlite3.IntegrityError):
            upsert_exam_generation_manifest(
                self.database, "missing-job", "snapshot-a", {}
            )

        upsert_exam_generation_manifest(
            self.database, "job-1", "snapshot-a", {"candidate_ids": []}
        )
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM background_jobs WHERE id='job-1'")
            connection.commit()
        self.assertIsNone(load_exam_generation_manifest(self.database, "job-1"))


if __name__ == "__main__":
    unittest.main()
