"""Command-line wrapper for the production environment diagnostics."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cuoti_app.services.environment_check import (
    CheckResult,
    EnvironmentReport,
    collect_checks,
)


def main() -> int:
    report = collect_checks()
    labels = {"ok": "通过", "warning": "提示", "error": "失败"}
    for item in report.checks:
        print(f"[{labels[item.level]}] {item.message}")

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
