"""Keep daily-review snapshots independent of replaceable source mistakes."""

from .v59_daily_review import ITEM_TABLE_SQL
from .v62_learning_evidence import EVIDENCE_COLUMNS


def migrate_v62_to_v63(db):
    # mistake_id is the historical source identity. Mistakes use AUTOINCREMENT,
    # so replacement imports cannot reuse it and inherit old learning evidence.
    indexes = [row[0] for row in db.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND tbl_name='daily_review_items' AND sql IS NOT NULL"
    )]
    sequence = db.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='daily_review_items'"
    ).fetchone()
    sql = ITEM_TABLE_SQL.replace('CREATE TABLE daily_review_items(',
                                 'CREATE TABLE daily_review_items_v63(')
    sql = sql.replace('    FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE,\n', '')
    extra = ['coverage_round INTEGER', 'snapshot_json TEXT', *EVIDENCE_COLUMNS]
    sql = sql.replace('    UNIQUE(round_id,mistake_id),',
                      ''.join(f'    {column},\n' for column in extra) + '    UNIQUE(round_id,mistake_id),')
    db.execute(sql)
    db.execute('INSERT INTO daily_review_items_v63 SELECT * FROM daily_review_items')
    db.execute('DROP TABLE daily_review_items')
    db.execute('ALTER TABLE daily_review_items_v63 RENAME TO daily_review_items')
    for sql in indexes:
        db.execute(sql)
    if sequence is not None:
        db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='daily_review_items'",
                   (sequence[0],))


def schema_issues(db):
    keys = {(row[3], row[2], row[4], row[6]) for row in
            db.execute('PRAGMA foreign_key_list(daily_review_items)')}
    expected = {('round_id', 'daily_review_rounds', 'id', 'CASCADE'),
                ('export_batch_id', 'mistake_export_batches', 'id', 'SET NULL')}
    return [] if keys == expected else ['daily_review_items 历史保留外键不匹配']


migrate_v62_to_v63.validation_tables = ('daily_review_rounds', 'daily_review_items')
