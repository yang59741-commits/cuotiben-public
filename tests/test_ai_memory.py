from __future__ import annotations

import tempfile
import unittest
import json
from datetime import date
from pathlib import Path

from cuoti_app.services.ai_memory import AIMemory


class AIMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.memory = AIMemory(Path(self.temp.name), daily_days=30)

    def tearDown(self):
        self.temp.cleanup()

    def test_role_permissions_and_idempotent_append(self):
        event = {
            "event_id": "same-event",
            "event_type": "result",
            "subject": "数学分析",
            "questions": [{"content": "题目内容", "fingerprint": "abc"}],
        }
        first = self.memory.append_event("free", "free_exam", event, day="2026-07-31")
        second = self.memory.append_event("free", "free_exam", event, day="2026-07-31")
        self.assertEqual(first["event_id"], second["event_id"])
        daily = Path(self.temp.name) / "free" / "events" / "daily" / "2026-07-31.jsonl"
        self.assertEqual(len(daily.read_text(encoding="utf-8").splitlines()), 1)
        with self.assertRaises(PermissionError):
            self.memory.append_event("free", "mistake_exam", event, day="2026-07-31")
        self.assertEqual(self.memory.read_context("mistakes", "mistake_analysis"), "")

    def test_context_is_namespace_scoped(self):
        today = date.today().isoformat()
        self.memory.append_event(
            "free",
            "free_exam",
            {"event_type": "request", "subject": "数学分析"},
            day=today,
        )
        self.memory.append_event(
            "mistakes",
            "mistake_analysis",
            {"event_type": "analysis", "result": {"key_concept": "极限"}},
            day=today,
        )
        self.assertIn("数学分析", self.memory.read_context("free", "free_exam"))
        self.assertNotIn("数学分析", self.memory.read_context("mistakes", "mistake_exam"))
        self.assertIn("极限", self.memory.read_context("mistakes", "mistake_exam"))

    def test_exam_input_snapshot_projects_one_attested_inventory(self):
        today = date.today().isoformat()
        self.memory.append_event(
            "free",
            "free_exam",
            {
                "event_id": "snapshot-event",
                "event_type": "result",
                "subject": "数学分析",
                "questions": [
                    {
                        "candidate_id": "candidate-1",
                        "fingerprint": "fingerprint-1",
                    }
                ],
            },
            day=today,
        )
        original = self.memory._attest_namespace_read_only
        calls = []

        def counted(memory_type):
            calls.append(memory_type)
            return original(memory_type)

        self.memory._attest_namespace_read_only = counted
        snapshot = self.memory.read_exam_inputs_snapshot(
            "free", "free_exam", {"subject": "数学分析"}
        )

        self.assertIn("数学分析", snapshot["context"])
        self.assertEqual(snapshot["recent_question_ids"], ["candidate-1"])
        self.assertEqual(
            snapshot["recent_question_fingerprints"], ["fingerprint-1"]
        )
        self.assertEqual(calls, ["free"])

    def test_unauthorized_exam_snapshot_does_not_initialize_namespace(self):
        self.assertEqual(
            self.memory.read_exam_inputs_snapshot(
                "free", "mistake_exam", {"subject": "数学分析"}
            ),
            {
                "context": "",
                "recent_question_ids": [],
                "recent_question_fingerprints": [],
            },
        )
        self.assertFalse((Path(self.temp.name) / "free").exists())

    def test_expired_days_are_summarized_and_removed(self):
        self.memory.append_event(
            "past",
            "past_exam",
            {
                "event_type": "result",
                "subject": "高等代数",
                "chapters": ["矩阵"],
                "request": {"difficulty": "困难", "types": ["证明题"]},
            },
            day="2026-06-01",
        )
        archived = self.memory.rollover_expired_days("past", today="2026-07-31")
        self.assertEqual(archived, ["2026-06-01"])
        self.assertFalse(
            (Path(self.temp.name) / "past" / "daily" / "2026-06-01.jsonl").exists()
        )
        summary = (Path(self.temp.name) / "past" / "long_term.md").read_text(encoding="utf-8")
        self.assertIn("高等代数", summary)
        self.assertIn("矩阵", summary)

    def test_schema_two_profile_requires_repeat_days_and_filters_context(self):
        for index, day in enumerate(("2026-07-30", "2026-07-31", "2026-07-31"), start=1):
            self.memory.append_event(
                "free", "free_exam",
                {"event_id": f"e-{index}", "event_type": "result", "subject": "数学分析", "chapters": ["极限"], "request": {"difficulty": "困难"}},
                day=day,
            )
        self.memory.rebuild("free")
        profile = json.loads((Path(self.temp.name) / "free" / "profile.json").read_text(encoding="utf-8"))
        subject = next(item for item in profile["entries"] if item["key"] == "subject:数学分析")
        self.assertEqual(subject["status"], "stable")
        self.assertEqual(subject["distinct_days"], 2)
        context = self.memory.read_context("free", "free_exam", {"subject": "数学分析", "chapters": ["极限"]})
        self.assertIn("稳定画像", context)
        self.assertNotIn("solution", context.lower())

    def test_pause_and_profile_confirmation_are_namespace_scoped(self):
        self.memory.set_paused("past", True)
        paused = self.memory.append_event("past", "past_exam", {"event_id": "paused", "event_type": "result"}, day="2026-07-31")
        self.assertTrue(paused["memory_paused"])
        self.assertEqual(self.memory.status()["namespaces"][2]["event_count"], 0)
        self.memory.set_paused("past", False)


if __name__ == "__main__":
    unittest.main()
