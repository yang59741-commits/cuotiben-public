"""Incident retention, process isolation, and truthful diagnostic notices."""
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import launcher
from cuoti_app.services import diagnostic_evidence as evidence
from cuoti_app.services import runtime_diagnostics as diagnostics


def run_record(run_id, *, role="service", started="2026-09-29T00:00:00+00:00"):
    return {"run_id": run_id, "role": role, "build": "fixture-build", "pid": 10,
            "created": 20, "entry": "fixture", "started": started, "state": "running"}


def test_concurrent_reservations_respect_shared_count_and_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_CAPTURES", 3)
    monkeypatch.setattr(evidence, "MAX_CAPTURE_BYTES", 45 * 1024 * 1024)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: evidence.reserve_capture(
            tmp_path, run_record(f"run-{n}"), {}, native=True), range(4)))
    kept = [row for row in results if row.get("directory")]
    assert kept
    assert sum(row["reserved_bytes"] for row in kept) <= evidence.MAX_CAPTURE_BYTES
    assert len(kept) <= evidence.MAX_CAPTURES
    assert all(row.get("reason") == "capacity_reached" for row in results if not row.get("directory"))
    count, reserved = evidence.capture_usage(tmp_path)
    assert count == len(kept) and reserved <= evidence.MAX_CAPTURE_BYTES


def test_capacity_reached_preserves_old_files_and_never_invokes_dump(tmp_path, monkeypatch):
    folder = tmp_path / "first-crash"
    folder.mkdir()
    (folder / "capture.json").write_text('{"run_id":"old","dump_status":"saved"}')
    (folder / "native.dmp").write_bytes(b"irreplaceable")
    monkeypatch.setattr(evidence, "MAX_CAPTURES", 1)
    from cuoti_app.services.native_crash_monitor import DebugEvent, preserve_crash
    with patch("cuoti_app.services.native_crash_monitor.bounded_dump") as dump:
        result = preserve_crash(tmp_path, "new", DebugEvent(), None, None,
                                evidence.Journal(tmp_path / "monitor.jsonl"))
    assert result["reason"] == "capacity_reached"
    dump.assert_not_called()
    assert (folder / "native.dmp").read_bytes() == b"irreplaceable"
    assert evidence.read_json(folder / "capture.json")["run_id"] == "old"
    assert "本次仅保留异常摘要" in diagnostics.diagnostic_notice(tmp_path)


def test_fault_context_is_frozen_and_copies_the_latest_tail(tmp_path, monkeypatch):
    channel = tmp_path / "active/slot-0"
    channel.mkdir(parents=True)
    run = run_record("incident")
    evidence.write_json(channel / "current-run.json", run)
    (channel / "python-current.log").write_bytes(b"old line\n" * 100 + b"latest crash stack\n")
    (channel / "operations.jsonl").write_text('last operation: recover_stale_jobs\n')
    monkeypatch.setattr(evidence, "MAX_STACK_BYTES", 64)
    receipt = evidence.reserve_capture(tmp_path, run, {"exception_type": "RuntimeError"}, native=False)
    result = evidence.snapshot_context(channel, tmp_path, receipt, finished=True)
    frozen = tmp_path / receipt["directory"]
    stack = (frozen / "python-current.log").read_bytes()
    assert len(stack) == 64 and stack.endswith(b"latest crash stack\n")
    assert result["state"] == "frozen"
    (channel / "python-current.log").write_text("later successful startup")
    assert evidence.snapshot_context(channel, tmp_path, result, finished=True) == result
    assert (frozen / "python-current.log").read_bytes() == stack


def test_python_error_can_gain_a_native_dump_during_same_process_teardown(tmp_path):
    run = run_record("same-run")
    python = evidence.reserve_capture(tmp_path, run, {"exception_type": "RuntimeError"}, native=False)
    native = evidence.reserve_capture(tmp_path, run, {"exception_code": "0x80000003"}, native=True)
    assert native["directory"] == python["directory"]
    assert native["exception_type"] == "RuntimeError" and native["exception_code"] == "0x80000003"
    assert native["dump_status"] == "collecting" and native["limit_bytes"] > 0
    assert evidence.capture_usage(tmp_path)[0] == 1


def test_unreadable_diagnostic_directory_reports_failure_without_raising(tmp_path):
    with patch.object(diagnostics, "capture_directories", side_effect=PermissionError("fixture")):
        assert "读取取证状态失败" in diagnostics.diagnostic_notice(tmp_path)


def test_retiring_observer_cannot_overwrite_new_service_state(tmp_path):
    channel = tmp_path / "active/slot-0"
    channel.mkdir(parents=True)
    evidence.write_json(channel / "current-run.json", run_record("old"))
    evidence.write_json(tmp_path / "current-run.json", run_record("new"))
    evidence.write_json(tmp_path / "last-exit.json", {"run_id": "new", "unexpected": False})
    diagnostics.publish_state(channel, tmp_path, "last-exit.json", {"run_id": "old", "unexpected": True})
    assert evidence.read_json(tmp_path / "last-exit.json")["run_id"] == "new"
    assert evidence.read_json(channel / "last-exit.json")["run_id"] == "old"
    evidence.write_json(channel / "current-run.json", run_record("reused"))
    evidence.write_json(channel / "monitor-ready.json", {"run_id": "reused", "status": "active"})
    diagnostics.publish_state(channel, tmp_path, "monitor-ready.json", {"run_id": "old", "status": "unavailable"})
    assert evidence.read_json(channel / "monitor-ready.json")["run_id"] == "reused"


