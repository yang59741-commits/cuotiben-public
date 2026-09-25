"""Recoverable usage transfer between immutable question-bank indexes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .question_bank_repository import connect_readonly


def copy_usage(previous: Path, destination: Path) -> None:
    if not previous.is_file():
        return
    try:
        source = connect_readonly(previous)
        target = sqlite3.connect(destination)
        rows = source.execute(
            "SELECT question_id,exposed_count,selected_count,"
            "last_exposed_at,last_selected_at FROM question_usage"
        ).fetchall()
        current_ids = {
            str(row[0])
            for row in target.execute("SELECT question_id FROM questions").fetchall()
        }
        with target:
            target.executemany(
                "INSERT OR IGNORE INTO question_usage("
                "question_id,exposed_count,selected_count,"
                "last_exposed_at,last_selected_at) VALUES(?,?,?,?,?)",
                [
                    (
                        row["question_id"],
                        row["exposed_count"],
                        row["selected_count"],
                        row["last_exposed_at"],
                        row["last_selected_at"],
                    )
                    for row in rows
                    if str(row["question_id"]) in current_ids
                ],
            )
    except sqlite3.Error:
        return
    finally:
        if "source" in locals():
            source.close()
        if "target" in locals():
            target.close()


__all__ = ["copy_usage"]
