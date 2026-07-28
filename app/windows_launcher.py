"""The entry point of the packaged Windows application.

Replaces start_app.ps1 for an installed computer. ``start_app.ps1`` remains the DEVELOPMENT
launcher and is unchanged.

Startup, in order -- each step exists because the one before it can fail in a way the user
needs described differently:

1.  start logging, BEFORE any import that can fail (:mod:`app.bootstrap_log`);
2.  take the single-instance mutex; if another copy is already running, open ITS window
    instead of starting a second server;
3.  point Playwright at the bundled Chromium and keep Chromium's own log out of the
    installation;
4.  find PostgreSQL, and start its service if it is installed but stopped;
5.  resolve the credential and open one connection;
6.  ``alembic upgrade head``, then verify the resulting schema;
7.  choose a loopback port, publish the runtime state, start FastAPI;
8.  wait for the API *and* the web interface to answer, then open the browser once;
9.  on shutdown: stop accepting work, close the browsers this application started, stop the
    server, and remove the runtime state and token.

**This module imports almost nothing at the top.** It is the first code to run in a windowed
build with no console, so an ``ImportError`` in SQLAlchemy or SciPy here would be invisible.
Everything heavy is imported inside the function that needs it, after logging exists.

Everything writable goes to ``%LOCALAPPDATA%\\AIJobAutomation`` (app/paths.py). The packaged
program folder is only ever read. No message, log line or dialog contains a password.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)

APP_TITLE = "Rising Path Recruiters"
HOST = "127.0.0.1"
PREFERRED_PORT = 8000
# Scanned in order when the preferred port is taken. Loopback only, never 0.0.0.0.
PORT_SEARCH_RANGE = 40
READINESS_TIMEOUT_SECONDS = 90
LOCK_FILENAME = "launcher.lock"
LOG_FILENAME = "launcher.log"

BROWSER_ENV = "PLAYWRIGHT_BROWSERS_PATH"
BUNDLED_BROWSERS_DIRNAME = "ms-playwright"
# Chromium writes "debug.log" NEXT TO ITS OWN BINARY unless this variable says otherwise --
# a write into the installed program folder, which fails outright under Program Files. It is
# redirected into the per-user log directory with everything else.
CHROME_LOG_ENV = "CHROME_LOG_FILE"
CHROME_LOG_FILENAME = "chromium.log"
# A page that is served by Chromium itself: no network request, no portal, no cookies.
BLANK_PAGE = "about:blank"

# Used by --selftest only, and only when nothing is configured. Deliberately carries NO
# password -- there is nothing to authenticate to. Port 1 on loopback with a placeholder
# name: obviously not a real database, and never connected to, because creating a SQLAlchemy
# engine does not open a connection.
SELFTEST_PLACEHOLDER_URL = "postgresql+psycopg://selftest@127.0.0.1:1/selftest"

# What a served React shell always contains. Readiness requires this, not just /health:
# a backend that answers while the web interface 503s would otherwise open a broken page.
SHELL_MARKER = '<div id="root">'
BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


class ExitCode(IntEnum):
    """Stable process exit codes. Scripts and the installer branch on these."""

    SUCCESS = 0
    ALREADY_RUNNING = 3
    POSTGRES_NOT_FOUND = 10
    PORT_OCCUPIED_BY_OTHER = 11
    MIGRATION_FAILED = 14
    SCHEMA_VERIFICATION_FAILED = 15
    CREDENTIALS_MISSING = 17
    AUTHENTICATION_FAILED = 18
    STARTUP_FAILED = 19
    SERVICE_START_FAILED = 24
    FRONTEND_MISSING = 25
    BROWSER_MISSING = 26
    SHUTDOWN_FAILED = 27


@dataclass
class StartupProblem:
    """A failure translated into something a person can act on."""

    code: ExitCode
    title: str
    message: str

    def as_dialog(self) -> str:
        return f"{self.title}\n\n{self.message}"


def app_version() -> str:
    try:
        from app.version import VERSION

        return VERSION
    except Exception:  # pragma: no cover - defensive
        return ""


# --------------------------------------------------------------------------------------
# Packaged resources
# --------------------------------------------------------------------------------------

def bundled_browsers_dir() -> Path | None:
    """The Chromium shipped with the application.

    Packaged: ``<install>/ms-playwright``. Development: the project-local
    ``storage/ms-playwright`` that start_app.ps1 already uses. The developer's global cache
    in %LOCALAPPDATA% is never selected -- a packaged computer has no such cache."""
    from app.paths import install_root, is_packaged, project_root

    candidates = [install_root() / BUNDLED_BROWSERS_DIRNAME]
    if not is_packaged():
        candidates.append(project_root() / "storage" / BUNDLED_BROWSERS_DIRNAME)
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def redirect_chromium_log() -> Path:
    """Send Chromium's own debug log to the per-user log directory.

    Without this, Chromium writes ``debug.log`` into the directory holding its binary --
    which in a real installation is the read-only program folder. An explicit setting from
    the environment wins."""
    from app.paths import runtime_paths

    target = runtime_paths().logs / CHROME_LOG_FILENAME
    os.environ.setdefault(CHROME_LOG_ENV, str(target))
    return Path(os.environ[CHROME_LOG_ENV])


def configure_playwright_browsers() -> Path | None:
    """Point Playwright at the bundled browsers for this process and its children, and keep
    Chromium's own log out of the installation."""
    redirect_chromium_log()
    directory = bundled_browsers_dir()
    if directory is None:
        logger.warning("No bundled Playwright browsers found; automation may not start.")
        return None
    os.environ[BROWSER_ENV] = str(directory)
    logger.info("Playwright browsers: %s", directory)
    return directory


