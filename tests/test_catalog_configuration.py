from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cuoti_app.services.catalog import CatalogError, CatalogService, load_default_catalog
from cuoti_app.services.question_bank import (
    ensure_question_bank_index,
    question_bank_status,
)
from cuoti_app.services.tex import append_problems


class CatalogConfigurationTests(unittest.TestCase):
    def test_default_catalog_preserves_legacy_shape_and_reasons(self):
        catalog = load_default_catalog()
        self.assertEqual(len(catalog.subjects), 2)
        self.assertEqual(sum(len(item.chapters) for item in catalog.subjects), 35)
        self.assertEqual(len(catalog.chapter_sources), 35)
        rules = {
            (rule["source_file"], number): rule["reason"]
            for rule in catalog.exclusions
            if rule["kind"] == "question_numbers"
            for number in rule["numbers"]
        }
        self.assertEqual(
            rules[("chapterB7_09_内积空间.tex", 89)],
            "excluded_symplectic_matrix",
        )
        self.assertEqual(
            rules[("chapterB7_09_内积空间.tex", 93)],
            "excluded_generalized_inverse",
        )

    def test_bootstrap_is_atomic_and_invalid_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            catalog = CatalogService(path).ensure()
            self.assertTrue(path.is_file())
            self.assertTrue(catalog.semantic_digest)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["subjects"][0]["chapters"][0]["sources"] = ["../escape.tex"]
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(CatalogError):
                catalog.reload()

    def test_sync_is_add_only_and_assigns_stable_codes(self):
        catalog = load_default_catalog()
        connection = sqlite3.connect(":memory:")
        connection.executescript(
            "CREATE TABLE subjects(id INTEGER PRIMARY KEY,name TEXT UNIQUE,"
            "subject_code TEXT,display_order INTEGER NOT NULL DEFAULT 0);"
            "CREATE TABLE chapters(id INTEGER PRIMARY KEY,subject_id INTEGER,name TEXT,"
            "chapter_code TEXT,display_order INTEGER NOT NULL DEFAULT 0,parent_code TEXT,"
            "UNIQUE(subject_id,name));"
        )
        connection.execute("INSERT INTO subjects(id,name) VALUES(40,'数学分析')")
        result = catalog.sync_database(connection)
        self.assertEqual(
            connection.execute("SELECT id FROM subjects WHERE name='数学分析'").fetchone()[0],
            40,
        )
        self.assertEqual(
            connection.execute("SELECT subject_code FROM subjects WHERE id=40").fetchone()[0],
            "math_analysis",
        )
        self.assertGreater(result["added_chapters"], 0)
        catalog.sync_database(connection)
        self.assertEqual(connection.execute("SELECT count(*) FROM subjects").fetchone()[0], 2)


class QuestionBankConfigurationTests(unittest.TestCase):
    def test_invalid_encoding_is_visible_without_replacement_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "bad.tex"
            source.write_bytes("中文".encode("gbk"))
            config = root / "catalog.json"
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "subjects": [{
                            "subject_code": "subject_a", "name": "科目", "display_order": 1,
                            "fallback_source": "fallback.tex",
                            "chapters": [{
                                "chapter_code": "subject_a.chapter_1", "name": "章节",
                                "display_order": 1, "parent_code": None, "sources": ["bad.tex"],
                            }],
                        }],
                        "exclusions": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            catalog = CatalogService(config, default_path=config).reload()
            index_path = root / "index.sqlite3"
            ensure_question_bank_index(root, index_path, catalog=catalog)
            connection = sqlite3.connect(index_path)
            row = connection.execute("SELECT status,error FROM sources").fetchone()
            connection.close()
            self.assertEqual(row[0], "encoding_error")
            self.assertIn("UTF-8", row[1])

    def test_last_good_status_stays_available_when_source_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "one.tex"
            source.write_text(
                "\\begin{document}\n\\subsection{题目1}\n" + "有效题目内容" * 8 + "\n\\end{document}",
                encoding="utf-8",
            )
            mapping = {("科目", "章节"): ["one.tex"]}
            from types import SimpleNamespace
            index_path = root / "index.sqlite3"
            catalog = SimpleNamespace(chapter_sources=mapping, exclusions=())
            first = ensure_question_bank_index(root, index_path, catalog=catalog)
            source.write_text(source.read_text(encoding="utf-8") + "% changed", encoding="utf-8")
            status = question_bank_status(root, index_path, catalog=catalog)
            self.assertTrue(status["available"])
            self.assertTrue(status["stale"])
            self.assertEqual(status["last_good_snapshot"], first.snapshot_id)


class SafeLibraryWriteTests(unittest.TestCase):
    def test_structure_encoding_duplicate_and_optional_compile_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "bank.tex"
            target.write_text("\\begin{document}\n\\end{document}", encoding="utf-8")
            checked = []
            append_problems(
                root,
                [("bank.tex", "足够长的唯一题目内容" * 4)],
                compile_check=lambda path: checked.append(path.name),
            )
            self.assertEqual(len(checked), 1)
            with self.assertRaisesRegex(ValueError, "重复"):
                append_problems(root, [("bank.tex", "足够长的唯一题目内容" * 4)])
            broken = root / "broken.tex"
            broken.write_text("\\begin{document}\n缺少结尾", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "文档结构"):
                append_problems(root, [("broken.tex", "另一道题" * 10)])


if __name__ == "__main__":
    unittest.main()
