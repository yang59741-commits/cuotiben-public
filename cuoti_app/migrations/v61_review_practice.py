"""Keep focused practice independent of full-library coverage cycles."""

import json


def _freeze_legacy_items(db):
    fields = ('public_id', 'subject_id', 'chapter_id', 'subject', 'chapter', 'qnum', 'content', 'note', 'reasons', 'date_added')
    export_fields = ('subject', 'chapter', 'qnum', 'content', 'note', 'reasons', 'date_added')
    rows = db.execute(
        "SELECT i.id,i.export_batch_id,m.public_id,m.subject_id,m.chapter_id,s.name,c.name,"
        "m.qnum,m.content,m.note,m.reasons,m.date_added FROM daily_review_items i "
        "JOIN mistakes m ON m.id=i.mistake_id LEFT JOIN subjects s ON s.id=m.subject_id "
        "LEFT JOIN chapters c ON c.id=m.chapter_id"
    ).fetchall()
    for row in rows:
        snapshot = dict(zip(fields, row[2:]))
        old = db.execute(
            "SELECT ei.subject,ei.chapter,ei.qnum,ei.content,ei.note,ei.reasons,ei.date_added "
            "FROM mistake_export_items ei JOIN mistake_exports e ON e.id=ei.export_id "
            "WHERE e.batch_id=? AND ei.mistake_public_id=? ORDER BY ei.position LIMIT 1",
            (row[1], snapshot['public_id']),
        ).fetchone()
        if old:
            snapshot.update(zip(export_fields, old))
            ids = db.execute(
                "SELECT s.id,c.id FROM subjects s LEFT JOIN chapters c ON c.subject_id=s.id AND c.name=? WHERE s.name=?",
                (snapshot['chapter'], snapshot['subject']),
            ).fetchone()
            snapshot['subject_id'], snapshot['chapter_id'] = ids if ids else (None, None)
        snapshot['snapshot_version'] = 1
        snapshot['legacy_live_content'] = old is None
        snapshot['snapshot_origin'] = 'legacy_export' if old else 'migration_current'
        snapshot['classification'] = {'status': 'historical_unknown', 'type_name': '', 'primary_type_code': None}
        db.execute("UPDATE daily_review_items SET snapshot_json=? WHERE id=?", (json.dumps(snapshot, ensure_ascii=False), row[0]))


def migrate_v60_to_v61(db):
    db.execute("ALTER TABLE daily_review_rounds ADD COLUMN practice_mode TEXT NOT NULL DEFAULT 'mixed' CHECK(practice_mode IN ('mixed','focused'))")
    db.execute("ALTER TABLE daily_review_rounds ADD COLUMN selection_json TEXT NOT NULL DEFAULT '{}'")
    db.execute("ALTER TABLE daily_review_items ADD COLUMN coverage_round INTEGER")
    db.execute("ALTER TABLE daily_review_items ADD COLUMN snapshot_json TEXT")
    db.execute("UPDATE daily_review_items SET coverage_round=round_number")
    _freeze_legacy_items(db)
    db.execute("DROP INDEX ux_daily_review_items_round_mistake")
    db.execute("CREATE UNIQUE INDEX ux_daily_review_items_round_mistake ON daily_review_items(coverage_round,mistake_id)")
    db.execute("CREATE INDEX idx_daily_review_mode ON daily_review_rounds(practice_mode,id DESC)")


def schema_issues(db):
    issues = []
    for table, columns in {
        "daily_review_rounds": {"practice_mode", "selection_json"},
        "daily_review_items": {"coverage_round", "snapshot_json"},
    }.items():
        actual = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        issues.extend(f"{table} 缺少 {name}" for name in columns - actual)
    return issues


migrate_v60_to_v61.validation_tables = ("daily_review_rounds", "daily_review_items")
