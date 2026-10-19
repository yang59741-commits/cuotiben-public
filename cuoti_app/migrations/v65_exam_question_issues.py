"""Local issue reports for frozen exam questions; no learning-state writes."""


def migrate_v64_to_v65(db):
    db.execute("""CREATE TABLE exam_question_issues(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        public_id TEXT NOT NULL UNIQUE,
        exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
        exam_item_public_id TEXT NOT NULL,
        category TEXT NOT NULL CHECK(category IN('formula','condition','merged','classification','other')),
        description TEXT NOT NULL,
        snapshot_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open' CHECK(status IN('open','resolved','dismissed')),
        handling_note TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    db.execute('CREATE INDEX idx_exam_issues_exam ON exam_question_issues(exam_id,id)')
    db.execute('CREATE INDEX idx_exam_issues_status ON exam_question_issues(status,id)')


def schema_issues(db):
    expected = {'id', 'public_id', 'exam_id', 'exam_item_public_id', 'category', 'description',
                'snapshot_json', 'status', 'handling_note', 'revision', 'created_at', 'updated_at'}
    actual = {row[1] for row in db.execute('PRAGMA table_info(exam_question_issues)')}
    issues = [] if actual == expected else ['题目问题记录表结构不完整']
    indexes = {row[1] for row in db.execute('PRAGMA index_list(exam_question_issues)')}
    if not {'idx_exam_issues_exam', 'idx_exam_issues_status'} <= indexes:
        issues.append('题目问题记录索引不完整')
    return issues


migrate_v64_to_v65.validation_tables = ('exam_question_issues',)
