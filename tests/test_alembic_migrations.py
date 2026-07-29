"""The Alembic migration foundation (alembic/, app/schema_verification.py).

Every test here runs against a DISPOSABLE database created by tests/migration_db.py, whose
name always starts with "ai_job_migtest_". The application's real database is never created,
migrated, stamped or dropped by this file; one test explicitly proves the guard that makes
that impossible.

Guarantees under test:

* the baseline produces exactly the schema init_db() has always produced -- so existing
  installations and fresh ones end up identical;
* an empty database upgrades to head, with the right tables, enum values, indexes and
  constraints;
* upgrading twice is a no-op;
* an existing, matching database can be verified and stamped without losing data;
* a mismatched database is REJECTED rather than blindly stamped;
* the application still starts against a migrated database;
* no credential ever appears in the Alembic configuration.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.paths import project_root
from app.schema_verification import (
    BASELINE_REVISION,
    CRITICAL_COLUMNS,
    ENUM_NAME,
    NAMED_FOREIGN_KEYS,
    REQUIRED_ENUM_VALUES,
    REQUIRED_FOREIGN_KEYS,
    REQUIRED_INDEXES,
    REQUIRED_TABLES,
    REQUIRED_UNIQUE_CONSTRAINTS,
    SchemaMismatch,
    alembic_config,
    safe_database_label,
    stamp_baseline,
    upgrade_to_head,
    verify_schema,
)
from tests.migration_db import (
    TEST_DB_PREFIX,
    UnsafeDatabaseName,
    create_database,
    drop_database,
    postgres_available,
    unique_name,
)

POSTGRES = postgres_available()
requires_postgres = unittest.skipUnless(POSTGRES, "PostgreSQL is not reachable")


def build_with_init_db(url: str) -> None:
    """Build a database through the EXISTING startup path, as today's installations were."""
    import app.database as database

    original_engine, original_session = database.engine, database.SessionLocal
    engine = create_engine(url)
    database.engine = engine
    database.SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        database.init_db()
    finally:
        engine.dispose()
        database.engine, database.SessionLocal = original_engine, original_session


def schema_snapshot(url: str) -> dict:
    """Structure only: tables, columns, indexes, constraints, enum values."""
    engine = create_engine(url)
    try:
        inspector = inspect(engine)
        snapshot: dict = {}
        for table in sorted(inspector.get_table_names()):
            if table == "alembic_version":
                continue
            snapshot[table] = {
                "columns": {
                    c["name"]: (str(c["type"]), c["nullable"])
                    for c in inspector.get_columns(table)
                },
                "indexes": {
                    i["name"]: (tuple(i["column_names"]), bool(i["unique"]))
                    for i in inspector.get_indexes(table)
                },
                "unique": {
                    u["name"]: tuple(u["column_names"])
                    for u in inspector.get_unique_constraints(table)
                },
                "foreign_keys": sorted(
                    (tuple(f["constrained_columns"]), f["referred_table"])
                    for f in inspector.get_foreign_keys(table)
                ),
                "primary_key": tuple(
                    inspector.get_pk_constraint(table).get("constrained_columns") or ()
                ),
            }
        with engine.connect() as connection:
            snapshot["__enum__"] = sorted(
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                        "ON t.oid = e.enumtypid WHERE t.typname = :name"
                    ),
                    {"name": ENUM_NAME},
                )
            )
        return snapshot
    finally:
        engine.dispose()


class _DatabaseTestCase(unittest.TestCase):
    def new_database(self) -> str:
        name = unique_name()
        url = create_database(name)
        self.addCleanup(drop_database, name)
        return url


