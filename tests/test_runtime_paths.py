"""Windows-safe resolution of every writable runtime location (app/paths.py).

Guarantees under test:

* a packaged install never writes inside Program Files or the install directory;
* EVERY packaged path lives under %LOCALAPPDATA%\\AIJobAutomation -- roaming application
  data (%APPDATA%) is never used, because a managed Windows environment can synchronise it
  between computers;
* a source checkout keeps the historical project-relative layout, so an existing developer
  machine keeps its saved LinkedIn/Dice sessions and accumulated diagnostics;
* tests and launchers can redirect everything into an isolated tree;
* disposable data (diagnostics, logs, PIDs) and data worth keeping (sessions, uploads) are
  separated, while both stay machine-local;
* every machine keeps its own data -- nothing resolves to a shared or synchronised location.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import paths as paths_module
from app.config import Settings, get_settings, reset_settings_cache
from app.paths import (
    APP_NAME,
    DATA_DIR_ENV,
    DISPOSABLE_DIR_NAMES,
    LOCAL_DIR_ENV,
    MODE_DEVELOPMENT,
    MODE_ENV,
    MODE_OVERRIDE,
    MODE_PACKAGED,
    PERSISTENT_DIR_NAMES,
    RuntimePaths,
    project_root,
    reset_runtime_paths_cache,
    resolve_runtime_paths,
    runtime_paths,
)

# Every environment variable that can influence resolution. Cleared for each test so the
# developer's own machine settings never leak in and change an expected result. APPDATA is
# cleared too: it must never influence resolution, and clearing it proves so.
PATH_ENV_VARS = (
    DATA_DIR_ENV, LOCAL_DIR_ENV, MODE_ENV,
    "LOCALAPPDATA", "APPDATA", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
)

ALL_DIR_NAMES = DISPOSABLE_DIR_NAMES + PERSISTENT_DIR_NAMES


class _PathTestCase(unittest.TestCase):
    """Clears the resolver + settings caches around every test so no case can poison another
    (and so the real user's application data is never touched)."""

    def setUp(self) -> None:
        self.temp_root = Path(tempfile.mkdtemp(prefix="paths-test-"))
        self.addCleanup(shutil.rmtree, self.temp_root, ignore_errors=True)
        self.addCleanup(reset_settings_cache)
        self.addCleanup(reset_runtime_paths_cache)
        reset_runtime_paths_cache()
        reset_settings_cache()

    def env(self, **overrides: str):
        """Patch the environment with ONLY the given path variables set."""
        cleared = {name: "" for name in PATH_ENV_VARS}
        merged = {**cleared, **overrides}
        return patch.dict(os.environ, merged, clear=False)

    def packaged_env(self, local: Path, roaming: Path | None = None):
        """Simulate a packaged install. ``roaming`` sets %APPDATA% to a location that must
        never be used, so any accidental roaming dependency shows up as a failure."""
        values = {MODE_ENV: MODE_PACKAGED, "LOCALAPPDATA": str(local)}
        if roaming is not None:
            values["APPDATA"] = str(roaming)
        return self.env(**values)


class DefaultWindowsResolutionTests(_PathTestCase):
    def test_packaged_mode_puts_every_directory_under_localappdata(self) -> None:
        local = self.temp_root / "Local"
        roaming = self.temp_root / "Roaming"
        with self.packaged_env(local, roaming):
            resolved = resolve_runtime_paths()

        self.assertEqual(resolved.mode, MODE_PACKAGED)
        self.assertEqual(resolved.data_root, local / APP_NAME)
        for name in ALL_DIR_NAMES:
            with self.subTest(directory=name):
                self.assertEqual(getattr(resolved, name), local / APP_NAME / name)

    def test_development_mode_preserves_the_historical_project_layout(self) -> None:
        with self.env():
            resolved = resolve_runtime_paths()

        root = project_root()
        self.assertEqual(resolved.mode, MODE_DEVELOPMENT)
        self.assertEqual(resolved.data_root, root)
        self.assertEqual(resolved.outputs, root / "outputs")
        self.assertEqual(resolved.logs, root / "outputs")
        self.assertEqual(resolved.work, root / "work")
        self.assertEqual(resolved.screenshots, root / "outputs" / "screenshots")
        self.assertEqual(resolved.html_diagnostics, root / "outputs" / "html_diagnostics")
        self.assertEqual(resolved.cache, root / "outputs" / "cache")
        self.assertEqual(resolved.storage, root / "storage")
        self.assertEqual(resolved.uploads, root / "storage" / "uploads")
        self.assertEqual(resolved.browser_sessions, root / "storage")
        self.assertEqual(resolved.config, root / "storage" / "config")
        # The existing signed-in sessions keep their exact current location and filenames.
        self.assertEqual(resolved.linkedin_state_file, root / "storage" / "linkedin_state.json")
        self.assertEqual(resolved.dice_state_file, root / "storage" / "dice_state.json")

    def test_mode_detection_follows_sys_frozen(self) -> None:
        with self.env():
            with patch.object(paths_module.sys, "frozen", True, create=True):
                self.assertTrue(paths_module.is_packaged())
            self.assertFalse(paths_module.is_packaged())

    def test_resolution_creates_nothing(self) -> None:
        """Importing/resolving must have no side effects -- creation is explicit."""
        root = self.temp_root / "untouched"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolve_runtime_paths()
        self.assertFalse(root.exists(), "resolution must not create directories")


class NoRoamingApplicationDataTests(_PathTestCase):
    """%APPDATA% roams between computers in managed Windows environments. Using it would let
    resumes, uploads, history, configuration and portal sessions appear on another machine,
    so no runtime or persistent path may resolve under it."""

    def test_no_packaged_path_resolves_under_appdata(self) -> None:
        local = self.temp_root / "Local"
        roaming = self.temp_root / "Roaming"
        with self.packaged_env(local, roaming):
            resolved = resolve_runtime_paths()
            reset_settings_cache()
            settings = Settings()

        checked = list(resolved.directories()) + list(settings.writable_directories())
        checked += [resolved.linkedin_state_file, resolved.dice_state_file,
                    settings.linkedin_storage_state, settings.dice_storage_state]
        for path in checked:
            with self.subTest(path=path):
                self.assertFalse(
                    str(path).startswith(str(roaming)),
                    f"{path} resolved under roaming application data",
                )

    def test_appdata_is_never_consulted_even_when_localappdata_is_absent(self) -> None:
        """With %LOCALAPPDATA% unset the fallback is still a LOCAL directory, never %APPDATA%."""
        roaming = self.temp_root / "Roaming"
        with self.env(**{MODE_ENV: MODE_PACKAGED, "APPDATA": str(roaming)}):
            resolved = resolve_runtime_paths()

        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertFalse(str(directory).startswith(str(roaming)))
        if os.name == "nt":
            self.assertEqual(resolved.data_root, Path.home() / "AppData" / "Local" / APP_NAME)

    def test_no_resolved_path_contains_a_roaming_segment(self) -> None:
        local = self.temp_root / "Local"
        for env in (self.packaged_env(local, self.temp_root / "Roaming"),
                    self.env(**{DATA_DIR_ENV: str(self.temp_root / "override")}),
                    self.env()):
            with env:
                reset_runtime_paths_cache()
                resolved = resolve_runtime_paths()
            for directory in resolved.directories():
                parts = {part.lower() for part in directory.parts}
                with self.subTest(mode=resolved.mode, directory=directory):
                    self.assertNotIn("roaming", parts)

    def test_no_roaming_environment_variable_is_exposed(self) -> None:
        """The roaming override was removed; nothing may reintroduce it by name."""
        exported = {name for name in dir(paths_module) if name.endswith("_ENV")}
        for name in exported:
            with self.subTest(setting=name):
                self.assertNotIn("ROAMING", name)
        self.assertFalse(hasattr(paths_module, "ROAMING_DIR_NAMES"))


class PackagedLocalAppDataLayoutTests(_PathTestCase):
    """Every mutable directory the specification lists must sit directly under
    %LOCALAPPDATA%\\AIJobAutomation."""

    REQUIRED = ("outputs", "logs", "work", "screenshots", "html_diagnostics", "cache",
                "storage", "uploads", "browser_sessions", "config")

    def test_all_ten_directories_are_present_and_local(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths()

        expected_root = local / APP_NAME
        for name in self.REQUIRED:
            with self.subTest(directory=name):
                self.assertEqual(getattr(resolved, name), expected_root / name)

    def test_every_settings_path_is_under_the_single_local_root(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            reset_settings_cache()
            settings = Settings()

        expected_root = str(local / APP_NAME)
        checked = list(settings.writable_directories())
        checked += [settings.linkedin_storage_state, settings.dice_storage_state]
        for path in checked:
            with self.subTest(path=path):
                self.assertTrue(str(path).startswith(expected_root), f"{path} escaped {expected_root}")

    def test_the_app_folder_name_is_used_verbatim(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local):
            resolved = resolve_runtime_paths()
        self.assertEqual(resolved.data_root.name, APP_NAME)
        self.assertEqual(resolved.data_root.parent, local)


class EnvironmentOverrideTests(_PathTestCase):
    def test_data_dir_override_redirects_everything(self) -> None:
        root = self.temp_root / "custom-root"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolved = resolve_runtime_paths()

        self.assertEqual(resolved.mode, MODE_OVERRIDE)
        self.assertEqual(resolved.data_root, root)
        for name in ALL_DIR_NAMES:
            with self.subTest(directory=name):
                self.assertEqual(getattr(resolved, name), root / name)

    def test_override_wins_over_packaged_mode(self) -> None:
        root = self.temp_root / "wins"
        with self.env(**{DATA_DIR_ENV: str(root), MODE_ENV: MODE_PACKAGED,
                         "LOCALAPPDATA": str(self.temp_root / "ignored")}):
            resolved = resolve_runtime_paths()
        self.assertEqual(resolved.mode, MODE_OVERRIDE)
        self.assertTrue(str(resolved.outputs).startswith(str(root)))

    def test_blank_override_is_ignored(self) -> None:
        with self.env(**{DATA_DIR_ENV: "   "}):
            self.assertEqual(resolve_runtime_paths().mode, MODE_DEVELOPMENT)

    def test_local_data_dir_override_moves_the_packaged_root(self) -> None:
        custom = self.temp_root / "only-local"
        with self.env(**{MODE_ENV: MODE_PACKAGED, LOCAL_DIR_ENV: str(custom),
                         "LOCALAPPDATA": str(self.temp_root / "ignored")}):
            resolved = resolve_runtime_paths()
        self.assertEqual(resolved.data_root, custom)
        self.assertEqual(resolved.outputs, custom / "outputs")
        self.assertEqual(resolved.uploads, custom / "uploads")
        self.assertEqual(resolved.browser_sessions, custom / "browser_sessions")

    def test_local_data_dir_override_moves_the_development_root(self) -> None:
        custom = self.temp_root / "dev-elsewhere"
        with self.env(**{LOCAL_DIR_ENV: str(custom)}):
            resolved = resolve_runtime_paths()
        self.assertEqual(resolved.mode, MODE_DEVELOPMENT)
        self.assertEqual(resolved.outputs, custom / "outputs")
        self.assertEqual(resolved.linkedin_state_file, custom / "storage" / "linkedin_state.json")

    def test_environment_variables_expand(self) -> None:
        target = self.temp_root / "expanded"
        with self.env(**{"MY_TEST_BASE": str(self.temp_root),
                         DATA_DIR_ENV: os.path.join("%MY_TEST_BASE%", "expanded")
                         if os.name == "nt" else str(target)}):
            resolved = resolve_runtime_paths()
        self.assertTrue(str(resolved.data_root).startswith(str(self.temp_root)))

    def test_cache_is_shared_until_explicitly_reset(self) -> None:
        root = self.temp_root / "cached"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            first = runtime_paths()
            self.assertIs(first, runtime_paths(), "layout must be resolved once per process")
            reset_runtime_paths_cache()
            self.assertIsNot(first, runtime_paths())


class IsolatedTemporaryDirectoryTests(_PathTestCase):
    def test_tests_can_redirect_away_from_the_real_application_data(self) -> None:
        root = self.temp_root / "isolated"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings().ensure_directories()

            for directory in settings.writable_directories():
                with self.subTest(directory=directory):
                    self.assertTrue(str(directory).startswith(str(root)))
            # Nothing may resolve into the real user profile or the source checkout.
            self.assertFalse(str(settings.app_upload_dir).startswith(str(project_root())))
            for variable in ("APPDATA", "LOCALAPPDATA"):
                real = os.environ.get(variable) or ""
                if real:
                    self.assertFalse(str(settings.app_upload_dir).startswith(real))


class SeparateComputerTests(_PathTestCase):
    """Two computers must never see each other's data. There is no sync in this design, so
    the proof is that no writable location is shared between two simulated machines."""

    def test_two_simulated_computers_share_no_writable_location(self) -> None:
        with self.packaged_env(self.temp_root / "PC-1" / "Local",
                               self.temp_root / "PC-1" / "Roaming"):
            reset_runtime_paths_cache()
            first = resolve_runtime_paths()
        with self.packaged_env(self.temp_root / "PC-2" / "Local",
                               self.temp_root / "PC-2" / "Roaming"):
            reset_runtime_paths_cache()
            second = resolve_runtime_paths()

        self.assertNotEqual(first.data_root, second.data_root)
        self.assertNotEqual(first.linkedin_state_file, second.linkedin_state_file)
        self.assertNotEqual(first.dice_state_file, second.dice_state_file)
        self.assertNotEqual(first.uploads, second.uploads)
        self.assertNotEqual(first.config, second.config)
        overlap = {str(p) for p in first.directories()} & {str(p) for p in second.directories()}
        self.assertEqual(overlap, set(), "two machines must share no writable location")

    def test_data_written_on_one_computer_is_invisible_to_the_other(self) -> None:
        with self.packaged_env(self.temp_root / "PC-1" / "Local"):
            reset_runtime_paths_cache()
            first = resolve_runtime_paths().ensure()
            first.linkedin_state_file.write_text('{"cookies": []}', encoding="utf-8")
            (first.uploads / "resume.pdf").write_bytes(b"%PDF-1.4")
            (first.config / "settings.json").write_text("{}", encoding="utf-8")

        with self.packaged_env(self.temp_root / "PC-2" / "Local"):
            reset_runtime_paths_cache()
            second = resolve_runtime_paths().ensure()

        self.assertFalse(second.linkedin_state_file.exists(), "a session followed the user")
        self.assertFalse((second.uploads / "resume.pdf").exists(), "an upload followed the user")
        self.assertFalse((second.config / "settings.json").exists(), "config followed the user")

    def test_two_windows_accounts_on_one_computer_stay_separate(self) -> None:
        base = self.temp_root / "Users"
        with self.packaged_env(base / "alice" / "AppData" / "Local"):
            reset_runtime_paths_cache()
            alice = resolve_runtime_paths()
        with self.packaged_env(base / "bob" / "AppData" / "Local"):
            reset_runtime_paths_cache()
            bob = resolve_runtime_paths()

        overlap = {str(p) for p in alice.directories()} & {str(p) for p in bob.directories()}
        self.assertEqual(overlap, set())


class DirectoryCreationTests(_PathTestCase):
    def test_ensure_creates_every_directory(self) -> None:
        root = self.temp_root / "created"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolved = resolve_runtime_paths()
            self.assertFalse(root.exists())
            resolved.ensure()

        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir(), f"{directory} was not created")

    def test_ensure_is_idempotent_and_preserves_existing_files(self) -> None:
        root = self.temp_root / "idempotent"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolved = resolve_runtime_paths().ensure()
            marker = resolved.outputs / "keep.json"
            marker.write_text("{}", encoding="utf-8")
            resolved.ensure()

        self.assertTrue(marker.exists(), "ensure() must never clear existing data")

    def test_settings_ensure_directories_creates_session_parents(self) -> None:
        root = self.temp_root / "settings-created"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings().ensure_directories()

        self.assertTrue(settings.linkedin_storage_state.parent.is_dir())
        self.assertTrue(settings.dice_storage_state.parent.is_dir())
        self.assertTrue(settings.app_upload_dir.is_dir())


class SpacesAndUnicodePathTests(_PathTestCase):
    AWKWARD = "Ünïcode Földer — 空白 ある"

    def test_paths_with_spaces_and_unicode_resolve_and_create(self) -> None:
        root = self.temp_root / self.AWKWARD / "AI Job Automation"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolved = resolve_runtime_paths().ensure()

        self.assertIn(self.AWKWARD, str(resolved.outputs))
        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())

    def test_files_can_be_written_and_read_under_an_awkward_path(self) -> None:
        root = self.temp_root / self.AWKWARD / "Program Data"
        with self.env(**{DATA_DIR_ENV: str(root)}):
            resolved = resolve_runtime_paths().ensure()
            payload = '{"café": "naïve — 空白"}'
            resolved.linkedin_state_file.write_text(payload, encoding="utf-8")
            self.assertEqual(
                resolved.linkedin_state_file.read_text(encoding="utf-8"), payload
            )

    def test_a_username_style_path_with_spaces_is_handled(self) -> None:
        root = self.temp_root / "Users" / "First Last" / "AppData" / "Local"
        with self.packaged_env(root):
            resolved = resolve_runtime_paths().ensure()
        self.assertTrue(resolved.outputs.is_dir())
        self.assertIn("First Last", str(resolved.outputs))


class PersistentVersusDisposableTests(_PathTestCase):
    """Both groups are machine-local; they are separated so clearing diagnostics can never
    destroy a signed-in session or an uploaded resume."""

    def test_disposable_and_persistent_directories_do_not_overlap(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths()

        disposable = {str(p) for p in resolved.disposable_directories()}
        persistent = {str(p) for p in resolved.persistent_directories()}
        self.assertEqual(disposable & persistent, set())
        for path in disposable | persistent:
            with self.subTest(path=path):
                self.assertTrue(path.startswith(str(local / APP_NAME)))

    def test_deleting_disposable_data_leaves_sessions_and_uploads_intact(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths().ensure()
            resolved.linkedin_state_file.write_text("{}", encoding="utf-8")
            resume = resolved.uploads / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4")
            (resolved.outputs / "debug.json").write_text("{}", encoding="utf-8")

            for directory in resolved.disposable_directories():  # a user clearing diagnostics
                shutil.rmtree(directory, ignore_errors=True)

            self.assertTrue(resolved.linkedin_state_file.exists())
            self.assertTrue(resume.exists())


class PackagedInstallDirectoryTests(_PathTestCase):
    """A packaged install must never require write access to its own program directory."""

    PROGRAM_FILES = Path(r"C:\Program Files\AIJobAutomation")

    def test_no_writable_path_resolves_under_program_files(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths()
            reset_settings_cache()
            settings = Settings()

        forbidden = ("program files", "program files (x86)", "programfiles")
        checked = list(resolved.directories()) + list(settings.writable_directories())
        checked += [settings.linkedin_storage_state, settings.dice_storage_state]
        for path in checked:
            lowered = str(path).lower()
            with self.subTest(path=path):
                for marker in forbidden:
                    self.assertNotIn(marker, lowered)

    def test_no_writable_path_resolves_inside_the_install_directory(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths()
            reset_settings_cache()
            settings = Settings()

        install_dir = str(self.PROGRAM_FILES).lower()
        source_dir = str(project_root()).lower()
        for path in list(resolved.directories()) + list(settings.writable_directories()):
            lowered = str(path).lower()
            with self.subTest(path=path):
                self.assertFalse(lowered.startswith(install_dir))
                self.assertFalse(lowered.startswith(source_dir))

    def test_packaged_paths_stay_within_the_user_profile(self) -> None:
        local = self.temp_root / "Local"
        with self.packaged_env(local, self.temp_root / "Roaming"):
            resolved = resolve_runtime_paths()
        for directory in resolved.directories():
            with self.subTest(directory=directory):
                self.assertTrue(str(directory).startswith(str(local)))


class PortalSessionSeparationTests(_PathTestCase):
    def test_linkedin_and_dice_state_files_are_distinct(self) -> None:
        for env in ({DATA_DIR_ENV: str(self.temp_root / "root")},
                    {MODE_ENV: MODE_PACKAGED, "LOCALAPPDATA": str(self.temp_root / "l")},
                    {}):
            with self.subTest(env=sorted(env)):
                reset_runtime_paths_cache()
                with self.env(**env):
                    resolved = resolve_runtime_paths()
                self.assertNotEqual(resolved.linkedin_state_file, resolved.dice_state_file)
                self.assertEqual(resolved.linkedin_state_file.name, "linkedin_state.json")
                self.assertEqual(resolved.dice_state_file.name, "dice_state.json")

    def test_browser_session_files_are_machine_local_only(self) -> None:
        """A signed-in LinkedIn/Dice session must never be able to roam to another computer."""
        local = self.temp_root / "Local"
        roaming = self.temp_root / "Roaming"
        with self.packaged_env(local, roaming):
            resolved = resolve_runtime_paths()
            reset_settings_cache()
            settings = Settings()

        # The resolver's own answer: the dedicated machine-local session directory.
        session_root = local / APP_NAME / "browser_sessions"
        self.assertEqual(resolved.linkedin_state_file.parent, session_root)
        self.assertEqual(resolved.dice_state_file.parent, session_root)

        # Settings may point somewhere else when LINKEDIN_STORAGE_STATE/DICE_STORAGE_STATE are
        # configured (a relative value is anchored to the data root), but wherever they land
        # they must stay machine-local and must never touch roaming application data.
        for state_file in (resolved.linkedin_state_file, resolved.dice_state_file,
                           settings.linkedin_storage_state, settings.dice_storage_state):
            with self.subTest(state_file=state_file):
                self.assertTrue(str(state_file).startswith(str(local / APP_NAME)))
                self.assertFalse(str(state_file).startswith(str(roaming)))
                self.assertNotIn("roaming", {part.lower() for part in state_file.parts})

    def test_session_filenames_and_format_are_unchanged(self) -> None:
        """Playwright reads these files back; the names and JSON content must not change."""
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "sessions")}):
            resolved = resolve_runtime_paths().ensure()
            state = '{"cookies": [], "origins": []}'
            resolved.linkedin_state_file.write_text(state, encoding="utf-8")
            resolved.dice_state_file.write_text(state, encoding="utf-8")

            self.assertEqual(resolved.linkedin_state_file.read_text(encoding="utf-8"), state)
            self.assertEqual(resolved.dice_state_file.read_text(encoding="utf-8"), state)

    def test_writing_one_portal_session_does_not_affect_the_other(self) -> None:
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "one-portal")}):
            resolved = resolve_runtime_paths().ensure()
            resolved.linkedin_state_file.write_text('{"portal": "linkedin"}', encoding="utf-8")

            self.assertTrue(resolved.linkedin_state_file.exists())
            self.assertFalse(resolved.dice_state_file.exists())

    def test_settings_expose_the_same_separate_session_files(self) -> None:
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "settings-sessions")}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings()
        self.assertNotEqual(settings.linkedin_storage_state, settings.dice_storage_state)
        self.assertEqual(settings.linkedin_storage_state.name, "linkedin_state.json")
        self.assertEqual(settings.dice_storage_state.name, "dice_state.json")


