import sqlite3

from flask import Flask

from cuoti_app import database
from tests.migration_fixtures import create_historical_database
from cuoti_app.migrations import MIGRATIONS, migration_schema_issues
from cuoti_app.migrations.v54_exam_generation_memory_delivery import (
    MEMORY_EVENT_OUTBOX,
    MEMORY_EVENT_DELIVERY_HEADS,
    migrate_v53_to_v54,
)


def test_current_initialization_includes_registered_memory_delivery(tmp_path):
    assert MIGRATIONS[53] == (54, migrate_v53_to_v54)
    app = Flask(__name__)
    path = tmp_path / "fixture.db"
    app.config["DATABASE"] = str(path)
    with app.app_context():
        database.init_db()
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        assert db.execute("PRAGMA user_version").fetchone()[0] == database.DB_SCHEMA_VERSION
        assert migration_schema_issues(db, database.DB_SCHEMA_VERSION) == []
        for table in (MEMORY_EVENT_OUTBOX, MEMORY_EVENT_DELIVERY_HEADS):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    old_path = tmp_path / "schema53.db"
    create_historical_database(old_path, 53)
    with sqlite3.connect(old_path) as db:
        db.row_factory = sqlite3.Row
        subject_count = db.execute("SELECT count(*) FROM subjects").fetchone()[0]
        assert database._run_migration_step(db, 53) == 54
        assert migration_schema_issues(db, 54) == []
        assert db.execute("SELECT count(*) FROM subjects").fetchone()[0] == subject_count
