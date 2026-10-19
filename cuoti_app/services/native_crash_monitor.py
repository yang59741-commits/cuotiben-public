"""Windows x64 observer: preserve bounded incidents without changing system WER."""
from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path

from .launcher_runtime import process_identity
from .diagnostic_evidence import (
    Journal, MAX_DUMP_BYTES, publish_state, read_json, reserve_capture,
    snapshot_context, timestamp, write_json,
)


class ExceptionRecord(C.Structure):
    _fields_ = [("code", W.DWORD), ("flags", W.DWORD), ("record", C.c_void_p),
                ("address", C.c_void_p), ("count", W.DWORD), ("information", C.c_size_t * 15)]


class ExceptionInfo(C.Structure):
    _fields_ = [("record", ExceptionRecord), ("first_chance", W.DWORD)]


class CreateProcessInfo(C.Structure):
    _fields_ = [("file", W.HANDLE), ("process", W.HANDLE), ("thread", W.HANDLE),
                ("base", C.c_void_p), ("debug_offset", W.DWORD), ("debug_size", W.DWORD),
                ("tls", C.c_void_p), ("start", C.c_void_p), ("image", C.c_void_p), ("unicode", W.WORD)]


class DebugData(C.Union):
    _fields_ = [("exception", ExceptionInfo), ("created", CreateProcessInfo),
                ("exit_code", W.DWORD), ("file", W.HANDLE)]


class DebugEvent(C.Structure):
    _fields_ = [("kind", W.DWORD), ("pid", W.DWORD), ("tid", W.DWORD), ("data", DebugData)]


class ExceptionPointers(C.Structure):
    _fields_ = [("record", C.POINTER(ExceptionRecord)), ("context", C.c_void_p)]


# DbgHelp's minidumpapiset.h uses pshpack4, unlike DEBUG_EVENT and CONTEXT.
class DumpExceptionInfo(C.Structure):
    _pack_ = 4
    _fields_ = [("tid", W.DWORD), ("pointers", C.POINTER(ExceptionPointers)), ("client", W.BOOL)]


class DumpIO(C.Structure):
    _pack_ = 4
    _fields_ = [("handle", W.HANDLE), ("offset", C.c_uint64),
                ("buffer", C.c_void_p), ("size", W.DWORD)]


class CallbackInput(C.Structure):
    _pack_ = 4
    _fields_ = [("pid", W.DWORD), ("process", W.HANDLE), ("kind", W.DWORD), ("io", DumpIO)]


