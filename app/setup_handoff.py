"""The transient channel that carries the PostgreSQL administrator password to provisioning.

The password is typed into the installer's own wizard page. Provisioning runs in a separate
process, so the two have to be connected somehow -- and every obvious way of doing that is
forbidden:

* a command-line argument is readable by *every* user on the computer (``Get-CimInstance
  Win32_Process``), so argv is out;
* an environment variable survives in the child's environment block and in any crash dump
  that captures it, so the environment is out;
* a plain-text temporary file is readable for as long as it exists, however short that is,
  and an ACL is the only thing standing between it and every process running as this user;
* ``ConvertFrom-SecureString`` would put the plain text on a PowerShell command line and
  into ``ConsoleHost_history.txt``.

What is left is to encrypt the secret *before* it ever reaches the filesystem. This module is
the Python half of that: DPAPI in CURRENT-USER scope protects the password in memory, only
the ciphertext is written, and the file additionally carries a restrictive ACL so that even
the ciphertext is not casually readable. The installer's Pascal code performs the matching
``CryptProtectData`` call; see ``installer/RisingPathRecruiters.iss``.

**Why user scope works across the elevation boundary.** The wizard runs elevated and
provisioning runs as the original interactive user. Those are two different tokens, but --
when the same person approved the UAC prompt -- the same *account*, and a DPAPI user-scope
master key belongs to the account, not to the token's elevation level. When a *different*
administrator approves the prompt, decryption fails cleanly rather than silently storing a
credential the real user could never read. That is the correct outcome, and the error message
says exactly what to do about it.

The file is hexadecimal text rather than raw bytes purely so the installer can write it with
``SaveStringToFile``; the content is ciphertext either way.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

HANDOFF_FILENAME = "setup-handoff.bin"

# Recorded inside the DPAPI blob. Not a secret and not a protection -- it is what identifies
# the blob if one is ever found, so that it is obvious what produced it.
HANDOFF_DESCRIPTION = "Rising Path Recruiters setup"

CRYPTPROTECT_UI_FORBIDDEN = 0x1

# A protected password of this length would already be far beyond anything a person types;
# anything larger means the file is not what this module wrote.
MAX_BLOB_BYTES = 65536


class HandoffError(RuntimeError):
    """The secure handoff could not be created, read or destroyed."""


def _windows() -> bool:
    return os.name == "nt"


if _windows():  # pragma: no cover - the structure is only meaningful on Windows
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
else:  # pragma: no cover - defined so the module imports anywhere
    class _DataBlob(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.c_void_p)]


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _free(blob: _DataBlob) -> None:
    if blob.pbData:
        ctypes.windll.kernel32.LocalFree(blob.pbData)


def protect(secret: str) -> bytes:
    """DPAPI-protect a secret for the CURRENT WINDOWS USER.

    Used by the tests to produce a blob identical in shape to the installer's, and by the
    installer bridge's self-check. The application itself never needs to protect the
    administrator password -- it only ever reads one."""
    if not _windows():
        raise HandoffError("The secure handoff is only available on Windows.")

    raw = secret.encode("utf-8")
    source = _DataBlob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw, len(raw)),
                                             ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(source), HANDOFF_DESCRIPTION, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    if not ok:
        raise HandoffError(
            f"Windows could not protect the setup handoff (error {ctypes.GetLastError()}).")
    try:
        return _blob_bytes(target)
    finally:
        _free(target)


def unprotect(blob: bytes) -> str:
    """Recover a secret protected by :func:`protect` or by the installer's wizard page.

    A failure here is almost always one specific thing: the blob was produced by a different
    Windows account. The caller turns that into an instruction, never into a stack trace that
    could carry fragments of the value."""
    if not _windows():
        raise HandoffError("The secure handoff is only available on Windows.")
    if not blob:
        raise HandoffError("The setup handoff was empty.")

    source = _DataBlob(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)),
                                              ctypes.POINTER(ctypes.c_char)))
    target = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(target))
    if not ok:
        raise HandoffError(
            "The setup handoff could not be read by this Windows account. Run setup while "
            "signed in as the account that will use the application.")
    try:
        return _blob_bytes(target).decode("utf-8")
    finally:
        _free(target)


# --------------------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------------------

def handoff_path() -> Path:
    from app.installer_bridge import work_dir

    return work_dir() / HANDOFF_FILENAME


def create_channel(path: Path | None = None) -> Path:
    """Create the empty, ACL-restricted file the installer will write ciphertext into.

    The file is created and locked down *before* it holds anything, which is what makes a
    failure to secure it recoverable: there is nothing in it yet to leak, so it is simply
    removed and the caller reports the problem.

    The restriction names the account by its token SID rather than by ``%USERNAME%``; that
    distinction is not cosmetic -- see ``app/windows_acl.py``."""
    from app.windows_acl import AccessRestrictionError
    from app.windows_acl import restrict_to_current_user

    target = path or handoff_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    try:
        restrict_to_current_user(target)
    except AccessRestrictionError as error:
        target.unlink(missing_ok=True)
        raise HandoffError(f"Could not restrict access to the setup handoff: {error}") from None
    return target


def write_channel(secret: str, path: Path | None = None) -> Path:
    """Create the channel and put a protected secret in it. Used by the tests, and by any
    future caller that runs on the same side of the boundary as the reader."""
    target = create_channel(path)
    target.write_text(protect(secret).hex(), encoding="ascii")
    return target


def read_channel(path: Path | None = None) -> str:
    """Read and decrypt the handoff. Raises :class:`HandoffError` with an actionable message.

    The file is left in place: destroying it is :func:`destroy_channel`'s job and belongs in
    the caller's ``finally``, so that a failure anywhere in provisioning still removes it."""
    target = path or handoff_path()
    if not target.is_file():
        raise HandoffError("The setup handoff was not found.")

    try:
        text = target.read_text(encoding="ascii").strip()
    except OSError as error:
        raise HandoffError(f"The setup handoff could not be read: {error.strerror}.") from None

    if not text:
        raise HandoffError("The setup handoff was empty.")
    if len(text) > MAX_BLOB_BYTES * 2:
        raise HandoffError("The setup handoff is not in the expected format.")
    try:
        blob = bytes.fromhex(text)
    except ValueError:
        raise HandoffError("The setup handoff is not in the expected format.") from None
    return unprotect(blob)


