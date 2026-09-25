# -*- mode: python ; coding: utf-8 -*-

import os
import runpy
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files


PROJECT_ROOT = Path(SPECPATH).resolve().parent
STATIC_ROOT = Path(os.environ.get("CUOTI_BUILD_STATIC", PROJECT_ROOT / "cuoti_app" / "static"))
VERSION_FILE = os.environ.get("CUOTI_VERSION_FILE")

# Validate the actual staged assets even when PyInstaller is invoked directly.
runpy.run_path(str(PROJECT_ROOT / "tools" / "check_mathjax_assets.py"))[
    "check_mathjax_assets"
](STATIC_ROOT)

datas = [
    (str(PROJECT_ROOT / "cuoti_app" / "templates"), "cuoti_app/templates"),
    (str(STATIC_ROOT), "cuoti_app/static"),
    (str(PROJECT_ROOT / "cuoti_app" / "catalog.default.json"), "cuoti_app"),
    (str(PROJECT_ROOT / "launcher_assets"), "launcher_assets"),
]
datas += collect_data_files("customtkinter")

hiddenimports = ["pystray._win32"]

# Anaconda exposes many optional packages to module discovery. None of these
# are used by the application, and excluding them keeps analysis deterministic
# instead of importing unrelated scientific/Jupyter package metadata.
excludes = [
    "IPython",
    "bcrypt",
    "chardet",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "fontTools",
    "lxml",
]

a = Analysis(
    [str(PROJECT_ROOT / "launcher.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="错题本",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    manifest=str(PROJECT_ROOT / "packaging" / "cuoti_launcher.manifest"),
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(PROJECT_ROOT / "launcher_assets" / "cuoti.ico"),
    version=VERSION_FILE,
)
