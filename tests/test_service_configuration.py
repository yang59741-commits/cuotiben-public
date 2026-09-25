from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.database import init_db
from cuoti_app.services.ai import (
    DeepSeekConfigurationError,
    deepseek_api_call,
)


class ServiceConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.root / "test.db"),
                "PDF_DIR": str(self.root / "pdfs"),
                "TEX_BASE": "",
                "PAST_EXAM_DIR": "",
                "DEEPSEEK_API_KEY": "",
                "MATHPIX_APP_ID": "",
                "MATHPIX_APP_KEY": "",
            }
        )
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    @patch("cuoti_app.services.ai_providers.direct_urlopen")
    def test_deepseek_missing_key_fails_before_network(self, urlopen):
        with self.app.app_context():
            with self.assertRaises(DeepSeekConfigurationError):
                deepseek_api_call([{"role": "user", "content": "test"}])
        urlopen.assert_not_called()

    def test_unknown_ai_task_policy_is_a_configuration_error(self):
        with self.app.app_context():
            with self.assertRaisesRegex(DeepSeekConfigurationError, "未知 AI 任务策略"):
                deepseek_api_call(
                    [{"role": "user", "content": "test"}],
                    task_policy="classification_typo",
                )

    def test_ai_route_reports_configuration_without_secret_details(self):
        response = self.client.post(
            "/api/analyze",
            json={"problem": "求极限", "subject": "数学分析", "chapter": "函数极限"},
        )
        self.assertEqual(response.status_code, 503)
        self.assertIn("DEEPSEEK_API_KEY", response.get_json()["error"])
        self.assertNotIn("Bearer", response.get_data(as_text=True))

    def test_pdf_ocr_disabled_and_tex_library_fail_fast_when_unconfigured(self):
        ocr = self.client.post(
            "/internal/jobs/ocr-pdf",
            data={"file": (io.BytesIO(b"%PDF-test"), "test.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(ocr.status_code, 410)
        self.assertEqual(ocr.get_json()["code"], "pdf_ocr_disabled")

        library = self.client.post(
            "/api/add-problem",
            json={"subject_id": 1, "chapter_id": 1, "content": "测试题目"},
        )
        self.assertEqual(library.status_code, 503)
        self.assertIn("CUOTI_TEX_BASE", library.get_json()["error"])

    def test_startup_removes_only_obsolete_mathpix_settings(self):
        settings = self.root / "obsolete-settings.json"
        settings.write_text(
            json.dumps(
                {
                    "mathpix_app_id": "retired-id",
                    "mathpix_app_key": "retired-key",
                    "preserve": {"theme": "paper"},
                    "settings_revision": 4,
                }
            ),
            encoding="utf-8",
        )
        create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.root / "cleanup.db"),
                "SETTINGS_PATH": str(settings),
            }
        )
        cleaned = json.loads(settings.read_text(encoding="utf-8"))
        self.assertNotIn("mathpix_app_id", cleaned)
        self.assertNotIn("mathpix_app_key", cleaned)
        self.assertEqual(cleaned["preserve"], {"theme": "paper"})
        self.assertEqual(cleaned["settings_revision"], 5)

    @patch("cuoti_app.routes.ai.deepseek_api_call")
    def test_unexpected_ai_error_is_not_reflected_to_client(self, deepseek):
        deepseek.side_effect = RuntimeError(
            r"private-key-value C:\private\settings.json"
        )
        response = self.client.post(
            "/api/analyze",
            json={"problem": "求极限", "subject": "数学分析", "chapter": "函数极限"},
        )
        self.assertEqual(response.status_code, 502)
        body = response.get_data(as_text=True)
        self.assertNotIn("private-key-value", body)
        self.assertNotIn("settings.json", body)


if __name__ == "__main__":
    unittest.main()