def chromium_executable(browsers_dir: Path | None = None) -> Path | None:
    """The Chromium executable inside the bundled browser directory, if present."""
    directory = browsers_dir or bundled_browsers_dir()
    if directory is None:
        return None
    matches = sorted(directory.glob("chromium-*/chrome-win/chrome.exe"))
    return matches[0] if matches else None


def global_browser_cache() -> Path | None:
    """The developer's own ``%LOCALAPPDATA%\\ms-playwright``, if this computer has one.

    Returned so a packaged build can PROVE it did not use it. A clean installation has no
    such directory at all; a build machine does, which is exactly where a silent fallback
    would go unnoticed."""
    from app.paths import local_app_data_base

    candidate = local_app_data_base() / BUNDLED_BROWSERS_DIRNAME
    return candidate if candidate.is_dir() else None


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def browser_check() -> StartupProblem | None:
    """Launch the BUNDLED Chromium, open a blank page, and close it again.

    This is the packaged-automation smoke test. It deliberately does the minimum that still
    proves the browser works from inside the installation:

    * the executable Playwright resolves must live inside the packaged folder, and must NOT
      be the developer's global ``ms-playwright`` cache;
    * a browser is really started (its version is read from the running process, not from a
      file on disk) and really closed.

    It NEVER opens LinkedIn or Dice, never loads ``storage_state``, and never signs in, so no
    saved portal session is read or written. The page it opens is ``about:blank``."""
    import traceback

    from app.paths import install_root

    directory = configure_playwright_browsers()
    if directory is None:
        return StartupProblem(
            ExitCode.BROWSER_MISSING,
            "No bundled browser",
            "This installation contains no bundled Chromium, so automation cannot run.\n\n"
            f"Expected it in: {install_root() / BUNDLED_BROWSERS_DIRNAME}",
        )

    cache = global_browser_cache()
    print(f"BROWSER bundled-directory={directory}")
    print(f"BROWSER global-cache={cache if cache else '(none on this computer)'}")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            # The path Playwright itself resolved -- the only authoritative answer to
            # "which Chromium would actually start".
            resolved = Path(playwright.chromium.executable_path)
            inside = _is_inside(resolved, install_root())
            from_cache = bool(cache and _is_inside(resolved, cache))
            print(f"BROWSER executable={resolved}")
            print(f"BROWSER inside-installation={inside}")
            print(f"BROWSER global-cache-used={from_cache}")
            if not inside or from_cache:
                return StartupProblem(
                    ExitCode.BROWSER_MISSING,
                    "Wrong browser selected",
                    "Playwright resolved a Chromium outside this installation:\n"
                    f"{resolved}\n\nThe packaged build must use its own bundled browser.",
                )

            # Headed, because that is what the automation itself runs (linkedin_headless
            # defaults to False) and it is the mode that uses the executable verified above.
            # A headless launch would start a DIFFERENT bundled binary, so the path checked
            # and the path exercised would not be the same one.
            browser = playwright.chromium.launch(headless=False)
            try:
                # A fresh, empty context: no storage_state, so the saved LinkedIn and Dice
                # sessions are neither read nor written.
                context = browser.new_context()
                page = context.new_page()
                page.goto(BLANK_PAGE)
                print(f"BROWSER version={browser.version}")
                print(f"BROWSER page-url={page.url}")
                context.close()
            finally:
                browser.close()
    except Exception as error:
        return StartupProblem(
            ExitCode.BROWSER_MISSING,
            "Browser did not start",
            f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
        )

    print("BROWSER OK")
    return None


