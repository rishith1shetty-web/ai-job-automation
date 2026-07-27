"""Local PostgreSQL provisioning (app/provisioning.py, app/postgres_detection.py).

Every database created here is named ``ai_job_migtest_*`` and every role ``ai_job_testrole_*``;
both prefixes are enforced by guards in tests/migration_db.py, so the real
``linkedin_auto_apply`` database and the real application role can never be created, altered
or dropped by this file. Each is removed when its test finishes.

Guarantees under test:

* PostgreSQL absence and a non-PostgreSQL service on the port are distinguished, and each
  produces its own status;
* role and database creation, ownership and privileges;
* a second run changes nothing and destroys nothing;
* an existing database with data is adopted, ownership transfers, and every row survives;
* a wrong or missing stored password is recovered from by rotation, not by failure;
* Alembic reaches head and the schema verifies;
* no administrator password is ever passed in a command line, and no secret reaches a log.
"""

from __future__ import annotations

import ast
import inspect as inspect_module
import io
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, text

from app import provisioning as provisioning_module
from app.db_credentials import DatabaseCredential, load_credential, store_credential
from app.paths import project_root
from app.postgres_detection import (
    DEFAULT_PORT,
    DetectionReport,
    ProbeResult,
    PostgresInstallation,
    detect,
    find_installations,
    find_services,
    probe_port,
)
from app.provisioning import (
    ProvisionResult,
    Status,
    quote_literal,
    can_authenticate,
    current_state,
    database_exists,
    database_owner,
    provision,
    quote_identifier,
    role_exists,
)
from app.secure_store import InsecureFileStore
from tests.migration_db import (
    TEST_ROLE_PREFIX,
    UnsafeDatabaseName,
    admin_password,
    admin_user,
    base_url,
    create_database,
    drop_database,
    drop_role,
    postgres_available,
    unique_name,
    unique_role,
)

POSTGRES = postgres_available()
ADMIN_PASSWORD = admin_password() if POSTGRES else None
requires_postgres = unittest.skipUnless(
    POSTGRES and ADMIN_PASSWORD,
    "PostgreSQL is not reachable, or no administrator password is configured",
)



PROVISIONING_SOURCE = project_root() / "app" / "provisioning.py"


