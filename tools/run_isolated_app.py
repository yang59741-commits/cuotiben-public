"""Run the web UI against synthetic resources for local browser checks."""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from cuoti_app import create_app
from cuoti_app.database import init_db
from cuoti_app.services.settings import SettingsService


class MemoryKeyring:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._values.pop((service, username), None)


def build_app(
    root: Path,
    *,
    tex_base: Path | None = None,
    past_exam_dir: Path | None = None,
):
    root = root.resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if root == temporary_root or temporary_root not in root.parents:
        raise ValueError("isolation root must be a child of the system temp directory")

    root.mkdir(parents=True, exist_ok=True)
    tex_dir = (tex_base or (root / "synthetic_tex")).resolve()
    past_dir = (past_exam_dir or (root / "synthetic_past")).resolve()
    pdf_dir = root / "synthetic_pdfs"
    job_input_dir = root / "synthetic_job_inputs"
    directories = [pdf_dir, job_input_dir]
    if tex_base is None:
        directories.append(tex_dir)
    if past_exam_dir is None:
        directories.append(past_dir)
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

    settings_path = root / "synthetic_settings.json"
    if not settings_path.exists():
        settings_path.write_text("{}\n", encoding="utf-8")

    app = create_app(
        {
            "TESTING": True,
            "DATABASE": str(root / "synthetic.db"),
            "SETTINGS_PATH": str(settings_path),
            "PDF_DIR": str(pdf_dir),
            "TEX_BASE": str(tex_dir),
            "PAST_EXAM_DIR": str(past_dir),
            "QUESTION_BANK_DATABASE": str(
                root / "synthetic_question_bank.sqlite3"
            ),
            "AI_MEMORY_DIR": str(root / "synthetic_memory"),
            "JOB_INPUT_DIR": str(job_input_dir),
            "REGISTER_JOB_HANDLERS": False,
            "JOB_WORKER_ENABLED": False,
        }
    )
    app.extensions["ai_settings_service"] = SettingsService(
        app, keyring_backend=MemoryKeyring()
    )
    with app.app_context():
        init_db()
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tex-base", type=Path)
    parser.add_argument("--past-exam-dir", type=Path)
    args = parser.parse_args()
    app = build_app(
        args.root,
        tex_base=args.tex_base,
        past_exam_dir=args.past_exam_dir,
    )
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
