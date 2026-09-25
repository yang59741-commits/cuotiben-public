"""Check the local fonts required by the bundled MathJax CHTML renderer."""

from pathlib import Path
import re


def check_mathjax_assets(static_root: Path) -> tuple[Path, ...]:
    mathjax = static_root / "vendor" / "mathjax" / "es5"
    renderer = (mathjax / "tex-chtml.js").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r"MathJax_[A-Za-z0-9_-]+\.woff", renderer)))
    if not names:
        raise ValueError("No CHTML font references found in tex-chtml.js")
    fonts = tuple(mathjax / "output" / "chtml" / "fonts" / "woff-v2" / name for name in names)
    for font in fonts:
        if not font.is_file():
            raise ValueError(f"Missing MathJax CHTML font: {font}")
        data = font.read_bytes()
        if len(data) < 44 or data[:4] != b"wOFF" or int.from_bytes(data[8:12], "big") != len(data):
            raise ValueError(f"Invalid MathJax WOFF font: {font}")
    return fonts
