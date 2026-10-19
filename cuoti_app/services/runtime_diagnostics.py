"""Independent, bounded diagnostics for service and desktop GUI processes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import faulthandler
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from typing import NoReturn
import uuid

from .launcher_runtime import SingleInstanceGuard, process_identity, rotate_log
from .diagnostic_evidence import (
    Journal, MAX_CHANNELS, MAX_DUMP_BYTES, MAX_EVENT_BYTES, MAX_STACK_BYTES,
    MAX_CAPTURES, MAX_CAPTURE_BYTES, capture_directories, capture_usage,
    copy_evidence, publish_state, read_json, reserve_capture, snapshot_context,
    timestamp, write_json,
)
_active = None


def exception_details(error: BaseException) -> dict:
    details = {"exception_type": type(error).__name__, "message": str(error)}
    for name in ("errno", "winerror", "filename", "filename2", "sqlite_errorcode", "sqlite_errorname"):
        value = getattr(error, name, None)
        if value is not None:
            details[name] = str(value) if isinstance(value, (Path, bytes)) else value
    return details


def diagnostic_event(event: str, **details) -> None:
    session = _active
    if session is not None:
        session.journal.event(event, thread_id=threading.get_native_id(), **details)


def diagnostic_operation(operation: str, phase: str, **details) -> None:
    session = _active
    if session is not None:
        session.operations.event("database_operation", operation=operation, phase=phase,
                                 thread_id=threading.get_native_id(), **details)


def terminate_after_shutdown_timeout(error: BaseException) -> NoReturn:
    """End all writers before the service lease or fault handler can unwind."""
    session = _active
    try:
        stream = session.fault_file if session is not None and session.fault_file is not None else sys.stderr
        if stream is not None:
            traceback.print_exception(type(error), error, error.__traceback__, file=stream)
            stream.flush()
            faulthandler.dump_traceback(file=stream, all_threads=True)
        if sys.stderr is not None:
            print(f"服务停止超时，将在保持数据库互斥保护时结束进程：{error}",
                  file=sys.stderr, flush=True)
        if session is not None:
            session.journal.event("worker_shutdown_timeout", **exception_details(error))
            if session.run:
                session.run.update(state="shutdown_timeout", exception_type=type(error).__name__)
                publish_state(session.directory, session.root, "current-run.json", session.run)
    finally:
        # Normal unwinding releases the DB lock while daemon threads can still
        # write. The OS must instead stop those threads and release the lease
        # together. The independent observer remains alive to freeze evidence.
        os._exit(1)


def process_matches(pid, created) -> bool:
    identity = process_identity(pid) if pid else None
    return bool(identity and identity[1] == created)


def service_channel(root: Path, run: dict) -> Path:
    channel = run.get("channel")
    if channel in {f"active/slot-{number}" for number in range(MAX_CHANNELS)}:
        if root == Path(run.get("evidence_root", "")) / channel:
            return root
        return root / channel
    return root


def mark_expected_stop(directory: Path) -> None:
    run = read_json(directory / "current-run.json")
    if run.get("run_id"):
        try:
            publish_state(service_channel(directory, run), directory,
                          "stop-request.json", {"run_id": run["run_id"]})
        except OSError:
            # The native exception code still takes precedence over a stop request.
            pass


def diagnostic_notice(directory: Path) -> str:
    try:
        return _diagnostic_notice(directory)
    except OSError:
        return f"读取取证状态失败，请检查诊断目录权限：{directory}"


def diagnostic_notice_requires_attention(notice: str) -> bool:
    """Only open the launcher drawer automatically for current/actionable state."""
    lines = [line for line in notice.splitlines() if line.strip()]
    return bool(lines) and not (
        len(lines) == 1 and lines[0].startswith("历史异常：")
    )


def _diagnostic_notice(directory: Path) -> str:
    messages = []
    records = []
    for path in capture_directories(directory):
        capture = read_json(path / "capture.json")
        recorded_run = read_json(path / "current-run.json")
        if recorded_run.get("run_id") == capture.get("run_id"):
            capture = {**recorded_run, **capture}
        records.append(capture)
    latest_runs = {}
    channels = [directory] + [directory / f"active/slot-{n}" for n in range(MAX_CHANNELS)]
    for channel in channels:
        run = read_json(channel / "current-run.json")
        role = run.get("role", "service")
        if run.get("started", "") >= latest_runs.get(role, {}).get("started", ""):
            latest_runs[role] = run
        attempt = read_json(channel / "latest-capture.json")
        if attempt:
            records.append(attempt)
        previous = read_json(channel / "last-exit.json")
        if (previous.get("unexpected") and not previous.get("capture")
                and previous.get("exit_kind") != "control_termination"
                and (previous.get("exit_code") != "0xC000013A" or previous.get("native_failure"))):
            previous_build = run.get("build", "未知") if previous.get("run_id") == run.get("run_id") else "未知"
            records.append({**previous, "role": previous.get("role", role),
                            "build": previous.get("build", previous_build),
                            "dump_status": "missing"})
        if not process_matches(run.get("pid"), run.get("created")):
            continue
        label = "启动器界面" if role == "launcher" else "后台服务"
        ready = read_json(channel / "monitor-ready.json")
        active = (ready.get("run_id") == run.get("run_id") and ready.get("status") == "active"
                  and process_matches(ready.get("pid"), ready.get("created")))
        if not active:
            messages.append(f"{label}当前原生取证未就绪或观察者已退出，请查看诊断日志。")
    degraded = read_json(directory / "diagnostics-degraded.json")
    if process_matches(degraded.get("pid"), degraded.get("created")):
        label = "启动器界面" if degraded.get("role") == "launcher" else "后台服务"
        messages.append(f"{label}取证启动失败：{degraded.get('reason', '未知原因')}。")
    records = [item for item in records if item.get("run_id")]
    if records:
        latest = max(records, key=lambda item: item.get("time", ""))
        role = latest.get("role", "service")
        label = "启动器界面" if role == "launcher" else "后台服务"
        history = latest_runs.get(role, {}).get("started", "") > latest.get("time", "")
        try:
            when = datetime.fromisoformat(latest["time"]).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        except (KeyError, ValueError):
            when = "时间未记录"
        if latest.get("reason") == "capacity_reached":
            evidence = "取证容量已满，本次仅保留异常摘要；旧现场仍保留"
        elif latest.get("dump_status") == "saved":
            evidence = "已保留崩溃现场" + ("（小转储，堆信息不完整）" if latest.get("heap_omitted") else "（含原生转储）")
        elif latest.get("dump_status") == "not_requested":
            evidence = ("已保存 Python 异常栈和上下文" if latest.get("exception_type")
                        else "已保存退出上下文，未捕获原生转储")
        else:
            evidence = "已记录异常，原生转储未完整生成"
        if latest.get("context_errors"):
            evidence += "；部分上下文未能保存"
        messages.insert(0, f"{'历史异常' if history else '最近异常'}：{when}，{label}，"
                        f"版本 {latest.get('build', '未知')}，"
                        f"{latest.get('exception_code') or latest.get('exit_code') or latest.get('exception_type', '未知异常')}。"
                        f"{evidence}。诊断目录：{directory}")
    try:
        count, used = capture_usage(directory)
        if count >= MAX_CAPTURES or used >= MAX_CAPTURE_BYTES - 20 * 1024 * 1024:
            messages.append("故障留档份数或容量已达上限，后续故障仅记摘要；需审查现有现场后释放留档名额。")
    except OSError:
        messages.append("无法核实故障留档容量，请查看诊断目录权限。")
    return "\n".join(dict.fromkeys(messages))


def monitor_command(directory: Path, run: dict) -> list[str]:
    args = ["--pid", str(run["pid"]), "--created", str(run["created"]),
            "--directory", str(directory), "--run-id", run["run_id"],
            "--evidence-root", run["evidence_root"]]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--native-monitor", *args]
    return [sys.executable, "-B", "-m", "cuoti_app.services.native_crash_monitor", *args]


class DiagnosticSession:
    def __init__(self, directory: Path, entry: str, *, role="service"):
        self.root = self.directory = Path(directory).resolve()
        self.entry = entry
        self.role = role
        self.journal = Journal(self.directory / "runtime.jsonl")
        self.operations = Journal(self.directory / "operations.jsonl", max_bytes=256 * 1024, backups=1)
        self.fault_file = None
        self.monitor = None
        self.run = {}
        self.guard = None

    def _claim_channel(self) -> None:
        for number in range(MAX_CHANNELS):
            channel = self.root / f"active/slot-{number}"
            guard = SingleInstanceGuard(channel / "session.lock")
            if not guard.acquire():
                continue
            previous = read_json(channel / "current-run.json")
            ready = read_json(channel / "monitor-ready.json")
            owners = [(previous.get("pid"), previous.get("created")),
                      (previous.get("monitor_pid"), previous.get("monitor_created")),
                      (ready.get("pid"), ready.get("created"))]
            if any(process_matches(pid, created) for pid, created in owners):
                guard.release()
                continue
            self.directory, self.guard = channel, guard
            return
        raise OSError("并行取证会话已达上限")

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        notice = diagnostic_notice(self.root)
        if notice and sys.stderr is not None:
            print(notice, file=sys.stderr, flush=True)
        self._claim_channel()
        fault_path = self.directory / "python-current.log"
        rotate_log(fault_path, max_bytes=0, backups=2)
        self.fault_file = fault_path.open("a", encoding="utf-8", buffering=1)
        faulthandler.enable(file=self.fault_file, all_threads=True)
        identity = process_identity(os.getpid())
        from ..paths import RESOURCE_DIR
        try:
            with (RESOURCE_DIR / "cuoti_app/static/sw.js").open(encoding="utf-8") as source:
                first_line = source.readline()
            match = re.search(r"cuoti-shell-(\d{14})", first_line)
            build = match.group(1) if match else "source"
        except OSError:
            build = "unknown"
        self.run = {"run_id": uuid.uuid4().hex, "pid": os.getpid(), "role": self.role,
                    "created": identity[1] if identity else "", "entry": self.entry,
                    "build": build, "python": sys.version, "started": timestamp(), "state": "running",
                    "channel": self.directory.relative_to(self.root).as_posix(), "evidence_root": str(self.root)}
        context = {key: self.run[key] for key in ("run_id", "pid", "role", "build")}
        self.journal = Journal(self.directory / "runtime.jsonl", context=context)
        self.operations = Journal(self.directory / "operations.jsonl", context=context,
                                  max_bytes=256 * 1024, backups=1)
        write_json(self.directory / "current-run.json", self.run)
        if self.role == "service":
            write_json(self.root / "current-run.json", self.run)
        self.journal.event("service_start", **self.run)
        self.fault_file.write(json.dumps(self.run, ensure_ascii=False) + "\n")
        self.fault_file.flush()
        if os.name != "nt" or not identity:
            self.journal.event("native_monitor_unavailable", reason="unsupported_platform_or_process_identity")
            publish_state(self.directory, self.root, "monitor-ready.json",
                          {"run_id": self.run["run_id"], "status": "unavailable"})
            return
        environment = os.environ.copy()
        # The observer must finish recording exit information after the service dies.
        # A one-file child must own its extraction directory in that case.
        if getattr(sys, "frozen", False):
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        from ..paths import SOURCE_ROOT
        self.monitor = subprocess.Popen(
            monitor_command(self.directory, self.run),
            cwd=str(SOURCE_ROOT) if not getattr(sys, "frozen", False) else str(Path(sys.executable).parent),
            env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        monitor_identity = process_identity(self.monitor.pid)
        self.run.update(monitor_pid=self.monitor.pid,
                        monitor_created=monitor_identity[1] if monitor_identity else "")
        publish_state(self.directory, self.root, "current-run.json", self.run)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready = read_json(self.directory / "monitor-ready.json")
            if ready.get("run_id") == self.run["run_id"]:
                self.journal.event("native_monitor", **ready)
                if ready.get("status") != "active" and sys.stderr is not None:
                    print(f"原生崩溃转储未启用，诊断目录：{self.directory}", file=sys.stderr, flush=True)
                return
            if self.monitor.poll() is not None:
                break
            time.sleep(0.05)
        self.journal.event("native_monitor_unavailable", reason="startup_timeout_or_exit")
        publish_state(self.directory, self.root, "monitor-ready.json",
                      {"run_id": self.run["run_id"], "status": "unavailable", "reason": "startup_timeout_or_exit"})
        if sys.stderr is not None:
            print(f"原生崩溃取证进程未及时就绪，诊断目录：{self.directory}", file=sys.stderr, flush=True)

    def close(self, error=None) -> None:
        if isinstance(error, KeyboardInterrupt) or (isinstance(error, SystemExit) and error.code in (None, 0)):
            if self.run:
                write_json(self.directory / "stop-request.json", {"run_id": self.run["run_id"]})
            error = None
        if error is not None and self.fault_file is not None:
            traceback.print_exception(type(error), error, error.__traceback__, file=self.fault_file)
            self.journal.event("python_exception", **exception_details(error))
        if self.run:
            self.run["state"] = "python_error" if error is not None else "stopped"
            self.run["finished"] = timestamp()
            try:
                publish_state(self.directory, self.root, "current-run.json", self.run)
            except OSError as exc:
                self.journal.error = type(exc).__name__
            self.journal.event("service_context_exit", state=self.run["state"])
        if self.fault_file is not None:
            faulthandler.disable()
            self.fault_file.close()
        if error is not None and self.run:
            try:
                receipt = reserve_capture(self.root, self.run,
                                          {"exception_type": type(error).__name__}, native=False)
                if receipt.get("directory"):
                    receipt = snapshot_context(self.directory, self.root, receipt,
                                               finished=self.monitor is None or self.monitor.poll() is not None)
                write_json(self.directory / "latest-capture.json", receipt)
            except OSError as exc:
                self.journal.event("capture_failed", reason=type(exc).__name__)
        if self.guard is not None:
            self.guard.release()


@contextmanager
def diagnostic_session(directory: Path, entry: str, *, role="service"):
    global _active
    session = DiagnosticSession(directory, entry, role=role)
    try:
        session.start()
    except (OSError, ValueError) as exc:
        identity = process_identity(os.getpid())
        failure = {
            "time": timestamp(), "pid": os.getpid(), "created": identity[1] if identity else "",
            "role": role, "entry": entry, "directory": str(session.directory),
            "reason": str(exc), **exception_details(exc),
        }
        session.journal.event("diagnostics_degraded", **failure)
        try:
            write_json(session.root / "diagnostics-degraded.json", failure)
        except OSError:
            pass  # The stderr notice remains available when the directory is unwritable.
        if sys.stderr is not None:
            print("崩溃取证未完全启用：" + json.dumps(failure, ensure_ascii=False),
                  file=sys.stderr, flush=True)
    previous, _active = _active, session
    error = None
    try:
        if session.run:
            import sqlite3
            import _sqlite3
            diagnostic_event("sqlite_runtime", version=sqlite3.sqlite_version, extension=_sqlite3.__file__)
        yield session
    except BaseException as exc:
        error = exc
        raise
    finally:
        _active = previous
        session.close(error)
