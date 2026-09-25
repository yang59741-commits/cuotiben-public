from pathlib import Path
import shutil
import subprocess

import pytest

from tools.check_mathjax_assets import check_mathjax_assets


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "cuoti_app" / "static"


def test_bundled_chtml_fonts_are_valid_and_visible_to_git():
    fonts = check_mathjax_assets(STATIC)
    assert len(fonts) == 22
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *[str(font.relative_to(ROOT)) for font in fonts]],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert ignored.returncode == 1, ignored.stdout + ignored.stderr


@pytest.mark.parametrize("damage", ["missing", "empty", "truncated"])
def test_packaging_rejects_missing_or_broken_font(tmp_path, damage):
    relative = Path("vendor/mathjax/es5")
    shutil.copytree(STATIC / relative / "output/chtml", tmp_path / relative / "output/chtml")
    shutil.copyfile(STATIC / relative / "tex-chtml.js", tmp_path / relative / "tex-chtml.js")
    font = tmp_path / relative / "output/chtml/fonts/woff-v2/MathJax_Math-Italic.woff"
    if damage == "missing":
        font.unlink()
    elif damage == "empty":
        font.write_bytes(b"")
    else:
        font.write_bytes(font.read_bytes()[:44])
    with pytest.raises(ValueError, match="MathJax.*font"):
        check_mathjax_assets(tmp_path)
