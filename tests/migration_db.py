"""Disposable PostgreSQL databases for migration tests.

SAFETY: every database handed out by this module is created here, carries the
``ai_job_migtest_`` prefix, and is dropped again at the end of the test. The application's
real database can never be selected -- :func:`_guard` refuses any name without the prefix,
so a mistake in a test fails loudly instead of dropping the user's data.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import get_settings

# Only databases whose name starts with this may be created or dropped here.
TEST_DB_PREFIX = "ai_job_migtest_"
# The database Alembic connects to in order to issue CREATE/DROP DATABASE (which cannot run
# inside a transaction, and cannot run while connected to the target).
MAINTENANCE_DB = "postgres"


class UnsafeDatabaseName(RuntimeError):
    """Raised when something tries to create or drop a non-test database."""


def _guard(name: str) -> str:
    if not name.startswith(TEST_DB_PREFIX):
        raise UnsafeDatabaseName(
            f"refusing to touch database {name!r}: migration tests may only create or drop "
            f"databases named {TEST_DB_PREFIX}*"
        )
    return name


def base_url():
    """The application's URL, used only for host/port/credentials -- never its database."""
    return make_url(get_settings().database_url)


def url_for(database: str) -> str:
    return base_url().set(database=_guard(database)).render_as_string(hide_password=False)


def unique_name() -> str:
    return f"{TEST_DB_PREFIX}{uuid.uuid4().hex[:12]}"


def _maintenance_engine():
    url = base_url().set(database=MAINTENANCE_DB)
    return create_engine(url, isolation_level="AUTOCOMMIT", poolclass=None)


def create_database(name: str) -> str:
    _guard(name)
    engine = _maintenance_engine()
    try:
        with engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        engine.dispose()
    return url_for(name)


def drop_database(name: str) -> None:
    _guard(name)
    engine = _maintenance_engine()
    try:
        with engine.connect() as connection:
            # Evict stragglers so DROP cannot block on a leaked connection.
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    finally:
        engine.dispose()


def postgres_available() -> bool:
    try:
        engine = _maintenance_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


@contextmanager
def temporary_database():
    """A fresh, empty database that is dropped when the block exits."""
    name = unique_name()
    url = create_database(name)
    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    try:
        yield name, url
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous
        drop_database(name)
