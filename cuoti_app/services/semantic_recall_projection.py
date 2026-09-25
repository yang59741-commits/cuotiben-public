"""SQLite projection repository for semantic recall."""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from typing import Any, Callable, Sequence

from ..transactions import immediate_transaction
from .semantic_index import SemanticIndexContractError, validate_semantic_card
from .semantic_recall_contracts import (
    CARD_TERM_FIELDS,
    FIELD_WEIGHTS,
    ProjectionBuildResult,
    RECALL_ALGORITHM_VERSION,
    RECALL_DICTIONARY_VERSION,
    RECALL_WEIGHTS_VERSION,
    SemanticRecallError,
    required_text,
    sha256,
    timestamp as validate_timestamp,
)
from .semantic_recall_terms import normalized_terms


def source_file_key(value: Any) -> str:
    normalized = str(value or "").replace("\\", "/").strip().casefold()
    return "source-" + sha256(normalized)[:24] if normalized else ""


def ensure_optional_fts(db: sqlite3.Connection) -> bool:
    db.execute("SAVEPOINT semantic_recall_fts")
    try:
        db.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS semantic_recall_fts USING fts5("
            "projection_id UNINDEXED,semantic_question_id UNINDEXED,terms,tokenize='unicode61')"
        )
        db.execute("RELEASE semantic_recall_fts")
        return True
    except sqlite3.OperationalError as exc:
        db.execute("ROLLBACK TO semantic_recall_fts")
        db.execute("RELEASE semantic_recall_fts")
        message = str(exc).casefold()
        if "no such module" in message or "fts5" in message or "tokenizer" in message:
            return False
        raise


def projection_rows(
    db: sqlite3.Connection,
    snapshot_id: str,
) -> list[sqlite3.Row | tuple]:
    return db.execute(
        "SELECT si.semantic_question_id,si.card_version_id,si.content_hash,q.source_kind,"
        "q.candidate_id,q.subject,q.chapter,q.source_file,q.source_year,c.card_json "
        "FROM semantic_index_snapshot_items si "
        "JOIN semantic_questions q ON q.id=si.semantic_question_id "
        "JOIN semantic_card_versions c ON c.id=si.card_version_id "
        "WHERE si.snapshot_id=? AND q.active=1 AND q.excluded=0 AND c.status='valid' "
        "AND q.content_hash=si.content_hash AND c.content_hash=si.content_hash "
        "ORDER BY si.semantic_question_id",
        (snapshot_id,),
    ).fetchall()


def migrate_shared_terms(db: sqlite3.Connection, projection_id: str) -> None:
    """Dual-write only when an explicit P3 rehearsal schema is present."""

    from .semantic_shared_terms import (
        migrate_projection_terms,
        shared_term_schema_exists,
    )

    if shared_term_schema_exists(db):
        migrate_projection_terms(db, projection_id)


