"""Deterministic, read-only indexing for the external TeX question bank."""

from __future__ import annotations

import os
import random
import sqlite3
import threading
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .question_bank_contracts import (
    DEFAULT_INDEX_PATH,
    PARSER_VERSION,
    QUESTION_BANK_BUILD_FAILED,
    QUESTION_BANK_BUILDING,
    SCHEMA_VERSION,
    QuestionBankError,
)
from .question_bank_parser import (
    Candidate as _Candidate,
    candidate_exclusion_reason as _candidate_exclusion_reason,
    catalog_exclusions as _catalog_exclusions,
    clean_content as _clean_content,
    content_digest as _content_digest,
    estimate_tokens as _estimate_tokens,
    excluded_ranges as _excluded_ranges,
    find_range as _find_range,
    normalized_content as _normalized_content,
    number_exclusion as _number_exclusion,
    parse_candidates as _parse_candidates,
    stable_question_id as _stable_question_id,
    whole_file_exclusion as _whole_file_exclusion,
)
from .question_bank_sources import (
    catalog_semantic_digest as _catalog_semantic_digest,
    extract_problems_from_tex,
    manifest_snapshot_id as _manifest_snapshot_id,
    source_manifest as _source_manifest,
    whitelist_entries as _whitelist_entries,
)
from .question_bank_repository import (
    connect_readonly as _connect_readonly,
    create_schema as _create_schema,
    is_sha256 as _is_sha256,
    published_index_integrity as _published_index_integrity,
    record_exclusion as _record_exclusion,
    semantic_catalog_digest as _semantic_catalog_digest,
)
from .question_bank_backup import copy_usage as _copy_usage


_BUILD_LOCK = threading.Lock()
_ACTIVE_JOB_STATES = frozenset({"queued", "running", "cancel_requested"})


def _positive_catalog_id(raw: Any, label: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}必须是正整数") from exc
    if value < 1:
        raise ValueError(f"{label}必须是正整数")
    return value


