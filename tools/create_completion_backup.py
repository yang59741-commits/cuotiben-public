"""Create and freeze a verified post-development project snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any
from contextlib import ExitStack
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuoti_app.config import build_config
from cuoti_app.services.formal_runtime import build_formal_runtime_overrides, database_service_guard


SOURCE_DIRECTORIES = (
    "archive",
    "cuoti_app",
    "docs",
    "launcher_assets",
    "packaging",
    "tests",
    "tools",
)
SHAREABLE_SOURCE_DIRECTORIES = tuple(
    directory for directory in SOURCE_DIRECTORIES if directory != "archive"
)
SHAREABLE_TOP_LEVEL_FILES = frozenset(
    {
        ".gitignore",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
        "MANIFEST.in",
        "README.md",
        "app.py",
        "eslint.config.mjs",
        "launcher.py",
        "package-lock.json",
        "package.json",
        "pyproject.toml",
        "pytest.ini",
        "requirements-build.txt",
        "requirements-dev.txt",
        "requirements-constraints.txt",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
        "启动.bat",
        "安装依赖.bat",
    }
)
RECOVERABLE_SNAPSHOT_KINDS = frozenset(
    {"private", "private-full", "private-no-secrets"}
)
SNAPSHOT_SCHEMA_VERSION = 2
FORMAL_SNAPSHOT_PATHS = {
    "database": "data/cuoti.db",
    "settings": "data/runtime/settings.json",
    "pdfs": "data/runtime/pdfs",
    "attachments": "data/runtime/attachments",
    "ai_memory": "data/runtime/memory",
    "past_exam_profiles": "data/runtime/past_exam_profiles",
    "job_inputs": "data/runtime/job_inputs",
    "catalog": "data/runtime/catalog.json",
    "ai_usage": "data/runtime/ai-usage.jsonl",
    "d2_coverage": "data/runtime/d2-expansion-coverage.json",
    "profile_expansion_evidence": "data/runtime/profile_expansion_evidence",
}
SKIPPED_DIRECTORY_NAMES = {
    "__pycache__",
    ".pytest_cache",
    "build",
    "dist",
}
SKIPPED_FILE_NAMES = {"settings.json"}
SKIPPED_SUFFIXES = {".pyc", ".pyo"}
SKIPPED_TOP_LEVEL_FILES = {".git", "cuoti.db"}
SQLITE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
SENSITIVE_SETTING_FRAGMENTS = (
    "api_key",
    "app_id",
    "credential",
    "password",
    "secret",
    "token",
)
PATH_SETTING_NAMES = {
    "attachment_dir",
    "database",
    "past_exam_dir",
    "pdf_dir",
    "settings_path",
    "tex_base",
}
KEY_TABLES = (
    "subjects",
    "chapters",
    "mistakes",
    "review_events",
    "exams",
    "mistake_exports",
    "daily_analysis_items",
    "background_jobs",
    "job_inputs",
    "job_exam_results",
    "exam_generation_manifests",
)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _is_sqlite_artifact(path: Path) -> bool:
    lowered = path.name.casefold()
    return (
        path.suffix.casefold() in SQLITE_FILE_SUFFIXES
        or lowered.endswith(SQLITE_SIDECAR_SUFFIXES)
    )


def should_skip(relative: Path) -> bool:
    return (
        any(part in SKIPPED_DIRECTORY_NAMES for part in relative.parts)
        or relative.name in SKIPPED_FILE_NAMES
        or relative.suffix.lower() in SKIPPED_SUFFIXES
        or _is_sqlite_artifact(relative)
        or relative.name.endswith(".service.lock")
    )


def _resolved_exclusions(paths: set[Path] | None) -> tuple[Path, ...]:
    return tuple(sorted(
        {path.resolve() for path in (paths or set())},
        key=lambda item: (len(item.parts), str(item).casefold()),
    ))


def _excluded(path: Path, exclusions: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == excluded or excluded in resolved.parents for excluded in exclusions)


def _copy_tree_pruned(
    source_root: Path,
    project_root: Path,
    target: Path,
    *,
    exclusions: tuple[Path, ...],
    source_only: bool,
) -> None:
    """Copy one source tree without traversing excluded/runtime ancestors."""

    for current, directory_names, file_names in os.walk(
        source_root, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        if _excluded(current_path, exclusions):
            directory_names[:] = []
            continue
        kept_directories = []
        for name in sorted(directory_names, key=str.casefold):
            candidate = current_path / name
            relative = candidate.relative_to(project_root)
            if candidate.is_symlink() or should_skip(relative) or _excluded(candidate, exclusions):
                continue
            kept_directories.append(name)
        directory_names[:] = kept_directories
        for name in sorted(file_names, key=str.casefold):
            source = current_path / name
            relative = source.relative_to(project_root)
            if (
                source.is_symlink()
                or should_skip(relative)
                or (source_only and source.suffix.casefold() in {".exe", ".log"})
                or _excluded(source, exclusions)
            ):
                continue
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)


def copy_project_files(
    root: Path,
    target: Path,
    *,
    excluded_paths: set[Path] | None = None,
    source_only: bool = False,
) -> None:
    excluded = _resolved_exclusions(excluded_paths)
    for source in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if (
            not source.is_file()
            or (source_only and source.name not in SHAREABLE_TOP_LEVEL_FILES)
            or source.name in SKIPPED_TOP_LEVEL_FILES
            or source.is_symlink()
            or should_skip(Path(source.name))
            or source.suffix.casefold() == ".exe"
            or _excluded(source, excluded)
        ):
            continue
        shutil.copy2(source, target / source.name)

    directories = SHAREABLE_SOURCE_DIRECTORIES if source_only else SOURCE_DIRECTORIES
    for directory_name in directories:
        source_directory = root / directory_name
        if (
            not source_directory.is_dir()
            or source_directory.is_symlink()
            or _excluded(source_directory, excluded)
        ):
            continue
        _copy_tree_pruned(
            source_directory,
            root,
            target,
            exclusions=excluded,
            source_only=source_only,
        )


def copy_directory(
    source: Path,
    destination: Path,
    *,
    exclude_lock_files: bool = False,
) -> int:
    """Copy one configured runtime directory without following directory links."""

    if not source.is_dir():
        return 0
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in sorted(source.rglob("*"), key=lambda path: str(path).lower()):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_symlink():
            continue
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif (
            item.is_file()
            and not _is_sqlite_artifact(item)
            and not (
                exclude_lock_files
                and (item.name == ".memory.lock" or item.name.endswith(".lock"))
            )
        ):
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    return copied


def _file_metadata(path: Path) -> dict[str, Any]:
    return {
        "byte_size": path.stat().st_size,
        "sha256": file_digest(path),
    }


def _directory_metadata(path: Path) -> dict[str, Any]:
    entries: list[tuple[str, int, str]] = []
    total_bytes = 0
    if path.is_dir():
        for item in sorted(path.rglob("*"), key=lambda candidate: str(candidate).casefold()):
            if item.is_symlink() or not item.is_file() or _is_sqlite_artifact(item):
                continue
            relative = item.relative_to(path).as_posix()
            size = item.stat().st_size
            digest = file_digest(item)
            entries.append((relative, size, digest))
            total_bytes += size
    tree_digest = hashlib.sha256()
    for relative, size, digest in entries:
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(str(size).encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(digest.encode("ascii"))
        tree_digest.update(b"\n")
    return {
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "tree_sha256": tree_digest.hexdigest().upper(),
    }


def _source_path_state(path: Path) -> tuple[Any, ...]:
    if not path.exists():
        return ("missing",)
    if path.is_symlink():
        return ("symlink", str(path.resolve()))
    if path.is_file():
        stat = path.stat()
        return ("file", stat.st_size, stat.st_mtime_ns)
    if not path.is_dir():
        return ("other",)
    entries: list[tuple[str, int, int]] = []
    for item in sorted(path.rglob("*"), key=lambda candidate: str(candidate).casefold()):
        if item.is_symlink() or not item.is_file() or _is_sqlite_artifact(item):
            continue
        stat = item.stat()
        entries.append((item.relative_to(path).as_posix(), stat.st_size, stat.st_mtime_ns))
    return ("directory", tuple(entries))


def _sqlite_sidecars(path: Path) -> tuple[Path, ...]:
    return tuple(Path(str(path) + suffix) for suffix in SQLITE_SIDECAR_SUFFIXES)


def _assert_sqlite_sources_offline(
    paths: dict[str, Path | None],
    *,
    allow_online_backup: bool = False,
) -> None:
    for key in ("database", "question_bank"):
        source = paths.get(key)
        if source is None or not source.is_file():
            continue
        present = [sidecar for sidecar in _sqlite_sidecars(source) if sidecar.exists()]
        blocked = (
            present
            if not allow_online_backup
            else [sidecar for sidecar in present if sidecar.name.endswith("-journal")]
        )
        if blocked:
            raise RuntimeError(
                f"{key} must be offline before a recoverable snapshot; "
                f"SQLite sidecars are present: {', '.join(sidecar.name for sidecar in blocked)}"
            )


def _runtime_source_paths(
    paths: dict[str, Path | None], project_root: Path
) -> tuple[Path, ...]:
    selected = {
        path.resolve()
        for path in paths.values()
        if path is not None and path.exists()
    }
    executable = project_root / "错题本.exe"
    if executable.is_file():
        selected.add(executable.resolve())
    database = paths.get("database")
    for key in ("database", "question_bank"):
        source = paths.get(key)
        if source is None:
            continue
        for suffix in ("-wal", "-journal"):
            selected.add(Path(str(source) + suffix).resolve())
    if database is not None and database.resolve() == (project_root / "cuoti.db").resolve():
        logs = project_root / "日志"
        if logs.is_dir():
            selected.add(logs.resolve())
    return tuple(sorted(selected, key=lambda item: str(item).casefold()))


def _runtime_quiet_state(paths: tuple[Path, ...]) -> tuple[tuple[str, tuple[Any, ...]], ...]:
    return tuple((str(path), _source_path_state(path)) for path in paths)


def copy_bank_files(tex_base: Path, past_exam_dir: Path, target: Path) -> tuple[int, int]:
    """Copy approved source material without modifying either source directory."""

    tex_base = tex_base.resolve(strict=True)
    past_exam_dir = past_exam_dir.resolve(strict=True)
    if not tex_base.is_dir():
        raise NotADirectoryError(tex_base)
    if not past_exam_dir.is_dir():
        raise NotADirectoryError(past_exam_dir)

    ordinary_target = target / "普通题库"
    past_target = target / "正式真题"
    ordinary_target.mkdir()
    past_target.mkdir()

    ordinary_files = sorted(
        (path for path in tex_base.glob("*.tex") if path.is_file()),
        key=lambda path: path.name.lower(),
    )
    past_files = sorted(
        (path for path in past_exam_dir.rglob("*.tex") if path.is_file()),
        key=lambda path: str(path.relative_to(past_exam_dir)).lower(),
    )
    for source in ordinary_files:
        shutil.copy2(source, ordinary_target / source.name)
    for source in past_files:
        destination = past_target / source.relative_to(past_exam_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return len(ordinary_files), len(past_files)


def _sanitized_setting(value, key: str = ""):
    lowered = key.lower()
    if any(fragment in lowered for fragment in SENSITIVE_SETTING_FRAGMENTS):
        return "<redacted>" if value not in (None, "") else ""
    if lowered in PATH_SETTING_NAMES:
        return "<configured>" if value not in (None, "") else ""
    if isinstance(value, dict):
        return {
            child_key: _sanitized_setting(child_value, str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitized_setting(item) for item in value]
    return value


def write_sanitized_settings_summary(source: Path | None, target: Path, *,
                                     relative_path: str = "instance/settings.sanitized.json") -> bool:
    if source is None:
        return False
    if not source.is_file():
        return False
    with source.open("r", encoding="utf-8") as stream:
        settings = json.load(stream)
    if not isinstance(settings, dict):
        raise ValueError("instance/settings.json must contain a JSON object")
    destination = target / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as stream:
        json.dump(
            _sanitized_setting(settings),
            stream,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        stream.write("\n")
    return True


def backup_database(
    source_path: Path,
    destination_path: Path,
    *,
    allow_online_wal: bool = False,
) -> dict[str, Any]:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_options = "mode=ro" if allow_online_wal else "mode=ro&immutable=1"
    source_uri = f"file:{source_path.resolve().as_posix()}?{source_options}"
    source = sqlite3.connect(source_uri, uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        destination.commit()
        destination.execute("PRAGMA journal_mode=DELETE")
        quick_check = destination.execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_rows = destination.execute("PRAGMA foreign_key_check").fetchall()
        user_version = destination.execute("PRAGMA user_version").fetchone()[0]
        existing_tables = {
            row[0]
            for row in destination.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        table_counts = {
            table: int(
                destination.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in KEY_TABLES
            if table in existing_tables
        }
    finally:
        destination.close()
        source.close()
    if quick_check != "ok":
        raise RuntimeError(f"backup database quick_check failed: {quick_check}")
    if foreign_key_rows:
        raise RuntimeError(
            "backup database foreign_key_check failed: "
            f"{len(foreign_key_rows)} violation(s)"
        )
    unexpected_sidecars = [
        sidecar.name for sidecar in _sqlite_sidecars(destination_path) if sidecar.exists()
    ]
    if unexpected_sidecars:
        for sidecar in _sqlite_sidecars(destination_path):
            sidecar.unlink(missing_ok=True)
        raise RuntimeError(
            "standalone backup unexpectedly created SQLite sidecars: "
            + ", ".join(unexpected_sidecars)
        )
    return {
        "quick_check": quick_check,
        "foreign_key_check": "ok",
        "user_version": user_version,
        "table_counts": table_counts,
        "sha256": file_digest(destination_path),
    }


def _configured_paths(config: dict[str, Any], *, project_root: Path | None = None) -> dict[str, Path | None]:
    def optional(name: str) -> Path | None:
        value = str(config.get(name) or "").strip()
        return Path(value).expanduser() if value else None

    settings = optional("SETTINGS_PATH")
    database_path = optional("DATABASE")
    question_bank = optional("QUESTION_BANK_DATABASE") or optional(
        "QUESTION_BANK_INDEX"
    )
    if question_bank is None and settings is not None:
        question_bank = settings.parent / "question_bank.sqlite3"
    elif question_bank is None and database_path is not None:
        question_bank = database_path.with_name("question_bank.sqlite3")
    formal_settings = (
        project_root is not None and settings is not None
        and settings.resolve() == (project_root / "data/runtime/settings.json").resolve()
    )
    instance_root = project_root / "instance" if formal_settings else (
        settings.parent
        if settings is not None and settings.name.casefold() == "settings.json"
        else None
    )
    return {
        "database": database_path,
        "pdf_dir": optional("PDF_DIR"),
        "settings": settings,
        "question_bank": question_bank,
        "attachments": optional("ATTACHMENT_DIR")
        or optional("ATTACHMENTS_DIR"),
        "catalog": optional("CATALOG_PATH"),
        "memory": optional("AI_MEMORY_DIR"),
        "past_exam_profiles": optional("PAST_EXAM_PROFILE_DIR"),
        "semantic_formal_artifact": optional("SEMANTIC_FORMAL_ARTIFACT"),
        "job_inputs": optional("JOB_INPUT_DIR"),
        "ai_usage": optional("AI_USAGE_PATH") or (settings.with_name("ai-usage.jsonl") if settings else None),
        "d2_coverage": settings.with_name("d2-expansion-coverage.json") if settings else None,
        "profile_expansion_evidence": settings.parent / "profile_expansion_evidence" if settings else None,
        "material_understanding": (
            instance_root / "material_understanding" if instance_root else None
        ),
        "launcher_preferences": (
            instance_root / "launcher-preferences.json" if instance_root else None
        ),
    }


def capture_runtime_state(
    target: Path,
    *,
    config: dict[str, Any],
    snapshot_kind: str,
    project_root: Path,
    allow_online_sqlite_backup: bool = False,
    include_material_understanding: bool = True,
    layout: str = "legacy",
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Capture configured mutable state as one coherent restore unit."""

    if snapshot_kind not in RECOVERABLE_SNAPSHOT_KINDS:
        return None, []
    paths = _configured_paths(config, project_root=project_root)
    destinations = FORMAL_SNAPSHOT_PATHS if layout == "formal" else {}
    database_path = paths["database"]
    if database_path is None:
        raise ValueError("DATABASE is not configured")
    database_relative = destinations.get("database", "cuoti.db")
    database_target = target / database_relative
    database = backup_database(
        database_path,
        database_target,
        allow_online_wal=allow_online_sqlite_backup,
    )
    database["byte_size"] = database_target.stat().st_size
    assets: list[dict[str, Any]] = [
        {
            "asset_id": "database",
            "snapshot_path": database_relative,
            "kind": "sqlite",
            "required": True,
            "metadata": database,
        }
    ]

    question_bank = paths["question_bank"]
    if question_bank is not None and question_bank.is_file():
        destination = target / "instance" / "question_bank.sqlite3"
        try:
            metadata = backup_database(
                question_bank,
                destination,
                allow_online_wal=allow_online_sqlite_backup,
            )
        except RuntimeError:
            destination.unlink(missing_ok=True)
            assets.append(
                {
                    "asset_id": "question_bank_index",
                    "snapshot_path": "instance/question_bank.sqlite3",
                    "kind": "rebuild_required",
                    "required": False,
                    "metadata": {
                        "included": False,
                        "reason": "integrity_check_failed",
                    },
                }
            )
        else:
            metadata["byte_size"] = destination.stat().st_size
            assets.append(
                {
                    "asset_id": "question_bank_index",
                    "snapshot_path": "instance/question_bank.sqlite3",
                    "kind": "sqlite_derived",
                    "required": False,
                    "metadata": metadata,
                }
            )

    settings_path = paths["settings"]
    includes_private_settings = snapshot_kind in {"private", "private-full"}
    if includes_private_settings and settings_path is not None and settings_path.is_file():
        settings_relative = destinations.get("settings", "instance/settings.json")
        destination = target / settings_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(settings_path, destination)
        assets.append(
            {
                "asset_id": "settings",
                "snapshot_path": settings_relative,
                "kind": "private_config",
                "required": False,
                "metadata": _file_metadata(destination),
            }
        )

    for asset_id, path_key, relative in (
        ("pdfs", "pdf_dir", "pdfs"),
        ("attachments", "attachments", "instance/attachments"),
        ("ai_memory", "memory", "instance/memory"),
        ("past_exam_profiles", "past_exam_profiles", "instance/past_exam_profiles"),
        ("job_inputs", "job_inputs", "instance/job_inputs"),
        ("profile_expansion_evidence", "profile_expansion_evidence", "instance/profile_expansion_evidence"),
        (
            "material_understanding",
            "material_understanding",
            "instance/material_understanding",
        ),
    ):
        relative = destinations.get(asset_id, relative)
        if asset_id == "material_understanding" and not include_material_understanding:
            continue
        source = paths[path_key]
        if source is None or not source.is_dir():
            continue
        destination = target / relative
        copy_directory(
            source,
            destination,
            exclude_lock_files=asset_id == "ai_memory",
        )
        assets.append(
            {
                "asset_id": asset_id,
                "snapshot_path": relative,
                "kind": "directory",
                "required": False,
                "metadata": _directory_metadata(destination),
            }
        )

    for asset_id, path_key, relative in (
        ("catalog", "catalog", "instance/catalog.json"),
        ("d2_coverage", "d2_coverage", "instance/d2-expansion-coverage.json"),
        (
            "semantic_formal_artifact",
            "semantic_formal_artifact",
            "instance/semantic_index/formal_past_exams.json",
        ),
        ("ai_usage", "ai_usage", "instance/ai-usage.jsonl"),
        (
            "launcher_preferences",
            "launcher_preferences",
            "instance/launcher-preferences.json",
        ),
    ):
        relative = destinations.get(asset_id, relative)
        source = paths[path_key]
        if source is None or not source.is_file() or source.is_symlink():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        assets.append(
            {
                "asset_id": asset_id,
                "snapshot_path": relative,
                "kind": "runtime_file",
                "required": False,
                "metadata": _file_metadata(destination),
            }
        )

    executable = project_root / "错题本.exe"
    if executable.is_file():
        destination = target / "错题本.exe"
        shutil.copy2(executable, destination)
        assets.append(
            {
                "asset_id": "executable",
                "snapshot_path": "错题本.exe",
                "kind": "release_file",
                "required": False,
                "metadata": _file_metadata(destination),
            }
        )

    if database_path.resolve() == (project_root / "cuoti.db").resolve():
        logs = project_root / "日志"
        if logs.is_dir():
            destination = target / "日志"
            copy_directory(logs, destination)
            assets.append(
                {
                    "asset_id": "logs",
                    "snapshot_path": "日志",
                    "kind": "private_logs",
                    "required": False,
                    "metadata": _directory_metadata(destination),
                }
            )
    return database, assets