def build_recall_projection(
    db: sqlite3.Connection,
    *,
    snapshot_id: str,
    timestamp: str,
    algorithm_version: str = RECALL_ALGORITHM_VERSION,
    dictionary_version: str = RECALL_DICTIONARY_VERSION,
    weights_version: str = RECALL_WEIGHTS_VERSION,
    ensure_fts: Callable[[sqlite3.Connection], bool] = ensure_optional_fts,
    load_rows: Callable[
        [sqlite3.Connection, str], list[sqlite3.Row | tuple]
    ] = projection_rows,
    migrate_terms: Callable[[sqlite3.Connection, str], None] = migrate_shared_terms,
    caller_owns_transaction: bool = False,
) -> ProjectionBuildResult:
    snapshot_id = required_text(snapshot_id, "snapshot_id", 256)
    if not isinstance(caller_owns_transaction, bool):
        raise SemanticRecallError(
            "VALIDATION", "caller_owns_transaction 必须是布尔值"
        )
    if caller_owns_transaction and not db.in_transaction:
        raise SemanticRecallError(
            "TRANSACTION_REQUIRED", "调用方声明持有事务但当前没有活动事务"
        )
    timestamp = validate_timestamp(timestamp)
    versions = tuple(
        required_text(value, name, 128)
        for value, name in (
            (algorithm_version, "algorithm_version"),
            (dictionary_version, "dictionary_version"),
            (weights_version, "weights_version"),
        )
    )
    snapshot = db.execute(
        "SELECT status,valid_card_count FROM semantic_index_snapshots WHERE id=?",
        (snapshot_id,),
    ).fetchone()
    if snapshot is None:
        raise SemanticRecallError("SNAPSHOT_NOT_FOUND", "语义 snapshot 不存在")
    rows = load_rows(db, snapshot_id)
    if len(rows) != int(snapshot[1]):
        raise SemanticRecallError(
            "SNAPSHOT_INVALID", "snapshot 有效卡集合与投影输入不一致"
        )

    documents: list[tuple[Any, ...]] = []
    all_terms: list[tuple[int, str, str, float]] = []
    digest_documents: list[dict[str, Any]] = []
    for row in rows:
        try:
            card = validate_semantic_card(
                json.loads(row[9]), expected_candidate_id=str(row[4])
            )
        except (json.JSONDecodeError, SemanticIndexContractError) as exc:
            raise SemanticRecallError(
                "CARD_INVALID", "snapshot 包含无法投影的材料卡"
            ) from exc
        field_values = {name: getattr(card, name) for name in CARD_TERM_FIELDS}
        field_values["chapter"] = str(row[6])
        terms: list[tuple[int, str, str, float]] = []
        for field_name, value in field_values.items():
            values = value if isinstance(value, tuple) else (value,)
            seen: set[str] = set()
            for raw in values:
                for term in normalized_terms(raw):
                    if term in seen:
                        continue
                    seen.add(term)
                    terms.append(
                        (int(row[0]), field_name, term, FIELD_WEIGHTS[field_name])
                    )
        terms.sort(key=lambda item: (item[1], item[2]))
        all_terms.extend(terms)
        text_hash = sha256([(item[1], item[2], item[3]) for item in terms])
        document = (
            int(row[0]),
            int(row[1]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            card.question_type,
            card.difficulty,
            card.similarity_group,
            row[8],
            source_file_key(row[7]),
            float(card.analysis_confidence),
            str(row[2]),
            text_hash,
        )
        documents.append(document)
        digest_documents.append(
            {
                "document": document,
                "terms": [(item[1], item[2], item[3]) for item in terms],
            }
        )
    build_hash = sha256(
        {
            "snapshot_id": snapshot_id,
            "versions": versions,
            "documents": digest_documents,
        }
    )
    projection_id = "recall-" + sha256([snapshot_id, *versions, build_hash])[:32]
    existing = db.execute(
        "SELECT item_count,build_hash FROM semantic_recall_projection_versions "
        "WHERE id=? AND status='published'",
        (projection_id,),
    ).fetchone()
    if existing is not None:
        if int(existing[0]) != len(documents) or str(existing[1]) != build_hash:
            raise SemanticRecallError(
                "PROJECTION_CONFLICT", "同名召回投影内容不一致"
            )
        migrate_terms(db, projection_id)
        fts_available = (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='semantic_recall_fts'"
            ).fetchone()
            is not None
        )
        return ProjectionBuildResult(
            projection_id,
            snapshot_id,
            len(documents),
            len(all_terms),
            build_hash,
            fts_available,
            True,
        )

    transaction = (
        nullcontext() if caller_owns_transaction else immediate_transaction(db)
    )
    with transaction:
        db.execute(
            "INSERT INTO semantic_recall_projection_versions("
            "id,snapshot_id,algorithm_version,dictionary_version,weights_version,status,"
            "build_hash,item_count,created_at) VALUES(?,?,?,?,?,'building',?,?,?)",
            (
                projection_id,
                snapshot_id,
                *versions,
                build_hash,
                len(documents),
                timestamp,
            ),
        )
        db.executemany(
            "INSERT INTO semantic_recall_documents("
            "projection_id,semantic_question_id,card_version_id,source_kind,candidate_id,"
            "subject,chapter,question_type,difficulty,similarity_group,source_year,"
            "source_file_key,confidence,content_hash,normalized_text_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((projection_id, *document) for document in documents),
        )
        db.executemany(
            "INSERT INTO semantic_recall_terms("
            "projection_id,semantic_question_id,field_name,normalized_term,weight) "
            "VALUES(?,?,?,?,?)",
            ((projection_id, *term) for term in all_terms),
        )
        fts_available = ensure_fts(db)
        if fts_available:
            by_question: dict[int, list[str]] = {}
            for question_id, _field, term, _weight in all_terms:
                by_question.setdefault(question_id, []).append(term)
            db.executemany(
                "INSERT INTO semantic_recall_fts(projection_id,semantic_question_id,terms) "
                "VALUES(?,?,?)",
                (
                    (projection_id, question_id, " ".join(terms))
                    for question_id, terms in sorted(by_question.items())
                ),
            )
        db.execute(
            "UPDATE semantic_recall_projection_versions "
            "SET status='published',published_at=? WHERE id=?",
            (timestamp, projection_id),
        )
    migrate_terms(db, projection_id)
    return ProjectionBuildResult(
        projection_id,
        snapshot_id,
        len(documents),
        len(all_terms),
        build_hash,
        fts_available,
        False,
    )


