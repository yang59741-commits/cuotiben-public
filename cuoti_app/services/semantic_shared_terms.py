"""Copy-only shared semantic-term projection used by the P3 rehearsal.

The schema is deliberately not registered as an application migration yet.
It can be created explicitly on an isolated database, populated in bounded
batches, compared with the legacy projection, and resumed after interruption.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any

from ..migrations.v31_semantic_shared_terms import (
    SHARED_TERM_SCHEMA_STATEMENTS,
    SHARED_TERM_TABLES,
)
from ..transactions import immediate_transaction


SHARED_TERMS_SCHEMA = "semantic-shared-terms-v1"
_TABLES = SHARED_TERM_TABLES


class SemanticSharedTermsError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def shared_term_schema_exists(db) -> bool:
    tables = {
        str(row[0])
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'semantic_recall_%'"
        ).fetchall()
    }
    return set(_TABLES) <= tables


def _transaction(db, caller_owns_transaction: bool):
    if not isinstance(caller_owns_transaction, bool):
        raise SemanticSharedTermsError(
            "VALIDATION", "caller_owns_transaction 必须是布尔值"
        )
    if caller_owns_transaction:
        if not db.in_transaction:
            raise SemanticSharedTermsError(
                "TRANSACTION_REQUIRED", "调用方声明持有事务但当前没有活动事务"
            )
        return nullcontext()
    return immediate_transaction(db)


def ensure_shared_term_schema(db, *, caller_owns_transaction: bool = False) -> None:
    with _transaction(db, caller_owns_transaction):
        for statement in SHARED_TERM_SCHEMA_STATEMENTS:
            db.execute(statement)


def _term_rows_digest(rows) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        value = (int(row[0]), str(row[1]), str(row[2]), float(row[3]))
        digest.update(_canonical(value).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def legacy_projection_terms_digest(db, projection_id: str) -> tuple[int, str]:
    rows = db.execute(
        "SELECT semantic_question_id,field_name,normalized_term,weight "
        "FROM semantic_recall_terms WHERE projection_id=? "
        "ORDER BY semantic_question_id,field_name,normalized_term",
        (projection_id,),
    )
    return _term_rows_digest(rows)


def shared_projection_terms_digest(db, projection_id: str) -> tuple[int, str]:
    if not shared_term_schema_exists(db):
        return (0, hashlib.sha256(b"").hexdigest())
    rows = db.execute(
        "SELECT pts.semantic_question_id,i.field_name,d.normalized_term,w.weight "
        "FROM semantic_recall_projection_term_sets pts "
        "JOIN semantic_recall_term_set_items i ON i.term_set_id=pts.term_set_id "
        "JOIN semantic_recall_term_dictionary d ON d.id=i.term_id "
        "JOIN semantic_recall_weight_profiles w "
        "ON w.weights_version=pts.weights_version AND w.field_name=i.field_name "
        "WHERE pts.projection_id=? "
        "ORDER BY pts.semantic_question_id,i.field_name,d.normalized_term",
        (projection_id,),
    )
    return _term_rows_digest(rows)


def _dictionary_ids(db, terms: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for start in range(0, len(terms), 500):
        batch = terms[start : start + 500]
        if not batch:
            continue
        rows = db.execute(
            "SELECT normalized_term,id FROM semantic_recall_term_dictionary WHERE normalized_term IN ("
            + ",".join("?" for _ in batch)
            + ")",
            list(batch),
        ).fetchall()
        result.update({str(row[0]): int(row[1]) for row in rows})
    if len(result) != len(terms):
        raise SemanticSharedTermsError("DICTIONARY_INCOMPLETE", "共享词典写入不完整")
    return result


def _term_set_ids(db, logical_keys: Sequence[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for start in range(0, len(logical_keys), 500):
        batch = logical_keys[start : start + 500]
        if not batch:
            continue
        rows = db.execute(
            "SELECT logical_key,id FROM semantic_recall_term_sets WHERE logical_key IN ("
            + ",".join("?" for _ in batch)
            + ")",
            list(batch),
        ).fetchall()
        result.update({str(row[0]): int(row[1]) for row in rows})
    if len(result) != len(logical_keys):
        raise SemanticSharedTermsError("TERM_SET_INCOMPLETE", "共享 term set 写入不完整")
    return result


def migrate_projection_terms(
    db,
    projection_id: str,
    *,
    batch_size: int = 250,
    progress: Callable[[dict[str, int]], None] | None = None,
    caller_owns_transaction: bool = False,
) -> dict[str, Any]:
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    ensure_shared_term_schema(
        db, caller_owns_transaction=caller_owns_transaction
    )
    projection = db.execute(
        "SELECT dictionary_version,weights_version,item_count,status "
        "FROM semantic_recall_projection_versions WHERE id=?",
        (projection_id,),
    ).fetchone()
    if projection is None or str(projection[3]) != "published":
        raise SemanticSharedTermsError("PROJECTION_UNAVAILABLE", "共享词项迁移要求已发布投影")
    dictionary_version = str(projection[0])
    weights_version = str(projection[1])
    item_count = int(projection[2])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _transaction(db, caller_owns_transaction):
        db.execute(
            "INSERT OR IGNORE INTO semantic_recall_shared_migrations("
            "projection_id,schema_name,status,started_at,updated_at) "
            "VALUES(?,?,'running',?,?)",
            (projection_id, SHARED_TERMS_SCHEMA, now, now),
        )
        db.execute(
            "UPDATE semantic_recall_shared_migrations SET status='running',updated_at=? "
            "WHERE projection_id=? AND status<>'complete'",
            (now, projection_id),
        )
    state = db.execute(
        "SELECT status,last_semantic_question_id,migrated_documents "
        "FROM semantic_recall_shared_migrations WHERE projection_id=?",
        (projection_id,),
    ).fetchone()
    if str(state[0]) == "complete":
        legacy = legacy_projection_terms_digest(db, projection_id)
        shared = shared_projection_terms_digest(db, projection_id)
        if legacy[0] and legacy != shared:
            raise SemanticSharedTermsError("DIGEST_MISMATCH", "共享词项与旧投影不一致")
        return {
            "projection_id": projection_id,
            "documents": int(state[2]),
            "legacy_terms": legacy[0],
            "shared_terms": shared[0],
            "digest": shared[1],
            "reused": True,
        }
    cursor = int(state[1])
    migrated = int(state[2])
    while True:
        documents = db.execute(
            "SELECT semantic_question_id,card_version_id FROM semantic_recall_documents "
            "WHERE projection_id=? AND semantic_question_id>? "
            "ORDER BY semantic_question_id LIMIT ?",
            (projection_id, cursor, batch_size),
        ).fetchall()
        if not documents:
            break
        question_ids = [int(row[0]) for row in documents]
        term_rows = db.execute(
            "SELECT semantic_question_id,field_name,normalized_term,weight "
            "FROM semantic_recall_terms WHERE projection_id=? AND semantic_question_id IN ("
            + ",".join("?" for _ in question_ids)
            + ") ORDER BY semantic_question_id,field_name,normalized_term",
            [projection_id, *question_ids],
        ).fetchall()
        by_question: dict[int, list[tuple[str, str, float]]] = {
            question_id: [] for question_id in question_ids
        }
        for row in term_rows:
            by_question[int(row[0])].append((str(row[1]), str(row[2]), float(row[3])))
        unique_terms = sorted({term for rows in by_question.values() for _field, term, _weight in rows})
        batch_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with _transaction(db, caller_owns_transaction):
            db.executemany(
                "INSERT OR IGNORE INTO semantic_recall_term_dictionary(normalized_term) VALUES(?)",
                ((term,) for term in unique_terms),
            )
            term_ids = _dictionary_ids(db, unique_terms)
            weight_pairs = sorted(
                {
                    (field_name, weight)
                    for terms in by_question.values()
                    for field_name, _term, weight in terms
                }
            )
            db.executemany(
                "INSERT OR IGNORE INTO semantic_recall_weight_profiles("
                "weights_version,field_name,weight) VALUES(?,?,?)",
                (
                    (weights_version, field_name, weight)
                    for field_name, weight in weight_pairs
                ),
            )
            stored_weights = {
                str(row[0]): float(row[1])
                for row in db.execute(
                    "SELECT field_name,weight FROM semantic_recall_weight_profiles "
                    "WHERE weights_version=?",
                    (weights_version,),
                ).fetchall()
            }
            if any(stored_weights.get(field_name) != weight for field_name, weight in weight_pairs):
                raise SemanticSharedTermsError(
                    "WEIGHT_CONFLICT", "同一权重版本包含冲突字段权重"
                )
            term_set_rows = []
            term_set_specs = []
            for row in documents:
                question_id = int(row[0])
                card_version_id = int(row[1])
                terms = by_question[question_id]
                logical_hash = _sha256([(field, term) for field, term, _weight in terms])
                logical_key = "termset-" + _sha256(
                    [card_version_id, dictionary_version, logical_hash]
                )[:40]
                term_set_rows.append(
                    (
                        logical_key, card_version_id, dictionary_version,
                        logical_hash, len(terms), batch_now,
                    )
                )
                term_set_specs.append((question_id, logical_key, terms))
            db.executemany(
                "INSERT OR IGNORE INTO semantic_recall_term_sets("
                "logical_key,card_version_id,dictionary_version,logical_hash,term_count,created_at) "
                "VALUES(?,?,?,?,?,?)",
                term_set_rows,
            )
            set_ids = _term_set_ids(
                db, sorted({logical_key for _question_id, logical_key, _terms in term_set_specs})
            )
            item_rows = [
                (set_ids[logical_key], field_name, term_ids[term])
                for _question_id, logical_key, terms in term_set_specs
                for field_name, term, _weight in terms
            ]
            mapping_rows = [
                (projection_id, question_id, set_ids[logical_key], weights_version)
                for question_id, logical_key, _terms in term_set_specs
            ]
            db.executemany(
                "INSERT OR IGNORE INTO semantic_recall_term_set_items("
                "term_set_id,field_name,term_id) VALUES(?,?,?)",
                item_rows,
            )
            db.executemany(
                "INSERT OR REPLACE INTO semantic_recall_projection_term_sets("
                "projection_id,semantic_question_id,term_set_id,weights_version) "
                "VALUES(?,?,?,?)",
                mapping_rows,
            )
            cursor = question_ids[-1]
            migrated += len(documents)
            db.execute(
                "UPDATE semantic_recall_shared_migrations SET "
                "last_semantic_question_id=?,migrated_documents=?,updated_at=? "
                "WHERE projection_id=?",
                (cursor, migrated, batch_now, projection_id),
            )
        if progress:
            progress({"documents": migrated, "total": item_count, "cursor": cursor})
    mapped = int(
        db.execute(
            "SELECT COUNT(*) FROM semantic_recall_projection_term_sets WHERE projection_id=?",
            (projection_id,),
        ).fetchone()[0]
    )
    if mapped != item_count or migrated != item_count:
        raise SemanticSharedTermsError("DOCUMENT_COUNT_MISMATCH", "共享 term set 文档覆盖不完整")
    legacy = legacy_projection_terms_digest(db, projection_id)
    shared = shared_projection_terms_digest(db, projection_id)
    if legacy != shared:
        raise SemanticSharedTermsError("DIGEST_MISMATCH", "共享词项与旧投影不一致")
    finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _transaction(db, caller_owns_transaction):
        db.execute(
            "UPDATE semantic_recall_shared_migrations SET status='complete',"
            "legacy_term_count=?,legacy_digest=?,shared_digest=?,updated_at=? "
            "WHERE projection_id=?",
            (legacy[0], legacy[1], shared[1], finished, projection_id),
        )
    return {
        "projection_id": projection_id,
        "documents": migrated,
        "legacy_terms": legacy[0],
        "shared_terms": shared[0],
        "digest": legacy[1],
        "reused": False,
    }


def load_shared_terms(
    db,
    projection_id: str,
    question_ids: Sequence[int],
    query_terms: Sequence[str] | None = None,
) -> dict[int, list[tuple[str, str, float]]]:
    result: dict[int, list[tuple[str, str, float]]] = {}
    filtered_terms = tuple(dict.fromkeys(query_terms or ()))
    if query_terms is not None and not filtered_terms:
        return result
    for start in range(0, len(question_ids), 500):
        batch = question_ids[start : start + 500]
        if not batch:
            continue
        query = (
            "SELECT pts.semantic_question_id,i.field_name,d.normalized_term,w.weight "
            "FROM semantic_recall_projection_term_sets pts "
            "JOIN semantic_recall_term_set_items i ON i.term_set_id=pts.term_set_id "
            "JOIN semantic_recall_term_dictionary d ON d.id=i.term_id "
            "JOIN semantic_recall_weight_profiles w "
            "ON w.weights_version=pts.weights_version AND w.field_name=i.field_name "
            "WHERE pts.projection_id=? AND pts.semantic_question_id IN ("
            + ",".join("?" for _ in batch)
            + ")"
        )
        params: list[Any] = [projection_id, *batch]
        if query_terms is not None:
            query += " AND d.normalized_term IN (" + ",".join("?" for _ in filtered_terms) + ")"
            params.extend(filtered_terms)
        query += " ORDER BY pts.semantic_question_id,i.field_name,d.normalized_term"
        for row in db.execute(query, params).fetchall():
            result.setdefault(int(row[0]), []).append(
                (str(row[1]), str(row[2]), float(row[3]))
            )
    return result


def remove_verified_legacy_terms(db, projection_id: str) -> int:
    legacy = legacy_projection_terms_digest(db, projection_id)
    shared = shared_projection_terms_digest(db, projection_id)
    if legacy[0] == 0 or legacy != shared:
        raise SemanticSharedTermsError("VERIFICATION_REQUIRED", "旧/新词项尚未通过等价校验")
    with immediate_transaction(db):
        deleted = db.execute(
            "DELETE FROM semantic_recall_terms WHERE projection_id=?", (projection_id,)
        ).rowcount
    return int(deleted)
