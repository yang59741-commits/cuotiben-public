from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from cuoti_app.services import ai_memory as memory_module
from cuoti_app.services.ai_memory import AIMemory
from cuoti_app.services.exam_generation_mode_memory_authority import (
    MemoryAuthorityError,
    build_memory_access_authority,
    build_memory_snapshot,
    build_memory_write_decision,
    validate_memory_access_authority,
)


# Keep these expectations independent from the implementation constants.  If a
# permission matrix changes, this file should fail for an explicit review.
EXPECTED_READS = {
    "free_exam": {"free", "mistakes"},
    "mistake_analysis": set(),
    "mistake_exam": {"mistakes"},
    "past_exam": {"mistakes", "past"},
}
EXPECTED_WRITES = {
    "free_exam": {"free"},
    "mistake_analysis": {"mistakes"},
    "mistake_exam": {"mistakes"},
    "past_exam": {"past"},
}
MEMORY_TYPES = ("free", "mistakes", "past")
ROLES = tuple(EXPECTED_READS)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 18)

    monkeypatch.setattr(__import__(__name__, fromlist=["date"]), "date", FixedDate)
    monkeypatch.setattr(
        memory_module, "_local_now",
        lambda: datetime(2026, 10, 18, 12, tzinfo=timezone.utc),
    )


def _seed_memory(memory: AIMemory) -> None:
    today = date.today().isoformat()
    memory.append_event(
        "free",
        "free_exam",
        {
            "event_id": "free-seed",
            "event_type": "result",
            "subject": "数学",
            "chapters": ["自由章节"],
            "questions": [
                {"question_id": "free-question", "fingerprint": "free-fingerprint"}
            ],
        },
        day=today,
    )
    memory.append_event(
        "mistakes",
        "mistake_analysis",
        {
            "event_id": "analysis-seed",
            "event_type": "analysis",
            "subject": "数学",
            "chapters": ["错题章节"],
            "questions": [
                {
                    "question_id": "mistake-question",
                    "fingerprint": "mistake-fingerprint",
                }
            ],
            "result": {"key_concept": "分析观察"},
        },
        day=today,
    )
    memory.append_event(
        "past",
        "past_exam",
        {
            "event_id": "past-seed",
            "event_type": "result",
            "subject": "数学",
            "chapters": ["真题章节"],
            "questions": [
                {"question_id": "past-question", "fingerprint": "past-fingerprint"}
            ],
        },
        day=today,
    )


@pytest.mark.parametrize("actor", (*ROLES, "unknown_role"))
@pytest.mark.parametrize("memory_type", MEMORY_TYPES)
def test_read_matrix_is_explicit_and_bidirectional(tmp_path, actor, memory_type):
    memory = AIMemory(tmp_path / "memory")
    _seed_memory(memory)
    filters = {"subject": "数学"}
    allowed = memory_type in EXPECTED_READS.get(actor, set())

    context = memory.read_context(memory_type, actor, filters)
    attested = memory.read_context_attested(memory_type, actor, filters)
    snapshot = memory.read_exam_inputs_snapshot(memory_type, actor, filters)
    question_ids = memory.recent_question_ids(memory_type, actor)
    fingerprints = memory.recent_question_fingerprints(memory_type, actor)

    if not allowed:
        assert context == ""
        assert attested["context"] == ""
        assert snapshot == {
            "context": "",
            "recent_question_ids": [],
            "recent_question_fingerprints": [],
        }
        assert question_ids == set()
        assert fingerprints == set()
        return

    assert context
    assert attested["context"] == context
    assert snapshot["context"] == context
    if memory_type == "free":
        assert question_ids == {"free-question"}
        assert fingerprints == {"free-fingerprint"}
    elif memory_type == "past":
        assert question_ids == {"past-question"}
        assert fingerprints == {"past-fingerprint"}
    else:
        # Mistake memory is preference evidence, never a dedupe source.
        assert question_ids == set()
        assert fingerprints == set()


@pytest.mark.parametrize("actor", (*ROLES, "unknown_role"))
@pytest.mark.parametrize("memory_type", MEMORY_TYPES)
def test_write_matrix_rejects_every_unlisted_route_without_initializing(
    tmp_path, actor, memory_type
):
    root = tmp_path / f"{actor}-{memory_type}"
    memory = AIMemory(root)
    event = {"event_id": "write-probe", "event_type": "result", "subject": "数学"}
    allowed = memory_type in EXPECTED_WRITES.get(actor, set())

    if allowed:
        stored = memory.append_event(memory_type, actor, event, day=date.today())
        assert stored["event_id"] == "write-probe"
        assert (root / memory_type).is_dir()
    else:
        with pytest.raises(PermissionError):
            memory.append_event(memory_type, actor, event, day=date.today())
        assert not root.exists()


