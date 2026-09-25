import sqlite3

import pytest

from cuoti_app.services.exam_generation_stage4_worker import _projection_identity, Stage4WorkerError


def test_current_membership_selects_new_projection_without_retiring_history():
    with sqlite3.connect(':memory:') as db:
        db.executescript('''
            CREATE TABLE semantic_index_snapshots(id TEXT,source_snapshot TEXT,status TEXT);
            CREATE TABLE semantic_recall_projection_versions(id TEXT,snapshot_id TEXT,algorithm_version TEXT,
                dictionary_version TEXT,weights_version TEXT,build_hash TEXT,item_count INTEGER,status TEXT);
            CREATE TABLE semantic_questions(id INTEGER,current_card_version_id INTEGER,source_kind TEXT,
                candidate_id TEXT,subject TEXT,chapter TEXT,content_hash TEXT,active INTEGER,excluded INTEGER);
            CREATE TABLE semantic_card_versions(id INTEGER,status TEXT,content_hash TEXT);
            CREATE TABLE semantic_recall_documents(projection_id TEXT,semantic_question_id INTEGER,
                card_version_id INTEGER,source_kind TEXT,candidate_id TEXT,subject TEXT,chapter TEXT,content_hash TEXT);
        ''')
        for index in (1, 2):
            db.execute('INSERT INTO semantic_questions VALUES(?,?,?,?,?,?,?,?,?)',
                       (index, index, 'ordinary', f'q{index}', 'math', 'A', str(index) * 64, 1, 0))
            db.execute('INSERT INTO semantic_card_versions VALUES(?,?,?)', (index, 'valid', str(index) * 64))
        for name, count in (('old', 1), ('new', 2)):
            db.execute('INSERT INTO semantic_index_snapshots VALUES(?,?,?)', (name, 'same-source', 'complete'))
            db.execute('INSERT INTO semantic_recall_projection_versions VALUES(?,?,?,?,?,?,?,?)',
                       (name, name, 'semantic-recall-v2', 'math-terms-v1', 'semantic-weights-v2', 'a' * 64, count, 'published'))
            for index in range(1, count + 1):
                db.execute('INSERT INTO semantic_recall_documents VALUES(?,?,?,?,?,?,?,?)',
                           (name, index, index, 'ordinary', f'q{index}', 'math', 'A', str(index) * 64))
        selected = _projection_identity(db, 'same-source', 'semantic-recall-v2', source_kinds=('ordinary',))
        assert selected['projection_id'] == 'new'
        assert db.execute("SELECT status FROM semantic_recall_projection_versions WHERE id='old'").fetchone()[0] == 'published'
        with pytest.raises(Stage4WorkerError, match='多个'):
            _projection_identity(db, 'same-source', 'semantic-recall-v2')
        db.execute("UPDATE semantic_recall_projection_versions SET item_count=2 WHERE id='old'")
        db.execute("INSERT INTO semantic_recall_documents SELECT 'old',semantic_question_id,card_version_id,"
                   "source_kind,candidate_id,subject,chapter,content_hash FROM semantic_recall_documents "
                   "WHERE projection_id='new' AND semantic_question_id=2")
        with pytest.raises(Stage4WorkerError):
            _projection_identity(db, 'same-source', 'semantic-recall-v2', source_kinds=('ordinary',))
        db.execute("UPDATE semantic_recall_documents SET card_version_id=99 "
                   "WHERE projection_id='old' AND semantic_question_id=2")
        assert _projection_identity(db, 'same-source', 'semantic-recall-v2',
                                    source_kinds=('ordinary',))['projection_id'] == 'new'
        db.execute("UPDATE semantic_recall_documents SET content_hash=? WHERE projection_id='new' AND semantic_question_id=2", ('b' * 64,))
        with pytest.raises(Stage4WorkerError):
            _projection_identity(db, 'same-source', 'semantic-recall-v2', source_kinds=('ordinary',))
