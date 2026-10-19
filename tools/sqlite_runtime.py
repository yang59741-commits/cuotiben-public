"""Install/check the pinned Windows SQLite DLL in this project's existing venv."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PRELOAD = ('import ctypes, os, sys; sys._cuoti_sqlite_runtime = '
           'ctypes.WinDLL(os.path.join(sys.prefix, "Scripts", "sqlite3.dll")) '
           'if sys.platform == "win32" else None\n')


def load_pin() -> dict:
    return json.loads((ROOT / 'packaging/sqlite-runtime.json').read_text(encoding='utf-8'))


def runtime_paths() -> tuple[Path, Path]:
    venv = ROOT / '.venv-stability'
    if sys.platform != 'win32' or ctypes.sizeof(ctypes.c_void_p) != 8 or Path(sys.prefix).resolve() != venv.resolve():
        raise RuntimeError('Use the existing project .venv-stability Windows x64 Python; global Python is not modified')
    return venv / 'Scripts/sqlite3.dll', venv / 'Lib/site-packages/00_cuoti_sqlite_runtime.pth'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def install_runtime(archive: Path | None = None) -> Path:
    pin = load_pin()
    dll, binding = runtime_paths()
    if dll.exists() and digest(dll.read_bytes()) != pin['dll_sha256']:
        raise RuntimeError(f'Preserve and review the existing DLL before replacing it: {dll}')
    if binding.exists() and binding.read_text(encoding='ascii').strip() != PRELOAD.strip():
        raise RuntimeError(f'Existing SQLite startup binding differs; preserve it: {binding}')
    if not dll.exists():
        if archive is None:
            archive = ROOT / 'build' / ('sqlite-runtime-' + pin['version']) / Path(pin['url']).name
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                with urllib.request.urlopen(pin['url'], timeout=30) as response:
                    data = response.read(8 * 1024 * 1024 + 1)
                if len(data) > 8 * 1024 * 1024 or digest(data) != pin['archive_sha256']:
                    raise RuntimeError('SQLite download does not match the pinned official archive')
                archive.write_bytes(data)
        if digest(archive.read_bytes()) != pin['archive_sha256']:
            raise RuntimeError('SQLite archive checksum mismatch')
        with tarfile.open(archive, 'r:bz2') as bundle:
            entry = bundle.getmember('Library/bin/sqlite3.dll')
            if not entry.isfile() or entry.size > 8 * 1024 * 1024:
                raise RuntimeError('Unexpected SQLite DLL size')
            data = bundle.extractfile(entry).read()
        if digest(data) != pin['dll_sha256']:
            raise RuntimeError('SQLite DLL checksum mismatch')
        dll.write_bytes(data)
    binding.write_text(PRELOAD, encoding='ascii')
    return dll


def check_runtime() -> dict:
    pin = load_pin()
    dll, _binding = runtime_paths()
    if not dll.is_file() or digest(dll.read_bytes()) != pin['dll_sha256']:
        raise RuntimeError('Pinned SQLite DLL missing/changed; run tools/sqlite_runtime.py install')
    import sqlite3
    with sqlite3.connect(':memory:') as db:
        source_id = db.execute('SELECT sqlite_source_id()').fetchone()[0]
        variable_limit = db.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
        trigger_depth = db.getlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH)
    db.close()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    kernel.GetModuleFileNameW.restype = ctypes.c_uint
    buffer = ctypes.create_unicode_buffer(32768)
    handle = kernel.GetModuleHandleW('sqlite3.dll')
    if not handle or not kernel.GetModuleFileNameW(handle, buffer, len(buffer)):
        raise ctypes.WinError(ctypes.get_last_error())
    if Path(buffer.value).resolve() != dll.resolve() or sqlite3.sqlite_version != pin['version'] or source_id != pin['source_id']:
        raise RuntimeError('Python loaded a different SQLite runtime; run tools/sqlite_runtime.py install, then restart Python')
    if variable_limit < pin['minimum_variable_limit'] or trigger_depth < pin['minimum_trigger_depth']:
        raise RuntimeError('SQLite runtime reduces existing SQL parameter/trigger limits')
    return {'dll': str(dll), 'version': sqlite3.sqlite_version, 'source_id': source_id, 'sha256': pin['dll_sha256'],
            'variable_limit': variable_limit, 'trigger_depth': trigger_depth}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'check'])
    parser.add_argument('--archive', type=Path, help='Use an already downloaded, checksum-verified archive')
    args = parser.parse_args()
    if args.action == 'install':
        install_runtime(args.archive)
        # Verify the startup binding in a fresh process; this process may have
        # imported the old Anaconda runtime before installation.
        return subprocess.call([sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()), 'check'], cwd=ROOT)
    print(json.dumps(check_runtime(), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
