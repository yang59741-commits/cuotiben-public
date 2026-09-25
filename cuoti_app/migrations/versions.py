"""Forward-only database schema migrations."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from flask import current_app

from ..services.review_schedule import SCHEDULE_VERSION
from ..services.semantic_index import (
    FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
    TOKEN_EXTENSION_START,
)
from ..validation import mistake_content_fingerprint


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}


def _add_column(db: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(db, table):
        db.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')


def _backfill_public_ids(db: sqlite3.Connection, table: str) -> None:
    for row in db.execute(
        f'SELECT id FROM "{table}" WHERE public_id IS NULL OR public_id=""'
    ).fetchall():
        db.execute(
            f'UPDATE "{table}" SET public_id=? WHERE id=?',
            (str(uuid.uuid4()), row[0]),
        )


def _execute_statements(
    db: sqlite3.Connection, statements: tuple[str, ...]
) -> None:
    """Execute upgrade statements without sqlite3 executescript implicit commits."""
    for statement in statements:
        db.execute(statement)


def _migrate_6_to_7(db: sqlite3.Connection) -> None:
    for definition in (
        "public_id TEXT",
        "revision INTEGER NOT NULL DEFAULT 1",
        "updated_at TEXT",
        "history_complete INTEGER NOT NULL DEFAULT 0 CHECK(history_complete IN (0,1))",
        "content_fingerprint TEXT",
        "content_fingerprint_version INTEGER NOT NULL DEFAULT 1",
        "answer TEXT NOT NULL DEFAULT ''",
        "solution TEXT NOT NULL DEFAULT ''",
        "source_kind TEXT NOT NULL DEFAULT 'manual'",
        "source_ref TEXT",
        "difficulty TEXT",
    ):
        _add_column(db, "mistakes", definition)
    for definition in (
        "public_id TEXT",
        "client_event_id TEXT",
        "created_at TEXT",
        "lapse_count_after INTEGER NOT NULL DEFAULT 0",
        "source_import_id TEXT",
    ):
        _add_column(db, "review_events", definition)
    now = datetime.now(timezone.utc).isoformat()
    _backfill_public_ids(db, "mistakes")
    _backfill_public_ids(db, "review_events")
    db.execute("UPDATE mistakes SET updated_at=? WHERE updated_at IS NULL", (now,))
    db.execute(
        "UPDATE review_events SET created_at=reviewed_at WHERE created_at IS NULL"
    )
    db.execute(
        "UPDATE review_events SET lapse_count_after=COALESCE((SELECT lapse_count "
        "FROM mistakes WHERE mistakes.id=review_events.mistake_id),0)"
    )
    for row in db.execute(
        "SELECT id,qnum,content FROM mistakes WHERE content_fingerprint IS NULL"
    ).fetchall():
        db.execute(
            "UPDATE mistakes SET content_fingerprint=? WHERE id=?",
            (mistake_content_fingerprint(row[1], row[2]), row[0]),
        )
    # Do not use executescript in migrations: sqlite3 executescript performs an
    # implicit commit and would make a failed migration only partly reversible.
    for statement in (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mistakes_public_id ON mistakes(public_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_public_id ON review_events(public_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_review_events_client_event "
        "ON review_events(client_event_id) WHERE client_event_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS mistake_reason_items("
        "mistake_id INTEGER NOT NULL,reason_code TEXT NOT NULL,"
        "PRIMARY KEY(mistake_id,reason_code),"
        "FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE)",
        "CREATE TABLE IF NOT EXISTS tags("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,"
        "normalized_name TEXT NOT NULL UNIQUE)",
        "CREATE TABLE IF NOT EXISTS mistake_tags("
        "mistake_id INTEGER NOT NULL,tag_id INTEGER NOT NULL,"
        "PRIMARY KEY(mistake_id,tag_id),"
        "FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE,"
        "FOREIGN KEY(tag_id) REFERENCES tags(id) ON DELETE CASCADE)",
    ):
        db.execute(statement)
    for row in db.execute(
        "SELECT id,reasons FROM mistakes WHERE COALESCE(reasons,'')!=''"
    ).fetchall():
        for reason in (part.strip() for part in row[1].split(",")):
            if reason:
                db.execute(
                    "INSERT OR IGNORE INTO mistake_reason_items(mistake_id,reason_code) "
                    "VALUES(?,?)",
                    (row[0], reason),
                )


def _migrate_7_to_8(db: sqlite3.Connection) -> None:
    _add_column(db, "mistakes", "deleted_at TEXT")
    for statement in (
        "CREATE TABLE IF NOT EXISTS attachments("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,public_id TEXT NOT NULL UNIQUE,"
        "mistake_id INTEGER NOT NULL,object_key TEXT NOT NULL UNIQUE,"
        "mime_type TEXT NOT NULL,extension TEXT NOT NULL,"
        "byte_size INTEGER NOT NULL CHECK(byte_size BETWEEN 1 AND 10485760),"
        "width INTEGER NOT NULL CHECK(width > 0),"
        "height INTEGER NOT NULL CHECK(height > 0),sha256 TEXT NOT NULL,"
        "sort_order INTEGER NOT NULL DEFAULT 0,"
        "status TEXT NOT NULL DEFAULT 'published' "
        "CHECK(status IN ('staged','published','deleting')),created_at TEXT NOT NULL,"
        "FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE)",
        "CREATE INDEX IF NOT EXISTS idx_attachments_mistake_sort "
        "ON attachments(mistake_id,sort_order,id)",
        "CREATE INDEX IF NOT EXISTS idx_mistakes_active_updated "
        "ON mistakes(deleted_at,updated_at,id)",
    ):
        db.execute(statement)


def _migrate_8_to_9(db: sqlite3.Connection) -> None:
    for definition in (
        "lane TEXT NOT NULL DEFAULT 'heavy' CHECK(lane IN ('fast','heavy'))",
        "priority INTEGER NOT NULL DEFAULT 0 CHECK(priority BETWEEN -100 AND 100)",
        "attempts_in_cycle INTEGER NOT NULL DEFAULT 0 CHECK(attempts_in_cycle >= 0)",
        "manual_retry_count INTEGER NOT NULL DEFAULT 0 CHECK(manual_retry_count >= 0)",
        "error_category TEXT",
        "error_retryable INTEGER CHECK(error_retryable IS NULL OR error_retryable IN (0,1))",
        "error_correlation_id TEXT",
    ):
        _add_column(db, "background_jobs", definition)
    _add_column(db, "mistake_exports", "source_job_id TEXT")
    db.execute(
        "UPDATE background_jobs SET lane=CASE "
        "WHEN kind IN ('analysis','classification','batch-mistakes') THEN 'fast' "
        "ELSE 'heavy' END"
    )
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS job_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
            manual_retry_number INTEGER NOT NULL DEFAULT 0
                CHECK(manual_retry_number >= 0),
            worker_id TEXT NOT NULL,
            lane TEXT NOT NULL CHECK(lane IN ('fast','heavy')),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL CHECK(status IN (
                'running','succeeded','failed','cancelled','retrying','interrupted'
            )),
            error_code TEXT,
            error_correlation_id TEXT,
            UNIQUE(job_id,attempt_number),
            FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS job_business_results(
            job_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            result_json TEXT NOT NULL,
            committed_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES background_jobs(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_job_attempts_job "
        "ON job_attempts(job_id,attempt_number DESC)",
        "CREATE INDEX IF NOT EXISTS idx_background_jobs_lane_claim "
        "ON background_jobs(lane,status,available_at,priority,created_at,id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mistake_exports_source_job "
        "ON mistake_exports(source_job_id) WHERE source_job_id IS NOT NULL",
    ))
def _migrate_9_to_10(db: sqlite3.Connection) -> None:
    subject_columns = {
        row[1] for row in db.execute("PRAGMA table_info(subjects)").fetchall()
    }
    chapter_columns = {
        row[1] for row in db.execute("PRAGMA table_info(chapters)").fetchall()
    }
    if "subject_code" not in subject_columns:
        db.execute("ALTER TABLE subjects ADD COLUMN subject_code TEXT")
    if "display_order" not in subject_columns:
        db.execute("ALTER TABLE subjects ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
    if "chapter_code" not in chapter_columns:
        db.execute("ALTER TABLE chapters ADD COLUMN chapter_code TEXT")
    if "display_order" not in chapter_columns:
        db.execute("ALTER TABLE chapters ADD COLUMN display_order INTEGER NOT NULL DEFAULT 0")
    if "parent_code" not in chapter_columns:
        db.execute("ALTER TABLE chapters ADD COLUMN parent_code TEXT")
    catalog = current_app.extensions.get("catalog_service")
    if catalog is not None:
        catalog.sync_database(db)
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_subjects_code "
        "ON subjects(subject_code) WHERE subject_code IS NOT NULL"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chapters_code "
        "ON chapters(chapter_code) WHERE chapter_code IS NOT NULL"
    )


def _migrate_10_to_11(db: sqlite3.Connection) -> None:
    _add_column(db, "mistakes", "history_complete_from TEXT")
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS import_previews(
            token_hash TEXT PRIMARY KEY,
            input_sha256 TEXT NOT NULL,
            database_revision TEXT NOT NULL,
            mode TEXT NOT NULL CHECK(mode IN ('merge','replace')),
            plan_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_import_previews_expiry "
        "ON import_previews(expires_at,used_at)",
        """CREATE TABLE IF NOT EXISTS bulk_operations(
            operation_id TEXT PRIMARY KEY,
            input_hash TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
            result_json TEXT,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS bulk_previews(
            token_hash TEXT PRIMARY KEY,
            input_hash TEXT NOT NULL,
            database_revision TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_bulk_previews_expiry "
        "ON bulk_previews(expires_at,used_at)",
    ))


