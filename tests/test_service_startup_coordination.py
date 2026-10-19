import subprocess
import sys
import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app as desktop
from cuoti_app.services.formal_runtime import FormalRuntimeError, database_service_guard
from tools import run_q1c2_release_app as source


def test_cross_process_lock_blocks_both_entries_and_recovers_after_crash(tmp_path):
    database = tmp_path / "data/cuoti.db"
    database.parent.mkdir()
    database.touch()
    code = (
        "import sys; from cuoti_app.services.formal_runtime import database_service_guard; "
        "guard=database_service_guard(sys.argv[1]); guard.__enter__(); "
        "print('locked', flush=True); sys.stdin.readline()"
    )
    owner = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-B", "-c", code, str(database)],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert owner.stdout.readline().strip() == "locked"
        with patch.object(desktop, "DATA_DIR", tmp_path), patch.object(desktop, "get_application") as create:
            with pytest.raises(FormalRuntimeError, match="database_service_already_running"):
                desktop.run(open_browser=False)
            create.assert_not_called()
        args = SimpleNamespace(database=database, runtime_root=tmp_path, port=8765, no_browser=True)
        with patch.object(source, "_select_release_port", return_value=("free", 8765)), patch.object(source, "_prepare_runtime") as prepare:
            with pytest.raises(FormalRuntimeError, match="database_service_already_running"):
                source.run(args)
            prepare.assert_not_called()
        other = tmp_path / "other.db"
        other.touch()
        with database_service_guard(other):
            pass
    finally:
        owner.kill()
        owner.communicate(timeout=10)
    # The stale filename is harmless: ownership is the OS lock, not its presence.
    assert database.with_name("cuoti.db.service.lock").exists()
    with database_service_guard(database):
        pass


SHUTDOWN_CHILD = r'''
import faulthandler, json, os, sqlite3, sys, threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import app as desktop
from tools import run_q1c2_release_app as source
from cuoti_app.services.job_handlers import stop_job_worker

root = Path(sys.argv[1]); entry = sys.argv[2]
database = root / 'data/cuoti.db'
writing = threading.Event()
blocked = threading.Event()
def blocked_sqlite_writer():
    db = sqlite3.connect(database)
    with db:
        db.execute('INSERT INTO writes VALUES(1)')
        writing.set()
        blocked.wait()
    db.close()
worker = threading.Thread(target=blocked_sqlite_writer, name='blocked-sqlite-writer', daemon=True)
worker.start()
assert writing.wait(3)
application = SimpleNamespace(extensions={'job_worker': {'threads': [worker], 'stop_event': threading.Event()}})
original_exit = os._exit
def delayed_exit(code):
    (root / 'before-exit.json').write_text(json.dumps({
        'code': code, 'worker_alive': worker.is_alive(), 'faulthandler_enabled': faulthandler.is_enabled()
    }), encoding='utf-8')
    sys.stdin.readline()
    original_exit(code)
os._exit = delayed_exit
def stop(*args, **kwargs):
    stop_job_worker(application, timeout=.02)
with patch.object(desktop, 'DATA_DIR', root), patch.object(desktop, '_run_server', side_effect=stop), \
     patch.object(source, '_select_release_port', return_value=('free',8765)), \
     patch.object(source, '_run_locked', side_effect=stop):
    if entry == 'desktop': desktop.run(open_browser=False)
    else: source.run(SimpleNamespace(database=database, runtime_root=root/'data/runtime', port=8765, no_browser=True))
raise AssertionError('Timed-out writers must not outlive the service lease')
'''


