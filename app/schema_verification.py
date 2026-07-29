"""Verify a live database against the Alembic baseline, and stamp it only if it matches.

An existing installation already contains real application history, so it must NOT be
migrated from scratch -- it has to be ADOPTED: told that it already sits at the baseline
revision. ``alembic stamp`` does that, but stamping is a blind assertion; if the database
does not actually match the baseline, every later migration will run against a schema it
was not written for.

This module makes the assertion checked. :func:`verify_schema` compares the structures the
application genuinely depends on, and :func:`stamp_baseline` refuses to stamp unless the
verification passed.

Deliberately NOT compared:

* ``server_default`` -- older databases carry DDL defaults on columns that were added by
  ``ALTER TABLE ... ADD COLUMN ... DEFAULT`` in init_db(), while columns created by
  ``create_all`` never had one. The ORM declares Python-side defaults, so both behave
  identically at runtime. Comparing them would reject perfectly good databases.
* extra tables, columns and indexes -- an installation may carry leftovers from an older
  build. They are reported for information, never treated as failures.

Nothing here prints a connection URL or a password.

Command line::

    python -m app.schema_verification              # verify the configured database
    python -m app.schema_verification --stamp      # verify, then stamp if it matches
    python -m app.schema_verification --url ...    # verify a specific database
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import make_url

# The revision produced by alembic/versions/*_baseline_current_schema.py. An existing
# database that passes verification is stamped at exactly this revision.
BASELINE_REVISION = "ed2a513cd149"

ENUM_NAME = "applicationstatus"
# The database stores the enum member NAMES, not the lowercase values.
REQUIRED_ENUM_VALUES = (
    "MATCHED", "QUEUED", "APPLIED", "NEEDS_REVIEW",
    "SKIPPED", "FAILED", "DRY_RUN_READY", "UNSUPPORTED_FORM",
)

REQUIRED_TABLES = (
    "application_records",
    "apply_top_batches",
    "candidate_employment_history",
    "candidate_profiles",
    "form_field_learning",
    "generalized_form_field_metadata",
    "job_opportunities",
    "job_search_batches",
    "portal_runtime_state",
    "resume_profiles",
    "screening_automation_settings",
    "screening_knowledge_answers",
)

# Columns the application cannot run without. Not an exhaustive column list: these are the
# ones whose absence breaks matching, application history, portal state or resume identity.
CRITICAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "resume_profiles": (
        "id", "portal", "filename", "stored_path", "raw_text", "resume_fingerprint",
        "skills", "domain_profile", "role_profile", "created_at",
    ),
    "candidate_profiles": ("id", "portal", "email", "is_default", "field_sources"),
    "job_search_batches": (
        "id", "resume_id", "portal", "status", "requested_limit",
        "current_variation_index", "total_variations", "jobs_collected", "created_at",
    ),
    "job_opportunities": (
        "id", "resume_id", "search_batch_id", "search_timestamp", "title", "company",
        "job_url", "portal", "easy_apply", "match_score", "priority_score",
        "freshness_score", "job_description_metadata", "status", "created_at",
    ),
    "application_records": (
        "id", "job_id", "portal", "status", "application_date", "dry_run",
        "current_step", "evidence_path", "evidence_sources", "run_log",
    ),
    "apply_top_batches": (
        "id", "resume_id", "search_batch_id", "portal", "status", "dry_run",
        "application_limit", "current_job_id", "job_results",
    ),
    "portal_runtime_state": ("id", "active_portal", "updated_at"),
    "screening_knowledge_answers": (
        "id", "candidate_profile_id", "resume_id", "normalized_question", "answer",
    ),
    "candidate_employment_history": ("id", "candidate_profile_id", "resume_id", "job_title"),
    "form_field_learning": ("id", "original_label", "normalized_field", "field_category"),
    "generalized_form_field_metadata": (
        "id", "portal", "original_field_label", "normalized_field_name",
        "detected_control_type", "recommended_fill_strategy",
    ),
    "screening_automation_settings": ("id", "mode", "notice_period_default"),
}

# Indexes the application's ordering and lookup paths depend on.
REQUIRED_INDEXES: dict[str, tuple[str, ...]] = {
    "job_opportunities": (
        "ix_job_opportunities_search_batch_id", "ix_job_opportunities_search_timestamp",
        "ix_job_opportunities_priority_score", "ix_job_opportunities_portal",
        "ix_job_opportunities_status", "ix_job_opportunities_resume_id",
    ),
    "application_records": (
        "ix_application_records_job_id", "ix_application_records_portal",
        "ix_application_records_status",
    ),
    "job_search_batches": (
        "ix_job_search_batches_status", "ix_job_search_batches_portal",
        "ix_job_search_batches_resume_id", "ix_job_search_batches_created_at",
    ),
    "apply_top_batches": (
        "ix_apply_top_batches_status", "ix_apply_top_batches_portal",
        "ix_apply_top_batches_resume_id", "ix_apply_top_batches_search_batch_id",
    ),
    "resume_profiles": (
        "ix_resume_profiles_portal", "ix_resume_profiles_fingerprint",
    ),
    "candidate_profiles": ("ix_candidate_profiles_portal", "ix_candidate_profiles_is_default"),
}

REQUIRED_UNIQUE_CONSTRAINTS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "job_opportunities": (
        ("uq_job_url_resume_batch", ("job_url", "resume_id", "search_batch_id")),
    ),
    "screening_knowledge_answers": (
        ("uq_screening_answer_candidate_question",
         ("candidate_profile_id", "normalized_question")),
    ),
    "form_field_learning": (
        ("uq_form_field_learning_mapping",
         ("original_label", "normalized_field", "company", "role")),
    ),
    "generalized_form_field_metadata": (
        ("uq_generalized_form_field_metadata",
         ("portal", "original_field_label", "normalized_field_name")),
    ),
}

# Foreign keys application history and portal state rely on, compared by SHAPE
# (columns -> referred table) rather than by name, because create_all's auto-generated
# names differ between PostgreSQL versions and installation ages.
REQUIRED_FOREIGN_KEYS: dict[str, tuple[tuple[tuple[str, ...], str], ...]] = {
    "application_records": ((("job_id",), "job_opportunities"),),
    "job_opportunities": (
        (("resume_id",), "resume_profiles"),
        (("search_batch_id",), "job_search_batches"),
    ),
    "job_search_batches": ((("resume_id",), "resume_profiles"),),
    "apply_top_batches": (
        (("resume_id",), "resume_profiles"),
        (("current_job_id",), "job_opportunities"),
    ),
    "candidate_employment_history": (
        (("candidate_profile_id",), "candidate_profiles"),
        (("resume_id",), "resume_profiles"),
    ),
    "screening_knowledge_answers": (
        (("candidate_profile_id",), "candidate_profiles"),
        (("resume_id",), "resume_profiles"),
    ),
}

# The one foreign key init_db() creates under an explicit name.
NAMED_FOREIGN_KEYS: dict[str, tuple[str, ...]] = {
    "job_opportunities": ("fk_job_opportunities_search_batch",),
}


class SchemaMismatch(RuntimeError):
    """Raised when a database does not match the baseline and must not be stamped."""


@dataclass
class SchemaReport:
    """What a live database is missing relative to the baseline."""

    database: str = ""
    tables_present: int = 0
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    missing_enum_values: list[str] = field(default_factory=list)
    missing_indexes: list[str] = field(default_factory=list)
    missing_unique_constraints: list[str] = field(default_factory=list)
    missing_foreign_keys: list[str] = field(default_factory=list)
    alembic_revision: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return (
            [f"missing table: {t}" for t in self.missing_tables]
            + [f"missing column: {c}" for c in self.missing_columns]
            + [f"missing enum value: {v}" for v in self.missing_enum_values]
            + [f"missing index: {i}" for i in self.missing_indexes]
            + [f"missing unique constraint: {u}" for u in self.missing_unique_constraints]
            + [f"missing foreign key: {f}" for f in self.missing_foreign_keys]
        )

    @property
    def matches(self) -> bool:
        return not self.problems

    def render(self) -> str:
        lines = [
            f"Database          : {self.database}",
            f"Tables present    : {self.tables_present}/{len(REQUIRED_TABLES)}",
            f"Alembic revision  : {self.alembic_revision or '(not stamped)'}",
            f"Baseline expected : {BASELINE_REVISION}",
            "",
        ]
        if self.matches:
            lines.append("RESULT: schema MATCHES the baseline.")
        else:
            lines.append(f"RESULT: schema does NOT match the baseline ({len(self.problems)} problems).")
            lines += [f"  - {p}" for p in self.problems]
        if self.notes:
            lines += ["", "Notes (informational only):"] + [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


def _engine(url: str | None) -> Engine:
    from app.config import get_settings

    return create_engine(url or get_settings().database_url, pool_pre_ping=True)


def safe_database_label(url: str) -> str:
    """host/database only -- never the user name or password."""
    parsed = make_url(url)
    return f"{parsed.host or 'localhost'}/{parsed.database or '?'}"


def verify_schema(url: str | None = None, engine: Engine | None = None) -> SchemaReport:
    """Compare a live database against the baseline. Read-only: changes nothing."""
    from app.config import get_settings

    owns_engine = engine is None
    target_url = url or get_settings().database_url
    engine = engine or _engine(target_url)
    report = SchemaReport(database=safe_database_label(str(engine.url)))

    try:
        inspector = inspect(engine)
        present = set(inspector.get_table_names())
        report.tables_present = len(present & set(REQUIRED_TABLES))
        report.missing_tables = sorted(set(REQUIRED_TABLES) - present)

        for table, columns in CRITICAL_COLUMNS.items():
            if table not in present:
                continue  # already reported as a missing table
            actual = {c["name"] for c in inspector.get_columns(table)}
            report.missing_columns += [f"{table}.{c}" for c in columns if c not in actual]

        with engine.connect() as connection:
            enum_values = {
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT e.enumlabel FROM pg_enum e "
                        "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :name"
                    ),
                    {"name": ENUM_NAME},
                )
            }
            report.missing_enum_values = [
                v for v in REQUIRED_ENUM_VALUES if v not in enum_values
            ]
            extra_enum = sorted(enum_values - set(REQUIRED_ENUM_VALUES))
            if extra_enum:
                report.notes.append(f"extra {ENUM_NAME} values present: {', '.join(extra_enum)}")

            row = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).first() if "alembic_version" in present else None
            report.alembic_revision = row[0] if row else None

        for table, indexes in REQUIRED_INDEXES.items():
            if table not in present:
                continue
            actual = {i["name"] for i in inspector.get_indexes(table)}
            report.missing_indexes += [f"{table}.{i}" for i in indexes if i not in actual]

        for table, constraints in REQUIRED_UNIQUE_CONSTRAINTS.items():
            if table not in present:
                continue
            actual = {
                u["name"]: tuple(u["column_names"])
                for u in inspector.get_unique_constraints(table)
            }
            # A unique INDEX satisfies the same guarantee as a unique CONSTRAINT.
            actual_unique_indexes = {
                i["name"]: tuple(i["column_names"])
                for i in inspector.get_indexes(table)
                if i.get("unique")
            }
            for name, columns in constraints:
                found = actual.get(name) or actual_unique_indexes.get(name)
                if found is None:
                    report.missing_unique_constraints.append(f"{table}.{name}")
                elif tuple(found) != tuple(columns):
                    report.missing_unique_constraints.append(
                        f"{table}.{name} (columns {found} != expected {columns})"
                    )

        for table, keys in REQUIRED_FOREIGN_KEYS.items():
            if table not in present:
                continue
            actual = {
                (tuple(f["constrained_columns"]), f["referred_table"])
                for f in inspector.get_foreign_keys(table)
            }
            for columns, referred in keys:
                if (columns, referred) not in actual:
                    report.missing_foreign_keys.append(
                        f"{table}{list(columns)} -> {referred}"
                    )

        for table, names in NAMED_FOREIGN_KEYS.items():
            if table not in present:
                continue
            actual = {f.get("name") for f in inspector.get_foreign_keys(table)}
            report.missing_foreign_keys += [
                f"{table}.{n} (named constraint)" for n in names if n not in actual
            ]
    finally:
        if owns_engine:
            engine.dispose()

    return report


ALEMBIC_INI_ENV = "AI_JOB_AUTOMATION_ALEMBIC_INI"
ALEMBIC_DIR_ENV = "AI_JOB_AUTOMATION_ALEMBIC_DIR"


def alembic_locations() -> tuple[Path, Path]:
    """(alembic.ini, alembic/) for this installation.

    A packaged build unpacks both next to the executable rather than beside this module, so
    the runtime hook records where they landed. Source checkouts fall back to the project
    root, which is the historical behaviour."""
    import os

    from app.paths import install_root

    configured_ini = (os.environ.get(ALEMBIC_INI_ENV) or "").strip()
    configured_dir = (os.environ.get(ALEMBIC_DIR_ENV) or "").strip()
    if configured_ini and configured_dir:
        return Path(configured_ini), Path(configured_dir)

    root = Path(__file__).resolve().parent.parent
    if not (root / "alembic.ini").is_file():
        root = install_root()
    return root / "alembic.ini", root / "alembic"


def alembic_config(url: str | None = None):
    """An Alembic Config pointing at this project. The URL is passed in memory only."""
    from alembic.config import Config

    ini_file, script_location = alembic_locations()
    config = Config(str(ini_file))
    config.set_main_option("script_location", str(script_location))
    if url:
        # ConfigParser interpolates "%"; a password containing one would otherwise raise.
        config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def stamp_baseline(url: str | None = None, revision: str = BASELINE_REVISION,
                   force: bool = False) -> SchemaReport:
    """Verify, then stamp. Refuses to stamp a database that does not match.

    ``force`` exists only for a deliberate operator override and is never used by the
    application or the tests."""
    from alembic import command

    report = verify_schema(url)
    if not report.matches and not force:
        raise SchemaMismatch(
            "refusing to stamp: the database does not match the baseline.\n"
            + "\n".join(f"  - {p}" for p in report.problems)
            + "\nResolve the differences first; never stamp a mismatched database."
        )
    command.stamp(alembic_config(url), revision)
    report.alembic_revision = revision
    return report


def upgrade_to_head(url: str | None = None) -> None:
    """Run ``alembic upgrade head`` against the given database."""
    from alembic import command

    command.upgrade(alembic_config(url), "head")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify a database against the Alembic baseline, and optionally stamp it."
    )
    parser.add_argument("--url", default=None,
                        help="Database URL. Defaults to the application setting.")
    parser.add_argument("--stamp", action="store_true",
                        help="Stamp the baseline revision if verification passes.")
    parser.add_argument("--upgrade", action="store_true",
                        help="Run 'alembic upgrade head' after a successful verification.")
    args = parser.parse_args(argv)

    report = verify_schema(args.url)
    print(report.render())

    if not report.matches:
        print("\nNothing was changed. Fix the differences before stamping.")
        return 1

    if args.stamp:
        stamp_baseline(args.url)
        print(f"\nStamped at {BASELINE_REVISION}.")
    if args.upgrade:
        upgrade_to_head(args.url)
        print("Upgraded to head.")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
