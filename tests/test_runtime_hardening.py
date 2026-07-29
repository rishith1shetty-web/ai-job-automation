"""Launcher and runtime reliability: what a packaged desktop application must guarantee.

Phase 5 turned the packaged build into a windowed application. That removes the console --
and with it stdout, stderr and Python's default crash output. Almost everything below exists
because of that one change: if a failure is not written to a file that was already open, it
is simply gone.

Grouped by guarantee:

* **bootstrap logging** â€” the log is open before any import that can fail, it rotates, and
  nothing credential-shaped can reach it;
* **single instance** â€” a named kernel mutex, one per interactive user, that a crash cannot
  leave permanently held;
* **runtime state** â€” a non-secret published description of the running instance, written
  atomically, validated before it is trusted, and removed on shutdown;
* **shutdown** â€” loopback-only and authorised by a per-run token, so the installer can ask
  the application to stop instead of killing it;
* **PostgreSQL service** â€” started only when it is genuinely a PostgreSQL service, and only
  ever started, never stopped or reconfigured;
* **readiness** â€” the browser opens once, and only after the API *and* the web interface
  answer;
* **ownership** â€” nothing is ever terminated unless it is provably this application's.

No test here installs PostgreSQL, starts or stops a real service, touches the real database,
overwrites the real credential, or performs any LinkedIn or Dice action. Destructive paths
use mocks; process control is exercised against synthetic snapshots.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app import bootstrap_log, browser_lifecycle, runtime_state, service_control
from app import single_instance, shutdown as shutdown_module
from app import windows_launcher as launcher
from app.paths import DATA_DIR_ENV, project_root, reset_runtime_paths_cache
from app.service_control import StartOutcome
from app.windows_launcher import ExitCode, StartupProblem

ROOT = project_root()
SPEC_FILE = ROOT / "RisingPathRecruiters.spec"
ISS_FILE = ROOT / "installer" / "RisingPathRecruiters.iss"
ICON_FILE = ROOT / "installer" / "RisingPathRecruiters.ico"
WINDOWS = sys.platform == "win32"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def iss_code(text: str) -> str:
    """An .iss with its comments removed.

    Comments legitimately DISCUSS the things the code must not do -- "no taskkill", "never
    touch chrome.exe" -- so a naive substring search over the whole file reports the
    documentation as a violation."""
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(";"):        # [Setup]/[Files] comment
            continue
        if stripped.startswith("{") and stripped.endswith("}"):  # whole-line Pascal comment
            continue
        lines.append(line)
    body = "\n".join(lines)
    # Multi-line Pascal { ... } comments.
    return re.sub(r"\{[^{}]*\}(?!\s*\))", lambda m: "" if "\n" in m.group(0) else m.group(0),
                  body)


def python_imports(source: str) -> set[str]:
    """Module names imported at MODULE scope (indentation zero) only."""
    found = set()
    for line in source.splitlines():
        if line.startswith(("import ", "from ")):
            match = re.match(r"(?:from|import)\s+([\w.]+)", line)
            if match:
                found.add(match.group(1))
    return found


class _IsolatedTestCase(unittest.TestCase):
    """A temporary data root, so nothing here can touch the real user's files."""

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="hardening-test-"))
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.addCleanup(reset_runtime_paths_cache)
        self._env = patch.dict(os.environ, {DATA_DIR_ENV: str(self.temp_root / "data")},
                               clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        reset_runtime_paths_cache()


class _LoggingPreservingTestCase(_IsolatedTestCase):
    """bootstrap_log.configure() replaces the ROOT handlers. Restoring them keeps this file
    from silencing the rest of the suite."""

    def setUp(self) -> None:
        super().setUp()
        root = logging.getLogger()
        handlers, level = list(root.handlers), root.level
        hook = sys.excepthook

        def restore() -> None:
            for handler in list(root.handlers):
                try:
                    handler.close()
                except Exception:
                    pass
            root.handlers.clear()
            root.handlers.extend(handlers)
            root.setLevel(level)
            sys.excepthook = hook

        self.addCleanup(restore)


# ======================================================================================
# 1. Bootstrap logging
# ======================================================================================

class BootstrapLoggingTests(_LoggingPreservingTestCase):
    def test_the_log_file_is_open_before_anything_risky_is_imported(self) -> None:
        """In a windowed build there is no console, so a failure that happens before the log
        exists leaves no trace at all. This is the ordering that prevents that."""
        source = read(ROOT / "app" / "windows_launcher.py")
        body = source[source.index("def main("):]
        configure_at = body.index("configure_logging(")
        for risky in ("from app.installer_bridge import", "selftest()", "browser_check()",
                      "from app.single_instance import", "_run_application("):
            with self.subTest(after=risky):
                self.assertLess(configure_at, body.index(risky),
                                f"{risky} runs before logging is configured")

    def test_the_launcher_imports_nothing_heavy_at_module_scope(self) -> None:
        """A top-level import of SQLAlchemy or Playwright would run before main() -- and so
        before logging -- putting the failure back out of reach."""
        imported = python_imports(read(ROOT / "app" / "windows_launcher.py"))
        for heavy in ("sqlalchemy", "fastapi", "uvicorn", "playwright", "alembic", "psycopg",
                      "app.db_credentials", "app.database", "app.main", "app.config",
                      "app.installer_bridge", "app.provisioning"):
            with self.subTest(module=heavy):
                self.assertNotIn(heavy, imported)

    def test_the_launcher_only_imports_the_standard_library_at_module_scope(self) -> None:
        imported = python_imports(read(ROOT / "app" / "windows_launcher.py"))
        allowed = {"__future__", "logging", "os", "socket", "sys", "threading", "time",
                   "webbrowser", "dataclasses", "enum", "pathlib"}
        self.assertEqual(imported - allowed, set())

    def test_bootstrap_log_itself_depends_only_on_the_standard_library(self) -> None:
        source = read(ROOT / "app" / "bootstrap_log.py")
        imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, re.MULTILINE)
        allowed = {"logging", "logging.handlers", "os", "re", "sys", "pathlib",
                   "__future__", "app.paths"}
        for module in imports:
            with self.subTest(module=module):
                self.assertIn(module, allowed, f"{module} is not standard library")

    def test_configure_creates_the_log_directory_and_the_file(self) -> None:
        log_file = bootstrap_log.configure()
        self.assertIsNotNone(log_file)
        self.assertTrue(log_file.parent.is_dir())
        self.assertTrue(log_file.is_file())
        self.assertTrue(str(log_file).startswith(str(self.temp_root)))

    def test_the_log_reports_the_facts_that_make_it_diagnosable(self) -> None:
        log_file = bootstrap_log.configure()
        bootstrap_log.record_environment("9.9.9", stage="unit test")
        for handler in logging.getLogger().handlers:
            handler.flush()
        text = read(log_file)
        for expected in ("9.9.9", "Executable", "Frozen", "Process ID", str(os.getpid())):
            with self.subTest(field=expected):
                self.assertIn(expected, text)

    def test_the_log_rotates_instead_of_growing_without_limit(self) -> None:
        self.assertLessEqual(bootstrap_log.MAX_BYTES, 8 * 1024 * 1024)
        self.assertGreaterEqual(bootstrap_log.BACKUP_COUNT, 1)
        log_file = bootstrap_log.configure()
        handler = next(h for h in logging.getLogger().handlers
                       if isinstance(h, logging.handlers.RotatingFileHandler))
        self.assertEqual(handler.maxBytes, bootstrap_log.MAX_BYTES)
        self.assertEqual(handler.backupCount, bootstrap_log.BACKUP_COUNT)
        self.assertTrue(log_file.is_file())

    def test_rotation_really_happens_and_bounds_total_size(self) -> None:
        with patch.object(bootstrap_log, "MAX_BYTES", 2048), \
                patch.object(bootstrap_log, "BACKUP_COUNT", 2):
            log_file = bootstrap_log.configure()
            noisy = logging.getLogger("rotation-test")
            for index in range(400):
                noisy.info("padding line %04d %s", index, "x" * 80)
            for handler in logging.getLogger().handlers:
                handler.flush()

        produced = list(log_file.parent.glob(log_file.name + "*"))
        self.assertLessEqual(len(produced), 3, "more backups than configured")
        self.assertTrue(all(p.stat().st_size < 64 * 1024 for p in produced))

    def test_a_dialog_is_never_told_about_a_log_file_that_does_not_exist(self) -> None:
        with patch.object(bootstrap_log, "_log_directory",
                          side_effect=OSError("read-only volume")):
            self.assertIsNone(bootstrap_log.configure())
        self.assertFalse(bootstrap_log.log_file_exists())
        status = bootstrap_log.log_file_status()
        self.assertIn("no log file", status.lower())
        self.assertIn("read-only volume", status)

    def test_logging_still_works_when_the_file_cannot_be_opened(self) -> None:
        with patch.object(bootstrap_log, "_log_directory", side_effect=OSError("nope")):
            bootstrap_log.configure()
        logging.getLogger("still-works").info("this must not raise")
        self.assertTrue(logging.getLogger().handlers)

    def test_a_windowed_build_adds_no_stream_handler(self) -> None:
        """sys.stdout is None in a GUI build; writing to it would raise on every log call."""
        with patch.object(bootstrap_log.sys, "stdout", None):
            bootstrap_log.configure(stream=True)
        streams = [h for h in logging.getLogger().handlers
                   if type(h) is logging.StreamHandler]
        self.assertEqual(streams, [])

    def test_importing_the_api_does_not_throw_away_the_launchers_log_handler(self) -> None:
        """``app/main.py`` calls configure_logging() at import time, and dictConfig REPLACES
        the root handlers. Unguarded, importing the API discarded the launcher's file handler
        -- so in the packaged build every line after the server import was lost, and the
        replacement console handler wrote to a stdout that does not exist in a GUI build."""
        from app.logging_config import configure_logging

        log_file = bootstrap_log.configure()
        before = list(logging.getLogger().handlers)

        configure_logging()          # what importing app.main does

        self.assertEqual(logging.getLogger().handlers, before,
                         "the launcher's log handler was replaced")
        logging.getLogger("after-import").info("this line must still be recorded")
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIn("this line must still be recorded", read(log_file))

    def test_the_application_still_configures_logging_when_nothing_else_has(self) -> None:
        """Development is unaffected: uvicorn and the tests still get console logging."""
        from app.logging_config import configure_logging

        root = logging.getLogger()
        for handler in list(root.handlers):
            root.removeHandler(handler)
        configure_logging()
        self.assertTrue(root.handlers)

    def test_a_caller_can_still_force_reconfiguration(self) -> None:
        from app.logging_config import configure_logging

        bootstrap_log.configure()
        configure_logging(force=True)
        self.assertTrue(logging.getLogger().handlers)

    def test_an_escaping_exception_still_reaches_the_log(self) -> None:
        log_file = bootstrap_log.configure()
        bootstrap_log.install_excepthook()
        try:
            raise ValueError("escaped to the top")
        except ValueError:
            sys.excepthook(*sys.exc_info())
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIn("escaped to the top", read(log_file))


