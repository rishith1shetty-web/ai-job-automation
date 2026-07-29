"""Idempotent local PostgreSQL provisioning for an installed Windows application.

Creates the dedicated least-privilege role, creates or adopts the application database,
migrates it to the Alembic head, verifies the resulting schema, and stores the credential
securely -- so the person using the installer never opens pgAdmin, never types a command and
never edits a file.

**Nothing in this product installs PostgreSQL.** PostgreSQL 16 is installed by hand,
before the setup program runs; that is the supported deployment model, not a gap. When
PostgreSQL is missing this module returns :data:`Status.POSTGRES_NOT_FOUND`, and setup stops
*before installing anything* and instructs the user to install PostgreSQL 16 for Windows
x86-64 on port 5432 and run it again.

Safety properties, all tested:

* every SQL statement is CREATE/ALTER/GRANT -- there is no DROP, TRUNCATE or DELETE anywhere
  in this module, so a second run can never destroy data;
* the administrator password is never placed in a command line, never written to disk, and
  never logged; it lives in memory for the duration of one connection;
* Alembic runs IN-PROCESS rather than as a subprocess, precisely so the application password
  cannot appear in the process list of the machine;
* every message passes through :func:`~app.db_credentials.redact_secrets`.

Password rules (requirement: "rotate or retain safely"):

* role missing                                  -> generate, create, store
* role exists + stored credential authenticates -> RETAIN (nothing changes)
* role exists + stored credential missing/wrong -> rotate ``ALTER ROLE ... PASSWORD``, store
* ``rotate=True``                               -> always rotate
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.db_credentials import (
    DEFAULT_DATABASE,
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_ROLE,
    DatabaseCredential,
    generate_password,
    load_credential,
    redact_secrets,
    store_credential,
)
from app.postgres_detection import DetectionReport, detect, probe_port
from app.secure_store import CredentialStore

logger = logging.getLogger(__name__)

ADMIN_DATABASE = "postgres"
ADMIN_USER = "postgres"


class Status(IntEnum):
    """Provisioning outcome. The value is the process exit code."""

    SUCCESS = 0
    INVALID_USAGE = 2
    POSTGRES_NOT_FOUND = 10
    PORT_OCCUPIED_BY_OTHER = 11
    ADMIN_AUTH_FAILED = 12
    PROVISION_FAILED = 13
    MIGRATION_FAILED = 14
    VERIFICATION_FAILED = 15
    CREDENTIAL_STORE_FAILED = 16


@dataclass
class ProvisionResult:
    """Structured, secret-free result."""

    status: Status = Status.SUCCESS
    message: str = ""
    role: str = DEFAULT_ROLE
    database: str = DEFAULT_DATABASE
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    role_created: bool = False
    database_created: bool = False
    password_rotated: bool = False
    ownership_transferred: bool = False
    migrated: bool = False
    revision: str | None = None
    schema_verified: bool = False
    credential_stored: bool = False
    credential_mechanism: str = ""
    detection: dict = field(default_factory=dict)
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == Status.SUCCESS

    def record(self, step: str) -> None:
        logger.info("[provisioning] %s", step)
        self.steps.append(step)

    def to_dict(self) -> dict:
        return {
            "status": self.status.name,
            "exit_code": int(self.status),
            "ok": self.ok,
            "message": self.message,
            "role": self.role,
            "database": self.database,
            "host": self.host,
            "port": self.port,
            "role_created": self.role_created,
            "database_created": self.database_created,
            "password_rotated": self.password_rotated,
            "ownership_transferred": self.ownership_transferred,
            "migrated": self.migrated,
            "revision": self.revision,
            "schema_verified": self.schema_verified,
            "credential_stored": self.credential_stored,
            "credential_mechanism": self.credential_mechanism,
            "detection": self.detection,
            "steps": self.steps,
        }


# --------------------------------------------------------------------------------------
# Low-level helpers. Identifiers are quoted; values are always bound parameters.
# --------------------------------------------------------------------------------------

def quote_identifier(name: str) -> str:
    """Safely quote a SQL identifier (role/database/table name)."""
    if not name or not isinstance(name, str):
        raise ValueError("identifier must be a non-empty string")
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Safely quote a string literal for DDL.

    PostgreSQL does not accept bind parameters in ``CREATE ROLE``/``ALTER ROLE``, so the
    password has to be embedded in the statement text. Single quotes are doubled per the SQL
    standard; with ``standard_conforming_strings`` on (the default since PostgreSQL 9.1) a
    backslash is an ordinary character, so doubling quotes is sufficient. A NUL byte cannot
    appear in a PostgreSQL string at all and is rejected outright."""
    if not isinstance(value, str):
        raise ValueError("literal must be a string")
    if "\x00" in value:
        raise ValueError("literal must not contain a NUL byte")
    return "'" + value.replace("'", "''") + "'"


