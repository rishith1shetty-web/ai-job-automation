"""The single place the database URL is resolved, for the app AND for Alembic.

Resolution order -- explicit, tested, and identical for every caller:

1. ``DATABASE_URL`` -- an explicit environment variable (or ``.env``). Development and tests.
2. The secure credential store written by provisioning (``app/secure_store.py``).
3. A development fallback, active ONLY when ``AI_JOB_AUTOMATION_ALLOW_DEV_DB`` is set, and
   assembled from the standard libpq variables (``PGHOST``/``PGPORT``/``PGUSER``/
   ``PGPASSWORD``/``PGDATABASE``).
4. Otherwise: fail with an actionable error naming the provisioning command.

There is deliberately NO hardcoded password anywhere in this module, and none anywhere else
in ``app/``. ``app/config.py`` previously carried a complete connection URL -- superuser name
and real password included -- as the default value of ``database_url``, which meant a working
production credential lived in source control. It has been removed, and a test scans every
module to make sure no such literal can come back.

Nothing here logs a password. :func:`redact_url` and :func:`redact_secrets` produce the safe
forms used in every message this codebase emits.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import string
from dataclasses import dataclass, replace

from sqlalchemy.engine import URL, make_url

from app.secure_store import CredentialStore, get_credential_store

logger = logging.getLogger(__name__)

DRIVER = "postgresql+psycopg"
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 5432
DEFAULT_DATABASE = "linkedin_auto_apply"
# The dedicated least-privilege application role created by provisioning.
DEFAULT_ROLE = "ai_job_automation_app"

URL_ENV = "DATABASE_URL"
ALEMBIC_URL_ENV = "ALEMBIC_DATABASE_URL"
ALLOW_DEV_DB_ENV = "AI_JOB_AUTOMATION_ALLOW_DEV_DB"

REDACTED = "***"

# Password alphabet. Excludes characters that complicate URLs, shell quoting and SQL string
# literals, so the generated secret is safe to move around without escaping surprises.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "-_.~"
PASSWORD_LENGTH = 40

SOURCE_ENVIRONMENT = "environment"
SOURCE_SECURE_STORE = "secure-store"
SOURCE_DEV_FALLBACK = "development-fallback"


class DatabaseCredentialsUnavailable(RuntimeError):
    """No usable database credential could be resolved."""


@dataclass(frozen=True)
class DatabaseCredential:
    """A complete connection identity. ``password`` is never logged or reported."""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    database: str = DEFAULT_DATABASE
    username: str = DEFAULT_ROLE
    password: str = ""

    def url(self) -> str:
        return URL.create(
            DRIVER, username=self.username, password=self.password,
            host=self.host, port=self.port, database=self.database,
        ).render_as_string(hide_password=False)

    def safe_url(self) -> str:
        return URL.create(
            DRIVER, username=self.username, host=self.host,
            port=self.port, database=self.database,
        ).render_as_string(hide_password=True)

    def to_payload(self) -> dict:
        return {
            "host": self.host, "port": self.port, "database": self.database,
            "username": self.username, "password": self.password,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> "DatabaseCredential":
        return cls(
            host=payload.get("host", DEFAULT_HOST),
            port=int(payload.get("port", DEFAULT_PORT)),
            database=payload.get("database", DEFAULT_DATABASE),
            username=payload.get("username", DEFAULT_ROLE),
            password=payload.get("password", ""),
        )

    @classmethod
    def from_url(cls, url: str) -> "DatabaseCredential":
        parsed = make_url(url)
        return cls(
            host=parsed.host or DEFAULT_HOST,
            port=int(parsed.port or DEFAULT_PORT),
            database=parsed.database or DEFAULT_DATABASE,
            username=parsed.username or DEFAULT_ROLE,
            password=parsed.password or "",
        )

    def with_password(self, password: str) -> "DatabaseCredential":
        return replace(self, password=password)


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    """A strong random password. ``secrets``, never ``random``."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def redact_url(url: str | None) -> str:
    """A URL safe to log: the password becomes ``***``."""
    if not url:
        return ""
    try:
        parsed = make_url(url)
    except Exception:
        return re.sub(r"//([^:/@]+):([^@]+)@", rf"//\1:{REDACTED}@", url)
    if not parsed.password:
        return parsed.render_as_string(hide_password=False)
    # render_as_string(hide_password=True) substitutes the mask itself. Setting the password
    # to "***" instead would URL-encode it to "%2A%2A%2A", which reads as a real value.
    return parsed.render_as_string(hide_password=True)