def test_unauthorized_reads_do_not_initialize_a_namespace(tmp_path):
    root = tmp_path / "memory"
    memory = AIMemory(root)

    for actor in (*ROLES, "unknown_role"):
        for memory_type in MEMORY_TYPES:
            if memory_type in EXPECTED_READS.get(actor, set()):
                continue
            assert memory.read_context(memory_type, actor, {}) == ""
            assert memory.read_context_attested(memory_type, actor, {})["context"] == ""
            assert memory.read_exam_inputs_snapshot(memory_type, actor, {})["context"] == ""
            assert memory.recent_question_ids(memory_type, actor) == set()
            assert memory.recent_question_fingerprints(memory_type, actor) == set()
            assert not (root / memory_type).exists()


def test_mistakes_reads_only_use_analysis_actor_evidence(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    today = date.today().isoformat()
    memory.append_event(
        "mistakes",
        "mistake_analysis",
        {
            "event_id": "analysis-only",
            "event_type": "analysis",
            "subject": "数学",
            "result": {"key_concept": "分析授权观察"},
        },
        day=today,
    )
    memory.append_event(
        "mistakes",
        "mistake_exam",
        {
            "event_id": "exam-only",
            "event_type": "result",
            "subject": "数学",
            "result": {
                "exam_id": "mistake-paper",
                "title": "组卷来源不应进入偏好",
                "source": "mistakes",
            },
        },
        day=today,
    )

    for actor in ("free_exam", "mistake_exam", "past_exam"):
        context = memory.read_context("mistakes", actor, {"subject": "数学"})
        assert "分析授权观察" in context
        assert "组卷来源不应进入偏好" not in context

    assert memory.read_context("mistakes", "mistake_analysis", {}) == ""


def test_mistakes_never_provides_recent_deduplication_evidence(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    today = date.today().isoformat()
    memory.append_event(
        "mistakes",
        "mistake_analysis",
        {
            "event_id": "mistake-dedupe-probe",
            "event_type": "analysis",
            "subject": "数学",
            "questions": [
                {"question_id": "mistake-q", "fingerprint": "mistake-fp"}
            ],
        },
        day=today,
    )
    assert memory.recent_question_ids("mistakes", "free_exam") == set()
    assert memory.recent_question_ids("mistakes", "mistake_exam") == set()
    assert memory.recent_question_ids("mistakes", "past_exam") == set()
    assert memory.recent_question_fingerprints("mistakes", "free_exam") == set()
    assert memory.recent_question_fingerprints("mistakes", "mistake_exam") == set()
    assert memory.recent_question_fingerprints("mistakes", "past_exam") == set()


def _append_stable_free_evidence(memory: AIMemory, subject: str) -> None:
    today = date.today()
    for index, day in enumerate(
        (today - timedelta(days=1), today, today), start=1
    ):
        memory.append_event(
            "free",
            "free_exam",
            {
                "event_id": f"stable-{index}",
                "event_type": "result",
                "subject": subject,
                "chapters": ["范围章节"],
            },
            day=day,
        )


def test_source_actor_filtering_is_preserved_when_rebuilding(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    today = date.today()
    for actor, marker in (
        ("mistake_analysis", "分析来源"),
        ("mistake_exam", "错题组卷来源"),
    ):
        for index, day in enumerate((today - timedelta(days=1), today, today)):
            memory.append_event(
                "mistakes", actor,
                {
                    "event_id": f"actor-{actor}-{index}",
                    "event_type": "analysis" if actor == "mistake_analysis" else "result",
                    "subject": "数学",
                    "chapters": [marker],
                },
                day=day,
            )

    memory.rebuild("mistakes")
    reopened = AIMemory(tmp_path / "memory")
    for actor in ("free_exam", "mistake_exam", "past_exam"):
        context = reopened.read_context("mistakes", actor, {"subject": "数学"})
        stable_section = context.split("【稳定画像】")[1].split("【近期相关事件】")[0]
        assert "分析来源" in stable_section
        assert "错题组卷来源" not in context


def test_read_context_does_not_change_projection_or_refresh_stale_data(tmp_path):
    root = tmp_path / "memory"
    memory = AIMemory(root)
    memory.append_event(
        "free",
        "free_exam",
        {"event_id": "read-only-probe", "event_type": "result", "subject": "数学"},
        day=date.today(),
    )
    profile_path = root / "free" / "profile.json"
    profile_before = profile_path.read_bytes()
    manifest_before = (root / "free" / "manifest.json").read_bytes()
    memory.read_context("free", "free_exam", {"subject": "数学"})
    assert profile_path.read_bytes() == profile_before
    assert (root / "free" / "manifest.json").read_bytes() == manifest_before


def test_user_authority_is_not_replaced_by_new_automatic_evidence(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    _append_stable_free_evidence(memory, "用户否定范围")
    memory.command_profile("free", "subject:用户否定范围", action="reject")
    memory.append_event(
        "free",
        "free_exam",
        {
            "event_id": "later-evidence",
            "event_type": "result",
            "subject": "用户否定范围",
            "chapters": ["范围章节"],
        },
        day=date.today(),
    )
    restarted = AIMemory(tmp_path / "memory")
    entry = next(
        item
        for item in restarted.list_profile("free", page_size=100)["items"]
        if item["key"] == "subject:用户否定范围"
    )
    assert entry["status"] == "rejected"
    assert "用户否定范围" not in restarted.read_context(
        "free", "free_exam", {"subject": "用户否定范围"}
    )


def test_automatic_profiles_keep_distinct_evidence_and_dates(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    today = date.today()
    for index, day in enumerate(
        (today - timedelta(days=1), today, today), start=1
    ):
        memory.append_event(
            "free",
            "free_exam",
            {
                "event_id": f"distinct-{index}",
                "event_type": "result",
                "subject": "证据范围",
                "chapters": ["范围章节"],
            },
            day=day,
        )
    entry = next(
        item
        for item in memory.list_profile("free", page_size=100)["items"]
        if item["key"] == "subject:证据范围"
    )
    assert entry["evidence_count"] == 3
    assert entry["distinct_days"] == 2
    assert entry["status"] == "stable"
    assert entry["status"] != "confirmed"


@pytest.mark.parametrize(
    ("action", "expected_status", "expected_value"),
    [
        ("confirm", "confirmed", "原始范围"),
        ("correct", "confirmed", "用户纠正范围"),
        ("reject", "rejected", "原始范围"),
        ("suppress", "suppressed", "原始范围"),
    ],
)
def test_user_profile_commands_survive_rebuild_and_restart(
    tmp_path, action, expected_status, expected_value
):
    root = tmp_path / "memory"
    memory = AIMemory(root)
    _append_stable_free_evidence(memory, "原始范围")
    correction = "用户纠正范围" if action == "correct" else None

    memory.command_profile(
        "free",
        "subject:原始范围",
        action=action,
        value=correction,
    )

    memory.append_event("free", "free_exam", {
        "event_id": "after-user-command", "event_type": "result",
        "subject": "原始范围",
    }, day=date.today())
    memory.rebuild("free")
    restarted = AIMemory(root)
    page = restarted.list_profile("free", page_size=100)
    entry = next(item for item in page["items"] if item["key"] == "subject:原始范围")
    assert entry["status"] == expected_status
    assert entry["effective_value"] == expected_value
    assert entry["value"] == "原始范围"

    context = restarted.read_context(
        "free", "free_exam", {"subject": "原始范围"}
    )
    if action in {"reject", "suppress"}:
        assert "原始范围" not in context
    else:
        assert expected_value in context
        if action == "correct":
            assert "原始范围" not in context


def test_automatic_stable_profile_is_not_user_confirmed(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    _append_stable_free_evidence(memory, "自动稳定范围")

    page = memory.list_profile("free", page_size=100)
    entry = next(item for item in page["items"] if item["key"] == "subject:自动稳定范围")
    assert entry["status"] == "stable"
    assert entry["status"] != "confirmed"
    assert memory.list_profile("free", status="confirmed")["total"] == 0
    assert "稳定画像" in memory.read_context(
        "free", "free_exam", {"subject": "自动稳定范围"}
    )


def test_event_whitelist_drops_sensitive_payload_fields(tmp_path):
    memory = AIMemory(tmp_path / "memory")
    memory.append_event(
        "free",
        "free_exam",
        {
            "event_id": "whitelist-probe",
            "event_type": "result",
            "subject": "数学",
            "chapters": ["极限"],
            "prompt": "TOP_LEVEL_PROMPT_SENTINEL",
            "answer": "TOP_LEVEL_ANSWER_SENTINEL",
            "solution": "TOP_LEVEL_SOLUTION_SENTINEL",
            "secret": "TOP_LEVEL_SECRET_SENTINEL",
            "request": {
                "difficulty": "中等",
                "question_count": 1,
                "types": ["计算题"],
                "prompt": "REQUEST_PROMPT_SENTINEL",
                "answer": "REQUEST_ANSWER_SENTINEL",
            },
            "questions": [
                {
                    "question_id": "question-kept",
                    "fingerprint": "fingerprint-kept",
                    "source_number": "source-number-kept",
                    "content": "QUESTION_CONTENT_SENTINEL",
                    "answer": "QUESTION_ANSWER_SENTINEL",
                    "solution": "QUESTION_SOLUTION_SENTINEL",
                }
            ],
            "result": {
                "key_concept": "概念保留",
                "prompt": "RESULT_PROMPT_SENTINEL",
                "answer": "RESULT_ANSWER_SENTINEL",
                "solution": "RESULT_SOLUTION_SENTINEL",
            },
        },
        day=date.today(),
    )

    path = tmp_path / "memory" / "free" / "events" / "daily" / f"{date.today()}.jsonl"
    raw_text = path.read_text(encoding="utf-8")
    stored = json.loads(raw_text)
    assert stored["subject"] == "数学"
    assert stored["request"] == {
        "difficulty": "中等",
        "question_count": 1,
        "types": ["计算题"],
    }
    assert stored["questions"] == [
        {
            "question_id": "question-kept",
            "fingerprint": "fingerprint-kept",
            "source_number": "source-number-kept",
        }
    ]
    assert stored["result"] == {"key_concept": "概念保留"}
    derived_text = b"\n".join(_file_bytes(tmp_path / "memory").values()).decode("utf-8")
    for sentinel in (
        "TOP_LEVEL_PROMPT_SENTINEL",
        "TOP_LEVEL_ANSWER_SENTINEL",
        "TOP_LEVEL_SOLUTION_SENTINEL",
        "TOP_LEVEL_SECRET_SENTINEL",
        "REQUEST_PROMPT_SENTINEL",
        "REQUEST_ANSWER_SENTINEL",
        "QUESTION_CONTENT_SENTINEL",
        "QUESTION_ANSWER_SENTINEL",
        "QUESTION_SOLUTION_SENTINEL",
        "RESULT_PROMPT_SENTINEL",
        "RESULT_ANSWER_SENTINEL",
        "RESULT_SOLUTION_SENTINEL",
    ):
        assert sentinel not in raw_text
        assert sentinel not in derived_text
    assert "fingerprint-kept" in raw_text
    assert "概念保留" in raw_text


def _file_bytes(root):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and path.name != ".memory.lock"
    }


def test_attested_snapshot_is_read_only_and_corruption_is_not_repaired(tmp_path):
    root = tmp_path / "memory"
    memory = AIMemory(root)
    memory.append_event(
        "free",
        "free_exam",
        {"event_id": "snapshot-probe", "event_type": "result", "subject": "数学"},
        day=date.today(),
    )
    namespace = root / "free"
    before = _file_bytes(namespace)
    snapshot = memory.read_exam_inputs_snapshot(
        "free", "free_exam", {"subject": "数学"}
    )
    assert snapshot["context"]
    assert _file_bytes(namespace) == before

    profile = namespace / "profile.json"
    profile.write_text("{not-json", encoding="utf-8")
    with pytest.raises(MemoryAuthorityError) as error:
        memory.read_context_attested("free", "free_exam", {"subject": "数学"})
    assert error.value.reason == "ai_memory_profile_corrupt"
    assert profile.read_text(encoding="utf-8") == "{not-json"


def _authority_sources(mode: str):
    return {
        "mode": mode,
        "subject_id": "subject-1",
        "profile_identity_hash": "a" * 64 if mode == "past" else None,
        "execution_kind": "production_candidate",
    }


def test_mode_memory_authority_remains_read_only_and_non_writing():
    expected_sources = {
        "free": ["free", "mistake"],
        "mistakes": ["mistake"],
        "past": ["mistake", "past"],
    }
    for mode in ("free", "mistakes", "past"):
        access_sources = _authority_sources(mode)
        access = build_memory_access_authority(**access_sources)
        assert access["allowed_read_namespaces"] == expected_sources[mode]
        assert access["memory_may_change_hard_graph"] is False
        assert access["memory_may_override_explicit_user_settings"] is False
        assert access["memory_write_allowed"] is False

        snapshot_sources = {
            "access_authority": access,
            "access_sources": access_sources,
            "cutoff": {
                "kind": "event_watermark",
                "watermark": 0,
                "authority_hash": "b" * 64,
            },
            "memory_events": [],
            "snapshot_authority_ref": "boundary-test",
        }
        snapshot = build_memory_snapshot(**snapshot_sources)
        decision = build_memory_write_decision(
            memory_snapshot=snapshot,
            snapshot_sources=snapshot_sources,
            requested_fact_types=[],
            trigger="boundary_test",
            publication_authority=None,
            decision_authority_ref="boundary-test",
        )
        assert decision["memory_write_allowed"] is False
        assert decision["m_once_state_change_allowed"] is False

        tampered = dict(access)
        tampered["memory_write_allowed"] = True
        with pytest.raises(MemoryAuthorityError):
            validate_memory_access_authority(tampered, **access_sources)
