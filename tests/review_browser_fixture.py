"""Small, isolated browser/PDF fixture. Never opens the installed database."""

import argparse
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_isolated_app import build_app
from cuoti_app.database import get_db
from cuoti_app.services import mistake_classification as classification
from cuoti_app.services.job_handlers import register_app_job_handlers, start_job_worker, stop_job_worker
from cuoti_app.services.mistake_exports import create_mistake_export
from cuoti_app.services.daily_review import record_result
from cuoti_app.services.jobs import JobCoordinator


def prepare(root):
    app = build_app(root)
    app.config['XELATEX_COMMAND'] = 'xelatex'
    app.config['JOB_WORKER_ENABLED'] = True
    with app.app_context():
        db = get_db()
        if not db.execute('SELECT COUNT(*) FROM mistakes').fetchone()[0]:
            samples = [
                ('数学分析', '数列极限', '数列收敛与发散判定', r'证明数列 $\{\tan n\}$ 发散。'),
                ('数学分析', '数列极限', '渐近关系证明', r'设 $\lim_{n\to\infty}(a_n\sum_{i=1}^n a_i^2)=1$，证明 $\lim_{n\to\infty}\sqrt[3]{3n}a_n=1$。'),
                ('高等代数', '二次型', '对称矩阵正交对角化', r'将二次型 $f=2x_1^2+2x_2^2+3x_3^2+2x_1x_2-2x_1x_3+4x_2x_3$ 通过正交变换化为标准形。'),
            ]
            for index in range(21):
                subject, chapter, type_name, content = samples[index % len(samples)]
                sid = db.execute('SELECT id FROM subjects WHERE name=?', (subject,)).fetchone()[0]
                cid = db.execute('SELECT id FROM chapters WHERE name=? AND subject_id=?', (chapter, sid)).fetchone()[0]
                public_id = str(uuid.uuid4())
                db.execute("INSERT INTO mistakes(public_id,subject_id,chapter_id,qnum,content,note,reasons,date_added,status,next_review) VALUES(?,?,?,?,?,'先独立判断条件','方法不会','2026-09-16','未掌握','2026-09-16')",
                           (public_id, sid, cid, str(index + 1), content))
                db.commit()
                if index < 18:
                    row = classification.mistake_row(db, public_id)
                    payload = {'primary_type_code': db.execute('SELECT code FROM question_types WHERE name=?', (type_name,)).fetchone()[0],
                               'knowledge_points': [chapter], 'methods': ['子列法'] if index % 3 == 0 else [], 'confidence': 0.96 if index < 15 else 0.6}
                    classification.publish_prediction(db, row, payload, job_id='fixture-'+str(index), base_revision=0)
                    db.commit()
            job, _ = JobCoordinator(app.config['DATABASE']).submit('mistake-export', {'export_context': 'daily_review'}, idempotency_key='fixture-export')
            result, _ = create_mistake_export(db, payload={'export_context':'daily_review','_job_id':job['id']},
                pdf_dir=Path(app.config['PDF_DIR']), xelatex_command='xelatex')
            for index, row in enumerate(db.execute('SELECT id,round_id FROM daily_review_items LIMIT 2').fetchall()):
                record_result(db, round_id=row['round_id'], item_id=row['id'], result='correct' if index == 0 else 'again')
            print('Fixture PDF:', result.get('download_url'), flush=True)
    register_app_job_handlers(app)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    app = prepare(args.root)
    start_job_worker(app)
    try:
        app.run(host='127.0.0.1', port=args.port, use_reloader=False)
    finally:
        stop_job_worker(app)
