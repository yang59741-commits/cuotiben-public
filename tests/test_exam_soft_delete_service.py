from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from cuoti_app import database
from cuoti_app.migrations import MIGRATIONS, migration_schema_issues
from cuoti_app.migrations.v56_exam_soft_delete import (
    EXAM_SOFT_DELETE_INDEX,
    migrate_v55_to_v56,
    schema_issues,
)
from cuoti_app.migrations.v57_question_review_markers import (
    migrate_v56_to_v57,
    schema_issues as review_marker_schema_issues,
)
from cuoti_app.services import exam_artifacts
from cuoti_app.services.exam_query import (
    ExamListFilters,
    ExamRecompileConflict,
    ExamRecompileNotFound,
    delete_published_exam,
    get_exam_detail,
    list_deleted_exams,
    list_exams,
    published_exam_exists,
    recompile_published_exam,
    restore_published_exam,
)


def _schema(db):
    db.executescript(
        """
        CREATE TABLE subjects(id INTEGER PRIMARY KEY,name TEXT);
        CREATE TABLE app_revisions(
            id INTEGER PRIMARY KEY,
            learning_revision INTEGER NOT NULL,
            statistics_revision INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE exams(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id INTEGER,
            title TEXT,
            config TEXT,
            content TEXT,
            created_at TEXT,
            generation_job_id TEXT,
            artifact_status TEXT,
            latex_source TEXT,
            latex_sha256 TEXT,
            pdf_sha256 TEXT,
            artifact_revision INTEGER,
            recompile_available INTEGER,
            last_recompile_job_id TEXT,
            scoring_mode TEXT,
            total_points TEXT,
            deleted_at TEXT
        );
        CREATE TABLE exam_items(
            id INTEGER PRIMARY KEY,
            public_id TEXT,
            exam_id INTEGER,
            position INTEGER,
            question_type TEXT,
            difficulty TEXT,
            content TEXT,
            content_hash TEXT,
            points TEXT,
            points_canonical TEXT,
            points_evidence_hash TEXT,
            source_kind TEXT,
            source_mistake_public_id TEXT,
            source_candidate_id TEXT,
            source_metadata_json TEXT
        );
        CREATE TABLE exam_item_mistake_lineage(
            exam_item_id INTEGER,mistake_public_id TEXT,source_lineage_hash TEXT,
            lineage_policy_version TEXT,content_fingerprint TEXT,source_order INTEGER
        );
        CREATE TABLE exam_attempts(id INTEGER PRIMARY KEY,exam_id INTEGER);
        CREATE TABLE exam_generation_v3_candidate_usage(id INTEGER PRIMARY KEY,exam_id INTEGER);
        CREATE TABLE exam_generation_v3_publications(id INTEGER PRIMARY KEY,exam_id INTEGER);
        CREATE TABLE exam_generation_memory_event_outbox_v1(id INTEGER PRIMARY KEY,exam_id INTEGER);
        CREATE TABLE exam_generation_priority_once_consumptions(id INTEGER PRIMARY KEY,exam_id INTEGER);
        """
    )
    db.execute("INSERT INTO subjects VALUES(1,'数学')")
    db.execute("INSERT INTO app_revisions VALUES(1,0,0,'t0')")
    migrate_v55_to_v56(db)


def _open(path=None):
    db = sqlite3.connect(":memory:" if path is None else path)
    db.row_factory = sqlite3.Row
    _schema(db)
    return db


