"""The entry point of the packaged Windows application.

Replaces start_app.ps1 for an installed computer. ``start_app.ps1`` remains the DEVELOPMENT
launcher and is unchanged.

What it does, in order:

1. take a single-instance lock -- a second double-click focuses the running app instead of
   starting a competing server on another port;
2. point Playwright at the BUNDLED Chromium, never the developer's global cache;
3. resolve the database credential (secure store, or DATABASE_URL in development);
4. classify what is wrong when that fails -- PostgreSQL missing, stopped, wrong password,
   no stored credential -- because each needs different instructions;
5. run ``alembic upgrade head``;
6. start FastAPI on 127.0.0.1, on 8000 or the next free port;
7. wait for the web interface to actually answer, then open the browser;
8. shut the server down cleanly and release the lock.

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

from app.db_credentials import redact_secrets, redact_url

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


class ExitCode(IntEnum):
    SUCCESS = 0
    ALREADY_RUNNING = 3
    POSTGRES_NOT_FOUND = 10
    PORT_OCCUPIED_BY_OTHER = 11
    CREDENTIALS_MISSING = 17
    AUTHENTICATION_FAILED = 18
    MIGRATION_FAILED = 14
    STARTUP_FAILED = 19


@dataclass
class StartupProblem:
    """A failure translated into something a person can act on."""

    code: ExitCode
    title: str
    message: str

    def as_dialog(self) -> str:
        return f"{self.title}\n\n{self.message}"


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
            ExitCode.STARTUP_FAILED,
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
                    ExitCode.STARTUP_FAILED,
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
            ExitCode.STARTUP_FAILED,
            "Browser did not start",
            f"{type(error).__name__}: {error}\n\n{traceback.format_exc()}",
        )

    print("BROWSER OK")
    return None


# --------------------------------------------------------------------------------------
# Single instance
# --------------------------------------------------------------------------------------

class SingleInstanceLock:
    """An exclusive lock file under the per-user data directory.

    Uses ``O_EXCL`` creation plus a liveness check, so a crashed previous run cannot lock
    the user out permanently."""

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
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, recorded)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            # Cannot tell; assume stale rather than blocking the user forever.
            return False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self._holder_is_alive():
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

    def __enter__(self) -> "SingleInstanceLock":
        return self

    def __exit__(self, *_) -> None:
        self.release()


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


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def configure_logging() -> Path:
    """Log to %LOCALAPPDATA%\\AIJobAutomation\\logs. Never into the program folder."""
    from app.paths import runtime_paths

    paths = runtime_paths().ensure()
    log_file = paths.logs / LOG_FILENAME
    handlers: list[logging.Handler] = [logging.FileHandler(log_file, encoding="utf-8")]
    if sys.stdout is not None:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_file


# --------------------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------------------

# Set for verification runs (--check / --selftest). A modal message box would block an
# automated build check forever, so those modes report to stderr instead. The normal GUI
# launch is unaffected and still shows dialogs.
_interactive = True


def set_interactive(value: bool) -> None:
    global _interactive
    _interactive = value


def show_error_dialog(problem: StartupProblem, secrets: tuple[str, ...] = ()) -> None:
    """A Windows message box. Falls back to stderr when no GUI is available, and is
    deliberately suppressed in non-interactive verification modes."""
    text = redact_secrets(problem.as_dialog(), *secrets)
    if not _interactive:
        print(text, file=sys.stderr)
        return
    try:
        import ctypes

        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, f"{APP_TITLE} - {problem.title}",
                                         0x00 | 0x10 | 0x10000)
    except Exception:
        print(text, file=sys.stderr)


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
# Startup checks
# --------------------------------------------------------------------------------------

def classify_startup_problem() -> StartupProblem | None:
    """Work out what -- if anything -- prevents the application from starting.

    Ordered so the most fundamental cause is reported first: a missing credential is not
    reported as a connection failure, and a stopped server is not reported as a bad
    password."""
    from app.database import (
        DatabaseAuthenticationFailed,
        DatabaseCredentialsMissing,
        DatabaseUnavailable,
        check_connection,
    )
    from app.postgres_detection import detect

    try:
        check_connection()
        return None
    except DatabaseCredentialsMissing:
        return StartupProblem(
            ExitCode.CREDENTIALS_MISSING,
            "Database not set up yet",
            "No database credential is stored on this computer.\n\n"
            "Run the setup step to create the database and store its credential:\n"
            "    scripts\\provision_postgres.ps1",
        )
    except DatabaseAuthenticationFailed:
        return StartupProblem(
            ExitCode.AUTHENTICATION_FAILED,
            "Database sign-in failed",
            "PostgreSQL rejected the stored credential.\n\n"
            "This usually means the database password was changed outside the "
            "application. Re-run the setup step to store a new one; your data is not "
            "affected.",
        )
    except DatabaseUnavailable:
        report = detect()
        if report.probe.occupied_by_other:
            return StartupProblem(
                ExitCode.PORT_OCCUPIED_BY_OTHER,
                "Database port is in use",
                f"Port {report.probe.port} is being used by a program that is not "
                "PostgreSQL. Stop that program, or configure PostgreSQL on another port.",
            )
        if report.installed:
            return StartupProblem(
                ExitCode.POSTGRES_NOT_FOUND,
                "Database is not running",
                "PostgreSQL is installed but not running.\n\n"
                "Start the PostgreSQL service in Windows Services, then start "
                f"{APP_TITLE} again.",
            )
        return StartupProblem(
            ExitCode.POSTGRES_NOT_FOUND,
            "Database is not installed",
            "PostgreSQL could not be found on this computer.\n\n"
            f"{APP_TITLE} needs PostgreSQL to store your resumes, matches and "
            "application history.",
        )


def run_migrations() -> StartupProblem | None:
    """``alembic upgrade head`` against the resolved database."""
    from app.config import get_settings
    from app.schema_verification import upgrade_to_head

    url = get_settings().database_url
    try:
        upgrade_to_head(url)
        logger.info("Database is at the latest revision.")
        return None
    except Exception as error:
        logger.error("Migration failed: %s", redact_secrets(str(error), _password_of(url)))
        return StartupProblem(
            ExitCode.MIGRATION_FAILED,
            "Database update failed",
            "The database could not be brought up to date.\n\n"
            f"Details: {type(error).__name__}\n\n"
            "Your existing data has not been changed. See the log file for more.",
        )


def _password_of(url: str) -> str:
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).password or ""
    except Exception:
        return ""


# --------------------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------------------

def wait_until_ready(port: int, timeout: float = READINESS_TIMEOUT_SECONDS) -> bool:
    """Poll /health until the API answers. The browser opens only after this succeeds."""
    import urllib.error
    import urllib.request

    deadline = time.monotonic() + timeout
    url = f"http://{HOST}:{port}/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.4)
    return False


def serve(port: int):
    """Start uvicorn in this process, on loopback only."""
    import uvicorn

    from app.main import app

    config = uvicorn.Config(app, host=HOST, port=port, log_level="info", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server, thread


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_browser = "--no-browser" in argv
    check_only = "--check" in argv
    selftest_only = "--selftest" in argv
    browser_check_only = "--browser-check" in argv
    # Verification modes are driven by build scripts, so they must never block on a dialog.
    set_interactive(not (check_only or selftest_only or browser_check_only))

    log_file = configure_logging()

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

    # Before the single-instance lock on purpose: this is a diagnostic that needs neither the
    # database nor the web server, and it must be runnable while the application is running.
    if browser_check_only:
        logger.info("Launching the bundled Chromium (blank page only).")
        problem = browser_check()
        if problem is not None:
            logger.error("Bundled browser check failed:\n%s", problem.message)
            show_error_dialog(problem)
            return int(problem.code)
        logger.info("Bundled browser check passed.")
        return int(ExitCode.SUCCESS)
    logger.info("%s starting (packaged=%s)", APP_TITLE, getattr(sys, "frozen", False))
    logger.info("Log file: %s", log_file)

    from app.paths import runtime_paths

    lock = SingleInstanceLock(runtime_paths().work / LOCK_FILENAME)
    if not lock.acquire():
        show_error_dialog(StartupProblem(
            ExitCode.ALREADY_RUNNING,
            "Already running",
            f"{APP_TITLE} is already running on this computer.\n\n"
            "Look for it in your web browser, or close it and try again.",
        ))
        return int(ExitCode.ALREADY_RUNNING)

    try:
        configure_playwright_browsers()

        problem = classify_startup_problem()
        if problem is not None:
            logger.error("%s: %s", problem.title, problem.message.splitlines()[0])
            show_error_dialog(problem)
            return int(problem.code)

        problem = run_migrations()
        if problem is not None:
            show_error_dialog(problem)
            return int(problem.code)

        if check_only:
            logger.info("Startup checks passed.")
            return int(ExitCode.SUCCESS)

        try:
            port = find_available_port()
        except RuntimeError as error:
            show_error_dialog(StartupProblem(
                ExitCode.PORT_OCCUPIED_BY_OTHER, "No free port", str(error)
            ))
            return int(ExitCode.PORT_OCCUPIED_BY_OTHER)
        if port != PREFERRED_PORT:
            logger.info("Port %s was busy; using %s instead.", PREFERRED_PORT, port)

        server, thread = serve(port)
        if not wait_until_ready(port):
            server.should_exit = True
            show_error_dialog(StartupProblem(
                ExitCode.STARTUP_FAILED,
                "Could not start",
                f"{APP_TITLE} did not finish starting.\n\n"
                f"See the log file for details:\n{log_file}",
            ))
            return int(ExitCode.STARTUP_FAILED)

        url = f"http://{HOST}:{port}"
        logger.info("Ready at %s", url)
        if not no_browser:
            webbrowser.open(url)

        try:
            while thread.is_alive():
                thread.join(timeout=1.0)
        except KeyboardInterrupt:
            logger.info("Shutdown requested.")
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            logger.info("Stopped.")
        return int(ExitCode.SUCCESS)
    except Exception as error:  # pragma: no cover - last-resort guard
        logger.exception("Unexpected startup failure")
        show_error_dialog(StartupProblem(
            ExitCode.STARTUP_FAILED,
            "Unexpected error",
            f"{type(error).__name__}\n\nSee the log file:\n{log_file}",
        ))
        return int(ExitCode.STARTUP_FAILED)
    finally:
        lock.release()


if __name__ == "__main__":  # pragma: no cover - packaged entry point
    sys.exit(main())
