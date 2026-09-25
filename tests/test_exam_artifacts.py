from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services.exam_artifacts import (
    reconcile_exam_artifacts,
    reconcile_exam_deletions,
    recompile_exam,
)
from cuoti_app.services.exam_generation_compat import PipelineIdentity


class ExamArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdfs = self.root / "pdfs"
        self.pdfs.mkdir()
        self.database = self.root / "data.db"
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(self.database),
            "PDF_DIR": str(self.pdfs),
        })

    def tearDown(self):
        self.temporary.cleanup()

    def _exam(self, *, status="publishing", job_id=None, batch_size=1, index=1):
        source = "\\documentclass{article}\\begin{document}题目\\end{document}"
        config = {
            "batch_size": batch_size,
            "pipeline_identity": PipelineIdentity().as_dict(),
            "lineage_closed": True,
        }
        with self.app.app_context():
            db = get_db()
            cursor = db.execute(
                "INSERT INTO exams(title,config,content,created_at,generation_job_id,"
                "batch_index,artifact_status,latex_source,latex_sha256) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "恢复测试",
                    json.dumps(config),
                    "题目",
                    "2026-08-06 10:00",
                    job_id,
                    index,
                    status,
                    source,
                    hashlib.sha256(source.encode()).hexdigest(),
                ),
            )
            db.commit()
            return cursor.lastrowid, source

    def test_complete_publishing_artifact_is_recovered(self):
        exam_id, source = self._exam()
        (self.pdfs / f"exam_{exam_id}.tex").write_text(source, encoding="utf-8")
        (self.pdfs / f"exam_{exam_id}.pdf").write_bytes(b"%PDF-1.7\ncomplete")
        result = reconcile_exam_artifacts(str(self.database), str(self.pdfs))
        self.assertEqual(result["recovered"], 1)
        db = sqlite3.connect(self.database)
        try:
            row = db.execute(
                "SELECT artifact_status,recompile_available,pdf_sha256 FROM exams WHERE id=?",
                (exam_id,),
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(row[0:2], ("published", 1))
        self.assertEqual(row[2], hashlib.sha256(b"%PDF-1.7\ncomplete").hexdigest())

    def test_legacy_published_exam_items_are_backfilled_without_answers(self):
        exam_id, _source = self._exam(status="published")
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE exams SET content=? WHERE id=?",
                (r"\begin{problem}旧卷题目快照\end{problem}", exam_id),
            )
            db.commit()
        result = reconcile_exam_artifacts(str(self.database), str(self.pdfs))
        self.assertEqual(result["items_backfilled"], 1)
        with self.app.app_context():
            row = get_db().execute(
                "SELECT content,source_kind FROM exam_items WHERE exam_id=?", (exam_id,)
            ).fetchone()
            self.assertEqual(tuple(row), ("旧卷题目快照", "legacy_snapshot"))

    def test_incomplete_generation_batch_is_removed_as_a_unit(self):
        job_id = "a" * 32
        exam_id, source = self._exam(job_id=job_id, batch_size=2)
        (self.pdfs / f"exam_{exam_id}.tex").write_text(source, encoding="utf-8")
        (self.pdfs / f"exam_{exam_id}.pdf").write_bytes(b"%PDF-1.7\nfirst")
        result = reconcile_exam_artifacts(str(self.database), str(self.pdfs))
        self.assertEqual(result["removed"], 1)
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM exams").fetchone()[0], 0)
        finally:
            db.close()
        self.assertFalse((self.pdfs / f"exam_{exam_id}.pdf").exists())

    def test_recompile_failure_preserves_old_pdf_and_tex(self):
        exam_id, source = self._exam(status="published")
        old_pdf = b"%PDF-1.7\nold"
        old_tex = "old source"
        (self.pdfs / f"exam_{exam_id}.pdf").write_bytes(old_pdf)
        (self.pdfs / f"exam_{exam_id}.tex").write_text(old_tex, encoding="utf-8")
        with patch(
            "cuoti_app.services.exam_artifacts.compile_latex_artifact",
            side_effect=RuntimeError("compile failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "compile failed"):
                recompile_exam(str(self.pdfs), "xelatex", exam_id, source)
        self.assertEqual((self.pdfs / f"exam_{exam_id}.pdf").read_bytes(), old_pdf)
        self.assertEqual(
            (self.pdfs / f"exam_{exam_id}.tex").read_text(encoding="utf-8"), old_tex
        )

    def test_recompile_success_atomically_replaces_pair(self):
        exam_id, source = self._exam(status="published")
        (self.pdfs / f"exam_{exam_id}.pdf").write_bytes(b"old pdf")
        (self.pdfs / f"exam_{exam_id}.tex").write_text("old tex", encoding="utf-8")

        def fake_compile(pdf_dir, _command, stem, latex, **_kwargs):
            Path(pdf_dir, stem + ".pdf").write_bytes(b"%PDF-1.7\nnew")
            Path(pdf_dir, stem + ".tex").write_text(latex, encoding="utf-8")

        with patch(
            "cuoti_app.services.exam_artifacts.compile_latex_artifact",
            side_effect=fake_compile,
        ):
            hashes = recompile_exam(str(self.pdfs), "xelatex", exam_id, source)
        self.assertEqual((self.pdfs / f"exam_{exam_id}.pdf").read_bytes(), b"%PDF-1.7\nnew")
        self.assertEqual(
            (self.pdfs / f"exam_{exam_id}.tex").read_text(encoding="utf-8"), source
        )
        self.assertEqual(hashes["pdf_sha256"], hashlib.sha256(b"%PDF-1.7\nnew").hexdigest())

    def test_staged_delete_is_restored_when_database_row_survives(self):
        exam_id, source = self._exam(status="published")
        staging = self.pdfs / f".delete-exam-{exam_id}-{'a' * 32}"
        staging.mkdir()
        (staging / f"exam_{exam_id}.tex").write_text(source, encoding="utf-8")
        (staging / f"exam_{exam_id}.pdf").write_bytes(b"%PDF-1.7\nstaged")
        result = reconcile_exam_deletions(str(self.database), str(self.pdfs))
        self.assertEqual(result["restored"], 2)
        self.assertTrue((self.pdfs / f"exam_{exam_id}.tex").is_file())
        self.assertFalse(staging.exists())

    def test_staged_legacy_delete_is_restored_for_soft_deleted_row(self):
        exam_id, source = self._exam(status="published")
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE exams SET deleted_at='2026-09-08T00:00:00+00:00' WHERE id=?",
                (exam_id,),
            )
            db.commit()
        staging = self.pdfs / f".delete-exam-{exam_id}-{'c' * 32}"
        staging.mkdir()
        staged_tex = staging / f"exam_{exam_id}.tex"
        staged_pdf = staging / f"exam_{exam_id}.pdf"
        staged_tex.write_text(source, encoding="utf-8")
        staged_pdf.write_bytes(b"%PDF-1.7\nsoft-deleted")

        result = reconcile_exam_deletions(str(self.database), str(self.pdfs))

        self.assertEqual(result["restored"], 2)
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.pdfs / f"exam_{exam_id}.tex").is_file())
        self.assertTrue((self.pdfs / f"exam_{exam_id}.pdf").is_file())
        self.assertFalse(staging.exists())
        db = sqlite3.connect(self.database)
        try:
            self.assertEqual(
                db.execute("SELECT deleted_at FROM exams WHERE id=?", (exam_id,)).fetchone()[0],
                "2026-09-08T00:00:00+00:00",
            )
        finally:
            db.close()

    def test_staged_delete_is_removed_when_database_row_is_gone(self):
        staging = self.pdfs / f".delete-exam-999-{'b' * 32}"
        staging.mkdir()
        (staging / "exam_999.pdf").write_bytes(b"orphan")
        result = reconcile_exam_deletions(str(self.database), str(self.pdfs))
        self.assertEqual(result["removed"], 1)
        self.assertFalse(staging.exists())

    def test_hash_selects_valid_staged_copy_and_preserves_conflict(self):
        exam_id, _source = self._exam(status="published")
        valid_pdf = b"%PDF-1.7\nvalid"
        with self.app.app_context():
            db = get_db()
            db.execute(
                "UPDATE exams SET pdf_sha256=? WHERE id=?",
                (hashlib.sha256(valid_pdf).hexdigest(), exam_id),
            )
            db.commit()
        target = self.pdfs / f"exam_{exam_id}.pdf"
        target.write_bytes(b"unexpected-current")
        staging = self.pdfs / f".delete-exam-{exam_id}-{'c' * 32}"
        staging.mkdir()
        (staging / target.name).write_bytes(valid_pdf)
        result = reconcile_exam_deletions(str(self.database), str(self.pdfs))
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(target.read_bytes(), valid_pdf)
        self.assertEqual(
            (staging / f"conflict-current-{target.name}").read_bytes(),
            b"unexpected-current",
        )


if __name__ == "__main__":
    unittest.main()
