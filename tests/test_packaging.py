"""The packaged Windows build: what the executable must guarantee on a clean computer.

A packaged install has no source checkout, no virtual environment, no Node.js, no ``.env``,
and -- until provisioning has run -- no database credential. Everything below is a property
that had to hold for the build to work at all, or that broke at least once while getting it
to work, so each test pins a real failure mode rather than an implementation detail:

* paths resolve from the INSTALLATION, never from the current working directory;
* importing the application never touches the database, so a missing credential produces a
  readable message instead of a crash before logging even exists;
* every writable location is under ``%LOCALAPPDATA%\\AIJobAutomation``; the program folder is
  read-only;
* the server binds loopback only, a second instance is refused, and the browser opens only
  after the interface actually answers;
* PyInstaller ships the resources the app opens by path (built React app, Alembic revisions,
  bundled Chromium) and the modules it reaches only at runtime;
* nothing private (``.env``, credentials, portal sessions, resumes, logs) can reach ``dist/``;
* no message, log line or dialog can contain a password.

Tests that need the real build skip themselves when ``dist/`` has not been produced, so the
suite stays runnable on a machine that has never run scripts\\build_windows.ps1.

Nothing here performs a live LinkedIn or Dice action, submits an application, or connects to
the user's real database.
"""

from __future__ import annotations

import builtins
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from app import paths as paths_module
from app import windows_launcher as launcher
from app.paths import (
    APP_NAME,
    DATA_DIR_ENV,
    MODE_ENV,
    MODE_PACKAGED,
    frontend_dist_dir,
    install_root,
    project_root,
    reset_runtime_paths_cache,
    resolve_runtime_paths,
    runtime_paths,
)
from app.windows_launcher import ExitCode, StartupProblem
from scripts.package_modules import collect_package_modules
from scripts.scan_build_secrets import scan

ROOT = project_root()
SPEC_FILE = ROOT / "RisingPathRecruiters.spec"
RUNTIME_HOOK = ROOT / "scripts" / "pyinstaller_runtime_hook.py"
BUILD_SCRIPT = ROOT / "scripts" / "build_windows.ps1"
PACKAGED_TEST_SCRIPT = ROOT / "scripts" / "test_packaged_build.ps1"
DIST_DIR = ROOT / "dist" / "RisingPathRecruiters"
DIST_INTERNAL = DIST_DIR / "_internal"

# A URL that is syntactically valid but points nowhere. create_engine() never connects, so
# this exercises engine construction without touching any real database.
UNREACHABLE_URL = "postgresql+psycopg://nobody:not-a-real-password@127.0.0.1:1/nothing"

requires_build = unittest.skipUnless(
    DIST_DIR.is_dir(), "no packaged build in dist/ (run scripts\\build_windows.ps1)"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class _IsolatedTestCase(unittest.TestCase):
    """A temporary data root, and a guarantee that the real user's data is never touched."""

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="packaging-test-"))
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.addCleanup(reset_runtime_paths_cache)
        reset_runtime_paths_cache()

    def data_dir_env(self, **overrides: str):
        values = {DATA_DIR_ENV: str(self.temp_root / "data"), **overrides}
        return patch.dict(os.environ, values, clear=False)


class _LoggingPreservingTestCase(_IsolatedTestCase):
    """``configure_logging`` calls ``basicConfig(force=True)``, which replaces the ROOT
    handlers for the whole process. Restoring them keeps this file from silencing the rest of
    the suite -- the same class of failure that once emptied the packaged launcher's log."""

    def setUp(self) -> None:
        super().setUp()
        root = logging.getLogger()
        handlers = list(root.handlers)
        level = root.level
        self.addCleanup(lambda: (root.handlers.clear(), root.handlers.extend(handlers),
                                 root.setLevel(level)))


# ======================================================================================
# 1. Packaged path detection
# ======================================================================================

class PackagedPathDetectionTests(_IsolatedTestCase):
    """Bundled assets are found relative to the INSTALLATION. Resolving them from the current
    working directory is what breaks an executable launched from the Start menu."""

    def test_install_root_is_the_pyinstaller_bundle_when_one_exists(self) -> None:
        bundle = self.temp_root / "onefile-bundle"
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(install_root(), bundle)

    def test_install_root_is_the_executable_directory_for_a_onedir_build(self) -> None:
        exe = self.temp_root / "Program" / "RisingPathRecruiters.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        with patch.object(paths_module.sys, "frozen", True, create=True), \
                patch.object(paths_module.sys, "executable", str(exe)):
            self.assertEqual(install_root(), exe.parent)

    def test_install_root_is_the_project_root_in_a_checkout(self) -> None:
        self.assertEqual(install_root(), ROOT)

    def test_the_frontend_is_looked_for_inside_the_installation(self) -> None:
        bundle = self.temp_root / "installed"
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(frontend_dist_dir(), bundle / "frontend" / "dist")

    def test_frozen_builds_select_the_packaged_layout(self) -> None:
        with patch.dict(os.environ, {MODE_ENV: ""}, clear=False):
            with patch.object(paths_module.sys, "frozen", True, create=True):
                self.assertTrue(paths_module.is_packaged())
            self.assertFalse(paths_module.is_packaged())

    def test_packaged_data_lives_under_localappdata(self) -> None:
        local = self.temp_root / "Local"
        with patch.dict(os.environ, {DATA_DIR_ENV: "", MODE_ENV: MODE_PACKAGED,
                                     "LOCALAPPDATA": str(local)}, clear=False):
            resolved = resolve_runtime_paths()
        self.assertEqual(resolved.data_root, local / APP_NAME)
        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertTrue(str(directory).startswith(str(local)))

    def test_the_installation_is_never_a_writable_location(self) -> None:
        bundle = self.temp_root / "Program Files" / "RisingPathRecruiters"
        bundle.mkdir(parents=True)
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                self.data_dir_env():
            resolved = resolve_runtime_paths()
        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertFalse(str(directory).startswith(str(bundle)))


# ======================================================================================
# 2. Lazy database initialization / no import-time crash
# ======================================================================================

def run_isolated(code: str, env_overrides: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """Run ``code`` in a child interpreter with a controlled environment.

    A child process is the only honest way to test import-time behaviour, and running it from
    a directory OUTSIDE the checkout reproduces the packaged situation exactly: pydantic reads
    ``.env`` relative to the working directory, so a developer's ``.env`` would otherwise
    supply the credential the test is trying to do without."""
    environment = {k: v for k, v in os.environ.items()
                   if k not in {"DATABASE_URL", "ALEMBIC_DATABASE_URL", "PGPASSWORD",
                                "AI_JOB_AUTOMATION_ALLOW_DEV_DB"}}
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.update(env_overrides)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, env=environment, cwd=str(cwd), timeout=180,
    )


