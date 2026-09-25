from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from flask import jsonify

from cuoti_app import create_app


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.database = self.root / "health.db"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.database),
                "PDF_DIR": str(self.root / "pdfs"),
                "SETTINGS_PATH": str(self.root / "settings.json"),
                "TEX_BASE": "",
                "PAST_EXAM_DIR": "",
                "DEEPSEEK_API_KEY": "",
                "JOB_WORKER_ENABLED": False,
                "HEALTH_CACHE_SECONDS": 0,
            }
        )
        self.client = self.app.test_client()

    def test_optional_failures_are_degraded_and_do_not_leak_paths(self):
        response = self.client.get("/api/v2/health")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["contract_version"], "1")
        self.assertEqual(payload["overall_status"], "degraded")
        self.assertTrue(payload["checked_at"])
        self.assertTrue(payload["checks"])
        for check in payload["checks"]:
            self.assertEqual(
                set(check),
                {
                    "check_id",
                    "status",
                    "affected_features",
                    "message",
                    "retryable",
                },
            )
        body = response.get_data(as_text=True)
        self.assertNotIn(str(self.root), body)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_database_failure_is_fatal(self):
        self.database.unlink()
        response = self.client.get("/api/v2/health?refresh=1")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["overall_status"], "fatal")

    def test_settings_page_exposes_manual_runtime_diagnostics(self):
        response = self.client.get("/")
        body = response.get_data(as_text=True)
        self.assertIn('id="runtime-health-refresh"', body)
        script = (
            Path(__file__).resolve().parents[1]
            / "cuoti_app"
            / "static"
            / "js"
            / "settings"
            / "api.js"
        ).read_text(encoding="utf-8")
        self.assertIn("/api/v2/health", script)

    def test_v2_errors_have_safe_stable_envelope(self):
        @self.app.get("/api/v2/test-error-envelope")
        def test_error_envelope():
            return jsonify({"error": r"failed at C:\private\settings.json"}), 503

        response = self.client.get(
            "/api/v2/test-error-envelope",
            headers={"X-Correlation-ID": "test-correlation-123"},
        )
        payload = response.get_json()
        self.assertEqual(
            set(("code", "message", "retryable", "hint", "correlation_id"))
            - set(payload),
            set(),
        )
        self.assertEqual(payload["correlation_id"], "test-correlation-123")
        self.assertTrue(payload["retryable"])
        self.assertNotIn(r"C:\private", response.get_data(as_text=True))

    def test_frontend_uses_nonblocking_toast_and_no_math_delay(self):
        root = Path(__file__).resolve().parents[1] / "cuoti_app"
        template = (root / "templates" / "index.html").read_text(encoding="utf-8")
        core = (root / "static" / "js" / "core.js").read_text(encoding="utf-8")
        mistakes = (root / "static" / "js" / "mistakes.js").read_text(encoding="utf-8")
        self.assertIn('id="toast-region"', template)
        self.assertIn("function showToast", core)
        self.assertIn("await renderMath(root)", mistakes)
        self.assertNotIn("setTimeout(()=>renderMath(div),200)", mistakes)


if __name__ == "__main__":
    unittest.main()
