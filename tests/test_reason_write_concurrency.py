"""A competing writer cannot pass the versioned HTTP reason update unnoticed."""
import sqlite3
from contextlib import closing

from cuoti_app.database import connect_database, get_db
from cuoti_app.routes import mistakes as routes
from cuoti_app.services.mistakes import sync_reason_items
from tests.test_review_fixes import app


def test_reason_update_serializes_or_rejects_competing_writer(app, monkeypatch):
    client = app.test_client()
    item = client.post('/api/v2/mistakes', json={'subject_id':1, 'chapter_id':1,
        'qnum':'race', 'content':'fixture', 'date_added':'2026-09-01'}).json
    competing = {'blocked':False}
    class RowCursor:
        def __init__(self, row): self.row=row
        def fetchone(self): return self.row
    class Connection:
        def __init__(self, db): self.db=db; self.interleaved=False
        def __getattr__(self, key): return getattr(self.db, key)
        def __enter__(self): self.db.__enter__(); return self
        def __exit__(self, *args): return self.db.__exit__(*args)
        def execute(self, sql, params=()):
            cursor=self.db.execute(sql,params)
            if not self.interleaved and sql.startswith('SELECT id,revision FROM mistakes'):
                self.interleaved=True
                row=cursor.fetchone()
                with closing(connect_database(app.config['DATABASE'],timeout=0)) as writer:
                    try:
                        with writer:
                            writer.execute('UPDATE mistakes SET reasons=?,revision=revision+1 WHERE id=? AND revision=1',
                                           ('方法不会',row['id']))
                            sync_reason_items(writer,row['id'],'方法不会')
                    except sqlite3.OperationalError as exc:
                        assert 'locked' in str(exc)
                        competing['blocked']=True
                return RowCursor(row)
            return cursor
    with closing(connect_database(app.config['DATABASE'])) as db:
        proxy=Connection(db)
        with monkeypatch.context() as patch:
            patch.setattr(routes,'get_db',lambda:proxy)
            response=client.put(f"/api/v2/mistakes/{item['public_id']}/reasons",
                                json={'revision':1,'reasons':['计算错误']})
    if competing['blocked']:
        assert response.status_code==200,response.json
        stale=client.put(f"/api/v2/mistakes/{item['public_id']}/reasons",
                         json={'revision':1,'reasons':['方法不会']})
        assert stale.status_code==409 and stale.json['code']=='REVISION_CONFLICT'
        expected='计算错误'
    else:
        assert response.status_code==409,response.json
        expected='方法不会'
    with app.app_context():
        db=get_db()
        row=db.execute('SELECT id,reasons,revision FROM mistakes WHERE public_id=?',(item['public_id'],)).fetchone()
        assert row['revision']==2 and row['reasons']==expected
        assert [r[0] for r in db.execute('SELECT reason_code FROM mistake_reason_items WHERE mistake_id=?',(row['id'],))]==[expected]
