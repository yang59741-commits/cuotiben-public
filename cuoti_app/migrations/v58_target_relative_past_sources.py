"""Keep local semantic identities while allowing their other-institution role."""
from . import v42_exam_generation_stage4_facts as facts

FACT_ITEMS_SQL = facts.CREATE_TABLE_SQL[facts.FACT_ITEMS].replace(
    "source_kind IN ('ordinary','past_exam_other')",
    "source_kind IN ('ordinary','past_exam_other','past_exam_local')",
).replace(
    "source_kind='past_exam_other' AND chapter IS NULL",
    "source_kind IN ('past_exam_other','past_exam_local') AND chapter IS NULL",
)


def migrate_v57_to_v58(db):
    # Only the small immutable fact table is rebuilt, not the application DB.
    db.execute(f"CREATE TEMP TABLE stage4_source_role_rows AS SELECT * FROM {facts.FACT_ITEMS}")
    for name, (table, _) in facts.TRIGGER_SQL.items():
        if table == facts.FACT_ITEMS:
            db.execute(f'DROP TRIGGER "{name}"')
    db.execute(f"DROP TABLE {facts.FACT_ITEMS}")
    db.execute(FACT_ITEMS_SQL)
    db.execute(f"INSERT INTO {facts.FACT_ITEMS} SELECT * FROM stage4_source_role_rows")
    db.execute("DROP TABLE stage4_source_role_rows")
    for table, sql in facts.TRIGGER_SQL.values():
        if table == facts.FACT_ITEMS:
            db.execute(sql)


def schema_issues(db):
    return facts.schema_issues(db, fact_items_sql=FACT_ITEMS_SQL)


migrate_v57_to_v58.validation_tables = (facts.FACT_ITEMS,)
