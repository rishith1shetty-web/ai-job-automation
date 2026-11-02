"""The single source of truth for the application's identity and version.

Everything that has to agree on "which application is this, and which build" reads from
here: the packaged executable's file metadata, the Inno Setup installer, Add/Remove
Programs, the installer filename, the documentation, and any future upgrade comparison.

There is deliberately no second editable copy anywhere. ``scripts/build_installer.ps1``
imports these values at build time and passes them to ISCC as preprocessor definitions, and
``tests/test_installer.py`` asserts that nothing has drifted.

APP_ID is a permanent identity, NOT a version. Inno Setup uses it to recognise an existing
installation as "the same application" so an upgrade replaces it instead of installing a
second copy next to it. Changing it would strand every existing installation, so it must
never change for the lifetime of this product.
"""

from __future__ import annotations

# --------------------------------------------------------------------------------------
# Version
# --------------------------------------------------------------------------------------

# Semantic version of the application. Bump this ONE value for a release.
VERSION = "1.3.0"

# Windows file-version metadata needs exactly four numeric components.
VERSION_TUPLE = tuple(int(part) for part in VERSION.split(".")) + (0,)
FILE_VERSION = ".".join(str(part) for part in VERSION_TUPLE)

# --------------------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------------------

APP_NAME = "Rising Path Recruiters"
# The executable and the PyInstaller output folder. No spaces: it appears in paths,
# command lines and process lists.
EXECUTABLE_NAME = "RisingPathRecruiters"
PUBLISHER = "Rising Path Recruiters"

# Permanent installation identity. Generated once; never regenerate it.
APP_ID = "{7B3D9E42-5C18-4A6F-9E27-1D4A8F0C6B35}"


def inno_app_id() -> str:
    """``APP_ID`` in the form Inno Setup needs.

    Inno treats ``{`` as the start of a constant, so a value that begins with one has to be
    written with the brace doubled. The stored identity is unchanged -- only the escaping
    differs -- which is why this is derived rather than duplicated."""
    return "{" + APP_ID if APP_ID.startswith("{") else APP_ID

# Where the installer puts the program (under {autopf}, i.e. 64-bit Program Files).
INSTALL_DIR_NAME = "Rising Path Recruiters"
# The Start-menu group.
START_MENU_GROUP = "Rising Path Recruiters"

# Machine-local writable data root, under %LOCALAPPDATA%. Must match app/paths.py:APP_NAME.
DATA_DIR_NAME = "AIJobAutomation"

COPYRIGHT = "Copyright (C) Rising Path Recruiters"
DESCRIPTION = "AI recruitment platform: resume analysis, job matching and application automation"
URL = ""


def installer_filename() -> str:
    """The deterministic installer name: same inputs, same output name, every time."""
    return f"{EXECUTABLE_NAME}-Setup-{VERSION}.exe"


def describe() -> dict[str, str]:
    """Everything a build script needs, as plain strings. Contains no secrets."""
    return {
        "version": VERSION,
        "file_version": FILE_VERSION,
        "app_name": APP_NAME,
        "executable_name": EXECUTABLE_NAME,
        "publisher": PUBLISHER,
        "app_id": APP_ID,
        "inno_app_id": inno_app_id(),
        "install_dir_name": INSTALL_DIR_NAME,
        "start_menu_group": START_MENU_GROUP,
        "data_dir_name": DATA_DIR_NAME,
        "copyright": COPYRIGHT,
        "description": DESCRIPTION,
        "url": URL,
        "installer_filename": installer_filename(),
    }


if __name__ == "__main__":  # pragma: no cover - build-script helper
    import json

    print(json.dumps(describe(), indent=2))