def _migrate_11_to_12(db: sqlite3.Connection) -> None:
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS exam_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            exam_id INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position >= 1),
            question_type TEXT NOT NULL DEFAULT '',
            difficulty TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            points REAL NOT NULL DEFAULT 0 CHECK(points >= 0),
            source_kind TEXT NOT NULL DEFAULT 'unknown',
            source_mistake_public_id TEXT,
            source_candidate_id TEXT,
            source_metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            UNIQUE(exam_id,position),
            FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exam_items_exam_position "
        "ON exam_items(exam_id,position)",
    ))


def _migrate_12_to_13(db: sqlite3.Connection) -> None:
    _add_column(db, "exams", "latex_source TEXT")
    _add_column(db, "exams", "latex_sha256 TEXT")
    _add_column(db, "exams", "pdf_sha256 TEXT")
    _add_column(
        db, "exams", "artifact_revision INTEGER NOT NULL DEFAULT 1"
    )
    _add_column(
        db, "exams", "recompile_available INTEGER NOT NULL DEFAULT 0"
    )
    _add_column(db, "exams", "last_recompile_job_id TEXT")


def _migrate_13_to_14(db: sqlite3.Connection) -> None:
    _add_column(db, "mistakes", "source_exam_attempt_item_id INTEGER")
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS exam_attempts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            exam_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft','submitted')),
            started_at TEXT NOT NULL,
            submitted_at TEXT,
            duration_seconds INTEGER CHECK(duration_seconds IS NULL OR duration_seconds >= 0),
            total_score REAL NOT NULL DEFAULT 0 CHECK(total_score >= 0),
            max_score REAL NOT NULL DEFAULT 0 CHECK(max_score >= 0),
            note TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exam_attempts_exam_created "
        "ON exam_attempts(exam_id,created_at DESC,id DESC)",
        """CREATE TABLE IF NOT EXISTS exam_attempt_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            attempt_id INTEGER NOT NULL,
            exam_item_id INTEGER NOT NULL,
            result_status TEXT NOT NULL DEFAULT 'unanswered'
                CHECK(result_status IN ('correct','incorrect','unanswered')),
            score REAL NOT NULL DEFAULT 0 CHECK(score >= 0),
            comment TEXT NOT NULL DEFAULT '',
            returned_mistake_public_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(attempt_id,exam_item_id),
            FOREIGN KEY(attempt_id) REFERENCES exam_attempts(id) ON DELETE CASCADE,
            FOREIGN KEY(exam_item_id) REFERENCES exam_items(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exam_attempt_items_attempt "
        "ON exam_attempt_items(attempt_id,exam_item_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_mistakes_source_attempt_item "
        "ON mistakes(source_exam_attempt_item_id) "
        "WHERE source_exam_attempt_item_id IS NOT NULL",
    ))


def _migrate_14_to_15(db: sqlite3.Connection) -> None:
    _add_column(db, "mistake_exports", "batch_id INTEGER")
    _add_column(
        db, "mistake_exports", "volume_index INTEGER NOT NULL DEFAULT 1"
    )
    _add_column(
        db, "mistake_exports", "layout TEXT NOT NULL DEFAULT 'compact'"
    )
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS mistake_export_batches(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            public_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            total_count INTEGER NOT NULL CHECK(total_count BETWEEN 1 AND 1000),
            volume_count INTEGER NOT NULL CHECK(volume_count BETWEEN 1 AND 10),
            layout TEXT NOT NULL CHECK(layout IN ('compact','notes','two_column')),
            filter_snapshot_json TEXT NOT NULL DEFAULT '{}',
            source_job_id TEXT UNIQUE,
            artifact_status TEXT NOT NULL DEFAULT 'pending'
                CHECK(artifact_status IN ('pending','published'))
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mistake_exports_batch_volume "
        "ON mistake_exports(batch_id,volume_index)",
        """CREATE TABLE IF NOT EXISTS mistake_export_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id INTEGER NOT NULL,
            position INTEGER NOT NULL CHECK(position >= 1),
            mistake_public_id TEXT,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            qnum TEXT NOT NULL,
            date_added TEXT NOT NULL,
            reasons TEXT NOT NULL,
            note TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(export_id,position),
            FOREIGN KEY(export_id) REFERENCES mistake_exports(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_mistake_export_items_export_position "
        "ON mistake_export_items(export_id,position)",
    ))
    legacy_rows = db.execute(
        "SELECT id,created_at,expires_at,item_count,artifact_status,source_job_id "
        "FROM mistake_exports WHERE batch_id IS NULL ORDER BY id"
    ).fetchall()
    for row in legacy_rows:
        cursor = db.execute(
            "INSERT INTO mistake_export_batches(public_id,created_at,expires_at,"
            "total_count,volume_count,layout,filter_snapshot_json,source_job_id,"
            "artifact_status) VALUES(?,?,?,?,1,'compact','{}',?,?)",
            (
                str(uuid.uuid4()), row["created_at"], row["expires_at"],
                row["item_count"], row["source_job_id"], row["artifact_status"],
            ),
        )
        db.execute(
            "UPDATE mistake_exports SET batch_id=?,volume_index=1,layout='compact' "
            "WHERE id=?",
            (cursor.lastrowid, row["id"]),
        )