def _snapshot_asset_path(snapshot: Path, value: object) -> Path:
    relative = Path(str(value or ""))
    if not str(value or "").strip() or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("snapshot asset path is unsafe")
    path = (snapshot / relative).resolve()
    try:
        path.relative_to(snapshot.resolve())
    except ValueError as exc:
        raise RuntimeError("snapshot asset path escapes the snapshot") from exc
    return path


def _validate_snapshot_database(path: Path, metadata: dict[str, Any]) -> dict[str, Any]:
    if _file_metadata(path) != {
        "byte_size": metadata.get("byte_size"),
        "sha256": metadata.get("sha256"),
    }:
        raise RuntimeError("snapshot database file metadata mismatch")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        expected_counts = metadata.get("table_counts") or {}
        actual_counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in expected_counts
            if table in existing_tables
        }
    finally:
        connection.close()
    if quick_check != "ok" or foreign_key_rows:
        raise RuntimeError("snapshot database integrity check failed")
    if user_version != metadata.get("user_version"):
        raise RuntimeError("snapshot database schema metadata mismatch")
    if actual_counts != expected_counts:
        raise RuntimeError("snapshot database table-count metadata mismatch")
    return {
        "quick_check": "ok",
        "foreign_key_check": "ok",
        "user_version": user_version,
    }


