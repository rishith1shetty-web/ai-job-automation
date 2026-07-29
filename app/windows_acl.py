"""Lock a file down to the account that created it, by SID rather than by name.

Two files in this application hold a secret for a short time -- the PostgreSQL option file
(``app/postgres_install.py``) and the shutdown token (``app/runtime_state.py``). Both had
their own copy of the same ``icacls`` call, and both copies contained the same defect:

    account = os.environ.get("USERNAME")          # "PC10"
    icacls <file> /inheritance:r /grant:r "PC10:(F)" ...

An account NAME is ambiguous. ``LookupAccountName`` resolves it against the built-in domain,
the local SAM domain -- which is named after the computer -- and then any joined domain. On
a workgroup computer named ``PC10`` whose user is also called ``PC10``, the bare string
resolves to the machine's SAM DOMAIN, not to the user: a SID that no access token contains.
``icacls`` reports success, because the name did resolve to something.

The consequence is severe rather than cosmetic, because ``/inheritance:r`` removes the
inherited grant in the same command. The file is left with three ACEs, none of which the
writing process holds:

* the wrong SID from the ambiguous name,
* ``SYSTEM``,
* ``Administrators`` -- which is DENY-ONLY in the filtered token of a process that is not
  elevated, so it grants nothing even when the user is an administrator.

The very next write to the file then fails with ``PermissionError: [Errno 13]``. That is
exactly what happened on the clean machine PC10: ``prepare-postgres`` could not fill the
option file it had just created, and ``provision`` could not read it back, so PostgreSQL was
never installed.

The fix is to stop naming the account at all. The token's own user SID is unambiguous on
every configuration -- workgroup, domain-joined, renamed account, or a username that happens
to equal the computer name -- and it needs no lookup.

``SYSTEM`` and ``Administrators`` stay: the elevated PostgreSQL installer is a different
process, quite possibly running as a DIFFERENT administrator account (an installation
approved at the UAC prompt with another user's credentials), and it has to read the option
file. What the restriction removes is access by the computer's OTHER standard users.

Finally, the restriction is VERIFIED rather than assumed. ``icacls`` exiting 0 does not
prove the caller can still use its own file, and a silent lockout surfaces far away from its
cause as a bare ``PermissionError``. Both callers pass an empty file here, so failing at
this point is safe: no secret has been written yet.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# Well-known SIDs, used in their literal form so that no name lookup is involved.
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"

# Groups that must never end up with access to a file holding a secret.
OPEN_GROUP_SIDS = ("S-1-1-0", "S-1-5-32-545", "S-1-5-11")  # Everyone, Users, Authenticated


class AccessRestrictionError(RuntimeError):
    """The file could not be restricted, or was restricted so tightly we lost access."""


def current_user_sid() -> str:
    """The SID of the account this process is running as, e.g. ``S-1-5-21-...-1001``.

    Read from the process token, so it is the real answer even when ``%USERNAME%`` is
    misleading, unset, or identical to the computer name. Returns "" if it cannot be
    determined, which callers must treat as "do not restrict"."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD,
                                                 ctypes.POINTER(wintypes.DWORD)]
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(ctypes.c_wchar_p)]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        TOKEN_QUERY = 0x0008
        TOKEN_USER_CLASS = 1

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                         ctypes.byref(token)):
            return ""
        try:
            size = wintypes.DWORD(0)
            # First call fails with ERROR_INSUFFICIENT_BUFFER and reports the size needed.
            advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, None, 0, ctypes.byref(size))
            if size.value == 0:
                return ""
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, TOKEN_USER_CLASS, buffer, size.value,
                                                ctypes.byref(size)):
                return ""
            # TOKEN_USER begins with SID_AND_ATTRIBUTES, whose first member is the PSID.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
            text = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return ""
            try:
                return text.value or ""
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    except Exception as error:  # pragma: no cover - defensive; any failure means "unknown"
        logger.debug("Could not read the current user SID: %s", type(error).__name__)
        return ""


def restriction_command(path: Path, sid: str) -> list[str]:
    """The exact ``icacls`` command used. Separated so tests can assert on it.

    Every principal is given as a literal SID (``*S-1-...``), so no part of this command
    depends on a name lookup."""
    grants = [f"*{sid}:(F)", f"*{SYSTEM_SID}:(F)", f"*{ADMINISTRATORS_SID}:(F)"]
    arguments = ["icacls", str(path), "/inheritance:r"]
    for grant in grants:
        arguments += ["/grant:r", grant]
    return arguments


def _still_usable(path: Path) -> bool:
    """Can this process still read AND write the file it just restricted?"""
    try:
        with path.open("r+b"):
            return True
    except OSError:
        return False


def _reset_access(path: Path) -> None:
    """Put the inherited permissions back after a restriction that locked us out."""
    try:
        subprocess.run(["icacls", str(path), "/reset"], capture_output=True, text=True,
                       timeout=30)
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - best effort
        logger.debug("Could not restore inherited permissions on %s.", path.name)


def restrict_to_current_user(path: Path) -> None:
    """Grant this account, SYSTEM and Administrators; remove everything else.

    Raises :class:`AccessRestrictionError` rather than leaving the caller with a file it
    cannot use. Call it on an EMPTY file, before the secret goes in."""
    if os.name != "nt":  # pragma: no cover - Windows-only protection
        path.chmod(0o600)
        return

    sid = current_user_sid()
    if not sid:
        # Locking the file to SYSTEM and Administrators alone would lock this process out.
        # Inherited access is weaker than intended but still confined to the user's own
        # profile directory, so leaving it is the safer of the two failures.
        raise AccessRestrictionError(
            "Could not determine this account's security identifier, so the file was left "
            "with its inherited permissions.")

    try:
        completed = subprocess.run(restriction_command(path, sid),
                                   capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        raise AccessRestrictionError(
            f"Could not run icacls: {type(error).__name__}") from None

    if completed.returncode != 0:
        raise AccessRestrictionError(
            (completed.stderr or completed.stdout or "icacls failed").strip())

    if not _still_usable(path):
        # Leaving a file the caller cannot open is worse than leaving it unrestricted, and
        # the owner always keeps WRITE_DAC, so the inherited permissions can be put back.
        _reset_access(path)
        raise AccessRestrictionError(
            "The permissions were applied but this account could no longer use the file, so "
            "the inherited permissions were restored.")


def describe_access(path: Path) -> str:
    """The current ACL as text, for diagnostics. Never contains file content."""
    if os.name != "nt":  # pragma: no cover - Windows-only helper
        return ""
    try:
        completed = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                                   timeout=30)
        return (completed.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - diagnostics only
        return ""
