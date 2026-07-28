"""What the Windows installer calls into. Runs from the PACKAGED application, never from a
development virtual environment.

The installer is a wizard, not a shell: it can run a program and read an exit code, and that
is all. Everything it needs to decide therefore has to be an exit code, and everything it
needs to display has to be a non-secret JSON file it can read afterwards.

The elevation boundary is the reason this module exists at all
-------------------------------------------------------------
Inno Setup runs elevated so it can write to Program Files and install PostgreSQL. But the
database credential is protected with DPAPI in **current-user** scope, and "current user" for
an elevated installer is whichever account approved the UAC prompt -- often not the person
who will actually use the application. A credential written there would be undecryptable by
the real user, and the application would ask to be provisioned again on every start.

So the work is split by *what privilege it genuinely needs*, not by convenience:

===========================  ===========  =========================================
step                         context      why
===========================  ===========  =========================================
``detect``                   either       reads only; no privilege needed
``prepare-postgres``         orig. user   writes the protected option file into the
                                          user's own data directory
*(install PostgreSQL)*       elevated     installing a service needs administrator
``provision``                orig. user   connects to PostgreSQL as its administrator
                                          -- a DATABASE credential, not a Windows one
                                          -- and stores DPAPI for the right account
===========================  ===========  =========================================

The key observation is that provisioning needs **no Windows elevation at all**: creating a
role and a database requires the PostgreSQL administrator password, which is unrelated to
UAC. Only the PostgreSQL *installation* needs elevation. That lets the credential step run as
the original user with ``runasoriginaluser``, and removes any need to hand a secret from an
elevated process back down to an unelevated one.

Secrets never appear in a command line. The administrator password arrives either in the
protected option file this module wrote, or in an environment variable that is read once and
immediately removed -- a process's environment is readable only by the same user, while its
command line is readable by every user on the computer.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

from app.db_credentials import DEFAULT_DATABASE, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_ROLE
from app.postgres_install import (
    ADMIN_PASSWORD_ENV,
    InstallPlan,
    generate_admin_password,
    is_compatible,
    postgres_prerequisite,
    read_admin_password_from_environment,
    read_admin_password_from_option_file,
    secure_delete,
    write_option_file,
)
from app.provisioning import Status

logger = logging.getLogger(__name__)

# The protected option file lives in the user's own work directory, never in Program Files
# and never in a world-readable temp folder.
OPTION_FILENAME = "postgres-setup.options"
# Non-secret results the installer reads back to build its messages.
RESULT_FILENAME = "installer-result.json"

# Exit codes the installer branches on. Values 0-16 are app.provisioning.Status verbatim, so
# there is one numbering scheme rather than two that can drift apart.
EXIT_POSTGRES_COMPATIBLE = int(Status.SUCCESS)
EXIT_POSTGRES_MISSING = int(Status.POSTGRES_NOT_FOUND)
EXIT_PORT_OCCUPIED = int(Status.PORT_OCCUPIED_BY_OTHER)
EXIT_INVALID_USAGE = int(Status.INVALID_USAGE)
# Above the provisioning range, so a new Status value can never collide with these.
EXIT_POSTGRES_STOPPED = 20
EXIT_NO_ADMIN_PASSWORD = 21
EXIT_UNEXPECTED = 22


@dataclass
class BridgeResult:
    """A non-secret result the installer can read. Never contains a password or a URL."""

    action: str
    exit_code: int
    summary: str
    detail: dict

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "exit_code": self.exit_code,
            "summary": self.summary,
            **self.detail,
        }


def work_dir() -> Path:
    from app.paths import runtime_paths

    return runtime_paths().ensure().work


def option_file_path() -> Path:
    return work_dir() / OPTION_FILENAME


def result_file_path() -> Path:
    return work_dir() / RESULT_FILENAME


def write_result(result: BridgeResult, path: Path | None = None) -> Path:
    target = path or result_file_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    return target


# --------------------------------------------------------------------------------------
# detect
# --------------------------------------------------------------------------------------

def detect_action(host: str, port: int) -> BridgeResult:
    """Decide what the installer has to do about PostgreSQL. Changes nothing.

    Five outcomes, matching the five situations a real computer can be in: a usable server,
    a server that is installed but stopped, a supported server on a non-default port, a port
    held by something else entirely, and nothing at all."""
    from app.postgres_detection import detect

    report = detect(host, port)
    installations = report.installations
    majors = [i.major_version for i in installations if i.major_version]
    best = max(majors) if majors else None
    compatible = is_compatible(best)
    # detect() probes the configured port when it differs from the default, so this is the
    # port a server was actually found on rather than the one that was asked for.
    found_port = report.probe.port

    detail = {
        "host": host,
        "requested_port": port,
        "port": found_port,
        "installed": report.installed,
        "running": report.running,
        "postgres_on_port": report.probe.is_postgres,
        "port_occupied_by_other": report.probe.occupied_by_other,
        "major_version": best,
        "compatible": compatible,
        "services": [{"name": s.name, "state": s.state} for s in report.services],
        "detection": report.to_dict(),
    }

    if report.probe.occupied_by_other:
        return BridgeResult("detect", EXIT_PORT_OCCUPIED,
                            f"Port {found_port} is in use by a program that is not PostgreSQL.",
                            detail)
    if report.probe.is_postgres:
        if not compatible and best is not None:
            return BridgeResult(
                "detect", EXIT_POSTGRES_MISSING,
                f"PostgreSQL {best} is older than this application supports.", detail)
        return BridgeResult("detect", EXIT_POSTGRES_COMPATIBLE,
                            f"A usable PostgreSQL server is running on port {found_port}.",
                            detail)
    if report.installed:
        return BridgeResult("detect", EXIT_POSTGRES_STOPPED,
                            "PostgreSQL is installed but is not answering; its service may "
                            "be stopped.", detail)
    return BridgeResult("detect", EXIT_POSTGRES_MISSING,
                        "PostgreSQL is not installed on this computer.", detail)


# --------------------------------------------------------------------------------------
# prepare-postgres
# --------------------------------------------------------------------------------------

def prepare_action(port: int, installer: Path | None) -> BridgeResult:
    """Generate the temporary administrator password and write the protected option file.

    Runs as the ORIGINAL user so the file lands in that user's own data directory. The
    password is generated here and never returned, printed or logged; the elevated step that
    follows passes the file to the PostgreSQL installer without ever seeing the value."""
    prerequisite = postgres_prerequisite()
    plan = InstallPlan(installer=installer or Path(prerequisite.filename), port=port)
    target = option_file_path()

    password = generate_admin_password()
    try:
        write_option_file(target, plan, password)
    finally:
        password = ""  # drop the only reference held here

    return BridgeResult(
        "prepare-postgres", int(Status.SUCCESS),
        "Prepared the protected PostgreSQL installation options.",
        {
            "option_file": str(target),
            "port": port,
            "postgres_version": prerequisite.version,
            "disabled_components": list(plan.disabled_components),
            # The command the installer should run. No secret appears in it.
            "command": plan.command(target),
        },
    )


# --------------------------------------------------------------------------------------
# provision
# --------------------------------------------------------------------------------------

def _admin_password(option_file: Path | None) -> str:
    """From the environment first, then the protected option file. Never from argv."""
    password = read_admin_password_from_environment()
    if password:
        return password
    if option_file and option_file.is_file():
        return read_admin_password_from_option_file(option_file)
    return ""


def provision_action(host: str, port: int, database: str, role: str,
                     option_file: Path | None, keep_option_file: bool = False,
                     store=None) -> BridgeResult:
    """Create or adopt the role and database, migrate, and store the credential.

    Runs as the ORIGINAL user: the DPAPI blob has to belong to the account that will start
    the application. The temporary option file is destroyed here whatever the outcome, so a
    failed installation never leaves an administrator password on disk."""
    from app.provisioning import provision

    password = _admin_password(option_file)
    if not password:
        return BridgeResult(
            "provision", EXIT_NO_ADMIN_PASSWORD,
            "No PostgreSQL administrator password was supplied.",
            {"expected_environment_variable": ADMIN_PASSWORD_ENV,
             "expected_option_file": str(option_file) if option_file else None},
        )

    try:
        result = provision(password, host=host, port=port, database=database,
                           role=role, store=store)
    finally:
        password = ""
        if option_file and not keep_option_file:
            try:
                secure_delete(option_file)
            except Exception as error:  # never mask the provisioning outcome
                logger.warning("Could not remove the temporary option file: %s",
                               type(error).__name__)

    payload = result.to_dict()
    # provision() is already secret-free, but the credential mechanism description and the
    # step log are the only fields the installer shows, so only those are surfaced.
    return BridgeResult(
        "provision", int(result.status), result.message or result.status.name,
        {
            "role": result.role,
            "database": result.database,
            "host": result.host,
            "port": result.port,
            "role_created": result.role_created,
            "database_created": result.database_created,
            "ownership_transferred": result.ownership_transferred,
            "migrated": result.migrated,
            "revision": result.revision,
            "schema_verified": result.schema_verified,
            "credential_stored": result.credential_stored,
            "credential_mechanism": result.credential_mechanism,
            "steps": payload["steps"],
        },
    )


# --------------------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------------------

def status_action(host: str, port: int, database: str, role: str) -> BridgeResult:
    """Is this computer already provisioned? Used to skip work during an upgrade."""
    from app.provisioning import current_state

    state = current_state(host, port, database, role)
    ready = bool(state["can_connect"])
    return BridgeResult(
        "status", int(Status.SUCCESS if ready else Status.POSTGRES_NOT_FOUND),
        "The application database is ready." if ready
        else "The application database is not usable yet.",
        state,
    )


# --------------------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------------------

ACTIONS = ("detect", "prepare-postgres", "provision", "status")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="RisingPathRecruiters --installer",
        description="Installer support actions for the packaged application.",
        epilog="The PostgreSQL administrator password is NEVER accepted as an argument: "
               f"pass it in {ADMIN_PASSWORD_ENV}, or let --option-file supply the generated "
               "one. On Windows, any user can read the command line of any process.",
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--installer", type=Path, default=None,
                        help="Path to the pinned PostgreSQL installer (prepare-postgres).")
    parser.add_argument("--option-file", type=Path, default=None,
                        help="The protected option file. Defaults to the standard location.")
    parser.add_argument("--keep-option-file", action="store_true",
                        help="Do not delete the option file (diagnostics only).")
    parser.add_argument("--result-file", type=Path, default=None,
                        help="Where to write the non-secret JSON result.")
    parser.add_argument("--json", action="store_true", help="Also print the result.")
    return parser


def run(args: argparse.Namespace) -> BridgeResult:
    option_file = args.option_file or option_file_path()
    if args.action == "detect":
        return detect_action(args.host, args.port)
    if args.action == "prepare-postgres":
        return prepare_action(args.port, args.installer)
    if args.action == "provision":
        return provision_action(args.host, args.port, args.database, args.role,
                                option_file, args.keep_option_file)
    return status_action(args.host, args.port, args.database, args.role)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as error:  # a wizard must never show a traceback
        logger.exception("Installer action %r failed", args.action)
        result = BridgeResult(args.action, EXIT_UNEXPECTED,
                              f"The step did not complete ({type(error).__name__}).",
                              {"error_type": type(error).__name__})

    try:
        written = write_result(result, args.result_file)
        logger.info("Installer result written to %s", written)
    except OSError as error:
        logger.warning("Could not write the installer result file: %s", error)

    if args.json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        print(f"{result.action}: {result.summary}")
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - packaged entry point
    sys.exit(main())
