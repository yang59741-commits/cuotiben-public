"""Bounded readers for legacy JSON and portable v3/v4/v5 ZIP imports."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeAlias


MAX_COMPRESSED_BYTES = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 300 * 1024 * 1024
MAX_FILES = 2_000
MAX_MISTAKES = 10_000
MAX_EVENTS = 200_000
MAX_DAILY_TIPS = 10_000
MAX_COMPRESSION_RATIO = 200
SUPPORTED_PORTABLE_VERSIONS = frozenset({3, 4, 5, 6, 7})
MAX_DAILY_ANALYSIS_BYTES = 100_000

ImportSource: TypeAlias = bytes | str | Path


class PortableImportError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ParsedImport:
    input_sha256: str
    format_version: int | None
    mistakes: list[dict]
    daily_tips: list[dict]
    attachments: Mapping[str, bytes]
    legacy: bool


class ZipAttachmentBodies(Mapping[str, bytes]):
    """Lazily decompress validated attachment entries from the original ZIP."""

    def __init__(self, source: ImportSource, names: set[str]):
        self._source = source if isinstance(source, bytes) else Path(source)
        self._names = frozenset(names)

    def __getitem__(self, name: str) -> bytes:
        if name not in self._names:
            raise KeyError(name)
        archive_source = (
            io.BytesIO(self._source)
            if isinstance(self._source, bytes)
            else self._source
        )
        with zipfile.ZipFile(archive_source) as archive:
            return archive.read(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self._names)

    def __len__(self) -> int:
        return len(self._names)


def _safe_archive_name(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise PortableImportError("ARCHIVE_PATH", "ZIP 中包含不安全路径")
    return path.as_posix()


def _json_object(source: ImportSource):
    try:
        if isinstance(source, bytes):
            return json.loads(source.decode("utf-8-sig"))
        with Path(source).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableImportError("INVALID_JSON", "导入文件不是有效的 UTF-8 JSON") from exc


def _source_size(source: ImportSource) -> int:
    return len(source) if isinstance(source, bytes) else Path(source).stat().st_size


def _source_sha256(source: ImportSource) -> str:
    if isinstance(source, bytes):
        return hashlib.sha256(source).hexdigest()
    digest = hashlib.sha256()
    with Path(source).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _is_zip(source: ImportSource) -> bool:
    if isinstance(source, bytes):
        return source.startswith(b"PK\x03\x04")
    with Path(source).open("rb") as handle:
        return handle.read(4) == b"PK\x03\x04"


def _validate_counts(mistakes: object) -> list[dict]:
    if not isinstance(mistakes, list):
        raise PortableImportError("INVALID_MANIFEST", "备份中的 mistakes 必须是数组")
    if len(mistakes) > MAX_MISTAKES:
        raise PortableImportError("TOO_MANY_MISTAKES", "一次最多导入 10000 道题", 413)
    if any(not isinstance(item, dict) for item in mistakes):
        raise PortableImportError("INVALID_MANIFEST", "错题条目必须是对象")
    event_count = sum(
        len(item.get("reviews") or [])
        for item in mistakes
        if isinstance(item.get("reviews") or [], list)
    )
    if event_count > MAX_EVENTS:
        raise PortableImportError("TOO_MANY_EVENTS", "备份中的复习事件总数过多", 413)
    for item in mistakes:
        analysis = item.get("daily_analysis")
        if analysis is not None:
            if not isinstance(analysis, dict):
                raise PortableImportError("INVALID_MANIFEST", "每日分析缓存格式无效")
            encoded = json.dumps(analysis, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > MAX_DAILY_ANALYSIS_BYTES:
                raise PortableImportError("ANALYSIS_TOO_LARGE", "单题每日分析缓存过大", 413)
    return mistakes


def _daily_tip_entries(value: object, version: int | None) -> list[dict]:
    if version in {None, 2, 3, 4}:
        return []
    if not isinstance(value, list):
        raise PortableImportError("INVALID_MANIFEST", "备份中的 daily_tips 必须是数组")
    if len(value) > MAX_DAILY_TIPS:
        raise PortableImportError("TOO_MANY_DAILY_TIPS", "一次最多导入 10000 条每日提示", 413)
    if any(not isinstance(item, dict) for item in value):
        raise PortableImportError("INVALID_MANIFEST", "每日提示条目必须是对象")
    return value


def _parse_zip(source: ImportSource, digest: str) -> ParsedImport:
    try:
        archive_source = io.BytesIO(source) if isinstance(source, bytes) else Path(source)
        archive = zipfile.ZipFile(archive_source)
    except zipfile.BadZipFile as exc:
        raise PortableImportError("INVALID_ZIP", "导入文件不是有效 ZIP") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES:
            raise PortableImportError("TOO_MANY_FILES", "ZIP 文件数量过多", 413)
        names: set[str] = set()
        total = 0
        for info in infos:
            name = _safe_archive_name(info.filename)
            folded = name.casefold()
            if folded in names:
                raise PortableImportError("DUPLICATE_ARCHIVE_PATH", "ZIP 包含重复路径")
            names.add(folded)
            if info.flag_bits & 1:
                raise PortableImportError("ENCRYPTED_ZIP", "不支持加密 ZIP")
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise PortableImportError("ARCHIVE_TOO_LARGE", "ZIP 解压后内容过大", 413)
            if info.file_size and info.compress_size == 0:
                raise PortableImportError("ZIP_BOMB", "ZIP 压缩比异常", 413)
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise PortableImportError("ZIP_BOMB", "ZIP 压缩比超过安全限制", 413)
        if "manifest.json" not in {info.filename for info in infos}:
            raise PortableImportError("MANIFEST_MISSING", "ZIP 缺少 manifest.json")
        manifest = _json_object(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("format") != "cuoti-portable-backup":
            raise PortableImportError("INVALID_MANIFEST", "不是错题本可移植备份")
        version = manifest.get("format_version")
        if version not in SUPPORTED_PORTABLE_VERSIONS:
            raise PortableImportError("FUTURE_VERSION", "不支持的可移植备份版本")
        mistakes = _validate_counts(manifest.get("mistakes"))
        daily_tips = _daily_tip_entries(manifest.get("daily_tips"), version)
        referenced: set[str] = set()
        for item in mistakes:
            attachments = item.get("attachments") or []
            if not isinstance(attachments, list):
                raise PortableImportError("INVALID_MANIFEST", "attachments 必须是数组")
            for metadata in attachments:
                if not isinstance(metadata, dict):
                    raise PortableImportError("INVALID_MANIFEST", "附件清单格式无效")
                name = _safe_archive_name(str(metadata.get("archive_path") or ""))
                if not name.startswith("attachments/") or name in referenced:
                    raise PortableImportError("ATTACHMENT_PATH", "附件路径无效或重复")
                referenced.add(name)
                try:
                    attachment_source = archive.open(name)
                except KeyError as exc:
                    raise PortableImportError("ATTACHMENT_MISSING", "附件文件缺失") from exc
                size = 0
                attachment_digest = hashlib.sha256()
                with attachment_source:
                    while True:
                        chunk = attachment_source.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        attachment_digest.update(chunk)
                if size != metadata.get("byte_size"):
                    raise PortableImportError("ATTACHMENT_SIZE", "附件大小校验失败")
                if attachment_digest.hexdigest() != metadata.get("sha256"):
                    raise PortableImportError("ATTACHMENT_HASH", "附件 SHA-256 校验失败")
        extras = {
            info.filename
            for info in infos
            if not info.is_dir() and info.filename != "manifest.json" and info.filename not in referenced
        }
        if extras:
            raise PortableImportError("UNREFERENCED_FILE", "ZIP 包含清单未引用的文件")
        return ParsedImport(
            digest,
            version,
            mistakes,
            daily_tips,
            ZipAttachmentBodies(source, referenced),
            False,
        )


def parse_import(
    source: ImportSource, *, expected_sha256: str | None = None
) -> ParsedImport:
    """Parse bytes or a staged file without loading the staged file wholesale."""

    if _source_size(source) > MAX_COMPRESSED_BYTES:
        raise PortableImportError("IMPORT_TOO_LARGE", "导入文件超过 100 MB", 413)
    digest = _source_sha256(source)
    if expected_sha256 is not None and digest != expected_sha256:
        raise PortableImportError(
            "IMPORT_CHANGED", "导入文件在接收后发生变化，请重新预览", 409
        )
    if _is_zip(source):
        return _parse_zip(source, digest)
    parsed = _json_object(source)
    if isinstance(parsed, list):
        return ParsedImport(digest, None, _validate_counts(parsed), [], {}, True)
    if not isinstance(parsed, dict):
        raise PortableImportError("INVALID_BACKUP", "备份必须是数组或版本化对象")
    if "format_version" in parsed:
        version = parsed.get("format_version")
        if version not in SUPPORTED_PORTABLE_VERSIONS:
            raise PortableImportError("FUTURE_VERSION", "不支持的可移植备份版本")
        return ParsedImport(
            digest,
            version,
            _validate_counts(parsed.get("mistakes")),
            _daily_tip_entries(parsed.get("daily_tips"), version),
            {},
            False,
        )
    version = parsed.get("schema_version")
    if version != 2:
        raise PortableImportError("FUTURE_VERSION", "不支持的备份版本")
    return ParsedImport(digest, 2, _validate_counts(parsed.get("mistakes")), [], {}, True)
