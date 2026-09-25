"""Persist local D2 selection and its native quality/publication state."""


def migrate_v50_to_v51(db):
    from ..services.exam_generation_d2_publication import SCHEMA_STATEMENTS
    from ..services.exam_generation_stage6_d2_repository import d2_candidate_schema_statements

    for statement in (*d2_candidate_schema_statements(), *SCHEMA_STATEMENTS):
        db.execute(statement)


def schema_issues(db):
    from ..services.exam_generation_d2_publication import D2_TABLE
    from ..services.exam_generation_stage6_d2_repository import d2_candidate_schema_issues

    issues = d2_candidate_schema_issues(db)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (D2_TABLE,)).fetchone() is None:
        issues.append("D2 quality publication table missing")
    return issues
