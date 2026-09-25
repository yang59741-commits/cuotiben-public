"""Schema 41: explicit legacy, unscored, and verified scoring semantics."""

from __future__ import annotations

import re
import sqlite3


EXAM_COLUMNS = {
    "scoring_mode": (
        "scoring_mode TEXT NOT NULL DEFAULT 'legacy_unknown' "
        "CHECK(scoring_mode IN ('legacy_unknown','unscored','verified'))"
    ),
    "total_points": "total_points TEXT",
    "scoring_evidence_hash": "scoring_evidence_hash TEXT",
    "scoring_projection_hash": "scoring_projection_hash TEXT",
}

EXPECTED_EXAM_COLUMNS = {
    "scoring_mode": ("TEXT", 1, "'legacy_unknown'"),
    "total_points": ("TEXT", 0, None),
    "scoring_evidence_hash": ("TEXT", 0, None),
    "scoring_projection_hash": ("TEXT", 0, None),
}

CREATE_ITEMS_V40_SQL = """CREATE TABLE exam_items(
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
)"""

CREATE_ITEMS_SQL = """CREATE TABLE exam_items_v41(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id TEXT NOT NULL UNIQUE,
    exam_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK(position >= 1),
    question_type TEXT NOT NULL DEFAULT '',
    difficulty TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    points REAL CHECK(points IS NULL OR points >= 0),
    points_canonical TEXT,
    points_evidence_hash TEXT,
    source_kind TEXT NOT NULL DEFAULT 'unknown',
    source_mistake_public_id TEXT,
    source_candidate_id TEXT,
    source_metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(exam_id,position),
    FOREIGN KEY(exam_id) REFERENCES exams(id) ON DELETE CASCADE
)"""