# --------------------------------------------------------------------------------------
# Ports
# --------------------------------------------------------------------------------------

def port_is_free(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


def find_available_port(preferred: int = PREFERRED_PORT, host: str = HOST,
                        attempts: int = PORT_SEARCH_RANGE) -> int:
    """The preferred port, or the next free one. Never binds anything but loopback."""
    for offset in range(attempts):
        candidate = preferred + offset
        if port_is_free(candidate, host):
            return candidate
    raise RuntimeError(
        f"No free port found between {preferred} and {preferred + attempts - 1}."
    )


def describe_port_conflict(port: int, host: str = HOST) -> str:
    """Who is holding the preferred port -- another copy of this application, or something
    else entirely? The two need different advice, so they are distinguished rather than
    lumped together as "port in use"."""
    from app.runtime_state import port_owner_is_this_application

    if port_owner_is_this_application(port, host):
        return "another copy of this application"
    return "another program"


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def configure_logging(stream: bool = True) -> Path | None:
    """Start the bootstrap logger. Safe to call before anything risky is imported."""
    from app import bootstrap_log

    log_file = bootstrap_log.configure(stream=stream)
    bootstrap_log.install_excepthook()
    return log_file


def log_file_reference() -> str:
    """A log path to show a user -- only when the file genuinely exists.

    A dialog that names a file which was never created sends people looking for something
    that is not there; that happened once and is worth never repeating."""
    from app import bootstrap_log

    return bootstrap_log.log_file_status()


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------

# Set for verification runs (--check / --selftest / --installer / --shutdown). A modal
# message box would block an automated check or an installer wizard forever, so those modes
# report to stderr instead. The normal GUI launch is unaffected and still shows dialogs.
_interactive = True


def set_interactive(value: bool) -> None:
    global _interactive
    _interactive = value


def show_error_dialog(problem: StartupProblem, secrets: tuple[str, ...] = ()) -> None:
    """A Windows message box, branded and free of secrets.

    Everything shown is redacted twice: once through the bootstrap redactor, which removes
    credential-shaped text wherever it came from, and once against any specific values the
    caller knows about."""
    from app.bootstrap_log import redact

    text = redact(problem.as_dialog())
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")

    if not _interactive:
        print(text, file=sys.stderr)
        return
    try:
        import ctypes

        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, f"{APP_TITLE} - {problem.title}",
                                         0x00 | 0x10 | 0x10000)
    except Exception:
        # No GUI available (a service, a locked session). stderr may also be absent in a
        # windowed build, so the log is the guaranteed record -- it was written above.
        try:
            print(text, file=sys.stderr)
        except Exception:  # pragma: no cover - nothing left to do
            pass


def report(problem: StartupProblem, secrets: tuple[str, ...] = ()) -> int:
    """Log a failure, show it, and return its exit code. One path for every failure."""
    logger.error("%s: %s", problem.title, problem.message.splitlines()[0])
    logger.debug("Full problem detail:\n%s", problem.message)
    show_error_dialog(problem, secrets)
    return int(problem.code)


