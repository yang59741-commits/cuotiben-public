"""Runtime configuration with environment-first local overrides."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path

from .paths import DATA_DIR, INSTANCE_DIR


# Kept as a compatibility name for modules that treat it as the writable root.
BASE_DIR = DATA_DIR


class ConfigError(RuntimeError):
    """Raised when local configuration cannot be loaded safely."""


def _local_settings(path: str | Path | None = None) -> dict:
    path = Path(path) if path is not None else INSTANCE_DIR / "settings.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件 {path} 不是有效 JSON（第 {exc.lineno} 行）"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path} 的顶层必须是 JSON 对象")
    return data


def build_config(overrides: dict | None = None) -> dict:
    overrides = dict(overrides or {})
    isolated_test_settings = bool(overrides.get("TESTING")) and not overrides.get(
        "SETTINGS_PATH"
    )
    if isolated_test_settings:
        settings_path = Path(tempfile.gettempdir()) / (
            f"cuoti-test-settings-{os.getpid()}-{uuid.uuid4().hex}.json"
        )
        local = {}
    else:
        settings_path = Path(
            overrides.get("SETTINGS_PATH") or INSTANCE_DIR / "settings.json"
        )
        local = _local_settings(settings_path)

    ai_settings = local.get("ai") if isinstance(local.get("ai"), dict) else {}
    active_profile = ai_settings.get("active_profile", "deepseek")
    profiles = ai_settings.get("profiles") if isinstance(ai_settings.get("profiles"), dict) else {}
    profile = profiles.get(active_profile) if isinstance(profiles.get(active_profile), dict) else {}

    def setting(env_name: str, local_name: str, default=""):
        return os.environ.get(env_name, local.get(local_name, default))

    def ai_setting(env_name: str, profile_name: str, local_name: str, default=""):
        return os.environ.get(
            env_name, profile.get(profile_name, local.get(local_name, default))
        )

    def int_setting(
        env_name: str,
        local_name: str,
        default: int,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = setting(env_name, local_name, default)
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"{env_name}/{local_name} 必须是整数"
            ) from exc
        if parsed < minimum or parsed > maximum:
            raise ConfigError(
                f"{env_name}/{local_name} 必须在 {minimum}–{maximum} 之间"
            )
        return parsed

    def path_setting(
        env_name: str,
        local_name: str,
        default: str | Path = "",
        *,
        optional: bool = False,
    ) -> str:
        raw = setting(env_name, local_name, default)
        if raw is None or not str(raw).strip():
            return "" if optional else str(default)
        return str(Path(str(raw)).expanduser())

    memory_default = (
        Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent / "memory"
        if overrides.get("TESTING")
        else INSTANCE_DIR / "memory"
    )
    attachment_default = (
        Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent / "attachments"
        if overrides.get("TESTING")
        else INSTANCE_DIR / "attachments"
    )
    catalog_default = (
        Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent / "catalog.json"
        if overrides.get("TESTING")
        else INSTANCE_DIR / "catalog.json"
    )
    question_bank_default = (
        Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent
        / "question_bank.sqlite3"
        if overrides.get("TESTING")
        else INSTANCE_DIR / "question_bank.sqlite3"
    )
    semantic_formal_default = (
        Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent
        / "semantic_index"
        / "formal_past_exams.json"
        if overrides.get("TESTING")
        else INSTANCE_DIR / "semantic_index" / "formal_past_exams.json"
    )

    config = {
        "SERVICE_INSTANCE_ID": str(os.environ.get("CUOTI_SERVICE_INSTANCE_ID", "")),
        "DATABASE": path_setting(
            "CUOTI_DATABASE",
            "database",
            BASE_DIR / "cuoti.db",
        ),
        "PDF_DIR": path_setting(
            "CUOTI_PDF_DIR",
            "pdf_dir",
            BASE_DIR / "pdfs",
        ),
        "ATTACHMENT_DIR": path_setting(
            "CUOTI_ATTACHMENT_DIR",
            "attachment_dir",
            attachment_default,
        ),
        "CATALOG_PATH": path_setting(
            "CUOTI_CATALOG_PATH",
            "catalog_path",
            catalog_default,
        ),
        "TEX_BASE": path_setting(
            "CUOTI_TEX_BASE",
            "tex_base",
            optional=True,
        ),
        "PAST_EXAM_DIR": path_setting(
            "CUOTI_PAST_EXAM_DIR",
            "past_exam_dir",
            BASE_DIR / "past-exams" if not overrides.get("TESTING") else "",
            optional=True,
        ),
        "QUESTION_BANK_DATABASE": path_setting(
            "CUOTI_QUESTION_BANK_DATABASE",
            "question_bank_database",
            question_bank_default,
        ),
        "SEMANTIC_FORMAL_ARTIFACT": path_setting(
            "CUOTI_SEMANTIC_FORMAL_ARTIFACT",
            "semantic_formal_artifact",
            semantic_formal_default,
        ),
        "SEMANTIC_HOME_INSTITUTION": setting(
            "CUOTI_SEMANTIC_HOME_INSTITUTION",
            "semantic_home_institution",
            "",
        ),
        "SEMANTIC_RECALL_ALGORITHM_VERSION": setting(
            "CUOTI_SEMANTIC_RECALL_ALGORITHM_VERSION",
            "semantic_recall_algorithm_version",
            "semantic-recall-v2",
        ),
        "AI_MEMORY_DIR": path_setting(
            "CUOTI_AI_MEMORY_DIR",
            "ai_memory_dir",
            memory_default,
        ),
        "AI_MEMORY_DAILY_DAYS": int_setting(
            "CUOTI_AI_MEMORY_DAILY_DAYS",
            "ai_memory_daily_days",
            30,
            minimum=1,
            maximum=365,
        ),
        "AI_MEMORY_MAX_EVENT_CHARS": int_setting(
            "CUOTI_AI_MEMORY_MAX_EVENT_CHARS",
            "ai_memory_max_event_chars",
            20_000,
            minimum=500,
            maximum=100_000,
        ),
        "AI_MEMORY_MAX_CONTEXT_CHARS": int_setting(
            "CUOTI_AI_MEMORY_MAX_CONTEXT_CHARS",
            "ai_memory_max_context_chars",
            12_000,
            minimum=1_000,
            maximum=100_000,
        ),
        "SETTINGS_PATH": str(settings_path),
        "AI_PROVIDER": profile.get("provider", "openai_compatible"),
        "AI_ACTIVE_PROFILE": active_profile,
        "AI_API_KEY_CONFIG_OVERRIDE": "DEEPSEEK_API_KEY" in overrides,
        "DEEPSEEK_API_KEY": setting(
            "DEEPSEEK_API_KEY",
            "deepseek_api_key",
        ),
        "DEEPSEEK_URL": ai_setting(
            "DEEPSEEK_URL",
            "base_url",
            "deepseek_url",
            "https://api.deepseek.com/v1/chat/completions",
        ),
        "DEEPSEEK_MODEL": ai_setting(
            "DEEPSEEK_MODEL",
            "quality_model",
            "deepseek_model",
            "deepseek-flash",
        ),
        "DEEPSEEK_FLASH_MODEL": ai_setting(
            "DEEPSEEK_FLASH_MODEL",
            "fast_model",
            "deepseek_flash_model",
            "deepseek-flash",
        ),
        "BATCH_CLASSIFY_TIMEOUT": int_setting(
            "CUOTI_BATCH_CLASSIFY_TIMEOUT",
            "batch_classify_timeout",
            24,
            minimum=5,
            maximum=24,
        ),
        "XELATEX_COMMAND": setting(
            "XELATEX_COMMAND",
            "xelatex_command",
            "xelatex",
        ),
        "XELATEX_TIMEOUT": int_setting(
            "CUOTI_XELATEX_TIMEOUT",
            "xelatex_timeout",
            60,
            minimum=1,
            maximum=600,
        ),
        "MAX_CONTENT_LENGTH": 20 * 1024 * 1024,
        "JOB_WORKER_ENABLED": not bool(overrides.get("TESTING")),
        "JOB_FAST_WORKERS": int_setting(
            "CUOTI_JOB_FAST_WORKERS", "job_fast_workers", 1,
            minimum=1, maximum=4,
        ),
        "JOB_HEAVY_WORKERS": int_setting(
            "CUOTI_JOB_HEAVY_WORKERS", "job_heavy_workers", 1,
            minimum=1, maximum=4,
        ),
        "JOB_AGING_SECONDS": int_setting(
            "CUOTI_JOB_AGING_SECONDS", "job_aging_seconds", 300,
            minimum=10, maximum=86_400,
        ),
        "JOB_INPUT_DIR": str(
            Path(overrides.get("DATABASE", BASE_DIR / "cuoti.db")).parent
            / "job_inputs"
            if overrides.get("TESTING")
            else INSTANCE_DIR / "job_inputs"
        ),
    }
    config.update(overrides)
    return config
