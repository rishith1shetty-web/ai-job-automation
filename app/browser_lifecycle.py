"""Close the Chromium processes this application started -- and provably only those.

Playwright normally cleans up after itself: ``browser.close()`` ends the browser and its
helper processes. This module is the safety net for when that does not happen -- an automation
run that failed hard, or a shutdown that arrived mid-session -- so the application does not
leave orphaned Chromium processes holding a portal profile open.

The entire design is about **proving ownership before touching anything**. A user very
plausibly has their own Chrome open, possibly signed in to LinkedIn, while this application
runs. Terminating that would be indefensible. So a process is only ever a candidate when BOTH
of these hold:

1. its executable lives inside this installation's bundled ``ms-playwright`` directory --
   the user's own Chrome is at a completely different path, and no other product ships from
   inside our program folder;
2. it descends from THIS process -- established by walking parent process ids up to our own
   pid, so a second instance's browsers are not ours to close either.

Either condition alone is insufficient: (1) alone would match a sibling instance's browsers,
(2) alone would match any child process at all. Requiring both means a false positive would
need an unrelated program to be running from inside our installation as our own descendant.

If ownership cannot be established, nothing happens. That is the deliberate failure mode:
an orphaned browser is a minor annoyance, whereas killing the user's signed-in Chrome is not.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CHROMIUM_IMAGES = frozenset({"chrome.exe", "headless_shell.exe"})
# How long a browser is given to exit after being asked politely.
CLOSE_TIMEOUT_SECONDS = 10.0
_QUERY_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class BrowserProcess:
    """One Chromium process that has been proven to belong to this application."""

    pid: int
    parent_pid: int
    executable: str
    image: str


def _snapshot() -> list[tuple[int, int, str, str]]:
    """(pid, parent_pid, executable_path, image_name) for every process we can see.

    Read-only. Uses PowerShell's CIM interface rather than a third-party library so the
    packaged build gains no dependency."""
    if sys.platform != "win32":
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Select-Object ProcessId,ParentProcessId,ExecutablePath,Name | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|"
        "$($_.ExecutablePath)|$($_.Name)\" }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        logger.debug("Could not enumerate processes: %s", type(error).__name__)
        return []

    rows: list[tuple[int, int, str, str]] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.strip().split("|")
        if len(parts) != 4:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2], parts[3]))
        except ValueError:
            continue
    return rows


def _descends_from(pid: int, ancestor: int, parents: dict[int, int]) -> bool:
    """Does ``pid`` have ``ancestor`` somewhere above it?

    Bounded: the loop cannot run longer than the number of processes, so a corrupted parent
    map containing a cycle cannot hang shutdown."""
    seen = set()
    current = pid
    for _ in range(len(parents) + 1):
        if current in seen:
            return False
        seen.add(current)
        parent = parents.get(current)
        if parent is None or parent == 0:
            return False
        if parent == ancestor:
            return True
        current = parent
    return False


def owned_browsers(root_pid: int | None = None, install_dir=None) -> list[BrowserProcess]:
    """Chromium processes that are provably ours. Empty when ownership cannot be proven."""
    if sys.platform != "win32":
        return []

    from pathlib import Path

    if install_dir is None:
        try:
            from app.windows_launcher import bundled_browsers_dir

            install_dir = bundled_browsers_dir()
        except Exception:  # pragma: no cover - defensive
            install_dir = None
    if install_dir is None:
        logger.debug("No bundled browser directory; not claiming ownership of any process.")
        return []

    root = os.getpid() if root_pid is None else root_pid
    bundled = str(Path(install_dir).resolve()).lower()

    rows = _snapshot()
    parents = {pid: parent for pid, parent, _, _ in rows}

    owned: list[BrowserProcess] = []
    for pid, parent, executable, image in rows:
        if image.lower() not in CHROMIUM_IMAGES:
            continue
        # Condition 1: it runs from inside our installation.
        if not executable or not str(Path(executable).resolve()).lower().startswith(bundled):
            continue
        # Condition 2: it descends from us.
        if pid != root and not _descends_from(pid, root, parents):
            continue
        owned.append(BrowserProcess(pid=pid, parent_pid=parent, executable=executable,
                                    image=image))
    return owned


def close_owned_browsers(root_pid: int | None = None, install_dir=None,
                         timeout: float = CLOSE_TIMEOUT_SECONDS) -> list[int]:
    """Close the browsers this application owns. Returns the process ids acted on.

    Anything not proven to be ours is left completely alone -- including the user's own
    Chrome, another instance's browsers, and every non-Chromium process on the machine."""
    import time

    from app.single_instance import process_is_running

    targets = owned_browsers(root_pid, install_dir)
    if not targets:
        return []

    logger.info("Closing %d Chromium process(es) started by this application.", len(targets))
    acted: list[int] = []
    for target in targets:
        try:
            # /T ends the browser's own child processes (renderers, GPU) with it. Scoped to
            # a pid that has already been proven ours -- never to an image name, which would
            # match every Chrome on the computer.
            subprocess.run(["taskkill", "/PID", str(target.pid), "/T"],
                           capture_output=True, text=True, timeout=_QUERY_TIMEOUT_SECONDS)
            acted.append(target.pid)
        except (OSError, subprocess.SubprocessError) as error:
            logger.debug("Could not close process %s: %s", target.pid, type(error).__name__)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(process_is_running(pid) for pid in acted):
            break
        time.sleep(0.5)
    return acted
