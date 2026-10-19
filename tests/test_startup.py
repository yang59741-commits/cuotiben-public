import subprocess
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import launcher
from app import build_argument_parser
from cuoti_app.config import ConfigError
from cuoti_app.paths import resolve_data_root, resolve_resource_root
from tools import check_environment
from cuoti_app.services.settings import KEYRING_SERVICE
from cuoti_app.services import environment_check
from cuoti_app.services import runtime_diagnostics


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class EnvironmentCheckTests(unittest.TestCase):
    def test_diagnostics_read_formal_settings_instead_of_legacy_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings_path = root / "data" / "runtime" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"xelatex_command": "formal-xelatex"}), encoding="utf-8"
            )
            legacy_dir = root / "instance"
            legacy_dir.mkdir()
            (legacy_dir / "settings.json").write_text("invalid legacy JSON", encoding="utf-8")
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(environment_check, "SETTINGS_PATH", settings_path),
                mock.patch("cuoti_app.config.INSTANCE_DIR", legacy_dir),
                mock.patch.object(environment_check.importlib.util, "find_spec", return_value=object()),
                mock.patch.object(environment_check.shutil, "which", return_value="xelatex") as find_tex,
                mock.patch.object(environment_check, "ai_credential_status", return_value=(False, "none")) as credential,
            ):
                report = environment_check.collect_checks()

            self.assertTrue(report.ok, report.checks)
            find_tex.assert_called_once_with("formal-xelatex")
            self.assertEqual(credential.call_args.args[0]["SETTINGS_PATH"], str(settings_path))
            self.assertFalse((root / "data" / "cuoti.db").exists())

    def test_missing_formal_settings_remains_an_environment_only_warning(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            settings_path = root / "data" / "runtime" / "settings.json"
            with (
                mock.patch.dict(os.environ, {"XELATEX_COMMAND": "env-xelatex"}, clear=True),
                mock.patch.object(environment_check, "SETTINGS_PATH", settings_path),
                mock.patch("cuoti_app.config.INSTANCE_DIR", root / "instance"),
                mock.patch.object(environment_check.importlib.util, "find_spec", return_value=object()),
                mock.patch.object(environment_check.shutil, "which", return_value="xelatex") as find_tex,
                mock.patch.object(environment_check, "ai_credential_status", return_value=(False, "none")),
            ):
                report = environment_check.collect_checks()

            self.assertTrue(report.ok, report.checks)
            find_tex.assert_called_once_with("env-xelatex")
            self.assertTrue(any(
                item.level == "warning" and "data/runtime/settings.json" in item.message
                for item in report.checks
            ))
            self.assertEqual(list(root.iterdir()), [])

    def test_ai_credential_check_reads_windows_credential_backend(self):
        class FakeKeyring:
            def get_password(self, service, username):
                self.request = (service, username)
                return "secret-for-test"

        backend = FakeKeyring()
        with (
            tempfile.TemporaryDirectory() as folder,
            mock.patch(
                "cuoti_app.services.settings._default_keyring_backend",
                return_value=backend,
            ),
        ):
            settings_path = Path(folder) / "settings.json"
            settings_path.write_text(
                '{"ai":{"active_profile":"deepseek","profiles":{"deepseek":'
                '{"credential_ref":"deepseek"}}}}',
                encoding="utf-8",
            )
            configured, source = environment_check.ai_credential_status(
                {"SETTINGS_PATH": str(settings_path)}
            )
        self.assertTrue(configured)
        self.assertEqual(source, "windows_keyring")
        self.assertEqual(backend.request, (KEYRING_SERVICE, "deepseek"))

    def test_invalid_configuration_returns_structured_failure(self):
        with mock.patch.object(
            environment_check,
            "build_config",
            side_effect=ConfigError("测试配置错误"),
        ):
            report = check_environment.collect_checks()
        self.assertFalse(report.ok)
        self.assertTrue(
            any(
                item.level == "error" and "测试配置错误" in item.message
                for item in report.checks
            )
        )

    def test_cli_keeps_success_and_failure_exit_codes(self):
        ok_report = check_environment.EnvironmentReport(
            (check_environment.CheckResult("ok", "正常"),)
        )
        failed_report = check_environment.EnvironmentReport(
            (check_environment.CheckResult("error", "失败"),)
        )
        with mock.patch.object(
            check_environment, "collect_checks", return_value=ok_report
        ):
            self.assertEqual(check_environment.main(), 0)
        with mock.patch.object(
            check_environment, "collect_checks", return_value=failed_report
        ):
            self.assertEqual(check_environment.main(), 1)


class ServiceInspectionTests(unittest.TestCase):
    def test_free_port_is_detected(self):
        with mock.patch.object(
            launcher.socket,
            "create_connection",
            side_effect=OSError("not listening"),
        ):
            state = launcher.inspect_service(timeout=0.01)
        self.assertEqual(state.kind, "free")

    def test_existing_notebook_is_detected(self):
        body = json.dumps(
            {
                "contract_version": "1",
                "overall_status": "degraded",
                "checked_at": "2026-08-06T00:00:00+00:00",
                "checks": [],
            }
        ).encode()
        with (
            mock.patch.object(
                launcher.socket,
                "create_connection",
                return_value=mock.MagicMock(),
            ),
            mock.patch.object(
                launcher.urllib.request,
                "urlopen",
                return_value=FakeResponse(body),
            ),
        ):
            state = launcher.inspect_service(timeout=0.01)
        self.assertEqual(state.kind, "running")
        self.assertIn("部分功能受限", state.message)

    def test_health_404_is_the_only_legacy_fallback(self):
        legacy = (
            b"<title>cuoti v2</title>"
            b"<link href=\"/static/css/app.css\">"
        ).replace(b"cuoti", "错题本".encode("utf-8"))
        missing = urllib.error.HTTPError(
            "http://127.0.0.1:8080/api/v2/health",
            404,
            "missing",
            {},
            None,
        )
        with (
            mock.patch.object(
                launcher.socket,
                "create_connection",
                return_value=mock.MagicMock(),
            ),
            mock.patch.object(
                launcher.urllib.request,
                "urlopen",
                side_effect=[missing, FakeResponse(legacy)],
            ) as urlopen,
        ):
            state = launcher.inspect_service(timeout=0.01)
        self.assertEqual(state.kind, "running")
        self.assertEqual(urlopen.call_count, 2)

    def test_malformed_health_does_not_fall_back_to_homepage(self):
        with (
            mock.patch.object(
                launcher.socket,
                "create_connection",
                return_value=mock.MagicMock(),
            ),
            mock.patch.object(
                launcher.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"not-json"),
            ) as urlopen,
        ):
            state = launcher.inspect_service(timeout=0.01)
        self.assertEqual(state.kind, "occupied")
        self.assertIn("JSON", state.message)
        self.assertEqual(urlopen.call_count, 1)

    def test_unrelated_process_is_reported_as_conflict(self):
        with (
            mock.patch.object(
                launcher.socket,
                "create_connection",
                return_value=mock.MagicMock(),
            ),
            mock.patch.object(
                launcher.urllib.request,
                "urlopen",
                return_value=FakeResponse(b"<title>Other app</title>"),
            ),
        ):
            state = launcher.inspect_service(timeout=0.01)
        self.assertEqual(state.kind, "occupied")


