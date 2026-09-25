from __future__ import annotations

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cuoti_app.services.tex import append_problems


class AtomicTexWriteTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.first = self.root / "one.tex"
        self.second = self.root / "two.tex"
        self.first.write_text(
            "\\begin{document}\n\\subsection{题目1}\nA\n\\end{document}",
            encoding="utf-8",
        )
        self.second.write_text("\\begin{document}\n\\end{document}", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def test_batch_numbers_and_publishes_all_files(self):
        numbers = append_problems(
            str(self.root),
            [("one.tex", "B"), ("one.tex", "C"), ("two.tex", "D")],
        )
        self.assertEqual(numbers, [2, 3, 1])
        self.assertIn("\\subsection{题目3}\nC", self.first.read_text(encoding="utf-8"))
        self.assertIn("\\subsection{题目1}\nD", self.second.read_text(encoding="utf-8"))

    def test_publish_failure_restores_already_replaced_file(self):
        original_first = self.first.read_text(encoding="utf-8")
        original_second = self.second.read_text(encoding="utf-8")
        real_replace = os.replace
        publish_count = 0

        def fail_second_publish(source, destination):
            nonlocal publish_count
            if str(source).endswith(".tmp"):
                publish_count += 1
                if publish_count == 2:
                    raise OSError("simulated publish failure")
            return real_replace(source, destination)

        with patch("cuoti_app.services.tex.os.replace", side_effect=fail_second_publish):
            with self.assertRaises(OSError):
                append_problems(
                    str(self.root), [("one.tex", "B"), ("two.tex", "C")]
                )
        self.assertEqual(self.first.read_text(encoding="utf-8"), original_first)
        self.assertEqual(self.second.read_text(encoding="utf-8"), original_second)

    def test_concurrent_batches_allocate_unique_numbers(self):
        def add(index):
            return append_problems(str(self.root), [("one.tex", f"P{index}")])[0]

        with ThreadPoolExecutor(max_workers=4) as executor:
            numbers = list(executor.map(add, range(8)))
        self.assertEqual(sorted(numbers), list(range(2, 10)))


if __name__ == "__main__":
    unittest.main()