def resolve_coverage_scope(
    db,
    *,
    subject: str | None,
    chapters: Iterable[str],
    raw_subject_id: Any,
    raw_chapter_ids: Iterable[str],
) -> tuple[str | None, list[str], int | None]:
    """Resolve stable catalog identifiers for the coverage read model."""

    resolved_subject_id = None
    resolved_chapters = list(chapters)
    if raw_subject_id not in (None, ""):
        resolved_subject_id = _positive_catalog_id(raw_subject_id, "科目 ID")
        subject_row = db.execute(
            "SELECT id,name FROM subjects WHERE id=?",
            (resolved_subject_id,),
        ).fetchone()
        if not subject_row:
            raise ValueError("科目不存在")
        if subject and subject != subject_row["name"]:
            raise ValueError("科目 ID 与科目名称不一致")
        subject = subject_row["name"]

    chapter_id_values = []
    for raw_value in raw_chapter_ids:
        chapter_id_values.extend(
            _positive_catalog_id(item.strip(), "章节 ID")
            for item in raw_value.split(",")
            if item.strip()
        )
    chapter_id_values = list(dict.fromkeys(chapter_id_values))
    if len(chapter_id_values) > 50:
        raise ValueError("一次最多查询 50 个章节")
    if chapter_id_values:
        if resolved_subject_id is None:
            raise ValueError("使用 chapter_ids 时必须同时提供 subject_id")
        rows = db.execute(
            "SELECT id,name,subject_id FROM chapters WHERE id IN ({})".format(
                ",".join("?" for _ in chapter_id_values)
            ),
            chapter_id_values,
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        if len(by_id) != len(chapter_id_values):
            raise ValueError("部分章节不存在")
        if any(
            row["subject_id"] != resolved_subject_id for row in by_id.values()
        ):
            raise ValueError("部分章节不属于所选科目")
        resolved_chapters = [
            by_id[chapter_id]["name"] for chapter_id in chapter_id_values
        ]
    return subject, resolved_chapters, resolved_subject_id


def enrich_coverage_catalog(db, result: dict, subject_id: int | None) -> dict:
    """Attach relational catalog IDs to a question-bank coverage result."""

    chapter_catalog = {
        (row["subject"], row["name"]): {
            "chapter_id": row["id"],
            "subject_id": row["subject_id"],
        }
        for row in db.execute(
            "SELECT c.id,c.name,c.subject_id,s.name AS subject "
            "FROM chapters c JOIN subjects s ON s.id=c.subject_id"
        ).fetchall()
    }
    for item in result["chapters"]:
        identifiers = chapter_catalog.get((item["subject"], item["chapter"]), {})
        item.update(identifiers)
    if subject_id is not None:
        result["subject_id"] = subject_id
    return result


def _job_requested_snapshot(coordinator, job: Mapping[str, Any]) -> str:
    """Read a rebuild job's requested source snapshot defensively."""

    try:
        payload = coordinator.get_input(str(job.get("id") or ""))
    except Exception:
        return ""
    if not isinstance(payload, Mapping):
        return ""
    return str(payload.get("requested_source_snapshot") or "").strip()


def find_question_bank_job(
    coordinator,
    source_snapshot: str,
    *,
    active_only: bool = False,
) -> dict[str, Any] | None:
    """Find the newest rebuild job for one immutable source snapshot.

    Job payloads are intentionally inspected through ``get_input`` rather than
    trusting an idempotency-key naming convention.  This also lets a manually
    forced rebuild be reused by automatic callers while it is still active.
    """

    snapshot = str(source_snapshot or "").strip()
    if not snapshot:
        return None
    try:
        jobs = coordinator.list_jobs(
            kind="question-bank-rebuild", limit=200, active=active_only
        )
    except Exception:
        return None
    for job in jobs:
        if active_only and str(job.get("status") or "") not in _ACTIVE_JOB_STATES:
            continue
        if _job_requested_snapshot(coordinator, job) == snapshot:
            return dict(job)
    return None


def submit_question_bank_rebuild_job(
    coordinator,
    source_snapshot: str,
    *,
    force: bool = False,
    idempotency_key: str | None = None,
    max_attempts: int = 2,
) -> tuple[dict[str, Any], bool]:
    """Submit or reuse one rebuild task for a source snapshot.

    Automatic requests share a deterministic key and first reuse any active
    task for the same snapshot, including one submitted manually.  A terminal
    failed/cancelled task is advanced with a key derived from that task ID so
    it cannot permanently poison future automatic rebuilds while concurrent
    callers still converge on the same retry key.
    """

    snapshot = str(source_snapshot or "").strip()
    if not snapshot:
        raise ValueError("题库源快照不能为空")
    payload = {"requested_source_snapshot": snapshot}
    # A forced rebuild means "rebuild even when the current index is fresh";
    # it must not mean "run a second writer beside an active rebuild".  Reuse
    # the active task for every caller so repeated clicks or two open tabs
    # cannot race on the same SQLite index.
    active = find_question_bank_job(coordinator, snapshot, active_only=True)
    if active is not None:
        return active, False
    base_key = str(idempotency_key or "").strip()
    if not base_key:
        base_key = (
            f"question-bank:{snapshot}:manual:{uuid.uuid4().hex}"
            if force
            else f"question-bank:{snapshot}"
        )
    key = base_key
    for _attempt in range(4):
        job, created = coordinator.submit(
            "question-bank-rebuild",
            payload,
            idempotency_key=key,
            max_attempts=max_attempts,
        )
        status = str(job.get("status") or "")
        if created or status in _ACTIVE_JOB_STATES or status == "succeeded":
            return job, created
        # A deterministic key may point at an old terminal failure.  Derive
        # the next key from its stable ID; all callers observing that failure
        # therefore converge on one replacement task.
        if status in {"failed", "cancelled"} and job.get("id"):
            if force:
                return job, created
            # Allow one automatic replacement for an old deterministic task,
            # then surface the replacement's terminal error instead of
            # creating an unbounded chain on every exam retry.  A user can
            # still explicitly force another rebuild through the settings UI.
            try:
                prior_key = str(
                    coordinator.get_idempotency_key(str(job["id"])) or ""
                )
            except Exception:
                prior_key = str(job.get("idempotency_key") or "")
            if prior_key.startswith(base_key + ":retry:"):
                return job, created
            key = f"{base_key}:retry:{job['id']}"
            continue
        return job, created
    return job, created


def question_bank_unavailable_error(
    job: Mapping[str, Any] | None,
    source_snapshot: str,
) -> QuestionBankError:
    """Create a structured error for callers that have no last-good index."""

    job_payload = dict(job) if isinstance(job, Mapping) else None
    job_id = str(job_payload.get("id") or "") if job_payload else ""
    details: dict[str, Any] = {
        "source_snapshot": str(source_snapshot),
        "actions": ["rebuild_question_bank"],
    }
    if job_id:
        details["job_id"] = job_id
        progress = job_payload.get("progress")
        if isinstance(progress, Mapping):
            details["stage"] = str(
                progress.get("message") or "正在构建题库索引"
            )
            details["progress"] = {
                "current": progress.get("current"),
                "total": progress.get("total"),
                "message": progress.get("message"),
            }
        if str(job_payload.get("status") or "") in _ACTIVE_JOB_STATES:
            details["retry_after"] = 2
    if job_payload and str(job_payload.get("status") or "") == "failed":
        error = job_payload.get("error")
        if isinstance(error, Mapping):
            details["dependency_error"] = {
                "code": str(error.get("code") or ""),
                "message": str(error.get("message") or ""),
            }
        return QuestionBankError(
            "题库索引构建失败，请重建题库后再试",
            code=QUESTION_BANK_BUILD_FAILED,
            status_code=409,
            retryable=False,
            details=details,
            job=job_payload,
        )
    return QuestionBankError(
        "题库索引正在后台构建，请稍后重试",
        code=QUESTION_BANK_BUILDING,
        # The request has been accepted and a durable build job is already
        # running.  Callers can use the job metadata to resume automatically;
        # 409 is reserved for a failed build or a real input conflict.
        status_code=202,
        retryable=True,
        details=details,
        job=job_payload,
    )


def _build_index(
    tex_base: Path,
    destination: Path,
    manifest: list[dict],
    snapshot_id: str,
    catalog=None,
) -> None:
    connection = sqlite3.connect(destination)
    try:
        _create_schema(connection)
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "parser_version": PARSER_VERSION,
            "snapshot_id": snapshot_id,
            "catalog_semantic_digest": _catalog_semantic_digest(catalog),
            "built_at": datetime.now(timezone.utc).astimezone().isoformat(
                timespec="seconds"
            ),
            "tex_base": str(tex_base.resolve()),
        }
        connection.executemany(
            "INSERT INTO metadata(key,value) VALUES(?,?)", metadata.items()
        )

        for source in manifest:
            subject = source["subject"]
            chapter = source["chapter"]
            filename = source["source_file"]
            path = tex_base / filename
            if source["status"] == "missing":
                connection.execute(
                    "INSERT INTO sources(source_file,subject,chapter,sha256,size,"
                    "status,error) VALUES(?,?,?,?,?,'missing',?)",
                    (
                        filename,
                        subject,
                        chapter,
                        source["sha256"],
                        source["size"],
                        "白名单文件不存在",
                    ),
                )
                continue

            reason = _whole_file_exclusion(filename, catalog)
            if reason:
                connection.execute(
                    "INSERT INTO sources(source_file,subject,chapter,sha256,size,"
                    "status,excluded_count) VALUES(?,?,?,?,?,'excluded',1)",
                    (
                        filename,
                        subject,
                        chapter,
                        source["sha256"],
                        source["size"],
                    ),
                )
                _record_exclusion(
                    connection,
                    subject=subject,
                    chapter=chapter,
                    source_file=filename,
                    reason=reason,
                )
                continue

            try:
                text = path.read_text(encoding="utf-8-sig")
                candidates = _parse_candidates(text)
                ranges = _excluded_ranges(filename, text, catalog)
                indexed_count = 0
                excluded_count = 0
                matched_range_reasons = set()
                parser_kind = candidates[0].marker if candidates else "none"
                for candidate in candidates:
                    reason = _candidate_exclusion_reason(
                        filename, candidate, ranges, catalog
                    )
                    content_hash = (
                        _content_digest(candidate.content)
                        if candidate.content
                        else None
                    )
                    if reason:
                        _record_exclusion(
                            connection,
                            subject=subject,
                            chapter=chapter,
                            source_file=filename,
                            reason=reason,
                            candidate=candidate,
                            content_hash=content_hash,
                        )
                        excluded_count += 1
                        if any(
                            start <= candidate.start < end
                            and range_reason == reason
                            for start, end, range_reason in ranges
                        ):
                            matched_range_reasons.add(reason)
                        continue

                    question_id = _stable_question_id(content_hash)
                    existing = connection.execute(
                        "SELECT question_id FROM questions WHERE content_hash=?",
                        (content_hash,),
                    ).fetchone()
                    if existing:
                        question_id = existing[0]
                        _record_exclusion(
                            connection,
                            subject=subject,
                            chapter=chapter,
                            source_file=filename,
                            reason="duplicate_content",
                            candidate=candidate,
                            content_hash=content_hash,
                        )
                        excluded_count += 1
                    else:
                        connection.execute(
                            "INSERT INTO questions(question_id,content_hash,content,"
                            "char_count,token_estimate) VALUES(?,?,?,?,?)",
                            (
                                question_id,
                                content_hash,
                                candidate.content,
                                len(candidate.content),
                                _estimate_tokens(candidate.content),
                            ),
                        )
                    connection.execute(
                        "INSERT INTO question_sources(question_id,subject,chapter,"
                        "source_file,source_number,source_ordinal,marker) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            question_id,
                            subject,
                            chapter,
                            filename,
                            candidate.source_number,
                            candidate.source_ordinal,
                            candidate.marker,
                        ),
                    )
                    indexed_count += 1

                # A suffix may intentionally contain prose/examples rather than
                # parseable exercises. Record the applied boundary even when it
                # therefore excluded no candidate rows.
                for _start, _end, range_reason in ranges:
                    if range_reason in matched_range_reasons:
                        continue
                    _record_exclusion(
                        connection,
                        subject=subject,
                        chapter=chapter,
                        source_file=filename,
                        reason=range_reason,
                    )
                    excluded_count += 1

                status = "indexed" if candidates else "empty"
                connection.execute(
                    "INSERT INTO sources(source_file,subject,chapter,sha256,size,"
                    "status,parser_kind,parsed_count,indexed_count,excluded_count) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        filename,
                        subject,
                        chapter,
                        source["sha256"],
                        source["size"],
                        status,
                        parser_kind,
                        len(candidates),
                        indexed_count,
                        excluded_count,
                    ),
                )
            except UnicodeDecodeError:
                connection.execute(
                    "INSERT INTO sources(source_file,subject,chapter,sha256,size,"
                    "status,error) VALUES(?,?,?,?,?,'encoding_error',?)",
                    (
                        filename,
                        subject,
                        chapter,
                        source["sha256"],
                        source["size"],
                        "源文件不是有效 UTF-8/UTF-8-SIG，请显式转换编码",
                    ),
                )
            except Exception as exc:
                connection.execute(
                    "INSERT INTO sources(source_file,subject,chapter,sha256,size,"
                    "status,error) VALUES(?,?,?,?,?,'error',?)",
                    (
                        filename,
                        subject,
                        chapter,
                        source["sha256"],
                        source["size"],
                        f"{type(exc).__name__}: {exc}",
                    ),
                )

        connection.execute(
            "INSERT INTO metadata(key,value) VALUES('semantic_catalog_digest',?)",
            (_semantic_catalog_digest(connection),),
        )
        connection.commit()
        check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise QuestionBankError(f"题库索引校验失败：{check}")
    finally:
        connection.close()


