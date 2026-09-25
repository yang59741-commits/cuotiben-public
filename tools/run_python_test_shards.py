"""Run the complete pytest file inventory in bounded deterministic processes."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
PASSED_PATTERN = re.compile(r"(\d+) passed")


def discover_test_files(root: Path = ROOT) -> tuple[str, ...]:
    """Return every default pytest module exactly once in stable path order."""

    return tuple(
        path.relative_to(root).as_posix()
        for path in sorted((root / "tests").glob("test_*.py"))
    )


def partition_test_files(
    files: Sequence[str], shard_count: int
) -> tuple[tuple[str, ...], ...]:
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    size = max(1, (len(files) + shard_count - 1) // shard_count)
    return tuple(
        tuple(files[index : index + size])
        for index in range(0, len(files), size)
    )


def run_shards(*, shard_count: int = 12, per_file: bool = False,
               keep_going: bool = False, output_dir: Path | None = None,
               test_files: Sequence[str] | None = None) -> int:
    files = tuple(test_files) if test_files else discover_test_files()
    if not files:
        print("PYTHON_TEST_SHARDS_ERROR no tests discovered", file=sys.stderr)
        return 2
    shards = tuple((name,) for name in files) if per_file else partition_test_files(files, shard_count)
    environment = dict(os.environ)
    environment["PYTHONUNBUFFERED"] = "1"
    environment["NO_COLOR"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
    passed = 0
    failed = False
    reports = []
    for index, shard in enumerate(shards, start=1):
        try:
            result = subprocess.run(
                (sys.executable, "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider", *shard),
                cwd=ROOT, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=900, check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            code = int(result.returncode)
            status = "passed" if code == 0 else "failed" if code == 1 else "error" if 2 <= code <= 5 else "crashed"
        except subprocess.TimeoutExpired as exc:
            def decoded(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
            output = decoded(exc.stdout) + decoded(exc.stderr)
            code, status = 124, "timeout"
        reports.append({"files": list(shard), "status": status, "returncode": code})
        failed = failed or code != 0
        print(f"python-tests-shard-{index:02d}/{len(shards):02d} {status} {','.join(shard)}", flush=True)
        if output_dir is not None:
            (output_dir / f"shard-{index:03d}.log").write_text(output, encoding="utf-8")
        else:
            print(output.rstrip())
        summaries = PASSED_PATTERN.findall(output)
        if summaries:
            passed += int(summaries[-1])
        if code and not keep_going:
            break
    if output_dir is not None:
        (output_dir / "summary.json").write_text(json.dumps({
            "python": sys.executable, "files": list(files), "shards": reports,
            "passed_tests": passed, "unexecuted": [f for group in shards[len(reports):] for f in group],
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"PYTHON_TEST_SHARDS_{'FAILED' if failed else 'OK'} "
        f"files={len(files)} shards={len(reports)}/{len(shards)} passed={passed}"
    )
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shards", type=int, default=12)
    parser.add_argument("--per-file", action="store_true")
    parser.add_argument("--keep-going", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("files", nargs="*", help="optional focused pytest files or node IDs")
    args = parser.parse_args()
    return run_shards(shard_count=args.shards, per_file=args.per_file,
                      keep_going=args.keep_going, output_dir=args.output_dir,
                      test_files=args.files)


if __name__ == "__main__":
    raise SystemExit(main())
