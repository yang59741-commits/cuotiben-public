"""Read the maintained source graph used by the browser's application shell."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "cuoti_app"


def template_source(name="index.html"):
    source = (ROOT / "templates" / name).read_text(encoding="utf-8")
    return re.sub(r"\{%\s*include\s+['\"]([^'\"]+)['\"]\s*-?%\}",
                  lambda match: template_source(match[1]), source)


def stylesheet_source(path=None):
    path = path or ROOT / "static/css/app.css"
    source = path.read_text(encoding="utf-8")
    return re.sub(r"@import\s+url\(['\"]([^'\"]+)['\"]\);",
                  lambda match: stylesheet_source(path.parent / match[1]), source)


def application_styles():
    names = re.findall(r"filename=['\"](css/[^'\"]+)['\"]", template_source())
    return "\n".join(stylesheet_source(ROOT / "static" / name) for name in names)


def script_source(name):
    path = ROOT / "static/js" / name
    parts = [path]
    module_root = path.with_suffix("")
    if module_root.is_dir():
        parts.extend(sorted(module_root.rglob("*.js")))
    if name == "core.js":
        parts.extend(sorted((path.parent / "ui").glob("*.js")))
    return "\n".join(part.read_text(encoding="utf-8") for part in parts)
