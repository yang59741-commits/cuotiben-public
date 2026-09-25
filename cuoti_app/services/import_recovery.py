"""Small, restorable database + attachment snapshot before replace imports."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def create_replace_recovery(
    database_path: str | Path,
    attachment_root: str | Path,
    backup_parent: str | Path,
) -> Path:
    parent = Path(backup_parent)
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S_%f")
    final = parent / f"导入替换前自动备份_{stamp}"
    staging = parent / f".tmp_import_recovery_{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        database_target = staging / "cuoti.db"
        source = sqlite3.connect(Path(database_path))
        target = sqlite3.connect(database_target)
        try:
            source.backup(target)
            quick = target.execute("PRAGMA quick_check").fetchone()[0]
            foreign = target.execute("PRAGMA foreign_key_check").fetchall()
            version = target.execute("PRAGMA user_version").fetchone()[0]
            if quick != "ok" or foreign:
                raise RuntimeError("自动恢复点数据库校验失败")
        finally:
            target.close()
            source.close()
        attachments_target = staging / "attachments"
        attachments_target.mkdir()
        source_root = Path(attachment_root)
        if source_root.is_dir():
            for item in source_root.iterdir():
                if item.is_file() and not item.name.startswith("."):
                    shutil.copy2(item, attachments_target / item.name)
        files = []
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                files.append({
                    "path": path.relative_to(staging).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": _digest(path),
                })
        (staging / "SNAPSHOT.json").write_text(
            json.dumps({
                "schema_version": 1,
                "snapshot_kind": "import-replace-recovery",
                "created_at": datetime.now().astimezone().isoformat(),
                "database": {"quick_check": quick, "foreign_key_check": "ok", "user_version": version},
                "files": files,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(staging, final)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
