"""Minimal Windows Credential Manager backend with no plugin dependencies."""

from __future__ import annotations

import ctypes
from ctypes import wintypes


_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_ENTERPRISE = 3
_ERROR_NOT_FOUND = 1168


class _CREDENTIALW(ctypes.Structure):
    pass


_LPBYTE = ctypes.POINTER(ctypes.c_ubyte)
_PCREDENTIALW = ctypes.POINTER(_CREDENTIALW)
_CREDENTIALW._fields_ = [
    ("Flags", wintypes.DWORD),
    ("Type", wintypes.DWORD),
    ("TargetName", wintypes.LPWSTR),
    ("Comment", wintypes.LPWSTR),
    ("LastWritten", wintypes.FILETIME),
    ("CredentialBlobSize", wintypes.DWORD),
    ("CredentialBlob", _LPBYTE),
    ("Persist", wintypes.DWORD),
    ("AttributeCount", wintypes.DWORD),
    ("Attributes", ctypes.c_void_p),
    ("TargetAlias", wintypes.LPWSTR),
    ("UserName", wintypes.LPWSTR),
]


class WindowsCredentialBackend:
    """Store application secrets in the current user's Windows vault."""

    def __init__(self):
        if not hasattr(ctypes, "WinDLL"):
            raise OSError("Windows Credential Manager is unavailable")
        advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._cred_read = advapi32.CredReadW
        self._cred_read.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIALW),
        ]
        self._cred_read.restype = wintypes.BOOL
        self._cred_write = advapi32.CredWriteW
        self._cred_write.argtypes = [ctypes.POINTER(_CREDENTIALW), wintypes.DWORD]
        self._cred_write.restype = wintypes.BOOL
        self._cred_delete = advapi32.CredDeleteW
        self._cred_delete.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        self._cred_delete.restype = wintypes.BOOL
        self._cred_free = advapi32.CredFree
        self._cred_free.argtypes = [ctypes.c_void_p]
        self._cred_free.restype = None

    @staticmethod
    def _targets(service: str, username: str):
        """Return the profile-scoped target followed by the legacy shared target."""

        return f"{username}@{service}", service

    @staticmethod
    def _raise_last_error():
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)

    def _read(self, target: str):
        pointer = _PCREDENTIALW()
        if self._cred_read(
            target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            return pointer
        if ctypes.get_last_error() == _ERROR_NOT_FOUND:
            return None
        self._raise_last_error()

    @staticmethod
    def _decode_blob(blob: bytes) -> str:
        for encoding in ("utf-16", "utf-16-le", "utf-8"):
            try:
                return blob.decode(encoding).rstrip("\x00")
            except (UnicodeDecodeError, UnicodeError):
                continue
        raise UnicodeDecodeError("utf-8", blob, 0, len(blob), "invalid credential")

    def _password_at_target(self, target: str, username: str):
        pointer = self._read(target)
        if pointer is None:
            return None
        try:
            credential = pointer.contents
            if str(credential.UserName or "") != username:
                return None
            size = int(credential.CredentialBlobSize)
            blob = ctypes.string_at(credential.CredentialBlob, size)
            return self._decode_blob(blob)
        finally:
            self._cred_free(pointer)

    def _target_owned_by(self, target: str, username: str) -> bool:
        pointer = self._read(target)
        if pointer is None:
            return False
        try:
            return str(pointer.contents.UserName or "") == username
        finally:
            self._cred_free(pointer)

    def _write_password(self, target: str, username: str, password: str):
        encoded = str(password).encode("utf-16-le")
        buffer = ctypes.create_string_buffer(encoded)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = target
        credential.Comment = "Stored by Cuoti Notebook"
        credential.CredentialBlobSize = len(encoded)
        credential.CredentialBlob = ctypes.cast(buffer, _LPBYTE)
        credential.Persist = _CRED_PERSIST_ENTERPRISE
        credential.UserName = username
        if not self._cred_write(ctypes.byref(credential), 0):
            self._raise_last_error()

    def _delete_target(self, target: str):
        if not self._cred_delete(target, _CRED_TYPE_GENERIC, 0):
            if ctypes.get_last_error() != _ERROR_NOT_FOUND:
                self._raise_last_error()

    def get_password(self, service: str, username: str):
        for target in self._targets(service, username):
            password = self._password_at_target(target, username)
            if password is not None:
                return password
        return None

    def set_password(self, service: str, username: str, password: str):
        scoped_target, _legacy_target = self._targets(service, username)
        self._write_password(scoped_target, username, password)

    def delete_password(self, service: str, username: str):
        for target in self._targets(service, username):
            if not self._target_owned_by(target, username):
                continue
            self._delete_target(target)
