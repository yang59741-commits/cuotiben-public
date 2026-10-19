"""
错题标记与复习系统
启动: 双击 错题本.exe，或运行 python app.py
访问: http://127.0.0.1:8080
"""

import argparse
import sys
import threading
import webbrowser

from cuoti_app import create_app
from cuoti_app.paths import DATA_DIR
from cuoti_app.services.formal_runtime import build_formal_runtime_overrides, database_service_guard
from cuoti_app.routes.mistake_exports import cleanup_on_startup
from cuoti_app.services.job_handlers import WorkerShutdownTimeout, stop_job_worker
from cuoti_app.services.release_workers import start_release_workers
from cuoti_app.services.ai_memory import memory_from_app
from cuoti_app.services.process_temp import configure_process_temp
from cuoti_app.services.runtime_diagnostics import diagnostic_session, terminate_after_shutdown_timeout


_application = None


def get_application():
    global _application
    if _application is None:
        overrides = build_formal_runtime_overrides(
            DATA_DIR, require_database=False, require_reference_assets=False,
        )
        # Let the user's environment/settings select their own target school.
        overrides.pop("SEMANTIC_HOME_INSTITUTION")
        _application = create_app(overrides)
    return _application


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动本地错题本服务")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="启动服务但不自动打开浏览器",
    )
    parser.add_argument("--port", type=int, default=8080, help="本机服务端口，默认 8080")
    return parser


def run(*, open_browser: bool = True, port: int = 8080) -> None:
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1 到 65535 之间")
    configure_process_temp(DATA_DIR / "data" / "runtime")
    with database_service_guard(DATA_DIR / "data" / "cuoti.db"):
        with diagnostic_session(DATA_DIR / "data/runtime/diagnostics", "desktop" if getattr(sys, "frozen", False) else "source-app"):
            try:
                _run_server(open_browser=open_browser, port=port)
            except WorkerShutdownTimeout as error:
                terminate_after_shutdown_timeout(error)


def _run_server(*, open_browser: bool, port: int = 8080) -> None:
    from werkzeug.serving import make_server

    app = get_application()
    server = make_server("127.0.0.1", port, app, threaded=True)
    try:
        with app.app_context():
            cleanup_on_startup()
            memory = memory_from_app(app)
            for memory_type in ("free", "mistakes", "past"):
                memory.rollover_expired_days(memory_type)
        start_release_workers(app)
        url = f"http://127.0.0.1:{port}"
        if sys.stdout is not None:
            try:
                print(f"\U0001f4d2 错题本已启动 -> {url}")
            except UnicodeEncodeError:
                print(f"错题本已启动 -> {url}")
        if open_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    finally:
        try:
            stop_job_worker(app)
        finally:
            server.server_close()


if __name__ == "__main__":
    arguments = build_argument_parser().parse_args()
    run(open_browser=not arguments.no_browser, port=arguments.port)
