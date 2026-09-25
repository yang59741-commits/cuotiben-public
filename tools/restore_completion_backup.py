"""Verify and restore one completion snapshot into a new, empty directory."""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
import sys
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.create_completion_backup import (
    RECOVERABLE_SNAPSHOT_KINDS,
    SNAPSHOT_SCHEMA_VERSION,
    SQLITE_FILE_SUFFIXES,
    SQLITE_SIDECAR_SUFFIXES,
    file_digest,
    validate_captured_assets,
)


class SnapshotVerificationError(RuntimeError):
    """Raised when a snapshot cannot be trusted for restoration."""


def read_snapshot(snapshot: Path) -> dict:
    path = snapshot / "SNAPSHOT.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotVerificationError("SNAPSHOT.json 无法读取") from exc
    if payload.get("schema_version") not in {1, SNAPSHOT_SCHEMA_VERSION} or not isinstance(
        payload.get("assets"), list
    ):
        raise SnapshotVerificationError("SNAPSHOT.json 契约不兼容")
    return payload


def verify_hash_manifest(snapshot: Path) -> int:
    manifest = snapshot / "SHA256_MANIFEST.csv"
    try:
        with manifest.open("r", encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
    except OSError as exc:
        raise SnapshotVerificationError("SHA256_MANIFEST.csv 无法读取") from exc
    if not rows:
        raise SnapshotVerificationError("SHA256 清单为空")
    seen: set[str] = set()
    for row in rows:
        rendered = str(row.get("RelativePath") or "").replace("\\", "/")
        pure = PurePosixPath(rendered)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise SnapshotVerificationError("SHA256 清单包含不安全路径")
        relative = Path(*pure.parts)
        normalized = pure.as_posix()
        if not normalized or normalized in seen:
            raise SnapshotVerificationError("SHA256 清单包含重复或空路径")
        seen.add(normalized)
        path = snapshot / relative
        try:
            size_matches = path.is_file() and path.stat().st_size == int(row["Length"])
        except (OSError, TypeError, ValueError, KeyError) as exc:
            raise SnapshotVerificationError("SHA256 清单格式无效") from exc
        if not size_matches or file_digest(path) != row.get("SHA256"):
            raise SnapshotVerificationError(f"SHA256 校验失败：{relative.as_posix()}")
    actual = {
        path.relative_to(snapshot).as_posix()
        for path in snapshot.rglob("*")
        if path.is_file() and path.name != "SHA256_MANIFEST.csv"
    }
    if actual != seen:
        raise SnapshotVerificationError("SHA256 清单与快照实际文件集合不一致")
    return len(rows)


def verify_sqlite(path: Path) -> dict[str, int | str]:
    if not path.is_file():
        raise SnapshotVerificationError(f"缺少数据库：{path.name}")
    connection = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro&immutable=1", uri=True
    )
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    if quick_check != "ok" or foreign_key_rows:
        raise SnapshotVerificationError(f"数据库一致性校验失败：{path.name}")
    return {
        "quick_check": "ok",
        "foreign_key_check": "ok",
        "user_version": user_version,
    }


