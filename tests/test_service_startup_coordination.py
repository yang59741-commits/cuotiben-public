import subprocess
import sys
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
        args = SimpleNamespace(database=database, port=8765, no_browser=True)
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