def _existing_snapshot_id(path: Path) -> str | None:
    if not path.is_file():
        return None
    try:
        connection = _connect_readonly(path)
        try:
            rows = dict(connection.execute("SELECT key,value FROM metadata"))
            if (
                rows.get("schema_version") != str(SCHEMA_VERSION)
                or rows.get("parser_version") != PARSER_VERSION
                or not _published_index_integrity(connection, rows)
            ):
                return None
            return rows.get("snapshot_id")
        finally:
            connection.close()
    except (sqlite3.Error, QuestionBankError):
        return None


def question_bank_status(
    tex_base: str | Path,
    index_path: str | Path | None = None,
    *,
    catalog=None,
) -> dict:
    """Describe source freshness without rebuilding or mutating the index."""

    base = Path(tex_base)
    destination = Path(index_path) if index_path else DEFAULT_INDEX_PATH
    if not base.exists() or not base.is_dir():
        raise QuestionBankError("普通试题库目录不存在或不可访问")
    try:
        manifest, source_snapshot = _source_manifest(base, catalog)
    except OSError as exc:
        raise QuestionBankError("普通试题库文件无法读取") from exc
    last_good = _existing_snapshot_id(destination)
    built_at = None
    coverage = None
    if last_good:
        try:
            connection = _connect_readonly(destination)
            try:
                row = connection.execute(
                    "SELECT value FROM metadata WHERE key='built_at'"
                ).fetchone()
                built_at = row[0] if row else None
            finally:
                connection.close()
            coverage = QuestionBankIndex(destination, last_good).coverage()
        except (OSError, sqlite3.Error, QuestionBankError):
            last_good = None
            built_at = None
            coverage = None
    return {
        "last_good_snapshot": last_good,
        "source_snapshot": source_snapshot,
        "stale": last_good != source_snapshot,
        "available": bool(last_good),
        "last_success_at": built_at,
        "coverage": coverage,
        "sources": {
            "configured": len(manifest),
            "available": sum(row["status"] == "available" for row in manifest),
            "missing": sum(row["status"] == "missing" for row in manifest),
        },
    }


