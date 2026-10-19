"""Fail-closed path overrides for the promoted desktop runtime."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import os


DEFAULT_HOME_INSTITUTION = ""
DEFAULT_RECALL_ALGORITHM = "semantic-recall-v2"


def runtime_identity(database: str | Path, runtime_root: str | Path) -> dict[str, str]:
    """Compare layouts by resolved paths, independent of spelling or port."""
    return {
        "database": os.path.normcase(str(Path(database).resolve())),
        "runtime_root": os.path.normcase(str(Path(runtime_root).resolve())),
    }


class FormalRuntimeError(RuntimeError):
    """Raised when a required formal-runtime input is unavailable."""

    def __init__(self, code: str, path: Path):
        message = "该数据库的错题本服务已运行，请先停止现有服务。" if code == "database_service_already_running" else ""
        super().__init__(f"{code}: {message}{path}")
        self.code = code
        self.path = path


@contextmanager
def database_service_guard(database: str | Path):
    """Hold one server lease per canonical database, independent of HTTP port."""
    from .launcher_runtime import SingleInstanceGuard

    path = Path(database).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = SingleInstanceGuard(path.with_name(path.name + ".service.lock"))
    if not guard.acquire():
        raise FormalRuntimeError("database_service_already_running", path)
    try:
        yield
    finally:
        guard.release()


@dataclass(frozen=True, slots=True)
class FormalRuntimePaths:
    project_root: Path
    database: Path
    runtime_root: Path
    question_bank: Path
    formal_artifact: Path

    def overrides(
        self,
        *,
        home_institution: str = DEFAULT_HOME_INSTITUTION,
        recall_algorithm: str = DEFAULT_RECALL_ALGORITHM,
    ) -> dict[str, object]:
        runtime = self.runtime_root
        return {
            "DATABASE": str(self.database),
            "RUNTIME_ROOT": str(runtime),
            "PDF_DIR": str(runtime / "pdfs"),
            "ATTACHMENT_DIR": str(runtime / "attachments"),
            "AI_MEMORY_DIR": str(runtime / "memory"),
            "JOB_INPUT_DIR": str(runtime / "job_inputs"),
            "PAST_EXAM_PROFILE_DIR": str(runtime / "past_exam_profiles"),
            "AI_USAGE_PATH": str(runtime / "ai-usage.jsonl"),
            "SETTINGS_PATH": str(runtime / "settings.json"),
            "CATALOG_PATH": str(runtime / "catalog.json"),
            "QUESTION_BANK_DATABASE": str(self.question_bank),
            "SEMANTIC_FORMAL_ARTIFACT": str(self.formal_artifact),
            "SEMANTIC_HOME_INSTITUTION": home_institution,
            "SEMANTIC_RECALL_ALGORITHM_VERSION": recall_algorithm,
            "JOB_WORKER_ENABLED": True,
        }


def resolve_formal_runtime(
    project_root: str | Path,
    *,
    require_database: bool = True,
    require_reference_assets: bool = True,
) -> FormalRuntimePaths:
    """Resolve the promoted layout without falling back to a legacy root DB."""

    root = Path(project_root).resolve()
    database = root / "data" / "cuoti.db"
    question_bank = root / "instance" / "question_bank.sqlite3"
    formal_artifact = (
        root / "instance" / "semantic_index" / "formal_past_exams.json"
    )

    required: list[tuple[str, Path]] = []
    if require_database:
        required.append(("formal_database_missing", database))
    if require_reference_assets:
        required.extend(
            (
                ("question_bank_missing", question_bank),
                ("formal_semantic_artifact_missing", formal_artifact),
            )
        )
    for code, path in required:
        if not path.is_file():
            raise FormalRuntimeError(code, path)

    return FormalRuntimePaths(
        project_root=root,
        database=database,
        runtime_root=root / "data" / "runtime",
        question_bank=question_bank,
        formal_artifact=formal_artifact,
    )


def build_formal_runtime_overrides(
    project_root: str | Path,
    *,
    require_database: bool = True,
    require_reference_assets: bool = True,
    home_institution: str = DEFAULT_HOME_INSTITUTION,
    recall_algorithm: str = DEFAULT_RECALL_ALGORITHM,
) -> dict[str, object]:
    """Return application overrides for the promoted desktop layout."""

    paths = resolve_formal_runtime(
        project_root,
        require_database=require_database,
        require_reference_assets=require_reference_assets,
    )
    return paths.overrides(
        home_institution=home_institution,
        recall_algorithm=recall_algorithm,
    )


__all__ = [
    "DEFAULT_HOME_INSTITUTION",
    "DEFAULT_RECALL_ALGORITHM",
    "FormalRuntimeError",
    "FormalRuntimePaths",
    "build_formal_runtime_overrides",
    "database_service_guard",
    "resolve_formal_runtime",
    "runtime_identity",
]