def selftest() -> StartupProblem | None:
    """Import everything the running application needs, and report the first failure.

    Exists because a packaging gap shows up as an ImportError deep inside a third-party
    package, and finding it by starting the whole application is slow and pops a dialog.
    This imports the same module graph and prints the traceback.

    ``app.main`` resolves the application settings at import time, and the settings resolve
    the database URL -- so on a computer that has not been provisioned yet the import would
    fail for a reason that says nothing about packaging. When no credential is configured an
    inert placeholder URL is supplied for the duration of the import ONLY. SQLAlchemy does not
    connect when an engine is created, so nothing is contacted and nothing is stored; the
    substitution is announced in the log. Import errors are never suppressed."""
    import traceback

    from app.db_credentials import URL_ENV

    substituted = not (os.environ.get(URL_ENV) or "").strip()
    if substituted:
        logger.info("No database credential is configured; the import test uses an inert "
                    "placeholder URL. No database is contacted.")
        os.environ[URL_ENV] = SELFTEST_PLACEHOLDER_URL

    try:
        import app.main  # noqa: F401  (the full application graph)
        from app.services.matcher import MatchScorer  # noqa: F401
        from app.services.portals.dice_portal import DicePortalAdapter  # noqa: F401
        from app.services.linkedin import LinkedInAutomation  # noqa: F401
        from playwright.sync_api import sync_playwright  # noqa: F401
        import alembic  # noqa: F401
        import psycopg  # noqa: F401
    except Exception as error:
        return StartupProblem(
            ExitCode.STARTUP_FAILED,
            "Packaging problem",
            f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
        )
    finally:
        if substituted:
            os.environ.pop(URL_ENV, None)
    return None


# --------------------------------------------------------------------------------------
# PostgreSQL
# --------------------------------------------------------------------------------------

def ensure_postgres_running() -> StartupProblem | None:
    """Make sure PostgreSQL is available, starting its service if that is all it needs.

    Nothing is installed here -- that is the installer's job. The only action this takes is
    starting a service that PostgreSQL's own installer registered, and only after
    :mod:`app.service_control` has confirmed the name really is a PostgreSQL service."""
    from app.postgres_detection import detect
    from app.service_control import StartOutcome, start_detected_postgres

    detection = detect()

    if detection.probe.occupied_by_other:
        return StartupProblem(
            ExitCode.PORT_OCCUPIED_BY_OTHER,
            "Database port is in use",
            f"Port {detection.probe.port} is being used by a program that is not "
            "PostgreSQL.\n\nStop that program, or configure PostgreSQL to use a different "
            f"port, and start {APP_TITLE} again.",
        )

    if detection.probe.is_postgres:
        logger.info("PostgreSQL is already answering on port %s.", detection.probe.port)
        return None

    if not detection.installed:
        return StartupProblem(
            ExitCode.POSTGRES_NOT_FOUND,
            "Database is not installed",
            "PostgreSQL could not be found on this computer.\n\n"
            f"{APP_TITLE} needs PostgreSQL to store your resumes, matches and application "
            "history. Re-run the installer to add it.",
        )

    logger.info("PostgreSQL is installed but not answering; attempting to start its service.")
    result = start_detected_postgres(detection)
    logger.info("Service start outcome: %s (%s)", result.outcome.value, result.detail or "-")

    if result.ok and result.accepting_connections:
        return None

    if result.outcome is StartOutcome.PERMISSION_DENIED:
        return StartupProblem(
            ExitCode.SERVICE_START_FAILED,
            "Database service could not be started",
            "PostgreSQL is installed but not running, and this account is not permitted to "
            "start Windows services.\n\n"
            "Ask an administrator to start the PostgreSQL service, or start it yourself "
            "from Windows Services, and then start " + APP_TITLE + " again.",
        )
    if result.outcome is StartOutcome.TIMED_OUT:
        return StartupProblem(
            ExitCode.SERVICE_START_FAILED,
            "Database did not start in time",
            "The PostgreSQL service was asked to start but did not begin accepting "
            "connections.\n\nIt may still be starting. Wait a moment and start "
            f"{APP_TITLE} again, or check the PostgreSQL service in Windows Services.",
        )
    return StartupProblem(
        ExitCode.SERVICE_START_FAILED,
        "Database is not running",
        "PostgreSQL is installed but is not running, and it could not be started "
        "automatically.\n\nStart the PostgreSQL service in Windows Services, then start "
        f"{APP_TITLE} again.",
    )


