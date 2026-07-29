"""The Windows installer: what it must do, and what it must never do.

The installer is the only part of this system that runs on a stranger's computer with
administrator rights, so the tests below are weighted towards the things that would be
expensive to get wrong: destroying data, publishing a secret, or writing a credential that
the person who will use the application cannot decrypt.

Grouped by the guarantee under test:

* **identity** -- one version source, one permanent AppId, so an upgrade is recognised as an
  upgrade rather than installed a second time alongside;
* **payload** -- the packaged application and nothing else: no source checkout, no ``.venv``,
  no ``node_modules``, no tests, no ``.env``, no credential, no portal session, no resume;
* **the PostgreSQL requirement** -- PostgreSQL 16 is installed BY HAND before setup runs;
  nothing here, and nothing in the product, downloads, bundles, verifies or installs it;
* **refusal before installation** -- setup stops before writing anything when PostgreSQL is
  absent, stopped, on the wrong major version, or not answering on port 5432;
* **secrets** -- the administrator password never reaches a command line, the temporary file
  that carries it is ACL-restricted and destroyed, and no message can contain it;
* **the elevation boundary** -- the DPAPI credential is written by the ORIGINAL user, not by
  whichever account approved the UAC prompt;
* **uninstall** -- removes the program, preserves everything the person cares about.

Nothing here installs PostgreSQL, runs a PostgreSQL installer, touches the real database,
or writes the real stored credential. Destructive paths use disposable databases and mocks.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import installer_bridge, postgres_install, version
from app.installer_bridge import (
    EXIT_ADMIN_AUTH_FAILED,
    EXIT_HANDOFF_FAILED,
    EXIT_NO_ADMIN_PASSWORD,
    EXIT_PORT_OCCUPIED,
    EXIT_POSTGRES_COMPATIBLE,
    EXIT_POSTGRES_MISSING,
    EXIT_POSTGRES_STOPPED,
    EXIT_POSTGRES_WRONG_VERSION,
    BridgeResult,
    authenticate_action,
    begin_handoff_action,
    cleanup_action,
    detect_action,
    provision_action,
    require_action,
)
from app.paths import DATA_DIR_ENV, project_root, reset_runtime_paths_cache
from app.postgres_detection import (
    DetectionReport,
    PostgresInstallation,
    ProbeResult,
    ServiceState,
)
from app.postgres_install import (
    ADMIN_PASSWORD_ENV,
    is_compatible,
    load_manifest,
    meets_required_major,
    minimum_supported_major,
    postgres_prerequisite,
    required_major,
    secure_delete,
)
from app import setup_handoff
from app.provisioning import Status

ROOT = project_root()
ISS_FILE = ROOT / "installer" / "RisingPathRecruiters.iss"
MANIFEST_FILE = ROOT / "installer" / "prerequisites.json"
BUILD_SCRIPT = ROOT / "scripts" / "build_installer.ps1"
TEST_SCRIPT = ROOT / "scripts" / "test_installer.ps1"
VERIFY_SCRIPT = ROOT / "scripts" / "verify_clean_machine.ps1"
PAYLOAD_DIR = ROOT / "dist" / "RisingPathRecruiters"
RELEASE_DIR = ROOT / "release"

WINDOWS = sys.platform == "win32"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def python_code(source: str) -> str:
    """Python source with its docstrings and comments removed.

    Needed for "this must never appear again" assertions: the modules that were fixed
    explain the defect they no longer contain, and quoting a broken line in a docstring must
    not read as the line coming back."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0]))
    kept = []
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        kept.append((line_number, line))
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and id(node) in docstrings:
            spans.append((node.lineno, node.end_lineno))
    return "\n".join(line for number, line in kept
                     if not any(start <= number <= end for start, end in spans))


def powershell_code(script: str) -> str:
    """A PowerShell script with its help block and comments removed.

    Same reason as :func:`python_code`: the verifier documents the WOW64 redirection it was
    fixed to avoid, and naming the thing in a comment must not read as still doing it."""
    without_help = re.sub(r"<#.*?#>", "", script, flags=re.DOTALL)
    return "\n".join(
        re.sub(r"(?<!`)#.*$", "", line) if line.strip().startswith("#") else line
        for line in without_help.splitlines()
    )


def strip_comments(iss: str) -> str:
    """An Inno Setup script with its comments removed.

    Needed because almost every assertion below is of the form "this must not appear", and
    the file's own commentary legitimately discusses the things being forbidden -- it
    explains that unrelated ``python.exe`` processes must never be touched, and that no
    ``.venv`` is referenced. Matching raw text would fail on the explanation rather than on
    the code.

    Pascal ``{ ... }`` comments are removed first. Inno constants (``{app}``, ``{tmp}``,
    ``{#Name}``) are never followed by whitespace, so the ``\\{\\s`` anchor cannot eat one."""
    without_blocks = re.sub(r"\{\s[^{}]*?\}", " ", iss, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines()
        if not line.strip().startswith((";", "//"))
    )


def inno_section(iss: str, name: str) -> str:
    """The body of one ``[Section]``, so a check aimed at one section cannot match another."""
    match = re.search(
        rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[[A-Za-z]+\]\s*$|\Z)",
        iss, re.MULTILINE | re.DOTALL,
    )
    return match.group(1) if match else ""


