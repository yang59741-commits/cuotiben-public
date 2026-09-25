"""Exact M_once eligibility and caller-owned publication consumption."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..migrations.v53_exam_generation_priority_once import PRIORITY_ONCE_CONSUMPTIONS
from .exam_generation_mistake_relations import (
    RELATION_VERSION,
    relation_content_fingerprint,
    relation_index,
)
from .exam_generation_stage5_solver import FrozenCandidateEdgeGraph
from .exam_repetition_policy import normalize_question_content


_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_MODES = frozenset({"free", "mistakes", "past"})


class PriorityOnceError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.code = "exam_priority_once_failed"
        self.reason = reason
        self.status_code = 409
        self.retryable = False


class PriorityOnceConflictError(PriorityOnceError):
    def __init__(self, conflicts: Iterable["PriorityOnceConsumption"]) -> None:
        self.conflicts = tuple(conflicts)
        self.keys = tuple(value.key for value in self.conflicts)
        super().__init__(
            "priority_once_already_consumed_by_other_job",
            "一次性优先资格已被其他组卷任务消费",
        )


@dataclass(frozen=True, slots=True, order=True)
class PriorityOnceKey:
    subject_id: int
    mode: str
    candidate_content_hash: str
    source_mistake_public_id: str
    relation_content_fingerprint: str


@dataclass(frozen=True, slots=True)
class PriorityOnceConsumption:
    key: PriorityOnceKey
    job_id: str
    exam_id: int
    publication_hash: str
    consumed_at: str


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PriorityOnceError(f"{field}_invalid", f"{field} must be non-empty text")
    return value


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH_RE.fullmatch(value) is None:
        raise PriorityOnceError(f"{field}_invalid", f"{field} must be a lowercase sha256 hash")
    return value


def _scope(subject_id: Any, mode: Any) -> tuple[int, str]:
    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
        raise PriorityOnceError("subject_id_invalid", "subject_id must be a positive integer")
    if mode not in _MODES:
        raise PriorityOnceError("mode_invalid", "mode is not supported")
    return subject_id, mode


def _graph_payload(graph: FrozenCandidateEdgeGraph) -> Mapping[str, Any]:
    if not isinstance(graph, FrozenCandidateEdgeGraph):
        raise PriorityOnceError("candidate_graph_invalid", "a frozen candidate graph is required")
    payload = graph.to_dict()
    if payload.get("contract_version") != graph.contract_version:
        raise PriorityOnceError("candidate_graph_identity_invalid", "candidate graph identity changed")
    candidates, edges = payload.get("candidates"), payload.get("edges")
    if not isinstance(candidates, Mapping) or not isinstance(edges, list):
        raise PriorityOnceError("candidate_graph_shape_invalid", "candidate graph shape is invalid")
    return payload


def _relation_records(snapshot: Mapping[str, Any], *, subject_id: int, mode: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(snapshot, Mapping) or snapshot.get("mode") != mode:
        raise PriorityOnceError("mistake_relation_scope_invalid", "frozen mistake relation mode changed")
    if snapshot.get("allowed_sources") != ["mistakes"]:
        raise PriorityOnceError("mistake_relation_source_invalid", "frozen mistake relation source changed")
    payload = snapshot.get("payload")
    if (
        not isinstance(payload, Mapping)
        or payload.get("version") != RELATION_VERSION
        or payload.get("subject_id") != subject_id
    ):
        raise PriorityOnceError("mistake_relation_identity_invalid", "frozen mistake relation identity changed")
    if payload.get("state") != "usable":
        return {}
    # The shared parser rejects duplicate, malformed, or partial lineage rows.
    index = relation_index(snapshot)
    if index is None:
        raise PriorityOnceError("mistake_relation_evidence_invalid", "frozen mistake relation evidence is invalid")
    return index


def derive_priority_once_keys(
    *,
    subject_id: int,
    mode: str,
    stage4: Mapping[str, Any],
    graph: FrozenCandidateEdgeGraph,
    mistake_relations: Mapping[str, Any],
) -> dict[str, tuple[PriorityOnceKey, ...]]:
    """Return exact keys only for candidates that have a hard-compatible edge."""

    subject_id, mode = _scope(subject_id, mode)
    if not isinstance(stage4, Mapping) or not isinstance(stage4.get("candidates"), list):
        raise PriorityOnceError("candidate_facts_invalid", "frozen candidate facts are invalid")
    graph_payload = _graph_payload(graph)
    relation_ids = _relation_records(mistake_relations, subject_id=subject_id, mode=mode)
    if not relation_ids:
        return {}

    hard_candidate_ids: set[str] = set()
    graph_content_hashes: dict[str, str] = {}
    for edge in graph_payload["edges"]:
        if not isinstance(edge, Mapping):
            raise PriorityOnceError("candidate_edge_invalid", "candidate graph edge is invalid")
        candidate_id = _text(edge.get("candidate_id"), "candidate_id")
        content_hash = _hash(edge.get("content_hash"), "candidate_content_hash")
        prior = graph_content_hashes.setdefault(candidate_id, content_hash)
        if prior != content_hash:
            raise PriorityOnceError("candidate_content_identity_drift", "candidate content identity changed between edges")
        hard_candidate_ids.add(candidate_id)

    stage4_by_id: dict[str, Mapping[str, Any]] = {}
    for raw in stage4["candidates"]:
        if not isinstance(raw, Mapping):
            raise PriorityOnceError("candidate_row_invalid", "candidate fact row is invalid")
        candidate_id = _text(raw.get("candidate_id"), "candidate_id")
        if candidate_id in stage4_by_id:
            raise PriorityOnceError("candidate_id_duplicate", "candidate identity is not unique")
        stage4_by_id[candidate_id] = raw

    result: dict[str, tuple[PriorityOnceKey, ...]] = {}
    for candidate_id in sorted(hard_candidate_ids):
        raw = stage4_by_id.get(candidate_id)
        if raw is None:
            raise PriorityOnceError("candidate_fact_missing", "hard-compatible candidate fact is missing")
        content_hash = _hash(raw.get("content_hash"), "candidate_content_hash")
        if content_hash != graph_content_hashes[candidate_id]:
            raise PriorityOnceError("candidate_content_identity_drift", "candidate content identity changed after graph freeze")
        snapshot = raw.get("content_snapshot")
        try:
            snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        except (TypeError, ValueError):
            snapshot = None
        if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("content"), str):
            continue
        # Equality is not provable while auxiliary conditions, options, or figures exist.
        if any(snapshot.get(field) for field in ("conditions", "options", "figure_references")):
            continue
        content = snapshot["content"]
        if not normalize_question_content(content):
            continue
        relation_fingerprint = relation_content_fingerprint(content)
        public_ids = relation_ids.get(relation_fingerprint, ())
        if public_ids:
            result[candidate_id] = tuple(
                PriorityOnceKey(subject_id, mode, content_hash, public_id, relation_fingerprint)
                for public_id in public_ids
            )
    return result


def read_unconsumed_priority_once(
    db,
    *,
    subject_id: int,
    mode: str,
    stage4: Mapping[str, Any],
    graph: FrozenCandidateEdgeGraph,
    mistake_relations: Mapping[str, Any],
) -> dict[str, tuple[PriorityOnceKey, ...]]:
    derived = derive_priority_once_keys(
        subject_id=subject_id,
        mode=mode,
        stage4=stage4,
        graph=graph,
        mistake_relations=mistake_relations,
    )
    result: dict[str, tuple[PriorityOnceKey, ...]] = {}
    for candidate_id, keys in derived.items():
        remaining = tuple(key for key in keys if _read_consumption(db, key) is None)
        if remaining:
            result[candidate_id] = remaining
    return result


def _row_to_consumption(row) -> PriorityOnceConsumption:
    key = PriorityOnceKey(
        int(row["subject_id"]), str(row["mode"]), str(row["candidate_content_hash"]),
        str(row["source_mistake_public_id"]), str(row["relation_content_fingerprint"]),
    )
    return PriorityOnceConsumption(
        key, str(row["job_id"]), int(row["exam_id"]),
        str(row["publication_hash"]), str(row["consumed_at"]),
    )


def _read_consumption(db, key: PriorityOnceKey) -> PriorityOnceConsumption | None:
    row = db.execute(
        f"SELECT * FROM {PRIORITY_ONCE_CONSUMPTIONS} WHERE "
        "subject_id=? AND mode=? AND candidate_content_hash=? AND "
        "source_mistake_public_id=? AND relation_content_fingerprint=?",
        (
            key.subject_id, key.mode, key.candidate_content_hash,
            key.source_mistake_public_id, key.relation_content_fingerprint,
        ),
    ).fetchone()
    return None if row is None else _row_to_consumption(row)


def _validated_keys(keys: Iterable[PriorityOnceKey]) -> tuple[PriorityOnceKey, ...]:
    try:
        ordered = tuple(sorted(set(keys)))
    except TypeError as exc:
        raise PriorityOnceError("priority_once_keys_invalid", "priority keys are invalid") from exc
    if not ordered or any(not isinstance(key, PriorityOnceKey) for key in ordered):
        raise PriorityOnceError("priority_once_keys_invalid", "at least one priority key is required")
    for key in ordered:
        _scope(key.subject_id, key.mode)
        _hash(key.candidate_content_hash, "candidate_content_hash")
        _text(key.source_mistake_public_id, "source_mistake_public_id")
        _hash(key.relation_content_fingerprint, "relation_content_fingerprint")
    return ordered


def assert_priority_once_available(db, *, keys: Iterable[PriorityOnceKey], job_id: str) -> None:
    """Read-only early conflict check; publication consume remains authoritative."""

    ordered = _validated_keys(keys)
    job_id = _text(job_id, "job_id")
    conflicts = [
        value for key in ordered
        if (value := _read_consumption(db, key)) is not None and value.job_id != job_id
    ]
    if conflicts:
        raise PriorityOnceConflictError(conflicts)


def verify_priority_once_consumed(
    db,
    *,
    keys: Iterable[PriorityOnceKey],
    publication_hash: str,
    job_id: str,
    exam_id: int,
) -> None:
    """Verify recovery provenance without writing or recreating consumption."""

    ordered = _validated_keys(keys)
    publication_hash = _hash(publication_hash, "publication_hash")
    job_id = _text(job_id, "job_id")
    if isinstance(exam_id, bool) or not isinstance(exam_id, int) or exam_id < 1:
        raise PriorityOnceError("exam_id_invalid", "exam_id must be a positive integer")
    existing = [_read_consumption(db, key) for key in ordered]
    if any(value is None for value in existing):
        raise PriorityOnceError(
            "priority_once_consumption_missing",
            "published priority consumption is incomplete",
        )
    conflicts = [value for value in existing if value is not None and value.job_id != job_id]
    if conflicts:
        raise PriorityOnceConflictError(conflicts)
    if any(
        value is not None
        and (value.publication_hash != publication_hash or value.exam_id != exam_id)
        for value in existing
    ):
        raise PriorityOnceError(
            "priority_once_consumption_identity_drift",
            "published priority consumption provenance changed",
        )


def consume_priority_once_in_transaction(
    db,
    *,
    keys: Iterable[PriorityOnceKey],
    publication_hash: str,
    job_id: str,
    exam_id: int,
    consumed_at: str,
) -> tuple[PriorityOnceConsumption, ...]:
    """Consume all keys inside the caller's publication transaction, or none."""

    if not bool(getattr(db, "in_transaction", False)):
        raise PriorityOnceError("publication_transaction_required", "priority consumption requires the publication transaction")
    publication_hash = _hash(publication_hash, "publication_hash")
    job_id = _text(job_id, "job_id")
    consumed_at = _text(consumed_at, "consumed_at")
    if isinstance(exam_id, bool) or not isinstance(exam_id, int) or exam_id < 1:
        raise PriorityOnceError("exam_id_invalid", "exam_id must be a positive integer")
    ordered = _validated_keys(keys)

    savepoint = "exam_priority_once_consume"
    db.execute(f"SAVEPOINT {savepoint}")
    try:
        existing = [value for key in ordered if (value := _read_consumption(db, key)) is not None]
        conflicts = [value for value in existing if value.job_id != job_id]
        if conflicts:
            raise PriorityOnceConflictError(conflicts)
        drifted = [
            value for value in existing
            if value.exam_id != exam_id or value.publication_hash != publication_hash
        ]
        if drifted:
            raise PriorityOnceError(
                "same_job_publication_identity_drift",
                "the same job cannot consume a key under a different publication",
            )
        existing_keys = {value.key for value in existing}
        new_values = [
            PriorityOnceConsumption(key, job_id, exam_id, publication_hash, consumed_at)
            for key in ordered if key not in existing_keys
        ]
        db.executemany(
            f"INSERT INTO {PRIORITY_ONCE_CONSUMPTIONS} ("
            "subject_id,mode,candidate_content_hash,source_mistake_public_id,"
            "relation_content_fingerprint,job_id,exam_id,publication_hash,consumed_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            [
                (
                    value.key.subject_id, value.key.mode, value.key.candidate_content_hash,
                    value.key.source_mistake_public_id, value.key.relation_content_fingerprint,
                    value.job_id, value.exam_id, value.publication_hash, value.consumed_at,
                )
                for value in new_values
            ],
        )
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
    except sqlite3.IntegrityError as exc:
        db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
        conflicts = [value for key in ordered if (value := _read_consumption(db, key)) is not None and value.job_id != job_id]
        if conflicts:
            raise PriorityOnceConflictError(conflicts) from exc
        raise PriorityOnceError("priority_once_insert_conflict", "priority consumption could not be recorded atomically") from exc
    except Exception:
        db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        db.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    by_key = {value.key: value for value in existing}
    by_key.update({value.key: value for value in new_values})
    return tuple(by_key[key] for key in ordered)


__all__ = [
    "PriorityOnceConflictError", "PriorityOnceConsumption", "PriorityOnceError",
    "PriorityOnceKey", "assert_priority_once_available",
    "consume_priority_once_in_transaction", "derive_priority_once_keys",
    "read_unconsumed_priority_once", "verify_priority_once_consumed",
]
