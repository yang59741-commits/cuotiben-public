"""Evidence must survive real native failure, restart, and storage limits."""
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cuoti_app.services import runtime_diagnostics as diagnostics
from cuoti_app.services import diagnostic_evidence as evidence


def test_journal_rotates_and_request_context_does_not_record_payload(tmp_path):
    from flask import Flask
    from cuoti_app.errors import init_error_contract
    journal = diagnostics.Journal(tmp_path / 'runtime.jsonl')
    app = Flask(__name__)
    init_error_contract(app)
    app.add_url_rule('/fixture/<string:value>', view_func=lambda value: 'ok', methods=['POST'])
    with patch.object(diagnostics, '_active', SimpleNamespace(journal=journal)):
        response = app.test_client().post('/fixture/private-text?api_key=secret-query',
                                          json={'secret': 'secret-body'},
                                          headers={'Authorization': 'Bearer secret-header'})
    text = journal.path.read_text(encoding='utf-8')
    assert response.status_code == 200
    assert '/fixture/<string:value>' in text
    assert all(secret not in text for secret in ('private-text', 'secret-query', 'secret-body', 'secret-header'))
    with patch.object(evidence, 'MAX_EVENT_BYTES', 512):
        for number in range(60):
            journal.event('sample', number=number)
    files = list(tmp_path.glob('runtime.jsonl*'))
    assert len(files) == 3
    assert max(path.stat().st_size for path in files) < 1024


def test_preserved_capture_is_never_overwritten(tmp_path):
    from cuoti_app.services.native_crash_monitor import DebugEvent, preserve_crash
    folder = tmp_path / 'first-crash'
    folder.mkdir()
    original = {'run_id': 'previous', 'dump_status': 'saved'}
    (folder / 'capture.json').write_text(json.dumps(original), encoding='utf-8')
    (folder / 'native.dmp').write_bytes(b'previous evidence')
    def dump(_kernel, _process, _event, path, *, limit):
        path.write_bytes(b'new evidence')
        return {'dump_status': 'saved', 'bytes': path.stat().st_size}
    with patch('cuoti_app.services.native_crash_monitor.bounded_dump', side_effect=dump):
        result = preserve_crash(tmp_path, 'next', DebugEvent(), None, None,
                                diagnostics.Journal(tmp_path / 'monitor.jsonl'))
    assert result['dump_status'] == 'saved'
    assert (tmp_path / 'incidents/next/native.dmp').read_bytes() == b'new evidence'
    assert json.loads((folder / 'capture.json').read_text()) == original
    assert (folder / 'native.dmp').read_bytes() == b'previous evidence'
    assert '已保留崩溃现场' in diagnostics.diagnostic_notice(tmp_path)


def test_unavailable_diagnostics_does_not_block_service(tmp_path):
    with patch.object(diagnostics.DiagnosticSession, 'start', side_effect=PermissionError('synthetic')):
        with diagnostics.diagnostic_session(tmp_path, 'fixture'):
            diagnostics.diagnostic_event('service_continues')
    journal = (tmp_path / 'runtime.jsonl').read_text(encoding='utf-8')
    assert 'diagnostics_degraded' in journal and 'service_continues' in journal


CHILD = r'''
import ctypes, json, os, sys, time
from pathlib import Path
from cuoti_app.services.runtime_diagnostics import diagnostic_session, diagnostic_event, mark_expected_stop
directory=Path(sys.argv[1]); action=sys.argv[2]
ctypes.windll.kernel32.SetErrorMode(3)
role='launcher' if action=='gui-native' else 'service'
with diagnostic_session(directory, 'synthetic-native-test', role=role) as session:
    directory=session.directory
    ready=json.loads((directory/'monitor-ready.json').read_text(encoding='utf-8'))
    assert ready['status']=='active',ready
    diagnostic_event('fixture_ready', action=action)
    if action=='python': raise RuntimeError('synthetic-python-error')
    if action=='control': ctypes.windll.kernel32.ExitProcess(0xc000013a)
    if action=='expected':
        mark_expected_stop(directory)
        ctypes.windll.kernel32.ExitProcess(7)
    if action=='heap':
        from cuoti_app.services.native_crash_monitor import ExceptionRecord
        record=ExceptionRecord();record.code=0xc0000374;record.flags=1
        fail=ctypes.windll.kernel32.RaiseFailFastException
        fail.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong]
        fail(ctypes.byref(record),None,1)
    if action=='gui-native':
        import tkinter
        window=tkinter.Tk();window.withdraw()
    if action in ('native','large','gui-native'):
        if action=='large': memory=bytearray(128*1024*1024)
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.CreateThread.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_ulong,ctypes.POINTER(ctypes.c_ulong)]
        k.CreateThread.restype=ctypes.c_void_p
        tid=ctypes.c_ulong()
        assert k.CreateThread(None,0,None,None,0,ctypes.byref(tid))
        time.sleep(15)
        raise AssertionError('Native fault did not terminate the fixture')
'''


