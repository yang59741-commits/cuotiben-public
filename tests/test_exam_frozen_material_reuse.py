import json
import sqlite3
from unittest.mock import Mock

import pytest

from cuoti_app.services.exam_material import understand_frozen_material
from cuoti_app.services.semantic_index import semantic_content_hash


def _case(prompt_version):
    candidate = {
        "candidate_id": "QB_TEST", "source_kind": "ordinary",
        "content": "求矩阵的特征值。", "chapter": "相似标准型", "source_file": "fixture.tex",
    }
    card = {
        "candidate_id": "QB_TEST", "question_type": "计算题", "difficulty": "中等",
        "discrimination": "中等", "analysis_confidence": 0.8, "similarity_group": "特征值",
        **{key: ["特征值"] for key in (
            "knowledge_points", "characteristics", "prerequisites", "common_traps",
            "methods", "semantic_keywords", "source_constraints",
        )},
    }
    digest = semantic_content_hash(candidate["content"])
    decision = {
        "candidate_id": "QB_TEST", "source_kind": "ordinary", "coverage_group": "A",
        "coverage_reason": "valid_current_card", "card_version_id": 1,
        "content_hash": digest, "card_content_hash": digest,
    }
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE semantic_questions(id INTEGER PRIMARY KEY,source_kind TEXT,candidate_id TEXT);"
        "CREATE TABLE semantic_card_versions(id INTEGER PRIMARY KEY,semantic_question_id INTEGER,"
        "content_hash TEXT,analysis_schema_version INTEGER,prompt_version TEXT,card_json TEXT,status TEXT);"
        "INSERT INTO semantic_questions VALUES(1,'ordinary','QB_TEST');"
    )
    db.execute("INSERT INTO semantic_card_versions VALUES(1,1,?,1,?,?,?)",
               (digest, prompt_version, json.dumps(card, ensure_ascii=False), "valid"))
    db.commit()
    return db, candidate, decision


@pytest.mark.parametrize("prompt_version", ["material-cards-v3", "material-cards-v4", "exam68-source-boundary-subagent-review-v1"])
def test_valid_frozen_card_does_not_require_prompt_version_reanalysis(prompt_version):
    db, candidate, decision = _case(prompt_version)
    deep = Mock(side_effect=AssertionError("valid frozen card must not call provider"))
    result = understand_frozen_material(
        db, [candidate], [decision], snapshot="fixture", kind="question_bank",
        generation_job_id="job", deep_reader=deep,
    )
    assert result["complete"] is True
    assert result["permanent_card_reused_count"] == 1
    assert result["legacy_deep_read_count"] == 0
    assert result["cards"][0]["candidate_id"] == "QB_TEST"
    assert result["summary"]["global"]["candidate_count"] == 1
    deep.assert_not_called()
    db.close()


@pytest.mark.parametrize("damage", ["status", "schema", "content"])
def test_reuse_still_requires_valid_schema_status_and_exact_content(damage):
    db, candidate, decision = _case("material-cards-v3")
    if damage == "content":
        candidate["content"] += "另求特征向量。"
    else:
        column, value = ("status", "invalid") if damage == "status" else ("analysis_schema_version", 99)
        db.execute(f"UPDATE semantic_card_versions SET {column}=?", (value,))
        db.commit()
    deep = Mock(return_value={"cards": [], "complete": False})
    result = understand_frozen_material(
        db, [candidate], [decision], snapshot="fixture", kind="question_bank",
        generation_job_id="job", deep_reader=deep,
    )
    assert result["complete"] is False
    assert result["permanent_card_reused_count"] == 0
    deep.assert_called_once()
    db.close()