class LazyDatabaseInitializationTests(_IsolatedTestCase):
    """The engine used to be created at import time, so on a computer with no credential yet
    merely importing ``app.*`` raised before the launcher could say anything useful."""

    def setUp(self) -> None:
        super().setUp()
        from app.database import reset_database_state

        self.addCleanup(reset_database_state)
        reset_database_state()

    def test_importing_the_application_creates_no_engine(self) -> None:
        result = run_isolated(
            """
            import app.database, app.paths, app.static_site, app.windows_launcher
            import app.schema_verification, app.db_credentials, app.secure_store
            assert app.database._engine is None, "an engine was built at import time"
            assert app.database._session_factory is None
            print("IMPORTS OK")
            """,
            {DATA_DIR_ENV: str(self.temp_root / "child"),
             "AI_JOB_AUTOMATION_CREDENTIAL_DIR": str(self.temp_root / "empty-store")},
            cwd=self.temp_root,
        )
        self.assertIn("IMPORTS OK", result.stdout, result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_engine_appears_on_first_use_and_is_reused(self) -> None:
        from app import database

        with patch.dict(os.environ, {"DATABASE_URL": UNREACHABLE_URL}, clear=False):
            from app.config import reset_settings_cache

            reset_settings_cache()
            self.addCleanup(reset_settings_cache)
            self.assertIsNone(database._engine)
            engine = database.get_engine()
            self.assertIsNotNone(database._engine)
            self.assertIs(engine, database.get_engine())

    def test_the_module_attributes_resolve_lazily(self) -> None:
        from app import database

        with patch.dict(os.environ, {"DATABASE_URL": UNREACHABLE_URL}, clear=False):
            from app.config import reset_settings_cache

            reset_settings_cache()
            self.addCleanup(reset_settings_cache)
            self.assertIs(database.engine, database.get_engine())
            self.assertIs(database.SessionLocal, database.get_session_factory())

    def test_an_assigned_engine_still_wins(self) -> None:
        """The migration tests point the startup path at a disposable database this way."""
        from app import database

        sentinel = object()
        database.engine = sentinel  # type: ignore[attr-defined]
        try:
            self.assertIs(database.get_engine(), sentinel)
        finally:
            database.reset_database_state()

    def test_an_unknown_attribute_still_raises(self) -> None:
        from app import database

        with self.assertRaises(AttributeError):
            database.not_a_real_attribute  # noqa: B018

    def test_no_module_binds_the_engine_or_session_factory_at_import_time(self) -> None:
        """``from app.database import SessionLocal`` resolves the lazy attribute AT IMPORT,
        which builds the engine and so demands a credential before the launcher can report
        that none is stored. Reaching it through the module keeps it lazy."""
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            if path.name == "database.py":
                continue
            for line in read(path).splitlines():
                statement = line.strip()
                if statement.startswith("from app.database import") and (
                        "SessionLocal" in statement or "engine" in statement):
                    offenders.append(f"{path.relative_to(ROOT)}: {statement}")
        self.assertEqual(offenders, [])

    def test_the_api_module_opens_sessions_through_the_lazy_attribute(self) -> None:
        self.assertIn("database.SessionLocal()", read(ROOT / "app" / "main.py"))


class SelfTestModeTests(_LoggingPreservingTestCase):
    """``--selftest`` answers one question: was everything PACKAGED? It must not fail for a
    reason that says nothing about packaging -- an unprovisioned computer is the normal state
    of a fresh install."""

    def test_a_placeholder_url_is_supplied_while_the_modules_are_imported(self) -> None:
        from app.db_credentials import URL_ENV

        real_import = builtins.__import__
        seen: list[str | None] = []

        def recording_import(name, *args, **kwargs):
            seen.append(os.environ.get(URL_ENV))
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {URL_ENV: ""}, clear=False), \
                patch.object(launcher, "logger"), \
                patch("builtins.__import__", side_effect=recording_import):
            problem = launcher.selftest()

        self.assertIsNone(problem, problem.message if problem else "")
        self.assertIn(launcher.SELFTEST_PLACEHOLDER_URL, seen)

    def test_the_placeholder_does_not_outlive_the_import(self) -> None:
        from app.db_credentials import URL_ENV

        with patch.dict(os.environ, {URL_ENV: ""}, clear=False), \
                patch.object(launcher, "logger"):
            launcher.selftest()
            self.assertFalse((os.environ.get(URL_ENV) or "").strip(),
                             "the placeholder was left in the environment")

    def test_a_configured_credential_is_never_replaced(self) -> None:
        from app.db_credentials import URL_ENV

        with patch.dict(os.environ, {URL_ENV: UNREACHABLE_URL}, clear=False):
            with patch.object(launcher, "logger"):
                launcher.selftest()
            self.assertEqual(os.environ[URL_ENV], UNREACHABLE_URL)

    def test_the_placeholder_carries_no_credential_and_names_no_real_database(self) -> None:
        from sqlalchemy.engine import make_url

        url = make_url(launcher.SELFTEST_PLACEHOLDER_URL)
        self.assertIsNone(url.password, "the placeholder must contain no password")
        self.assertEqual(url.host, "127.0.0.1")
        self.assertEqual(url.port, 1)
        self.assertNotIn("linkedin_auto_apply", launcher.SELFTEST_PLACEHOLDER_URL)

    def test_an_import_failure_is_still_reported_in_full(self) -> None:
        """Nothing about the placeholder may swallow a real packaging gap. The message below
        is the exact one that produced a Windows error dialog during this phase."""
        from app.db_credentials import URL_ENV

        real_import = builtins.__import__

        def explode_on_the_application(name, *args, **kwargs):
            if name == "app.main":
                raise ModuleNotFoundError("No module named 'scipy._cyutility'")
            return real_import(name, *args, **kwargs)

        with patch.dict(os.environ, {URL_ENV: UNREACHABLE_URL}, clear=False), \
                patch.object(launcher, "logger"), \
                patch("builtins.__import__", side_effect=explode_on_the_application):
            problem = launcher.selftest()

        self.assertIsNotNone(problem)
        self.assertEqual(problem.title, "Packaging problem")
        self.assertEqual(problem.code, ExitCode.STARTUP_FAILED)
        self.assertIn("scipy._cyutility", problem.message)
        self.assertIn("Traceback", problem.message)