def test_channels_do_not_reuse_a_retiring_observers_files(tmp_path):
    channel = tmp_path / "active/slot-0"
    channel.mkdir(parents=True)
    evidence.write_json(channel / "current-run.json", run_record("old"))
    evidence.write_json(channel / "monitor-ready.json", {"run_id": "old", "pid": 11, "created": 30})
    session = diagnostics.DiagnosticSession(tmp_path, "gui", role="launcher")
    with patch.object(diagnostics, "process_matches", side_effect=lambda pid, created: pid == 11 and created == 30):
        session._claim_channel()
    try:
        assert session.directory.name == "slot-1"
        assert evidence.read_json(channel / "current-run.json")["run_id"] == "old"
    finally:
        session.guard.release()


def test_stop_request_targets_service_channel_and_keeps_gui_independent(tmp_path):
    service, gui = tmp_path / "active/slot-0", tmp_path / "active/slot-1"
    service.mkdir(parents=True)
    gui.mkdir()
    record = {**run_record("service"), "channel": "active/slot-0", "evidence_root": str(tmp_path)}
    evidence.write_json(tmp_path / "current-run.json", record)
    evidence.write_json(service / "current-run.json", record)
    evidence.write_json(gui / "current-run.json", run_record("gui", role="launcher"))
    diagnostics.mark_expected_stop(tmp_path)
    assert evidence.read_json(service / "stop-request.json") == {"run_id": "service"}
    assert not (gui / "stop-request.json").exists()
    diagnostics.mark_expected_stop(service)
    assert evidence.read_json(service / "stop-request.json") == {"run_id": "service"}
    assert not (service / "active").exists()


def test_latest_incident_notice_does_not_hide_observer_failure(tmp_path):
    folder = tmp_path / "first-crash"
    folder.mkdir()
    evidence.write_json(folder / "capture.json", {
        "run_id": "old", "time": "2026-09-27T00:00:00+00:00", "dump_status": "saved"})
    evidence.write_json(folder / "current-run.json", {**run_record("old"), "build": "legacy-build"})
    evidence.write_json(tmp_path / "current-run.json", run_record("current"))
    evidence.write_json(tmp_path / "monitor-ready.json", {"run_id": "current", "status": "unavailable"})
    with patch.object(diagnostics, "process_matches", side_effect=lambda pid, created: pid == 10):
        notice = diagnostics.diagnostic_notice(tmp_path)
    assert "历史异常" in notice and "时间未记录" not in notice
    assert "legacy-build" in notice and "已保留崩溃现场" in notice
    assert "后台服务当前原生取证未就绪" in notice


def test_new_gui_crash_is_displayed_instead_of_first_historical_crash(tmp_path):
    old = tmp_path / "first-crash"
    recent = tmp_path / "incidents/gui"
    old.mkdir()
    recent.mkdir(parents=True)
    evidence.write_json(old / "capture.json", {
        **run_record("old"), "time": "2026-09-27T00:00:00+00:00", "dump_status": "saved"})
    evidence.write_json(recent / "capture.json", {
        **run_record("gui", role="launcher"), "time": "2026-09-29T00:00:00+00:00",
        "exception_code": "0x80000003", "dump_status": "saved", "heap_omitted": True})
    notice = diagnostics.diagnostic_notice(tmp_path)
    assert "启动器界面" in notice and "0x80000003" in notice and "堆信息不完整" in notice


def test_operation_journal_is_bounded_and_records_thread_and_run(tmp_path):
    journal = evidence.Journal(tmp_path / "operations.jsonl", max_bytes=400, backups=1,
                               context={"run_id": "fixture", "role": "service"})
    with patch.object(diagnostics, "_active", SimpleNamespace(operations=journal)):
        for n in range(30):
            diagnostics.diagnostic_operation("recover_stale_jobs", "connection_ready", connection_id=n)
    files = list(tmp_path.glob("operations.jsonl*"))
    assert len(files) == 2 and max(file.stat().st_size for file in files) < 800
    latest = json.loads(journal.path.read_text().splitlines()[-1])
    assert latest["connection_id"] == 29 and latest["thread_id"]
    assert latest["run_id"] == "fixture" and latest["role"] == "service"


def test_gui_initialization_and_mainloop_are_inside_diagnostic_session(tmp_path):
    order = []
    with (patch.object(launcher, "ROOT", tmp_path),
          patch.object(launcher, "diagnostic_session") as session,
          patch.object(launcher, "run_launcher_ui", side_effect=lambda: order.append("gui") or 0)):
        session.return_value.__enter__.side_effect = lambda: order.append("start")
        session.return_value.__exit__.side_effect = lambda *args: order.append("stop") or False
        assert launcher.main([]) == 0
    assert order == ["start", "gui", "stop"]
    session.assert_called_once_with(tmp_path / "data/runtime/diagnostics", "desktop-gui", role="launcher")


def test_gui_error_is_propagated_to_the_diagnostic_session(tmp_path):
    with (patch.object(launcher, "ROOT", tmp_path),
          patch.object(launcher, "diagnostic_session") as session,
          patch.object(launcher, "run_launcher_ui", side_effect=RuntimeError("fixture GUI initialization"))):
        session.return_value.__exit__.return_value = False
        with pytest.raises(RuntimeError):
            launcher.main([])
    assert session.return_value.__exit__.call_args.args[0] is RuntimeError
