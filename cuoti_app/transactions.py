"""Explicit SQLite transaction helpers.

Ordinary writes use ``with db:``. Workflows that require the write lock before
reading use :func:`begin_immediate` and remain responsible for commit/rollback.
"""

from contextlib import contextmanager


def begin_immediate(db) -> None:
    """Acquire a SQLite write lock, refusing accidental nested transactions."""
    if db.in_transaction:
        raise RuntimeError("cannot begin immediate inside an active transaction")
    db.execute("BEGIN IMMEDIATE")


@contextmanager
def immediate_transaction(db):
    """Own one immediate transaction, including commit-failure rollback."""

    begin_immediate(db)
    try:
        yield
    except BaseException:
        db.rollback()
        raise
    try:
        db.commit()
    except BaseException:
        db.rollback()
        raise
