"""Modern desktop launcher for the local mistake notebook."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
import webbrowser
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

try:
    import customtkinter as ctk
    from PIL import Image, ImageTk
except ImportError:  # The source fallback shows an actionable dependency error.
    ctk = None
    Image = None
    ImageTk = None

try:
    import pystray
except ImportError:
    pystray = None

from cuoti_app.paths import DATA_DIR, RESOURCE_DIR
from cuoti_app.services.launcher_runtime import (
    OwnedServiceRecord,
    autostart_enabled,
    health_service_identity,
    process_identity,
    read_log_tail,
    read_launcher_preferences,
    read_service_state,
    rotate_log,
    set_autostart,
    terminate_owned_pid,
    validate_owned_service,
    write_launcher_preferences,
    write_service_state,
)
from tools.check_environment import CheckResult, EnvironmentReport, collect_checks


APP_URL = "http://127.0.0.1:8080"
APP_HOST = "127.0.0.1"
APP_PORT = 8080
STARTUP_TIMEOUT_SECONDS = 180.0
WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 625
ROOT = DATA_DIR
LOGO_PATH = RESOURCE_DIR / "cuoti_app" / "static" / "img" / "logo.png"
BACKGROUND_PATH = RESOURCE_DIR / "launcher_assets" / "night_desk.png"
LAUNCH_LOG = Path(tempfile.gettempdir()) / "cuoti-notebook-launcher.log"

INK = "#0B1116"
PANEL = "#111A20"
PANEL_ALT = "#17242A"
IVORY = "#F7F1E8"
MUTED = "#B7C2BE"
TEAL = "#78B7A0"
GOLD = "#E8B95E"
GOLD_HOVER = "#F1C974"
SUCCESS = "#7BC39B"
WARNING = "#E8B36B"
ERROR = "#E98076"


@dataclass(frozen=True, slots=True)
class ServiceState:
    kind: str
    message: str


@dataclass(frozen=True, slots=True)
class StartResult:
    kind: str
    message: str


def inspect_service(
    host: str = APP_HOST,
    port: int = APP_PORT,
    url: str = APP_URL,
    *,
    timeout: float = 0.7,
) -> ServiceState:
    """Distinguish a free port, this application, and an unrelated process."""

    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError:
        return ServiceState("free", "服务尚未启动")

    health_request = urllib.request.Request(
        f"{url.rstrip('/')}/api/v2/health",
        headers={"User-Agent": "CuotiNotebookLauncher/2.0"},
    )
    try:
        with urllib.request.urlopen(health_request, timeout=timeout) as response:
            body = response.read(65536)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _inspect_legacy_service(url, timeout=timeout)
        if exc.code == 503:
            body = exc.read(65536)
        else:
            return ServiceState(
                "occupied",
                "检测到服务，但健康接口返回异常状态",
            )
    except (OSError, urllib.error.URLError):
        return ServiceState("occupied", "8080 端口已被其他程序占用")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ServiceState("occupied", "检测到服务，但健康响应不是有效 JSON")
    if (
        not isinstance(payload, dict)
        or str(payload.get("contract_version")) != "1"
        or payload.get("overall_status") not in {"healthy", "degraded", "fatal"}
        or not isinstance(payload.get("checks"), list)
    ):
        return ServiceState("occupied", "检测到服务，但健康契约不兼容")
    messages = {
        "healthy": "错题本服务正在运行",
        "degraded": "错题本服务正在运行，部分功能受限",
        "fatal": "错题本服务正在运行，但核心检查失败",
    }
    return ServiceState("running", messages[payload["overall_status"]])


def _inspect_legacy_service(url: str, *, timeout: float) -> ServiceState:
    """Use the previous HTML identity marker only for one 404 compatibility cycle."""

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CuotiNotebookLauncher/2.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(8192).decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError):
        return ServiceState("occupied", "8080 端口已被其他程序占用")
    if "<title>错题本 v2</title>" in body and "/static/css/app.css" in body:
        return ServiceState("running", "旧版错题本服务正在运行")
    return ServiceState("occupied", "8080 端口已被其他程序占用")


def build_server_command(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    root: Path = ROOT,
) -> list[str]:
    """Build the source or frozen command used for the background service."""

    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable = str(executable or sys.executable)
    if frozen:
        return [executable, "--server"]
    return [executable, str(Path(root) / "app.py"), "--no-browser"]


class ServiceController:
    """Start and stop only the server process owned by this launcher."""

    def __init__(
        self,
        *,
        root: Path = ROOT,
        inspector=inspect_service,
        process_factory=subprocess.Popen,
        log_path: Path = LAUNCH_LOG,
        frozen: bool | None = None,
        executable: str | Path | None = None,
        environment: dict[str, str] | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.root = Path(root)
        self.inspector = inspector
        self.process_factory = process_factory
        self.log_path = Path(log_path)
        self.frozen = (
            bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        )
        self.executable = str(executable or sys.executable)
        self.environment = dict(environment or os.environ)
        self.state_path = Path(state_path or (self.root / "instance" / "launcher-service.json"))
        self.process = None
        self._adopted_record = None
        self.instance_id = None

    @property
    def owns_running_service(self) -> bool:
        if self.process is not None:
            if self.process.poll() is None:
                return True
            self.process = None
        record = read_service_state(self.state_path)
        if record and validate_owned_service(
            record, url=APP_URL, host=APP_HOST, port=APP_PORT
        ):
            self._adopted_record = record
            return True
        self._adopted_record = None
        return False

    def start(self) -> StartResult:
        state = self.inspector()
        if state.kind == "running":
            return StartResult("already_running", state.message)
        if state.kind == "occupied":
            return StartResult("port_conflict", state.message)

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rotate_log(self.log_path)
        command = build_server_command(
            frozen=self.frozen,
            executable=self.executable,
            root=self.root,
        )
        environment = self.environment.copy()
        instance_id = str(uuid.uuid4())
        self.instance_id = instance_id
        environment["CUOTI_SERVICE_INSTANCE_ID"] = instance_id
        if self.frozen:
            environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self.log_path.open("ab", buffering=0) as log_handle:
            self.process = self.process_factory(
                command,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                env=environment,
            )
        pid = getattr(self.process, "pid", None)
        identity = process_identity(pid) if isinstance(pid, int) else None
        if identity:
            write_service_state(
                self.state_path,
                OwnedServiceRecord(
                    pid=pid, created=identity[1], executable=identity[0],
                    host=APP_HOST, port=APP_PORT, instance_id=instance_id,
                ),
            )
        return StartResult("started", "正在启动错题本服务")

    def refresh_ownership(self) -> bool:
        """Replace a one-file bootloader PID with the actual listening child PID."""
        identity = health_service_identity(APP_URL)
        if not identity or identity[0] != self.instance_id:
            return False
        pid = identity[1]
        fingerprint = process_identity(pid)
        if not fingerprint:
            return False
        record = OwnedServiceRecord(
            pid=pid, created=fingerprint[1], executable=fingerprint[0],
            host=APP_HOST, port=APP_PORT, instance_id=identity[0],
        )
        write_service_state(self.state_path, record)
        if self.process is None or getattr(self.process, "pid", None) != pid:
            self.process = None
            self._adopted_record = record
        return True

    def stop(self, *, timeout: float = 4.0) -> bool:
        if not self.owns_running_service:
            self.process = None
            return False
        if self.process is not None:
            process = self.process
            process.terminate()
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        elif self._adopted_record is not None:
            record = self._adopted_record
            if not validate_owned_service(record, url=APP_URL, host=APP_HOST, port=APP_PORT):
                return False
            if not terminate_owned_pid(record.pid, timeout=timeout):
                return False
        self.process = None
        self._adopted_record = None
        self.instance_id = None
        try:
            self.state_path.unlink()
        except FileNotFoundError:
            pass
        return True


def autostart_command(*, frozen: bool | None = None) -> str:
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    if frozen:
        return f'"{Path(sys.executable).resolve()}"'
    return f'"{Path(sys.executable).resolve()}" "{(ROOT / "launcher.py").resolve()}"'


class TrayController:
    """Optional Windows tray menu with bounded reminder polling."""

    def __init__(self, window) -> None:
        self.window = window
        self.icon = None
        self.preferences_path = ROOT / "instance" / "launcher-preferences.json"
        self.preferences = read_launcher_preferences(self.preferences_path)

    @property
    def available(self) -> bool:
        return pystray is not None and Image is not None and LOGO_PATH.is_file()

    def start(self) -> None:
        if not self.available:
            return
        image = Image.open(LOGO_PATH).convert("RGBA")
        self.icon = pystray.Icon(
            "cuoti-notebook", image, "错题本",
            menu=pystray.Menu(
                pystray.MenuItem("打开错题本", self._open, default=True),
                pystray.MenuItem("检查今日复习", self._remind_now),
                pystray.MenuItem("复习提醒", self._toggle_reminders, checked=lambda _item: bool(self.preferences["reminders_enabled"])),
                pystray.MenuItem("开机自启", self._toggle_autostart, checked=lambda _item: autostart_enabled()),
                pystray.MenuItem("停止服务", self._stop_service),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出启动中心", self._exit),
            ),
        )
        threading.Thread(target=self.icon.run, daemon=True, name="cuoti-tray").start()

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

    def _on_ui(self, callback) -> None:
        try:
            self.window.root.after(0, callback)
        except tk.TclError:
            pass

    def _open(self, _icon=None, _item=None) -> None:
        self._on_ui(self.window.open_browser)

    def _stop_service(self, _icon=None, _item=None) -> None:
        self._on_ui(self.window.stop_service)

    def _exit(self, _icon=None, _item=None) -> None:
        self._on_ui(self.window.exit_from_tray)

    def _toggle_autostart(self, _icon=None, _item=None) -> None:
        set_autostart(not autostart_enabled(), autostart_command())
        if self.icon is not None:
            self.icon.update_menu()

    def _toggle_reminders(self, _icon=None, _item=None) -> None:
        self.preferences["reminders_enabled"] = not self.preferences["reminders_enabled"]
        write_launcher_preferences(self.preferences_path, self.preferences)
        if self.icon is not None:
            self.icon.update_menu()

    def _remind_now(self, _icon=None, _item=None) -> None:
        self.refresh_reminder(force=True)

    def refresh_reminder(self, *, force: bool = False) -> None:
        if self.icon is None or not self.preferences["reminders_enabled"]:
            return
        def worker():
            try:
                with urllib.request.urlopen(f"{APP_URL}/api/v2/stats/reminders", timeout=1.5) as response:
                    payload = json.loads(response.read(65536).decode("utf-8"))
                today = str(payload.get("as_of") or "")
                due = int(payload.get("due_count") or 0)
                if due and (force or self.preferences["last_notified_day"] != today):
                    self.icon.notify(str(payload.get("message") or f"今天有 {due} 道题待复习"), "错题本复习提醒")
                    self.preferences["last_notified_day"] = today
                    write_launcher_preferences(self.preferences_path, self.preferences)
            except (OSError, ValueError, json.JSONDecodeError):
                return
        threading.Thread(target=worker, daemon=True, name="cuoti-reminder").start()


class CanvasButton:
    """Rounded button drawn directly over the launcher background canvas."""

    def __init__(
        self,
        canvas: tk.Canvas,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        text: str,
        command,
        fill: str,
        hover_fill: str,
        text_color: str,
        disabled_fill: str,
        disabled_text_color: str,
        font,
        radius: int = 10,
        state: str = "normal",
    ) -> None:
        self.canvas = canvas
        self.command = command
        self.fill = fill
        self.hover_fill = hover_fill
        self.text_color = text_color
        self.disabled_fill = disabled_fill
        self.disabled_text_color = disabled_text_color
        self.state = state
        self.tag = f"canvas-button-{id(self)}"
        points = [
            x + radius,
            y,
            x + width - radius,
            y,
            x + width,
            y,
            x + width,
            y + radius,
            x + width,
            y + height - radius,
            x + width,
            y + height,
            x + width - radius,
            y + height,
            x + radius,
            y + height,
            x,
            y + height,
            x,
            y + height - radius,
            x,
            y + radius,
            x,
            y,
        ]
        self.shape = canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline="",
            tags=(self.tag,),
        )
        self.label = canvas.create_text(
            x + width / 2,
            y + height / 2,
            text=text,
            fill=text_color,
            font=font,
            tags=(self.tag,),
        )
        canvas.tag_bind(self.tag, "<Enter>", self._on_enter)
        canvas.tag_bind(self.tag, "<Leave>", self._on_leave)
        canvas.tag_bind(self.tag, "<Button-1>", self._on_click)
        self._apply_style()

    def _on_enter(self, _event=None) -> None:
        if self.state == "normal":
            self.canvas.itemconfigure(self.shape, fill=self.hover_fill)
            self.canvas.configure(cursor="hand2")

    def _on_leave(self, _event=None) -> None:
        self._apply_style()
        self.canvas.configure(cursor="")

    def _on_click(self, _event=None) -> None:
        if self.state == "normal":
            self.command()

    def _apply_style(self) -> None:
        enabled = self.state == "normal"
        self.canvas.itemconfigure(
            self.shape,
            fill=self.fill if enabled else self.disabled_fill,
        )
        self.canvas.itemconfigure(
            self.label,
            fill=self.text_color if enabled else self.disabled_text_color,
        )

    def configure(self, **kwargs) -> None:
        if "text" in kwargs:
            self.canvas.itemconfigure(self.label, text=kwargs["text"])
        if "state" in kwargs:
            self.state = kwargs["state"]
        self._apply_style()


class LauncherWindow:
    """Full-bleed CustomTkinter launcher with a compact diagnostics drawer."""

    def __init__(self, root, controller: ServiceController | None = None):
        self.root = root
        self.controller = controller or ServiceController()
        self.environment_ready = False
        self.details_visible = False
        self.start_deadline = 0.0
        self._drag_origin = None
        self._minimized = False
        self.background_image = None
        self.canvas = None
        self.icon_image = None
        self.tray = TrayController(self)

        self._configure_window()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Escape>", self.hide_details, add="+")
        self.root.after(160, self.begin_check)
        self.tray.start()

    def _configure_window(self) -> None:
        self.root.title("错题本 · 启动中心")
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}")
        self.root.resizable(False, False)
        self.root.configure(fg_color=INK)
        self.root.overrideredirect(True)
        self.root.update_idletasks()
        x = max((self.root.winfo_screenwidth() - WINDOW_WIDTH) // 2, 0)
        y = max((self.root.winfo_screenheight() - WINDOW_HEIGHT) // 2, 0)
        self.root.geometry(f"{WINDOW_WIDTH}x{WINDOW_HEIGHT}+{x}+{y}")
        if LOGO_PATH.is_file():
            try:
                self.icon_image = tk.PhotoImage(file=str(LOGO_PATH))
                self.root.iconphoto(True, self.icon_image)
            except tk.TclError:
                self.icon_image = None
        self.root.bind("<Map>", self._restore_frameless, add="+")
        self.root.after(40, self._apply_windows_frame)

    def _build_ui(self) -> None:
        background = self._load_background()
        if background.size != (WINDOW_WIDTH, WINDOW_HEIGHT):
            background = background.resize(
                (WINDOW_WIDTH, WINDOW_HEIGHT),
                Image.Resampling.LANCZOS,
            )
        self.background_image = ImageTk.PhotoImage(background)
        self.canvas = tk.Canvas(
            self.root,
            width=WINDOW_WIDTH,
            height=WINDOW_HEIGHT,
            bd=0,
            highlightthickness=0,
        )
        self.canvas.place(x=0, y=0)
        self.canvas.create_image(0, 0, anchor="nw", image=self.background_image)
        self.canvas.bind("<ButtonPress-1>", self._begin_drag)
        self.canvas.bind("<B1-Motion>", self._drag_window)

        self._icon_button("—", self.minimize, 914, "最小化")
        self._icon_button("×", self.on_close, 958, "关闭")

        self.status_dot = self.canvas.create_text(
            49,
            484,
            text="●",
            fill=WARNING,
            font=("Microsoft YaHei UI", 13),
            anchor="w",
        )
        self.status_label = self.canvas.create_text(
            66,
            478,
            text="正在检查运行环境",
            fill=IVORY,
            font=("Microsoft YaHei UI", 15, "bold"),
            anchor="w",
        )
        self.status_hint = self.canvas.create_text(
            66,
            507,
            text="请稍候",
            fill=MUTED,
            font=("Microsoft YaHei UI", 11),
            anchor="w",
        )

        self.details_button = CanvasButton(
            self.canvas,
            x=300,
            y=549,
            width=116,
            height=32,
            text="运行状态  ›",
            command=self.toggle_details,
            fill=PANEL_ALT,
            hover_fill="#22343A",
            text_color=IVORY,
            disabled_fill="#263136",
            disabled_text_color="#78827F",
            font=("Microsoft YaHei UI", 12, "bold"),
            radius=8,
        )

        self.primary_button = CanvasButton(
            self.canvas,
            x=42,
            y=538,
            width=238,
            height=60,
            text="▶   启动错题本",
            command=self.primary_action,
            fill=GOLD,
            hover_fill=GOLD_HOVER,
            text_color=INK,
            disabled_fill="#8A7654",
            disabled_text_color="#4E473C",
            font=("Microsoft YaHei UI", 18, "bold"),
            radius=12,
            state="disabled",
        )

        self._build_details_panel()

    def _load_background(self):
        if BACKGROUND_PATH.is_file():
            return Image.open(BACKGROUND_PATH).convert("RGB")
        return Image.new("RGB", (WINDOW_WIDTH, WINDOW_HEIGHT), INK)

    def _icon_button(self, text: str, command, x: int, tooltip: str):
        tag = f"window-control-{tooltip}"
        item = self.canvas.create_text(
            x + 18,
            28,
            text=text,
            fill=IVORY,
            font=("Microsoft YaHei UI", 20),
            tags=(tag,),
        )
        self.canvas.tag_bind(
            tag,
            "<Enter>",
            lambda _event: (
                self.canvas.itemconfigure(item, fill=GOLD),
                self.canvas.configure(cursor="hand2"),
            ),
        )
        self.canvas.tag_bind(
            tag,
            "<Leave>",
            lambda _event: (
                self.canvas.itemconfigure(item, fill=IVORY),
                self.canvas.configure(cursor=""),
            ),
        )
        self.canvas.tag_bind(tag, "<Button-1>", lambda _event: command())
        return item

    def _build_details_panel(self) -> None:
        self.details_panel = ctk.CTkFrame(
            self.root,
            width=566,
            height=226,
            corner_radius=14,
            fg_color=PANEL,
            bg_color="#6B4931",
            border_width=1,
            border_color="#2B3A40",
        )
        ctk.CTkLabel(
            self.details_panel,
            text="启动检查",
            text_color=IVORY,
            font=ctk.CTkFont("Microsoft YaHei UI", 15, "bold"),
            anchor="w",
        ).place(x=20, y=14)
        ctk.CTkButton(
            self.details_panel,
            text="×",
            width=32,
            height=28,
            corner_radius=7,
            fg_color="transparent",
            hover_color="#263840",
            text_color=IVORY,
            font=ctk.CTkFont("Microsoft YaHei UI", 18),
            command=self.hide_details,
            cursor="hand2",
        ).place(x=514, y=10)
        self.report_box = ctk.CTkTextbox(
            self.details_panel,
            width=526,
            height=118,
            corner_radius=8,
            fg_color="#0D151A",
            border_width=0,
            text_color=MUTED,
            font=ctk.CTkFont("Microsoft YaHei UI", 11),
            wrap="word",
            activate_scrollbars=True,
        )
        self.report_box.place(x=20, y=45)
        self.report_box.tag_config("ok", foreground=SUCCESS)
        self.report_box.tag_config("warning", foreground=WARNING)
        self.report_box.tag_config("error", foreground=ERROR)
        self.report_box.configure(state="disabled")

        self.recheck_button = self._secondary_button(
            self.details_panel,
            "↻  重新检查",
            self.begin_check,
            20,
        )
        self.stop_button = self._secondary_button(
            self.details_panel,
            "■  停止服务",
            self.stop_service,
            142,
        )
        self.stop_button.configure(state="disabled")
        self._secondary_button(
            self.details_panel,
            "日志",
            self.open_log,
            264,
            width=72,
        )
        self.log_label = ctk.CTkLabel(
            self.details_panel,
            text=r"%TEMP%\cuoti-notebook-launcher.log",
            text_color="#83928F",
            font=ctk.CTkFont("Microsoft YaHei UI", 9),
            anchor="e",
            width=200,
        )
        self.log_label.place(x=344, y=184)

    def _secondary_button(
        self,
        parent,
        text: str,
        command,
        x: int,
        *,
        width: int = 112,
    ):
        button = ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=32,
            corner_radius=8,
            fg_color=PANEL_ALT,
            hover_color="#24383E",
            text_color=IVORY,
            text_color_disabled="#60706F",
            font=ctk.CTkFont("Microsoft YaHei UI", 11, "bold"),
            command=command,
            cursor="hand2",
        )
        button.place(x=x, y=178)
        return button

    def _begin_drag(self, event) -> None:
        if event.y_root - self.root.winfo_y() <= 54:
            self._drag_origin = (event.x_root, event.y_root)

    def _drag_window(self, event) -> None:
        if self._drag_origin is None:
            return
        dx = event.x_root - self._drag_origin[0]
        dy = event.y_root - self._drag_origin[1]
        self.root.geometry(
            f"+{self.root.winfo_x() + dx}+{self.root.winfo_y() + dy}"
        )
        self._drag_origin = (event.x_root, event.y_root)

    def _apply_windows_frame(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            gwl_exstyle = -20
            ws_ex_toolwindow = 0x00000080
            ws_ex_appwindow = 0x00040000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, gwl_exstyle)
            style = (style & ~ws_ex_toolwindow) | ws_ex_appwindow
            ctypes.windll.user32.SetWindowLongW(hwnd, gwl_exstyle, style)
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd,
                33,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )
        except (AttributeError, OSError):
            pass

    def minimize(self) -> None:
        self._minimized = True
        self.root.overrideredirect(False)
        self.root.iconify()

    def _restore_frameless(self, _event=None) -> None:
        if not self._minimized or self.root.state() != "normal":
            return
        self._minimized = False
        self.root.after(10, lambda: self.root.overrideredirect(True))
        self.root.after(30, self._apply_windows_frame)

    def toggle_details(self, *, force_open: bool = False) -> None:
        self.details_visible = force_open or not self.details_visible
        if self.details_visible:
            self.details_panel.place(x=42, y=236)
            self.details_panel.lift()
            self.details_button.configure(text="运行状态  ‹")
        else:
            self.details_panel.place_forget()
            self.details_button.configure(text="运行状态  ›")

    def hide_details(self, _event=None) -> None:
        if self.details_visible:
            self.toggle_details()

    def _set_status(
        self,
        text: str,
        hint: str,
        kind: str = "neutral",
    ) -> None:
        colors = {
            "neutral": WARNING,
            "running": SUCCESS,
            "ready": TEAL,
            "warning": WARNING,
            "error": ERROR,
        }
        self.canvas.itemconfigure(self.status_dot, fill=colors[kind])
        self.canvas.itemconfigure(self.status_label, text=text)
        self.canvas.itemconfigure(self.status_hint, text=hint)

    def _set_primary(self, text: str, *, enabled: bool) -> None:
        self.primary_button.configure(
            text=text,
            state="normal" if enabled else "disabled",
        )

    def _write_report(self, checks: tuple[CheckResult, ...]) -> None:
        icons = {"ok": "✓", "warning": "•", "error": "×"}
        self.report_box.configure(state="normal")
        self.report_box.delete("1.0", "end")
        for item in checks:
            self.report_box.insert(
                "end",
                f"{icons[item.level]}  {item.message}\n",
                item.level,
            )
        self.report_box.configure(state="disabled")

    def _append_message(self, level: str, message: str) -> None:
        icon = {"ok": "✓", "warning": "•", "error": "×"}[level]
        self.report_box.configure(state="normal")
        self.report_box.insert("end", f"{icon}  {message}\n", level)
        self.report_box.see("end")
        self.report_box.configure(state="disabled")

    def begin_check(self) -> None:
        self.environment_ready = False
        self.recheck_button.configure(state="disabled")
        self.stop_button.configure(
            state="normal" if self.controller.owns_running_service else "disabled"
        )
        self._set_primary("检查中…", enabled=False)
        self._set_status("正在检查运行环境", "请稍候", "neutral")
        self._write_report((CheckResult("warning", "正在检查运行环境…"),))
        threading.Thread(target=self._collect_checks, daemon=True).start()

    def _collect_checks(self) -> None:
        try:
            report = collect_checks()
            service_state = self.controller.inspector()
        except Exception as exc:  # Keep GUI failures actionable.
            report = EnvironmentReport(
                (CheckResult("error", f"环境检查未完成：{exc}"),)
            )
            service_state = ServiceState("free", "服务尚未启动")
        try:
            self.root.after(
                0,
                lambda: self._finish_check(report, service_state),
            )
        except tk.TclError:
            pass

    def _finish_check(
        self,
        report: EnvironmentReport,
        service_state: ServiceState | None = None,
    ) -> None:
        self.recheck_button.configure(state="normal")
        self._write_report(report.checks)
        if not report.ok:
            self._set_status("环境检查失败", "展开运行状态查看原因", "error")
            self._set_primary("无法启动", enabled=False)
            self.stop_button.configure(state="disabled")
            self.toggle_details(force_open=True)
            return

        self.environment_ready = True
        service_state = service_state or self.controller.inspector()
        if service_state.kind == "running":
            self._set_status("错题本正在运行", service_state.message, "running")
            self._set_primary("▶   打开错题本", enabled=True)
            self.stop_button.configure(
                state="normal"
                if self.controller.owns_running_service
                else "disabled"
            )
            self.tray.refresh_reminder()
            return
        if service_state.kind == "occupied":
            self._set_status("端口冲突", "8080 端口被其他程序占用", "error")
            self._set_primary("无法启动", enabled=False)
            self.stop_button.configure(state="disabled")
            self._append_message("error", service_state.message)
            self.toggle_details(force_open=True)
            return

        self._set_status("可以启动", "环境检查已完成", "ready")
        self._set_primary("▶   启动错题本", enabled=True)
        self.stop_button.configure(state="disabled")

    def primary_action(self) -> None:
        state = self.controller.inspector()
        if state.kind == "running":
            self.controller.refresh_ownership()
            self.open_browser()
            return
        if not self.environment_ready:
            return
        if state.kind == "occupied":
            self._finish_check(EnvironmentReport(tuple()), state)
            return
        self._start_service()

    def _start_service(self) -> None:
        try:
            result = self.controller.start()
        except OSError as exc:
            self._set_status("启动失败", "展开运行状态查看原因", "error")
            self._append_message("error", f"无法启动服务：{exc}")
            self.toggle_details(force_open=True)
            return

        if result.kind == "already_running":
            self._set_status("错题本正在运行", "正在打开网页", "running")
            self._set_primary("▶   打开错题本", enabled=True)
            self.open_browser()
            return
        if result.kind == "port_conflict":
            self._set_status("端口冲突", "8080 端口被其他程序占用", "error")
            self._set_primary("无法启动", enabled=False)
            self._append_message("error", result.message)
            self.toggle_details(force_open=True)
            return

        self._set_status("正在启动错题本", "服务准备中，请稍候", "neutral")
        self._set_primary("启动中…", enabled=False)
        self._append_message("warning", "服务正在启动，请稍候…")
        self.stop_button.configure(state="normal")
        self.start_deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        self.root.after(250, self._poll_startup)

    def _poll_startup(self) -> None:
        state = self.controller.inspector()
        if state.kind == "running":
            self.controller.refresh_ownership()
            self._set_status("错题本已经就绪", "网页即将打开", "running")
            self._set_primary("▶   打开错题本", enabled=True)
            self.stop_button.configure(state="normal")
            self._append_message("ok", "错题本已经准备就绪。")
            self.tray.refresh_reminder()
            self.open_browser()
            return
        if (
            self.controller.process is not None
            and self.controller.process.poll() is not None
        ):
            self._set_status("启动失败", "请查看启动日志", "error")
            self._set_primary("重新检查", enabled=True)
            self._append_message(
                "error",
                f"服务提前退出，请查看日志：{self.controller.log_path}",
            )
            self._append_log_tail()
            self.stop_button.configure(state="disabled")
            self.toggle_details(force_open=True)
            return
        if time.monotonic() >= self.start_deadline:
            self.controller.stop()
            self._set_status("启动超时", "请查看启动日志", "error")
            self._set_primary("重新检查", enabled=True)
            self._append_message(
                "error",
                f"等待服务超时，请查看日志：{self.controller.log_path}",
            )
            self._append_log_tail()
            self.stop_button.configure(state="disabled")
            self.toggle_details(force_open=True)
            return
        self.root.after(250, self._poll_startup)

    def open_browser(self) -> None:
        if self.controller.inspector().kind != "running":
            self._set_status("服务未运行", "请重新检查后再试", "warning")
            return
        webbrowser.open(APP_URL)

    def open_log(self) -> None:
        if not self.controller.log_path.exists():
            messagebox.showinfo(
                "启动日志",
                f"当前还没有启动日志。\n\n日志位置：\n{self.controller.log_path}",
                parent=self.root,
            )

    def _append_log_tail(self) -> None:
        tail = read_log_tail(self.controller.log_path, max_lines=20, max_bytes=8192)
        if tail:
            self._append_message("warning", "日志末尾（已限长、脱敏）：\n" + tail)
            return
        try:
            os.startfile(self.controller.log_path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror(
                "无法打开日志",
                str(exc),
                parent=self.root,
            )

    def stop_service(self) -> None:
        if self.controller.stop():
            self._set_status("服务已停止", "可以重新启动", "warning")
            self._set_primary("▶   启动错题本", enabled=self.environment_ready)
            self._append_message("warning", "错题本服务已停止。")
            self.stop_button.configure(state="disabled")

    def on_close(self) -> None:
        if self.controller.owns_running_service:
            choice = messagebox.askyesnocancel(
                "退出启动中心",
                "是否同时停止错题本服务？\n\n"
                "选择“否”将关闭启动中心，但服务会继续运行。",
                parent=self.root,
            )
            if choice is None:
                return
            if choice:
                self.controller.stop()
        self.tray.stop()
        self.root.destroy()

    def exit_from_tray(self) -> None:
        if self.controller.owns_running_service and messagebox.askyesno(
            "退出启动中心", "是否同时停止错题本服务？", parent=self.root
        ):
            self.controller.stop()
        self.tray.stop()
        self.root.destroy()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="错题本桌面启动器")
    parser.add_argument("--server", action="store_true", help=argparse.SUPPRESS)
    return parser


def run_server_mode(log_path: Path = LAUNCH_LOG) -> int:
    """Run Flask inside the frozen GUI executable without requiring a console."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", buffering=1) as log_handle:
        previous_stdout, previous_stderr = sys.stdout, sys.stderr
        sys.stdout = log_handle
        sys.stderr = log_handle
        try:
            from app import run

            run(open_browser=False)
            return 0
        except BaseException:
            traceback.print_exc()
            return 1
        finally:
            sys.stdout = previous_stdout
            sys.stderr = previous_stderr