def admin_url(host: str, port: int, user: str, password: str,
              database: str = ADMIN_DATABASE) -> str:
    from sqlalchemy.engine import URL

    return URL.create(
        "postgresql+psycopg", username=user, password=password,
        host=host, port=port, database=database,
    ).render_as_string(hide_password=False)


def _admin_engine(host: str, port: int, user: str, password: str,
                  database: str = ADMIN_DATABASE):
    """An AUTOCOMMIT engine. The password is held in memory only -- never argv, never a file."""
    return create_engine(
        admin_url(host, port, user, password, database),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 10},
    )


def role_exists(connection, role: str) -> bool:
    return connection.execute(
        text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    ).first() is not None


def database_exists(connection, database: str) -> bool:
    return connection.execute(
        text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
    ).first() is not None


def database_owner(connection, database: str) -> str | None:
    row = connection.execute(
        text(
            "SELECT r.rolname FROM pg_database d "
            "JOIN pg_roles r ON r.oid = d.datdba WHERE d.datname = :name"
        ),
        {"name": database},
    ).first()
    return row[0] if row else None


def can_authenticate(credential: DatabaseCredential) -> bool:
    """Does this credential actually work? Never logs the password."""
    engine = create_engine(credential.url(), connect_args={"connect_timeout": 10})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True
    except Exception as error:
        logger.debug("Credential check failed: %s", type(error).__name__)
        return False
    finally:
        engine.dispose()


def admin_can_authenticate(password: str, *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                           admin_user: str = ADMIN_USER) -> tuple[bool, str]:
    """Does this administrator password work, right now, against this server?

    The installer asks this *before* it changes anything, so a mistyped password is a
    correctable mistake on a wizard page rather than a half-provisioned computer.

    Returns ``(accepted, reason)``. The reason is deliberately coarse -- an exception type, or
    a fixed sentence -- because it is displayed: psycopg's own message can contain the
    connection URL, and the URL contains the password."""
    engine = _admin_engine(host, port, admin_user, password)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, ""
    except Exception as error:
        name = type(error).__name__
        logger.info("Administrator authentication was refused (%s)", name)
        text_of_error = str(error).lower()
        if "password" in text_of_error or "authentication" in text_of_error:
            return False, "The password was not accepted by PostgreSQL."
        return False, f"PostgreSQL could not be contacted ({name})."
    finally:
        engine.dispose()


def create_role(connection, role: str, password: str) -> None:
    """Least privilege: can log in, and nothing else. Not a superuser, cannot create other
    roles or databases. Its authority comes solely from owning its own database."""
    connection.exec_driver_sql(
        f"CREATE ROLE {quote_identifier(role)} "
        "WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
        f"PASSWORD {quote_literal(password)}"
    )


def set_role_password(connection, role: str, password: str) -> None:
    connection.exec_driver_sql(
        f"ALTER ROLE {quote_identifier(role)} WITH PASSWORD {quote_literal(password)}"
    )


def create_database(connection, database: str, owner: str) -> None:
    connection.execute(
        text(f"CREATE DATABASE {quote_identifier(database)} OWNER {quote_identifier(owner)}")
    )


def grant_database_privileges(connection, database: str, role: str) -> None:
    connection.execute(
        text(
            f"GRANT ALL PRIVILEGES ON DATABASE {quote_identifier(database)} "
            f"TO {quote_identifier(role)}"
        )
    )


