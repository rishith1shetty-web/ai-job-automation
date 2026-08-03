"""Fail a build whose packaged archive cannot be read back.

PyInstaller can report a successful build and still produce an executable with a corrupt
entry inside it. Version 1.1.0 was built that way once: PyInstaller exited 0, every expected
file was present, and the secrets scan passed -- but the first run on a real machine died with

    zlib.error: Error -3 while decompressing data: incorrect data check

while extracting ``pygments/lexers/python.py`` from the embedded PYZ. It surfaced as
"Database update failed" only because Alembic happens to be the first thing that imports that
module, through ``alembic.util.pyfiles -> mako.exceptions -> pygments``.

Nothing in the build noticed, because nothing in the build ever read an entry BACK. Checking
that a file exists proves it was written; it does not prove it can be decompressed. That is
what this does, as a mandatory build step: ``build_windows.ps1`` exits 8 when it reports
anything.

Two levels:

* **CArchive** -- the table of contents appended to the .exe (bootstrap modules, run-time
  hooks, the entry-point script, and the PYZ itself). Every entry is extracted.
* **PYZ** -- every pure-Python module PyInstaller compressed into the archive. Each one is
  decompressed AND unmarshalled, because a blob can decompress to bytes that are not a valid
  code object.

Read-only by construction: the archive readers open the executable for reading and this module
never writes, moves or truncates anything under the packaged folder.
"""

from __future__ import annotations

import argparse
import marshal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# PyInstaller's CArchive type code for an embedded ZlibArchive (the PYZ).
PYZ_TYPECODE = "z"
# Index of the type code within a CArchive table-of-contents entry.
_TYPECODE_INDEX = 4


@dataclass
class ArchiveReport:
    exe: Path
    carchive_entries: int = 0
    pyz_archives: int = 0
    modules: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.failures

    def render(self) -> str:
        lines = [
            f"Archive  : {self.exe}",
            f"CArchive : {self.carchive_entries} entries read",
            f"PYZ      : {self.modules} modules read from {self.pyz_archives} archive(s)",
        ]
        if self.clean:
            lines.append("RESULT: clean -- every entry decompressed and unmarshalled.")
            return "\n".join(lines)
        lines.append(f"RESULT: {len(self.failures)} unreadable entr"
                     f"{'y' if len(self.failures) == 1 else 'ies'}.")
        # The name comes first on every line: a corrupt build has to be diagnosable from the
        # build log alone, without re-running anything.
        lines += [f"  unreadable entry: {item}" for item in self.failures]
        return "\n".join(lines)


def find_executable(root: Path) -> Path | None:
    """The packaged executable inside a PyInstaller --onedir folder.

    Prefers the one named after the folder, which is what PyInstaller produces; falls back to
    a sole .exe so this keeps working if the product is ever renamed."""
    preferred = root / f"{root.name}.exe"
    if preferred.is_file():
        return preferred
    candidates = sorted(path for path in root.glob("*.exe") if path.is_file())
    return candidates[0] if len(candidates) == 1 else None


def _describe(exc: BaseException) -> str:
    """``zlib.error: ...`` rather than ``error: ...``.

    ``zlib.error.__name__`` is only "error", which says nothing in a build log. Tracebacks
    print the qualified name, so a build failure should read the same way as the crash it is
    there to prevent."""
    kind = type(exc)
    module = getattr(kind, "__module__", "")
    name = kind.__name__ if module in ("", "builtins") else f"{module}.{kind.__name__}"
    return f"{name}: {exc}"


def _check_object(obj: Any) -> None:
    """Prove an extracted entry is genuinely intact, not merely decompressed.

    A PYZ entry comes back as a code object; re-marshalling it fails loudly on a structure
    that unpickled into nonsense. A CArchive entry comes back as bytes, where a successful
    decompression is the whole assertion."""
    if obj is None:
        raise ValueError("extract returned nothing")
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return
    marshal.dumps(obj)


def verify(exe: Path, *, open_archive: Callable[[str], Any] | None = None) -> ArchiveReport:
    """Read back every entry in ``exe``. Never raises for a corrupt archive -- the point is
    to name every bad entry, so failures are collected rather than propagated."""
    report = ArchiveReport(exe=exe)

    if open_archive is None:                      # imported lazily: PyInstaller is a build-
        from PyInstaller.archive.readers import CArchiveReader   # time dependency only.

        open_archive = CArchiveReader

    try:
        archive = open_archive(str(exe))
    except Exception as exc:                      # noqa: BLE001 - naming the fault is the job
        report.failures.append(f"{exe.name}: {_describe(exc)}")
        return report

    pyz_names: list[str] = []
    for name, entry in _toc_items(archive):
        typecode = entry[_TYPECODE_INDEX] if _has_typecode(entry) else None
        if typecode == PYZ_TYPECODE:
            pyz_names.append(name)                # walked module-by-module below
            continue
        report.carchive_entries += 1
        try:
            _check_object(archive.extract(name))
        except Exception as exc:                  # noqa: BLE001
            report.failures.append(f"CArchive {name}: {_describe(exc)}")

    for pyz_name in pyz_names:
        report.carchive_entries += 1
        try:
            reader = archive.open_embedded_archive(pyz_name)
        except Exception as exc:                  # noqa: BLE001
            report.failures.append(f"PYZ {pyz_name}: {_describe(exc)}")
            continue
        report.pyz_archives += 1
        for module in list(reader.toc):
            report.modules += 1
            try:
                _check_object(reader.extract(module))
            except Exception as exc:              # noqa: BLE001
                report.failures.append(f"PYZ {pyz_name}:{module}: {_describe(exc)}")

    return report


def _toc_items(archive: Any) -> Iterable[tuple[str, Any]]:
    toc = archive.toc
    return toc.items() if hasattr(toc, "items") else ((name, ()) for name in toc)


def _has_typecode(entry: Any) -> bool:
    return isinstance(entry, (tuple, list)) and len(entry) > _TYPECODE_INDEX


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fail the build if any packaged archive entry cannot be read back.")
    parser.add_argument("--path", required=True,
                        help="The PyInstaller --onedir output folder (or the .exe itself).")
    args = parser.parse_args(argv)

    target = Path(args.path)
    if target.is_dir():
        exe = find_executable(target)
        if exe is None:
            print(f"No packaged executable found in {target}", file=sys.stderr)
            return 2
    elif target.is_file():
        exe = target
    else:
        print(f"Not found: {target}", file=sys.stderr)
        return 2

    report = verify(exe)
    print(report.render())
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