class SettingsIntegrationTests(_PathTestCase):
    def test_explicit_environment_variables_still_win(self) -> None:
        """The documented per-setting overrides (APP_UPLOAD_DIR, ...) must keep working."""
        custom_uploads = self.temp_root / "explicit-uploads"
        custom_state = self.temp_root / "explicit" / "linkedin_state.json"
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "ignored"),
                         "APP_UPLOAD_DIR": str(custom_uploads),
                         "LINKEDIN_STORAGE_STATE": str(custom_state)}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings()

        self.assertEqual(settings.app_upload_dir, custom_uploads)
        self.assertEqual(settings.linkedin_storage_state, custom_state)

    def test_no_configuration_path_is_relative(self) -> None:
        """A relative path would resolve against the current working directory -- the exact
        failure mode that breaks a packaged install."""
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "absolute")}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings()

        for path in list(settings.writable_directories()) + [
            settings.linkedin_storage_state, settings.dice_storage_state
        ]:
            with self.subTest(path=path):
                self.assertTrue(Path(path).is_absolute(), f"{path} is not absolute")

    def test_development_settings_match_the_historical_layout(self) -> None:
        """The developer workflow is unchanged: same folders as before this refactor."""
        with self.env():
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings()

        root = project_root()
        self.assertEqual(settings.app_upload_dir, root / "storage" / "uploads")
        self.assertEqual(settings.dice_debug_dir, root / "outputs" / "dice_debug")
        self.assertEqual(settings.find_matches_debug_dir, root / "outputs" / "find_matches_debug")
        self.assertEqual(settings.linkedin_storage_state, root / "storage" / "linkedin_state.json")
        self.assertEqual(settings.dice_storage_state, root / "storage" / "dice_state.json")
        self.assertEqual(settings.logs_dir, root / "outputs")
        self.assertEqual(settings.work_dir, root / "work")

    def test_database_url_is_untouched_by_path_resolution(self) -> None:
        """Redirecting the writable data directories must not rewrite the database URL.

        The field carries no default any more -- a hardcoded URL in source meant a working
        production password in Git -- so the value is supplied here explicitly and checked
        for pass-through."""
        configured = "postgresql+psycopg://someone:somepassword@localhost:5432/somedb"
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "db"),
                         "DATABASE_URL": configured}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = Settings()
        self.assertEqual(settings.database_url, configured)

    def test_database_url_has_no_hardcoded_default(self) -> None:
        """A default value here would be a live credential committed to source control."""
        from pydantic_core import PydanticUndefined

        field = Settings.model_fields["database_url"]
        self.assertIs(field.default, PydanticUndefined)
        self.assertIsNotNone(field.default_factory)

    def test_get_settings_returns_a_cached_instance_with_directories_created(self) -> None:
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "cached-settings")}):
            reset_runtime_paths_cache()
            reset_settings_cache()
            settings = get_settings()
            self.assertIs(settings, get_settings())
            self.assertTrue(settings.app_upload_dir.is_dir())


