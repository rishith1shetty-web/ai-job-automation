"""The packaged-archive integrity check.

Version 1.1.0 was built once with a corrupt PYZ entry. PyInstaller exited 0, every expected
file was present, and the secrets scan passed; the build was fully shippable. It failed on
first run with

    zlib.error: Error -3 while decompressing data: incorrect data check

while extracting ``pygments/lexers/python.py``. ``scripts/verify_archive.py`` exists so that
a build like that is rejected by the build, and these tests exist so the check cannot be
weakened or quietly disconnected from ``build_windows.ps1``.

Corruption is exercised with fake archive readers rather than by damaging a real 1 GB
executable: the reader interface is small, deliberately injectable, and a test that mutated a
real build would be both slow and destructive. The real archive is verified separately, and
only when one has been built.
"""

from __future__ import annotations

import marshal
import re
import unittest
import zlib
from pathlib import Path

from app.paths import project_root
from scripts.verify_archive import (
    ArchiveReport,
    PYZ_TYPECODE,
    find_executable,
    main,
    verify,
)

ROOT = project_root()
BUILD_SCRIPT = ROOT / "scripts" / "build_windows.ps1"
DIST_DIR = ROOT / "dist" / "RisingPathRecruiters"

CORRUPT = "Error -3 while decompressing data: incorrect data check"


def a_code_object():
    return compile("value = 1", "<test>", "exec")


# ======================================================================================
# Fake archives -- the smallest thing that behaves like PyInstaller's readers
# ======================================================================================

class FakePyz:
    """A ZlibArchiveReader stand-in. ``broken`` maps a module name to the exception raised
    when it is extracted."""

    def __init__(self, modules: list[str], broken: dict[str, Exception] | None = None) -> None:
        self.toc = list(modules)
        self._broken = broken or {}

    def extract(self, name: str):
        if name in self._broken:
            raise self._broken[name]
        return a_code_object()


class FakeCArchive:
    def __init__(self, entries: dict[str, tuple], pyzs: dict[str, FakePyz] | None = None,
                 broken: dict[str, Exception] | None = None) -> None:
        self.toc = entries
        self._pyzs = pyzs or {}
        self._broken = broken or {}

    def extract(self, name: str):
        if name in self._broken:
            raise self._broken[name]
        return b"payload"

    def open_embedded_archive(self, name: str) -> FakePyz:
        if name in self._broken:
            raise self._broken[name]
        return self._pyzs[name]


def entry(typecode: str = "m") -> tuple:
    """A table-of-contents tuple shaped like PyInstaller's: the type code is index 4."""
    return (0, 10, 20, 1, typecode)


def archive_of(*, modules: list[str] | None = None,
               broken_modules: dict[str, Exception] | None = None,
               broken_entries: dict[str, Exception] | None = None) -> FakeCArchive:
    modules = modules if modules is not None else ["app.main", "pygments.lexers.python"]
    entries = {
        "pyimod01_archive": entry("m"),
        "windows_launcher": entry("s"),
        "PYZ-00.pyz": entry(PYZ_TYPECODE),
    }
    return FakeCArchive(
        entries,
        pyzs={"PYZ-00.pyz": FakePyz(modules, broken_modules)},
        broken=broken_entries or {},
    )


def run(archive: FakeCArchive, name: str = "RisingPathRecruiters.exe") -> ArchiveReport:
    return verify(Path(name), open_archive=lambda _path: archive)


# ======================================================================================
# A sound archive
# ======================================================================================

class SoundArchiveTests(unittest.TestCase):
    def test_a_readable_archive_is_clean(self) -> None:
        report = run(archive_of())
        self.assertTrue(report.clean, report.render())
        self.assertEqual(report.failures, [])

    def test_every_carchive_entry_is_counted_including_the_pyz(self) -> None:
        report = run(archive_of())
        # two ordinary entries plus the PYZ itself
        self.assertEqual(report.carchive_entries, 3)
        self.assertEqual(report.pyz_archives, 1)

    def test_every_module_in_the_pyz_is_read(self) -> None:
        modules = [f"pkg.module{index}" for index in range(50)]
        report = run(archive_of(modules=modules))
        self.assertEqual(report.modules, 50)
        self.assertTrue(report.clean)

    def test_the_clean_report_says_so(self) -> None:
        self.assertIn("clean", run(archive_of()).render())