class _InstallerTestCase(unittest.TestCase):
    """An isolated data root, so no test can touch the real credential or work directory."""

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="installer-test-"))
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.addCleanup(reset_runtime_paths_cache)
        self._env = patch.dict(os.environ, {DATA_DIR_ENV: str(self.temp_root / "data")},
                               clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        reset_runtime_paths_cache()


def report(*, installed=False, running=False, is_postgres=False, reachable=False,
           major: int | None = None, port: int = 5432) -> DetectionReport:
    """A synthetic detection result, so every machine state can be tested on any machine."""
    installations = []
    if major is not None:
        installations.append(PostgresInstallation(
            version=f"{major}.4", base_directory=rf"C:\Program Files\PostgreSQL\{major}",
            data_directory="", service_name=f"postgresql-x64-{major}", port=port,
        ))
    return DetectionReport(
        installations=installations,
        services=[ServiceState(name=f"postgresql-x64-{major}",
                               state="RUNNING" if running else "STOPPED")]
        if installed or major is not None else [],
        probe=ProbeResult(port=port, reachable=reachable or is_postgres, is_postgres=is_postgres),
    )


# ======================================================================================
# Identity and version
# ======================================================================================

class VersionSourceTests(unittest.TestCase):
    def test_the_version_is_a_three_part_semantic_version(self) -> None:
        self.assertRegex(version.VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_windows_file_version_has_four_components(self) -> None:
        self.assertEqual(len(version.VERSION_TUPLE), 4)
        self.assertRegex(version.FILE_VERSION, r"^\d+\.\d+\.\d+\.\d+$")
        self.assertTrue(version.FILE_VERSION.startswith(version.VERSION))

    def test_the_app_id_is_a_guid_and_is_not_the_version(self) -> None:
        self.assertRegex(
            version.APP_ID,
            r"^\{[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}\}$",
        )
        self.assertNotIn(version.VERSION, version.APP_ID)

    def test_the_inno_form_of_the_app_id_escapes_the_leading_brace(self) -> None:
        """Inno reads a single '{' as the start of a constant, so it has to be doubled."""
        self.assertTrue(version.inno_app_id().startswith("{{"))
        self.assertEqual(version.inno_app_id()[1:], version.APP_ID)

    def test_the_installer_filename_is_deterministic(self) -> None:
        self.assertEqual(version.installer_filename(),
                         f"RisingPathRecruiters-Setup-{version.VERSION}.exe")
        self.assertEqual(version.installer_filename(), version.installer_filename())

    def test_the_data_directory_name_matches_the_runtime_resolver(self) -> None:
        """A mismatch here would install shortcuts pointing at data the app never writes."""
        from app.paths import APP_NAME as RUNTIME_DATA_DIR

        self.assertEqual(version.DATA_DIR_NAME, RUNTIME_DATA_DIR)

    def test_describe_exposes_every_value_the_build_needs_and_no_secret(self) -> None:
        described = version.describe()
        for key in ("version", "file_version", "app_name", "publisher", "app_id",
                    "inno_app_id", "install_dir_name", "installer_filename"):
            with self.subTest(key=key):
                self.assertTrue(described[key])
        blob = " ".join(described.values()).lower()
        for secret in ("password", "postgresql://", "sk-"):
            self.assertNotIn(secret, blob)

    def test_no_other_file_carries_an_editable_copy_of_the_version(self) -> None:
        """One source of truth: the .iss and the build script must read it, not restate it."""
        iss = read(ISS_FILE)
        # The #define default is the only literal, and it exists so the file can be opened
        # standalone; the build always overrides it with /DAppVersion.
        defaults = re.findall(r'#define AppVersion "([^"]+)"', iss)
        self.assertEqual(defaults, [version.VERSION],
                         "the .iss fallback version drifted from app/version.py")
        self.assertIn("/DAppVersion=$($version.version)", read(BUILD_SCRIPT))


# ======================================================================================
# The Inno Setup source
# ======================================================================================

class InnoSetupSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = read(ISS_FILE)

    def test_installs_per_machine_into_64_bit_program_files(self) -> None:
        self.assertIn("DefaultDirName={autopf}\\{#InstallDirName}", self.iss)
        self.assertIn("ArchitecturesInstallIn64BitMode=x64compatible", self.iss)
        self.assertIn("ArchitecturesAllowed=x64compatible", self.iss)

    def test_requires_administrator_only_because_program_files_needs_it(self) -> None:
        self.assertIn("PrivilegesRequired=admin", self.iss)
        # ...and every step that does not need it is explicitly delegated back.
        self.assertIn("ExecAsOriginalUser", self.iss)

    def test_uses_a_stable_app_id_so_upgrades_replace_rather_than_duplicate(self) -> None:
        self.assertIn("AppId={#AppIdValue}", self.iss)
        self.assertIn(version.APP_ID.strip("{}"), self.iss)
        self.assertIn("UsePreviousAppDir=yes", self.iss)

    def test_uses_the_modern_wizard(self) -> None:
        self.assertIn("WizardStyle=modern", self.iss)

    def test_declares_uninstall_metadata(self) -> None:
        for setting in ("UninstallDisplayName=", "UninstallDisplayIcon=", "AppPublisher=",
                        "VersionInfoVersion=", "VersionInfoCompany="):
            with self.subTest(setting=setting):
                self.assertIn(setting, self.iss)

    def test_output_goes_to_the_guarded_release_directory_with_a_deterministic_name(self) -> None:
        self.assertIn("OutputDir={#OutputDir}", self.iss)
        self.assertIn("OutputBaseFilename={#ExeName}-Setup-{#AppVersion}", self.iss)

    def test_compression_settings_are_stated_deliberately(self) -> None:
        self.assertIn("Compression=lzma2/max", self.iss)
        # Not solid: the payload is dominated by already-compressed binaries, and solid
        # compression on ~1 GB costs build time and decompression memory for little gain.
        self.assertIn("SolidCompression=no", self.iss)

    def test_the_payload_is_the_packaged_application_only(self) -> None:
        sources = re.findall(r'^Source: "([^"]+)"', self.iss, re.MULTILINE)
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(source.startswith("..\\app"), "source checkout referenced")
                for forbidden in (".venv", "node_modules", "tests", "outputs", "work",
                                  "storage", ".env"):
                    self.assertNotIn(forbidden, source)

    def test_the_payload_includes_the_executable_and_its_bundled_files(self) -> None:
        self.assertIn('Source: "{#PayloadDir}\\{#ExeName}.exe"', self.iss)
        self.assertIn('Source: "{#PayloadDir}\\_internal\\*"', self.iss)
        self.assertIn("recursesubdirs createallsubdirs", self.iss)

    def test_a_start_menu_shortcut_is_created_and_the_desktop_one_is_optional(self) -> None:
        self.assertIn('Name: "{group}\\{#AppName}"', self.iss)
        self.assertIn('Name: "{autodesktop}\\{#AppName}"', self.iss)
        self.assertIn("Tasks: desktopicon", self.iss)
        self.assertRegex(self.iss, r'Name: "desktopicon".*Flags: unchecked')

    def test_shortcuts_point_at_the_executable_never_at_a_script(self) -> None:
        targets = re.findall(r'^Name: "[^"]+"; Filename: "([^"]+)"', self.iss, re.MULTILINE)
        self.assertTrue(targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(target.endswith("{#ExeName}.exe"))
                self.assertNotIn(".ps1", target)
                self.assertNotIn("powershell", target.lower())

    def test_the_running_application_is_handled_before_upgrade_or_uninstall(self) -> None:
        self.assertIn("function InitializeSetup", self.iss)
        self.assertIn("function InitializeUninstall", self.iss)
        self.assertIn("AskUserToCloseApp", self.iss)
        # Narrow by design: only this application's own image name.
        self.assertIn('IMAGENAME eq {#ExeName}.exe', self.iss)

    def test_no_unrelated_process_is_ever_terminated(self) -> None:
        """Only this application's own image name may ever be named in process handling."""
        code = strip_comments(self.iss)
        for dangerous in ("taskkill", "Stop-Process", "python.exe", "chrome.exe",
                          "postgres.exe", "node.exe"):
            with self.subTest(pattern=dangerous):
                self.assertNotIn(dangerous, code)

    def test_no_postgresql_installer_is_embedded(self) -> None:
        """The deployment model changed: PostgreSQL is installed by hand, before setup.

        Everything that downloaded, embedded, unpacked or ran a PostgreSQL installer is
        gone -- not disabled behind a flag, gone -- and this is the test that keeps it
        gone. A payload that carries a 334 MB database installer nobody runs is the
        cheapest possible mistake to make and the hardest to notice."""
        code = strip_comments(self.iss)
        for removed in ("InstallPostgres", "NeedsPostgresInstall", "prepare-postgres",
                        "ExtractTemporaryFile", "dontcopy", "IncludePostgres",
                        "PostgresInstaller", "--optionfile"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, code)
        self.assertNotIn("postgresql-", inno_section(self.iss, "Files").lower())

    def test_the_only_embedded_files_are_the_packaged_application(self) -> None:
        sources = re.findall(r'Source: "([^"]+)"', inno_section(self.iss, "Files"))
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertTrue(source.startswith("{#PayloadDir}"), source)

    def test_no_password_ever_appears_on_a_command_line(self) -> None:
        self.assertNotIn("superpassword", self.iss)
        # The one secret this installer handles reaches the other process as DPAPI
        # ciphertext in a file, never as an argument to anything.
        for pattern in ("--password", "PGPASSWORD", "psql -U", "-W "):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.iss)

    def test_the_exit_codes_match_the_bridge(self) -> None:
        """A drifted constant would map a real failure onto the wrong message."""
        expected = {
            "EXIT_POSTGRES_MISSING": EXIT_POSTGRES_MISSING,
            "EXIT_PORT_OCCUPIED": EXIT_PORT_OCCUPIED,
            "EXIT_POSTGRES_STOPPED": EXIT_POSTGRES_STOPPED,
            "EXIT_NO_ADMIN_PASSWORD": EXIT_NO_ADMIN_PASSWORD,
            "EXIT_MIGRATION_FAILED": int(Status.MIGRATION_FAILED),
            "EXIT_CREDENTIAL_FAILED": int(Status.CREDENTIAL_STORE_FAILED),
        }
        for name, value in expected.items():
            with self.subTest(constant=name):
                self.assertRegex(self.iss, rf"{name}\s*=\s*{value};")


class PreInstallGateTests(unittest.TestCase):
    """Nothing may be installed onto a computer that cannot run this application.

    The old flow installed the application first and discovered the database problem
    afterwards, which is how a clean machine ended up with a program it could not use and a
    message about the wrong thing. The gate now runs in InitializeSetup -- before the wizard
    is even shown -- and refuses outright."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = read(ISS_FILE)
        cls.code = strip_comments(cls.iss)
        cls.gate = cls.code[cls.code.index("function RequirePostgres"):
                            cls.code.index("function ProtectToHex")]

    def test_the_gate_runs_before_anything_is_installed(self) -> None:
        setup = self.code[self.code.index("function InitializeSetup"):]
        setup = setup[:setup.index("function InitializeUninstall")]
        self.assertIn("RequirePostgres", setup)
        # ...and the database work is the LAST step, after the files are copied.
        self.assertLess(self.code.index("function InitializeSetup"),
                        self.code.index("procedure CurStepChanged"))

    def test_the_gate_checks_all_four_required_conditions(self) -> None:
        for condition in ("PostgresMajor = 0", "PostgresMajor < ", "ServiceIsRunning",
                          "ServerIsAccepting"):
            with self.subTest(condition=condition):
                self.assertIn(condition, self.gate)

    def test_the_version_is_read_from_the_64_bit_registry_view(self) -> None:
        """Setup is a 32-bit process, so plain HKLM would be redirected to WOW6432Node and
        a 64-bit PostgreSQL would look as if it were not installed at all."""
        self.assertIn("HKLM64", self.code)
        self.assertNotIn("RegQueryStringValue(HKLM,", self.code)

    def test_reachability_is_proven_by_pg_isready_not_assumed(self) -> None:
        """A running service is not a reachable server. The old flow treated a zero exit
        code as proof and reported the wrong failure when it was not."""
        self.assertIn("pg_isready.exe", self.gate + self.code)

    def test_every_refusal_tells_the_person_exactly_what_to_do(self) -> None:
        instructions = self.code[self.code.index("function PostgresInstructions"):
                                 self.code.index("function ReadInstallationValue")]
        for expected in ("PostgreSQL {#RequiredPostgresMajor}", "x86-64",
                         "{#PostgresPort}", "run this setup again"):
            with self.subTest(expected=expected):
                self.assertIn(expected, instructions)

    def test_a_stopped_service_names_the_service_to_start(self) -> None:
        self.assertIn("Open Services, start", self.gate)

    def test_every_gate_decision_is_logged(self) -> None:
        self.assertGreaterEqual(self.gate.count("Log("), 4,
                                "a gate that fails silently cannot be diagnosed remotely")


class UninstallPreservationTests(unittest.TestCase):
    """Uninstalling must never be the reason someone loses their application history."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = read(ISS_FILE)

    def test_uninstall_removes_only_the_program_directory(self) -> None:
        entries = re.findall(r"Name: \"([^\"]+)\"", inno_section(self.iss, "UninstallDelete"))
        self.assertTrue(entries)
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertTrue(entry.startswith("{app}"), f"{entry} is outside the program dir")

    def test_uninstall_never_touches_the_user_data_directory(self) -> None:
        block = inno_section(self.iss, "UninstallDelete")
        for preserved in ("{localappdata}", "{userappdata}", "AIJobAutomation",
                          "browser_sessions", "uploads", "storage"):
            with self.subTest(preserved=preserved):
                self.assertNotIn(preserved, block)

    def test_no_destructive_database_command_exists_anywhere_in_the_installer(self) -> None:
        code = strip_comments(self.iss)
        for destructive in ("DROP DATABASE", "DROP ROLE", "dropdb", "dropuser",
                            "DROP SCHEMA", "TRUNCATE", "DELETE FROM"):
            with self.subTest(command=destructive):
                self.assertNotIn(destructive, code)

    def test_the_uninstaller_does_not_remove_postgresql(self) -> None:
        """The uninstall path names PostgreSQL nowhere: it is a shared prerequisite that
        other software on the computer may also depend on."""
        block = inno_section(self.iss, "UninstallDelete")
        self.assertNotIn("postgres", block.lower())
        uninstall_code = strip_comments(self.iss)
        start = uninstall_code.index("function InitializeUninstall")
        body = uninstall_code[start:uninstall_code.index("end;", start)]
        self.assertNotIn("postgres", body.lower())


# ======================================================================================
# The PostgreSQL requirement -- reference metadata only, nothing is downloaded
# ======================================================================================

class PostgresRequirementTests(unittest.TestCase):
    """``installer/prerequisites.json`` describes what the USER must install.

    It is not a download manifest, and the checksum/Authenticode tests that used to live here
    were deleted with the flow they guarded: nothing verifies a download any more, because
    nothing downloads anything. The build reads exactly one value from this file --
    ``required_postgres_major`` -- and setup uses it to refuse to install anything until a
    server of that major version is running."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest()

    def test_the_required_major_is_present_and_is_what_setup_enforces(self) -> None:
        self.assertEqual(required_major(self.manifest), 16)
        self.assertEqual(self.manifest["required_postgres_major"],
                         required_major(self.manifest))

    def test_the_required_major_is_at_least_the_minimum_supported_one(self) -> None:
        """The strict gate for a NEW installation can never be looser than the permissive one
        that governs a server the application is already running against."""
        self.assertGreaterEqual(required_major(self.manifest),
                                minimum_supported_major(self.manifest))

    def test_users_are_pointed_at_the_official_postgresql_download(self) -> None:
        url = self.manifest["required_postgres_download"]
        self.assertTrue(url.startswith("https://www.postgresql.org/"), url)

    def test_the_referenced_build_matches_the_required_major(self) -> None:
        """Reference information only -- but it must not name a version setup would reject."""
        reference = postgres_prerequisite(self.manifest)
        self.assertEqual(reference.major, required_major(self.manifest))
        self.assertRegex(reference.version, r"^\d+\.\d+-\d+$")
        self.assertTrue(reference.url.startswith("https://"))

    def test_the_manifest_does_not_claim_anything_is_downloaded_or_embedded(self) -> None:
        """Its comment block used to describe checksum and Authenticode trust anchors for a
        verification that no longer runs. Reading it as a promise would be misleading."""
        blob = read(MANIFEST_FILE)
        self.assertIn("never downloads, bundles, verifies or installs PostgreSQL", blob)
        self.assertIn("NOT a download manifest", blob)

    def test_the_manifest_carries_no_secret(self) -> None:
        blob = read(MANIFEST_FILE).lower()
        for secret in ("password", "pgpassword", "sk-", "postgresql+psycopg://"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_no_script_prepares_or_verifies_a_postgresql_download(self) -> None:
        """The download-and-pin script was deleted with the flow it served. Nothing may
        reference it, and no build may reintroduce a bundled prerequisite."""
        self.assertFalse((ROOT / "scripts" / "prepare_installer_prerequisites.ps1").exists(),
                         "the orphaned prerequisite-download script is back")
        for name, script in (("build", read(BUILD_SCRIPT)), ("test", read(TEST_SCRIPT))):
            with self.subTest(script=name):
                self.assertNotIn("prepare_installer_prerequisites", script)
                self.assertNotIn("SkipPostgres", script)
        self.assertIn("required_postgres_major", read(BUILD_SCRIPT))

    def test_no_downloaded_installer_is_referenced_by_the_build(self) -> None:
        build = read(BUILD_SCRIPT)
        self.assertNotIn("manifest.postgresql", build)
        self.assertIn("postgresBinaries", build)


# ======================================================================================
# The PostgreSQL installation command -- constructed, never executed
# ======================================================================================

class RequiredVersionTests(_InstallerTestCase):
    """Two different questions, deliberately answered by two different functions.

    ``meets_required_major`` governs a NEW installation and is strict. ``is_compatible``
    governs a server this application is ALREADY running against and is permissive -- this
    deployment change must not break a computer that is working today."""

    def test_the_required_major_is_the_one_the_model_is_tested_on(self) -> None:
        self.assertEqual(required_major(), 16)

    def test_nothing_installed_is_never_acceptable(self) -> None:
        self.assertFalse(meets_required_major(None))

    def test_an_older_major_is_refused(self) -> None:
        for major in (9, 12, 13, 15):
            with self.subTest(major=major):
                self.assertFalse(meets_required_major(major))

    def test_the_required_major_and_newer_are_accepted(self) -> None:
        """Refusing 17 would send somebody to uninstall a working database server."""
        for major in (16, 17, 18):
            with self.subTest(major=major):
                self.assertTrue(meets_required_major(major))

    def test_an_existing_older_installation_is_still_supported_at_runtime(self) -> None:
        self.assertTrue(is_compatible(13))
        self.assertFalse(meets_required_major(13))

    def test_the_download_the_user_is_pointed_at_is_the_official_one(self) -> None:
        from app.postgres_install import required_download_url

        self.assertTrue(required_download_url().startswith("https://www.postgresql.org/"))


class AccountIdentificationTests(_InstallerTestCase):
    """Regression tests for the clean-machine failure on PC10.

    The option file was locked to the account named by ``%USERNAME%``. On that computer the
    user was called ``PC10`` and so was the machine, so the bare name resolved to the local
    SAM domain rather than to the user -- ``icacls`` succeeded, and the process that had just
    created the file could no longer write to it. PostgreSQL was never installed."""

    def test_the_grant_never_names_an_account(self) -> None:
        from app.windows_acl import restriction_command

        command = restriction_command(Path(r"C:\x\options"), "S-1-5-21-1-2-3-1001")
        granted = [command[index + 1] for index, item in enumerate(command)
                   if item == "/grant:r"]
        self.assertEqual(len(granted), 3)
        for grant in granted:
            with self.subTest(grant=grant):
                # A leading '*' is what makes icacls treat the value as a literal SID.
                self.assertRegex(grant, r"^\*S-1-[\d-]+:\(F\)$")

    def test_the_elevated_installer_can_still_read_the_file(self) -> None:
        """SYSTEM and Administrators must stay: the PostgreSQL installer runs elevated, and
        may be running as a different administrator than the user who started setup."""
        from app.windows_acl import ADMINISTRATORS_SID, SYSTEM_SID, restriction_command

        command = " ".join(restriction_command(Path(r"C:\x\options"), "S-1-5-21-1-2-3-1001"))
        self.assertIn(f"*{SYSTEM_SID}:(F)", command)
        self.assertIn(f"*{ADMINISTRATORS_SID}:(F)", command)

    def test_no_acl_code_reads_the_username_environment_variable(self) -> None:
        """The exact defect. Both copies of it read ``%USERNAME%``; neither may again.

        Docstrings are stripped first: ``app/windows_acl.py`` quotes the broken line in the
        explanation of why it is broken, and that must not read as a reoccurrence."""
        for module in ("windows_acl.py", "postgres_install.py", "runtime_state.py"):
            with self.subTest(module=module):
                code = python_code(read(ROOT / "app" / module))
                self.assertNotIn('environ.get("USERNAME")', code)
                self.assertNotIn("environ.get('USERNAME')", code)
                self.assertNotIn('environ["USERNAME"]', code)

    @unittest.skipUnless(WINDOWS, "SIDs are a Windows concept")
    def test_the_current_user_sid_is_read_from_the_process_token(self) -> None:
        from app.windows_acl import current_user_sid

        sid = current_user_sid()
        # S-1-5-21-<domain>-<rid>: a real account, not a domain or a well-known group.
        self.assertRegex(sid, r"^S-1-5-21-\d+-\d+-\d+-\d+$")

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_a_username_equal_to_the_computer_name_still_leaves_the_file_usable(self) -> None:
        """The PC10 reproduction. Before the fix this raised PermissionError on the write."""
        target = self.temp_root / "options"
        target.parent.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"USERNAME": os.environ.get("COMPUTERNAME", "PC10")},
                        clear=False):
            setup_handoff.write_channel("secret-value", target)
            self.assertEqual(setup_handoff.read_channel(target), "secret-value")

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_a_restricted_file_stays_readable_and_writable_by_its_creator(self) -> None:
        from app.windows_acl import restrict_to_current_user

        target = self.temp_root / "token"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        restrict_to_current_user(target)
        target.write_text("value-written-after-the-acl", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "value-written-after-the-acl")

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_a_restriction_that_locks_us_out_is_reported_and_undone(self) -> None:
        """A silent lockout surfaces far from its cause. It must fail here, loudly, while
        the file is still empty -- and leave a usable file behind."""
        from app import windows_acl

        target = self.temp_root / "locked"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        with patch.object(windows_acl, "_still_usable", return_value=False):
            with self.assertRaises(windows_acl.AccessRestrictionError):
                windows_acl.restrict_to_current_user(target)
        target.write_text("usable again", encoding="utf-8")
        self.assertEqual(target.read_text(encoding="utf-8"), "usable again")

    def test_an_unknown_sid_leaves_the_file_alone_rather_than_locking_it(self) -> None:
        from app import windows_acl

        target = self.temp_root / "unknown"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        with patch.object(windows_acl, "current_user_sid", return_value=""), \
                patch.object(windows_acl.os, "name", "nt"), \
                patch.object(windows_acl.subprocess, "run") as run:
            with self.assertRaises(windows_acl.AccessRestrictionError):
                windows_acl.restrict_to_current_user(target)
        run.assert_not_called()
        target.write_text("still writable", encoding="utf-8")

    def test_a_failed_restriction_never_leaves_a_handoff_behind(self) -> None:
        from app import windows_acl

        target = self.temp_root / "handoff.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        with patch.object(windows_acl, "restrict_to_current_user",
                          side_effect=windows_acl.AccessRestrictionError("denied")):
            with self.assertRaises(setup_handoff.HandoffError):
                setup_handoff.write_channel("secret-value", target)
        self.assertFalse(target.exists(), "an empty handoff was left behind")


class HandoffProtectionTests(_InstallerTestCase):
    """The channel that carries the administrator password from the wizard to provisioning.

    Requirement, in the user's words: the password must not appear in command-line
    arguments, environment variables, logs, message boxes, PowerShell history, plain-text
    temporary files or broadly readable files. What is left is to encrypt it before it ever
    reaches the filesystem, which is what this is."""

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_the_handoff_round_trips(self) -> None:
        target = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("P@ssw0rd-with-symbols-#1", target)
        self.assertEqual(setup_handoff.read_channel(target), "P@ssw0rd-with-symbols-#1")

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_the_file_never_contains_the_plain_text(self) -> None:
        target = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("admin-secret-value", target)
        blob = target.read_bytes()
        self.assertNotIn(b"admin-secret-value", blob)
        self.assertNotIn("admin-secret-value".encode("utf-16-le"), blob)

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_the_file_is_hexadecimal_ciphertext_and_nothing_else(self) -> None:
        """Hex rather than raw bytes only so the installer can write it with
        SaveStringToFile; what it holds is a DPAPI blob either way."""
        target = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("x", target)
        self.assertRegex(target.read_text(encoding="ascii"), r"^[0-9a-f]+$")

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_the_empty_channel_is_locked_down_before_anything_is_written_to_it(self) -> None:
        """Secured while still empty: tightening the ACL afterwards would leave a window in
        which any user on the computer could read it."""
        source = read(ROOT / "app" / "setup_handoff.py")
        body = source[source.index("def create_channel"):source.index("def write_channel")]
        self.assertLess(body.index("write_bytes(b"),
                        body.index("restrict_to_current_user(target)"))

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_other_standard_users_cannot_read_the_channel(self) -> None:
        target = self.temp_root / "handoff.bin"
        setup_handoff.create_channel(target)
        access = setup_handoff.__dict__  # keep the import list honest
        from app.windows_acl import OPEN_GROUP_SIDS, describe_access

        described = describe_access(target)
        for open_group in OPEN_GROUP_SIDS:
            with self.subTest(sid=open_group):
                self.assertNotIn(open_group, described)
        self.assertTrue(access is not None)

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_destroying_the_channel_overwrites_and_removes_it(self) -> None:
        target = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("admin-secret-value", target)
        self.assertTrue(setup_handoff.destroy_channel(target))
        self.assertFalse(target.exists())
        self.assertEqual(setup_handoff.residue(target), "none")

    def test_destroying_a_channel_that_is_already_gone_is_not_an_error(self) -> None:
        self.assertFalse(setup_handoff.destroy_channel(self.temp_root / "absent.bin"))

    def test_a_corrupt_channel_is_reported_rather_than_raised_at_the_caller(self) -> None:
        target = self.temp_root / "handoff.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("not-hexadecimal", encoding="ascii")
        with self.assertRaises(setup_handoff.HandoffError):
            setup_handoff.read_channel(target)

    def test_a_blob_from_another_account_fails_with_an_instruction(self) -> None:
        """Simulated by corrupting the blob: DPAPI cannot tell us the difference, and the
        installer's answer is the same either way."""
        target = self.temp_root / "handoff.bin"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("00" * 64, encoding="ascii")
        with self.assertRaises(setup_handoff.HandoffError) as caught:
            setup_handoff.read_channel(target)
        self.assertIn("signed in as the account", str(caught.exception).lower()
                      .replace("run setup while ", ""))

    def test_the_handoff_lives_in_the_users_own_data_directory(self) -> None:
        path = setup_handoff.handoff_path()
        self.assertEqual(path.parent.name, "work")
        self.assertEqual(path.name, setup_handoff.HANDOFF_FILENAME)


class PostgresDetectionDecisionTests(_InstallerTestCase):
    """One decision table, covering every situation the requirements name."""

    def decide(self, detection: DetectionReport) -> BridgeResult:
        with patch("app.postgres_detection.detect", return_value=detection):
            return detect_action("localhost", 5432)

    def test_a_compatible_running_server_is_used_as_it_is(self) -> None:
        result = self.decide(report(installed=True, running=True, is_postgres=True, major=16))
        self.assertEqual(result.exit_code, EXIT_POSTGRES_COMPATIBLE)
        self.assertTrue(result.detail["compatible"])
        self.assertEqual(result.detail["major_version"], 16)

    def test_an_installed_but_stopped_server_is_reported_separately(self) -> None:
        result = self.decide(report(installed=True, running=False, is_postgres=False, major=16))
        self.assertEqual(result.exit_code, EXIT_POSTGRES_STOPPED)

    def test_no_postgresql_at_all_asks_for_the_prerequisite(self) -> None:
        self.assertEqual(self.decide(report()).exit_code, EXIT_POSTGRES_MISSING)

    def test_a_port_held_by_something_else_is_not_treated_as_postgresql(self) -> None:
        detection = report(reachable=True)
        detection.probe.is_postgres = False
        result = self.decide(detection)
        self.assertEqual(result.exit_code, EXIT_PORT_OCCUPIED)
        self.assertTrue(result.detail["port_occupied_by_other"])

    def test_a_port_conflict_never_triggers_a_postgresql_installation(self) -> None:
        detection = report(reachable=True)
        with patch("app.postgres_detection.detect", return_value=detection):
            self.assertNotEqual(detect_action("localhost", 5432).exit_code,
                                EXIT_POSTGRES_MISSING)

    def test_a_compatible_server_on_a_non_default_port_is_used_on_that_port(self) -> None:
        result = self.decide(report(installed=True, running=True, is_postgres=True,
                                    major=16, port=5433))
        self.assertEqual(result.exit_code, EXIT_POSTGRES_COMPATIBLE)
        self.assertEqual(result.detail["port"], 5433)

    def test_an_unsupported_old_version_is_not_silently_accepted(self) -> None:
        result = self.decide(report(installed=True, running=True, is_postgres=True, major=9))
        self.assertEqual(result.exit_code, EXIT_POSTGRES_MISSING)
        self.assertFalse(result.detail["compatible"])

    def test_compatibility_is_decided_by_the_manifest_not_by_a_literal(self) -> None:
        minimum = load_manifest()["minimum_supported_postgres_major"]
        self.assertTrue(is_compatible(minimum))
        self.assertFalse(is_compatible(minimum - 1))
        self.assertFalse(is_compatible(None))

    def test_detection_reports_no_secret(self) -> None:
        result = self.decide(report(installed=True, running=True, is_postgres=True, major=16))
        blob = json.dumps(result.to_dict()).lower()
        for secret in ("password", "postgresql+psycopg://", "pgpassword"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)


class DetectionAgainstThisMachineTests(unittest.TestCase):
    """Read-only, against whatever this computer really has. Installs nothing."""

    @unittest.skipUnless(WINDOWS, "PostgreSQL detection is Windows-specific here")
    def test_detection_returns_a_known_state_without_changing_anything(self) -> None:
        result = detect_action("localhost", 5432)
        self.assertIn(result.exit_code,
                      {EXIT_POSTGRES_COMPATIBLE, EXIT_POSTGRES_MISSING,
                       EXIT_PORT_OCCUPIED, EXIT_POSTGRES_STOPPED})
        self.assertTrue(result.summary)


# ======================================================================================
# The provisioning bridge
# ======================================================================================

class ProvisioningBridgeTests(_InstallerTestCase):
    def test_missing_admin_password_is_reported_not_guessed(self) -> None:
        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            result = provision_action("localhost", 5432, "d", "r",
                                      self.temp_root / "absent.options")
        self.assertEqual(result.exit_code, EXIT_NO_ADMIN_PASSWORD)
        self.assertNotIn("password", json.dumps(result.detail).replace(
            "expected_environment_variable", ""))

    def test_the_password_is_taken_from_the_environment_and_removed_from_it(self) -> None:
        captured = {}

        def fake_provision(password, **kwargs):
            captured["password"] = password
            from app.provisioning import ProvisionResult

            return ProvisionResult(status=Status.SUCCESS, message="ok")

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: "admin-secret"}, clear=False), \
                patch("app.provisioning.provision", side_effect=fake_provision):
            provision_action("localhost", 5432, "d", "r", None)

        self.assertEqual(captured["password"], "admin-secret")
        self.assertIsNone(os.environ.get(ADMIN_PASSWORD_ENV),
                          "the password was left in the environment")

    def test_an_unreadable_handoff_is_reported_and_still_destroyed(self) -> None:
        """The second half of the PC10 failure: reading the secret raised, the raise escaped
        the try/finally, and the material stayed on disk. An installation that fails must
        never leave it behind."""
        handoff = self.temp_root / "handoff.bin"
        handoff.parent.mkdir(parents=True, exist_ok=True)
        handoff.write_text("00" * 32, encoding="ascii")

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            result = provision_action("localhost", 5432, "d", "r", handoff)

        self.assertEqual(result.exit_code, EXIT_NO_ADMIN_PASSWORD)
        self.assertFalse(handoff.exists(), "protected material was left on disk")

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_the_handoff_is_destroyed_even_when_provisioning_fails(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("admin-secret", handoff)

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False), \
                patch("app.provisioning.provision", side_effect=RuntimeError("boom")):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            with self.assertRaises(RuntimeError):
                provision_action("localhost", 5432, "d", "r", handoff)

        self.assertFalse(handoff.exists(),
                         "protected material was left on disk after a failure")

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_the_handoff_is_destroyed_after_a_successful_provision(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("admin-secret", handoff)

        from app.provisioning import ProvisionResult

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False), \
                patch("app.provisioning.provision",
                      return_value=ProvisionResult(status=Status.SUCCESS)):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            result = provision_action("localhost", 5432, "d", "r", handoff)

        self.assertFalse(handoff.exists())
        self.assertEqual(result.detail["handoff_residue"], "none")

    def test_provisioning_exit_codes_are_the_provisioning_status_values(self) -> None:
        from app.provisioning import ProvisionResult

        for status in (Status.SUCCESS, Status.ADMIN_AUTH_FAILED, Status.MIGRATION_FAILED,
                       Status.CREDENTIAL_STORE_FAILED, Status.VERIFICATION_FAILED):
            with self.subTest(status=status.name):
                with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: "x"}, clear=False), \
                        patch("app.provisioning.provision",
                              return_value=ProvisionResult(status=status)):
                    result = provision_action("localhost", 5432, "d", "r", None)
                self.assertEqual(result.exit_code, int(status))

    def test_an_unexpected_failure_becomes_a_message_not_a_traceback(self) -> None:
        with patch.object(installer_bridge, "detect_action", side_effect=RuntimeError("x")), \
                patch.object(installer_bridge, "write_result"):
            code = installer_bridge.main(["detect"])
        self.assertEqual(code, installer_bridge.EXIT_UNEXPECTED)

    def test_the_result_file_is_json_and_contains_no_secret(self) -> None:
        from app.provisioning import ProvisionResult

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: "admin-secret-value"}, clear=False), \
                patch("app.provisioning.provision",
                      return_value=ProvisionResult(status=Status.SUCCESS,
                                                   message="Database ready.")):
            result = provision_action("localhost", 5432, "d", "r", None)
            written = installer_bridge.write_result(result)

        payload = read(written)
        self.assertIn('"exit_code"', payload)
        self.assertNotIn("admin-secret-value", payload)
        for secret in ("postgresql+psycopg://", "password"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, payload.lower())

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_begin_handoff_creates_an_empty_protected_channel(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        result = begin_handoff_action(handoff)
        self.assertEqual(result.exit_code, int(Status.SUCCESS))
        self.assertTrue(handoff.is_file())
        self.assertEqual(handoff.read_bytes(), b"")

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_authentication_is_refused_when_no_password_was_handed_over(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        begin_handoff_action(handoff)
        result = authenticate_action("localhost", 5432, handoff)
        self.assertEqual(result.exit_code, EXIT_HANDOFF_FAILED)

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_a_rejected_password_is_retryable_and_never_echoed(self) -> None:
        """A wrong password must be a correctable mistake, not a failed installation --
        and the value must not come back in the message that says so."""
        handoff = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("wrong-admin-secret", handoff)

        with patch("app.provisioning.admin_can_authenticate",
                   return_value=(False, "The password was not accepted by PostgreSQL.")):
            result = authenticate_action("localhost", 5432, handoff)

        self.assertEqual(result.exit_code, EXIT_ADMIN_AUTH_FAILED)
        self.assertNotIn("wrong-admin-secret", json.dumps(result.to_dict()))
        # The channel survives, so the wizard can simply write a new blob and ask again.
        self.assertTrue(handoff.is_file())

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_an_accepted_password_reports_success_and_changes_nothing(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("right-admin-secret", handoff)

        with patch("app.provisioning.admin_can_authenticate", return_value=(True, "")):
            result = authenticate_action("localhost", 5432, handoff)

        self.assertEqual(result.exit_code, int(Status.SUCCESS))
        self.assertNotIn("right-admin-secret", json.dumps(result.to_dict()))

    @unittest.skipUnless(WINDOWS, "DPAPI is a Windows mechanism")
    def test_cleanup_removes_the_channel_on_an_abandoned_installation(self) -> None:
        handoff = self.temp_root / "handoff.bin"
        setup_handoff.write_channel("admin-secret", handoff)
        result = cleanup_action(handoff)
        self.assertEqual(result.exit_code, int(Status.SUCCESS))
        self.assertFalse(handoff.exists())
        self.assertEqual(result.detail["residue"], "none")


class DisposableProvisioningTests(_InstallerTestCase):
    """The bridge driven end to end against a THROWAWAY database.

    This is the validation mode requirement 15 asks for. Everything it touches is created
    here and destroyed afterwards: the database name carries the ``ai_job_migtest_`` prefix
    that ``tests/migration_db.py`` enforces, the role carries ``ai_job_testrole_``, and the
    credential goes into an insecure store in a temporary directory -- so the real
    ``linkedin_auto_apply`` database and the real DPAPI credential are never involved."""

    def setUp(self) -> None:
        super().setUp()
        from tests import migration_db

        if not migration_db.postgres_available():
            self.skipTest("no local PostgreSQL")
        self.admin_password = migration_db.admin_password()
        if not self.admin_password:
            self.skipTest("no PostgreSQL administrator password is configured")
        self.migration_db = migration_db
        self.url = migration_db.base_url()

        from app.secure_store import InsecureFileStore

        self.store = InsecureFileStore(self.temp_root / "credential.dat")
        self.database = migration_db.unique_name()
        self.role = migration_db.unique_role()
        self.addCleanup(migration_db.drop_role, self.role)
        self.addCleanup(migration_db.drop_database, self.database)

    def provision(self, **kwargs) -> BridgeResult:
        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: self.admin_password}, clear=False):
            return provision_action(
                self.url.host or "localhost", int(self.url.port or 5432),
                self.database, self.role, None, store=self.store, **kwargs)

    def test_a_fresh_machine_is_provisioned_end_to_end(self) -> None:
        result = self.provision()
        self.assertEqual(result.exit_code, int(Status.SUCCESS), result.summary)
        self.assertTrue(result.detail["role_created"])
        self.assertTrue(result.detail["database_created"])
        self.assertTrue(result.detail["migrated"])
        self.assertTrue(result.detail["schema_verified"])
        self.assertTrue(result.detail["credential_stored"])

    def test_the_migration_reaches_the_expected_revision(self) -> None:
        from app.schema_verification import BASELINE_REVISION

        result = self.provision()
        self.assertEqual(result.exit_code, int(Status.SUCCESS), result.summary)
        self.assertIsNotNone(result.detail["revision"])
        self.assertTrue(result.detail["revision"])
        # The baseline is the floor; a later head is equally valid.
        self.assertTrue(BASELINE_REVISION)

    def test_running_it_a_second_time_changes_nothing(self) -> None:
        """An upgrade re-runs this step. It must adopt, not recreate."""
        first = self.provision()
        self.assertEqual(first.exit_code, int(Status.SUCCESS), first.summary)

        second = self.provision()
        self.assertEqual(second.exit_code, int(Status.SUCCESS), second.summary)
        self.assertFalse(second.detail["role_created"], "the role was recreated")
        self.assertFalse(second.detail["database_created"], "the database was recreated")
        self.assertTrue(second.detail["schema_verified"])

    def test_existing_data_survives_a_second_provision(self) -> None:
        from sqlalchemy import create_engine, text

        self.assertEqual(self.provision().exit_code, int(Status.SUCCESS))

        credential = self.store.load()
        from app.db_credentials import DatabaseCredential

        engine = create_engine(DatabaseCredential.from_payload(credential).url())
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO portal_runtime_state (id, active_portal, updated_at) "
                    "VALUES (99, 'dice', now()) ON CONFLICT (id) DO NOTHING"))
            self.assertEqual(self.provision().exit_code, int(Status.SUCCESS))
            with engine.connect() as connection:
                kept = connection.execute(text(
                    "SELECT active_portal FROM portal_runtime_state WHERE id = 99")).scalar()
        finally:
            engine.dispose()
        self.assertEqual(kept, "dice", "provisioning destroyed existing data")

    def test_the_stored_credential_uses_the_least_privilege_role(self) -> None:
        self.assertEqual(self.provision().exit_code, int(Status.SUCCESS))
        credential = self.store.load()
        self.assertEqual(credential["username"], self.role)
        self.assertNotEqual(credential["username"], "postgres")

    def test_the_result_written_for_the_installer_carries_no_password(self) -> None:
        result = self.provision()
        written = installer_bridge.write_result(result)
        payload = read(written)
        credential = self.store.load()
        self.assertNotIn(credential["password"], payload)
        self.assertNotIn(self.admin_password, payload)