def classify_startup_problem() -> StartupProblem | None:
    """Work out what -- if anything -- prevents the application from reaching its database.

    Ordered so the most fundamental cause is reported first: a missing credential is not
    reported as a connection failure, and a stopped server is not reported as a bad
    password."""
    from app.database import (
        DatabaseAuthenticationFailed,
        DatabaseCredentialsMissing,
        DatabaseUnavailable,
        check_connection,
    )

    try:
        check_connection()
        return None
    except DatabaseCredentialsMissing:
        return StartupProblem(
            ExitCode.CREDENTIALS_MISSING,
            "Database not set up yet",
            "No database credential is stored for your Windows account.\n\n"
            "Re-run the installer to finish setting up the database. Your existing data is "
            "not affected.",
        )
    except DatabaseAuthenticationFailed:
        return StartupProblem(
            ExitCode.AUTHENTICATION_FAILED,
            "Database sign-in failed",
            "PostgreSQL rejected the stored credential.\n\n"
            "This usually means the database password was changed outside the application. "
            "Re-run the installer to store a new one; your data is not affected.",
        )
    except DatabaseUnavailable:
        # Detection and service start already ran, so reaching here means it went away
        # between the two checks, or the host/port in the stored credential is wrong.
        return StartupProblem(
            ExitCode.POSTGRES_NOT_FOUND,
            "Database is not reachable",
            "The PostgreSQL server could not be reached.\n\n"
            "Check that the PostgreSQL service is running, then start "
            f"{APP_TITLE} again.",
        )


def run_migrations() -> StartupProblem | None:
    """``alembic upgrade head``, then confirm the schema really is what this build expects."""
    from app.config import get_settings
    from app.schema_verification import upgrade_to_head, verify_schema

    url = get_settings().database_url
    try:
        upgrade_to_head(url)
        logger.info("Database is at the latest revision.")
    except Exception as error:
        logger.error("Migration failed: %s", type(error).__name__, exc_info=True)
        return StartupProblem(
            ExitCode.MIGRATION_FAILED,
            "Database update failed",
            "The database could not be brought up to date.\n\n"
            f"Details: {type(error).__name__}\n\n"
            "Your existing data has not been changed.\n"
            f"See the log file:\n{log_file_reference()}",
        )

    try:
        schema = verify_schema(url)
    except Exception as error:
        logger.error("Schema verification could not run: %s", type(error).__name__,
                     exc_info=True)
        return StartupProblem(
            ExitCode.SCHEMA_VERIFICATION_FAILED,
            "Database check failed",
            "The database structure could not be checked.\n\n"
            f"Details: {type(error).__name__}\n\n"
            "Your existing data has not been changed.\n"
            f"See the log file:\n{log_file_reference()}",
        )

    if not schema.matches:
        logger.error("Schema does not match the baseline: %s", "; ".join(schema.problems[:8]))
        return StartupProblem(
            ExitCode.SCHEMA_VERIFICATION_FAILED,
            "Database structure is unexpected",
            "The database does not match what this version of the application expects.\n\n"
            "Your existing data has not been changed. Re-run the installer, or restore a "
            "backup.\n"
            f"See the log file:\n{log_file_reference()}",
        )

    logger.info("Schema verified at revision %s.", schema.alembic_revision)
    return None


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------

