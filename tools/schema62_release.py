"""Minimal, release-window recovery for the additive learning-evidence migration."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'cuoti_app').is_dir():
    ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))

TABLE_COLUMNS = {
    'review_events': ('assistance', 'evidence_version', 'reviewed_on', 'content_fingerprint'),
    'daily_review_items': ('assistance', 'evidence_version', 'reviewed_on', 'content_fingerprint'),
    'exam_attempt_items': ('assistance',),
}
COUNT_TABLES = ('mistakes', 'exams', 'exam_attempts', 'daily_review_rounds',
                'mistake_export_batches', *TABLE_COLUMNS)


def counts(db):
    return {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
            for table in COUNT_TABLES}


def baseline(db):
    if db.execute('PRAGMA user_version').fetchone()[0] != 61:
        raise ValueError('Expected schema61 before release')
    return {'schema': 61, 'scope': 'additive-columns-before-new-feature-use',
            'counts': counts(db), 'columns': {
                table: [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')]
                for table in TABLE_COLUMNS}}


def migrate(db, before):
    from cuoti_app.migrations.v62_learning_evidence import migrate_v61_to_v62, schema_issues
    if baseline(db) != before:
        raise ValueError('Release baseline changed')
    migrate_v61_to_v62(db)
    db.execute('PRAGMA user_version=62')
    if schema_issues(db) or counts(db) != before['counts']:
        raise ValueError('Migration verification failed')
    for table in TABLE_COLUMNS:
        if db.execute('PRAGMA quick_check(' + table + ')').fetchone()[0] != 'ok':
            raise ValueError('Table check failed: ' + table)
        if db.execute('PRAGMA foreign_key_check(' + table + ')').fetchone():
            raise ValueError('Foreign key check failed: ' + table)


def rollback(db, before):
    if before['schema'] != 61 or db.execute('PRAGMA user_version').fetchone()[0] != 62:
        raise ValueError('Expected this release schema62 recovery')
    if counts(db) != before['counts']:
        raise ValueError('Business rows changed; this minimal recovery no longer applies')
    for table in TABLE_COLUMNS:
        condition = "assistance<>'unknown'"
        if table != 'exam_attempt_items':
            condition += ' OR evidence_version<>1 OR reviewed_on IS NOT NULL OR content_fingerprint IS NOT NULL'
        if db.execute('SELECT 1 FROM ' + table + ' WHERE ' + condition + ' LIMIT 1').fetchone():
            raise ValueError('New learning evidence exists; preserve it and repair forward')
    db.execute('DROP INDEX idx_daily_learning_evidence')
    for table, columns in TABLE_COLUMNS.items():
        for column in reversed(columns):
            db.execute('ALTER TABLE ' + table + ' DROP COLUMN ' + column)
        if [row[1] for row in db.execute('PRAGMA table_info(' + table + ')')] != before['columns'][table]:
            raise ValueError('Rollback columns differ: ' + table)
    db.execute('PRAGMA user_version=61')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('migrate', 'rollback'))
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=rw', uri=True,
                         isolation_level=None, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        from cuoti_app.services.job_contracts import ACTIVE_STATUSES
        states = sorted(ACTIVE_STATUSES)
        if db.execute('SELECT 1 FROM background_jobs WHERE status IN (' +
                      ','.join('?' for _ in states) + ') LIMIT 1', states).fetchone():
            raise ValueError('Finish active jobs before release')
        db.execute('BEGIN IMMEDIATE')
        if args.mode == 'migrate':
            before = baseline(db)
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            with args.receipt.open('x', encoding='utf-8') as stream:
                json.dump(before, stream, ensure_ascii=False, indent=2)
            migrate(db, before)
        else:
            before = json.loads(args.receipt.read_text(encoding='utf-8'))
            rollback(db, before)
        db.commit()
        checkpoint = db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        print(json.dumps({'mode': args.mode, 'schema': db.execute('PRAGMA user_version').fetchone()[0],
                          'counts': counts(db), 'checkpoint_busy': checkpoint[0],
                          'receipt_bytes': args.receipt.stat().st_size}))
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


if __name__ == '__main__':
    main()
