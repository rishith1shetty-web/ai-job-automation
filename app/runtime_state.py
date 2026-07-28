"""Where the running application says what it is, so other processes need not guess.

Two files, both under ``%LOCALAPPDATA%\\AIJobAutomation\\work``, both removed on a clean
shutdown:

``runtime.json``
    Non-secret. Process id, host, port, version, start time, readiness. A second launch reads
    it to find the existing window instead of assuming port 8000; the installer reads it to
    find the exact process it must ask to stop.

``shutdown.token``
    A per-run random value, ACL-restricted to the current user, that authorises the local
    shutdown endpoint. Deliberately in a separate file: the state file is meant to be readable
    (it carries nothing private), and mixing a secret into it would make that unsafe.

**Stale state is normal, not exceptional.** A power cut leaves both files behind. Everything
here therefore validates before trusting: the recorded process must still exist AND still be
this application (process ids are reused -- acting on a recycled one would mean touching an
unrelated program), and the recorded server must actually answer. Anything that fails those
checks is discarded and startup continues normally.

Writes are atomic (temporary file + ``os.replace``), so a crash mid-write can never leave a
half-written state file that parses as valid.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILENAME = "runtime.json"
TOKEN_FILENAME = "shutdown.token"
EXECUTABLE_NAME = "RisingPathRecruiters.exe"

# Fields that are allowed to exist in the state file. Anything else is dropped on read, so a
# future version -- or a tampered file -- cannot smuggle unexpected data into the launcher.
ALLOWED_FIELDS = frozenset({
    "pid", "host", "port", "version", "started_at", "ready", "state_version",
})
STATE_VERSION = 1


@dataclass
class RuntimeState:
    """What a running instance publishes about itself. NOTHING private belongs here."""

    pid: int = 0
    host: str = "127.0.0.1"
    port: int = 0
    version: str = ""
    started_at: float = 0.0
    ready: bool = False
    state_version: int = STATE_VERSION

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def to_dict(self) -> dict:
        return {key: value for key, value in asdict(self).items() if key in ALLOWED_FIELDS}


def work_dir() -> Path:
    from app.paths import runtime_paths

    return runtime_paths().ensure().work


def state_path() -> Path:
    return work_dir() / STATE_FILENAME


def token_path() -> Path:
    return work_dir() / TOKEN_FILENAME


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------

def write_state(state: RuntimeState, path: Path | None = None) -> Path:
    """Publish the state atomically.

    Temporary file plus ``os.replace``: on Windows that is an atomic rename within a volume,
    so a reader never sees a partially written file and a crash mid-write leaves either the
    old state or the new one, never a corrupt mixture."""
    target = path or state_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def clear_state(path: Path | None = None) -> None:
    (path or state_path()).unlink(missing_ok=True)


def read_state(path: Path | None = None) -> RuntimeState | None:
    """Parse the state file. Returns None for anything malformed -- never raises."""
    target = path or state_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    known = {key: value for key, value in payload.items() if key in ALLOWED_FIELDS}
    try:
        return RuntimeState(**known)
    except TypeError:
        return None


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------

def state_is_live(state: RuntimeState | None, *, verify_http: bool = True,
                  timeout: float = 2.0) -> bool:
    """Is this state describing a real, running, reachable instance?

    Three independent checks, because any one of them alone can be fooled:

    1. the recorded process still exists;
    2. it is still THIS application -- process ids are recycled, and a recycled id could
       belong to anything;
    3. the recorded address actually answers ``/health``.
    """
    if state is None or not state.pid or not state.port:
        return False

    from app.single_instance import process_image_name, process_is_running

    if not process_is_running(state.pid):
        return False

    if sys.platform == "win32":
        image = process_image_name(state.pid)
        # An empty answer means "could not determine", which is not proof of anything, so it
        # is not treated as a mismatch. A DIFFERENT name is proof, and rejects.
        if image and image.lower() != EXECUTABLE_NAME.lower():
            logger.info("Recorded process %s is %s, not this application; state is stale.",
                        state.pid, image)
            return False

    if not verify_http:
        return True
    return health_answers(state.host, state.port, timeout)


def health_answers(host: str, port: int, timeout: float = 2.0) -> bool:
    """Does a server answer /health at this address?"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=timeout) as reply:
            return 200 <= reply.status < 500
    except (urllib.error.URLError, OSError, ValueError):
        return False


def port_owner_is_this_application(port: int, host: str = "127.0.0.1") -> bool:
    """Is the process listening on this port one of ours?

    Distinguishes "another Rising Path Recruiters instance" from "an unrelated program"
    without terminating or signalling anything."""
    pid = listening_pid(port, host)
    if pid is None:
        return False
    from app.single_instance import process_image_name

    return process_image_name(pid).lower() == EXECUTABLE_NAME.lower()


def listening_pid(port: int, host: str = "127.0.0.1") -> int | None:
    """The process id listening on a loopback port, via netstat. Read-only."""
    if sys.platform != "win32":
        return None
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    needle = f"{host}:{port}"
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1] == needle \
                and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                return None
    return None


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


# --------------------------------------------------------------------------------------
# The shutdown token
# --------------------------------------------------------------------------------------

TOKEN_BYTES = 32


def generate_token() -> str:
    """A fresh, unguessable value for THIS run only."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def write_token(token: str, path: Path | None = None) -> Path:
    """Store the token so only this Windows user can read it.

    Created empty and locked down BEFORE the value is written: writing first and tightening
    the ACL afterwards would leave a window in which any user could read it."""
    target = path or token_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("", encoding="utf-8")
    restrict_to_current_user(target)
    target.write_text(token, encoding="utf-8")
    return target


def read_token(path: Path | None = None) -> str:
    try:
        return (path or token_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clear_token(path: Path | None = None) -> None:
    target = path or token_path()
    try:
        if target.is_file():
            # Overwrite before unlinking so the value does not linger in free space.
            target.write_text("0" * TOKEN_BYTES, encoding="utf-8")
    except OSError:  # pragma: no cover - best effort
        pass
    target.unlink(missing_ok=True)


def restrict_to_current_user(path: Path) -> None:
    """Remove inherited access; grant only this user, SYSTEM and Administrators."""
    if os.name != "nt":
        path.chmod(0o600)
        return
    account = os.environ.get("USERNAME") or ""
    grants = ["*S-1-5-18:(F)", "*S-1-5-32-544:(F)"]  # SYSTEM, Administrators
    if account:
        grants.insert(0, f"{account}:(F)")
    arguments = ["icacls", str(path), "/inheritance:r"]
    for grant in grants:
        arguments += ["/grant:r", grant]
    try:
        completed = subprocess.run(arguments, capture_output=True, text=True, timeout=30)
        if completed.returncode != 0:
            logger.warning("Could not restrict access to %s.", path.name)
    except (OSError, subprocess.SubprocessError) as error:
        logger.warning("Could not restrict access to %s: %s", path.name, type(error).__name__)


# --------------------------------------------------------------------------------------
# Publishing helpers used by the launcher
# --------------------------------------------------------------------------------------

def publish(port: int, host: str = "127.0.0.1", version: str = "",
            ready: bool = False) -> RuntimeState:
    state = RuntimeState(pid=os.getpid(), host=host, port=port, version=version,
                         started_at=time.time(), ready=ready)
    write_state(state)
    return state


def mark_ready(state: RuntimeState) -> RuntimeState:
    state.ready = True
    write_state(state)
    return state


def cleanup() -> None:
    """Remove everything this run published. Safe to call twice."""
    clear_state()
    clear_token()