class MissingSecureCredentialTests(_IsolatedTestCase):
    """No credential is a NORMAL state on a freshly installed computer: it means provisioning
    has not run yet. It must produce instructions, not a traceback."""

    def _classify(self, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return run_isolated(
            """
            from app.windows_launcher import ExitCode, classify_startup_problem
            problem = classify_startup_problem()
            print("CODE", int(problem.code) if problem else "none")
            print("TITLE", problem.title if problem else "")
            print("MESSAGE", (problem.message if problem else "").replace(chr(10), " | "))
            """,
            {DATA_DIR_ENV: str(self.temp_root / "child"),
             "AI_JOB_AUTOMATION_CREDENTIAL_DIR": str(self.temp_root / "empty-store"),
             **(extra_env or {})},
            cwd=self.temp_root,
        )

    def test_a_missing_credential_is_reported_as_setup_not_as_a_failure(self) -> None:
        result = self._classify()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"CODE {int(ExitCode.CREDENTIALS_MISSING)}", result.stdout)
        self.assertIn("Database not set up yet", result.stdout)
        # The message points at the installer, not at a PowerShell script: an installed
        # computer has the installer and no source checkout to run scripts from.
        self.assertIn("installer", result.stdout.lower())
        self.assertNotIn(".ps1", result.stdout)

    def test_the_message_names_no_credential_and_no_traceback(self) -> None:
        result = self._classify()
        lowered = result.stdout.lower()
        for leak in ("password=", "postgresql+psycopg://", "traceback"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, lowered)

    def test_an_unreadable_credential_store_does_not_crash_resolution(self) -> None:
        """A corrupt DPAPI blob is reported, not raised through the import path."""
        store = self.temp_root / "broken-store"
        store.mkdir()
        (store / "database_credential.dat").write_bytes(b"not a real dpapi blob")
        result = self._classify({"AI_JOB_AUTOMATION_CREDENTIAL_DIR": str(store)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CODE", result.stdout)


# ======================================================================================
# 3. Ports and loopback binding
# ======================================================================================

class LocalPortSelectionTests(unittest.TestCase):
    def test_the_preferred_port_is_used_when_it_is_free(self) -> None:
        with patch.object(launcher, "port_is_free", return_value=True):
            self.assertEqual(launcher.find_available_port(8000), 8000)

    def test_an_occupied_port_is_skipped(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind((launcher.HOST, 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            chosen = launcher.find_available_port(port, attempts=5)
        self.assertNotEqual(chosen, port)
        self.assertGreater(chosen, port)

    def test_port_probing_reflects_reality(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind((launcher.HOST, 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            self.assertFalse(launcher.port_is_free(port))
        self.assertTrue(launcher.port_is_free(port))

    def test_an_exhausted_range_raises_instead_of_binding_something_unexpected(self) -> None:
        with patch.object(launcher, "port_is_free", return_value=False):
            with self.assertRaises(RuntimeError):
                launcher.find_available_port(8000, attempts=3)

    def test_the_search_stays_inside_the_declared_range(self) -> None:
        seen: list[int] = []

        def record(port: int, host: str = launcher.HOST) -> bool:
            seen.append(port)
            return False

        with patch.object(launcher, "port_is_free", side_effect=record):
            with self.assertRaises(RuntimeError):
                launcher.find_available_port(8000, attempts=4)
        self.assertEqual(seen, [8000, 8001, 8002, 8003])


class LoopbackOnlyTests(unittest.TestCase):
    """The application is a local tool. Binding 0.0.0.0 would expose a database-backed
    interface with no authentication to the whole network."""

    def test_the_host_constant_is_loopback(self) -> None:
        self.assertEqual(launcher.HOST, "127.0.0.1")

    def test_the_server_is_configured_on_loopback(self) -> None:
        captured: dict = {}

        class FakeConfig:
            def __init__(self, app, **kwargs):
                captured.update(kwargs)

        class FakeServer:
            def __init__(self, config):
                self.config = config

            def run(self):
                return None

        fake_uvicorn = type("uvicorn", (), {"Config": FakeConfig, "Server": FakeServer})
        with patch.dict(sys.modules, {"uvicorn": fake_uvicorn}), \
                patch.dict(sys.modules, {"app.main": type("m", (), {"app": object()})}):
            server, thread = launcher.serve(8123)
            thread.join(timeout=5)

        self.assertEqual(captured["host"], "127.0.0.1")
        self.assertEqual(captured["port"], 8123)

    def test_no_wildcard_address_is_used_anywhere_in_the_launcher(self) -> None:
        """Comments may mention it; no line of CODE may contain it."""
        offenders = [
            line.strip()
            for line in read(ROOT / "app" / "windows_launcher.py").splitlines()
            if "0.0.0.0" in line and not line.strip().startswith("#")
        ]
        self.assertEqual(offenders, [])


# ======================================================================================
# 4. Single instance
# ======================================================================================

class SingleInstanceTests(_IsolatedTestCase):
    """Only the packaging-relevant part.

    The lock mechanism itself -- the named Windows mutex, its abandonment recovery, and the
    cross-platform lock-file fallback -- belongs to app/single_instance.py and is covered in
    tests/test_runtime_hardening.py. Repeating it here would be duplication."""

    def test_the_lock_lives_in_the_per_user_data_directory(self) -> None:
        with self.data_dir_env():
            reset_runtime_paths_cache()
            work = runtime_paths().work
        lock_path = work / launcher.LOCK_FILENAME
        self.assertTrue(str(lock_path).startswith(str(self.temp_root)))
        self.assertFalse(str(lock_path).startswith(str(install_root())))

    def test_the_guard_is_created_from_that_location(self) -> None:
        source = read(ROOT / "app" / "windows_launcher.py")
        self.assertIn("create_guard(runtime_paths().ensure().work / LOCK_FILENAME)", source)


# ======================================================================================
# 5. Startup sequence: readiness before the browser, and a clean shutdown
# ======================================================================================

class StartupDispatchTests(_LoggingPreservingTestCase):
    """How main() routes a launch.

    The startup SEQUENCE itself -- readiness before the browser, migration failure blocking
    the browser, clean shutdown, runtime state -- is exercised against the real code path in
    tests/test_runtime_hardening.py. What is packaging-specific, and therefore tested here,
    is that every non-interactive mode is dispatched before anything heavy happens."""

    def test_a_failing_startup_check_stops_before_the_server_starts(self) -> None:
        problem = StartupProblem(ExitCode.POSTGRES_NOT_FOUND, "Database is not running", "x")
        with self.data_dir_env(),                 patch.object(launcher, "configure_playwright_browsers"),                 patch.object(launcher, "ensure_postgres_running", return_value=problem),                 patch.object(launcher, "serve") as serve,                 patch.object(launcher, "show_error_dialog") as dialog:
            reset_runtime_paths_cache()
            code = launcher._run_application(no_browser=True, check_only=False)
        self.assertEqual(code, int(ExitCode.POSTGRES_NOT_FOUND))
        serve.assert_not_called()
        dialog.assert_called_once()

    def test_check_mode_verifies_and_exits_without_serving(self) -> None:
        with self.data_dir_env(),                 patch.object(launcher, "configure_playwright_browsers"),                 patch.object(launcher, "ensure_postgres_running", return_value=None),                 patch.object(launcher, "classify_startup_problem", return_value=None),                 patch.object(launcher, "run_migrations", return_value=None),                 patch.object(launcher, "serve") as serve:
            reset_runtime_paths_cache()
            code = launcher._run_application(no_browser=True, check_only=True)
        self.assertEqual(code, int(ExitCode.SUCCESS))
        serve.assert_not_called()

    def test_the_installer_mode_is_dispatched_before_the_instance_guard(self) -> None:
        """The installer may run these actions while the application is running."""
        source = read(ROOT / "app" / "windows_launcher.py")
        body = source[source.index("def main("):]
        self.assertLess(body.index("if installer_mode:"), body.index("create_guard("))
        self.assertLess(body.index("if shutdown_mode:"), body.index("create_guard("))
        self.assertLess(body.index("if browser_check_only:"), body.index("create_guard("))


class LocalAppDataLoggingTests(_LoggingPreservingTestCase):
    """The dialog points the user at a log file. It has to exist, and it has to be written
    before anything that can fail -- the packaged launcher once reported an ImportError by
    naming a file that had never been created."""

    def test_the_log_file_is_created_under_the_resolved_data_root(self) -> None:
        with self.data_dir_env():
            reset_runtime_paths_cache()
            log_file = launcher.configure_logging()
        self.assertTrue(log_file.is_file())
        self.assertEqual(log_file.name, launcher.LOG_FILENAME)
        self.assertTrue(str(log_file).startswith(str(self.temp_root)))

    def test_packaged_mode_logs_into_localappdata(self) -> None:
        local = self.temp_root / "Local"
        with patch.dict(os.environ, {DATA_DIR_ENV: "", MODE_ENV: MODE_PACKAGED,
                                     "LOCALAPPDATA": str(local)}, clear=False):
            reset_runtime_paths_cache()
            log_file = launcher.configure_logging()
        self.assertEqual(log_file.parent, local / APP_NAME / "logs")
        self.assertTrue(log_file.is_file())

    def test_startup_entries_reach_the_file(self) -> None:
        with self.data_dir_env():
            reset_runtime_paths_cache()
            log_file = launcher.configure_logging()
            logging.getLogger("packaging-test").info("started")
            for handler in logging.getLogger().handlers:
                handler.flush()
        self.assertIn("started", read(log_file))

    def test_nothing_is_logged_into_the_installation(self) -> None:
        with self.data_dir_env():
            reset_runtime_paths_cache()
            log_file = launcher.configure_logging()
        self.assertFalse(str(log_file).startswith(str(install_root())))


class ReadOnlyInstallationTests(_LoggingPreservingTestCase):
    """A real install sits in Program Files, where the user cannot write. Any runtime write
    into the program folder would fail there and silently work on the developer's machine."""

    def test_no_runtime_file_is_written_into_the_installation(self) -> None:
        bundle = self.temp_root / "Program Files" / "RisingPathRecruiters"
        (bundle / "_internal").mkdir(parents=True)
        (bundle / "_internal" / "marker.txt").write_text("bundled", encoding="utf-8")
        before = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))

        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                self.data_dir_env(**{"AI_JOB_AUTOMATION_CREDENTIAL_DIR": ""}):
            reset_runtime_paths_cache()
            resolved = runtime_paths().ensure()
            launcher.configure_logging()
            from app.secure_store import credential_path

            store = credential_path()

        after = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*"))
        self.assertEqual(before, after, "the packaged folder was written to")
        self.assertFalse(str(store).startswith(str(bundle)))
        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertFalse(str(directory).startswith(str(bundle)))


# ======================================================================================
# 6. PyInstaller resources and hidden imports
# ======================================================================================

class PyInstallerSpecTests(unittest.TestCase):
    """The spec is the build's contract. Each assertion below corresponds to a resource the
    application opens BY PATH or a module it reaches only at runtime."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.spec = read(SPEC_FILE)

    def test_the_entry_point_is_the_windows_launcher(self) -> None:
        self.assertIn('Analysis(\n    ["app/windows_launcher.py"]', self.spec)

    def test_the_build_is_onedir(self) -> None:
        self.assertIn("exclude_binaries=True", self.spec)
        self.assertIn("coll = COLLECT(", self.spec)

    def test_resources_opened_by_path_are_bundled(self) -> None:
        for resource in ('"frontend" / "dist"', '"alembic.ini"', '"alembic"',
                         'collect_data_files("playwright")',
                         'collect_data_files("certifi")',
                         '"build" / "ms-playwright"'):
            with self.subTest(resource=resource):
                self.assertIn(resource, self.spec)

    def test_the_runtime_hook_is_installed(self) -> None:
        self.assertIn('runtime_hooks=["scripts/pyinstaller_runtime_hook.py"]', self.spec)
        self.assertTrue(RUNTIME_HOOK.is_file())

    def test_dynamically_resolved_modules_are_declared(self) -> None:
        for module in ("psycopg", "psycopg_binary", "sqlalchemy.dialects.postgresql.psycopg",
                       "alembic.runtime.migration", "uvicorn.protocols.http.auto",
                       "uvicorn.loops.auto", "pdfplumber", "docx", "ctypes.wintypes"):
            with self.subTest(module=module):
                self.assertIn(f'"{module}"', self.spec)

    def test_the_application_package_is_collected(self) -> None:
        self.assertIn('collect_submodules("app")', self.spec)

    def test_development_only_packages_are_excluded(self) -> None:
        for excluded in ("pytest", "PyInstaller", "matplotlib", "tkinter", "streamlit"):
            with self.subTest(excluded=excluded):
                self.assertIn(f'"{excluded}"', self.spec)

    def test_the_scientific_stack_is_collected_from_disk_not_by_importing(self) -> None:
        """collect_submodules walks by IMPORTING, and scipy vendors a package that shadows
        numpy during that walk -- three packaged-runtime crashes came from exactly this."""
        self.assertIn('for _package in ("numpy", "scipy", "sklearn")', self.spec)
        self.assertIn("collect_package_modules(_package)", self.spec)
        self.assertNotIn('collect_submodules("scipy")', self.spec)
        self.assertNotIn('collect_submodules("numpy")', self.spec)
        self.assertNotIn('collect_submodules("sklearn")', self.spec)


class ModuleCollectionTests(unittest.TestCase):
    """The three modules below are the exact ones whose absence produced a Windows error
    dialog. Enumerating from the filesystem is what finds them."""

    HISTORICAL_FAILURES = (
        ("numpy", "numpy._core._exceptions"),
        ("scipy", "scipy._cyutility"),
        ("scipy", "scipy._external.array_api_compat.numpy.fft"),
    )

    def test_previously_missing_modules_are_now_collected(self) -> None:
        for package, module in self.HISTORICAL_FAILURES:
            with self.subTest(module=module):
                if __import__("importlib.util", fromlist=["util"]).find_spec(package) is None:
                    self.skipTest(f"{package} is not installed")
                self.assertIn(module, collect_package_modules(package))

    def test_compiled_extensions_are_named_without_their_abi_tag(self) -> None:
        names = collect_package_modules("numpy")
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertNotIn(".cp3", name)
                self.assertNotIn("-win_amd64", name)

    def test_test_subpackages_are_skipped(self) -> None:
        for name in collect_package_modules("numpy"):
            with self.subTest(name=name):
                self.assertNotIn(".tests.", f".{name}.")

    def test_an_absent_package_yields_nothing_rather_than_raising(self) -> None:
        self.assertEqual(collect_package_modules("a_package_that_is_not_installed"), [])


class RuntimeHookTests(unittest.TestCase):
    """The hook runs before any application code, so Playwright and Alembic are already
    pointed at the bundled copies by the time anything imports them."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.hook = read(RUNTIME_HOOK)

    def test_the_hook_points_playwright_at_the_bundled_browsers(self) -> None:
        self.assertIn('BROWSER_ENV = "PLAYWRIGHT_BROWSERS_PATH"', self.hook)
        self.assertIn('root / "ms-playwright"', self.hook)

    def test_an_explicit_browser_path_still_wins(self) -> None:
        self.assertIn("if os.environ.get(BROWSER_ENV):", self.hook)

    def test_the_hook_uses_the_same_alembic_variables_the_application_reads(self) -> None:
        from app.schema_verification import ALEMBIC_DIR_ENV, ALEMBIC_INI_ENV

        self.assertIn(ALEMBIC_INI_ENV, self.hook)
        self.assertIn(ALEMBIC_DIR_ENV, self.hook)


# ======================================================================================
# 7. Alembic revision discovery from a packaged build
# ======================================================================================

class PackagedAlembicDiscoveryTests(_IsolatedTestCase):
    def test_the_configured_locations_are_honoured(self) -> None:
        from app.schema_verification import (
            ALEMBIC_DIR_ENV,
            ALEMBIC_INI_ENV,
            alembic_locations,
        )

        ini = self.temp_root / "packaged" / "alembic.ini"
        directory = self.temp_root / "packaged" / "alembic"
        with patch.dict(os.environ, {ALEMBIC_INI_ENV: str(ini), ALEMBIC_DIR_ENV: str(directory)},
                        clear=False):
            self.assertEqual(alembic_locations(), (ini, directory))

    def test_a_source_checkout_falls_back_to_the_project_root(self) -> None:
        from app.schema_verification import (
            ALEMBIC_DIR_ENV,
            ALEMBIC_INI_ENV,
            alembic_locations,
        )

        with patch.dict(os.environ, {ALEMBIC_INI_ENV: "", ALEMBIC_DIR_ENV: ""}, clear=False):
            ini, directory = alembic_locations()
        self.assertEqual(ini, ROOT / "alembic.ini")
        self.assertEqual(directory, ROOT / "alembic")
        self.assertTrue(ini.is_file())

    def test_the_config_points_at_the_bundled_revisions(self) -> None:
        from app.schema_verification import (
            ALEMBIC_DIR_ENV,
            ALEMBIC_INI_ENV,
            alembic_config,
        )

        directory = self.temp_root / "bundle" / "alembic"
        directory.mkdir(parents=True)
        ini = self.temp_root / "bundle" / "alembic.ini"
        shutil.copyfile(ROOT / "alembic.ini", ini)
        with patch.dict(os.environ, {ALEMBIC_INI_ENV: str(ini), ALEMBIC_DIR_ENV: str(directory)},
                        clear=False):
            config = alembic_config()
        self.assertEqual(config.get_main_option("script_location"), str(directory))

    def test_a_password_containing_a_percent_sign_does_not_break_the_config(self) -> None:
        """ConfigParser interpolates '%', so a generated password could crash startup."""
        from app.schema_verification import alembic_config

        url = "postgresql+psycopg://user:pa%%ss%word@localhost:5432/db"
        config = alembic_config(url)
        self.assertEqual(config.get_main_option("sqlalchemy.url"), url)

    def test_the_baseline_revision_exists_in_the_bundled_revision_scripts(self) -> None:
        from app.schema_verification import BASELINE_REVISION

        versions = list((ROOT / "alembic" / "versions").glob("*.py"))
        self.assertTrue(versions)
        self.assertTrue(
            any(BASELINE_REVISION in read(path) for path in versions),
            f"no revision script declares {BASELINE_REVISION}",
        )


# ======================================================================================
# 8. Frontend and bundled Chromium discovery
# ======================================================================================

class FrontendDiscoveryTests(_IsolatedTestCase):
    def test_the_shell_and_assets_are_resolved_from_the_installation(self) -> None:
        bundle = self.temp_root / "install"
        dist = bundle / "frontend" / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<div id=\"root\"></div>", encoding="utf-8")
        (dist / "assets" / "index-abc123.js").write_text("//", encoding="utf-8")

        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            resolved = frontend_dist_dir()
        self.assertEqual(resolved, dist)
        self.assertTrue((resolved / "index.html").is_file())
        self.assertTrue(any((resolved / "assets").glob("*.js")))

    def test_the_frontend_is_not_looked_for_in_the_working_directory(self) -> None:
        bundle = self.temp_root / "install"
        bundle.mkdir()
        original = Path.cwd()
        os.chdir(self.temp_root)
        try:
            with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
                self.assertEqual(frontend_dist_dir(), bundle / "frontend" / "dist")
        finally:
            os.chdir(original)


class BundledBrowserDiscoveryTests(_IsolatedTestCase):
    def bundle_with_chromium(self) -> Path:
        bundle = self.temp_root / "install"
        chrome = bundle / "ms-playwright" / "chromium-1148" / "chrome-win" / "chrome.exe"
        chrome.parent.mkdir(parents=True)
        chrome.write_bytes(b"")
        return bundle

    def test_the_bundled_browser_directory_is_inside_the_installation(self) -> None:
        bundle = self.bundle_with_chromium()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            self.assertEqual(launcher.bundled_browsers_dir(), bundle / "ms-playwright")

    def test_the_chromium_executable_is_found(self) -> None:
        bundle = self.bundle_with_chromium()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            executable = launcher.chromium_executable()
        self.assertIsNotNone(executable)
        self.assertEqual(executable.name, "chrome.exe")
        self.assertTrue(str(executable).startswith(str(bundle)))

    def test_a_build_without_a_browser_reports_it_rather_than_guessing(self) -> None:
        bundle = self.temp_root / "no-browser"
        bundle.mkdir()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.object(paths_module, "is_packaged", return_value=True):
            self.assertIsNone(launcher.bundled_browsers_dir())
            self.assertIsNone(launcher.chromium_executable())

    def test_configuring_playwright_sets_the_bundled_path_for_child_processes(self) -> None:
        bundle = self.bundle_with_chromium()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.dict(os.environ, {launcher.BROWSER_ENV: ""}, clear=False):
            directory = launcher.configure_playwright_browsers()
            self.assertEqual(os.environ[launcher.BROWSER_ENV], str(directory))
            self.assertTrue(str(directory).startswith(str(bundle)))

    def test_the_global_cache_is_identified_so_it_can_be_ruled_out(self) -> None:
        cache = self.temp_root / "Local" / "ms-playwright"
        cache.mkdir(parents=True)
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp_root / "Local")}, clear=False):
            if os.name == "nt":
                self.assertEqual(launcher.global_browser_cache(), cache)

    def test_a_missing_global_cache_is_reported_as_none(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.temp_root / "absent")},
                        clear=False):
            if os.name == "nt":
                self.assertIsNone(launcher.global_browser_cache())

    # -- the bundled-browser smoke test ----------------------------------------------------

    def fake_playwright(self, executable: Path, record: dict):
        """A stand-in for playwright.sync_api that records how it was driven."""

        class FakePage:
            url = launcher.BLANK_PAGE

            def goto(self, url: str) -> None:
                record["goto"] = url

        class FakeContext:
            def new_page(self):
                return FakePage()

            def close(self) -> None:
                record["context_closed"] = True

        class FakeBrowser:
            version = "0.0-fake"

            def new_context(self, **kwargs):
                record["context_kwargs"] = kwargs
                return FakeContext()

            def close(self) -> None:
                record["browser_closed"] = True

        class FakeChromium:
            executable_path = str(executable)

            def launch(self, **kwargs):
                record["launch_kwargs"] = kwargs
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                record["playwright_closed"] = True
                return False

        module = type(sys)("playwright.sync_api")
        module.sync_playwright = lambda: FakePlaywright()  # type: ignore[attr-defined]
        return module

    def test_the_browser_check_launches_and_closes_the_bundled_chromium(self) -> None:
        bundle = self.bundle_with_chromium()
        chrome = bundle / "ms-playwright" / "chromium-1148" / "chrome-win" / "chrome.exe"
        record: dict = {}
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.dict(sys.modules,
                           {"playwright.sync_api": self.fake_playwright(chrome, record)}):
            problem = launcher.browser_check()

        self.assertIsNone(problem)
        self.assertTrue(record["browser_closed"], "the browser was left running")
        self.assertTrue(record["context_closed"])
        self.assertTrue(record["playwright_closed"])

    def test_the_browser_check_opens_a_blank_page_and_no_portal(self) -> None:
        """It must never reach LinkedIn or Dice, and never load a saved session: a fresh,
        empty context means the stored linkedin_state.json / dice_state.json are untouched."""
        bundle = self.bundle_with_chromium()
        chrome = bundle / "ms-playwright" / "chromium-1148" / "chrome-win" / "chrome.exe"
        record: dict = {}
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.dict(sys.modules,
                           {"playwright.sync_api": self.fake_playwright(chrome, record)}):
            launcher.browser_check()

        self.assertEqual(record["goto"], "about:blank")
        self.assertEqual(record["context_kwargs"], {},
                         "the smoke test must not pass storage_state")
        # Headed, so the binary launched is the one whose path was verified. A headless
        # launch would start a different bundled binary.
        self.assertFalse(record["launch_kwargs"].get("headless"))

    def test_chromium_writes_its_own_log_outside_the_installation(self) -> None:
        """Chromium drops debug.log next to its binary unless CHROME_LOG_FILE redirects it --
        a write into the program folder, which fails under Program Files."""
        bundle = self.bundle_with_chromium()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                self.data_dir_env(), \
                patch.dict(os.environ, {launcher.CHROME_LOG_ENV: ""}, clear=False):
            os.environ.pop(launcher.CHROME_LOG_ENV, None)
            reset_runtime_paths_cache()
            target = launcher.redirect_chromium_log()

        self.assertEqual(target.name, launcher.CHROME_LOG_FILENAME)
        self.assertFalse(str(target).startswith(str(bundle)))
        self.assertTrue(str(target).startswith(str(self.temp_root)))

    def test_an_explicit_chromium_log_location_is_respected(self) -> None:
        chosen = self.temp_root / "chosen" / "chromium.log"
        with self.data_dir_env(), \
                patch.dict(os.environ, {launcher.CHROME_LOG_ENV: str(chosen)}, clear=False):
            reset_runtime_paths_cache()
            self.assertEqual(launcher.redirect_chromium_log(), chosen)

    def test_configuring_playwright_also_redirects_the_chromium_log(self) -> None:
        bundle = self.bundle_with_chromium()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                self.data_dir_env(), \
                patch.dict(os.environ, {launcher.BROWSER_ENV: ""}, clear=False):
            os.environ.pop(launcher.CHROME_LOG_ENV, None)
            reset_runtime_paths_cache()
            launcher.configure_playwright_browsers()
            configured = os.environ[launcher.CHROME_LOG_ENV]
        self.assertFalse(configured.startswith(str(bundle)))

    def test_a_browser_resolved_outside_the_installation_is_rejected(self) -> None:
        """The guard that proves the developer's global cache was not used."""
        bundle = self.bundle_with_chromium()
        elsewhere = self.temp_root / "Local" / "ms-playwright" / "chromium-1148" / "chrome.exe"
        elsewhere.parent.mkdir(parents=True)
        elsewhere.write_bytes(b"")
        record: dict = {}
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.dict(sys.modules,
                           {"playwright.sync_api": self.fake_playwright(elsewhere, record)}):
            problem = launcher.browser_check()

        self.assertIsNotNone(problem)
        self.assertEqual(problem.title, "Wrong browser selected")
        self.assertNotIn("launch_kwargs", record, "a foreign browser must not be started")

    def test_a_build_with_no_browser_reports_it_instead_of_falling_back(self) -> None:
        bundle = self.temp_root / "browserless"
        bundle.mkdir()
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True), \
                patch.object(paths_module, "is_packaged", return_value=True):
            problem = launcher.browser_check()
        self.assertIsNotNone(problem)
        self.assertEqual(problem.title, "No bundled browser")