class ConfigurationTests(unittest.TestCase):
    """No credential may ever be committed, and the URL must come from the application."""

    def test_alembic_ini_contains_no_url_or_password(self) -> None:
        """Only real settings lines are inspected; the file's comments legitimately discuss
        URLs and passwords in order to explain why neither may be stored here."""
        raw = (project_root() / "alembic.ini").read_text(encoding="utf-8")
        settings = [
            line.strip() for line in raw.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for line in settings:
            with self.subTest(line=line):
                self.assertNotRegex(line, r"^sqlalchemy\.url\s*=\s*\S",
                                    "alembic.ini must not carry a database URL")
                for secret in ("postgresql://", "postgresql+psycopg://", "@localhost", "5555"):
                    self.assertNotIn(secret, line)

    def test_config_loads_and_resolves_script_location(self) -> None:
        config = alembic_config()
        self.assertTrue(Path(config.get_main_option("script_location")).is_dir())
        self.assertIsNone(config.get_main_option("sqlalchemy.url", None))

    def test_running_a_migration_does_not_disable_application_logging(self) -> None:
        """logging.fileConfig defaults to disable_existing_loggers=True, which would silence
        every logger the application had already created."""
        import logging

        env = (project_root() / "alembic" / "env.py").read_text(encoding="utf-8")
        self.assertIn("disable_existing_loggers=False", env)

        marker = logging.getLogger("app.services.test_marker_for_alembic")
        self.assertFalse(marker.disabled)
        alembic_config()  # loads alembic.ini through fileConfig
        self.assertFalse(marker.disabled, "Alembic silenced an existing application logger")

    def test_env_py_reads_the_application_settings(self) -> None:
        env = (project_root() / "alembic" / "env.py").read_text(encoding="utf-8")
        self.assertIn("from app.config import get_settings", env)
        self.assertIn("ALEMBIC_DATABASE_URL", env)
        self.assertNotIn("postgresql://", env)

    def test_a_percent_in_a_password_does_not_break_the_config(self) -> None:
        """ConfigParser interpolates '%'; an unescaped password would raise."""
        url = "postgresql+psycopg://u:p%40ss%25word@localhost:5432/db"
        config = alembic_config(url)
        # Reading it back must not raise and must return exactly what went in.
        self.assertEqual(config.get_main_option("sqlalchemy.url"), url)

    def test_safe_label_hides_credentials(self) -> None:
        label = safe_database_label("postgresql+psycopg://user:secret@localhost:5432/mydb")
        self.assertEqual(label, "localhost/mydb")
        self.assertNotIn("secret", label)
        self.assertNotIn("user", label)

    def test_exactly_one_baseline_revision_exists(self) -> None:
        versions = list((project_root() / "alembic" / "versions").glob("*.py"))
        self.assertEqual(len(versions), 1, "Phase 2A defines exactly one migration")
        source = versions[0].read_text(encoding="utf-8")
        self.assertIn(f"revision: str = '{BASELINE_REVISION}'", source)
        self.assertIn("down_revision: Union[str, None] = None", source)


class TestDatabaseSafetyTests(unittest.TestCase):
    """The guard that makes it impossible to point these tests at the real database."""

    def test_only_prefixed_databases_may_be_created_or_dropped(self) -> None:
        for forbidden in ("linkedin_auto_apply", "postgres", "", "ai_job_migtes"):
            with self.subTest(name=forbidden):
                with self.assertRaises(UnsafeDatabaseName):
                    create_database(forbidden)
                with self.assertRaises(UnsafeDatabaseName):
                    drop_database(forbidden)

    def test_generated_names_carry_the_prefix(self) -> None:
        self.assertTrue(unique_name().startswith(TEST_DB_PREFIX))
        self.assertNotEqual(unique_name(), unique_name())


@requires_postgres
class BaselineParityTests(_DatabaseTestCase):
    """The whole adoption story rests on this: a migrated database and one built by the
    existing init_db() path must be structurally identical."""

    def test_migrated_schema_equals_init_db_schema(self) -> None:
        legacy_url = self.new_database()
        migrated_url = self.new_database()

        build_with_init_db(legacy_url)
        upgrade_to_head(migrated_url)

        legacy, migrated = schema_snapshot(legacy_url), schema_snapshot(migrated_url)
        self.assertEqual(sorted(legacy), sorted(migrated), "table sets differ")
        for table in legacy:
            with self.subTest(table=table):
                self.assertEqual(legacy[table], migrated[table])

    def test_portal_runtime_state_seed_row_is_created(self) -> None:
        url = self.new_database()
        upgrade_to_head(url)
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                rows = list(connection.execute(
                    text("SELECT id, active_portal FROM portal_runtime_state")
                ))
        finally:
            engine.dispose()
        self.assertEqual(rows, [(1, "linkedin")])


@requires_postgres
class FreshDatabaseTests(_DatabaseTestCase):
    """One migrated database, many read-only assertions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.name = unique_name()
        cls.url = create_database(cls.name)
        upgrade_to_head(cls.url)
        cls.engine = create_engine(cls.url)
        cls.inspector = inspect(cls.engine)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.engine.dispose()
        drop_database(cls.name)

    def test_every_required_table_is_created(self) -> None:
        present = set(self.inspector.get_table_names())
        for table in REQUIRED_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, present)

    def test_alembic_version_is_at_the_baseline(self) -> None:
        with self.engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        self.assertEqual(revision, BASELINE_REVISION)

    def test_enum_has_exactly_the_expected_values(self) -> None:
        with self.engine.connect() as connection:
            values = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t "
                        "ON t.oid = e.enumtypid WHERE t.typname = :name"
                    ),
                    {"name": ENUM_NAME},
                )
            }
        self.assertEqual(values, set(REQUIRED_ENUM_VALUES))

    def test_every_critical_column_exists(self) -> None:
        for table, columns in CRITICAL_COLUMNS.items():
            actual = {c["name"] for c in self.inspector.get_columns(table)}
            for column in columns:
                with self.subTest(column=f"{table}.{column}"):
                    self.assertIn(column, actual)

    def test_jsonb_columns_use_the_postgresql_type(self) -> None:
        """A plain JSON column would silently lose the operators the app relies on."""
        for table, column in (("resume_profiles", "domain_profile"),
                              ("resume_profiles", "role_profile"),
                              ("job_opportunities", "job_description_metadata"),
                              ("application_records", "evidence_sources"),
                              ("apply_top_batches", "job_results")):
            actual = {c["name"]: str(c["type"]) for c in self.inspector.get_columns(table)}
            with self.subTest(column=f"{table}.{column}"):
                self.assertEqual(actual[column], "JSONB")

    def test_every_required_index_exists(self) -> None:
        for table, indexes in REQUIRED_INDEXES.items():
            actual = {i["name"] for i in self.inspector.get_indexes(table)}
            for index in indexes:
                with self.subTest(index=index):
                    self.assertIn(index, actual)

    def test_every_required_unique_constraint_exists(self) -> None:
        for table, constraints in REQUIRED_UNIQUE_CONSTRAINTS.items():
            actual = {
                u["name"]: tuple(u["column_names"])
                for u in self.inspector.get_unique_constraints(table)
            }
            for name, columns in constraints:
                with self.subTest(constraint=name):
                    self.assertIn(name, actual)
                    self.assertEqual(actual[name], tuple(columns))

    def test_every_required_foreign_key_exists(self) -> None:
        for table, keys in REQUIRED_FOREIGN_KEYS.items():
            actual = {
                (tuple(f["constrained_columns"]), f["referred_table"])
                for f in self.inspector.get_foreign_keys(table)
            }
            for key in keys:
                with self.subTest(foreign_key=f"{table}{key}"):
                    self.assertIn(key, actual)

    def test_the_named_foreign_key_exists(self) -> None:
        for table, names in NAMED_FOREIGN_KEYS.items():
            actual = {f.get("name") for f in self.inspector.get_foreign_keys(table)}
            for name in names:
                with self.subTest(name=name):
                    self.assertIn(name, actual)

    def test_verification_passes_on_a_freshly_migrated_database(self) -> None:
        report = verify_schema(self.url)
        self.assertTrue(report.matches, report.render())
        self.assertEqual(report.alembic_revision, BASELINE_REVISION)
        self.assertEqual(report.tables_present, len(REQUIRED_TABLES))

    def test_the_report_never_contains_a_password(self) -> None:
        rendered = verify_schema(self.url).render()
        self.assertNotIn("postgresql", rendered)
        self.assertNotIn("@", rendered)


@requires_postgres
class IdempotencyTests(_DatabaseTestCase):
    def test_upgrading_twice_changes_nothing(self) -> None:
        url = self.new_database()
        upgrade_to_head(url)
        first = schema_snapshot(url)
        upgrade_to_head(url)  # already at head
        self.assertEqual(schema_snapshot(url), first)

    def test_init_db_still_works_on_a_migrated_database(self) -> None:
        """Phase 2A keeps init_db() running at startup, so the two must coexist."""
        url = self.new_database()
        upgrade_to_head(url)
        before = schema_snapshot(url)
        build_with_init_db(url)
        self.assertEqual(schema_snapshot(url), before)


@requires_postgres
class ExistingDatabaseAdoptionTests(_DatabaseTestCase):
    """Adopting a database that already holds real application history."""

    def seeded_legacy_database(self) -> str:
        """A database built the legacy way and holding real application history.

        Seeded through the ORM rather than raw INSERTs: most NOT NULL columns carry
        Python-side defaults and no DDL default, so a hand-written INSERT would have to
        name all of them."""
        from app.models import (
            ApplicationRecord, ApplicationStatus, JobOpportunity, ResumeProfile,
        )

        url = self.new_database()
        build_with_init_db(url)
        engine = create_engine(url)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with Session() as session:
                resume = ResumeProfile(
                    portal="linkedin", filename="cv.pdf", stored_path="/tmp/cv.pdf",
                    raw_text="resume text",
                )
                session.add(resume)
                session.flush()
                job = JobOpportunity(
                    resume_id=resume.id, title="Analyst", company="ACME",
                    job_url="https://example.test/1", portal="linkedin",
                    easy_apply=True, match_score=50, status=ApplicationStatus.MATCHED,
                )
                session.add(job)
                session.flush()
                session.add(ApplicationRecord(
                    job_id=job.id, portal="linkedin",
                    status=ApplicationStatus.DRY_RUN_READY, dry_run=True,
                ))
                session.commit()
        finally:
            engine.dispose()
        return url

    def row_counts(self, url: str) -> dict[str, int]:
        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                return {
                    table: connection.execute(
                        text(f"SELECT count(*) FROM {table}")
                    ).scalar()
                    for table in ("resume_profiles", "job_opportunities",
                                  "application_records", "portal_runtime_state")
                }
        finally:
            engine.dispose()

    def test_an_existing_database_verifies_and_can_be_stamped(self) -> None:
        url = self.seeded_legacy_database()
        report = verify_schema(url)
        self.assertTrue(report.matches, report.render())
        self.assertIsNone(report.alembic_revision, "not stamped yet")

        stamped = stamp_baseline(url)
        self.assertEqual(stamped.alembic_revision, BASELINE_REVISION)
        self.assertEqual(verify_schema(url).alembic_revision, BASELINE_REVISION)

    def test_adoption_deletes_no_data(self) -> None:
        url = self.seeded_legacy_database()
        before = self.row_counts(url)
        self.assertEqual(before["application_records"], 1)

        stamp_baseline(url)
        upgrade_to_head(url)  # already at head: must be a no-op

        self.assertEqual(self.row_counts(url), before)

    def test_application_history_survives_and_is_still_readable(self) -> None:
        url = self.seeded_legacy_database()
        stamp_baseline(url)
        upgrade_to_head(url)

        engine = create_engine(url)
        try:
            with engine.connect() as connection:
                row = connection.execute(text(
                    "SELECT a.status, j.title FROM application_records a "
                    "JOIN job_opportunities j ON j.id = a.job_id WHERE a.id = 1"
                )).one()
        finally:
            engine.dispose()
        self.assertEqual(tuple(row), ("DRY_RUN_READY", "Analyst"))


@requires_postgres
class MismatchRejectionTests(_DatabaseTestCase):
    """A mismatched database must never be stamped."""

    def damaged_database(self, statement: str) -> str:
        url = self.new_database()
        upgrade_to_head(url)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM alembic_version"))
                connection.execute(text(statement))
        finally:
            engine.dispose()
        return url

    def test_a_missing_table_is_detected_and_rejected(self) -> None:
        url = self.damaged_database("DROP TABLE application_records CASCADE")
        report = verify_schema(url)
        self.assertFalse(report.matches)
        self.assertIn("application_records", report.missing_tables)
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_a_missing_column_is_detected_and_rejected(self) -> None:
        url = self.damaged_database(
            "ALTER TABLE job_opportunities DROP COLUMN priority_score CASCADE"
        )
        report = verify_schema(url)
        self.assertFalse(report.matches)
        self.assertIn("job_opportunities.priority_score", report.missing_columns)
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_a_missing_index_is_detected_and_rejected(self) -> None:
        url = self.damaged_database("DROP INDEX ix_resume_profiles_fingerprint")
        report = verify_schema(url)
        self.assertFalse(report.matches)
        self.assertIn("resume_profiles.ix_resume_profiles_fingerprint", report.missing_indexes)
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_a_missing_unique_constraint_is_detected_and_rejected(self) -> None:
        url = self.damaged_database(
            "ALTER TABLE job_opportunities DROP CONSTRAINT uq_job_url_resume_batch"
        )
        report = verify_schema(url)
        self.assertFalse(report.matches)
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_a_missing_named_foreign_key_is_detected_and_rejected(self) -> None:
        url = self.damaged_database(
            "ALTER TABLE job_opportunities DROP CONSTRAINT fk_job_opportunities_search_batch"
        )
        report = verify_schema(url)
        self.assertFalse(report.matches)
        self.assertTrue(any("fk_job_opportunities_search_batch" in f
                            for f in report.missing_foreign_keys))
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_an_empty_database_is_rejected(self) -> None:
        url = self.new_database()
        report = verify_schema(url)
        self.assertFalse(report.matches)
        self.assertEqual(sorted(report.missing_tables), sorted(REQUIRED_TABLES))
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)

    def test_a_rejected_database_is_left_unstamped(self) -> None:
        url = self.damaged_database("DROP TABLE application_records CASCADE")
        with self.assertRaises(SchemaMismatch):
            stamp_baseline(url)
        self.assertIsNone(verify_schema(url).alembic_revision)

    def test_missing_enum_value_is_detected(self) -> None:
        """A database predating the DRY_RUN_READY status must not be stamped."""
        url = self.new_database()
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                connection.execute(text("CREATE TYPE applicationstatus AS ENUM ('MATCHED')"))
        finally:
            engine.dispose()
        report = verify_schema(url)
        self.assertIn("DRY_RUN_READY", report.missing_enum_values)
        self.assertFalse(report.matches)


@requires_postgres
class ApplicationStartupTests(_DatabaseTestCase):
    """The application must run against a database built purely by Alembic."""

    def test_orm_can_read_and_write_a_migrated_database(self) -> None:
        url = self.new_database()
        upgrade_to_head(url)

        from app.models import ApplicationRecord, ApplicationStatus, JobOpportunity, ResumeProfile

        engine = create_engine(url)
        Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        try:
            with Session() as session:
                resume = ResumeProfile(
                    portal="linkedin", filename="cv.pdf", stored_path="/tmp/cv.pdf",
                    raw_text="text",
                )
                session.add(resume)
                session.flush()
                job = JobOpportunity(
                    resume_id=resume.id, title="Analyst", company="ACME",
                    job_url="https://example.test/1", portal="dice",
                    status=ApplicationStatus.MATCHED,
                )
                session.add(job)
                session.flush()
                session.add(ApplicationRecord(
                    job_id=job.id, portal="dice", status=ApplicationStatus.DRY_RUN_READY,
                ))
                session.commit()

            with Session() as session:
                record = session.query(ApplicationRecord).one()
                self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)
                self.assertTrue(record.dry_run, "dry-run default must survive the migration")
                self.assertEqual(record.job.title, "Analyst")
        finally:
            engine.dispose()

    def test_portal_state_row_is_usable_by_the_application(self) -> None:
        url = self.new_database()
        upgrade_to_head(url)

        from app.models import PortalRuntimeState

        engine = create_engine(url)
        Session = sessionmaker(bind=engine)
        try:
            with Session() as session:
                state = session.get(PortalRuntimeState, 1)
                self.assertIsNotNone(state, "portal_runtime_state seed row is missing")
                self.assertEqual(state.active_portal, "linkedin")
        finally:
            engine.dispose()


class RealDatabaseUntouchedTests(unittest.TestCase):
    """Verification against the real database is read-only, and the test helper cannot
    target it."""

    def test_the_real_database_name_is_not_a_test_database(self) -> None:
        from app.config import get_settings
        from sqlalchemy.engine import make_url

        real = make_url(get_settings().database_url).database or ""
        self.assertFalse(real.startswith(TEST_DB_PREFIX))
        with self.assertRaises(UnsafeDatabaseName):
            drop_database(real)

    @requires_postgres
    def test_verifying_the_real_database_changes_nothing(self) -> None:
        """verify_schema() is read-only: no revision is written, no table is created."""
        from app.config import get_settings

        url = get_settings().database_url
        before = schema_snapshot(url)
        report = verify_schema(url)
        self.assertEqual(schema_snapshot(url), before, "verification must not alter the schema")
        # It must also not create an alembic_version table as a side effect.
        engine = create_engine(url)
        try:
            self.assertNotIn(
                "alembic_version",
                set(inspect(engine).get_table_names()) - set(REQUIRED_TABLES)
                if report.alembic_revision is None else set(),
            )
        finally:
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
