"""Secure credential storage and database URL resolution.

Guarantees under test:

* no production credential exists anywhere in source;
* the resolution order is exactly: DATABASE_URL -> secure store -> explicitly enabled
  development fallback -> actionable error;
* the application and Alembic resolve the SAME url;
* passwords are redacted from every message this codebase produces;
* the Windows DPAPI store round-trips, and there is no silent downgrade to unprotected
  storage on any platform.

Every test points the store at a temporary directory, so the developer's own stored
credential is never read, written or deleted.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db_credentials import (
    ALLOW_DEV_DB_ENV,
    DEFAULT_DATABASE,
    DEFAULT_ROLE,
    PASSWORD_LENGTH,
    REDACTED,
    SOURCE_DEV_FALLBACK,
    SOURCE_ENVIRONMENT,
    SOURCE_SECURE_STORE,
    URL_ENV,
    DatabaseCredential,
    DatabaseCredentialsUnavailable,
    generate_password,
    load_credential,
    redact_secrets,
    redact_url,
    resolve_database_url,
    resolve_source,
    store_credential,
)
from app.paths import project_root
from app.secure_store import (
    INSECURE_STORE_ENV,
    STORE_DIR_ENV,
    CredentialStoreError,
    InsecureFileStore,
    WindowsDpapiStore,
    credential_path,
    get_credential_store,
    windows_dpapi_available,
)

WINDOWS = sys.platform == "win32"
requires_windows = unittest.skipUnless(WINDOWS, "DPAPI is Windows-only")

SAMPLE_PASSWORD = "s3cr3t-pa55word-not-real"
SAMPLE_URL = f"postgresql+psycopg://someuser:{SAMPLE_PASSWORD}@localhost:5432/somedb"

# Everything that can influence resolution, cleared for each test.
CREDENTIAL_ENV = (
    URL_ENV, ALLOW_DEV_DB_ENV, INSECURE_STORE_ENV, STORE_DIR_ENV,
    "PGPASSWORD", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE",
)


class _CredentialTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="cred-test-"))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

    def env(self, **overrides: str):
        """Only the given credential variables are set; the rest are removed."""
        environ = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_ENV}
        environ[STORE_DIR_ENV] = str(self.temp_dir)
        environ.update(overrides)
        return patch.dict(os.environ, environ, clear=True)

    def insecure_store(self) -> InsecureFileStore:
        return InsecureFileStore(self.temp_dir / "database_credential.dat")


class NoHardcodedCredentialTests(unittest.TestCase):
    """The previous default put a live password in source control."""

    def test_config_has_no_hardcoded_database_password(self) -> None:
        source = (project_root() / "app" / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("postgres:5555", source)
        self.assertNotIn("postgresql+psycopg://", source)
        self.assertIn("default_factory=resolve_database_url", source)

    def test_no_module_carries_a_default_postgres_password(self) -> None:
        """A URL literal with credentials must not reappear anywhere in app/."""
        import re

        pattern = re.compile(r"postgresql(\+\w+)?://[^\s\"']*:[^\s\"'@/]+@")
        for path in (project_root() / "app").rglob("*.py"):
            text_content = path.read_text(encoding="utf-8")
            for line in text_content.splitlines():
                if line.strip().startswith("#") or '"""' in line:
                    continue
                with self.subTest(file=path.name, line=line.strip()[:70]):
                    self.assertIsNone(pattern.search(line))

    def test_generated_passwords_are_strong_and_unique(self) -> None:
        first, second = generate_password(), generate_password()
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), PASSWORD_LENGTH)
        # No characters that would need URL or SQL escaping.
        for forbidden in ("'", '"', "\\", "@", ":", "/", " "):
            self.assertNotIn(forbidden, first)