def destroy_channel(path: Path | None = None) -> bool:
    """Overwrite and remove the handoff file. Safe to call when it is already gone.

    Overwriting ciphertext is belt and braces; the real protections are DPAPI and how briefly
    the file exists. It is done anyway because "the secret material is gone" should not
    depend on an argument about filesystem internals."""
    from app.postgres_install import OptionFileError, secure_delete

    target = path or handoff_path()
    try:
        return secure_delete(target)
    except OptionFileError as error:
        raise HandoffError(str(error)) from None


def residue(path: Path | None = None) -> str:
    """A non-secret description of what, if anything, the handoff left behind.

    Reported after provisioning so that "nothing remains" is an observation rather than an
    assumption. It never opens the file."""
    target = path or handoff_path()
    if not target.is_file():
        return "none"
    try:
        return f"{target.name} still present ({target.stat().st_size} bytes)"
    except OSError:  # pragma: no cover - racing with our own deletion
        return "none"


def self_check() -> bool:
    """Can this account protect and recover a value right now?

    Run before the installer is told the channel is usable, so that a computer where DPAPI is
    unavailable fails at the password page rather than half-way through provisioning."""
    if not _windows():
        return False
    probe = "handoff-self-check"
    try:
        return unprotect(protect(probe)) == probe
    except HandoffError:
        return False


__all__ = [
    "HANDOFF_DESCRIPTION",
    "HANDOFF_FILENAME",
    "HandoffError",
    "create_channel",
    "destroy_channel",
    "handoff_path",
    "protect",
    "read_channel",
    "residue",
    "self_check",
    "unprotect",
    "write_channel",
]

if sys.platform != "win32":  # pragma: no cover - documentation, not behaviour
    logger.debug("The setup handoff is a Windows-only mechanism.")
