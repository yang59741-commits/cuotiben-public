import json

import pytest

from cuoti_app.migrations.v56_exam_soft_delete import migrate_v55_to_v56
from cuoti_app.migrations.v46_exam_generation_v3_publish import CREATE_TABLE_SQL, USAGE
from cuoti_app.migrations.v53_exam_generation_priority_once import PRIORITY_ONCE_CONSUMPTIONS
from cuoti_app.services import exam_generation_d2_runtime as runtime
from cuoti_app.services.exam_generation_d2_publication import D2_TABLE, published_d2_usage
from cuoti_app.services.exam_generation_memory_delivery import MEMORY_EVENT_OUTBOX, MEMORY_EVENT_DELIVERY_HEADS
from cuoti_app.services.exam_generation_past_v3_production import build_recent_usage_snapshot
from cuoti_app.services.exam_generation_priority_runtime import load_priority_claims
from cuoti_app.services.exam_query import delete_published_exam, restore_published_exam
from tests.test_exam_d2_memory_delivery import _install_delivery, _memory_arguments
from tests.test_exam_priority_runtime import setup_runtime


def test_deleted_native_paper_keeps_history_priority_memory_and_recovers_without_ai(monkeypatch, tmp_path):
    db, args, _ = setup_runtime(monkeypatch, tmp_path)
    # The generation fixture is deliberately tiny; add the formal revision table.
    db.execute("CREATE TABLE app_revisions(id INTEGER PRIMARY KEY,learning_revision INTEGER,statistics_revision INTEGER,updated_at TEXT)")
    db.execute("INSERT INTO app_revisions VALUES(1,0,0,'2026-09-06')")
    migrate_v55_to_v56(db)
    db.execute(CREATE_TABLE_SQL[USAGE])
    _install_delivery(db)
    args.update(_memory_arguments(tmp_path))
    result = runtime.run_d2_generation(db, **args)
    assert result.status == "completed"
    exam_id = db.execute("SELECT id FROM exams").fetchone()[0]
    state = runtime.recover_d2_candidate_chain(db, job_id="job")
    claims = load_priority_claims(db, job_id="job", state=state)
    assert claims
    consumption = [tuple(row) for row in db.execute(f"SELECT * FROM {PRIORITY_ONCE_CONSUMPTIONS}")]
    assert len(consumption) == 1
    publication = db.execute(f"SELECT publication_json FROM {D2_TABLE}").fetchone()[0]
    usage = published_d2_usage(db, subject_id=1)
    history = build_recent_usage_snapshot(db, subject_id=1)
    memory_rows = [tuple(row) for row in db.execute(f"SELECT * FROM {MEMORY_EVENT_OUTBOX}")]
    memory_head = [tuple(row) for row in db.execute(f"SELECT * FROM {MEMORY_EVENT_DELIVERY_HEADS}")]
    files = {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert delete_published_exam(db, tmp_path, exam_id, memory=args['memory'])
    assert published_d2_usage(db, subject_id=1) == usage
    assert build_recent_usage_snapshot(db, subject_id=1) == history
    args['quality_callback'] = lambda *_: pytest.fail("AI must not run after publication")
    args['artifact_callback'] = lambda *_: pytest.fail("PDF must not be rebuilt after publication")
    args['stage5_runner'] = lambda *_a, **_k: pytest.fail("selection must not repeat")
    recovered = runtime.run_d2_generation(db, **args)
    assert recovered.status == "recovered" and recovered.call_counts == {}
    assert db.execute("SELECT COUNT(*) FROM exams").fetchone()[0] == 1
    assert db.execute("SELECT deleted_at FROM exams WHERE id=?", (exam_id,)).fetchone()[0]
    assert restore_published_exam(db, exam_id)
    assert db.execute(f"SELECT publication_json FROM {D2_TABLE}").fetchone()[0] == publication
    assert build_recent_usage_snapshot(db, subject_id=1) == history
    assert load_priority_claims(db, job_id="job", state=state) == claims
    assert [tuple(row) for row in db.execute(f"SELECT * FROM {PRIORITY_ONCE_CONSUMPTIONS}")] == consumption
    assert [tuple(row) for row in db.execute(f"SELECT * FROM {MEMORY_EVENT_OUTBOX}")] == memory_rows
    assert [tuple(row) for row in db.execute(f"SELECT * FROM {MEMORY_EVENT_DELIVERY_HEADS}")] == memory_head
    assert {str(p.relative_to(tmp_path)): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == files
    events = [json.loads(line) for p in (tmp_path / 'memory' / 'past' / 'events' / 'daily').glob('*.jsonl') for line in p.read_text(encoding='utf-8').splitlines()]
    assert [e['event_id'] for e in events] == ['job:request', 'job:result:1']
