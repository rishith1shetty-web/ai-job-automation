"""What the Windows installer calls into. Runs from the PACKAGED application, never from a
development virtual environment.

The installer is a wizard, not a shell: it can run a program and read an exit code, and that
is all. Everything it needs to decide therefore has to be an exit code, and everything it
needs to display has to be a non-secret JSON file it can read afterwards.

PostgreSQL is not installed by this application
----------------------------------------------
It is installed by hand, before setup runs. Setup's job is to *refuse to proceed* until a
suitable server is there, and then to provision the application's own role and database
inside it. ``require-postgres`` is the gate that makes that refusal precise.

The elevation boundary is the reason this module exists at all
-------------------------------------------------------------
Inno Setup runs elevated so it can write to Program Files. But the database credential is
protected with DPAPI in **current-user** scope, and "current user" for an elevated installer
is whichever account approved the UAC prompt -- often not the person who will actually use
the application. A credential written there would be undecryptable by the real user, and the
application would ask to be provisioned again on every start.

So the work is split by *what privilege it genuinely needs*, not by convenience:

===========================  ===========  =========================================
step                         context      why
===========================  ===========  =========================================
``detect``                   either       reads only; no privilege needed
``require-postgres``         either       reads only; the pre-installation gate
``begin-handoff``            orig. user   creates the protected, empty handoff file
                                          in the user's own data directory
*(wizard writes the blob)*   elevated     DPAPI-protects the typed password in memory
``authenticate``             orig. user   proves the password before anything is
                                          installed or created
``provision``                orig. user   connects to PostgreSQL as its administrator
                                          -- a DATABASE credential, not a Windows one
                                          -- and stores DPAPI for the right account
``cleanup``                  orig. user   destroys the handoff whatever happened
===========================  ===========  =========================================

The key observation is that provisioning needs **no Windows elevation at all**: creating a
role and a database requires the PostgreSQL administrator password, which is unrelated to
UAC. Nothing in the database flow is elevated. That lets every credential step run as the
original user with ``runasoriginaluser``, so the DPAPI blob belongs to the account that will
start the application.

Secrets never appear in a command line, an environment variable, a log or a message box. The
administrator password reaches this module only through the DPAPI-protected handoff file
described in :mod:`app.setup_handoff`, and that file is destroyed whatever the outcome.
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
    is_compatible,
    meets_required_major,
    read_admin_password_from_environment,
    required_download_url,
    required_major,
    secure_delete,
)
from app.provisioning import Status

logger = logging.getLogger(__name__)

# Non-secret results the installer reads back to build its messages.
RESULT_FILENAME = "installer-result.json"

# Exit codes the installer branches on. Values 0-16 are app.provisioning.Status verbatim, so
# there is one numbering scheme rather than two that can drift apart.
EXIT_POSTGRES_COMPATIBLE = int(Status.SUCCESS)
EXIT_POSTGRES_MISSING = int(Status.POSTGRES_NOT_FOUND)
EXIT_PORT_OCCUPIED = int(Status.PORT_OCCUPIED_BY_OTHER)
EXIT_ADMIN_AUTH_FAILED = int(Status.ADMIN_AUTH_FAILED)
EXIT_INVALID_USAGE = int(Status.INVALID_USAGE)
# Above the provisioning range, so a new Status value can never collide with these.
EXIT_POSTGRES_STOPPED = 20
EXIT_NO_ADMIN_PASSWORD = 21
EXIT_UNEXPECTED = 22
EXIT_POSTGRES_WRONG_VERSION = 23
EXIT_HANDOFF_FAILED = 24


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
# require-postgres -- the gate that runs BEFORE anything is installed
# --------------------------------------------------------------------------------------

def require_action(host: str, port: int) -> BridgeResult:
    """Is this computer ready for the application to be installed onto it?

    Stricter than :func:`detect_action`, and used for a different purpose. ``detect`` reports
    what is there; this decides whether setup may continue at all, and every "no" carries the
    instruction that fixes it. All four of the required conditions are checked here rather
    than spread across the wizard, so there is one place to read and one place to test:

    * a PostgreSQL Windows service exists,
    * that service is running,
    * ``localhost:5432`` is reachable,
    * and the server answering there is the required major version.

    The fifth condition -- that the server *accepts a connection* -- is proven by
    :func:`authenticate_action`, because accepting a connection means accepting credentials
    and there are none to offer until the password page has been filled in."""
    from app.postgres_detection import detect

    report = detect(host, port)
    majors = [i.major_version for i in report.installations if i.major_version]
    best = max(majors) if majors else None
    wanted = required_major()
    found_port = report.probe.port

    detail = {
        "host": host,
        "requested_port": port,
        "port": found_port,
        "required_major": wanted,
        "major_version": best,
        "meets_required_major": meets_required_major(best),
        "installed": report.installed,
        "running": report.running,
        "service_present": bool(report.services),
        "postgres_on_port": report.probe.is_postgres,
        "port_occupied_by_other": report.probe.occupied_by_other,
        "download": required_download_url(),
        "services": [{"name": s.name, "state": s.state} for s in report.services],
        "detection": report.to_dict(),
    }

    install_it = (f"Install PostgreSQL {wanted} for Windows x86-64, keep the default port "
                  f"{port}, and run this setup again.")

    if not report.installed and not report.probe.is_postgres:
        return BridgeResult("require-postgres", EXIT_POSTGRES_MISSING,
                            f"PostgreSQL {wanted} is not installed on this computer. "
                            f"{install_it}", detail)
    if report.probe.occupied_by_other:
        return BridgeResult("require-postgres", EXIT_PORT_OCCUPIED,
                            f"Port {found_port} is in use by a program that is not "
                            f"PostgreSQL. Stop that program and run this setup again.",
                            detail)
    if not report.probe.is_postgres:
        return BridgeResult("require-postgres", EXIT_POSTGRES_STOPPED,
                            f"PostgreSQL is installed but is not answering on {host}:{port}. "
                            f"Start the PostgreSQL service in Windows Services, then run "
                            f"this setup again.", detail)
    # Reachable, so the version question is the last one -- and it is asked against what is
    # installed rather than what is running, because a computer can carry more than one.
    if not meets_required_major(best):
        found = f"PostgreSQL {best}" if best else "The installed PostgreSQL"
        return BridgeResult("require-postgres", EXIT_POSTGRES_WRONG_VERSION,
                            f"{found} is not the version this application requires. "
                            f"{install_it}", detail)
    return BridgeResult("require-postgres", EXIT_POSTGRES_COMPATIBLE,
                        f"PostgreSQL {best} is running on {host}:{found_port}.", detail)


# --------------------------------------------------------------------------------------
# The secure handoff
# --------------------------------------------------------------------------------------

def begin_handoff_action(handoff: Path | None) -> BridgeResult:
    """Create the empty, ACL-restricted file the wizard will write the protected password to.

    Runs as the ORIGINAL user, which is what gives the file the right owner and the right
    SID in its ACL. It also proves DPAPI works for this account *before* the password page
    accepts anything, so a computer where the handoff cannot work says so at the page rather
    than half-way through provisioning."""
    from app import setup_handoff

    if not setup_handoff.self_check():
        return BridgeResult(
            "begin-handoff", EXIT_HANDOFF_FAILED,
            "Windows could not protect a value for this account, so setup cannot carry the "
            "password securely. Sign in as the account that will use the application and "
            "run setup again.",
            {"handoff_file": None},
        )
    try:
        target = setup_handoff.create_channel(handoff)
    except setup_handoff.HandoffError as error:
        return BridgeResult("begin-handoff", EXIT_HANDOFF_FAILED, str(error),
                            {"handoff_file": str(handoff) if handoff else None})

    from app.windows_acl import describe_access

    return BridgeResult("begin-handoff", int(Status.SUCCESS),
                        "The secure handoff is ready.",
                        {"handoff_file": str(target), "access": describe_access(target)})


def _handoff_password(handoff: Path | None) -> tuple[str, str]:
    """From the DPAPI handoff, or -- for a support engineer only -- the environment.

    Returns ``(password, reason)``. The reason is a sentence for a person, never an
    exception: a handoff that cannot be read is an installation problem to describe, not a
    traceback that might carry fragments of the value."""
    from app import setup_handoff

    try:
        return setup_handoff.read_channel(handoff), ""
    except setup_handoff.HandoffError as error:
        fallback = read_admin_password_from_environment()
        if fallback:
            return fallback, ""
        return "", str(error)


def authenticate_action(host: str, port: int, handoff: Path | None) -> BridgeResult:
    """Prove the administrator password works, and change nothing.

    Called from the password page, so that a mistyped password is corrected on that page --
    with the application not yet installed and no role, database or credential created.
    Retrying simply means writing a new blob and calling this again."""
    from app.provisioning import ADMIN_USER, admin_can_authenticate

    detail = {"host": host, "port": port, "user": ADMIN_USER,
              "handoff_file": str(handoff) if handoff else None}

    password, reason = _handoff_password(handoff)
    if not password:
        return BridgeResult("authenticate", EXIT_HANDOFF_FAILED,
                            reason or "No PostgreSQL administrator password was supplied.",
                            detail)
    try:
        accepted, why = admin_can_authenticate(password, host=host, port=port)
    finally:
        password = ""

    if accepted:
        return BridgeResult("authenticate", int(Status.SUCCESS),
                            f"PostgreSQL accepted the {ADMIN_USER} password.", detail)
    return BridgeResult("authenticate", EXIT_ADMIN_AUTH_FAILED,
                        why or "The password was not accepted by PostgreSQL.", detail)


def cleanup_action(handoff: Path | None) -> BridgeResult:
    """Destroy the handoff, whatever happened. Safe to call when there is nothing to destroy.

    Called on every exit path the wizard has, including Cancel, so that abandoning setup at
    the password page does not leave protected material behind."""
    from app import setup_handoff

    try:
        removed = setup_handoff.destroy_channel(handoff)
        return BridgeResult("cleanup", int(Status.SUCCESS),
                            "The secure handoff was removed." if removed
                            else "There was nothing to remove.",
                            {"removed": removed,
                             "residue": setup_handoff.residue(handoff)})
    except setup_handoff.HandoffError as error:
        return BridgeResult("cleanup", EXIT_HANDOFF_FAILED, str(error),
                            {"removed": False, "residue": setup_handoff.residue(handoff)})


# --------------------------------------------------------------------------------------
# provision
# --------------------------------------------------------------------------------------

def provision_action(host: str, port: int, database: str, role: str,
                     handoff: Path | None, keep_handoff: bool = False,
                     store=None) -> BridgeResult:
    """Create or adopt the role and database, migrate, and store the credential.

    Runs as the ORIGINAL user: the DPAPI blob has to belong to the account that will start
    the application. The handoff is destroyed here whatever the outcome, so a failed
    installation never leaves protected administrator material on disk."""
    from app import setup_handoff
    from app.provisioning import provision

    # One try/finally around BOTH steps. Reading the password used to sit outside it, so a
    # file that could not be read left the administrator password on disk -- exactly the
    # state a failed installation must never leave behind.
    try:
        password, reason = _handoff_password(handoff)
        if not password:
            message = "No PostgreSQL administrator password was supplied."
            if reason:
                message = f"{message} {reason}"
            return BridgeResult(
                "provision", EXIT_NO_ADMIN_PASSWORD, message,
                {"expected_handoff_file": str(handoff) if handoff else None},
            )

        result = provision(password, host=host, port=port, database=database,
                           role=role, store=store)
    finally:
        password = ""
        if handoff and not keep_handoff:
            try:
                secure_delete(handoff)
            except Exception as error:  # never mask the provisioning outcome
                logger.warning("Could not remove the secure handoff: %s",
                               type(error).__name__)
        residue = setup_handoff.residue(handoff)
        if residue != "none":
            logger.warning("The secure handoff was not fully removed: %s", residue)

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
            # Reported so that "no secret material remains" is something the verifier can
            # read back, rather than something the installer merely intended.
            "handoff_residue": residue,
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

ACTIONS = ("detect", "require-postgres", "begin-handoff", "authenticate", "provision",
           "status", "cleanup")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="RisingPathRecruiters --installer",
        description="Installer support actions for the packaged application.",
        epilog="The PostgreSQL administrator password is NEVER accepted as an argument, and "
               "the installer never passes it in the environment either: it arrives only in "
               "the DPAPI-protected handoff named by --handoff-file. On Windows, any user "
               f"can read the command line of any process. {ADMIN_PASSWORD_ENV} remains as a "
               "manual, out-of-band path for support use.",
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--handoff-file", type=Path, default=None,
                        help="The DPAPI-protected handoff. Defaults to the standard location.")
    parser.add_argument("--keep-handoff", action="store_true",
                        help="Do not destroy the handoff (diagnostics only).")
    parser.add_argument("--result-file", type=Path, default=None,
                        help="Where to write the non-secret JSON result.")
    parser.add_argument("--json", action="store_true", help="Also print the result.")
    return parser


def run(args: argparse.Namespace) -> BridgeResult:
    from app.setup_handoff import handoff_path

    handoff = args.handoff_file or handoff_path()
    if args.action == "detect":
        return detect_action(args.host, args.port)
    if args.action == "require-postgres":
        return require_action(args.host, args.port)
    if args.action == "begin-handoff":
        return begin_handoff_action(handoff)
    if args.action == "authenticate":
        return authenticate_action(args.host, args.port, handoff)
    if args.action == "provision":
        return provision_action(args.host, args.port, args.database, args.role,
                                handoff, args.keep_handoff)
    if args.action == "cleanup":
        return cleanup_action(handoff)
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
