import tempfile
import unittest
from pathlib import Path

from tools.verify_acceptance_state import Target, capture, differences


class AcceptanceStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "app.sqlite3"
        self.database.write_bytes(b"database")
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        (self.artifacts / "exam.pdf").write_bytes(b"%PDF-test")
        self.targets = [
            Target("database", "file", self.database),
            Target("artifacts", "directory", self.artifacts),
        ]

    def tearDown(self):
        self.temporary.cleanup()

    def test_unchanged_state_has_no_differences(self):
        expected = capture(self.targets)
        actual = capture(self.targets)
        self.assertEqual(differences(expected, actual), [])
        self.assertNotIn(str(self.root), str(expected))
        self.assertNotIn("exam.pdf", str(expected))

    def test_changed_file_is_reported_by_label_only(self):
        expected = capture(self.targets)
        self.database.write_bytes(b"changed")
        actual = capture(self.targets)
        self.assertEqual(
            differences(expected, actual),
            ["changed target: database"],
        )

    def test_changed_directory_is_reported(self):
        expected = capture(self.targets)
        (self.artifacts / "exam.tex").write_text("test", encoding="utf-8")
        actual = capture(self.targets)
        self.assertEqual(
            differences(expected, actual),
            ["changed target: artifacts"],
        )


if __name__ == "__main__":
    unittest.main()
