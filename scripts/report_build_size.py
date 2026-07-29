"""Break the packaged build down by component, so size decisions are made on evidence.

Answers three questions a build folder cannot answer by itself: what the total is, which
components account for it, and which of them are safe to remove.

    python -m scripts.report_build_size
    python -m scripts.report_build_size --path dist\\RisingPathRecruiters --top 20

Read-only: it measures and prints, and changes nothing.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# The components worth naming separately, and where they live inside the build. Everything
# not matched by one of these is reported as "frozen Python runtime and dependencies".
COMPONENTS: tuple[tuple[str, str], ...] = (
    ("Bundled Chromium", "_internal/ms-playwright"),
    ("Playwright driver", "_internal/playwright"),
    ("Built React app", "_internal/frontend/dist"),
    ("Alembic revisions", "_internal/alembic"),
)

MB = 1024 * 1024


@dataclass
class Entry:
    name: str
    bytes_: int
    files: int

    def line(self, total: int) -> str:
        share = (self.bytes_ / total * 100) if total else 0
        return f"  {self.name:<42} {self.bytes_ / MB:>9.1f} MB  {share:>5.1f}%  {self.files:>6} files"


def measure(root: Path) -> tuple[int, int]:
    size = files = 0
    if root.is_dir():
        for path in root.rglob("*"):
            if path.is_file():
                size += path.stat().st_size
                files += 1
    elif root.is_file():
        size, files = root.stat().st_size, 1
    return size, files


def largest_packages(internal: Path, top: int) -> list[Entry]:
    """The biggest immediate children of ``_internal`` -- one row per bundled dependency."""
    entries = []
    for child in internal.iterdir():
        size, files = measure(child)
        entries.append(Entry(child.name, size, files))
    entries.sort(key=lambda entry: entry.bytes_, reverse=True)
    return entries[:top]


def report(root: Path, top: int) -> str:
    total, total_files = measure(root)
    internal = root / "_internal"

    lines = [
        f"Build      : {root}",
        f"Total      : {total / MB:.1f} MB in {total_files} files",
        "",
        "By component:",
    ]

    accounted = 0
    for name, relative in COMPONENTS:
        size, files = measure(root / relative)
        accounted += size
        lines.append(Entry(name, size, files).line(total))

    remainder = total - accounted
    lines.append(Entry("Frozen Python runtime and dependencies", remainder, 0)
                 .line(total).replace("      0 files", "             "))

    if internal.is_dir():
        lines += ["", f"Largest bundled items under _internal ({top}):"]
        lines += [entry.line(total) for entry in largest_packages(internal, top)]

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Break the packaged build down by size.")
    parser.add_argument("--path", default="dist/RisingPathRecruiters",
                        help="The packaged folder to measure.")
    parser.add_argument("--top", type=int, default=15,
                        help="How many of the largest bundled items to list.")
    args = parser.parse_args(argv)

    root = Path(args.path)
    if not root.is_dir():
        print(f"Not a build directory: {root}", file=sys.stderr)
        return 2
    print(report(root, args.top))
    return 0


if __name__ == "__main__":
    sys.exit(main())