class RedactionTests(unittest.TestCase):
    def test_redact_url_removes_the_password(self) -> None:
        redacted = redact_url(SAMPLE_URL)
        self.assertNotIn(SAMPLE_PASSWORD, redacted)
        self.assertIn(REDACTED, redacted)
        self.assertIn("somedb", redacted, "non-secret parts stay readable")
        self.assertIn("someuser", redacted)

    def test_redact_url_handles_missing_and_malformed_input(self) -> None:
        self.assertEqual(redact_url(None), "")
        self.assertEqual(redact_url(""), "")
        self.assertNotIn("pw", redact_url("not://a:pw@real/url"))

    def test_redact_secrets_scrubs_arbitrary_text(self) -> None:
        message = f"connection failed for password {SAMPLE_PASSWORD} on host"
        cleaned = redact_secrets(message, SAMPLE_PASSWORD)
        self.assertNotIn(SAMPLE_PASSWORD, cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_redact_secrets_ignores_short_or_empty_values(self) -> None:
        """Redacting a 1-2 character value would mangle unrelated text."""
        self.assertEqual(redact_secrets("a normal message", ""), "a normal message")
        self.assertEqual(redact_secrets("a normal message", None), "a normal message")
        self.assertEqual(redact_secrets("a normal message", "a"), "a normal message")

    def test_credential_safe_url_never_contains_the_password(self) -> None:
        credential = DatabaseCredential(password=SAMPLE_PASSWORD)
        self.assertNotIn(SAMPLE_PASSWORD, credential.safe_url())
        self.assertIn(SAMPLE_PASSWORD, credential.url(), "the real URL still works")


class ResolutionPrecedenceTests(_CredentialTestCase):
    def test_environment_variable_wins(self) -> None:
        store = self.insecure_store()
        store_credential(DatabaseCredential(password="from-store"), store)
        with self.env(**{URL_ENV: SAMPLE_URL, INSECURE_STORE_ENV: "1"}):
            self.assertEqual(resolve_database_url(store), SAMPLE_URL)
            self.assertEqual(resolve_source(store), SOURCE_ENVIRONMENT)

    def test_secure_store_is_used_when_no_environment_variable(self) -> None:
        store = self.insecure_store()
        store_credential(
            DatabaseCredential(host="localhost", port=5432, database="mydb",
                               username="myrole", password="stored-password"),
            store,
        )
        with self.env(**{INSECURE_STORE_ENV: "1"}):
            url = resolve_database_url(store)
            self.assertIn("myrole", url)
            self.assertIn("mydb", url)
            self.assertIn("stored-password", url)
            self.assertEqual(resolve_source(store), SOURCE_SECURE_STORE)

    def test_development_fallback_requires_explicit_opt_in(self) -> None:
        store = self.insecure_store()
        with self.env(**{"PGPASSWORD": "devpass"}):
            with self.assertRaises(DatabaseCredentialsUnavailable):
                resolve_database_url(store)

        with self.env(**{ALLOW_DEV_DB_ENV: "1", "PGPASSWORD": "devpass"}):
            url = resolve_database_url(store)
            self.assertIn("devpass", url)
            # PGUSER is unset, so the fallback uses the conventional postgres account.
            self.assertEqual(DatabaseCredential.from_url(url).username, "postgres")
            self.assertEqual(resolve_source(store), SOURCE_DEV_FALLBACK)

    def test_development_fallback_honours_libpq_variables(self) -> None:
        store = self.insecure_store()
        with self.env(**{ALLOW_DEV_DB_ENV: "1", "PGPASSWORD": "devpass",
                         "PGHOST": "127.0.0.1", "PGPORT": "5433",
                         "PGUSER": "someone", "PGDATABASE": "otherdb"}):
            url = resolve_database_url(store)
        self.assertIn("127.0.0.1:5433", url)
        self.assertIn("someone", url)
        self.assertIn("otherdb", url)

    def test_development_fallback_without_a_password_fails_loudly(self) -> None:
        store = self.insecure_store()
        with self.env(**{ALLOW_DEV_DB_ENV: "1"}):
            with self.assertRaises(DatabaseCredentialsUnavailable) as caught:
                resolve_database_url(store)
        self.assertIn("PGPASSWORD", str(caught.exception))

    def test_nothing_configured_raises_an_actionable_error(self) -> None:
        store = self.insecure_store()
        with self.env():
            with self.assertRaises(DatabaseCredentialsUnavailable) as caught:
                resolve_database_url(store)
        message = str(caught.exception)
        self.assertIn("provision_postgres.ps1", message)
        self.assertIn(URL_ENV, message)
        self.assertIn("docs/POSTGRES_PROVISIONING.md", message)

    def test_it_never_falls_back_to_a_hardcoded_password(self) -> None:
        """With nothing configured, resolution FAILS rather than guessing."""
        store = self.insecure_store()
        with self.env():
            with self.assertRaises(DatabaseCredentialsUnavailable):
                resolve_database_url(store)
            self.assertIsNone(resolve_source(store))

    def test_unreadable_store_does_not_prevent_the_dev_fallback(self) -> None:
        """A corrupt blob must degrade to the next step, not crash resolution."""
        store = self.insecure_store()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_bytes(b"this is not a valid credential blob")
        with self.env(**{ALLOW_DEV_DB_ENV: "1", "PGPASSWORD": "devpass"}):
            self.assertIn("devpass", resolve_database_url(store))


class ApplicationAndAlembicAgreeTests(_CredentialTestCase):
    def test_settings_and_alembic_resolve_the_same_url(self) -> None:
        from app.config import Settings, reset_settings_cache

        with self.env(**{URL_ENV: SAMPLE_URL}):
            reset_settings_cache()
            self.addCleanup(reset_settings_cache)
            settings = Settings()
            self.assertEqual(settings.database_url, SAMPLE_URL)
            # alembic/env.py falls through to exactly this value.
            self.assertEqual(resolve_database_url(), settings.database_url)

    def test_env_py_defers_to_the_application_settings(self) -> None:
        env = (project_root() / "alembic" / "env.py").read_text(encoding="utf-8")
        self.assertIn("get_settings().database_url", env)


class StoreRoundTripTests(_CredentialTestCase):
    def test_insecure_store_round_trip(self) -> None:
        store = self.insecure_store()
        original = DatabaseCredential(host="localhost", port=5433, database="db",
                                      username="role", password=SAMPLE_PASSWORD)
        store_credential(original, store)
        self.assertEqual(load_credential(store), original)

    def test_store_reports_that_it_is_not_secure(self) -> None:
        info = self.insecure_store().describe()
        self.assertFalse(info.secure)
        self.assertEqual(info.mechanism, "insecure-file")

    def test_missing_credential_returns_none(self) -> None:
        self.assertIsNone(load_credential(self.insecure_store()))

    def test_delete_removes_the_credential(self) -> None:
        store = self.insecure_store()
        store_credential(DatabaseCredential(password="x"), store)
        self.assertTrue(store.exists())
        store.delete()
        self.assertFalse(store.exists())
        self.assertIsNone(load_credential(store))

    def test_saving_twice_replaces_rather_than_appends(self) -> None:
        store = self.insecure_store()
        store_credential(DatabaseCredential(password="first"), store)
        store_credential(DatabaseCredential(password="second"), store)
        self.assertEqual(load_credential(store).password, "second")

    def test_port_is_persisted_with_the_credential(self) -> None:
        """A non-default port must survive, or the app would look on 5432 next start."""
        store = self.insecure_store()
        store_credential(DatabaseCredential(port=5433, password="x"), store)
        self.assertEqual(load_credential(store).port, 5433)


@requires_windows
class WindowsDpapiTests(_CredentialTestCase):
    def test_dpapi_round_trip(self) -> None:
        store = WindowsDpapiStore(self.temp_dir / "database_credential.dat")
        original = DatabaseCredential(database="dpapi_db", username="dpapi_role",
                                      password=SAMPLE_PASSWORD)
        store_credential(original, store)
        self.assertEqual(load_credential(store), original)

    def test_the_stored_file_does_not_contain_the_plaintext_password(self) -> None:
        store = WindowsDpapiStore(self.temp_dir / "database_credential.dat")
        store_credential(DatabaseCredential(password=SAMPLE_PASSWORD), store)
        raw = store.path.read_bytes()
        self.assertNotIn(SAMPLE_PASSWORD.encode(), raw)
        self.assertNotIn(SAMPLE_PASSWORD.encode("utf-16-le"), raw)

    def test_a_corrupted_blob_is_reported_clearly(self) -> None:
        store = WindowsDpapiStore(self.temp_dir / "database_credential.dat")
        store_credential(DatabaseCredential(password=SAMPLE_PASSWORD), store)
        store.path.write_bytes(b"\x01\x02\x03 not a DPAPI blob")
        with self.assertRaises(CredentialStoreError) as caught:
            store.load()
        self.assertIn("re-run provisioning", str(caught.exception))

    def test_the_default_store_on_windows_is_dpapi(self) -> None:
        with self.env():
            store = get_credential_store()
        self.assertIsInstance(store, WindowsDpapiStore)
        self.assertTrue(store.describe().secure)

    def test_dpapi_is_reported_available(self) -> None:
        self.assertTrue(windows_dpapi_available())


class StoreSelectionTests(_CredentialTestCase):
    def test_insecure_store_is_never_selected_silently(self) -> None:
        """Without the opt-in, a platform with no DPAPI must FAIL rather than downgrade."""
        with self.env():
            with patch("app.secure_store.windows_dpapi_available", return_value=False):
                with self.assertRaises(CredentialStoreError) as caught:
                    get_credential_store()
        self.assertIn(INSECURE_STORE_ENV, str(caught.exception))

    def test_the_opt_in_selects_the_insecure_store_on_any_platform(self) -> None:
        with self.env(**{INSECURE_STORE_ENV: "1"}):
            store = get_credential_store()
        self.assertIsInstance(store, InsecureFileStore)
        self.assertFalse(store.describe().secure)

    def test_the_credential_path_follows_the_runtime_config_directory(self) -> None:
        from app.paths import reset_runtime_paths_cache, runtime_paths

        environ = {k: v for k, v in os.environ.items() if k != STORE_DIR_ENV}
        with patch.dict(os.environ, environ, clear=True):
            reset_runtime_paths_cache()
            self.addCleanup(reset_runtime_paths_cache)
            self.assertEqual(credential_path().parent, runtime_paths().config)

    def test_an_explicit_directory_override_is_honoured(self) -> None:
        with self.env():
            self.assertEqual(credential_path().parent, self.temp_dir)


class CredentialShapeTests(unittest.TestCase):
    def test_defaults_name_the_dedicated_role_and_database(self) -> None:
        credential = DatabaseCredential()
        self.assertEqual(credential.username, DEFAULT_ROLE)
        self.assertEqual(credential.database, DEFAULT_DATABASE)
        self.assertEqual(credential.username, "ai_job_automation_app")

    def test_round_trip_through_a_url(self) -> None:
        original = DatabaseCredential(host="127.0.0.1", port=5433, database="db",
                                      username="role", password=SAMPLE_PASSWORD)
        self.assertEqual(DatabaseCredential.from_url(original.url()), original)

    def test_a_password_with_url_metacharacters_survives_a_round_trip(self) -> None:
        awkward = "p@ss:word/with#chars?and&more"
        original = DatabaseCredential(password=awkward)
        self.assertEqual(DatabaseCredential.from_url(original.url()).password, awkward)


if __name__ == "__main__":
    unittest.main()