TRIGGERS = {
    "exams_scoring_shape_insert": """CREATE TRIGGER exams_scoring_shape_insert BEFORE INSERT ON exams
    WHEN NOT ((NEW.scoring_mode='legacy_unknown' AND NEW.total_points IS NULL AND NEW.scoring_evidence_hash IS NULL AND NEW.scoring_projection_hash IS NULL)
      OR (NEW.scoring_mode='unscored' AND NEW.total_points IS NULL AND NEW.scoring_evidence_hash IS NULL AND length(NEW.scoring_projection_hash)=64 AND NEW.scoring_projection_hash NOT GLOB '*[^0-9a-f]*')
      OR (NEW.scoring_mode='verified' AND CAST(NEW.total_points AS REAL)>0 AND length(NEW.scoring_evidence_hash)=64 AND NEW.scoring_evidence_hash NOT GLOB '*[^0-9a-f]*' AND length(NEW.scoring_projection_hash)=64 AND NEW.scoring_projection_hash NOT GLOB '*[^0-9a-f]*'))
    BEGIN SELECT RAISE(ABORT,'invalid_exam_scoring_shape'); END""",
    "exams_scoring_shape_update": """CREATE TRIGGER exams_scoring_shape_update BEFORE UPDATE OF scoring_mode,total_points,scoring_evidence_hash,scoring_projection_hash ON exams
    WHEN OLD.artifact_status='published' OR NOT ((NEW.scoring_mode='legacy_unknown' AND NEW.total_points IS NULL AND NEW.scoring_evidence_hash IS NULL AND NEW.scoring_projection_hash IS NULL)
      OR (NEW.scoring_mode='unscored' AND NEW.total_points IS NULL AND NEW.scoring_evidence_hash IS NULL AND length(NEW.scoring_projection_hash)=64 AND NEW.scoring_projection_hash NOT GLOB '*[^0-9a-f]*')
      OR (NEW.scoring_mode='verified' AND CAST(NEW.total_points AS REAL)>0 AND length(NEW.scoring_evidence_hash)=64 AND NEW.scoring_evidence_hash NOT GLOB '*[^0-9a-f]*' AND length(NEW.scoring_projection_hash)=64 AND NEW.scoring_projection_hash NOT GLOB '*[^0-9a-f]*'))
    BEGIN SELECT RAISE(ABORT,'invalid_exam_scoring_update'); END""",
    "exam_items_scoring_shape_insert": """CREATE TRIGGER exam_items_scoring_shape_insert BEFORE INSERT ON exam_items
    WHEN NOT EXISTS(SELECT 1 FROM exams e WHERE e.id=NEW.exam_id AND ((e.scoring_mode='legacy_unknown' AND NEW.points_evidence_hash IS NULL AND NEW.points_canonical IS NULL) OR (e.artifact_status!='published' AND e.scoring_mode='unscored' AND NEW.points IS NULL AND NEW.points_canonical IS NULL AND NEW.points_evidence_hash IS NULL) OR (e.artifact_status!='published' AND e.scoring_mode='verified' AND NEW.points>0 AND CAST(NEW.points_canonical AS REAL)>0 AND length(NEW.points_evidence_hash)=64 AND NEW.points_evidence_hash NOT GLOB '*[^0-9a-f]*')))
    BEGIN SELECT RAISE(ABORT,'invalid_exam_item_scoring_shape'); END""",
    "exam_items_scoring_immutable_update": """CREATE TRIGGER exam_items_scoring_immutable_update BEFORE UPDATE OF points,points_canonical,points_evidence_hash ON exam_items
    BEGIN SELECT RAISE(ABORT,'exam_item_scoring_immutable'); END""",
    "exam_items_scoring_published_delete": """CREATE TRIGGER exam_items_scoring_published_delete AFTER DELETE ON exam_items WHEN EXISTS(SELECT 1 FROM exams e WHERE e.id=OLD.exam_id AND e.artifact_status='published' AND e.scoring_mode!='legacy_unknown')
    BEGIN SELECT RAISE(ABORT,'published_exam_item_scoring_immutable'); END""",
    "exams_scoring_publish_guard": """CREATE TRIGGER exams_scoring_publish_guard BEFORE UPDATE OF artifact_status ON exams WHEN NEW.artifact_status='published' AND ((NEW.scoring_mode!='legacy_unknown' AND (NOT EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=NEW.id) OR (SELECT MIN(i.position) FROM exam_items i WHERE i.exam_id=NEW.id)!=1 OR (SELECT MAX(i.position) FROM exam_items i WHERE i.exam_id=NEW.id)!=(SELECT COUNT(*) FROM exam_items i WHERE i.exam_id=NEW.id))) OR (NEW.scoring_mode='unscored' AND EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=NEW.id AND (i.points IS NOT NULL OR i.points_canonical IS NOT NULL OR i.points_evidence_hash IS NOT NULL))) OR (NEW.scoring_mode='verified' AND EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=NEW.id AND (i.points IS NULL OR i.points<=0 OR i.points_canonical IS NULL OR i.points_evidence_hash IS NULL))))
    BEGIN SELECT RAISE(ABORT,'invalid_published_exam_scoring'); END""",
}


def _norm(value: object) -> str:
    return (
        re.sub(r"\s+", "", str(value or ""))
        .upper()
        .replace("IFNOTEXISTS", "")
        .replace('"', "")
        .replace("`", "")
        .replace("[", "")
        .replace("]", "")
    )


def _table_sql(db: sqlite3.Connection, name: str) -> str:
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def _columns(db: sqlite3.Connection, table: str) -> dict[str, tuple[str, int, object]]:
    return {
        str(row[1]): (str(row[2]).upper(), int(row[3]), row[4])
        for row in db.execute(f'PRAGMA table_info("{table}")')
    }


def _validate_existing_exam_columns(db: sqlite3.Connection) -> set[str]:
    columns = _columns(db, "exams")
    present = set(EXAM_COLUMNS).intersection(columns)
    for name in present:
        if columns[name] != EXPECTED_EXAM_COLUMNS[name]:
            raise RuntimeError(f"v41 existing exams column {name} is incompatible")
    if "scoring_mode" in present:
        exam_sql = _norm(_table_sql(db, "exams"))
        required = "CHECK(SCORING_MODEIN('LEGACY_UNKNOWN','UNSCORED','VERIFIED'))"
        if required not in exam_sql:
            raise RuntimeError("v41 existing exams scoring_mode constraint is incompatible")
    return present


