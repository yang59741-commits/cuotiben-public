from __future__ import annotations

from contextlib import closing
import sqlite3

import pytest

from cuoti_app.migrations.v52_exam_generation_soft_inputs import migrate_v51_to_v52
from cuoti_app.services.exam_generation_mode_memory_authority import MemoryAuthorityError
from cuoti_app.services.exam_generation_soft_inputs import SOFT_INPUTS
from cuoti_app.services.exam_generation_source_context import (
    prepare_mistakes_source_context,
    read_optional_exam_memory_context,
    read_optional_recent_question_fingerprints,
    read_optional_recent_question_ids,
    task_frozen_mode_memory,
)
from cuoti_app.services.exam_prompting import build_structured_mistakes_prompts


class _Memory:
    def __init__(self, reason: str | None):
        self.reason = reason

    def read_context(self, *_args, **_kwargs):
        if self.reason is not None:
            raise MemoryAuthorityError(self.reason)
        return "verified context"

    def recent_question_ids(self, *_args, **_kwargs):
        if self.reason is not None:
            raise MemoryAuthorityError(self.reason)
        return {"candidate-1"}

    def recent_question_fingerprints(self, *_args, **_kwargs):
        if self.reason is not None:
            raise MemoryAuthorityError(self.reason)
        return {"fingerprint-1"}


class _CountingMemory(_Memory):
    def __init__(self, reason: str | None):
        super().__init__(reason)
        self.context_calls = []

    def read_context(self, namespace, actor, filters):
        self.context_calls.append((namespace, actor, dict(filters)))
        return super().read_context(namespace, actor, filters)


class _MutableModeMemory:
    def __init__(self, corrupt=()):
        self.revision = 1
        self.corrupt = set(corrupt)
        self.calls = []
        self.writes = []

    def _value(self, namespace, operation):
        self.calls.append((namespace, operation, self.revision))
        if namespace in self.corrupt:
            raise MemoryAuthorityError("ai_memory_event_identity_corrupt")

    def read_context(self, namespace, actor, filters):
        self._value(namespace, "context")
        return f"{namespace}-context-r{self.revision}"

    def recent_question_ids(self, namespace, actor):
        self._value(namespace, "ids")
        return {f"{namespace}-id-r{self.revision}"}

    def recent_question_fingerprints(self, namespace, actor):
        self._value(namespace, "fingerprints")
        return {f"{namespace}-fingerprint-r{self.revision}"}

    def append_many(self, memory_type, actor, events):
        self.writes.append((memory_type, actor, list(events)))
        return len(events)


class _SnapshotLockFailureMemory(_MutableModeMemory):
    def read_exam_inputs_snapshot(self, *_args, **_kwargs):
        raise MemoryAuthorityError("ai_memory_snapshot_unavailable")


