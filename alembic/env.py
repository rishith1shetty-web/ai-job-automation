"""Alembic environment.

The database URL is taken from the application's own settings, so migrations always target
exactly the database the app uses. It is never written into alembic.ini (which is committed
to source control) and never logged.

``ALEMBIC_DATABASE_URL`` overrides the application setting. That is how the migration tests
point Alembic at a disposable database instead of the user's real one.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# Importing the app package requires the project root on sys.path when Alembic is invoked
# from another working directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app import models  # noqa: F401,E402  (registers every table on Base.metadata)

config = context.config

if config.config_file_name is not None and not logging.getLogger().handlers:
    # Two guards, both learned the hard way:
    #
    # * disable_existing_loggers=False -- the default (True) SILENCES every logger that
    #   already exists, so an in-process migration would permanently disable the
    #   application's own loggers.
    # * only configure at all when the root logger has NO handlers yet. fileConfig REPLACES
    #   the root handlers, so running a migration from inside the packaged launcher used to
    #   throw away the launcher's file handler -- the app then pointed the user at a log
    #   file that contained nothing after the migration step, including the crash that
    #   caused the dialog. When the host application has already configured logging, its
    #   configuration wins; the standalone `alembic` CLI still gets alembic.ini's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate compares against the ORM models.
target_metadata = Base.metadata

# Overrides the application setting; used by the migration tests so they never touch the
# real database.
URL_ENV = "ALEMBIC_DATABASE_URL"


def database_url() -> str:
    """Resolve the target URL. Never printed or logged -- it contains a password.

    Below the two explicit overrides this defers to exactly the same resolver the
    application uses (``app.db_credentials.resolve_database_url``, reached through
    ``get_settings()``), so a migration can never run against a different database than the
    one the app will open."""
    override = (os.environ.get(URL_ENV) or "").strip()
    if override:
        return override
    url = (config.get_main_option("sqlalchemy.url", None) or "").strip()
    if url:
        return url
    return get_settings().database_url


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keep Alembic's attention on the application's own tables."""
    if type_ == "table" and name == "alembic_version":
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (``alembic upgrade head --sql``)."""
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and run migrations against the live database."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
