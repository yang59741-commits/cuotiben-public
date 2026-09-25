import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from cuoti_app.routes.exams_legacy import (
    _difficulty_quota,
    _generate_indexed_free_exam,
    _require_current_question_bank_snapshot,
    _validate_past_analysis_contract,
    _validate_free_reference_ids,
    _validated_past_selection,
)


class PastAnalysisContractTests(unittest.TestCase):
    def setUp(self):
        self.years = [2024, 2025, 2026]
        self.structure = {
            "sections": [
                {"label": "一、计算题", "question_count": 2},
                {"label": "二、证明题", "question_count": 1},
            ]
        }
        self.analysis = {
            "learned_years": list(self.years),
            "structure_summary": "两板块逐步递进",
            "sections": [
                {"title": "计算题", "role": "基础", "difficulty": "中等"},
                {"title": "证明题", "role": "综合", "difficulty": "困难"},
            ],
            "topic_distribution": ["线性变换"],
            "difficulty_progression": "由计算到证明",
            "style_rules": ["重视结构化推理"],
        }

    def test_contract_accepts_matching_years_and_sections(self):
        normalized = _validate_past_analysis_contract(
            self.analysis,
            self.years,
            self.structure,
        )
        self.assertEqual(normalized["learned_years"], self.years)
        self.assertEqual([item["title"] for item in normalized["sections"]], ["计算题", "证明题"])

    def test_contract_rejects_missing_style_and_wrong_years(self):
        missing_style = dict(self.analysis)
        missing_style.pop("style_rules")
        with self.assertRaisesRegex(ValueError, "命题风格规则"):
            _validate_past_analysis_contract(
                missing_style,
                self.years,
                self.structure,
            )

        wrong_years = dict(self.analysis, learned_years=[2023, 2024, 2025])
        with self.assertRaisesRegex(ValueError, "冻结年份不一致"):
            _validate_past_analysis_contract(
                wrong_years,
                self.years,
                self.structure,
            )


class ExamDifficultyQuotaTests(unittest.TestCase):
    def test_easy_questions_are_strictly_smallest_for_one_to_twenty(self):
        for overall in ("基础", "中等", "困难"):
            for count in range(1, 21):
                with self.subTest(overall=overall, count=count):
                    quota = _difficulty_quota(count, overall)
                    self.assertEqual(sum(quota.values()), count)
                    self.assertEqual(
                        quota["简单"],
                        count // 5 if count >= 5 else 0,
                    )
                    for label in ("中等", "困难"):
                        if quota[label]:
                            self.assertLess(quota["简单"], quota[label])

    def test_overall_difficulty_only_changes_medium_hard_balance(self):
        basic = _difficulty_quota(20, "基础")
        medium = _difficulty_quota(20, "中等")
        hard = _difficulty_quota(20, "困难")
        self.assertGreater(basic["中等"], basic["困难"])
        self.assertEqual(medium["中等"], medium["困难"])
        self.assertLess(hard["中等"], hard["困难"])
        self.assertEqual(
            {basic["简单"], medium["简单"], hard["简单"]},
            {4},
        )

    @patch("cuoti_app.routes.exams_legacy.deepseek_api_call")
    def test_independent_audit_labels_replace_subjective_generation_labels(
        self,
        deepseek,
    ):
        generated = {
            "knowledge_prelude": "预备知识",
            "questions": [
                {
                    "position": position,
                    "type": "计算题",
                    "difficulty": label,
                    "latex": f"测试题目{position}",
                    "reference_ids": [],
                }
                for position, label in enumerate(
                    ["简单", "中等", "中等", "困难", "困难"],
                    start=1,
                )
            ],
        }
        audited = ["中等", "简单", "困难", "中等", "困难"]
        audit_payload = {
            "items": [
                {"position": position, "difficulty": label}
                for position, label in enumerate(audited, start=1)
            ]
        }
        deepseek.side_effect = [
            json.dumps(generated, ensure_ascii=False),
            json.dumps(audit_payload, ensure_ascii=False),
        ]
        app = Flask("difficulty-audit-test")
        app.config["TESTING"] = False
        with app.app_context():
            _, items, labels = _generate_indexed_free_exam(
                "系统提示",
                "用户提示",
                5,
                {"简单": 1, "中等": 2, "困难": 2},
                "数学分析",
            )
        self.assertEqual(labels, audited)
        self.assertEqual(
            [item["difficulty"] for item in items],
            audited,
        )
        self.assertEqual(deepseek.call_count, 2)

    @patch("cuoti_app.routes.exams_legacy._audit_question_difficulties")
    def test_past_selection_uses_independent_audit_labels(self, audit):
        audited = ["中等", "简单", "困难", "中等", "困难"]
        audit.return_value = audited
        candidates = [
            {
                "candidate_id": f"QB{position:024X}",
                "chapter": "数列极限",
                "source_file": "chapter.tex",
                "source_number": str(position),
                "content": f"完整测试题目{position}，用于独立难度复核。",
            }
            for position in range(1, 6)
        ]
        raw = json.dumps(
            {
                "selected": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "position": position,
                        "difficulty": label,
                        "reason": "覆盖章节",
                        "adapted_content": "",
                    }
                    for position, (candidate, label) in enumerate(
                        zip(
                            candidates,
                            ["简单", "中等", "中等", "困难", "困难"],
                        ),
                        start=1,
                    )
                ]
            },
            ensure_ascii=False,
        )
        app = Flask("past-difficulty-audit-test")
        app.config["TESTING"] = False
        with app.app_context():
            selected = _validated_past_selection(
                raw,
                candidates,
                5,
                [{"chapter": "数列极限", "question_count": 5}],
                {"简单": 1, "中等": 2, "困难": 2},
                allow_adaptation=False,
                subject="数学分析",
            )
        self.assertEqual(
            [item["difficulty"] for item in selected],
            audited,
        )
        audit.assert_called_once()

    def test_free_reference_ids_must_be_nonempty_and_from_candidates(self):
        items = [
            {"position": 1, "reference_ids": ["QB" + ("A" * 24)]},
            {"position": 2, "reference_ids": ["QB" + ("B" * 24)]},
        ]
        _validate_free_reference_ids(
            items,
            ["QB" + ("A" * 24), "QB" + ("B" * 24)],
        )
        with self.assertRaisesRegex(ValueError, "没有返回参考题ID"):
            _validate_free_reference_ids(
                [{"position": 1, "reference_ids": []}],
                ["QB" + ("A" * 24)],
            )
        with self.assertRaisesRegex(ValueError, "候选清单外"):
            _validate_free_reference_ids(
                [{"position": 1, "reference_ids": ["QB" + ("C" * 24)]}],
                ["QB" + ("A" * 24)],
            )

    @patch("cuoti_app.routes.exams_legacy._question_bank_index")
    def test_snapshot_change_during_ai_rolls_back(self, current_index):
        current_index.return_value = SimpleNamespace(snapshot_id="new-snapshot")
        with self.assertRaisesRegex(RuntimeError, "AI组卷期间发生变化"):
            _require_current_question_bank_snapshot("frozen-snapshot")


if __name__ == "__main__":
    unittest.main()