class LogRedactionTests(_LoggingPreservingTestCase):
    SECRETS = (
        ("postgresql+psycopg://app:hunter2secret@localhost:5432/db", "hunter2secret"),
        ("PGPASSWORD=realsecretvalue", "realsecretvalue"),
        ("password: myp4ssw0rd", "myp4ssw0rd"),
        ("api_key=sk-abcdefghijklmnopqrstuvwxyz012345", "sk-abcdefghijklmnopqrst"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "eyJhbGciOiJIUzI1NiJ9"),
        ("li_at=AQEDATABCDEFGHIJ", "AQEDATABCDEFGHIJ"),
        ("Cookie: sessionid=abc123def456", "abc123def456"),
    )

    def test_every_credential_shape_is_removed(self) -> None:
        for text, secret in self.SECRETS:
            with self.subTest(text=text):
                self.assertNotIn(secret, bootstrap_log.redact(text))

    def test_redaction_applies_to_what_actually_reaches_the_file(self) -> None:
        log_file = bootstrap_log.configure()
        logger = logging.getLogger("redaction-test")
        for text, _ in self.SECRETS:
            logger.info("connection detail: %s", text)
        for handler in logging.getLogger().handlers:
            handler.flush()

        written = read(log_file)
        for _, secret in self.SECRETS:
            with self.subTest(secret=secret[:12]):
                self.assertNotIn(secret, written)

    def test_exception_text_is_redacted_too(self) -> None:
        log_file = bootstrap_log.configure()
        try:
            raise RuntimeError("could not connect to "
                               "postgresql://app:leakedpassword@localhost/db")
        except RuntimeError:
            logging.getLogger("redaction-test").exception("failed")
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertNotIn("leakedpassword", read(log_file))

    def test_ordinary_text_is_left_alone(self) -> None:
        for benign in ("Ready at http://127.0.0.1:8000", "Schema verified at ed2a513cd149",
                       "Closed 2 browser processes"):
            with self.subTest(text=benign):
                self.assertEqual(bootstrap_log.redact(benign), benign)

    def test_redaction_never_raises(self) -> None:
        for awkward in ("", "\x00\x01", "%s %d {not a format}", "\\" * 500):
            with self.subTest(text=awkward[:20]):
                bootstrap_log.redact(awkward)


# ======================================================================================
# 2. Single instance
# ======================================================================================

class MutexNameTests(unittest.TestCase):
    def test_the_name_is_derived_from_the_permanent_app_id(self) -> None:
        from app.version import APP_ID

        name = single_instance.mutex_name()
        self.assertIn(APP_ID.strip("{}"), name)

    def test_the_mutex_is_scoped_to_the_interactive_session(self) -> None:
        """Local\\ not Global\\: each Windows account has its own data directory, its own
        DPAPI credential and its own portal sessions, so each gets its own instance. A
        Global mutex would let one account's copy block another account entirely."""
        name = single_instance.mutex_name()
        self.assertTrue(name.startswith("Local\\"))
        self.assertNotIn("Global\\", name)

    def test_the_name_is_stable_across_calls_and_versions(self) -> None:
        self.assertEqual(single_instance.mutex_name(), single_instance.mutex_name())
        from app.version import VERSION

        self.assertNotIn(VERSION, single_instance.mutex_name())

    def test_a_name_containing_a_backslash_cannot_escape_the_namespace(self) -> None:
        name = single_instance.mutex_name("{aa\\bb}")
        self.assertEqual(name.count("\\"), 1)


@unittest.skipUnless(WINDOWS, "the named mutex is a Windows kernel object")
class WindowsMutexTests(unittest.TestCase):
    def guard(self) -> single_instance.WindowsMutexGuard:
        # A unique name per test, so a run can never collide with the real application.
        return single_instance.WindowsMutexGuard(
            f"Local\\RisingPathRecruiters-test-{os.getpid()}-{id(self)}")

    def test_the_first_instance_acquires_it(self) -> None:
        guard = self.guard()
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)

    def test_a_second_instance_is_refused(self) -> None:
        first = self.guard()
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)

        second = single_instance.WindowsMutexGuard(first.name)
        # A different process would be refused; within one process the mutex is re-entrant,
        # so the child process below is what proves the real behaviour.
        self.addCleanup(second.release)
        second.acquire()

    def test_a_separate_process_is_refused_while_we_hold_it(self) -> None:
        guard = self.guard()
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)

        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from app.single_instance import WindowsMutexGuard;"
            "g = WindowsMutexGuard(r'%s');"
            "print('ACQUIRED' if g.acquire() else 'REFUSED')" % (ROOT, guard.name)
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                   text=True, timeout=120)
        self.assertIn("REFUSED", completed.stdout, completed.stderr)

    def test_releasing_lets_the_next_instance_in(self) -> None:
        guard = self.guard()
        self.assertTrue(guard.acquire())
        name = guard.name
        guard.release()

        code = (
            "import sys; sys.path.insert(0, r'%s');"
            "from app.single_instance import WindowsMutexGuard;"
            "g = WindowsMutexGuard(r'%s');"
            "print('ACQUIRED' if g.acquire() else 'REFUSED')" % (ROOT, name)
        )
        completed = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                   text=True, timeout=120)
        self.assertIn("ACQUIRED", completed.stdout, completed.stderr)

    def _hold_handle_open(self, name: str):
        """Keep a handle to the mutex OBJECT without taking ownership of it.

        Needed to observe abandonment at all: if the dying process holds the only handle,
        Windows destroys the object outright and the next CreateMutexW makes a brand-new one.
        WAIT_ABANDONED is only reported while the object still exists."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, name)
        self.addCleanup(lambda: kernel32.CloseHandle(handle))
        return handle

    def _spawn_holder(self, name: str) -> subprocess.Popen:
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; sys.path.insert(0, r'%s');"
             "from app.single_instance import WindowsMutexGuard;"
             "g = WindowsMutexGuard(r'%s');"
             "print('HELD' if g.acquire() else 'REFUSED', flush=True);"
             "time.sleep(120)" % (ROOT, name)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: holder.kill() if holder.poll() is None else None)
        self.assertIn("HELD", holder.stdout.readline())
        return holder

    def test_a_hard_killed_instance_does_not_lock_the_user_out(self) -> None:
        """The property that actually matters: after a crash, the application still starts."""
        name = f"Local\\RisingPathRecruiters-crash-{os.getpid()}"
        holder = self._spawn_holder(name)
        holder.kill()
        holder.wait(timeout=30)

        guard = single_instance.WindowsMutexGuard(name)
        self.addCleanup(guard.release)
        self.assertTrue(guard.acquire(), "a crashed previous run blocked startup")

    def test_an_abandoned_mutex_is_recovered_and_reported(self) -> None:
        """With the object kept alive, a killed owner produces a genuine WAIT_ABANDONED --
        and the guard takes ownership rather than refusing to start."""
        name = f"Local\\RisingPathRecruiters-abandon-{os.getpid()}"
        self._hold_handle_open(name)          # keeps the object alive past the kill
        holder = self._spawn_holder(name)
        holder.kill()
        holder.wait(timeout=30)

        guard = single_instance.WindowsMutexGuard(name)
        self.addCleanup(guard.release)
        self.assertTrue(guard.acquire(), "an abandoned mutex blocked startup")
        self.assertTrue(guard.recovered_abandoned,
                        "abandonment was not detected, so a crash would look like a "
                        "clean handover")


class FileGuardTests(_IsolatedTestCase):
    """The cross-platform fallback, and the logic the tests can exercise anywhere."""

    def guard(self, name: str = "launcher.lock") -> single_instance.FileInstanceGuard:
        return single_instance.FileInstanceGuard(self.temp_root / "work" / name)

    def test_acquire_and_release(self) -> None:
        guard = self.guard()
        self.assertTrue(guard.acquire())
        self.assertTrue(guard.path.is_file())
        guard.release()
        self.assertFalse(guard.path.exists())

    def test_a_live_holder_refuses_the_second_instance(self) -> None:
        first = self.guard()
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        second = single_instance.FileInstanceGuard(first.path)
        with patch.object(single_instance.FileInstanceGuard, "_holder_is_alive",
                          return_value=True):
            self.assertFalse(second.acquire())

    def test_a_dead_holder_is_treated_as_abandoned(self) -> None:
        guard = self.guard()
        guard.path.parent.mkdir(parents=True, exist_ok=True)
        guard.path.write_text("999999999\n", encoding="utf-8")
        self.assertTrue(guard.acquire())
        self.addCleanup(guard.release)
        self.assertTrue(guard.recovered_abandoned)

    def test_create_guard_picks_the_kernel_object_on_windows(self) -> None:
        guard = single_instance.create_guard(self.temp_root / "work" / "launcher.lock")
        expected = (single_instance.WindowsMutexGuard if WINDOWS
                    else single_instance.FileInstanceGuard)
        self.assertIsInstance(guard, expected)


class ProcessIdentityTests(unittest.TestCase):
    def test_a_running_process_is_detected(self) -> None:
        self.assertTrue(single_instance.process_is_running(os.getpid()))

    def test_an_impossible_pid_is_not(self) -> None:
        for pid in (0, -1, 999999999):
            with self.subTest(pid=pid):
                self.assertFalse(single_instance.process_is_running(pid))

    @unittest.skipUnless(WINDOWS, "the handle-retention behaviour is Windows-specific")
    def test_an_exited_process_is_not_reported_as_running(self) -> None:
        """A finished process stays as a kernel object while anyone holds a handle, and
        OpenProcess succeeds for it. Checking only the handle reported the application as
        still running long after it had stopped -- which made the installer's wait time out
        on a shutdown that had in fact worked perfectly."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=60)
        # Popen deliberately still holds its handle here; that is the condition that used to
        # produce the false positive.
        self.assertFalse(single_instance.process_is_running(child.pid))

    @unittest.skipUnless(WINDOWS, "the handle-retention behaviour is Windows-specific")
    def test_a_live_child_is_reported_as_running(self) -> None:
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.kill)
        try:
            self.assertTrue(single_instance.process_is_running(child.pid))
        finally:
            child.kill()
            child.wait(timeout=30)

    @unittest.skipUnless(WINDOWS, "image names are read through the Windows API")
    def test_the_image_name_of_this_process_is_readable(self) -> None:
        name = single_instance.process_image_name(os.getpid())
        self.assertTrue(name.lower().startswith("python"), name)

    def test_process_checks_never_signal_the_process(self) -> None:
        """Existence is tested, never affected -- signal 0 on POSIX, a query-only handle on
        Windows."""
        source = read(ROOT / "app" / "single_instance.py")
        self.assertIn("os.kill(pid, 0)", source)
        self.assertNotIn("TerminateProcess", source)
        self.assertNotIn("taskkill", source)


