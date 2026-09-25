from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cuoti_app.services.material_understanding import (
    coverage_batches,
    ensure_material_understanding,
    freeze_manifest,
)
from cuoti_app.services.past_exams import candidate_prompt_text, learning_sample_questions


def candidates(count=7):
    return [
        {
            "candidate_id": f"Q{index}", "chapter": "章节甲" if index<4 else "章节乙",
            "source_file": "a.tex" if index%2 else "b.tex", "source_number": index,
            "source_ordinal": index, "content": f"完整题干-{index}-"+("x"*200),
            "token_estimate": 60,
        }
        for index in range(1,count+1)
    ]


class MaterialUnderstandingTests(unittest.TestCase):
    def test_batches_cover_middle_and_last_question_without_truncation(self):
        manifest=freeze_manifest(candidates())
        batches=coverage_batches(manifest,token_budget=450)
        ids=[item["candidate_id"] for batch in batches for item in batch]
        self.assertEqual(ids,[f"Q{index}" for index in range(1,8)])
        self.assertGreater(len(batches),1)
        self.assertIn("Q4",ids);self.assertEqual(ids[-1],"Q7")

    def test_complete_cache_reuses_all_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            calls=[]
            def analyze(batch,freeze_id=None,on_cards=None):
                calls.extend(item["candidate_id"] for item in batch)
                cards = [{"candidate_id":item["candidate_id"],"content_hash":item["content_hash"],
                         "chapter":item["chapter"],"source_file":item["source_file"],
                         "source_number":item["source_number"],"knowledge_points":[],
                         "question_type":"证明题","methods":[],"difficulty":"中等",
                         "discrimination":"中等","similarity_group":"","characteristics":[],
                         "prerequisites":[],"common_traps":[],"semantic_keywords":[],"source_constraints":[],
                         "analysis_confidence":0.9}
                        for item in batch]
                if on_cards:
                    on_cards(cards)
                return cards
            with patch("cuoti_app.services.material_understanding._analyze_batch",side_effect=analyze),patch(
                "cuoti_app.services.material_understanding._summarize",
                return_value={"chapters":[],"global":{}},
            ):
                first=ensure_material_understanding(candidates=candidates(),snapshot="s1",model="m1",cache_dir=directory)
                second=ensure_material_understanding(candidates=candidates(),snapshot="s1",model="m1",cache_dir=directory)
            self.assertEqual(first["analyzed_count"],7)
            self.assertEqual(calls,[f"Q{index}" for index in range(1,8)])
            self.assertEqual(second["cache_key"],first["cache_key"])

    def test_past_year_questions_and_prompt_keep_complete_bodies(self):
        long_tail="尾部唯一标记"+("z"*1400)
        samples=[{"year":2026,"text":"\\subsection{题目1}\n题干一"+("a"*30)+"\n\\subsection{题目2}\n"+long_tail}]
        questions=learning_sample_questions(samples)
        self.assertEqual(len(questions),2)
        self.assertIn("尾部唯一标记",questions[-1]["content"])
        prompt=candidate_prompt_text([{"candidate_id":"P1","chapter":"板块","source_file":"x.tex","source_number":1,"content":long_tail}])
        self.assertTrue(prompt.endswith(long_tail))


if __name__ == "__main__":
    unittest.main()
