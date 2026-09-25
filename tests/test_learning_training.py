from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from cuoti_app.services.exam_generation_quality import QualityPaperInput, enforce_exam_quality
from cuoti_app.services.exam_generation_drafts import _public_quality_review_summary
from cuoti_app.services.exam_local_repair import freeze_generation_evidence
from cuoti_app.services.exam_prompting import build_exam_quality_review_messages
from cuoti_app.services.exam_quality_review import validate_review_payload
from cuoti_app.services.learning_training import validate_training_target
from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services.exam_generation import generate_exam
from cuoti_app.services.exam_runtime import build_exam_generation_runtime
from cuoti_app.services.exam_generation_raw_papers import RawGeneratedBatch
from cuoti_app.services.exam_repetition_policy import question_content_fingerprint
from cuoti_app.services.exam_generation_quality_stage import run_mistakes_quality_stage


def _review(status="match", reason=""):
    return {"items": [{
        "position": 1,
        "actual_chapter": "极限",
        "actual_question_type": "计算题",
        "actual_difficulty": "中等",
        "section_slot_match": True,
        "reference_grounded": True,
        "duplicate_group": "",
        "duplicate_risk": 0.0,
        "adaptation_scope_valid": True,
        "answer_leak": False,
        "issues": [],
        "confidence": 0.9,
        "training_target_status": status,
        "training_target_reason": reason,
    }]}


def _item(content="求函数在给定条件下的极限。"):
    return {
        "position": 1, "content": content, "candidate_id": "m1",
        "reference_ids": ["m1"], "chapter": "极限", "target_chapter": "极限",
        "type": "计算题", "target_question_type": "计算题",
        "difficulty": "中等", "target_difficulty": "中等",
    }


def test_training_target_is_explicit_and_changes_only_new_frozen_evidence():
    assert validate_training_target(None, explicit=False) is None
    assert validate_training_target("general", explicit=True) == "general"
    with pytest.raises(ValueError):
        validate_training_target("other", explicit=True)
    old = freeze_generation_evidence(
        "mistakes", expected_count=1, source_records=[{"public_id": "m1"}],
        constraints={"subject": "数学分析"},
    )
    new = freeze_generation_evidence(
        "mistakes", expected_count=1, source_records=[{"public_id": "m1"}],
        constraints={"subject": "数学分析", "training_target": {"code": "general"}},
    )
    assert "training_target" not in old.payload()["constraints"]
    assert old.sha256 != new.sha256


def test_training_review_schema_requires_reason_only_for_mismatch():
    common = dict(items=[_item()], allowed_chapters=["极限"],
                  allowed_question_types=["计算题"], training_target="concept")
    matched = validate_review_payload(_review("match"), **common)
    assert matched.passed
    unknown = validate_review_payload(_review("unknown", "题面信息不足"), **common)
    assert unknown.passed
    speculative = _review("unknown", "可能有其他解法")
    speculative["items"][0]["issues"] = ["training_target_mismatch"]
    assert validate_review_payload(speculative, **common).passed
    mismatch = validate_review_payload(_review("mismatch", "题面只要求直接计算，未突出概念辨析。"), **common)
    assert not mismatch.passed
    assert mismatch.failed_positions == (1,)
    assert mismatch.items[0]["issues"] == ["training_target_mismatch"]
    with pytest.raises(ValueError):
        validate_review_payload(_review("mismatch", ""), **common)


def test_training_prompt_keeps_multi_solution_language_non_suspicious():
    messages = build_exam_quality_review_messages(
        subject="数学分析", allowed_chapters=["极限"],
        allowed_question_types=["计算题"], items=[_item()],
        prompt_version="exam-quality-review-training-v2",
        training_target="method_selection",
    )
    text = "\n".join(message["content"] for message in messages)
    assert "method_selection" in text
    assert "多种解法" in text
    assert "不得求解" in text


def test_mismatch_gets_one_targeted_repair_and_one_rereview():
    calls = []
    repair_prompt = []
    responses = iter([
        json.dumps(_review("mismatch", "题面未体现概念辨析。"), ensure_ascii=False),
        "比较两个相近概念在给定条件下的适用范围。",
        json.dumps(_review("match"), ensure_ascii=False),
    ])

    def ai_call(messages, **kwargs):
        calls.append(kwargs.get("task_policy"))
        if kwargs.get("task_policy") == "exam_item_repair":
            repair_prompt.append("\n".join(message["content"] for message in messages))
        return next(responses)

    item = _item()
    paper = QualityPaperInput(
        body=r"\begin{problem}求函数在给定条件下的极限。\end{problem}",
        items=(item,),
        reference_evidence={"m1": {"candidate_id": "m1", "content": "原错题"}},
    )
    result = enforce_exam_quality(
        (paper,), subject="数学分析", allowed_chapters=("极限",),
        allowed_question_types=("计算题",), ai_call=ai_call,
        same_model_review=False, quality_metadata={"training_target": "concept"},
        source_mode="mistakes",
    )
    assert calls == ["exam_quality_review", "exam_item_repair", "exam_quality_review"]
    assert "concept" in repair_prompt[0]
    assert "题面未体现概念辨析" in repair_prompt[0]
    assert "比较两个相近概念" in result.bodies[0]
    review = result.reviews[0]
    assert review["training_target"] == "concept"
    assert review["repair"]["repaired_positions"] == [1]
    assert review["repair"]["rereview_count"] == 1