class DescribeTests(_PathTestCase):
    def test_describe_exposes_paths_only_and_no_secrets(self) -> None:
        with self.env(**{DATA_DIR_ENV: str(self.temp_root / "describe"),
                         "OPENAI_API_KEY": "must-not-appear",
                         "DATABASE_URL": "postgresql://user:pw@host/db"}):
            reset_runtime_paths_cache()
            described = paths_module.describe()

        blob = " ".join(described.values()).lower()
        for secret in ("must-not-appear", "postgresql://", "pw@host"):
            self.assertNotIn(secret.lower(), blob)
        self.assertEqual(described["mode"], MODE_OVERRIDE)
        self.assertIn("linkedin_state_file", described)
        self.assertIn("work", described)
        self.assertIn("data_root", described)

    def test_runtime_paths_is_frozen(self) -> None:
        resolved = RuntimePaths(
            mode=MODE_DEVELOPMENT, data_root=self.temp_root,
            outputs=self.temp_root, logs=self.temp_root, work=self.temp_root,
            screenshots=self.temp_root, html_diagnostics=self.temp_root, cache=self.temp_root,
            storage=self.temp_root, uploads=self.temp_root, browser_sessions=self.temp_root,
            config=self.temp_root,
        )
        with self.assertRaises(Exception):
            resolved.outputs = self.temp_root / "nope"  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
