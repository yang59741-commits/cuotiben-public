import copy
import json

from cuoti_app.services.exam_generation_mistake_relations import (
    exact_relation_factor, freeze_mistake_relations, relation_index, relation_content_fingerprint,
)
from cuoti_app.services.exam_generation_d2_runtime import local_factors, release_objective_policy
from cuoti_app.services.exam_generation_stage5_candidate_base_runner import build_candidate_stage5_d2_base
from cuoti_app.services.exam_generation_stage5_d2_solver import solve_stage5_d2_selection
from cuoti_app.services.exam_generation_stage6_local_factor_contract import project_local_factor_receipts_for_d2
from tests.test_exam_generation_stage6_d2_repository import _case, _digest
from tests.test_exam_soft_inputs import make_db


def relation_db():
    db = make_db()
    db.execute("CREATE TABLE mistakes(public_id TEXT,content TEXT,subject_id INTEGER,deleted_at TEXT)")
    db.executemany("INSERT INTO mistakes VALUES(?,?,?,?)", [
        ("mistake-1", r"\begin{problem}求 $x+1=2$ 的解。\end{problem}", 1, None),
        ("wrong-subject", "other", 2, None),
        ("deleted", "other", 1, "2026-09-05"),
    ])
    db.commit()
    return db


def candidate(content, **extra):
    return {"content_snapshot": json.dumps({"content": content, "conditions": [],
            "options": [], "figure_references": [], **extra})}


def test_real_relation_is_traceable_and_ignores_mastery():
    with relation_db() as db:
        snapshot = freeze_mistake_relations(db, job_id="job", subject_id=1)
        index = relation_index(snapshot)
        factor = exact_relation_factor(candidate("求 $x+1=2$ 的解。"), index)
        assert factor["level"] == "strong"
        assert "mistake_public_id:mistake-1" in factor["evidence_codes"]
        assert exact_relation_factor(candidate("other"), index)["level"] == "none"
        assert exact_relation_factor(candidate("求 $x+1=2$ 的解。", options=["A"]), index)["level"] == "not_applicable"


def test_first_relation_read_survives_live_edits_and_missing_evidence_is_omitted():
    with relation_db() as db:
        first = freeze_mistake_relations(db, job_id="job", subject_id=1)
        db.execute("UPDATE mistakes SET content='changed' WHERE public_id='mistake-1'")
        db.commit()
        assert freeze_mistake_relations(db, job_id="job", subject_id=1) == first
        next_input = freeze_mistake_relations(db, job_id="next", subject_id=1)
        assert exact_relation_factor(candidate("changed"), relation_index(next_input))["level"] == "strong"
        invalid = copy.deepcopy(first)
        invalid["payload"]["records"][0]["public_id"] = ""
        assert exact_relation_factor(candidate("changed"), relation_index(invalid))["level"] == "not_applicable"
        assert exact_relation_factor(candidate("changed"), None)["level"] == "not_applicable"


def test_verified_real_source_pair_only_normalizes_balanced_inline_display():
    mistake = "证明：n级实矩阵A正交相似于一个上三角矩阵的充分必要条件是：A的特征多项式在复数域中的根都是实数。"
    source = r"证明：\(n\) 级实矩阵 \(A\) 正交相似于一个上三角矩阵的充分必要条件是：\(A\) 的特征多项式在复数域中的根都是实数。"
    assert relation_content_fingerprint(mistake) == relation_content_fingerprint(source)
    assert relation_content_fingerprint(source + "另证A正定。") != relation_content_fingerprint(mistake)
    assert relation_content_fingerprint(r"\(A") != relation_content_fingerprint("A")


def test_verified_relation_changes_actual_solver_choice_not_hard_constraints():
    old, *_ = _case()
    stage4 = copy.deepcopy(old.stage4)
    first = stage4["candidates"][0]
    first.update(candidate("unrelated"))
    second = {**copy.deepcopy(first), "candidate_id": "candidate-2", "content_hash": "e" * 64,
              **candidate("求 $x+1=2$ 的解。")}
    stage4["candidates"].append(second)
    constraint = old.constraint.to_dict()
    constraint["chapter_weight_fact"]["fact_hash"] = _digest(constraint["chapter_weight_fact"])
    base = build_candidate_stage5_d2_base(
        old.stage3, stage4, stage3_contract_hash=old.constraint.stage3_contract_hash,
        candidate_facts_hash=_digest(stage4), projection_identity_hash=old.constraint.projection_identity_hash,
        **{key: constraint[key] for key in ("chapter_weight_fact", "recent_usage", "diversity_policy",
                                           "coverage_policy", "uniqueness_policy", "policy_fact_set_hash")},
    )
    policy = release_objective_policy()
    baseline_factors = local_factors(base, policy)
    baseline = solve_stage5_d2_selection(base.constraint, base.graph, objective_policy=policy,
        factor_receipts=project_local_factor_receipts_for_d2(baseline_factors, policy=policy))
    with relation_db() as db:
        frozen = freeze_mistake_relations(db, job_id="job", subject_id=1)
    factors = local_factors(base, policy, mistake_relations=frozen)
    result = solve_stage5_d2_selection(base.constraint, base.graph, objective_policy=policy,
        factor_receipts=project_local_factor_receipts_for_d2(factors, policy=policy))
    assert result.assignments[0]["candidate_id"] == "candidate-2"
    assert baseline.assignments[0]["candidate_id"] == "candidate-1"
    assert len(base.graph.to_dict()["edges"]) == 2
