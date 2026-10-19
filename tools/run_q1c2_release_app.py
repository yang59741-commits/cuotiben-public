"""Run the normal application directly on the Q1C2 release working database.

This entry point does not use TESTING mode and does not copy the database.  It
keeps all newly mutable runtime files beside the working database while reading
the published question-bank index, formal artifact, and Windows credential
entry through the normal application services.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cuoti_app import create_app
from cuoti_app.services.runtime_diagnostics import diagnostic_session, terminate_after_shutdown_timeout
from cuoti_app.services.process_temp import configure_process_temp
from cuoti_app.services.formal_runtime import (
    FormalRuntimeError, FormalRuntimePaths, database_service_guard, runtime_identity,
)
from cuoti_app.services.job_handlers import WorkerShutdownTimeout, stop_job_worker
from cuoti_app.services.release_workers import (
    ReleaseJobCoordinator as _ReleaseJobCoordinator,
    run_release_worker as _run_release_worker,
    start_release_workers as _start_release_workers,
)


DEFAULT_DATABASE = (
    PROJECT_ROOT
    / "data"
    / "cuoti.db"
)
DEFAULT_RUNTIME_ROOT = DEFAULT_DATABASE.parent / "runtime"
DEFAULT_SETTINGS = PROJECT_ROOT / "instance" / "settings.json"
DEFAULT_CATALOG = PROJECT_ROOT / "instance" / "catalog.json"
DEFAULT_QUESTION_BANK = PROJECT_ROOT / "instance" / "question_bank.sqlite3"
DEFAULT_FORMAL_ARTIFACT = (
    PROJECT_ROOT / "instance" / "semantic_index" / "formal_past_exams.json"
)
RECOVERABLE_JOB_STATUSES = (
    "queued",
    "running",
    "retry_wait",
    "cancel_requested",
)
RELEASE_INSTANCE_ID = "q1c2-release-v4"
RELEASE_PORT_SCAN_COUNT = 20
RELEASE_PORT_PROBE_TIMEOUT = 1.0


class ReleaseAppStartupError(RuntimeError):
    def __init__(self, code: str, details: dict[str, Any]):
        super().__init__(code)
        self.code = code
        self.details = details


def _readonly_database(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    db = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _startup_state(database: Path) -> dict[str, Any]:
    """Inspect only work that the normal worker or artifact repair could resume."""

    db = _readonly_database(database)
    try:
        placeholders = ",".join("?" for _ in RECOVERABLE_JOB_STATUSES)
        jobs = [
            {
                "id": str(row["id"]),
                "kind": str(row["kind"]),
                "status": str(row["status"]),
                "created_at": str(row["created_at"] or ""),
            }
            for row in db.execute(
                "SELECT id,kind,status,created_at FROM background_jobs "
                f"WHERE status IN ({placeholders}) ORDER BY created_at,id",
                RECOVERABLE_JOB_STATUSES,
            ).fetchall()
        ]
        reconcile_candidates = int(
            db.execute(
                "SELECT COUNT(*) FROM exams e WHERE e.artifact_status<>'published' "
                "OR e.latex_source IS NULL OR e.latex_sha256 IS NULL "
                "OR e.pdf_sha256 IS NULL OR COALESCE(e.recompile_available,0)<>1 "
                "OR NOT EXISTS(SELECT 1 FROM exam_items i WHERE i.exam_id=e.id)"
            ).fetchone()[0]
        )
        staged_attachments = int(
            db.execute(
                "SELECT COUNT(*) FROM attachments WHERE status='staged'"
            ).fetchone()[0]
        )
        return {
            "recoverable_jobs": jobs,
            "recoverable_job_count": len(jobs),
            "artifact_reconcile_candidate_count": reconcile_candidates,
            "staged_attachment_count": staged_attachments,
        }
    finally:
        db.close()


def _copy_seed_once(source: Path, destination: Path) -> str:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        return "reused"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    return "copied"


def _runtime_initialized(runtime_root: Path) -> bool:
    return all(
        (runtime_root / name).is_file()
        for name in ("settings.json", "catalog.json")
    )


def _inspect_release_port(port: int, *, expected_identity: dict[str, str] | None = None,
                          timeout: float = RELEASE_PORT_PROBE_TIMEOUT) -> str:
    """Return free, same_release, or occupied without trusting arbitrary HTTP."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except OSError:
        return "free"
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/v2/health",
        headers={"User-Agent": "CuotiQ1C2Release/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        if exc.code != 503:
            return "occupied"
        body = exc.read(65536)
    except (TimeoutError, socket.timeout):
        return "identity_unavailable"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return "identity_unavailable"
        return "occupied"
    except OSError:
        return "identity_unavailable"
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "occupied"
    if (
        isinstance(payload, dict)
        and str(payload.get("contract_version")) == "1"
        and payload.get("overall_status") in {"healthy", "degraded", "fatal"}
        and payload.get("service_instance_id") == RELEASE_INSTANCE_ID
    ):
        identity = payload.get("runtime_identity")
        if not expected_identity or not isinstance(identity, dict) or not all(
            isinstance(identity.get(key), str) and Path(identity[key]).is_absolute()
            for key in ("database", "runtime_root")
        ):
            return "identity_unavailable"
        identity = runtime_identity(identity["database"], identity["runtime_root"])
        if identity["database"] != expected_identity["database"]:
            return "occupied"
        if identity["runtime_root"] != expected_identity["runtime_root"]:
            return "runtime_mismatch"
        return "same_release"
    return "occupied"


def _select_release_port(
    requested_port: int,
    *,
    scan_count: int = RELEASE_PORT_SCAN_COUNT,
    expected_identity: dict[str, str] | None = None,
    inspector=None,
) -> tuple[str, int]:
    """Prefer an existing release instance, otherwise the first free port."""

    if type(scan_count) is not int or scan_count < 1:
        raise ValueError("scan_count must be a positive integer")
    first_free = None
    if inspector is None:
        inspector = lambda port: _inspect_release_port(port, expected_identity=expected_identity)
    uncertain_ports = []
    last_port = min(65535, requested_port + scan_count - 1)
    for port in range(requested_port, last_port + 1):
        state = inspector(port)
        if state == "same_release":
            return state, port
        if state == "runtime_mismatch":
            raise ReleaseAppStartupError("release_runtime_mismatch", {"port": port})
        if state == "free" and first_free is None:
            first_free = port
            if port == requested_port:
                # There cannot be a competing service on the requested port;
                # avoid a needless scan of nineteen firewalled localhost
                # ports during normal startup.
                return "free", port
        elif state == "identity_unavailable":
            uncertain_ports.append(port)
    if uncertain_ports:
        raise ReleaseAppStartupError(
            "release_identity_unavailable",
            {"ports": uncertain_ports},
        )
    if first_free is not None:
        return "free", first_free
    raise ReleaseAppStartupError(
        "release_ports_unavailable",
        {"requested_port": requested_port, "last_port": last_port},
    )


def _startup_blocker(
    state: dict[str, Any], *, runtime_initialized: bool
) -> str | None:
    if not runtime_initialized:
        if state["recoverable_job_count"]:
            return "existing_jobs_require_review"
        if state["artifact_reconcile_candidate_count"]:
            return "existing_exam_artifacts_require_original_pdf_directory"
        if state["staged_attachment_count"]:
            return "staged_attachments_require_original_attachment_directory"
    return None


def _select_runtime_profile(settings_path: Path, requested: str | None) -> str:
    try:
        document = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseAppStartupError("runtime_settings_invalid", {}) from exc
    ai = document.get("ai") if isinstance(document, dict) else None
    profiles = ai.get("profiles") if isinstance(ai, dict) else None
    if not isinstance(profiles, dict) or not profiles:
        raise ReleaseAppStartupError("runtime_ai_profiles_missing", {})
    selected = str(requested or ai.get("active_profile") or "").strip()
    if not selected or selected not in profiles or not isinstance(profiles[selected], dict):
        raise ReleaseAppStartupError(
            "requested_runtime_profile_missing", {"requested_profile": selected}
        )
    if ai.get("active_profile") == selected:
        return selected
    ai["active_profile"] = selected
    revision = document.get("settings_revision")
    document["settings_revision"] = (
        int(revision) + 1
        if isinstance(revision, int) and not isinstance(revision, bool)
        else 1
    )
    temporary = settings_path.with_name(f".{settings_path.name}.profile-{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, settings_path)
    return selected


def _prepare_runtime(args: argparse.Namespace) -> tuple[Any, dict[str, Any]]:
    database = args.database.resolve(strict=True)
    runtime_root = args.runtime_root.resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    directories = {
        name: runtime_root / name
        for name in ("pdfs", "attachments", "memory", "job_inputs", "past_exam_profiles")
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    settings_path = runtime_root / "settings.json"
    catalog_path = runtime_root / "catalog.json"
    seeds = {
        "settings": _copy_seed_once(args.source_settings, settings_path),
        "catalog": _copy_seed_once(args.source_catalog, catalog_path),
    }
    selected_profile = _select_runtime_profile(settings_path, args.profile)
    paths = FormalRuntimePaths(
        project_root=PROJECT_ROOT, database=database, runtime_root=runtime_root,
        question_bank=args.question_bank.resolve(strict=True),
        formal_artifact=args.formal_artifact.resolve(strict=True),
    )
    overrides = paths.overrides(
        home_institution=args.home_institution, recall_algorithm=args.recall_algorithm,
    )
    overrides.update(TESTING=False, SERVICE_INSTANCE_ID=RELEASE_INSTANCE_ID)
    app = create_app(overrides)
    return app, {
        "database": str(database),
        "runtime_root": str(runtime_root),
        "settings_seed": seeds["settings"],
        "catalog_seed": seeds["catalog"],
        "active_profile": selected_profile,
        "pdf_dir": str(directories["pdfs"]),
        "job_input_dir": str(directories["job_inputs"]),
    }


def run(args: argparse.Namespace) -> int:
    configure_process_temp(args.runtime_root)
    if not args.database.is_file():
        raise ReleaseAppStartupError("formal_database_missing", {"database": str(args.database)})
    port_state, selected_port = _select_release_port(
        args.port, expected_identity=runtime_identity(args.database, args.runtime_root)
    )
    url = f"http://127.0.0.1:{selected_port}"
    if port_state == "same_release":
        print(
            json.dumps(
                {"status": "already_running", "url": url, "provider_workers_started": 0},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    with database_service_guard(args.database):
        with diagnostic_session(args.runtime_root / "diagnostics", "source-cmd"):
            try:
                return _run_locked(args, selected_port, url)
            except WorkerShutdownTimeout as error:
                terminate_after_shutdown_timeout(error)


def _run_locked(args: argparse.Namespace, selected_port: int, url: str) -> int:
    state = _startup_state(args.database)
    initialized = _runtime_initialized(args.runtime_root.resolve())
    state["runtime_initialized"] = initialized
    blocker = _startup_blocker(state, runtime_initialized=initialized)
    if blocker is not None:
        raise ReleaseAppStartupError(blocker, state)

    app, runtime = _prepare_runtime(args)
    from werkzeug.serving import make_server

    try:
        server = make_server("127.0.0.1", selected_port, app, threaded=True)
    except (OSError, SystemExit) as exc:
        raise ReleaseAppStartupError(
            "release_port_bind_failed", {"port": selected_port}
        ) from exc
    try:
        _start_release_workers(app)
        print(
            json.dumps(
                {"status": "started", "url": url, "startup_state": state,
                 "runtime": runtime, "database_copies": 0},
                ensure_ascii=False,
            ),
            flush=True,
        )
        if not args.no_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        server.serve_forever()
    finally:
        try:
            stop_job_worker(app)
        finally:
            server.server_close()
    return 0


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--runtime-root", type=Path, default=DEFAULT_RUNTIME_ROOT)
    parser.add_argument("--source-settings", type=Path, default=DEFAULT_SETTINGS)
    parser.add_argument("--source-catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--question-bank", type=Path, default=DEFAULT_QUESTION_BANK)
    parser.add_argument("--formal-artifact", type=Path, default=DEFAULT_FORMAL_ARTIFACT)
    parser.add_argument("--home-institution", default="")
    parser.add_argument("--recall-algorithm", default="semantic-recall-v2")
    parser.add_argument(
        "--profile",
        help="Select an existing profile in the runtime settings copy",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()
    if not 1 <= args.port <= 65535:
        print(json.dumps({"status": "blocked", "code": "invalid_port"}))
        return 2
    try:
        return run(args)
    except FormalRuntimeError as exc:
        print(json.dumps({"status": "blocked", "code": exc.code,
                          "message": "该数据库的服务已运行，请使用现有窗口或先停止服务。"}, ensure_ascii=False))
        return 2
    except ReleaseAppStartupError as exc:
        print(
            json.dumps(
                {"status": "blocked", "code": exc.code, **exc.details},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
