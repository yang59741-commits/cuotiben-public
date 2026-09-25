from __future__ import annotations

import io
import json
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services.portable_import import PortableImportError, parse_import


class PortableImportV3Tests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(root / "data.db"),
            "ATTACHMENT_DIR": str(root / "attachments"),
            "PDF_DIR": str(root / "pdfs"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def _backup(self, *, public_id=None, note="导入备注", content="导入正文"):
        event_id = str(uuid.uuid4())
        item = {
            "public_id": public_id or str(uuid.uuid4()),
            "subject": "数学分析", "chapter": "引论", "qnum": "A-1",
            "content": content, "note": note, "date_added": "2026-08-06",
            "status": "未掌握", "review_count": 1, "correct_streak": 0,
            "lapse_count": 0, "reasons": ["概念不清"], "tags": ["重点"],
            "history_complete": True,
            "reviews": [{
                "public_id": event_id, "client_event_id": str(uuid.uuid4()),
                "event_type": "again", "reviewed_at": "2026-08-06T10:00:00+08:00",
                "created_at": "2026-08-06T10:00:00+08:00", "status_after": "未掌握",
                "correct_streak": 0, "lapse_count_after": 1,
            }],
            "attachments": [],
        }
        return {"format": "cuoti-portable-backup", "format_version": 3, "mistakes": [item]}, event_id

    def test_preview_and_commit_deep_merge_by_public_id(self):
        public_id = str(uuid.uuid4())
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute("SELECT id FROM subjects WHERE name='数学分析'").fetchone()[0]
            chapter_id = db.execute(
                "SELECT id FROM chapters WHERE subject_id=? AND name='引论'", (subject_id,)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,note,"
                "date_added,status,revision,updated_at,history_complete,fingerprint) "
                "VALUES(?,?,?,?,?,?,?,'未掌握',1,?,1,'local-fp')",
                (public_id, subject_id, chapter_id, "A-1", "本地正文", "", "2026-08-06", "2026-08-06T09:00:00+08:00"),
            )
            db.commit()
        backup, event_id = self._backup(public_id=public_id)
        raw = json.dumps(backup, ensure_ascii=False).encode()
        preview = self.client.post("/api/v3/import/preview?mode=merge", data=raw)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        body = preview.get_json()
        self.assertEqual(body["plan"]["counts"]["merge"], 1)
        self.assertIn("content", body["plan"]["items"][0]["conflict_fields"])
        committed = self.client.post(
            "/api/v3/import/commit?mode=merge", data=raw,
            headers={"X-Import-Preview-Token": body["preview_token"]},
        )
        self.assertEqual(committed.status_code, 200, committed.get_json())
        with self.app.app_context():
            db = get_db()
            row = db.execute("SELECT content,note FROM mistakes WHERE public_id=?", (public_id,)).fetchone()
            self.assertEqual(tuple(row), ("本地正文", "导入备注"))
            self.assertEqual(db.execute("SELECT count(*) FROM review_events WHERE public_id=?", (event_id,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM mistake_tags").fetchone()[0], 1)

        second_preview = self.client.post(
            "/api/v3/import/preview?mode=merge", data=raw
        ).get_json()
        second_commit = self.client.post(
            "/api/v3/import/commit?mode=merge",
            data=raw,
            headers={
                "X-Import-Preview-Token": second_preview["preview_token"],
                "X-Import-Resolutions": json.dumps({"1": ["content"]}),
            },
        )
        self.assertEqual(second_commit.status_code, 200, second_commit.get_json())
        with self.app.app_context():
            db = get_db()
            self.assertEqual(
                db.execute(
                    "SELECT content FROM mistakes WHERE public_id=?", (public_id,)
                ).fetchone()[0],
                "导入正文",
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM review_events WHERE public_id=?", (event_id,)
                ).fetchone()[0],
                1,
            )

    def test_file_and_database_changes_require_new_preview(self):
        backup, _event_id = self._backup()
        raw = json.dumps(backup, ensure_ascii=False).encode()
        token = self.client.post("/api/v3/import/preview", data=raw).get_json()["preview_token"]
        changed = raw + b" "
        response = self.client.post(
            "/api/v3/import/commit", data=changed,
            headers={"X-Import-Preview-Token": token},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "IMPORT_CHANGED")

        token = self.client.post("/api/v3/import/preview", data=raw).get_json()["preview_token"]
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute("SELECT id FROM subjects WHERE name='数学分析'").fetchone()[0]
            db.execute(
                "INSERT INTO mistakes(subject_id,qnum,date_added,status,content,updated_at) "
                "VALUES(?,'changed','2026-08-06','未掌握','changed','changed')",
                (subject_id,),
            )
            db.commit()
        response = self.client.post(
            "/api/v3/import/commit", data=raw,
            headers={"X-Import-Preview-Token": token},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "DATABASE_CHANGED")

    def test_expired_token_and_legacy_fingerprint_ambiguity_are_explicit(self):
        backup, _event_id = self._backup()
        raw = json.dumps(backup, ensure_ascii=False).encode()
        preview = self.client.post("/api/v3/import/preview", data=raw).get_json()
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE import_previews SET expires_at='2000-01-01T00:00:00+00:00'"
            )
            db.commit()
        expired = self.client.post(
            "/api/v3/import/commit",
            data=raw,
            headers={"X-Import-Preview-Token": preview["preview_token"]},
        )
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(expired.get_json()["code"], "PREVIEW_TOKEN_EXPIRED")

        legacy = [{
            "subject": "数学分析",
            "chapter": "引论",
            "qnum": "LEGACY-AMBIGUOUS",
            "content": "旧格式歧义测试",
            "date_added": "2026-08-06",
            "reviews": [],
        }]
        legacy_raw = json.dumps(legacy, ensure_ascii=False).encode()
        first_plan = self.client.post(
            "/api/v3/import/preview", data=legacy_raw
        ).get_json()["plan"]
        fingerprint = first_plan["items"][0]["identity"]["fingerprint"]
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute(
                "SELECT id FROM subjects WHERE name='数学分析'"
            ).fetchone()[0]
            db.executemany(
                "INSERT INTO mistakes(subject_id,qnum,content,date_added,status,fingerprint) "
                "VALUES(?,?,'本地候选','2026-08-06','未掌握',?)",
                [
                    (subject_id, "LOCAL-A", fingerprint),
                    (subject_id, "LOCAL-B", fingerprint),
                ],
            )
            db.commit()
        ambiguous = self.client.post(
            "/api/v3/import/preview", data=legacy_raw
        ).get_json()
        self.assertTrue(ambiguous["legacy"])
        self.assertEqual(ambiguous["plan"]["counts"]["ambiguous"], 1)
        rejected = self.client.post(
            "/api/v3/import/commit",
            data=legacy_raw,
            headers={"X-Import-Preview-Token": ambiguous["preview_token"]},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.get_json()["code"], "UNRESOLVED_CONFLICT")

    def test_zip_attachment_hash_and_future_versions_are_rejected(self):
        backup, _event_id = self._backup()
        backup["mistakes"][0]["attachments"] = [{
            "archive_path": "attachments/a.png", "byte_size": 3, "sha256": "0" * 64,
            "extension": ".png", "mime_type": "image/png",
        }]
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(backup, ensure_ascii=False))
            archive.writestr("attachments/a.png", b"abc")
        with self.assertRaisesRegex(PortableImportError, "SHA-256"):
            parse_import(output.getvalue())
        with self.assertRaises(PortableImportError):
            parse_import(json.dumps({"schema_version": 99, "mistakes": []}).encode())

    def test_replace_creates_verified_recovery_unit(self):
        backup, _event_id = self._backup()
        raw = json.dumps(backup, ensure_ascii=False).encode()
        preview = self.client.post("/api/v3/import/preview?mode=replace", data=raw).get_json()
        response = self.client.post(
            "/api/v3/import/commit?mode=replace",
            data=raw,
            headers={"X-Import-Preview-Token": preview["preview_token"]},
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        recovery = Path(response.get_json()["recovery_backup"])
        self.assertTrue((recovery / "cuoti.db").is_file())
        snapshot = json.loads((recovery / "SNAPSHOT.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["database"]["quick_check"], "ok")
        self.assertEqual(snapshot["snapshot_kind"], "import-replace-recovery")


if __name__ == "__main__":
    unittest.main()