def windows_api():
    kernel = C.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "OpenThread": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
        "CloseHandle": ([W.HANDLE], W.BOOL),
        "GetThreadContext": ([W.HANDLE, C.c_void_p], W.BOOL),
        "DebugActiveProcess": ([W.DWORD], W.BOOL),
        "DebugActiveProcessStop": ([W.DWORD], W.BOOL),
        "DebugSetProcessKillOnExit": ([W.BOOL], W.BOOL),
        "WaitForDebugEvent": ([C.POINTER(DebugEvent), W.DWORD], W.BOOL),
        "ContinueDebugEvent": ([W.DWORD, W.DWORD, W.DWORD], W.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def bounded_dump(kernel, process, event: DebugEvent, path: Path, limit: int = MAX_DUMP_BYTES) -> dict:
    import msvcrt
    callback_type = C.WINFUNCTYPE(W.BOOL, C.c_void_p, C.POINTER(CallbackInput), C.c_void_p)

    class CallbackInfo(C.Structure):
        _pack_ = 4
        _fields_ = [("callback", callback_type), ("parameter", C.c_void_p)]

    dbghelp = C.WinDLL("dbghelp", use_last_error=True)
    write_dump = dbghelp.MiniDumpWriteDump
    write_dump.argtypes = [W.HANDLE, W.DWORD, W.HANDLE, W.DWORD,
                           C.POINTER(DumpExceptionInfo), C.c_void_p, C.POINTER(CallbackInfo)]
    write_dump.restype = W.BOOL
    thread = kernel.OpenThread(0x0048, False, event.tid)
    if not thread:
        raise C.WinError(C.get_last_error())
    try:
        storage = C.create_string_buffer(1232 + 16)
        context = (C.addressof(storage) + 15) & ~15
        C.c_uint32.from_address(context + 48).value = 0x10001F
        if not kernel.GetThreadContext(thread, context):
            raise C.WinError(C.get_last_error())
        record = ExceptionRecord.from_buffer_copy(event.data.exception.record)
        record.record = None
        pointers = ExceptionPointers(C.pointer(record), context)
        exception = DumpExceptionInfo(event.tid, C.pointer(pointers), False)
        with path.open("x+b", buffering=0) as handle:
            result = {}
            heap_omitted = False
            for kind, flags, maximum in (("heap", 0x1261, limit), ("mini", 0x1061, min(limit, 8 * 1024 * 1024))):
                handle.seek(0)
                handle.truncate()
                failure = {}

                @callback_type
                def callback(_parameter, incoming, outgoing):
                    item = incoming.contents
                    status = C.cast(outgoing, C.POINTER(C.c_int32))
                    if item.kind == 11:  # DbgHelp must send every write through our cap.
                        status[0] = 1  # S_FALSE
                    elif item.kind == 12:
                        if item.io.offset + item.io.size > maximum:
                            failure["reason"] = "size_limit"
                            return False
                        try:
                            handle.seek(item.io.offset)
                            if handle.write(C.string_at(item.io.buffer, item.io.size)) != item.io.size:
                                failure["reason"] = "short_write"
                                return False
                        except OSError as exc:
                            failure["reason"] = type(exc).__name__
                            return False
                        status[0] = 0
                    elif item.kind == 13:
                        status[0] = 0
                    elif item.kind in (5, 9, 7):
                        return False  # No additional regions or kernel dump.
                    elif item.kind == 6:
                        status[0], status[1] = 0, 0
                    return True

                info = CallbackInfo(callback, None)
                success = write_dump(process, event.pid, msvcrt.get_osfhandle(handle.fileno()),
                                     flags, C.byref(exception), None, C.byref(info))
                result = {"dump_status": "saved" if success else "failed", "dump_kind": kind,
                          "bytes": path.stat().st_size, "reason": failure.get("reason"),
                          "winerror": 0 if success else C.get_last_error(), "heap_omitted": heap_omitted}
                if success or failure.get("reason") != "size_limit":
                    return result
                heap_omitted = True
            return result
    finally:
        kernel.CloseHandle(thread)


def preserve_crash(directory: Path, run_id: str, event: DebugEvent, kernel, process,
                   journal: Journal, *, evidence_root: Path | None = None) -> dict:
    root = evidence_root or directory
    run = read_json(directory / "current-run.json")
    run.update(run_id=run_id, pid=event.pid)
    receipt = reserve_capture(root, run, {
        "thread_id": event.tid, "exception_code": f"0x{event.data.exception.record.code:08X}",
        "exception_address": hex(event.data.exception.record.address or 0),
    }, native=True)
    if not receipt.get("directory"):
        write_json(directory / "latest-capture.json", receipt)
        journal.event("capture_skipped", **receipt)
        return receipt
    if receipt.get("dump_status") != "collecting":
        return receipt
    destination = root / receipt["directory"]
    try:
        receipt = snapshot_context(directory, root, receipt)
        partial = destination / "native.dmp.part"
        receipt.update(bounded_dump(kernel, process, event, partial, limit=receipt["limit_bytes"]))
        if receipt["dump_status"] == "saved":
            partial.rename(destination / "native.dmp")
    except OSError as exc:
        receipt.update(dump_status="failed", reason=type(exc).__name__)
    write_json(destination / "capture.json", receipt)
    write_json(directory / "latest-capture.json", receipt)
    journal.event("native_capture", **receipt)
    return receipt


def monitor(pid: int, created: int, directory: Path, run_id: str,
            *, evidence_root: Path | None = None) -> int:
    root = evidence_root or directory
    run = read_json(directory / "current-run.json")
    if run.get("run_id") != run_id:
        raise OSError("Diagnostic channel no longer belongs to this run")
    journal = Journal(directory / "monitor.jsonl", context={
        "run_id": run_id, "role": run.get("role", "service"), "build": run.get("build", "unknown")})
    kernel = windows_api()
    identity = process_identity(pid)
    if not identity or identity[1] != created:
        raise OSError("Target process no longer matches the launched service")
    process = kernel.OpenProcess(0x0410, False, pid)
    if not process:
        raise C.WinError(C.get_last_error())
    attached = False
    first_breakpoint = True
    native_failure = False
    capture = None
    try:
        if not kernel.DebugActiveProcess(pid):
            raise C.WinError(C.get_last_error())
        attached = True
        if not kernel.DebugSetProcessKillOnExit(False):
            raise C.WinError(C.get_last_error())
        observer_identity = process_identity(os.getpid())
        publish_state(directory, root, "monitor-ready.json",
                      {"run_id": run_id, "status": "active", "pid": os.getpid(),
                       "created": observer_identity[1] if observer_identity else ""})
        journal.event("attached", target_pid=pid, run_id=run_id)
        while True:
            event = DebugEvent()
            if not kernel.WaitForDebugEvent(C.byref(event), 1000):
                if C.get_last_error() == 121:  # ERROR_SEM_TIMEOUT
                    continue
                raise C.WinError(C.get_last_error())
            continuation = 0x00010002
            exiting = event.kind == 5
            try:
                if event.kind == 1:
                    fault = event.data.exception
                    continuation = 0x80010001  # Never swallow a real application exception.
                    if first_breakpoint and fault.first_chance and fault.record.code == 0x80000003:
                        first_breakpoint = False
                        continuation = 0x00010002
                    elif not fault.first_chance:
                        native_failure = True
                        try:
                            capture = preserve_crash(directory, run_id, event, kernel, process, journal,
                                                     evidence_root=root)
                        except OSError as exc:
                            capture = {**run, "time": timestamp(), "dump_status": "failed",
                                       "reason": type(exc).__name__}
                            write_json(directory / "latest-capture.json", capture)
                            journal.event("capture_failed", **capture)
                elif event.kind in (3, 6) and event.data.file:
                    kernel.CloseHandle(event.data.file)
                elif exiting:
                    expected = read_json(directory / "stop-request.json").get("run_id") == run_id
                    result = {"run_id": run_id, "time": timestamp(), "pid": pid,
                              "role": run.get("role", "service"), "build": run.get("build", "unknown"),
                              "exit_code": f"0x{event.data.exit_code:08X}",
                              "unexpected": native_failure or (event.data.exit_code != 0 and not expected),
                              "native_failure": native_failure, "expected_stop": expected}
                    result["exit_kind"] = ("native_failure" if native_failure else
                                           "control_termination" if event.data.exit_code == 0xC000013A else
                                           "error_exit" if event.data.exit_code else "normal")
                    if result["unexpected"] and result["exit_kind"] != "control_termination":
                        try:
                            if capture is None:
                                final_run = read_json(directory / "current-run.json")
                                capture = reserve_capture(root, final_run, {"exit_code": result["exit_code"]}, native=False)
                            if capture.get("directory"):
                                capture = snapshot_context(directory, root, {**capture, "exit": result}, finished=True)
                            write_json(directory / "latest-capture.json", capture)
                            result["capture"] = {key: capture[key] for key in ("directory", "dump_status", "reason") if key in capture}
                        except OSError as exc:
                            result["capture"] = {"dump_status": "failed", "reason": type(exc).__name__}
                            journal.event("capture_failed", reason=type(exc).__name__)
                    publish_state(directory, root, "last-exit.json", result)
                    journal.event("process_exit", **result)
            finally:
                kernel.ContinueDebugEvent(event.pid, event.tid, continuation)
            if exiting:
                attached = False
                return 0
    finally:
        if attached:
            kernel.DebugActiveProcessStop(pid)
        kernel.CloseHandle(process)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--created", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence-root", type=Path)
    args = parser.parse_args(argv)
    try:
        if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
            raise OSError("Native capture requires Windows x64")
        return monitor(args.pid, args.created, args.directory, args.run_id, evidence_root=args.evidence_root)
    except (OSError, ValueError) as exc:
        if read_json(args.directory / "current-run.json").get("run_id") != args.run_id:
            return 1
        Journal(args.directory / "monitor.jsonl").event("monitor_failed", reason=type(exc).__name__, winerror=getattr(exc, "winerror", None))
        try:
            publish_state(args.directory, args.evidence_root or args.directory, "monitor-ready.json",
                          {"run_id": args.run_id, "status": "unavailable", "reason": str(exc)})
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
