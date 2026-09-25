"""One entry point for the public project's synthetic test suite."""
from pathlib import Path
import argparse
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
QUICK = ("tests/test_public_startup.py", "tests/test_startup.py",
         "tests/test_spaced_review_summary.py", "tests/test_review_dual_mode.py",
         "tests/test_daily_review_history_preservation.py", "tests/test_formal_runtime.py",
         "tests/test_service_startup_coordination.py")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--full", action="store_true", help="run every public Python test file")
    parser.add_argument("--output-dir", type=Path, default=ROOT/"output/checks")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ, PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    python = [sys.executable, "-X", "utf8", "-B"]
    suite = python + ["tools/run_python_test_shards.py", "--per-file", "--keep-going",
                       "--output-dir", str(args.output_dir/"python")]
    if not args.full:
        suite.extend(QUICK)
    commands = [suite, python+["tools/check_frontend.py"], python+["tools/check_mathjax_assets.py"]]
    failed = False
    for index, command in enumerate(commands, 1):
        with (args.output_dir/f"step-{index}.log").open("w", encoding="utf-8") as log:
            result = subprocess.run(command, cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT)
        print(f"step {index}/{len(commands)}: {'PASS' if result.returncode == 0 else 'FAIL'}; log={args.output_dir/f'step-{index}.log'}", flush=True)
        failed = failed or result.returncode != 0
    return int(failed)

if __name__ == "__main__":
    raise SystemExit(main())
