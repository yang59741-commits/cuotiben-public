import sqlite3

import pytest

from cuoti_app.migrations import v42_exam_generation_stage4_facts as old
from cuoti_app.migrations.v58_target_relative_past_sources import migrate_v57_to_v58, FACT_ITEMS_SQL


def test_registered_step_checks_only_declared_affected_tables(monkeypatch):
    from cuoti_app import database
    def migration(db):
        db.execute('INSERT INTO affected VALUES(1)')
    migration.validation_tables = ('affected',)
    monkeypatch.setitem(database.MIGRATIONS, 57, (58, migration))
    monkeypatch.setattr(database, 'migration_schema_issues', lambda db, version: [])
    with sqlite3.connect(':memory:') as db:
        db.execute('CREATE TABLE affected(id INTEGER PRIMARY KEY)')
        statements = []
        db.set_trace_callback(statements.append)
        assert database._run_migration_step(db, 57) == 58
        assert 'PRAGMA quick_check("affected")' in statements
        assert 'PRAGMA foreign_key_check("affected")' in statements
        assert 'PRAGMA quick_check' not in statements
        assert 'PRAGMA foreign_key_check' not in statements
        assert db.execute('SELECT count(*) FROM affected').fetchone()[0] == 1


@pytest.mark.parametrize('commit', [True, False])
def test_source_role_migration_preserves_old_rows_and_transaction_boundary(commit):
    with sqlite3.connect(':memory:') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute(f'CREATE TABLE {old.FACT_RECEIPTS}(job_id TEXT,candidate_facts_hash TEXT,PRIMARY KEY(job_id,candidate_facts_hash))')
        db.execute(old.CREATE_TABLE_SQL[old.FACT_ITEMS])
        for table, sql in old.TRIGGER_SQL.values():
            if table == old.FACT_ITEMS:
                db.execute(sql)
        row = dict(job_id='job', candidate_facts_hash='a' * 64, ordinal=0, source_kind='ordinary',
                   candidate_id='QB1', subject='math', question_type_family='calculation', chapter='chapter',
                   source_exam_id=None, section_id=None, source_institution=None, content_hash='b' * 64,
                   content_snapshot='original snapshot', content_snapshot_hash='c' * 64,
                   card_version_id=1, card_hash='d' * 64, projection_document_hash='e' * 64,
                   difficulty_resolved=1, difficulty_label='中等', difficulty_confidence=0.9,
                   difficulty_reason='difficulty_resolved', fact_json='original fact bytes', fact_hash='f' * 64,
                   created_at='original time')
        db.execute(f'INSERT INTO {old.FACT_RECEIPTS} VALUES(?,?)', ('job', 'a' * 64))
        insert = f'INSERT INTO {old.FACT_ITEMS}({",".join(row)}) VALUES({",".join("?" for _ in row)})'
        db.execute(insert, tuple(row.values()))
        original = db.execute(f'SELECT * FROM {old.FACT_ITEMS}').fetchone()
        db.commit()
        db.execute('BEGIN')
        migrate_v57_to_v58(db)
        assert db.execute(f'SELECT * FROM {old.FACT_ITEMS}').fetchone() == original
        local = row | dict(ordinal=1, candidate_id='PL1', source_kind='past_exam_local', chapter=None,
                           source_exam_id='home-exam', section_id='home-section', source_institution='Home')
        db.execute(insert, tuple(local.values()))
        with pytest.raises(sqlite3.IntegrityError, match='immutable'):
            db.execute(f"UPDATE {old.FACT_ITEMS} SET candidate_id='changed' WHERE candidate_id='QB1'")
        if commit:
            db.commit()
        else:
            db.rollback()
        expected_sql = FACT_ITEMS_SQL if commit else old.CREATE_TABLE_SQL[old.FACT_ITEMS]
        actual_sql = db.execute('SELECT sql FROM sqlite_master WHERE name=?', (old.FACT_ITEMS,)).fetchone()[0]
        assert old._normalized(actual_sql) == old._normalized(expected_sql)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert db.execute(f'SELECT count(*) FROM {old.FACT_ITEMS}').fetchone()[0] == (2 if commit else 1)
        assert db.execute("SELECT count(*) FROM sqlite_temp_master WHERE name='stage4_source_role_rows'").fetchone()[0] == 0