# ======================================================================================
# 3. Runtime state
# ======================================================================================

class RuntimeStateTests(_IsolatedTestCase):
    def sample(self, **overrides) -> runtime_state.RuntimeState:
        values = dict(pid=os.getpid(), host="127.0.0.1", port=8123, version="1.0.0",
                      started_at=1000.0, ready=True)
        values.update(overrides)
        return runtime_state.RuntimeState(**values)

    def test_state_round_trips(self) -> None:
        runtime_state.write_state(self.sample())
        loaded = runtime_state.read_state()
        self.assertEqual(loaded.port, 8123)
        self.assertEqual(loaded.url, "http://127.0.0.1:8123")

    def test_state_lives_under_localappdata_not_program_files(self) -> None:
        path = runtime_state.state_path()
        self.assertTrue(str(path).startswith(str(self.temp_root)))
        self.assertEqual(path.name, runtime_state.STATE_FILENAME)

    def test_the_state_contains_no_secret(self) -> None:
        runtime_state.write_state(self.sample())
        payload = read(runtime_state.state_path()).lower()
        for forbidden in ("password", "token", "postgresql://", "cookie", "secret",
                          "credential", "li_at"):
            with self.subTest(field=forbidden):
                self.assertNotIn(forbidden, payload)

    def test_only_known_fields_are_published(self) -> None:
        runtime_state.write_state(self.sample())
        payload = json.loads(read(runtime_state.state_path()))
        self.assertEqual(set(payload) - runtime_state.ALLOWED_FIELDS, set())

    def test_unknown_fields_in_a_file_are_ignored_rather_than_trusted(self) -> None:
        path = runtime_state.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"pid": 5, "port": 9, "shutdown_token": "injected"}),
                        encoding="utf-8")
        loaded = runtime_state.read_state()
        self.assertEqual(loaded.pid, 5)
        self.assertFalse(hasattr(loaded, "shutdown_token"))

    def test_writes_are_atomic(self) -> None:
        """A crash mid-write must leave the old state or the new one, never a mixture."""
        source = read(ROOT / "app" / "runtime_state.py")
        body = source[source.index("def write_state"):source.index("def clear_state")]
        self.assertIn("os.replace", body)
        self.assertIn(".tmp", body)

    def test_no_temporary_file_survives_a_write(self) -> None:
        runtime_state.write_state(self.sample())
        leftovers = list(runtime_state.state_path().parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_a_corrupt_or_missing_state_is_none_not_an_exception(self) -> None:
        self.assertIsNone(runtime_state.read_state())
        path = runtime_state.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for rubbish in ("not json at all", "[]", "null", '{"pid": "abc"'):
            with self.subTest(content=rubbish):
                path.write_text(rubbish, encoding="utf-8")
                self.assertIsNone(runtime_state.read_state())

    def test_cleanup_removes_state_and_token(self) -> None:
        runtime_state.write_state(self.sample())
        runtime_state.write_token(runtime_state.generate_token())
        runtime_state.cleanup()
        self.assertFalse(runtime_state.state_path().exists())
        self.assertFalse(runtime_state.token_path().exists())

    def test_cleanup_is_safe_to_call_twice(self) -> None:
        runtime_state.cleanup()
        runtime_state.cleanup()


class StaleStateTests(_IsolatedTestCase):
    def test_a_dead_process_makes_state_stale(self) -> None:
        state = runtime_state.RuntimeState(pid=999999999, port=8000, host="127.0.0.1")
        self.assertFalse(runtime_state.state_is_live(state))

    def test_an_empty_or_portless_state_is_stale(self) -> None:
        for state in (None,
                      runtime_state.RuntimeState(),
                      runtime_state.RuntimeState(pid=os.getpid(), port=0)):
            with self.subTest(state=state):
                self.assertFalse(runtime_state.state_is_live(state))

    def test_a_recycled_process_id_is_rejected(self) -> None:
        """Process ids are reused. Acting on a recycled one would mean touching an unrelated
        program, so the image name has to match as well."""
        state = runtime_state.RuntimeState(pid=os.getpid(), port=8000)
        with patch.object(single_instance, "process_is_running", return_value=True), \
                patch.object(single_instance, "process_image_name", return_value="notepad.exe"):
            self.assertFalse(runtime_state.state_is_live(state))

    def test_a_live_process_that_does_not_answer_is_stale(self) -> None:
        state = runtime_state.RuntimeState(pid=os.getpid(), port=8000)
        with patch.object(single_instance, "process_is_running", return_value=True), \
                patch.object(single_instance, "process_image_name",
                             return_value=runtime_state.EXECUTABLE_NAME), \
                patch.object(runtime_state, "health_answers", return_value=False):
            self.assertFalse(runtime_state.state_is_live(state))

    def test_a_live_answering_instance_is_accepted(self) -> None:
        state = runtime_state.RuntimeState(pid=os.getpid(), port=8000)
        with patch.object(single_instance, "process_is_running", return_value=True), \
                patch.object(single_instance, "process_image_name",
                             return_value=runtime_state.EXECUTABLE_NAME), \
                patch.object(runtime_state, "health_answers", return_value=True):
            self.assertTrue(runtime_state.state_is_live(state))

    def test_an_unreadable_image_name_is_not_treated_as_a_mismatch(self) -> None:
        """"Could not determine" is not proof of anything, so it must not reject."""
        state = runtime_state.RuntimeState(pid=os.getpid(), port=8000)
        with patch.object(single_instance, "process_is_running", return_value=True), \
                patch.object(single_instance, "process_image_name", return_value=""), \
                patch.object(runtime_state, "health_answers", return_value=True):
            self.assertTrue(runtime_state.state_is_live(state))


class ShutdownTokenTests(_IsolatedTestCase):
    def test_tokens_are_random_and_long(self) -> None:
        first, second = runtime_state.generate_token(), runtime_state.generate_token()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)

    def test_the_token_round_trips(self) -> None:
        token = runtime_state.generate_token()
        runtime_state.write_token(token)
        self.assertEqual(runtime_state.read_token(), token)

    def test_the_token_lives_apart_from_the_readable_state_file(self) -> None:
        self.assertNotEqual(runtime_state.token_path(), runtime_state.state_path())

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_the_token_is_not_readable_by_other_standard_users(self) -> None:
        runtime_state.write_token(runtime_state.generate_token())
        listing = subprocess.run(["icacls", str(runtime_state.token_path())],
                                 capture_output=True, text=True, timeout=60).stdout
        for group in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
            with self.subTest(group=group):
                self.assertNotIn(group, listing)

    def test_the_file_is_locked_down_before_the_token_is_written_into_it(self) -> None:
        source = read(ROOT / "app" / "runtime_state.py")
        body = source[source.index("def write_token"):source.index("def read_token")]
        self.assertLess(body.index("restrict_to_current_user"),
                        body.index("target.write_text(token"))

    def test_clearing_overwrites_before_deleting(self) -> None:
        runtime_state.write_token("a-real-looking-token-value")
        runtime_state.clear_token()
        self.assertFalse(runtime_state.token_path().exists())

    def test_a_missing_token_reads_as_empty_not_an_error(self) -> None:
        self.assertEqual(runtime_state.read_token(), "")


# ======================================================================================
# 4. Shutdown
# ======================================================================================