def open_last_good_question_bank_index(
    index_path: str | Path | None = None,
) -> QuestionBankIndex | None:
    destination = Path(index_path) if index_path else DEFAULT_INDEX_PATH
    snapshot_id = _existing_snapshot_id(destination)
    if not snapshot_id:
        return None
    return QuestionBankIndex(destination, snapshot_id)


class QuestionBankIndex:
    """Stable read interface for one immutable index snapshot."""

    def __init__(self, path: str | Path, snapshot_id: str | None = None):
        self.path = Path(path)
        self.snapshot_id = snapshot_id or self._metadata().get("snapshot_id", "")

    def full_catalog_snapshot(self) -> dict:
        """Read one complete published catalog through a single SQLite handle."""

        connection = _connect_readonly(self.path)
        try:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            source_rows = connection.execute(
                "SELECT subject,chapter,source_file,sha256,size,status,error FROM sources "
                "ORDER BY subject,chapter,source_file"
            ).fetchall()
            errors = [
                {
                    "subject": row["subject"],
                    "chapter": row["chapter"],
                    "source_file": row["source_file"],
                    "status": row["status"],
                    "error": row["error"] or "",
                }
                for row in source_rows
                if row["status"] in {"missing", "error", "encoding_error"}
            ]
            scopes: dict[str, list[str]] = {}
            for row in source_rows:
                scopes.setdefault(row["subject"], [])
                if row["chapter"] not in scopes[row["subject"]]:
                    scopes[row["subject"]].append(row["chapter"])
            rows = connection.execute(
                "SELECT q.question_id,q.content,q.char_count,q.token_estimate,"
                "qs.subject,qs.chapter,qs.source_file,qs.source_number,"
                "qs.source_ordinal FROM questions q JOIN question_sources qs "
                "ON qs.question_id=q.question_id "
                "ORDER BY qs.subject,qs.chapter,qs.source_file,qs.source_ordinal,"
                "q.question_id"
            ).fetchall()
            found = {}
            for row in rows:
                found.setdefault(
                    row["question_id"],
                    {
                        "candidate_id": row["question_id"],
                        "subject": row["subject"],
                        "chapter": row["chapter"],
                        "source_file": row["source_file"],
                        "source_number": row["source_number"],
                        "source_ordinal": row["source_ordinal"],
                        "content": row["content"],
                        "char_count": row["char_count"],
                        "token_estimate": row["token_estimate"],
                    },
                )
            status = "ready"
            if errors:
                status = "degraded"
            elif not found:
                status = "empty"
            reconstructed_manifest = [
                {
                    "subject": row["subject"],
                    "chapter": row["chapter"],
                    "source_file": row["source_file"],
                    "sha256": row["sha256"],
                    "size": row["size"],
                    "status": "missing" if row["status"] == "missing" else "available",
                }
                for row in source_rows
            ]
            catalog_digest = metadata.get("catalog_semantic_digest", "")
            reconstructed_snapshot = _manifest_snapshot_id(
                reconstructed_manifest,
                catalog_semantic_digest=catalog_digest,
            )
            semantic_catalog_digest = _semantic_catalog_digest(connection)
            integrity_valid = (
                _is_sha256(catalog_digest)
                and _is_sha256(metadata.get("snapshot_id"))
                and _is_sha256(metadata.get("semantic_catalog_digest"))
                and metadata.get("snapshot_id") == reconstructed_snapshot
                and metadata.get("semantic_catalog_digest") == semantic_catalog_digest
            )
            return {
                "snapshot_id": metadata.get("snapshot_id", ""),
                "schema_version": int(metadata.get("schema_version", SCHEMA_VERSION)),
                "parser_version": metadata.get("parser_version", ""),
                "status": status,
                "integrity_valid": integrity_valid,
                "reconstructed_snapshot_id": reconstructed_snapshot,
                "semantic_catalog_digest": semantic_catalog_digest,
                "scopes": scopes,
                "candidates": list(found.values()),
                "errors": errors,
            }
        finally:
            connection.close()

    def _metadata(self) -> dict[str, str]:
        connection = _connect_readonly(self.path)
        try:
            return dict(connection.execute("SELECT key,value FROM metadata"))
        finally:
            connection.close()

    @staticmethod
    def _filters(
        subject: str | None,
        chapters: Iterable[str] | None,
        *,
        alias: str = "qs",
    ) -> tuple[str, list[str]]:
        where = []
        params: list[str] = []
        if subject:
            where.append(f"{alias}.subject=?")
            params.append(str(subject))
        if isinstance(chapters, str):
            chapters = [chapters]
        chapter_values = [
            str(chapter).strip()
            for chapter in (chapters or [])
            if str(chapter).strip()
        ]
        if chapter_values:
            where.append(
                f"{alias}.chapter IN ({','.join('?' for _ in chapter_values)})"
            )
            params.extend(chapter_values)
        return (" AND ".join(where), params)

    def coverage(
        self,
        subject: str | None = None,
        chapters: Iterable[str] | None = None,
    ) -> dict:
        connection = _connect_readonly(self.path)
        try:
            # Normalize once so callers may safely pass a generator.  The
            # coverage response uses the same scope for every query below.
            if isinstance(chapters, str):
                chapter_values = [chapters]
            else:
                chapter_values = list(chapters or [])
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            where, params = self._filters(subject, chapter_values)
            question_where = f"WHERE {where}" if where else ""
            total_questions = connection.execute(
                "SELECT COUNT(DISTINCT qs.question_id) FROM question_sources qs "
                f"{question_where}",
                params,
            ).fetchone()[0]
            occurrence_count = connection.execute(
                f"SELECT COUNT(*) FROM question_sources qs {question_where}",
                params,
            ).fetchone()[0]

            source_where, source_params = self._filters(
                subject, chapter_values, alias="s"
            )
            source_clause = f"WHERE {source_where}" if source_where else ""
            source_rows = connection.execute(
                "SELECT * FROM sources s " + source_clause,
                source_params,
            ).fetchall()
            errors = [
                {
                    "source_file": row["source_file"],
                    "subject": row["subject"],
                    "chapter": row["chapter"],
                    "status": row["status"],
                    "error": row["error"] or "",
                }
                for row in source_rows
                if row["status"] in {"missing", "error", "encoding_error"}
            ]

            exclusion_where, exclusion_params = self._filters(
                subject, chapter_values, alias="e"
            )
            exclusion_clause = (
                f"WHERE {exclusion_where}" if exclusion_where else ""
            )
            exclusion_counts = Counter(
                {
                    row["reason"]: row["count"]
                    for row in connection.execute(
                        "SELECT e.reason,COUNT(*) count FROM exclusions e "
                        f"{exclusion_clause} GROUP BY e.reason",
                        exclusion_params,
                    )
                }
            )

            chapter_where, chapter_params = self._filters(
                subject, chapter_values, alias="s"
            )
            chapter_clause = f"WHERE {chapter_where}" if chapter_where else ""
            chapter_rows = connection.execute(
                """
                SELECT s.subject,s.chapter,
                       COUNT(*) mapped_files,
                       SUM(s.status!='missing') available_files,
                       SUM(s.status='indexed') indexed_files,
                       SUM(s.parsed_count) parsed_occurrences,
                       SUM(s.indexed_count) indexed_occurrences,
                       SUM(s.excluded_count) excluded_occurrences
                FROM sources s
                """
                + chapter_clause
                + " GROUP BY s.subject,s.chapter ORDER BY s.subject,s.chapter",
                chapter_params,
            ).fetchall()

            # Compute all chapter-level unique question counts in one pass.
            # The previous implementation issued one COUNT(DISTINCT ...) query
            # per chapter, which became a noticeable N+1 cost for broad scopes.
            question_count_where, question_count_params = self._filters(
                subject, chapter_values, alias="qs"
            )
            question_count_clause = (
                f"WHERE {question_count_where}" if question_count_where else ""
            )
            question_count_rows = connection.execute(
                "SELECT qs.subject,qs.chapter,"
                "COUNT(DISTINCT qs.question_id) total_questions "
                "FROM question_sources qs "
                + question_count_clause
                + " GROUP BY qs.subject,qs.chapter",
                question_count_params,
            ).fetchall()
            question_counts = {
                (row["subject"], row["chapter"]): int(row["total_questions"])
                for row in question_count_rows
            }
            chapter_summaries = []
            for row in chapter_rows:
                item = dict(row)
                item["total_questions"] = question_counts.get(
                    (row["subject"], row["chapter"]), 0
                )
                chapter_summaries.append(item)

            status = "ready"
            if errors:
                status = "degraded"
            elif total_questions == 0:
                status = "empty"
            duplicate_count = exclusion_counts.get("duplicate_content", 0)
            total_excluded_occurrences = sum(exclusion_counts.values())
            return {
                "snapshot_id": metadata.get("snapshot_id", ""),
                "schema_version": int(
                    metadata.get("schema_version", SCHEMA_VERSION)
                ),
                "parser_version": metadata.get("parser_version", ""),
                "built_at": metadata.get("built_at", ""),
                "status": status,
                "total_questions": total_questions,
                "occurrence_count": occurrence_count,
                "duplicate_count": duplicate_count,
                "excluded_count": total_excluded_occurrences - duplicate_count,
                "total_excluded_occurrences": total_excluded_occurrences,
                "excluded_by_reason": dict(sorted(exclusion_counts.items())),
                "source_count": len(source_rows),
                "chapters": chapter_summaries,
                "errors": errors,
            }
        finally:
            connection.close()

    def select_candidates(
        self,
        subject: str,
        chapters: Iterable[str],
        question_count: int,
        seed: int | str,
        chapter_quotas: dict[str, int] | None = None,
        token_budget: int | None = None,
    ) -> list[dict]:
        subject = str(subject or "").strip()
        if isinstance(chapters, str):
            chapters = [chapters]
        # Chapter order is not an implicit user priority in this API.  A
        # canonical order keeps quota remainders and prompt manifests stable
        # when callers pass a Set or a differently ordered JSON array.
        chapter_values = sorted(
            dict.fromkeys(
                str(chapter).strip()
                for chapter in chapters
                if str(chapter).strip()
            ),
            key=lambda value: (value.casefold(), value),
        )
        if not subject:
            raise ValueError("subject 不能为空")
        if not chapter_values:
            raise ValueError("chapters 不能为空")
        if not isinstance(question_count, int) or question_count < 1:
            raise ValueError("question_count 必须是正整数")
        # Kept as a source-compatible keyword for older internal callers.
        # Cumulative token estimates no longer reject or truncate an exam
        # candidate pool; the AI boundary enforces per-request context safety.

        quotas = {}
        if chapter_quotas is not None:
            if not isinstance(chapter_quotas, dict):
                raise ValueError("chapter_quotas 必须是对象")
            for chapter, raw_count in chapter_quotas.items():
                chapter = str(chapter).strip()
                if chapter not in chapter_values:
                    raise ValueError(f"章节配额不属于所选章节：{chapter}")
                if not isinstance(raw_count, int) or raw_count < 0:
                    raise ValueError("章节配额必须是非负整数")
                quotas[chapter] = raw_count

        connection = _connect_readonly(self.path)
        try:
            placeholders = ",".join("?" for _ in chapter_values)
            rows = connection.execute(
                """
                SELECT q.question_id,q.content,q.char_count,q.token_estimate,
                       qs.chapter,qs.source_file,qs.source_number,
                       qs.source_ordinal,
                       COALESCE(u.exposed_count,0) exposed_count,
                       COALESCE(u.selected_count,0) selected_count
                FROM questions q
                JOIN question_sources qs ON qs.question_id=q.question_id
                LEFT JOIN question_usage u ON u.question_id=q.question_id
                WHERE qs.subject=? AND qs.chapter IN ("""
                + placeholders
                + """)
                ORDER BY qs.chapter,qs.source_file,qs.source_ordinal
                """,
                [subject, *chapter_values],
            ).fetchall()
        finally:
            connection.close()

        by_chapter: dict[str, list[dict]] = defaultdict(list)
        seen_occurrences = set()
        for row in rows:
            key = (row["chapter"], row["question_id"])
            if key in seen_occurrences:
                continue
            seen_occurrences.add(key)
            candidate = {
                "candidate_id": row["question_id"],
                "chapter": row["chapter"],
                "source_file": row["source_file"],
                "source_number": row["source_number"],
                "content": row["content"],
                "char_count": row["char_count"],
                "token_estimate": row["token_estimate"],
                "exposed_count": row["exposed_count"],
                "selected_count": row["selected_count"],
            }
            by_chapter[row["chapter"]].append(candidate)

        for chapter in chapter_values:
            tie_breaker = random.Random(f"{seed}:{subject}:{chapter}")
            decorated = [
                (
                    item["selected_count"],
                    item["exposed_count"],
                    tie_breaker.random(),
                    item,
                )
                for item in by_chapter[chapter]
            ]
            decorated.sort(key=lambda value: value[:3])
            by_chapter[chapter] = [
                value[3] for value in decorated
            ]

        for chapter in chapter_values:
            if not by_chapter[chapter]:
                continue
            # Spread sources before consuming deeper questions from one file.
            by_source: dict[str, list[dict]] = defaultdict(list)
            for item in by_chapter[chapter]:
                by_source[item["source_file"]].append(item)
            interleaved = []
            depth = 0
            while True:
                added = False
                for source_file in sorted(by_source):
                    values = by_source[source_file]
                    if depth < len(values):
                        interleaved.append(values[depth])
                        added = True
                if not added:
                    break
                depth += 1
            by_chapter[chapter] = interleaved

        targets = {}
        if quotas:
            targets.update(quotas)
        else:
            base, extra = divmod(question_count, len(chapter_values))
            for index, chapter in enumerate(chapter_values):
                targets[chapter] = base + (1 if index < extra else 0)

        selected: list[dict] = []
        selected_ids = set()
        def try_add(candidate: dict) -> bool:
            if candidate["candidate_id"] in selected_ids:
                return False
            selected.append(candidate)
            selected_ids.add(candidate["candidate_id"])
            return True

        positions = {chapter: 0 for chapter in chapter_values}
        for chapter in chapter_values:
            target = min(targets.get(chapter, 0), question_count - len(selected))
            while (
                len(selected) < question_count
                and target > 0
                and positions[chapter] < len(by_chapter[chapter])
            ):
                candidate = by_chapter[chapter][positions[chapter]]
                positions[chapter] += 1
                if try_add(candidate):
                    target -= 1

        while len(selected) < question_count:
            added = False
            for chapter in chapter_values:
                while positions[chapter] < len(by_chapter[chapter]):
                    candidate = by_chapter[chapter][positions[chapter]]
                    positions[chapter] += 1
                    if try_add(candidate):
                        added = True
                        break
                if len(selected) >= question_count:
                    break
            if not added:
                break

        for position, candidate in enumerate(selected, start=1):
            candidate["position"] = position
        return selected

    def all_candidates(
        self,
        subject: str,
        chapters: Iterable[str],
    ) -> list[dict]:
        """Return every unique valid question in stable source order, untruncated."""
        subject = str(subject or "").strip()
        chapter_values = sorted(
            dict.fromkeys(
                str(chapter).strip()
                for chapter in chapters
                if str(chapter).strip()
            ),
            key=lambda value: (value.casefold(), value),
        )
        if not subject or not chapter_values:
            raise ValueError("全量题目清单需要科目和章节")
        connection = _connect_readonly(self.path)
        try:
            rows = connection.execute(
                "SELECT q.question_id,q.content,q.char_count,q.token_estimate,"
                "qs.chapter,qs.source_file,qs.source_number,qs.source_ordinal "
                "FROM questions q JOIN question_sources qs ON qs.question_id=q.question_id "
                "WHERE qs.subject=? AND qs.chapter IN ("
                + ",".join("?" for _ in chapter_values)
                + ") ORDER BY qs.chapter,qs.source_file,qs.source_ordinal,q.question_id",
                [subject, *chapter_values],
            ).fetchall()
        finally:
            connection.close()
        found={}
        for row in rows:
            found.setdefault(row["question_id"], {
                "candidate_id": row["question_id"], "chapter": row["chapter"],
                "source_file": row["source_file"], "source_number": row["source_number"],
                "source_ordinal": row["source_ordinal"], "content": row["content"],
                "char_count": row["char_count"], "token_estimate": row["token_estimate"],
            })
        return list(found.values())

    def candidates_by_ids(
        self,
        candidate_ids: Iterable[str],
        *,
        subject: str,
        chapters: Iterable[str],
    ) -> list[dict]:
        """Restore a frozen candidate list in its original manifest order."""

        ids = list(
            dict.fromkeys(
                str(value).strip() for value in candidate_ids if str(value).strip()
            )
        )
        chapter_values = list(
            dict.fromkeys(
                str(chapter).strip()
                for chapter in chapters
                if str(chapter).strip()
            )
        )
        if not ids:
            raise QuestionBankError("冻结组卷清单没有候选题")
        if not str(subject).strip() or not chapter_values:
            raise QuestionBankError("冻结组卷清单缺少科目或章节范围")

        connection = _connect_readonly(self.path)
        try:
            rows = connection.execute(
                """
                SELECT q.question_id,q.content,q.char_count,q.token_estimate,
                       qs.chapter,qs.source_file,qs.source_number,
                       qs.source_ordinal,
                       COALESCE(u.exposed_count,0) exposed_count,
                       COALESCE(u.selected_count,0) selected_count
                FROM questions q
                JOIN question_sources qs ON qs.question_id=q.question_id
                LEFT JOIN question_usage u ON u.question_id=q.question_id
                WHERE q.question_id IN ("""
                + ",".join("?" for _ in ids)
                + """) AND qs.subject=? AND qs.chapter IN ("""
                + ",".join("?" for _ in chapter_values)
                + """)
                ORDER BY qs.source_file,qs.source_ordinal
                """,
                [*ids, str(subject).strip(), *chapter_values],
            ).fetchall()
        finally:
            connection.close()

        found: dict[str, dict] = {}
        for row in rows:
            if row["question_id"] in found:
                continue
            found[row["question_id"]] = {
                "candidate_id": row["question_id"],
                "chapter": row["chapter"],
                "source_file": row["source_file"],
                "source_number": row["source_number"],
                "content": row["content"],
                "char_count": row["char_count"],
                "token_estimate": row["token_estimate"],
                "exposed_count": row["exposed_count"],
                "selected_count": row["selected_count"],
            }
        missing = [candidate_id for candidate_id in ids if candidate_id not in found]
        if missing:
            raise QuestionBankError(
                "冻结组卷清单包含当前范围内不存在的候选题："
                + "、".join(missing[:3])
            )
        restored = [found[candidate_id] for candidate_id in ids]
        for position, candidate in enumerate(restored, start=1):
            candidate["position"] = position
        return restored

    def record_usage(
        self,
        exposed_ids: Iterable[str],
        selected_ids: Iterable[str] = (),
    ) -> None:
        exposed = list(dict.fromkeys(str(value) for value in exposed_ids if value))
        selected = list(dict.fromkeys(str(value) for value in selected_ids if value))
        if not exposed and not selected:
            return
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            with connection:
                for question_id in exposed:
                    connection.execute(
                        "INSERT INTO question_usage("
                        "question_id,exposed_count,selected_count,last_exposed_at"
                        ") VALUES(?,1,0,?) "
                        "ON CONFLICT(question_id) DO UPDATE SET "
                        "exposed_count=exposed_count+1,"
                        "last_exposed_at=excluded.last_exposed_at",
                        (question_id, now),
                    )
                for question_id in selected:
                    connection.execute(
                        "INSERT INTO question_usage("
                        "question_id,exposed_count,selected_count,last_selected_at"
                        ") VALUES(?,0,1,?) "
                        "ON CONFLICT(question_id) DO UPDATE SET "
                        "selected_count=selected_count+1,"
                        "last_selected_at=excluded.last_selected_at",
                        (question_id, now),
                    )
        finally:
            connection.close()