def _show_missing_gui_dependency() -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(
        "错题本无法启动",
        "缺少桌面启动器依赖。请运行“安装依赖.bat”后重试。",
        parent=root,
    )
    root.destroy()


def _enable_windows_dpi_awareness() -> None:
    """Keep the 1000x625 canvas and native window on one physical pixel grid."""

    if sys.platform != "win32":
        return
    try:
        per_monitor_v2 = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(per_monitor_v2):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    if arguments.server:
        return run_server_mode()
    if ctk is None or Image is None or ImageTk is None:
        _show_missing_gui_dependency()
        return 1

    # CustomTkinter otherwise multiplies the requested window geometry by the
    # monitor scale (150% on the target machine) while a native Canvas keeps
    # physical pixels, leaving unused space on the right and bottom.
    ctk.deactivate_automatic_dpi_awareness()
    _enable_windows_dpi_awareness()
    ctk.set_appearance_mode("dark")
    # Keep native Canvas coordinates and CustomTkinter widgets on the same
    # physical pixel grid on Windows systems using 125%/150% display scaling.
    ctk.set_window_scaling(1.0)
    ctk.set_widget_scaling(1.0)
    root = ctk.CTk()
    root.tk.call("tk", "scaling", 96 / 72)
    LauncherWindow(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
