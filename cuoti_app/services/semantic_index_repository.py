"""Transaction-owned persistence for the permanent semantic index.

This repository only persists already validated domain data.  It deliberately
does not scan the question bank, claim leases, call an AI provider, account for
provider tokens, publish jobs, or participate in exam generation.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from ..transactions import immediate_transaction
from .semantic_index import (
    CARD_LIST_FIELDS,
    CARD_TEXT_FIELDS,
    FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
    FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
    SemanticChangeKind,
    SemanticQuestionIdentity,
    ValidatedSemanticCard,
    validate_semantic_card,
)


AUDITED_SNAPSHOT_AGGREGATE_ROLE = "audited_snapshot_aggregate"
MIXED_AUDITED_IDENTITY_POLICY = "mixed_audited"


class SemanticIndexRepositoryError(RuntimeError):
    """A persistence invariant was not satisfied."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FrozenRunItem:
    semantic_question_id: int
    expected_content_hash: str
    change_kind: SemanticChangeKind | str


@dataclass(frozen=True, slots=True)
class SnapshotMember:
    semantic_question_id: int
    card_version_id: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class StoredCard:
    card_version_id: int
    previous_card_version_id: int | None
    reused: bool


_PENDING_CHANGE_KINDS = frozenset(
    {
        SemanticChangeKind.NEW,
        SemanticChangeKind.CONTENT_CHANGED,
        SemanticChangeKind.ANALYSIS_STALE,
        SemanticChangeKind.RETRY_PENDING,
    }
)
_RUN_KINDS = frozenset({"first_full", "incremental", "rebuild"})
_SNAPSHOT_STATUSES = frozenset({"partial", "complete"})
_HEX_DIGITS = frozenset("0123456789abcdef")


def _required_text(value: Any, field: str, *, max_chars: int = 2_048) -> str:
    if not isinstance(value, str):
        raise SemanticIndexRepositoryError("VALIDATION", f"{field} 必须是字符串")
    rendered = value.strip()
    if not rendered:
        raise SemanticIndexRepositoryError("VALIDATION", f"{field} 不能为空")
    if len(rendered) > max_chars:
        raise SemanticIndexRepositoryError(
            "VALIDATION", f"{field} 超过 {max_chars} 字符"
        )
    return rendered


def _optional_text(value: Any, field: str, *, max_chars: int = 2_048) -> str | None:
    if value is None:
        return None
    return _required_text(str(value), field, max_chars=max_chars)


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SemanticIndexRepositoryError("VALIDATION", f"{field} 必须是正整数")
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SemanticIndexRepositoryError("VALIDATION", f"{field} 必须是非负整数")
    return value


def _sha256(value: Any, field: str) -> str:
    rendered = _required_text(value, field, max_chars=64).casefold()
    if len(rendered) != 64 or any(char not in _HEX_DIGITS for char in rendered):
        raise SemanticIndexRepositoryError(
            "VALIDATION", f"{field} 必须是 64 位十六进制 SHA-256"
        )
    return rendered


def _timestamp(value: Any, field: str = "timestamp") -> str:
    rendered = _required_text(value, field, max_chars=128)
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise SemanticIndexRepositoryError(
            "VALIDATION", f"{field} 必须是 ISO-8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SemanticIndexRepositoryError(
            "VALIDATION", f"{field} 必须包含时区"
        )
    return rendered


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _fetch_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def _normalized_terms(card: ValidatedSemanticCard) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for field in (*CARD_LIST_FIELDS, *CARD_TEXT_FIELDS):
        value = getattr(card, field)
        values = value if isinstance(value, tuple) else (value,)
        for raw in values:
            term = " ".join(str(raw).split()).casefold()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    return _canonical_json(terms)


