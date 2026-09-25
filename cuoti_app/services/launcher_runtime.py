"""Safe local service ownership, log limits, and reversible Windows startup."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import os
import re
import signal
import sys
import time
import urllib.request
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_NAME = "CuotiNotebook"
OWNERSHIP_HEALTH_TIMEOUT_SECONDS = 5.0


class SingleInstanceGuard(AbstractContextManager["SingleInstanceGuard"]):
    """Hold an OS-backed lock for the desktop launcher lifetime.

    The lock handle remains open while the launcher window is alive, so a
    crashed process releases it automatically.  A lock file is used instead
    of a PID-only marker because stale PID files can be reused by Windows.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle = None
        self._locked = False

    def acquire(self) -> bool:
        if self._locked:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            return False
        self._handle = handle
        self._locked = True
        return True

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        self._locked = False
        if handle is None:
            return
        try:
            handle.seek(0)
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            handle.close()

    def __enter__(self) -> "SingleInstanceGuard":
        if not self.acquire():
            raise RuntimeError("启动中心已经在运行")
        return self

    def __exit__(self, *_args) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class OwnedServiceRecord:
    pid: int
    created: int
    executable: str
    host: str
    port: int
    instance_id: str


def process_identity(pid: int) -> tuple[str, int] | None:
    if pid <= 0:
        return None
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(ctypes.wintypes.FILETIME),
            ctypes.POINTER(ctypes.wintypes.FILETIME),
            ctypes.POINTER(ctypes.wintypes.FILETIME),
            ctypes.POINTER(ctypes.wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            creation = ctypes.wintypes.FILETIME()
            exit_time = ctypes.wintypes.FILETIME()
            kernel = ctypes.wintypes.FILETIME()
            user = ctypes.wintypes.FILETIME()
            if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            created = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return str(Path(buffer.value).resolve()), created
        finally:
            kernel32.CloseHandle(handle)
    proc = Path("/proc") / str(pid)
    try:
        executable = str((proc / "exe").resolve())
        created = int((proc / "stat").read_text(encoding="utf-8").split()[21])
        return executable, created
    except (OSError, IndexError, ValueError):
        return None


def write_service_state(path: Path, record: OwnedServiceRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(asdict(record), ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def read_service_state(path: Path) -> OwnedServiceRecord | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return OwnedServiceRecord(
            pid=int(data["pid"]), created=int(data["created"]),
            executable=str(data["executable"]), host=str(data["host"]),
            port=int(data["port"]), instance_id=str(data["instance_id"]),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def health_service_identity(
    url: str, timeout: float = OWNERSHIP_HEALTH_TIMEOUT_SECONDS
) -> tuple[str, int] | None:
    request = urllib.request.Request(f"{url.rstrip('/')}/api/v2/health", headers={"User-Agent": "CuotiNotebookLauncher/3.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    value, pid = payload.get("service_instance_id"), payload.get("service_pid")
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    return (str(value), pid) if value and pid > 0 else None


def health_instance_id(url: str, timeout: float = 0.8) -> str | None:
    identity = health_service_identity(url, timeout=timeout)
    return identity[0] if identity else None


def validate_owned_service(record: OwnedServiceRecord, *, url: str, host: str, port: int, identity_reader=process_identity, health_reader=health_service_identity) -> bool:
    if record.host != host or record.port != port or not record.instance_id:
        return False
    identity = identity_reader(record.pid)
    if not identity:
        return False
    executable, created = identity
    if created != record.created or os.path.normcase(str(Path(executable).resolve())) != os.path.normcase(str(Path(record.executable).resolve())):
        return False
    return health_reader(url) == (record.instance_id, record.pid)


def terminate_owned_pid(pid: int, timeout: float = 4.0) -> bool:
    identity = process_identity(pid)
    if identity is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_identity(pid) is None:
            return True
        time.sleep(0.05)
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)
        if handle:
            try:
                ctypes.windll.kernel32.TerminateProcess(handle, 1)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
    return process_identity(pid) is None


def rotate_log(path: Path, *, max_bytes: int = 2 * 1024 * 1024, backups: int = 3) -> None:
    try:
        if path.stat().st_size <= max_bytes:
            return
    except FileNotFoundError:
        return
    for index in range(backups, 0, -1):
        source = path.with_name(path.name + (f".{index-1}" if index > 1 else ""))
        target = path.with_name(path.name + f".{index}")
        if source.exists():
            os.replace(source, target)


def append_runtime_event(path: Path, event: str, **details) -> bool:
    """Append one bounded structured lifecycle event without exposing settings."""

    record = {
        "event": str(event),
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record.update(details)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", buffering=1) as handle:
            handle.write(
                "[cuoti-runtime] "
                + json.dumps(record, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
    except OSError:
        return False
    return True


def redact_log(value: str) -> str:
    value = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)\S+", r"\1[REDACTED]", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", value)
    value = re.sub(r"(?i)(api[_-]?key\s*[:=]\s*)\S+", r"\1[REDACTED]", value)
    return value


def read_log_tail(path: Path, *, max_lines: int = 100, max_bytes: int = 32 * 1024) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read(max_bytes)
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return redact_log("\n".join(text.splitlines()[-max_lines:]))


def read_launcher_preferences(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"reminders_enabled": True, "last_notified_day": ""}
    return {
        "reminders_enabled": bool(data.get("reminders_enabled", True)),
        "last_notified_day": str(data.get("last_notified_day") or ""),
    } if isinstance(data, dict) else {"reminders_enabled": True, "last_notified_day": ""}


def write_launcher_preferences(path: Path, preferences: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(preferences, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, AUTOSTART_NAME)
            return bool(str(value).strip())
    except OSError:
        return False


def set_autostart(enabled: bool, command: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, command)
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass
    return autostart_enabled()
