"""Isolated learning-quality UI fixture, with no provider calls."""

import argparse
import json
from pathlib import Path

from review_browser_fixture import prepare, start_job_worker, stop_job_worker
from cuoti_app.database import get_db
from cuoti_app.services import daily_analysis
from cuoti_app.routes.exams_legacy import _store_exam_items


def build(root):
    app = prepare(root)
    with app.app_context():
        db = get_db()
        for row in daily_analysis._active_rows(db, '2026-09-16'):
            db.execute("INSERT OR IGNORE INTO daily_analysis_items(mistake_id,analysis_date,analysis_schema_version,prompt_version,source_hash,analysis_json,generated_at,updated_at) VALUES(?,'2026-09-16',1,3,?,?,'2026-09-17','2026-09-17')", (row['id'], daily_analysis.daily_analysis_source_hash(row), json.dumps({'note_assessment':'部分正确','correction':'先核对使用条件。','hint':'比较不同子列。','exam_pattern':'收敛与发散'}, ensure_ascii=False)))
        db.execute("UPDATE mistakes SET status='已掌握' WHERE id=(SELECT mistake_id FROM daily_review_items WHERE completion_result='again' LIMIT 1)")
        if not db.execute('SELECT 1 FROM exams LIMIT 1').fetchone():
            sid, cid = db.execute('SELECT subject_id,id FROM chapters ORDER BY id LIMIT 1').fetchone()
            content = r'\begin{problem}证明数列 $a_n=1/n$ 收敛。\end{problem}'
            exam = db.execute("INSERT INTO exams(subject_id,title,config,content,created_at,artifact_status) VALUES(?,'学习记录检查',?,?,'2026-09-17','published')", (sid, json.dumps({'chapters':[cid]}), content)).lastrowid
            _store_exam_items(db, exam, content, source='free')
        db.commit()
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    app = build(args.root)
    start_job_worker(app)
    try:
        app.run(host='127.0.0.1', port=args.port, use_reloader=False)
    finally:
        stop_job_worker(app)
