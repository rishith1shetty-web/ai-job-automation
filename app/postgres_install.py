"""What this application requires of a PostgreSQL server that somebody else installed.

**This module no longer installs anything.** PostgreSQL is installed by hand, before setup
runs, and the deployment model is documented in ``docs/WINDOWS_INSTALLER.md``: the person
installing the application installs PostgreSQL 16 for Windows x86-64 first, keeps port 5432,
and chooses the administrator password; setup then asks for that password on its own wizard
page and does the rest.

Everything that used to construct an unattended EDB installation -- the install plan, the
option file, the generated superuser password, the option-file command line -- has been
removed with that flow, and nothing here downloads, verifies or runs anything. What remains
is the part that is still true whoever installed the server: which major version is
acceptable, and the file primitives used to create and destroy the short-lived material setup
hands between its own processes.

The secure handoff those primitives serve now lives in :mod:`app.setup_handoff`, which
protects the administrator password with DPAPI before it reaches the filesystem at all.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the PostgreSQL requirement metadata lives, relative to the project/install root.
# It is REFERENCE DATA -- the required major version, and which official build this model was
# tested against -- not a download manifest. Nothing fetches or embeds anything from it.
PREREQUISITES_DIRNAME = "prerequisites"
MANIFEST_FILENAME = "prerequisites.json"

DEFAULT_SUPERACCOUNT = "postgres"
DEFAULT_PORT = 5432

# Retained only so that a development or support workflow can still hand a password to
# `provision` out of band. The INSTALLER never uses it: an environment variable is one of the
# places the administrator password is required not to appear.
ADMIN_PASSWORD_ENV = "AI_JOB_AUTOMATION_ADMIN_PASSWORD"


class OptionFileError(RuntimeError):
    """A protected temporary file could not be written, secured or removed."""


# --------------------------------------------------------------------------------------
# What a usable server looks like
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prerequisite:
    """The official PostgreSQL build this deployment model was tested against.

    Reference information only, read from installer/prerequisites.json: it is what the
    documentation and the setup program's instructions name when they tell somebody which
    PostgreSQL to install. Nothing downloads it, verifies it or embeds it -- the ``sha256``
    and ``authenticode_subject`` fields are the historical record of a pinning step that no
    longer runs."""

    version: str
    major: int
    filename: str
    url: str
    sha256: str | None
    size_bytes: int | None = None
    authenticode_subject: str = ""

    @property
    def pinned(self) -> bool:
        return bool(self.sha256)


def manifest_path(root: Path | None = None) -> Path:
    """Where the PostgreSQL requirement metadata lives.

    A packaged build has no source checkout, so the manifest ships with the installation and
    is found next to the executable. The source layout is the fallback, which is what a
    development run and the tests use."""
    from app.paths import install_root, project_root

    if root is not None:
        return root / "installer" / MANIFEST_FILENAME

    bundled = install_root() / "installer" / MANIFEST_FILENAME
    if bundled.is_file():
        return bundled
    return project_root() / "installer" / MANIFEST_FILENAME


def load_manifest(path: Path | None = None) -> dict:
    return json.loads((path or manifest_path()).read_text(encoding="utf-8"))


def postgres_prerequisite(manifest: dict | None = None) -> Prerequisite:
    entry = (manifest or load_manifest())["postgresql"]
    return Prerequisite(
        version=entry["version"],
        major=int(entry["major"]),
        filename=entry["filename"],
        url=entry["url"],
        sha256=(entry.get("sha256") or None),
        size_bytes=entry.get("size_bytes"),
        authenticode_subject=entry.get("authenticode_subject", ""),
    )


def minimum_supported_major(manifest: dict | None = None) -> int:
    return int((manifest or load_manifest())["minimum_supported_postgres_major"])


def is_compatible(major: int | None, manifest: dict | None = None) -> bool:
    """Is an already-provisioned PostgreSQL good enough to keep using?

    Deliberately more permissive than :func:`meets_required_major`: an existing installation
    that the application has been running against is not broken by this deployment change."""
    return major is not None and major >= minimum_supported_major(manifest)


def required_major(manifest: dict | None = None) -> int:
    """The major version setup insists on before it installs anything.

    Higher than :func:`minimum_supported_major` on purpose. The looser value governs a server
    that is already in use; this one governs a NEW installation, where there is no reason to
    accept anything other than the version the deployment model is tested on."""
    return int((manifest or load_manifest())["required_postgres_major"])


def required_download_url(manifest: dict | None = None) -> str:
    """Where the person installing this is told to get PostgreSQL."""
    return str((manifest or load_manifest())["required_postgres_download"])


def meets_required_major(major: int | None, manifest: dict | None = None) -> bool:
    """Is this the version setup requires? ``None`` (nothing installed) is never acceptable.

    Newer majors pass. Refusing PostgreSQL 17 on a computer that already has it would send
    somebody to uninstall a working database server, which is a far worse outcome than
    running against a version one ahead of the tested one."""
    return major is not None and major >= required_major(manifest)


def restrict_to_current_user(path: Path) -> None:
    """Remove inherited access and grant only this account, SYSTEM and Administrators.

    Administrators are included deliberately: setup runs elevated -- and on an installation
    approved with another user's credentials it runs as a DIFFERENT administrator account --
    so it has to be able to write the file. What this removes is access by the computer's
    other standard users.

    The account is identified by the process token's SID. Naming it with ``%USERNAME%``
    looked equivalent and was not: see ``app/windows_acl.py`` for the failure that caused."""
    from app.windows_acl import AccessRestrictionError
    from app.windows_acl import restrict_to_current_user as restrict

    try:
        restrict(path)
    except AccessRestrictionError as error:
        raise OptionFileError(
            f"Could not restrict access to the temporary file: {error}") from None


def secure_delete(path: Path) -> bool:
    """Overwrite the file's bytes, then remove it.

    Overwriting is not a guarantee on a journalling or wear-levelling filesystem, and is not
    claimed to be. It removes the bytes from the obvious place; the real protections are
    DPAPI, the restrictive ACL and how briefly the file exists."""
    if not path.is_file():
        return False
    try:
        size = path.stat().st_size
        with path.open("r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:  # pragma: no cover - best effort
        logger.debug("Could not overwrite %s before deleting it: %s", path.name, error)
    try:
        path.unlink()
        return True
    except OSError as error:
        raise OptionFileError(f"Could not delete the temporary file: {error}") from None


def read_admin_password_from_environment(clear: bool = True) -> str:
    """Take an administrator password from the environment, and remove it from it.

    **Not used by the installer.** Setup passes the password through the DPAPI-protected
    handoff in :mod:`app.setup_handoff`, precisely because an environment variable is one of
    the places the password must not appear. This remains for a support engineer running
    ``provision`` by hand on a computer they are already sitting at."""
    password = os.environ.get(ADMIN_PASSWORD_ENV, "")
    if clear:
        os.environ.pop(ADMIN_PASSWORD_ENV, None)
    return password