def _validate_attachment_links(snapshot: Path, assets: list[dict[str, Any]]) -> None:
    by_id = {str(asset.get("asset_id") or ""): asset for asset in assets}
    database_asset = by_id.get("database")
    if database_asset is None:
        return
    database_path = _snapshot_asset_path(snapshot, database_asset.get("snapshot_path"))
    attachment_asset = by_id.get("attachments")
    attachment_root = (
        _snapshot_asset_path(snapshot, attachment_asset.get("snapshot_path"))
        if attachment_asset is not None
        else None
    )
    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    connection.row_factory = sqlite3.Row
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'"
        ).fetchone()
        if not table_exists:
            return
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(attachments)")
        }
        if "object_key" not in columns:
            raise RuntimeError("snapshot attachment table lacks object_key")
        selected = ["object_key"]
        selected.extend(
            name for name in ("byte_size", "sha256", "status") if name in columns
        )
        rows = connection.execute(
            f"SELECT {','.join(selected)} FROM attachments"
            + (" WHERE status='published'" if "status" in columns else "")
        ).fetchall()
    finally:
        connection.close()
    if rows and attachment_root is None:
        raise RuntimeError("snapshot database references attachments but no attachment asset exists")
    for row in rows:
        key = str(row["object_key"] or "")
        if not key or Path(key).name != key or key in {".", ".."}:
            raise RuntimeError("snapshot database contains an unsafe attachment key")
        path = attachment_root / key
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"snapshot attachment is missing: {key}")
        if "byte_size" in row.keys() and path.stat().st_size != int(row["byte_size"]):
            raise RuntimeError(f"snapshot attachment size mismatch: {key}")
        if "sha256" in row.keys() and file_digest(path).casefold() != str(row["sha256"]).casefold():
            raise RuntimeError(f"snapshot attachment hash mismatch: {key}")


