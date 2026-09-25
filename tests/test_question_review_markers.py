from __future__ import annotations

import sqlite3

from cuoti_app.migrations.v57_question_review_markers import (
    QUESTION_REVIEW_MARKERS_INDEX,
    QUESTION_REVIEW_MARKERS_TABLE,
    migrate_v56_to_v57,
    schema_issues,
)
from cuoti_app.services.question_review_markers import (
    attach_review_markers,
    marker_for,
)
from cuoti_app.services.exam_generation_drafts import _inject_review_marker_notes


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    migrate_v56_to_v57(db)
    return db


def test_marker_schema_is_additive_and_indexed():
    db = _db()
    assert schema_issues(db) == []
    assert db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?",
        (QUESTION_REVIEW_MARKERS_INDEX,),
    ).fetchone()
    db.close()


def test_markers_are_display_metadata_for_exact_identities():
    db = _db()
    db.execute(
        f"""INSERT INTO {QUESTION_REVIEW_MARKERS_TABLE}
        (source_kind,candidate_id,marker_kind,marker_scope,message,
         source_sequence,active,created_at,updated_at)
        VALUES('ordinary','candidate-1','suspect','logic','需要人工核对',1,1,'t','t')"""
    )
    db.commit()
    marker = marker_for(db, source_kind="ordinary", candidate_id="candidate-1")
    assert marker == {
        "schema_version": "question-review-marker-v1",
        "kind": "suspect",
        "scope": "logic",
        "label": "题面提示：嫌疑题",
        "message": "需要人工核对",
    }
    items = attach_review_markers(
        db,
        [
            {
                "position": 1,
                "source_kind": "ordinary",
                "candidate_id": "candidate-1",
            },
            {
                "position": 2,
                "source_kind": "ordinary",
                "candidate_id": "candidate-2",
            },
        ],
    )
    assert items[0]["review_markers"] == [marker]
    assert "review_markers" not in items[1]
    db.close()


def test_past_exam_aliases_resolve_and_legacy_latex_notice_is_positioned():
    db = _db()
    db.execute(
        f"""INSERT INTO {QUESTION_REVIEW_MARKERS_TABLE}
        (source_kind,candidate_id,marker_kind,marker_scope,message,
         source_sequence,active,created_at,updated_at)
        VALUES('past_exam_local','past-1','uncertain','structure','需核对题面',2,1,'t','t')"""
    )
    db.commit()
    assert marker_for(db, source_kind="past_exam", candidate_id="past-1")["kind"] == "uncertain"
    body = r"\begin{problem}第一题\end{problem}\begin{problem}第二题\end{problem}"
    rendered = _inject_review_marker_notes(
        body,
        [{
            "position": 1,
            "review_markers": [{"label": "题面提示：不确定题"}],
        }],
    )
    assert rendered.count(r"\textbf{题面提示：不确定题}") == 1
    assert rendered.endswith(r"\begin{problem}第二题\end{problem}")
    assert _inject_review_marker_notes(body, [{"position": 1}]) == body
    db.close()