def _insert_exam(db, *, title="保留试卷", created_at="2026-09-06T00:00:00+00:00"):
    source = r"\documentclass{article}\begin{document}题目\end{document}"
    old_pdf = b"%PDF-1.7\nold"
    cursor = db.execute(
        "INSERT INTO exams(subject_id,title,config,content,created_at,generation_job_id,"
        "artifact_status,latex_source,latex_sha256,pdf_sha256,artifact_revision,"
        "recompile_available,last_recompile_job_id,scoring_mode,total_points,deleted_at) "
        "VALUES(1,?,'{\"source\":\"past\",\"question_count\":1}','题目',?,'',"
        "'published',?,?,?,?,1,NULL,'unscored',NULL,NULL)",
        (
            title,
            created_at,
            source,
            hashlib.sha256(source.encode()).hexdigest(),
            hashlib.sha256(old_pdf).hexdigest(),
            1,
        ),
    )
    exam_id = int(cursor.lastrowid)
    db.execute(
        "INSERT INTO exam_items VALUES(1,'item-1',?,1,'证明题','中等','题目',?,"
        "NULL,NULL,NULL,'ordinary',NULL,'candidate-1','{}')",
        (exam_id, "a" * 64),
    )
    for table in (
        "exam_attempts",
        "exam_generation_v3_candidate_usage",
        "exam_generation_v3_publications",
        "exam_generation_memory_event_outbox_v1",
        "exam_generation_priority_once_consumptions",
    ):
        db.execute(f"INSERT INTO {table} VALUES(1,?)", (exam_id,))
    db.commit()
    return exam_id, source, old_pdf


def test_v56_migration_is_additive_and_nullable():
    db = sqlite3.connect(":memory:")
    db.execute(
        "CREATE TABLE exams(id INTEGER PRIMARY KEY,title TEXT,artifact_status TEXT)"
    )
    db.execute(
        "CREATE TABLE app_revisions(id INTEGER PRIMARY KEY,learning_revision INTEGER,"
        "statistics_revision INTEGER,updated_at TEXT)"
    )
    db.execute("INSERT INTO app_revisions VALUES(1,0,0,'t0')")
    db.execute("INSERT INTO exams VALUES(7,'既有试卷','published')")
    migrate_v55_to_v56(db)
    assert schema_issues(db) == []
    assert db.execute("SELECT id,title,artifact_status,deleted_at FROM exams").fetchall() == [
        (7, "既有试卷", "published", None)
    ]
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (EXAM_SOFT_DELETE_INDEX,),
    ).fetchone()


def test_empty_database_initialization_runs_registered_v57(tmp_path):
    assert MIGRATIONS[55] == (56, migrate_v55_to_v56)
    assert MIGRATIONS[56] == (57, migrate_v56_to_v57)
    path = tmp_path / "empty-v57.db"
    app = Flask(__name__)
    app.config["DATABASE"] = str(path)
    with app.app_context():
        database.init_db()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == database.DB_SCHEMA_VERSION
        assert schema_issues(db) == []
        assert review_marker_schema_issues(db) == []
        assert migration_schema_issues(db, database.DB_SCHEMA_VERSION) == []