def _migrate_15_to_16(db: sqlite3.Connection) -> None:
    """Enable forward-only statistics completeness and daily state snapshots."""
    enabled_at = datetime.now().astimezone().isoformat(timespec="seconds")
    complete_since = enabled_at[:10]
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS statistics_metadata(
            id INTEGER PRIMARY KEY CHECK(id=1),
            complete_since TEXT NOT NULL,
            enabled_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS learning_daily_snapshots(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_day TEXT NOT NULL,
            subject_id INTEGER NOT NULL DEFAULT 0,
            chapter_id INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL CHECK(total_count >= 0),
            mastered_count INTEGER NOT NULL CHECK(mastered_count >= 0),
            active_count INTEGER NOT NULL CHECK(active_count >= 0),
            captured_at TEXT NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0 CHECK(estimated IN (0,1)),
            UNIQUE(snapshot_day,subject_id,chapter_id)
        )""",
        "CREATE INDEX IF NOT EXISTS idx_learning_snapshots_day "
        "ON learning_daily_snapshots(snapshot_day,subject_id,chapter_id)",
    ))
    db.execute(
        "INSERT OR IGNORE INTO statistics_metadata(id,complete_since,enabled_at) "
        "VALUES(1,?,?)",
        (complete_since, enabled_at),
    )


def _migrate_16_to_17(db: sqlite3.Connection) -> None:
    """Add traceable state for the fixed four-stage review schedule."""
    for definition in (
        "review_stage INTEGER NOT NULL DEFAULT 0 CHECK(review_stage BETWEEN 0 AND 3)",
        "review_cycle INTEGER NOT NULL DEFAULT 1 CHECK(review_cycle >= 1)",
        f"schedule_version TEXT NOT NULL DEFAULT '{SCHEDULE_VERSION}'",
    ):
        _add_column(db, "mistakes", definition)
    for definition in (
        "stage_before INTEGER",
        "stage_after INTEGER",
        "cycle_after INTEGER",
        "next_review_after TEXT",
        "schedule_version TEXT",
    ):
        _add_column(db, "review_events", definition)
    # Preserve all existing due dates and event history. Only the current stage
    # is mapped from the compatibility streak; old events remain explicitly null.
    db.execute(
        "UPDATE mistakes SET review_stage=CASE "
        "WHEN status='已掌握' THEN 3 "
        "WHEN COALESCE(correct_streak,0)<0 THEN 0 "
        "WHEN COALESCE(correct_streak,0)>3 THEN 3 "
        "ELSE COALESCE(correct_streak,0) END,review_cycle=1,"
        "schedule_version=?",
        (SCHEDULE_VERSION,),
    )


def _migrate_17_to_18(db: sqlite3.Connection) -> None:
    """Cover the active-mistake cursor order measured in the 100k-row gate."""
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_mistakes_active_id "
        "ON mistakes(deleted_at,id DESC)"
    )


def _migrate_18_to_19(db: sqlite3.Connection) -> None:
    """Add the durable permanent semantic-card schema without touching source data."""

    _execute_statements(
        db,
        (
            """CREATE TABLE IF NOT EXISTS semantic_questions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind TEXT NOT NULL
                    CHECK(length(trim(source_kind)) BETWEEN 1 AND 64),
                candidate_id TEXT NOT NULL
                    CHECK(length(trim(candidate_id)) BETWEEN 1 AND 256),
                subject TEXT NOT NULL CHECK(length(trim(subject)) > 0),
                chapter TEXT NOT NULL CHECK(length(trim(chapter)) > 0),
                source_file TEXT,
                source_year INTEGER,
                source_number TEXT,
                content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                metadata_hash TEXT NOT NULL CHECK(length(metadata_hash)=64),
                token_estimate INTEGER NOT NULL CHECK(token_estimate >= 1),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
                excluded INTEGER NOT NULL DEFAULT 0 CHECK(excluded IN (0,1)),
                first_seen_snapshot TEXT NOT NULL,
                last_seen_snapshot TEXT NOT NULL,
                current_card_version_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(current_card_version_id)
                    REFERENCES semantic_card_versions(id) ON DELETE SET NULL
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_card_versions(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                semantic_question_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                analysis_schema_version INTEGER NOT NULL
                    CHECK(analysis_schema_version >= 1),
                prompt_version TEXT NOT NULL,
                model_role TEXT NOT NULL,
                model_name TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                card_json TEXT NOT NULL CHECK(length(card_json) > 0),
                normalized_terms_json TEXT NOT NULL
                    CHECK(length(normalized_terms_json) > 0),
                card_hash TEXT NOT NULL CHECK(length(card_hash)=64),
                status TEXT NOT NULL
                    CHECK(status IN ('valid','superseded','invalid')),
                provider_request_id_hash TEXT,
                analyzed_at TEXT NOT NULL,
                superseded_at TEXT,
                FOREIGN KEY(semantic_question_id)
                    REFERENCES semantic_questions(id) ON DELETE RESTRICT
            )""",
            f"""CREATE TABLE IF NOT EXISTS semantic_index_runs(
                id TEXT PRIMARY KEY,
                run_kind TEXT NOT NULL
                    CHECK(run_kind IN ('first_full','incremental','rebuild')),
                source_snapshot TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'scanning','queued','running','paused',
                    'paused_budget_review','paused_token_hard_limit',
                    'succeeded','failed','cancelled'
                )),
                analysis_schema_version INTEGER NOT NULL
                    CHECK(analysis_schema_version >= 1),
                prompt_version TEXT NOT NULL,
                model_role TEXT NOT NULL,
                model_name TEXT NOT NULL,
                reasoning_effort TEXT NOT NULL,
                pending_count INTEGER NOT NULL DEFAULT 0 CHECK(pending_count >= 0),
                new_count INTEGER NOT NULL DEFAULT 0 CHECK(new_count >= 0),
                changed_count INTEGER NOT NULL DEFAULT 0 CHECK(changed_count >= 0),
                stale_count INTEGER NOT NULL DEFAULT 0 CHECK(stale_count >= 0),
                metadata_only_count INTEGER NOT NULL DEFAULT 0
                    CHECK(metadata_only_count >= 0),
                deleted_count INTEGER NOT NULL DEFAULT 0 CHECK(deleted_count >= 0),
                completed_count INTEGER NOT NULL DEFAULT 0 CHECK(completed_count >= 0),
                failed_count INTEGER NOT NULL DEFAULT 0 CHECK(failed_count >= 0),
                reused_count INTEGER NOT NULL DEFAULT 0 CHECK(reused_count >= 0),
                requested_concurrency INTEGER NOT NULL DEFAULT 1
                    CHECK(requested_concurrency IN (1,2)),
                effective_concurrency INTEGER NOT NULL DEFAULT 1
                    CHECK(effective_concurrency IN (1,2)),
                token_initial_limit INTEGER NOT NULL
                    DEFAULT {FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT}
                    CHECK(token_initial_limit >= 0),
                token_current_limit INTEGER NOT NULL
                    DEFAULT {FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT},
                token_hard_limit INTEGER NOT NULL
                    DEFAULT {FIRST_FULL_BUILD_HARD_TOKEN_LIMIT},
                token_consumed INTEGER NOT NULL DEFAULT 0 CHECK(token_consumed >= 0),
                token_reserved INTEGER NOT NULL DEFAULT 0 CHECK(token_reserved >= 0),
                token_consumed_unverified INTEGER NOT NULL DEFAULT 0
                    CHECK(token_consumed_unverified >= 0),
                token_remaining INTEGER NOT NULL
                    DEFAULT {FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT},
                token_extension_index INTEGER NOT NULL DEFAULT 0
                    CHECK(token_extension_index BETWEEN 0 AND 6),
                token_next_extension INTEGER DEFAULT {TOKEN_EXTENSION_START},
                token_last_extended_at TEXT,
                token_stop_reason TEXT,
                degrade_reason TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                published_snapshot_id TEXT,
                last_error_code TEXT,
                last_error_summary TEXT,
                CHECK(token_initial_limit <= token_current_limit),
                CHECK(token_current_limit <= token_hard_limit),
                CHECK(token_hard_limit <= {FIRST_FULL_BUILD_HARD_TOKEN_LIMIT}),
                CHECK(token_consumed_unverified <= token_consumed),
                CHECK(token_consumed + token_reserved <= token_current_limit),
                CHECK(token_remaining =
                    token_current_limit - token_consumed - token_reserved),
                CHECK(token_next_extension IS NULL OR token_next_extension >= 0),
                FOREIGN KEY(published_snapshot_id)
                    REFERENCES semantic_index_snapshots(id) ON DELETE SET NULL
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_index_run_items(
                run_id TEXT NOT NULL,
                semantic_question_id INTEGER NOT NULL,
                expected_content_hash TEXT NOT NULL
                    CHECK(length(expected_content_hash)=64),
                change_kind TEXT NOT NULL CHECK(change_kind IN (
                    'new','content_changed','metadata_changed','unchanged',
                    'deleted','excluded','analysis_stale','retry_pending'
                )),
                status TEXT NOT NULL CHECK(status IN (
                    'pending','leased','succeeded','retry_wait',
                    'failed','cancelled','skipped'
                )),
                lease_owner TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
                available_at TEXT NOT NULL,
                card_version_id INTEGER,
                error_code TEXT,
                error_summary TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(run_id,semantic_question_id),
                FOREIGN KEY(run_id)
                    REFERENCES semantic_index_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(semantic_question_id)
                    REFERENCES semantic_questions(id) ON DELETE RESTRICT,
                FOREIGN KEY(card_version_id)
                    REFERENCES semantic_card_versions(id) ON DELETE SET NULL
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_index_snapshots(
                id TEXT PRIMARY KEY,
                source_snapshot TEXT NOT NULL,
                run_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('partial','complete')),
                eligible_count INTEGER NOT NULL CHECK(eligible_count >= 0),
                valid_card_count INTEGER NOT NULL CHECK(valid_card_count >= 0),
                stale_card_count INTEGER NOT NULL CHECK(stale_card_count >= 0),
                missing_card_count INTEGER NOT NULL CHECK(missing_card_count >= 0),
                coverage_ratio REAL NOT NULL CHECK(coverage_ratio BETWEEN 0.0 AND 1.0),
                analysis_schema_version INTEGER NOT NULL
                    CHECK(analysis_schema_version >= 1),
                created_at TEXT NOT NULL,
                published_at TEXT NOT NULL,
                CHECK(valid_card_count <= eligible_count),
                CHECK(stale_card_count <= eligible_count),
                CHECK(missing_card_count <= eligible_count),
                CHECK(status != 'complete' OR missing_card_count = 0),
                FOREIGN KEY(run_id)
                    REFERENCES semantic_index_runs(id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_index_snapshot_items(
                snapshot_id TEXT NOT NULL,
                semantic_question_id INTEGER NOT NULL,
                card_version_id INTEGER NOT NULL,
                content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
                PRIMARY KEY(snapshot_id,semantic_question_id),
                FOREIGN KEY(snapshot_id)
                    REFERENCES semantic_index_snapshots(id) ON DELETE CASCADE,
                FOREIGN KEY(semantic_question_id)
                    REFERENCES semantic_questions(id) ON DELETE RESTRICT,
                FOREIGN KEY(card_version_id)
                    REFERENCES semantic_card_versions(id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS semantic_index_token_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    'reservation','settlement','usage_unverified',
                    'extension','reconciliation','hard_stop'
                )),
                reservation_key TEXT,
                extension_index INTEGER,
                previous_limit INTEGER,
                nominal_amount INTEGER,
                actual_amount INTEGER,
                new_limit INTEGER,
                token_delta_consumed INTEGER NOT NULL DEFAULT 0,
                token_delta_reserved INTEGER NOT NULL DEFAULT 0,
                token_delta_unverified INTEGER NOT NULL DEFAULT 0,
                consumed_after INTEGER NOT NULL CHECK(consumed_after >= 0),
                reserved_after INTEGER NOT NULL CHECK(reserved_after >= 0),
                unverified_after INTEGER NOT NULL CHECK(unverified_after >= 0),
                current_limit_after INTEGER NOT NULL CHECK(current_limit_after >= 0),
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                CHECK(extension_index IS NULL OR extension_index BETWEEN 0 AND 6),
                CHECK(unverified_after <= consumed_after),
                CHECK(consumed_after + reserved_after <= current_limit_after),
                FOREIGN KEY(run_id)
                    REFERENCES semantic_index_runs(id) ON DELETE CASCADE
            )""",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_questions_identity "
            "ON semantic_questions(source_kind,candidate_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_questions_active_scope "
            "ON semantic_questions(active,excluded,subject,chapter)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_questions_content_hash "
            "ON semantic_questions(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_questions_last_seen "
            "ON semantic_questions(last_seen_snapshot)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_card_versions_unique "
            "ON semantic_card_versions(semantic_question_id,content_hash,"
            "analysis_schema_version,prompt_version,model_name,reasoning_effort)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_card_versions_question_status "
            "ON semantic_card_versions(semantic_question_id,status,id DESC)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_card_versions_content_status "
            "ON semantic_card_versions(content_hash,status)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_runs_status_created "
            "ON semantic_index_runs(status,created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_runs_active_identity "
            "ON semantic_index_runs(run_kind,source_snapshot,analysis_schema_version,"
            "prompt_version,model_name,reasoning_effort) WHERE status IN "
            "('scanning','queued','running','paused','paused_budget_review',"
            "'paused_token_hard_limit')",
            "CREATE INDEX IF NOT EXISTS idx_semantic_run_items_claim "
            "ON semantic_index_run_items(run_id,status,available_at,"
            "lease_expires_at,semantic_question_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_run_items_card "
            "ON semantic_index_run_items(card_version_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_snapshots_source_published "
            "ON semantic_index_snapshots(source_snapshot,published_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_snapshot_items_question "
            "ON semantic_index_snapshot_items(semantic_question_id,snapshot_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_snapshot_items_card "
            "ON semantic_index_snapshot_items(card_version_id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_token_events_run_id "
            "ON semantic_index_token_events(run_id,id)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_token_events_reservation "
            "ON semantic_index_token_events(run_id,reservation_key,event_type)",
        ),
    )


def _migrate_19_to_20(db: sqlite3.Connection) -> None:
    _execute_statements(
        db,
        (
            """CREATE TABLE IF NOT EXISTS semantic_index_provider_attempts(
                reservation_key TEXT PRIMARY KEY
                    CHECK(length(reservation_key)=64),
                run_id TEXT NOT NULL,
                semantic_question_id INTEGER NOT NULL,
                item_attempt INTEGER NOT NULL CHECK(item_attempt >= 1),
                provider_attempt INTEGER NOT NULL
                    CHECK(provider_attempt BETWEEN 1 AND 3),
                expected_content_hash TEXT NOT NULL
                    CHECK(length(expected_content_hash)=64),
                prompt_hash TEXT NOT NULL CHECK(length(prompt_hash)=64),
                request_fingerprint TEXT NOT NULL
                    CHECK(length(request_fingerprint)=64),
                status TEXT NOT NULL CHECK(status IN (
                    'reserved','dispatching','response_valid','response_invalid',
                    'provider_failed','outcome_unknown','abandoned','checkpointed'
                )),
                usage_known INTEGER NOT NULL DEFAULT 0
                    CHECK(usage_known IN (0,1)),
                usage_total_tokens INTEGER CHECK(usage_total_tokens >= 0),
                provider_request_id_hash TEXT
                    CHECK(provider_request_id_hash IS NULL OR
                          length(provider_request_id_hash)=64),
                validated_card_json TEXT,
                validated_card_hash TEXT
                    CHECK(validated_card_hash IS NULL OR
                          length(validated_card_hash)=64),
                error_code TEXT,
                error_summary TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                dispatched_at TEXT,
                responded_at TEXT,
                checkpointed_at TEXT,
                CHECK((usage_known=0 AND usage_total_tokens IS NULL) OR
                      (usage_known=1 AND usage_total_tokens IS NOT NULL)),
                CHECK(status NOT IN ('reserved','dispatching','outcome_unknown',
                                     'abandoned') OR
                      (usage_known=0 AND usage_total_tokens IS NULL AND
                       provider_request_id_hash IS NULL)),
                CHECK((status IN ('response_valid','checkpointed') AND
                       validated_card_json IS NOT NULL AND
                       validated_card_hash IS NOT NULL) OR
                      (status NOT IN ('response_valid','checkpointed') AND
                       validated_card_json IS NULL AND
                       validated_card_hash IS NULL)),
                CHECK(status IN ('reserved','abandoned') OR dispatched_at IS NOT NULL),
                CHECK((status IN ('response_valid','response_invalid',
                                  'provider_failed','checkpointed') AND
                       responded_at IS NOT NULL) OR
                      (status NOT IN ('response_valid','response_invalid',
                                      'provider_failed','checkpointed') AND
                       responded_at IS NULL)),
                CHECK((status IN ('reserved','dispatching','response_valid',
                                  'checkpointed') AND error_code IS NULL AND
                       error_summary IS NULL) OR
                      (status IN ('response_invalid','provider_failed',
                                  'outcome_unknown','abandoned') AND
                       error_code IS NOT NULL AND
                       error_summary IS NOT NULL)),
                CHECK((status='checkpointed' AND checkpointed_at IS NOT NULL) OR
                      (status!='checkpointed' AND checkpointed_at IS NULL)),
                UNIQUE(run_id,semantic_question_id,item_attempt,provider_attempt),
                FOREIGN KEY(run_id)
                    REFERENCES semantic_index_runs(id) ON DELETE CASCADE,
                FOREIGN KEY(semantic_question_id)
                    REFERENCES semantic_questions(id) ON DELETE RESTRICT
            )""",
            "CREATE INDEX IF NOT EXISTS idx_semantic_provider_attempts_status "
            "ON semantic_index_provider_attempts(run_id,status,updated_at)",
            "CREATE INDEX IF NOT EXISTS idx_semantic_provider_attempts_question "
            "ON semantic_index_provider_attempts(semantic_question_id,created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_token_reservation_unique "
            "ON semantic_index_token_events(run_id,reservation_key) "
            "WHERE event_type='reservation'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_token_terminal_unique "
            "ON semantic_index_token_events(run_id,reservation_key) "
            "WHERE event_type IN ('settlement','usage_unverified')",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_token_reconcile_unique "
            "ON semantic_index_token_events(run_id,reservation_key) "
            "WHERE event_type='reconciliation'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_token_extension_unique "
            "ON semantic_index_token_events(run_id,extension_index) "
            "WHERE event_type='extension'",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_semantic_token_hard_stop_unique "
            "ON semantic_index_token_events(run_id) WHERE event_type='hard_stop'",
        ),
    )


def _migrate_20_to_21(db: sqlite3.Connection) -> None:
    """Enforce the single-dispatch invariant at the run-item boundary."""

    _execute_statements(
        db,
        (
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_semantic_run_items_single_lease "
            "ON semantic_index_run_items(run_id) WHERE status='leased'",
        ),
    )


def _migrate_21_to_22(db: sqlite3.Connection) -> None:
    """Add two-slot claims and durable batch-boundary control requests."""

    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(semantic_index_runs)")
    }
    statements = ["DROP INDEX IF EXISTS idx_semantic_run_items_single_lease"]
    if "control_requested" not in columns:
        statements.append(
            "ALTER TABLE semantic_index_runs ADD COLUMN control_requested TEXT "
            "CHECK(control_requested IN ('pause','cancel'))"
        )
    if "control_requested_at" not in columns:
        statements.append(
            "ALTER TABLE semantic_index_runs ADD COLUMN control_requested_at TEXT"
        )
    statements.append(
        "CREATE INDEX IF NOT EXISTS idx_semantic_run_items_active_leases "
        "ON semantic_index_run_items(run_id,lease_owner) WHERE status='leased'"
    )
    _execute_statements(db, tuple(statements))


def _migrate_22_to_23(db: sqlite3.Connection) -> None:
    """Persist durable deprecation decisions without deleting source records."""
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS semantic_question_deprecations(
            semantic_question_id INTEGER PRIMARY KEY,
            reason_code TEXT NOT NULL CHECK(length(trim(reason_code)) BETWEEN 1 AND 64),
            reason_summary TEXT NOT NULL CHECK(length(trim(reason_summary)) BETWEEN 1 AND 2048),
            source_run_id TEXT,
            source_shard_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(semantic_question_id)
                REFERENCES semantic_questions(id) ON DELETE RESTRICT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_semantic_question_deprecations_reason "
        "ON semantic_question_deprecations(reason_code,updated_at)",
    ))


def _migrate_23_to_24(db: sqlite3.Connection) -> None:
    """Record explicit per-profile provenance for audited mixed snapshots."""
    columns = {
        str(row[1]) for row in db.execute("PRAGMA table_info(semantic_index_snapshots)")
    }
    statements: list[str] = []
    if "identity_policy" not in columns:
        statements.append(
            "ALTER TABLE semantic_index_snapshots ADD COLUMN identity_policy TEXT "
            "NOT NULL DEFAULT 'single_run' "
            "CHECK(identity_policy IN ('single_run','mixed_audited'))"
        )
    statements.extend((
        """CREATE TABLE IF NOT EXISTS semantic_index_snapshot_profiles(
            snapshot_id TEXT NOT NULL,
            analysis_schema_version INTEGER NOT NULL
                CHECK(analysis_schema_version >= 1),
            prompt_version TEXT NOT NULL,
            model_role TEXT NOT NULL,
            model_name TEXT NOT NULL,
            reasoning_effort TEXT NOT NULL,
            item_count INTEGER NOT NULL CHECK(item_count >= 1),
            created_at TEXT NOT NULL,
            PRIMARY KEY(snapshot_id,analysis_schema_version,prompt_version,
                        model_role,model_name,reasoning_effort),
            FOREIGN KEY(snapshot_id)
                REFERENCES semantic_index_snapshots(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_semantic_snapshot_profiles_model "
        "ON semantic_index_snapshot_profiles(model_role,model_name,reasoning_effort)",
    ))
    _execute_statements(db, tuple(statements))
    db.execute(
        "INSERT OR IGNORE INTO semantic_index_snapshot_profiles("
        "snapshot_id,analysis_schema_version,prompt_version,model_role,model_name,"
        "reasoning_effort,item_count,created_at) "
        "SELECT si.snapshot_id,c.analysis_schema_version,c.prompt_version,c.model_role,"
        "c.model_name,c.reasoning_effort,COUNT(*),s.published_at "
        "FROM semantic_index_snapshot_items si "
        "JOIN semantic_card_versions c ON c.id=si.card_version_id "
        "JOIN semantic_index_snapshots s ON s.id=si.snapshot_id "
        "GROUP BY si.snapshot_id,c.analysis_schema_version,c.prompt_version,c.model_role,"
        "c.model_name,c.reasoning_effort"
    )


def _migrate_24_to_25(db: sqlite3.Connection) -> None:
    """Add the immutable SQLite semantic-recall projection and shadow ledger."""
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS semantic_recall_projection_versions(
            id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            dictionary_version TEXT NOT NULL,
            weights_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('building','published','failed')),
            build_hash TEXT NOT NULL CHECK(length(build_hash)=64),
            item_count INTEGER NOT NULL CHECK(item_count >= 0),
            created_at TEXT NOT NULL,
            published_at TEXT,
            FOREIGN KEY(snapshot_id) REFERENCES semantic_index_snapshots(id)
                ON DELETE RESTRICT
        )""",
        """CREATE TABLE IF NOT EXISTS semantic_recall_documents(
            projection_id TEXT NOT NULL,
            semantic_question_id INTEGER NOT NULL,
            card_version_id INTEGER NOT NULL,
            source_kind TEXT NOT NULL,
            candidate_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            question_type TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            similarity_group TEXT NOT NULL,
            source_year INTEGER,
            source_file_key TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            content_hash TEXT NOT NULL CHECK(length(content_hash)=64),
            normalized_text_hash TEXT NOT NULL CHECK(length(normalized_text_hash)=64),
            PRIMARY KEY(projection_id,semantic_question_id),
            UNIQUE(projection_id,source_kind,candidate_id),
            FOREIGN KEY(projection_id) REFERENCES semantic_recall_projection_versions(id)
                ON DELETE CASCADE,
            FOREIGN KEY(semantic_question_id) REFERENCES semantic_questions(id)
                ON DELETE RESTRICT,
            FOREIGN KEY(card_version_id) REFERENCES semantic_card_versions(id)
                ON DELETE RESTRICT
        )""",
        """CREATE TABLE IF NOT EXISTS semantic_recall_terms(
            projection_id TEXT NOT NULL,
            semantic_question_id INTEGER NOT NULL,
            field_name TEXT NOT NULL,
            normalized_term TEXT NOT NULL,
            weight REAL NOT NULL CHECK(weight > 0 AND weight <= 10),
            PRIMARY KEY(projection_id,semantic_question_id,field_name,normalized_term),
            FOREIGN KEY(projection_id) REFERENCES semantic_recall_projection_versions(id)
                ON DELETE CASCADE,
            FOREIGN KEY(semantic_question_id) REFERENCES semantic_questions(id)
                ON DELETE RESTRICT
        )""",
        """CREATE TABLE IF NOT EXISTS semantic_recall_shadow_runs(
            id TEXT PRIMARY KEY,
            request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
            source_snapshot TEXT NOT NULL,
            projection_id TEXT NOT NULL,
            algorithm_version TEXT NOT NULL,
            dictionary_version TEXT NOT NULL,
            weights_version TEXT NOT NULL,
            hard_pool_count INTEGER NOT NULL CHECK(hard_pool_count >= 0),
            a_count INTEGER NOT NULL CHECK(a_count >= 0),
            b_count INTEGER NOT NULL CHECK(b_count >= 0),
            c_count INTEGER NOT NULL CHECK(c_count >= 0),
            d_count INTEGER NOT NULL CHECK(d_count >= 0),
            legacy_ids_hash TEXT NOT NULL CHECK(length(legacy_ids_hash)=64),
            recalled_ids_hash TEXT NOT NULL CHECK(length(recalled_ids_hash)=64),
            overlap_count INTEGER NOT NULL CHECK(overlap_count >= 0),
            added_count INTEGER NOT NULL CHECK(added_count >= 0),
            dropped_count INTEGER NOT NULL CHECK(dropped_count >= 0),
            partial_cache INTEGER NOT NULL CHECK(partial_cache IN (0,1)),
            relaxed_quotas_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(projection_id) REFERENCES semantic_recall_projection_versions(id)
                ON DELETE RESTRICT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_semantic_recall_documents_scope "
        "ON semantic_recall_documents(projection_id,subject,chapter,question_type,difficulty)",
        "CREATE INDEX IF NOT EXISTS idx_semantic_recall_terms_lookup "
        "ON semantic_recall_terms(projection_id,normalized_term,field_name)",
        "CREATE INDEX IF NOT EXISTS idx_semantic_recall_shadow_request "
        "ON semantic_recall_shadow_runs(request_hash,created_at)",
    ))