def verify_snapshot(snapshot: Path) -> dict:
    snapshot = snapshot.resolve(strict=True)
    payload = read_snapshot(snapshot)
    entries = verify_hash_manifest(snapshot)
    assets = payload["assets"]
    if any(not isinstance(asset, dict) for asset in assets):
        raise SnapshotVerificationError("快照资产记录无效")
    layout = payload.get("runtime_layout", "legacy")
    if not isinstance(layout, str) or layout not in {"formal", "legacy"}:
        raise SnapshotVerificationError("快照运行布局不受支持")
    if layout == "formal" and payload.get("restorable"):
        database_asset = next((asset for asset in assets if asset.get("asset_id") == "database"), {})
        if database_asset.get("snapshot_path") != "data/cuoti.db":
            raise SnapshotVerificationError("正式布局快照的数据库路径不一致")
    snapshot_kind = str(payload.get("snapshot_kind") or "")
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version >= SNAPSHOT_SCHEMA_VERSION:
        restorable = payload.get("restorable") is True
    else:
        restorable = snapshot_kind in RECOVERABLE_SNAPSHOT_KINDS
    if snapshot_kind == "shareable":
        restorable = False
        forbidden_top_level = {"cuoti.db", "data", "instance", "pdfs", "日志", "错题本.exe"}
        if any((snapshot / name).exists() for name in forbidden_top_level):
            raise SnapshotVerificationError("共享快照包含运行时或本机数据")
        if assets:
            raise SnapshotVerificationError("共享快照不得声明可恢复运行资产")
        for path in snapshot.rglob("*"):
            lowered = path.name.casefold()
            if path.is_file() and (
                path.suffix.casefold() in SQLITE_FILE_SUFFIXES
                or path.suffix.casefold() in {".exe", ".log"}
                or lowered.endswith(SQLITE_SIDECAR_SUFFIXES)
            ):
                raise SnapshotVerificationError("共享快照包含数据库、可执行文件或日志")
    elif snapshot_kind not in RECOVERABLE_SNAPSHOT_KINDS:
        raise SnapshotVerificationError("快照类型不受支持")
    elif schema_version >= SNAPSHOT_SCHEMA_VERSION and not restorable:
        raise SnapshotVerificationError("可恢复快照缺少恢复授权标记")
    if restorable and not any(asset.get("asset_id") == "database" for asset in assets):
        raise SnapshotVerificationError("可恢复快照缺少数据库资产")
    try:
        if schema_version >= SNAPSHOT_SCHEMA_VERSION:
            asset_results = validate_captured_assets(snapshot, assets)
        else:
            asset_results = {}
            for path in snapshot.rglob("*"):
                if path.is_symlink() or (
                    path.is_file()
                    and path.name.casefold().endswith(SQLITE_SIDECAR_SUFFIXES)
                ):
                    raise SnapshotVerificationError(
                        "旧快照包含符号链接或 SQLite sidecar，不能安全恢复"
                    )
            for asset in assets:
                relative = Path(str(asset.get("snapshot_path") or ""))
                if relative.is_absolute() or ".." in relative.parts:
                    raise SnapshotVerificationError("快照资产路径不安全")
                path = snapshot / relative
                if asset.get("required") and not path.exists():
                    raise SnapshotVerificationError(
                        f"缺少必需资产：{asset.get('asset_id')}"
                    )
                if str(asset.get("kind") or "").startswith("sqlite") and path.exists():
                    asset_results[str(asset.get("asset_id") or "")] = verify_sqlite(path)
    except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
        raise SnapshotVerificationError(str(exc)) from exc
    sqlite_results = {
        asset_id: result
        for asset_id, result in asset_results.items()
        if str(next(
            asset.get("kind")
            for asset in assets
            if asset.get("asset_id") == asset_id
        )).startswith("sqlite")
    }
    return {
        "manifest_entries": entries,
        "snapshot_kind": snapshot_kind,
        "restorable": restorable,
        "sqlite": sqlite_results,
        "runtime_layout": layout,
    }


def restore_snapshot(snapshot: Path, target: Path) -> dict:
    snapshot = snapshot.resolve(strict=True)
    target = target.resolve()
    if target == snapshot or snapshot in target.parents or target in snapshot.parents:
        raise ValueError("恢复目标不能与快照目录互相包含")
    if target.exists() and any(target.iterdir()):
        raise FileExistsError("恢复目标必须不存在或为空")
    verification = verify_snapshot(snapshot)
    if not verification["restorable"]:
        raise SnapshotVerificationError("该快照是共享源码包，不是可恢复快照")
    target.mkdir(parents=True, exist_ok=True)
    shutil.copytree(snapshot, target, dirs_exist_ok=True)
    restored_verification = verify_snapshot(target)
    if restored_verification["sqlite"] != verification["sqlite"]:
        raise SnapshotVerificationError("恢复后数据库或运行资产与快照不一致")
    verification["restored_to"] = str(target)
    return verification


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--restore-to", type=Path)
    args = parser.parse_args()
    result = (
        restore_snapshot(args.snapshot, args.restore_to)
        if args.restore_to is not None
        else verify_snapshot(args.snapshot)
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
