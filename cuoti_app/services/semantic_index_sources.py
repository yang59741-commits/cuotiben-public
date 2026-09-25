"""Immutable source orchestration for semantic-index scans.

This module publishes a last-good formal-past-exam artifact and combines it
with one already-published ordinary question-bank index.  It never calls AI,
submits jobs, writes semantic database rows, or reads an in-progress artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping

from .ai_budgeting import estimate_text_tokens
from .past_exams import (
    PastExamSourceError,
    canonical_problem_content,
    discover_past_exam_sources,
    extract_yearly_problems,
    iter_official_tex_files,
    normalize_institution_name,
    past_exam_source_snapshot,
)
from .question_bank import (
    PARSER_VERSION as QUESTION_BANK_PARSER_VERSION,
    SCHEMA_VERSION as QUESTION_BANK_SCHEMA_VERSION,
)
from .semantic_index_scanner import (
    SemanticScanCandidate,
    prepare_semantic_candidates,
    semantic_composite_source_snapshot,
)


FORMAL_PAST_ARTIFACT_SCHEMA_VERSION = 2
FORMAL_PAST_PARSER_VERSION = 2
FORMAL_PAST_NAMESPACE_POLICY_VERSION = (
    "local-location-v1_other-content-v1_explicit-provenance-v1"
)
SEMANTIC_SOURCE_ORCHESTRATOR_VERSION = 1
FORMAL_PAST_SOURCE_KINDS = ("past_exam_local", "past_exam_other")


class SemanticIndexSourceError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SemanticSourceSlice:
    source_kind: str
    source_snapshot: str
    scope_digest: str
    candidates: tuple[SemanticScanCandidate, ...]


@dataclass(frozen=True, slots=True)
class FormalPastExamArtifact:
    source_snapshot: str
    artifact_snapshot: str
    namespace_policy_version: str
    home_institution: str
    source_manifest: tuple[Mapping[str, Any], ...]
    candidates: tuple[SemanticScanCandidate, ...]


@dataclass(frozen=True, slots=True)
class SemanticSourceBundle:
    source_snapshot: str
    policy_version: str
    scanned_source_kinds: tuple[str, ...]
    candidates: tuple[SemanticScanCandidate, ...]
    slices: tuple[SemanticSourceSlice, ...]


def _text(value: Any, field: str, *, maximum: int = 2_048) -> str:
    if not isinstance(value, str):
        raise SemanticIndexSourceError("VALIDATION", f"{field} 必须是字符串")
    rendered = unicodedata.normalize("NFC", value).strip()
    if not rendered:
        raise SemanticIndexSourceError("VALIDATION", f"{field} 不能为空")
    if len(rendered) > maximum:
        raise SemanticIndexSourceError(
            "VALIDATION", f"{field} 超过 {maximum} 字符"
        )
    return rendered


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: Any, field: str) -> str:
    rendered = _text(value, field, maximum=64)
    if len(rendered) != 64 or any(
        char not in "0123456789abcdefABCDEF" for char in rendered
    ):
        raise SemanticIndexSourceError("ARTIFACT_INVALID", f"{field} 不是 SHA-256 摘要")
    return rendered


def _normalized_home_institution(value: str) -> str:
    normalized = normalize_institution_name(
        _text(value, "home_institution", maximum=320)
    )
    if not normalized:
        raise SemanticIndexSourceError(
            "VALIDATION", "home_institution 规范化后不能为空"
        )
    return normalized


def _local_candidate_id(
    institution: str, subject: str, year: int, source_number: str | int
) -> str:
    payload = {
        "policy": "local-location-v1",
        "institution": normalize_institution_name(institution),
        "subject": unicodedata.normalize("NFC", str(subject)).strip(),
        "year": int(year),
        "source_number": str(source_number).strip(),
    }
    return "PL" + hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()[:24].upper()


def _other_candidate_id(subject: str, content: str) -> str:
    canonical = canonical_problem_content(content)
    return "PE" + hashlib.sha256(
        f"{subject}\0{canonical}".encode("utf-8")
    ).hexdigest()[:24].upper()


def _source_exam_id(
    institution: str, subject: str, year: int, relative_path: str
) -> str:
    return "FX" + _sha256(
        {
            "policy": "formal-past-exam-provenance-v1",
            "institution": normalize_institution_name(institution),
            "subject": unicodedata.normalize("NFC", subject).strip(),
            "year": int(year),
            "published_source_file": relative_path.replace("\\", "/"),
        }
    )[:30].upper()


def _section_id(source_exam_id: str, source_section: str) -> str:
    return "FS" + _sha256(
        {
            "policy": "formal-past-section-provenance-v1",
            "source_exam_id": source_exam_id,
            "source_section": unicodedata.normalize("NFC", source_section).strip(),
        }
    )[:30].upper()


def _candidate_record(
    *,
    source_kind: str,
    candidate_id: str,
    institution: str,
    subject: str,
    section: Mapping[str, Any],
    problem: Mapping[str, Any],
    display_order: int,
) -> dict:
    relative_path = str(section.get("relative_path") or "").replace("\\", "/")
    source_exam_id = _source_exam_id(
        institution, subject, int(section["year"]), relative_path
    )
    source_section = str(problem.get("source_section") or "未分板块")
    return {
        "source_kind": source_kind,
        "candidate_id": candidate_id,
        "source_institution": institution,
        "subject": subject,
        "chapter": source_section,
        "source_exam_id": source_exam_id,
        "section_id": _section_id(source_exam_id, source_section),
        "content": str(problem.get("content") or ""),
        "token_estimate": estimate_text_tokens(problem.get("content")) or 1,
        "source_file": relative_path,
        "source_year": int(section["year"]),
        "source_number": str(problem["source_number"]),
        "display_order": display_order,
    }


def _formal_source_manifest(directory: str | Path) -> list[dict]:
    base = Path(directory)
    try:
        paths = list(iter_official_tex_files(base))
        manifest = []
        for path in paths:
            payload = path.read_bytes()
            manifest.append(
                {
                    "relative_path": path.relative_to(base).as_posix(),
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        return manifest
    except OSError as exc:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_UNAVAILABLE", "正式真题来源清单无法完整读取"
        ) from exc


def _validate_formal_discovery(
    directory: str | Path, sources: Iterable[Mapping[str, Any]]
) -> None:
    base = Path(directory)
    expected = {
        path.resolve()
        for path in iter_official_tex_files(base)
    }
    represented = {
        Path(path).resolve()
        for source in sources
        for path in source.get("paths") or ()
    }
    if expected != represented:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_DEGRADED",
            "部分正式真题文件未形成可发布来源，保留旧 last-good 工件",
        )
    unavailable = [
        (source.get("institution"), source.get("subject"), section.get("year"))
        for source in sources
        for section in source.get("all_exam_sections") or ()
        if not section.get("available", False)
    ]
    if unavailable:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_DEGRADED",
            "正式真题包含不可用年份，保留旧 last-good 工件",
        )


def _enumerate_formal_candidates(
    directory: str | Path, *, home_institution: str
) -> list[dict]:
    try:
        sources = discover_past_exam_sources(str(directory))
    except (OSError, PastExamSourceError) as exc:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_UNAVAILABLE", "正式真题来源无法完整读取"
        ) from exc
    _validate_formal_discovery(directory, sources)
    records: list[dict] = []
    local_identities: dict[str, tuple[str, str]] = {}
    other_content_seen: set[tuple[str, str]] = set()
    display_order = 0
    for source in sources:
        institution = normalize_institution_name(source.get("institution") or "")
        subject = str(source.get("subject") or "").strip()
        is_local = institution == home_institution
        sections = sorted(
            source.get("exam_sections") or (),
            key=lambda item: (
                int(item["year"]),
                str(item.get("relative_path") or ""),
            ),
        )
        for section in sections:
            for problem in extract_yearly_problems(section):
                content = str(problem.get("content") or "")
                if is_local:
                    candidate_id = _local_candidate_id(
                        institution,
                        subject,
                        int(section["year"]),
                        problem["source_number"],
                    )
                    identity_payload = (
                        canonical_problem_content(content),
                        str(section.get("relative_path") or ""),
                    )
                    previous = local_identities.get(candidate_id)
                    if previous is not None and previous[0] != identity_payload[0]:
                        raise SemanticIndexSourceError(
                            "LOCAL_IDENTITY_CONFLICT",
                            "同一校内稳定身份对应不同题干，拒绝发布 last-good 工件",
                        )
                    if previous is not None:
                        continue
                    local_identities[candidate_id] = identity_payload
                    source_kind = "past_exam_local"
                else:
                    canonical = canonical_problem_content(content)
                    dedupe_key = (subject, canonical)
                    if dedupe_key in other_content_seen:
                        continue
                    other_content_seen.add(dedupe_key)
                    candidate_id = _other_candidate_id(subject, content)
                    source_kind = "past_exam_other"
                display_order += 1
                records.append(
                    _candidate_record(
                        source_kind=source_kind,
                        candidate_id=candidate_id,
                        institution=institution,
                        subject=subject,
                        section=section,
                        problem=problem,
                        display_order=display_order,
                    )
                )
    if not any(row["source_kind"] == "past_exam_local" for row in records):
        raise SemanticIndexSourceError(
            "HOME_SOURCE_MISSING",
            f"正式真题中没有可发布的校内题目：{home_institution}",
        )
    if not records:
        raise SemanticIndexSourceError(
            "EMPTY_FORMAL_SOURCE", "正式真题没有可发布的有效题目"
        )
    records.sort(key=lambda row: (row["source_kind"], row["candidate_id"]))
    return records


def _artifact_payload(
    *,
    home_institution: str,
    source_snapshot: str,
    source_manifest: list[dict],
    candidates: list[dict],
) -> dict:
    return {
        "schema_version": FORMAL_PAST_ARTIFACT_SCHEMA_VERSION,
        "parser_version": FORMAL_PAST_PARSER_VERSION,
        "namespace_policy_version": FORMAL_PAST_NAMESPACE_POLICY_VERSION,
        "home_institution": home_institution,
        "source_snapshot": source_snapshot,
        "source_manifest": source_manifest,
        "candidates": candidates,
    }


def _write_atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _validated_relative_source_file(value: Any) -> str:
    rendered = _text(value, "source_file", maximum=2_048).replace("\\", "/")
    path = PurePosixPath(rendered)
    windows_path = PureWindowsPath(rendered)
    if (
        path.is_absolute()
        or ".." in path.parts
        or windows_path.is_absolute()
        or bool(windows_path.drive)
        or ":" in rendered
    ):
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件包含非相对来源路径"
        )
    return path.as_posix()


def _record_to_candidate(record: Mapping[str, Any], home_institution: str):
    try:
        source_kind = _text(record.get("source_kind"), "source_kind", maximum=64)
        candidate_id = _text(record.get("candidate_id"), "candidate_id", maximum=256)
        institution = normalize_institution_name(
            _text(record.get("source_institution"), "source_institution", maximum=320)
        )
        subject = _text(record.get("subject"), "subject", maximum=320)
        chapter = _text(record.get("chapter"), "chapter", maximum=640)
        content = _text(record.get("content"), "content", maximum=2_000_000)
        source_file = _validated_relative_source_file(record.get("source_file"))
        source_year = int(record.get("source_year"))
        source_number = _text(
            str(record.get("source_number")), "source_number", maximum=512
        )
        source_exam_id = _text(
            record.get("source_exam_id"), "source_exam_id", maximum=512
        )
        section_id = _text(record.get("section_id"), "section_id", maximum=512)
        token_estimate = int(record.get("token_estimate"))
        display_order = int(record.get("display_order"))
    except (TypeError, ValueError) as exc:
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件字段类型无效"
        ) from exc
    expected_kind = (
        "past_exam_local" if institution == home_institution else "past_exam_other"
    )
    if source_kind != expected_kind:
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件来源命名空间与院校策略不一致"
        )
    expected_id = (
        _local_candidate_id(institution, subject, source_year, source_number)
        if source_kind == "past_exam_local"
        else _other_candidate_id(subject, content)
    )
    if candidate_id != expected_id:
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件候选身份校验失败"
        )
    expected_exam_id = _source_exam_id(
        institution, subject, source_year, source_file
    )
    expected_section_id = _section_id(expected_exam_id, chapter)
    if source_exam_id != expected_exam_id or section_id != expected_section_id:
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件来源试卷或分区身份校验失败"
        )
    if token_estimate < 1 or display_order < 1:
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题工件计数必须为正整数"
        )
    return SemanticScanCandidate(
        source_kind=source_kind,
        candidate_id=candidate_id,
        subject=subject,
        chapter=chapter,
        content=content,
        token_estimate=token_estimate,
        source_file=source_file,
        source_year=source_year,
        source_number=source_number,
        source_note=institution,
        source_exam_id=source_exam_id,
        section_id=section_id,
        source_institution=institution,
        display_order=display_order,
    )


def open_formal_past_exam_artifact(
    artifact_path: str | Path,
) -> FormalPastExamArtifact:
    path = Path(artifact_path)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SemanticIndexSourceError(
            "ARTIFACT_UNAVAILABLE", "正式真题 last-good 工件不存在或已损坏"
        ) from exc
    if not isinstance(document, dict):
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件根结构无效")
    if document.get("schema_version") != FORMAL_PAST_ARTIFACT_SCHEMA_VERSION:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件 schema 不兼容")
    if document.get("parser_version") != FORMAL_PAST_PARSER_VERSION:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题解析器版本不兼容")
    if document.get("namespace_policy_version") != FORMAL_PAST_NAMESPACE_POLICY_VERSION:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题命名空间策略不兼容")
    home_institution = _normalized_home_institution(document.get("home_institution"))
    source_snapshot = _digest(document.get("source_snapshot"), "source_snapshot").upper()
    records = document.get("candidates")
    source_manifest = document.get("source_manifest")
    if not isinstance(source_manifest, list) or not source_manifest:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件来源清单为空")
    seen_paths = set()
    for item in source_manifest:
        if not isinstance(item, dict):
            raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题来源清单结构无效")
        relative_path = _validated_relative_source_file(item.get("relative_path"))
        if relative_path in seen_paths:
            raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题来源清单路径重复")
        seen_paths.add(relative_path)
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError) as exc:
            raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题来源大小无效") from exc
        digest = str(item.get("sha256") or "")
        if size < 0 or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题来源摘要无效")
    if not isinstance(records, list) or not records:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件候选清单为空")
    payload = _artifact_payload(
        home_institution=home_institution,
        source_snapshot=source_snapshot,
        source_manifest=source_manifest,
        candidates=records,
    )
    artifact_snapshot = _sha256(payload)
    if document.get("artifact_snapshot") != artifact_snapshot:
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件摘要校验失败")
    if any(
        not isinstance(record, Mapping)
        or _validated_relative_source_file(record.get("source_file")) not in seen_paths
        for record in records
    ):
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题候选引用了未发布的来源文件"
        )
    candidates = tuple(
        _record_to_candidate(record, home_institution)
        for record in records
        if isinstance(record, Mapping)
    )
    if len(candidates) != len(records):
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件候选结构无效")
    if any(candidate.source_file not in seen_paths for candidate in candidates):
        raise SemanticIndexSourceError(
            "ARTIFACT_INVALID", "正式真题候选引用了未发布的来源文件"
        )
    prepare_semantic_candidates(candidates, scanned_source_kinds=FORMAL_PAST_SOURCE_KINDS)
    if not any(item.source_kind == "past_exam_local" for item in candidates):
        raise SemanticIndexSourceError("ARTIFACT_INVALID", "正式真题工件缺少校内来源")
    return FormalPastExamArtifact(
        source_snapshot=source_snapshot,
        artifact_snapshot=artifact_snapshot,
        namespace_policy_version=FORMAL_PAST_NAMESPACE_POLICY_VERSION,
        home_institution=home_institution,
        source_manifest=tuple(source_manifest),
        candidates=candidates,
    )


def ensure_formal_past_exam_artifact(
    directory: str | Path,
    *,
    artifact_path: str | Path,
    home_institution: str,
    allow_candidate_deletions: bool = False,
) -> FormalPastExamArtifact:
    if not isinstance(allow_candidate_deletions, bool):
        raise SemanticIndexSourceError(
            "VALIDATION", "allow_candidate_deletions 必须是布尔值"
        )
    home = _normalized_home_institution(home_institution)
    try:
        source_snapshot = past_exam_source_snapshot(str(directory))
    except (OSError, PastExamSourceError) as exc:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_UNAVAILABLE", "正式真题来源 snapshot 无法读取"
        ) from exc
    path = Path(artifact_path)
    try:
        existing = open_formal_past_exam_artifact(path)
    except SemanticIndexSourceError:
        existing = None
    if (
        existing is not None
        and existing.source_snapshot == source_snapshot
        and existing.home_institution == home
    ):
        return existing
    records = _enumerate_formal_candidates(directory, home_institution=home)
    if existing is not None and existing.home_institution == home:
        old_identities = {
            (candidate.source_kind, candidate.candidate_id)
            for candidate in existing.candidates
        }
        new_identities = {
            (record["source_kind"], record["candidate_id"])
            for record in records
        }
        missing = old_identities - new_identities
        if missing and not allow_candidate_deletions:
            raise SemanticIndexSourceError(
                "FORMAL_SOURCE_REGRESSION",
                "正式真题候选减少，未获删除授权，保留旧 last-good 工件",
            )
    source_manifest = _formal_source_manifest(directory)
    try:
        verified_snapshot = past_exam_source_snapshot(str(directory))
    except (OSError, PastExamSourceError) as exc:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_CHANGED", "正式真题构建后无法复核来源 snapshot"
        ) from exc
    if verified_snapshot != source_snapshot:
        raise SemanticIndexSourceError(
            "FORMAL_SOURCE_CHANGED", "正式真题构建期间发生变化，保留旧 last-good 工件"
        )
    payload = _artifact_payload(
        home_institution=home,
        source_snapshot=source_snapshot,
        source_manifest=source_manifest,
        candidates=records,
    )
    document = {**payload, "artifact_snapshot": _sha256(payload)}
    try:
        _write_atomic_json(path, document)
    except OSError as exc:
        raise SemanticIndexSourceError(
            "ARTIFACT_PUBLISH_FAILED", "正式真题 last-good 工件原子发布失败"
        ) from exc
    return open_formal_past_exam_artifact(path)


def formal_past_exam_status(
    directory: str | Path,
    *,
    artifact_path: str | Path,
    home_institution: str,
) -> dict:
    home = _normalized_home_institution(home_institution)
    try:
        artifact = open_formal_past_exam_artifact(artifact_path)
    except SemanticIndexSourceError:
        artifact = None
    try:
        source_snapshot = past_exam_source_snapshot(str(directory))
    except (OSError, PastExamSourceError):
        source_snapshot = None
    policy_matches = artifact is not None and artifact.home_institution == home
    counts = {kind: 0 for kind in FORMAL_PAST_SOURCE_KINDS}
    if artifact is not None:
        for candidate in artifact.candidates:
            counts[candidate.source_kind] += 1
    return {
        "available": bool(artifact and policy_matches),
        "stale": not (
            artifact
            and policy_matches
            and source_snapshot is not None
            and artifact.source_snapshot == source_snapshot
        ),
        "source_snapshot": source_snapshot,
        "last_good_snapshot": artifact.artifact_snapshot if artifact else None,
        "home_institution": artifact.home_institution if artifact else None,
        "counts": counts,
    }


def load_ordinary_source_slice(index: Any) -> SemanticSourceSlice:
    try:
        snapshot = index.full_catalog_snapshot()
    except Exception as exc:
        raise SemanticIndexSourceError(
            "ORDINARY_SOURCE_UNAVAILABLE", "普通题库 last-good 索引无法完整读取"
        ) from exc
    if snapshot.get("snapshot_id") != getattr(index, "snapshot_id", None):
        raise SemanticIndexSourceError(
            "ORDINARY_SNAPSHOT_MISMATCH", "普通题库索引句柄与已发布 snapshot 不一致"
        )
    if not snapshot.get("integrity_valid"):
        raise SemanticIndexSourceError(
            "ORDINARY_INTEGRITY_FAILED",
            "普通题库 last-good 索引来源清单或内容摘要校验失败",
        )
    if (
        snapshot.get("schema_version") != QUESTION_BANK_SCHEMA_VERSION
        or snapshot.get("parser_version") != QUESTION_BANK_PARSER_VERSION
    ):
        raise SemanticIndexSourceError(
            "ORDINARY_VERSION_MISMATCH",
            "普通题库 last-good 索引 schema/parser 版本不兼容",
        )
    if snapshot.get("status") != "ready":
        raise SemanticIndexSourceError(
            "ORDINARY_NOT_READY", "普通题库 last-good 索引不是完整 ready 状态"
        )
    rows = snapshot.get("candidates")
    scopes = snapshot.get("scopes")
    if not isinstance(rows, list) or not rows or not isinstance(scopes, dict) or not scopes:
        raise SemanticIndexSourceError(
            "ORDINARY_NOT_READY", "普通题库 last-good 索引为空或缺少完整范围"
        )
    scope_pairs = {
        (str(subject), str(chapter))
        for subject, chapters in scopes.items()
        for chapter in chapters
    }
    if any(
        (str(row.get("subject")), str(row.get("chapter"))) not in scope_pairs
        for row in rows
    ):
        raise SemanticIndexSourceError(
            "ORDINARY_INTEGRITY_FAILED",
            "普通题库候选范围不属于已发布来源清单",
        )
    candidates = tuple(
        SemanticScanCandidate(
            source_kind="ordinary",
            candidate_id=str(row.get("candidate_id") or ""),
            subject=str(row.get("subject") or ""),
            chapter=str(row.get("chapter") or ""),
            content=row.get("content") or "",
            token_estimate=row.get("token_estimate"),
            source_file=row.get("source_file"),
            source_number=row.get("source_number"),
            display_order=row.get("source_ordinal"),
        )
        for row in rows
    )
    prepare_semantic_candidates(candidates, scanned_source_kinds=("ordinary",))
    scope_digest = _sha256(scopes)
    component_snapshot = _sha256(
        {
            "published_snapshot": snapshot["snapshot_id"],
            "scope_digest": scope_digest,
            "orchestrator_version": SEMANTIC_SOURCE_ORCHESTRATOR_VERSION,
        }
    )
    return SemanticSourceSlice(
        source_kind="ordinary",
        source_snapshot=component_snapshot,
        scope_digest=scope_digest,
        candidates=candidates,
    )


def formal_past_source_slices(
    artifact: FormalPastExamArtifact,
) -> tuple[SemanticSourceSlice, SemanticSourceSlice]:
    slices = []
    for source_kind in FORMAL_PAST_SOURCE_KINDS:
        candidates = tuple(
            candidate
            for candidate in artifact.candidates
            if candidate.source_kind == source_kind
        )
        source_snapshot = _sha256(
            {
                "artifact_snapshot": artifact.artifact_snapshot,
                "source_kind": source_kind,
                "orchestrator_version": SEMANTIC_SOURCE_ORCHESTRATOR_VERSION,
            }
        )
        slices.append(
            SemanticSourceSlice(
                source_kind=source_kind,
                source_snapshot=source_snapshot,
                scope_digest=_sha256(
                    {
                        "home_institution": artifact.home_institution,
                        "namespace_policy_version": artifact.namespace_policy_version,
                        "source_kind": source_kind,
                    }
                ),
                candidates=candidates,
            )
        )
    return slices[0], slices[1]


def build_semantic_source_bundle(
    ordinary_index: Any,
    formal_artifact: FormalPastExamArtifact,
) -> SemanticSourceBundle:
    slices = (
        load_ordinary_source_slice(ordinary_index),
        *formal_past_source_slices(formal_artifact),
    )
    source_kinds = tuple(item.source_kind for item in slices)
    if len(source_kinds) != len(set(source_kinds)):
        raise SemanticIndexSourceError(
            "DUPLICATE_SOURCE_KIND", "来源编排包含重复命名空间"
        )
    candidates = tuple(
        sorted(
            (candidate for item in slices for candidate in item.candidates),
            key=lambda candidate: (candidate.source_kind, candidate.candidate_id),
        )
    )
    prepare_semantic_candidates(candidates, scanned_source_kinds=source_kinds)
    policy_version = (
        f"source-orchestrator-v{SEMANTIC_SOURCE_ORCHESTRATOR_VERSION}:"
        f"{formal_artifact.namespace_policy_version}:"
        f"{formal_artifact.home_institution}"
    )
    composite = semantic_composite_source_snapshot(
        {item.source_kind: item.source_snapshot for item in slices},
        policy_version=policy_version,
    )
    return SemanticSourceBundle(
        source_snapshot=composite,
        policy_version=policy_version,
        scanned_source_kinds=source_kinds,
        candidates=candidates,
        slices=slices,
    )


def select_semantic_source_bundle(
    bundle: SemanticSourceBundle,
    source_kinds: Iterable[str],
) -> SemanticSourceBundle:
    """Return a complete, identity-bound subset of an existing source bundle."""

    if not isinstance(bundle, SemanticSourceBundle):
        raise SemanticIndexSourceError(
            "VALIDATION", "source bundle 必须是正式 SemanticSourceBundle"
        )
    requested = tuple(
        sorted(
            {
                _text(str(value), "source_kind", maximum=64)
                for value in source_kinds
            }
        )
    )
    if not requested:
        raise SemanticIndexSourceError(
            "INCOMPLETE_SOURCE_SCOPE", "release source scope 不能为空"
        )
    slices_by_kind = {item.source_kind: item for item in bundle.slices}
    missing = set(requested) - set(slices_by_kind)
    if missing:
        raise SemanticIndexSourceError(
            "INCOMPLETE_SOURCE_SCOPE", "release source scope 不属于正式来源快照"
        )
    selected_slices = tuple(slices_by_kind[kind] for kind in requested)
    selected_candidates = tuple(
        sorted(
            (
                candidate
                for item in selected_slices
                for candidate in item.candidates
            ),
            key=lambda candidate: (candidate.source_kind, candidate.candidate_id),
        )
    )
    prepare_semantic_candidates(
        selected_candidates,
        scanned_source_kinds=requested,
    )
    policy_version = (
        f"{bundle.policy_version}:selected-source-kinds-v1:{','.join(requested)}"
    )
    source_snapshot = semantic_composite_source_snapshot(
        {
            item.source_kind: item.source_snapshot
            for item in selected_slices
        },
        policy_version=policy_version,
    )
    return SemanticSourceBundle(
        source_snapshot=source_snapshot,
        policy_version=policy_version,
        scanned_source_kinds=requested,
        candidates=selected_candidates,
        slices=selected_slices,
    )
