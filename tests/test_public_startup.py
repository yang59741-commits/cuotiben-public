"""Fresh downloads start without private databases or prebuilt question indexes."""
import app as desktop
from cuoti_app.database import DB_SCHEMA_VERSION, get_db
from cuoti_app.services.formal_runtime import database_service_guard

def test_empty_download_initializes_and_preserves_its_own_database(tmp_path, monkeypatch):
    monkeypatch.setenv("CUOTI_SEMANTIC_HOME_INSTITUTION", "Synthetic University")
    monkeypatch.setattr(desktop, "DATA_DIR", tmp_path)
    monkeypatch.setattr(desktop, "_application", None)
    path = tmp_path/"data/cuoti.db"
    assert not path.exists()
    with database_service_guard(path):
        first = desktop.get_application()
        assert first.config["SEMANTIC_HOME_INSTITUTION"] == "Synthetic University"
        with first.app_context():
            db = get_db()
            assert db.execute("PRAGMA user_version").fetchone()[0] == DB_SCHEMA_VERSION
            assert db.execute("SELECT COUNT(*) FROM mistakes").fetchone()[0] == 0
            db.execute("INSERT INTO subjects(name) VALUES(?)", ("Synthetic startup subject",))
            db.commit()
        response = first.test_client().get("/api/v2/reviews/due?limit=1")
        assert response.status_code == 200
        assert response.json["summary"] == {"active": 0, "mastered": 0}
    monkeypatch.setattr(desktop, "_application", None)
    with database_service_guard(path):
        second = desktop.get_application()
        with second.app_context():
            assert get_db().execute("SELECT COUNT(*) FROM subjects WHERE name=?", ("Synthetic startup subject",)).fetchone()[0] == 1
    assert not (tmp_path/"instance/question_bank.sqlite3").exists()
