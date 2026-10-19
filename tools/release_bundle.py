"""Prepare, verify, and atomically publish one approved release bundle.

The module never builds a candidate and never starts the application.  It binds
an already accepted candidate to provenance, a rollback plan, the previous formal
executable, and an acceptance receipt before any formal file can be replaced.
Data-changing releases retain the full-snapshot contract. Executable-only
rollback requires explicit acceptance of data and old-executable compatibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SCHEMA_VERSION = 1
APPROVED_STATUS = "approved-for-publish"
FINAL_STATE = "release_receipt_committed"
PENDING_STATE = "pending_replacement"
FAILED_ROLLBACK_STATE = "root_hash_failed_rolled_back"
RESUMABLE_STATES = frozenset(
    {
        "prepared",
        "rollback_bundle_verified",
        "candidate_acceptance_verified",
        "root_replace_started",
    }
)
REQUIRED_ACCEPTANCE_GATES = frozenset(
    {
        "full_test_suite",
        "frontend",
        "registry_assets",
        "source_provenance",
        "isolated_start",
        "isolated_adoption",
        "isolated_stop",
        "browser_acceptance",
        "windows_constraints",
        "rollback_restore",
    }
)


class ReleaseBundleError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseBundleError(f"无法读取 JSON：{path.name}") from exc
    if not isinstance(value, dict):
        raise ReleaseBundleError(f"JSON 顶层必须是对象：{path.name}")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & 0x400
    )


def _project_path(
    root: Path,
    path: Path,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = absolute.relative_to(root)
    except ValueError as exc:
        raise ReleaseBundleError(f"{label} 必须位于项目根目录内") from exc
    if not relative.parts:
        raise ReleaseBundleError(f"{label} 不能是项目根目录")
    current = root
    missing_parent = False
    for part in relative.parts:
        current /= part
        try:
            current.lstat()
        except FileNotFoundError:
            missing_parent = True
            continue
        if missing_parent:
            raise ReleaseBundleError(f"{label} 路径结构无效")
        if _is_link_or_reparse(current):
            raise ReleaseBundleError(f"{label} 不得经过符号链接或 reparse point")
    if must_exist and not absolute.exists():
        raise ReleaseBundleError(f"{label} 不存在")
    return absolute


def _relative(root: Path, path: Path) -> str:
    return _project_path(
        root, path, label="发布资产", must_exist=True
    ).relative_to(root).as_posix()


def _relative_target(root: Path, path: Path, *, label: str) -> str:
    return _project_path(
        root, path, label=label, must_exist=False
    ).relative_to(root).as_posix()


def _declared_path(root: Path, value: object, *, label: str) -> Path:
    text = str(value or "").strip()
    relative = Path(text)
    if not text or relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ReleaseBundleError(f"{label} 路径无效")
    return _project_path(
        root, root / relative, label=label, must_exist=False
    )


def _text(value: object) -> str:
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16-le", "latin-1"):
            try:
                return value.decode(encoding).rstrip("\x00").strip()
            except UnicodeDecodeError:
                continue
    return str(value).rstrip("\x00").strip()


def read_product_version(path: Path) -> str:
    """Read ProductVersion from the candidate itself, never from its filename."""

    try:
        import pefile
    except ImportError as exc:
        raise ReleaseBundleError("缺少 pefile，无法复核 EXE ProductVersion") from exc
    try:
        executable = pefile.PE(str(path), fast_load=True)
        executable.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
    except Exception as exc:
        raise ReleaseBundleError(f"无法读取 EXE ProductVersion：{path.name}") from exc
    try:
        pending: list[object] = list(getattr(executable, "FileInfo", None) or [])
        while pending:
            item = pending.pop()
            if isinstance(item, (list, tuple)):
                pending.extend(item)
                continue
            for table in getattr(item, "StringTable", None) or []:
                for key, value in (getattr(table, "entries", None) or {}).items():
                    if _text(key).casefold() == "productversion":
                        version = _text(value)
                        if version:
                            return version
    finally:
        executable.close()
    raise ReleaseBundleError(f"EXE 缺少 ProductVersion：{path.name}")


def _acceptance_candidate_sha(payload: dict[str, Any]) -> str:
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        return str(candidate.get("sha256") or "").upper()
    return ""


def _acceptance_status(payload: dict[str, Any]) -> str:
    return str(payload.get("status") or "").strip().lower()


def _validate_acceptance_contract(
    payload: dict[str, Any],
    *,
    candidate_hash: str,
    product_version: str,
    provenance_hash: str,
) -> None:
    acceptance_status = _acceptance_status(payload)
    if acceptance_status not in {"passed", "approved-with-explicit-waiver"}:
        raise ReleaseBundleError("候选验收收据未通过")
    if _acceptance_candidate_sha(payload) != candidate_hash:
        raise ReleaseBundleError("候选验收收据与候选 SHA-256 不一致")
    candidate = payload.get("candidate") or {}
    if str(candidate.get("product_version") or "") != product_version:
        raise ReleaseBundleError("候选验收收据与 ProductVersion 不一致")
    provenance = payload.get("source_provenance") or {}
    if str(provenance.get("sha256") or "").upper() != provenance_hash:
        raise ReleaseBundleError("候选验收收据与 provenance 不一致")
    required = payload.get("required_gates")
    gates = payload.get("gates")
    if not isinstance(required, list) or not isinstance(gates, dict):
        raise ReleaseBundleError("候选验收收据缺少 required_gates/gates")
    declared = {str(name) for name in required}
    if not REQUIRED_ACCEPTANCE_GATES.issubset(declared):
        raise ReleaseBundleError("候选验收收据缺少发布必需验收门")
    failed = {name for name in declared if gates.get(name) is not True}
    if acceptance_status == "passed" and failed:
        raise ReleaseBundleError(
            "候选验收收据存在未通过门：" + ", ".join(sorted(failed))
        )
    if acceptance_status == "passed":
        return

    raw_waived = payload.get("waived_gates")
    waiver = payload.get("waiver")
    if (
        not isinstance(raw_waived, list)
        or any(not isinstance(name, str) or not name for name in raw_waived)
        or len(raw_waived) != len(set(raw_waived))
        or not isinstance(waiver, dict)
        or waiver.get("authorized_by") != "user"
        or waiver.get("authorization") != "explicit"
        or not str(waiver.get("reason") or "").strip()
    ):
        raise ReleaseBundleError("候选验收豁免合同无效")
    waived = set(raw_waived)
    if waived != failed or not waived <= declared:
        raise ReleaseBundleError("候选验收豁免门与未通过门不一致")


def _validate_provenance_contract(
    provenance: Path, archive: Path, patch: Path, *, root: Path | None = None
) -> tuple[dict[str, Any], str, str]:
    payload = _read_json(provenance)
    source_tree = payload.get("source_tree")
    git = payload.get("git")
    if not isinstance(source_tree, dict) or not isinstance(
        source_tree.get("archive"), dict
    ):
        raise ReleaseBundleError("provenance 缺少 source snapshot 事实")
    if not isinstance(git, dict):
        raise ReleaseBundleError("provenance 缺少 git 事实")
    archive_hash = file_sha256(archive)
    patch_hash = file_sha256(patch)
    if str(source_tree["archive"].get("sha256") or "").upper() != archive_hash:
        raise ReleaseBundleError("source snapshot 与 provenance 不一致")
    if str(git.get("tracked_patch_sha256") or "").upper() != patch_hash:
        raise ReleaseBundleError("tracked patch 与 provenance 不一致")
    if source_tree["archive"].get("mode", "full") != "full" or payload.get("schema_version") == 2:
        try:
            from tools.capture_source_provenance import validate_source_reference
        except ModuleNotFoundError:
            from capture_source_provenance import validate_source_reference
        if root is None:
            raise ReleaseBundleError("源码差异证明需要项目 Git 仓库")
        try:
            validate_source_reference(root, payload, archive)
        except (ValueError, RuntimeError) as exc:
            raise ReleaseBundleError(str(exc)) from exc
    return payload, archive_hash, patch_hash


def _validate_schema_contract(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseBundleError("schema 兼容矩阵无效")
    supported = value.get("candidate_supported")
    if not isinstance(supported, dict):
        raise ReleaseBundleError("schema 兼容矩阵无效")
    fields = (
        value.get("formal_before"),
        value.get("after_candidate_start"),
        supported.get("min"),
        supported.get("max"),
        value.get("previous_executable_max"),
    )
    if any(not isinstance(item, int) or isinstance(item, bool) for item in fields):
        raise ReleaseBundleError("schema 兼容矩阵无效")
    before, after, candidate_min, candidate_max, previous_max = fields
    if (
        before < 0
        or after < before
        or candidate_min < 0
        or candidate_max < candidate_min
        or previous_max < before
        or not (candidate_min <= before <= candidate_max)
        or not (candidate_min <= after <= candidate_max)
        or value.get("whole_bundle_restore_required") is not (previous_max < after)
    ):
        raise ReleaseBundleError("schema 兼容矩阵无效")
    return value


def _snapshot_metadata(snapshot: Path) -> dict[str, Any]:
    descriptor = _read_json(snapshot / "SNAPSHOT.json")
    try:
        from tools.create_completion_backup import SNAPSHOT_SCHEMA_VERSION
    except ModuleNotFoundError:
        from create_completion_backup import SNAPSHOT_SCHEMA_VERSION

    if descriptor.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
        raise ReleaseBundleError("新发布 bundle 必须使用当前 schema 的回退快照")
    if descriptor.get("restorable") is not True:
        raise ReleaseBundleError("回退快照未声明 restorable=true")
    if descriptor.get("snapshot_kind") not in {
        "private",
        "private-full",
        "private-no-secrets",
    }:
        raise ReleaseBundleError("回退快照种类不可用于正式恢复")
    manifest = snapshot / "SHA256_MANIFEST.csv"
    if not manifest.is_file():
        raise ReleaseBundleError("回退快照缺少 SHA256_MANIFEST.csv")
    try:
        from tools.restore_completion_backup import verify_snapshot
    except ModuleNotFoundError:
        from restore_completion_backup import verify_snapshot

    verification = verify_snapshot(snapshot)
    if verification.get("restorable") is not True:
        raise ReleaseBundleError("回退快照未通过可恢复性验证")
    return {
        "snapshot_descriptor_sha256": file_sha256(snapshot / "SNAPSHOT.json"),
        "manifest_sha256": file_sha256(manifest),
        "snapshot_kind": descriptor["snapshot_kind"],
        "verification": verification,
    }


def _validate_executable_only_rollback(before: int, after: int, acceptance: dict) -> None:
    if before != after:
        raise ReleaseBundleError("仅 EXE 回退不允许数据库 schema 变化")
    proof = acceptance.get("executable_only_rollback")
    if not isinstance(proof, dict) or any(proof.get(key) is not True for key in (
        "no_persistent_data_changes", "previous_executable_compatible",
    )):
        raise ReleaseBundleError("仅 EXE 回退需要验收证明无持久化数据变更且旧程序兼容")


def _validate_catalog_guard(before: int, after: int, acceptance: dict) -> dict:
    proof = acceptance.get('catalog_guarded_rollback')
    if before != after or not isinstance(proof, dict) or any(proof.get(key) is not True for key in (
            'no_release_data_migration', 'previous_compatible_without_new_codes', 'guard_rehearsal_passed')):
        raise ReleaseBundleError('目录受限回退需要同 schema、无发布迁移及已演练的兼容证明')
    codes = proof.get('new_method_codes')
    if not isinstance(codes, list) or not codes or any(
            not isinstance(code, str) or not code or any(ch not in 'abcdefghijklmnopqrstuvwxyz_0123456789' for ch in code)
            for code in codes) or len(set(codes)) != len(codes):
        raise ReleaseBundleError('目录受限回退缺少明确的新增方法代码')
    return proof


def check_catalog_rollback(database: Path, codes: list[str], schema: int) -> dict:
    """Check only on release/rollback; never transform or discard newer annotations."""
    database = database.resolve(strict=True)
    columns = (
        ('mistake_classifications', 'method_annotations_json'),
        ('classification_suggestions', 'payload_json'),
        ('daily_review_items', 'snapshot_json'),
        ('daily_review_rounds', 'selection_json'),
        ('job_inputs', 'payload_json'),
        ('background_jobs', 'result_json'),
        ('job_business_results', 'result_json'),
    )
    placeholders = ','.join('?' for _ in codes)
    references = []
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA user_version').fetchone()[0] != schema:
            raise ReleaseBundleError('回退检查的数据库 schema 与发布证明不符')
        for table, column in columns:
            if db.execute(f"SELECT 1 FROM {table} r,json_tree(COALESCE(r.{column},'null')) j "
                          f"WHERE j.key IN ('code','method_code') AND j.value IN ({placeholders}) LIMIT 1", codes).fetchone():
                references.append(table)
    if references:
        raise ReleaseBundleError('已有新增方法引用，禁止仅替换旧 EXE；保留数据并向前修复：' + '、'.join(references))
    return {'safe_before_new_method_use': True, 'schema': schema}


def _require_release_scoped_target(
    root: Path, path: Path, *, build_id: str, label: str
) -> None:
    relative = path.relative_to(root)
    if not any(build_id in part for part in relative.parts):
        raise ReleaseBundleError(f"{label} 必须使用包含 build_id 的发布唯一路径")


def prepare_release_bundle(
    root: Path,
    *,
    candidate: Path,
    provenance: Path,
    acceptance: Path,
    rollback_snapshot: Path | None,
    formal_executable: Path,
    previous_executable: Path,
    output: Path,
    receipt_path: Path,
    build_id: str,
    product_version: str,
    previous_product_version: str,
    schema_before: int,
    schema_after: int,
    previous_max_schema: int,
    candidate_min_schema: int,
    candidate_max_schema: int,
    rollback_mode: str = "full-snapshot",
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    candidate = _project_path(root, candidate, label="candidate", must_exist=True)
    provenance = _project_path(root, provenance, label="provenance", must_exist=True)
    acceptance = _project_path(root, acceptance, label="acceptance", must_exist=True)
    if rollback_mode not in {"full-snapshot", "executable-only", "catalog-guarded"}:
        raise ReleaseBundleError("不支持的回退方式")
    if rollback_mode == "full-snapshot":
        if rollback_snapshot is None:
            raise ReleaseBundleError("完整回退需要 --rollback-snapshot；无数据变更的发布可显式选择 executable-only")
        rollback_snapshot = _project_path(
            root, rollback_snapshot, label="rollback snapshot", must_exist=True
        )
    elif rollback_snapshot is not None:
        raise ReleaseBundleError("仅 EXE 回退不接受完整快照路径")
    formal_executable = _project_path(
        root, formal_executable, label="formal executable", must_exist=True
    )
    output = _project_path(root, output, label="release bundle", must_exist=False)
    receipt_path = _project_path(
        root, receipt_path, label="release receipt", must_exist=False
    )
    previous_executable = _project_path(
        root,
        previous_executable,
        label="previous executable archive",
        must_exist=False,
    )
    if not build_id.isdigit() or len(build_id) != 14:
        raise ReleaseBundleError("build_id 必须是 14 位数字")
    if product_version != build_id:
        raise ReleaseBundleError("candidate ProductVersion 必须等于 build_id")
    if not previous_product_version.isdigit() or len(previous_product_version) != 14:
        raise ReleaseBundleError("上一正式 EXE ProductVersion 必须是 14 位数字")
    if (
        schema_before < 0
        or schema_after < schema_before
        or previous_max_schema < 0
        or candidate_min_schema < 0
        or candidate_max_schema < candidate_min_schema
        or schema_before > previous_max_schema
        or not (candidate_min_schema <= schema_before <= candidate_max_schema)
        or not (candidate_min_schema <= schema_after <= candidate_max_schema)
    ):
        raise ReleaseBundleError("schema 兼容矩阵无效")
    for target, label in (
        (output, "release bundle"),
        (receipt_path, "release receipt"),
        (previous_executable, "previous executable archive"),
    ):
        _require_release_scoped_target(
            root, target, build_id=build_id, label=label
        )
    if output.exists():
        raise ReleaseBundleError("release bundle 发布唯一路径已存在")
    if len(
        {
            candidate,
            provenance,
            acceptance,
            rollback_snapshot,
            formal_executable,
            output,
            receipt_path,
            previous_executable,
        }
    ) != 8:
        raise ReleaseBundleError("release bundle 资产与写入目标不得复用同一路径")
    actual_candidate_version = read_product_version(candidate)
    if actual_candidate_version != product_version:
        raise ReleaseBundleError("candidate 实际 ProductVersion 与声明不一致")
    actual_previous_version = read_product_version(formal_executable)
    if actual_previous_version != previous_product_version:
        raise ReleaseBundleError("上一正式 EXE 实际 ProductVersion 与声明不一致")

    acceptance_payload = _read_json(acceptance)
    candidate_hash = file_sha256(candidate)
    archive = _project_path(
        root,
        provenance.with_name("source-snapshot.zip"),
        label="source snapshot",
        must_exist=True,
    )
    patch = _project_path(
        root,
        provenance.with_name("tracked-changes.patch"),
        label="tracked patch",
        must_exist=True,
    )
    if not archive.is_file():
        raise ReleaseBundleError("provenance 缺少 source-snapshot.zip")
    if not patch.is_file():
        raise ReleaseBundleError("provenance 缺少 tracked-changes.patch")
    provenance_payload, archive_hash, patch_hash = _validate_provenance_contract(
        provenance, archive, patch, root=root
    )
    git = provenance_payload["git"]
    provenance_hash = file_sha256(provenance)
    _validate_acceptance_contract(
        acceptance_payload,
        candidate_hash=candidate_hash,
        product_version=product_version,
        provenance_hash=provenance_hash,
    )

    rollback_metadata: dict[str, Any] = {"mode": rollback_mode}
    if rollback_mode == "executable-only":
        _validate_executable_only_rollback(schema_before, schema_after, acceptance_payload)
    elif rollback_mode == 'catalog-guarded':
        proof = _validate_catalog_guard(schema_before, schema_after, acceptance_payload)
        check_catalog_rollback(root / 'data' / 'cuoti.db', proof['new_method_codes'], schema_before)
        rollback_metadata['restriction'] = 'Check new method references with check-rollback before any old EXE restoration; otherwise forward-fix only.'
    else:
        snapshot = _snapshot_metadata(rollback_snapshot)
        snapshot_schema = snapshot.get("verification", {}).get("sqlite", {}).get("database", {}).get("user_version")
        if snapshot_schema != schema_before:
            raise ReleaseBundleError("回退快照数据库 schema 与 formal_before 不一致")
        rollback_metadata.update(snapshot_path=_relative(root, rollback_snapshot), **snapshot)
    previous_hash = file_sha256(formal_executable)
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "status": APPROVED_STATUS,
        "prepared_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "build_id": build_id,
        "candidate": {
            "path": _relative(root, candidate),
            "length": candidate.stat().st_size,
            "sha256": candidate_hash,
            "product_version": product_version,
        },
        "source_provenance": {
            "path": _relative(root, provenance),
            "sha256": provenance_hash,
            "source_snapshot_path": _relative(root, archive),
            "source_snapshot_sha256": archive_hash,
            "tracked_patch_path": _relative(root, patch),
            "tracked_patch_sha256": patch_hash,
            "git": git,
            "versions": provenance_payload.get("versions", {}),
        },
        "acceptance": {
            "path": _relative(root, acceptance),
            "sha256": file_sha256(acceptance),
            "status": _acceptance_status(acceptance_payload),
        },
        "rollback": {
            **rollback_metadata,
            "previous_executable": {
                "path": _relative(root, formal_executable),
                "length": formal_executable.stat().st_size,
                "sha256": previous_hash,
                "product_version": previous_product_version,
                "archive_path": _relative_target(
                    root, previous_executable, label="previous executable archive"
                ),
            },
        },
        "schema_compatibility": {
            "formal_before": int(schema_before),
            "after_candidate_start": int(schema_after),
            "candidate_supported": {
                "min": int(candidate_min_schema),
                "max": int(candidate_max_schema),
            },
            "previous_executable_max": int(previous_max_schema),
            "whole_bundle_restore_required": previous_max_schema < schema_after,
        },
        "formal": {
            "path": _relative(root, formal_executable),
            "receipt_path": _relative_target(root, receipt_path, label="release receipt"),
        },
    }
    _atomic_json(output, bundle)
    return bundle


def verify_release_bundle(
    root: Path,
    bundle_path: Path,
    *,
    expected_bundle_sha256: str | None = None,
    verify_current_formal: bool = True,
    expected_candidate: Path | None = None,
    expected_provenance: Path | None = None,
    expected_product_version: str | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    bundle_path = _project_path(
        root, bundle_path, label="release bundle", must_exist=True
    )
    if expected_bundle_sha256:
        expected = expected_bundle_sha256.strip().upper()
        if len(expected) != 64 or file_sha256(bundle_path) != expected:
            raise ReleaseBundleError("release bundle SHA-256 不一致")
    bundle = _read_json(bundle_path)
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseBundleError("release bundle schema 不受支持")
    if bundle.get("status") != APPROVED_STATUS:
        raise ReleaseBundleError("release bundle 未获准发布")

    candidate_info = bundle.get("candidate") or {}
    provenance_info = bundle.get("source_provenance") or {}
    acceptance_info = bundle.get("acceptance") or {}
    rollback_info = bundle.get("rollback") or {}
    formal_info = bundle.get("formal") or {}
    candidate = _declared_path(root, candidate_info.get("path"), label="candidate")
    provenance = _declared_path(root, provenance_info.get("path"), label="provenance")
    if expected_candidate is not None and candidate != _project_path(
        root, expected_candidate, label="expected candidate", must_exist=True
    ):
        raise ReleaseBundleError("release bundle 未绑定指定候选路径")
    if expected_provenance is not None and provenance != _project_path(
        root, expected_provenance, label="expected provenance", must_exist=True
    ):
        raise ReleaseBundleError("release bundle 未绑定指定 provenance 路径")
    archive = _declared_path(
        root, provenance_info.get("source_snapshot_path"), label="source snapshot"
    )
    patch = _declared_path(
        root, provenance_info.get("tracked_patch_path"), label="tracked patch"
    )
    acceptance = _declared_path(root, acceptance_info.get("path"), label="acceptance")
    rollback_mode = rollback_info.get("mode", "full-snapshot")
    if rollback_mode not in {"full-snapshot", "executable-only", "catalog-guarded"}:
        raise ReleaseBundleError("不支持的回退方式")
    snapshot = (_declared_path(root, rollback_info.get("snapshot_path"), label="rollback snapshot")
                if rollback_mode == "full-snapshot" else None)
    formal = _declared_path(root, formal_info.get("path"), label="formal executable")
    required_files = (candidate, provenance, archive, patch, acceptance)
    if not all(path.is_file() for path in required_files) or (snapshot is not None and not snapshot.is_dir()):
        raise ReleaseBundleError("release bundle 资产缺失")
    expected_hashes = (
        (candidate, candidate_info.get("sha256"), "candidate"),
        (provenance, provenance_info.get("sha256"), "provenance"),
        (archive, provenance_info.get("source_snapshot_sha256"), "source snapshot"),
        (patch, provenance_info.get("tracked_patch_sha256"), "tracked patch"),
        (acceptance, acceptance_info.get("sha256"), "acceptance"),
    )
    for path, expected, label in expected_hashes:
        if file_sha256(path) != str(expected or "").upper():
            raise ReleaseBundleError(f"{label} SHA-256 不一致")
    if candidate.stat().st_size != candidate_info.get("length"):
        raise ReleaseBundleError("candidate 长度与 release bundle 不一致")
    provenance_payload, archive_hash, patch_hash = _validate_provenance_contract(
        provenance, archive, patch, root=root
    )
    if (
        archive_hash != str(provenance_info.get("source_snapshot_sha256") or "").upper()
        or patch_hash != str(provenance_info.get("tracked_patch_sha256") or "").upper()
        or provenance_payload.get("git") != provenance_info.get("git")
    ):
        raise ReleaseBundleError("release bundle 与 provenance 内容不一致")
    acceptance_payload = _read_json(acceptance)
    product_version = str(candidate_info.get("product_version") or "")
    if read_product_version(candidate) != product_version:
        raise ReleaseBundleError("candidate 实际 ProductVersion 与 release bundle 不一致")
    if expected_product_version is not None and product_version != expected_product_version:
        raise ReleaseBundleError("release bundle ProductVersion 与指定候选不一致")
    _validate_acceptance_contract(
        acceptance_payload,
        candidate_hash=file_sha256(candidate),
        product_version=product_version,
        provenance_hash=file_sha256(provenance),
    )
    schema = _validate_schema_contract(bundle.get("schema_compatibility"))
    if rollback_mode == "executable-only":
        _validate_executable_only_rollback(schema["formal_before"], schema["after_candidate_start"], acceptance_payload)
    elif rollback_mode == 'catalog-guarded':
        _validate_catalog_guard(schema['formal_before'], schema['after_candidate_start'], acceptance_payload)
    else:
        snapshot_metadata = _snapshot_metadata(snapshot)
        if snapshot_metadata["snapshot_descriptor_sha256"] != str(
            rollback_info.get("snapshot_descriptor_sha256") or ""
        ).upper():
            raise ReleaseBundleError("rollback snapshot descriptor 已变化")
        if snapshot_metadata["manifest_sha256"] != str(
            rollback_info.get("manifest_sha256") or ""
        ).upper():
            raise ReleaseBundleError("rollback snapshot manifest 已变化")
        if snapshot_metadata["snapshot_kind"] != rollback_info.get("snapshot_kind"):
            raise ReleaseBundleError("rollback snapshot kind 已变化")
        snapshot_schema = snapshot_metadata.get("verification", {}).get("sqlite", {}).get("database", {}).get("user_version")
        if snapshot_schema != schema["formal_before"]:
            raise ReleaseBundleError("回退快照数据库 schema 与 formal_before 不一致")
    previous_info = rollback_info.get("previous_executable") or {}
    if verify_current_formal:
        if not formal.is_file() or file_sha256(formal) != str(
            previous_info.get("sha256") or ""
        ).upper():
            raise ReleaseBundleError("当前正式 EXE 不等于 bundle 记录的上一正式版本")
        if read_product_version(formal) != str(
            previous_info.get("product_version") or ""
        ):
            raise ReleaseBundleError("上一正式 EXE ProductVersion 与 bundle 不一致")
    return {
        "bundle": bundle,
        "bundle_sha256": file_sha256(bundle_path),
        "candidate": candidate,
        "formal": formal,
        "snapshot": snapshot,
    }


def _copy_fsync(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as reader, destination.open("wb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())


def _release_receipt_payload(
    *,
    root: Path,
    bundle_path: Path,
    bundle_sha256: str,
    bundle: dict[str, Any],
    formal: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "kind": "formal-release-acceptance",
        "release_id": bundle["build_id"],
        "released_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "status": "formal-published",
        "release_bundle": {
            "path": bundle_path.resolve().relative_to(root).as_posix(),
            "sha256": bundle_sha256,
        },
        "candidate": dict(bundle["candidate"]),
        "formal_executable": {
            "path": bundle["formal"]["path"],
            "length": formal.stat().st_size,
            "sha256": file_sha256(formal),
            "matches_candidate": True,
        },
        "rollback": bundle["rollback"],
        "schema_compatibility": bundle["schema_compatibility"],
    }


def _load_matching_state(
    state: Path,
    *,
    root: Path,
    formal: Path,
    build_id: str,
    bundle_sha256: str,
) -> dict[str, Any] | None:
    if not state.exists():
        return None
    payload = _read_json(state)
    if payload.get("schema_version") != 1:
        raise ReleaseBundleError("已有发布状态 schema 不受支持")
    mismatched = (
        payload.get("build_id") != build_id
        or str(payload.get("bundle_sha256") or "").upper() != bundle_sha256
    )
    if mismatched:
        if payload.get("status") != FINAL_STATE:
            raise ReleaseBundleError("未完成的发布状态属于另一个 release bundle")
        receipt = _declared_path(
            root, payload.get("receipt_path"), label="previous release receipt"
        )
        previous_bundle = _declared_path(
            root, payload.get("bundle_path"), label="previous release bundle"
        )
        if (
            not receipt.is_file()
            or file_sha256(receipt)
            != str(payload.get("receipt_sha256") or "").upper()
            or not previous_bundle.is_file()
            or file_sha256(previous_bundle)
            != str(payload.get("bundle_sha256") or "").upper()
        ):
            raise ReleaseBundleError("上一发布的 FINAL 状态证据不可验证")
        receipt_payload = _read_json(receipt)
        release_binding = receipt_payload.get("release_bundle") or {}
        formal_binding = receipt_payload.get("formal_executable") or {}
        if (
            receipt_payload.get("status") != "formal-published"
            or str(release_binding.get("sha256") or "").upper()
            != str(payload.get("bundle_sha256") or "").upper()
            or release_binding.get("path") != payload.get("bundle_path")
            or not formal.is_file()
            or file_sha256(formal)
            != str(formal_binding.get("sha256") or "").upper()
        ):
            raise ReleaseBundleError("上一发布收据与当前正式 EXE 不一致")
        return None
    if payload.get("status") == FAILED_ROLLBACK_STATE:
        raise ReleaseBundleError("上一替换已因根哈希失败回退；必须重新批准新 bundle")
    if payload.get("status") not in RESUMABLE_STATES | {
        PENDING_STATE,
        "root_hash_verified",
        FINAL_STATE,
    }:
        raise ReleaseBundleError("已有发布状态值无效")
    return payload


def _validate_existing_receipt(
    receipt_payload: dict[str, Any],
    *,
    expected: dict[str, Any],
) -> None:
    for key in ("schema_version", "kind", "release_id", "status"):
        if receipt_payload.get(key) != expected.get(key):
            raise ReleaseBundleError("现有正式发布收据与本 bundle 不一致")
    for key in (
        "release_bundle",
        "candidate",
        "formal_executable",
        "rollback",
        "schema_compatibility",
    ):
        if receipt_payload.get(key) != expected.get(key):
            raise ReleaseBundleError("现有正式发布收据与本 bundle 不一致")


def _commit_receipt(
    *,
    root: Path,
    bundle_path: Path,
    verified: dict[str, Any],
    formal: Path,
    receipt: Path,
    state: Path,
    state_payload: dict[str, Any],
) -> dict[str, Any]:
    bundle = verified["bundle"]
    candidate_hash = str(bundle["candidate"]["sha256"]).upper()
    if not formal.is_file() or file_sha256(formal) != candidate_hash:
        raise ReleaseBundleError("正式 EXE 尚未通过候选哈希校验，不能提交发布收据")
    expected_receipt = _release_receipt_payload(
        root=root,
        bundle_path=bundle_path,
        bundle_sha256=verified["bundle_sha256"],
        bundle=bundle,
        formal=formal,
    )
    if receipt.exists():
        receipt_payload = _read_json(receipt)
        _validate_existing_receipt(receipt_payload, expected=expected_receipt)
    else:
        receipt_payload = expected_receipt
        _atomic_json(receipt, receipt_payload)
    state_payload["status"] = FINAL_STATE
    state_payload["receipt_path"] = receipt.relative_to(root).as_posix()
    state_payload["receipt_sha256"] = file_sha256(receipt)
    _atomic_json(state, state_payload)
    return receipt_payload


def publish_release_bundle(
    root: Path,
    bundle_path: Path,
    *,
    expected_bundle_sha256: str,
    expected_candidate: Path | None = None,
    expected_provenance: Path | None = None,
    expected_product_version: str | None = None,
) -> dict[str, Any]:
    root = root.resolve(strict=True)
    if (
        expected_candidate is None
        or expected_provenance is None
        or expected_product_version is None
    ):
        raise ReleaseBundleError(
            "publish 必须显式绑定实际 candidate、provenance 与 ProductVersion"
        )
    bundle_path = _project_path(
        root, bundle_path, label="release bundle", must_exist=True
    )
    verified = verify_release_bundle(
        root,
        bundle_path,
        expected_bundle_sha256=expected_bundle_sha256,
        verify_current_formal=False,
        expected_candidate=expected_candidate,
        expected_provenance=expected_provenance,
        expected_product_version=expected_product_version,
    )
    bundle = verified["bundle"]
    candidate = verified["candidate"]
    formal = verified["formal"]
    if bundle['rollback'].get('mode') == 'catalog-guarded':
        acceptance = _read_json(_declared_path(root, bundle['acceptance']['path'], label='acceptance'))
        proof = acceptance['catalog_guarded_rollback']
        check_catalog_rollback(root / 'data' / 'cuoti.db', proof['new_method_codes'], bundle['schema_compatibility']['formal_before'])
    rollback_info = bundle["rollback"]["previous_executable"]
    previous = _declared_path(root, rollback_info["archive_path"], label="previous executable")
    receipt = _declared_path(root, bundle["formal"]["receipt_path"], label="release receipt")
    state = _project_path(
        root,
        formal.with_name(formal.name + ".release-state.json"),
        label="release state",
        must_exist=False,
    )

    candidate_hash = str(bundle["candidate"]["sha256"]).upper()
    previous_hash = str(rollback_info["sha256"]).upper()
    bundle_relative = bundle_path.relative_to(root).as_posix()
    staged = _project_path(
        root,
        formal.with_name(f".{formal.name}.{bundle['build_id']}.staged"),
        label="staged candidate",
        must_exist=False,
    )
    previous_temp = _project_path(
        root,
        previous.with_name(previous.name + ".tmp"),
        label="previous executable temporary",
        must_exist=False,
    )
    pending = _project_path(
        root,
        formal.with_name(f"{formal.stem}_待替换{formal.suffix}"),
        label="pending replacement",
        must_exist=False,
    )
    existing_state = _load_matching_state(
        state,
        root=root,
        formal=formal,
        build_id=bundle["build_id"],
        bundle_sha256=verified["bundle_sha256"],
    )
    state_payload = existing_state or {
        "schema_version": 1,
        "build_id": bundle["build_id"],
        "bundle_path": bundle_relative,
        "bundle_sha256": verified["bundle_sha256"],
        "status": "prepared",
    }
    if state_payload.get("bundle_path") != bundle_relative:
        raise ReleaseBundleError("发布状态记录的 bundle 路径不一致")

    formal_hash = file_sha256(formal) if formal.is_file() else ""
    if formal_hash == candidate_hash:
        if not previous.is_file() or file_sha256(previous) != previous_hash:
            raise ReleaseBundleError("正式 EXE 已替换，但上一正式版本备份不可验证")
        return _commit_receipt(
            root=root,
            bundle_path=bundle_path,
            verified=verified,
            formal=formal,
            receipt=receipt,
            state=state,
            state_payload=state_payload,
        )
    if formal_hash != previous_hash:
        raise ReleaseBundleError("当前正式 EXE 既不是 bundle 候选，也不是记录的上一正式版本")
    if read_product_version(formal) != str(rollback_info.get("product_version") or ""):
        raise ReleaseBundleError("当前正式 EXE ProductVersion 与 bundle 上一版本不一致")
    if existing_state is not None and state_payload.get("status") in {
        "root_hash_verified",
        FINAL_STATE,
    }:
        raise ReleaseBundleError("发布状态已越过根哈希验证，但正式 EXE 不是候选")

    if state_payload.get("status") == PENDING_STATE:
        if pending.is_file() and file_sha256(pending) == candidate_hash:
            return {
                "status": "pending-replacement",
                "build_id": bundle["build_id"],
                "pending_path": pending.relative_to(root).as_posix(),
                "sha256": candidate_hash,
                "release_bundle_sha256": verified["bundle_sha256"],
            }
        raise ReleaseBundleError("待替换发布状态存在，但待替换候选不可验证")

    if existing_state is None:
        for temporary in (staged, previous_temp):
            if temporary.exists():
                raise ReleaseBundleError(
                    f"发现没有 matching state 的发布临时文件：{temporary.name}"
                )
    _atomic_json(state, state_payload)
    try:
        expected_candidate_length = int(bundle["candidate"]["length"])
        if staged.exists():
            staged_valid = (
                staged.is_file()
                and staged.stat().st_size == expected_candidate_length
                and file_sha256(staged) == candidate_hash
            )
            if not staged_valid:
                if state_payload.get("status") != "prepared":
                    raise ReleaseBundleError("matching state 的 staging 候选不可验证")
                staged.unlink()
        if not staged.exists():
            _copy_fsync(candidate, staged)
        if (
            staged.stat().st_size != expected_candidate_length
            or file_sha256(staged) != candidate_hash
        ):
            raise ReleaseBundleError("正式同目录 staging 候选哈希不一致")
        if previous.exists():
            if not previous.is_file() or file_sha256(previous) != previous_hash:
                raise ReleaseBundleError("上一正式 EXE 备份已存在但哈希不一致")
        else:
            if previous_temp.exists() and file_sha256(previous_temp) != previous_hash:
                if state_payload.get("status") != "prepared":
                    raise ReleaseBundleError("matching state 的上一版本临时备份不可验证")
                previous_temp.unlink()
            if not previous_temp.exists():
                _copy_fsync(formal, previous_temp)
            if file_sha256(previous_temp) != previous_hash:
                raise ReleaseBundleError("上一正式 EXE 临时备份哈希不一致")
            previous.parent.mkdir(parents=True, exist_ok=True)
            os.replace(previous_temp, previous)
        state_payload["status"] = "rollback_bundle_verified"
        _atomic_json(state, state_payload)

        state_payload["status"] = "candidate_acceptance_verified"
        _atomic_json(state, state_payload)

        state_payload["status"] = "root_replace_started"
        _atomic_json(state, state_payload)
        try:
            os.replace(staged, formal)
        except (PermissionError, OSError) as exc:
            if not isinstance(exc, PermissionError) and getattr(exc, "winerror", None) not in {
                5,
                32,
            }:
                raise
            if pending.exists() and file_sha256(pending) != candidate_hash:
                raise ReleaseBundleError("固定待替换文件已存在且不属于本 bundle") from exc
            if staged.exists():
                os.replace(staged, pending)
            state_payload["status"] = PENDING_STATE
            state_payload["pending_path"] = pending.relative_to(root).as_posix()
            state_payload["pending_sha256"] = file_sha256(pending)
            _atomic_json(state, state_payload)
            return {
                "status": "pending-replacement",
                "build_id": bundle["build_id"],
                "pending_path": pending.relative_to(root).as_posix(),
                "sha256": candidate_hash,
                "release_bundle_sha256": verified["bundle_sha256"],
            }
        if file_sha256(formal) != candidate_hash:
            restore = _project_path(
                root,
                formal.with_name(f".{formal.name}.{bundle['build_id']}.restore"),
                label="formal restore temporary",
                must_exist=False,
            )
            if restore.exists():
                raise ReleaseBundleError("发现无法证明归属的正式恢复临时文件")
            _copy_fsync(previous, restore)
            os.replace(restore, formal)
            if file_sha256(formal) != previous_hash:
                raise ReleaseBundleError("正式 EXE 根哈希失败，且上一正式版本恢复校验失败")
            state_payload["status"] = FAILED_ROLLBACK_STATE
            state_payload["restored_formal_sha256"] = previous_hash
            _atomic_json(state, state_payload)
            raise ReleaseBundleError("正式 EXE 替换后哈希不一致，已恢复上一正式版本")
        state_payload["status"] = "root_hash_verified"
        _atomic_json(state, state_payload)

        return _commit_receipt(
            root=root,
            bundle_path=bundle_path,
            verified=verified,
            formal=formal,
            receipt=receipt,
            state=state,
            state_payload=state_payload,
        )
    except BaseException:
        staged.unlink(missing_ok=True)
        previous_temp.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("--root", type=Path, required=True)
    prepare.add_argument("--candidate", type=Path, required=True)
    prepare.add_argument("--provenance", type=Path, required=True)
    prepare.add_argument("--acceptance", type=Path, required=True)
    prepare.add_argument("--rollback-snapshot", type=Path)
    prepare.add_argument("--rollback-mode", choices=("full-snapshot", "executable-only", "catalog-guarded"), default="full-snapshot")
    prepare.add_argument("--formal-executable", type=Path, required=True)
    prepare.add_argument("--previous-executable", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--receipt-path", type=Path, required=True)
    prepare.add_argument("--build-id", required=True)
    prepare.add_argument("--product-version", required=True)
    prepare.add_argument("--previous-product-version", required=True)
    prepare.add_argument("--schema-before", type=int, required=True)
    prepare.add_argument("--schema-after", type=int, required=True)
    prepare.add_argument("--previous-max-schema", type=int, required=True)
    prepare.add_argument("--candidate-min-schema", type=int, required=True)
    prepare.add_argument("--candidate-max-schema", type=int, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--bundle", type=Path, required=True)
    verify.add_argument("--expected-sha256", required=True)
    verify.add_argument("--expected-candidate", type=Path)
    verify.add_argument("--expected-provenance", type=Path)
    verify.add_argument("--expected-product-version")
    publish = subparsers.add_parser("publish")
    publish.add_argument("--root", type=Path, required=True)
    publish.add_argument("--bundle", type=Path, required=True)
    publish.add_argument("--expected-sha256", required=True)
    publish.add_argument("--expected-candidate", type=Path, required=True)
    publish.add_argument("--expected-provenance", type=Path, required=True)
    publish.add_argument("--expected-product-version", required=True)
    rollback = subparsers.add_parser('check-rollback')
    rollback.add_argument('--root', type=Path, required=True)
    rollback.add_argument('--bundle', type=Path, required=True)
    rollback.add_argument('--expected-sha256', required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "prepare":
        bundle = prepare_release_bundle(
            args.root,
            candidate=args.candidate,
            provenance=args.provenance,
            acceptance=args.acceptance,
            rollback_snapshot=args.rollback_snapshot,
            rollback_mode=args.rollback_mode,
            formal_executable=args.formal_executable,
            previous_executable=args.previous_executable,
            output=args.output,
            receipt_path=args.receipt_path,
            build_id=args.build_id,
            product_version=args.product_version,
            previous_product_version=args.previous_product_version,
            schema_before=args.schema_before,
            schema_after=args.schema_after,
            previous_max_schema=args.previous_max_schema,
            candidate_min_schema=args.candidate_min_schema,
            candidate_max_schema=args.candidate_max_schema,
        )
        print(json.dumps(bundle, ensure_ascii=False, sort_keys=True))
    elif args.command == 'check-rollback':
        verified = verify_release_bundle(args.root, args.bundle, expected_bundle_sha256=args.expected_sha256)
        bundle = verified['bundle']
        if bundle['rollback'].get('mode') != 'catalog-guarded':
            raise ReleaseBundleError('此入口仅检查目录受限回退')
        acceptance = _read_json(_declared_path(args.root, bundle['acceptance']['path'], label='acceptance'))
        result = check_catalog_rollback(args.root / 'data' / 'cuoti.db',
                                        acceptance['catalog_guarded_rollback']['new_method_codes'],
                                        bundle['schema_compatibility']['formal_before'])
        print(json.dumps(result, ensure_ascii=False))
    elif args.command == "verify":
        result = verify_release_bundle(
            args.root,
            args.bundle,
            expected_bundle_sha256=args.expected_sha256,
            expected_candidate=args.expected_candidate,
            expected_provenance=args.expected_provenance,
            expected_product_version=args.expected_product_version,
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "bundle_sha256": result["bundle_sha256"],
                },
                sort_keys=True,
            )
        )
    else:
        result = publish_release_bundle(
            args.root,
            args.bundle,
            expected_bundle_sha256=args.expected_sha256,
            expected_candidate=args.expected_candidate,
            expected_provenance=args.expected_provenance,
            expected_product_version=args.expected_product_version,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
