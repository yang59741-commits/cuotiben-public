from __future__ import annotations

import json
import io
import tempfile
import unittest
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.database import get_db, init_db
from cuoti_app.services.mistake_exports import cleanup_exports, serialize_items, timestamp
from cuoti_app.services.job_contracts import SUPPORTED_JOB_KINDS
from cuoti_app.services.job_handlers import register_app_job_handlers
from cuoti_app.services.jobs import JobCoordinator, unregister_job_handler
from cuoti_app.services.tex import TexCompileError


class MistakeExportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.database = self.root / "test.db"
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
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def add_mistake(self, content="求极限 $\\lim_{x\\to 0} \\frac{\\sin x}{x}$"):
        response = self.client.post(
            "/internal/legacy/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "例 1",
                "content": content,
                "reasons": "概念不清",
                "note": "复习等价无穷小",
                "date_added": "2026-07-28",
            },
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()["id"]

    def fake_compile(self, pdf_dir, _command, stem, _latex, **kwargs):
        self.assertFalse(kwargs.get("publish_source", True))
        Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\nsynthetic")

    def add_scheduled_mistake(
        self,
        *,
        subject_id: int,
        chapter_id: int,
        qnum: str,
        content: str,
        next_review: str | None,
        status: str = "未掌握",
        deleted_at: str | None = None,
    ) -> int:
        with self.app.app_context():
            db = get_db()
            mistake_id = db.execute(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "date_added,status,next_review,deleted_at,updated_at) "
                "VALUES(?,?,?,?,?,'2026-01-01',?,?,?,'2026-01-01T00:00:00+08:00')",
                (
                    str(uuid.uuid4()),
                    subject_id,
                    chapter_id,
                    qnum,
                    content,
                    status,
                    next_review,
                    deleted_at,
                ),
            ).lastrowid
            db.commit()
            return int(mistake_id)

    def test_create_download_and_history_publish_only_pdf(self):
        first = self.add_mistake()
        second = self.add_mistake("证明闭区间上的连续函数一致连续")
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=self.fake_compile,
        ):
            response = self.client.post(
                "/internal/jobs/mistake-exports", json={"mistake_ids": [first, second]}
            )
        self.assertEqual(response.status_code, 201)
        payload = response.get_json()
        self.assertEqual(payload["item_count"], 2)
        self.assertEqual(len(payload["items"]), 2)
        export_id = payload["id"]
        self.assertTrue((self.pdf_dir / f"mistake_export_{export_id}.pdf").is_file())
        self.assertFalse((self.pdf_dir / f"mistake_export_{export_id}.tex").exists())

        history = self.client.get("/api/v2/mistake-exports?limit=20").get_json()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["id"], export_id)
        download = self.client.get(payload["download_url"])
        self.assertEqual(download.status_code, 200)
        self.assertIn("attachment", download.headers["Content-Disposition"])
        download.close()

    def test_two_hundred_items_publish_two_volumes_with_frozen_manifest(self):
        ids = []
        with self.app.app_context():
            db = get_db()
            for index in range(200):
                cursor = db.execute(
                    "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                    "date_added,status,updated_at) VALUES(?,?,?, ?,?,'2026-08-06',"
                    "'未掌握','2026-08-06T10:00:00+08:00')",
                    (str(uuid.uuid4()), 1, 1, f"B-{index + 1}", f"冻结正文 {index + 1}"),
                )
                ids.append(cursor.lastrowid)
            db.commit()
        compiled_sources = []

        def compile_volume(pdf_dir, _command, stem, latex, **_kwargs):
            compiled_sources.append(latex)
            Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\nvolume")

        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=compile_volume,
        ):
            response = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"mistake_ids": ids, "layout": "two_column"},
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["item_count"], 200)
        self.assertEqual(payload["volume_count"], 2)
        self.assertEqual(len(payload["volumes"]), 2)
        self.assertEqual(len(payload["items"]), 200)
        self.assertTrue(all("twocolumn" in latex for latex in compiled_sources))
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE mistakes SET content='已修改' WHERE id=?", (ids[0],))
            db.commit()
        history = self.client.get("/api/v2/mistake-exports").get_json()["items"][0]
        self.assertEqual(history["items"][0]["content"], "冻结正文 200")
        archive = self.client.get(payload["download_url"])
        self.assertEqual(archive.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
            self.assertEqual(
                sorted(bundle.namelist()),
                ["manifest.json", "错题复习_第1册.pdf", "错题复习_第2册.pdf"],
            )

    def test_notes_layout_adds_writing_space(self):
        mistake_id = self.add_mistake()
        sources = []

        def compile_notes(pdf_dir, _command, stem, latex, **_kwargs):
            sources.append(latex)
            Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\nnotes")

        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=compile_notes,
        ):
            response = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"mistake_ids": [mistake_id], "layout": "notes"},
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        self.assertIn(r"\vspace{4cm}", sources[0])

    def test_server_side_filters_are_frozen_with_resolved_hashes(self):
        selected = self.add_mistake("筛选命中正文")
        self.add_mistake("筛选排除正文")
        with self.app.app_context():
            db = get_db()
            db.execute(
                "INSERT INTO tags(name,normalized_name) VALUES('重点','重点')"
            )
            tag_id = db.execute(
                "SELECT id FROM tags WHERE normalized_name='重点'"
            ).fetchone()[0]
            db.execute(
                "INSERT INTO mistake_tags(mistake_id,tag_id) VALUES(?,?)",
                (selected, tag_id),
            )
            db.commit()
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=self.fake_compile,
        ):
            response = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"filters": {
                    "subject_id": 1,
                    "chapter_id": 1,
                    "status": "未掌握",
                    "tags": ["重点"],
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-31",
                }},
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        payload = response.get_json()
        self.assertEqual(payload["item_count"], 1)
        self.assertEqual(payload["filter_snapshot"]["filters"]["tags"], ["重点"])
        self.assertEqual(len(payload["filter_snapshot"]["resolved"]), 1)

    def test_due_review_export_freezes_scope_labels_and_all_layouts(self):
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute(
                "SELECT id FROM subjects ORDER BY id LIMIT 1"
            ).fetchone()[0]
            chapters = [
                int(row[0])
                for row in db.execute(
                    "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 2",
                    (subject_id,),
                )
            ]
        self.assertEqual(len(chapters), 2)
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[0],
            qnum="D-1",
            content="到期正文一",
            next_review="2000-01-01",
        )
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[0],
            qnum="D-2",
            content="到期正文二",
            next_review=None,
        )
        expected_due_qnums = {"D-1", "D-2"}
        for index in range(3, 33):
            qnum = f"D-{index}"
            expected_due_qnums.add(qnum)
            self.add_scheduled_mistake(
                subject_id=subject_id,
                chapter_id=chapters[0],
                qnum=qnum,
                content=f"未加载页到期正文 {index}",
                next_review="2000-01-01",
            )
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[0],
            qnum="F-1",
            content="未来到期",
            next_review="2999-01-01",
        )
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[0],
            qnum="M-1",
            content="已掌握",
            next_review="2000-01-01",
            status="已掌握",
        )
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[0],
            qnum="T-1",
            content="回收站",
            next_review="2000-01-01",
            deleted_at="2026-01-02T00:00:00+08:00",
        )
        self.add_scheduled_mistake(
            subject_id=subject_id,
            chapter_id=chapters[1],
            qnum="X-1",
            content="其他章节",
            next_review="2000-01-01",
        )

        for layout in ("compact", "notes", "two_column"):
            sources = []

            def compile_due(pdf_dir, _command, stem, latex, **_kwargs):
                sources.append(latex)
                Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\ndue")

            with self.subTest(layout=layout), patch(
                "cuoti_app.routes.mistake_exports.compile_latex_artifact",
                side_effect=compile_due,
            ):
                response = self.client.post(
                    "/internal/jobs/mistake-exports",
                    json={
                        "export_context": "selected_mistakes",
                        "filters": {
                            "review_due": True,
                            "subject_id": subject_id,
                            "chapter_id": chapters[0],
                        },
                        "layout": layout,
                    },
                )
                self.assertEqual(response.status_code, 201, response.get_json())
                payload = response.get_json()
                self.assertEqual(payload["export_context"], "selected_mistakes")
                self.assertEqual(payload["context_label"], "间隔复习")
                self.assertEqual(payload["item_count"], 32)
                self.assertEqual(
                    {item["qnum"] for item in payload["items"]},
                    expected_due_qnums,
                )
                filters = payload["filter_snapshot"]["filters"]
                self.assertIs(filters["review_due"], True)
                self.assertEqual(filters["review_date"], date.today().isoformat())
                self.assertIn("间隔复习册", sources[0])
                self.assertEqual("twocolumn" in sources[0], layout == "two_column")
                self.assertEqual(r"\vspace{4cm}" in sources[0], layout == "notes")
                archive = self.client.get(payload["download_url"])
                self.assertEqual(archive.status_code, 200)
                with zipfile.ZipFile(io.BytesIO(archive.data)) as bundle:
                    self.assertIn("间隔复习_第1册.pdf", bundle.namelist())

        history = self.client.get("/api/v2/mistake-exports").get_json()["items"]
        self.assertEqual(history[0]["context_label"], "间隔复习")

    def test_due_review_export_rejects_conflicts_empty_and_over_limit(self):
        due_id = self.add_scheduled_mistake(
            subject_id=1,
            chapter_id=1,
            qnum="D-1",
            content="到期题",
            next_review="2000-01-01",
        )
        invalid_payloads = (
            {"mistake_ids": [due_id], "filters": {"review_due": True}},
            {"filters": {"review_due": False}},
            {"filters": {"review_due": True, "status": "未掌握"}},
            {"filters": {"review_due": True, "tags": ["重点"]}},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/internal/jobs/mistake-exports", json=payload
                )
                self.assertEqual(response.status_code, 400)

        empty = self.client.post(
            "/internal/jobs/mistake-exports",
            json={"filters": {"review_due": True, "chapter_id": 999999}},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertIn("没有可导出", empty.get_json()["error"])

        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=TexCompileError("synthetic due failure"),
        ):
            failed = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"filters": {"review_due": True, "subject_id": 1}},
            )
        self.assertEqual(failed.status_code, 500)
        with self.app.app_context():
            self.assertEqual(
                get_db().execute(
                    "SELECT COUNT(*) FROM mistake_export_batches"
                ).fetchone()[0],
                0,
            )

        with self.app.app_context():
            db = get_db()
            db.executemany(
                "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,"
                "date_added,status,next_review,updated_at) VALUES(?,?,1,?,?,"
                "'2026-01-01','未掌握','2000-01-01','2026-01-01T00:00:00+08:00')",
                [
                    (str(uuid.uuid4()), 1, f"L-{index}", f"上限题 {index}")
                    for index in range(1000)
                ],
            )
            db.commit()
        over = self.client.post(
            "/internal/jobs/mistake-exports",
            json={"filters": {"review_due": True, "subject_id": 1}},
        )
        self.assertEqual(over.status_code, 400)
        self.assertIn("超过 1000", over.get_json()["error"])

    def test_registered_worker_replays_due_review_export_without_daily_session(self):
        self.add_scheduled_mistake(
            subject_id=1,
            chapter_id=1,
            qnum="W-1",
            content="后台间隔复习",
            next_review="2000-01-01",
        )
        register_app_job_handlers(self.app)
        try:
            coordinator = JobCoordinator(self.database, lease_seconds=2)
            key = uuid.uuid4().hex
            payload = {"filters": {"review_due": True, "subject_id": 1}}
            job, created = coordinator.submit(
                "mistake-export", payload, idempotency_key=key
            )
            replay, replay_created = coordinator.submit(
                "mistake-export", payload, idempotency_key=key
            )
            self.assertTrue(created)
            self.assertFalse(replay_created)
            self.assertEqual(job["id"], replay["id"])
            with patch(
                "cuoti_app.services.mistake_exports.compile_latex_artifact",
                side_effect=self.fake_compile,
            ) as compiler:
                completed = coordinator.run_once()
            self.assertEqual(completed["status"], "succeeded", completed)
            self.assertEqual(completed["result"]["context_label"], "间隔复习")
            self.assertEqual(compiler.call_count, 1)
            with self.app.app_context():
                db = get_db()
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM daily_review_rounds").fetchone()[0],
                    0,
                )
        finally:
            for kind in SUPPORTED_JOB_KINDS:
                unregister_job_handler(kind)

    def test_request_validation_rejects_empty_duplicate_and_oversized_ids(self):
        mistake_id = self.add_mistake()
        for body in (
            {},
            {"mistake_ids": []},
            {"mistake_ids": [mistake_id, mistake_id]},
            {"mistake_ids": list(range(1, 1002))},
            {"mistake_ids": [True]},
        ):
            with self.subTest(body=body):
                response = self.client.post("/internal/jobs/mistake-exports", json=body)
                self.assertEqual(response.status_code, 400)

    def test_missing_and_unsafe_mistakes_leave_no_history(self):
        unsafe_id = self.add_mistake(r"\input{C:/secret.txt}")
        missing = self.client.post(
            "/internal/jobs/mistake-exports", json={"mistake_ids": [999999]}
        )
        self.assertEqual(missing.status_code, 400)
        unsafe = self.client.post(
            "/internal/jobs/mistake-exports", json={"mistake_ids": [unsafe_id]}
        )
        self.assertEqual(unsafe.status_code, 400)
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM mistake_exports").fetchone()[0],
                0,
            )

    def test_compile_failure_removes_pending_record_and_artifact(self):
        mistake_id = self.add_mistake()
        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=TexCompileError("synthetic failure"),
        ):
            response = self.client.post(
                "/internal/jobs/mistake-exports", json={"mistake_ids": [mistake_id]}
            )
        self.assertEqual(response.status_code, 500)
        with self.app.app_context():
            self.assertEqual(
                get_db().execute("SELECT COUNT(*) FROM mistake_exports").fetchone()[0],
                0,
            )
        self.assertEqual(list(self.pdf_dir.glob("mistake_export_*")), [])

    def test_cleanup_removes_expired_and_interrupted_exports(self):
        now = datetime(2026, 7, 28, 1, 0, tzinfo=timezone.utc)
        summaries = serialize_items(
            [{"mistake_id": 1, "subject": "数学分析", "chapter": "引论", "qnum": "1"}]
        )
        with self.app.app_context():
            db = get_db()
            expired = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,artifact_status) "
                "VALUES(?,?,?,?,?)",
                (
                    timestamp(now - timedelta(days=31)),
                    timestamp(now - timedelta(days=1)),
                    1,
                    summaries,
                    "published",
                ),
            ).lastrowid
            pending = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,artifact_status) "
                "VALUES(?,?,?,?,?)",
                (timestamp(now), timestamp(now + timedelta(days=30)), 1, summaries, "pending"),
            ).lastrowid
            db.commit()
            for export_id in (expired, pending):
                (self.pdf_dir / f"mistake_export_{export_id}.pdf").write_bytes(b"pdf")
            result = cleanup_exports(
                db,
                self.pdf_dir,
                now=now,
                include_pending=True,
            )
            self.assertEqual(result, {"deleted_records": 2, "deleted_files": 2})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM mistake_exports").fetchone()[0], 0)
        self.assertEqual(list(self.pdf_dir.glob("mistake_export_*")), [])

    def test_history_is_cursor_paginated_and_excludes_expired_rows(self):
        now = datetime.now(timezone.utc)
        summaries = serialize_items(
            [{"mistake_id": 1, "subject": "数学分析", "chapter": "引论", "qnum": "1"}]
        )
        with self.app.app_context():
            db = get_db()
            for index in range(25):
                created_at = timestamp(now + timedelta(seconds=index))
                expires_at = timestamp(now + timedelta(days=30))
                batch_id = db.execute(
                    "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
                    "total_count,volume_count,layout,artifact_status) "
                    "VALUES(?,?,?,1,1,'compact','published')",
                    (str(uuid.uuid4()), created_at, expires_at),
                ).lastrowid
                db.execute(
                    "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,"
                    "artifact_status,batch_id) VALUES(?,?,?,?,?,?)",
                    (
                        created_at, expires_at, 1, summaries, "published", batch_id,
                    ),
                )
            expired_created = timestamp(now - timedelta(days=31))
            expired_at = timestamp(now - timedelta(seconds=1))
            expired_batch = db.execute(
                "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
                "total_count,volume_count,layout,artifact_status) "
                "VALUES(?,?,?,1,1,'compact','published')",
                (str(uuid.uuid4()), expired_created, expired_at),
            ).lastrowid
            expired = db.execute(
                "INSERT INTO mistake_exports(created_at,expires_at,item_count,items_json,"
                "artifact_status,batch_id) VALUES(?,?,?,?,?,?)",
                (
                    expired_created, expired_at, 1, summaries, "published", expired_batch,
                ),
            ).lastrowid
            db.commit()
            (self.pdf_dir / f"mistake_export_{expired}.pdf").write_bytes(b"pdf")

        first = self.client.get("/api/v2/mistake-exports?limit=20").get_json()
        self.assertEqual(first["total"], 25)
        self.assertEqual(len(first["items"]), 20)
        self.assertTrue(first["has_more"])
        second = self.client.get(
            "/api/v2/mistake-exports?limit=20&after=" + first["next_cursor"]
        ).get_json()
        self.assertEqual(len(second["items"]), 5)
        self.assertFalse(second["has_more"])
        self.assertEqual(self.client.get(f"/mistake-exports/{expired}.pdf").status_code, 404)
        self.assertFalse((self.pdf_dir / f"mistake_export_{expired}.pdf").exists())


if __name__ == "__main__":
    unittest.main()