# ======================================================================================
# 9. Nothing private reaches the build
# ======================================================================================

class BuildSecretScannerTests(_IsolatedTestCase):
    """The scanner is a mandatory build step (build_windows.ps1 exits 7 on any finding), so
    its own detection has to be trustworthy."""

    def build_tree(self) -> Path:
        root = self.temp_root / "dist"
        (root / "_internal").mkdir(parents=True)
        (root / "_internal" / "app.pyc").write_bytes(b"\x00")
        return root

    def test_a_clean_tree_passes(self) -> None:
        self.assertTrue(scan(self.build_tree()).clean)

    def test_each_forbidden_file_is_detected(self) -> None:
        for name in (".env", "database_credential.dat", "linkedin_state.json",
                     "dice_state.json", "backup.dump", "local.sqlite", "server.key"):
            with self.subTest(name=name):
                root = self.build_tree()
                (root / name).write_text("x", encoding="utf-8")
                report = scan(root)
                self.assertFalse(report.clean)
                self.assertIn(name, " ".join(report.forbidden_files))
                shutil.rmtree(root)

    def test_user_data_directories_are_detected(self) -> None:
        for directory in ("outputs", "work", "uploads", "storage", "browser_sessions"):
            with self.subTest(directory=directory):
                root = self.build_tree()
                (root / directory).mkdir()
                (root / directory / "leaked.txt").write_text("x", encoding="utf-8")
                self.assertFalse(scan(root).clean)
                shutil.rmtree(root)

    def test_credential_shaped_content_is_detected(self) -> None:
        samples = {
            "url.py": 'URL = "postgresql+psycopg://app:hunter2hunter2@localhost:5432/db"',
            "key.py": 'KEY = "sk-abcdefghijklmnopqrstuvwxyz0123456789"',
            "aws.py": 'ID = "AKIAIOSFODNN7EXAMPLE"',
            "pem.pem": "-----BEGIN RSA PRIVATE KEY-----\nabc\n",
            "run.ps1": "PGPASSWORD=realsecretvalue",
        }
        for name, content in samples.items():
            with self.subTest(name=name):
                root = self.build_tree()
                (root / name).write_text(content, encoding="utf-8")
                self.assertFalse(scan(root).clean, f"{name} was not detected")
                shutil.rmtree(root)

    def test_the_public_certificate_bundle_is_not_a_false_positive(self) -> None:
        """certifi ships cacert.pem, which outbound HTTPS genuinely needs."""
        root = self.build_tree()
        bundle = root / "_internal" / "certifi"
        bundle.mkdir(parents=True)
        (bundle / "cacert.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n", encoding="utf-8"
        )
        self.assertTrue(scan(root).clean)

    def test_documentation_placeholders_are_not_false_positives(self) -> None:
        root = self.build_tree()
        (root / "README.md").write_text(
            "Set DATABASE_URL=postgresql://user:yourpassword@localhost/db", encoding="utf-8"
        )
        self.assertTrue(scan(root).clean)

    def test_a_library_module_called_logs_is_not_mistaken_for_our_log_directory(self) -> None:
        root = self.build_tree()
        nested = root / "_internal" / "somelib" / "logs"
        nested.mkdir(parents=True)
        (nested / "__init__.py").write_text("", encoding="utf-8")
        self.assertTrue(scan(root).clean)