def _validate_exam_scoring_rows(db: sqlite3.Connection) -> None:
    invalid = db.execute(
        "SELECT 1 FROM exams WHERE CASE WHEN "
        "(scoring_mode='legacy_unknown' AND total_points IS NULL AND scoring_evidence_hash IS NULL AND scoring_projection_hash IS NULL) OR "
        "(scoring_mode='unscored' AND total_points IS NULL AND scoring_evidence_hash IS NULL AND length(scoring_projection_hash)=64 AND scoring_projection_hash NOT GLOB '*[^0-9a-f]*') OR "
        "(scoring_mode='verified' AND CAST(total_points AS REAL)>0 AND length(scoring_evidence_hash)=64 AND scoring_evidence_hash NOT GLOB '*[^0-9a-f]*' AND length(scoring_projection_hash)=64 AND scoring_projection_hash NOT GLOB '*[^0-9a-f]*') "
        "THEN 0 ELSE 1 END=1 LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("v41 existing exams scoring data is incompatible")


def _validate_item_rows(db: sqlite3.Connection, *, current: bool) -> None:
    if not current:
        if db.execute(
            "SELECT 1 FROM exams WHERE scoring_mode!='legacy_unknown' LIMIT 1"
        ).fetchone() is not None:
            raise RuntimeError("v41 legacy exam_items cannot substantiate non-legacy scoring")
        return
    invalid = db.execute(
        "SELECT 1 FROM exam_items i JOIN exams e ON e.id=i.exam_id WHERE CASE WHEN "
        "(e.scoring_mode='legacy_unknown' AND i.points_canonical IS NULL AND i.points_evidence_hash IS NULL) OR "
        "(e.scoring_mode='unscored' AND i.points IS NULL AND i.points_canonical IS NULL AND i.points_evidence_hash IS NULL) OR "
        "(e.scoring_mode='verified' AND i.points>0 AND CAST(i.points_canonical AS REAL)>0 AND length(i.points_evidence_hash)=64 AND i.points_evidence_hash NOT GLOB '*[^0-9a-f]*') "
        "THEN 0 ELSE 1 END=1 LIMIT 1"
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("v41 existing exam_items scoring data is incompatible")


def _validate_existing_triggers(db: sqlite3.Connection) -> set[str]:
    rows = {
        str(row[0]): str(row[1])
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (%s)"
            % ",".join("?" for _ in TRIGGERS),
            tuple(TRIGGERS),
        )
    }
    for name, sql in rows.items():
        if _norm(sql) != _norm(TRIGGERS[name]):
            raise RuntimeError(f"v41 existing trigger {name} is incompatible")
    return set(rows)


def _item_schema_state(db: sqlite3.Connection) -> str:
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='exam_items_v41'"
    ).fetchone():
        raise RuntimeError("v41 migration staging table already exists")
    sql = _table_sql(db, "exam_items")
    if _norm(sql) == _norm(CREATE_ITEMS_SQL.replace("exam_items_v41", "exam_items", 1)):
        return "current"
    if _norm(sql) == _norm(CREATE_ITEMS_V40_SQL):
        return "legacy"
    raise RuntimeError("v41 existing exam_items definition is incompatible")


def _validate_item_index(db: sqlite3.Connection, *, may_be_missing: bool) -> None:
    indexes = {
        str(row[1]): str(row[4])
        for row in db.execute("PRAGMA index_list(exam_items)")
        if str(row[3]) == "c"
    }
    if set(indexes) - {"idx_exam_items_exam_position"}:
        raise RuntimeError("v41 existing exam_items index set is incompatible")
    if "idx_exam_items_exam_position" not in indexes:
        if may_be_missing:
            return
        raise RuntimeError("v41 legacy exam_items index is missing")
    columns = [str(row[2]) for row in db.execute("PRAGMA index_info(idx_exam_items_exam_position)")]
    if columns != ["exam_id", "position"]:
        raise RuntimeError("v41 existing exam_items index is incompatible")


