import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cuoti_app import create_app
from cuoti_app.services.catalog import load_default_catalog
from cuoti_app.database import get_db, init_db
from cuoti_app.services.question_bank import ensure_question_bank_index


LONG_A = "证明这个数学命题成立，并写出完整严谨的推导过程。" * 3
LONG_B = "计算下列数学表达式的结果，同时说明所使用的主要定理。" * 3
LONG_C = "判断给定线性映射的性质，并给出必要且充分的证明。" * 3
DEFAULT_CATALOG = load_default_catalog()
CHAPTER_TO_TEX = DEFAULT_CATALOG.chapter_sources


def catalog_for(mapping):
    return SimpleNamespace(
        chapter_sources=mapping,
        exclusions=DEFAULT_CATALOG.exclusions,
    )


class QuestionBankIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.tex = self.root / "tex"
        self.tex.mkdir()
        self.index_path = self.root / "question_bank.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, content):
        (self.tex / name).write_text(content, encoding="utf-8")

    def test_mislabeled_integral_sources_follow_question_semantics(self):
        self.assertEqual(
            CHAPTER_TO_TEX[("数学分析", "不定积分")],
            ["chapterB3_09_定积分.tex"],
        )
        self.assertEqual(
            CHAPTER_TO_TEX[("数学分析", "定积分")],
            ["chapterB3_10_定积分.tex"],
        )
        self.assertIn(
            "chapterB3_07_不定积分.tex",
            CHAPTER_TO_TEX[("数学分析", "微分学应用")],
        )

    def test_indexes_only_whitelist_and_supports_both_parsers(self):
        mapping = {
            ("数学分析", "数列极限"): ["standard.tex"],
            ("高等代数", "矩阵"): ["numbered.tex"],
        }
        self.write(
            "standard.tex",
            "\\subsection{题目1}\n"
            + LONG_A
            + "\n\\subsection*{题目2}\n"
            + LONG_B,
        )
        self.write("numbered.tex", "1．" + LONG_B + "\n2. " + LONG_C)
        self.write(
            "unmapped.tex", "\\subsection{题目1}\n不应进入索引的完整题目内容" * 3
        )
        source_hashes = {
            path.name: path.read_bytes()
            for path in self.tex.iterdir()
            if path.is_file()
        }

        index = ensure_question_bank_index(
            self.tex, index_path=self.index_path, catalog=catalog_for(mapping)
        )

        coverage = index.coverage()
        self.assertEqual(coverage["status"], "ready")
        self.assertEqual(coverage["total_questions"], 3)
        self.assertEqual(coverage["duplicate_count"], 1)
        self.assertEqual(coverage["source_count"], 2)
        candidates = index.select_candidates(
            "数学分析", ["数列极限"], 10, seed=7
        )
        self.assertEqual(len(candidates), 2)
        self.assertTrue(
            {
                "candidate_id",
                "chapter",
                "source_file",
                "source_number",
                "content",
            }.issubset(candidates[0])
        )
        self.assertNotIn("unmapped.tex", {c["source_file"] for c in candidates})
        self.assertEqual(
            source_hashes,
            {
                path.name: path.read_bytes()
                for path in self.tex.iterdir()
                if path.is_file()
            },
        )

    def test_stable_ids_seed_quotas_and_source_change_rebuild(self):
        mapping = {
            ("数学分析", "数列极限"): ["a.tex"],
            ("数学分析", "函数极限"): ["b.tex"],
        }
        self.write(
            "a.tex",
            "\\subsection{题目1}\n"
            + LONG_A
            + "\n\\subsection{题目2}\n"
            + LONG_B,
        )
        self.write("b.tex", "\\subsection{题目1}\n" + LONG_C)
        catalog = catalog_for(mapping)
        if catalog:
            first = ensure_question_bank_index(
                self.tex, index_path=self.index_path, catalog=catalog
            )
            ids_before = [
                item["candidate_id"]
                for item in first.select_candidates(
                    "数学分析",
                    ["数列极限", "函数极限"],
                    3,
                    seed=11,
                    chapter_quotas={"函数极限": 1, "数列极限": 2},
                )
            ]
            first_snapshot = first.snapshot_id
            second = ensure_question_bank_index(
                self.tex, index_path=self.index_path, catalog=catalog
            )
            ids_after = [
                item["candidate_id"]
                for item in second.select_candidates(
                    "数学分析",
                    ["数列极限", "函数极限"],
                    3,
                    seed=11,
                    chapter_quotas={"函数极限": 1, "数列极限": 2},
                )
            ]
            self.assertEqual(ids_before, ids_after)
            self.assertEqual(first_snapshot, second.snapshot_id)

            self.write(
                "b.tex",
                "\\subsection{题目1}\n"
                + LONG_C
                + "\n\\subsection{题目2}\n"
                + LONG_A
                + "附加条件",
            )
            rebuilt = ensure_question_bank_index(
                self.tex, index_path=self.index_path, catalog=catalog
            )
            self.assertNotEqual(first_snapshot, rebuilt.snapshot_id)
            self.assertEqual(rebuilt.coverage()["total_questions"], 4)

    def test_frozen_candidates_restore_order_and_usage_survives_rebuild(self):
        mapping = {("数学分析", "数列极限"): ["usage.tex"]}
        self.write(
            "usage.tex",
            "\\subsection{题目1}\n"
            + LONG_A
            + "\n\\subsection{题目2}\n"
            + LONG_B,
        )
        catalog = catalog_for(mapping)
        if catalog:
            index = ensure_question_bank_index(
                self.tex, index_path=self.index_path, catalog=catalog
            )
            candidates = index.select_candidates(
                "数学分析", ["数列极限"], 2, seed="manifest"
            )
            frozen_ids = [
                candidates[1]["candidate_id"],
                candidates[0]["candidate_id"],
            ]
            index.record_usage(frozen_ids, [frozen_ids[0]])
            restored = index.candidates_by_ids(
                frozen_ids,
                subject="数学分析",
                chapters=["数列极限"],
            )
            self.assertEqual(
                [item["candidate_id"] for item in restored],
                frozen_ids,
            )
            self.assertEqual(restored[0]["selected_count"], 1)

            self.write(
                "usage.tex",
                (self.tex / "usage.tex").read_text(encoding="utf-8")
                + "\n\\subsection{题目3}\n"
                + LONG_C,
            )
            rebuilt = ensure_question_bank_index(
                self.tex, index_path=self.index_path, catalog=catalog
            )
            restored_after_rebuild = rebuilt.candidates_by_ids(
                frozen_ids,
                subject="数学分析",
                chapters=["数列极限"],
            )
            self.assertEqual(restored_after_rebuild[0]["selected_count"], 1)

    def test_explicit_exclusions_are_counted(self):
        mapping = {
            ("高等代数", "内积空间"): [
                "chapterB7_09_内积空间.tex"
            ],
            ("高等代数", "双线性型"): [
                "chapterB7_10_双线性型.tex",
                "chapter11_多重线性代数.tex",
            ],
        }
        self.write(
            "chapterB7_09_内积空间.tex",
            "".join(
                f"\\subsection{{题目{number}}}\n{LONG_A}{number}\n"
                for number in (88, 89, 92, 93, 94, 95, 96)
            ),
        )
        self.write(
            "chapterB7_10_双线性型.tex",
            "\n".join(
                f"{number}．{LONG_B}{number}"
                for number in (20, 21, 22, 23)
            ),
        )
        self.write("chapter11_多重线性代数.tex", "")

        index = ensure_question_bank_index(
            self.tex, index_path=self.index_path, catalog=catalog_for(mapping)
        )
        coverage = index.coverage()
        self.assertEqual(coverage["total_questions"], 5)
        self.assertEqual(
            coverage["excluded_by_reason"]["excluded_generalized_inverse"],
            3,
        )
        self.assertEqual(
            coverage["excluded_by_reason"]["excluded_symplectic_matrix"], 1
        )
        self.assertEqual(
            coverage["excluded_by_reason"]["excluded_symplectic_space"], 2
        )
        self.assertEqual(
            coverage["excluded_by_reason"][
                "excluded_whole_file_empty_placeholder"
            ],
            1,
        )

    def test_chapter5_and_suffix_ranges_are_excluded(self):
        mapping = {
            ("高等代数", "相似标准型"): [
                "chapter5_矩阵的相抵与相似.tex"
            ],
            ("数学分析", "重积分"): [
                "chapterB4_07_多元积分学.tex",
                "chapterB5_07_多元积分学.tex",
            ],
        }
        self.write(
            "chapter5_矩阵的相抵与相似.tex",
            "\\textbf{5.2 前置}\n\\subsection{题目1}\n"
            + LONG_A
            + "\n\\textbf{5.3 广义逆矩阵}\n\\subsection{题目2}\n"
            + LONG_B
            + "\n\\textbf{5.4 相似}\n\\subsection{题目3}\n"
            + LONG_C
            + "\n\\textbf{习题 5.3}\n\\subsection{题目4}\n"
            + LONG_A
            + "\n\\textbf{习题 5.4}\n\\subsection{题目5}\n"
            + LONG_B,
        )
        self.write(
            "chapterB4_07_多元积分学.tex",
            "1．"
            + LONG_A
            + "\n7.5 场论\n2．"
            + LONG_B,
        )
        self.write(
            "chapterB5_07_多元积分学.tex",
            "1．"
            + LONG_A
            + "\n单元练习7．5\n2．"
            + LONG_C,
        )
        index = ensure_question_bank_index(
            self.tex, index_path=self.index_path, catalog=catalog_for(mapping)
        )
        coverage = index.coverage()
        self.assertEqual(
            coverage["excluded_by_reason"]["excluded_section_5_3"], 1
        )
        self.assertEqual(
            coverage["excluded_by_reason"]["excluded_exercises_5_3"], 1
        )
        self.assertEqual(
            coverage["excluded_by_reason"][
                "excluded_from_section_7_5_field_theory"
            ],
            1,
        )
        self.assertEqual(
            coverage["excluded_by_reason"][
                "excluded_from_unit_exercises_7_5"
            ],
            1,
        )

    def test_coverage_route(self):
        mapping = {("数学分析", "数列极限"): ["route.tex"]}
        self.write("route.tex", "\\subsection{题目1}\n" + LONG_A)
        database = self.root / "app.sqlite3"
        pdf_dir = self.root / "pdfs"
        catalog_path = self.root / "catalog.json"
        catalog_path.write_text(
            __import__("json").dumps({
                "schema_version": 1,
                "subjects": [{
                    "subject_code": "math_analysis",
                    "name": "数学分析",
                    "display_order": 10,
                    "fallback_source": "综合题目_数学分析.tex",
                    "chapters": [{
                        "chapter_code": "math_analysis.chapter_02",
                        "name": "数列极限",
                        "display_order": 20,
                        "parent_code": None,
                        "sources": ["route.tex"],
                    }],
                }],
                "exclusions": [],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        app = create_app(
                {
                    "TESTING": True,
                    "DATABASE": str(database),
                    "PDF_DIR": str(pdf_dir),
                    "TEX_BASE": str(self.tex),
                    "QUESTION_BANK_DATABASE": str(self.index_path),
                    "CATALOG_PATH": str(catalog_path),
                }
            )
        with app.app_context():
            init_db()
            db = get_db()
            subject_id = db.execute(
                    "SELECT id FROM subjects WHERE name='数学分析'"
                ).fetchone()["id"]
            chapter_id = db.execute(
                    "SELECT id FROM chapters WHERE subject_id=? AND name='数列极限'",
                    (subject_id,),
                ).fetchone()["id"]
        response = app.test_client().get(
                "/api/v2/question-bank/coverage",
                query_string={
                    "subject_id": subject_id,
                    "chapter_ids": str(chapter_id),
                },
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["status"], "ready", body)
        self.assertEqual(body["total_questions"], 1)
        self.assertTrue(body["snapshot_id"])
        self.assertEqual(body["subject_id"], subject_id)
        self.assertEqual(body["chapters"][0]["chapter_id"], chapter_id)


if __name__ == "__main__":
    unittest.main()
