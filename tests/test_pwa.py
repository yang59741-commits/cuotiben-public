from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jinja2 import FileSystemLoader
from PIL import Image

from cuoti_app import create_app


ROOT = Path(__file__).resolve().parents[1]


class PwaShellTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({"TESTING": True, "DATABASE": str(root / "x.db"), "PDF_DIR": str(root / "pdf")})
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_has_installable_root_scope_and_icons(self):
        response = self.client.get("/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        manifest = json.loads(response.get_data(as_text=True))
        response.close()
        self.assertEqual(manifest["scope"], "/")
        self.assertTrue(manifest["start_url"].startswith("/"))
        self.assertEqual({item["sizes"] for item in manifest["icons"]}, {"192x192", "512x512"})
        self.assertTrue(any(item.get("purpose") == "maskable" for item in manifest["icons"]))
        for name, size in (("pwa-192.png", (192, 192)), ("pwa-512.png", (512, 512)), ("pwa-maskable-512.png", (512, 512))):
            with Image.open(ROOT / "cuoti_app" / "static" / "img" / name) as image:
                self.assertEqual(image.size, size)

    def test_root_service_worker_is_versioned_and_never_queues_api_writes(self):
        response = self.client.get("/sw.js")
        source = response.get_data(as_text=True)
        self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
        self.assertEqual(response.headers["Cache-Control"], "no-cache")
        self.assertNotIn("__CUOTI_BUILD_ID__", source)
        self.assertRegex(source, r"cuoti-shell-[0-9a-f]{20}")
        for script in ("mistakes", "stats", "review", "data", "exam", "library", "settings", "jobs", "remote"):
            self.assertIn(f"/static/js/{script}.js", source)
        self.assertIn("request.method!=='GET'", source)
        self.assertIn("url.pathname.startsWith('/api/')", source)
        self.assertIn("request.mode==='navigate'", source)
        self.assertNotIn("skipWaiting()", source.split("message", 1)[0])
        response.close()

    def test_source_build_id_changes_with_shell_html_and_javascript(self):
        root = Path(self.temporary.name) / "source-shell"
        static = root / "static"
        templates = root / "templates"
        (static / "js").mkdir(parents=True)
        (static / "css").mkdir(parents=True)
        templates.mkdir(parents=True)
        worker = (
            "const BUILD_ID='cuoti-shell-__CUOTI_BUILD_ID__';\n"
            "const SHELL_ASSETS=['/','/static/css/app.css','/static/js/app.js'];\n"
        )
        (static / "sw.js").write_text(worker, encoding="utf-8")
        (static / "css" / "app.css").write_text("body{color:black}", encoding="utf-8")
        (static / "js" / "app.js").write_text("const value=1;", encoding="utf-8")
        (templates / "index.html").write_text("<main>one</main>", encoding="utf-8")
        self.app.static_folder = str(static)
        self.app.jinja_loader = FileSystemLoader(str(templates))

        first = self.client.get("/sw.js").get_data(as_text=True)
        (templates / "index.html").write_text("<main>two</main>", encoding="utf-8")
        second = self.client.get("/sw.js").get_data(as_text=True)
        (static / "js" / "app.js").write_text("const value=2;", encoding="utf-8")
        third = self.client.get("/sw.js").get_data(as_text=True)

        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertRegex(first, r"cuoti-shell-[0-9a-f]{20}")
        self.assertRegex(second, r"cuoti-shell-[0-9a-f]{20}")
        self.assertRegex(third, r"cuoti-shell-[0-9a-f]{20}")

    def test_packaged_service_worker_keeps_its_fixed_build_id(self):
        root = Path(self.temporary.name) / "packaged-shell"
        root.mkdir(parents=True)
        fixed = "const BUILD_ID='cuoti-shell-release-20260906';\n"
        (root / "sw.js").write_bytes(fixed.encode("utf-8"))
        self.app.static_folder = str(root)

        response = self.client.get("/sw.js")
        try:
            self.assertEqual(response.get_data(), fixed.encode("utf-8"))
            self.assertEqual(response.headers["Service-Worker-Allowed"], "/")
            self.assertEqual(response.headers["Cache-Control"], "no-cache")
        finally:
            response.close()

    def test_offline_shell_targets_the_main_class(self):
        source = (ROOT / "cuoti_app" / "static" / "js" / "pwa.js").read_text(encoding="utf-8")
        self.assertIn(".main button,.main input,.main select,.main textarea", source)
        self.assertNotIn("#main button", source)

    def test_build_injects_service_worker_build_id_into_staged_assets(self):
        script = (ROOT / "tools" / "build_launcher.ps1").read_text(encoding="utf-8-sig")
        spec = (ROOT / "packaging" / "cuoti_launcher.spec").read_text(encoding="utf-8")
        self.assertIn('Replace("__CUOTI_BUILD_ID__", $buildId)', script)
        self.assertIn("CUOTI_BUILD_STATIC", script)
        self.assertIn("CUOTI_BUILD_STATIC", spec)
        self.assertIn("CUOTI_VERSION_FILE", script)
        self.assertIn("CUOTI_VERSION_FILE", spec)
        self.assertIn("generate_windows_version.py", script)
        self.assertIn("npm run lint:js", script)

    def test_page_explains_shell_only_offline_mode_and_user_driven_update(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('rel="manifest"', html)
        self.assertIn("当前仅显示应用壳", html)
        self.assertIn("不会离线保存任何写入", html)
        self.assertIn("pwa-update-refresh", html)
        self.assertIn("js/pwa.js", html)


if __name__ == "__main__":
    unittest.main()
