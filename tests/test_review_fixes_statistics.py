from datetime import datetime, timedelta
import pytest
from cuoti_app.database import get_db
from cuoti_app.services.statistics import ensure_daily_snapshot
from tests.test_review_fixes import app, seed_mistake


@pytest.mark.parametrize('change', ['delete', 'move', 'subject'])
def test_snapshots_zero_disappeared_groups_and_restore(app, change):
    with app.app_context():
        db = get_db()
        mistake_id, subject, chapter = seed_mistake(db)
        now = datetime.now().astimezone()
        ensure_daily_snapshot(db, now - timedelta(days=1))
        ensure_daily_snapshot(db, now)
        history = [tuple(r) for r in db.execute(
            'SELECT * FROM learning_daily_snapshots WHERE snapshot_day<?', (now.date().isoformat(),))]
        if change == 'delete':
            db.execute("UPDATE mistakes SET deleted_at='2026-09-25' WHERE id=?", (mistake_id,))
        elif change == 'move':
            target = db.execute('SELECT id FROM chapters WHERE subject_id=? AND id!=? LIMIT 1',
                                (subject, chapter)).fetchone()[0]
            db.execute('UPDATE mistakes SET chapter_id=? WHERE id=?', (target, mistake_id))
        else:
            target = db.execute('SELECT id,subject_id FROM chapters WHERE subject_id!=? LIMIT 1',
                                (subject,)).fetchone()
            db.execute('UPDATE mistakes SET chapter_id=?,subject_id=? WHERE id=?',
                       (target['id'], target['subject_id'], mistake_id))
        db.commit()
        ensure_daily_snapshot(db, now)
        row = db.execute('SELECT total_count,mastered_count,active_count FROM learning_daily_snapshots '
                         'WHERE snapshot_day=? AND subject_id=? AND chapter_id=?',
                         (now.date().isoformat(), subject, chapter)).fetchone()
        assert tuple(row) == (0, 0, 0)
        if change != 'move':
            assert db.execute('SELECT total_count FROM learning_daily_snapshots WHERE snapshot_day=? '
                              'AND subject_id=? AND chapter_id=0', (now.date().isoformat(), subject)).fetchone()[0] == 0
        assert [tuple(r) for r in db.execute('SELECT * FROM learning_daily_snapshots WHERE snapshot_day<?',
                                            (now.date().isoformat(),))] == history
        changes = db.total_changes
        assert not ensure_daily_snapshot(db, now)['updated']
        assert db.total_changes == changes
        db.execute('UPDATE mistakes SET deleted_at=NULL,subject_id=?,chapter_id=? WHERE id=?',
                   (subject, chapter, mistake_id))
        db.commit()
        ensure_daily_snapshot(db, now)
        assert db.execute('SELECT total_count FROM learning_daily_snapshots WHERE snapshot_day=? '
                          'AND subject_id=? AND chapter_id=?',
                          (now.date().isoformat(), subject, chapter)).fetchone()[0] == 1


def test_snapshot_repairs_stale_group_when_overall_revision_is_current(app):
    with app.app_context():
        db = get_db()
        mistake_id, subject, chapter = seed_mistake(db)
        ensure_daily_snapshot(db)
        db.execute('DELETE FROM mistakes WHERE id=?', (mistake_id,))
        db.commit()
        revision = db.execute('SELECT learning_revision FROM app_revisions WHERE id=1').fetchone()[0]
        db.execute('UPDATE learning_daily_snapshots SET learning_revision=?,total_count=0,active_count=0 '
                   'WHERE subject_id=0 AND chapter_id=0', (revision,))
        db.commit()
        assert ensure_daily_snapshot(db)['updated']
        assert all(tuple(r) == (0, 0, 0, revision) for r in db.execute(
            'SELECT total_count,mastered_count,active_count,learning_revision FROM learning_daily_snapshots'))


def test_statistics_read_does_not_commit_callers_transaction(app):
    from cuoti_app.services.statistics import build_statistics, parse_scope
    with app.app_context():
        db = get_db()
        mistake_id, _, _ = seed_mistake(db)
        db.execute("UPDATE mistakes SET note='uncommitted' WHERE id=?", (mistake_id,))
        build_statistics(db, parse_scope({}))
        assert db.in_transaction
        db.rollback()
        assert db.execute('SELECT note FROM mistakes WHERE id=?', (mistake_id,)).fetchone()[0] != 'uncommitted'
