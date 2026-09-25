"""HTML page routes."""

import hashlib
import re
from pathlib import Path

from flask import Blueprint, current_app, render_template, send_from_directory

bp = Blueprint("pages", __name__)

_BUILD_ID_PLACEHOLDER = "__CUOTI_BUILD_ID__"
_SHELL_TEXT_ASSET = re.compile(
    r"['\"](/static/(?:css|js)/[A-Za-z0-9_./-]+\.(?:css|js))['\"]"
)


def _source_shell_build_id(worker_source: str) -> str:
    """Bind a source checkout's worker cache to its actual text shell."""

    digest = hashlib.sha256()

    def add(identity: str, content: bytes) -> None:
        name = identity.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)

    add("/sw.js", worker_source.encode("utf-8"))
    template_source, _filename, _uptodate = current_app.jinja_loader.get_source(
        current_app.jinja_env, "index.html"
    )
    add("/", template_source.encode("utf-8"))
    static_root = Path(str(current_app.static_folder)).resolve()
    for asset_url in sorted(set(_SHELL_TEXT_ASSET.findall(worker_source))):
        relative = asset_url.removeprefix("/static/")
        asset_path = (static_root / relative).resolve()
        if static_root not in asset_path.parents:
            raise RuntimeError("service worker shell asset escaped static root")
        add(asset_url, asset_path.read_bytes())
    return digest.hexdigest()[:20]


@bp.route("/")
def index():
    return render_template("index.html")


@bp.get("/manifest.webmanifest")
def manifest():
    response = send_from_directory(current_app.static_folder, "manifest.webmanifest")
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.get("/sw.js")
def service_worker():
    worker_path = Path(str(current_app.static_folder)) / "sw.js"
    worker_source = worker_path.read_text(encoding="utf-8")
    if _BUILD_ID_PLACEHOLDER in worker_source:
        build_id = _source_shell_build_id(worker_source)
        response = current_app.response_class(
            worker_source.replace(_BUILD_ID_PLACEHOLDER, build_id),
            mimetype="application/javascript",
        )
    else:
        # Packaged builds already carry their release identity and retain the
        # established send-file response behavior.
        response = send_from_directory(current_app.static_folder, "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response