def _migrate_25_to_26(db: sqlite3.Connection) -> None:
    """Add durable request identity for exam-attempt draft creation."""

    _add_column(db, "exam_attempts", "idempotency_key TEXT")
    _execute_statements(db, (
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_exam_attempts_exam_idempotency "
        "ON exam_attempts(exam_id,idempotency_key) "
        "WHERE idempotency_key IS NOT NULL",
    ))


def _migrate_26_to_27(db: sqlite3.Connection) -> None:
    """Map every attempt request key to the draft it resumed or created."""

    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS exam_attempt_requests(
            exam_id INTEGER NOT NULL,
            idempotency_key TEXT NOT NULL,
            attempt_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(exam_id,idempotency_key),
            FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE,
            FOREIGN KEY(attempt_id) REFERENCES exam_attempts(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_exam_attempt_requests_attempt "
        "ON exam_attempt_requests(attempt_id)",
        "INSERT OR IGNORE INTO exam_attempt_requests("
        "exam_id,idempotency_key,attempt_id,created_at) "
        "SELECT exam_id,idempotency_key,id,created_at FROM exam_attempts "
        "WHERE idempotency_key IS NOT NULL",
    ))


def _migrate_27_to_28(db: sqlite3.Connection) -> None:
    """Persist actionable metadata alongside durable job failures."""

    _add_column(db, "background_jobs", "error_details_json TEXT")


