"""Prepare one durable semantic-index refresh dispatch.

Preparation itself never calls AI.  The semantic run is the long-lived source
of truth; a later handler may drain it only when an explicit dispatch runtime
has been installed, otherwise it safely remains frozen and queued.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..database import connect_database
from .material_understanding import PROMPT_VERSION
from .question_bank import open_last_good_question_bank_index
from .semantic_index import SEMANTIC_CARD_SCHEMA_VERSION
from .semantic_index_checkpoints import (
    SemanticCheckpointError,
    activate_run,
)
from .semantic_index_scanner import SemanticIndexScanError, freeze_semantic_scan
from .semantic_index_sources import (
    SemanticIndexSourceError,
    SemanticSourceBundle,
    build_semantic_source_bundle,
    open_formal_past_exam_artifact,
)
from .settings import SettingsError, get_settings_service
from .jobs import JobCoordinator, JobConflict


SEMANTIC_REFRESH_OPERATION = "prepare"
SEMANTIC_REFRESH_OPERATIONS = frozenset({"prepare", "resume"})
SEMANTIC_REFRESH_PROFILE_ID = "material-cards-v3"
SEMANTIC_REFRESH_REQUEST_VERSION = 1
_RUN_KINDS = frozenset({"first_full", "incremental", "rebuild"})
_REQUEST_FIELDS = frozenset(
    {
        "operation",
        "run_kind",
        "expected_source_snapshot",
        "analysis_profile_id",
        "analysis_fingerprint",
    }
)


class SemanticIndexRefreshError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def active_semantic_refresh_keys(db) -> set[str]:
    return {
        str(row[0] or "")
        for row in db.execute(
            "SELECT idempotency_key FROM background_jobs "
            "WHERE kind='semantic-index-refresh' "
            "AND status IN ('queued','running','cancel_requested')"
        ).fetchall()
    }


def bind_semantic_resume_epoch(db, request, *, profile, required_key: str) -> str:
    """Bind a resume key to the paused run revision and reject competing jobs."""

    row = db.execute(
        "SELECT status,updated_at FROM semantic_index_runs WHERE id=?",
        (request.run_id,),
    ).fetchone()
    if row is None or str(row["status"]) != "paused":
        raise SemanticIndexRefreshError(
            "RUN_NOT_RESUMABLE", "只有普通 paused run 可以恢复"
        )
    epoch = hashlib.sha256(str(row["updated_at"]).encode("utf-8")).hexdigest()
    bound_key = f"{required_key}:{epoch}"
    active_jobs = db.execute(
        "SELECT j.idempotency_key,i.payload_json FROM background_jobs j "
        "JOIN job_inputs i ON i.job_id=j.id WHERE j.kind='semantic-index-refresh' "
        "AND j.status IN ('queued','running','cancel_requested')"
    ).fetchall()
    for active in active_jobs:
        try:
            active_payload = json.loads(str(active["payload_json"]))
            active_request = normalize_semantic_refresh_request(
                active_payload, profile=profile
            )
        except (json.JSONDecodeError, SemanticIndexRefreshError):
            continue
        if (
            active_request.run_id == request.run_id
            and str(active["idempotency_key"] or "") != bound_key
        ):
            raise SemanticIndexRefreshError(
                "RUN_ALREADY_ACTIVE", "同一 semantic run 已有活动 durable job"
            )
    return bound_key


def maybe_submit_auto_semantic_refresh(
    app: Any,
    *,
    source_kind: str,
    published_source_snapshot: str,
) -> dict[str, Any]:
    """Submit at most one refresh after a formal source publication.

    This coordinator is intentionally side-effect free unless every activation,
    capability, conflict, and snapshot gate passes.  Callers may safely invoke
    it after ordinary or formal last-good publication.
    """

    if source_kind not in {"ordinary", "formal_past"}:
        return {"status": "skipped", "reason": "source_kind_unsupported"}
    settings = get_settings_service(app).semantic_runtime_settings(
        provider_enabled=bool(app.config.get("SEMANTIC_INDEX_PROVIDER_ENABLED"))
    )
    if not settings["requested"].get("auto_refresh_enabled"):
        return {"status": "skipped", "reason": "disabled"}
    if not settings["activation"].get("auto_refresh"):
        return {"status": "skipped", "reason": "activation_required"}
    if not settings["effective"].get("provider_enabled"):
        return {
            "status": "skipped",
            "reason": settings["reasons"].get("provider_enabled")
            or "provider_disabled",
        }
    try:
        bundle = load_app_semantic_source_bundle(app)
        source_snapshot = str(bundle.source_snapshot)
    except SemanticIndexRefreshError as exc:
        return {"status": "deferred", "reason": exc.code.lower()}
    if published_source_snapshot and source_snapshot == str(published_source_snapshot):
        # The caller may supply the combined bundle snapshot.  A source-specific
        # snapshot is still checked against the published semantic snapshot below.
        pass
    try:
        profile = semantic_analysis_profile_from_app(app)
        submission = {
            "operation": "prepare",
            "run_kind": "incremental",
            "expected_source_snapshot": source_snapshot,
            "analysis_profile_id": profile.profile_id,
            "analysis_fingerprint": profile.fingerprint,
        }
        normalized, key = prepare_semantic_refresh_submission(app, submission)
    except SemanticIndexRefreshError as exc:
        return {"status": "deferred", "reason": exc.code.lower(), "source_snapshot": source_snapshot}
    try:
        with closing(
            connect_database(app.config["DATABASE"], role="background")
        ) as db:
            latest = db.execute(
                "SELECT source_snapshot FROM semantic_index_snapshots "
                "WHERE status IN ('complete','partial') ORDER BY published_at DESC,id DESC LIMIT 1"
            ).fetchone()
            if latest is not None and str(latest[0]) == source_snapshot:
                return {"status": "skipped", "reason": "snapshot_already_indexed", "source_snapshot": source_snapshot}
            active_jobs = db.execute(
                "SELECT kind,idempotency_key FROM background_jobs "
                "WHERE status IN ('queued','running','retry_wait','cancel_requested') "
                "AND (kind='semantic-index-refresh' OR kind LIKE 'exam%')"
            ).fetchall()
            if any(
                str(active["kind"]) != "semantic-index-refresh"
                or str(active["idempotency_key"] or "") != key
                for active in active_jobs
            ):
                return {"status": "deferred", "reason": "active_job_conflict", "source_snapshot": source_snapshot}
    except sqlite3.DatabaseError:
        return {"status": "deferred", "reason": "database_unavailable"}
    coordinator = JobCoordinator(app.config["DATABASE"])
    try:
        job, created = coordinator.submit(
            "semantic-index-refresh", normalized, idempotency_key=key
        )
    except JobConflict:
        return {"status": "deferred", "reason": "idempotency_conflict", "source_snapshot": source_snapshot}
    return {
        "status": "submitted" if created else "replayed",
        "reason": None,
        "job_id": job["id"],
        "source_snapshot": source_snapshot,
        "settings_revision": settings["settings_revision"],
    }


@dataclass(frozen=True, slots=True)
class SemanticAnalysisProfile:
    profile_id: str
    analysis_schema_version: int
    prompt_version: str
    model_role: str
    model_name: str
    reasoning_effort: str
    wire_api: str = "chat_completions"

    def identity_payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "analysis_schema_version": self.analysis_schema_version,
            "prompt_version": self.prompt_version,
            "model_role": self.model_role,
            "model_name": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "wire_api": self.wire_api,
        }

    @property
    def fingerprint(self) -> str:
        return _digest(self.identity_payload())


@dataclass(frozen=True, slots=True)
class SemanticRefreshRequest:
    operation: str
    run_kind: str
    expected_source_snapshot: str
    analysis_profile_id: str
    analysis_fingerprint: str

    def payload(self) -> dict[str, str]:
        return {
            "operation": self.operation,
            "run_kind": self.run_kind,
            "expected_source_snapshot": self.expected_source_snapshot,
            "analysis_profile_id": self.analysis_profile_id,
            "analysis_fingerprint": self.analysis_fingerprint,
        }

    @property
    def run_id(self) -> str:
        return "semantic-" + _digest(
            {
                "version": SEMANTIC_REFRESH_REQUEST_VERSION,
                "run_kind": self.run_kind,
                "source_snapshot": self.expected_source_snapshot,
                "analysis_fingerprint": self.analysis_fingerprint,
            }
        )

    @property
    def idempotency_key(self) -> str:
        return "semantic-index-refresh:v1:" + _digest(self.payload())


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SemanticIndexRefreshError("VALIDATION", f"{field} 不能为空")
    rendered = value.strip()
    if len(rendered) > maximum:
        raise SemanticIndexRefreshError("VALIDATION", f"{field} 过长")
    return rendered


def _sha256(value: Any, field: str) -> str:
    rendered = _text(value, field, maximum=64).lower()
    if len(rendered) != 64 or any(
        char not in "0123456789abcdef" for char in rendered
    ):
        raise SemanticIndexRefreshError("VALIDATION", f"{field} 不是 SHA-256 摘要")
    return rendered


def _timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise SemanticIndexRefreshError("VALIDATION", "执行时间必须包含时区")
    return current.astimezone(timezone.utc).isoformat(timespec="microseconds")


def semantic_analysis_profile_from_app(app: Any) -> SemanticAnalysisProfile:
    try:
        settings = get_settings_service(app)
        public_settings = settings.public_ai_settings()
        role = settings.model_role_for_task("exam_generation")
        model = settings.model_for_task("exam_generation")
    except SettingsError as exc:
        raise SemanticIndexRefreshError(
            "ANALYSIS_PROFILE_UNAVAILABLE", "语义材料卡分析配置不可用"
        ) from exc
    return SemanticAnalysisProfile(
        profile_id=SEMANTIC_REFRESH_PROFILE_ID,
        analysis_schema_version=SEMANTIC_CARD_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        model_role=role,
        model_name=model,
        reasoning_effort="max",
        wire_api=str(public_settings.get("wire_api") or "chat_completions"),
    )


def normalize_semantic_refresh_request(
    payload: Mapping[str, Any],
    *,
    profile: SemanticAnalysisProfile,
) -> SemanticRefreshRequest:
    if not isinstance(payload, Mapping):
        raise SemanticIndexRefreshError("VALIDATION", "semantic refresh 输入必须是对象")
    unexpected = set(payload) - _REQUEST_FIELDS
    if unexpected:
        raise SemanticIndexRefreshError(
            "VALIDATION", "semantic refresh 输入包含不允许的字段"
        )
    operation = _text(payload.get("operation"), "operation", maximum=32)
    if operation not in SEMANTIC_REFRESH_OPERATIONS:
        raise SemanticIndexRefreshError("VALIDATION", "当前只允许 prepare 或 resume 操作")
    run_kind = _text(payload.get("run_kind"), "run_kind", maximum=32)
    if run_kind not in _RUN_KINDS:
        raise SemanticIndexRefreshError("VALIDATION", "run_kind 无效")
    snapshot = _sha256(
        payload.get("expected_source_snapshot"), "expected_source_snapshot"
    )
    profile_id = _text(
        payload.get("analysis_profile_id"), "analysis_profile_id", maximum=128
    )
    if profile_id != profile.profile_id:
        raise SemanticIndexRefreshError("ANALYSIS_PROFILE_UNKNOWN", "分析配置版本无效")
    supplied_fingerprint = payload.get("analysis_fingerprint")
    if supplied_fingerprint is not None and _sha256(
        supplied_fingerprint, "analysis_fingerprint"
    ) != profile.fingerprint:
        raise SemanticIndexRefreshError(
            "ANALYSIS_PROFILE_CHANGED", "分析配置在任务执行前发生变化"
        )
    return SemanticRefreshRequest(
        operation=operation,
        run_kind=run_kind,
        expected_source_snapshot=snapshot,
        analysis_profile_id=profile.profile_id,
        analysis_fingerprint=profile.fingerprint,
    )


def prepare_semantic_refresh_submission(
    app: Any, payload: Mapping[str, Any]
) -> tuple[dict[str, str], str]:
    """Return server-normalized input and its mandatory idempotency key."""

    request = normalize_semantic_refresh_request(
        payload, profile=semantic_analysis_profile_from_app(app)
    )
    return request.payload(), request.idempotency_key


def load_app_semantic_source_bundle(app: Any) -> SemanticSourceBundle:
    """Open only already-published last-good artifacts; never rebuild sources."""

    index_path = app.config.get("QUESTION_BANK_DATABASE") or app.config.get(
        "QUESTION_BANK_INDEX"
    )
    ordinary = open_last_good_question_bank_index(index_path)
    if ordinary is None:
        raise SemanticIndexRefreshError(
            "ORDINARY_LAST_GOOD_UNAVAILABLE", "普通题库 last-good 索引不可用"
        )
    artifact_path = str(app.config.get("SEMANTIC_FORMAL_ARTIFACT") or "").strip()
    home = str(app.config.get("SEMANTIC_HOME_INSTITUTION") or "").strip()
    if not artifact_path or not home:
        raise SemanticIndexRefreshError(
            "SEMANTIC_SOURCE_NOT_CONFIGURED",
            "正式真题 last-good 工件或校内院校策略尚未配置",
        )
    try:
        formal = open_formal_past_exam_artifact(Path(artifact_path))
    except SemanticIndexSourceError as exc:
        raise SemanticIndexRefreshError(exc.code, str(exc)) from exc
    if formal.home_institution != home:
        raise SemanticIndexRefreshError(
            "HOME_INSTITUTION_MISMATCH", "正式真题工件与校内院校策略不一致"
        )
    try:
        return build_semantic_source_bundle(ordinary, formal)
    except SemanticIndexSourceError as exc:
        raise SemanticIndexRefreshError(exc.code, str(exc)) from exc


def _run_result(db: Any, run_id: str, *, replayed: bool) -> dict[str, Any] | None:
    row = db.execute(
        "SELECT id,status,source_snapshot,pending_count,new_count,changed_count,"
        "stale_count,metadata_only_count,deleted_count,reused_count,"
        "completed_count,failed_count FROM semantic_index_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "run_id": str(row["id"]),
        "semantic_run_status": str(row["status"]),
        "source_snapshot": str(row["source_snapshot"]),
        "pending_count": int(row["pending_count"]),
        "new_count": int(row["new_count"]),
        "changed_count": int(row["changed_count"]),
        "stale_count": int(row["stale_count"]),
        "metadata_only_count": int(row["metadata_only_count"]),
        "deleted_count": int(row["deleted_count"]),
        "reused_count": int(row["reused_count"]),
        "completed_count": int(row["completed_count"]),
        "failed_count": int(row["failed_count"]),
        "phase": "prepared",
        "replayed": bool(replayed),
    }


def prepare_semantic_index_refresh(
    db: Any,
    *,
    request: SemanticRefreshRequest,
    profile: SemanticAnalysisProfile,
    bundle_loader: Callable[[], SemanticSourceBundle],
    raise_if_cancelled: Callable[[], None],
    now: datetime | None = None,
) -> dict[str, Any]:
    """Freeze or replay one run and stop before any provider item is claimed."""

    if request.analysis_fingerprint != profile.fingerprint:
        raise SemanticIndexRefreshError(
            "ANALYSIS_PROFILE_CHANGED", "分析配置与提交时不一致"
        )
    timestamp = _timestamp(now)
    run_id = request.run_id
    existing_identity = db.execute(
        "SELECT run_kind,source_snapshot,status,pending_count,"
        "analysis_schema_version,prompt_version,model_role,model_name,"
        "reasoning_effort FROM semantic_index_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if existing_identity is not None:
        expected_identity = (
            request.run_kind,
            request.expected_source_snapshot,
            profile.analysis_schema_version,
            profile.prompt_version,
            profile.model_role,
            profile.model_name,
            profile.reasoning_effort,
        )
        actual_identity = (
            str(existing_identity["run_kind"]),
            str(existing_identity["source_snapshot"]),
            int(existing_identity["analysis_schema_version"]),
            str(existing_identity["prompt_version"]),
            str(existing_identity["model_role"]),
            str(existing_identity["model_name"]),
            str(existing_identity["reasoning_effort"]),
        )
        if actual_identity != expected_identity:
            raise SemanticIndexRefreshError(
                "RUN_ID_CONFLICT", "既有 semantic run 身份与 dispatch 不一致"
            )
        if (
            existing_identity["status"] == "queued"
            and int(existing_identity["pending_count"]) == 0
        ):
            try:
                activate_run(db, run_id=run_id, timestamp=timestamp)
            except SemanticCheckpointError as exc:
                raise SemanticIndexRefreshError(exc.code, str(exc)) from exc
        result = _run_result(db, run_id, replayed=True)
        if result is None:
            raise SemanticIndexRefreshError("PERSISTENCE", "既有 semantic run 不可见")
        return result

    raise_if_cancelled()
    bundle = bundle_loader()
    if bundle.source_snapshot.lower() != request.expected_source_snapshot:
        raise SemanticIndexRefreshError(
            "SOURCE_SNAPSHOT_CHANGED", "last-good 来源已不是任务提交时的 snapshot"
        )
    raise_if_cancelled()
    try:
        frozen = freeze_semantic_scan(
            db,
            run_id=run_id,
            run_kind=request.run_kind,
            source_snapshot=request.expected_source_snapshot,
            analysis_schema_version=profile.analysis_schema_version,
            prompt_version=profile.prompt_version,
            model_role=profile.model_role,
            model_name=profile.model_name,
            reasoning_effort=profile.reasoning_effort,
            candidates=bundle.candidates,
            scanned_source_kinds=bundle.scanned_source_kinds,
            timestamp=timestamp,
        )
    except SemanticIndexScanError as exc:
        raise SemanticIndexRefreshError(exc.code, str(exc)) from exc
    raise_if_cancelled()
    if frozen.pending_count == 0:
        try:
            activate_run(db, run_id=frozen.run_id, timestamp=timestamp)
        except SemanticCheckpointError as exc:
            raise SemanticIndexRefreshError(exc.code, str(exc)) from exc
    result = _run_result(db, frozen.run_id, replayed=frozen.replayed)
    if result is None:
        raise SemanticIndexRefreshError(
            "PERSISTENCE", "semantic run 冻结后不可见"
        )
    return result