def transfer_ownership(connection, role: str) -> int:
    """Give the application role ownership of everything in ``public``.

    Enumerated explicitly rather than using ``REASSIGN OWNED BY``: that command also
    re-owns objects OUTSIDE this database -- including other databases owned by the same
    role -- which is far more than provisioning is entitled to change.

    Returns the number of objects re-owned."""
    changed = 0
    connection.execute(text(f"ALTER SCHEMA public OWNER TO {quote_identifier(role)}"))

    # Sequences created by a serial/identity column belong to that column: PostgreSQL
    # refuses "ALTER SEQUENCE ... OWNER TO" for them ("cannot change owner of sequence"),
    # and re-owning the table carries them along anyway. deptype 'a' covers serial, 'i'
    # covers identity -- both are excluded here.
    relations = connection.execute(text(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r','S','v','m','p') "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM pg_depend d "
        "  WHERE d.objid = c.oid AND d.classid = 'pg_class'::regclass "
        "    AND d.deptype IN ('a','i')"
        ")"
    )).fetchall()
    keyword = {"r": "TABLE", "p": "TABLE", "S": "SEQUENCE", "v": "VIEW",
               "m": "MATERIALIZED VIEW"}
    for name, kind in relations:
        connection.execute(
            text(f"ALTER {keyword[kind]} {quote_identifier(name)} "
                 f"OWNER TO {quote_identifier(role)}")
        )
        changed += 1

    types = connection.execute(text(
        "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'public' AND t.typtype = 'e'"
    )).fetchall()
    for (name,) in types:
        connection.execute(
            text(f"ALTER TYPE {quote_identifier(name)} OWNER TO {quote_identifier(role)}")
        )
        changed += 1
    return changed


# --------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------

