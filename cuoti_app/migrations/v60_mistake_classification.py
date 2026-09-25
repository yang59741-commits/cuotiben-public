"""Add question taxonomy and independently versioned mistake classification."""

import json

from ..services.question_type_catalog import seed_types


def migrate_v59_to_v60(db):
    db.execute("""CREATE TABLE question_types(
        code TEXT PRIMARY KEY,subject TEXT NOT NULL,name TEXT NOT NULL,
        definition TEXT NOT NULL,chapters_json TEXT NOT NULL DEFAULT '[]',
        aliases_json TEXT NOT NULL DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,
        merged_into TEXT REFERENCES question_types(code),revision INTEGER NOT NULL DEFAULT 1,
        UNIQUE(subject,name))""")
    db.execute("""CREATE TABLE mistake_classifications(
        mistake_id INTEGER PRIMARY KEY REFERENCES mistakes(id) ON DELETE CASCADE,
        primary_type_code TEXT REFERENCES question_types(code),
        knowledge_points_json TEXT NOT NULL DEFAULT '[]',methods_json TEXT NOT NULL DEFAULT '[]',
        source_hash TEXT NOT NULL,source TEXT NOT NULL,locked INTEGER NOT NULL DEFAULT 0,
        revision INTEGER NOT NULL DEFAULT 1,catalog_version INTEGER NOT NULL DEFAULT 1,
        updated_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE classification_suggestions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mistake_id INTEGER NOT NULL REFERENCES mistakes(id) ON DELETE CASCADE,
        source_hash TEXT NOT NULL,base_revision INTEGER NOT NULL,payload_json TEXT NOT NULL,
        reason TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',source_job_id TEXT,
        created_at TEXT NOT NULL,UNIQUE(mistake_id,source_job_id))""")
    db.execute("CREATE INDEX idx_classification_type ON mistake_classifications(primary_type_code,mistake_id)")
    db.execute("CREATE INDEX idx_classification_pending ON classification_suggestions(status,mistake_id)")
    for item in seed_types():
        db.execute("INSERT INTO question_types(code,subject,name,definition,chapters_json) VALUES(?,?,?,?,?)",
                   (item['code'], item['subject'], item['name'], item['definition'], json.dumps(item['chapters'], ensure_ascii=False)))


def schema_issues(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [f"缺少表 {name}" for name in migrate_v59_to_v60.validation_tables if name not in tables]


migrate_v59_to_v60.validation_tables = ("question_types", "mistake_classifications", "classification_suggestions")
