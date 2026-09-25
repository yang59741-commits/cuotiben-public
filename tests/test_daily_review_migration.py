from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app import database


class DailyReviewMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def app_for(self, path: Path):
        return create_app(
            {
                "TESTING": True,
                "DATABASE": str(path),
                "PDF_DIR": str(self.root / "pdfs"),
                "REGISTER_JOB_HANDLERS": False,
            }
        )

    def test_empty_database_initializes_directly_at_v59(self):
        path = self.root / "empty.db"
        self.app_for(path)
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], database.DB_SCHEMA_VERSION)
            tables = {
                row[0]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("daily_review_rounds", tables)
            self.assertIn("daily_review_items", tables)
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_full_v58_fixture_upgrades_one_step_and_preserves_rows(self):
        path = self.root / "v58.db"
        old_columns = {
            name: columns
            for name, columns in database.REQUIRED_TABLE_COLUMNS.items()
            if not name.startswith("daily_review_")
        }
        old_indexes = {
            name
            for name in database.REQUIRED_INDEXES
            if "daily_review" not in name
        }
        old_foreign_keys = {
            name: keys
            for name, keys in database.REQUIRED_FOREIGN_KEYS.items()
            if name != "daily_review_items"
        }
        # Build the fixture through the registered migration chain rather than
        # fabricating a downgraded user_version.
        with (
            patch.object(database, "DB_SCHEMA_VERSION", 58),
            patch.object(database, "SCHEMA_VERSION", 58),
            patch.object(database, "REQUIRED_TABLE_COLUMNS", old_columns),
            patch.object(database, "REQUIRED_INDEXES", old_indexes),
            patch.object(database, "REQUIRED_FOREIGN_KEYS", old_foreign_keys),
        ):
            self.app_for(path)

        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 58)
            self.assertIsNotNone(
                db.execute(
                    "SELECT export_context FROM mistake_export_batches LIMIT 0"
                ).description
            )
            mistake_id = db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "date_added,status,updated_at) VALUES('migration-sentinel',1,1,'M-1',"
                "'迁移保留正文','2026-09-14','已掌握','2026-09-14T12:00:00+08:00')"
            ).lastrowid
            batch_id = db.execute(
                "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
                "total_count,volume_count,layout,filter_snapshot_json,artifact_status,"
                "export_context) VALUES('migration-batch','2026-09-14T00:00:00+00:00',"
                "'2026-10-14T00:00:00+00:00',1,1,'compact','{}','published','daily_review')"
            ).lastrowid
            db.commit()

        self.app_for(path)

        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], database.DB_SCHEMA_VERSION)
            self.assertEqual(
                db.execute("SELECT content FROM mistakes WHERE id=?", (mistake_id,)).fetchone()[0],
                "迁移保留正文",
            )
            self.assertEqual(
                db.execute(
                    "SELECT export_context FROM mistake_export_batches WHERE id=?",
                    (batch_id,),
                ).fetchone()[0],
                "daily_review",
            )
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_review_rounds").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM daily_review_items").fetchone()[0], 0)
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_v59_history_survives_export_cleanup_and_source_edits(self):
        path = self.root / 'v59.db'
        with patch.object(database, 'DB_SCHEMA_VERSION', 59), patch.object(database, 'SCHEMA_VERSION', 59):
            self.app_for(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO mistake_export_batches(id,public_id,created_at,expires_at,total_count,volume_count,layout,filter_snapshot_json,artifact_status,export_context) VALUES(1,'old-batch','2026-09-01','2026-10-01',2,1,'compact','{}','published','daily_review')")
            db.execute("INSERT INTO mistake_exports(id,created_at,expires_at,item_count,items_json,artifact_status,batch_id) VALUES(1,'2026-09-01','2026-10-01',2,'[]','published',1)")
            db.execute("INSERT INTO daily_review_rounds(id,round_number,generation_mode,status,created_at,item_count,published_at) VALUES(1,1,'earliest','published','2026-09-01',2,'2026-09-01')")
            for index in (1, 2):
                db.execute("INSERT INTO mistakes(id,public_id,subject_id,chapter_id,qnum,content,date_added) VALUES(?,?,1,1,'Q','升级时可用题面','2026-09-01')", (index, 'legacy-'+str(index)))
                db.execute("INSERT INTO daily_review_items(id,round_id,round_number,mistake_id,export_batch_id,reserved_at,status,created_at) VALUES(?,1,1,?,1,'2026-09-01','published','2026-09-01')", (index, index))
            db.execute("INSERT INTO mistake_export_items(export_id,position,mistake_public_id,subject,chapter,qnum,date_added,reasons,note,content,content_hash) VALUES(1,1,'legacy-1','数学分析','数列极限','原编号','2026-09-01','','原备注','当时的原始题面','original')")
            db.commit()
        app = self.app_for(path)
        with app.app_context():
            db = database.get_db()
            snapshot = json.loads(db.execute('SELECT snapshot_json FROM daily_review_items WHERE id=1').fetchone()[0])
            self.assertEqual(snapshot['content'], '当时的原始题面')
            self.assertEqual(snapshot['snapshot_origin'], 'legacy_export')
            self.assertEqual(snapshot['classification']['status'], 'historical_unknown')
            db.execute('DELETE FROM mistake_exports')
            db.execute('DELETE FROM mistake_export_batches')
            db.execute("UPDATE mistakes SET content='升级后再次修改'")
            db.commit()
            from cuoti_app.services.daily_review import get_round
            session = get_round(db, 1)
            self.assertEqual(session['items'][0]['content'], '当时的原始题面')
            self.assertEqual(session['items'][1]['content'], '升级时可用题面')
            self.assertTrue(session['items'][1]['legacy_live_content'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM daily_review_items WHERE coverage_round=1').fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()