class ElevationBoundaryTests(unittest.TestCase):
    """DPAPI is current-user scoped. If the credential were written by the elevated
    installer account, the person who actually uses the application could not decrypt it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.iss = read(ISS_FILE)
        cls.bridge = read(ROOT / "app" / "installer_bridge.py")

    def test_the_credential_step_runs_as_the_original_user(self) -> None:
        self.assertIn("function RunAppAsUser", self.iss)
        self.assertIn("ExecAsOriginalUser(AppExe", self.iss)
        # Provisioning -- the step that stores the credential -- goes through it.
        self.assertIn("RunAppAsUser('--installer provision'", self.iss)

    def test_no_step_in_the_database_flow_runs_elevated(self) -> None:
        """Nothing about creating a role and a database needs a Windows privilege: the
        authority is a POSTGRESQL credential. Every one of these steps therefore runs as the
        original user, which is also what makes the DPAPI credential belong to them."""
        code = strip_comments(self.iss)
        for action in ("begin-handoff", "authenticate", "provision", "status", "cleanup"):
            with self.subTest(action=action):
                self.assertIn(f"RunAppAsUser('--installer {action}'", code)

    def test_the_password_crosses_the_boundary_as_ciphertext_only(self) -> None:
        code = strip_comments(self.iss)
        self.assertIn("CryptProtectData", code)
        # The wizard keeps hex, never the typed value.
        self.assertIn("HandoffHex", code)
        for forbidden in ("PGPASSWORD", "AI_JOB_AUTOMATION_ADMIN_PASSWORD",
                          "--password", "SetEnvironmentVariable"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code)

    def test_dpapi_is_never_weakened_to_machine_scope(self) -> None:
        from app import secure_store

        self.assertNotIn("SCOPE_MACHINE", self.bridge)
        self.assertNotIn("CRYPTPROTECT_LOCAL_MACHINE", self.bridge)
        # The default really is user scope.
        self.assertEqual(secure_store.get_credential_store.__defaults__, (secure_store.SCOPE_USER,))

    def test_the_bridge_documents_why_the_split_exists(self) -> None:
        self.assertIn("elevation boundary", self.bridge.lower())

    def test_provisioning_needs_no_windows_privilege(self) -> None:
        """The reason the split is possible at all: creating a role needs a DATABASE
        credential, not an elevated Windows token. The bridge never asks for elevation."""
        code = "\n".join(
            line for line in self.bridge.splitlines() if not line.strip().startswith("#")
        )
        # The module docstring explains the design, so only executable lines are checked.
        body = code[code.index('"""', code.index('"""') + 3) + 3:]
        for elevated in ("ShellExecute", "ShellExecuteEx", "AdjustTokenPrivileges",
                         "CreateProcessAsUser", "runas"):
            with self.subTest(pattern=elevated):
                self.assertNotIn(elevated, body)


# ======================================================================================
# Build scripts
# ======================================================================================

class BuildScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = read(BUILD_SCRIPT)
        cls.test = read(TEST_SCRIPT)

    def test_both_scripts_use_strict_mode(self) -> None:
        for name, script in (("build", self.build), ("test", self.test)):
            with self.subTest(script=name):
                self.assertIn("Set-StrictMode -Version Latest", script)
                self.assertIn('$ErrorActionPreference = "Stop"', script)

    def test_inno_setup_is_located_never_installed(self) -> None:
        self.assertIn("Inno Setup 6\\ISCC.exe", self.build)
        self.assertIn("jrsoftware.org", self.build)
        self.assertNotIn("Invoke-WebRequest", self.build)
        self.assertIn("exit 3", self.build)

    def test_the_build_verifies_the_payload_before_compiling(self) -> None:
        """A payload that has not passed the secrets scan must never reach ISCC."""
        self.assertIn("scan_build_secrets", self.build)
        self.assertIn("exit 4", self.build)
        # Anchored on the compiler invocation itself rather than on the helper that happens
        # to wrap it, so adding or removing a wrapper cannot silently disable this check.
        invocation = re.search(r"^& \$iscc .*\$issFile", self.build, re.MULTILINE)
        self.assertIsNotNone(invocation, "could not find the ISCC invocation")
        self.assertLess(self.build.index("scan_build_secrets"), invocation.start())

    def test_the_build_refuses_an_embedded_postgresql_installer(self) -> None:
        """Checked on what ISCC actually embedded, so reintroducing the prerequisite by
        accident fails the build rather than quietly adding 334 MB back."""
        invocation = re.search(r"^& \$iscc .*\$issFile", self.build, re.MULTILINE)
        self.assertLess(invocation.start(), self.build.index("postgresBinaries"))
        self.assertIn("postgresql.*\\.exe$", self.build)

    def test_the_embedded_file_list_is_checked_after_compiling(self) -> None:
        """Containment is verified against what ISCC actually embedded, not against intent."""
        invocation = re.search(r"^& \$iscc .*\$issFile", self.build, re.MULTILINE)
        self.assertLess(invocation.start(), self.build.index("outside the packaged payload"))
        self.assertIn("Compressing:", self.build)

    def test_the_build_writes_only_to_the_guarded_release_directory(self) -> None:
        self.assertIn('$releaseDir = Join-Path $projectRoot "release"', self.build)
        self.assertIn("/DOutputDir=$releaseDir", self.build)
        self.assertNotIn("Remove-Item -Recurse", self.build)

    def test_the_build_verifies_the_produced_installer(self) -> None:
        self.assertIn("VersionInfo", self.build)
        self.assertIn("100MB", self.build)
        self.assertIn("exit 7", self.build)

    def test_neither_script_modifies_the_caller_environment(self) -> None:
        for name, script in (("build", self.build), ("test", self.test)):
            with self.subTest(script=name):
                for pattern in ("setx", "SetEnvironmentVariable", "$env:PATH ="):
                    self.assertNotIn(pattern, script)

    def test_the_test_script_never_installs_postgresql(self) -> None:
        self.assertNotIn("--mode unattended", self.test)
        self.assertNotIn("--optionfile", self.test)
        self.assertIn("read-only", self.test.lower())

    def test_the_scripts_use_no_powershell_7_only_syntax(self) -> None:
        """These run under Windows PowerShell 5.1, where ?: and ?? are parse errors."""
        for name, script in (("build", self.build), ("test", self.test)):
            for number, line in enumerate(script.splitlines(), start=1):
                code = line.split("#")[0]
                with self.subTest(script=name, line=number):
                    self.assertNotRegex(code, r"\?\?")
                    self.assertNotRegex(code, r"\S\s+\?\s+\S+\s+:\s+\S")

    def test_the_build_uses_the_packaged_application_not_development_python(self) -> None:
        """The installed product runs the frozen executable and nothing else. The .venv
        interpreter is used at BUILD time only, to read metadata and run the scanner."""
        code = strip_comments(read(ISS_FILE))
        for development in (".venv", "python.exe", "node_modules", "npm", "vite"):
            with self.subTest(pattern=development):
                self.assertNotIn(development, code)
        self.assertIn("{app}\\{#ExeName}.exe", code)


# ======================================================================================
# The built installer, when one exists
# ======================================================================================

def find_iscc() -> Path | None:
    """Inno Setup's command-line compiler, if this machine has it."""
    for variable in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / "Inno Setup 6" / "ISCC.exe"
            if candidate.is_file():
                return candidate
    found = shutil.which("ISCC.exe")
    return Path(found) if found else None