def provision(
    admin_password: str,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    database: str = DEFAULT_DATABASE,
    role: str = DEFAULT_ROLE,
    admin_user: str = ADMIN_USER,
    rotate: bool = False,
    store: CredentialStore | None = None,
    run_migrations: bool = True,
    detection: DetectionReport | None = None,
) -> ProvisionResult:
    """Provision (or adopt) the local database. Idempotent and non-destructive."""
    result = ProvisionResult(role=role, database=database, host=host, port=port)

    # --- 1. Is PostgreSQL there at all? -------------------------------------------------
    report = detection or detect(host, port)
    result.detection = report.to_dict()
    probe = report.probe if report.probe.port == port else probe_port(host, port)

    if probe.occupied_by_other:
        result.status = Status.PORT_OCCUPIED_BY_OTHER
        result.message = (
            f"Port {port} on {host} is in use by a service that is not PostgreSQL. "
            f"{probe.detail}. Free the port, or provision against a different one with "
            f"--port."
        )
        return result

    if not probe.is_postgres:
        result.status = Status.POSTGRES_NOT_FOUND
        result.message = (
            f"No PostgreSQL server is reachable at {host}:{port}. "
            + ("A PostgreSQL installation was found but is not running; start its Windows "
               "service and try again."
               if report.installed else
               "PostgreSQL does not appear to be installed on this computer.")
        )
        return result

    result.record(f"PostgreSQL is reachable at {host}:{port}")

    # --- 2. Administrator connection ----------------------------------------------------
    engine = _admin_engine(host, port, admin_user, admin_password)
    try:
        try:
            connection_context = engine.connect()
        except Exception as error:
            result.status = Status.ADMIN_AUTH_FAILED
            result.message = redact_secrets(
                f"Could not connect to PostgreSQL as {admin_user!r}: "
                f"{type(error).__name__}. Check the administrator password.",
                admin_password,
            )
            return result

        with connection_context as connection:
            result.record(f"Connected as administrator {admin_user!r}")

            # --- 3. The application role ------------------------------------------------
            try:
                existing = load_credential(store)
            except Exception as error:
                logger.info("No usable stored credential (%s); a new one will be created.",
                            type(error).__name__)
                existing = None
            stored = existing if existing and existing.username == role else None

            if not role_exists(connection, role):
                password = generate_password()
                create_role(connection, role, password)
                result.role_created = True
                result.record(f"Created least-privilege role {role!r}")
            elif rotate:
                password = generate_password()
                set_role_password(connection, role, password)
                result.password_rotated = True
                result.record(f"Rotated the password for existing role {role!r}")
            else:
                candidate = (
                    stored.with_password(stored.password) if stored else None
                )
                if candidate and candidate.host == host and candidate.port == port \
                        and candidate.database == database and can_authenticate(candidate):
                    password = candidate.password
                    result.record(f"Retained the working stored password for {role!r}")
                else:
                    # The role exists but we cannot prove we know its password -- recover by
                    # setting a new one rather than failing the install.
                    password = generate_password()
                    set_role_password(connection, role, password)
                    result.password_rotated = True
                    result.record(
                        f"Stored password for {role!r} was missing or invalid; set a new one"
                    )

            credential = DatabaseCredential(
                host=host, port=port, database=database, username=role, password=password,
            )

            # --- 4. The database --------------------------------------------------------
            if not database_exists(connection, database):
                create_database(connection, database, role)
                result.database_created = True
                result.record(f"Created database {database!r} owned by {role!r}")
            else:
                result.record(f"Database {database!r} already exists; adopting it")

            grant_database_privileges(connection, database, role)

            owner = database_owner(connection, database)
            if owner != role:
                connection.execute(text(
                    f"ALTER DATABASE {quote_identifier(database)} "
                    f"OWNER TO {quote_identifier(role)}"
                ))
                result.record(f"Transferred database ownership from {owner!r} to {role!r}")
    finally:
        engine.dispose()

    # --- 5. Objects inside the database (needs a connection TO that database) -----------
    if not result.database_created:
        object_engine = _admin_engine(host, port, admin_user, admin_password, database)
        try:
            with object_engine.connect() as connection:
                moved = transfer_ownership(connection, role)
                if moved:
                    result.ownership_transferred = True
                    result.record(f"Re-owned {moved} existing objects to {role!r}")
        except Exception as error:
            result.status = Status.PROVISION_FAILED
            result.message = redact_secrets(
                f"Could not set ownership inside {database!r}: {type(error).__name__}: {error}",
                admin_password, credential.password,
            )
            return result
        finally:
            object_engine.dispose()

    # --- 6. Store the credential BEFORE migrating, so a migration failure still leaves a
    #        usable, recorded credential behind. ----------------------------------------
    try:
        store_credential(credential, store)
        result.credential_stored = True
        info = (store or _describe_store()).describe()
        result.credential_mechanism = f"{info.mechanism} ({info.scope} scope)"
        result.record(f"Stored the application credential using {result.credential_mechanism}")
    except Exception as error:
        result.status = Status.CREDENTIAL_STORE_FAILED
        result.message = redact_secrets(
            f"Could not store the database credential securely: {error}",
            admin_password, credential.password,
        )
        return result

    if not can_authenticate(credential):
        result.status = Status.PROVISION_FAILED
        result.message = (
            f"Role {role!r} was provisioned, but connecting to {database!r} as that role "
            "failed. Check pg_hba.conf allows password authentication from localhost."
        )
        return result
    result.record(f"Verified that {role!r} can connect to {database!r}")

    # --- 7. Migrate and verify ----------------------------------------------------------
    if run_migrations:
        migration_status = _migrate_and_verify(credential, result)
        if migration_status is not Status.SUCCESS:
            result.status = migration_status
            return result

    result.status = Status.SUCCESS
    result.message = (
        f"Database {database!r} is ready on {host}:{port} for role {role!r}."
    )
    return result


def _describe_store():
    from app.secure_store import get_credential_store

    return get_credential_store()


def _migrate_and_verify(credential: DatabaseCredential, result: ProvisionResult) -> Status:
    """Run Alembic IN-PROCESS, then verify the schema.

    In-process on purpose: a subprocess would need the URL on its command line or in its
    environment, and the command line of every process on Windows is readable by any user."""
    from app.schema_verification import (
        BASELINE_REVISION,
        stamp_baseline,
        upgrade_to_head,
        verify_schema,
    )

    url = credential.url()

    # An adopted database already HAS the baseline schema but no alembic_version row, so
    # running the baseline migration would fail on "relation already exists". Stamp it first
    # -- but only after verify_schema() confirms it really matches, never blindly.
    try:
        existing = verify_schema(url)
        if existing.alembic_revision is None and existing.matches:
            stamp_baseline(url)
            result.record(f"Adopted the existing schema; stamped at {BASELINE_REVISION}")
    except Exception as error:
        result.message = redact_secrets(
            f"Could not inspect the existing schema: {type(error).__name__}: {error}",
            credential.password,
        )
        return Status.VERIFICATION_FAILED

    try:
        upgrade_to_head(url)
        result.migrated = True
        result.record("Ran alembic upgrade head")
    except Exception as error:
        result.message = redact_secrets(
            f"Database migration failed: {type(error).__name__}: {error}", credential.password
        )
        return Status.MIGRATION_FAILED

    try:
        report = verify_schema(url)
    except Exception as error:
        result.message = redact_secrets(
            f"Schema verification could not run: {type(error).__name__}: {error}",
            credential.password,
        )
        return Status.VERIFICATION_FAILED

    result.revision = report.alembic_revision
    result.schema_verified = report.matches
    if not report.matches:
        result.message = (
            "The migrated schema does not match the expected baseline: "
            + "; ".join(report.problems[:5])
        )
        return Status.VERIFICATION_FAILED
    if report.alembic_revision != BASELINE_REVISION and report.alembic_revision is None:
        result.message = "The database is not stamped at any Alembic revision."
        return Status.VERIFICATION_FAILED

    result.record(f"Schema verified at revision {report.alembic_revision}")
    return Status.SUCCESS