def load_projection(
    db: sqlite3.Connection,
    projection_id: str,
    identities: Sequence[tuple[str, str]] | None = None,
) -> tuple[sqlite3.Row | tuple, dict[tuple[str, str], dict[str, Any]]]:
    projection = db.execute(
        "SELECT p.snapshot_id,s.source_snapshot,p.algorithm_version,p.dictionary_version,"
        "p.weights_version,p.item_count,p.build_hash FROM semantic_recall_projection_versions p "
        "JOIN semantic_index_snapshots s ON s.id=p.snapshot_id "
        "WHERE p.id=? AND p.status='published' AND s.status='complete'",
        (projection_id,),
    ).fetchone()
    if projection is None:
        raise SemanticRecallError("PROJECTION_NOT_FOUND", "已发布召回投影不存在")
    query = (
        "SELECT d.semantic_question_id,d.source_kind,d.candidate_id,d.subject,d.chapter,"
        "d.question_type,d.difficulty,d.similarity_group,d.source_year,d.source_file_key,"
        "d.confidence,d.content_hash,d.card_version_id,q.active,q.excluded,"
        "q.current_card_version_id,c.status AS card_status "
        "FROM semantic_recall_documents d "
        "JOIN semantic_questions q ON q.id=d.semantic_question_id "
        "JOIN semantic_card_versions c ON c.id=d.card_version_id "
        "WHERE d.projection_id=?"
    )
    params: list[Any] = [projection_id]
    if identities is not None:
        unique = tuple(dict.fromkeys(identities))
        if not unique:
            return projection, {}
        query += " AND (d.source_kind,d.candidate_id) IN (" + ",".join(
            "(?,?)" for _ in unique
        ) + ")"
        for source_kind, candidate_id in unique:
            params.extend((source_kind, candidate_id))
    cursor = db.execute(query, params)
    columns = [item[0] for item in cursor.description]
    documents = {
        (str(row[1]), str(row[2])): dict(zip(columns, row))
        for row in cursor.fetchall()
    }
    if identities is None and len(documents) != int(projection[5]):
        raise SemanticRecallError(
            "PROJECTION_CORRUPT", "召回投影文档数量与冻结清单不一致"
        )
    return projection, documents


def load_terms(
    db: sqlite3.Connection,
    projection_id: str,
    question_ids: Sequence[int],
    query_terms: Sequence[str] | None = None,
) -> dict[int, list[tuple[str, str, float]]]:
    result: dict[int, list[tuple[str, str, float]]] = {}
    filtered_terms = tuple(dict.fromkeys(query_terms or ()))
    if query_terms is not None and not filtered_terms:
        return result
    for start in range(0, len(question_ids), 500):
        batch = question_ids[start:start + 500]
        if not batch:
            continue
        query = (
            "SELECT semantic_question_id,field_name,normalized_term,weight "
            "FROM semantic_recall_terms WHERE projection_id=? "
            "AND semantic_question_id IN ("
            + ",".join("?" for _ in batch)
            + ")"
        )
        params: list[Any] = [projection_id, *batch]
        if query_terms is not None:
            query += " AND normalized_term IN (" + ",".join(
                "?" for _ in filtered_terms
            ) + ")"
            params.extend(filtered_terms)
        rows = db.execute(query, params).fetchall()
        for row in rows:
            result.setdefault(int(row[0]), []).append(
                (str(row[1]), str(row[2]), float(row[3]))
            )
    if not result and question_ids:
        from .semantic_shared_terms import load_shared_terms, shared_term_schema_exists

        if shared_term_schema_exists(db):
            return load_shared_terms(db, projection_id, question_ids, query_terms)
    return result


def question_states(
    db: sqlite3.Connection,
    identities: Sequence[tuple[str, str]] | None = None,
) -> dict[tuple[str, str], tuple[bool, bool]]:
    query = "SELECT source_kind,candidate_id,active,excluded FROM semantic_questions"
    params: list[Any] = []
    if identities is not None:
        unique = tuple(dict.fromkeys(identities))
        if not unique:
            return {}
        query += " WHERE (source_kind,candidate_id) IN (" + ",".join(
            "(?,?)" for _ in unique
        ) + ")"
        for source_kind, candidate_id in unique:
            params.extend((source_kind, candidate_id))
    return {
        (str(row[0]), str(row[1])): (bool(row[2]), bool(row[3]))
        for row in db.execute(query, params).fetchall()
    }


def latest_published_projection(
    db: sqlite3.Connection, *, algorithm_version: str | None = None
) -> str | None:
    query = (
        "SELECT p.id FROM semantic_recall_projection_versions p "
        "JOIN semantic_index_snapshots s ON s.id=p.snapshot_id "
        "WHERE p.status='published' AND s.status='complete' "
    )
    parameters: tuple[str, ...] = ()
    if algorithm_version:
        query += "AND p.algorithm_version=? "
        parameters = (str(algorithm_version),)
    row = db.execute(
        query + "ORDER BY s.published_at DESC,p.published_at DESC LIMIT 1",
        parameters,
    ).fetchone()
    return str(row[0]) if row else None


__all__ = [
    "build_recall_projection",
    "ensure_optional_fts",
    "latest_published_projection",
    "load_projection",
    "load_terms",
    "migrate_shared_terms",
    "projection_rows",
    "question_states",
    "source_file_key",
]
