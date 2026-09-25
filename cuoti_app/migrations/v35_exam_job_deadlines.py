"""Schema 35: persist frozen exam deadlines and accumulated timing evidence."""

from __future__ import annotations

import re
import sqlite3


TABLE_NAME = "background_jobs"
COLUMN_DEFINITIONS = (
    "deadline_at TEXT",
    "deadline_policy_mode TEXT CHECK(deadline_policy_mode IS NULL OR deadline_policy_mode IN ('observe','enforce'))",
    "deadline_policy_version TEXT",
    "exam_timing_json TEXT NOT NULL DEFAULT '{}'",
)
EXPECTED_COLUMNS = {
    "deadline_at": ("TEXT", 0, None),
    "deadline_policy_mode": ("TEXT", 0, None),
    "deadline_policy_version": ("TEXT", 0, None),
    "exam_timing_json": ("TEXT", 1, "'{}'"),
}
EXPECTED_MODE_CHECK = (
    "CHECK(DEADLINE_POLICY_MODEISNULLORDEADLINE_POLICY_MODEIN('OBSERVE','ENFORCE'))"
)


def _normalized_sql(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def _columns(db: sqlite3.Connection) -> dict[str, tuple[str, int, str | None]]:
    return {
        str(row[1]): (
            str(row[2]).upper(),
            int(row[3]),
            None if row[4] is None else str(row[4]),
        )
        for row in db.execute(f'PRAGMA table_info("{TABLE_NAME}")').fetchall()
    }


def schema_issues(db: sqlite3.Connection) -> list[str]:
    master = db.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (TABLE_NAME,)
    ).fetchone()
    if master is None or str(master[0]) != "table":
        return [f"版本 35 缺少表 {TABLE_NAME}"]
    actual = _columns(db)
    issues: list[str] = []
    for name, expected in EXPECTED_COLUMNS.items():
        if name not in actual:
            issues.append(f"版本 35 的表 {TABLE_NAME} 缺少字段 {name}")
        elif actual[name] != expected:
            issues.append(f"版本 35 的表 {TABLE_NAME} 字段 {name} 定义不兼容")
    if (
        "deadline_policy_mode" in actual
        and EXPECTED_MODE_CHECK not in _normalized_sql(master[1])
    ):
        issues.append(
            f"版本 35 的表 {TABLE_NAME} 缺少 deadline_policy_mode 约束"
        )
    return issues


def migrate_v34_to_v35(db: sqlite3.Connection) -> None:
    existing = _columns(db)
    conflicting = [
        name
        for name, expected in EXPECTED_COLUMNS.items()
        if name in existing and existing[name] != expected
    ]
    if conflicting:
        raise RuntimeError(
            "；".join(
                f"版本 35 的表 {TABLE_NAME} 字段 {name} 定义不兼容"
                for name in conflicting
            )
        )
    for definition in COLUMN_DEFINITIONS:
        name = definition.split()[0]
        if name not in existing:
            db.execute(f'ALTER TABLE "{TABLE_NAME}" ADD COLUMN {definition}')


__all__ = ["migrate_v34_to_v35", "schema_issues"]
