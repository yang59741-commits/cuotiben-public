import sqlite3
from contextlib import closing
from unittest.mock import patch

import pytest

from cuoti_app import create_app, database
from tools.schema62_release import baseline, migrate, rollback


def test_additive_release_roundtrip_and_used_evidence_protection(tmp_path):
    path = tmp_path / 'fixture.db'
    with patch.object(database, 'DB_SCHEMA_VERSION', 61), patch.object(database, 'SCHEMA_VERSION', 61):
        create_app({'TESTING': True, 'DATABASE': str(path), 'PDF_DIR': str(tmp_path / 'pdfs'),
                    'REGISTER_JOB_HANDLERS': False})
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,date_added) VALUES('sentinel',1,1,'1','unchanged','2026-09-17')")
        mid = db.execute('SELECT id FROM mistakes').fetchone()[0]
        db.execute("INSERT INTO review_events(mistake_id,event_type,reviewed_at,status_after) VALUES(?,'pass','2026-09-16T12:00:00+08:00','未掌握')", (mid,))
        db.commit()
        old = dict(db.execute('SELECT * FROM review_events').fetchone())
        before = baseline(db)
        migrate(db, before)
        new = dict(db.execute('SELECT * FROM review_events').fetchone())
        assert {key: new[key] for key in old} == old
        assert new['assistance'] == 'unknown'
        rollback(db, before)
        assert dict(db.execute('SELECT * FROM review_events').fetchone()) == old
        assert baseline(db) == before
        migrate(db, before)
        db.execute("UPDATE review_events SET evidence_version=2,assistance='none'")
        with pytest.raises(ValueError, match='New learning evidence'):
            rollback(db, before)
        assert db.execute('PRAGMA user_version').fetchone()[0] == 62
