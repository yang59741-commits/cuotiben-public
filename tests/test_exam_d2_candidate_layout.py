import json

import pytest

from cuoti_app.services import exam_generation_d2_publication as publication
from tests.test_exam_d2_publication import _artifact, _db, _digest, _fixture, _quality
from tests.test_exam_paper_layout import _direct_product_question


def test_d2_draft_passes_the_frozen_candidate_id_to_paper_layout(monkeypatch):
    state, facts, contract = _fixture()
    fact = facts.candidates[0]
    snapshot = json.loads(fact["content_snapshot"])
    assignment = dict(state.selection.assignments[0])
    assignment["candidate_id"] = "QB2C9BE69EFFDCAE915841983C"
    selected = [{
        "position": 1,
        "assignment": assignment,
        "candidate_fact": fact,
        "content_snapshot": snapshot,
    }]
    quality = {
        "selection_hash": state.selection.selection_hash,
        "pre_adaptation_hash": "b" * 64,
        "items": [{
            "position": 1,
            "candidate_id": assignment["candidate_id"],
            "content_snapshot": snapshot,
        }],
    }
    observed = []

    def render(content, *, candidate_id=None):
        observed.append((content, candidate_id))
        return content

    monkeypatch.setattr(publication, "render_question_content", render)
    publication._draft("Candidate layout", selected, contract, quality)
    assert observed == [(snapshot["content"], assignment["candidate_id"])]


def test_d2_document_supports_source_option_labels_and_surface_integrals():
    content = (r'\begin{enumerate}[label=（\Alph*）]\item $\oiint_S f$'
               r'\item $\mathscr{A}$\end{enumerate}')
    state, facts, contract = _fixture(content)
    fact = facts.candidates[0]
    snapshot = json.loads(fact['content_snapshot'])
    items = [{'position': 1, 'assignment': dict(state.selection.assignments[0]),
              'candidate_fact': fact, 'content_snapshot': snapshot}]
    draft = publication._draft('Source math', items, contract,
        {'selection_hash': state.selection.selection_hash, 'pre_adaptation_hash': 'b' * 64})
    assert r'\usepackage{amsmath,amssymb,bm,mathrsfs,esint,enumitem}' in draft.latex_source
    assert content in draft.latex_source


def test_native_screen_body_separates_source_label_but_keeps_frozen_item_text():
    content = '37．（清华大学，2002 年）设 A=0。'
    state, facts, contract = _fixture(content)
    fact = facts.candidates[0]
    snapshot = json.loads(fact['content_snapshot'])
    items = [{'position': 1, 'assignment': dict(state.selection.assignments[0]),
              'candidate_fact': fact, 'content_snapshot': snapshot}]
    draft = publication._draft('Source label', items, contract,
        {'selection_hash': state.selection.selection_hash, 'pre_adaptation_hash': 'b' * 64})
    assert draft.body == '1. （清华大学，2002 年）设 A=0。'
    assert draft.structured_items[0]['content'] == content
    assert draft.structured_items[0]['content_hash'] == items[0]['assignment']['content_hash']


def test_published_recovery_does_not_rebuild_old_candidate_layout(tmp_path, monkeypatch):
    state, original_facts, contract = _fixture(_direct_product_question())
    candidate_id = "QB2C9BE69EFFDCAE915841983C"
    facts_payload = original_facts.to_dict()
    fact = facts_payload["candidates"][0]
    fact["candidate_id"] = candidate_id
    facts_hash = _digest(facts_payload)
    facts = type(original_facts)(
        original_facts.contract_version,
        original_facts.projection_identity_hash,
        facts_hash,
        original_facts.target_institution,
        tuple(facts_payload["candidates"]),
        original_facts.unresolved_candidate_ids,
        json.dumps(
            facts_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    assignment = state.selection.assignments[0]
    assignment["candidate_id"] = candidate_id
    assignment["candidate_fact_hash"] = _digest(fact)
    state.base.constraint.candidate_facts_hash = facts_hash
    state.factor_set_receipt["candidate_facts_hash"] = facts_hash
    state.checkpoint.to_dict()["bindings"]["candidate_facts_hash"] = facts_hash

    db = _db()
    artifacts = []
    monkeypatch.setattr(
        publication,
        "render_question_content",
        lambda content, *, candidate_id=None: content,
    )
    first = publication.run_d2_quality_and_publish(
        db,
        state=state,
        facts=facts,
        stage3_contract=contract,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="Old layout",
        artifact_dir=str(tmp_path),
        artifact_callback=_artifact(artifacts),
        quality_callback=_quality(),
        receipt_callback=lambda _receipt: None,
    )
    assert first.state == "completed"
    before_state = tuple(db.execute(
        f"SELECT exam_id,selection_hash,request_hash,artifact_hash,publication_hash "
        f"FROM {publication.D2_TABLE} WHERE job_id='job'"
    ).fetchone())
    before_exam = tuple(db.execute(
        "SELECT id,latex_source,latex_sha256,pdf_sha256 FROM exams WHERE id=?",
        (first.exam_id,),
    ).fetchone())
    assert r"\tag{1}" not in before_exam[1]
    assert r"\tag{2}" not in before_exam[1]
    before_item = tuple(db.execute(
        "SELECT source_candidate_id,content,content_hash FROM exam_items WHERE exam_id=?",
        (first.exam_id,),
    ).fetchone())
    assert before_item[0] == candidate_id
    before_pdf = (tmp_path / f"exam_{first.exam_id}.pdf").read_bytes()

    def repeated(*_args, **_kwargs):
        pytest.fail("published recovery repeated a callback or rebuilt the draft")

    monkeypatch.setattr(publication, "render_question_content", repeated)
    monkeypatch.setattr(publication, "D2_DOCUMENT_PACKAGES", r"\usepackage{future-package-not-in-the-frozen-paper}")
    second = publication.run_d2_quality_and_publish(
        db,
        state=state,
        facts=facts,
        stage3_contract=contract,
        job_id="job",
        worker_id="worker",
        lease_attempt=1,
        subject_id=1,
        title="Old layout",
        artifact_dir=str(tmp_path),
        artifact_callback=repeated,
        quality_callback=repeated,
        receipt_callback=repeated,
    )
    assert second.state == "completed"
    assert second.recovered is True
    assert tuple(db.execute(
        f"SELECT exam_id,selection_hash,request_hash,artifact_hash,publication_hash "
        f"FROM {publication.D2_TABLE} WHERE job_id='job'"
    ).fetchone()) == before_state
    assert tuple(db.execute(
        "SELECT id,latex_source,latex_sha256,pdf_sha256 FROM exams WHERE id=?",
        (first.exam_id,),
    ).fetchone()) == before_exam
    assert tuple(db.execute(
        "SELECT source_candidate_id,content,content_hash FROM exam_items WHERE exam_id=?",
        (first.exam_id,),
    ).fetchone()) == before_item
    assert (tmp_path / f"exam_{first.exam_id}.pdf").read_bytes() == before_pdf
    assert len(artifacts) == 1