def _exercise(directory, action):
    process = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', CHILD, str(directory), action],
                             cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=35)
    channels = [directory / f'active/slot-{n}' for n in range(evidence.MAX_CHANNELS)]
    channel = max(channels, key=lambda item: diagnostics.read_json(item/'current-run.json').get('started',''))
    run = diagnostics.read_json(channel / 'current-run.json')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = diagnostics.read_json(channel / 'last-exit.json')
        if result.get('run_id') == run.get('run_id'):
            return process, result
        time.sleep(0.05)
    raise AssertionError((process.returncode, process.stderr.decode('utf-8', 'replace'),
                          diagnostics.read_json(channel / 'monitor-ready.json')))


def _dump_exception(path):
    with path.open('rb') as handle:
        header = handle.read(32)
        assert header[:4] == b'MDMP'
        count, rva = struct.unpack_from('<II', header, 8)
        handle.seek(rva)
        entries = [struct.unpack('<III', handle.read(12)) for _ in range(count)]
        stream = next(entry for entry in entries if entry[0] == 6)
        handle.seek(stream[2])
        body = handle.read(40)
        return struct.unpack_from('<I', body, 0)[0], struct.unpack_from('<I', body, 8)[0]


@pytest.mark.skipif(os.name != 'nt', reason='Windows native exception capture')
def test_real_native_capture_normal_stop_and_restart():
    with tempfile.TemporaryDirectory(prefix='native-capture-test-') as temp:
        directory = Path(temp)
        normal, result = _exercise(directory, 'normal')
        assert normal.returncode == 0
        assert result['unexpected'] is False
        python, result = _exercise(directory, 'python')
        assert python.returncode != 0 and result['unexpected'] is True
        python_folder = directory / result['capture']['directory']
        assert 'synthetic-python-error' in (python_folder / 'python-current.log').read_text(encoding='utf-8')
        native, result = _exercise(directory, 'native')
        assert native.returncode & 0xffffffff == 0xc0000005
        assert result['unexpected'] and result['native_failure']
        first_native = directory / result['capture']['directory']
        capture = diagnostics.read_json(first_native / 'capture.json')
        assert capture['dump_status'] == 'saved', capture
        dump = first_native / 'native.dmp'
        tid, code = _dump_exception(dump)
        assert code == 0xc0000005 and tid == capture['thread_id']
        assert 0 < dump.stat().st_size <= diagnostics.MAX_DUMP_BYTES
        original_time = dump.stat().st_mtime_ns
        again, result = _exercise(directory, 'native')
        assert again.returncode & 0xffffffff == 0xc0000005
        second_native = directory / result['capture']['directory']
        assert second_native != first_native and (second_native / 'native.dmp').is_file()
        assert diagnostics.read_json(first_native / 'capture.json') == capture
        assert dump.stat().st_mtime_ns == original_time
        heap, result = _exercise(directory, 'heap')
        assert heap.returncode & 0xffffffff == 0xc0000374
        assert result['native_failure'] and result['exit_code'] == '0xC0000374'
        assert diagnostics.read_json(first_native / 'capture.json') == capture
        assert 'synthetic-python-error' in (python_folder / 'python-current.log').read_text(encoding='utf-8')
        expected, result = _exercise(directory, 'expected')
        assert expected.returncode == 7
        assert result['expected_stop'] and not result['unexpected']


@pytest.mark.skipif(os.name != 'nt', reason='Windows bounded dump writer')
def test_heap_dump_over_budget_falls_back_within_cap():
    with tempfile.TemporaryDirectory(prefix='native-capture-cap-test-') as temp:
        directory = Path(temp)
        native, result = _exercise(directory, 'large')
        assert native.returncode & 0xffffffff == 0xc0000005
        capture = diagnostics.read_json(directory / 'first-crash/capture.json')
        assert capture['dump_status'] == 'saved', capture
        assert capture['dump_kind'] == 'mini' and capture['heap_omitted']
        assert (directory / 'first-crash/native.dmp').stat().st_size <= 8*1024*1024


@pytest.mark.skipif(os.name != 'nt', reason='Windows GUI process capture')
def test_gui_native_failure_has_its_own_role_and_keeps_service_state(tmp_path):
    normal, result = _exercise(tmp_path, 'normal')
    assert normal.returncode == 0
    service = diagnostics.read_json(tmp_path / 'current-run.json')
    process, result = _exercise(tmp_path, 'gui-native')
    assert process.returncode & 0xffffffff == 0xc0000005
    assert result['role'] == 'launcher' and result['native_failure']
    assert diagnostics.read_json(tmp_path / 'current-run.json') == service
    capture = diagnostics.read_json(tmp_path / result['capture']['directory'] / 'capture.json')
    assert capture['role'] == 'launcher' and capture['dump_status'] == 'saved'
    assert capture['state'] == 'frozen'


@pytest.mark.skipif(os.name != 'nt', reason='Windows control termination')
def test_control_termination_does_not_consume_a_crash_slot(tmp_path):
    process, result = _exercise(tmp_path, 'control')
    assert process.returncode & 0xffffffff == 0xc000013a
    assert result['exit_kind'] == 'control_termination' and not result['native_failure']
    assert not evidence.capture_directories(tmp_path)
