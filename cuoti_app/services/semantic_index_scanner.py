"""Deterministic semantic-index scanning and atomic run freezing.

The scanner receives a fully enumerated, last-good source snapshot.  It hashes
question content outside the database transaction, then synchronizes identities
and freezes one run in a short ``BEGIN IMMEDIATE`` transaction.  It does not
read source files, call providers, claim leases, or account for provider tokens.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from ..transactions import immediate_transaction
from .semantic_index import (
    FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
    SemanticChangeKind,
    SemanticQuestionIdentity,
    semantic_content_hash,
    semantic_metadata_hash,
)


class SemanticIndexScanError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


SEMANTIC_SCAN_ADAPTER_VERSION = 1


@dataclass(frozen=True, slots=True)
class SemanticScanCandidate:
    source_kind: str
    candidate_id: str
    subject: str
    chapter: str
    content: str | bytes
    token_estimate: int
    source_file: str | None = None
    source_year: int | None = None
    source_number: str | int | None = None
    source_note: str | None = None
    # Optional explicit Stage-4 provenance.  Existing artifacts that do not
    # publish these identities remain readable for semantic indexing, but are
    # not eligible as formal other-school generation candidates.
    source_exam_id: str | None = None
    section_id: str | None = None
    source_institution: str | None = None
    display_order: int | None = None
    excluded: bool = False
    figure_references: tuple[str | bytes, ...] = ()
    options: tuple[str | bytes, ...] = ()
    conditions: tuple[str | bytes, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedSemanticCandidate:
    identity: SemanticQuestionIdentity
    subject: str
    chapter: str
    source_file: str | None
    source_year: int | None
    source_number: str | None
    content_hash: str
    metadata_hash: str
    token_estimate: int
    excluded: bool


@dataclass(frozen=True, slots=True)
class SemanticScanClassification:
    source_kind: str
    candidate_id: str
    semantic_question_id: int
    change_kind: SemanticChangeKind


@dataclass(frozen=True, slots=True)
class SemanticScanFreezeResult:
    run_id: str
    status: str
    replayed: bool
    pending_count: int
    new_count: int
    changed_count: int
    stale_count: int
    metadata_only_count: int
    deleted_count: int
    reused_count: int
    classifications: tuple[SemanticScanClassification, ...]


def semantic_effective_concurrency(
    pending_count: int, *, requested_concurrency: int = 2
) -> int:
    if isinstance(pending_count, bool) or pending_count < 0:
        raise SemanticIndexScanError("VALIDATION", "pending_count 必须是非负整数")
    if requested_concurrency not in (1, 2):
        raise SemanticIndexScanError("VALIDATION", "requested_concurrency 只能是 1 或 2")
    return 2 if pending_count >= 501 and requested_concurrency == 2 else 1


_RUN_KINDS = frozenset({"first_full", "incremental", "rebuild"})
_ACTIVE_RUN_STATUSES = (
    "scanning",
    "queued",
    "running",
    "paused",
    "paused_budget_review",
    "paused_token_hard_limit",
)
_PENDING_CHANGE_KINDS = frozenset(
    {
        SemanticChangeKind.NEW,
        SemanticChangeKind.CONTENT_CHANGED,
        SemanticChangeKind.ANALYSIS_STALE,
        SemanticChangeKind.RETRY_PENDING,
    }
)


def _text(value: Any, field: str, *, maximum: int = 2_048) -> str:
    if not isinstance(value, str):
        raise SemanticIndexScanError("VALIDATION", f"{field} 必须是字符串")
    rendered = unicodedata.normalize("NFC", value).strip()
    if not rendered:
        raise SemanticIndexScanError("VALIDATION", f"{field} 不能为空")
    if len(rendered) > maximum:
        raise SemanticIndexScanError("VALIDATION", f"{field} 超过 {maximum} 字符")
    return rendered


def _optional_text(value: Any, field: str, *, maximum: int = 2_048) -> str | None:
    if value is None:
        return None
    return _text(str(value), field, maximum=maximum)


def _timestamp(value: Any) -> str:
    rendered = _text(value, "timestamp", maximum=128)
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise SemanticIndexScanError(
            "VALIDATION", "timestamp 必须是 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticIndexScanError("VALIDATION", "timestamp 必须包含时区")
    return rendered


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticIndexScanError("VALIDATION", f"{field} 必须是正整数")
    return value


def _source_kinds(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise SemanticIndexScanError("VALIDATION", "scanned_source_kinds 必须是序列")
    normalized = {
        SemanticQuestionIdentity(str(value), "source-scope").source_kind
        for value in values
    }
    if not normalized:
        raise SemanticIndexScanError(
            "VALIDATION", "scanned_source_kinds 至少包含一个来源"
        )
    return tuple(sorted(normalized))


def semantic_composite_source_snapshot(
    components: Mapping[str, str], *, policy_version: str
) -> str:
    """Bind every last-good source snapshot and namespace policy to one ID."""

    if not isinstance(components, Mapping) or not components:
        raise SemanticIndexScanError(
            "VALIDATION", "components 必须是非空来源 snapshot 对象"
        )
    normalized: dict[str, str] = {}
    for source_kind, snapshot_id in components.items():
        kind = SemanticQuestionIdentity(str(source_kind), "source-scope").source_kind
        if kind in normalized:
            raise SemanticIndexScanError(
                "DUPLICATE_SOURCE_KIND", "composite snapshot 来源命名空间重复"
            )
        normalized[kind] = _text(
            snapshot_id, f"components.{kind}", maximum=2_048
        )
    payload = {
        "adapter_version": SEMANTIC_SCAN_ADAPTER_VERSION,
        "policy_version": _text(policy_version, "policy_version", maximum=256),
        "components": dict(sorted(normalized.items())),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def ordinary_question_bank_scan_candidates(
    index: Any, *, scopes: Mapping[str, Iterable[str]]
) -> tuple[SemanticScanCandidate, ...]:
    """Adapt the caller's complete ordinary-bank catalog, without truncation.

    Deletion is frozen at ``source_kind`` granularity, so this API deliberately
    accepts the whole subject/chapter scope map instead of one partial slice.
    The caller must derive ``scopes`` from the immutable last-good catalog that
    contributed to the bound source snapshot.
    """

    if not isinstance(scopes, Mapping) or not scopes:
        raise SemanticIndexScanError(
            "INCOMPLETE_SOURCE_SCOPE", "ordinary scopes 必须是完整的非空科目章节映射"
        )
    adapted_by_id: dict[str, SemanticScanCandidate] = {}
    for raw_subject in sorted(scopes, key=lambda value: str(value)):
        subject = _text(str(raw_subject), "subject", maximum=320)
        chapters = scopes[raw_subject]
        if isinstance(chapters, str):
            chapters = (chapters,)
        chapter_values = tuple(
            sorted(
                {
                    _text(str(chapter), "chapter", maximum=640)
                    for chapter in chapters
                }
            )
        )
        if not chapter_values:
            raise SemanticIndexScanError(
                "INCOMPLETE_SOURCE_SCOPE", f"科目 {subject} 的 chapters 不能为空"
            )
        for row in index.all_candidates(subject, chapter_values):
            candidate_id = str(row.get("candidate_id") or "")
            adapted_by_id.setdefault(
                candidate_id,
                SemanticScanCandidate(
                    source_kind="ordinary",
                    candidate_id=candidate_id,
                    subject=subject,
                    chapter=str(row.get("chapter") or ""),
                    content=row.get("content") or "",
                    token_estimate=row.get("token_estimate"),
                    source_file=row.get("source_file"),
                    source_number=row.get("source_number"),
                    display_order=row.get("source_ordinal"),
                ),
            )
    return tuple(adapted_by_id.values())


def prepare_semantic_candidates(
    candidates: Sequence[SemanticScanCandidate],
    *,
    scanned_source_kinds: Iterable[str],
) -> tuple[tuple[PreparedSemanticCandidate, ...], tuple[str, ...]]:
    """Validate and hash one complete source snapshot without database writes."""

    source_kinds = _source_kinds(scanned_source_kinds)
    allowed_sources = set(source_kinds)
    prepared: list[PreparedSemanticCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        if not isinstance(candidate, SemanticScanCandidate):
            raise SemanticIndexScanError(
                "VALIDATION", "candidates 必须由 SemanticScanCandidate 组成"
            )
        identity = SemanticQuestionIdentity(
            candidate.source_kind, candidate.candidate_id
        )
        if identity.source_kind not in allowed_sources:
            raise SemanticIndexScanError(
                "SOURCE_SCOPE_MISMATCH", "候选题来源不在本次完整扫描范围"
            )
        if identity.key in seen:
            raise SemanticIndexScanError(
                "DUPLICATE_IDENTITY", "同一次扫描不能包含重复题目身份"
            )
        seen.add(identity.key)
        subject = _text(candidate.subject, "subject", maximum=320)
        chapter = _text(candidate.chapter, "chapter", maximum=640)
        token_estimate = _positive_integer(candidate.token_estimate, "token_estimate")
        if candidate.source_year is not None and (
            isinstance(candidate.source_year, bool)
            or not isinstance(candidate.source_year, int)
        ):
            raise SemanticIndexScanError(
                "VALIDATION", "source_year 必须是整数或空值"
            )
        if candidate.display_order is not None and (
            isinstance(candidate.display_order, bool)
            or not isinstance(candidate.display_order, int)
        ):
            raise SemanticIndexScanError(
                "VALIDATION", "display_order 必须是整数或空值"
            )
        if not isinstance(candidate.excluded, bool):
            raise SemanticIndexScanError("VALIDATION", "excluded 必须是布尔值")
        content_hash = semantic_content_hash(
            candidate.content,
            figure_references=candidate.figure_references,
            options=candidate.options,
            conditions=candidate.conditions,
        )
        metadata_hash = semantic_metadata_hash(
            subject=subject,
            chapter=chapter,
            source_file=candidate.source_file,
            source_year=candidate.source_year,
            source_number=candidate.source_number,
            source_note=candidate.source_note,
            display_order=candidate.display_order,
            active=True,
            excluded=candidate.excluded,
        )
        prepared.append(
            PreparedSemanticCandidate(
                identity=identity,
                subject=subject,
                chapter=chapter,
                source_file=_optional_text(candidate.source_file, "source_file"),
                source_year=candidate.source_year,
                source_number=_optional_text(
                    candidate.source_number, "source_number", maximum=512
                ),
                content_hash=content_hash,
                metadata_hash=metadata_hash,
                token_estimate=token_estimate,
                excluded=candidate.excluded,
            )
        )
    prepared.sort(key=lambda item: item.identity.key)
    return tuple(prepared), source_kinds


def _existing_result(row: sqlite3.Row | tuple) -> SemanticScanFreezeResult:
    return SemanticScanFreezeResult(
        run_id=str(row[0]),
        status=str(row[1]),
        replayed=True,
        pending_count=int(row[2]),
        new_count=int(row[3]),
        changed_count=int(row[4]),
        stale_count=int(row[5]),
        metadata_only_count=int(row[6]),
        deleted_count=int(row[7]),
        reused_count=int(row[8]),
        classifications=(),
    )


def _run_result_row(db: sqlite3.Connection, run_id: str):
    return db.execute(
        "SELECT id,status,pending_count,new_count,changed_count,stale_count,"
        "metadata_only_count,deleted_count,reused_count "
        "FROM semantic_index_runs WHERE id=?",
        (run_id,),
    ).fetchone()


def freeze_semantic_scan(
    db: sqlite3.Connection,
    *,
    run_id: str,
    run_kind: str,
    source_snapshot: str,
    analysis_schema_version: int,
    prompt_version: str,
    model_role: str,
    model_name: str,
    reasoning_effort: str,
    candidates: Sequence[SemanticScanCandidate],
    scanned_source_kinds: Iterable[str],
    timestamp: str,
    requested_concurrency: int = 2,
) -> SemanticScanFreezeResult:
    """Synchronize one complete scan and freeze its provider work atomically."""

    run_id = _text(run_id, "run_id", maximum=256)
    run_kind = _text(run_kind, "run_kind", maximum=32)
    if run_kind not in _RUN_KINDS:
        raise SemanticIndexScanError("VALIDATION", "run_kind 无效")
    source_snapshot = _text(source_snapshot, "source_snapshot")
    analysis_schema_version = _positive_integer(
        analysis_schema_version, "analysis_schema_version"
    )
    prompt_version = _text(prompt_version, "prompt_version", maximum=256)
    model_role = _text(model_role, "model_role", maximum=128)
    model_name = _text(model_name, "model_name", maximum=256)
    reasoning_effort = _text(reasoning_effort, "reasoning_effort", maximum=128)
    timestamp = _timestamp(timestamp)
    prepared, source_kinds = prepare_semantic_candidates(
        candidates, scanned_source_kinds=scanned_source_kinds
    )
    source_placeholders = ",".join("?" for _ in source_kinds)

    try:
        with immediate_transaction(db):
            same_id = db.execute(
                "SELECT id,run_kind,source_snapshot,analysis_schema_version,"
                "prompt_version,model_role,model_name,reasoning_effort "
                "FROM semantic_index_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            expected_identity = (
                run_kind,
                source_snapshot,
                analysis_schema_version,
                prompt_version,
                model_role,
                model_name,
                reasoning_effort,
            )
            if same_id is not None:
                if tuple(same_id[1:]) != expected_identity:
                    raise SemanticIndexScanError(
                        "RUN_ID_CONFLICT", "run_id 已属于不同的冻结运行"
                    )
                return _existing_result(_run_result_row(db, run_id))

            active = db.execute(
                "SELECT id,model_role FROM semantic_index_runs WHERE run_kind=? "
                "AND source_snapshot=? AND analysis_schema_version=? "
                "AND prompt_version=? AND model_name=? AND reasoning_effort=? "
                f"AND status IN ({','.join('?' for _ in _ACTIVE_RUN_STATUSES)})",
                (
                    run_kind,
                    source_snapshot,
                    analysis_schema_version,
                    prompt_version,
                    model_name,
                    reasoning_effort,
                    *_ACTIVE_RUN_STATUSES,
                ),
            ).fetchone()
            if active is not None:
                if active[1] != model_role:
                    raise SemanticIndexScanError(
                        "ACTIVE_RUN_CONFLICT",
                        "相同冻结身份的活动 run 使用不同模型角色",
                    )
                return _existing_result(_run_result_row(db, str(active[0])))

            other_active = db.execute(
                "SELECT id FROM semantic_index_runs "
                f"WHERE status IN ({','.join('?' for _ in _ACTIVE_RUN_STATUSES)}) "
                "ORDER BY created_at,id LIMIT 1",
                _ACTIVE_RUN_STATUSES,
            ).fetchone()
            if other_active is not None:
                raise SemanticIndexScanError(
                    "SCAN_RUN_BUSY",
                    f"已有活动语义扫描 {other_active[0]}，不能并行冻结新 snapshot",
                )

            existing_rows = db.execute(
                "SELECT q.id,q.source_kind,q.candidate_id,q.content_hash,"
                "q.metadata_hash,q.active,q.excluded,q.current_card_version_id,"
                "c.semantic_question_id,c.content_hash,c.analysis_schema_version,"
                "c.prompt_version,c.model_role,c.model_name,c.reasoning_effort,c.status "
                "FROM semantic_questions q LEFT JOIN semantic_card_versions c "
                "ON c.id=q.current_card_version_id "
                f"WHERE q.source_kind IN ({source_placeholders})",
                source_kinds,
            ).fetchall()
            existing = {(str(row[1]), str(row[2])): row for row in existing_rows}
            has_deprecations = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='semantic_question_deprecations'"
            ).fetchone()
            deprecated_ids = (
                {
                    int(row[0])
                    for row in db.execute(
                        "SELECT semantic_question_id FROM semantic_question_deprecations"
                    ).fetchall()
                }
                if has_deprecations
                else set()
            )
            for row in existing_rows:
                question_id = int(row[0])
                if bool(int(row[6])) != (question_id in deprecated_ids):
                    raise SemanticIndexScanError(
                        "GRANDFATHERED_EXCLUSION_STATE_DRIFT",
                        "历史永久排除账本与 excluded 标志不一致，扫描不得改写",
                    )
            latest_item_status: dict[
                tuple[int, str, int, str, str, str, str], str
            ] = {}
            for row in db.execute(
                "SELECT ri.semantic_question_id,ri.expected_content_hash,"
                "r.analysis_schema_version,r.prompt_version,r.model_role,r.model_name,"
                "r.reasoning_effort,ri.status FROM semantic_index_run_items ri "
                "JOIN semantic_index_runs r ON r.id=ri.run_id "
                "ORDER BY julianday(r.created_at) DESC,r.id DESC"
            ).fetchall():
                latest_item_status.setdefault(
                    (
                        int(row[0]),
                        str(row[1]),
                        int(row[2]),
                        str(row[3]),
                        str(row[4]),
                        str(row[5]),
                        str(row[6]),
                    ),
                    str(row[7]),
                )

            classifications: list[SemanticScanClassification] = []
            pending: list[tuple[int, str, SemanticChangeKind]] = []
            seen_keys: set[tuple[str, str]] = set()
            for candidate in prepared:
                key = candidate.identity.key
                seen_keys.add(key)
                old = existing.get(key)
                if (
                    old is not None
                    and int(old[0]) in deprecated_ids
                    and str(old[3]) != candidate.content_hash
                ):
                    raise SemanticIndexScanError(
                        "GRANDFATHERED_EXCLUSION_CONTENT_DRIFT",
                        "历史永久排除不能迁移到新的题目内容身份",
                    )
                if candidate.excluded and not (
                    old is not None
                    and int(old[6]) == 1
                    and int(old[0]) in deprecated_ids
                ):
                    raise SemanticIndexScanError(
                        "FUTURE_PERMANENT_EXCLUSION_FORBIDDEN",
                        "扫描不得新增永久题目排除；当次不合规只能绑定 generation_job_id",
                    )
                if old is None:
                    change_kind = SemanticChangeKind.NEW
                    db.execute(
                        "INSERT INTO semantic_questions("
                        "source_kind,candidate_id,subject,chapter,source_file,source_year,"
                        "source_number,content_hash,metadata_hash,token_estimate,active,"
                        "excluded,first_seen_snapshot,last_seen_snapshot,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            candidate.identity.source_kind,
                            candidate.identity.candidate_id,
                            candidate.subject,
                            candidate.chapter,
                            candidate.source_file,
                            candidate.source_year,
                            candidate.source_number,
                            candidate.content_hash,
                            candidate.metadata_hash,
                            candidate.token_estimate,
                            1,
                            0,
                            source_snapshot,
                            source_snapshot,
                            timestamp,
                            timestamp,
                        ),
                    )
                    question_id = int(db.execute("SELECT last_insert_rowid()").fetchone()[0])
                else:
                    question_id = int(old[0])
                    effective_excluded = candidate.excluded or question_id in deprecated_ids
                    if effective_excluded:
                        change_kind = SemanticChangeKind.EXCLUDED
                    elif str(old[3]) != candidate.content_hash:
                        change_kind = SemanticChangeKind.CONTENT_CHANGED
                    else:
                        card_is_current = (
                            old[7] is not None
                            and old[8] == question_id
                            and old[9] == candidate.content_hash
                            and old[15] == "valid"
                        )
                        last_status = latest_item_status.get(
                            (
                                question_id,
                                candidate.content_hash,
                                analysis_schema_version,
                                prompt_version,
                                model_role,
                                model_name,
                                reasoning_effort,
                            )
                        )
                        if not card_is_current and last_status in {"failed", "retry_wait"}:
                            change_kind = SemanticChangeKind.RETRY_PENDING
                        elif not card_is_current or (
                            int(old[10]) != analysis_schema_version
                            or str(old[11]) != prompt_version
                            or str(old[12]) != model_role
                            or str(old[13]) != model_name
                            or str(old[14]) != reasoning_effort
                        ):
                            change_kind = SemanticChangeKind.ANALYSIS_STALE
                        elif str(old[4]) != candidate.metadata_hash:
                            change_kind = SemanticChangeKind.METADATA_CHANGED
                        else:
                            change_kind = SemanticChangeKind.UNCHANGED
                    db.execute(
                        "UPDATE semantic_questions SET subject=?,chapter=?,source_file=?,"
                        "source_year=?,source_number=?,content_hash=?,metadata_hash=?,"
                        "token_estimate=?,active=1,excluded=?,last_seen_snapshot=?,updated_at=? "
                        "WHERE id=?",
                        (
                            candidate.subject,
                            candidate.chapter,
                            candidate.source_file,
                            candidate.source_year,
                            candidate.source_number,
                            candidate.content_hash,
                            candidate.metadata_hash,
                            candidate.token_estimate,
                            int(effective_excluded),
                            source_snapshot,
                            timestamp,
                            question_id,
                        ),
                    )
                classifications.append(
                    SemanticScanClassification(
                        source_kind=key[0],
                        candidate_id=key[1],
                        semantic_question_id=question_id,
                        change_kind=change_kind,
                    )
                )
                if change_kind in _PENDING_CHANGE_KINDS:
                    pending.append((question_id, candidate.content_hash, change_kind))

            for key, old in sorted(existing.items()):
                if key in seen_keys:
                    continue
                question_id = int(old[0])
                db.execute(
                    "UPDATE semantic_questions SET active=0,excluded=?,updated_at=? "
                    "WHERE id=?",
                    (int(old[6]), timestamp, question_id),
                )
                classifications.append(
                    SemanticScanClassification(
                        source_kind=key[0],
                        candidate_id=key[1],
                        semantic_question_id=question_id,
                        change_kind=SemanticChangeKind.DELETED,
                    )
                )

            classifications.sort(
                key=lambda item: (item.source_kind, item.candidate_id)
            )
            new_count = sum(
                item.change_kind is SemanticChangeKind.NEW for item in classifications
            )
            changed_count = sum(
                item.change_kind is SemanticChangeKind.CONTENT_CHANGED
                for item in classifications
            )
            stale_count = sum(
                item.change_kind
                in {SemanticChangeKind.ANALYSIS_STALE, SemanticChangeKind.RETRY_PENDING}
                for item in classifications
            )
            metadata_only_count = sum(
                item.change_kind
                in {SemanticChangeKind.METADATA_CHANGED, SemanticChangeKind.EXCLUDED}
                for item in classifications
            )
            deleted_count = sum(
                item.change_kind is SemanticChangeKind.DELETED
                for item in classifications
            )
            reused_count = sum(
                item.change_kind is SemanticChangeKind.UNCHANGED
                for item in classifications
            )
            effective_concurrency = semantic_effective_concurrency(
                len(pending), requested_concurrency=requested_concurrency
            )
            db.execute(
                "INSERT INTO semantic_index_runs("
                "id,run_kind,source_snapshot,status,analysis_schema_version,"
                "prompt_version,model_role,model_name,reasoning_effort,pending_count,"
                "new_count,changed_count,stale_count,metadata_only_count,deleted_count,"
                "reused_count,requested_concurrency,effective_concurrency,"
                "token_initial_limit,token_current_limit,token_hard_limit,token_remaining,"
                "created_at,updated_at) VALUES(?,?,?,'scanning',?,?,?,?,?,?,?,?,?,?,?,?,"
                "?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    run_kind,
                    source_snapshot,
                    analysis_schema_version,
                    prompt_version,
                    model_role,
                    model_name,
                    reasoning_effort,
                    len(pending),
                    new_count,
                    changed_count,
                    stale_count,
                    metadata_only_count,
                    deleted_count,
                    reused_count,
                    requested_concurrency,
                    effective_concurrency,
                    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                    FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
                    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                    timestamp,
                    timestamp,
                ),
            )
            for question_id, expected_hash, change_kind in pending:
                db.execute(
                    "INSERT INTO semantic_index_run_items("
                    "run_id,semantic_question_id,expected_content_hash,change_kind,status,"
                    "available_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",
                    (
                        run_id,
                        question_id,
                        expected_hash,
                        change_kind.value,
                        timestamp,
                        timestamp,
                    ),
                )
            db.execute(
                "UPDATE semantic_index_runs SET status='queued',updated_at=? WHERE id=?",
                (timestamp, run_id),
            )
    except sqlite3.IntegrityError as exc:
        raise SemanticIndexScanError(
            "SCAN_FREEZE_CONFLICT", "扫描冻结违反数据库约束"
        ) from exc

    return SemanticScanFreezeResult(
        run_id=run_id,
        status="queued",
        replayed=False,
        pending_count=len(pending),
        new_count=new_count,
        changed_count=changed_count,
        stale_count=stale_count,
        metadata_only_count=metadata_only_count,
        deleted_count=deleted_count,
        reused_count=reused_count,
        classifications=tuple(classifications),
    )
