"""Read-only environment diagnostics shared by production entry points."""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError, INSTANCE_DIR, build_config


SETTINGS_PATH = INSTANCE_DIR / "settings.json"
SOURCE_DEPENDENCIES = (
    ("flask", "Flask"),
    ("keyring", "keyring"),
    ("customtkinter", "customtkinter"),
    ("PIL", "Pillow"),
    ("pymupdf", "PyMuPDF"),
    ("pystray", "pystray"),
)


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One user-facing environment check."""

    level: str
    message: str


@dataclass(frozen=True, slots=True)
class EnvironmentReport:
    """Structured diagnostics shared by the CLI and graphical launcher."""

    checks: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        return all(item.level != "error" for item in self.checks)


def ai_credential_status(config: dict) -> tuple[bool, str]:
    """Resolve AI credentials exactly as the application does, without exposing them."""

    from flask import Flask

    from .settings import SettingsService

    diagnostic_app = Flask("cuoti-environment-check")
    diagnostic_app.config.update(config)
    key, source = SettingsService(diagnostic_app).credential()
    return bool(str(key or "").strip()), source


def collect_checks() -> EnvironmentReport:
    """Collect diagnostics without printing or mutating project state."""

    checks: list[CheckResult] = []

    def add(level: str, message: str) -> None:
        checks.append(CheckResult(level, message))

    if sys.version_info < (3, 10):
        add("error", "需要 Python 3.10 或更高版本")
    else:
        add("ok", f"Python {sys.version_info.major}.{sys.version_info.minor}")

    missing_dependencies = []
    for module_name, package_name in SOURCE_DEPENDENCIES:
        if importlib.util.find_spec(module_name) is None:
            missing_dependencies.append(package_name)
            add("error", f"缺少 Python 依赖 {package_name}；请运行“安装依赖.bat”")
    if not missing_dependencies:
        add("ok", "Python 依赖已安装")

    if SETTINGS_PATH.exists():
        add("ok", "已发现本地配置文件")
    else:
        add("warning", "尚未创建 instance/settings.json；也可以完全通过环境变量配置")
    try:
        config = build_config()
    except ConfigError as exc:
        add("error", f"运行配置无效：{exc}")
        return EnvironmentReport(tuple(checks))
    add("ok", "环境变量与本地配置已按优先级加载")

    command = str(config.get("XELATEX_COMMAND") or "xelatex").strip()
    if shutil.which(command) or Path(command).is_file():
        add("ok", "已找到 XeLaTeX")
    else:
        add(
            "warning",
            "未找到 XeLaTeX；普通功能可用，但生成 PDF 前需安装 "
            "TeX Live/MiKTeX 或配置 xelatex_command",
        )

    try:
        has_ai_key, credential_source = ai_credential_status(config)
    except Exception as exc:
        add("warning", f"无法读取 AI 凭据状态：{exc}")
    else:
        if has_ai_key:
            source_labels = {
                "environment": "环境变量",
                "windows_keyring": "Windows 凭据管理器",
                "legacy_file": "旧版配置文件",
            }
            add(
                "ok",
                f"已从 {source_labels.get(credential_source, credential_source)} 读取 AI 凭据",
            )
        else:
            add("warning", "未配置 DeepSeek API 密钥，AI 分析和组卷暂不可用")
    tex_base = str(config.get("TEX_BASE") or "").strip()
    if not tex_base:
        add("warning", "未配置普通 TeX 试题库目录，试题库参考功能暂不可用")
    elif not Path(tex_base).is_dir():
        add("warning", "普通 TeX 试题库目录不存在，试题库参考功能暂不可用")
    past_exam_dir = str(config.get("PAST_EXAM_DIR") or "").strip()
    if not past_exam_dir:
        add("warning", "未配置真题 TeX 目录，真题组卷暂不可用")
    elif not Path(past_exam_dir).is_dir():
        add("warning", "真题 TeX 目录不存在，真题组卷暂不可用")

    return EnvironmentReport(tuple(checks))


__all__ = [
    "CheckResult",
    "EnvironmentReport",
    "SOURCE_DEPENDENCIES",
    "SETTINGS_PATH",
    "ai_credential_status",
    "collect_checks",
]
