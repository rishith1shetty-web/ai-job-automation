"""Windows-protected storage for the application's database credential.

The installed application needs a database password at every start, with no human present to
type it. It therefore has to be stored -- but never in source, Git, a plain-text ``.env``, an
installer command line, a log, or the registry in clear text.

**Mechanism: DPAPI (``CryptProtectData``), current-user scope.** The blob is written to
``<config>/database_credential.dat`` (see ``app/paths.py``), which already lives inside the
per-user application-data folder. Windows derives the key from the logged-in user's
credentials, so:

* the application decrypts it silently at start -- no second sign-in, no prompt;
* another Windows account on the same computer CANNOT read it, even with the file;
* copying the file to another computer is useless;
* if the Windows account is deleted or its profile reset, the credential is unrecoverable
  and the database must be re-provisioned (documented recovery path).

Chosen over Windows Credential Manager because provisioning may run from an elevated
installer process while the app runs unelevated: a Credential Manager entry written by one
account is invisible to the other, whereas a DPAPI blob in the user's own profile keeps the
same per-user boundary while remaining a plain file the installer can place. Chosen over
machine-scope DPAPI because machine scope lets every account on the computer decrypt it,
which would break the per-user isolation established in Phase 1A.

A deliberately insecure fallback exists for non-Windows CI and for developers, and activates
ONLY when ``AI_JOB_AUTOMATION_INSECURE_CREDENTIAL_STORE`` is set. It never activates by
accident, and it announces itself in :meth:`CredentialStore.describe`.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from app.paths import runtime_paths

logger = logging.getLogger(__name__)

CREDENTIAL_FILENAME = "database_credential.dat"
INSECURE_STORE_ENV = "AI_JOB_AUTOMATION_INSECURE_CREDENTIAL_STORE"
STORE_DIR_ENV = "AI_JOB_AUTOMATION_CREDENTIAL_DIR"

# Bumped if the stored payload shape ever changes.
PAYLOAD_VERSION = 1

SCOPE_USER = "user"
SCOPE_MACHINE = "machine"


class CredentialStoreError(RuntimeError):
    """Raised when a credential cannot be stored or retrieved."""


# --------------------------------------------------------------------------------------
# DPAPI bindings
# --------------------------------------------------------------------------------------

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


CRYPTPROTECT_UI_FORBIDDEN = 0x01
CRYPTPROTECT_LOCAL_MACHINE = 0x04

# A fixed secondary entropy value. Not a secret (it is in source): it simply scopes the blob
# to this application, so another program running as the same user cannot decrypt it by
# accident just by pointing DPAPI at the file.
_ENTROPY = b"AIJobAutomation/database-credential/v1"


def _blob(data: bytes) -> _DataBlob:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def dpapi_protect(data: bytes, scope: str = SCOPE_USER) -> bytes:
    """Encrypt with DPAPI. Windows only."""
    flags = CRYPTPROTECT_UI_FORBIDDEN
    if scope == SCOPE_MACHINE:
        flags |= CRYPTPROTECT_LOCAL_MACHINE
    out = _DataBlob()
    entropy = _blob(_ENTROPY)
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(_blob(data)), None, ctypes.byref(entropy), None, None, flags,
        ctypes.byref(out),
    )
    if not ok:
        raise CredentialStoreError(
            f"Windows could not protect the credential (error {ctypes.GetLastError()})."
        )
    try:
        return _blob_bytes(out)
    finally:
        _free(out)


def dpapi_unprotect(data: bytes) -> bytes:
    """Decrypt with DPAPI. Fails if the blob belongs to another user or machine."""
    out = _DataBlob()
    entropy = _blob(_ENTROPY)
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(_blob(data)), None, ctypes.byref(entropy), None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise CredentialStoreError(
            "Windows could not read the stored credential. It was created by a different "
            "Windows user account or on a different computer; re-run provisioning to "
            f"create a new one (error {ctypes.GetLastError()})."
        )
    try:
        return _blob_bytes(out)
    finally:
        _free(out)


# --------------------------------------------------------------------------------------
# Stores
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class StoreInfo:
    """Non-secret description of where and how the credential is protected."""

    mechanism: str
    scope: str
    path: str
    secure: bool


class CredentialStore:
    """Common interface. Implementations only ever move opaque payload dictionaries."""

    mechanism = "abstract"
    secure = False
    scope = SCOPE_USER

    def __init__(self, path: Path) -> None:
        self.path = path

    # -- subclass hooks ------------------------------------------------------------------
    def _encode(self, raw: bytes) -> bytes:
        raise NotImplementedError

    def _decode(self, raw: bytes) -> bytes:
        raise NotImplementedError

    # -- public API ----------------------------------------------------------------------
    def describe(self) -> StoreInfo:
        return StoreInfo(self.mechanism, self.scope, str(self.path), self.secure)

    def exists(self) -> bool:
        return self.path.is_file()

    def save(self, payload: dict) -> None:
        raw = json.dumps({"version": PAYLOAD_VERSION, **payload}).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Written via a temporary file so an interrupted write cannot leave a half-written
        # credential that would lock the user out of their own database.
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(self._encode(raw))
        temporary.replace(self.path)
        self._restrict_permissions()
        logger.info("Stored database credential using %s (%s scope).",
                    self.mechanism, self.scope)

    def load(self) -> dict | None:
        if not self.exists():
            return None
        payload = json.loads(self._decode(self.path.read_bytes()).decode("utf-8"))
        if payload.get("version") != PAYLOAD_VERSION:
            raise CredentialStoreError(
                f"stored credential has unsupported version {payload.get('version')!r}; "
                "re-run provisioning"
            )
        return payload

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def _restrict_permissions(self) -> None:
        """Best-effort tightening. The file already sits in the per-user profile."""
        try:
            os.chmod(self.path, 0o600)
        except OSError:  # pragma: no cover - non-fatal on Windows
            pass


class WindowsDpapiStore(CredentialStore):
    mechanism = "windows-dpapi"
    secure = True

    def __init__(self, path: Path, scope: str = SCOPE_USER) -> None:
        super().__init__(path)
        self.scope = scope

    def _encode(self, raw: bytes) -> bytes:
        return dpapi_protect(raw, self.scope)

    def _decode(self, raw: bytes) -> bytes:
        return dpapi_unprotect(raw)


class InsecureFileStore(CredentialStore):
    """Base64 only -- NOT protection. Development and non-Windows CI, opt-in only."""

    mechanism = "insecure-file"
    secure = False

    def _encode(self, raw: bytes) -> bytes:
        return base64.b64encode(raw)

    def _decode(self, raw: bytes) -> bytes:
        return base64.b64decode(raw)


def insecure_store_enabled() -> bool:
    return (os.environ.get(INSECURE_STORE_ENV) or "").strip().lower() in {"1", "true", "yes"}


def credential_path() -> Path:
    """Where the credential blob lives. Honours an explicit directory override so tests
    never touch the developer's own stored credential."""
    override = (os.environ.get(STORE_DIR_ENV) or "").strip()
    directory = Path(override) if override else runtime_paths().config
    return directory / CREDENTIAL_FILENAME


def windows_dpapi_available() -> bool:
    return sys.platform == "win32" and hasattr(ctypes, "windll")


def get_credential_store(scope: str = SCOPE_USER) -> CredentialStore:
    """The store for this platform.

    Windows uses DPAPI. Anywhere else -- and on Windows when explicitly requested -- the
    insecure store is used, but ONLY when the environment opts in; otherwise there is no
    silent downgrade to unprotected storage."""
    path = credential_path()
    if insecure_store_enabled():
        return InsecureFileStore(path)
    if windows_dpapi_available():
        return WindowsDpapiStore(path, scope)
    raise CredentialStoreError(
        "No secure credential store is available on this platform. Windows DPAPI is "
        f"required; set {INSECURE_STORE_ENV}=1 to use unprotected development storage."
    )