def validate_captured_assets(
    snapshot: Path, assets: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Validate asset records against files and cross-resource database links."""

    snapshot = snapshot.resolve(strict=True)
    identifiers: set[str] = set()
    results: dict[str, dict[str, Any]] = {}
    for path in snapshot.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("snapshot must not contain symbolic links")
        if path.is_file() and path.name.casefold().endswith(SQLITE_SIDECAR_SUFFIXES):
            raise RuntimeError(f"snapshot contains a forbidden SQLite sidecar: {path.name}")
    for asset in assets:
        asset_id = str(asset.get("asset_id") or "")
        if not asset_id or asset_id in identifiers:
            raise RuntimeError("snapshot asset identifiers must be unique")
        identifiers.add(asset_id)
        path = _snapshot_asset_path(snapshot, asset.get("snapshot_path"))
        if asset.get("required") and not path.exists():
            raise RuntimeError(f"snapshot required asset is missing: {asset_id}")
        if not path.exists():
            continue
        kind = str(asset.get("kind") or "")
        metadata = asset.get("metadata")
        if not isinstance(metadata, dict):
            raise RuntimeError(f"snapshot asset metadata is missing: {asset_id}")
        if kind.startswith("sqlite"):
            results[asset_id] = _validate_snapshot_database(path, metadata)
        elif kind in {"directory", "private_logs"}:
            actual = _directory_metadata(path)
            if actual != metadata:
                raise RuntimeError(f"snapshot directory metadata mismatch: {asset_id}")
            results[asset_id] = actual
        else:
            actual = _file_metadata(path)
            if actual != metadata:
                raise RuntimeError(f"snapshot file metadata mismatch: {asset_id}")
            results[asset_id] = actual
    _validate_attachment_links(snapshot, assets)
    return results


def write_description(
    target: Path,
    created_at: datetime,
    database: dict[str, Any] | None,
    executable_hash: str | None,
    label: str,
    bank_counts: tuple[int, int] | None,
    settings_summary_written: bool,
    snapshot_kind: str,
    assets: list[dict[str, Any]],
    settings_summary_path: str = "instance/settings.sanitized.json",
) -> None:
    bank_description = (
        f"；普通题库顶层 TeX {bank_counts[0]} 个、正式真题 TeX "
        f"{bank_counts[1]} 个"
        if bank_counts is not None
        else ""
    )
    settings_description = (
        f"已生成 `{settings_summary_path}`（只供参考，不能直接作为运行设置；恢复后需重新配置 AI）"
        if settings_summary_written
        else "未发现本地真实设置文件"
    )
    counts = database.get("table_counts", {}) if database is not None else {}
    mistake_count = counts.get("mistakes", 0)
    export_count = counts.get("mistake_exports", 0)
    asset_names = "、".join(str(asset["asset_id"]) for asset in assets)
    settings_line = (
        "不包含本机设置或设置摘要"
        if snapshot_kind == "shareable"
        else "已包含本机 settings.json；Windows keyring 密钥不会导出"
        if snapshot_kind in {"private", "private-full"}
        else settings_description
    )
    if snapshot_kind == "shareable":
        content_line = "仅包含受控源码、测试、工具和构建资料；不含数据库、instance、运行日志、运行产物或本机设置"
        restore_line = "不可恢复；仅用于共享和审阅源码"
        database_line = "不适用（共享包不包含数据库）"
        executable_line = "不包含正式 EXE"
    else:
        content_line = (
            "应用源码、测试、工具、构建配置、启动文件，以及通过受控资产清单捕获的数据库、"
            f"运行文件和正式 EXE{bank_description}"
        )
        restore_line = asset_names
        database_line = (
            f"`PRAGMA quick_check = {database['quick_check']}`，"
            f"`PRAGMA foreign_key_check = {database['foreign_key_check']}`，"
            f"user_version = {database['user_version']}，错题 {mistake_count} 道，"
            f"导出记录 {export_count} 条"
        )
        executable_line = f"`{executable_hash}`"
    content = f"""# 备份说明

- 创建时间：{created_at:%Y-%m-%d %H:%M:%S}（Asia/Shanghai）
- 标签：{label}
- 类型：{snapshot_kind}（`private-full` 为本机完整回退包，`private-no-secrets` 为默认无设置回退包，`shareable` 为可分享脱敏包）
- 内容：{content_line}
- 恢复单元：{restore_line}
- 设置：{settings_line}
- 排除：已有备份、构建缓存、项目内 `错题库/` 与组件材库
- 数据库校验：{database_line}
- 当前根目录 EXE SHA-256：{executable_line}
- 完成规则：`SHA256_MANIFEST.csv` 生成并验证后，本目录冻结，不再修改
"""
    (target / "备份说明.md").write_text(content, encoding="utf-8")


def write_manifest(target: Path) -> list[dict[str, str | int]]:
    rows = []
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.name == "SHA256_MANIFEST.csv":
            continue
        rows.append(
            {
                "RelativePath": path.relative_to(target).as_posix(),
                "Length": path.stat().st_size,
                "SHA256": file_digest(path),
            }
        )
    with (target / "SHA256_MANIFEST.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.DictWriter(
            stream, fieldnames=("RelativePath", "Length", "SHA256")
        )
        writer.writeheader()
        writer.writerows(rows)
    return rows


def verify_manifest(target: Path) -> tuple[int, int]:
    mismatches = 0
    with (target / "SHA256_MANIFEST.csv").open(
        "r", encoding="utf-8", newline=""
    ) as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        rendered = str(row["RelativePath"]).replace("\\", "/")
        pure = PurePosixPath(rendered)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            mismatches += 1
            continue
        path = target.joinpath(*pure.parts)
        if (
            not path.is_file()
            or path.stat().st_size != int(row["Length"])
            or file_digest(path) != row["SHA256"]
        ):
            mismatches += 1
    return len(rows), mismatches


def _validate_label(label: str) -> str:
    label = str(label).strip()
    if not label:
        raise ValueError("backup label must not be empty")
    if label in {".", ".."} or any(character in label for character in '<>:"/\\|?*'):
        raise ValueError("backup label contains invalid path characters")
    return label


def create_backup(
    root: Path,
    *,
    label: str = "snapshot",
    include_bank: bool = False,
    tex_base: Path | None = None,
    past_exam_dir: Path | None = None,
    snapshot_kind: str = "private-no-secrets",
    config: dict[str, Any] | None = None,
    backup_parent: Path | None = None,
    allow_online_sqlite_backup: bool = False,
    include_material_understanding: bool = True,
    layout: str | None = None,
) -> Path:
    root = root.resolve(strict=True)
    # Existing explicit-config callers retain their legacy restore layout.
    layout = layout or ("legacy" if config is not None else "formal")
    if layout not in {"formal", "legacy"}:
        raise ValueError("layout must be formal or legacy")
    label = _validate_label(label)
    if snapshot_kind not in {"private", "private-full", "private-no-secrets", "shareable"}:
        raise ValueError("snapshot_kind must be private-full, private-no-secrets, or shareable")
    if include_bank and (tex_base is None or past_exam_dir is None):
        raise ValueError(
            "--include-bank requires both --tex-base and --past-exam-dir"
        )
    if snapshot_kind == "shareable" and include_bank:
        raise ValueError("shareable snapshots cannot include user question-bank material")
    created_at = datetime.now(ZoneInfo("Asia/Shanghai"))
    if backup_parent is not None:
        backup_parent = backup_parent.expanduser().resolve()
        if backup_parent == root or root in backup_parent.parents:
            raise ValueError("explicit backup_parent must be outside the project root")
    else:
        backup_parent = root / "备份"
    backup_parent.mkdir(parents=True, exist_ok=True)
    suffix = created_at.strftime("%Y%m%d_%H%M%S")
    final = backup_parent / f"{label}_{suffix}"
    staging = backup_parent / f".tmp_{label}_{suffix}_{os.getpid()}"
    if final.exists() or staging.exists():
        raise FileExistsError(final)

    resources = ExitStack()
    try:
        staging.mkdir()
        recoverable = snapshot_kind in RECOVERABLE_SNAPSHOT_KINDS
        if config is not None:
            runtime_config = dict(config)
        elif not recoverable:
            runtime_config = {}
        elif layout == "formal":
            runtime_config = build_config(build_formal_runtime_overrides(root))
        else:
            runtime_config = build_config({
                "DATABASE": str(root / "cuoti.db"),
                "SETTINGS_PATH": str(root / "instance/settings.json"),
                "PDF_DIR": str(root / "pdfs"),
                "QUESTION_BANK_DATABASE": str(root / "instance/question_bank.sqlite3"),
                "SEMANTIC_FORMAL_ARTIFACT": str(root / "instance/semantic_index/formal_past_exams.json"),
            })
        runtime_paths = _configured_paths(runtime_config, project_root=root)
        source_copy_exclusions = {
            path for path in runtime_paths.values() if path is not None
        }
        if not include_material_understanding:
            runtime_paths["material_understanding"] = None
        runtime_sources: tuple[Path, ...] = ()
        runtime_state_before: tuple[tuple[str, tuple[Any, ...]], ...] = ()
        if recoverable:
            resources.enter_context(database_service_guard(runtime_paths["database"]))
            _assert_sqlite_sources_offline(
                runtime_paths,
                allow_online_backup=allow_online_sqlite_backup,
            )
            runtime_sources = _runtime_source_paths(runtime_paths, root)
            if include_bank:
                runtime_sources = tuple(sorted(
                    {
                        *runtime_sources,
                        tex_base.resolve(strict=True),
                        past_exam_dir.resolve(strict=True),
                    },
                    key=lambda item: str(item).casefold(),
                ))
            runtime_state_before = _runtime_quiet_state(runtime_sources)
        copy_project_files(
            root,
            staging,
            excluded_paths={
                *source_copy_exclusions,
            },
            source_only=not recoverable,
        )
        database, assets = capture_runtime_state(
            staging,
            config=runtime_config,
            snapshot_kind=snapshot_kind,
            project_root=root,
            allow_online_sqlite_backup=allow_online_sqlite_backup,
            include_material_understanding=include_material_understanding,
            layout=layout,
        )
        executable = root / "错题本.exe"
        executable_hash = file_digest(executable) if recoverable and executable.is_file() else None
        settings_summary_path = "data/runtime/settings.sanitized.json" if layout == "formal" else "instance/settings.sanitized.json"
        settings_summary_written = (
            write_sanitized_settings_summary(runtime_paths["settings"], staging, relative_path=settings_summary_path)
            if snapshot_kind == "private-no-secrets"
            else False
        )
        bank_counts = (
            copy_bank_files(tex_base, past_exam_dir, staging)
            if include_bank and tex_base is not None and past_exam_dir is not None
            else None
        )
        if recoverable:
            runtime_state_after = _runtime_quiet_state(runtime_sources)
            if runtime_state_after != runtime_state_before:
                before_by_path = dict(runtime_state_before)
                after_by_path = dict(runtime_state_after)
                changed_paths = sorted(
                    path
                    for path in {*before_by_path, *after_by_path}
                    if before_by_path.get(path) != after_by_path.get(path)
                )
                raise RuntimeError(
                    "recoverable snapshot sources changed during capture; stop the app "
                    "and retry; changed paths: " + ", ".join(changed_paths)
                )
        if recoverable:
            validate_captured_assets(staging, assets)
        write_description(
            staging,
            created_at,
            database,
            executable_hash,
            label,
            bank_counts,
            settings_summary_written,
            snapshot_kind,
            assets,
            settings_summary_path,
        )
        (staging / "SNAPSHOT.json").write_text(
            json.dumps(
                {
                    "schema_version": SNAPSHOT_SCHEMA_VERSION,
                    "runtime_layout": layout,
                    "created_at": created_at.isoformat(),
                    "label": label,
                    "snapshot_kind": snapshot_kind,
                    "restorable": recoverable,
                    "source_scope": "source-only" if not recoverable else "source-and-runtime",
                    "excluded_optional_assets": (
                        [] if include_material_understanding else ["material_understanding"]
                    ),
                    "assets": assets,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        write_manifest(staging)
        entries, mismatches = verify_manifest(staging)
        if mismatches:
            raise RuntimeError(f"manifest verification failed: {mismatches}")
        staging.replace(final)
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    finally:
        resources.close()

    entries, mismatches = verify_manifest(final)
    print(f"backup={final}")
    print(f"manifest_entries={entries}")
    print(f"mismatches={mismatches}")
    print(f"restorable={snapshot_kind in RECOVERABLE_SNAPSHOT_KINDS}")
    if database is not None:
        print(f"quick_check={database['quick_check']}")
        print(f"user_version={database['user_version']}")
        print(f"foreign_key_check={database['foreign_key_check']}")
        print(f"mistakes={database['table_counts'].get('mistakes', 0)}")
        print(f"exports={database['table_counts'].get('mistake_exports', 0)}")
    if executable_hash is not None:
        print(f"exe_sha256={executable_hash}")
    return final


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--layout", choices=("formal", "legacy"), default="formal",
                        help="formal uses data/cuoti.db and data/runtime; legacy is explicit opt-in")
    parser.add_argument(
        "--label",
        default="snapshot",
        help="backup directory label (timestamp is appended automatically)",
    )
    parser.add_argument(
        "--include-bank",
        action="store_true",
        help="also copy top-level ordinary-bank TeX and formal past-exam TeX",
    )
    parser.add_argument("--tex-base", type=Path)
    parser.add_argument("--past-exam-dir", type=Path)
    parser.add_argument(
        "--semantic-formal-artifact",
        type=Path,
        help="explicitly select the formal semantic candidate artifact",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="use an explicitly selected database (for example an isolated acceptance database)",
    )
    parser.add_argument(
        "--backup-parent",
        type=Path,
        help="write the frozen snapshot below this directory instead of <root>/备份",
    )
    parser.add_argument(
        "--allow-online-sqlite-backup",
        action="store_true",
        help=(
            "allow SQLite WAL/SHM sidecars after the application and all background "
            "writers have been stopped; database bytes still use SQLite Online Backup"
        ),
    )
    parser.add_argument(
        "--exclude-material-understanding",
        action="store_true",
        help=(
            "exclude the rebuildable material-understanding cache; the exclusion is "
            "recorded in SNAPSHOT.json"
        ),
    )
    parser.add_argument(
        "--kind",
        choices=("private-no-secrets", "private-full", "private", "shareable"),
        default="private-no-secrets",
        help=(
            "private-no-secrets is the default; private-full includes local settings; "
            "private is a legacy alias; shareable is source-only and non-restorable"
        ),
    )
    args = parser.parse_args()
    runtime_overrides: dict[str, Any] = {}
    if args.database is not None:
        database = args.database.resolve(strict=True)
        runtime_overrides.update(
            {
                "TESTING": True,
                "DATABASE": str(database),
                "SETTINGS_PATH": str(database.with_name("isolated-settings.json")),
                "PDF_DIR": str(database.with_name("isolated-pdfs")),
                "ATTACHMENT_DIR": str(database.with_name("isolated-attachments")),
                "CATALOG_PATH": str(database.with_name("isolated-catalog.json")),
                "PAST_EXAM_PROFILE_DIR": str(
                    database.with_name("isolated-past-exam-profiles")
                ),
                "JOB_WORKER_ENABLED": False,
            }
        )
    if args.semantic_formal_artifact is not None:
        runtime_overrides["SEMANTIC_FORMAL_ARTIFACT"] = str(
            args.semantic_formal_artifact.resolve(strict=True)
        )
    runtime_config = build_config(runtime_overrides) if runtime_overrides else None
    create_backup(
        args.root,
        label=args.label,
        include_bank=args.include_bank,
        tex_base=args.tex_base,
        past_exam_dir=args.past_exam_dir,
        snapshot_kind=args.kind,
        config=runtime_config,
        backup_parent=args.backup_parent,
        allow_online_sqlite_backup=args.allow_online_sqlite_backup,
        include_material_understanding=not args.exclude_material_understanding,
        layout=args.layout,
    )


if __name__ == "__main__":
    main()
