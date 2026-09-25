from __future__ import annotations

import io
import os
import sqlite3
import tempfile
import unittest
import json
import zipfile
from pathlib import Path

from PIL import Image

from cuoti_app import create_app
from cuoti_app.services.portable_backup import PORTABLE_BACKUP_FORMAT_VERSION
from cuoti_app.services.attachments import reconcile_attachments


def png_bytes() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (12, 8), (20, 40, 60)).save(stream, format="PNG")
    return stream.getvalue()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "test.db"
        self.attachments = self.root / "attachments"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.database),
                "ATTACHMENT_DIR": str(self.attachments),
                "PDF_DIR": str(self.root / "pdf"),
                "TEX_BASE": str(self.root / "tex"),
                "PAST_EXAM_DIR": str(self.root / "past"),
                "QUESTION_BANK_DATABASE": str(self.root / "question-bank.db"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def create_mistake(self):
        response = self.client.post(
            "/api/v2/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "A-1",
                "content": "附件测试",
                "date_added": "2026-08-06",
            },
        )
        self.assertEqual(response.status_code, 201, response.get_data(as_text=True))
        return response.get_json()

    def upload(self, public_id, revision, content=None):
        return self.client.post(
            f"/api/v2/mistakes/{public_id}/attachments",
            data={
                "revision": str(revision),
                "file": (io.BytesIO(content if content is not None else png_bytes()), "a.png"),
            },
            content_type="multipart/form-data",
        )

    def test_upload_list_content_conflict_and_soft_delete(self):
        mistake = self.create_mistake()
        uploaded = self.upload(mistake["public_id"], 1)
        self.assertEqual(uploaded.status_code, 201, uploaded.get_data(as_text=True))
        attachment = uploaded.get_json()
        self.assertEqual(attachment["revision"], 2)
        self.assertEqual(attachment["mime_type"], "image/png")

        listing = self.client.get(
            f"/api/v2/mistakes/{mistake['public_id']}/attachments"
        ).get_json()["items"]
        self.assertEqual(len(listing), 1)
        self.assertNotIn("object_key", listing[0])
        content = self.client.get(
            f"/api/v2/attachments/{attachment['public_id']}/content"
        )
        self.assertEqual(content.status_code, 200)
        self.assertEqual(content.mimetype, "image/png")
        content.close()

        stale = self.upload(mistake["public_id"], 1)
        self.assertEqual(stale.status_code, 409)
        invalid = self.upload(mistake["public_id"], 2, b"not-an-image")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(len(list(self.attachments.glob("*.png"))), 1)

        deleted = self.client.delete(
            f"/api/v2/mistakes/{mistake['public_id']}", json={"revision": 2}
        )
        self.assertEqual(deleted.status_code, 200)
        hidden = self.client.get(
            f"/api/v2/attachments/{attachment['public_id']}/content"
        )
        self.assertEqual(hidden.status_code, 404)

    def test_permanent_purge_removes_row_and_attachment(self):
        mistake = self.create_mistake()
        attachment = self.upload(mistake["public_id"], 1).get_json()
        self.client.delete(
            f"/api/v2/mistakes/{mistake['public_id']}", json={"revision": 2}
        )
        refused = self.client.delete(
            f"/api/v2/trash/mistakes/{mistake['public_id']}",
            json={"revision": 3},
        )
        self.assertEqual(refused.status_code, 400)
        purged = self.client.delete(
            f"/api/v2/trash/mistakes/{mistake['public_id']}",
            json={"revision": 3, "confirm": True},
        )
        self.assertEqual(purged.status_code, 200, purged.get_data(as_text=True))
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM mistakes WHERE public_id=?",
                    (mistake["public_id"],),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()
        self.assertFalse(list(self.attachments.glob(".purge-*")))
        self.assertEqual(
            self.client.get(
                f"/api/v2/attachments/{attachment['public_id']}/content"
            ).status_code,
            404,
        )

    def test_reconcile_staged_publication(self):
        mistake = self.create_mistake()
        connection = sqlite3.connect(self.database)
        mistake_id = connection.execute(
            "SELECT id FROM mistakes WHERE public_id=?", (mistake["public_id"],)
        ).fetchone()[0]
        key = "11111111-1111-4111-8111-111111111111.png"
        staged = self.attachments / f".{key}.staged"
        staged.write_bytes(png_bytes())
        with connection:
            connection.execute(
                "INSERT INTO attachments(public_id,mistake_id,object_key,mime_type,extension,"
                "byte_size,width,height,sha256,sort_order,status,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    "22222222-2222-4222-8222-222222222222",
                    mistake_id,
                    key,
                    "image/png",
                    ".png",
                    staged.stat().st_size,
                    12,
                    8,
                    "0" * 64,
                    0,
                    "staged",
                    "2026-08-06T00:00:00+00:00",
                ),
            )
        connection.close()
        result = reconcile_attachments(str(self.database), str(self.attachments))
        self.assertEqual(result["repaired"], 1)
        self.assertTrue((self.attachments / key).is_file())

    def test_portable_zip_contains_manifest_and_verified_attachment_body(self):
        mistake = self.create_mistake()
        uploaded = self.upload(mistake["public_id"], 1).get_json()
        response = self.client.get("/api/v3/export")
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.data)) as archive:
            self.assertIn("manifest.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format_version"], PORTABLE_BACKUP_FORMAT_VERSION)
            self.assertEqual(len(manifest["mistakes"]), 1)
            exported = manifest["mistakes"][0]
            self.assertEqual(exported["public_id"], mistake["public_id"])
            metadata = exported["attachments"][0]
            body = archive.read(metadata["archive_path"])
            self.assertEqual(len(body), metadata["byte_size"])
            import hashlib

            self.assertEqual(hashlib.sha256(body).hexdigest(), metadata["sha256"])
            self.assertEqual(metadata["public_id"], uploaded["public_id"])
        response.close()

    def test_portable_zip_refuses_tampered_attachment(self):
        mistake = self.create_mistake()
        self.upload(mistake["public_id"], 1)
        connection = sqlite3.connect(self.database)
        try:
            key = connection.execute("SELECT object_key FROM attachments").fetchone()[0]
        finally:
            connection.close()
        (self.attachments / key).write_bytes(b"tampered")
        response = self.client.get("/api/v3/export")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "ATTACHMENT_INTEGRITY")
        response.close()

    def test_reconcile_restores_interrupted_purge_staging(self):
        mistake = self.create_mistake()
        self.upload(mistake["public_id"], 1)
        self.client.delete(
            f"/api/v2/mistakes/{mistake['public_id']}", json={"revision": 2}
        )
        connection = sqlite3.connect(self.database)
        try:
            key = connection.execute("SELECT object_key FROM attachments").fetchone()[0]
        finally:
            connection.close()
        purge = self.attachments / ".purge-interrupted"
        purge.mkdir()
        (purge / "manifest.json").write_text(
            json.dumps({"public_id": mistake["public_id"], "object_keys": [key]}),
            encoding="utf-8",
        )
        os.replace(self.attachments / key, purge / key)
        result = reconcile_attachments(str(self.database), str(self.attachments))
        self.assertEqual(result["purge_restored"], 1)
        self.assertTrue((self.attachments / key).is_file())
        self.assertFalse(purge.exists())


if __name__ == "__main__":
    unittest.main()