class ServiceControllerTests(unittest.TestCase):
    def make_controller(self, state, process_factory=None):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        return launcher.ServiceController(
            root=Path(temp_dir.name),
            inspector=lambda: launcher.ServiceState(state, "test"),
            process_factory=process_factory or mock.Mock(),
            log_path=Path(temp_dir.name) / "launcher.log",
        )

    def test_normal_start_uses_no_browser_flag(self):
        process = FakeProcess()
        factory = mock.Mock(return_value=process)
        controller = self.make_controller("free", factory)
        result = controller.start()
        self.assertEqual(result.kind, "started")
        command = factory.call_args.args[0]
        self.assertIn("--no-browser", command)
        self.assertTrue(controller.owns_running_service)

    def test_frozen_start_reuses_executable_without_console(self):
        process = FakeProcess()
        factory = mock.Mock(return_value=process)
        controller = launcher.ServiceController(
            root=Path("C:/notebook"),
            inspector=lambda: launcher.ServiceState("free", "test"),
            process_factory=factory,
            log_path=Path(tempfile.gettempdir()) / "launcher-frozen-test.log",
            frozen=True,
            executable=Path("C:/notebook/错题本.exe"),
            environment={"PATH": "test"},
        )

        controller.start()

        self.assertEqual(
            factory.call_args.args[0],
            ["C:\\notebook\\错题本.exe", "--server"],
        )
        kwargs = factory.call_args.kwargs
        self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
        self.assertEqual(
            kwargs["creationflags"],
            getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_duplicate_service_is_not_started_twice(self):
        factory = mock.Mock()
        controller = self.make_controller("running", factory)
        result = controller.start()
        self.assertEqual(result.kind, "already_running")
        factory.assert_not_called()

    def test_port_conflict_blocks_start(self):
        factory = mock.Mock()
        controller = self.make_controller("occupied", factory)
        result = controller.start()
        self.assertEqual(result.kind, "port_conflict")
        factory.assert_not_called()

    def test_stop_terminates_owned_process(self):
        process = FakeProcess()
        controller = self.make_controller(
            "free",
            mock.Mock(return_value=process),
        )
        controller.start()
        self.assertTrue(controller.stop())
        self.assertTrue(process.terminated)
        self.assertFalse(controller.owns_running_service)

    def test_stop_escalates_after_timeout(self):
        process = FakeProcess()

        def timeout_wait(timeout=None):
            del timeout
            if not process.killed:
                raise subprocess.TimeoutExpired("app", 1)
            return process.returncode

        process.wait = timeout_wait
        controller = self.make_controller(
            "free",
            mock.Mock(return_value=process),
        )
        controller.start()
        controller.stop(timeout=0.01)
        self.assertTrue(process.killed)


class StartupCompatibilityTests(unittest.TestCase):
    def test_no_browser_argument_is_available(self):
        parsed = build_argument_parser().parse_args(["--no-browser"])
        self.assertTrue(parsed.no_browser)

    def test_batch_prefers_exe_then_uses_verified_python_fallback(self):
        source = (
            Path(__file__).resolve().parents[1] / "启动.bat"
        ).read_text(encoding="utf-8")
        self.assertIn('if exist "错题本.exe"', source)
        self.assertIn('start "" "错题本.exe"', source)
        self.assertIn('import flask, customtkinter, PIL', source)
        self.assertIn('"%CUOTI_PYTHONW_EXE%" launcher.py', source)

    def test_launcher_server_argument_is_internal_entrypoint(self):
        parsed = launcher.build_argument_parser().parse_args(["--server"])
        self.assertTrue(parsed.server)

    def test_environment_check_does_not_auto_start_service(self):
        window = object.__new__(launcher.LauncherWindow)
        window.controller = mock.Mock()
        window.controller.owns_running_service = False
        window.recheck_button = mock.Mock()
        window.stop_button = mock.Mock()
        window._write_report = mock.Mock()
        window._set_status = mock.Mock()
        window._set_primary = mock.Mock()
        window.toggle_details = mock.Mock()
        window._start_service = mock.Mock()
        report = check_environment.EnvironmentReport(
            (check_environment.CheckResult("ok", "正常"),)
        )

        with mock.patch.object(launcher, "diagnostic_notice", return_value=""):
            window._finish_check(report, launcher.ServiceState("free", "未启动"))

        self.assertTrue(window.environment_ready)
        window._set_primary.assert_called_once_with(
            "▶   启动错题本", enabled=True
        )
        window._start_service.assert_not_called()

    def test_server_mode_redirects_output_to_log(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "server.log"
            with mock.patch("app.run") as run:
                result = launcher.run_server_mode(log_path)

            self.assertEqual(result, 0)
            run.assert_called_once_with(open_browser=False, port=8080)
            self.assertTrue(log_path.exists())

    def test_server_failure_keeps_extended_codes_when_diagnostics_unwritable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            log_path = root / "server.log"
            directory = root / "data/runtime/diagnostics"
            denied = PermissionError(13, "synthetic denied", str(directory), 5)
            failure = sqlite3.OperationalError("synthetic disk I/O error")
            failure.sqlite_errorcode = sqlite3.SQLITE_IOERR_SHMOPEN
            failure.sqlite_errorname = "SQLITE_IOERR_SHMOPEN"

            def fail_start(**_kwargs):
                with runtime_diagnostics.diagnostic_session(directory, "desktop"):
                    raise failure

            previous_streams = sys.stdout, sys.stderr
            with (
                mock.patch.object(launcher, "ROOT", root),
                mock.patch("app.run", side_effect=fail_start),
                mock.patch.object(runtime_diagnostics.DiagnosticSession, "start", side_effect=denied),
                mock.patch.object(runtime_diagnostics, "write_json", side_effect=denied),
                mock.patch.object(runtime_diagnostics.Journal, "event"),
            ):
                result = launcher.run_server_mode(log_path, port=18765)

            self.assertEqual(result, 1)
            self.assertEqual((sys.stdout, sys.stderr), previous_streams)
            lines = log_path.read_text(encoding="utf-8").splitlines()
            start, degraded, error = [json.loads(line.split("：", 1)[-1]) for line in lines[:3]]
            self.assertEqual(start["event"], "server_launch")
            self.assertEqual(start["database"], str(root / "data/cuoti.db"))
            self.assertEqual(start["executable"], sys.executable)
            self.assertEqual(start["port"], 18765)
            self.assertEqual(degraded["filename"], str(directory))
            self.assertEqual(degraded["errno"], 13)
            if os.name == "nt":
                self.assertEqual(degraded["winerror"], 5)
            self.assertEqual(error["event"], "server_failed")
            self.assertEqual(error["sqlite_errorcode"], sqlite3.SQLITE_IOERR_SHMOPEN)
            self.assertEqual(error["sqlite_errorname"], "SQLITE_IOERR_SHMOPEN")
            self.assertTrue(all(item["time"] and item["pid"] == os.getpid() for item in (start, degraded, error)))
            self.assertIn("Traceback", log_path.read_text(encoding="utf-8"))

    def test_server_os_error_records_affected_path(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "server.log"
            failure = PermissionError(13, "synthetic denied", str(Path(temp_dir) / "cuoti.db"), 5)
            with mock.patch("app.run", side_effect=failure):
                self.assertEqual(launcher.run_server_mode(log_path), 1)
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[1])
            self.assertEqual(record["exception_type"], "PermissionError")
            self.assertEqual(record["filename"], failure.filename)
            self.assertEqual(record["errno"], failure.errno)

    def test_saved_crash_notice_is_visible_without_blocking_start(self):
        window = object.__new__(launcher.LauncherWindow)
        for name in ('controller', 'recheck_button', 'stop_button', '_write_report',
                     '_set_status', '_set_primary', 'toggle_details'):
            setattr(window, name, mock.Mock())
        report = check_environment.EnvironmentReport((check_environment.CheckResult('ok', '正常'),))
        with mock.patch.object(launcher, 'diagnostic_notice', return_value='最近异常：已保留崩溃现场'):
            window._finish_check(report, launcher.ServiceState('free', '未启动'))
        self.assertTrue(window.environment_ready)
        self.assertEqual(window._write_report.call_args.args[0][-1].message, '最近异常：已保留崩溃现场')
        window.toggle_details.assert_called_once_with(force_open=True)
        window._set_primary.assert_called_once_with('▶   启动错题本', enabled=True)

    def test_historical_crash_notice_stays_in_report_without_reopening_drawer(self):
        window = object.__new__(launcher.LauncherWindow)
        for name in ('controller', 'recheck_button', 'stop_button', '_write_report',
                     '_set_status', '_set_primary', 'toggle_details'):
            setattr(window, name, mock.Mock())
        report = check_environment.EnvironmentReport((check_environment.CheckResult('ok', '正常'),))
        with mock.patch.object(launcher, 'diagnostic_notice', return_value='历史异常：已保留崩溃现场'):
            window._finish_check(report, launcher.ServiceState('free', '未启动'))
        self.assertTrue(window.environment_ready)
        self.assertEqual(window._write_report.call_args.args[0][-1].message, '历史异常：已保留崩溃现场')
        window.toggle_details.assert_not_called()
        window._set_primary.assert_called_once_with('▶   启动错题本', enabled=True)

    def test_log_button_opens_diagnostic_evidence_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            window = object.__new__(launcher.LauncherWindow)
            root = Path(folder)
            diagnostics = root / 'data/runtime/diagnostics'
            diagnostics.mkdir(parents=True)
            window.controller = mock.Mock(root=root, log_path=root / 'startup.log')
            with mock.patch.object(launcher.os, 'startfile', create=True) as open_file:
                window.open_log()
            open_file.assert_called_once_with(diagnostics)


class FrozenPathTests(unittest.TestCase):
    def test_frozen_data_stays_next_to_executable(self):
        path = resolve_data_root(
            frozen=True,
            executable=Path("C:/Apps/Cuoti/错题本.exe"),
        )
        self.assertEqual(path, Path("C:/Apps/Cuoti").resolve())

    def test_frozen_resources_use_bundle_directory(self):
        path = resolve_resource_root(
            frozen=True,
            bundle_dir=Path("C:/Temp/_MEI12345"),
        )
        self.assertEqual(path, Path("C:/Temp/_MEI12345").resolve())


if __name__ == "__main__":
    unittest.main()