# ======================================================================================
# Corruption -- the failure this check exists for
# ======================================================================================

class CorruptArchiveTests(unittest.TestCase):
    def test_the_exact_1_1_0_failure_is_caught(self) -> None:
        """The real one: a zlib error extracting pygments.lexers.python from the PYZ."""
        report = run(archive_of(
            broken_modules={"pygments.lexers.python": zlib.error(CORRUPT)}))
        self.assertFalse(report.clean)
        self.assertEqual(len(report.failures), 1)

    def test_the_failing_entry_is_named(self) -> None:
        report = run(archive_of(
            broken_modules={"pygments.lexers.python": zlib.error(CORRUPT)}))
        self.assertIn("pygments.lexers.python", report.failures[0])
        self.assertIn("zlib.error", report.failures[0])
        self.assertIn(CORRUPT, report.failures[0])

    def test_the_rendered_report_names_the_entry_too(self) -> None:
        """A corrupt build has to be diagnosable from the build log alone."""
        rendered = run(archive_of(
            broken_modules={"pygments.lexers.python": zlib.error(CORRUPT)})).render()
        self.assertIn("pygments.lexers.python", rendered)
        self.assertIn("unreadable entry", rendered)
        self.assertNotIn("clean", rendered)

    def test_a_corrupt_carchive_entry_is_caught(self) -> None:
        report = run(archive_of(broken_entries={"windows_launcher": zlib.error(CORRUPT)}))
        self.assertFalse(report.clean)
        self.assertIn("CArchive windows_launcher", report.failures[0])

    def test_an_unopenable_pyz_is_caught(self) -> None:
        report = run(archive_of(broken_entries={"PYZ-00.pyz": zlib.error(CORRUPT)}))
        self.assertFalse(report.clean)
        self.assertIn("PYZ-00.pyz", report.failures[0])

    def test_an_unreadable_executable_is_caught_rather_than_raised(self) -> None:
        def explode(_path):
            raise OSError("the file is not a valid archive")

        report = verify(Path("RisingPathRecruiters.exe"), open_archive=explode)
        self.assertFalse(report.clean)
        self.assertIn("not a valid archive", report.failures[0])

    def test_every_bad_entry_is_reported_not_just_the_first(self) -> None:
        """One rebuild should fix everything, so the report must be complete."""
        report = run(archive_of(
            modules=["a", "b", "c", "d"],
            broken_modules={"a": zlib.error(CORRUPT), "c": zlib.error(CORRUPT)}))
        self.assertEqual(len(report.failures), 2)
        self.assertTrue(any("a:" in f for f in report.failures))
        self.assertTrue(any("c:" in f for f in report.failures))

    def test_a_module_that_decompresses_to_nonsense_is_caught(self) -> None:
        """Decompressing is not enough -- the bytes must also BE a code object. This is why
        the check unmarshals rather than stopping at extract()."""
        class NotACodeObject:
            pass

        class Nonsense(FakePyz):
            def extract(self, name):
                return NotACodeObject()

        archive = FakeCArchive({"PYZ-00.pyz": entry(PYZ_TYPECODE)},
                               pyzs={"PYZ-00.pyz": Nonsense(["broken.module"])})
        report = run(archive)
        self.assertFalse(report.clean)
        self.assertIn("broken.module", report.failures[0])

    def test_an_entry_that_extracts_to_nothing_is_caught(self) -> None:
        class Empty(FakePyz):
            def extract(self, name):
                return None

        archive = FakeCArchive({"PYZ-00.pyz": entry(PYZ_TYPECODE)},
                               pyzs={"PYZ-00.pyz": Empty(["silent.module"])})
        report = run(archive)
        self.assertFalse(report.clean)
        self.assertIn("silent.module", report.failures[0])

    def test_a_genuine_code_object_round_trips(self) -> None:
        """Guards the assertion itself: marshal.dumps must accept what PyInstaller returns,
        or every build would fail for the wrong reason."""
        marshal.dumps(a_code_object())


# ======================================================================================
# The command-line entry point the build script calls
# ======================================================================================

