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
from cuoti_app.services.job_handlers import stop_job_worker
from cuoti_app.services.release_workers import start_release_workers
from cuoti_app.services.ai_memory import memory_from_app


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
    return parser


def run(*, open_browser: bool = True) -> None:
    with database_service_guard(DATA_DIR / "data" / "cuoti.db"):
        _run_server(open_browser=open_browser)


def _run_server(*, open_browser: bool) -> None:
    from werkzeug.serving import make_server

    app = get_application()
    server = make_server("127.0.0.1", 8080, app, threaded=True)
    try:
        with app.app_context():
            cleanup_on_startup()
            memory = memory_from_app(app)
            for memory_type in ("free", "mistakes", "past"):
                memory.rollover_expired_days(memory_type)
        start_release_workers(app)
        url = "http://127.0.0.1:8080"
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
    run(open_browser=not arguments.no_browser)
