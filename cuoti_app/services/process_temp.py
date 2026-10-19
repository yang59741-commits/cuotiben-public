"""Route Python and SQLite temporary files to an application-writable directory."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def configure_process_temp(runtime_root: Path) -> Path:
    """Create and select runtime_root/temp for this process and its children."""

    temp_dir = (Path(runtime_root).resolve() / "temp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = str(temp_dir)
    os.environ["TEMP"] = temp_path
    os.environ["TMP"] = temp_path
    tempfile.tempdir = temp_path
    return temp_dir