def executable_string_literals(path: Path) -> list[str]:
    """Every string literal that is real code, with docstrings excluded.

    Scanning raw text would match the module's own prose -- the docstring explains that it
    uses no REASSIGN OWNED, which is exactly the phrase being searched for. Parsing instead
    means these tests check the SQL the module can actually execute."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)                     and isinstance(body[0].value.value, str):
                docstring_nodes.add(id(body[0].value))
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        and id(node) not in docstring_nodes
    ]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class _ProvisioningTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="prov-test-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)
        self.store = InsecureFileStore(self.temp_dir / "database_credential.dat")
        self.host = base_url().host or "localhost"
        self.port = int(base_url().port or DEFAULT_PORT)

    def _register_cleanup(self) -> None:
        """One cleanup for both kinds of resource, so ordering is guaranteed.

        A role cannot be dropped while it still owns a database, and unittest runs cleanups
        in reverse registration order -- which is not necessarily databases-before-roles.
        Registering a single hook that drops databases first and roles second removes the
        ordering hazard entirely, so no stray test role is ever left in the cluster."""
        if getattr(self, "_cleanup_registered", False):
            return
        self._cleanup_registered = True
        self._databases: list[str] = []
        self._roles: list[str] = []

        def cleanup() -> None:
            for database in self._databases:
                drop_database(database)
            for role in self._roles:
                drop_role(role)

        self.addCleanup(cleanup)

    def new_role(self) -> str:
        self._register_cleanup()
        name = unique_role()
        self._roles.append(name)
        return name

    def new_database_name(self) -> str:
        self._register_cleanup()
        name = unique_name()
        self._databases.append(name)
        return name

    def provision(self, **kwargs) -> ProvisionResult:
        defaults = dict(
            host=self.host, port=self.port, store=self.store,
            admin_user=admin_user(),
        )
        defaults.update(kwargs)
        return provision(ADMIN_PASSWORD, **defaults)

    def admin_connection(self, database: str = "postgres"):
        url = base_url().set(database=database).render_as_string(hide_password=False)
        return create_engine(url, isolation_level="AUTOCOMMIT")


# --------------------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------------------

class PortProbeTests(unittest.TestCase):
    """Telling PostgreSQL apart from anything else holding the port."""

    def test_nothing_listening_is_reported_as_unreachable(self) -> None:
        # Port 1 is reserved and never has a listener.
        result = probe_port("127.0.0.1", 1, timeout=1.0)
        self.assertFalse(result.reachable)
        self.assertFalse(result.is_postgres)
        self.assertFalse(result.occupied_by_other)

    def test_a_non_postgres_service_on_the_port_is_detected(self) -> None:
        """A plain TCP server that is not PostgreSQL must NOT be mistaken for one."""
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)

        def serve() -> None:
            try:
                connection, _ = server.accept()
                connection.recv(64)
                connection.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                connection.close()
            except OSError:
                pass

        threading.Thread(target=serve, daemon=True).start()
        result = probe_port("127.0.0.1", port, timeout=3.0)
        self.assertTrue(result.reachable)
        self.assertFalse(result.is_postgres)
        self.assertTrue(result.occupied_by_other)
        self.assertIn("not PostgreSQL", result.detail)

    def test_a_silent_service_on_the_port_is_not_taken_for_postgres(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        result = probe_port("127.0.0.1", port, timeout=1.0)
        self.assertTrue(result.reachable)
        self.assertFalse(result.is_postgres)

    @unittest.skipUnless(POSTGRES, "PostgreSQL is not reachable")
    def test_the_real_postgres_is_positively_identified(self) -> None:
        url = base_url()
        result = probe_port(url.host or "localhost", int(url.port or DEFAULT_PORT))
        self.assertTrue(result.reachable)
        self.assertTrue(result.is_postgres)
        self.assertFalse(result.occupied_by_other)


class DetectionReportTests(unittest.TestCase):
    def test_detection_never_assumes_a_fixed_install_directory(self) -> None:
        source = (project_root() / "app" / "postgres_detection.py").read_text(encoding="utf-8")
        self.assertNotIn(r"C:\Program Files\PostgreSQL\16", source)
        self.assertIn("SOFTWARE\\PostgreSQL\\Installations", source)

    def test_report_serialises_without_secrets(self) -> None:
        report = DetectionReport(
            installations=[PostgresInstallation(version="17.2", base_directory="X")],
            probe=ProbeResult(reachable=True, is_postgres=True),
        )
        payload = report.to_dict()
        self.assertTrue(payload["installed"])
        self.assertTrue(payload["running"])
        self.assertNotIn("password", str(payload).lower())

    def test_discovery_helpers_run_without_error_on_this_platform(self) -> None:
        self.assertIsInstance(find_installations(), list)
        self.assertIsInstance(find_services(), list)
        self.assertIsInstance(detect().to_dict(), dict)

    def test_installation_reports_its_major_version(self) -> None:
        self.assertEqual(PostgresInstallation(version="17.2").major_version, 17)
        self.assertIsNone(PostgresInstallation().major_version)


class MissingPostgresTests(_ProvisioningTestCase):
    """Status codes a future installer will branch on."""

    def test_absent_postgres_returns_its_own_status(self) -> None:
        with patch("app.provisioning.detect") as detect_mock, \
             patch("app.provisioning.probe_port") as probe_mock:
            detect_mock.return_value = DetectionReport(probe=ProbeResult(port=5999))
            probe_mock.return_value = ProbeResult(port=5999, reachable=False)
            result = provision("unused", port=5999, store=self.store)

        self.assertEqual(result.status, Status.POSTGRES_NOT_FOUND)
        self.assertEqual(int(result.status), 10)
        self.assertIn("not appear to be installed", result.message)
        self.assertFalse(result.credential_stored)

    def test_installed_but_stopped_gives_a_different_message(self) -> None:
        report = DetectionReport(
            installations=[PostgresInstallation(version="17.2")],
            probe=ProbeResult(port=5999, reachable=False),
        )
        with patch("app.provisioning.detect", return_value=report), \
             patch("app.provisioning.probe_port",
                   return_value=ProbeResult(port=5999, reachable=False)):
            result = provision("unused", port=5999, store=self.store)

        self.assertEqual(result.status, Status.POSTGRES_NOT_FOUND)
        self.assertIn("start its Windows service", result.message)

    def test_a_foreign_service_on_the_port_is_reported_separately(self) -> None:
        occupied = ProbeResult(port=5999, reachable=True, is_postgres=False,
                               detail="unexpected handshake reply")
        with patch("app.provisioning.detect", return_value=DetectionReport(probe=occupied)), \
             patch("app.provisioning.probe_port", return_value=occupied):
            result = provision("unused", port=5999, store=self.store)

        self.assertEqual(result.status, Status.PORT_OCCUPIED_BY_OTHER)
        self.assertEqual(int(result.status), 11)
        self.assertIn("not PostgreSQL", result.message)

    def test_a_bad_administrator_password_is_its_own_status(self) -> None:
        healthy = ProbeResult(reachable=True, is_postgres=True)
        with patch("app.provisioning.detect", return_value=DetectionReport(probe=healthy)), \
             patch("app.provisioning.probe_port", return_value=healthy), \
             patch("app.provisioning._admin_engine") as engine_mock:
            engine_mock.return_value.connect.side_effect = RuntimeError("auth failed")
            result = provision("wrong-password", store=self.store)

        self.assertEqual(result.status, Status.ADMIN_AUTH_FAILED)
        self.assertEqual(int(result.status), 12)
        self.assertNotIn("wrong-password", result.message)


# --------------------------------------------------------------------------------------
# Real provisioning
# --------------------------------------------------------------------------------------

@requires_postgres
class FreshProvisioningTests(_ProvisioningTestCase):
    def test_role_database_migration_and_credential(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        result = self.provision(role=role, database=database)

        self.assertEqual(result.status, Status.SUCCESS, result.message)
        self.assertTrue(result.role_created)
        self.assertTrue(result.database_created)
        self.assertTrue(result.migrated)
        self.assertTrue(result.schema_verified)
        self.assertTrue(result.credential_stored)
        self.assertEqual(result.revision, "ed2a513cd149")

        engine = self.admin_connection()
        try:
            with engine.connect() as connection:
                self.assertTrue(role_exists(connection, role))
                self.assertTrue(database_exists(connection, database))
                self.assertEqual(database_owner(connection, database), role)
        finally:
            engine.dispose()

    def test_the_role_is_least_privilege(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        engine = self.admin_connection()
        try:
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolcanlogin "
                    "FROM pg_roles WHERE rolname = :role"
                ), {"role": role}).one()
        finally:
            engine.dispose()
        superuser, createdb, createrole, replication, canlogin = row
        self.assertFalse(superuser, "the application role must not be a superuser")
        self.assertFalse(createdb)
        self.assertFalse(createrole)
        self.assertFalse(replication)
        self.assertTrue(canlogin)

    def test_the_stored_credential_connects(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        credential = load_credential(self.store)
        self.assertIsNotNone(credential)
        self.assertEqual(credential.username, role)
        self.assertEqual(credential.database, database)
        self.assertEqual(credential.port, self.port)
        self.assertTrue(can_authenticate(credential))

    def test_all_application_tables_are_present_and_owned_by_the_role(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        credential = load_credential(self.store)
        engine = create_engine(credential.url())
        try:
            tables = set(inspect(engine).get_table_names())
            self.assertIn("application_records", tables)
            self.assertIn("alembic_version", tables)
            with engine.connect() as connection:
                owners = connection.execute(text(
                    "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'"
                )).scalars().all()
        finally:
            engine.dispose()
        self.assertEqual(set(owners), {role})

    def test_the_application_role_can_read_and_write(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        credential = load_credential(self.store)
        engine = create_engine(credential.url())
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO portal_runtime_state (id, active_portal, updated_at) "
                    "VALUES (2, 'dice', now()) ON CONFLICT (id) DO NOTHING"
                ))
            with engine.connect() as connection:
                count = connection.execute(
                    text("SELECT count(*) FROM portal_runtime_state")
                ).scalar()
        finally:
            engine.dispose()
        self.assertGreaterEqual(count, 1)


@requires_postgres
class IdempotencyTests(_ProvisioningTestCase):
    def test_a_second_run_changes_nothing_and_retains_the_password(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        first = self.provision(role=role, database=database)
        original_password = load_credential(self.store).password

        second = self.provision(role=role, database=database)

        self.assertEqual(second.status, Status.SUCCESS, second.message)
        self.assertFalse(second.role_created)
        self.assertFalse(second.database_created)
        self.assertFalse(second.password_rotated, "a working password must be retained")
        self.assertEqual(load_credential(self.store).password, original_password)
        self.assertEqual(first.revision, second.revision)

    def test_a_second_run_preserves_data(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        credential = load_credential(self.store)
        engine = create_engine(credential.url())
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO resume_profiles "
                    "(portal, filename, stored_path, raw_text, skills, experience, education, "
                    " job_titles, keywords, technologies, domain_expertise, role_suggestions, "
                    " generated_search_keywords, role_confidence, domain_profile, role_profile, "
                    " created_at) VALUES ('linkedin','cv.pdf','/tmp/cv.pdf','text','[]','[]',"
                    " '[]','[]','[]','[]','[]','[]','[]',0,'{}','{}',now())"
                ))
        finally:
            engine.dispose()

        self.provision(role=role, database=database)

        engine = create_engine(load_credential(self.store).url())
        try:
            with engine.connect() as connection:
                count = connection.execute(text("SELECT count(*) FROM resume_profiles")).scalar()
        finally:
            engine.dispose()
        self.assertEqual(count, 1, "provisioning must never delete data")

    def test_explicit_rotation_replaces_the_password_and_still_connects(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)
        original = load_credential(self.store).password

        result = self.provision(role=role, database=database, rotate=True)

        self.assertEqual(result.status, Status.SUCCESS, result.message)
        self.assertTrue(result.password_rotated)
        rotated = load_credential(self.store)
        self.assertNotEqual(rotated.password, original)
        self.assertTrue(can_authenticate(rotated))


@requires_postgres
class WrongStoredPasswordTests(_ProvisioningTestCase):
    def test_a_wrong_stored_password_is_recovered_by_rotation(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        # Simulate a credential that no longer matches the role.
        broken = load_credential(self.store).with_password("no-longer-correct")
        store_credential(broken, self.store)
        self.assertFalse(can_authenticate(broken))

        result = self.provision(role=role, database=database)

        self.assertEqual(result.status, Status.SUCCESS, result.message)
        self.assertTrue(result.password_rotated)
        self.assertTrue(can_authenticate(load_credential(self.store)))

    def test_a_missing_stored_credential_is_recovered_by_rotation(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)
        self.store.delete()

        result = self.provision(role=role, database=database)

        self.assertEqual(result.status, Status.SUCCESS, result.message)
        self.assertTrue(result.password_rotated)
        self.assertTrue(can_authenticate(load_credential(self.store)))


@requires_postgres
class ExistingDatabaseAdoptionTests(_ProvisioningTestCase):
    """Case C: a database created under the old development postgres credential."""

    def legacy_database_with_data(self) -> str:
        """Built and owned by the administrator, exactly like today's development database."""
        from tests.test_alembic_migrations import build_with_init_db

        database = self.new_database_name()
        url = create_database(database)
        build_with_init_db(url)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO resume_profiles "
                    "(id, portal, filename, stored_path, raw_text, skills, experience, "
                    " education, job_titles, keywords, technologies, domain_expertise, "
                    " role_suggestions, generated_search_keywords, role_confidence, "
                    " domain_profile, role_profile, created_at) "
                    "VALUES (1,'linkedin','legacy.pdf','/tmp/legacy.pdf','legacy text',"
                    " '[]','[]','[]','[]','[]','[]','[]','[]','[]',0,'{}','{}',now())"
                ))
        finally:
            engine.dispose()
        return database

    def test_an_existing_database_is_adopted_without_losing_data(self) -> None:
        database = self.legacy_database_with_data()
        role = self.new_role()

        result = self.provision(role=role, database=database)

        self.assertEqual(result.status, Status.SUCCESS, result.message)
        self.assertTrue(result.role_created)
        self.assertFalse(result.database_created, "the existing database must be adopted")
        self.assertTrue(result.ownership_transferred)
        self.assertTrue(result.schema_verified)

        credential = load_credential(self.store)
        engine = create_engine(credential.url())
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT filename FROM resume_profiles WHERE id = 1")
                ).one()
        finally:
            engine.dispose()
        self.assertEqual(row[0], "legacy.pdf", "existing data must survive adoption")

    def test_ownership_moves_to_the_application_role(self) -> None:
        database = self.legacy_database_with_data()
        role = self.new_role()
        self.provision(role=role, database=database)

        engine = self.admin_connection(database)
        try:
            with engine.connect() as connection:
                owners = connection.execute(text(
                    "SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'public'"
                )).scalars().all()
                schema_owner = connection.execute(text(
                    "SELECT r.rolname FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner "
                    "WHERE n.nspname = 'public'"
                )).scalar()
        finally:
            engine.dispose()
        self.assertEqual(set(owners), {role})
        self.assertEqual(schema_owner, role)

    def test_the_adopted_database_is_stamped_and_at_head(self) -> None:
        database = self.legacy_database_with_data()
        role = self.new_role()

        # An already-adopted database (Phase 2A) carries the stamp; this one does not, so
        # provisioning must reach head either way.
        result = self.provision(role=role, database=database)
        self.assertEqual(result.revision, "ed2a513cd149")
        self.assertTrue(result.migrated)


