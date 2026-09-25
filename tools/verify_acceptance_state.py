"""Capture and verify production state around an isolated acceptance run.

This helper is deliberately independent from the web application.  It never
copies protected data and stores only aggregate SHA-256 fingerprints, counts,
and byte sizes; file names and configured absolute paths are not written to
the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Target:
    label: str
    kind: str
    path: Path


def _file_digest(path: Path) -> tuple[str, int]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (
        after.st_size,
        after.st_mtime_ns,
    ):
        raise RuntimeError(f"target changed while hashing: {path}")
    return digest.hexdigest().upper(), after.st_size


def _directory_files(root: Path) -> list[tuple[str, Path, int, int]]:
    files: list[tuple[str, Path, int, int]] = []
    for current, directories, names in os.walk(root, followlinks=False):
        current_path = Path(current)
        directories[:] = sorted(directories, key=str.casefold)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(f"symbolic links are not accepted: {path}")
        for name in sorted(names, key=str.casefold):
            path = current_path / name
            if path.is_symlink():
                raise RuntimeError(f"symbolic links are not accepted: {path}")
            stat = path.stat()
            files.append(
                (
                    path.relative_to(root).as_posix(),
                    path,
                    stat.st_size,
                    stat.st_mtime_ns,
                )
            )
    files.sort(key=lambda item: item[0].casefold())
    return files


def _directory_digest(root: Path) -> dict[str, int | str]:
    before = _directory_files(root)
    combined = hashlib.sha256()
    total_bytes = 0
    for relative, path, _size, _mtime_ns in before:
        digest, size = _file_digest(path)
        total_bytes += size
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(str(size).encode("ascii"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\n")
    after = _directory_files(root)
    before_metadata = [(item[0], item[2], item[3]) for item in before]
    after_metadata = [(item[0], item[2], item[3]) for item in after]
    if before_metadata != after_metadata:
        raise RuntimeError(f"target changed while hashing: {root}")
    return {
        "sha256": combined.hexdigest().upper(),
        "file_count": len(before),
        "total_bytes": total_bytes,
    }


def fingerprint(target: Target) -> dict[str, int | str]:
    path = target.path.resolve(strict=True)
    if target.kind == "file":
        if not path.is_file():
            raise ValueError(f"{target.label} must be a file")
        digest, size = _file_digest(path)
        return {"kind": "file", "sha256": digest, "total_bytes": size}
    if not path.is_dir():
        raise ValueError(f"{target.label} must be a directory")
    return {"kind": "directory", **_directory_digest(path)}


def capture(targets: list[Target]) -> dict:
    labels = [target.label for target in targets]
    if len(labels) != len(set(labels)):
        raise ValueError("target labels must be unique")
    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "SHA-256",
        "targets": {
            target.label: fingerprint(target)
            for target in targets
        },
    }


def write_manifest(path: Path, manifest: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        Path(temporary_name).replace(path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def read_manifest(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported acceptance-state manifest version")
    if not isinstance(manifest.get("targets"), dict):
        raise ValueError("invalid acceptance-state manifest")
    return manifest


def differences(expected: dict, actual: dict) -> list[str]:
    expected_targets = expected["targets"]
    actual_targets = actual["targets"]
    messages = []
    for label in sorted(set(expected_targets) | set(actual_targets)):
        if label not in expected_targets:
            messages.append(f"unexpected target: {label}")
        elif label not in actual_targets:
            messages.append(f"missing target: {label}")
        elif expected_targets[label] != actual_targets[label]:
            messages.append(f"changed target: {label}")
    return messages


def _targets(args: argparse.Namespace) -> list[Target]:
    targets = [
        Target("database", "file", args.database),
        Target("question_bank", "file", args.question_bank),
        Target("pdf_artifacts", "directory", args.pdf_dir),
        Target("past_exams", "directory", args.past_exam_dir),
        Target("ai_memory", "directory", args.memory_dir),
    ]
    if args.ordinary_bank_dir is not None:
        targets.append(
            Target("ordinary_bank", "directory", args.ordinary_bank_dir)
        )
    return targets


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture or verify immutable production state."
    )
    parser.add_argument("action", choices=("capture", "verify"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--question-bank", type=Path, required=True)
    parser.add_argument("--pdf-dir", type=Path, required=True)
    parser.add_argument("--past-exam-dir", type=Path, required=True)
    parser.add_argument("--memory-dir", type=Path, required=True)
    parser.add_argument("--ordinary-bank-dir", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    actual = capture(_targets(args))
    if args.action == "capture":
        write_manifest(args.manifest, actual)
        print(
            f"captured={len(actual['targets'])} "
            f"manifest={args.manifest.resolve()}"
        )
        return 0
    expected = read_manifest(args.manifest)
    changed = differences(expected, actual)
    if changed:
        for message in changed:
            print(message)
        return 1
    print(f"verified={len(actual['targets'])} unchanged=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