def redact_secrets(text: str, *secret_values: str | None) -> str:
    """Remove specific secret values from arbitrary text (driver errors, tracebacks)."""
    cleaned = text or ""
    for secret in secret_values:
        if secret and len(secret) >= 4:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def dev_fallback_enabled() -> bool:
    return (os.environ.get(ALLOW_DEV_DB_ENV) or "").strip().lower() in {"1", "true", "yes"}


def _from_environment() -> str | None:
    return (os.environ.get(URL_ENV) or "").strip() or None


def _from_secure_store(store: CredentialStore | None = None) -> str | None:
    try:
        store = store or get_credential_store()
    except Exception as error:  # no store available on this platform
        logger.debug("Credential store unavailable: %s", error)
        return None
    try:
        payload = store.load()
    except Exception as error:
        # A blob that cannot be decrypted is a real, reportable condition -- but it must not
        # crash resolution before the development fallback has been considered.
        logger.warning("Stored database credential could not be read: %s", error)
        return None
    if not payload:
        return None
    return DatabaseCredential.from_payload(payload).url()


def _from_dev_fallback() -> str | None:
    """Assembled from libpq variables. Never contains a literal password from source."""
    if not dev_fallback_enabled():
        return None
    password = os.environ.get("PGPASSWORD")
    if not password:
        raise DatabaseCredentialsUnavailable(
            f"{ALLOW_DEV_DB_ENV} is set, but PGPASSWORD is not. Set PGPASSWORD (and "
            f"optionally PGHOST/PGPORT/PGUSER/PGDATABASE), or set {URL_ENV} directly."
        )
    return DatabaseCredential(
        host=os.environ.get("PGHOST") or DEFAULT_HOST,
        port=int(os.environ.get("PGPORT") or DEFAULT_PORT),
        database=os.environ.get("PGDATABASE") or DEFAULT_DATABASE,
        username=os.environ.get("PGUSER") or "postgres",
        password=password,
    ).url()


UNAVAILABLE_MESSAGE = (
    "No database credential is configured.\n"
    "  - Installed application: run the provisioning step to create the database and store "
    "its credential securely:\n"
    "      powershell -ExecutionPolicy Bypass -File scripts\\provision_postgres.ps1\n"
    f"  - Development: set {URL_ENV} in your .env file.\n"
    f"See docs/POSTGRES_PROVISIONING.md."
)


def resolve_database_url(store: CredentialStore | None = None) -> str:
    """The resolved URL, following the documented precedence. Raises when nothing is usable."""
    from_env = _from_environment()
    if from_env:
        return from_env

    from_store = _from_secure_store(store)
    if from_store:
        return from_store

    from_dev = _from_dev_fallback()
    if from_dev:
        logger.warning("Using the development database fallback (%s is set).", ALLOW_DEV_DB_ENV)
        return from_dev

    raise DatabaseCredentialsUnavailable(UNAVAILABLE_MESSAGE)


def resolve_source(store: CredentialStore | None = None) -> str | None:
    """Which precedence step supplied the URL. For diagnostics; never returns a secret."""
    if _from_environment():
        return SOURCE_ENVIRONMENT
    if _from_secure_store(store):
        return SOURCE_SECURE_STORE
    if dev_fallback_enabled():
        return SOURCE_DEV_FALLBACK
    return None


def store_credential(credential: DatabaseCredential,
                     store: CredentialStore | None = None) -> None:
    """Persist a credential securely. The password never reaches a log line."""
    store = store or get_credential_store()
    store.save(credential.to_payload())


def load_credential(store: CredentialStore | None = None) -> DatabaseCredential | None:
    store = store or get_credential_store()
    payload = store.load()
    return DatabaseCredential.from_payload(payload) if payload else None
