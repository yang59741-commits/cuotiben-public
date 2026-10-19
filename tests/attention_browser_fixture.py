"""Synthetic UI acceptance fixture in an explicitly named candidate directory."""

import argparse
import json
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cuoti_app import create_app
from cuoti_app.database import get_db
from cuoti_app.services import daily_analysis, mistake_classification as classification


def build(root):
    root = root.resolve()
    if root.parent != ROOT / 'build' / 'candidates':
        raise ValueError('Use an explicit build/candidates/<task> directory')
    runtime = root / 'data' / 'runtime'
    runtime.mkdir(parents=True, exist_ok=True)
    app = create_app({
        'TESTING': True, 'DATABASE': str(root / 'data' / 'cuoti.db'),
        'SETTINGS_PATH': str(runtime / 'settings.json'),
        'PDF_DIR': str(root / 'data' / 'pdfs'), 'ATTACHMENT_DIR': str(runtime / 'attachments'),
        'CATALOG_PATH': str(runtime / 'catalog.json'),
        'QUESTION_BANK_DATABASE': str(runtime / 'question_bank.sqlite3'),
        'AI_MEMORY_DIR': str(runtime / 'memory'), 'JOB_INPUT_DIR': str(runtime / 'job_inputs'),
        'TEX_BASE': str(root / 'synthetic_tex'), 'PAST_EXAM_DIR': str(root / 'synthetic_past'),
        'REGISTER_JOB_HANDLERS': False, 'JOB_WORKER_ENABLED': False,
    })
    with app.app_context():
        db = get_db()
        if db.execute('SELECT 1 FROM mistakes LIMIT 1').fetchone():
            return app
        sid = db.execute("SELECT id FROM subjects WHERE name='数学分析'").fetchone()[0]
        cid = db.execute("SELECT id FROM chapters WHERE subject_id=? AND name='数列极限'", (sid,)).fetchone()[0]
        type_code = db.execute("SELECT code FROM question_types WHERE name='数列收敛与发散判定'").fetchone()[0]
        for i in range(35):
            uid = str(uuid.uuid4())
            due = (date.today() - timedelta(days=i % 3)).isoformat() if i % 5 else None
            db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,note,date_added,next_review) "
                       "VALUES(?,?,?,?,?,'先检查单调性和有界性。',?,?)",
                       (uid, sid, cid, f'UX-{i + 1:02d}', r'设 $a_n=1/n$，证明数列收敛并求极限。',
                        '2026-09-27' if i >= 30 else '2026-09-16', due))
            db.commit()
            row = classification.mistake_row(db, uid)
            payload = {'primary_type_code': type_code, 'knowledge_points': ['数列极限'],
                       'method_annotations': [{'code': 'monotone_bounded', 'role': 'primary'},
                                              {'code': 'squeeze_method', 'role': 'auxiliary'}],
                       'method_basis': r'先验证 $0<a_n\leq 1$，再比较相邻项。也可用 $0\leq a_n\leq 1/n$ 作辅助估计。',
                       'confidence': 0.6 if i % 5 in {0, 1} else 0.95}
            if i % 5 == 1:
                classification.update_classification(db, uid, {**payload, 'revision': 0})
            if i % 5 != 3:
                classification.publish_prediction(db, row, payload, job_id=f'fixture-{i}', base_revision=0)
                db.commit()
                source = daily_analysis.source_rows_by_ids(db, [row['id']])[0]
                card = {'note_assessment': '正确', 'correction': '', 'hint': r'比较 $a_{n+1}$ 与 $a_n$。', 'exam_pattern': '数列收敛判定'}
                db.execute("INSERT INTO daily_analysis_items(mistake_id,analysis_date,analysis_schema_version,prompt_version,source_hash,analysis_json,generated_at,updated_at) VALUES(?,?,1,3,?,?,?,?)",
                           (row['id'], row['date_added'], daily_analysis.daily_analysis_source_hash(source), json.dumps(card), classification.now(), classification.now()))
                if i % 5 == 2:
                    db.execute("UPDATE mistakes SET note='已更新的备注' WHERE id=?", (row['id'],))
                db.commit()
        db.execute("UPDATE mistakes SET status='已掌握' WHERE qnum='UX-01'")
        db.commit()
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--port', default=18766, type=int)
    args = parser.parse_args()
    build(args.root).run(host='127.0.0.1', port=args.port, use_reloader=False)
