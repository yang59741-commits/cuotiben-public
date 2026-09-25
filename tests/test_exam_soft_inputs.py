from concurrent.futures import ThreadPoolExecutor
import sqlite3
import threading

import pytest

from cuoti_app.migrations.v52_exam_generation_soft_inputs import migrate_v51_to_v52, schema_issues
from cuoti_app.services.exam_generation_soft_inputs import freeze_soft_input, SOFT_INPUTS


def make_db(path=":memory:"):
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE background_jobs(id TEXT PRIMARY KEY)")
    db.executemany("INSERT INTO background_jobs VALUES(?)", [("job",), ("next",)])
    migrate_v51_to_v52(db)
    db.commit()
    return db


def freeze(db, loader, **overrides):
    args = dict(job_id="job", mode="past", input_key="test", allowed_sources=["mistakes"], loader=loader)
    return freeze_soft_input(db, **{**args, **overrides})


def test_additive_migration_and_first_read_survive_restart(tmp_path):
    path = tmp_path / "fixture.db"
    db = make_db(path)
    assert schema_issues(db) == []
    migrate_v51_to_v52(db)
    first = freeze(db, lambda: {"items": [1]})
    db.close()
    with sqlite3.connect(path) as db:
        recovered = freeze(db, lambda: pytest.fail("must not read newer inputs"))
        assert recovered == first
        db.execute(f"UPDATE {SOFT_INPUTS} SET allowed_sources_json=?", ('[ "mistakes" ]',))
        db.commit()
        assert freeze(db, lambda: pytest.fail("formatting is not a new input")) == first
        assert freeze(db, lambda: {"items": [2]}, job_id="next")["payload"] == {"items": [2]}


def test_damaged_optional_payload_never_refreshes_live_input():
    with make_db() as db:
        first = freeze(db, lambda: {"items": []})
        db.execute(f"UPDATE {SOFT_INPUTS} SET payload_json='[]'")
        db.commit()
        damaged = freeze(db, lambda: pytest.fail("damaged snapshot must stay frozen"))
        assert damaged["read_cutoff"] == first["read_cutoff"]
        assert damaged["payload"]["state"] == "omitted"


def test_mode_boundaries_and_failed_first_read():
    with make_db() as db:
        with pytest.raises(ValueError, match="read scope"):
            freeze(db, lambda: {}, mode="mistakes", allowed_sources=["free"])
        with pytest.raises(RuntimeError):
            freeze(db, lambda: (_ for _ in ()).throw(RuntimeError("read failed")))
        assert db.execute(f"SELECT count(*) FROM {SOFT_INPUTS}").fetchone()[0] == 0
        freeze(db, lambda: {"state": "omitted"})
        with pytest.raises(ValueError, match="scope changed"):
            freeze(db, lambda: {}, allowed_sources=["past"])


def test_two_connections_freeze_one_input(tmp_path):
    path = tmp_path / "concurrent.db"
    make_db(path).close()
    barrier = threading.Barrier(2)
    calls = []

    def worker(value):
        with sqlite3.connect(path) as db:
            barrier.wait(timeout=5)
            return freeze(db, lambda: (calls.append(value) or {"value": value}))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, [1, 2]))
    assert len(calls) == 1
    assert results[0] == results[1]
