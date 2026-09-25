import json
import subprocess
from unittest.mock import patch

from tools.run_python_test_shards import run_shards


def test_keep_going_records_failure_crash_and_unexecuted_separately(tmp_path):
    results = [
        subprocess.CompletedProcess([], 1, "1 failed, 2 passed", ""),
        subprocess.CompletedProcess([], 3221225477, "", "native crash"),
        subprocess.CompletedProcess([], 0, "3 passed", ""),
    ]
    with patch("tools.run_python_test_shards.subprocess.run", side_effect=results) as run:
        assert run_shards(per_file=True, keep_going=True, output_dir=tmp_path,
                          test_files=["a.py", "b.py", "c.py"]) == 1
    report = json.loads((tmp_path / "summary.json").read_text())
    assert [row["status"] for row in report["shards"]] == ["failed", "crashed", "passed"]
    assert report["passed_tests"] == 5
    assert report["unexecuted"] == []
    assert [call.args[0][-1] for call in run.call_args_list] == ["a.py", "b.py", "c.py"]


def test_timeout_preserves_output_and_fail_fast_inventory(tmp_path):
    timeout = subprocess.TimeoutExpired("pytest", 900, output=b"before timeout", stderr=b"diagnostic")
    with patch("tools.run_python_test_shards.subprocess.run", side_effect=timeout):
        assert run_shards(per_file=True, output_dir=tmp_path,
                          test_files=["a.py", "b.py"]) == 1
    report = json.loads((tmp_path / "summary.json").read_text())
    assert report["shards"][0]["status"] == "timeout"
    assert report["unexecuted"] == ["b.py"]
    assert (tmp_path / "shard-001.log").read_text() == "before timeoutdiagnostic"
