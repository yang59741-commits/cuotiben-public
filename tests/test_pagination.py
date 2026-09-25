from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from cuoti_app import create_app
from cuoti_app.database import init_db


class PaginationApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "pagination.db"
        self.app = create_app(
            {
                "TESTING": True,
                "DATABASE": str(self.db_path),
                "PDF_DIR": str(self.root / "pdfs"),
                "TEX_BASE": str(self.root / "synthetic_tex"),
                "QUESTION_BANK_DATABASE": str(
                    self.root / "question_bank.sqlite3"
                ),
                "PAST_EXAM_DIR": str(self.root / "synthetic_past"),
                "DEEPSEEK_API_KEY": "test-only",
            }
        )
        with self.app.app_context():
            init_db()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tempdir.cleanup()

    def insert_mistakes(self, count: int, *, content_prefix: str = "分页题目"):
        db = sqlite3.connect(self.db_path)
        try:
            db.executemany(
                "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,note,"
                "date_added,status,next_review) VALUES(1,1,?,?,?,?,?,'未掌握',?)",
                [
                    (
                        str(index),
                        f"{content_prefix}{index}",
                        "计算错误",
                        "",
                        "2026-07-27",
                        "2026-07-27",
                    )
                    for index in range(count)
                ],
            )
            db.commit()
        finally:
            db.close()

    def test_mistake_cursor_reads_snapshot_without_duplicates(self):
        self.insert_mistakes(205)
        first = self.client.get("/api/v2/mistakes?limit=30&status=未掌握")
        self.assertEqual(first.status_code, 200)
        payload = first.get_json()
        self.assertEqual(len(payload["items"]), 30)
        self.assertEqual(payload["total"], 205)
        snapshot_ids = [item["id"] for item in payload["items"]]

        self.insert_mistakes(1, content_prefix="翻页后新增")
        cursor = payload["next_cursor"]
        while cursor:
            page = self.client.get(
                "/api/v2/mistakes",
                query_string={"limit": 30, "status": "未掌握", "after": cursor},
            ).get_json()
            snapshot_ids.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(len(snapshot_ids), 205)
        self.assertEqual(len(snapshot_ids), len(set(snapshot_ids)))
        self.assertNotIn(max(snapshot_ids) + 1, snapshot_ids)

    def test_three_pages_return_older_dates_content_and_no_duplicates(self):
        db = sqlite3.connect(self.db_path)
        try:
            start = date(2026, 1, 1)
            db.executemany(
                "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,reasons,note,"
                "date_added,status,next_review) VALUES(1,1,?,?,?,?,?,'未掌握',?)",
                [
                    (
                        f"H-{index:02d}",
                        f"历史分页正文 {index:02d}",
                        "计算错误",
                        "",
                        (start + timedelta(days=index)).isoformat(),
                        "2026-09-20",
                    )
                    for index in range(75)
                ],
            )
            db.commit()
        finally:
            db.close()

        pages = []
        cursor = None
        while True:
            query = {"limit": 30}
            if cursor:
                query["after"] = cursor
            response = self.client.get("/api/v2/mistakes", query_string=query)
            self.assertEqual(response.status_code, 200)
            page = response.get_json()
            pages.append(page)
            cursor = page["next_cursor"]
            if not cursor:
                break

        self.assertEqual([len(page["items"]) for page in pages], [30, 30, 15])
        items = [item for page in pages for item in page["items"]]
        self.assertEqual(len(items), 75)
        self.assertEqual(len({item["public_id"] for item in items}), 75)
        self.assertEqual(items[0]["content"], "历史分页正文 74")
        self.assertEqual(pages[1]["items"][0]["content"], "历史分页正文 44")
        self.assertEqual(pages[2]["items"][0]["content"], "历史分页正文 14")
        self.assertLess(
            max(item["date_added"] for item in pages[1]["items"]),
            min(item["date_added"] for item in pages[0]["items"]),
        )
        self.assertLess(
            max(item["date_added"] for item in pages[2]["items"]),
            min(item["date_added"] for item in pages[1]["items"]),
        )

    def test_cursor_is_bound_to_filters(self):
        self.insert_mistakes(40)
        cursor = self.client.get(
            "/api/v2/mistakes?limit=10&status=未掌握"
        ).get_json()["next_cursor"]
        response = self.client.get(
            "/api/v2/mistakes",
            query_string={"limit": 10, "status": "已掌握", "after": cursor},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "INVALID_CURSOR")

    def test_search_preserves_short_chinese_and_literal_wildcards(self):
        self.insert_mistakes(1, content_prefix="矩阵中的 100%_准确")
        short = self.client.get("/api/v2/mistakes", query_string={"search": "矩阵"})
        literal = self.client.get("/api/v2/mistakes", query_string={"search": "%_"})
        self.assertEqual(short.get_json()["total"], 1)
        self.assertEqual(literal.get_json()["total"], 1)

    def test_trigram_acceleration_tracks_updates_without_changing_results(self):
        self.insert_mistakes(1, content_prefix="数列极限技巧")
        initial = self.client.get(
            "/api/v2/mistakes", query_string={"search": "数列极限"}
        )
        self.assertEqual(initial.get_json()["total"], 1)
        mistake_id = initial.get_json()["items"][0]["id"]
        db = sqlite3.connect(self.db_path)
        try:
            db.execute(
                "UPDATE mistakes SET content='矩阵特征值方法' WHERE id=?",
                (mistake_id,),
            )
            db.commit()
        finally:
            db.close()
        old = self.client.get(
            "/api/v2/mistakes", query_string={"search": "数列极限"}
        )
        new = self.client.get(
            "/api/v2/mistakes", query_string={"search": "矩阵特征"}
        )
        self.assertEqual(old.get_json()["total"], 0)
        self.assertEqual(new.get_json()["total"], 1)

    def test_due_reviews_and_exams_are_paginated(self):
        self.insert_mistakes(55)
        due = self.client.get("/api/v2/reviews/due?limit=20").get_json()
        self.assertEqual(len(due["items"]), 20)
        self.assertEqual(due["total"], 55)
        self.assertTrue(due["has_more"])

        db = sqlite3.connect(self.db_path)
        try:
            db.executemany(
                "INSERT INTO exams(subject_id,title,config,content,created_at) "
                "VALUES(1,?,'{}','',?)",
                [(f"临时试卷 {index}", "2026-07-27") for index in range(55)],
            )
            db.commit()
        finally:
            db.close()
        exams = self.client.get("/api/v2/exams?limit=20").get_json()
        candidates = self.client.get("/api/v2/exam-candidates?limit=20").get_json()
        self.assertEqual(exams["total"], 55)
        self.assertEqual(len(exams["items"]), 20)
        self.assertEqual(candidates["total"], 55)
        self.assertEqual(len(candidates["items"]), 20)

    def test_due_reviews_filter_subject_chapter_and_bind_cursor(self):
        db = sqlite3.connect(self.db_path)
        try:
            subject_id = db.execute(
                "SELECT id FROM subjects ORDER BY id LIMIT 1"
            ).fetchone()[0]
            chapters = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 2",
                    (subject_id,),
                )
            ]
            self.assertEqual(len(chapters), 2)
            other_subject_id = db.execute(
                "SELECT id FROM subjects WHERE id<>? ORDER BY id LIMIT 1",
                (subject_id,),
            ).fetchone()[0]
            other_chapter_id = db.execute(
                "SELECT id FROM chapters WHERE subject_id=? ORDER BY id LIMIT 1",
                (other_subject_id,),
            ).fetchone()[0]
            rows = []
            for index in range(65):
                rows.append(
                    (
                        subject_id,
                        chapters[0],
                        f"S-{index}",
                        f"间隔复习分页正文 {index}",
                    )
                )
            for index in range(7):
                rows.append(
                    (subject_id, chapters[1], f"X-{index}", f"其他章节 {index}")
                )
            for index in range(6):
                rows.append(
                    (
                        other_subject_id,
                        other_chapter_id,
                        f"O-{index}",
                        f"其他科目 {index}",
                    )
                )
            db.executemany(
                "INSERT INTO mistakes(subject_id,chapter_id,qnum,content,date_added,"
                "status,next_review) VALUES(?,?,?,?,'2026-01-01','未掌握','2026-01-02')",
                rows,
            )
            db.commit()
        finally:
            db.close()

        query = {
            "limit": 30,
            "subject_id": subject_id,
            "chapter_id": chapters[0],
        }
        items = []
        cursor = None
        while True:
            if cursor:
                query["after"] = cursor
            response = self.client.get("/api/v2/reviews/due", query_string=query)
            self.assertEqual(response.status_code, 200, response.get_json())
            page = response.get_json()
            self.assertEqual(page["total"], 65)
            items.extend(page["items"])
            cursor = page["next_cursor"]
            if not cursor:
                break

        self.assertEqual(len(items), 65)
        self.assertEqual(len({item["public_id"] for item in items}), 65)
        self.assertTrue(all("间隔复习分页正文" in item["content"] for item in items))

        subject_only = self.client.get(
            "/api/v2/reviews/due",
            query_string={"limit": 100, "subject_id": subject_id},
        ).get_json()
        self.assertEqual(subject_only["total"], 72)
        self.assertTrue(
            all(item["subject_id"] == subject_id for item in subject_only["items"])
        )

        first = self.client.get(
            "/api/v2/reviews/due",
            query_string={"limit": 10, "subject_id": subject_id},
        ).get_json()
        changed = self.client.get(
            "/api/v2/reviews/due",
            query_string={
                "limit": 10,
                "subject_id": subject_id,
                "chapter_id": chapters[0],
                "after": first["next_cursor"],
            },
        )
        self.assertEqual(changed.status_code, 400)
        self.assertEqual(changed.get_json()["code"], "INVALID_CURSOR")
        invalid = self.client.get(
            "/api/v2/reviews/due", query_string={"subject_id": "invalid"}
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.get_json()["code"], "VALIDATION")


if __name__ == "__main__":
    unittest.main()