def _http_ok(url: str, headers: dict | None = None, timeout: float = 2.0) -> str | None:
    """Fetch a URL, returning its body on success and None on any failure."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            if 200 <= reply.status < 400:
                return reply.read(65536).decode("utf-8", errors="replace")
            return None
    except (urllib.error.URLError, OSError, ValueError):
        return None


def frontend_is_served() -> bool:
    """Does this run serve the React application itself?

    False in development, where Vite owns the UI -- readiness must not then wait for a shell
    that this process was never going to serve."""
    try:
        from app.config import get_settings

        return bool(get_settings().should_serve_frontend())
    except Exception:  # pragma: no cover - defensive
        return False


def wait_until_ready(port: int, timeout: float = READINESS_TIMEOUT_SECONDS,
                     require_shell: bool | None = None) -> bool:
    """Wait until the API answers AND the web interface is really being served.

    Checking ``/health`` alone is not enough: FastAPI answers it as soon as the process is
    up, while the React shell can still be missing or unreadable. Opening a browser at that
    moment shows the user an error page and makes a working installation look broken."""
    base = f"http://{HOST}:{port}"
    if require_shell is None:
        require_shell = frontend_is_served()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _http_ok(f"{base}/health") is not None:
            if not require_shell:
                return True
            # Same request a browser makes, so this exercises the real navigation path.
            shell = _http_ok(base, headers={"Accept": BROWSER_ACCEPT})
            if shell is not None and SHELL_MARKER in shell:
                return True
        time.sleep(0.4)
    return False


def serve(port: int, coordinator=None):
    """Start uvicorn in this process, on loopback only."""
    import uvicorn

    from app.main import app

    if coordinator is not None:
        from app.shutdown import install

        install(app, coordinator)

    config = uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    if coordinator is not None:
        def stop() -> None:
            server.should_exit = True

        coordinator.bind(stop)
    return server, thread


# --------------------------------------------------------------------------------------
# Already-running instance
# --------------------------------------------------------------------------------------

def open_existing_instance(show_window: bool = True) -> int | None:
    """A second launch: show the window that is already open, and exit quietly.

    Returns an exit code when an existing healthy instance was found, or None when the
    recorded state turned out to be stale -- in which case it is removed and this process
    continues starting normally.

    Deliberately NOT an error dialog: double-clicking the icon while the application is
    already running is completely reasonable, and the right response is to show it.

    ``show_window=False`` for the diagnostic modes: ``--check`` reporting that the
    application is already running must not fling a browser window at whoever ran it."""
    from app import runtime_state

    state = runtime_state.read_state()
    if state is None:
        return None

    if not runtime_state.state_is_live(state):
        logger.info("Found runtime state from a previous run that is no longer valid; "
                    "removing it and starting normally.")
        runtime_state.cleanup()
        return None

    logger.info("%s is already running (pid %s) at %s.", APP_TITLE, state.pid, state.url)
    if show_window:
        try:
            webbrowser.open(state.url)
        except Exception as error:  # pragma: no cover - not fatal here
            logger.warning("Could not open the existing window: %s", type(error).__name__)
    return int(ExitCode.ALREADY_RUNNING)


def request_running_instance_shutdown(timeout: float = 30.0) -> int:
    """Ask a running instance to stop. This is what the installer calls.

    Exit codes: SUCCESS when it stopped (or was not running at all), SHUTDOWN_FAILED when a
    live instance would not stop. Nothing is ever killed here."""
    from app import runtime_state
    from app.shutdown import request_shutdown
    from app.single_instance import process_is_running

    state = runtime_state.read_state()
    if state is None or not runtime_state.state_is_live(state):
        logger.info("No running instance was found; nothing to stop.")
        runtime_state.cleanup()
        print("SHUTDOWN: not running")
        return int(ExitCode.SUCCESS)

    token = runtime_state.read_token()
    if not token:
        logger.warning("A running instance was found but its shutdown token is unavailable.")
        print("SHUTDOWN: token unavailable")
        return int(ExitCode.SHUTDOWN_FAILED)

    if not request_shutdown(state.host, state.port, token):
        print("SHUTDOWN: request refused")
        return int(ExitCode.SHUTDOWN_FAILED)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_is_running(state.pid):
            logger.info("The running instance stopped cleanly.")
            print("SHUTDOWN: stopped")
            return int(ExitCode.SUCCESS)
        time.sleep(0.5)

    logger.warning("The running instance did not exit within %.0f seconds.", timeout)
    print("SHUTDOWN: timed out")
    return int(ExitCode.SHUTDOWN_FAILED)


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------

def _run_application(no_browser: bool, check_only: bool) -> int:
    """Everything after the single-instance decision. Assumes logging is running."""
    from app import runtime_state
    from app.shutdown import ShutdownCoordinator

    configure_playwright_browsers()

    problem = ensure_postgres_running()
    if problem is not None:
        return report(problem)

    problem = classify_startup_problem()
    if problem is not None:
        return report(problem)

    problem = run_migrations()
    if problem is not None:
        return report(problem)

    if check_only:
        logger.info("Startup checks passed.")
        return int(ExitCode.SUCCESS)

    if not port_is_free(PREFERRED_PORT):
        logger.info("Port %s is held by %s.", PREFERRED_PORT,
                    describe_port_conflict(PREFERRED_PORT))
    try:
        port = find_available_port()
    except RuntimeError as error:
        return report(StartupProblem(
            ExitCode.PORT_OCCUPIED_BY_OTHER, "No free port",
            f"{APP_TITLE} could not find a free network port on this computer.\n\n"
            f"{error}",
        ))
    if port != PREFERRED_PORT:
        logger.info("Using port %s instead of %s.", port, PREFERRED_PORT)

    # The token authorises the local shutdown endpoint for THIS run only.
    token = runtime_state.generate_token()
    runtime_state.write_token(token)
    coordinator = ShutdownCoordinator(token)

    state = runtime_state.publish(port, HOST, app_version())
    server, thread = serve(port, coordinator)

    try:
        if not wait_until_ready(port):
            server.should_exit = True
            return report(StartupProblem(
                ExitCode.STARTUP_FAILED,
                "Could not start",
                f"{APP_TITLE} started but its web interface did not become available.\n\n"
                f"See the log file:\n{log_file_reference()}",
            ))

        runtime_state.mark_ready(state)
        url = f"http://{HOST}:{port}"
        logger.info("Ready at %s", url)
        # Opened exactly once, only after the interface is genuinely serving pages.
        if not no_browser:
            webbrowser.open(url)

        try:
            while thread.is_alive() and not coordinator.requested:
                thread.join(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("Shutdown requested from the console.")
        return int(ExitCode.SUCCESS)
    finally:
        _shut_down(server, thread)


def surviving_threads() -> list[str]:
    """Non-daemon threads still alive. These are what would keep the process running."""
    current = threading.current_thread()
    return sorted(
        t.name for t in threading.enumerate()
        if t is not current and t.is_alive() and not t.daemon
    )


def _shut_down(server, thread) -> None:
    """Stop cleanly: server first, then the browsers we own, then our published state."""
    from app import runtime_state

    logger.info("Stopping.")
    try:
        server.should_exit = True
        thread.join(timeout=20)
    except Exception:  # pragma: no cover - defensive
        logger.debug("The server thread did not join cleanly.", exc_info=True)

    try:
        from app.browser_lifecycle import close_owned_browsers

        closed = close_owned_browsers()
        if closed:
            logger.info("Closed %d browser process(es) started by this application.",
                        len(closed))
    except Exception:  # pragma: no cover - cleanup must never mask the exit
        logger.debug("Browser cleanup did not complete.", exc_info=True)

    runtime_state.cleanup()

    # Recorded rather than assumed: the web stack leaves worker threads behind (anyio's
    # thread pool among them), and knowing which ones is what makes a future hang
    # diagnosable instead of mysterious.
    remaining = surviving_threads()
    if remaining:
        logger.info("Threads still alive after cleanup: %s", ", ".join(remaining))
    logger.info("Stopped.")


def _exit_now(code: int) -> None:
    """End the process, immediately and for certain.

    Returning from main() is not enough. The interpreter then waits for every non-daemon
    thread, and the web stack leaves some behind -- so the process lingered for minutes after
    it had finished shutting down. In a WINDOWED build that is a genuine problem rather than
    an untidiness: the process has no window, so the user cannot see or close it, and while
    it lives it holds the single-instance mutex and the application will not restart.

    Everything that needs to happen has already happened by this point -- the server is
    stopped, owned browsers are closed, runtime state and token are removed -- so the only
    thing left to protect is the log, which is flushed and closed first."""
    logging.shutdown()
    os._exit(code)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_browser = "--no-browser" in argv
    check_only = "--check" in argv
    selftest_only = "--selftest" in argv
    browser_check_only = "--browser-check" in argv
    installer_mode = "--installer" in argv
    shutdown_mode = "--shutdown" in argv
    diagnostic_console = "--diagnostic-console" in argv

    non_interactive = (check_only or selftest_only or browser_check_only
                       or installer_mode or shutdown_mode)
    set_interactive(not non_interactive)

    # FIRST, before anything that can fail. A windowed build has no console, so a failure
    # from here on is only ever visible because this line already ran.
    configure_logging(stream=diagnostic_console or not is_windowed())

    from app import bootstrap_log

    bootstrap_log.record_environment(app_version(), stage=f"{APP_TITLE} starting")

    # The installer calls into the PACKAGED application rather than a development virtual
    # environment. Handled first, and outside the single-instance guard: these actions run
    # while no server is running, and one of them may run twice during an installation.
    if installer_mode:
        from app.installer_bridge import main as installer_main

        index = argv.index("--installer")
        return installer_main(argv[index + 1:])

    if shutdown_mode:
        return request_running_instance_shutdown()

    if selftest_only:
        logger.info("Running the packaging self-test.")
        problem = selftest()
        if problem is not None:
            logger.error("Self-test failed:\n%s", problem.message)
            show_error_dialog(problem)
            return int(problem.code)
        logger.info("Self-test passed: every packaged module imported.")
        print("SELFTEST OK")
        return int(ExitCode.SUCCESS)

    # Before the single-instance guard on purpose: this is a diagnostic that needs neither
    # the database nor the web server, and it must be runnable while the application runs.
    if browser_check_only:
        logger.info("Launching the bundled Chromium (blank page only).")
        problem = browser_check()
        if problem is not None:
            logger.error("Bundled browser check failed:\n%s", problem.message)
            show_error_dialog(problem)
            return int(problem.code)
        logger.info("Bundled browser check passed.")
        return int(ExitCode.SUCCESS)

    from app.paths import runtime_paths
    from app.single_instance import create_guard

    guard = create_guard(runtime_paths().ensure().work / LOCK_FILENAME)
    if not guard.acquire():
        existing = open_existing_instance(show_window=not (non_interactive or no_browser))
        if existing is not None:
            return existing
        # The mutex is held but no usable state was published -- an instance that is still
        # starting, or one that is wedged. Say so rather than starting a competing server.
        return report(StartupProblem(
            ExitCode.ALREADY_RUNNING,
            "Already running",
            f"{APP_TITLE} is already running on this computer.\n\n"
            "Look for it in your web browser, or close it and try again.",
        ))

    if guard.recovered_abandoned:
        logger.info("Recovered from a previous unclean shutdown.")

    try:
        return _run_application(no_browser, check_only)
    except Exception as error:  # pragma: no cover - last-resort guard
        logger.exception("Unexpected startup failure")
        return report(StartupProblem(
            ExitCode.STARTUP_FAILED,
            "Unexpected error",
            f"{APP_TITLE} could not start because of an unexpected problem.\n\n"
            f"Details: {type(error).__name__}\n\n"
            f"See the log file:\n{log_file_reference()}",
        ))
    finally:
        guard.release()


def is_windowed() -> bool:
    """True in a GUI build, where there is no console to write to."""
    return bool(getattr(sys, "frozen", False)) and getattr(sys, "stdout", None) is None


if __name__ == "__main__":  # pragma: no cover - packaged entry point
    # _exit_now rather than sys.exit: see its docstring. A windowed build that lingers is
    # invisible to the user and blocks the next launch.
    _exit_now(main())
