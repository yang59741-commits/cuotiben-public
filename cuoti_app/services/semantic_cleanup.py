"""Conservative semantic-index cleanup preview and quarantine execution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..transactions import immediate_transaction


CLEANUP_SCHEMA = "semantic-index-cleanup-preview-v1"
CLEANUP_CONFIRMATION = "QUARANTINE_SEMANTIC_INDEX_CLEANUP_V1"


class SemanticCleanupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _ids(rows: Any) -> list[Any]:
    return [row[0] for row in rows]


def _manifest_protection(db: Any) -> tuple[set[str], set[str], set[int]]:
    snapshots: set[str] = set()
    projections: set[str] = set()
    cards: set[int] = set()
    for row in db.execute("SELECT manifest_json FROM exam_generation_manifests"):
        try:
            manifest = json.loads(str(row[0]))
            payload = manifest.get("freeze_payload", manifest)
            if not isinstance(payload, dict):
                raise ValueError
            snapshot = payload.get("semantic_snapshot_id")
            projection = payload.get("projection_id")
            if snapshot and snapshot != "unavailable":
                snapshots.add(str(snapshot))
            if projection and projection != "unavailable":
                projections.add(str(projection))
            decisions = payload.get("decisions", [])
            if not isinstance(decisions, list):
                raise ValueError
            for decision in decisions:
                if isinstance(decision, dict) and isinstance(decision.get("card_version_id"), int):
                    cards.add(int(decision["card_version_id"]))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise SemanticCleanupError(
                "MANIFEST_UNREADABLE", "存在无法验证的历史组卷 manifest，清理预览已关闭"
            ) from exc
    return snapshots, projections, cards


def build_cleanup_preview(db: Any, *, retention: dict[str, int]) -> dict[str, Any]:
    manifest_snapshots, manifest_projections, manifest_cards = _manifest_protection(db)
    protected_snapshots = set(manifest_snapshots)
    protected_projections = set(manifest_projections)
    protected_cards = set(manifest_cards)
    protected_snapshots.update(
        str(row[0]) for row in db.execute(
            "SELECT published_snapshot_id FROM semantic_index_runs WHERE published_snapshot_id IS NOT NULL"
        )
    )
    protected_cards.update(
        int(row[0]) for row in db.execute(
            "SELECT current_card_version_id FROM semantic_questions WHERE current_card_version_id IS NOT NULL"
        )
    )
    latest_projection = db.execute(
        "SELECT id FROM semantic_recall_projection_versions WHERE status='published' "
        "ORDER BY published_at DESC,created_at DESC,id DESC LIMIT 1"
    ).fetchone()
    if latest_projection:
        protected_projections.add(str(latest_projection[0]))
    protected_snapshots.update(
        str(row[0]) for row in db.execute(
            "SELECT snapshot_id FROM semantic_recall_projection_versions"
        )
    )
    protected_cards.update(
        int(row[0]) for row in db.execute("SELECT card_version_id FROM semantic_index_snapshot_items")
    )
    protected_cards.update(
        int(row[0]) for row in db.execute("SELECT card_version_id FROM semantic_recall_documents")
    )

    cards = _ids(db.execute("SELECT id FROM semantic_card_versions WHERE status='superseded' ORDER BY id"))
    cards = [int(value) for value in cards if int(value) not in protected_cards]
    partial = _ids(db.execute(
        "SELECT id FROM semantic_index_snapshots WHERE status='partial' ORDER BY published_at DESC,id DESC"
    ))
    keep_snapshots = int(retention.get("snapshot_history_count", 3))
    partial = [str(value) for value in partial[keep_snapshots:] if str(value) not in protected_snapshots]
    projections = _ids(db.execute(
        "SELECT id FROM semantic_recall_projection_versions ORDER BY created_at DESC,id DESC"
    ))
    projections = [str(value) for value in projections if str(value) not in protected_projections]
    runs = _ids(db.execute(
        "SELECT id FROM semantic_index_runs WHERE status IN ('succeeded','failed','cancelled') "
        "AND published_snapshot_id IS NULL ORDER BY finished_at DESC,created_at DESC,id DESC"
    ))
    runs = [
        str(value) for value in runs
        if not db.execute("SELECT 1 FROM semantic_index_snapshots WHERE run_id=?", (value,)).fetchone()
    ]
    items = {
        "superseded_cards": cards,
        "partial_snapshots": partial,
        "old_projections": projections,
        "terminal_runs": runs,
        "private_material_cache": [],
    }
    identity = {"schema": CLEANUP_SCHEMA, "items": items}
    return {
        **identity,
        "preview_hash": _hash(identity),
        "counts": {name: len(values) for name, values in items.items()},
        "confirmation": CLEANUP_CONFIRMATION,
        "destructive": True,
        "permanent_delete": False,
    }


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SemanticCleanupError("BACKUP_INVALID", "清理备份 quick_check 失败")
        if dst.execute("PRAGMA foreign_key_check").fetchall():
            raise SemanticCleanupError("BACKUP_INVALID", "清理备份外键检查失败")


def quarantine_cleanup(
    db: Any,
    *,
    database_path: str | Path,
    quarantine_root: str | Path,
    retention: dict[str, int],
    preview_hash: str,
    confirmation: str,
) -> dict[str, Any]:
    if confirmation != CLEANUP_CONFIRMATION:
        raise SemanticCleanupError("CONFIRMATION_REQUIRED", "清理确认串无效")
    active = db.execute(
        "SELECT 1 FROM background_jobs WHERE status IN ('queued','running','cancel_requested') "
        "AND (kind='semantic-index-refresh' OR kind LIKE 'exam%') LIMIT 1"
    ).fetchone()
    if active:
        raise SemanticCleanupError("ACTIVE_JOB_CONFLICT", "存在活动语义或组卷任务")
    preview = build_cleanup_preview(db, retention=retention)
    if preview["preview_hash"] != preview_hash:
        raise SemanticCleanupError("PREVIEW_CHANGED", "清理候选已变化，请重新预览")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(quarantine_root) / f"semantic-cleanup-{stamp}-{preview_hash[:12]}"
    root.mkdir(parents=True, exist_ok=False)
    backup = root / "database-before.sqlite3"
    _backup_database(Path(database_path), backup)
    (root / "PREVIEW.json").write_bytes(_canonical(preview) + b"\n")
    items = preview["items"]
    with immediate_transaction(db):
        if items["old_projections"] and db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='semantic_recall_fts'"
        ).fetchone():
            db.executemany(
                "DELETE FROM semantic_recall_fts WHERE projection_id=?",
                ((projection_id,) for projection_id in items["old_projections"]),
            )
        for table, key in (
            ("semantic_card_versions", "superseded_cards"),
            ("semantic_recall_projection_versions", "old_projections"),
            ("semantic_index_snapshots", "partial_snapshots"),
            ("semantic_index_runs", "terminal_runs"),
        ):
            for identifier in items[key]:
                db.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise SemanticCleanupError("FOREIGN_KEY_FAILURE", "清理后外键检查失败")
    receipt = {
        "schema": "semantic-index-cleanup-quarantine-v1",
        "preview_hash": preview_hash,
        "deleted_id_hash": _hash(items),
        "counts": preview["counts"],
        "recovery_location": str(root),
        "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
        "permanent_delete": False,
    }
    (root / "RECEIPT.json").write_bytes(_canonical(receipt) + b"\n")
    return receipt