class CommandLineTests(unittest.TestCase):
    def test_a_missing_path_is_a_usage_error_not_a_pass(self) -> None:
        self.assertEqual(main(["--path", str(ROOT / "no-such-directory")]), 2)

    def test_a_directory_without_an_executable_is_a_usage_error(self) -> None:
        self.assertEqual(main(["--path", str(ROOT / "docs")]), 2)

    def test_find_executable_prefers_the_folder_name(self) -> None:
        self.assertIsNone(find_executable(ROOT / "docs"))


# ======================================================================================
# Wiring: the check must actually run, in the right place
# ======================================================================================

class BuildScriptWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = BUILD_SCRIPT.read_text(encoding="utf-8")

    def test_the_build_script_invokes_the_verifier(self) -> None:
        self.assertIn("scripts.verify_archive", self.build)

    def test_a_corrupt_archive_fails_the_build_with_its_own_exit_code(self) -> None:
        self.assertIn("exit 8", self.build)
        self.assertIn("8  THE PACKAGED ARCHIVE IS CORRUPT", self.build)

    def test_verification_runs_after_pyinstaller(self) -> None:
        """There is nothing to read back before PyInstaller has written it."""
        self.assertLess(self.build.index("-m\", \"PyInstaller\""),
                        self.build.index("scripts.verify_archive"))

    def test_verification_runs_before_the_build_is_declared_successful(self) -> None:
        self.assertLess(self.build.index("scripts.verify_archive"),
                        self.build.index("Build complete."))

    def test_the_failure_is_not_swallowed(self) -> None:
        """The exit must be anchored to the verifier's own result, not to a later step."""
        section = self.build[self.build.index("scripts.verify_archive"):]
        section = section[:section.index("scan_build_secrets")]
        self.assertRegex(section, r"if \(\$code -ne 0\)[\s\S]*exit 8")

    def test_the_verifier_does_not_modify_the_payload(self) -> None:
        """It is handed the shipped folder, so it must be incapable of writing to it."""
        source = (ROOT / "scripts" / "verify_archive.py").read_text(encoding="utf-8")
        code = re.sub(r'"""[\s\S]*?"""', "", source)      # drop docstrings
        code = re.sub(r"(?m)#.*$", "", code)              # drop comments
        for verb in ("open(", "write_text", "write_bytes", "unlink", "rmtree",
                     "shutil", "os.remove", "os.rename", "mkdir", "truncate"):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, code)


# ======================================================================================
# The real archive, when one has been built
# ======================================================================================

@unittest.skipUnless(DIST_DIR.is_dir(),
                     "no packaged build in dist/ (run scripts\\build_windows.ps1)")
class RealArchiveTests(unittest.TestCase):
    def test_the_packaged_executable_is_found(self) -> None:
        self.assertIsNotNone(find_executable(DIST_DIR))

    def test_the_built_archive_reads_back_completely(self) -> None:
        exe = find_executable(DIST_DIR)
        report = verify(exe)
        self.assertTrue(report.clean, report.render())
        self.assertGreater(report.modules, 1000, "the PYZ was not walked")
        self.assertGreater(report.carchive_entries, 5)

    def test_the_modules_that_broke_1_1_0_are_present_and_readable(self) -> None:
        """Named explicitly: these are the ones the discarded build could not extract."""
        from PyInstaller.archive.readers import CArchiveReader

        exe = find_executable(DIST_DIR)
        archive = CArchiveReader(str(exe))
        pyz = next(name for name, item in archive.toc.items() if item[4] == PYZ_TYPECODE)
        reader = archive.open_embedded_archive(pyz)
        for module in ("pygments.lexers.python", "mako.exceptions", "alembic.util.pyfiles"):
            with self.subTest(module=module):
                self.assertIn(module, reader.toc)
                marshal.dumps(reader.extract(module))

    def test_verification_leaves_the_packaged_folder_untouched(self) -> None:
        exe = find_executable(DIST_DIR)
        def fingerprint():
            return sorted((str(p), p.stat().st_size, p.stat().st_mtime_ns)
                          for p in DIST_DIR.iterdir() if p.is_file())

        before = fingerprint()
        verify(exe)
        self.assertEqual(before, fingerprint())


if __name__ == "__main__":
    unittest.main()
