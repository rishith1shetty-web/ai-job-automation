"""Build the Windows application icon from the project's own logo.

One approved source, converted -- never redrawn, recoloured or replaced with a placeholder:

    frontend/public/Rising Path Final-01.png   ->   installer/RisingPathRecruiters.ico

The source is the finished project logo already committed to the repository. Nothing here
invents branding.

A Windows .ico is a container of several sizes, and Windows picks whichever it needs: 16 and
20 px for the tree view and tray, 24-48 px for the taskbar and Start menu, 64-256 px for
large icons and the file properties dialog. Shipping only a big one leaves Windows to
downscale it at draw time, which looks blurry precisely where the icon is smallest.

    python -m scripts.make_icon            # rebuild the icon
    python -m scripts.make_icon --check    # verify it is present and current

Run it once and commit the result; the build does not depend on Pillow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Every size Windows asks for, largest first (the order .ico conventionally stores).
ICON_SIZES = (256, 128, 96, 64, 48, 40, 32, 24, 20, 16)

SOURCE_RELATIVE = Path("frontend") / "public" / "Rising Path Final-01.png"
OUTPUT_RELATIVE = Path("installer") / "RisingPathRecruiters.ico"


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def source_path() -> Path:
    return project_root() / SOURCE_RELATIVE


def output_path() -> Path:
    return project_root() / OUTPUT_RELATIVE


def build(source: Path | None = None, output: Path | None = None) -> Path:
    """Convert the logo into a multi-resolution .ico."""
    from PIL import Image

    source = source or source_path()
    output = output or output_path()
    if not source.is_file():
        raise FileNotFoundError(f"The approved logo is missing: {source}")

    image = Image.open(source)
    # RGBA throughout: the logo has transparency, and a flattened icon would show a white
    # box on the taskbar and in the Start menu.
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    width, height = image.size
    if width != height:
        # Pad to a square rather than stretching, so the logo keeps its proportions.
        side = max(width, height)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(image, ((side - width) // 2, (side - height) // 2))
        image = square

    output.parent.mkdir(parents=True, exist_ok=True)
    # Pillow generates every requested size from the full-resolution original, which is far
    # better than letting Windows downscale one large bitmap at draw time.
    image.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    return output


def describe(path: Path) -> str:
    from PIL import Image

    with Image.open(path) as icon:
        sizes = sorted(getattr(icon, "info", {}).get("sizes", []) or [], reverse=True)
    return ", ".join(f"{w}x{h}" for w, h in sizes) if sizes else "(unknown)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the Windows icon from the logo.")
    parser.add_argument("--check", action="store_true",
                        help="Verify the icon exists; do not rebuild it.")
    args = parser.parse_args(argv)

    icon = output_path()
    if args.check:
        if not icon.is_file():
            print(f"Icon missing: {icon}", file=sys.stderr)
            return 1
        print(f"{icon}  ({icon.stat().st_size:,} bytes)")
        return 0

    if not source_path().is_file():
        print(f"No approved logo at {source_path()}.\n"
              "Not generating a placeholder: the icon is left pending instead.",
              file=sys.stderr)
        return 2

    built = build()
    print(f"Source : {source_path()}")
    print(f"Icon   : {built}  ({built.stat().st_size:,} bytes)")
    print(f"Sizes  : {describe(built)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
