"""Add task-local frozen optional inputs; published exam facts are unchanged."""

from ..services.exam_generation_soft_inputs import SOFT_INPUTS, SOFT_INPUT_VERSION


def migrate_v51_to_v52(db):
    db.execute(f"""CREATE TABLE IF NOT EXISTS {SOFT_INPUTS}(
        job_id TEXT NOT NULL,
        input_key TEXT NOT NULL,
        mode TEXT NOT NULL CHECK(mode IN ('free','mistakes','past')),
        allowed_sources_json TEXT NOT NULL CHECK(json_valid(allowed_sources_json)),
        read_cutoff TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        version TEXT NOT NULL CHECK(version='{SOFT_INPUT_VERSION}'),
        PRIMARY KEY(job_id,input_key),
        FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
    )""")


def schema_issues(db):
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({SOFT_INPUTS})")}
    expected = {"job_id", "input_key", "mode", "allowed_sources_json", "read_cutoff",
                "payload_json", "version"}
    return [] if columns == expected else ["exam task soft input table missing or invalid"]