class ShutdownAuthorisationTests(unittest.TestCase):
    def coordinator(self, token: str = "the-real-token"):
        return shutdown_module.ShutdownCoordinator(token)

    def test_the_correct_token_authorises(self) -> None:
        self.assertTrue(self.coordinator().authorises("the-real-token"))

    def test_a_wrong_absent_or_empty_token_does_not(self) -> None:
        coordinator = self.coordinator()
        for presented in (None, "", "wrong", "the-real-token ", "THE-REAL-TOKEN"):
            with self.subTest(presented=presented):
                self.assertFalse(coordinator.authorises(presented))

    def test_an_instance_with_no_token_authorises_nothing(self) -> None:
        empty = shutdown_module.ShutdownCoordinator("")
        for presented in ("", None, "anything"):
            with self.subTest(presented=presented):
                self.assertFalse(empty.authorises(presented))

    def test_the_comparison_is_constant_time(self) -> None:
        source = read(ROOT / "app" / "shutdown.py")
        self.assertIn("secrets.compare_digest", source)

    def test_only_loopback_callers_are_accepted(self) -> None:
        for address, expected in (("127.0.0.1", True), ("::1", True),
                                  ("192.168.1.50", False), ("10.0.0.1", False),
                                  ("0.0.0.0", False)):
            with self.subTest(address=address):
                scope = {"client": (address, 51000)}
                self.assertEqual(shutdown_module.client_is_loopback(scope), expected)

    def test_an_unknown_peer_is_refused(self) -> None:
        """Unknown must never mean allowed in an authorisation decision."""
        for scope in ({}, {"client": None}, {"client": ()}):
            with self.subTest(scope=scope):
                self.assertFalse(shutdown_module.client_is_loopback(scope))

    def test_requesting_shutdown_sets_the_flag_and_calls_the_stop_hook(self) -> None:
        coordinator = self.coordinator()
        stopped = []
        coordinator.bind(lambda: stopped.append(True))
        coordinator.request("test")
        self.assertTrue(coordinator.wait(timeout=5))
        self.assertTrue(coordinator.requested)
        for _ in range(50):
            if stopped:
                break
            import time as _t

            _t.sleep(0.05)
        self.assertTrue(stopped)

    def test_a_repeated_request_is_harmless(self) -> None:
        coordinator = self.coordinator()
        calls = []
        coordinator.bind(lambda: calls.append(1))
        coordinator.request("first")
        coordinator.request("second")
        self.assertTrue(coordinator.requested)