def test_soft_delete_retains_files_items_attempts_and_history_then_restores(tmp_path):
    db = _open()
    exam_id, source, old_pdf = _insert_exam(db)
    tex = tmp_path / f"exam_{exam_id}.tex"
    pdf = tmp_path / f"exam_{exam_id}.pdf"
    tex.write_text(source, encoding="utf-8")
    pdf.write_bytes(old_pdf)

    class UntouchedMemory:
        def __getattr__(self, _name):
            raise AssertionError("soft delete must not touch memory")

    assert delete_published_exam(
        db,
        tmp_path,
        exam_id,
        memory=UntouchedMemory(),
        deleted_at="2026-09-07T00:00:00+00:00",
    )
    assert not delete_published_exam(db, tmp_path, exam_id)
    assert db.execute(
        "SELECT statistics_revision FROM app_revisions WHERE id=1"
    ).fetchone()[0] == 1
    row = db.execute("SELECT id,artifact_status,deleted_at FROM exams").fetchone()
    assert tuple(row) == (exam_id, "published", "2026-09-07T00:00:00+00:00")
    assert tex.read_text(encoding="utf-8") == source
    assert pdf.read_bytes() == old_pdf
    assert db.execute("SELECT COUNT(*) FROM exam_items").fetchone()[0] == 1
    for table in (
        "exam_attempts",
        "exam_generation_v3_candidate_usage",
        "exam_generation_v3_publications",
        "exam_generation_memory_event_outbox_v1",
        "exam_generation_priority_once_consumptions",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    assert list_exams(db, ExamListFilters(), limit=10, after=None)["items"] == []
    assert get_exam_detail(db, exam_id) is None
    assert not published_exam_exists(db, exam_id)
    with pytest.raises(ExamRecompileNotFound):
        recompile_published_exam(
            db, pdf_dir=tmp_path, command="xelatex", exam_id=exam_id, job_id="job"
        )

    trash = list_deleted_exams(db, ExamListFilters(), limit=10, after=None)
    assert trash["total"] == 1
    assert trash["items"][0]["id"] == exam_id
    assert trash["items"][0]["deleted_at"] == "2026-09-07T00:00:00+00:00"
    assert restore_published_exam(db, exam_id)
    assert not restore_published_exam(db, exam_id)
    assert db.execute(
        "SELECT statistics_revision FROM app_revisions WHERE id=1"
    ).fetchone()[0] == 2
    assert published_exam_exists(db, exam_id)
    assert get_exam_detail(db, exam_id)["id"] == exam_id
    assert tex.read_text(encoding="utf-8") == source
    assert pdf.read_bytes() == old_pdf


def test_trash_metadata_cursor_is_separate_and_filter_bound():
    db = _open()
    ids = []
    for index in range(3):
        exam_id, _, _ = _insert_exam(
            db,
            title=f"归档-{index}",
            created_at=f"2026-09-0{index + 1}T00:00:00+00:00",
        )
        # The compact fixture has one fixed related-row identity per fresh exam.
        if index < 2:
            for table in (
                "exam_items", "exam_attempts", "exam_generation_v3_candidate_usage",
                "exam_generation_v3_publications", "exam_generation_memory_event_outbox_v1",
                "exam_generation_priority_once_consumptions",
            ):
                db.execute(f"DELETE FROM {table}")
            db.commit()
        delete_published_exam(db, None, exam_id, deleted_at=f"2026-09-1{index}T00:00:00+00:00")
        ids.append(exam_id)
    first = list_deleted_exams(db, ExamListFilters(title="归档"), limit=2, after=None)
    assert [item["id"] for item in first["items"]] == list(reversed(ids[-2:]))
    assert first["has_more"] is True and first["total"] == 3
    second = list_deleted_exams(
        db, ExamListFilters(title="归档"), limit=2, after=first["next_cursor"]
    )
    assert [item["id"] for item in second["items"]] == [ids[0]]
    assert second["has_more"] is False


def test_delete_during_recompile_cannot_reactivate_or_replace_old_files(tmp_path, monkeypatch):
    path = tmp_path / "race.db"
    owner = _open(path)
    exam_id, source, old_pdf = _insert_exam(owner)
    tex = tmp_path / f"exam_{exam_id}.tex"
    pdf = tmp_path / f"exam_{exam_id}.pdf"
    tex.write_text("old tex", encoding="utf-8")
    pdf.write_bytes(old_pdf)
    deleter = sqlite3.connect(path)
    deleter.row_factory = sqlite3.Row

    def fake_compile(directory, _command, stem, latex, **_kwargs):
        Path(directory, stem + ".tex").write_text(latex, encoding="utf-8")
        Path(directory, stem + ".pdf").write_bytes(b"%PDF-1.7\nnew")

    monkeypatch.setattr(exam_artifacts, "compile_latex_artifact", fake_compile)

    def delete_after_compile():
        assert delete_published_exam(
            deleter,
            tmp_path,
            exam_id,
            deleted_at="2026-09-08T00:00:00+00:00",
        )

    with pytest.raises(ExamRecompileConflict):
        recompile_published_exam(
            owner,
            pdf_dir=tmp_path,
            command="xelatex",
            exam_id=exam_id,
            job_id="recompile-race",
            on_compiled=delete_after_compile,
        )
    row = owner.execute(
        "SELECT deleted_at,artifact_revision,last_recompile_job_id FROM exams WHERE id=?",
        (exam_id,),
    ).fetchone()
    assert tuple(row) == ("2026-09-08T00:00:00+00:00", 1, None)
    assert tex.read_text(encoding="utf-8") == "old tex"
    assert pdf.read_bytes() == old_pdf