@requires_build
class PackagedBuildContentTests(unittest.TestCase):
    """Assertions against the REAL build output, independent of the scanner."""

    def test_the_build_contains_no_secrets_or_user_data(self) -> None:
        report = scan(DIST_DIR)
        self.assertTrue(report.clean, report.render())

    def test_no_environment_file_credential_or_portal_session_was_packaged(self) -> None:
        forbidden = {".env", "database_credential.dat", "linkedin_state.json",
                     "dice_state.json", ".pgpass"}
        found = [str(p.relative_to(DIST_DIR)) for p in DIST_DIR.rglob("*")
                 if p.is_file() and p.name.lower() in forbidden]
        self.assertEqual(found, [])

    def test_no_resume_upload_log_or_database_dump_was_packaged(self) -> None:
        found = [str(p.relative_to(DIST_DIR)) for p in DIST_DIR.rglob("*")
                 if p.is_file() and p.suffix.lower() in {".dump", ".bak", ".sqlite", ".db"}]
        self.assertEqual(found, [])
        self.assertFalse((DIST_INTERNAL / "outputs").exists())
        self.assertFalse((DIST_INTERNAL / "work").exists())
        self.assertFalse((DIST_INTERNAL / "storage").exists())

    def test_the_executable_and_every_bundled_resource_is_present(self) -> None:
        for relative in ("RisingPathRecruiters.exe",
                         "_internal/frontend/dist/index.html",
                         "_internal/frontend/dist/assets",
                         "_internal/alembic.ini",
                         "_internal/alembic/versions"):
            with self.subTest(resource=relative):
                self.assertTrue((DIST_DIR / relative).exists(), f"missing {relative}")

    def test_the_bundled_chromium_is_present(self) -> None:
        found = list((DIST_INTERNAL / "ms-playwright").rglob("chrome.exe"))
        self.assertTrue(found, "no bundled chrome.exe in the build")
        self.assertTrue(str(found[0]).startswith(str(DIST_DIR)))

    def test_the_baseline_revision_shipped(self) -> None:
        from app.schema_verification import BASELINE_REVISION

        versions = list((DIST_INTERNAL / "alembic" / "versions").glob("*.py"))
        self.assertTrue(versions)
        self.assertTrue(any(BASELINE_REVISION in read(p) for p in versions))

    def test_no_python_interpreter_or_node_runtime_is_required(self) -> None:
        self.assertFalse((DIST_DIR / "python.exe").exists())
        self.assertFalse((DIST_INTERNAL / "node.exe").exists())
        self.assertFalse((DIST_INTERNAL / "frontend" / "node_modules").exists())


