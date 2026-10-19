"""Keep pre-change copies of explicitly named files in one local ZIP.

This is a task rollback aid, not a database snapshot or a disaster-recovery pack.
Revisiting a path preserves its first captured state, including initial absence.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath


MANIFEST = "CHANGE_BACKUP.json"
FORMAT = "cuoti-change-backup-v1"
RESTORE = """This ZIP contains only the explicitly listed pre-change files.
Read CHANGE_BACKUP.json before restoring. Entries marked present=false did not
exist when first captured; do not delete later user work without reviewing it.
Restore selected files from files/<relative path> only within the intended task.
An old EXE must be compatible with the current database schema. Stop the owning
service before replacing its files. This pack contains no consistent database
snapshot and cannot be passed to restore_completion_backup.py or substituted
for a complete disaster-recovery backup. Existing Git history remains required.
"""


def _file_path(root: Path, value: str) -> tuple[str, Path]:
    relative = PurePosixPath(value.replace("\\", "/"))
    if (not relative.parts or relative.is_absolute() or ".." in relative.parts
            or PureWindowsPath(value).drive or ":" in str(relative)
            or relative.parts[0].casefold() == ".git"):
        raise ValueError(f"expected a project-relative file path: {value}")
    path = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"backup path cannot traverse a link: {value}")
    if path.exists() and not path.is_file():
        raise ValueError(f"name individual files, not directories: {value}")
    name = path.name.casefold()
    if path.suffix.casefold() in {".db", ".sqlite", ".sqlite3"} or name.endswith(("-wal", "-shm", "-journal")):
        raise ValueError("SQLite files require a consistent database backup; raw copies are not supported")
    return relative.as_posix(), path


def create_change_backup(root: Path, output: Path, files: list[str], *, note: str = "") -> dict:
    root = root.resolve(strict=True)
    output = output.absolute()
    selected = dict(_file_path(root, value) for value in files)
    if not selected:
        raise ValueError("at least one explicit file is required")
    if any(path.resolve() == output.resolve() for path in selected.values()):
        raise ValueError("a backup cannot contain itself")

    with ExitStack() as resources:
        old = resources.enter_context(zipfile.ZipFile(output)) if output.exists() else None
        if old is not None:
            manifest = json.loads(old.read(MANIFEST))
            if manifest.get("format") != FORMAT or manifest.get("root") != str(root):
                raise ValueError("existing archive belongs to a different backup format or project")
            entries = manifest["files"]
            for relative, entry in entries.items():
                _file_path(root, relative)
                if entry["present"]:
                    info = old.getinfo("files/" + relative)
                    if info.file_size != entry["size"]:
                        raise ValueError(f"existing backup entry size differs: {relative}")
        else:
            git = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True)
            manifest = {"format": FORMAT, "root": str(root),
                        "base_commit": git.stdout.strip() if git.returncode == 0 else None,
                        "note": note, "files": {}}
            entries = manifest["files"]

        captured = {relative.casefold() for relative in entries}
        additions = {relative: path for relative, path in selected.items() if relative.casefold() not in captured}
        if not additions:
            return manifest
        for relative, path in additions.items():
            entries[relative] = {"present": path.is_file(), "size": path.stat().st_size if path.is_file() else 0}

        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix="." + output.name + ".", suffix=".tmp", dir=output.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for relative, entry in entries.items():
                    if not entry["present"]:
                        continue
                    member = "files/" + relative
                    if relative in additions:
                        source = additions[relative]
                        before = source.stat()
                        entry["size"] = before.st_size
                        archive.write(source, member)
                        after = source.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise RuntimeError(f"file changed during capture: {relative}")
                    else:
                        with old.open(member) as source, archive.open(member, "w") as target:
                            while chunk := source.read(1024 * 1024):
                                target.write(chunk)
                archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
                archive.writestr("RESTORE.txt", RESTORE)
            # The old archive must be closed before replacement on Windows.
            resources.close()
            os.replace(temporary, output)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True, help="one task's latest.zip; existing pre-images are retained")
    parser.add_argument("--file", action="append", required=True, dest="files", help="exact project-relative path; repeat for each file")
    parser.add_argument("--note", default="", help="purpose and restore limits, recorded when creating the pack")
    args = parser.parse_args()
    manifest = create_change_backup(args.root, args.output, args.files, note=args.note)
    print(f"backup={args.output} files={len(manifest['files'])} bytes={args.output.stat().st_size}")


if __name__ == "__main__":
    main()
