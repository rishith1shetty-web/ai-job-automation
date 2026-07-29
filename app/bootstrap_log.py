"""Logging that is running before anything can go wrong.

A packaged GUI application has no console. If it fails while importing SciPy, Playwright or
SQLAlchemy, there is no stderr to print to and no window to show -- the process simply
disappears. The only way the failure survives is if a log file was already open when it
happened.

So this module is deliberately **standard library only**. It imports ``logging``, ``os``,
``sys`` and ``pathlib`` and nothing else -- no application module, no third-party package. It
can therefore run as the very first statement of the program, before any import that could
fail. ``app.paths`` is imported lazily *inside* a function so that even a fault in the path
resolver is caught and reported rather than crashing before logging exists.

Two failures this was written for, both of which really happened:

* a ``ModuleNotFoundError`` deep inside SciPy, which produced a dialog naming a log file that
  had never been created;
* Alembic's ``fileConfig`` replacing the root handlers mid-startup, which silently truncated
  the log at the migration step.

Nothing here is allowed to raise. If the log file cannot be opened, logging degrades to
memory and :func:`log_file_status` says so honestly, so a dialog can avoid pointing the user
at a file that does not exist.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

LOG_FILENAME = "launcher.log"
# Small enough to open in Notepad, large enough to hold a full startup with tracebacks.
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3
LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

# Set once by configure(); read by the dialog code so it never names a file that is not there.
_log_file: Path | None = None
_file_logging_failed: str = ""


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------

# Applied to every record, so a secret cannot reach the log by being interpolated into a
# message somewhere far from here. Deliberately conservative patterns: each one matches a
# shape that is only ever a credential.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # postgresql://user:secret@host -> postgresql://user:***@host
    (re.compile(r"(?i)\b((?:postgres|postgresql|mysql|mongodb)(?:\+\w+)?://[^\s:/@]+:)[^\s@]+@"),
     r"\1***@"),
    # password=..., pwd=..., PGPASSWORD=...
    (re.compile(r"(?i)\b(pg)?(password|passwd|pwd)\s*[=:]\s*\S+"), r"\1\2=***"),
    # Authorization headers carry a scheme AND a value ("Bearer eyJ..."), so the whole
    # remainder of the line goes -- matching only the next word would leave the token behind.
    (re.compile(r"(?i)\bauthorization\s*[=:]\s*.+"), "authorization=***"),
    # api-key / token / secret style assignments.
    (re.compile(r"(?i)\b(api[_-]?key|token|secret)\s*[=:]\s*\S+"), r"\1=***"),
    (re.compile(r"\bsk-[A-Za-z0-9]{16,}"), "sk-***"),
    # Cookie/session values.
    (re.compile(r"(?i)\b(cookie|session[_-]?id|li_at|jsessionid)\s*[=:]\s*\S+"), r"\1=***"),
)


class RedactingFilter(logging.Filter):
    """Strips credential-shaped text from a record's message before it is formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Render now so arguments are covered too, then clear them: the message has
            # already been substituted, and leaving args would re-interpolate at format time.
            message = record.getMessage()
            cleaned = redact(message)
            if cleaned != message:
                record.msg = cleaned
                record.args = ()
        except Exception:  # a logging filter must never break logging
            pass
        return True