# ======================================================================================
# 10. The environment is left alone, and no message can carry a password
# ======================================================================================

class EnvironmentIsolationTests(_IsolatedTestCase):
    """A local application has no business editing the machine's PATH."""

    def test_starting_up_does_not_change_path(self) -> None:
        before = os.environ.get("PATH", "")
        bundle = self.temp_root / "install"
        (bundle / "ms-playwright").mkdir(parents=True)
        with patch.object(paths_module.sys, "_MEIPASS", str(bundle), create=True):
            launcher.configure_playwright_browsers()
            launcher.bundled_browsers_dir()
        self.assertEqual(os.environ.get("PATH", ""), before)

    def test_no_application_module_edits_the_persistent_environment(self) -> None:
        """Reading the registry is fine -- app/postgres_detection.py locates PostgreSQL that
        way. WRITING to it, or to the machine PATH, is not."""
        writes = ("setx ", "SetEnvironmentVariable", "winreg.SetValue", "winreg.CreateKey",
                  "winreg.DeleteKey", "winreg.DeleteValue")
        offenders = []
        for path in (ROOT / "app").rglob("*.py"):
            text = read(path)
            offenders += [f"{path.relative_to(ROOT)}: {marker}"
                          for marker in writes if marker in text]
        self.assertEqual(offenders, [])

    def test_the_build_and_test_scripts_do_not_edit_the_machine_environment(self) -> None:
        for script in (BUILD_SCRIPT, PACKAGED_TEST_SCRIPT):
            with self.subTest(script=script.name):
                text = read(script)
                self.assertNotIn("setx", text)
                self.assertNotIn("SetEnvironmentVariable", text)

    def test_the_browser_variable_is_scoped_to_this_process(self) -> None:
        """build_windows.ps1 sets PLAYWRIGHT_BROWSERS_PATH for the staging step only."""
        text = read(BUILD_SCRIPT)
        self.assertIn("$env:PLAYWRIGHT_BROWSERS_PATH = $stagedBrowsers", text)
        self.assertIn("Remove-Item Env:\\PLAYWRIGHT_BROWSERS_PATH", text)