def _migrate_28_to_29(db: sqlite3.Connection) -> None:
    """Persist per-mistake daily analysis cards and export provenance."""

    _add_column(
        db,
        "mistake_export_batches",
        "export_context TEXT NOT NULL DEFAULT 'selected_mistakes' "
        "CHECK(export_context IN ('selected_mistakes','daily_review'))",
    )
    _execute_statements(db, (
        """CREATE TABLE IF NOT EXISTS daily_analysis_items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mistake_id INTEGER NOT NULL UNIQUE,
            analysis_date TEXT NOT NULL,
            analysis_schema_version INTEGER NOT NULL CHECK(analysis_schema_version >= 1),
            prompt_version INTEGER NOT NULL CHECK(prompt_version >= 1),
            source_hash TEXT NOT NULL,
            analysis_json TEXT NOT NULL,
            source_job_id TEXT,
            model_role TEXT,
            model_name TEXT,
            generated_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_daily_analysis_date "
        "ON daily_analysis_items(analysis_date,mistake_id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_daily_analysis_source_job "
        "ON daily_analysis_items(source_job_id)",
        """CREATE TABLE IF NOT EXISTS daily_analysis_leases(
            mistake_id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL,
            generation_scope TEXT NOT NULL
                CHECK(generation_scope IN ('missing','all','single')),
            claimed_at TEXT NOT NULL,
            FOREIGN KEY(mistake_id) REFERENCES mistakes(id) ON DELETE CASCADE
        )""",
        "CREATE INDEX IF NOT EXISTS idx_daily_analysis_leases_claimed "
        "ON daily_analysis_leases(claimed_at)",
    ))


MIGRATIONS = {
    6: (7, _migrate_6_to_7),
    7: (8, _migrate_7_to_8),
    8: (9, _migrate_8_to_9),
    9: (10, _migrate_9_to_10),
    10: (11, _migrate_10_to_11),
    11: (12, _migrate_11_to_12),
    12: (13, _migrate_12_to_13),
    13: (14, _migrate_13_to_14),
    14: (15, _migrate_14_to_15),
    15: (16, _migrate_15_to_16),
    16: (17, _migrate_16_to_17),
    17: (18, _migrate_17_to_18),
    18: (19, _migrate_18_to_19),
    19: (20, _migrate_19_to_20),
    20: (21, _migrate_20_to_21),
    21: (22, _migrate_21_to_22),
    22: (23, _migrate_22_to_23),
    23: (24, _migrate_23_to_24),
    24: (25, _migrate_24_to_25),
    25: (26, _migrate_25_to_26),
    26: (27, _migrate_26_to_27),
    27: (28, _migrate_27_to_28),
    28: (29, _migrate_28_to_29),
}


MIGRATION_REQUIRED_SCHEMA = {
    7: {
        "columns": {
            "mistakes": {
                "public_id", "revision", "updated_at", "history_complete",
                "content_fingerprint", "content_fingerprint_version", "answer",
                "solution", "source_kind", "source_ref", "difficulty",
            },
            "review_events": {
                "public_id", "client_event_id", "created_at", "lapse_count_after",
                "source_import_id",
            },
        },
        "tables": {"mistake_reason_items", "tags", "mistake_tags"},
        "indexes": {
            "idx_mistakes_public_id", "idx_review_events_public_id",
            "idx_review_events_client_event",
        },
    },
    8: {
        "columns": {"mistakes": {"deleted_at"}},
        "tables": {"attachments"},
        "indexes": {"idx_attachments_mistake_sort", "idx_mistakes_active_updated"},
    },
    9: {
        "columns": {
            "background_jobs": {
                "lane", "priority", "attempts_in_cycle", "manual_retry_count",
                "error_category", "error_retryable", "error_correlation_id",
            },
            "mistake_exports": {"source_job_id"},
        },
        "tables": {"job_attempts", "job_business_results"},
        "indexes": {
            "idx_job_attempts_job", "idx_background_jobs_lane_claim",
            "idx_mistake_exports_source_job",
        },
    },
    10: {
        "columns": {
            "subjects": {"subject_code", "display_order"},
            "chapters": {"chapter_code", "display_order", "parent_code"},
        },
        "indexes": {"idx_subjects_code", "idx_chapters_code"},
    },
    11: {
        "columns": {"mistakes": {"history_complete_from"}},
        "tables": {"import_previews", "bulk_operations", "bulk_previews"},
        "indexes": {"idx_import_previews_expiry", "idx_bulk_previews_expiry"},
    },
    12: {
        "tables": {"exam_items"},
        "indexes": {"idx_exam_items_exam_position"},
    },
    13: {
        "columns": {
            "exams": {
                "latex_source", "latex_sha256", "pdf_sha256", "artifact_revision",
                "recompile_available", "last_recompile_job_id",
            }
        },
    },
    14: {
        "columns": {"mistakes": {"source_exam_attempt_item_id"}},
        "tables": {"exam_attempts", "exam_attempt_items"},
        "indexes": {
            "idx_exam_attempts_exam_created", "idx_exam_attempt_items_attempt",
            "idx_mistakes_source_attempt_item",
        },
    },
    15: {
        "columns": {
            "mistake_exports": {"batch_id", "volume_index", "layout"},
        },
        "tables": {"mistake_export_batches", "mistake_export_items"},
        "indexes": {
            "idx_mistake_exports_batch_volume",
            "idx_mistake_export_items_export_position",
        },
    },
    16: {
        "tables": {"statistics_metadata", "learning_daily_snapshots"},
        "indexes": {"idx_learning_snapshots_day"},
    },
    17: {
        "columns": {
            "mistakes": {"review_stage", "review_cycle", "schedule_version"},
            "review_events": {
                "stage_before", "stage_after", "cycle_after", "next_review_after",
                "schedule_version",
            },
        },
    },
    18: {
        "indexes": {"idx_mistakes_active_id"},
    },
    19: {
        "tables": {
            "semantic_questions",
            "semantic_card_versions",
            "semantic_index_runs",
            "semantic_index_run_items",
            "semantic_index_snapshots",
            "semantic_index_snapshot_items",
            "semantic_index_token_events",
        },
        "columns": {
            "semantic_questions": {
                "id", "source_kind", "candidate_id", "subject", "chapter",
                "source_file", "source_year", "source_number", "content_hash",
                "metadata_hash", "token_estimate", "active", "excluded",
                "first_seen_snapshot", "last_seen_snapshot",
                "current_card_version_id", "created_at", "updated_at",
            },
            "semantic_card_versions": {
                "id", "semantic_question_id", "content_hash",
                "analysis_schema_version", "prompt_version", "model_role",
                "model_name", "reasoning_effort", "card_json",
                "normalized_terms_json", "card_hash", "status",
                "provider_request_id_hash", "analyzed_at", "superseded_at",
            },
            "semantic_index_runs": {
                "id", "run_kind", "source_snapshot", "status",
                "analysis_schema_version", "prompt_version", "model_role",
                "model_name", "reasoning_effort", "pending_count", "new_count",
                "changed_count", "stale_count", "metadata_only_count",
                "deleted_count", "completed_count", "failed_count",
                "reused_count", "requested_concurrency", "effective_concurrency",
                "token_initial_limit", "token_current_limit", "token_hard_limit",
                "token_consumed", "token_reserved", "token_consumed_unverified",
                "token_remaining", "token_extension_index",
                "token_next_extension", "token_last_extended_at",
                "token_stop_reason", "degrade_reason", "created_at", "started_at",
                "updated_at", "finished_at", "published_snapshot_id",
                "last_error_code", "last_error_summary",
            },
            "semantic_index_run_items": {
                "run_id", "semantic_question_id", "expected_content_hash",
                "change_kind", "status", "lease_owner", "lease_expires_at",
                "attempt_count", "available_at", "card_version_id", "error_code",
                "error_summary", "updated_at",
            },
            "semantic_index_snapshots": {
                "id", "source_snapshot", "run_id", "status", "eligible_count",
                "valid_card_count", "stale_card_count", "missing_card_count",
                "coverage_ratio", "analysis_schema_version", "created_at",
                "published_at",
            },
            "semantic_index_snapshot_items": {
                "snapshot_id", "semantic_question_id", "card_version_id",
                "content_hash",
            },
            "semantic_index_token_events": {
                "id", "run_id", "event_type", "reservation_key",
                "extension_index", "previous_limit", "nominal_amount",
                "actual_amount", "new_limit", "token_delta_consumed",
                "token_delta_reserved", "token_delta_unverified", "consumed_after",
                "reserved_after", "unverified_after", "current_limit_after",
                "metadata_json", "created_at",
            },
        },
        "indexes": {
            "idx_semantic_questions_identity",
            "idx_semantic_questions_active_scope",
            "idx_semantic_questions_content_hash",
            "idx_semantic_questions_last_seen",
            "idx_semantic_card_versions_unique",
            "idx_semantic_card_versions_question_status",
            "idx_semantic_card_versions_content_status",
            "idx_semantic_runs_status_created",
            "idx_semantic_runs_active_identity",
            "idx_semantic_run_items_claim",
            "idx_semantic_run_items_card",
            "idx_semantic_snapshots_source_published",
            "idx_semantic_snapshot_items_question",
            "idx_semantic_snapshot_items_card",
            "idx_semantic_token_events_run_id",
            "idx_semantic_token_events_reservation",
        },
        "foreign_keys": {
            "semantic_questions": {
                ("current_card_version_id", "semantic_card_versions", "id"),
            },
            "semantic_card_versions": {
                ("semantic_question_id", "semantic_questions", "id"),
            },
            "semantic_index_runs": {
                ("published_snapshot_id", "semantic_index_snapshots", "id"),
            },
            "semantic_index_run_items": {
                ("run_id", "semantic_index_runs", "id"),
                ("semantic_question_id", "semantic_questions", "id"),
                ("card_version_id", "semantic_card_versions", "id"),
            },
            "semantic_index_snapshots": {
                ("run_id", "semantic_index_runs", "id"),
            },
            "semantic_index_snapshot_items": {
                ("snapshot_id", "semantic_index_snapshots", "id"),
                ("semantic_question_id", "semantic_questions", "id"),
                ("card_version_id", "semantic_card_versions", "id"),
            },
            "semantic_index_token_events": {
                ("run_id", "semantic_index_runs", "id"),
            },
        },
    },
    20: {
        "tables": {"semantic_index_provider_attempts"},
        "columns": {
            "semantic_index_provider_attempts": {
                "reservation_key", "run_id", "semantic_question_id",
                "item_attempt", "provider_attempt", "expected_content_hash",
                "prompt_hash", "request_fingerprint", "status", "usage_known",
                "usage_total_tokens", "provider_request_id_hash",
                "validated_card_json", "validated_card_hash", "error_code",
                "error_summary", "created_at", "updated_at", "dispatched_at",
                "responded_at", "checkpointed_at",
            },
        },
        "indexes": {
            "idx_semantic_provider_attempts_status",
            "idx_semantic_provider_attempts_question",
            "idx_semantic_token_reservation_unique",
            "idx_semantic_token_terminal_unique",
            "idx_semantic_token_reconcile_unique",
            "idx_semantic_token_extension_unique",
            "idx_semantic_token_hard_stop_unique",
        },
        "foreign_keys": {
            "semantic_index_provider_attempts": {
                ("run_id", "semantic_index_runs", "id"),
                ("semantic_question_id", "semantic_questions", "id"),
            },
        },
    },
    21: {
        "indexes": {"idx_semantic_run_items_single_lease"},
    },
    22: {
        "columns": {
            "semantic_index_runs": {"control_requested", "control_requested_at"},
        },
        "indexes": {"idx_semantic_run_items_active_leases"},
    },
    23: {
        "tables": {"semantic_question_deprecations"},
        "columns": {"semantic_question_deprecations": {
            "semantic_question_id", "reason_code", "reason_summary", "source_run_id",
            "source_shard_id", "created_at", "updated_at",
        }},
        "indexes": {"idx_semantic_question_deprecations_reason"},
        "foreign_keys": {"semantic_question_deprecations": {
            ("semantic_question_id", "semantic_questions", "id"),
        }},
    },
    24: {
        "tables": {"semantic_index_snapshot_profiles"},
        "columns": {
            "semantic_index_snapshots": {"identity_policy"},
            "semantic_index_snapshot_profiles": {
                "snapshot_id", "analysis_schema_version", "prompt_version",
                "model_role", "model_name", "reasoning_effort", "item_count",
                "created_at",
            },
        },
        "indexes": {"idx_semantic_snapshot_profiles_model"},
        "foreign_keys": {"semantic_index_snapshot_profiles": {
            ("snapshot_id", "semantic_index_snapshots", "id"),
        }},
    },
    25: {
        "tables": {
            "semantic_recall_projection_versions", "semantic_recall_documents",
            "semantic_recall_terms", "semantic_recall_shadow_runs",
        },
        "columns": {
            "semantic_recall_projection_versions": {
                "id", "snapshot_id", "algorithm_version", "dictionary_version",
                "weights_version", "status", "build_hash", "item_count",
                "created_at", "published_at",
            },
            "semantic_recall_documents": {
                "projection_id", "semantic_question_id", "card_version_id",
                "source_kind", "candidate_id", "subject", "chapter", "question_type",
                "difficulty", "similarity_group", "source_year", "source_file_key",
                "confidence", "content_hash", "normalized_text_hash",
            },
            "semantic_recall_terms": {
                "projection_id", "semantic_question_id", "field_name",
                "normalized_term", "weight",
            },
            "semantic_recall_shadow_runs": {
                "id", "request_hash", "source_snapshot", "projection_id",
                "algorithm_version", "dictionary_version", "weights_version",
                "hard_pool_count", "a_count", "b_count", "c_count", "d_count",
                "legacy_ids_hash", "recalled_ids_hash", "overlap_count", "added_count",
                "dropped_count", "partial_cache", "relaxed_quotas_json", "created_at",
            },
        },
        "indexes": {
            "idx_semantic_recall_documents_scope",
            "idx_semantic_recall_terms_lookup",
            "idx_semantic_recall_shadow_request",
        },
        "foreign_keys": {
            "semantic_recall_projection_versions": {
                ("snapshot_id", "semantic_index_snapshots", "id"),
            },
            "semantic_recall_documents": {
                ("projection_id", "semantic_recall_projection_versions", "id"),
                ("semantic_question_id", "semantic_questions", "id"),
                ("card_version_id", "semantic_card_versions", "id"),
            },
            "semantic_recall_terms": {
                ("projection_id", "semantic_recall_projection_versions", "id"),
                ("semantic_question_id", "semantic_questions", "id"),
            },
            "semantic_recall_shadow_runs": {
                ("projection_id", "semantic_recall_projection_versions", "id"),
            },
        },
    },
    26: {
        "columns": {"exam_attempts": {"idempotency_key"}},
        "indexes": {"idx_exam_attempts_exam_idempotency"},
    },
    27: {
        "tables": {"exam_attempt_requests"},
        "columns": {"exam_attempt_requests": {
            "exam_id", "idempotency_key", "attempt_id", "created_at",
        }},
        "indexes": {"idx_exam_attempt_requests_attempt"},
        "foreign_keys": {"exam_attempt_requests": {
            ("exam_id", "exams", "id"),
            ("attempt_id", "exam_attempts", "id"),
        }},
    },
    28: {
        "columns": {"background_jobs": {"error_details_json"}},
    },
    29: {
        "tables": {"daily_analysis_items", "daily_analysis_leases"},
        "columns": {
            "mistake_export_batches": {"export_context"},
            "daily_analysis_items": {
                "id", "mistake_id", "analysis_date", "analysis_schema_version",
                "prompt_version", "source_hash", "analysis_json", "source_job_id",
                "model_role", "model_name", "generated_at", "updated_at",
            },
            "daily_analysis_leases": {
                "mistake_id", "job_id", "generation_scope", "claimed_at",
            },
        },
        "indexes": {
            "idx_daily_analysis_date", "idx_daily_analysis_source_job",
            "idx_daily_analysis_leases_claimed",
        },
        "foreign_keys": {
            "daily_analysis_items": {("mistake_id", "mistakes", "id")},
            "daily_analysis_leases": {("mistake_id", "mistakes", "id")},
        },
    },
}


def migration_schema_issues(db: sqlite3.Connection, target: int) -> list[str]:
    """Return missing objects that the just-finished migration must provide."""

    required = MIGRATION_REQUIRED_SCHEMA.get(target, {})
    existing_objects = {
        (row[0], row[1])
        for row in db.execute(
            "SELECT type,name FROM sqlite_master WHERE type IN ('table','index')"
        ).fetchall()
    }
    issues: list[str] = []
    for table in sorted(required.get("tables", set())):
        if ("table", table) not in existing_objects:
            issues.append(f"版本 {target} 缺少表 {table}")
    for index in sorted(required.get("indexes", set())):
        if ("index", index) not in existing_objects:
            issues.append(f"版本 {target} 缺少索引 {index}")
    for table, required_columns in required.get("columns", {}).items():
        if ("table", table) not in existing_objects:
            issues.append(f"版本 {target} 缺少表 {table}")
            continue
        columns = {
            row[1] for row in db.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        for column in sorted(required_columns - columns):
            issues.append(f"版本 {target} 的表 {table} 缺少字段 {column}")
    for table, required_foreign_keys in required.get("foreign_keys", {}).items():
        if ("table", table) not in existing_objects:
            issues.append(f"版本 {target} 缺少表 {table}")
            continue
        foreign_keys = {
            (row[3], row[2], row[4])
            for row in db.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        }
        for source_column, target_table, target_column in sorted(
            required_foreign_keys - foreign_keys
        ):
            issues.append(
                f"版本 {target} 的表 {table} 缺少外键 "
                f"{source_column}->{target_table}.{target_column}"
            )
    return issues

