from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cuoti_app.services.process_temp import configure_process_temp


class ProcessTempTests(unittest.TestCase):
    def test_configures_environment_cache_and_writable_directory(self):
        with (
            mock.patch.object(tempfile, "tempdir", None),
            tempfile.TemporaryDirectory() as directory,
        ):
            runtime_root = Path(directory) / "runtime"
            with (
                mock.patch.dict(
                    os.environ,
                    {"TEMP": "invalid-temp", "TMP": "invalid-tmp"},
                ),
                mock.patch.object(tempfile, "tempdir", "invalid-cache"),
            ):
                result = configure_process_temp(runtime_root)
                expected = (runtime_root / "temp").resolve()

                self.assertEqual(result, expected)
                self.assertEqual(os.environ["TEMP"], str(expected))
                self.assertEqual(os.environ["TMP"], str(expected))
                self.assertEqual(tempfile.tempdir, str(expected))
                (expected / "probe.tmp").write_text("ok", encoding="utf-8")
                self.assertEqual((expected / "probe.tmp").read_text(encoding="utf-8"), "ok")

    def test_child_process_inherits_selected_temp_directory(self):
        with (
            mock.patch.object(tempfile, "tempdir", None),
            tempfile.TemporaryDirectory() as directory,
        ):
            runtime_root = Path(directory) / "runtime"
            with mock.patch.dict(
                os.environ,
                {"TEMP": "invalid-temp", "TMP": "invalid-tmp"},
            ):
                expected = configure_process_temp(runtime_root)
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "import tempfile; print(tempfile.gettempdir())",
                    ],
                    env=os.environ.copy(),
                    capture_output=True,
                    check=True,
                    text=True,
                )

                self.assertEqual(completed.stdout.strip(), str(expected))


if __name__ == "__main__":
    unittest.main()
