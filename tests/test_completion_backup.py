from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from pathlib import Path

from tools.create_completion_backup import create_backup
from tools.restore_completion_backup import restore_snapshot, verify_snapshot


def make_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            PRAGMA user_version=6;
            CREATE TABLE subjects(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE chapters(
                id INTEGER PRIMARY KEY,
                subject_id INTEGER NOT NULL REFERENCES subjects(id),
                name TEXT NOT NULL
            );
            CREATE TABLE mistakes(id INTEGER PRIMARY KEY);
            CREATE TABLE mistake_exports(id INTEGER PRIMARY KEY);
            INSERT INTO subjects(name) VALUES('数学分析');
            INSERT INTO chapters(subject_id,name) VALUES(1,'函数极限');
            INSERT INTO mistakes DEFAULT VALUES;
            """
        )
        connection.commit()
    finally:
        connection.close()


class CompletionBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "project"
        self.root.mkdir()
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "instance").mkdir()
        (self.root / "pdf-output").mkdir()
        (self.root / "app.py").write_text("app = object()\n", encoding="utf-8")
        (self.root / "错题本.exe").write_bytes(b"test-executable")
        (self.root / "instance" / "settings.json").write_text(
            '{"ai":{"api_key":"private-test-key"}}\n', encoding="utf-8"
        )
        (self.root / "pdf-output" / "exam_1.pdf").write_bytes(b"%PDF-test")
        self.database = self.root / "configured.db"
        make_database(self.database)
        self.config = {
            "DATABASE": str(self.database),
            "PDF_DIR": str(self.root / "pdf-output"),
            "SETTINGS_PATH": str(self.root / "instance" / "settings.json"),
            "QUESTION_BANK_DATABASE": str(self.root / "missing-index.sqlite3"),
        }

    def test_private_snapshot_is_verified_and_restorable(self):
        snapshot = create_backup(
            self.root,
            label="test-private",
            snapshot_kind="private",
            config=self.config,
        )
        result = verify_snapshot(snapshot)
        self.assertGreater(result["manifest_entries"], 0)
        self.assertEqual(result["sqlite"]["database"]["user_version"], 6)
        self.assertTrue((snapshot / "pdfs" / "exam_1.pdf").is_file())
        self.assertTrue((snapshot / "instance" / "settings.json").is_file())

        restored = self.root.parent / "restored"
        restore_snapshot(snapshot, restored)
        self.assertTrue((restored / "cuoti.db").is_file())
        self.assertTrue((restored / "pdfs" / "exam_1.pdf").is_file())

    def test_shareable_snapshot_contains_only_source_and_is_not_restorable(self):
        snapshot = create_backup(
            self.root,
            label="test-shareable",
            snapshot_kind="shareable",
            config=self.config,
        )
        self.assertFalse((snapshot / "instance" / "settings.json").exists())
        self.assertFalse((snapshot / "instance").exists())
        self.assertFalse((snapshot / "cuoti.db").exists())
        self.assertFalse(verify_snapshot(snapshot)["restorable"])
        self.assertNotIn("private-test-key", "\n".join(
            path.read_text(encoding="utf-8") for path in snapshot.rglob("*") if path.is_file()
        ))
        self.assertFalse((snapshot / "pdfs").exists())

    def test_invalid_derived_index_is_marked_for_rebuild(self):
        index = self.root / "question-bank.sqlite3"
        connection = sqlite3.connect(index)
        try:
            connection.executescript(
                """
                PRAGMA foreign_keys=OFF;
                CREATE TABLE questions(id INTEGER PRIMARY KEY);
                CREATE TABLE usage(
                    id INTEGER PRIMARY KEY,
                    question_id INTEGER REFERENCES questions(id)
                );
                INSERT INTO usage(question_id) VALUES(99);
                """
            )
            connection.commit()
        finally:
            connection.close()
        config = dict(self.config, QUESTION_BANK_DATABASE=str(index))
        snapshot = create_backup(
            self.root,
            label="test-invalid-derived",
            snapshot_kind="private",
            config=config,
        )
        payload = json.loads(
            (snapshot / "SNAPSHOT.json").read_text(encoding="utf-8")
        )
        derived = next(
            item for item in payload["assets"]
            if item["asset_id"] == "question_bank_index"
        )
        self.assertEqual(derived["kind"], "rebuild_required")
        self.assertFalse((snapshot / "instance" / "question_bank.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