class SemanticIndexRepository:
    """Own short SQLite write transactions for immutable semantic-index data."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def get_question(
        self, identity: SemanticQuestionIdentity
    ) -> dict[str, Any] | None:
        identity = SemanticQuestionIdentity(identity.source_kind, identity.candidate_id)
        return _fetch_dict(
            self.db.execute(
                "SELECT * FROM semantic_questions WHERE source_kind=? AND candidate_id=?",
                identity.key,
            )
        )

    def upsert_question(
        self,
        *,
        identity: SemanticQuestionIdentity,
        subject: str,
        chapter: str,
        content_hash: str,
        metadata_hash: str,
        token_estimate: int,
        source_snapshot: str,
        timestamp: str,
        source_file: str | None = None,
        source_year: int | None = None,
        source_number: str | int | None = None,
        active: bool = True,
        excluded: bool = False,
    ) -> int:
        identity = SemanticQuestionIdentity(identity.source_kind, identity.candidate_id)
        subject = _required_text(subject, "subject", max_chars=320)
        chapter = _required_text(chapter, "chapter", max_chars=640)
        content_hash = _sha256(content_hash, "content_hash")
        metadata_hash = _sha256(metadata_hash, "metadata_hash")
        token_estimate = _positive_integer(token_estimate, "token_estimate")
        source_snapshot = _required_text(source_snapshot, "source_snapshot")
        timestamp = _timestamp(timestamp)
        source_file = _optional_text(source_file, "source_file")
        source_number = _optional_text(source_number, "source_number", max_chars=512)
        if source_year is not None and (
            isinstance(source_year, bool) or not isinstance(source_year, int)
        ):
            raise SemanticIndexRepositoryError(
                "VALIDATION", "source_year 必须是整数或空值"
            )
        if not isinstance(active, bool) or not isinstance(excluded, bool):
            raise SemanticIndexRepositoryError(
                "VALIDATION", "active 和 excluded 必须是布尔值"
            )

        with immediate_transaction(self.db):
            existing = self.db.execute(
                "SELECT id,excluded,content_hash FROM semantic_questions "
                "WHERE source_kind=? AND candidate_id=?",
                identity.key,
            ).fetchone()
            has_deprecation_table = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='semantic_question_deprecations'"
            ).fetchone() is not None
            has_grandfathered_deprecation = bool(
                existing is not None
                and has_deprecation_table
                and self.db.execute(
                    "SELECT 1 FROM semantic_question_deprecations "
                    "WHERE semantic_question_id=?",
                    (int(existing[0]),),
                ).fetchone() is not None
            )
            existing_excluded = bool(existing is not None and int(existing[1]) == 1)
            if (
                has_grandfathered_deprecation
                and str(existing[2]) != content_hash
            ):
                raise SemanticIndexRepositoryError(
                    "GRANDFATHERED_EXCLUSION_CONTENT_DRIFT",
                    "历史永久排除不能迁移到新的题目内容身份",
                )
            if excluded and not (
                existing_excluded and has_grandfathered_deprecation
            ):
                raise SemanticIndexRepositoryError(
                    "FUTURE_PERMANENT_EXCLUSION_FORBIDDEN",
                    "不得新增永久题目排除；当次不合规只能绑定 generation_job_id",
                )
            if not excluded and (
                existing_excluded or has_grandfathered_deprecation
            ):
                raise SemanticIndexRepositoryError(
                    "GRANDFATHERED_EXCLUSION_IMMUTABLE",
                    "历史永久排除只能原样回放，不能由 upsert 静默恢复或重分类",
                )
            self.db.execute(
                "INSERT INTO semantic_questions("
                "source_kind,candidate_id,subject,chapter,source_file,source_year,"
                "source_number,content_hash,metadata_hash,token_estimate,active,excluded,"
                "first_seen_snapshot,last_seen_snapshot,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_kind,candidate_id) DO UPDATE SET "
                "subject=excluded.subject,chapter=excluded.chapter,"
                "source_file=excluded.source_file,source_year=excluded.source_year,"
                "source_number=excluded.source_number,content_hash=excluded.content_hash,"
                "metadata_hash=excluded.metadata_hash,token_estimate=excluded.token_estimate,"
                "active=excluded.active,excluded=excluded.excluded,"
                "last_seen_snapshot=excluded.last_seen_snapshot,updated_at=excluded.updated_at",
                (
                    identity.source_kind,
                    identity.candidate_id,
                    subject,
                    chapter,
                    source_file,
                    source_year,
                    source_number,
                    content_hash,
                    metadata_hash,
                    token_estimate,
                    int(active),
                    int(excluded),
                    source_snapshot,
                    source_snapshot,
                    timestamp,
                    timestamp,
                ),
            )
            row = self.db.execute(
                "SELECT id FROM semantic_questions WHERE source_kind=? AND candidate_id=?",
                identity.key,
            ).fetchone()
            if row is None:
                raise SemanticIndexRepositoryError(
                    "PERSISTENCE", "题目身份写入后不可见"
                )
            return int(row[0])

    def store_valid_card(
        self,
        *,
        semantic_question_id: int,
        expected_content_hash: str,
        card: ValidatedSemanticCard | Mapping[str, Any],
        analysis_schema_version: int,
        prompt_version: str,
        model_role: str,
        model_name: str,
        reasoning_effort: str,
        timestamp: str,
        provider_request_id_hash: str | None = None,
    ) -> StoredCard:
        question_id = _positive_integer(semantic_question_id, "semantic_question_id")
        expected_hash = _sha256(expected_content_hash, "expected_content_hash")
        analysis_schema_version = _positive_integer(
            analysis_schema_version, "analysis_schema_version"
        )
        prompt_version = _required_text(prompt_version, "prompt_version", max_chars=256)
        model_role = _required_text(model_role, "model_role", max_chars=128)
        model_name = _required_text(model_name, "model_name", max_chars=256)
        reasoning_effort = _required_text(
            reasoning_effort, "reasoning_effort", max_chars=128
        )
        timestamp = _timestamp(timestamp)
        if provider_request_id_hash is not None:
            provider_request_id_hash = _sha256(
                provider_request_id_hash, "provider_request_id_hash"
            )

        with immediate_transaction(self.db):
            return self._store_valid_card_in_transaction(
                question_id=question_id,
                expected_hash=expected_hash,
                card=card,
                analysis_schema_version=analysis_schema_version,
                prompt_version=prompt_version,
                model_role=model_role,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                timestamp=timestamp,
                provider_request_id_hash=provider_request_id_hash,
            )

    def _store_valid_card_in_transaction(
        self,
        *,
        question_id: int,
        expected_hash: str,
        card: ValidatedSemanticCard | Mapping[str, Any],
        analysis_schema_version: int,
        prompt_version: str,
        model_role: str,
        model_name: str,
        reasoning_effort: str,
        timestamp: str,
        provider_request_id_hash: str | None,
    ) -> StoredCard:
        """Store a card inside a caller-owned transaction.

        Only semantic checkpoint orchestration may use this helper; ordinary
        callers must use :meth:`store_valid_card`, which owns rollback/commit.
        """

        if not self.db.in_transaction:
            raise RuntimeError("card transaction helper requires an active transaction")
        question = self.db.execute(
            "SELECT candidate_id,content_hash,current_card_version_id "
            "FROM semantic_questions WHERE id=?",
            (question_id,),
        ).fetchone()
        if question is None:
            raise SemanticIndexRepositoryError("NOT_FOUND", "语义题目不存在")
        if question[1] != expected_hash:
            raise SemanticIndexRepositoryError(
                "QUESTION_CONTENT_CHANGED", "题目内容哈希已变化"
            )
        validated = validate_semantic_card(
            card.as_payload() if isinstance(card, ValidatedSemanticCard) else card,
            expected_candidate_id=question[0],
        )
        card_json = _canonical_json(validated.as_payload())
        terms_json = _normalized_terms(validated)
        insert = self.db.execute(
            "INSERT INTO semantic_card_versions("
            "semantic_question_id,content_hash,analysis_schema_version,"
            "prompt_version,model_role,model_name,reasoning_effort,card_json,"
            "normalized_terms_json,card_hash,status,provider_request_id_hash,"
            "analyzed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(semantic_question_id,content_hash,analysis_schema_version,"
            "prompt_version,model_name,reasoning_effort) DO NOTHING",
            (
                question_id,
                expected_hash,
                analysis_schema_version,
                prompt_version,
                model_role,
                model_name,
                reasoning_effort,
                card_json,
                terms_json,
                validated.card_hash,
                "valid",
                provider_request_id_hash,
                timestamp,
            ),
        )
        stored = self.db.execute(
            "SELECT id,model_role,card_json,normalized_terms_json,card_hash "
            "FROM semantic_card_versions WHERE semantic_question_id=? "
            "AND content_hash=? AND analysis_schema_version=? AND prompt_version=? "
            "AND model_name=? AND reasoning_effort=?",
            (
                question_id,
                expected_hash,
                analysis_schema_version,
                prompt_version,
                model_name,
                reasoning_effort,
            ),
        ).fetchone()
        if stored is None:
            raise SemanticIndexRepositoryError(
                "PERSISTENCE", "材料卡写入后不可见"
            )
        if (stored[1], stored[2], stored[3], stored[4]) != (
            model_role,
            card_json,
            terms_json,
            validated.card_hash,
        ):
            raise SemanticIndexRepositoryError(
                "CARD_VERSION_CONFLICT", "相同卡版本键对应不同的不可变内容"
            )
        card_version_id = int(stored[0])
        previous_card_version_id = (
            int(question[2]) if question[2] is not None else None
        )
        self.db.execute(
            "UPDATE semantic_card_versions SET status='superseded',superseded_at=? "
            "WHERE semantic_question_id=? AND status='valid' AND id<>?",
            (timestamp, question_id, card_version_id),
        )
        self.db.execute(
            "UPDATE semantic_card_versions SET status='valid',superseded_at=NULL "
            "WHERE id=?",
            (card_version_id,),
        )
        changed = self.db.execute(
            "UPDATE semantic_questions SET current_card_version_id=?,updated_at=? "
            "WHERE id=? AND content_hash=?",
            (card_version_id, timestamp, question_id, expected_hash),
        ).rowcount
        if changed != 1:
            raise SemanticIndexRepositoryError(
                "QUESTION_CONTENT_CHANGED", "切换材料卡前题目内容已变化"
            )
        return StoredCard(
            card_version_id=card_version_id,
            previous_card_version_id=previous_card_version_id,
            reused=insert.rowcount == 0,
        )

    def create_run(
        self,
        *,
        run_id: str,
        run_kind: str,
        source_snapshot: str,
        analysis_schema_version: int,
        prompt_version: str,
        model_role: str,
        model_name: str,
        reasoning_effort: str,
        items: Sequence[FrozenRunItem],
        timestamp: str,
        metadata_only_count: int = 0,
        deleted_count: int = 0,
        reused_count: int = 0,
    ) -> str:
        run_id = _required_text(run_id, "run_id", max_chars=256)
        run_kind = _required_text(run_kind, "run_kind", max_chars=32)
        if run_kind not in _RUN_KINDS:
            raise SemanticIndexRepositoryError("VALIDATION", "run_kind 无效")
        source_snapshot = _required_text(source_snapshot, "source_snapshot")
        analysis_schema_version = _positive_integer(
            analysis_schema_version, "analysis_schema_version"
        )
        prompt_version = _required_text(prompt_version, "prompt_version", max_chars=256)
        model_role = _required_text(model_role, "model_role", max_chars=128)
        model_name = _required_text(model_name, "model_name", max_chars=256)
        reasoning_effort = _required_text(
            reasoning_effort, "reasoning_effort", max_chars=128
        )
        timestamp = _timestamp(timestamp)
        metadata_only_count = _nonnegative_integer(
            metadata_only_count, "metadata_only_count"
        )
        deleted_count = _nonnegative_integer(deleted_count, "deleted_count")
        reused_count = _nonnegative_integer(reused_count, "reused_count")
        frozen_items: list[tuple[int, str, SemanticChangeKind]] = []
        seen_question_ids: set[int] = set()
        for item in items:
            question_id = _positive_integer(
                item.semantic_question_id, "semantic_question_id"
            )
            if question_id in seen_question_ids:
                raise SemanticIndexRepositoryError(
                    "DUPLICATE_RUN_ITEM", "同一 run 不能冻结重复题目"
                )
            seen_question_ids.add(question_id)
            expected_hash = _sha256(
                item.expected_content_hash, "expected_content_hash"
            )
            try:
                change_kind = SemanticChangeKind(item.change_kind)
            except (TypeError, ValueError) as exc:
                raise SemanticIndexRepositoryError(
                    "VALIDATION", "change_kind 无效"
                ) from exc
            if change_kind not in _PENDING_CHANGE_KINDS:
                raise SemanticIndexRepositoryError(
                    "VALIDATION", "run item 只允许冻结真实待分析变化"
                )
            frozen_items.append((question_id, expected_hash, change_kind))

        new_count = sum(kind is SemanticChangeKind.NEW for _, _, kind in frozen_items)
        changed_count = sum(
            kind is SemanticChangeKind.CONTENT_CHANGED for _, _, kind in frozen_items
        )
        stale_count = sum(
            kind in {SemanticChangeKind.ANALYSIS_STALE, SemanticChangeKind.RETRY_PENDING}
            for _, _, kind in frozen_items
        )
        try:
            with immediate_transaction(self.db):
                self.db.execute(
                    "INSERT INTO semantic_index_runs("
                    "id,run_kind,source_snapshot,status,analysis_schema_version,"
                    "prompt_version,model_role,model_name,reasoning_effort,pending_count,"
                    "new_count,changed_count,stale_count,metadata_only_count,deleted_count,"
                    "reused_count,requested_concurrency,effective_concurrency,"
                    "token_initial_limit,token_current_limit,token_hard_limit,"
                    "token_remaining,created_at,updated_at"
                    ") VALUES(?,?,?,'scanning',?,?,?,?,?,?,?,?,?,?,?,?,1,1,?,?,?,?,?,?)",
                    (
                        run_id,
                        run_kind,
                        source_snapshot,
                        analysis_schema_version,
                        prompt_version,
                        model_role,
                        model_name,
                        reasoning_effort,
                        len(frozen_items),
                        new_count,
                        changed_count,
                        stale_count,
                        metadata_only_count,
                        deleted_count,
                        reused_count,
                        FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                        FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                        FIRST_FULL_BUILD_HARD_TOKEN_LIMIT,
                        FIRST_FULL_BUILD_INITIAL_TOKEN_LIMIT,
                        timestamp,
                        timestamp,
                    ),
                )
                for question_id, expected_hash, change_kind in frozen_items:
                    question = self.db.execute(
                        "SELECT content_hash,active,excluded,last_seen_snapshot "
                        "FROM semantic_questions WHERE id=?",
                        (question_id,),
                    ).fetchone()
                    if question is None:
                        raise SemanticIndexRepositoryError(
                            "NOT_FOUND", "run item 对应题目不存在"
                        )
                    if question[0] != expected_hash:
                        raise SemanticIndexRepositoryError(
                            "QUESTION_CONTENT_CHANGED", "冻结前题目内容已变化"
                        )
                    if not question[1] or question[2]:
                        raise SemanticIndexRepositoryError(
                            "QUESTION_NOT_ELIGIBLE", "冻结题目当前不可分析"
                        )
                    if question[3] != source_snapshot:
                        raise SemanticIndexRepositoryError(
                            "SOURCE_SNAPSHOT_CHANGED", "冻结前题目来源快照已变化"
                        )
                    self.db.execute(
                        "INSERT INTO semantic_index_run_items("
                        "run_id,semantic_question_id,expected_content_hash,change_kind,"
                        "status,available_at,updated_at) VALUES(?,?,?,?,'pending',?,?)",
                        (
                            run_id,
                            question_id,
                            expected_hash,
                            change_kind.value,
                            timestamp,
                            timestamp,
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise SemanticIndexRepositoryError(
                "RUN_CONFLICT", "run 身份或冻结清单发生冲突"
            ) from exc
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        run_id = _required_text(run_id, "run_id", max_chars=256)
        return _fetch_dict(
            self.db.execute("SELECT * FROM semantic_index_runs WHERE id=?", (run_id,))
        )

    def list_run_items(self, run_id: str) -> list[dict[str, Any]]:
        run_id = _required_text(run_id, "run_id", max_chars=256)
        cursor = self.db.execute(
            "SELECT * FROM semantic_index_run_items WHERE run_id=? "
            "ORDER BY semantic_question_id",
            (run_id,),
        )
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def publish_snapshot(
        self,
        *,
        snapshot_id: str,
        run_id: str,
        source_snapshot: str,
        status: str,
        members: Sequence[SnapshotMember],
        eligible_count: int,
        stale_card_count: int,
        missing_card_count: int,
        analysis_schema_version: int,
        timestamp: str,
    ) -> str:
        snapshot_id = _required_text(snapshot_id, "snapshot_id", max_chars=256)
        run_id = _required_text(run_id, "run_id", max_chars=256)
        source_snapshot = _required_text(source_snapshot, "source_snapshot")
        status = _required_text(status, "status", max_chars=32)
        if status not in _SNAPSHOT_STATUSES:
            raise SemanticIndexRepositoryError("VALIDATION", "snapshot status 无效")
        eligible_count = _nonnegative_integer(eligible_count, "eligible_count")
        stale_card_count = _nonnegative_integer(
            stale_card_count, "stale_card_count"
        )
        missing_card_count = _nonnegative_integer(
            missing_card_count, "missing_card_count"
        )
        analysis_schema_version = _positive_integer(
            analysis_schema_version, "analysis_schema_version"
        )
        timestamp = _timestamp(timestamp)
        valid_card_count = len(members)
        if valid_card_count + stale_card_count + missing_card_count != eligible_count:
            raise SemanticIndexRepositoryError(
                "SNAPSHOT_COUNT_MISMATCH", "snapshot 覆盖计数之和不等于 eligible_count"
            )
        if status == "complete" and (stale_card_count or missing_card_count):
            raise SemanticIndexRepositoryError(
                "SNAPSHOT_INCOMPLETE", "complete snapshot 不能包含 stale 或 missing"
            )
        coverage_ratio = (
            valid_card_count / eligible_count if eligible_count else 1.0
        )
        normalized_members: list[tuple[int, int, str]] = []
        seen_question_ids: set[int] = set()
        for member in members:
            question_id = _positive_integer(
                member.semantic_question_id, "semantic_question_id"
            )
            card_version_id = _positive_integer(
                member.card_version_id, "card_version_id"
            )
            if question_id in seen_question_ids:
                raise SemanticIndexRepositoryError(
                    "DUPLICATE_SNAPSHOT_ITEM", "snapshot 不能包含重复题目"
                )
            seen_question_ids.add(question_id)
            normalized_members.append(
                (question_id, card_version_id, _sha256(member.content_hash, "content_hash"))
            )

        try:
            with immediate_transaction(self.db):
                existing = self.db.execute(
                    "SELECT 1 FROM semantic_index_snapshots WHERE id=?",
                    (snapshot_id,),
                ).fetchone()
                if existing:
                    raise SemanticIndexRepositoryError(
                        "SNAPSHOT_IMMUTABLE", "已发布 snapshot 不允许覆盖"
                    )
                run = self.db.execute(
                    "SELECT source_snapshot,status,analysis_schema_version,"
                    "prompt_version,model_role,model_name,reasoning_effort "
                    "FROM semantic_index_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise SemanticIndexRepositoryError("NOT_FOUND", "run 不存在")
                if run[0] != source_snapshot or run[2] != analysis_schema_version:
                    raise SemanticIndexRepositoryError(
                        "RUN_SNAPSHOT_MISMATCH", "snapshot 与冻结 run 身份不一致"
                    )
                allowed_statuses = (
                    {"running", "succeeded"}
                    if status == "complete"
                    else {
                        "running",
                        "paused",
                        "paused_budget_review",
                        "paused_token_hard_limit",
                        "succeeded",
                        "failed",
                    }
                )
                if run[1] not in allowed_statuses:
                    raise SemanticIndexRepositoryError(
                        "RUN_NOT_PUBLISHABLE", "run 当前状态不允许发布该 snapshot"
                    )
                if status == "complete":
                    unfinished = self.db.execute(
                        "SELECT COUNT(*) FROM semantic_index_run_items WHERE run_id=? "
                        "AND status NOT IN ('succeeded','skipped')",
                        (run_id,),
                    ).fetchone()[0]
                    if unfinished:
                        raise SemanticIndexRepositoryError(
                            "RUN_ITEMS_INCOMPLETE",
                            "complete snapshot 要求冻结项全部 succeeded 或 skipped",
                        )
                actual_eligible_count = self.db.execute(
                    "SELECT COUNT(*) FROM semantic_questions WHERE active=1 "
                    "AND excluded=0 AND last_seen_snapshot=?",
                    (source_snapshot,),
                ).fetchone()[0]
                if actual_eligible_count != eligible_count:
                    raise SemanticIndexRepositoryError(
                        "SNAPSHOT_ELIGIBLE_COUNT_MISMATCH",
                        "eligible_count 与来源快照当前有效题数不一致",
                    )
                self.db.execute(
                    "INSERT INTO semantic_index_snapshots("
                    "id,source_snapshot,run_id,status,eligible_count,valid_card_count,"
                    "stale_card_count,missing_card_count,coverage_ratio,"
                    "analysis_schema_version,created_at,published_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        source_snapshot,
                        run_id,
                        status,
                        eligible_count,
                        valid_card_count,
                        stale_card_count,
                        missing_card_count,
                        coverage_ratio,
                        analysis_schema_version,
                        timestamp,
                        timestamp,
                    ),
                )
                for question_id, card_version_id, content_hash in normalized_members:
                    owner = self.db.execute(
                        "SELECT q.content_hash,q.current_card_version_id,q.active,q.excluded,"
                        "q.last_seen_snapshot,c.semantic_question_id,c.content_hash,c.status,"
                        "c.analysis_schema_version,c.prompt_version,c.model_role,c.model_name,"
                        "c.reasoning_effort FROM semantic_questions q "
                        "JOIN semantic_card_versions c ON c.id=? WHERE q.id=?",
                        (card_version_id, question_id),
                    ).fetchone()
                    if owner is None:
                        raise SemanticIndexRepositoryError(
                            "NOT_FOUND", "snapshot 成员题目或材料卡不存在"
                        )
                    if (
                        owner[0] != content_hash
                        or owner[1] != card_version_id
                        or not owner[2]
                        or owner[3]
                        or owner[4] != source_snapshot
                        or owner[5] != question_id
                        or owner[6] != content_hash
                        or owner[7] != "valid"
                    ):
                        raise SemanticIndexRepositoryError(
                            "SNAPSHOT_MEMBER_MISMATCH",
                            "snapshot 成员不是该题目的当前有效卡",
                        )
                    if (owner[8], owner[9], owner[10], owner[11], owner[12]) != (
                        run[2],
                        run[3],
                        run[4],
                        run[5],
                        run[6],
                    ):
                        raise SemanticIndexRepositoryError(
                            "SNAPSHOT_CARD_VERSION_MISMATCH",
                            "snapshot 材料卡分析身份与冻结 run 不一致",
                        )
                    self.db.execute(
                        "INSERT INTO semantic_index_snapshot_items("
                        "snapshot_id,semantic_question_id,card_version_id,content_hash"
                        ") VALUES(?,?,?,?)",
                        (snapshot_id, question_id, card_version_id, content_hash),
                    )
                has_profile_table = self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='semantic_index_snapshot_profiles'"
                ).fetchone()
                if normalized_members and has_profile_table:
                    self.db.execute(
                        "INSERT INTO semantic_index_snapshot_profiles("
                        "snapshot_id,analysis_schema_version,prompt_version,model_role,"
                        "model_name,reasoning_effort,item_count,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            snapshot_id,
                            run[2],
                            run[3],
                            run[4],
                            run[5],
                            run[6],
                            len(normalized_members),
                            timestamp,
                        ),
                    )
                member_card_ids = {
                    question_id: card_version_id
                    for question_id, card_version_id, _content_hash in normalized_members
                }
                succeeded_items = self.db.execute(
                    "SELECT ri.semantic_question_id,ri.expected_content_hash,"
                    "ri.card_version_id,c.semantic_question_id,c.content_hash,c.status,"
                    "c.analysis_schema_version,c.prompt_version,c.model_role,c.model_name,"
                    "c.reasoning_effort FROM semantic_index_run_items ri "
                    "LEFT JOIN semantic_card_versions c ON c.id=ri.card_version_id "
                    "WHERE ri.run_id=? AND ri.status='succeeded'",
                    (run_id,),
                ).fetchall()
                for item in succeeded_items:
                    if (
                        item[2] is None
                        or member_card_ids.get(item[0]) != item[2]
                        or item[3] != item[0]
                        or item[4] != item[1]
                        or item[5] != "valid"
                        or (item[6], item[7], item[8], item[9], item[10])
                        != (run[2], run[3], run[4], run[5], run[6])
                    ):
                        raise SemanticIndexRepositoryError(
                            "RUN_ITEM_CARD_MISMATCH",
                            "succeeded run item 与冻结 snapshot 材料卡不一致",
                        )
                self.db.execute(
                    "UPDATE semantic_index_runs SET published_snapshot_id=?,updated_at=? "
                    "WHERE id=?",
                    (snapshot_id, timestamp, run_id),
                )
        except sqlite3.IntegrityError as exc:
            raise SemanticIndexRepositoryError(
                "SNAPSHOT_CONFLICT", "snapshot 发布违反数据库约束"
            ) from exc
        return snapshot_id

    def publish_audited_mixed_snapshot(
        self,
        *,
        snapshot_id: str,
        run_id: str,
        source_snapshot: str,
        members: Sequence[SnapshotMember],
        eligible_count: int,
        analysis_schema_version: int,
        timestamp: str,
        caller_owns_transaction: bool = False,
    ) -> str:
        """Publish a complete snapshot while preserving each card's real profile."""
        snapshot_id = _required_text(snapshot_id, "snapshot_id", max_chars=256)
        run_id = _required_text(run_id, "run_id", max_chars=256)
        source_snapshot = _required_text(source_snapshot, "source_snapshot")
        eligible_count = _nonnegative_integer(eligible_count, "eligible_count")
        analysis_schema_version = _positive_integer(
            analysis_schema_version, "analysis_schema_version"
        )
        timestamp = _timestamp(timestamp)
        if not isinstance(caller_owns_transaction, bool):
            raise SemanticIndexRepositoryError(
                "VALIDATION", "caller_owns_transaction 必须是布尔值"
            )
        if caller_owns_transaction and not self.db.in_transaction:
            raise SemanticIndexRepositoryError(
                "TRANSACTION_REQUIRED", "调用方声明持有事务但当前没有活动事务"
            )
        normalized_members: list[tuple[int, int, str]] = []
        seen_question_ids: set[int] = set()
        for member in members:
            question_id = _positive_integer(
                member.semantic_question_id, "semantic_question_id"
            )
            if question_id in seen_question_ids:
                raise SemanticIndexRepositoryError(
                    "DUPLICATE_SNAPSHOT_ITEM", "snapshot 不能包含重复题目"
                )
            seen_question_ids.add(question_id)
            normalized_members.append(
                (
                    question_id,
                    _positive_integer(member.card_version_id, "card_version_id"),
                    _sha256(member.content_hash, "content_hash"),
                )
            )
        if len(normalized_members) != eligible_count:
            raise SemanticIndexRepositoryError(
                "SNAPSHOT_COUNT_MISMATCH", "聚合 snapshot 成员数不等于 eligible_count"
            )

        try:
            transaction = (
                nullcontext()
                if caller_owns_transaction
                else immediate_transaction(self.db)
            )
            with transaction:
                if self.db.execute(
                    "SELECT 1 FROM semantic_index_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone():
                    raise SemanticIndexRepositoryError(
                        "SNAPSHOT_IMMUTABLE", "已发布 snapshot 不允许覆盖"
                    )
                run = self.db.execute(
                    "SELECT source_snapshot,status,analysis_schema_version,model_role "
                    "FROM semantic_index_runs WHERE id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise SemanticIndexRepositoryError("NOT_FOUND", "run 不存在")
                if (
                    run[0] != source_snapshot
                    or run[1] != "succeeded"
                    or run[2] != analysis_schema_version
                    or run[3] != AUDITED_SNAPSHOT_AGGREGATE_ROLE
                ):
                    raise SemanticIndexRepositoryError(
                        "RUN_SNAPSHOT_MISMATCH", "聚合 snapshot 与审计 run 身份不一致"
                    )
                actual_eligible = {
                    int(row[0])
                    for row in self.db.execute(
                        "SELECT id FROM semantic_questions WHERE active=1 AND excluded=0 "
                        "AND last_seen_snapshot=?",
                        (source_snapshot,),
                    ).fetchall()
                }
                if actual_eligible != seen_question_ids:
                    raise SemanticIndexRepositoryError(
                        "SNAPSHOT_ELIGIBLE_SET_MISMATCH",
                        "聚合 snapshot 成员与当前 eligible 集合不一致",
                    )
                profiles: dict[tuple[int, str, str, str, str], int] = {}
                for question_id, card_version_id, content_hash in normalized_members:
                    owner = self.db.execute(
                        "SELECT q.content_hash,q.current_card_version_id,q.active,q.excluded,"
                        "q.last_seen_snapshot,c.semantic_question_id,c.content_hash,c.status,"
                        "c.analysis_schema_version,c.prompt_version,c.model_role,c.model_name,"
                        "c.reasoning_effort FROM semantic_questions q "
                        "JOIN semantic_card_versions c ON c.id=? WHERE q.id=?",
                        (card_version_id, question_id),
                    ).fetchone()
                    if (
                        owner is None
                        or owner[0] != content_hash
                        or owner[1] != card_version_id
                        or not owner[2]
                        or owner[3]
                        or owner[4] != source_snapshot
                        or owner[5] != question_id
                        or owner[6] != content_hash
                        or owner[7] != "valid"
                        or owner[8] != analysis_schema_version
                    ):
                        raise SemanticIndexRepositoryError(
                            "SNAPSHOT_MEMBER_MISMATCH",
                            "聚合 snapshot 成员不是当前有效且兼容的材料卡",
                        )
                    profile = (
                        int(owner[8]),
                        str(owner[9]),
                        str(owner[10]),
                        str(owner[11]),
                        str(owner[12]),
                    )
                    profiles[profile] = profiles.get(profile, 0) + 1
                self.db.execute(
                    "INSERT INTO semantic_index_snapshots("
                    "id,source_snapshot,run_id,status,eligible_count,valid_card_count,"
                    "stale_card_count,missing_card_count,coverage_ratio,"
                    "analysis_schema_version,identity_policy,created_at,published_at) "
                    "VALUES(?,?,?,'complete',?,?,0,0,1.0,?,?,?,?)",
                    (
                        snapshot_id,
                        source_snapshot,
                        run_id,
                        eligible_count,
                        eligible_count,
                        analysis_schema_version,
                        MIXED_AUDITED_IDENTITY_POLICY,
                        timestamp,
                        timestamp,
                    ),
                )
                self.db.executemany(
                    "INSERT INTO semantic_index_snapshot_items("
                    "snapshot_id,semantic_question_id,card_version_id,content_hash) "
                    "VALUES(?,?,?,?)",
                    ((snapshot_id, *member) for member in normalized_members),
                )
                self.db.executemany(
                    "INSERT INTO semantic_index_snapshot_profiles("
                    "snapshot_id,analysis_schema_version,prompt_version,model_role,"
                    "model_name,reasoning_effort,item_count,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        (snapshot_id, *profile, count, timestamp)
                        for profile, count in sorted(profiles.items())
                    ),
                )
                self.db.execute(
                    "UPDATE semantic_index_runs SET published_snapshot_id=?,updated_at=? "
                    "WHERE id=?",
                    (snapshot_id, timestamp, run_id),
                )
        except sqlite3.IntegrityError as exc:
            raise SemanticIndexRepositoryError(
                "SNAPSHOT_CONFLICT", "聚合 snapshot 发布违反数据库约束"
            ) from exc
        return snapshot_id

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        snapshot_id = _required_text(snapshot_id, "snapshot_id", max_chars=256)
        snapshot = _fetch_dict(
            self.db.execute(
                "SELECT * FROM semantic_index_snapshots WHERE id=?", (snapshot_id,)
            )
        )
        if snapshot is None:
            return None
        cursor = self.db.execute(
            "SELECT semantic_question_id,card_version_id,content_hash "
            "FROM semantic_index_snapshot_items WHERE snapshot_id=? "
            "ORDER BY semantic_question_id",
            (snapshot_id,),
        )
        columns = [description[0] for description in cursor.description]
        snapshot["items"] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        has_profile_table = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='semantic_index_snapshot_profiles'"
        ).fetchone()
        snapshot["profiles"] = []
        if has_profile_table:
            profile_cursor = self.db.execute(
                "SELECT analysis_schema_version,prompt_version,model_role,model_name,"
                "reasoning_effort,item_count FROM semantic_index_snapshot_profiles "
                "WHERE snapshot_id=? ORDER BY item_count DESC,model_role,model_name",
                (snapshot_id,),
            )
            profile_columns = [
                description[0] for description in profile_cursor.description
            ]
            snapshot["profiles"] = [
                dict(zip(profile_columns, row)) for row in profile_cursor.fetchall()
            ]
        return snapshot
