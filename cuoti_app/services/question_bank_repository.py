"""SQLite storage and integrity primitives for the question-bank index."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .question_bank_contracts import QuestionBankError
from .question_bank_parser import Candidate
from .question_bank_sources import manifest_snapshot_id


def semantic_catalog_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    queries = (
        (
            "sources",
            "SELECT source_file,subject,chapter,sha256,size,status,parser_kind,"
            "parsed_count,indexed_count,excluded_count,COALESCE(error,'') "
            "FROM sources ORDER BY subject,chapter,source_file",
        ),
        (
            "questions",
            "SELECT question_id,content_hash,content,char_count,token_estimate "
            "FROM questions ORDER BY question_id",
        ),
        (
            "question_sources",
            "SELECT question_id,subject,chapter,source_file,source_number,"
            "source_ordinal,marker FROM question_sources "
            "ORDER BY subject,chapter,source_file,source_ordinal,question_id",
        ),
        (
            "exclusions",
            "SELECT subject,chapter,source_file,source_number,source_ordinal,reason,"
            "COALESCE(content_hash,'') FROM exclusions "
            "ORDER BY subject,chapter,source_file,source_ordinal,reason,content_hash",
        ),
    )
    for label, query in queries:
        digest.update(label.encode("ascii"))
        digest.update(b"\0")
        for row in connection.execute(query):
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def is_sha256(value: str | None) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def published_index_integrity(
    connection: sqlite3.Connection,
    metadata: dict[str, str],
) -> bool:
    catalog_digest = metadata.get("catalog_semantic_digest")
    snapshot_id = metadata.get("snapshot_id")
    semantic_digest = metadata.get("semantic_catalog_digest")
    if not all(
        is_sha256(value) for value in (catalog_digest, snapshot_id, semantic_digest)
    ):
        return False
    rows = connection.execute(
        "SELECT subject,chapter,source_file,sha256,size,status FROM sources "
        "ORDER BY subject,chapter,source_file"
    ).fetchall()
    manifest = [
        {
            "subject": row["subject"],
            "chapter": row["chapter"],
            "source_file": row["source_file"],
            "sha256": row["sha256"],
            "size": row["size"],
            "status": "missing" if row["status"] == "missing" else "available",
        }
        for row in rows
    ]
    return (
        snapshot_id
        == manifest_snapshot_id(
            manifest,
            catalog_semantic_digest=str(catalog_digest),
        )
        and semantic_digest == semantic_catalog_digest(connection)
    )


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise QuestionBankError("题库索引不存在，请先构建索引")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE metadata(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE sources(
            source_file TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            size INTEGER NOT NULL,
            status TEXT NOT NULL,
            parser_kind TEXT,
            parsed_count INTEGER NOT NULL DEFAULT 0,
            indexed_count INTEGER NOT NULL DEFAULT 0,
            excluded_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            PRIMARY KEY(source_file,subject,chapter)
        );
        CREATE TABLE questions(
            question_id TEXT PRIMARY KEY,
            content_hash TEXT UNIQUE NOT NULL,
            content TEXT NOT NULL,
            char_count INTEGER NOT NULL,
            token_estimate INTEGER NOT NULL
        );
        CREATE TABLE question_sources(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_number INTEGER NOT NULL,
            source_ordinal INTEGER NOT NULL,
            marker TEXT NOT NULL,
            FOREIGN KEY(question_id) REFERENCES questions(question_id),
            UNIQUE(subject,chapter,source_file,source_ordinal)
        );
        CREATE TABLE exclusions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            chapter TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_number INTEGER,
            source_ordinal INTEGER,
            reason TEXT NOT NULL,
            content_hash TEXT
        );
        CREATE TABLE question_usage(
            question_id TEXT PRIMARY KEY,
            exposed_count INTEGER NOT NULL DEFAULT 0,
            selected_count INTEGER NOT NULL DEFAULT 0,
            last_exposed_at TEXT,
            last_selected_at TEXT,
            FOREIGN KEY(question_id) REFERENCES questions(question_id)
                ON DELETE CASCADE
        );
        CREATE INDEX idx_question_sources_chapter
            ON question_sources(subject,chapter,question_id);
        CREATE INDEX idx_question_sources_question
            ON question_sources(question_id);
        CREATE INDEX idx_exclusions_reason
            ON exclusions(reason);
        """
    )


def record_exclusion(
    connection: sqlite3.Connection,
    *,
    subject: str,
    chapter: str,
    source_file: str,
    reason: str,
    candidate: Candidate | None = None,
    content_hash: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO exclusions(subject,chapter,source_file,source_number,"
        "source_ordinal,reason,content_hash) VALUES(?,?,?,?,?,?,?)",
        (
            subject,
            chapter,
            source_file,
            candidate.source_number if candidate else None,
            candidate.source_ordinal if candidate else None,
            reason,
            content_hash,
        ),
    )


__all__ = [
    "connect_readonly",
    "create_schema",
    "is_sha256",
    "published_index_integrity",
    "record_exclusion",
    "semantic_catalog_digest",
]
