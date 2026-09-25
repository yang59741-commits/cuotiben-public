"""Independent, candidate-only persistence for the D2 selection contract.

This module is deliberately separate from the historical rubric/score
repository.  It is an unregistered schema draft used for provider-free
replay.  Every stored artifact is immutable; only the per-job head advances by
revision CAS.  No production table, migration, provider, or legacy field is
consulted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from .exam_generation_stage5_candidate_base_runner import (
    CANDIDATE_BASE_D2_PROTOCOL,
    CandidateStage6D2Base,
)
from .exam_generation_stage5_d2_solver import (
    D2Selection,
    D2SearchIncompleteError,
    D2_SOLVER_VERSION,
    D2_V4_SOLVER_VERSION,
    D2_LEGACY_SOLVER_VERSIONS,
    solve_stage5_d2_selection,
)
from .exam_generation_stage5_solver import (
    FrozenCandidateEdgeGraph,
    Stage5GlobalConstraintContract,
)
from .exam_generation_stage6_local_factor_contract import (
    project_local_factor_receipts_for_d2,
    validate_local_factor_receipts,
)
from .exam_generation_stage6_objective import FACTOR_ORDER, validate_objective_policy
from .exam_time_policy import ExamDeadlineExceeded
from .job_contracts import JobCancelled, JobLeaseLost


D2_REPOSITORY_VERSION = "past-v3-stage6-d2-repository-v1"
D2_CONSTRAINT_VERSION = "past-v3-stage5-d2-constraint-record-v1"
D2_GRAPH_VERSION = "past-v3-stage5-d2-graph-record-v1"
D2_POLICY_VERSION = "past-v3-stage6-d2-objective-policy-record-v1"
D2_FACTOR_SET_VERSION = "candidate-d2-factor-set-v1"
D2_CHECKPOINT_VERSION = "past-v3-stage6-d2-candidate-checkpoint-v1"
D2_HEAD_PROTOCOL = "past-v3-stage6-d2-local-v1"
D2_CHECKPOINT_STAGE = "stage6_d2_selection_closed"
D2_DURATION_FIELDS = frozenset({"local_factor_ms", "local_solve_ms"})
_HASH = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_KEYS = {
    "score",
    "penalty",
    "score_identity_hash",
    "rubric_receipt_hash",
    "utility_receipt_hash",
}


class D2RepositoryError(RuntimeError):
    """Raised when an independent D2 artifact cannot be verified/replayed."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class D2CandidateCheckpoint:
    version: str
    identity_hash: str
    checkpoint_hash: str
    size_bytes: int
    _payload_json: str

    def to_dict(self) -> dict[str, Any]:
        return _checkpoint_payload(self)


@dataclass(frozen=True, slots=True)
class D2CandidateState:
    base: CandidateStage6D2Base
    objective_policy: Mapping[str, Any]
    factor_receipts: tuple[Mapping[str, Any], ...]
    factor_set_receipt: Mapping[str, Any]
    selection: D2Selection
    checkpoint: D2CandidateCheckpoint
    head: Mapping[str, Any]


D2_CONSTRAINTS = "exam_generation_stage5_d2_constraint_receipts"
D2_GRAPHS = "exam_generation_stage5_d2_graph_receipts"
D2_POLICIES = "exam_generation_stage6_d2_objective_policy_receipts"
D2_FACTORS = "exam_generation_stage6_d2_factor_receipts"
D2_FACTOR_SETS = "exam_generation_stage6_d2_factor_set_receipts"
D2_SELECTIONS = "exam_generation_stage6_d2_selection_receipts"
D2_CHECKPOINTS = "exam_generation_stage6_d2_checkpoints"
D2_HEADS = "exam_generation_stage6_d2_heads"


def _hc(field: str) -> str:
    return f"CHECK(length({field})=64 AND {field} NOT GLOB '*[^0-9a-f]*')"