def migrate_v40_to_v41(db: sqlite3.Connection) -> None:
    # The migration runner disables FK enforcement outside the transaction for
    # this SQLite table rebuild, and validates every FK before committing.
    if int(db.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise RuntimeError("v41 exam_items rebuild requires foreign_keys=OFF")
    present = _validate_existing_exam_columns(db)
    item_state = _item_schema_state(db)
    existing_triggers = _validate_existing_triggers(db)
    _validate_item_index(db, may_be_missing=item_state == "current")

    for name, definition in EXAM_COLUMNS.items():
        if name not in present:
            db.execute(f"ALTER TABLE exams ADD COLUMN {definition}")
    _validate_exam_scoring_rows(db)
    _validate_item_rows(db, current=item_state == "current")

    if item_state == "legacy":
        for name in existing_triggers:
            db.execute(f'DROP TRIGGER "{name}"')
        existing_triggers.clear()
        db.execute(CREATE_ITEMS_SQL)
        db.execute(
            "INSERT INTO exam_items_v41(id,public_id,exam_id,position,question_type,difficulty,content,content_hash,points,points_canonical,points_evidence_hash,source_kind,source_mistake_public_id,source_candidate_id,source_metadata_json,created_at) "
            "SELECT id,public_id,exam_id,position,question_type,difficulty,content,content_hash,points,NULL,NULL,source_kind,source_mistake_public_id,source_candidate_id,source_metadata_json,created_at FROM exam_items"
        )
        db.execute("DROP TABLE exam_items")
        db.execute("ALTER TABLE exam_items_v41 RENAME TO exam_items")
        db.execute("CREATE INDEX idx_exam_items_exam_position ON exam_items(exam_id,position)")
    elif "idx_exam_items_exam_position" not in {
        str(row[1]) for row in db.execute("PRAGMA index_list(exam_items)")
    }:
        db.execute("CREATE INDEX idx_exam_items_exam_position ON exam_items(exam_id,position)")
    for name, statement in TRIGGERS.items():
        if name not in existing_triggers:
            db.execute(statement)


def schema_issues(db: sqlite3.Connection) -> list[str]:
    issues: list[str] = []
    exam_sql_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='exams'").fetchone()
    exam_sql = _norm(exam_sql_row[0] if exam_sql_row else "")
    if "CHECK(SCORING_MODEIN('LEGACY_UNKNOWN','UNSCORED','VERIFIED'))" not in exam_sql:
        issues.append("版本 41 的 exams 分值模式约束不兼容")
    exam_columns = {str(r[1]): (str(r[2]).upper(), int(r[3]), r[4]) for r in db.execute("PRAGMA table_info(exams)")}
    for name, expected_column in EXPECTED_EXAM_COLUMNS.items():
        if exam_columns.get(name) != expected_column:
            issues.append(f"版本 41 的 exams 缺少字段 {name}")
    item_columns = {str(r[1]): (str(r[2]).upper(), int(r[3]), r[4]) for r in db.execute("PRAGMA table_info(exam_items)")}
    for name in ("points_canonical", "points_evidence_hash"):
        if item_columns.get(name) != ("TEXT", 0, None):
            issues.append(f"版本 41 的 exam_items 缺少字段 {name}")
    if item_columns.get("points") != ("REAL", 0, None):
        issues.append("版本 41 的 exam_items.points 必须可空")
    item_sql_row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='exam_items'").fetchone()
    expected_item_sql = CREATE_ITEMS_SQL.replace("exam_items_v41", "exam_items", 1)
    if item_sql_row is None or _norm(item_sql_row[0]) != _norm(expected_item_sql):
        issues.append("版本 41 的 exam_items 表定义不兼容")
    index_rows = list(db.execute("PRAGMA index_list(exam_items)"))
    user_indexes = {str(row[1]) for row in index_rows if str(row[3]) == "c"}
    if user_indexes != {"idx_exam_items_exam_position"}:
        issues.append("版本 41 的 exam_items 索引集不兼容")
    foreign_keys = {
        (str(row[3]), str(row[2]), str(row[4]), str(row[6]))
        for row in db.execute("PRAGMA foreign_key_list(exam_items)")
    }
    if foreign_keys != {("exam_id", "exams", "id", "CASCADE")}:
        issues.append("版本 41 的 exam_items 外键不兼容")
    trigger_rows = {
        str(row[0]): str(row[1])
        for row in db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name IN (%s)"
            % ",".join("?" for _ in TRIGGERS), tuple(TRIGGERS)
        )
    }
    if set(trigger_rows) != set(TRIGGERS) or any(
        _norm(trigger_rows.get(name)) != _norm(statement)
        for name, statement in TRIGGERS.items()
    ):
        issues.append("版本 41 的分值语义触发器不完整")
    return issues


migrate_v40_to_v41.requires_foreign_keys_off = True

__all__ = ["migrate_v40_to_v41", "schema_issues"]
