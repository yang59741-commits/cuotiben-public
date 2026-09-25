"""Owned subprocess lifecycle helpers for durable background jobs."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any, Sequence


class ProcessTreeTerminationError(RuntimeError):
    """Raised when an owned subprocess tree cannot be confirmed stopped."""


_REAL_POPEN = subprocess.Popen
_WINDOWS_JOB_ATTRIBUTE = "_cuoti_windows_job"


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_ACCESS_DENIED = 5
    _ERROR_INVALID_PARAMETER = 87
    _ERROR_MORE_DATA = 234
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 0x00000102
    _TH32CS_SNAPPROCESS = 0x00000002
    _TH32CS_SNAPTHREAD = 0x00000004
    _THREAD_SUSPEND_RESUME = 0x0002
    _SYNCHRONIZE = 0x00100000
    _CREATE_SUSPENDED = 0x00000004
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST = 3
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        )

    class _ThreadEntry32(ctypes.Structure):
        _fields_ = (
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        )

    class _JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = (
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        )

    class _IoCounters(ctypes.Structure):
        _fields_ = (
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        )

    class _JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = (
            ("BasicLimitInformation", _JobObjectBasicLimitInformation),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        )

    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    )
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ProcessEntry32W),
    )
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.Thread32First.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    )
    _kernel32.Thread32First.restype = wintypes.BOOL
    _kernel32.Thread32Next.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    )
    _kernel32.Thread32Next.restype = wintypes.BOOL
    _kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenThread.restype = wintypes.HANDLE
    _kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


def _format_windows_error(action: str, error_code: int | None = None) -> str:
    if os.name != "nt":
        return action
    code = int(error_code if error_code is not None else ctypes.get_last_error())
    return f"{action}（Windows error {code}: {ctypes.FormatError(code).strip()}）"


def _windows_descendant_pids(root_pid: int) -> tuple[int, ...]:
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        return ()
    parents: dict[int, list[int]] = {}
    entry = _ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if _kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            while True:
                parents.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                if not _kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)

    descendants: list[int] = []
    pending = list(parents.get(int(root_pid), ()))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.append(pid)
        pending.extend(parents.get(pid, ()))
    return tuple(descendants)


def _windows_thread_ids(pid: int) -> tuple[int, ...]:
    snapshot = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if snapshot == _INVALID_HANDLE_VALUE:
        raise OSError(_format_windows_error("无法枚举新进程线程"))
    thread_ids: list[int] = []
    entry = _ThreadEntry32()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        if _kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            while True:
                if int(entry.th32OwnerProcessID) == int(pid):
                    thread_ids.append(int(entry.th32ThreadID))
                if not _kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)
    return tuple(thread_ids)


def _resume_suspended_process(pid: int) -> None:
    deadline = time.monotonic() + 0.5
    thread_ids: tuple[int, ...] = ()
    while not thread_ids and time.monotonic() < deadline:
        thread_ids = _windows_thread_ids(pid)
        if not thread_ids:
            time.sleep(0.005)
    if not thread_ids:
        raise OSError("无法定位新进程的 suspended primary thread")
    resumed = False
    for thread_id in thread_ids:
        handle = _kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, thread_id)
        if not handle:
            continue
        try:
            previous_count = _kernel32.ResumeThread(handle)
            if previous_count != 0xFFFFFFFF:
                resumed = True
                break
        finally:
            _kernel32.CloseHandle(handle)
    if not resumed:
        raise OSError(_format_windows_error("无法恢复已纳入 Job Object 的新进程"))


def _job_process_id_list_type(capacity: int):
    class _JobObjectBasicProcessIdList(ctypes.Structure):
        _fields_ = (
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * capacity),
        )

    return _JobObjectBasicProcessIdList


class _WindowsJobObject:
    """One private kill-on-close Job Object owning a subprocess tree."""

    def __init__(self, handle: int, root_pid: int) -> None:
        self._handle = handle
        self.root_pid = int(root_pid)
        self._lock = threading.Lock()

    @classmethod
    def create(cls, root_pid: int) -> "_WindowsJobObject":
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(_format_windows_error("无法创建后台任务 Job Object"))
        information = _JobObjectExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not _kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            error = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise OSError(
                _format_windows_error("无法设置 Job Object 的 kill-on-close 限制", error)
            )
        return cls(handle, root_pid)

    @property
    def closed(self) -> bool:
        return not bool(self._handle)

    def assign(self, process: subprocess.Popen) -> None:
        process_handle = int(process._handle)  # type: ignore[attr-defined]
        if not _kernel32.AssignProcessToJobObject(
            self._handle, wintypes.HANDLE(process_handle)
        ):
            raise OSError(_format_windows_error("无法把后台任务进程纳入 Job Object"))
        if self.root_pid not in self.process_ids():
            raise OSError("Job Object 未报告刚分配的后台任务进程")

    def process_ids(self) -> set[int]:
        if self.closed:
            return set()
        capacity = 16
        for _attempt in range(6):
            value_type = _job_process_id_list_type(capacity)
            value = value_type()
            returned = wintypes.DWORD()
            if _kernel32.QueryInformationJobObject(
                self._handle,
                _JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(value),
                ctypes.sizeof(value),
                ctypes.byref(returned),
            ):
                count = min(int(value.NumberOfProcessIdsInList), capacity)
                return {int(value.ProcessIdList[index]) for index in range(count)}
            error = ctypes.get_last_error()
            if error != _ERROR_MORE_DATA:
                raise OSError(
                    _format_windows_error("无法读取 Job Object 的进程清单", error)
                )
            capacity = max(capacity * 2, int(value.NumberOfAssignedProcesses), 1)
        raise OSError("Job Object 进程清单超过可确认上限")

    def terminate_and_close(
        self, process: subprocess.Popen, *, timeout: float
    ) -> None:
        with self._lock:
            if self.closed:
                return
            try:
                owned_pids = self.process_ids()
            except OSError:
                owned_pids = set()
            owned_pids.update(
                {self.root_pid, *_windows_descendant_pids(self.root_pid)}
            )
            wait_handles, inaccessible = _open_windows_wait_handles(owned_pids)
            # TerminateJobObject is the primary stop request. Closing the
            # kill-on-close handle is an independent kernel-enforced backstop.
            _kernel32.TerminateJobObject(self._handle, 1)
            handle = self._handle
            self._handle = None
            close_ok = bool(_kernel32.CloseHandle(handle))

        deadline = time.monotonic() + max(0.25, float(timeout))
        remaining = _wait_windows_handles(wait_handles, deadline)
        remaining.update(pid for pid in inaccessible if is_process_running(pid))
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            remaining.add(self.root_pid)
        if not close_ok:
            remaining.update(owned_pids)
        if remaining:
            raise ProcessTreeTerminationError(
                "无法确认后台任务 Job Object 全部进程已停止："
                + ",".join(map(str, sorted(remaining)))
            )

    def close_after_failed_launch(self) -> None:
        with self._lock:
            if self.closed:
                return
            handle = self._handle
            self._handle = None
            _kernel32.TerminateJobObject(handle, 1)
            _kernel32.CloseHandle(handle)

    def __del__(self) -> None:  # pragma: no cover - emergency interpreter cleanup
        try:
            self.close_after_failed_launch()
        except Exception:
            pass


def _open_windows_wait_handles(
    pids: set[int],
) -> tuple[dict[int, int], set[int]]:
    handles: dict[int, int] = {}
    inaccessible: set[int] = set()
    for pid in pids:
        handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
        if handle:
            handles[int(pid)] = handle
            continue
        error = ctypes.get_last_error()
        if error == _ERROR_ACCESS_DENIED:
            inaccessible.add(int(pid))
        elif error != _ERROR_INVALID_PARAMETER:
            inaccessible.add(int(pid))
    return handles, inaccessible


def _wait_windows_handles(handles: dict[int, int], deadline: float) -> set[int]:
    remaining: set[int] = set()
    for pid, handle in handles.items():
        try:
            milliseconds = max(0, int((deadline - time.monotonic()) * 1000))
            result = _kernel32.WaitForSingleObject(handle, milliseconds)
            if result != _WAIT_OBJECT_0:
                remaining.add(pid)
        finally:
            _kernel32.CloseHandle(handle)
    return remaining


def _terminate_suspended_launch(process: subprocess.Popen) -> None:
    try:
        process.kill()
    finally:
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def start_owned_process(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    creationflags: int = 0,
) -> subprocess.Popen:
    """Start one process under an independently terminable OS owner."""

    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
    }
    if os.name == "nt":
        # The process cannot spawn an escaping descendant before assignment:
        # its primary thread stays suspended until the Job Object owns it.
        kwargs["creationflags"] = (
            creationflags
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | _CREATE_SUSPENDED
        )
    else:
        if creationflags:
            raise ValueError("creationflags are only supported on Windows")
        kwargs["start_new_session"] = True
    process = subprocess.Popen(args, **kwargs)
    if os.name != "nt" or not isinstance(process, _REAL_POPEN):
        # Popen-compatible test doubles do not own native handles and must keep
        # the legacy terminate/wait protocol used by focused service tests.
        return process

    raw_pid = getattr(process, "pid", None)
    if not isinstance(raw_pid, int) or raw_pid <= 0:
        _terminate_suspended_launch(process)
        raise ProcessTreeTerminationError("新后台任务进程没有可管理的 Windows PID")

    owner: _WindowsJobObject | None = None
    try:
        owner = _WindowsJobObject.create(raw_pid)
        owner.assign(process)
        setattr(process, _WINDOWS_JOB_ATTRIBUTE, owner)
        _resume_suspended_process(raw_pid)
        return process
    except Exception as exc:
        _terminate_suspended_launch(process)
        if owner is not None:
            owner.close_after_failed_launch()
        raise ProcessTreeTerminationError(
            f"无法建立后台任务 Windows Job Object 所有权：{exc}"
        ) from exc


def is_process_running(pid: int) -> bool:
    """Return whether one PID still identifies a running process."""

    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, int(pid))
    if not handle:
        # Access denied is not evidence that the process stopped. Treat every
        # failure except the OS's nonexistent-PID result as still running.
        return ctypes.get_last_error() != _ERROR_INVALID_PARAMETER
    try:
        return _kernel32.WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _kernel32.CloseHandle(handle)


def _terminate_untracked_process(process: subprocess.Popen, timeout: float) -> None:
    """Terminate a Popen-compatible test/dummy process without a native owner."""

    process.terminate()
    try:
        process.wait(timeout=max(0.1, float(timeout) / 2))
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=max(0.1, float(timeout) / 2))


def _terminate_windows_tree(process: subprocess.Popen, timeout: float) -> None:
    owner = getattr(process, _WINDOWS_JOB_ATTRIBUTE, None)
    if isinstance(owner, _WindowsJobObject):
        try:
            owner.terminate_and_close(process, timeout=timeout)
        finally:
            setattr(process, _WINDOWS_JOB_ATTRIBUTE, None)
        return
    # Only Popen-compatible test doubles are permitted to lack a Job Object.
    # A real Windows subprocess reaching this branch would be unowned and must
    # fail closed rather than degrading to parent-only termination.
    if isinstance(process, _REAL_POPEN):
        raise ProcessTreeTerminationError("真实 Windows 子进程缺少 Job Object 所有权")
    _terminate_untracked_process(process, timeout)


def _terminate_posix_tree(process: subprocess.Popen, timeout: float) -> None:
    deadline = time.monotonic() + max(0.25, float(timeout))
    try:
        os.killpg(int(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=max(0.0, min(timeout / 2, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(int(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise ProcessTreeTerminationError(
                f"无法确认后台任务子进程组已停止：{process.pid}"
            ) from exc


def close_process_tree(process: subprocess.Popen, *, timeout: float = 3.0) -> None:
    """Close ownership after normal root exit, stopping any orphan descendants."""

    if os.name != "nt":
        return
    owner = getattr(process, _WINDOWS_JOB_ATTRIBUTE, None)
    if isinstance(owner, _WindowsJobObject):
        try:
            owner.terminate_and_close(process, timeout=timeout)
        finally:
            setattr(process, _WINDOWS_JOB_ATTRIBUTE, None)


def terminate_process_tree(process: subprocess.Popen, *, timeout: float = 3.0) -> None:
    """Terminate and confirm the whole owned subprocess tree."""

    raw_pid = getattr(process, "pid", None)
    if not isinstance(raw_pid, int) or raw_pid <= 0:
        _terminate_untracked_process(process, timeout)
        return
    if os.name == "nt":
        _terminate_windows_tree(process, timeout)
    else:
        _terminate_posix_tree(process, timeout)


__all__ = [
    "ProcessTreeTerminationError",
    "close_process_tree",
    "is_process_running",
    "start_owned_process",
    "terminate_process_tree",
]