class IsccCompileTests(unittest.TestCase):
    """Compile the .iss for real.

    Static assertions cannot see a Pascal syntax error. This caught one immediately: a
    ``{ ... }`` comment containing ``{tmp}``. Pascal block comments do not nest, so the
    constant terminated the comment and the rest of the sentence became code.

    A stub payload is used so the compile takes about a second instead of minutes -- the
    thing under test is the script, not the compression of a gigabyte of Chromium."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.iscc = find_iscc()
        if cls.iscc is None:
            raise unittest.SkipTest("Inno Setup 6 is not installed")

    def compile(self, *definitions: str) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory(prefix="iscc-test-") as workspace:
            root = Path(workspace)
            payload = root / "payload"
            (payload / "_internal").mkdir(parents=True)
            (payload / "RisingPathRecruiters.exe").write_text("stub", encoding="utf-8")
            (payload / "_internal" / "marker.txt").write_text("stub", encoding="utf-8")
            output = root / "out"
            output.mkdir()
            return subprocess.run(
                [str(self.iscc), f"/DPayloadDir={payload}", f"/DOutputDir={output}",
                 *definitions, str(ISS_FILE)],
                capture_output=True, text=True, timeout=600,
            )

    def test_the_installer_script_compiles(self) -> None:
        completed = self.compile()
        self.assertEqual(completed.returncode, 0,
                         f"ISCC failed:\n{completed.stdout}\n{completed.stderr}")
        self.assertIn("Successful compile", completed.stdout)

    def test_the_code_section_compiles(self) -> None:
        """Inno resolves Pascal Script function names at compile time, so a successful
        compile also proves ExecAsOriginalUser, CreateCustomForm and the crypt32 imports
        really exist and are declared with the right number of parameters."""
        completed = self.compile()
        self.assertIn("Compiling [Code] section", completed.stdout)
        self.assertEqual(completed.returncode, 0)

    def test_no_pascal_comment_contains_a_nested_brace(self) -> None:
        """The exact defect the first real compile found. Pascal '{ }' comments do not nest,
        so an Inno constant inside one silently ends it and breaks the script."""
        iss = read(ISS_FILE)
        body = iss[iss.index("[Code]"):]
        offenders = []
        for match in re.finditer(r"\{\s", body):
            closing = body.index("}", match.start())
            comment = body[match.start():closing + 1]
            if "{" in comment[1:]:
                offenders.append(comment.splitlines()[0][:70])
        self.assertEqual(offenders, [])


class BuiltInstallerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = RELEASE_DIR / version.installer_filename()
        if not cls.installer.is_file():
            raise unittest.SkipTest(
                "no installer in release/ -- Inno Setup is required to build one")

    def test_the_installer_is_not_empty_and_is_large_enough(self) -> None:
        self.assertGreater(self.installer.stat().st_size, 100 * 1024 * 1024)

    def test_the_installer_reports_the_expected_version(self) -> None:
        if not WINDOWS:
            self.skipTest("version resources are a Windows concept")
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Item '{self.installer}').VersionInfo.ProductVersion"],
            capture_output=True, text=True, timeout=60)
        self.assertIn(version.VERSION, completed.stdout)


# ======================================================================================
# The clean-machine verifier -- the only script that runs on the test VM
# ======================================================================================

class CleanMachineVerifierTests(unittest.TestCase):
    """``scripts/verify_clean_machine.ps1`` is copied to a machine that has nothing else on
    it, and it runs there with administrator rights. Two properties therefore have to hold
    by construction rather than by care: it must be non-destructive, and it must depend on
    nothing but Windows and the installed product. These assertions are the guard."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.script = read(VERIFY_SCRIPT)
        # Negative assertions run against the code only; see powershell_code().
        cls.code = powershell_code(cls.script)

    def test_every_scenario_stage_exists(self) -> None:
        for stage in ("preflight", "install-verify", "runtime", "postuninstall"):
            with self.subTest(stage=stage):
                self.assertIn(f"'{stage}'", self.script)

    def test_the_verifier_changes_nothing_on_the_test_machine(self) -> None:
        """It may start and gracefully stop the application. It may not remove, stop,
        reconfigure or terminate anything -- including the database it is checking."""
        for verb in ("Remove-Item", "Stop-Process", "Stop-Service", "Restart-Service",
                     "Set-Service", "taskkill", "sc delete", "sc stop", "reg delete",
                     "DROP DATABASE", "DROP ROLE", "Uninstall-", "Clear-Content"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, self.code)

    def test_the_verifier_never_weakens_windows_security(self) -> None:
        for pattern in ("Set-MpPreference", "Add-MpPreference", "Set-ExecutionPolicy",
                        "DisableRealtimeMonitoring", "SmartScreenEnabled ="):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.code)

    def test_the_verifier_needs_no_python_node_or_source_checkout(self) -> None:
        for pattern in (".venv", "-m unittest", "pytest", "npm run", "requirements.txt"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.code)

    def test_the_verifier_proves_the_loopback_only_binding(self) -> None:
        self.assertIn("Every listener is 127.0.0.1", self.script)

    def test_the_verifier_proves_the_windowed_build(self) -> None:
        self.assertIn("Get-PeSubsystem", self.script)
        self.assertIn("subsystem is GUI (2)", self.script)

    def test_the_verifier_proves_the_shutdown_contract(self) -> None:
        for expected in ("runtime.json was removed", "shutdown.token was removed",
                         "No unrelated process was terminated"):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.script)

    def test_the_verifier_expects_a_refused_second_instance(self) -> None:
        self.assertIn("Second instance refused with exit code 3", self.script)

    def test_the_verifier_proves_program_files_is_never_written_at_runtime(self) -> None:
        self.assertIn("Installation directory is byte-for-byte unchanged", self.script)

    def test_the_verifier_checks_uninstall_preservation(self) -> None:
        for expected in ("DPAPI credential is preserved",
                         "PostgreSQL is still installed and running",
                         "Portal sessions are preserved"):
            with self.subTest(expected=expected):
                self.assertIn(expected, self.script)

    def test_the_verifier_scans_for_leaked_secrets_and_prints_none(self) -> None:
        self.assertIn("Test-LogForSecrets", self.script)
        self.assertIn("No unredacted secret in launcher.log", self.script)
        # The handoff carries the administrator password as ciphertext and is destroyed
        # after use; the verifier confirms its absence rather than reading it.
        self.assertIn("Secure password handoff was destroyed", self.script)
        self.assertIn("No other setup material remains", self.script)
        self.assertNotIn("read_admin_password", self.code)

    def test_the_verifier_resolves_the_64_bit_program_files(self) -> None:
        """On PC10 the verifier ran in a 32-bit host, where ``$env:ProgramFiles`` reads back
        as ``C:\\Program Files (x86)``. It looked for the application where it had not been
        installed and reported a correct installation as missing."""
        self.assertIn("$env:ProgramW6432", self.script)
        self.assertIn("$ProgramFiles64", self.script)
        self.assertIn("${env:ProgramFiles(x86)}", self.script)
        self.assertIn("Is64BitProcess", self.script)
        # The install directory must never come from the redirected variable.
        self.assertNotIn("Join-Path $env:ProgramFiles $AppName", self.code)

    def test_the_verifier_checks_both_program_files_locations(self) -> None:
        self.assertIn("No existing installation (both Program Files locations)", self.script)
        self.assertIn("Not installed under Program Files (x86)", self.script)

    def test_the_verifier_reads_the_registry_through_explicit_views(self) -> None:
        """The HKLM: drive is redirected to WOW6432Node in a 32-bit host, so the Add/Remove
        Programs entry was invisible. Both views are opened explicitly instead."""
        self.assertIn("OpenBaseKey", self.script)
        self.assertIn("[Microsoft.Win32.RegistryView]::Registry64", self.script)
        self.assertIn("[Microsoft.Win32.RegistryView]::Registry32", self.script)
        # The Uninstall hive specifically -- reading it through the HKLM: drive is what got
        # redirected. Unrelated policy keys may still be read that way.
        self.assertNotIn("HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall",
                         self.code)
        self.assertNotIn("WOW6432Node", self.code)

    def test_the_verifier_reads_exit_codes_reliably(self) -> None:
        """``Start-Process -Wait -PassThru`` reported an empty ExitCode on PC10, so every
        launched check failed regardless of its real outcome."""
        self.assertIn("function Start-AndWait", self.script)
        self.assertNotIn("-PassThru -Wait", self.code)

    def test_the_verifier_reports_the_handoff_acl_but_never_its_contents(self) -> None:
        self.assertIn("contents deliberately not read", self.script)
        self.assertIn("installer-result.json", self.script)

    def test_listener_results_are_always_counted_as_a_collection(self) -> None:
        """PowerShell unrolls a returned one-element array into a bare PSCustomObject, which
        has no .Count at all -- it reads back as $null. On PC9 that made "count=" print
        blank and failed the check in the NORMAL case of exactly one listener, while the
        neighbouring check kept passing because $null.Count really is 0."""
        for call in re.findall(r".*Get-AppListeners.*", self.script):
            if call.strip().startswith("function"):
                continue
            with self.subTest(call=call.strip()):
                self.assertIn("@(Get-AppListeners", call)

    def test_the_shell_is_requested_the_way_a_browser_requests_it(self) -> None:
        """The application deliberately serves the SPA only to callers that accept HTML --
        see accepts_html() in app/static_site.py. Invoke-WebRequest defaults to Accept: */*,
        which is treated as an API call and 404s, so the old check reported a correct React
        build as missing."""
        self.assertIn("$BrowserAccept", self.script)
        self.assertIn("text/html,application/xhtml+xml", self.script)
        shell = self.script[self.script.index("Dashboard shell is served") - 400:
                            self.script.index("Dashboard shell is served")]
        self.assertIn("$BrowserAccept", shell)

    def test_a_failed_http_check_reports_the_response(self) -> None:
        """A blank detail is undiagnosable remotely, which is what made this take a round
        trip to PC9 to understand."""
        self.assertIn("function Describe-Response", self.script)
        self.assertIn("Describe-Response $root", self.script)

    def test_postgres_backend_churn_is_not_treated_as_interference(self) -> None:
        """PostgreSQL forks one backend per connection; closing the pool at shutdown ends
        them by itself. The exclusion is only safe because the postmaster is checked
        directly, so both halves are asserted here."""
        block = self.script[self.script.index("$postgresChurn"):
                            self.script.index("Program Files was not modified")]
        self.assertIn("$_.Name -ne 'postgres.exe'", block)
        self.assertIn("The PostgreSQL server process is the same one as before shutdown",
                      block)
        self.assertIn("$pgServiceBefore", block)
        # The weaker check must not have simply been deleted.
        self.assertIn("No unrelated process was terminated", block)

    def test_the_report_collection_cannot_be_clobbered_by_a_similar_name(self) -> None:
        """PowerShell variable names are case-insensitive, so a local named $lines WAS
        $script:Lines. Assigning an array to it replaced the report accumulator with a
        fixed-size Object[] and every later Emit threw -- silently truncating the log while
        the console output still looked complete."""
        self.assertIn("$script:ReportLines", self.script)
        self.assertIn("System.Collections.Generic.List[string]", self.script)
        self.assertIn("function Assert-ReportList", self.script)
        # No variable whose name differs only by case may exist any more.
        for name in re.findall(r"\$(?:script:)?([Ll]ines)\b", self.code):
            self.fail(f"${name} is the report collection under another case")

    def test_the_verifier_confirms_the_dpapi_credential_owner(self) -> None:
        self.assertIn("Credential belongs to the interactive user", self.script)
        self.assertIn("USERNAME", self.script)

    def test_the_verifier_parses_under_powershell(self) -> None:
        if not WINDOWS:
            self.skipTest("PowerShell parsing is a Windows concept")
        command = (
            "$e=$null; "
            f"[void][System.Management.Automation.Language.Parser]::ParseFile('{VERIFY_SCRIPT}',"
            "[ref]$null,[ref]$e); $e.Count"
        )
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.stdout.strip(), "0", completed.stdout + completed.stderr)


if __name__ == "__main__":
    unittest.main()
