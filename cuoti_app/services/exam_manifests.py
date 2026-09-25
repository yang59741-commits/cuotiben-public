"""Persistence helpers for immutable exam-generation inputs.

The manifest fixes the question-bank snapshot, chapter plan and candidate IDs
for a background job so retries and idempotent replays use the same inputs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..database import connect_database

MANIFEST_CONTRACT_VERSION = "semantic-recall-freeze-v1"
# Unversioned manifests are normally legacy records and must never be silently
# replaced by a final freeze.  These are the only two explicit intermediate
# stages that are allowed to promote to the versioned contract.
STAGED_MANIFEST_STAGES = frozenset({
    "candidate-decision-pending-v1",
    "past-profile-frozen-v1",
})
MAX_MANIFEST_BYTES = 512 * 1024
MAX_CANDIDATES = 4_096
MAX_PAPERS = 3
_EXECUTION_RECEIPT_FIELDS = frozenset({
    "exam_ids",
    "published_at",
    "result_hash",
    "status",
})
_PUBLIC_AI_FREEZE_FIELDS = frozenset({
    "task", "policy", "model", "url", "provider", "version", "role",
    "dns_mode", "dns_servers", "reasoning_effort", "profile_id",
    "credential_ref", "capabilities", "wire_api",
})
_LEGACY_PUBLIC_AI_FREEZE_FIELDS = _PUBLIC_AI_FREEZE_FIELDS - {"wire_api"}


class ExamManifestError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _connect(database: str | Path) -> sqlite3.Connection:
    return connect_database(database, role="background")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    rendered = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _required_text(value: Any, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str):
        raise ExamManifestError("MANIFEST_INVALID", f"{field} 必须是字符串")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ExamManifestError("MANIFEST_INVALID", f"{field} 为空或过长")
    return value


def _bounded_string_list(value: Any, field: str, maximum: int = MAX_CANDIDATES) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ExamManifestError("MANIFEST_INVALID", f"{field} 必须是有界列表")
    result = [_required_text(item, field) for item in value]
    if len(result) != len(set(result)):
        raise ExamManifestError("MANIFEST_INVALID", f"{field} 不得重复")
    return result


def _validate_public_ai_task_freezes(value: Any) -> None:
    if not isinstance(value, Mapping) or len(value) > 32:
        raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结快照无效")
    for binding_key, raw in value.items():
        if not isinstance(binding_key, str) or not isinstance(raw, Mapping):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结条目无效")
        if set(raw) not in {
            _PUBLIC_AI_FREEZE_FIELDS,
            _LEGACY_PUBLIC_AI_FREEZE_FIELDS,
        }:
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结字段无效")
        task = _required_text(raw.get("task"), "task", 64)
        policy = _required_text(raw.get("policy"), "policy", 64)
        if binding_key != f"{task}:{policy}":
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结身份不一致")
        for field in ("model", "url", "provider", "profile_id", "credential_ref"):
            _required_text(raw.get(field), field, 2048 if field == "url" else 256)
        if raw.get("role") not in {"fast", "quality"}:
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结角色无效")
        if isinstance(raw.get("version"), bool) or not isinstance(raw.get("version"), int):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结版本无效")
        if not isinstance(raw.get("dns_mode"), str):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结 DNS 模式无效")
        if not isinstance(raw.get("dns_servers"), list) or not all(
            isinstance(item, str) for item in raw["dns_servers"]
        ):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结 DNS 列表无效")
        if not isinstance(raw.get("reasoning_effort"), str):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结推理强度无效")
        if "wire_api" in raw and raw.get("wire_api") not in {
            "chat_completions",
            "responses",
            "messages",
        }:
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结通信协议无效")
        if not isinstance(raw.get("capabilities"), Mapping):
            raise ExamManifestError("MANIFEST_INVALID", "AI 任务冻结能力无效")


def validate_freeze_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ExamManifestError("MANIFEST_INVALID", "freeze_payload 必须是对象")
    value = dict(payload)
    required = {
        "source",
        "question_bank_snapshot",
        "semantic_snapshot_id",
        "semantic_snapshot_identity_policy",
        "projection_id",
        "projection_build_hash",
        "recall_algorithm_version",
        "dictionary_version",
        "weights_version",
        "recall_request_hash",
        "hard_pool_hash",
        "recall_result_hash",
        "candidate_identities",
        "candidate_ids",
        "candidate_decisions",
        "coverage_counts",
        "coverage_reason_counts",
        "partial_cache",
        "relaxed_quotas",
        "fallback_reason",
        "deep_read_required_ids",
        "permanent_card_reused_ids",
        "prompt_candidate_count",
        "prompt_token_estimate",
        "prompt_byte_estimate",
        "paper_seeds",
        "paper_candidate_orders",
        "created_at",
    }
    if not required <= set(value):
        raise ExamManifestError(
            "MANIFEST_INVALID",
            "freeze_payload 缺少字段：" + ",".join(sorted(required - set(value))),
        )
    _required_text(value["source"], "source", 32)
    _required_text(value["question_bank_snapshot"], "question_bank_snapshot")
    _required_text(value["semantic_snapshot_identity_policy"], "semantic_snapshot_identity_policy", 128)
    for field in (
        "projection_build_hash",
        "recall_request_hash",
        "hard_pool_hash",
        "recall_result_hash",
    ):
        text = _required_text(value[field], field, 64)
        if len(text) != 64 or any(char not in "0123456789abcdef" for char in text.casefold()):
            raise ExamManifestError("MANIFEST_INVALID", f"{field} 必须是 SHA-256")
    for field in (
        "semantic_snapshot_id",
        "projection_id",
        "recall_algorithm_version",
        "dictionary_version",
        "weights_version",
        "created_at",
    ):
        _required_text(value[field], field)
    candidate_ids = _bounded_string_list(value["candidate_ids"], "candidate_ids")
    _bounded_string_list(value["deep_read_required_ids"], "deep_read_required_ids")
    _bounded_string_list(value["permanent_card_reused_ids"], "permanent_card_reused_ids")
    identities = value["candidate_identities"]
    decisions = value["candidate_decisions"]
    if not isinstance(identities, list) or len(identities) != len(candidate_ids):
        raise ExamManifestError("MANIFEST_INVALID", "candidate_identities 数量无效")
    if not isinstance(decisions, list) or len(decisions) != len(candidate_ids):
        raise ExamManifestError("MANIFEST_INVALID", "candidate_decisions 数量无效")
    for index, (identity, decision, candidate_id) in enumerate(
        zip(identities, decisions, candidate_ids)
    ):
        if not isinstance(identity, list) or len(identity) != 2:
            raise ExamManifestError("MANIFEST_INVALID", "candidate_identity 无效")
        source_kind = _required_text(identity[0], "source_kind", 32)
        identity_candidate = _required_text(identity[1], "candidate_id")
        if identity_candidate != candidate_id or not isinstance(decision, Mapping):
            raise ExamManifestError("MANIFEST_INVALID", f"候选决策 {index} 与身份不一致")
        if decision.get("source_kind") != source_kind or decision.get("candidate_id") != candidate_id:
            raise ExamManifestError("MANIFEST_INVALID", f"候选决策 {index} 与身份不一致")
        content_hash = decision.get("content_hash")
        if content_hash is not None:
            content_hash = _required_text(content_hash, "content_hash", 64)
            if len(content_hash) != 64:
                raise ExamManifestError("MANIFEST_INVALID", "content_hash 无效")
        card_content_hash = decision.get("card_content_hash")
        if card_content_hash is not None:
            card_content_hash = _required_text(
                card_content_hash, "card_content_hash", 64
            )
            if len(card_content_hash) != 64:
                raise ExamManifestError(
                    "MANIFEST_INVALID", "card_content_hash 无效"
                )
        coverage_reason = decision.get("coverage_reason")
        if coverage_reason is not None:
            _required_text(coverage_reason, "coverage_reason", 64)
        card_version_id = decision.get("card_version_id")
        if card_version_id is not None and (
            isinstance(card_version_id, bool)
            or not isinstance(card_version_id, int)
            or card_version_id < 1
        ):
            raise ExamManifestError("MANIFEST_INVALID", "card_version_id 无效")
    for field in ("coverage_counts", "coverage_reason_counts"):
        if not isinstance(value[field], Mapping):
            raise ExamManifestError("MANIFEST_INVALID", f"{field} 必须是对象")
    if not isinstance(value["partial_cache"], bool):
        raise ExamManifestError("MANIFEST_INVALID", "partial_cache 必须是布尔值")
    if not isinstance(value["relaxed_quotas"], list) or len(value["relaxed_quotas"]) > 64:
        raise ExamManifestError("MANIFEST_INVALID", "relaxed_quotas 无效")
    paper_seeds = _bounded_string_list(value["paper_seeds"], "paper_seeds", MAX_PAPERS)
    if not paper_seeds:
        raise ExamManifestError("MANIFEST_INVALID", "paper_seeds 不能为空")
    paper_orders = value["paper_candidate_orders"]
    if not isinstance(paper_orders, list) or len(paper_orders) != len(paper_seeds):
        raise ExamManifestError("MANIFEST_INVALID", "paper_candidate_orders 数量无效")
    for order in paper_orders:
        order_ids = _bounded_string_list(order, "paper_candidate_orders")
        if len(order_ids) != len(candidate_ids) or set(order_ids) != set(candidate_ids):
            raise ExamManifestError("MANIFEST_INVALID", "每份 paper 候选顺序必须覆盖冻结候选")
    for field in ("prompt_candidate_count", "prompt_token_estimate", "prompt_byte_estimate"):
        number = value[field]
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise ExamManifestError("MANIFEST_INVALID", f"{field} 无效")
    if "ai_task_freezes" in value:
        _validate_public_ai_task_freezes(value["ai_task_freezes"])
    encoded = _canonical_json(value).encode("utf-8")
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ExamManifestError("MANIFEST_TOO_LARGE", "冻结清单超过长度上限")
    return value


def _decode_manifest(raw: str) -> dict[str, Any]:
    if len(raw.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ExamManifestError("MANIFEST_TOO_LARGE", "组卷清单超过长度上限")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ExamManifestError("MANIFEST_CORRUPT", "组卷清单 JSON 损坏") from exc
    if not isinstance(manifest, dict):
        raise ExamManifestError("MANIFEST_CORRUPT", "组卷清单必须是对象")
    version = manifest.get("manifest_contract_version")
    if version is None:
        return manifest
    if version != MANIFEST_CONTRACT_VERSION:
        raise ExamManifestError("MANIFEST_VERSION_UNSUPPORTED", "组卷清单关键版本不受支持")
    freeze_payload = validate_freeze_payload(manifest.get("freeze_payload"))
    if manifest.get("freeze_hash") != _sha256(freeze_payload):
        raise ExamManifestError("MANIFEST_CORRUPT", "冻结清单 hash 不匹配")
    receipt = manifest.get("execution_receipt")
    if not isinstance(receipt, dict) or not set(receipt) <= _EXECUTION_RECEIPT_FIELDS:
        raise ExamManifestError("MANIFEST_CORRUPT", "执行收据字段无效")
    return manifest


def load_exam_generation_manifest(
    database: str | Path, job_id: str
) -> dict[str, Any] | None:
    """Load a job's frozen generation manifest, or ``None`` when absent."""

    with closing(_connect(database)) as connection:
        row = connection.execute(
            "SELECT job_id,snapshot_id,manifest_json,created_at,updated_at "
            "FROM exam_generation_manifests WHERE job_id=?",
            (job_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "job_id": row["job_id"],
        "snapshot_id": row["snapshot_id"],
        "manifest": _decode_manifest(row["manifest_json"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def exam_manifest_payload(loaded: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(loaded, Mapping):
        return {}
    manifest = loaded.get("manifest")
    if not isinstance(manifest, Mapping):
        return {}
    if manifest.get("manifest_contract_version") == MANIFEST_CONTRACT_VERSION:
        payload = manifest.get("freeze_payload")
        return dict(payload) if isinstance(payload, Mapping) else {}
    return dict(manifest)


def is_versioned_exam_manifest(loaded: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(loaded, Mapping)
        and isinstance(loaded.get("manifest"), Mapping)
        and loaded["manifest"].get("manifest_contract_version")
        == MANIFEST_CONTRACT_VERSION
    )


def manifest_stage(loaded: Mapping[str, Any] | None) -> str | None:
    """Return a recognized intermediate stage, if one was persisted."""

    payload = exam_manifest_payload(loaded)
    stage = payload.get("manifest_stage") if isinstance(payload, Mapping) else None
    return stage if stage in STAGED_MANIFEST_STAGES else None


def upsert_exam_generation_manifest(
    database: str | Path,
    job_id: str,
    snapshot_id: str,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Insert or replace a job manifest while preserving its creation time."""

    if not str(job_id).strip():
        raise ValueError("job_id must not be empty")
    if not str(snapshot_id).strip():
        raise ValueError("snapshot_id must not be empty")
    if not isinstance(manifest, Mapping):
        raise TypeError("manifest must be a mapping")

    encoded = json.dumps(
        dict(manifest),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    timestamp = _utc_timestamp()
    with closing(_connect(database)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO exam_generation_manifests("
                "job_id,snapshot_id,manifest_json,created_at,updated_at"
                ") VALUES(?,?,?,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET "
                "snapshot_id=excluded.snapshot_id,"
                "manifest_json=excluded.manifest_json,"
                "updated_at=excluded.updated_at",
                (job_id, snapshot_id, encoded, timestamp, timestamp),
            )
    loaded = load_exam_generation_manifest(database, job_id)
    if loaded is None:  # Defensive: the committed row must be visible.
        raise RuntimeError("exam generation manifest was not persisted")
    return loaded


def merge_exam_generation_manifest(
    database: str | Path,
    job_id: str,
    snapshot_id: str,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge checkpoint metadata without rewriting a newer freeze.

    Checkpoint writers run in more than one stage of a background task.  A
    plain read followed by ``upsert_exam_generation_manifest`` can race with
    the final freeze and replace its immutable payload.  This helper takes an
    immediate SQLite transaction, merges only the requested fields, and
    treats a versioned manifest as authoritative.
    """

    if not str(job_id).strip():
        raise ValueError("job_id must not be empty")
    if not str(snapshot_id).strip():
        raise ValueError("snapshot_id must not be empty")
    if not isinstance(updates, Mapping):
        raise TypeError("updates must be a mapping")

    timestamp = _utc_timestamp()
    with closing(_connect(database)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT snapshot_id,manifest_json,created_at FROM "
                "exam_generation_manifests WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                encoded = _canonical_json(dict(updates))
                connection.execute(
                    "INSERT INTO exam_generation_manifests("
                    "job_id,snapshot_id,manifest_json,created_at,updated_at"
                    ") VALUES(?,?,?,?,?)",
                    (job_id, snapshot_id, encoded, timestamp, timestamp),
                )
            else:
                if str(row["snapshot_id"]) != snapshot_id:
                    raise ExamManifestError(
                        "MANIFEST_CONFLICT", "组卷清单对应的题库快照已变化"
                    )
                current = _decode_manifest(str(row["manifest_json"]))
                if current.get("manifest_contract_version") == MANIFEST_CONTRACT_VERSION:
                    # A final freeze is immutable; a late checkpoint writer is
                    # allowed to observe it but never to replace it.
                    connection.commit()
                    return load_exam_generation_manifest(database, job_id)  # type: ignore[return-value]
                merged = dict(current)
                merged.update(dict(updates))
                encoded = _canonical_json(merged)
                if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
                    raise ExamManifestError(
                        "MANIFEST_TOO_LARGE", "组卷清单超过长度上限"
                    )
                connection.execute(
                    "UPDATE exam_generation_manifests SET manifest_json=?,updated_at=? "
                    "WHERE job_id=?",
                    (encoded, timestamp, job_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    loaded = load_exam_generation_manifest(database, job_id)
    if loaded is None:
        raise ExamManifestError("MANIFEST_WRITE_FAILED", "组卷清单未持久化")
    return loaded


def freeze_exam_generation_manifest(
    database: str | Path,
    job_id: str,
    snapshot_id: str,
    freeze_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Insert a versioned freeze once; retries may only present identical input."""

    job_id = _required_text(job_id, "job_id")
    snapshot_id = _required_text(snapshot_id, "snapshot_id")
    payload = validate_freeze_payload(freeze_payload)
    envelope = {
        "manifest_contract_version": MANIFEST_CONTRACT_VERSION,
        "freeze_payload": payload,
        "freeze_hash": _sha256(payload),
        "execution_receipt": {},
    }
    encoded = _canonical_json(envelope)
    if len(encoded.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ExamManifestError("MANIFEST_TOO_LARGE", "组卷清单超过长度上限")
    timestamp = _utc_timestamp()
    # The stage-to-final promotion must inspect and update the row while one
    # SQLite writer lock is held.  A read followed by a later UPDATE can race
    # with another retry that already published a final freeze and overwrite
    # that immutable decision.
    with closing(_connect(database)) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT snapshot_id,manifest_json FROM "
                "exam_generation_manifests WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO exam_generation_manifests("
                    "job_id,snapshot_id,manifest_json,created_at,updated_at"
                    ") VALUES(?,?,?,?,?)",
                    (job_id, snapshot_id, encoded, timestamp, timestamp),
                )
            else:
                existing_snapshot = str(row["snapshot_id"])
                manifest = _decode_manifest(str(row["manifest_json"]))
                # A generation retry may have persisted a characterization or
                # candidate-decision checkpoint before the final semantic
                # freeze. Promote only explicitly marked checkpoints; arbitrary
                # legacy manifests remain immutable and report a conflict.
                existing = {
                    "snapshot_id": existing_snapshot,
                    "manifest": manifest,
                }
                staged = manifest_stage(existing)
                if staged is not None and existing_snapshot == snapshot_id:
                    connection.execute(
                        "UPDATE exam_generation_manifests "
                        "SET manifest_json=?,updated_at=? WHERE job_id=?",
                        (encoded, timestamp, job_id),
                    )
                elif (
                    manifest.get("manifest_contract_version")
                    != MANIFEST_CONTRACT_VERSION
                    or existing_snapshot != snapshot_id
                    or manifest.get("freeze_hash") != envelope["freeze_hash"]
                ):
                    raise ExamManifestError(
                        "MANIFEST_CONFLICT", "同一任务已有不同的冻结组卷决定"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    loaded = load_exam_generation_manifest(database, job_id)
    if loaded is None:
        raise ExamManifestError("MANIFEST_WRITE_FAILED", "冻结组卷清单未持久化")
    return loaded


def append_exam_generation_receipt(
    database: str | Path,
    job_id: str,
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or not set(receipt) <= _EXECUTION_RECEIPT_FIELDS:
        raise ExamManifestError("RECEIPT_INVALID", "执行收据字段无效")
    with closing(_connect(database)) as connection:
        with connection:
            row = connection.execute(
                "SELECT manifest_json FROM exam_generation_manifests WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise ExamManifestError("MANIFEST_NOT_FOUND", "冻结组卷清单不存在")
            manifest = _decode_manifest(str(row[0]))
            if manifest.get("manifest_contract_version") != MANIFEST_CONTRACT_VERSION:
                raise ExamManifestError("MANIFEST_LEGACY", "旧版组卷清单不支持执行收据")
            current = dict(manifest["execution_receipt"])
            for key, value in receipt.items():
                if key in current and current[key] != value:
                    raise ExamManifestError("RECEIPT_CONFLICT", "执行收据不可改写")
                current[key] = value
            manifest["execution_receipt"] = current
            connection.execute(
                "UPDATE exam_generation_manifests SET manifest_json=?,updated_at=? WHERE job_id=?",
                (_canonical_json(manifest), _utc_timestamp(), job_id),
            )
    loaded = load_exam_generation_manifest(database, job_id)
    if loaded is None:
        raise ExamManifestError("MANIFEST_NOT_FOUND", "冻结组卷清单不存在")
    return loaded
