"""Add explicit learning evidence without reinterpreting historical results."""

EVIDENCE_COLUMNS = (
    "assistance TEXT NOT NULL DEFAULT 'unknown' CHECK(assistance IN ('none','hint','unknown'))",
    "evidence_version INTEGER NOT NULL DEFAULT 1",
    "reviewed_on TEXT",
    "content_fingerprint TEXT",
)


def migrate_v61_to_v62(db):
    for table in ('review_events', 'daily_review_items'):
        for definition in EVIDENCE_COLUMNS:
            db.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
    db.execute('ALTER TABLE exam_attempt_items ADD COLUMN ' + EVIDENCE_COLUMNS[0])
    db.execute('CREATE INDEX idx_daily_learning_evidence ON daily_review_items(mistake_id,evidence_version,completed_at)')


def schema_issues(db):
    issues = []
    for table in ('review_events', 'daily_review_items', 'exam_attempt_items'):
        expected = {'assistance'} if table == 'exam_attempt_items' else {item.split()[0] for item in EVIDENCE_COLUMNS}
        actual = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        issues.extend(f'{table} missing {column}' for column in sorted(expected - actual))
    return issues


migrate_v61_to_v62.validation_tables = ('review_events', 'daily_review_items', 'exam_attempt_items')