def ensure_question_bank_index(
    tex_base: str | Path,
    index_path: str | Path | None = None,
    *,
    catalog=None,
    force: bool = False,
    progress: Callable[[int, int, str], Any] | None = None,
) -> QuestionBankIndex:
    """Build or reuse an atomic index from the explicit catalog whitelist."""

    report = progress or (lambda _current, _total, _message: None)
    base = Path(tex_base)
    if not base.exists() or not base.is_dir():
        raise QuestionBankError("普通试题库目录不存在或不可访问")
    destination = Path(index_path) if index_path else DEFAULT_INDEX_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        manifest, snapshot_id = _source_manifest(base, catalog)
    except OSError as exc:
        raise QuestionBankError("普通试题库文件无法读取") from exc
    report(1, 3, "题库源文件清单已读取")
    with _BUILD_LOCK:
        if force or _existing_snapshot_id(destination) != snapshot_id:
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                report(2, 3, "正在解析并校验题库")
                _build_index(base, temporary, manifest, snapshot_id, catalog)
                try:
                    _verified_manifest, verified_snapshot_id = _source_manifest(
                        base, catalog
                    )
                except OSError as exc:
                    raise QuestionBankError(
                        "构建期间普通试题库文件无法读取"
                    ) from exc
                if verified_snapshot_id != snapshot_id:
                    raise QuestionBankError(
                        "构建期间普通试题库发生变化，旧索引保持不变"
                    )
                _copy_usage(destination, temporary)
                os.replace(temporary, destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        else:
            report(2, 3, "已复用当前题库索引")
    report(3, 3, "题库索引已校验并发布")
    return QuestionBankIndex(destination, snapshot_id)


# Short compatibility name for callers that do not need the concrete class name.
ensure_index = ensure_question_bank_index