class SecretRedactionTests(_LoggingPreservingTestCase):
    PASSWORD = "an-actual-database-password-9f2"
    URL = f"postgresql+psycopg://app_role:{PASSWORD}@localhost:5432/linkedin_auto_apply"

    def test_an_error_dialog_redacts_the_values_it_is_given(self) -> None:
        from io import StringIO

        problem = StartupProblem(ExitCode.STARTUP_FAILED, "Failed",
                                 f"connection to {self.URL} refused")
        launcher.set_interactive(False)
        self.addCleanup(launcher.set_interactive, True)
        buffer = StringIO()
        with patch.object(launcher.sys, "stderr", buffer):
            launcher.show_error_dialog(problem, secrets=(self.PASSWORD,))
        printed = buffer.getvalue()
        self.assertNotIn(self.PASSWORD, printed)
        self.assertIn("***", printed)

    def test_a_url_is_safe_to_log(self) -> None:
        from app.db_credentials import redact_url

        safe = redact_url(self.URL)
        self.assertNotIn(self.PASSWORD, safe)
        self.assertNotIn("%2A", safe)
        self.assertIn("linkedin_auto_apply", safe)

    def test_a_failed_migration_reports_the_error_type_and_no_credential(self) -> None:
        import app.config as config_module
        import app.schema_verification as schema_module

        class FakeSettings:
            database_url = self.URL

        def explode(url=None):
            raise RuntimeError(f"could not connect using {self.URL}")

        # Deliberately NOT wrapped in assertLogs: that swaps the root handlers for its own,
        # so nothing would reach the real log file -- and the file is exactly what this test
        # is about. The failure is logged with its traceback, and that traceback contains the
        # connection URL; the redacting formatter on the file handler is what removes it.
        with self.data_dir_env():
            reset_runtime_paths_cache()
            log_file = launcher.configure_logging()
            with patch.object(config_module, "get_settings", return_value=FakeSettings()), \
                    patch.object(schema_module, "upgrade_to_head", side_effect=explode):
                problem = launcher.run_migrations()
            for handler in logging.getLogger().handlers:
                handler.flush()
            written = read(log_file)

        self.assertIsNotNone(problem)
        self.assertEqual(problem.code, ExitCode.MIGRATION_FAILED)
        self.assertNotIn(self.PASSWORD, problem.as_dialog())
        self.assertIn("RuntimeError", problem.message)
        self.assertIn("Migration failed", written)
        self.assertIn("RuntimeError", written, "the traceback was not recorded at all")
        self.assertNotIn(self.PASSWORD, written)

    def test_no_startup_message_embeds_a_connection_url(self) -> None:
        """Every message the launcher can show is a fixed instruction, not driver output."""
        source = read(ROOT / "app" / "windows_launcher.py")
        block = source[source.index("def classify_startup_problem"):source.index("def run_migrations")]
        for leak in ("str(error)", "{url}", "database_url"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, block)

    def test_the_launcher_never_prints_the_resolved_url(self) -> None:
        source = read(ROOT / "app" / "windows_launcher.py")
        self.assertNotIn("logger.info(\"Database URL", source)
        # Redaction now happens centrally in app/bootstrap_log.py, so it covers every log
        # line and every dialog rather than only the places that remembered to ask for it.
        self.assertIn("from app.bootstrap_log import redact", source)


if __name__ == "__main__":
    unittest.main()
