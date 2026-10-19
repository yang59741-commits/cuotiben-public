"""Preserve legacy labels while adding explicit solution-method roles."""


def migrate_v63_to_v64(db):
    db.execute('ALTER TABLE mistake_classifications ADD COLUMN method_annotations_json TEXT')


def schema_issues(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(mistake_classifications)')}
    return [] if 'method_annotations_json' in columns else ['缺少解题方法角色标注字段']


migrate_v63_to_v64.validation_tables = ('mistake_classifications',)
