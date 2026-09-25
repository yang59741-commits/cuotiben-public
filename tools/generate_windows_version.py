"""Generate PyInstaller Windows version metadata from the shared build id."""

from __future__ import annotations

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("build_id", help="Build id in yyyyMMddHHmmss form")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build_id = args.build_id
    if len(build_id) != 14 or not build_id.isdigit():
        raise SystemExit("build id must use yyyyMMddHHmmss")
    year, month, day = int(build_id[:4]), int(build_id[4:6]), int(build_id[6:8])
    time_part = int(build_id[8:12])
    content = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({year}, {month}, {day}, {time_part}), prodvers=({year}, {month}, {day}, {time_part}), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('080404b0', [
    StringStruct('CompanyName', 'Local First Study Tools'),
    StringStruct('FileDescription', '错题本桌面启动器'),
    StringStruct('FileVersion', '{build_id}'),
    StringStruct('InternalName', 'cuoti-notebook'),
    StringStruct('OriginalFilename', '错题本.exe'),
    StringStruct('ProductName', '错题本'),
    StringStruct('ProductVersion', '{build_id}')
  ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])]
)
"""
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(content, encoding="utf-8")
    print(f"version_file={args.output}")
    print(f"build_id={build_id}")


if __name__ == "__main__":
    main()