@pytest.fixture
def mistakes_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE subjects(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        CREATE TABLE chapters(
            id INTEGER PRIMARY KEY,
            subject_id INTEGER NOT NULL,
            name TEXT NOT NULL
        );
        CREATE TABLE mistakes(
            id INTEGER PRIMARY KEY,
            public_id TEXT NOT NULL,
            subject_id INTEGER NOT NULL,
            chapter_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            status TEXT,
            deleted_at TEXT
        );
        INSERT INTO subjects(id,name) VALUES(1,'数学');
        INSERT INTO chapters(id,subject_id,name) VALUES
            (1,1,'极限'),(2,1,'导数');
        INSERT INTO mistakes(
            id,public_id,subject_id,chapter_id,content,status,deleted_at
        ) VALUES
            (1,'mistake-1',1,1,'x 趋近 0 时求极限','未掌握',NULL),
            (2,'mistake-2',1,2,'求 x^2 的导数','未掌握',NULL);
        """
    )
    try:
        yield db
    finally:
        db.close()


def _prepare_mistakes(db, memory):
    return prepare_mistakes_source_context(
        {
            "source": "mistakes",
            "mistake_ids": [2, 1],
            "question_count": 2,
            "types": ["计算题"],
        },
        db=db,
        memory=memory,
        chapter_sources={},
        tex_base="unused",
        logger=None,
        selection_seed="fixed",
    )


def _assert_unique_mistake_lineage(context):
    assert [slot.position for slot in context.lineage_slots] == [1, 2]
    source_ids = [slot.source_mistake_ids for slot in context.lineage_slots]
    assert sorted(source_ids) == [("mistake-1",), ("mistake-2",)]
    assert len({slot.source_lineage_hash for slot in context.lineage_slots}) == 2


def test_absent_optional_namespace_is_omitted_without_initialization():
    assert read_optional_exam_memory_context(
        _Memory("ai_memory_namespace_unavailable"),
        "free",
        "free_exam",
        {"subject": "数学"},
    ) == ""
    assert read_optional_recent_question_ids(
        _Memory("ai_memory_namespace_unavailable"), "free", "free_exam"
    ) == set()
    assert read_optional_recent_question_fingerprints(
        _Memory("ai_memory_namespace_unavailable"), "past", "past_exam"
    ) == set()


@pytest.mark.parametrize(
    "reason",
    [
        "ai_memory_manifest_unavailable",
        "ai_memory_manifest_corrupt",
        "ai_memory_profile_unavailable",
        "ai_memory_profile_authority_corrupt",
        "ai_memory_event_identity_corrupt",
    ],
)
def test_incomplete_or_corrupt_optional_memory_is_omitted_as_non_evidence(reason):
    memory = _Memory(reason)
    assert read_optional_exam_memory_context(
        memory, "mistakes", "mistake_exam", {"subject": "数学"}
    ) == ""
    assert read_optional_recent_question_ids(
        memory, "past", "past_exam"
    ) == set()
    assert read_optional_recent_question_fingerprints(
        memory, "past", "past_exam"
    ) == set()


def test_non_memory_authority_failure_is_not_degraded():
    with pytest.raises(MemoryAuthorityError, match="memory_authority_policy_invalid"):
        read_optional_exam_memory_context(
            _Memory("memory_authority_policy_invalid"),
            "mistakes",
            "mistake_exam",
            {"subject": "数学"},
        )


def test_structured_mistake_prompt_carries_optional_memory_without_changing_lineage():
    _, prompt = build_structured_mistakes_prompts(
        subject="数学",
        lineage_slots=[{
            "position": 1,
            "target_chapter": "极限",
            "target_question_type": "计算题",
            "source_mistake_ids": ["mistake-1"],
            "source_lineage_hash": "a" * 64,
        }],
        frozen_mistakes=[{"public_id": "mistake-1", "content": "x+1"}],
        difficulty_quota={"中等": 1},
        memory_context="优先练习极限计算",
    )
    assert '"optional_memory_context":"优先练习极限计算"' in prompt
    assert '"source_mistake_ids":["mistake-1"]' in prompt


def test_mistakes_source_context_reads_memory_once_and_passes_it_to_prompt(mistakes_db):
    memory = _CountingMemory(None)
    outcome = _prepare_mistakes(mistakes_db, memory)

    assert outcome.failure is None
    context = outcome.context
    assert context is not None
    assert context.memory_context == "verified context"
    assert '"optional_memory_context":"verified context"' in context.prompt
    assert memory.context_calls == [(
        "mistakes",
        "mistake_exam",
        {
            "task": "exam_generation",
            "subject": "数学",
            "chapters": ["极限", "导数"],
        },
    )]
    _assert_unique_mistake_lineage(context)


def test_corrupt_mistakes_memory_is_omitted_without_losing_lineage(mistakes_db):
    memory = _CountingMemory("ai_memory_manifest_corrupt")
    outcome = _prepare_mistakes(mistakes_db, memory)

    assert outcome.failure is None
    context = outcome.context
    assert context is not None
    assert context.memory_context == ""
    assert '"optional_memory_context":""' in context.prompt
    assert len(memory.context_calls) == 1
    _assert_unique_mistake_lineage(context)


@pytest.mark.parametrize(
    ("mode", "actor", "expected_sources"),
    [
        ("free", "free_exam", {"free", "mistakes"}),
        ("mistakes", "mistake_exam", {"mistakes"}),
        ("past", "past_exam", {"past", "mistakes"}),
    ],
)
def test_task_memory_freezes_entire_mode_matrix_on_first_read(
    mode, actor, expected_sources
):
    memory = _MutableModeMemory()
    frozen = task_frozen_mode_memory(
        memory, db=None, mode=mode, job_id=None
    )
    filters = {"task": "exam_generation", "subject": "数学"}

    primary = next(iter(expected_sources - {"mistakes"}), "mistakes")
    assert frozen.read_context(primary, actor, filters) == f"{primary}-context-r1"
    assert {namespace for namespace, _, _ in memory.calls} == expected_sources
    assert len(memory.calls) == len(expected_sources) * 3

    memory.revision = 2
    assert frozen.recent_question_ids(primary, actor) == {f"{primary}-id-r1"}
    assert frozen.recent_question_fingerprints(primary, actor) == {
        f"{primary}-fingerprint-r1"
    }
    assert len(memory.calls) == len(expected_sources) * 3


def test_corrupt_optional_source_is_omitted_without_losing_other_source():
    memory = _MutableModeMemory(corrupt={"mistakes"})
    frozen = task_frozen_mode_memory(
        memory, db=None, mode="free", job_id=None
    )
    filters = {"task": "exam_generation", "subject": "数学"}

    assert frozen.read_context("free", "free_exam", filters) == "free-context-r1"
    assert frozen.read_context("mistakes", "free_exam", filters) == ""
    assert frozen.recent_question_ids("mistakes", "free_exam") == set()
    assert frozen.recent_question_ids("free", "free_exam") == {"free-id-r1"}


def test_snapshot_lock_failure_is_omitted_as_optional_memory():
    frozen = task_frozen_mode_memory(
        _SnapshotLockFailureMemory(), db=None, mode="mistakes", job_id=None
    )
    assert frozen.read_context(
        "mistakes",
        "mistake_exam",
        {"task": "exam_generation", "subject": "数学", "chapters": ["极限"]},
    ) == ""


def test_task_memory_rejects_cross_mode_read_but_delegates_existing_writer():
    memory = _MutableModeMemory()
    frozen = task_frozen_mode_memory(
        memory, db=None, mode="mistakes", job_id=None
    )

    with pytest.raises(MemoryAuthorityError, match="read_forbidden"):
        frozen.read_context(
            "free",
            "mistake_exam",
            {"task": "exam_generation", "subject": "数学"},
        )

    assert frozen.append_many("mistakes", "mistake_exam", [{"event": 1}]) == 1
    assert memory.writes == [("mistakes", "mistake_exam", [{"event": 1}])]


def test_task_memory_omits_filter_drift_without_live_reread():
    memory = _MutableModeMemory()
    frozen = task_frozen_mode_memory(memory, db=None, mode="past", job_id=None)
    frozen.read_context(
        "past", "past_exam", {"task": "exam_generation", "subject": "数学"}
    )

    assert frozen.read_context(
        "mistakes",
        "past_exam",
        {"task": "exam_generation", "subject": "物理"},
    ) == ""
    assert len(memory.calls) == 6


def _soft_input_db(path):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE IF NOT EXISTS background_jobs(id TEXT PRIMARY KEY)")
    db.execute("INSERT OR IGNORE INTO background_jobs(id) VALUES('exam-job')")
    migrate_v51_to_v52(db)
    db.commit()
    return db


def test_background_task_memory_snapshot_survives_retry_and_process_restart(tmp_path):
    path = tmp_path / "jobs.db"
    memory = _MutableModeMemory()
    filters = {"task": "exam_generation", "subject": "数学"}
    with closing(_soft_input_db(path)) as db:
        first = task_frozen_mode_memory(
            memory, db=db, mode="past", job_id="exam-job"
        )
        assert first.read_context("past", "past_exam", filters) == "past-context-r1"
    assert len(memory.calls) == 6

    memory.revision = 2
    with closing(_soft_input_db(path)) as db:
        retried = task_frozen_mode_memory(
            memory, db=db, mode="past", job_id="exam-job"
        )
        assert retried.read_context("past", "past_exam", filters) == "past-context-r1"
        assert retried.recent_question_ids("past", "past_exam") == {"past-id-r1"}
    assert len(memory.calls) == 6


def test_damaged_background_memory_snapshot_is_omitted_without_live_refresh(tmp_path):
    path = tmp_path / "jobs.db"
    memory = _MutableModeMemory()
    filters = {"task": "exam_generation", "subject": "数学"}
    with closing(_soft_input_db(path)) as db:
        frozen = task_frozen_mode_memory(
            memory, db=db, mode="free", job_id="exam-job"
        )
        assert frozen.read_context("free", "free_exam", filters) == "free-context-r1"
        db.execute(
            f"UPDATE {SOFT_INPUTS} SET payload_json='{{\"unexpected\":true}}' "
            "WHERE job_id='exam-job' AND input_key='mode_memory'"
        )
        db.commit()
    memory.revision = 2
    calls_before = len(memory.calls)
    with closing(_soft_input_db(path)) as db:
        retried = task_frozen_mode_memory(
            memory, db=db, mode="free", job_id="exam-job"
        )
        assert retried.read_context("free", "free_exam", filters) == ""
        assert retried.recent_question_ids("free", "free_exam") == set()
    assert len(memory.calls) == calls_before