@pytest.mark.skipif(os.name != 'nt', reason='Windows process and native-observer lifetime')
@pytest.mark.parametrize('entry', ['desktop', 'source'])
def test_stop_timeout_retains_guard_and_all_thread_evidence_until_process_exit(tmp_path, entry):
    database = tmp_path / 'data/cuoti.db'
    database.parent.mkdir()
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE writes(value INTEGER)')
    child = subprocess.Popen(
        [sys.executable, '-X', 'utf8', '-B', '-c', SHUTDOWN_CHILD, str(tmp_path), entry],
        cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
    )
    marker = tmp_path / 'before-exit.json'
    diagnostics = tmp_path / 'data/runtime/diagnostics'
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.03)
        assert marker.exists(), child.communicate(timeout=2)
        assert json.loads(marker.read_text(encoding='utf-8')) == {
            'code': 1, 'worker_alive': True, 'faulthandler_enabled': True,
        }
        with pytest.raises(FormalRuntimeError, match='database_service_already_running'):
            with database_service_guard(database):
                pass
        run = json.loads((diagnostics / 'current-run.json').read_text(encoding='utf-8'))
        channel = diagnostics / run['channel']
        stack = (channel / 'python-current.log').read_text(encoding='utf-8')
        assert 'WorkerShutdownTimeout' in stack and 'blocked_sqlite_writer' in stack
        assert run['state'] == 'shutdown_timeout'
        child.communicate(input='exit\n', timeout=10)
        assert child.returncode == 1
        with database_service_guard(database):
            with sqlite3.connect(database) as db:
                assert db.execute('SELECT COUNT(*) FROM writes').fetchone()[0] == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            receipt = diagnostics / 'first-crash/capture.json'
            if receipt.exists():
                capture = json.loads(receipt.read_text(encoding='utf-8'))
                if capture.get('state') == 'frozen':
                    break
            time.sleep(.05)
        else:
            pytest.fail('Observer did not freeze shutdown evidence')
        assert capture['exception_type'] == 'WorkerShutdownTimeout'
        assert 'blocked_sqlite_writer' in (receipt.parent / 'python-current.log').read_text(encoding='utf-8')
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("entry", ["desktop", "source"])
@pytest.mark.parametrize("failure", [None, "initialization", "bind", "worker", "serve", "stop"])
def test_server_lifetime_releases_resources_and_starts_workers_after_bind(tmp_path, entry, failure):
    database = tmp_path / "data/cuoti.db"
    database.parent.mkdir()
    database.touch()
    app = MagicMock()
    server = MagicMock()
    order = []

    def initialize(*_args):
        order.append("initialization")
        if failure == "initialization":
            raise RuntimeError("initialization")
        return app if entry == "desktop" else (app, {})

    def bind(*_args, **_kwargs):
        order.append("bind")
        if failure == "bind":
            raise OSError("bind")
        return server

    def worker(*_args):
        order.append("worker")
        if failure == "worker":
            raise RuntimeError("worker")

    server.serve_forever.side_effect = RuntimeError("serve") if failure == "serve" else None
    module = desktop if entry == "desktop" else source
    args = SimpleNamespace(database=database, runtime_root=tmp_path, port=8765, no_browser=True)
    with (
        patch.object(desktop, "DATA_DIR", tmp_path),
        patch.object(module, "diagnostic_session"),
        patch.object(desktop, "get_application", side_effect=initialize),
        patch.object(desktop, "cleanup_on_startup"),
        patch.object(desktop, "memory_from_app"),
        patch.object(source, "_prepare_runtime", side_effect=initialize),
        patch.object(source, "_select_release_port", return_value=("free", 8765)),
        patch.object(source, "_startup_state", return_value={}),
        patch.object(source, "_runtime_initialized", return_value=True),
        patch.object(source, "_startup_blocker", return_value=None),
        patch("werkzeug.serving.make_server", side_effect=bind),
        patch.object(module, "start_release_workers" if entry == "desktop" else "_start_release_workers", side_effect=worker) as start,
        patch.object(module, "stop_job_worker", side_effect=RuntimeError("stop") if failure == "stop" else None),
    ):
        call = lambda: desktop.run(open_browser=False) if entry == "desktop" else source.run(args)
        if failure:
            with pytest.raises((RuntimeError, OSError)):
                call()
        else:
            call()
        if failure in {"initialization", "bind"}:
            start.assert_not_called()
            server.server_close.assert_not_called()
        else:
            assert order.index("bind") < order.index("worker")
            server.server_close.assert_called_once()
    with database_service_guard(database):
        pass