class RedactingFormatter(logging.Formatter):
    """Redacts the FINAL formatted line, exception traceback included.

    A filter alone is not enough, and that is easy to get wrong: ``record.exc_text`` is
    produced by the formatter, so at filter time it is still ``None``. A traceback carrying a
    connection URL would sail straight past a filter-only implementation -- which is exactly
    what a database failure produces. Redacting here catches the message, the arguments, the
    exception and any stack info, because all of them are already in the string."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            return redact(super().format(record))
        except Exception:  # pragma: no cover - logging must never break the application
            return super().format(record)


def redact(text: str) -> str:
    """Remove credential-shaped substrings. Safe on any input, never raises."""
    if not text:
        return text
    try:
        for pattern, replacement in _REDACTIONS:
            text = pattern.sub(replacement, text)
    except Exception:  # pragma: no cover - defensive
        return "<redaction failed>"
    return text


# --------------------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------------------

def _log_directory() -> Path:
    """The per-user log directory, resolved without importing anything risky.

    ``app.paths`` is the authority and is imported here rather than at module scope, so a
    failure inside it becomes a reported fallback instead of an invisible crash."""
    try:
        from app.paths import runtime_paths

        return runtime_paths().ensure().logs
    except Exception:
        # Mirror app/paths.py's own rule: machine-local only, never roaming.
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        fallback = Path(base) / "AIJobAutomation" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def configure(level: int = logging.INFO, stream: bool = True) -> Path | None:
    """Start logging. Call this FIRST, before importing anything that can fail.

    Returns the log file, or None when file logging could not be started -- in which case
    :func:`log_file_status` explains why and the caller must not name a file to the user."""
    global _log_file, _file_logging_failed

    root = logging.getLogger()
    root.setLevel(level)
    for existing in list(root.handlers):
        root.removeHandler(existing)

    redactor = RedactingFilter()
    # Both layers: the filter cleans the message, the formatter cleans the finished line
    # including any traceback. Either alone leaves a gap.
    formatter = RedactingFormatter(LOG_FORMAT)

    try:
        directory = _log_directory()
        target = directory / LOG_FILENAME
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8",
            delay=False,
        )
        handler.setFormatter(formatter)
        handler.addFilter(redactor)
        root.addHandler(handler)
        _log_file = target
        _file_logging_failed = ""
    except Exception as error:
        _log_file = None
        _file_logging_failed = f"{type(error).__name__}: {error}"

    # A windowed build has no stdout at all; sys.stdout is None there, and writing to it
    # would raise. Only add a stream handler when there is genuinely somewhere to write.
    if stream and getattr(sys, "stdout", None) is not None:
        try:
            console = logging.StreamHandler(sys.stdout)
            console.setFormatter(formatter)
            console.addFilter(redactor)
            root.addHandler(console)
        except Exception:  # pragma: no cover - defensive
            pass

    if not root.handlers:
        # Never leave logging completely unconfigured: a NullHandler keeps callers safe.
        root.addHandler(logging.NullHandler())

    return _log_file


def log_file() -> Path | None:
    return _log_file


def log_file_exists() -> bool:
    """True only when there really is a file to point the user at."""
    return _log_file is not None and _log_file.is_file()


def log_file_status() -> str:
    """A short, honest description for a dialog. Never invents a path."""
    if log_file_exists():
        return str(_log_file)
    if _file_logging_failed:
        return f"(no log file could be created: {_file_logging_failed})"
    return "(no log file was created)"


def record_environment(version: str = "", stage: str = "startup") -> None:
    """The facts that make a log from a stranger's computer diagnosable.

    No credential, no URL, no user data -- executable, packaging mode, version, PID and
    where the log itself is."""
    logger = logging.getLogger("bootstrap")
    logger.info("---- %s ----", stage)
    logger.info("Application : %s", version or "(version unknown)")
    logger.info("Executable  : %s", getattr(sys, "executable", "?"))
    logger.info("Frozen      : %s", bool(getattr(sys, "frozen", False)))
    logger.info("Process ID  : %s", os.getpid())
    logger.info("Python      : %s", sys.version.split()[0])
    logger.info("Log file    : %s", log_file_status())


def install_excepthook() -> None:
    """Route anything that escapes to the log.

    In a windowed build an unhandled exception would otherwise vanish: there is no console
    for Python's default handler to print to."""
    previous = sys.excepthook

    def handler(exc_type, exc_value, exc_traceback):
        try:
            logging.getLogger("bootstrap").critical(
                "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback))
        except Exception:  # pragma: no cover - defensive
            pass
        previous(exc_type, exc_value, exc_traceback)

    sys.excepthook = handler
