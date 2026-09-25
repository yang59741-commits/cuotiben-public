"""Deterministically validate every maintained frontend source and contract."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_JS = ROOT / "cuoti_app" / "static" / "js"
TESTS = ROOT / "tests"


def maintained_javascript() -> tuple[Path, ...]:
    return tuple(
        path
        for path in sorted(STATIC_JS.rglob("*.js"))
        if "vendor" not in path.relative_to(STATIC_JS).parts
    )


def node_contracts() -> tuple[Path, ...]:
    return tuple(sorted(TESTS.glob("*_contract.js")))


def _run(command: list[str]) -> None:
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        raise SystemExit(int(result.returncode))


def main() -> int:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    _run([npm, "run", "lint:js"])
    for path in maintained_javascript():
        _run(["node", "--check", str(path.relative_to(ROOT))])
    for path in node_contracts():
        _run(["node", str(path.relative_to(ROOT))])
    print(
        f"FRONTEND_CHECK_OK sources={len(maintained_javascript())} "
        f"contracts={len(node_contracts())}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
