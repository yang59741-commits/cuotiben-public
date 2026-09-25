from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import launcher
from cuoti_app.services.launcher_runtime import (
    OwnedServiceRecord,
    read_log_tail,
    read_service_state,
    rotate_log,
    validate_owned_service,
    write_service_state,
)


class LauncherOwnershipTests(unittest.TestCase):
    def test_state_round_trip_and_all_identity_factors_are_required(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service.json"
            record = OwnedServiceRecord(42, 123456, "C:/Apps/cuoti.exe", "127.0.0.1", 8080, "instance-1")
            write_service_state(path, record)
            loaded = read_service_state(path)
            self.assertEqual(loaded, record)
            self.assertTrue(validate_owned_service(
                loaded, url="http://127.0.0.1:8080", host="127.0.0.1", port=8080,
                identity_reader=lambda _pid: ("C:/Apps/cuoti.exe", 123456),
                health_reader=lambda _url: ("instance-1", 42),
            ))
            for identity, health, port in (
                (("C:/Apps/other.exe", 123456), ("instance-1", 42), 8080),
                (("C:/Apps/cuoti.exe", 999), ("instance-1", 42), 8080),
                (("C:/Apps/cuoti.exe", 123456), ("other-instance", 42), 8080),
                (("C:/Apps/cuoti.exe", 123456), ("instance-1", 42), 8081),
            ):
                self.assertFalse(validate_owned_service(
                    loaded, url="http://127.0.0.1:8080", host="127.0.0.1", port=port,
                    identity_reader=lambda _pid, value=identity: value,
                    health_reader=lambda _url, value=health: value,
                ))

    def test_controller_records_process_fingerprint_and_can_adopt_it(self):
        class Process:
            pid = 4321
            def poll(self): return None
            def terminate(self): pass
            def wait(self, timeout=None): return 0

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            controller = launcher.ServiceController(
                root=Path(directory), state_path=state_path,
                inspector=lambda: launcher.ServiceState("free", "free"),
                process_factory=mock.Mock(return_value=Process()),
                log_path=Path(directory) / "launcher.log",
            )
            with mock.patch.object(launcher, "process_identity", return_value=("C:/Python/python.exe", 9876)):
                controller.start()
            record = read_service_state(state_path)
            self.assertEqual((record.pid, record.created), (4321, 9876))
            self.assertEqual(record.port, 8080)

            adopted = launcher.ServiceController(
                root=Path(directory), state_path=state_path,
                inspector=lambda: launcher.ServiceState("running", "running"),
                log_path=Path(directory) / "launcher.log",
            )
            with (
                mock.patch.object(launcher, "validate_owned_service", return_value=True),
                mock.patch.object(launcher, "terminate_owned_pid", return_value=True) as terminate,
            ):
                self.assertTrue(adopted.owns_running_service)
                self.assertTrue(adopted.stop())
            terminate.assert_called_once_with(4321, timeout=4.0)
            self.assertFalse(state_path.exists())

    def test_unverified_state_never_allows_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_service_state(path, OwnedServiceRecord(5, 1, "C:/other.exe", "127.0.0.1", 8080, "wrong"))
            controller = launcher.ServiceController(root=Path(directory), state_path=path, inspector=lambda: launcher.ServiceState("running", "running"))
            with (
                mock.patch.object(launcher, "validate_owned_service", return_value=False),
                mock.patch.object(launcher, "terminate_owned_pid") as terminate,
            ):
                self.assertFalse(controller.stop())
            terminate.assert_not_called()

    def test_frozen_bootloader_pid_is_replaced_by_listening_child_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            controller = launcher.ServiceController(root=Path(directory), state_path=state)
            controller.instance_id = "instance-child"
            controller.process = mock.Mock(pid=100)
            with (
                mock.patch.object(launcher, "health_service_identity", return_value=("instance-child", 200)),
                mock.patch.object(launcher, "process_identity", return_value=("C:/Apps/cuoti.exe", 555)),
            ):
                self.assertTrue(controller.refresh_ownership())
            record = read_service_state(state)
            self.assertEqual(record.pid, 200)
            self.assertIsNone(controller.process)
            self.assertEqual(controller._adopted_record.pid, 200)


class LauncherLogTests(unittest.TestCase):
    def test_rotation_tail_limit_and_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "launcher.log"
            path.write_text("old" * 100, encoding="utf-8")
            rotate_log(path, max_bytes=10, backups=2)
            self.assertFalse(path.exists())
            self.assertTrue(path.with_name("launcher.log.1").exists())
            path.write_text("first\nAuthorization: Bearer super-secret\napi_key=hidden-value\nsk-abcdefghijk\nlast", encoding="utf-8")
            tail = read_log_tail(path, max_lines=3, max_bytes=1000)
            self.assertNotIn("hidden-value", tail)
            self.assertNotIn("sk-abcdefghijk", tail)
            self.assertEqual(len(tail.splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