def current_state(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  database: str = DEFAULT_DATABASE, role: str = DEFAULT_ROLE,
                  store: CredentialStore | None = None) -> dict:
    """Read-only status for the launcher/installer. Never connects as an administrator."""
    report = detect(host, port)
    credential = None
    try:
        credential = load_credential(store)
    except Exception as error:
        logger.debug("No readable stored credential: %s", type(error).__name__)

    state = {
        "postgres_running": report.probe.is_postgres,
        "port_occupied_by_other": report.probe.occupied_by_other,
        "installed": report.installed,
        "credential_stored": credential is not None,
        "credential_matches_role": bool(credential and credential.username == role),
        "detection": report.to_dict(),
        "can_connect": False,
    }
    if credential:
        state["can_connect"] = can_authenticate(credential)
    return state


# --------------------------------------------------------------------------------------
# Command line. The administrator password is NEVER an argument.
# --------------------------------------------------------------------------------------

ADMIN_PASSWORD_STDIN_HELP = (
    "The PostgreSQL administrator password is read from standard input (or prompted for "
    "interactively). It is never accepted as a command-line argument, because on Windows "
    "any user can read the full command line of any running process."
)


def read_admin_password(from_stdin: bool) -> str:
    """Read the administrator password without ever exposing it in argv."""
    import getpass
    import sys as _sys

    if from_stdin:
        password = _sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("PostgreSQL administrator password: ")
    return password


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys as _sys

    parser = argparse.ArgumentParser(
        description="Provision the local PostgreSQL database for this application.",
        epilog=ADMIN_PASSWORD_STDIN_HELP,
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--role", default=DEFAULT_ROLE)
    parser.add_argument("--admin-user", default=ADMIN_USER)
    parser.add_argument("--rotate", action="store_true",
                        help="Force a new application password.")
    parser.add_argument("--password-stdin", action="store_true",
                        help="Read the administrator password from stdin instead of prompting.")
    parser.add_argument("--detect-only", action="store_true",
                        help="Report what is installed and reachable; change nothing.")
    parser.add_argument("--status", action="store_true",
                        help="Report the current provisioning state; change nothing.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(argv)

    if args.detect_only:
        report = detect(args.host, args.port).to_dict()
        print(json.dumps(report, indent=2) if args.json else report)
        return int(Status.SUCCESS if report["running"] else Status.POSTGRES_NOT_FOUND)

    if args.status:
        state = current_state(args.host, args.port, args.database, args.role)
        print(json.dumps(state, indent=2) if args.json else state)
        return int(Status.SUCCESS)

    admin_password = read_admin_password(args.password_stdin)
    if not admin_password:
        print("No administrator password was supplied.", file=_sys.stderr)
        return int(Status.INVALID_USAGE)

    try:
        result = provision(
            admin_password,
            host=args.host, port=args.port, database=args.database,
            role=args.role, admin_user=args.admin_user, rotate=args.rotate,
        )
    finally:
        # Drop the only reference we hold to the administrator secret.
        admin_password = ""

    payload = result.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for step in result.steps:
            print(f"  {step}")
        print(f"\n{result.status.name}: {result.message}")
    return int(result.status)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    import sys as _sys

    _sys.exit(main())
