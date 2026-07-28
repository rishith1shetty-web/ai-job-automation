"""Construct the unattended PostgreSQL installation, without ever exposing its password.

This module BUILDS a command line and an option file. It deliberately does not run anything:
executing the PostgreSQL installer is the installer's job, and keeping the construction
separate is what makes it testable without installing a database server.

**Why an option file rather than command-line arguments.** The EDB installer accepts
``--superpassword`` on the command line, and that is exactly what must not be used: on
Windows any user can read the full command line of any running process (``wmic process get
CommandLine``, ``Get-CimInstance Win32_Process``). The InstallBuilder framework the EDB
installer is built on supports ``--optionfile <path>``, which reads the same settings from a
file -- so the password reaches the installer through a file this module creates with a
restrictive ACL and deletes immediately afterwards.

**Smallest supported server installation.** ``pgAdmin`` and ``StackBuilder`` are disabled:
the application talks to the server over the wire and never opens a GUI, and Stack Builder
downloads further software at install time. The command-line tools are kept, because
``psql``/``pg_dump`` are what a support engineer needs for recovery.

Options used here are EDB's documented unattended-installation parameters. They have NOT been
validated against this specific installer's ``--help`` output, because running the PostgreSQL
installer on a development machine was explicitly out of scope for this phase --
:func:`help_command` exists for that check when it is authorised.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import string
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the pinned installer and its metadata live, relative to the project/install root.
PREREQUISITES_DIRNAME = "prerequisites"
MANIFEST_FILENAME = "prerequisites.json"

DEFAULT_SUPERACCOUNT = "postgres"
DEFAULT_PORT = 5432
# Components the application genuinely does not need. Keeping the set explicit (rather than
# an --enable-components allowlist) means a future installer that adds a component does not
# silently lose something the application depends on.
DISABLED_COMPONENTS = ("pgAdmin", "stackbuilder")

# The environment variable an installer may use to hand over an administrator password.
# A child process's environment is readable only by the same user (or an administrator) --
# unlike its command line, which any user can read.
ADMIN_PASSWORD_ENV = "AI_JOB_AUTOMATION_ADMIN_PASSWORD"

# Alphabet for the generated PostgreSQL administrator password. Excludes quotes, backslashes
# and characters that would need escaping in an option file or a connection URL.
_ADMIN_ALPHABET = string.ascii_letters + string.digits + "-_.~"
ADMIN_PASSWORD_LENGTH = 32


class OptionFileError(RuntimeError):
    """The protected option file could not be written, secured or removed."""


def generate_admin_password(length: int = ADMIN_PASSWORD_LENGTH) -> str:
    """A cryptographically strong temporary administrator password.

    Temporary in the real sense: it exists only until the application role has been created,
    and is never persisted anywhere by this application."""
    return "".join(secrets.choice(_ADMIN_ALPHABET) for _ in range(length))


# --------------------------------------------------------------------------------------
# The pinned prerequisite
# --------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Prerequisite:
    """One pinned download, exactly as recorded in installer/prerequisites.json."""

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
    """Where the pinned prerequisite metadata lives.

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
    """Is an already-installed PostgreSQL good enough to use as-is?

    Any supported major version counts. Reinstalling over a working server -- or installing a
    second one beside it -- would be far more disruptive than using what is already there."""
    return major is not None and major >= minimum_supported_major(manifest)


# --------------------------------------------------------------------------------------
# The option file
# --------------------------------------------------------------------------------------

@dataclass
class InstallPlan:
    """Everything the unattended installation needs. ``superpassword`` never reaches argv."""

    installer: Path
    prefix: Path | None = None
    datadir: Path | None = None
    port: int = DEFAULT_PORT
    superaccount: str = DEFAULT_SUPERACCOUNT
    locale: str = "Default locale"
    disabled_components: tuple[str, ...] = DISABLED_COMPONENTS
    extra: dict[str, str] = field(default_factory=dict)

    def option_lines(self, superpassword: str) -> list[str]:
        """The ``key=value`` lines of the InstallBuilder option file.

        This is the ONLY place the password appears, and the file that receives it is
        created with a restrictive ACL and deleted immediately after the installation."""
        options: dict[str, str] = {
            "mode": "unattended",
            "unattendedmodeui": "minimal",
            "superaccount": self.superaccount,
            "superpassword": superpassword,
            "serverport": str(self.port),
            "locale": self.locale,
            "install_runtimes": "1",
        }
        if self.prefix:
            options["prefix"] = str(self.prefix)
        if self.datadir:
            options["datadir"] = str(self.datadir)
        if self.disabled_components:
            options["disable-components"] = ",".join(self.disabled_components)
        options.update(self.extra)
        return [f"{key}={value}" for key, value in options.items()]

    def command(self, option_file: Path) -> list[str]:
        """The command to run. Contains NO password -- only the path to the option file."""
        return [str(self.installer), "--optionfile", str(option_file)]


def help_command(installer: Path) -> list[str]:
    """The command that prints the installer's supported options.

    Not run automatically: executing the PostgreSQL installer was out of scope for this
    phase. It exists so the option names above can be confirmed against the real binary the
    moment that is authorised."""
    return [str(installer), "--help"]


def restrict_to_current_user(path: Path) -> None:
    """Remove inherited access and grant only this user, SYSTEM and Administrators.

    Administrators are included deliberately: the PostgreSQL installer runs elevated and has
    to read the file, and an administrator can read any file on the machine regardless. What
    this removes is access by OTHER standard users of the computer."""
    if os.name != "nt":  # pragma: no cover - Windows-only protection
        path.chmod(0o600)
        return

    account = os.environ.get("USERNAME") or ""
    grants = ["*S-1-5-18:(F)", "*S-1-5-32-544:(F)"]  # SYSTEM, Administrators (well-known SIDs)
    if account:
        grants.insert(0, f"{account}:(F)")
    completed = subprocess.run(
        ["icacls", str(path), "/inheritance:r", *sum((["/grant:r", g] for g in grants), [])],
        capture_output=True, text=True, timeout=30,
    )
    if completed.returncode != 0:
        raise OptionFileError(
            f"Could not restrict access to the temporary option file: "
            f"{(completed.stderr or completed.stdout).strip()}"
        )


def write_option_file(path: Path, plan: InstallPlan, superpassword: str) -> Path:
    """Write the option file and lock it down BEFORE the secret is in it.

    The file is created empty and secured first, then filled: writing the password and
    tightening the ACL afterwards would leave a window in which any user could read it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    restrict_to_current_user(path)
    path.write_text("\n".join(plan.option_lines(superpassword)) + "\n", encoding="utf-8")
    return path


def secure_delete(path: Path) -> bool:
    """Overwrite the file's bytes, then remove it.

    Overwriting is not a guarantee on a journalling or wear-levelling filesystem, and is not
    claimed to be. It removes the plain text from the obvious place; the real protections are
    the restrictive ACL and how briefly the file exists."""
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
        raise OptionFileError(f"Could not delete the temporary option file: {error}") from None


def read_admin_password_from_environment(clear: bool = True) -> str:
    """Take the administrator password from the environment, and remove it from it.

    An installer that prompted for the password passes it this way rather than as an
    argument: a process's environment is readable only by the same user or an administrator,
    while its command line is readable by every user on the computer."""
    password = os.environ.get(ADMIN_PASSWORD_ENV, "")
    if clear:
        os.environ.pop(ADMIN_PASSWORD_ENV, None)
    return password


def read_admin_password_from_option_file(path: Path) -> str:
    """Recover the generated password from the protected option file."""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == "superpassword":
            return value.strip()
    return ""
