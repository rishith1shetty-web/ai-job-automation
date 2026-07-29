"""One running copy per Windows user, using a named kernel mutex.

The previous mechanism was a PID file with a liveness check. That works, but it has a race
(two launches can both find the file absent) and it depends on the file being cleaned up --
a hard kill leaves it behind, and the recovery path is guesswork.

A named mutex has neither problem. The kernel owns it, so acquisition is atomic; and when
the owning process dies without releasing it, Windows reports ``WAIT_ABANDONED`` to the next
waiter, which then owns it. A crash therefore cannot lock the user out, and no cleanup is
needed for correctness.

**Scope: one instance per interactive user, not per machine.** The name is prefixed
``Local\\``, which places it in the caller's session namespace. Two people using the same
computer -- or the same person on two Remote Desktop sessions -- each get their own instance,
which matches the data model exactly: every Windows account has its own
``%LOCALAPPDATA%\\AIJobAutomation``, its own DPAPI credential and its own portal sessions.
A ``Global\\`` mutex would let one account's running copy block another account entirely.

The name is derived from the permanent ``APP_ID`` in :mod:`app.version`, so it is stable
across upgrades and cannot collide with another product.

:class:`FileInstanceGuard` is the fallback for non-Windows test runs. It is not as strong --
that is the point of keeping the two behind one interface: the tests exercise the logic on
any platform, while Windows gets the kernel object.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows error code returned when the mutex already exists.
ERROR_ALREADY_EXISTS = 183
# WaitForSingleObject results.
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


def mutex_name(app_id: str | None = None) -> str:
    """A stable, session-scoped name derived from the permanent application identity."""
    if app_id is None:
        from app.version import APP_ID

        app_id = APP_ID
    # Mutex names may not contain a backslash beyond the namespace prefix.
    cleaned = app_id.strip("{}").replace("\\", "-")
    return f"Local\\RisingPathRecruiters-{cleaned}"


class InstanceGuard:
    """Common interface: ``acquire()`` says whether this process is the first one."""

    #: True when the previous owner died without releasing -- recovered, not an error.
    recovered_abandoned = False

    def acquire(self) -> bool:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> "InstanceGuard":
        return self

    def __exit__(self, *_) -> None:
        self.release()


class WindowsMutexGuard(InstanceGuard):
    """A named kernel mutex in the caller's session namespace."""

    def __init__(self, name: str | None = None) -> None:
        self.name = name or mutex_name()
        self.handle = None
        self.acquired = False

    def acquire(self) -> bool:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

        # bInitialOwner=False, then wait: this keeps the "already exists" answer and the
        # "who owns it" answer separate, which is what makes abandonment detectable.
        handle = kernel32.CreateMutexW(None, False, self.name)
        last_error = ctypes.get_last_error()
        if not handle:
            logger.warning("Could not create the instance mutex (error %s); "
                           "falling back to allowing startup.", last_error)
            return True

        self.handle = handle
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        result = kernel32.WaitForSingleObject(handle, 0)

        if result == WAIT_OBJECT_0:
            self.acquired = True
        elif result == WAIT_ABANDONED:
            # The previous owner died without releasing. Windows has handed ownership over;
            # this process is now the single instance.
            self.acquired = True
            self.recovered_abandoned = True
            logger.info("Recovered an abandoned instance lock from a previous run.")
        else:
            # WAIT_TIMEOUT: someone else holds it and is alive.
            self.acquired = False
            if last_error != ERROR_ALREADY_EXISTS:
                logger.debug("Instance mutex is held (wait result 0x%X).", result)

        return self.acquired

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if self.acquired:
                kernel32.ReleaseMutex(self.handle)
            kernel32.CloseHandle(self.handle)
        except Exception:  # pragma: no cover - process is ending anyway
            pass
        finally:
            self.handle = None
            self.acquired = False


class FileInstanceGuard(InstanceGuard):
    """An ``O_EXCL`` lock file, for platforms without a Windows mutex.

    Used by the test suite on any OS and as a last resort. Weaker than the mutex: a hard kill
    leaves the file behind, so a recorded PID that is no longer running is treated as
    abandoned -- the same outcome the kernel gives us for free on Windows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def _holder_is_alive(self) -> bool:
        try:
            recorded = int(self.path.read_text(encoding="utf-8").strip().split()[0])
        except (OSError, ValueError, IndexError):
            return False
        if recorded == os.getpid():
            return False
        return process_is_running(recorded)

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._holder_is_alive():
            self.recovered_abandoned = True
            logger.info("Removing a stale instance lock from a previous run.")
            self.path.unlink(missing_ok=True)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{os.getpid()}\n")
        self.acquired = True
        return True

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False


# Returned by GetExitCodeProcess while a process is still running.
STILL_ACTIVE = 259


def process_is_running(pid: int) -> bool:
    """Is a process with this id alive? Never signals it, never touches it.

    On Windows, opening a handle is NOT sufficient. A process that has exited stays as a
    kernel object for as long as anyone holds a handle to it, and ``OpenProcess`` succeeds
    for that object -- so a handle-only check reports a finished process as running, forever,
    whenever a parent or a tool is still holding on to it. That is exactly what happened when
    the installer waited for the application to stop: it had stopped, and the wait timed out
    anyway. The exit code is what actually distinguishes the two states.

    (A process that genuinely exits with code 259 is indistinguishable from a running one by
    this API. That is a documented Windows quirk with no workaround; this application never
    exits with that code.)"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            # PROCESS_QUERY_LIMITED_INFORMATION: enough to ask, not enough to affect it.
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)  # signal 0 only tests for existence
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def process_image_name(pid: int) -> str:
    """The executable name of a running process, or "" when it cannot be determined.

    Used to confirm that a PID recorded in the runtime state still belongs to THIS
    application before acting on it -- process ids are reused, and acting on a recycled id
    would mean touching an unrelated program."""
    if pid <= 0 or sys.platform != "win32":
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return Path(buffer.value).name
            return ""
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


def create_guard(lock_path: Path, name: str | None = None) -> InstanceGuard:
    """The strongest guard this platform supports."""
    if sys.platform == "win32":
        return WindowsMutexGuard(name)
    return FileInstanceGuard(lock_path)
