"""Capture a deterministic, privacy-bounded source provenance receipt.

The receipt contains only current, Git-visible source files. Runtime data, ignored
artifacts, test/diagnostic scratch trees, and frozen completed plans are
deliberately outside the snapshot.
Dirty tracked content is also emitted as a binary Git patch, while the source
archive preserves untracked file bodies needed for reconstruction. Committed
source is referenced by Git commit, rather than copied into each candidate ZIP.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence


SCHEMA_VERSION = 2
CHUNK_SIZE = 1024 * 1024
FROZEN_PLAN_PREFIX = "internal-archive/"
POLICY_EXCLUDED_PREFIXES = (
    FROZEN_PLAN_PREFIX, "internal-archive/completed/", "internal-archive/backups/", "archive/",
    "备份/", "build/", "dist/", "output/", "temp/", "tmp/", "data/", "instance/",
    "Claude outputs/",
)
POLICY_EXCLUDED_UNTRACKED_TOP_LEVEL_PREFIXES = (
    ".pytest-",
    ".pytest_",
    ".stage11",
    ".tmp_pytest",
)
VERSION_PACKAGES = (
    "Flask",
    "Pillow",
    "PyInstaller",
    "PyMuPDF",
    "customtkinter",
    "keyring",
    "pystray",
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest().upper()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary_name).replace(path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _run(
    args: Sequence[str], *, root: Path, allow_failure: bool = False
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        list(args),
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode and not allow_failure:
        summary = completed.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"command failed ({completed.returncode}): {summary}")
    return completed


def _git_output(root: Path, *args: str, allow_failure: bool = False) -> bytes:
    return _run(("git", *args), root=root, allow_failure=allow_failure).stdout


def _decode_zero_paths(payload: bytes) -> list[str]:
    paths = []
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        paths.append(raw.decode("utf-8", errors="surrogateescape").replace("\\", "/"))
    return paths


def _normalized_relative(value: str) -> str:
    rendered = value.replace("\\", "/")
    pure = PurePosixPath(rendered)
    if pure.is_absolute() or not pure.parts or ".." in pure.parts:
        raise ValueError(f"unsafe source path: {value!r}")
    return pure.as_posix()


def is_policy_excluded(relative: str, *, untracked: bool = True) -> bool:
    normalized = _normalized_relative(relative)
    top_level = normalized.split("/", 1)[0]
    return (
        any(normalized.startswith(prefix) for prefix in POLICY_EXCLUDED_PREFIXES)
        or untracked and "/" in normalized and any(
            top_level.startswith(prefix)
            for prefix in POLICY_EXCLUDED_UNTRACKED_TOP_LEVEL_PREFIXES
        )
    )


def discover_git_paths(
    root: Path,
) -> tuple[list[str], list[str], list[str], list[str]]:
    tracked = _decode_zero_paths(_git_output(root, "ls-files", "--cached", "-z"))
    untracked = _decode_zero_paths(
        _git_output(root, "ls-files", "--others", "--exclude-standard", "-z")
    )
    def normalize(values, *, untracked: bool) -> list[str]:
        return sorted(
        {
            _normalized_relative(value)
            for value in values
            if not is_policy_excluded(value, untracked=untracked)
        },
        key=str.casefold,
    )
    tracked_paths = normalize(tracked, untracked=False)
    untracked_paths = normalize(untracked, untracked=True)
    excluded_paths = sorted(
        {
            _normalized_relative(value)
            for value in (*tracked, *untracked)
            if is_policy_excluded(
                value,
                untracked=value in untracked,
            )
        },
        key=str.casefold,
    )
    visible_paths = sorted(
        {*tracked_paths, *untracked_paths},
        key=str.casefold,
    )
    return visible_paths, tracked_paths, untracked_paths, excluded_paths


def _resolved_source_path(root: Path, relative: str) -> Path:
    normalized = _normalized_relative(relative)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    if candidate.is_symlink():
        raise RuntimeError(f"symbolic links are not accepted: {normalized}")
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"source path escapes project root: {normalized}") from exc
    if not resolved.is_file():
        raise RuntimeError(f"source entry is not a regular file: {normalized}")
    return resolved


def _read_stable(path: Path) -> bytes:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"source changed while hashing: {path}")
    return payload


def collect_file_entries(
    root: Path, source_paths: Iterable[str]
) -> tuple[list[dict[str, object]], str, int]:
    entries: list[dict[str, object]] = []
    combined = hashlib.sha256()
    total_bytes = 0
    for relative in sorted({_normalized_relative(item) for item in source_paths}, key=str.casefold):
        path = _resolved_source_path(root, relative)
        payload = _read_stable(path)
        digest = _sha256_bytes(payload)
        size = len(payload)
        entries.append({"path": relative, "sha256": digest, "size": size})
        total_bytes += size
        combined.update(relative.encode("utf-8", errors="surrogateescape"))
        combined.update(b"\0")
        combined.update(str(size).encode("ascii"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    return entries, combined.hexdigest().upper(), total_bytes


def write_source_archive(root: Path, entries: Sequence[dict[str, object]], path: Path) -> dict:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            for entry in entries:
                relative = str(entry["path"])
                payload = _read_stable(_resolved_source_path(root, relative))
                if len(payload) != int(entry["size"]) or _sha256_bytes(payload) != entry["sha256"]:
                    raise RuntimeError(f"source changed while archiving: {relative}")
                info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        payload = temporary.read_bytes()
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return {"sha256": _sha256_bytes(payload), "total_bytes": len(payload)}


def _command_version(root: Path, *args: str) -> str | None:
    executable = shutil.which(args[0])
    if executable is None:
        return None
    try:
        completed = _run((executable, *args[1:]), root=root, allow_failure=True)
    except OSError:
        return None
    if completed.returncode:
        return None
    return completed.stdout.decode("utf-8", errors="replace").strip()[:200] or None


def collect_versions(root: Path) -> dict[str, object]:
    packages: dict[str, str | None] = {}
    for name in VERSION_PACKAGES:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    xelatex = shutil.which("xelatex")
    return {
        "python": sys.version.splitlines()[0],
        "sqlite": sqlite3.sqlite_version,
        "packages": packages,
        "git": _command_version(root, "git", "--version"),
        "node": _command_version(root, "node", "--version"),
        "npm": _command_version(root, "npm", "--version"),
        "powershell": _command_version(root, "pwsh", "--version")
        or _command_version(
            root,
            "powershell",
            "-NoProfile",
            "-Command",
            "$PSVersionTable.PSVersion.ToString()",
        ),
        "xelatex": _command_version(root, xelatex, "--version") if xelatex else None,
    }


def build_manifest(
    *,
    root: Path,
    source_paths: Sequence[str],
    tracked_paths: Sequence[str],
    untracked_paths: Sequence[str],
    patch_bytes: bytes,
    status_bytes: bytes,
    git_head: str,
    git_branch: str,
    versions: dict[str, object],
    archive_output: Path,
    excluded_paths: Sequence[str] = (),
    archive_mode: str = "full",
) -> dict[str, object]:
    if archive_mode not in {"full", "git-delta"}:
        raise ValueError("archive_mode must be full or git-delta")
    root = root.resolve(strict=True)
    existing_paths = [
        relative
        for relative in source_paths
        if root.joinpath(*PurePosixPath(relative).parts).is_file()
    ]
    missing_tracked = sorted(
        set(tracked_paths) - set(existing_paths), key=str.casefold
    )
    entries, tree_hash, total_bytes = collect_file_entries(root, existing_paths)
    untracked = set(untracked_paths)
    archive_entries = entries if archive_mode == "full" else [
        entry for entry in entries if entry["path"] in untracked
    ]
    archive = write_source_archive(root, archive_entries, archive_output)
    archive.update(mode=archive_mode, paths=[entry["path"] for entry in archive_entries])
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "SHA-256",
        "git": {
            "head": git_head,
            "branch": git_branch,
            "worktree_clean": not bool(status_bytes),
            "status_sha256": _sha256_bytes(status_bytes),
            "tracked_patch_sha256": _sha256_bytes(patch_bytes),
            "tracked_patch_bytes": len(patch_bytes),
        },
        "source_tree": {
            "sha256": tree_hash,
            "file_count": len(entries),
            "total_bytes": total_bytes,
            "files": entries,
            "missing_tracked_paths": missing_tracked,
            "untracked_paths": sorted(set(untracked_paths), key=str.casefold),
            "archive": archive,
        },
        "policy_excluded_prefixes": list(POLICY_EXCLUDED_PREFIXES),
        "policy_excluded_untracked_top_level_prefixes": list(
            POLICY_EXCLUDED_UNTRACKED_TOP_LEVEL_PREFIXES
        ),
        "policy_excluded_paths": sorted(set(excluded_paths), key=str.casefold),
        "versions": versions,
    }


def capture(root: Path, *, patch_output: Path, archive_output: Path,
            archive_mode: str = "git-delta") -> dict[str, object]:
    root = root.resolve(strict=True)
    source_paths, tracked_paths, untracked_paths, excluded_paths = discover_git_paths(root)
    patch = _git_output(
        root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "HEAD",
        "--",
        ".",
        *(f":(exclude){prefix}**" for prefix in POLICY_EXCLUDED_PREFIXES),
    )
    status = _git_output(root, "status", "--porcelain=v1", "-z")
    head = _git_output(root, "rev-parse", "HEAD").decode("ascii").strip()
    branch = _git_output(root, "branch", "--show-current").decode("utf-8").strip()
    _atomic_write_bytes(patch_output, patch)
    return build_manifest(
        root=root,
        source_paths=source_paths,
        tracked_paths=tracked_paths,
        untracked_paths=untracked_paths,
        patch_bytes=patch,
        status_bytes=status,
        git_head=head,
        git_branch=branch,
        versions=collect_versions(root),
        archive_output=archive_output,
        excluded_paths=excluded_paths,
        archive_mode=archive_mode,
    )


def write_manifest(path: Path, manifest: dict[str, object]) -> None:
    payload = (_canonical_json(manifest) + "\n").encode("utf-8")
    _atomic_write_bytes(path, payload)


def validate_source_reference(root: Path, manifest: dict, archive: Path) -> None:
    """Validate the Git dependency of a delta receipt without changing checkout."""
    version = manifest.get("schema_version")
    mode = manifest["source_tree"]["archive"].get("mode", "full")
    if version not in {1, SCHEMA_VERSION} or mode not in {"full", "git-delta"}:
        raise ValueError("unsupported source provenance format")
    if mode == "full":
        return
    if version != SCHEMA_VERSION:
        raise ValueError("git-delta requires source provenance schema 2")
    head = manifest["git"].get("head", "")
    if len(head) not in {40, 64} or any(ch not in "0123456789abcdefABCDEF" for ch in head):
        raise ValueError("git-delta requires a full Git commit ID")
    if _git_output(root, "cat-file", "-t", head).strip() != b"commit":
        raise ValueError("git-delta base is not a retained Git commit")
    expected = set(manifest["source_tree"]["untracked_paths"])
    declared = manifest["source_tree"]["archive"].get("paths")
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.namelist()
    if declared is None or set(declared) != expected or set(members) != expected or len(members) != len(expected):
        raise ValueError("git-delta ZIP must contain exactly the declared untracked files")
    for name in members:
        _normalized_relative(name)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture deterministic source provenance for a launcher candidate."
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--patch-output", type=Path, required=True)
    parser.add_argument("--archive-output", type=Path, required=True)
    parser.add_argument("--archive-mode", choices=("git-delta", "full"), default="git-delta",
                        help="git-delta requires the recorded Git commit plus patch and untracked ZIP; full is a standalone source export")
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = capture(
        args.root,
        patch_output=args.patch_output,
        archive_output=args.archive_output,
        archive_mode=args.archive_mode,
    )
    write_manifest(args.manifest, manifest)
    manifest_hash = _sha256_bytes(args.manifest.resolve().read_bytes())
    print(
        f"source_files={manifest['source_tree']['file_count']} "
        f"worktree_clean={str(manifest['git']['worktree_clean']).lower()} "
        f"manifest_sha256={manifest_hash}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
