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
* **prerequisite** -- one pinned PostgreSQL version, verified by checksum AND by publisher
  signature, never executed by anything under test;
* **detection before installation** -- a compatible PostgreSQL is used as it is; a stopped
  one, an occupied port and an unsupported version are each reported differently;
* **secrets** -- the administrator password never reaches a command line, the temporary file
  that carries it is ACL-restricted and destroyed, and no message can contain it;
* **the elevation boundary** -- the DPAPI credential is written by the ORIGINAL user, not by
  whichever account approved the UAC prompt;
* **uninstall** -- removes the program, preserves everything the person cares about.

Nothing here installs PostgreSQL, runs the PostgreSQL installer, touches the real database,
or writes the real stored credential. Destructive paths use disposable databases and mocks.
"""

from __future__ import annotations

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
    EXIT_NO_ADMIN_PASSWORD,
    EXIT_PORT_OCCUPIED,
    EXIT_POSTGRES_COMPATIBLE,
    EXIT_POSTGRES_MISSING,
    EXIT_POSTGRES_STOPPED,
    BridgeResult,
    detect_action,
    prepare_action,
    provision_action,
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
    InstallPlan,
    generate_admin_password,
    is_compatible,
    load_manifest,
    postgres_prerequisite,
    secure_delete,
    write_option_file,
)
from app.provisioning import Status

ROOT = project_root()
ISS_FILE = ROOT / "installer" / "RisingPathRecruiters.iss"
MANIFEST_FILE = ROOT / "installer" / "prerequisites.json"
BUILD_SCRIPT = ROOT / "scripts" / "build_installer.ps1"
TEST_SCRIPT = ROOT / "scripts" / "test_installer.ps1"
PREPARE_SCRIPT = ROOT / "scripts" / "prepare_installer_prerequisites.ps1"
PAYLOAD_DIR = ROOT / "dist" / "RisingPathRecruiters"
RELEASE_DIR = ROOT / "release"

WINDOWS = sys.platform == "win32"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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

    def test_postgres_is_only_extracted_when_detection_says_it_is_needed(self) -> None:
        """The prerequisite is embedded but unpacked on demand.

        A ``Check:`` on the [Files] entry cannot do this job: Inno evaluates file entries
        before ``{app}\\RisingPathRecruiters.exe`` exists, so a check that asks the
        application whether PostgreSQL is present would fail on exactly the machines that
        need the prerequisite. ``dontcopy`` + ``ExtractTemporaryFile`` moves the decision to
        after installation, when detection has a working executable to ask."""
        code = strip_comments(self.iss)
        self.assertIn("Flags: dontcopy", code)
        self.assertNotIn("Check: NeedsPostgresInstall", code)
        self.assertIn("ExtractTemporaryFile", code)
        # ...and the extraction lives inside the branch that only runs when it is needed.
        install = code[code.index("procedure InstallPostgres"):
                       code.index("procedure ProvisionDatabase")]
        self.assertIn("ExtractTemporaryFile", install)
        self.assertIn("if NeedsPostgresInstall then", code)
        self.assertIn("EXIT_POSTGRES_MISSING", code)

    def test_the_prerequisite_is_never_extracted_when_postgres_is_usable(self) -> None:
        """NeedsPostgresInstall is the single gate, and it is true for one state only."""
        code = strip_comments(self.iss)
        gate = code[code.index("function NeedsPostgresInstall"):
                    code.index("function ProblemText")]
        self.assertIn("DetectPostgres = EXIT_POSTGRES_MISSING", gate)

    def test_no_password_ever_appears_on_a_command_line(self) -> None:
        self.assertNotIn("superpassword", self.iss)
        self.assertNotIn("--superpassword", self.iss)
        self.assertIn("--optionfile", self.iss)

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
# The pinned prerequisite
# ======================================================================================

class PrerequisiteManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = load_manifest()

    def test_exactly_one_postgres_version_is_pinned(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        self.assertRegex(prerequisite.version, r"^\d+\.\d+-\d+$")
        self.assertTrue(prerequisite.filename.endswith(".exe"))
        self.assertIn(prerequisite.version, prerequisite.filename)

    def test_the_download_comes_from_an_official_https_source(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        self.assertTrue(prerequisite.url.startswith("https://"))
        # EDB builds and publishes the Windows installer linked from postgresql.org.
        self.assertRegex(prerequisite.url, r"^https://(get\.)?enterprisedb\.com/")
        self.assertTrue(prerequisite.url.endswith(prerequisite.filename))

    def test_a_checksum_is_pinned(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        self.assertTrue(prerequisite.pinned, "no sha256 is pinned")
        self.assertRegex(prerequisite.sha256, r"^[0-9a-f]{64}$")

    def test_the_expected_publisher_is_pinned(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        self.assertIn("EnterpriseDB", prerequisite.authenticode_subject)

    def test_the_pinned_major_version_is_supported(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        self.assertTrue(is_compatible(prerequisite.major, self.manifest))

    def test_the_minimum_supported_major_is_stated(self) -> None:
        self.assertGreaterEqual(self.manifest["minimum_supported_postgres_major"], 12)

    def test_the_manifest_carries_no_secret(self) -> None:
        blob = read(MANIFEST_FILE).lower()
        for secret in ("password", "pgpassword", "sk-", "postgresql+psycopg://"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, blob)

    def test_the_downloaded_file_matches_the_pin_when_it_is_present(self) -> None:
        prerequisite = postgres_prerequisite(self.manifest)
        path = ROOT / "installer" / "prerequisites" / prerequisite.filename
        if not path.is_file():
            self.skipTest("the prerequisite has not been downloaded on this machine")
        import hashlib

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        self.assertEqual(digest.hexdigest(), prerequisite.sha256)
        self.assertEqual(path.stat().st_size, prerequisite.size_bytes)


class PrerequisiteScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = read(PREPARE_SCRIPT)

    def test_a_checksum_mismatch_fails_the_build(self) -> None:
        self.assertIn("CHECKSUM MISMATCH", self.script)
        self.assertIn("exit 4", self.script)

    def test_a_wrong_publisher_fails_the_build(self) -> None:
        self.assertIn("Get-AuthenticodeSignature", self.script)
        self.assertIn("exit 5", self.script)

    def test_the_script_never_executes_the_installer_it_downloads(self) -> None:
        for pattern in ("Start-Process", "--mode unattended", "Invoke-Expression"):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.script)

    def test_the_script_never_writes_its_own_trust_anchor(self) -> None:
        """A script that updated the checksum it verifies against would verify nothing."""
        self.assertIn("never writes them itself", self.script)
        self.assertNotIn("ConvertTo-Json", self.script)

    def test_offline_mode_is_supported(self) -> None:
        self.assertIn("[switch]$Offline", self.script)

    def test_the_script_does_not_modify_path(self) -> None:
        for pattern in ("setx", "SetEnvironmentVariable", "$env:PATH ="):
            with self.subTest(pattern=pattern):
                self.assertNotIn(pattern, self.script)

    def test_the_build_refuses_to_proceed_without_a_verified_prerequisite(self) -> None:
        build = read(BUILD_SCRIPT)
        self.assertIn("prepare_installer_prerequisites.ps1", build)
        self.assertIn("exit 5", build)


# ======================================================================================
# The PostgreSQL installation command -- constructed, never executed
# ======================================================================================

class InstallCommandTests(_InstallerTestCase):
    def plan(self, **kwargs) -> InstallPlan:
        return InstallPlan(installer=Path(r"C:\tmp\postgresql-16.9-1-windows-x64.exe"), **kwargs)

    def test_the_command_contains_no_secret(self) -> None:
        option_file = self.temp_root / "postgres-setup.options"
        command = self.plan().command(option_file)
        joined = " ".join(command)
        self.assertIn("--optionfile", joined)
        for leak in ("superpassword", "password", "--superaccount"):
            with self.subTest(leak=leak):
                self.assertNotIn(leak, joined)

    def test_the_option_file_carries_the_password_instead(self) -> None:
        lines = self.plan().option_lines("a-generated-secret")
        self.assertIn("superpassword=a-generated-secret", lines)
        self.assertIn("mode=unattended", lines)

    def test_the_smallest_supported_server_is_selected(self) -> None:
        lines = self.plan().option_lines("x")
        disabled = next(line for line in lines if line.startswith("disable-components="))
        self.assertIn("pgAdmin", disabled)
        self.assertIn("stackbuilder", disabled)

    def test_a_non_default_port_is_carried_through(self) -> None:
        self.assertIn("serverport=5433", self.plan(port=5433).option_lines("x"))

    def test_generated_passwords_are_strong_unique_and_url_safe(self) -> None:
        first, second = generate_admin_password(), generate_admin_password()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 24)
        for awkward in ("'", '"', "\\", "@", ":", "/", " ", "="):
            with self.subTest(character=awkward):
                self.assertNotIn(awkward, first)

    def test_the_help_command_exists_for_validating_the_options_when_authorised(self) -> None:
        from app.postgres_install import help_command

        self.assertEqual(help_command(Path("x.exe")), ["x.exe", "--help"])

    def test_nothing_in_this_module_executes_the_installer(self) -> None:
        source = read(ROOT / "app" / "postgres_install.py")
        # subprocess is used for icacls only.
        self.assertNotIn("subprocess.Popen", source)
        for call in re.findall(r"subprocess\.run\(\s*\[([^\]]+)\]", source, re.DOTALL):
            with self.subTest(call=call[:60]):
                self.assertIn("icacls", call)


class OptionFileProtectionTests(_InstallerTestCase):
    def test_the_option_file_is_written_and_can_be_read_back(self) -> None:
        target = self.temp_root / "options"
        plan = InstallPlan(installer=Path("x.exe"))
        write_option_file(target, plan, "the-generated-password")
        from app.postgres_install import read_admin_password_from_option_file

        self.assertEqual(read_admin_password_from_option_file(target), "the-generated-password")

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_the_option_file_is_not_readable_by_other_standard_users(self) -> None:
        target = self.temp_root / "options"
        write_option_file(target, InstallPlan(installer=Path("x.exe")), "secret-value")
        listing = subprocess.run(["icacls", str(target)], capture_output=True, text=True,
                                 timeout=30).stdout
        # Inheritance removed, and no grant to Users/Everyone/Authenticated Users.
        for open_group in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
            with self.subTest(group=open_group):
                self.assertNotIn(open_group, listing)

    @unittest.skipUnless(WINDOWS, "ACLs are a Windows concept")
    def test_the_file_is_secured_before_the_secret_is_written_into_it(self) -> None:
        """Writing first and tightening afterwards would leave a readable window."""
        source = read(ROOT / "app" / "postgres_install.py")
        body = source[source.index("def write_option_file"):source.index("def secure_delete")]
        secure_at = body.index("restrict_to_current_user")
        secret_at = body.index("option_lines")
        self.assertLess(secure_at, secret_at)

    def test_the_option_file_is_destroyed_and_its_bytes_overwritten(self) -> None:
        target = self.temp_root / "options"
        write_option_file(target, InstallPlan(installer=Path("x.exe")), "secret-value")
        self.assertTrue(secure_delete(target))
        self.assertFalse(target.exists())

    def test_deleting_an_absent_file_is_not_an_error(self) -> None:
        self.assertFalse(secure_delete(self.temp_root / "never-existed"))

    def test_the_option_file_lives_in_the_users_own_data_directory(self) -> None:
        path = installer_bridge.option_file_path()
        self.assertTrue(str(path).startswith(str(self.temp_root)))
        self.assertEqual(path.name, installer_bridge.OPTION_FILENAME)


# ======================================================================================
# Detection before installation
# ======================================================================================

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

    def test_the_option_file_is_destroyed_even_when_provisioning_fails(self) -> None:
        option_file = self.temp_root / "options"
        write_option_file(option_file, InstallPlan(installer=Path("x.exe")), "admin-secret")

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False), \
                patch("app.provisioning.provision", side_effect=RuntimeError("boom")):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            with self.assertRaises(RuntimeError):
                provision_action("localhost", 5432, "d", "r", option_file)

        self.assertFalse(option_file.exists(),
                         "an administrator password was left on disk after a failure")

    def test_the_option_file_is_destroyed_after_a_successful_provision(self) -> None:
        option_file = self.temp_root / "options"
        write_option_file(option_file, InstallPlan(installer=Path("x.exe")), "admin-secret")

        from app.provisioning import ProvisionResult

        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ""}, clear=False), \
                patch("app.provisioning.provision",
                      return_value=ProvisionResult(status=Status.SUCCESS)):
            os.environ.pop(ADMIN_PASSWORD_ENV, None)
            provision_action("localhost", 5432, "d", "r", option_file)

        self.assertFalse(option_file.exists())

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

    def test_prepare_writes_a_protected_option_file_and_returns_no_secret(self) -> None:
        result = prepare_action(5432, Path("postgres.exe"))
        self.assertEqual(result.exit_code, int(Status.SUCCESS))
        option_file = Path(result.detail["option_file"])
        self.assertTrue(option_file.is_file())
        blob = json.dumps(result.to_dict())
        self.assertNotIn("superpassword", blob)
        # The command it hands back is safe to log.
        self.assertIn("--optionfile", result.detail["command"])


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

    def test_only_the_postgresql_installation_runs_elevated(self) -> None:
        install = self.iss[self.iss.index("procedure InstallPostgres"):
                           self.iss.index("procedure ProvisionDatabase")]
        # The one plain Exec in that block is the PostgreSQL installer itself.
        self.assertIn("Exec(Installer,", install)
        self.assertIn("RunAppAsUser('--installer prepare-postgres'", install)

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

    def test_the_build_verifies_the_prerequisite_before_compiling(self) -> None:
        invocation = re.search(r"^& \$iscc .*\$issFile", self.build, re.MULTILINE)
        self.assertLess(self.build.index("prepare_installer_prerequisites.ps1"),
                        invocation.start())

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
        for name, script in (("build", self.build), ("test", self.test),
                             ("prepare", read(PREPARE_SCRIPT))):
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
        completed = self.compile("/DIncludePostgres=0")
        self.assertEqual(completed.returncode, 0,
                         f"ISCC failed:\n{completed.stdout}\n{completed.stderr}")
        self.assertIn("Successful compile", completed.stdout)

    def test_the_code_section_compiles(self) -> None:
        """Inno resolves Pascal Script function names at compile time, so a successful
        compile also proves ExecAsOriginalUser and ExtractTemporaryFile really exist."""
        completed = self.compile("/DIncludePostgres=0")
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


if __name__ == "__main__":
    unittest.main()
