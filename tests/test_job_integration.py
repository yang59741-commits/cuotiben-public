from __future__ import annotations

import io
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.database import get_db, init_db
from cuoti_app.services.ai import DeepSeekServiceError
from cuoti_app.services.jobs import (
    JobContext,
    JobCoordinator,
    SUPPORTED_JOB_KINDS,
    registered_job_handlers,
    unregister_job_handler,
)
from cuoti_app.services.job_handlers import _bounded_error_message


class JobBusinessIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "jobs.db"
        self.pdf_dir = self.root / "pdfs"
        self.tex_dir = self.root / "tex"
        self.past_dir = self.root / "past"
        self.input_dir = self.root / "inputs"
        for directory in (self.pdf_dir, self.tex_dir, self.past_dir, self.input_dir):
            directory.mkdir()
        (self.tex_dir / "chapterB3_01_引论.tex").write_text(
            "\n".join(
                (
                    f"\\subsection{{题目{index}}}\n"
                    f"JOB_INDEX_{index} 已审核的引论测试题目，"
                    "用于后台任务的有效题库索引验证。"
                )
                for index in range(1, 9)
            ),
            encoding="utf-8",
        )
        with patch("cuoti_app.config._local_settings", return_value={}):
            self.app = create_app(
                {
                    "TESTING": True,
                    "REGISTER_JOB_HANDLERS": True,
                    "DATABASE": str(self.db_path),
                    "PDF_DIR": str(self.pdf_dir),
                    "TEX_BASE": str(self.tex_dir),
                    "QUESTION_BANK_DATABASE": str(
                        self.root / "question_bank.sqlite3"
                    ),
                    "PAST_EXAM_DIR": str(self.past_dir),
                    "JOB_INPUT_DIR": str(self.input_dir),
                    "DEEPSEEK_API_KEY": "test-only",
                    "MATHPIX_APP_ID": "test-id",
                    "MATHPIX_APP_KEY": "test-key",
                    "XELATEX_COMMAND": "test-xelatex",
                }
            )
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        for kind in SUPPORTED_JOB_KINDS:
            unregister_job_handler(kind)
        self.tempdir.cleanup()

    def coordinator(self):
        return JobCoordinator(self.db_path, lease_seconds=2)

    def test_long_job_error_keeps_the_actionable_tail(self):
        message = "prefix-" + ("x" * 2500) + "-actual-tex-error"
        bounded = _bounded_error_message(message)
        self.assertLessEqual(len(bounded), 2000)
        self.assertTrue(bounded.startswith("prefix-"))
        self.assertTrue(bounded.endswith("-actual-tex-error"))

    def test_batch_mistake_job_and_route_replay_write_once(self):
        payload = {
            "problems": [
                {
                    "chapter": "数学分析:引论",
                    "qnum": "1",
                    "content": "临时任务错题",
                }
            ],
            "date_added": "2026-07-27",
            "reasons": "计算错误",
            "note": "",
        }
        coordinator = self.coordinator()
        job, _created = coordinator.submit(
            "batch-mistakes", payload, idempotency_key="batch-once"
        )
        completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["count"], 1)

        replay = self.client.post(
            "/internal/jobs/batch-mark-mistakes", json={**payload, "_job_id": job["id"]}
        )
        self.assertEqual(replay.status_code, 200)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0], 1
            )
        finally:
            connection.close()

    def test_data_import_replays_business_commit_after_worker_crash_gap(self):
        backup = {
            "schema_version": 2,
            "mistakes": [
                {
                    "subject": "数学分析",
                    "chapter": "引论",
                    "qnum": "IMPORT-JOB",
                    "content": "后台导入幂等测试",
                    "reasons": "概念不清",
                    "note": "",
                    "date_added": "2026-07-27",
                    "status": "未掌握",
                    "review_count": 0,
                    "correct_streak": 0,
                    "lapse_count": 0,
                    "reviews": [],
                }
            ],
        }
        coordinator = self.coordinator()
        job, _created = coordinator.submit(
            "data-import", {"backup": backup, "mode": "merge"},
            idempotency_key="import-once",
        )
        from cuoti_app.services.legacy_import import import_legacy_backup
        with self.app.app_context():
            import_legacy_backup(
                get_db(), raw=json.dumps(backup).encode(), mode="merge",
                source_job_id=job["id"],
            )
        completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["result"]["idempotency_replayed"])
        self.assertEqual(completed["progress"]["total"], 4)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM job_business_results").fetchone()[0], 1)
        finally:
            connection.close()

    def test_v3_import_is_staged_and_committed_by_heavy_job(self):
        backup = {
            "format": "cuoti-portable-backup",
            "format_version": 3,
            "mistakes": [{
                "public_id": str(uuid.uuid4()),
                "subject": "数学分析",
                "chapter": "引论",
                "qnum": "IMPORT-V3-JOB",
                "content": "后台 v3 导入测试",
                "note": "",
                "date_added": "2026-08-06",
                "status": "未掌握",
                "review_count": 0,
                "correct_streak": 0,
                "lapse_count": 0,
                "reasons": [],
                "tags": [],
                "history_complete": True,
                "reviews": [],
                "attachments": [],
            }],
        }
        raw = json.dumps(backup, ensure_ascii=False).encode("utf-8")
        preview = self.client.post("/api/v3/import/preview?mode=merge", data=raw)
        self.assertEqual(preview.status_code, 200, preview.get_json())
        token = preview.get_json()["preview_token"]
        staged = self.client.post(
            "/api/v3/import/stage?mode=merge",
            data=raw,
            headers={"X-Import-Preview-Token": token},
        )
        self.assertEqual(staged.status_code, 200, staged.get_json())
        staged_path = self.input_dir / staged.get_json()["staged_file"]
        self.assertTrue(staged_path.is_file())

        submitted = self.client.post(
            "/api/v2/jobs/data-import",
            json={"input": {
                **staged.get_json(),
                "preview_token": token,
                "content_type": "application/json",
                "mode": "merge",
            }},
            headers={"Idempotency-Key": "v3-import-heavy-job"},
        )
        self.assertEqual(submitted.status_code, 202, submitted.get_json())
        coordinator = JobCoordinator(
            self.db_path,
            lease_seconds=2,
            staged_input_roots=(self.input_dir,),
        )
        completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual(completed["result"]["inserted"], 1)
        self.assertFalse(staged_path.exists())
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM mistakes WHERE qnum='IMPORT-V3-JOB'"
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_exam_recompile_job_uses_frozen_source_and_replays_once(self):
        source = "\\documentclass{article}\\begin{document}冻结源\\end{document}"
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO exams(title,config,content,created_at,artifact_status,"
                "latex_source,latex_sha256,recompile_available,artifact_revision) "
                "VALUES('重编译测试','{}','冻结题目','2026-08-06 10:00','published',?,?,1,1)",
                (source, hashlib.sha256(source.encode()).hexdigest()),
            )
            exam_id = cursor.lastrowid
            db.commit()
        (self.pdf_dir / f"exam_{exam_id}.pdf").write_bytes(b"old pdf")
        (self.pdf_dir / f"exam_{exam_id}.tex").write_text("old tex", encoding="utf-8")

        def fake_compile(pdf_dir, _command, stem, latex, **_kwargs):
            Path(pdf_dir, stem + ".pdf").write_bytes(b"%PDF-1.7\nrecompiled")
            Path(pdf_dir, stem + ".tex").write_text(latex, encoding="utf-8")

        coordinator = self.coordinator()
        job, _created = coordinator.submit(
            "exam-recompile", {"exam_id": exam_id}, idempotency_key="recompile-once"
        )
        with patch(
            "cuoti_app.services.exam_artifacts.compile_latex_artifact",
            side_effect=fake_compile,
        ):
            completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded", completed)
        self.assertEqual(completed["result"]["artifact_revision"], 2)
        self.assertEqual(
            (self.pdf_dir / f"exam_{exam_id}.tex").read_text(encoding="utf-8"),
            source,
        )

        replay = registered_job_handlers()["exam-recompile"](
            JobContext(coordinator, job["id"]), {"exam_id": exam_id}
        )
        self.assertTrue(replay.result["idempotency_replayed"])
        connection = sqlite3.connect(self.db_path)
        try:
            revision = connection.execute(
                "SELECT artifact_revision FROM exams WHERE id=?", (exam_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(revision, 2)

    def test_mistake_export_replays_published_result_without_recompile(self):
        created = self.client.post(
            "/internal/legacy/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "EXPORT-JOB",
                "content": "后台导出幂等测试",
                "date_added": "2026-07-27",
            },
        )
        mistake_id = created.get_json()["id"]
        coordinator = self.coordinator()
        job, _created = coordinator.submit(
            "mistake-export", {"mistake_ids": [mistake_id]},
            idempotency_key="export-once",
        )

        def publish(pdf_dir, _command, stem, _latex, **_kwargs):
            Path(pdf_dir, f"{stem}.pdf").write_bytes(b"%PDF-1.7\njob")

        with patch(
            "cuoti_app.routes.mistake_exports.compile_latex_artifact",
            side_effect=publish,
        ) as compile_pdf:
            committed = self.client.post(
                "/internal/jobs/mistake-exports",
                json={"mistake_ids": [mistake_id], "_job_id": job["id"]},
            )
            self.assertEqual(committed.status_code, 201)
            completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded")
        self.assertTrue(completed["result"]["idempotency_replayed"])
        self.assertEqual(completed["progress"]["total"], 4)
        self.assertEqual(compile_pdf.call_count, 1)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mistake_exports").fetchone()[0], 1)
        finally:
            connection.close()

    @patch(
        "cuoti_app.services.job_handlers.deepseek_api_call",
        return_value=(
            '{"items":[{"index":1,"note_assessment":"无备注",'
            '"correction":"","hint":"临时分析","exam_pattern":""}]}'
        ),
    )
    def test_analysis_job_infers_day_operation(self, _deepseek):
        created = self.client.post(
            "/internal/legacy/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "1",
                "content": "临时分析题",
                "date_added": "2026-07-27",
            },
        )
        self.assertEqual(created.status_code, 200)
        coordinator = self.coordinator()
        coordinator.submit(
            "analysis", {"date": "2026-07-27"}, idempotency_key="day-analysis"
        )
        completed = coordinator.run_once()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(
            completed["result"]["items"][0]["analysis"]["hint"], "临时分析"
        )
        _deepseek.assert_called_once()

    @patch("cuoti_app.services.job_handlers.deepseek_api_call")
    def test_analysis_job_preserves_safe_service_error(self, deepseek):
        deepseek.side_effect = DeepSeekServiceError("AI 服务连接失败（已重试 3 次）")
        created = self.client.post(
            "/internal/legacy/mistakes",
            json={
                "subject_id": 1,
                "chapter_id": 1,
                "qnum": "1",
                "content": "临时分析题",
                "date_added": "2026-07-27",
            },
        )
        self.assertEqual(created.status_code, 200)
        coordinator = self.coordinator()
        coordinator.submit(
            "analysis",
            {"date": "2026-07-27"},
            idempotency_key="day-analysis-error",
            max_attempts=1,
        )

        completed = coordinator.run_once()

        self.assertEqual(completed["status"], "failed")
        self.assertEqual(
            completed["error"]["message"],
            "当日分析失败：AI 服务连接失败（已重试 3 次）",
        )

    @patch("cuoti_app.services.ocr.extract_docx_text", return_value="DOCX-TEXT")
    def test_docx_upload_job_removes_staged_file_after_success(self, _extract):
        response = self.client.post(
            "/api/v2/jobs/ocr",
            data={"file": (io.BytesIO(b"synthetic-docx"), "sample.docx")},
            content_type="multipart/form-data",
            headers={"Idempotency-Key": "ocr-once"},
        )
        self.assertEqual(response.status_code, 202, response.get_data(as_text=True))
        self.assertEqual(len(list(self.input_dir.iterdir())), 1)
        completed = self.coordinator().run_once()
        self.assertEqual(completed["status"], "succeeded")
        self.assertEqual(completed["result"]["text"], "DOCX-TEXT")
        self.assertEqual(list(self.input_dir.iterdir()), [])

    def test_pdf_ocr_job_is_rejected_before_staging(self):
        response = self.client.post(
            "/api/v2/jobs/ocr",
            data={"file": (io.BytesIO(b"must-not-be-uploaded"), "sample.pdf")},
            content_type="multipart/form-data",
            headers={"Idempotency-Key": "pdf-disabled"},
        )
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.get_json()["code"], "pdf_ocr_disabled")
        self.assertEqual(list(self.input_dir.iterdir()), [])

    @patch("cuoti_app.routes.exams_legacy.compile_exam")
    @patch(
        "cuoti_app.routes.exams_legacy.deepseek_api_call",
        return_value=r"\begin{problem}临时试题\end{problem}",
    )
    def test_exam_route_replays_published_job_result(self, deepseek, compile_exam):
        def publish(pdf_dir, _command, exam_id, _latex, **_kwargs):
            Path(pdf_dir, f"exam_{exam_id}.pdf").write_bytes(b"pdf")
            Path(pdf_dir, f"exam_{exam_id}.tex").write_text("tex", encoding="utf-8")

        compile_exam.side_effect = publish
        payload = {
            "source": "free",
            "subject_id": 1,
            "chapters": [1],
            "difficulty": "中等",
            "question_count": 1,
            "types": ["计算题"],
            "note": "",
            "_job_id": "a" * 32,
        }
        first = self.client.post("/internal/jobs/generate-exam", json=payload)
        second = self.client.post("/internal/jobs/generate-exam", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["idempotency_replayed"])
        self.assertEqual(first.get_json()["id"], second.get_json()["id"])
        self.assertEqual(deepseek.call_count, 1)
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 1
            )
        finally:
            connection.close()

    @patch("cuoti_app.services.job_handlers_generation.generate_exam")
    @patch("cuoti_app.services.tex.shutil.which", return_value="xelatex")
    @patch("cuoti_app.services.job_process.subprocess.Popen")
    def test_exam_job_cancel_terminates_xelatex_and_publishes_nothing(
        self,
        popen,
        _which,
        generate_exam,
    ):
        process_started = threading.Event()
        process_terminated = threading.Event()

        class BlockingCompiler:
            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15
                process_terminated.set()

            def wait(self, timeout=None):
                del timeout
                return self.returncode

            def kill(self):
                self.returncode = -9
                process_terminated.set()

        def start_compiler(*_args, **_kwargs):
            process_started.set()
            return BlockingCompiler()

        popen.side_effect = start_compiler

        def compile_through_current_runtime(_payload, runtime):
            # Exercise the handler's injected cancellable process runner
            # without making this process-ownership test depend on semantic,
            # material-understanding, or Provider stages that precede TeX.
            runtime.compile_exam(
                runtime.config["PDF_DIR"],
                runtime.config["XELATEX_COMMAND"],
                1,
                r"\documentclass{article}\begin{document}test\end{document}",
            )

        generate_exam.side_effect = compile_through_current_runtime
        coordinator = self.coordinator()
        job, _created = coordinator.submit(
            "exam-generation",
            {
                "source": "free",
                "subject_id": 1,
                "chapters": [1],
                "difficulty": "中等",
                "question_count": 1,
                "types": ["计算题"],
                "note": "",
            },
        )
        worker = threading.Thread(target=coordinator.run_once)
        worker.start()
        self.assertTrue(process_started.wait(2))
        requested = coordinator.request_cancel(job["id"])
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertTrue(process_terminated.wait(2))
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(coordinator.get_job(job["id"])["status"], "cancelled")
        self.assertEqual(list(self.pdf_dir.glob("exam_*")), [])
        connection = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM job_business_results WHERE job_id=?",
                    (job["id"],),
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT attempts FROM background_jobs WHERE id=?", (job["id"],)
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