TABLE_SQL: Mapping[str, str] = {
    D2_CONSTRAINTS: f"""CREATE TABLE {D2_CONSTRAINTS}(
        job_id TEXT NOT NULL,
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        stage3_contract_hash TEXT NOT NULL {_hc('stage3_contract_hash')},
        candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        projection_identity_hash TEXT NOT NULL {_hc('projection_identity_hash')},
        record_version TEXT NOT NULL CHECK(record_version='{D2_CONSTRAINT_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,constraint_hash)
    )""",
    D2_GRAPHS: f"""CREATE TABLE {D2_GRAPHS}(
        job_id TEXT NOT NULL,
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        record_version TEXT NOT NULL CHECK(record_version='{D2_GRAPH_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,graph_hash),
        FOREIGN KEY(job_id,constraint_hash)
            REFERENCES {D2_CONSTRAINTS}(job_id,constraint_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    D2_POLICIES: f"""CREATE TABLE {D2_POLICIES}(
        job_id TEXT NOT NULL,
        policy_hash TEXT NOT NULL {_hc('policy_hash')},
        parameter_set_hash TEXT NOT NULL {_hc('parameter_set_hash')},
        record_version TEXT NOT NULL CHECK(record_version='{D2_POLICY_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,policy_hash)
    )""",
    D2_FACTORS: f"""CREATE TABLE {D2_FACTORS}(
        job_id TEXT NOT NULL,
        receipt_hash TEXT NOT NULL {_hc('receipt_hash')},
        policy_hash TEXT NOT NULL {_hc('policy_hash')},
        candidate_id TEXT NOT NULL,
        candidate_fact_hash TEXT NOT NULL {_hc('candidate_fact_hash')},
        content_hash TEXT NOT NULL {_hc('content_hash')},
        receipt_version TEXT NOT NULL CHECK(receipt_version='candidate-local-factor-receipt-v1'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,receipt_hash),
        UNIQUE(job_id,candidate_id,candidate_fact_hash,content_hash,policy_hash),
        FOREIGN KEY(job_id,policy_hash)
            REFERENCES {D2_POLICIES}(job_id,policy_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    D2_FACTOR_SETS: f"""CREATE TABLE {D2_FACTOR_SETS}(
        job_id TEXT NOT NULL,
        factor_set_hash TEXT NOT NULL {_hc('factor_set_hash')},
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        policy_hash TEXT NOT NULL {_hc('policy_hash')},
        member_count INTEGER NOT NULL CHECK(member_count>0),
        record_version TEXT NOT NULL CHECK(record_version='{D2_FACTOR_SET_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,factor_set_hash),
        UNIQUE(job_id,graph_hash,policy_hash,factor_set_hash),
        FOREIGN KEY(job_id,graph_hash)
            REFERENCES {D2_GRAPHS}(job_id,graph_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,constraint_hash)
            REFERENCES {D2_CONSTRAINTS}(job_id,constraint_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,policy_hash)
            REFERENCES {D2_POLICIES}(job_id,policy_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    D2_SELECTIONS: f"""CREATE TABLE {D2_SELECTIONS}(
        job_id TEXT NOT NULL,
        selection_hash TEXT NOT NULL {_hc('selection_hash')},
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        policy_hash TEXT NOT NULL {_hc('policy_hash')},
        factor_set_hash TEXT NOT NULL {_hc('factor_set_hash')},
        objective_hash TEXT NOT NULL {_hc('objective_hash')},
        assignment_count INTEGER NOT NULL CHECK(assignment_count>0),
        record_version TEXT NOT NULL CHECK(record_version='{D2_REPOSITORY_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,selection_hash),
        FOREIGN KEY(job_id,graph_hash)
            REFERENCES {D2_GRAPHS}(job_id,graph_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,constraint_hash)
            REFERENCES {D2_CONSTRAINTS}(job_id,constraint_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,policy_hash)
            REFERENCES {D2_POLICIES}(job_id,policy_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,factor_set_hash)
            REFERENCES {D2_FACTOR_SETS}(job_id,factor_set_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    D2_CHECKPOINTS: f"""CREATE TABLE {D2_CHECKPOINTS}(
        job_id TEXT NOT NULL,
        checkpoint_hash TEXT NOT NULL {_hc('checkpoint_hash')},
        identity_hash TEXT NOT NULL {_hc('identity_hash')},
        stage3_contract_hash TEXT NOT NULL {_hc('stage3_contract_hash')},
        candidate_facts_hash TEXT NOT NULL {_hc('candidate_facts_hash')},
        projection_identity_hash TEXT NOT NULL {_hc('projection_identity_hash')},
        profile_hash TEXT NOT NULL {_hc('profile_hash')},
        exact_slot_hash TEXT NOT NULL {_hc('exact_slot_hash')},
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        objective_policy_hash TEXT NOT NULL {_hc('objective_policy_hash')},
        factor_set_hash TEXT NOT NULL {_hc('factor_set_hash')},
        selection_hash TEXT NOT NULL {_hc('selection_hash')},
        question_count INTEGER NOT NULL CHECK(question_count>0),
        size_bytes INTEGER NOT NULL CHECK(size_bytes BETWEEN 1 AND 24576),
        checkpoint_version TEXT NOT NULL CHECK(checkpoint_version='{D2_CHECKPOINT_VERSION}'),
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(job_id,checkpoint_hash),
        UNIQUE(job_id,identity_hash),
        FOREIGN KEY(job_id,selection_hash)
            REFERENCES {D2_SELECTIONS}(job_id,selection_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,factor_set_hash)
            REFERENCES {D2_FACTOR_SETS}(job_id,factor_set_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
    D2_HEADS: f"""CREATE TABLE {D2_HEADS}(
        job_id TEXT NOT NULL PRIMARY KEY,
        protocol TEXT NOT NULL CHECK(protocol='{D2_HEAD_PROTOCOL}'),
        state TEXT NOT NULL CHECK(state IN ('graph_frozen','factors_closed','selection_closed','checkpointed')),
        constraint_hash TEXT NOT NULL {_hc('constraint_hash')},
        graph_hash TEXT NOT NULL {_hc('graph_hash')},
        objective_policy_hash TEXT NOT NULL {_hc('objective_policy_hash')},
        factor_set_hash TEXT,
        selection_hash TEXT,
        checkpoint_hash TEXT,
        revision INTEGER NOT NULL CHECK(revision>0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        CHECK((state='graph_frozen' AND factor_set_hash IS NULL AND selection_hash IS NULL AND checkpoint_hash IS NULL)
           OR (state='factors_closed' AND length(factor_set_hash)=64 AND selection_hash IS NULL AND checkpoint_hash IS NULL)
           OR (state='selection_closed' AND length(factor_set_hash)=64 AND length(selection_hash)=64 AND checkpoint_hash IS NULL)
           OR (state='checkpointed' AND length(factor_set_hash)=64 AND length(selection_hash)=64 AND length(checkpoint_hash)=64)),
        FOREIGN KEY(job_id,constraint_hash)
            REFERENCES {D2_CONSTRAINTS}(job_id,constraint_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,graph_hash)
            REFERENCES {D2_GRAPHS}(job_id,graph_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,objective_policy_hash)
            REFERENCES {D2_POLICIES}(job_id,policy_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,factor_set_hash)
            REFERENCES {D2_FACTOR_SETS}(job_id,factor_set_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,selection_hash)
            REFERENCES {D2_SELECTIONS}(job_id,selection_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT,
        FOREIGN KEY(job_id,checkpoint_hash)
            REFERENCES {D2_CHECKPOINTS}(job_id,checkpoint_hash)
            ON UPDATE RESTRICT ON DELETE RESTRICT
    )""",
}

IMMUTABLE_TABLES = tuple(table for table in TABLE_SQL if table != D2_HEADS)

EXPECTED_COLUMNS: Mapping[str, frozenset[str]] = {
    D2_CONSTRAINTS: frozenset({
        "job_id", "constraint_hash", "stage3_contract_hash",
        "candidate_facts_hash", "projection_identity_hash", "record_version",
        "payload_json", "created_at",
    }),
    D2_GRAPHS: frozenset({
        "job_id", "graph_hash", "constraint_hash", "candidate_facts_hash",
        "record_version", "payload_json", "created_at",
    }),
    D2_POLICIES: frozenset({
        "job_id", "policy_hash", "parameter_set_hash", "record_version",
        "payload_json", "created_at",
    }),
    D2_FACTORS: frozenset({
        "job_id", "receipt_hash", "policy_hash", "candidate_id",
        "candidate_fact_hash", "content_hash", "receipt_version", "payload_json",
        "created_at",
    }),
    D2_FACTOR_SETS: frozenset({
        "job_id", "factor_set_hash", "graph_hash", "constraint_hash",
        "candidate_facts_hash", "policy_hash", "member_count", "record_version",
        "payload_json", "created_at",
    }),
    D2_SELECTIONS: frozenset({
        "job_id", "selection_hash", "graph_hash", "constraint_hash",
        "policy_hash", "factor_set_hash", "objective_hash", "assignment_count",
        "record_version", "payload_json", "created_at",
    }),
    D2_CHECKPOINTS: frozenset({
        "job_id", "checkpoint_hash", "identity_hash", "stage3_contract_hash",
        "candidate_facts_hash", "projection_identity_hash", "profile_hash",
        "exact_slot_hash", "constraint_hash", "graph_hash", "objective_policy_hash",
        "factor_set_hash", "selection_hash", "question_count", "size_bytes",
        "checkpoint_version", "payload_json", "created_at",
    }),
    D2_HEADS: frozenset({
        "job_id", "protocol", "state", "constraint_hash", "graph_hash",
        "objective_policy_hash", "factor_set_hash", "selection_hash",
        "checkpoint_hash", "revision", "created_at", "updated_at",
    }),
}


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise D2RepositoryError("non_canonical_json") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise D2RepositoryError(f"{field}_invalid")
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise D2RepositoryError("duplicate_json_key")
        result[key] = value
    return result


def _load_json(raw: Any, field: str) -> dict[str, Any]:
    try:
        value = json.loads(str(raw), object_pairs_hook=_pairs)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise D2RepositoryError(f"{field}_json_invalid") from exc
    if not isinstance(value, dict) or _canonical(value) != str(raw):
        raise D2RepositoryError(f"{field}_noncanonical_json")
    return value


def _reject_legacy(value: Any) -> None:
    if isinstance(value, Mapping):
        if any(str(key).casefold() in _FORBIDDEN_KEYS for key in value):
            raise D2RepositoryError("legacy_field_forbidden")
        for item in value.values():
            _reject_legacy(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_legacy(item)


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="microseconds")
    if not isinstance(value, str) or not value.strip():
        raise D2RepositoryError("created_at_invalid")
    return value


def _row_value(row: Any, key: str, index: int | None = None) -> Any:
    if row is None:
        return None
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if index is None:
            raise
        return row[index]


def _insert_immutable(
    db: Any,
    table: str,
    columns: Sequence[str],
    values: Sequence[Any],
    *,
    key_columns: Sequence[str],
    key_values: Sequence[Any],
    payload_column: str = "payload_json",
    payload_value: str,
) -> None:
    if table not in IMMUTABLE_TABLES:
        raise D2RepositoryError("immutable_table_invalid")
    existing = db.execute(
        f"SELECT {','.join(columns)} FROM {table} WHERE "
        + " AND ".join(f"{name}=?" for name in key_columns),
        tuple(key_values),
    ).fetchone()
    if existing is not None:
        stored = _row_value(existing, payload_column, columns.index(payload_column))
        if str(stored) == payload_value and all(
            _row_value(existing, name, columns.index(name)) == value
            for name, value in zip(columns, values)
            if name not in {payload_column, "created_at"}
        ):
            return
        raise D2RepositoryError(f"{table}_identity_conflict")
    try:
        db.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES "
            f"({','.join('?' for _ in columns)})",
            tuple(values),
        )
    except sqlite3.IntegrityError as exc:
        raise D2RepositoryError(f"{table}_insert_conflict") from exc


def _validate_base(base: Any) -> CandidateStage6D2Base:
    if not isinstance(base, CandidateStage6D2Base):
        raise D2RepositoryError("base_type_invalid")
    if base.protocol != CANDIDATE_BASE_D2_PROTOCOL:
        raise D2RepositoryError("base_protocol_invalid")
    try:
        stage3 = base.stage3
        stage4 = base.stage4
        constraint = base.constraint
        graph = base.graph
        if not isinstance(stage3, Mapping) or not isinstance(stage4, Mapping):
            raise ValueError
        if _digest(stage3) != constraint.stage3_contract_hash:
            raise ValueError
        if _digest(stage4) != constraint.candidate_facts_hash:
            raise ValueError
        if _digest(constraint.to_dict()) != constraint.constraint_hash:
            raise ValueError
        if _digest(graph.to_dict()) != graph.graph_hash:
            raise ValueError
        if graph.constraint_hash != constraint.constraint_hash:
            raise ValueError
    except Exception as exc:
        raise D2RepositoryError("base_identity_invalid") from exc
    _reject_legacy(base.to_dict())
    return base


def _profile_identity(base: CandidateStage6D2Base) -> tuple[str, str]:
    payload = base.constraint.to_dict()
    try:
        return _hash(payload["profile_hash"], "profile_hash"), _hash(
            payload["exact_slot_hash"], "exact_slot_hash"
        )
    except (KeyError, D2RepositoryError) as exc:
        raise D2RepositoryError("base_profile_identity_invalid") from exc


def build_d2_factor_set_receipt(
    *,
    base: CandidateStage6D2Base,
    objective_policy: Mapping[str, Any],
    factor_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Validate explicit local factors and bind their canonical member set."""
    base = _validate_base(base)
    try:
        policy = validate_objective_policy(objective_policy)
        _hash(policy["parameter_set_hash"], "parameter_set_hash")
        validated = validate_local_factor_receipts(
            factor_receipts, policy=policy
        )
    except Exception as exc:
        raise D2RepositoryError("factor_receipts_invalid") from exc
    edges = base.graph.to_dict().get("edges")
    if not isinstance(edges, list):
        raise D2RepositoryError("graph_edges_invalid")
    expected = {
        (edge.get("candidate_id"), edge.get("candidate_fact_hash"), edge.get("content_hash"))
        for edge in edges
        if isinstance(edge, Mapping)
    }
    actual = {
        (item["candidate_id"], item["candidate_fact_hash"], item["content_hash"])
        for item in validated
    }
    if actual != expected:
        raise D2RepositoryError("factor_set_graph_coverage_invalid")
    members = [
        {
            "candidate_id": item["candidate_id"],
            "candidate_fact_hash": item["candidate_fact_hash"],
            "content_hash": item["content_hash"],
            "receipt_hash": item["receipt_hash"],
        }
        for item in validated
    ]
    members.sort(
        key=lambda item: (
            item["candidate_id"],
            item["candidate_fact_hash"],
            item["content_hash"],
            item["receipt_hash"],
        )
    )
    body = {
        "version": D2_FACTOR_SET_VERSION,
        "graph_hash": base.graph.graph_hash,
        "constraint_hash": base.constraint.constraint_hash,
        "candidate_facts_hash": base.constraint.candidate_facts_hash,
        "policy_hash": policy["policy_hash"],
        "member_count": len(members),
        "members": members,
    }
    return {**body, "factor_set_hash": _digest(body)}


def _validate_factor_set(
    value: Mapping[str, Any],
    *,
    base: CandidateStage6D2Base,
    policy: Mapping[str, Any],
    factor_receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    expected = build_d2_factor_set_receipt(
        base=base, objective_policy=policy, factor_receipts=factor_receipts
    )
    if not isinstance(value, Mapping) or dict(value) != expected:
        raise D2RepositoryError("factor_set_binding_invalid")
    _hash(value.get("factor_set_hash"), "factor_set_hash")
    return dict(value)


def _validate_selection(
    selection: Any,
    *,
    base: CandidateStage6D2Base,
    policy: Mapping[str, Any],
    factor_receipts: Sequence[Mapping[str, Any]],
    search_guard: Callable[[], bool] | None = None,
) -> D2Selection:
    if not isinstance(selection, D2Selection):
        raise D2RepositoryError("selection_type_invalid")
    try:
        projected = project_local_factor_receipts_for_d2(
            factor_receipts, policy=policy
        )
        payload = selection.to_dict()
        replay_kwargs: dict[str, Any] = {}
        if "target_mode" in payload or "m_once_eligibility" in payload:
            eligibility = payload.get("m_once_eligibility")
            if not isinstance(eligibility, Mapping) or set(eligibility) != {"status", "identities"}:
                raise ValueError("selection_m_once_eligibility_invalid")
            identities = tuple(tuple(value) for value in eligibility["identities"])
            replay_kwargs = {"target_mode": payload.get("target_mode")}
            if eligibility.get("status") != "not_supplied":
                replay_kwargs["m_once_eligible_identities"] = identities
        rebuilt = solve_stage5_d2_selection(
            base.constraint,
            base.graph,
            factor_receipts=projected,
            objective_policy=policy,
            search_guard=search_guard,
            solver_version=selection.solver_version,
            **replay_kwargs,
        )
        if selection.solver_version in (D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION, D2_LEGACY_SOLVER_VERSIONS[1]):
            if rebuilt.to_dict() != payload:
                raise ValueError("selection_replay_drift")
        elif selection.solver_version == "stage5-d2-lexicographic-solver-v1":
            if (
                rebuilt.assignments != selection.assignments
                or rebuilt.objective_vector != selection.objective_vector
                or rebuilt.to_dict()["factor_receipt_hash"] != payload.get("factor_receipt_hash")
            ):
                raise ValueError("legacy_selection_replay_drift")
        else:
            raise ValueError("selection_solver_version_invalid")
        _reject_legacy(payload)
    except (D2SearchIncompleteError, ExamDeadlineExceeded, JobCancelled, JobLeaseLost):
        raise
    except Exception as exc:
        raise D2RepositoryError("selection_replay_invalid") from exc
    return selection


def _checkpoint_bindings(
    *,
    base: CandidateStage6D2Base,
    policy: Mapping[str, Any],
    factor_set_hash: str,
    selection: D2Selection,
    stage_durations_ms: Mapping[str, Any],
) -> dict[str, Any]:
    profile_hash, exact_slot_hash = _profile_identity(base)
    if not isinstance(stage_durations_ms, Mapping) or set(stage_durations_ms) != set(D2_DURATION_FIELDS):
        raise D2RepositoryError("stage_durations_invalid")
    durations: dict[str, int] = {}
    for field in sorted(D2_DURATION_FIELDS):
        value = stage_durations_ms[field]
        if type(value) is not int or not 0 <= value <= 86_400_000:
            raise D2RepositoryError(f"{field}_invalid")
        durations[field] = value
    return {
        "stage3_contract_hash": _hash(
            base.constraint.stage3_contract_hash, "stage3_contract_hash"
        ),
        "candidate_facts_hash": _hash(
            base.constraint.candidate_facts_hash, "candidate_facts_hash"
        ),
        "projection_identity_hash": _hash(
            base.constraint.projection_identity_hash, "projection_identity_hash"
        ),
        "profile_hash": profile_hash,
        "exact_slot_hash": exact_slot_hash,
        "constraint_hash": _hash(base.constraint.constraint_hash, "constraint_hash"),
        "graph_hash": _hash(base.graph.graph_hash, "graph_hash"),
        "objective_policy_hash": _hash(
            policy["policy_hash"], "objective_policy_hash"
        ),
        "factor_set_hash": _hash(factor_set_hash, "factor_set_hash"),
        "selection_hash": _hash(selection.selection_hash, "selection_hash"),
        "question_count": base.constraint.question_count,
        "stage_durations_ms": durations,
    }


def build_d2_checkpoint(
    *,
    base: CandidateStage6D2Base,
    objective_policy: Mapping[str, Any],
    factor_set_hash: str,
    selection: D2Selection,
    stage_durations_ms: Mapping[str, Any],
) -> D2CandidateCheckpoint:
    base = _validate_base(base)
    try:
        policy = validate_objective_policy(objective_policy)
        _hash(policy["parameter_set_hash"], "parameter_set_hash")
    except Exception as exc:
        raise D2RepositoryError("objective_policy_invalid") from exc
    bindings = _checkpoint_bindings(
        base=base,
        policy=policy,
        factor_set_hash=factor_set_hash,
        selection=selection,
        stage_durations_ms=stage_durations_ms,
    )
    body = {
        "version": D2_CHECKPOINT_VERSION,
        "protocol": D2_HEAD_PROTOCOL,
        "stage": D2_CHECKPOINT_STAGE,
        "bindings": bindings,
    }
    identity_hash = _digest(bindings)
    payload = {**body, "identity_hash": identity_hash}
    raw = _canonical(payload)
    checkpoint = D2CandidateCheckpoint(
        D2_CHECKPOINT_VERSION,
        identity_hash,
        hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        len(raw.encode("utf-8")),
        raw,
    )
    if checkpoint.size_bytes > 24_576:
        raise D2RepositoryError("checkpoint_size_exceeded")
    return checkpoint


def _checkpoint_payload(value: D2CandidateCheckpoint) -> dict[str, Any]:
    if not isinstance(value, D2CandidateCheckpoint):
        raise D2RepositoryError("checkpoint_type_invalid")
    payload = _load_json(value._payload_json, "checkpoint")
    if set(payload) != {"version", "protocol", "stage", "bindings", "identity_hash"}:
        raise D2RepositoryError("checkpoint_shape_invalid")
    if (
        payload["version"] != D2_CHECKPOINT_VERSION
        or payload["protocol"] != D2_HEAD_PROTOCOL
        or payload["stage"] != D2_CHECKPOINT_STAGE
        or not isinstance(payload["bindings"], Mapping)
    ):
        raise D2RepositoryError("checkpoint_identity_invalid")
    bindings = dict(payload["bindings"])
    required = {
        "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash",
        "profile_hash", "exact_slot_hash", "constraint_hash", "graph_hash",
        "objective_policy_hash", "factor_set_hash", "selection_hash",
        "question_count", "stage_durations_ms",
    }
    if set(bindings) != required:
        raise D2RepositoryError("checkpoint_binding_shape_invalid")
    for field in required - {"question_count", "stage_durations_ms"}:
        _hash(bindings[field], field)
    if type(bindings["question_count"]) is not int or bindings["question_count"] <= 0:
        raise D2RepositoryError("question_count_invalid")
    durations = bindings["stage_durations_ms"]
    if not isinstance(durations, Mapping) or set(durations) != set(D2_DURATION_FIELDS):
        raise D2RepositoryError("stage_durations_invalid")
    if any(type(durations[field]) is not int or not 0 <= durations[field] <= 86_400_000 for field in D2_DURATION_FIELDS):
        raise D2RepositoryError("stage_durations_invalid")
    if payload["identity_hash"] != _digest(bindings):
        raise D2RepositoryError("checkpoint_identity_hash_mismatch")
    _reject_legacy(payload)
    raw = _canonical(payload)
    if (
        value.version != D2_CHECKPOINT_VERSION
        or value.identity_hash != payload["identity_hash"]
        or value.checkpoint_hash != hashlib.sha256(raw.encode("utf-8")).hexdigest()
        or value.size_bytes != len(raw.encode("utf-8"))
        or value._payload_json != raw
    ):
        raise D2RepositoryError("checkpoint_integrity_mismatch")
    return payload


def validate_d2_checkpoint(
    value: D2CandidateCheckpoint,
    *,
    base: CandidateStage6D2Base,
    objective_policy: Mapping[str, Any],
    factor_set_hash: str,
    selection: D2Selection,
    stage_durations_ms: Mapping[str, Any],
) -> dict[str, Any]:
    payload = _checkpoint_payload(value)
    expected = build_d2_checkpoint(
        base=base,
        objective_policy=objective_policy,
        factor_set_hash=factor_set_hash,
        selection=selection,
        stage_durations_ms=stage_durations_ms,
    )
    if payload != expected.to_dict():
        raise D2RepositoryError("checkpoint_binding_drift")
    return payload


def _persist_factor_rows(
    db: Any,
    *,
    job_id: str,
    policy: Mapping[str, Any],
    factor_receipts: Sequence[Mapping[str, Any]],
    created_at: str,
) -> None:
    values = validate_local_factor_receipts(factor_receipts, policy=policy)
    for receipt in values:
        columns = (
            "job_id", "receipt_hash", "policy_hash", "candidate_id",
            "candidate_fact_hash", "content_hash", "receipt_version",
            "payload_json", "created_at",
        )
        row = (
            job_id, receipt["receipt_hash"], policy["policy_hash"],
            receipt["candidate_id"], receipt["candidate_fact_hash"],
            receipt["content_hash"], receipt["version"], _canonical(receipt),
            created_at,
        )
        _insert_immutable(
            db, D2_FACTORS, columns, row,
            key_columns=("job_id", "receipt_hash"),
            key_values=(job_id, receipt["receipt_hash"]),
            payload_value=row[-2],
        )


def _head_row(db: Any, job_id: str) -> Any:
    return db.execute(f"SELECT * FROM {D2_HEADS} WHERE job_id=?", (job_id,)).fetchone()


def _head_identity_matches(row: Any, *, base: CandidateStage6D2Base, policy_hash: str) -> bool:
    return (
        _row_value(row, "protocol", 1) == D2_HEAD_PROTOCOL
        and _row_value(row, "constraint_hash", 3) == base.constraint.constraint_hash
        and _row_value(row, "graph_hash", 4) == base.graph.graph_hash
        and _row_value(row, "objective_policy_hash", 5) == policy_hash
    )


def _cas_head(
    db: Any,
    *,
    job_id: str,
    from_state: str,
    to_state: str,
    field: str,
    value: str,
    expected_revision: int,
    updated_at: str,
) -> None:
    if field not in {"factor_set_hash", "selection_hash", "checkpoint_hash"}:
        raise D2RepositoryError("head_field_invalid")
    result = db.execute(
        f"UPDATE {D2_HEADS} SET state=?,{field}=?,revision=revision+1,updated_at=? "
        "WHERE job_id=? AND state=? AND revision=?",
        (to_state, value, updated_at, job_id, from_state, expected_revision),
    )
    if getattr(result, "rowcount", 0) != 1:
        raise D2RepositoryError("head_cas_conflict")


def _advance_head(
    db: Any,
    *,
    job_id: str,
    base: CandidateStage6D2Base,
    policy_hash: str,
    factor_set_hash: str,
    selection_hash: str,
    checkpoint_hash: str,
    created_at: str,
    expected_revision: int | None,
) -> None:
    row = _head_row(db, job_id)
    if row is None:
        if expected_revision not in (None, 0):
            raise D2RepositoryError("head_revision_conflict")
        db.execute(
            f"INSERT INTO {D2_HEADS} (job_id,protocol,state,constraint_hash,graph_hash,objective_policy_hash,"
            "factor_set_hash,selection_hash,checkpoint_hash,revision,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id, D2_HEAD_PROTOCOL, "graph_frozen", base.constraint.constraint_hash,
                base.graph.graph_hash, policy_hash, None, None, None, 1, created_at, created_at,
            ),
        )
        row = _head_row(db, job_id)
    if not _head_identity_matches(row, base=base, policy_hash=policy_hash):
        raise D2RepositoryError("head_identity_conflict")
    current_revision = int(_row_value(row, "revision", 9))
    if expected_revision is not None and current_revision != expected_revision and _row_value(row, "state", 2) != "checkpointed":
        raise D2RepositoryError("head_revision_conflict")
    state = str(_row_value(row, "state", 2))
    if state == "checkpointed":
        if (
            _row_value(row, "factor_set_hash", 6) != factor_set_hash
            or _row_value(row, "selection_hash", 7) != selection_hash
            or _row_value(row, "checkpoint_hash", 8) != checkpoint_hash
        ):
            raise D2RepositoryError("head_identity_conflict")
        return
    if state == "graph_frozen":
        _cas_head(db, job_id=job_id, from_state=state, to_state="factors_closed", field="factor_set_hash", value=factor_set_hash, expected_revision=current_revision, updated_at=created_at)
        state, current_revision = "factors_closed", current_revision + 1
    if state == "factors_closed":
        row = _head_row(db, job_id)
        if _row_value(row, "factor_set_hash", 6) != factor_set_hash:
            raise D2RepositoryError("head_factor_set_conflict")
        current_revision = int(_row_value(row, "revision", 9))
        _cas_head(db, job_id=job_id, from_state=state, to_state="selection_closed", field="selection_hash", value=selection_hash, expected_revision=current_revision, updated_at=created_at)
        state, current_revision = "selection_closed", current_revision + 1
    if state == "selection_closed":
        row = _head_row(db, job_id)
        if _row_value(row, "selection_hash", 7) != selection_hash:
            raise D2RepositoryError("head_selection_conflict")
        current_revision = int(_row_value(row, "revision", 9))
        _cas_head(db, job_id=job_id, from_state=state, to_state="checkpointed", field="checkpoint_hash", value=checkpoint_hash, expected_revision=current_revision, updated_at=created_at)
    elif state != "checkpointed":
        raise D2RepositoryError("head_state_invalid")


def persist_d2_candidate_chain(
    db: Any,
    *,
    job_id: str,
    base: CandidateStage6D2Base,
    objective_policy: Mapping[str, Any],
    factor_receipts: Sequence[Mapping[str, Any]],
    selection: D2Selection,
    stage_durations_ms: Mapping[str, Any],
    checkpoint: D2CandidateCheckpoint | None = None,
    created_at: str | None = None,
    expected_revision: int | None = None,
    advance_head: bool = True,
    search_guard: Callable[[], bool] | None = None,
    _selection_replay_validated: bool = False,
) -> D2CandidateState:
    """Persist and replay one complete D2 chain in the draft schema."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise D2RepositoryError("job_id_invalid")
    base = _validate_base(base)
    try:
        policy = validate_objective_policy(objective_policy)
    except Exception as exc:
        raise D2RepositoryError("objective_policy_invalid") from exc
    factor_set = build_d2_factor_set_receipt(
        base=base, objective_policy=policy, factor_receipts=factor_receipts
    )
    if _selection_replay_validated:
        if not isinstance(selection, D2Selection):
            raise D2RepositoryError("selection_type_invalid")
    else:
        selection = _validate_selection(
            selection, base=base, policy=policy, factor_receipts=factor_receipts, search_guard=search_guard
        )
    if checkpoint is None:
        checkpoint = build_d2_checkpoint(
            base=base,
            objective_policy=policy,
            factor_set_hash=factor_set["factor_set_hash"],
            selection=selection,
            stage_durations_ms=stage_durations_ms,
        )
    else:
        validate_d2_checkpoint(
            checkpoint,
            base=base,
            objective_policy=policy,
            factor_set_hash=factor_set["factor_set_hash"],
            selection=selection,
            stage_durations_ms=stage_durations_ms,
        )
    timestamp = _timestamp(created_at)
    owns_transaction = not bool(getattr(db, "in_transaction", False))
    try:
        if owns_transaction:
            db.execute("BEGIN IMMEDIATE")
        constraint_payload = {"version": D2_CONSTRAINT_VERSION, "constraint_hash": base.constraint.constraint_hash, "stage3": dict(base.stage3), "constraint": base.constraint.to_dict()}
        graph_payload = {"version": D2_GRAPH_VERSION, "graph_hash": base.graph.graph_hash, "stage4": dict(base.stage4), "graph": base.graph.to_dict()}
        policy_payload = {"version": D2_POLICY_VERSION, "policy": policy}
        constraint_columns = ("job_id", "constraint_hash", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "record_version", "payload_json", "created_at")
        constraint_values = (job_id, base.constraint.constraint_hash, base.constraint.stage3_contract_hash, base.constraint.candidate_facts_hash, base.constraint.projection_identity_hash, D2_CONSTRAINT_VERSION, _canonical(constraint_payload), timestamp)
        _insert_immutable(db, D2_CONSTRAINTS, constraint_columns, constraint_values, key_columns=("job_id", "constraint_hash"), key_values=(job_id, base.constraint.constraint_hash), payload_value=constraint_values[-2])
        graph_columns = ("job_id", "graph_hash", "constraint_hash", "candidate_facts_hash", "record_version", "payload_json", "created_at")
        graph_values = (job_id, base.graph.graph_hash, base.constraint.constraint_hash, base.graph.candidate_facts_hash, D2_GRAPH_VERSION, _canonical(graph_payload), timestamp)
        _insert_immutable(db, D2_GRAPHS, graph_columns, graph_values, key_columns=("job_id", "graph_hash"), key_values=(job_id, base.graph.graph_hash), payload_value=graph_values[-2])
        policy_columns = ("job_id", "policy_hash", "parameter_set_hash", "record_version", "payload_json", "created_at")
        policy_values = (job_id, policy["policy_hash"], policy["parameter_set_hash"], D2_POLICY_VERSION, _canonical(policy_payload), timestamp)
        _insert_immutable(db, D2_POLICIES, policy_columns, policy_values, key_columns=("job_id", "policy_hash"), key_values=(job_id, policy["policy_hash"]), payload_value=policy_values[-2])
        _persist_factor_rows(db, job_id=job_id, policy=policy, factor_receipts=factor_receipts, created_at=timestamp)
        factor_columns = ("job_id", "factor_set_hash", "graph_hash", "constraint_hash", "candidate_facts_hash", "policy_hash", "member_count", "record_version", "payload_json", "created_at")
        factor_values = (job_id, factor_set["factor_set_hash"], factor_set["graph_hash"], factor_set["constraint_hash"], factor_set["candidate_facts_hash"], factor_set["policy_hash"], factor_set["member_count"], D2_FACTOR_SET_VERSION, _canonical(factor_set), timestamp)
        _insert_immutable(db, D2_FACTOR_SETS, factor_columns, factor_values, key_columns=("job_id", "factor_set_hash"), key_values=(job_id, factor_set["factor_set_hash"]), payload_value=factor_values[-2])
        selection_payload = selection.to_dict()
        selection_columns = ("job_id", "selection_hash", "graph_hash", "constraint_hash", "policy_hash", "factor_set_hash", "objective_hash", "assignment_count", "record_version", "payload_json", "created_at")
        selection_values = (job_id, selection.selection_hash, base.graph.graph_hash, base.constraint.constraint_hash, policy["policy_hash"], factor_set["factor_set_hash"], selection_payload["objective_hash"], len(selection.assignments), D2_REPOSITORY_VERSION, _canonical(selection_payload), timestamp)
        _insert_immutable(db, D2_SELECTIONS, selection_columns, selection_values, key_columns=("job_id", "selection_hash"), key_values=(job_id, selection.selection_hash), payload_value=selection_values[-2])
        cp = checkpoint.to_dict()
        bindings = cp["bindings"]
        checkpoint_columns = ("job_id", "checkpoint_hash", "identity_hash", "stage3_contract_hash", "candidate_facts_hash", "projection_identity_hash", "profile_hash", "exact_slot_hash", "constraint_hash", "graph_hash", "objective_policy_hash", "factor_set_hash", "selection_hash", "question_count", "size_bytes", "checkpoint_version", "payload_json", "created_at")
        checkpoint_values = (job_id, checkpoint.checkpoint_hash, checkpoint.identity_hash, bindings["stage3_contract_hash"], bindings["candidate_facts_hash"], bindings["projection_identity_hash"], bindings["profile_hash"], bindings["exact_slot_hash"], bindings["constraint_hash"], bindings["graph_hash"], bindings["objective_policy_hash"], bindings["factor_set_hash"], bindings["selection_hash"], bindings["question_count"], checkpoint.size_bytes, D2_CHECKPOINT_VERSION, checkpoint._payload_json, timestamp)
        _insert_immutable(db, D2_CHECKPOINTS, checkpoint_columns, checkpoint_values, key_columns=("job_id", "checkpoint_hash"), key_values=(job_id, checkpoint.checkpoint_hash), payload_value=checkpoint._payload_json)
        if advance_head:
            _advance_head(db, job_id=job_id, base=base, policy_hash=policy["policy_hash"], factor_set_hash=factor_set["factor_set_hash"], selection_hash=selection.selection_hash, checkpoint_hash=checkpoint.checkpoint_hash, created_at=timestamp, expected_revision=expected_revision)
        if owns_transaction:
            db.commit()
    except Exception:
        if owns_transaction:
            db.rollback()
        raise
    if not owns_transaction:
        # The caller owns the write transaction (for example to bind priority
        # claims).  Do not replay the selection while its write lock is held;
        # the caller must recover this state after committing.
        return D2CandidateState(
            base,
            dict(policy),
            tuple(dict(item) for item in factor_receipts),
            dict(factor_set),
            selection,
            checkpoint,
            {},
        )
    return recover_d2_candidate_chain(
        db,
        job_id=job_id,
        checkpoint_hash=checkpoint.checkpoint_hash,
        require_head=advance_head,
        search_guard=search_guard,
    )


def _rebuild_base(constraint_record: Mapping[str, Any], graph_record: Mapping[str, Any]) -> CandidateStage6D2Base:
    if constraint_record.get("version") != D2_CONSTRAINT_VERSION or graph_record.get("version") != D2_GRAPH_VERSION:
        raise D2RepositoryError("base_record_version_invalid")
    stage3 = constraint_record.get("stage3")
    constraint_payload = constraint_record.get("constraint")
    stage4 = graph_record.get("stage4")
    graph_payload = graph_record.get("graph")
    if not all(isinstance(item, Mapping) for item in (stage3, constraint_payload, stage4, graph_payload)):
        raise D2RepositoryError("base_record_shape_invalid")
    try:
        constraint_hash = _hash(constraint_record.get("constraint_hash"), "constraint_hash")
        if _digest(constraint_payload) != constraint_hash:
            raise ValueError("constraint_hash_drift")
        constraint = Stage5GlobalConstraintContract(
            str(constraint_payload["contract_version"]),
            str(constraint_payload["stage3_contract_hash"]),
            str(constraint_payload["candidate_facts_hash"]),
            str(constraint_payload["projection_identity_hash"]),
            int(constraint_payload["question_count"]),
            constraint_hash,
            _canonical(constraint_payload),
        )
        graph_hash = _hash(graph_record.get("graph_hash"), "graph_hash")
        if _digest(graph_payload) != graph_hash:
            raise ValueError("graph_hash_drift")
        graph = FrozenCandidateEdgeGraph(
            str(graph_payload["contract_version"]),
            str(graph_payload["constraint_hash"]),
            str(graph_payload["candidate_facts_hash"]),
            str(graph_payload["graph_mode"]),
            len(graph_payload.get("edges", [])),
            graph_hash,
            _canonical(graph_payload),
        )
        base = CandidateStage6D2Base(
            CANDIDATE_BASE_D2_PROTOCOL, dict(stage3), dict(stage4), constraint, graph
        )
        return _validate_base(base)
    except Exception as exc:
        raise D2RepositoryError("base_record_identity_invalid") from exc


def _selection_from_payload(payload: Mapping[str, Any]) -> D2Selection:
    legacy_required = {"solver_version", "graph_hash", "constraint_hash", "policy_hash", "factor_receipt_hash", "objective_hash", "assignments", "objective_vector", "selection_hash"}
    current_required = legacy_required | {"target_mode", "m_once_eligibility"}
    v4_required = current_required | {"similarity_freeze_hash"}
    solver_version = payload.get("solver_version")
    payload_keys = frozenset(payload)
    if payload_keys not in {frozenset(legacy_required), frozenset(current_required), frozenset(v4_required)} or solver_version not in (D2_SOLVER_VERSION, D2_V4_SOLVER_VERSION, *D2_LEGACY_SOLVER_VERSIONS):
        raise D2RepositoryError("selection_payload_shape_invalid")
    if solver_version == D2_V4_SOLVER_VERSION and set(payload) != v4_required:
        raise D2RepositoryError("selection_payload_shape_invalid")
    if solver_version not in {D2_LEGACY_SOLVER_VERSIONS[0], D2_V4_SOLVER_VERSION} and set(payload) != current_required:
        raise D2RepositoryError("selection_payload_shape_invalid")
    if solver_version == D2_LEGACY_SOLVER_VERSIONS[0] and set(payload) != legacy_required:
        raise D2RepositoryError("selection_payload_shape_invalid")
    for field in ("graph_hash", "constraint_hash", "policy_hash", "factor_receipt_hash", "objective_hash", "selection_hash"):
        _hash(payload.get(field), field)
    if solver_version == D2_V4_SOLVER_VERSION:
        _hash(payload.get("similarity_freeze_hash"), "similarity_freeze_hash")
    if not isinstance(payload.get("assignments"), list) or not payload["assignments"] or not isinstance(payload.get("objective_vector"), list):
        raise D2RepositoryError("selection_payload_shape_invalid")
    if payload["selection_hash"] != _digest({key: payload[key] for key in payload if key not in {"objective_vector", "selection_hash"}}):
        raise D2RepositoryError("selection_hash_mismatch")
    if solver_version == "stage5-d2-lexicographic-solver-v1":
        legacy_objective = {
            "version": solver_version,
            "policy_hash": payload["policy_hash"],
            "factor_order": list(FACTOR_ORDER),
            "vector": payload["objective_vector"],
            "graph_hash": payload["graph_hash"],
            "assignments": payload["assignments"],
        }
        if payload["objective_hash"] != _digest(legacy_objective):
            raise D2RepositoryError("objective_hash_mismatch")
    raw = _canonical(payload)
    return D2Selection(
        solver_version,
        payload["graph_hash"],
        payload["constraint_hash"],
        payload["objective_hash"],
        payload["selection_hash"],
        tuple(payload["objective_vector"]),
        tuple(payload["assignments"]),
        raw,
    )


def recover_d2_candidate_chain(
    db: Any,
    *,
    job_id: str,
    checkpoint_hash: str | None = None,
    require_head: bool = True,
    search_guard: Callable[[], bool] | None = None,
) -> D2CandidateState:
    """Recover and revalidate every D2 parent, selection and checkpoint."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise D2RepositoryError("job_id_invalid")
    head = _head_row(db, job_id)
    if require_head and head is None:
        raise D2RepositoryError("head_missing")
    if not require_head and checkpoint_hash is not None:
        # Explicit historical/new checkpoints do not have to be the active head.
        head = None
    if head is None:
        if checkpoint_hash is None:
            raise D2RepositoryError("checkpoint_hash_missing")
        checkpoint_row = db.execute(f"SELECT * FROM {D2_CHECKPOINTS} WHERE job_id=? AND checkpoint_hash=?", (job_id, checkpoint_hash)).fetchone()
        if checkpoint_row is None:
            raise D2RepositoryError("checkpoint_missing")
        constraint_hash = _row_value(checkpoint_row, "constraint_hash")
        graph_hash = _row_value(checkpoint_row, "graph_hash")
    else:
        checkpoint_hash = checkpoint_hash or _row_value(head, "checkpoint_hash", 8)
        if checkpoint_hash is None:
            raise D2RepositoryError("checkpoint_not_closed")
        constraint_hash = _row_value(head, "constraint_hash", 3)
        graph_hash = _row_value(head, "graph_hash", 4)
    _hash(constraint_hash, "constraint_hash")
    _hash(graph_hash, "graph_hash")
    checkpoint_row = db.execute(f"SELECT * FROM {D2_CHECKPOINTS} WHERE job_id=? AND checkpoint_hash=?", (job_id, checkpoint_hash)).fetchone()
    if checkpoint_row is None:
        raise D2RepositoryError("checkpoint_missing")
    cp_payload = _load_json(_row_value(checkpoint_row, "payload_json"), "checkpoint")
    cp_bindings = cp_payload.get("bindings") if isinstance(cp_payload, Mapping) else None
    if not isinstance(cp_bindings, Mapping):
        raise D2RepositoryError("checkpoint_binding_shape_invalid")
    checkpoint_column_pairs = {
        "identity_hash": _row_value(checkpoint_row, "identity_hash", 2),
        "stage3_contract_hash": _row_value(checkpoint_row, "stage3_contract_hash", 3),
        "candidate_facts_hash": _row_value(checkpoint_row, "candidate_facts_hash", 4),
        "projection_identity_hash": _row_value(checkpoint_row, "projection_identity_hash", 5),
        "profile_hash": _row_value(checkpoint_row, "profile_hash", 6),
        "exact_slot_hash": _row_value(checkpoint_row, "exact_slot_hash", 7),
        "constraint_hash": _row_value(checkpoint_row, "constraint_hash", 8),
        "graph_hash": _row_value(checkpoint_row, "graph_hash", 9),
        "objective_policy_hash": _row_value(checkpoint_row, "objective_policy_hash", 10),
        "factor_set_hash": _row_value(checkpoint_row, "factor_set_hash", 11),
        "selection_hash": _row_value(checkpoint_row, "selection_hash", 12),
        "question_count": _row_value(checkpoint_row, "question_count", 13),
        "size_bytes": _row_value(checkpoint_row, "size_bytes", 14),
    }
    for field in checkpoint_column_pairs:
        if field == "identity_hash":
            expected_value = cp_payload.get(field)
        elif field == "size_bytes":
            expected_value = len(str(_row_value(checkpoint_row, "payload_json")).encode("utf-8"))
        else:
            expected_value = cp_bindings.get(field)
        if checkpoint_column_pairs[field] != expected_value:
            raise D2RepositoryError("checkpoint_column_binding_drift")
    checkpoint = D2CandidateCheckpoint(
        D2_CHECKPOINT_VERSION,
        str(_row_value(checkpoint_row, "identity_hash")),
        str(_row_value(checkpoint_row, "checkpoint_hash")),
        int(_row_value(checkpoint_row, "size_bytes")),
        str(_row_value(checkpoint_row, "payload_json")),
    )
    _checkpoint_payload(checkpoint)
    bindings = cp_payload["bindings"]
    if bindings["constraint_hash"] != constraint_hash or bindings["graph_hash"] != graph_hash:
        raise D2RepositoryError("checkpoint_parent_binding_drift")
    constraint_row = db.execute(f"SELECT * FROM {D2_CONSTRAINTS} WHERE job_id=? AND constraint_hash=?", (job_id, constraint_hash)).fetchone()
    graph_row = db.execute(f"SELECT * FROM {D2_GRAPHS} WHERE job_id=? AND graph_hash=?", (job_id, graph_hash)).fetchone()
    if constraint_row is None or graph_row is None:
        raise D2RepositoryError("base_parent_missing")
    constraint_record = _load_json(_row_value(constraint_row, "payload_json"), "constraint")
    graph_record = _load_json(_row_value(graph_row, "payload_json"), "graph")
    if (
        constraint_record.get("version") != D2_CONSTRAINT_VERSION
        or constraint_record.get("constraint_hash") != _row_value(constraint_row, "constraint_hash")
        or constraint_record.get("stage3", {}).get("contract_version") is None
        or graph_record.get("version") != D2_GRAPH_VERSION
        or graph_record.get("graph_hash") != _row_value(graph_row, "graph_hash")
    ):
        raise D2RepositoryError("base_record_column_binding_drift")
    base = _rebuild_base(constraint_record, graph_record)
    if base.constraint.constraint_hash != constraint_hash or base.graph.graph_hash != graph_hash:
        raise D2RepositoryError("base_parent_binding_drift")
    policy_hash = bindings["objective_policy_hash"]
    policy_row = db.execute(f"SELECT * FROM {D2_POLICIES} WHERE job_id=? AND policy_hash=?", (job_id, policy_hash)).fetchone()
    if policy_row is None:
        raise D2RepositoryError("policy_missing")
    policy_record = _load_json(_row_value(policy_row, "payload_json"), "policy")
    if policy_record.get("version") != D2_POLICY_VERSION:
        raise D2RepositoryError("policy_record_version_invalid")
    try:
        policy = validate_objective_policy(policy_record["policy"])
        _hash(policy["parameter_set_hash"], "parameter_set_hash")
    except Exception as exc:
        raise D2RepositoryError("policy_invalid") from exc
    if policy["policy_hash"] != policy_hash:
        raise D2RepositoryError("policy_hash_drift")
    if (
        _row_value(policy_row, "parameter_set_hash") != policy["parameter_set_hash"]
        or _row_value(policy_row, "record_version") != D2_POLICY_VERSION
    ):
        raise D2RepositoryError("policy_column_binding_drift")
    factor_set_hash = bindings["factor_set_hash"]
    factor_set_row = db.execute(f"SELECT * FROM {D2_FACTOR_SETS} WHERE job_id=? AND factor_set_hash=?", (job_id, factor_set_hash)).fetchone()
    if factor_set_row is None:
        raise D2RepositoryError("factor_set_missing")
    factor_set = _load_json(_row_value(factor_set_row, "payload_json", 0), "factor_set")
    if (
        factor_set.get("factor_set_hash") != _row_value(factor_set_row, "factor_set_hash", 1)
        or factor_set.get("graph_hash") != _row_value(factor_set_row, "graph_hash", 2)
        or factor_set.get("constraint_hash") != _row_value(factor_set_row, "constraint_hash", 3)
        or factor_set.get("candidate_facts_hash") != _row_value(factor_set_row, "candidate_facts_hash", 4)
        or factor_set.get("policy_hash") != _row_value(factor_set_row, "policy_hash", 5)
        or factor_set.get("member_count") != _row_value(factor_set_row, "member_count", 6)
    ):
        raise D2RepositoryError("factor_set_column_binding_drift")
    members = factor_set.get("members")
    if not isinstance(members, list) or any(not isinstance(item, Mapping) for item in members):
        raise D2RepositoryError("factor_set_binding_invalid")
    # Older selections retain their factors; replay only this checkpoint's members.
    factor_rows = db.execute(
        f"SELECT factor.* FROM {D2_FACTORS} AS factor JOIN json_each(?) AS member "
        "ON factor.receipt_hash=json_extract(member.value, '$.receipt_hash') "
        "WHERE factor.job_id=? AND factor.policy_hash=? "
        "ORDER BY factor.candidate_id,factor.candidate_fact_hash,factor.content_hash,factor.receipt_hash",
        (_canonical(members), job_id, policy_hash),
    ).fetchall()
    factor_receipts: list[dict[str, Any]] = []
    for row in factor_rows:
        receipt = _load_json(_row_value(row, "payload_json", 0), "factor")
        if (
            receipt.get("receipt_hash") != _row_value(row, "receipt_hash", 1)
            or receipt.get("policy_hash") != policy_hash
            or receipt.get("candidate_id") != _row_value(row, "candidate_id", 3)
            or receipt.get("candidate_fact_hash") != _row_value(row, "candidate_fact_hash", 4)
            or receipt.get("content_hash") != _row_value(row, "content_hash", 5)
            or receipt.get("version") != _row_value(row, "receipt_version", 6)
        ):
            raise D2RepositoryError("factor_column_binding_drift")
        factor_receipts.append(receipt)
    factor_set = _validate_factor_set(factor_set, base=base, policy=policy, factor_receipts=factor_receipts)
    selection_hash = bindings["selection_hash"]
    selection_row = db.execute(f"SELECT * FROM {D2_SELECTIONS} WHERE job_id=? AND selection_hash=?", (job_id, selection_hash)).fetchone()
    if selection_row is None:
        raise D2RepositoryError("selection_missing")
    selection_payload = _load_json(_row_value(selection_row, "payload_json", 0), "selection")
    if (
        selection_payload.get("selection_hash") != _row_value(selection_row, "selection_hash", 1)
        or selection_payload.get("graph_hash") != _row_value(selection_row, "graph_hash", 2)
        or selection_payload.get("constraint_hash") != _row_value(selection_row, "constraint_hash", 3)
        or selection_payload.get("policy_hash") != _row_value(selection_row, "policy_hash", 4)
        or selection_payload.get("objective_hash") != _row_value(selection_row, "objective_hash", 6)
        or len(selection_payload.get("assignments", [])) != _row_value(selection_row, "assignment_count", 7)
    ):
        raise D2RepositoryError("selection_column_binding_drift")
    selection = _selection_from_payload(selection_payload)
    _validate_selection(selection, base=base, policy=policy, factor_receipts=factor_receipts, search_guard=search_guard)
    # Rebuild the checkpoint from the recovered identities and measurements.
    validate_d2_checkpoint(
        checkpoint,
        base=base,
        objective_policy=policy,
        factor_set_hash=factor_set_hash,
        selection=selection,
        stage_durations_ms=bindings["stage_durations_ms"],
    )
    if head is not None:
        if not _head_identity_matches(head, base=base, policy_hash=policy_hash):
            raise D2RepositoryError("head_identity_drift")
        if _row_value(head, "state", 2) != "checkpointed" or _row_value(head, "checkpoint_hash", 8) != checkpoint.checkpoint_hash:
            raise D2RepositoryError("head_not_checkpointed")
        head_dict = {str(key): _row_value(head, str(key)) for key in ("job_id", "protocol", "state", "constraint_hash", "graph_hash", "objective_policy_hash", "factor_set_hash", "selection_hash", "checkpoint_hash", "revision", "created_at", "updated_at")}
    else:
        head_dict = {}
    return D2CandidateState(
        base,
        dict(policy),
        tuple(dict(item) for item in factor_receipts),
        dict(factor_set),
        selection,
        checkpoint,
        head_dict,
    )


def _trigger_name(table: str, action: str) -> str:
    return f"{table}_{action.casefold()}_forbidden"


def _trigger_sql(table: str, action: str) -> str:
    return f"CREATE TRIGGER {_trigger_name(table, action)} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'{table} is immutable'); END"


def d2_candidate_schema_statements() -> tuple[str, ...]:
    statements = list(TABLE_SQL.values())
    for table in IMMUTABLE_TABLES:
        statements.extend((_trigger_sql(table, "UPDATE"), _trigger_sql(table, "DELETE")))
    return tuple(statements)


def install_d2_candidate_schema_draft(db: Any) -> None:
    for statement in d2_candidate_schema_statements():
        db.execute(statement)


def d2_candidate_schema_issues(db: Any) -> list[str]:
    issues: list[str] = []
    for table, expected_sql in TABLE_SQL.items():
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if row is None:
            issues.append(f"missing_table:{table}")
            continue
        if " ".join(str(_row_value(row, "sql", 0)).split()) != " ".join(expected_sql.split()):
            issues.append(f"table_sql_drift:{table}")
        columns = {str(item[1]) for item in db.execute(f"PRAGMA table_info({table})")}
        if columns != set(EXPECTED_COLUMNS[table]):
            issues.append(f"column_drift:{table}")
        if table in IMMUTABLE_TABLES:
            for action in ("UPDATE", "DELETE"):
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (_trigger_name(table, action),)).fetchone()
                if trigger is None:
                    issues.append(f"missing_trigger:{_trigger_name(table, action)}")
                elif " ".join(str(_row_value(trigger, "sql", 0)).split()) != " ".join(_trigger_sql(table, action).split()):
                    issues.append(f"trigger_sql_drift:{_trigger_name(table, action)}")
    try:
        issues.extend(f"foreign_key_violation:{row[0]}:{row[1]}" for row in db.execute("PRAGMA foreign_key_check"))
    except Exception:
        issues.append("foreign_key_check_failed")
    return issues


__all__ = [
    "D2_REPOSITORY_VERSION", "D2_CONSTRAINT_VERSION", "D2_GRAPH_VERSION",
    "D2_POLICY_VERSION", "D2_FACTOR_SET_VERSION", "D2_CHECKPOINT_VERSION",
    "D2_HEAD_PROTOCOL", "D2_CHECKPOINT_STAGE", "D2CandidateCheckpoint",
    "D2CandidateState", "D2RepositoryError", "D2_CONSTRAINTS", "D2_GRAPHS",
    "D2_POLICIES", "D2_FACTORS", "D2_FACTOR_SETS", "D2_SELECTIONS",
    "D2_CHECKPOINTS", "D2_HEADS", "TABLE_SQL", "build_d2_factor_set_receipt",
    "build_d2_checkpoint", "validate_d2_checkpoint", "persist_d2_candidate_chain",
    "recover_d2_candidate_chain", "d2_candidate_schema_statements",
    "install_d2_candidate_schema_draft", "d2_candidate_schema_issues",
]