@requires_postgres
class AlreadyProvisionedTests(_ProvisioningTestCase):
    """Case D: everything already correct -- verify and upgrade, change nothing else."""

    def test_running_against_a_provisioned_database_is_a_clean_no_op(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        engine = create_engine(load_credential(self.store).url())
        try:
            before = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        result = self.provision(role=role, database=database)

        engine = create_engine(load_credential(self.store).url())
        try:
            after = set(inspect(engine).get_table_names())
        finally:
            engine.dispose()

        self.assertEqual(result.status, Status.SUCCESS)
        self.assertEqual(before, after)
        self.assertFalse(result.role_created)
        self.assertFalse(result.database_created)

    def test_current_state_reports_a_provisioned_installation(self) -> None:
        role, database = self.new_role(), self.new_database_name()
        self.provision(role=role, database=database)

        state = current_state(self.host, self.port, database, role, store=self.store)
        self.assertTrue(state["postgres_running"])
        self.assertTrue(state["credential_stored"])
        self.assertTrue(state["credential_matches_role"])
        self.assertTrue(state["can_connect"])
        self.assertNotIn("password", str(state).lower())


# --------------------------------------------------------------------------------------
# Safety properties
# --------------------------------------------------------------------------------------

class NoDestructiveSqlTests(unittest.TestCase):
    def test_the_provisioning_module_contains_no_destructive_sql(self) -> None:
        """A provisioning run must be incapable of destroying data, by construction."""
        literals = " \n ".join(executable_string_literals(PROVISIONING_SOURCE)).upper()
        for forbidden in ("DROP TABLE", "DROP DATABASE", "DROP ROLE", "DROP SCHEMA",
                          "TRUNCATE", "DELETE FROM", "DROP OWNED", "DROP COLUMN"):
            with self.subTest(statement=forbidden):
                self.assertNotIn(forbidden, literals)

    def test_reassign_owned_is_not_used(self) -> None:
        """REASSIGN OWNED BY re-owns objects outside the target database, including other
        databases owned by the same role -- far more than provisioning may change."""
        literals = " \n ".join(executable_string_literals(PROVISIONING_SOURCE)).upper()
        self.assertNotIn("REASSIGN OWNED", literals)

    def test_identifiers_are_quoted(self) -> None:
        self.assertEqual(quote_identifier("plain"), '"plain"')
        self.assertEqual(quote_identifier('we"ird'), '"we""ird"')
        for bad in ("", None):
            with self.assertRaises(ValueError):
                quote_identifier(bad)

    def test_a_password_literal_cannot_break_out_of_its_quotes(self) -> None:
        """PostgreSQL forbids bind parameters in CREATE/ALTER ROLE, so the password is
        embedded as a literal and must be escaped correctly."""
        self.assertEqual(quote_literal("plain"), "'plain'")
        self.assertEqual(quote_literal("it's"), "'it''s'")
        self.assertEqual(
            quote_literal("x'; ALTER ROLE postgres SUPERUSER; --"),
            "'x''; ALTER ROLE postgres SUPERUSER; --'",
        )
        # A backslash is literal under standard_conforming_strings; it must not be doubled.
        self.assertEqual(quote_literal("back\slash"), "'back\slash'")
        with self.assertRaises(ValueError):
            quote_literal("has" + chr(0) + "nul")

    def test_generated_passwords_never_need_escaping(self) -> None:
        from app.db_credentials import generate_password

        for _ in range(20):
            password = generate_password()
            self.assertEqual(quote_literal(password), f"'{password}'")

    def test_a_role_name_cannot_inject_sql(self) -> None:
        injected = 'x"; DROP TABLE resume_profiles; --'
        self.assertEqual(
            quote_identifier(injected), '"x""; DROP TABLE resume_profiles; --"'
        )


class AdminPasswordSafetyTests(unittest.TestCase):
    def test_provisioning_never_shells_out(self) -> None:
        """A command line is readable by any user on the machine, so no secret may travel
        through one. Checked against real imports, not prose."""
        self.assertNotIn("subprocess", imported_modules(PROVISIONING_SOURCE))
        self.assertNotIn("os", imported_modules(PROVISIONING_SOURCE))

    def test_the_cli_offers_no_password_argument(self) -> None:
        """An --admin-password flag would put the secret in the process list."""
        literals = executable_string_literals(PROVISIONING_SOURCE)
        flags = {value for value in literals if value.startswith("--")}
        self.assertIn("--password-stdin", flags, "the stdin route must exist")
        for flag in flags:
            with self.subTest(flag=flag):
                # A flag that TAKES a password would place it in the process command line.
                # "--password-stdin" is a switch and carries no value.
                self.assertNotIn(flag, {"--password", "--admin-password", "--pass", "--pwd"})

    def test_the_password_is_read_from_stdin_when_requested(self) -> None:
        from app.provisioning import read_admin_password

        with patch("sys.stdin", io.StringIO("piped-secret\n")):
            self.assertEqual(read_admin_password(True), "piped-secret")

    def test_alembic_runs_in_process_not_as_a_subprocess(self) -> None:
        """A subprocess would need the URL -- password included -- on its command line or in
        its environment."""
        source = inspect_module.getsource(provisioning_module._migrate_and_verify)
        self.assertIn("upgrade_to_head(url)", source)
        self.assertNotIn("Popen", source)
        self.assertNotIn("subprocess.run", source)


class SecretRedactionTests(_ProvisioningTestCase):
    def test_no_secret_reaches_the_log_during_a_failed_run(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("app")
        logger.addHandler(handler)
        previous_level = logger.level
        logger.setLevel(logging.DEBUG)
        self.addCleanup(logger.setLevel, previous_level)
        self.addCleanup(logger.removeHandler, handler)

        secret = "sup3r-s3cret-admin-pw"
        healthy = ProbeResult(reachable=True, is_postgres=True)
        with patch("app.provisioning.detect", return_value=DetectionReport(probe=healthy)), \
             patch("app.provisioning.probe_port", return_value=healthy), \
             patch("app.provisioning._admin_engine") as engine_mock:
            engine_mock.return_value.connect.side_effect = RuntimeError(
                f"FATAL: password authentication failed for {secret}"
            )
            result = provision(secret, store=self.store)

        self.assertEqual(result.status, Status.ADMIN_AUTH_FAILED)
        self.assertNotIn(secret, result.message)
        self.assertNotIn(secret, stream.getvalue())
        self.assertNotIn(secret, str(result.to_dict()))

    def test_the_structured_result_never_carries_a_password(self) -> None:
        payload = ProvisionResult(message="all good").to_dict()
        self.assertNotIn("password", payload)
        self.assertNotIn("admin_password", payload)


class TestRoleSafetyTests(unittest.TestCase):
    def test_only_prefixed_roles_may_be_dropped(self) -> None:
        for forbidden in ("postgres", "ai_job_automation_app", "", "ai_job_testrol"):
            with self.subTest(role=forbidden):
                with self.assertRaises(UnsafeDatabaseName):
                    drop_role(forbidden)

    def test_generated_test_roles_carry_the_prefix(self) -> None:
        self.assertTrue(unique_role().startswith(TEST_ROLE_PREFIX))
        self.assertNotEqual(unique_role(), unique_role())

    def test_no_stray_test_roles_are_left_behind(self) -> None:
        """A leaked role would accumulate in the cluster on every run."""
        from sqlalchemy import create_engine

        if not POSTGRES:
            self.skipTest("PostgreSQL is not reachable")
        engine = create_engine(
            base_url().set(database="postgres").render_as_string(hide_password=False)
        )
        try:
            with engine.connect() as connection:
                stray = [
                    row[0] for row in connection.execute(text("SELECT rolname FROM pg_roles"))
                    if row[0].startswith(TEST_ROLE_PREFIX)
                ]
        finally:
            engine.dispose()
        self.assertEqual(stray, [], f"{len(stray)} test roles were not cleaned up")

    def test_the_real_application_role_is_not_a_test_role(self) -> None:
        from app.db_credentials import DEFAULT_ROLE

        self.assertFalse(DEFAULT_ROLE.startswith(TEST_ROLE_PREFIX))


class PowerShellInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script = (project_root() / "scripts" / "provision_postgres.ps1").read_text(
            encoding="utf-8"
        )

    def code_lines(self) -> str:
        """The script with comments and the comment-based help block removed."""
        lines, in_block = [], False
        for line in self.script.splitlines():
            stripped = line.strip()
            if stripped.startswith("<#"):
                in_block = True
            if in_block:
                if stripped.endswith("#>"):
                    in_block = False
                continue
            if stripped.startswith("#"):
                continue
            lines.append(line)
        return "\n".join(lines)

    def test_uses_strict_mode(self) -> None:
        self.assertIn("Set-StrictMode -Version Latest", self.script)
        self.assertIn('$ErrorActionPreference = "Stop"', self.script)

    def test_accepts_only_non_secret_configuration_as_parameters(self) -> None:
        self.assertIn("[string]$DbHost", self.script)
        self.assertIn("[int]$Port", self.script)
        self.assertIn("[string]$Database", self.script)
        self.assertIn("[string]$Role", self.script)
        self.assertIn("[securestring]$AdminPassword", self.script)

    def test_the_password_is_piped_to_stdin_not_passed_as_an_argument(self) -> None:
        self.assertIn("--password-stdin", self.script)
        self.assertRegex(self.script, r"\$plain \| & \$python @arguments")
        # The password must never be appended to the argument array.
        self.assertNotRegex(self.script, r"\$arguments \+= .*\$plain")
        self.assertNotRegex(self.script, r"\$arguments \+= .*AdminPassword")

    def test_the_secure_string_buffer_is_cleared(self) -> None:
        self.assertIn("ZeroFreeBSTR", self.script)
        self.assertIn("finally", self.script)

    def test_it_never_echoes_the_password(self) -> None:
        for line in self.script.splitlines():
            if re.search(r"(Write-Host|Write-Output|Write-Verbose)", line):
                with self.subTest(line=line.strip()[:60]):
                    self.assertNotIn("$plain", line)
                    self.assertNotIn("$AdminPassword", line)

    def test_it_does_not_modify_the_callers_path(self) -> None:
        """Checked against executable lines only: the script's own comments explain that it
        makes no such call, and matching prose would defeat the test."""
        code = self.code_lines()
        self.assertNotIn("SetEnvironmentVariable", code)
        self.assertNotRegex(code, r"\$env:PATH\s*=")
        self.assertNotRegex(code, r"\$env:Path\s*=")
        self.assertNotIn("Set-Item Env:", code)

    def test_it_does_not_install_postgresql(self) -> None:
        for forbidden in ("Invoke-WebRequest", "Start-BitsTransfer", "winget", "choco",
                          "postgresql-.*installer", "msiexec"):
            with self.subTest(term=forbidden):
                self.assertNotRegex(self.script, forbidden)

    def test_exit_codes_are_documented_and_match_the_python_statuses(self) -> None:
        for status in Status:
            if status is Status.SUCCESS:
                continue
            with self.subTest(status=status.name):
                self.assertRegex(self.script, rf"{int(status)}\s+{status.name}")

    def test_it_returns_the_python_exit_code(self) -> None:
        self.assertIn("exit $exitCode", self.script)
        self.assertIn("exit $LASTEXITCODE", self.script)


class RealDatabaseUntouchedTests(unittest.TestCase):
    """Phase 2B performs read-only inspection of the real database only."""

    @unittest.skipUnless(POSTGRES, "PostgreSQL is not reachable")
    def test_the_real_database_owner_is_unchanged(self) -> None:
        """Nothing in this phase converts the real database to the dedicated role."""
        from app.db_credentials import DEFAULT_ROLE

        url = base_url()
        engine = create_engine(url.set(database="postgres").render_as_string(hide_password=False))
        try:
            with engine.connect() as connection:
                owner = database_owner(connection, "linkedin_auto_apply")
                dedicated_exists = role_exists(connection, DEFAULT_ROLE)
        finally:
            engine.dispose()
        # The real database is still owned by the development superuser, and the dedicated
        # role has NOT been created behind the user's back.
        self.assertNotEqual(owner, DEFAULT_ROLE)
        self.assertFalse(dedicated_exists,
                         "Phase 2B must not create the real application role automatically")


if __name__ == "__main__":
    unittest.main()
