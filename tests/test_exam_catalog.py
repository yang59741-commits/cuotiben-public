from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.routes.exams_legacy import _store_exam_items


class ExamCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app({
            "TESTING": True,
            "DATABASE": str(root / "data.db"),
            "PDF_DIR": str(root / "pdfs"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def _insert_exam(self, *, title, status="published", source="free", date="2026-08-06"):
        with self.app.app_context():
            db = get_db()
            subject_id = db.execute(
                "SELECT id FROM subjects WHERE name='数学分析'"
            ).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) "
                "VALUES(?,?,?,?,?,?)",
                (
                    subject_id,
                    title,
                    json.dumps({
                        "source": source,
                        "chapters": ["引论"],
                        "question_count": 1,
                        "difficulty": "中等",
                        "candidate_source": "ordinary",
                        "reference_year": 2025,
                    }, ensure_ascii=False),
                    r"\begin{problem}不可变题目快照\end{problem}",
                    date + " 10:00",
                    status,
                ),
            )
            exam_id = cursor.lastrowid
            _store_exam_items(
                db,
                exam_id,
                r"\begin{problem}不可变题目快照\end{problem}",
                source=source,
                structured_items=[{
                    "position": 1,
                    "type": "证明题",
                    "difficulty": "中等",
                    "reference_ids": ["candidate-1"],
                }],
            )
            db.commit()
            return exam_id, subject_id

    def test_catalog_filters_published_and_returns_stable_summary(self):
        published_id, subject_id = self._insert_exam(title="目标试卷")
        self._insert_exam(title="未发布目标试卷", status="publishing")
        self._insert_exam(title="其他来源", source="past", date="2026-07-01")

        response = self.client.get(
            "/api/v2/exams?title=目标&source=free&date_from=2026-08-01"
            f"&subject_id={subject_id}"
        )
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = response.get_json()
        self.assertEqual([item["id"] for item in payload["items"]], [published_id])
        self.assertEqual(payload["items"][0]["item_count"], 1)
        self.assertEqual(payload["items"][0]["summary"], {
            "candidate_source": "ordinary",
            "chapters": ["引论"],
            "difficulty": "中等",
            "generation_mode": "free",
            "question_count": 1,
            "reference_year": 2025,
            "training_target": "",
            "training_target_counts": {},
            "training_target_findings": [],
        })

    def test_exam_item_snapshot_is_answer_free_and_independent(self):
        exam_id, _subject_id = self._insert_exam(title="快照试卷")
        detail = self.client.get(f"/api/v2/exams/{exam_id}")
        self.assertEqual(detail.status_code, 200, detail.get_json())
        item = detail.get_json()["items"][0]
        self.assertEqual(item["content"], "不可变题目快照")
        self.assertEqual(
            item["content_hash"],
            hashlib.sha256("不可变题目快照".encode()).hexdigest(),
        )
        self.assertEqual(item["source_candidate_id"], "candidate-1")
        self.assertNotIn("answer", item)
        self.assertNotIn("solution", item)
        # HTTP detail keeps its historical projection; the job detail service
        # additionally exposes lineage, timing and scoring evidence.
        self.assertEqual(set(item), {
            "public_id", "position", "question_type", "difficulty", "content",
            "content_hash", "points", "source_kind", "source_mistake_public_id",
            "source_candidate_id", "source_metadata",
        })
        self.assertEqual(item["points"], 0)
        self.assertNotIn("mistake_lineage", detail.get_json())
        with self.app.app_context():
            columns = {
                row[1] for row in get_db().execute("PRAGMA table_info(exam_items)")
            }
            self.assertNotIn("answer", columns)
            self.assertNotIn("solution", columns)

    def test_unpublished_detail_and_pdf_are_not_exposed(self):
        exam_id, _subject_id = self._insert_exam(
            title="发布中", status="publishing"
        )
        self.assertEqual(self.client.get(f"/api/v2/exams/{exam_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/pdfs/exam_{exam_id}.pdf").status_code, 404)


if __name__ == "__main__":
    unittest.main()
