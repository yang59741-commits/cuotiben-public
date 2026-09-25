"""Crash-safe exam artifact publication, recovery, and deterministic recompilation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import uuid
from pathlib import Path

from ..database import connect_database
from ..utils import file_sha256
from .exam_snapshots import ExamSnapshotError, extract_exam_item_snapshots
from .tex import compile_latex_artifact
from .exam_generation_compat import PipelineIdentity

logger = logging.getLogger(__name__)
_DELETE_DIR = re.compile(r"\.delete-exam-([1-9]\d*)-[0-9a-f]{32}\Z")
_RECOMPILE_DIR = re.compile(r"\.recompile-exam-([1-9]\d*)-[0-9a-f]{32}\Z")
_RECOMPILE_LOCKS: dict[tuple[str, int], threading.Lock] = {}
_RECOMPILE_LOCKS_GUARD = threading.Lock()

def _sha256(path: Path) -> str:
    return file_sha256(path)


def _expected_batch_size(row) -> int:
    try:
        config = json.loads(row["config"] or "{}")
        value = int(config.get("batch_size") or 1) if isinstance(config, dict) else 1
    except (TypeError, ValueError, json.JSONDecodeError):
        value = 1
    return max(1, min(value, 20))


def _artifact_pair(root: Path, exam_id: int) -> tuple[Path, Path]:
    return root / f"exam_{exam_id}.tex", root / f"exam_{exam_id}.pdf"


def _recovery_proofs(row) -> bool:
    """Require explicit candidate identity and closed lineage for recovery."""
    try:
        config = json.loads(row["config"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(config, dict) or config.get("compatibility") == "legacy_read_only":
        return False
    identity = config.get("pipeline_identity")
    expected = PipelineIdentity().as_dict()
    if not isinstance(identity, dict) or set(identity) != set(expected):
        return False
    if any(identity.get(key) != value for key, value in expected.items()):
        return False
    return bool(config.get("lineage_closed"))


def _recompile_lock(root: Path, exam_id: int) -> threading.Lock:
    key = (str(root.resolve()), exam_id)
    with _RECOMPILE_LOCKS_GUARD:
        return _RECOMPILE_LOCKS.setdefault(key, threading.Lock())


def _path_hash(path: Path) -> str | None:
    return _sha256(path) if path.is_file() else None


def reconcile_exam_recompiles(database: str, pdf_dir: str) -> dict[str, int]:
    """Resolve interrupted recompile stages from database and file hashes."""

    root = Path(pdf_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database, role="maintenance")
    restored = completed = removed = uncertain = 0
    try:
        for staging in root.iterdir():
            match = _RECOMPILE_DIR.fullmatch(staging.name)
            if not match or not staging.is_dir():
                continue
            exam_id = int(match.group(1))
            try:
                state = json.loads((staging / "state.json").read_text(encoding="utf-8"))
                if state.get("schema_version") != 1 or state.get("exam_id") != exam_id:
                    raise ValueError("invalid recompile state")
                before = state["database_before"]
                new = state["new"]
                old = state["old_files"]
                if set(before) != {".tex", ".pdf"} or set(new) != {".tex", ".pdf"}:
                    raise ValueError("invalid recompile hashes")
                row = connection.execute(
                    "SELECT latex_sha256,pdf_sha256 FROM exams WHERE id=?", (exam_id,)
                ).fetchone()
                targets = {
                    suffix: root / f"exam_{exam_id}{suffix}"
                    for suffix in (".tex", ".pdf")
                }
                target_hashes = {
                    suffix: _path_hash(path) for suffix, path in targets.items()
                }
                if row is None:
                    if any(
                        value is not None and value != new[suffix]
                        for suffix, value in target_hashes.items()
                    ):
                        raise ValueError("unowned recompile target conflict")
                    for path in targets.values():
                        path.unlink(missing_ok=True)
                    shutil.rmtree(staging)
                    removed += 1
                    continue
                database_hashes = {".tex": row["latex_sha256"], ".pdf": row["pdf_sha256"]}
                if database_hashes == new:
                    if target_hashes != new:
                        raise ValueError("committed recompile target mismatch")
                    shutil.rmtree(staging)
                    completed += 1
                    continue
                if database_hashes != before:
                    raise ValueError("database no longer matches recompile stage")
                for suffix, target in targets.items():
                    metadata = old[suffix]
                    old_hash = metadata["sha256"]
                    if metadata["exists"]:
                        if target_hashes[suffix] == old_hash:
                            continue
                        backup = staging / f"old{suffix}"
                        if _path_hash(backup) != old_hash or target_hashes[suffix] not in {
                            None,
                            new[suffix],
                        }:
                            raise ValueError("old recompile artifact unavailable")
                        target.unlink(missing_ok=True)
                        os.replace(backup, target)
                    elif target_hashes[suffix] == new[suffix]:
                        target.unlink()
                    elif target_hashes[suffix] is not None:
                        raise ValueError("unexpected recompile artifact")
                shutil.rmtree(staging)
                restored += 1
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                uncertain += 1
                logger.warning("uncertain staged exam recompile preserved exam_id=%s", exam_id)
        return {
            "restored": restored,
            "completed": completed,
            "removed": removed,
            "uncertain": uncertain,
        }
    finally:
        connection.close()


def reconcile_exam_deletions(database: str, pdf_dir: str) -> dict[str, int]:
    """Restore staged deletes when the row survived; remove them when it did not."""

    root = Path(pdf_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database, role="maintenance")
    restored = removed = conflicts = 0
    try:
        for staging in root.iterdir():
            match = _DELETE_DIR.fullmatch(staging.name)
            if not match or not staging.is_dir():
                continue
            exam_id = int(match.group(1))
            row = connection.execute(
                "SELECT latex_sha256,pdf_sha256 FROM exams WHERE id=?", (exam_id,)
            ).fetchone()
            if not row:
                shutil.rmtree(staging)
                removed += 1
                continue
            for suffix, expected_key in ((".tex", "latex_sha256"), (".pdf", "pdf_sha256")):
                staged = staging / f"exam_{exam_id}{suffix}"
                if not staged.is_file():
                    continue
                target = root / staged.name
                if not target.exists():
                    os.replace(staged, target)
                    restored += 1
                    continue
                staged_hash = _sha256(staged)
                target_hash = _sha256(target)
                expected = row[expected_key]
                if staged_hash == target_hash:
                    staged.unlink()
                elif expected and staged_hash == expected and target_hash != expected:
                    conflict = staging / f"conflict-current-{target.name}"
                    os.replace(target, conflict)
                    os.replace(staged, target)
                    restored += 1
                    conflicts += 1
                    logger.warning(
                        "exam artifact conflict preserved exam_id=%s suffix=%s", exam_id, suffix
                    )
                else:
                    conflicts += 1
                    logger.warning(
                        "uncertain staged exam delete preserved exam_id=%s suffix=%s",
                        exam_id, suffix,
                    )
            if not any(staging.iterdir()):
                staging.rmdir()
        return {"restored": restored, "removed": removed, "conflicts": conflicts}
    finally:
        connection.close()


def reconcile_exam_artifacts(database: str, pdf_dir: str) -> dict[str, int]:
    """Finish complete publishing batches and remove incomplete batches."""

    root = Path(pdf_dir)
    root.mkdir(parents=True, exist_ok=True)
    connection = connect_database(database, role="maintenance")
    recovered = removed = backfilled = items_backfilled = uncertain = 0
    try:
        rows = connection.execute(
            "SELECT * FROM exams e WHERE e.artifact_status<>'published' "
            "OR e.latex_source IS NULL OR e.latex_sha256 IS NULL "
            "OR e.pdf_sha256 IS NULL OR COALESCE(e.recompile_available,0)<>1 "
            "OR NOT EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=e.id) "
            "ORDER BY e.id"
        ).fetchall()
        all_job_groups: dict[str, list] = {}
        publishing_groups: dict[str, list] = {}
        job_ids = sorted({str(row["generation_job_id"]) for row in rows if row["generation_job_id"]})
        if job_ids:
            placeholders = ",".join("?" for _ in job_ids)
            for sibling in connection.execute(
                f"SELECT * FROM exams WHERE generation_job_id IN ({placeholders}) ORDER BY id",
                job_ids,
            ).fetchall():
                all_job_groups.setdefault(str(sibling["generation_job_id"]), []).append(sibling)
        for row in rows:
            if row["artifact_status"] == "publishing":
                key = row["generation_job_id"] or f"exam:{row['id']}"
                publishing_groups.setdefault(key, []).append(row)
            elif row["artifact_status"] == "published":
                has_items = connection.execute(
                    "SELECT 1 FROM exam_items WHERE exam_id=? LIMIT 1", (row["id"],)
                ).fetchone()
                if not has_items:
                    # Historical rows are audit-only; do not synthesize
                    # exam_items unless immutable identity and lineage proofs
                    # are present.
                    if not _recovery_proofs(row):
                        uncertain += 1
                        continue
                    try:
                        config = json.loads(row["config"] or "{}")
                    except (TypeError, json.JSONDecodeError):
                        config = {}
                    if isinstance(config, dict) and config.get("source") == "past":
                        uncertain += 1
                        logger.warning(
                            "published past exam requires explicit item repair exam_id=%s",
                            row["id"],
                        )
                        continue
                    created_at = row["created_at"] or ""
                    try:
                        snapshots = extract_exam_item_snapshots(row["content"])
                    except ExamSnapshotError:
                        uncertain += 1
                        logger.warning(
                            "published exam item snapshot is ambiguous exam_id=%s",
                            row["id"],
                        )
                        continue
                    for position, content in enumerate(snapshots, start=1):
                        connection.execute(
                            "INSERT INTO exam_items(public_id,exam_id,position,content,"
                            "content_hash,source_kind,source_metadata_json,created_at) "
                            "VALUES(?,?,?,?,?,'legacy_snapshot',?,?)",
                            (
                                str(uuid.uuid4()), row["id"], position, content,
                                hashlib.sha256(content.encode("utf-8")).hexdigest(),
                                '{"backfilled_from":"exams.content"}', created_at,
                            ),
                        )
                        items_backfilled += 1
                tex_path, pdf_path = _artifact_pair(root, row["id"])
                if tex_path.is_file() and pdf_path.is_file() and pdf_path.stat().st_size:
                    try:
                        source = tex_path.read_text(encoding="utf-8")
                        pdf_hash = _sha256(pdf_path)
                    except (OSError, UnicodeError):
                        uncertain += 1
                        logger.warning("published exam artifacts are unreadable exam_id=%s", row["id"])
                        continue
                    connection.execute(
                        "UPDATE exams SET latex_source=COALESCE(latex_source,?),"
                        "latex_sha256=?,pdf_sha256=?,recompile_available=1 WHERE id=?",
                        (
                            source,
                            hashlib.sha256(source.encode("utf-8")).hexdigest(),
                            pdf_hash,
                            row["id"],
                        ),
                    )
                    backfilled += int(row["latex_source"] is None)

        for job_id, group in all_job_groups.items():
            expected = max(_expected_batch_size(row) for row in group)
            if job_id in publishing_groups or len(group) < expected:
                publishing_groups[job_id] = group

        for _key, group in publishing_groups.items():
            if not all(_recovery_proofs(row) for row in group):
                uncertain += 1
                continue
            expected = max(_expected_batch_size(row) for row in group)
            complete = len(group) == expected
            group_uncertain = False
            hashes = {}
            for row in group:
                tex_path, pdf_path = _artifact_pair(root, row["id"])
                source = row["latex_source"]
                expected_tex = row["latex_sha256"]
                try:
                    valid = (
                        source and expected_tex and tex_path.is_file()
                        and pdf_path.is_file() and pdf_path.stat().st_size > 0
                        and _sha256(tex_path) == expected_tex
                    )
                    if not valid:
                        complete = False
                        break
                    hashes[row["id"]] = _sha256(pdf_path)
                except OSError:
                    group_uncertain = True
                    uncertain += 1
                    logger.warning("publishing exam artifacts are unreadable exam_id=%s", row["id"])
                    break
            if group_uncertain:
                continue
            if complete:
                for row in group:
                    connection.execute(
                        "UPDATE exams SET artifact_status='published',pdf_sha256=?,"
                        "recompile_available=1 WHERE id=?",
                        (hashes[row["id"]], row["id"]),
                    )
                    recovered += 1
            else:
                for row in group:
                    for path in _artifact_pair(root, row["id"]):
                        path.unlink(missing_ok=True)
                    connection.execute("DELETE FROM exams WHERE id=?", (row["id"],))
                    removed += 1
        connection.commit()
        return {
            "recovered": recovered,
            "removed": removed,
            "backfilled": backfilled,
            "items_backfilled": items_backfilled,
            "uncertain": uncertain,
        }
    finally:
        connection.close()


def recompile_exam(
    pdf_dir: str,
    command: str,
    exam_id: int,
    latex_source: str,
    *,
    timeout: int = 60,
    runner=None,
    on_compiled=None,
    publish=None,
    previous_hashes: dict[str, str | None] | None = None,
) -> dict[str, str]:
    """Compile in isolation, replace the pair, and compensate publish failures."""

    root = Path(pdf_dir)
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".recompile-exam-{exam_id}-{uuid.uuid4().hex}"
    staging.mkdir()
    temporary_stem = "candidate"
    compiled = {suffix: staging / f"{temporary_stem}{suffix}" for suffix in (".tex", ".pdf")}
    targets = {suffix: root / f"exam_{exam_id}{suffix}" for suffix in (".tex", ".pdf")}
    cleanup_staging = True
    try:
        compile_latex_artifact(
            str(staging), command, temporary_stem, latex_source,
            timeout=timeout, runner=runner, publish_source=True,
        )
        hashes = {
            "latex_sha256": hashlib.sha256(latex_source.encode("utf-8")).hexdigest(),
            "pdf_sha256": _sha256(compiled[".pdf"]),
        }
        new_hashes = {".tex": hashes["latex_sha256"], ".pdf": hashes["pdf_sha256"]}
        database_before = previous_hashes or {".tex": None, ".pdf": None}
        old_files = {
            suffix: {"exists": target.is_file(), "sha256": _path_hash(target)}
            for suffix, target in targets.items()
        }
        state = {
            "schema_version": 1,
            "exam_id": exam_id,
            "database_before": database_before,
            "new": new_hashes,
            "old_files": old_files,
        }
        state_tmp = staging / "state.tmp"
        state_tmp.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        os.replace(state_tmp, staging / "state.json")
        if on_compiled is not None:
            on_compiled()
        with _recompile_lock(root, exam_id):
            backed_up = []
            installed = []
            cleanup_staging = False
            try:
                for suffix, target in targets.items():
                    if target.exists():
                        backup = staging / f"old{suffix}"
                        os.replace(target, backup)
                        backed_up.append((target, backup))
                for suffix, target in targets.items():
                    os.replace(compiled[suffix], target)
                    installed.append(target)
                if publish is not None:
                    publish(hashes)
            except Exception as original:
                try:
                    for target in installed:
                        target.unlink(missing_ok=True)
                    for target, backup in reversed(backed_up):
                        if backup.exists():
                            os.replace(backup, target)
                except Exception as restore_error:
                    raise RuntimeError(
                        "recompile publish failed and old artifacts need startup recovery"
                    ) from restore_error
                cleanup_staging = True
                raise original
            cleanup_staging = True
        return hashes
    finally:
        if cleanup_staging:
            shutil.rmtree(staging, ignore_errors=True)