def test_public_summary_preserves_target_status_without_problem_content():
    payload = {
        **_review("mismatch", "题面只训练计算，没有要求核对条件。"),
        "passed": False, "failed_positions": [1], "same_model_review": False,
        "prompt_version": "exam-quality-review-training-v2",
        "schema_version": "exam-quality-review-training-v2",
        "training_target": "condition_check",
    }
    summary = _public_quality_review_summary(payload)
    assert summary["training_target"] == "condition_check"
    assert summary["training_target_counts"] == {"mismatch": 1}
    assert summary["training_target_findings"] == [{
        "position": 1, "status": "mismatch",
        "reason": "题面只训练计算，没有要求核对条件。",
    }]
    assert "content" not in json.dumps(summary, ensure_ascii=False)


@pytest.mark.parametrize("delivery_failure", [False, True])
def test_real_mistakes_orchestration_carries_explicit_target_to_config(delivery_failure):
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tex = root / "tex"
        tex.mkdir()
        app = create_app({
            "TESTING": True,
            "DATABASE": str(root / "data.db"),
            "PDF_DIR": str(root / "pdfs"),
            "TEX_BASE": str(tex),
            "QUESTION_BANK_DATABASE": str(root / "question-bank.db"),
            "AI_MEMORY_DIR": str(root / "memory"),
            "PAST_EXAM_DIR": str(root / "past"),
            "DEEPSEEK_API_KEY": "fixture-key",
            "AI_API_KEY_CONFIG_OVERRIDE": True,
            "XELATEX_COMMAND": "fixture-xelatex",
        })
        connection = sqlite3.connect(root / "data.db")
        subject_id, chapter_id, chapter_name = connection.execute(
            "SELECT s.id,c.id,c.name FROM subjects s JOIN chapters c ON c.subject_id=s.id "
            "ORDER BY s.id,c.id LIMIT 1"
        ).fetchone()
        now = "2026-09-17T10:00:00+00:00"
        public_id = str(uuid.uuid4())
        cursor = connection.execute(
            "INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (public_id, subject_id, chapter_id, "1", "求一个函数极限。", "2026-09-17", now),
        )
        mistake_id = int(cursor.lastrowid)
        connection.commit()
        connection.close()
        def raw_batch(request, **_kwargs):
            slot = request.lineage_slots[0]
            item = {
                    "position": 1, "chapter": slot.target_chapter,
                    "type": slot.target_question_type,
                    "question_type": slot.target_question_type, "difficulty": "中等",
                "content": "求另一个函数极限。", "latex": "求另一个函数极限。",
                "reference_ids": list(slot.source_mistake_ids),
                "source_mistake_ids": list(slot.source_mistake_ids),
                "source_lineage_hash": slot.source_lineage_hash,
                "lineage_policy_version": slot.policy_version,
                "content_fingerprint": question_content_fingerprint("求另一个函数极限。"),
            }
            return RawGeneratedBatch(
                bodies=(r"\begin{problem}求另一个函数极限。\end{problem}",),
                structured_items=(item,), difficulty_labels=("中等",),
                frozen_evidence=request.frozen_evidence,
            )

        review_values = iter([
            json.dumps({"items": [{**_review("mismatch", "题面未体现迁移应用。")["items"][0],
                                   "actual_chapter": chapter_name}]}, ensure_ascii=False),
            "在新的表示方式下求对应函数极限。",
            json.dumps({"items": [{**_review("match")["items"][0],
                                   "actual_chapter": chapter_name}]}, ensure_ascii=False),
        ])

        def quality_stage(request, **_kwargs):
            return run_mistakes_quality_stage(
                replace(request, testing=False),
                ai_call=lambda *_args, **_call_kwargs: next(review_values),
            )

        compiled = []

        def compile_fixture(pdf_dir, _command, exam_id, latex, **_kwargs):
            compiled.append(exam_id)
            Path(pdf_dir).mkdir(exist_ok=True)
            (Path(pdf_dir) / f"exam_{exam_id}.pdf").write_bytes(b"%PDF-fixture")
            (Path(pdf_dir) / f"exam_{exam_id}.tex").write_text(latex, encoding="utf-8")

        with app.app_context(), patch(
            "cuoti_app.services.exams.mistakes.generate_structured_mistakes_raw_papers",
            side_effect=raw_batch,
        ), patch(
            "cuoti_app.services.exams.mistakes.audit_question_difficulties",
            return_value=["中等"],
        ), patch(
            "cuoti_app.services.exams.mistakes.run_mistakes_quality_stage",
            side_effect=quality_stage,
        ), (patch(
            "cuoti_app.services.exam_generation_publish.dispatch_generation_memory_delivery",
            side_effect=OSError("memory temporarily unavailable"),
        ) if delivery_failure else nullcontext()):
            runtime = build_exam_generation_runtime(
                app, get_db(), ai_call=lambda *_args, **_kwargs: "{}",
                compile_exam=compile_fixture,
                progress=lambda *_args: None,
            )
            runtime = replace(runtime, require_deepseek_config=lambda: None)
            response = generate_exam({
                "source": "mistakes", "mistake_ids": [mistake_id],
                "difficulty": "中等", "target": "日常练习", "question_count": 1,
                "types": ["计算题"], "training_target": "transfer", "note": "",
            }, runtime)
        assert response.status == (503 if delivery_failure else 200), response.body
        if delivery_failure:
            assert response.body["code"] == "memory_delivery_pending"
        else:
            assert response.body["memory_delivery"] == "delivered"
        connection = sqlite3.connect(root / "data.db")
        connection.row_factory = sqlite3.Row
        from cuoti_app.migrations.v54_exam_generation_memory_delivery import MEMORY_EVENT_DELIVERY_HEADS
        from cuoti_app.services.exam_generation_memory_delivery import dispatch_generation_memory_delivery
        from cuoti_app.services.ai_memory import AIMemory

        exam = connection.execute("SELECT id,artifact_status FROM exams").fetchone()
        assert exam["artifact_status"] == "published"
        assert compiled == [exam["id"]]
        assert (root / "pdfs" / f"exam_{exam['id']}.pdf").read_bytes() == b"%PDF-fixture"
        assert (root / "pdfs" / f"exam_{exam['id']}.tex").is_file()
        delivery = connection.execute(f"SELECT operation_id,state FROM {MEMORY_EVENT_DELIVERY_HEADS}").fetchone()
        assert delivery["state"] == ("pending" if delivery_failure else "delivered")
        for _ in range(2):
            result = dispatch_generation_memory_delivery(
                connection, operation_id=delivery["operation_id"], memory=AIMemory(root / "memory"),
            )
            assert result["state"] == "delivered"
        assert connection.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
        events = [
            json.loads(line)
            for path in (root / "memory/mistakes/events/daily").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        assert [event["event_type"] for event in events] == ["request", "result"]
        config = json.loads(connection.execute(
            "SELECT config FROM exams ORDER BY id DESC LIMIT 1"
        ).fetchone()[0])
        connection.close()
        assert config["training_target"] == "transfer"
        assert config["quality_review"]["training_target"] == "transfer"
        assert config["quality_review"]["training_target_counts"] == {"match": 1}
        assert config["quality_review"]["repair"]["repaired_positions"] == [1]


def test_generate_exam_past_branch_never_reads_training_target():
    class ReachedPastMastery(RuntimeError):
        pass

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "tex").mkdir()
        app = create_app({
            "TESTING": True, "DATABASE": str(root / "data.db"),
            "PDF_DIR": str(root / "pdfs"), "TEX_BASE": str(root / "tex"),
            "QUESTION_BANK_DATABASE": str(root / "question-bank.db"),
            "AI_MEMORY_DIR": str(root / "memory"), "PAST_EXAM_DIR": str(root / "past"),
        })
        frozen = SimpleNamespace(
            source="past", memory_type="past_exam", memory_actor={}, operation_id="op",
            pending_memory_request={}, allow_adaptation=False,
        )
        preflight = SimpleNamespace(completed=False, generation_job_id=None, request=frozen)
        context = SimpleNamespace(
            subject_id=1, subject="数学分析", chapters=("极限",), difficulty="中等",
            target="考研训练", question_count=1, question_types=("计算题",), note="",
            title="真题", exam_type="真题", system_prompt="", prompt="", memory_context="",
            past_source={}, institution="测试大学", paper_count=1, allow_adaptation=False,
            reference_year=2026, reference_section={}, structure_contract={},
            candidate_source="ordinary", source_quotas={},
        )
        with app.app_context(), patch(
            "cuoti_app.services.exams.generation.prepare_generation_preflight",
            return_value=preflight,
        ), patch(
            "cuoti_app.services.exams.generation.task_frozen_mode_memory",
            side_effect=lambda memory, **_kwargs: memory,
        ), patch(
            "cuoti_app.services.exams.legacy_past.prepare_past_source_context",
            return_value=SimpleNamespace(failure=None, context=context),
        ), patch(
            "cuoti_app.services.exams.legacy_past.load_latest_mistake_mastery_by_fingerprint",
            side_effect=ReachedPastMastery,
        ):
            runtime = build_exam_generation_runtime(
                app, get_db(), ai_call=lambda *_args, **_kwargs: "{}",
                compile_exam=lambda *_args, **_kwargs: None,
                progress=lambda *_args: None,
                allow_legacy_past_characterization=True,
            )
            runtime = replace(runtime, require_deepseek_config=lambda: None)
            with pytest.raises(ReachedPastMastery):
                generate_exam({"source": "past"}, runtime)
