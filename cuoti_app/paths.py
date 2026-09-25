"""Resolve bundled resources separately from user-owned application data."""

from __future__ import annotations

import sys
from pathlib import Path


SOURCE_ROOT = Path(__file__).resolve().parent.parent


def resolve_data_root(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
) -> Path:
    """Return the directory that owns writable data such as the database."""

    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not frozen:
        return SOURCE_ROOT
    executable = executable or sys.executable
    return Path(executable).resolve().parent


def resolve_resource_root(
    *,
    frozen: bool | None = None,
    bundle_dir: str | Path | None = None,
) -> Path:
    """Return the read-only source or PyInstaller extraction directory."""

    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if not frozen:
        return SOURCE_ROOT
    bundle_dir = bundle_dir or getattr(sys, "_MEIPASS", SOURCE_ROOT)
    return Path(bundle_dir).resolve()


DATA_DIR = resolve_data_root()
RESOURCE_DIR = resolve_resource_root()
INSTANCE_DIR = DATA_DIR / "instance"
