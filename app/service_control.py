"""Start the PostgreSQL Windows service -- that one service, and nothing else.

An installed PostgreSQL whose service happens to be stopped is a routine situation (a
"Manual" start type, a service that failed at boot, someone who stopped it last week). Asking
the user to open services.msc for that is poor behaviour when the application can simply
start it.

The whole risk here is scope. Service control is a blunt instrument, so this module is built
so that starting the wrong thing is not possible:

* the service name never comes from a caller, a pattern or a guess -- it comes from
  :func:`app.postgres_detection.find_services`, which reads the names PostgreSQL's own
  installer registered;
* :func:`is_postgres_service_name` re-validates that name before anything is started, so even
  a corrupted detection result cannot turn into "start an arbitrary service";
* nothing here ever STOPS a service, or changes a start type, or touches a service this
  application did not find as PostgreSQL;
* success is not "the command returned 0" -- it is "PostgreSQL answers a connection
  handshake afterwards", which is the only thing the application actually needs.

``sc.exe`` is used rather than a Python service library so there is no new dependency inside
the packaged build, and the exact command is visible and auditable.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# How long to wait for the service to report RUNNING, and then for it to accept connections.
START_TIMEOUT_SECONDS = 45.0
POLL_INTERVAL_SECONDS = 1.0
COMMAND_TIMEOUT_SECONDS = 30

# Windows: the caller does not have permission to start services.
ERROR_ACCESS_DENIED = 5
# The service is already running -- not a failure.
ERROR_SERVICE_ALREADY_RUNNING = 1056

# The only names this module will ever act on. PostgreSQL's installer registers services as
# "postgresql-x64-16", "postgresql-16" and similar.
_POSTGRES_SERVICE = re.compile(r"^postgresql[\w.-]*$", re.IGNORECASE)


class StartOutcome(Enum):
    """Why the service is, or is not, running now."""

    ALREADY_RUNNING = "already_running"
    STARTED = "started"
    PERMISSION_DENIED = "permission_denied"
    TIMED_OUT = "timed_out"
    NOT_FOUND = "not_found"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"       # not Windows
    REFUSED = "refused"               # the name did not look like PostgreSQL


@dataclass
class StartReport:
    """A secret-free account of what was attempted."""

    outcome: StartOutcome
    service: str = ""
    detail: str = ""
    accepting_connections: bool = False

    @property
    def ok(self) -> bool:
        return self.outcome in (StartOutcome.ALREADY_RUNNING, StartOutcome.STARTED)


def is_postgres_service_name(name: str) -> bool:
    """Would this application be willing to start a service with this name?

    The last line of defence before ``sc start``. A name that does not match is refused, so
    a wrong or tampered detection result cannot be turned into starting something unrelated.
    """
    return bool(name) and bool(_POSTGRES_SERVICE.match(name.strip()))


def _run_sc(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["sc", *arguments], capture_output=True, text=True,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


def query_state(service: str) -> str:
    """RUNNING / STOPPED / START_PENDING / ... or "" when it cannot be read."""
    if sys.platform != "win32":
        return ""
    try:
        completed = _run_sc("query", service)
    except (OSError, subprocess.SubprocessError):
        return ""
    match = re.search(r"STATE\s+:\s+\d+\s+(\w+)", completed.stdout or "")
    return match.group(1).upper() if match else ""


def _exit_code_of(completed: subprocess.CompletedProcess) -> int:
    """sc reports the Windows error in its output as well as its exit code."""
    text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    match = re.search(r"FAILED (\d+)", text)
    if match:
        return int(match.group(1))
    return completed.returncode


def start_postgres_service(service: str, *, timeout: float = START_TIMEOUT_SECONDS,
                           verify=None) -> StartReport:
    """Start ONE named PostgreSQL service and wait for it to accept connections.

    ``verify`` is the callable used to decide whether PostgreSQL is really answering; it is
    injected so the tests can exercise every branch without a database."""
    if sys.platform != "win32":
        return StartReport(StartOutcome.UNSUPPORTED, service,
                           "service control is only available on Windows")

    if not is_postgres_service_name(service):
        # Refuse rather than proceed: this is the guard that makes "start a service" safe.
        logger.error("Refusing to start %r: it is not a PostgreSQL service name.", service)
        return StartReport(StartOutcome.REFUSED, service,
                           "the name is not a PostgreSQL service")

    state = query_state(service)
    if state == "RUNNING":
        logger.info("PostgreSQL service %s is already running.", service)
        return StartReport(StartOutcome.ALREADY_RUNNING, service,
                           accepting_connections=_verify(verify))
    if not state:
        return StartReport(StartOutcome.NOT_FOUND, service, "the service could not be queried")

    logger.info("PostgreSQL service %s is %s; starting it.", service, state)
    try:
        completed = _run_sc("start", service)
    except (OSError, subprocess.SubprocessError) as error:
        return StartReport(StartOutcome.FAILED, service, type(error).__name__)

    code = _exit_code_of(completed)
    if code == ERROR_ACCESS_DENIED:
        logger.warning("Not permitted to start the PostgreSQL service.")
        return StartReport(StartOutcome.PERMISSION_DENIED, service,
                           "administrator rights are required to start the service")
    if code not in (0, ERROR_SERVICE_ALREADY_RUNNING):
        return StartReport(StartOutcome.FAILED, service, f"sc start reported error {code}")

    # "sc start" returns as soon as the request is accepted, so the state has to be polled.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if query_state(service) == "RUNNING":
            break
        time.sleep(POLL_INTERVAL_SECONDS)
    else:
        return StartReport(StartOutcome.TIMED_OUT, service,
                           f"the service did not reach RUNNING within {timeout:.0f}s")

    # RUNNING is not the same as ready: PostgreSQL performs recovery before it listens.
    while time.monotonic() < deadline:
        if _verify(verify):
            logger.info("PostgreSQL service %s is running and accepting connections.", service)
            return StartReport(StartOutcome.STARTED, service, accepting_connections=True)
        time.sleep(POLL_INTERVAL_SECONDS)

    return StartReport(StartOutcome.TIMED_OUT, service,
                       "the service started but did not accept connections in time")


def _verify(verify) -> bool:
    """Ask whether PostgreSQL is actually answering. Never raises."""
    try:
        if verify is not None:
            return bool(verify())
        from app.postgres_detection import probe_port

        return probe_port().is_postgres
    except Exception:  # pragma: no cover - defensive
        return False


def start_detected_postgres(report=None, *, timeout: float = START_TIMEOUT_SECONDS,
                            verify=None) -> StartReport:
    """Start whichever PostgreSQL service detection found stopped.

    The service name comes only from detection -- never from configuration or a caller -- so
    there is no path by which an arbitrary name reaches ``sc start``."""
    from app.postgres_detection import detect

    report = report or detect()
    candidates = [service.name for service in report.services
                  if is_postgres_service_name(service.name)]
    if not candidates:
        return StartReport(StartOutcome.NOT_FOUND, "",
                           "no PostgreSQL service is registered on this computer")

    # Prefer one that is already running; otherwise take the highest-numbered (newest) one.
    for service in report.services:
        if service.running and is_postgres_service_name(service.name):
            return StartReport(StartOutcome.ALREADY_RUNNING, service.name,
                               accepting_connections=_verify(verify))

    chosen = sorted(candidates)[-1]
    return start_postgres_service(chosen, timeout=timeout, verify=verify)