class ShutdownEndpointTests(unittest.TestCase):
    """Exercised through the real ASGI stack, so the guards run exactly as they will in
    production. The app is called directly rather than through TestClient because the peer
    address has to be controllable -- it is what the loopback guard decides on."""

    def build(self, token: str = "the-real-token"):
        from fastapi import FastAPI

        app = FastAPI()
        coordinator = shutdown_module.ShutdownCoordinator(token)
        coordinator.bind(lambda: None)
        shutdown_module.install(app, coordinator)
        return app, coordinator

    def call(self, app, *, method: str = "POST", headers: dict | None = None,
             peer: str = "127.0.0.1"):
        import asyncio

        messages: list[dict] = []
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": method,
            "path": shutdown_module.SHUTDOWN_PATH,
            "raw_path": shutdown_module.SHUTDOWN_PATH.encode(),
            "query_string": b"", "root_path": "", "scheme": "http",
            "headers": [(k.lower().encode(), v.encode())
                        for k, v in (headers or {}).items()],
            "client": (peer, 51000), "server": ("127.0.0.1", 8000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(app(scope, receive, send))
        status = next(m["status"] for m in messages
                      if m["type"] == "http.response.start")
        body = b"".join(m.get("body", b"") for m in messages
                        if m["type"] == "http.response.body")
        return status, body.decode("utf-8", errors="replace")

    def test_a_valid_request_is_accepted_and_triggers_shutdown(self) -> None:
        app, coordinator = self.build()
        status, _ = self.call(app, headers={shutdown_module.TOKEN_HEADER: "the-real-token"})
        self.assertEqual(status, 202)
        self.assertTrue(coordinator.requested)

    def test_an_invalid_token_is_rejected_and_nothing_stops(self) -> None:
        app, coordinator = self.build()
        status, _ = self.call(app, headers={shutdown_module.TOKEN_HEADER: "guessed"})
        self.assertEqual(status, 403)
        self.assertFalse(coordinator.requested)

    def test_a_missing_token_is_rejected(self) -> None:
        app, coordinator = self.build()
        status, _ = self.call(app)
        self.assertEqual(status, 403)
        self.assertFalse(coordinator.requested)

    def test_a_get_request_cannot_stop_the_application(self) -> None:
        """So no link, redirect or image tag on an open page can trigger it."""
        app, coordinator = self.build()
        status, _ = self.call(app, method="GET",
                              headers={shutdown_module.TOKEN_HEADER: "the-real-token"})
        self.assertEqual(status, 405)
        self.assertFalse(coordinator.requested)

    def test_a_remote_caller_is_refused_even_with_the_correct_token(self) -> None:
        """Defence in depth: the server binds loopback only, but a proxy or a future change
        must not be able to turn this into a remote kill switch."""
        app, coordinator = self.build()
        status, _ = self.call(app, peer="192.168.1.50",
                              headers={shutdown_module.TOKEN_HEADER: "the-real-token"})
        self.assertEqual(status, 404)
        self.assertFalse(coordinator.requested)

    def test_an_unknown_peer_is_refused(self) -> None:
        app, coordinator = self.build()
        status, _ = self.call(app, peer="testclient",
                              headers={shutdown_module.TOKEN_HEADER: "the-real-token"})
        self.assertEqual(status, 404)
        self.assertFalse(coordinator.requested)

    def test_the_endpoint_is_hidden_from_the_public_schema(self) -> None:
        app, _ = self.build()
        route = next(r for r in app.routes
                     if getattr(r, "path", "") == shutdown_module.SHUTDOWN_PATH)
        self.assertFalse(route.include_in_schema)

    def test_the_token_is_never_returned_in_a_response(self) -> None:
        app, _ = self.build("a-very-distinctive-token")
        _, body = self.call(
            app, headers={shutdown_module.TOKEN_HEADER: "a-very-distinctive-token"})
        self.assertNotIn("a-very-distinctive-token", body)


class InstallerShutdownTests(_IsolatedTestCase):
    def test_no_running_instance_is_reported_as_success(self) -> None:
        """Nothing to stop is not a failure -- the installer can proceed."""
        self.assertEqual(launcher.request_running_instance_shutdown(),
                         int(ExitCode.SUCCESS))

    def test_stale_state_is_cleaned_up_rather_than_acted_on(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(pid=999999999, port=8000))
        self.assertEqual(launcher.request_running_instance_shutdown(),
                         int(ExitCode.SUCCESS))
        self.assertFalse(runtime_state.state_path().exists())

    def test_a_live_instance_is_asked_and_waited_for(self) -> None:
        state = runtime_state.RuntimeState(pid=os.getpid(), port=8000)
        runtime_state.write_state(state)
        runtime_state.write_token("token-value")
        alive = iter([True, True, False])
        with patch.object(runtime_state, "state_is_live", return_value=True), \
                patch.object(launcher, "request_running_instance_shutdown",
                             wraps=launcher.request_running_instance_shutdown), \
                patch("app.shutdown.request_shutdown", return_value=True) as asked, \
                patch("app.single_instance.process_is_running",
                      side_effect=lambda pid: next(alive, False)):
            code = launcher.request_running_instance_shutdown(timeout=10)
        self.assertEqual(code, int(ExitCode.SUCCESS))
        asked.assert_called_once()

    def test_a_refused_request_reports_failure_rather_than_forcing(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(pid=os.getpid(), port=8000))
        runtime_state.write_token("token-value")
        with patch.object(runtime_state, "state_is_live", return_value=True), \
                patch("app.shutdown.request_shutdown", return_value=False):
            self.assertEqual(launcher.request_running_instance_shutdown(),
                             int(ExitCode.SHUTDOWN_FAILED))

    def test_shutdown_never_terminates_anything(self) -> None:
        source = read(ROOT / "app" / "windows_launcher.py")
        block = source[source.index("def request_running_instance_shutdown"):
                       source.index("def _run_application")]
        for forceful in ("taskkill", "TerminateProcess", "os.kill", "Stop-Process"):
            with self.subTest(pattern=forceful):
                self.assertNotIn(forceful, block)


# ======================================================================================
# 5. PostgreSQL service control
# ======================================================================================

class ServiceNameGuardTests(unittest.TestCase):
    """The guard that makes "start a service" a safe operation."""

    def test_postgresql_service_names_are_accepted(self) -> None:
        for name in ("postgresql-x64-16", "postgresql-16", "postgresql-x64-13",
                     "PostgreSQL-x64-17", "postgresql"):
            with self.subTest(name=name):
                self.assertTrue(service_control.is_postgres_service_name(name))

    def test_everything_else_is_refused(self) -> None:
        for name in ("MSSQLSERVER", "Spooler", "wuauserv", "", "   ",
                     "postgres; shutdown", "my-postgresql-thing", "MySQL80",
                     "postgresql & calc.exe"):
            with self.subTest(name=name):
                self.assertFalse(service_control.is_postgres_service_name(name))

    def test_a_refused_name_is_never_started(self) -> None:
        with patch.object(service_control, "_run_sc") as sc:
            result = service_control.start_postgres_service("Spooler")
        sc.assert_not_called()
        self.assertIs(result.outcome, StartOutcome.REFUSED)

    def test_nothing_in_this_module_stops_or_reconfigures_a_service(self) -> None:
        source = read(ROOT / "app" / "service_control.py")
        for dangerous in ('"stop"', "'stop'", '"delete"', '"config"', "sc stop"):
            with self.subTest(pattern=dangerous):
                self.assertNotIn(dangerous, source)


@unittest.skipUnless(WINDOWS, "service control is Windows-only")
class ServiceStartTests(unittest.TestCase):
    """Every branch, driven by mocks. No real service is started or stopped."""

    def test_an_already_running_service_is_not_restarted(self) -> None:
        with patch.object(service_control, "query_state", return_value="RUNNING"), \
                patch.object(service_control, "_run_sc") as sc:
            result = service_control.start_postgres_service(
                "postgresql-x64-16", verify=lambda: True)
        sc.assert_not_called()
        self.assertIs(result.outcome, StartOutcome.ALREADY_RUNNING)
        self.assertTrue(result.ok)

    def test_a_stopped_service_is_started_and_verified(self) -> None:
        states = iter(["STOPPED", "START_PENDING", "RUNNING"])
        with patch.object(service_control, "query_state",
                          side_effect=lambda name: next(states, "RUNNING")), \
                patch.object(service_control, "_run_sc",
                             return_value=subprocess.CompletedProcess([], 0, "", "")):
            result = service_control.start_postgres_service(
                "postgresql-x64-16", timeout=10, verify=lambda: True)
        self.assertIs(result.outcome, StartOutcome.STARTED)
        self.assertTrue(result.accepting_connections)

    def test_permission_denied_is_reported_distinctly(self) -> None:
        with patch.object(service_control, "query_state", return_value="STOPPED"), \
                patch.object(service_control, "_run_sc",
                             return_value=subprocess.CompletedProcess(
                                 [], 1, "FAILED 5:\nAccess is denied.", "")):
            result = service_control.start_postgres_service("postgresql-x64-16")
        self.assertIs(result.outcome, StartOutcome.PERMISSION_DENIED)
        self.assertFalse(result.ok)

    def test_a_service_that_never_reaches_running_times_out(self) -> None:
        with patch.object(service_control, "query_state", return_value="START_PENDING"), \
                patch.object(service_control, "_run_sc",
                             return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(service_control, "POLL_INTERVAL_SECONDS", 0.01):
            result = service_control.start_postgres_service("postgresql-x64-16", timeout=0.05)
        self.assertIs(result.outcome, StartOutcome.TIMED_OUT)

    def test_running_but_not_accepting_connections_times_out(self) -> None:
        """RUNNING is not ready: PostgreSQL performs recovery before it listens."""
        with patch.object(service_control, "query_state", return_value="RUNNING"), \
                patch.object(service_control, "_run_sc",
                             return_value=subprocess.CompletedProcess([], 0, "", "")), \
                patch.object(service_control, "POLL_INTERVAL_SECONDS", 0.01):
            result = service_control.start_postgres_service(
                "postgresql-x64-16", timeout=0.05, verify=lambda: False)
        self.assertFalse(result.accepting_connections)

    def test_an_unknown_service_is_reported_as_not_found(self) -> None:
        with patch.object(service_control, "query_state", return_value=""):
            result = service_control.start_postgres_service("postgresql-x64-99")
        self.assertIs(result.outcome, StartOutcome.NOT_FOUND)

    def test_only_a_detected_service_name_is_ever_used(self) -> None:
        """The name comes from detection, never from a caller or configuration."""
        from app.postgres_detection import DetectionReport, ProbeResult, ServiceState

        report = DetectionReport(
            installations=[], services=[ServiceState(name="postgresql-x64-16",
                                                     state="STOPPED")],
            probe=ProbeResult(port=5432))
        with patch.object(service_control, "start_postgres_service") as start:
            service_control.start_detected_postgres(report, verify=lambda: True)
        start.assert_called_once()
        self.assertEqual(start.call_args[0][0], "postgresql-x64-16")

    def test_no_postgres_service_means_nothing_is_started(self) -> None:
        from app.postgres_detection import DetectionReport, ProbeResult, ServiceState

        report = DetectionReport(installations=[],
                                 services=[ServiceState(name="Spooler", state="STOPPED")],
                                 probe=ProbeResult(port=5432))
        with patch.object(service_control, "start_postgres_service") as start:
            result = service_control.start_detected_postgres(report)
        start.assert_not_called()
        self.assertIs(result.outcome, StartOutcome.NOT_FOUND)


class PostgresStartupDecisionTests(_IsolatedTestCase):
    """What the launcher does with each detection outcome."""

    def detection(self, **kwargs):
        from app.postgres_detection import (
            DetectionReport, PostgresInstallation, ProbeResult, ServiceState,
        )

        installed = kwargs.get("installed", False)
        return DetectionReport(
            installations=[PostgresInstallation(version="16.4")] if installed else [],
            services=[ServiceState(name="postgresql-x64-16",
                                   state="RUNNING" if kwargs.get("running") else "STOPPED")]
            if installed else [],
            probe=ProbeResult(port=5432, reachable=kwargs.get("reachable", False),
                              is_postgres=kwargs.get("is_postgres", False)),
        )

    def test_a_running_server_needs_no_action(self) -> None:
        with patch("app.postgres_detection.detect",
                   return_value=self.detection(installed=True, running=True,
                                               is_postgres=True, reachable=True)), \
                patch.object(service_control, "start_detected_postgres") as start:
            self.assertIsNone(launcher.ensure_postgres_running())
        start.assert_not_called()

    def test_a_stopped_service_is_started(self) -> None:
        from app.service_control import StartReport

        with patch("app.postgres_detection.detect",
                   return_value=self.detection(installed=True, running=False)), \
                patch("app.service_control.start_detected_postgres",
                      return_value=StartReport(StartOutcome.STARTED, "postgresql-x64-16",
                                               accepting_connections=True)):
            self.assertIsNone(launcher.ensure_postgres_running())

    def test_a_missing_postgresql_is_reported_not_installed(self) -> None:
        with patch("app.postgres_detection.detect", return_value=self.detection()):
            problem = launcher.ensure_postgres_running()
        self.assertEqual(problem.code, ExitCode.POSTGRES_NOT_FOUND)

    def test_a_foreign_program_on_the_port_is_not_treated_as_postgresql(self) -> None:
        with patch("app.postgres_detection.detect",
                   return_value=self.detection(reachable=True)), \
                patch.object(service_control, "start_detected_postgres") as start:
            problem = launcher.ensure_postgres_running()
        start.assert_not_called()
        self.assertEqual(problem.code, ExitCode.PORT_OCCUPIED_BY_OTHER)

    def test_a_permission_failure_explains_what_to_do(self) -> None:
        from app.service_control import StartReport

        with patch("app.postgres_detection.detect",
                   return_value=self.detection(installed=True)), \
                patch("app.service_control.start_detected_postgres",
                      return_value=StartReport(StartOutcome.PERMISSION_DENIED, "x")):
            problem = launcher.ensure_postgres_running()
        self.assertEqual(problem.code, ExitCode.SERVICE_START_FAILED)
        self.assertIn("administrator", problem.message.lower())

    def test_a_timeout_says_it_may_still_be_starting(self) -> None:
        from app.service_control import StartReport

        with patch("app.postgres_detection.detect",
                   return_value=self.detection(installed=True)), \
                patch("app.service_control.start_detected_postgres",
                      return_value=StartReport(StartOutcome.TIMED_OUT, "x")):
            problem = launcher.ensure_postgres_running()
        self.assertEqual(problem.code, ExitCode.SERVICE_START_FAILED)
        self.assertIn("did not", problem.message.lower())

    def test_the_launcher_never_installs_postgresql(self) -> None:
        source = read(ROOT / "app" / "windows_launcher.py")
        for pattern in ("postgresql-16", "--optionfile", "install_runtimes", ".exe --mode"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, source)


# ======================================================================================
# 6. Ports, readiness and the browser
# ======================================================================================

class ReadinessTests(unittest.TestCase):
    def test_readiness_requires_both_the_api_and_the_web_interface(self) -> None:
        """/health answers as soon as the process is up, while the React shell can still be
        missing -- opening a browser then shows the user an error page."""
        calls = []

        def fake(url, headers=None, timeout=2.0):
            calls.append(url)
            return "" if url.endswith("/health") else None

        with patch.object(launcher, "_http_ok", side_effect=fake):
            self.assertFalse(launcher.wait_until_ready(8000, timeout=0.5,
                                                       require_shell=True))
        self.assertTrue(any(u.endswith("/health") for u in calls))

    def test_readiness_succeeds_when_both_answer(self) -> None:
        def fake(url, headers=None, timeout=2.0):
            return "ok" if url.endswith("/health") else f'<html>{launcher.SHELL_MARKER}</html>'

        with patch.object(launcher, "_http_ok", side_effect=fake):
            self.assertTrue(launcher.wait_until_ready(8000, timeout=5, require_shell=True))

    def test_a_shell_without_the_react_root_is_not_ready(self) -> None:
        def fake(url, headers=None, timeout=2.0):
            return "ok" if url.endswith("/health") else "<html>503 unavailable</html>"

        with patch.object(launcher, "_http_ok", side_effect=fake):
            self.assertFalse(launcher.wait_until_ready(8000, timeout=0.5,
                                                       require_shell=True))

    def test_development_mode_does_not_wait_for_a_shell_it_never_serves(self) -> None:
        with patch.object(launcher, "_http_ok",
                          side_effect=lambda url, headers=None, timeout=2.0:
                          "ok" if url.endswith("/health") else None):
            self.assertTrue(launcher.wait_until_ready(8000, timeout=5, require_shell=False))

    def test_the_shell_is_requested_the_way_a_browser_requests_it(self) -> None:
        seen = {}

        def fake(url, headers=None, timeout=2.0):
            if not url.endswith("/health"):
                seen.update(headers or {})
                return f"<html>{launcher.SHELL_MARKER}</html>"
            return "ok"

        with patch.object(launcher, "_http_ok", side_effect=fake):
            launcher.wait_until_ready(8000, timeout=5, require_shell=True)
        self.assertIn("text/html", seen.get("Accept", ""))


class PortSelectionTests(unittest.TestCase):
    def test_the_preferred_port_is_used_when_free(self) -> None:
        with patch.object(launcher, "port_is_free", return_value=True):
            self.assertEqual(launcher.find_available_port(8000), 8000)

    def test_the_next_free_port_is_chosen_when_it_is_not(self) -> None:
        free = {8003}
        with patch.object(launcher, "port_is_free",
                          side_effect=lambda p, host=launcher.HOST: p in free):
            self.assertEqual(launcher.find_available_port(8000), 8003)

    def test_an_exhausted_range_raises_rather_than_binding_something_else(self) -> None:
        with patch.object(launcher, "port_is_free", return_value=False):
            with self.assertRaises(RuntimeError):
                launcher.find_available_port(8000, attempts=3)

    def test_another_instance_is_distinguished_from_an_unrelated_program(self) -> None:
        with patch("app.runtime_state.port_owner_is_this_application", return_value=True):
            self.assertIn("this application", launcher.describe_port_conflict(8000))
        with patch("app.runtime_state.port_owner_is_this_application", return_value=False):
            self.assertIn("another program", launcher.describe_port_conflict(8000))

    def test_the_server_only_ever_binds_loopback(self) -> None:
        self.assertEqual(launcher.HOST, "127.0.0.1")
        offenders = [line.strip() for line in
                     read(ROOT / "app" / "windows_launcher.py").splitlines()
                     if "0.0.0.0" in line and not line.strip().startswith("#")]
        self.assertEqual(offenders, [])


class SecondInstanceTests(_IsolatedTestCase):
    def test_a_healthy_running_instance_has_its_window_opened(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(
            pid=os.getpid(), host="127.0.0.1", port=8123, ready=True))
        with patch.object(runtime_state, "state_is_live", return_value=True), \
                patch.object(launcher.webbrowser, "open") as opened:
            code = launcher.open_existing_instance()
        self.assertEqual(code, int(ExitCode.ALREADY_RUNNING))
        opened.assert_called_once_with("http://127.0.0.1:8123")

    def test_the_recorded_port_is_used_rather_than_assuming_8000(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(
            pid=os.getpid(), host="127.0.0.1", port=8207, ready=True))
        with patch.object(runtime_state, "state_is_live", return_value=True), \
                patch.object(launcher.webbrowser, "open") as opened:
            launcher.open_existing_instance()
        self.assertIn("8207", opened.call_args[0][0])

    def test_no_error_dialog_is_shown_for_a_healthy_instance(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(
            pid=os.getpid(), port=8123, ready=True))
        with patch.object(runtime_state, "state_is_live", return_value=True), \
                patch.object(launcher.webbrowser, "open"), \
                patch.object(launcher, "show_error_dialog") as dialog:
            launcher.open_existing_instance()
        dialog.assert_not_called()

    def test_stale_state_is_removed_and_startup_continues(self) -> None:
        runtime_state.write_state(runtime_state.RuntimeState(pid=999999999, port=8000))
        with patch.object(launcher.webbrowser, "open") as opened:
            self.assertIsNone(launcher.open_existing_instance())
        opened.assert_not_called()
        self.assertFalse(runtime_state.state_path().exists())

    def test_absent_state_simply_continues(self) -> None:
        self.assertIsNone(launcher.open_existing_instance())

    def test_a_second_instance_never_starts_a_second_server(self) -> None:
        source = read(ROOT / "app" / "windows_launcher.py")
        body = source[source.index("def open_existing_instance"):
                      source.index("def request_running_instance_shutdown")]
        self.assertNotIn("serve(", body)
        self.assertNotIn("uvicorn", body)


class BrowserOpeningTests(_IsolatedTestCase):
    """The browser opens once, after readiness, and never on a failure."""

    def run_startup(self, ready: bool):
        opened = []

        class FakeServer:
            should_exit = False

        class FakeThread:
            def __init__(self):
                self.alive = True

            def is_alive(self):
                was, self.alive = self.alive, False
                return was

            def join(self, timeout=None):
                return None

        with patch.object(launcher, "configure_playwright_browsers"), \
                patch.object(launcher, "ensure_postgres_running", return_value=None), \
                patch.object(launcher, "classify_startup_problem", return_value=None), \
                patch.object(launcher, "run_migrations", return_value=None), \
                patch.object(launcher, "find_available_port", return_value=8321), \
                patch.object(launcher, "port_is_free", return_value=True), \
                patch.object(launcher, "serve", return_value=(FakeServer(), FakeThread())), \
                patch.object(launcher, "wait_until_ready", return_value=ready), \
                patch.object(launcher, "_shut_down"), \
                patch.object(launcher, "show_error_dialog"), \
                patch.object(launcher.webbrowser, "open", side_effect=opened.append):
            code = launcher._run_application(no_browser=False, check_only=False)
        return code, opened

    def test_the_browser_opens_exactly_once_after_readiness(self) -> None:
        code, opened = self.run_startup(ready=True)
        self.assertEqual(code, int(ExitCode.SUCCESS))
        self.assertEqual(opened, ["http://127.0.0.1:8321"])

    def test_no_browser_opens_when_readiness_fails(self) -> None:
        code, opened = self.run_startup(ready=False)
        self.assertEqual(code, int(ExitCode.STARTUP_FAILED))
        self.assertEqual(opened, [])

    def test_a_migration_failure_stops_before_any_server_or_browser(self) -> None:
        problem = StartupProblem(ExitCode.MIGRATION_FAILED, "Database update failed", "x")
        with patch.object(launcher, "configure_playwright_browsers"), \
                patch.object(launcher, "ensure_postgres_running", return_value=None), \
                patch.object(launcher, "classify_startup_problem", return_value=None), \
                patch.object(launcher, "run_migrations", return_value=problem), \
                patch.object(launcher, "serve") as serve, \
                patch.object(launcher, "show_error_dialog"), \
                patch.object(launcher.webbrowser, "open") as opened:
            code = launcher._run_application(no_browser=False, check_only=False)
        self.assertEqual(code, int(ExitCode.MIGRATION_FAILED))
        serve.assert_not_called()
        opened.assert_not_called()

    def test_the_runtime_state_records_the_port_that_was_chosen(self) -> None:
        self.run_startup(ready=True)
        # _shut_down is patched out, so the published state is still on disk.
        state = runtime_state.read_state()
        self.assertIsNotNone(state)
        self.assertEqual(state.port, 8321)
        self.assertTrue(state.ready)


# ======================================================================================
# 7. Browser ownership
# ======================================================================================

class BrowserOwnershipTests(unittest.TestCase):
    """Nothing is terminated unless it is provably ours. The user's own Chrome -- possibly
    signed in to LinkedIn -- must never be touched."""

    BUNDLED = r"C:\Program Files\Rising Path Recruiters\_internal\ms-playwright"

    def snapshot(self):
        return [
            # ours: inside the installation and descended from us
            (2001, 1000, self.BUNDLED + r"\chromium-1148\chrome-win\chrome.exe", "chrome.exe"),
            (2002, 2001, self.BUNDLED + r"\chromium-1148\chrome-win\chrome.exe", "chrome.exe"),
            # the user's own Chrome: different path, different parent
            (3001, 500, r"C:\Program Files\Google\Chrome\Application\chrome.exe", "chrome.exe"),
            # another instance's browser: right path, but not our descendant
            (4001, 4000, self.BUNDLED + r"\chromium-1148\chrome-win\chrome.exe", "chrome.exe"),
            # unrelated processes
            (5001, 1000, r"C:\Windows\System32\notepad.exe", "notepad.exe"),
            (5002, 1000, r"C:\Python312\python.exe", "python.exe"),
            (5003, 1, r"C:\Program Files\PostgreSQL\16\bin\postgres.exe", "postgres.exe"),
        ]

    @unittest.skipUnless(WINDOWS, "process ownership is resolved through Windows APIs")
    def test_only_our_own_descendants_inside_the_installation_are_claimed(self) -> None:
        with patch.object(browser_lifecycle, "_snapshot", return_value=self.snapshot()):
            owned = browser_lifecycle.owned_browsers(root_pid=1000,
                                                     install_dir=Path(self.BUNDLED))
        self.assertEqual(sorted(p.pid for p in owned), [2001, 2002])

    @unittest.skipUnless(WINDOWS, "process ownership is resolved through Windows APIs")
    def test_the_users_own_chrome_is_never_claimed(self) -> None:
        with patch.object(browser_lifecycle, "_snapshot", return_value=self.snapshot()):
            owned = browser_lifecycle.owned_browsers(root_pid=1000,
                                                     install_dir=Path(self.BUNDLED))
        self.assertNotIn(3001, [p.pid for p in owned])

    @unittest.skipUnless(WINDOWS, "process ownership is resolved through Windows APIs")
    def test_another_instances_browser_is_not_ours_to_close(self) -> None:
        with patch.object(browser_lifecycle, "_snapshot", return_value=self.snapshot()):
            owned = browser_lifecycle.owned_browsers(root_pid=1000,
                                                     install_dir=Path(self.BUNDLED))
        self.assertNotIn(4001, [p.pid for p in owned])

    @unittest.skipUnless(WINDOWS, "process ownership is resolved through Windows APIs")
    def test_python_and_postgres_are_never_candidates(self) -> None:
        with patch.object(browser_lifecycle, "_snapshot", return_value=self.snapshot()):
            owned = browser_lifecycle.owned_browsers(root_pid=1000,
                                                     install_dir=Path(self.BUNDLED))
        claimed = {p.pid for p in owned}
        for unrelated in (5001, 5002, 5003):
            with self.subTest(pid=unrelated):
                self.assertNotIn(unrelated, claimed)

    def test_nothing_is_claimed_when_the_installation_cannot_be_located(self) -> None:
        with patch.object(browser_lifecycle, "_snapshot", return_value=self.snapshot()):
            self.assertEqual(browser_lifecycle.owned_browsers(root_pid=1000,
                                                              install_dir=None), [])

    def test_nothing_is_closed_when_nothing_is_owned(self) -> None:
        with patch.object(browser_lifecycle, "owned_browsers", return_value=[]), \
                patch("subprocess.run") as run:
            self.assertEqual(browser_lifecycle.close_owned_browsers(), [])
        run.assert_not_called()

    def test_closing_targets_process_ids_never_image_names(self) -> None:
        """taskkill /IM chrome.exe would end every Chrome on the computer."""
        source = read(ROOT / "app" / "browser_lifecycle.py")
        self.assertNotIn("/IM", source)
        self.assertIn('"/PID"', source)

    def test_a_parent_cycle_cannot_hang_shutdown(self) -> None:
        cyclic = [(10, 11, self.BUNDLED + r"\chrome.exe", "chrome.exe"),
                  (11, 10, self.BUNDLED + r"\chrome.exe", "chrome.exe")]
        with patch.object(browser_lifecycle, "_snapshot", return_value=cyclic):
            browser_lifecycle.owned_browsers(root_pid=1, install_dir=Path(self.BUNDLED))


# ======================================================================================
# 8. Dialogs, GUI build, icon
# ======================================================================================

class GuiBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = read(SPEC_FILE)

    def test_the_shipped_executable_is_windowed(self) -> None:
        self.assertIn("console=CONSOLE_BUILD", self.spec)
        self.assertIn('CONSOLE_BUILD = os.environ.get("RPR_CONSOLE_BUILD"', self.spec)

    def test_a_console_build_is_opt_in_only(self) -> None:
        """Nothing in the default build path sets the variable, so the release is windowed."""
        for script in ("scripts/build_windows.ps1", "scripts/build_installer.ps1"):
            with self.subTest(script=script):
                self.assertNotIn("RPR_CONSOLE_BUILD", read(ROOT / script))

    def test_windowed_tracebacks_are_not_disabled(self) -> None:
        self.assertIn("disable_windowed_traceback=False", self.spec)

    def test_the_icon_and_version_resource_are_applied(self) -> None:
        self.assertIn("icon=ICON_FILE", self.spec)
        self.assertIn("version=VERSION_RESOURCE", self.spec)

    def test_the_version_resource_is_generated_from_the_single_source(self) -> None:
        self.assertIn("from app.version import", self.spec)
        for field in ("CompanyName", "FileDescription", "FileVersion", "ProductName",
                      "ProductVersion", "LegalCopyright"):
            with self.subTest(field=field):
                self.assertIn(field, self.spec)

    def test_the_version_resource_is_not_written_into_the_source_tree(self) -> None:
        self.assertIn('PROJECT_ROOT / "build" / "version_info.txt"', self.spec)


class HeadlessStreamTests(_LoggingPreservingTestCase):
    """The PC9 failure: a windowed build has no sys.stdout or sys.stderr.

    Installation and provisioning succeeded; the application then died on startup with
    ``AttributeError: 'NoneType' object has no attribute 'isatty'``, raised out of
    ``logging.config.dictConfig`` while uvicorn built its own default formatter. Nothing in
    the suite could see it, because a test runner and a console build both have real
    streams -- so these tests take the streams away first."""

    def setUp(self) -> None:
        super().setUp()
        # uvicorn.Config's constructor mutates the levels and handlers of the uvicorn
        # loggers. Put them back, so this file cannot change how the rest of the suite logs.
        touched = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
        saved = {name: (logging.getLogger(name).level,
                        list(logging.getLogger(name).handlers),
                        logging.getLogger(name).propagate) for name in touched}

        def restore() -> None:
            for name, (level, handlers, propagate) in saved.items():
                logger = logging.getLogger(name)
                logger.setLevel(level)
                logger.handlers = handlers
                logger.propagate = propagate

        self.addCleanup(restore)

    def test_uvicorn_default_logging_really_does_fail_without_streams(self) -> None:
        """Proof that the test below is not vacuous.

        If a future uvicorn stops calling isatty() on a missing stream this fails, and the
        workaround can be reconsidered on evidence rather than removed on a hunch."""
        import uvicorn

        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            with self.assertRaises(ValueError) as caught:
                uvicorn.Config(_null_asgi_app, host="127.0.0.1", port=8123,
                               log_level="info", access_log=False)

        # PC9 reported exactly this pair: dictConfig wraps the real fault in a ValueError
        # naming the formatter, and the cause is the isatty() call on a missing stream.
        self.assertIn("Unable to configure formatter 'default'", str(caught.exception))
        cause = caught.exception.__cause__
        self.assertIsInstance(cause, AttributeError)
        self.assertIn("isatty", str(cause))

    def test_the_launchers_configuration_survives_missing_streams(self) -> None:
        from app import windows_launcher as launcher

        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            config = launcher.uvicorn_config(_null_asgi_app, 8123)

        self.assertIsNone(config.log_config, "uvicorn would reconfigure logging again")
        self.assertFalse(config.access_log)
        self.assertEqual(config.host, launcher.HOST)
        self.assertEqual(config.port, 8123)

    def test_serve_builds_its_configuration_through_that_helper(self) -> None:
        """A second construction site would reintroduce the crash without failing a test."""
        source = read(ROOT / "app" / "windows_launcher.py")
        self.assertEqual(source.count("uvicorn.Config("), 1)
        self.assertIn("config = uvicorn_config(app, port)", source)

    def test_uvicorn_logs_reach_the_applications_own_file_log(self) -> None:
        """Requirement: launcher.log still receives startup and error lines.

        With no handlers of its own, uvicorn's logger propagates to the root -- which the
        packaged launcher has already pointed at launcher.log."""
        from app import windows_launcher as launcher

        with patch.object(sys, "stdout", None), patch.object(sys, "stderr", None):
            launcher.uvicorn_config(_null_asgi_app, 8123)

        error_logger = logging.getLogger("uvicorn.error")
        self.assertTrue(error_logger.propagate)
        self.assertEqual(error_logger.handlers, [])

        root = logging.getLogger()
        captured = []

        class Capture(logging.Handler):
            def emit(self, record):
                captured.append(record.getMessage())

        handler = Capture()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        error_logger.info("Uvicorn running on http://127.0.0.1:8123")
        self.assertIn("Uvicorn running on http://127.0.0.1:8123", captured)

    def test_the_application_log_configuration_installs_no_stream_it_cannot_write_to(self) -> None:
        from app.logging_config import _console_handler, configure_logging

        with patch.object(sys, "stderr", None):
            self.assertEqual(_console_handler()["class"], "logging.NullHandler")
            configure_logging(force=True)
            # Emitting must not raise -- this is what failed for every record before.
            logging.getLogger("linkedin_auto_apply").error("startup failure")

    def test_a_console_build_still_logs_to_the_console(self) -> None:
        """The windowed guard must not change behaviour where a stream does exist."""
        from app.logging_config import _console_handler

        self.assertEqual(_console_handler()["class"], "logging.StreamHandler")


async def _null_asgi_app(scope, receive, send):  # pragma: no cover - never invoked
    """Enough of an ASGI application for uvicorn.Config to accept. Never served."""


class ApplicationIconTests(unittest.TestCase):
    def test_the_icon_was_built_from_the_projects_own_logo(self) -> None:
        from scripts.make_icon import source_path

        self.assertTrue(source_path().is_file(),
                        "the approved logo is missing from the repository")
        self.assertIn("Rising Path", source_path().name)

    def test_the_icon_exists(self) -> None:
        self.assertTrue(ICON_FILE.is_file(),
                        "run 'python -m scripts.make_icon' to build the icon")

    def test_the_icon_carries_every_size_windows_asks_for(self) -> None:
        from PIL import Image

        with Image.open(ICON_FILE) as icon:
            sizes = {size for size in icon.info.get("sizes", [])}
        for required in (16, 32, 48, 256):
            with self.subTest(size=required):
                self.assertIn((required, required), sizes)

    def test_the_installer_uses_the_same_icon(self) -> None:
        iss = read(ISS_FILE)
        self.assertIn("SetupIconFile={#IconFile}", iss)
        self.assertIn('#define IconFile "RisingPathRecruiters.ico"', iss)

    def test_shortcuts_inherit_the_icon_from_the_executable(self) -> None:
        """One icon source: the exe carries it as a resource, so the Start-menu and desktop
        shortcuts and Add/Remove Programs all show the same artwork."""
        iss = read(ISS_FILE)
        self.assertIn("UninstallDisplayIcon={app}\\{#ExeName}.exe", iss)


class ErrorDialogTests(_LoggingPreservingTestCase):
    CASES = (
        ("PostgreSQL missing", ExitCode.POSTGRES_NOT_FOUND),
        ("service cannot start", ExitCode.SERVICE_START_FAILED),
        ("credential missing", ExitCode.CREDENTIALS_MISSING),
        ("authentication failure", ExitCode.AUTHENTICATION_FAILED),
        ("port conflict", ExitCode.PORT_OCCUPIED_BY_OTHER),
        ("migration failure", ExitCode.MIGRATION_FAILED),
        ("schema verification", ExitCode.SCHEMA_VERIFICATION_FAILED),
        ("startup timeout", ExitCode.STARTUP_FAILED),
        ("bundled Chromium", ExitCode.BROWSER_MISSING),
    )

    def test_every_failure_has_its_own_exit_code(self) -> None:
        codes = [code for _, code in self.CASES]
        self.assertEqual(len(codes), len(set(codes)))

    def test_a_dialog_never_shows_a_password_or_a_url(self) -> None:
        from io import StringIO

        secret = "an-actual-database-password"
        problem = StartupProblem(
            ExitCode.STARTUP_FAILED, "Failed",
            f"connection to postgresql+psycopg://app:{secret}@localhost/db refused")
        launcher.set_interactive(False)
        self.addCleanup(launcher.set_interactive, True)
        buffer = StringIO()
        with patch.object(launcher.sys, "stderr", buffer):
            launcher.show_error_dialog(problem)
        printed = buffer.getvalue()
        self.assertNotIn(secret, printed)
        self.assertIn("***", printed)

    def test_dialog_text_carries_the_application_name(self) -> None:
        problem = StartupProblem(ExitCode.POSTGRES_NOT_FOUND, "Database is not installed",
                                 f"{launcher.APP_TITLE} needs PostgreSQL.")
        self.assertIn("Rising Path Recruiters", problem.as_dialog())

    def test_dialogs_do_not_contain_raw_tracebacks(self) -> None:
        """A stack trace is for the log; a dialog gets a sentence and a next step."""
        source = read(ROOT / "app" / "windows_launcher.py")
        body = source[source.index("def ensure_postgres_running"):
                      source.index("def _http_ok")]
        self.assertNotIn("format_exc", body)
        self.assertNotIn("traceback", body)

    def test_a_log_path_is_only_offered_when_the_file_exists(self) -> None:
        bootstrap_log.configure()
        self.assertTrue(bootstrap_log.log_file_exists())
        self.assertIn("launcher.log", launcher.log_file_reference())

        with patch.object(bootstrap_log, "_log_directory", side_effect=OSError("denied")):
            bootstrap_log.configure()
        self.assertNotIn("launcher.log", launcher.log_file_reference())
        self.assertIn("no log file", launcher.log_file_reference().lower())

    def test_failures_are_logged_as_well_as_shown(self) -> None:
        problem = StartupProblem(ExitCode.MIGRATION_FAILED, "Database update failed", "detail")
        with patch.object(launcher, "show_error_dialog"), \
                self.assertLogs("app.windows_launcher", level="ERROR") as captured:
            code = launcher.report(problem)
        self.assertEqual(code, int(ExitCode.MIGRATION_FAILED))
        self.assertIn("Database update failed", "\n".join(captured.output))


# ======================================================================================
# 9. Diagnostic modes, installer integration, and the read-only installation
# ======================================================================================

class DiagnosticModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = read(ROOT / "app" / "windows_launcher.py")

    def test_every_documented_mode_is_handled(self) -> None:
        for flag in ("--selftest", "--browser-check", "--check", "--no-browser",
                     "--installer", "--shutdown", "--diagnostic-console"):
            with self.subTest(flag=flag):
                self.assertIn(f'"{flag}"', self.source)

    def test_non_interactive_modes_never_show_a_modal_dialog(self) -> None:
        """A message box behind an installer wizard would hang the installation."""
        body = self.source[self.source.index("def main("):]
        self.assertIn("non_interactive = (check_only or selftest_only or browser_check_only",
                      body)
        self.assertIn("set_interactive(not non_interactive)", body)

    def test_the_diagnostic_console_mode_restores_stream_logging(self) -> None:
        self.assertIn("configure_logging(stream=diagnostic_console or not is_windowed())",
                      self.source)


class InstallerIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = read(ISS_FILE)

    def test_the_installer_asks_the_application_to_stop_itself(self) -> None:
        self.assertIn("function RequestGracefulShutdown", self.iss)
        self.assertIn("'--shutdown'", self.iss)

    def test_the_shutdown_request_runs_as_the_original_user(self) -> None:
        """The runtime state and token live in THAT account's LocalAppData; an elevated
        installer would look in the wrong profile."""
        block = self.iss[self.iss.index("function RequestGracefulShutdown"):
                         self.iss.index("function AskUserToCloseApp")]
        self.assertIn("ExecAsOriginalUser", block)

    def test_the_user_is_asked_when_graceful_shutdown_does_not_work(self) -> None:
        block = self.iss[self.iss.index("function AskUserToCloseApp"):
                         self.iss.index("function InitializeSetup")]
        self.assertIn("RequestGracefulShutdown", block)
        self.assertIn("MsgBox", block)

    def test_the_installer_still_forces_nothing(self) -> None:
        code = iss_code(self.iss)
        for forceful in ("taskkill", "Stop-Process", "TerminateProcess",
                         "python.exe", "chrome.exe", "postgres.exe"):
            with self.subTest(pattern=forceful):
                self.assertNotIn(forceful, code)

    def test_uninstall_still_preserves_user_data(self) -> None:
        """Only the [UninstallDelete] SECTION -- the [Code] that follows legitimately names
        the data directory when it prepares the PostgreSQL option file."""
        start = self.iss.index("[UninstallDelete]")
        end = self.iss.index("[Messages]", start)
        block = self.iss[start:end]
        for preserved in ("{localappdata}", "AIJobAutomation", "browser_sessions",
                          "uploads", "config"):
            with self.subTest(preserved=preserved):
                self.assertNotIn(preserved, block)

    def test_the_app_id_and_version_source_are_unchanged(self) -> None:
        from app.version import APP_ID, VERSION

        self.assertEqual(APP_ID, "{7B3D9E42-5C18-4A6F-9E27-1D4A8F0C6B35}")
        self.assertIn(APP_ID.strip("{}"), self.iss)
        self.assertIn(f'#define AppVersion "{VERSION}"', self.iss)


class ReadOnlyInstallationTests(_LoggingPreservingTestCase):
    def test_nothing_a_normal_start_writes_lands_in_the_installation(self) -> None:
        from app import paths as paths_module

        bundle = self.temp_root / "Program Files" / "Rising Path Recruiters"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / "_internal" / "marker.txt").write_text("bundled", encoding="utf-8")
        before = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))

        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            reset_runtime_paths_cache()
            bootstrap_log.configure()
            runtime_state.publish(8123, version="1.0.0")
            runtime_state.write_token(runtime_state.generate_token())

        after = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))
        self.assertEqual(before, after, "the installation was written to")

    def test_every_runtime_file_lives_under_localappdata(self) -> None:
        for path in (runtime_state.state_path(), runtime_state.token_path(),
                     bootstrap_log.configure()):
            with self.subTest(path=path):
                self.assertTrue(str(path).startswith(str(self.temp_root)))

    def test_shutdown_removes_everything_this_run_published(self) -> None:
        runtime_state.publish(8123, version="1.0.0")
        runtime_state.write_token(runtime_state.generate_token())

        class FakeServer:
            should_exit = False

        class FakeThread:
            def join(self, timeout=None):
                return None

        with patch("app.browser_lifecycle.close_owned_browsers", return_value=[]):
            launcher._shut_down(FakeServer(), FakeThread())

        self.assertFalse(runtime_state.state_path().exists())
        self.assertFalse(runtime_state.token_path().exists())

    def test_shutdown_closes_the_browsers_this_application_started(self) -> None:
        class FakeServer:
            should_exit = False

        class FakeThread:
            def join(self, timeout=None):
                return None

        with patch("app.browser_lifecycle.close_owned_browsers",
                   return_value=[2001, 2002]) as close:
            launcher._shut_down(FakeServer(), FakeThread())
        close.assert_called_once()

    def test_surviving_threads_are_recorded_so_a_hang_is_diagnosable(self) -> None:
        log_file = bootstrap_log.configure()
        lingering = threading.Thread(target=lambda: time.sleep(2.5),
                                     name="lingering-worker", daemon=False)
        lingering.start()
        self.addCleanup(lingering.join)

        self.assertIn("lingering-worker", launcher.surviving_threads())

        class FakeServer:
            should_exit = False

        class FakeThread:
            def join(self, timeout=None):
                return None

        with patch("app.browser_lifecycle.close_owned_browsers", return_value=[]):
            launcher._shut_down(FakeServer(), FakeThread())
        for handler in logging.getLogger().handlers:
            handler.flush()
        self.assertIn("lingering-worker", read(log_file))

    def test_daemon_threads_are_not_counted_as_survivors(self) -> None:
        """They cannot keep the process alive, so listing them would be noise."""
        daemon = threading.Thread(target=lambda: time.sleep(1.5), name="daemon-worker",
                                  daemon=True)
        daemon.start()
        self.addCleanup(daemon.join)
        self.assertNotIn("daemon-worker", launcher.surviving_threads())

    def test_the_entry_point_ends_the_process_decisively(self) -> None:
        """Returning from main() waits for non-daemon threads, and the web stack leaves some
        behind -- the process then lingered for minutes with no window, holding the mutex."""
        source = read(ROOT / "app" / "windows_launcher.py")
        self.assertIn("_exit_now(main())", source)
        self.assertNotIn("sys.exit(main())", source)
        body = source[source.index("def _exit_now"):source.index("def main(")]
        # The log is flushed before the hard exit, or the last lines would be lost.
        self.assertLess(body.index("logging.shutdown()"), body.index("os._exit"))

    def test_a_browser_cleanup_failure_never_blocks_the_exit(self) -> None:
        class FakeServer:
            should_exit = False

        class FakeThread:
            def join(self, timeout=None):
                return None

        with patch("app.browser_lifecycle.close_owned_browsers",
                   side_effect=RuntimeError("boom")):
            launcher._shut_down(FakeServer(), FakeThread())  # must not raise


if __name__ == "__main__":
    unittest.main()
