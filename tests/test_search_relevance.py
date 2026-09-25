from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import get_db


class SearchRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "search.db"
        self.app = create_app({"TESTING": True, "DATABASE": str(database)})
        self.client = self.app.test_client()
        with self.app.app_context():
            db = get_db()
            subject = db.execute("SELECT id FROM subjects ORDER BY id LIMIT 1").fetchone()[0]
            rows = [
                ("极限定理", "无", "无"),
                ("极限定理专题", "无", "无"),
                ("N3", "这是极限定理备注", "无"),
                ("N4", "无", "正文讨论极限定理与证明"),
                (r"\alpha-1", "LaTeX", r"求 \alpha 的值"),
            ]
            for qnum, note, content in rows:
                db.execute(
                    "INSERT INTO mistakes(public_id,subject_id,qnum,note,content,date_added,status,updated_at) "
                    "VALUES(?,?,?,?,?,'2026-08-06','未掌握','2026-08-06T10:00:00+08:00')",
                    (str(uuid.uuid4()), subject, qnum, note, content),
                )
            db.commit()

    def tearDown(self):
        self.temporary.cleanup()

    def test_explicit_relevance_order_and_cursor_are_stable(self):
        first = self.client.get("/api/v2/mistakes", query_string={"search": "极限定理", "limit": 2})
        self.assertEqual(first.status_code, 200, first.get_json())
        body = first.get_json()
        self.assertIn(body["search_backend"], {"fts5_trigram_explicit_score", "like_explicit_score"})
        self.assertEqual([item["qnum"] for item in body["items"]], ["极限定理", "极限定理专题"])
        second = self.client.get("/api/v2/mistakes", query_string={
            "search": "极限定理", "limit": 2, "after": body["next_cursor"],
        }).get_json()
        self.assertEqual([item["qnum"] for item in second["items"]], ["N3", "N4"])
        self.assertFalse(second["has_more"])

    def test_short_and_latex_queries_use_safe_like_fallback(self):
        short = self.client.get("/api/v2/mistakes", query_string={"search": "极"}).get_json()
        self.assertEqual(short["search_backend"], "like_explicit_score")
        latex = self.client.get("/api/v2/mistakes", query_string={"search": r"\alpha"})
        self.assertEqual(latex.status_code, 200, latex.get_json())
        self.assertEqual(latex.get_json()["items"][0]["qnum"], r"\alpha-1")


if __name__ == "__main__":
    unittest.main()
