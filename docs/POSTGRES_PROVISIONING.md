# Local PostgreSQL provisioning and secure credentials

How the application gets a database on a new Windows computer without the person using it
ever opening pgAdmin, creating a role, editing `.env` or typing a command.

**This layer does not install PostgreSQL, and nothing in the product does.** PostgreSQL 16 is
installed by hand, before the Rising Path Recruiters installer runs — that is the final,
supported deployment model, not a gap waiting to be filled. An earlier design embedded the
official PostgreSQL installer and ran it unattended; it was removed. When PostgreSQL is
missing this layer reports `POSTGRES_NOT_FOUND` (exit code 10), and setup refuses to install
anything until the user has fixed it.

There are two ways this provisioning runs:

| | Used by | Administrator password comes from |
|---|---|---|
| **The installer** | Everybody installing the product | A masked wizard page, handed over as DPAPI ciphertext — see [WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md) |
| **`scripts\provision_postgres.ps1`** | Developers and support | An interactive `SecureString` prompt, piped through stdin |

Both end at the same place: the role, the database, the migrations and the DPAPI-protected
credential described below.

---

## The identity it creates

| | Value | Notes |
|---|---|---|
| Role | `ai_job_automation_app` | `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION` |
| Database | `linkedin_auto_apply` | owned by the role |
| Password | 40 random characters | `secrets`, from `A-Za-z0-9-_.~` |

The role's only authority is over its own database, which it **owns**. Ownership (rather than
a pile of `GRANT`s) is what lets Alembic create, alter and drop the application's own tables
during a migration, while the role remains unable to touch anything else in the cluster,
create databases, create roles or read another database.

After provisioning, the application **never uses the `postgres` superuser again**.

---

## Secure credential storage

**Mechanism: Windows DPAPI (`CryptProtectData`), current-user scope.**
File: `%LOCALAPPDATA%\AIJobAutomation\config\database_credential.dat`

The password is not in source, not in Git, not in `.env`, not in an installer command line,
not in the registry, and not in any log.

**Protection scope and its consequences:**

| Situation | Result |
|---|---|
| Same Windows user, same computer | decrypts silently at every start — no second sign-in |
| Different Windows account, same computer | **cannot** decrypt, even with the file |
| File copied to another computer | useless |
| Windows profile reset / account deleted | credential unrecoverable → re-run provisioning |
| Windows password *changed* normally | still works (DPAPI re-keys automatically) |
| Windows password *reset by an administrator* | may be unrecoverable → re-run provisioning |

Re-running provisioning after a loss is safe: it rotates the role's password and stores the
new one. **No data is lost** — the database is untouched.

Chosen over **Windows Credential Manager** because provisioning may run from an elevated
installer process while the app runs unelevated; a Credential Manager entry written by one
account is invisible to the other. Chosen over **machine-scope DPAPI** because machine scope
lets every account on the computer decrypt it, breaking the per-user isolation established in
Phase 1A.

> The installer must run the provisioning step **as the installing user**, not as `SYSTEM`,
> or the credential will be written to the wrong profile.

A deliberately insecure fallback (`AI_JOB_AUTOMATION_INSECURE_CREDENTIAL_STORE=1`, base64
only) exists for non-Windows CI and for developers. It never activates by accident: without
it, a platform with no DPAPI **fails** rather than silently downgrading.

---

## Credential resolution order

Used identically by the application and by Alembic — they can never disagree about which
database they are talking to.

1. **`DATABASE_URL`** environment variable or `.env` — development and tests.
2. **Secure credential store** — the installed application.
3. **Development fallback** — only when `AI_JOB_AUTOMATION_ALLOW_DEV_DB=1`, assembled from
   `PGHOST` / `PGPORT` / `PGUSER` / `PGPASSWORD` / `PGDATABASE`. Fails loudly if `PGPASSWORD`
   is absent.
4. **Otherwise: an actionable error** naming the provisioning command.

There is **no hardcoded password fallback**. `app/config.py` previously carried a complete
working URL — superuser name and password included — as the default value of `database_url`.
It has been removed, and a test scans every module in `app/` to make sure no such literal can
return.

---

## New computer

On a computer where the product was **installed**, none of this is run by hand: the installer
does it. This is the developer and support path.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\provision_postgres.ps1
```

Prompts for the PostgreSQL administrator password (a `SecureString`, piped to Python through
**stdin** — never a command-line argument, because any user can read any process's command
line on Windows), then:

1. detects PostgreSQL and confirms the port really speaks the PostgreSQL protocol;
2. creates the role if missing;
3. creates the database if missing, owned by that role;
4. stores the credential with DPAPI;
5. runs `alembic upgrade head`;
6. verifies the schema against the baseline.

Add `-Port 5433` for a non-default port — it is persisted **with the credential**, so the app
finds the server again on the next start.

Read-only modes need no password at all:

```powershell
.\scripts\provision_postgres.ps1 -DetectOnly -Json   # what is installed and running
.\scripts\provision_postgres.ps1 -Status -Json       # is this computer provisioned?
```

---

## Provisioning states and exit codes

`app.provisioning.Status`. The values ARE the process exit codes, so there is one numbering
scheme rather than two that can drift apart.

| Code | Status | Meaning | What acts on it |
|---|---|---|---|
| 0 | `SUCCESS` | ready | continue |
| 2 | `INVALID_USAGE` | no administrator password supplied | re-prompt |
| 10 | `POSTGRES_NOT_FOUND` | nothing reachable on the port | setup stops **before installing anything** and tells the user to install PostgreSQL 16 |
| 11 | `PORT_OCCUPIED_BY_OTHER` | something else holds the port | tell the user; free the port |
| 12 | `ADMIN_AUTH_FAILED` | administrator password rejected | re-prompt on the password page |
| 13 | `PROVISION_FAILED` | role/database/ownership step failed | show the message |
| 14 | `MIGRATION_FAILED` | `alembic upgrade head` failed | show the message; database is intact |
| 15 | `VERIFICATION_FAILED` | resulting schema did not match the baseline | do not continue |
| 16 | `CREDENTIAL_STORE_FAILED` | credential could not be protected | show the message |

`POSTGRES_NOT_FOUND` is distinguished from `ADMIN_AUTH_FAILED` and from `MIGRATION_FAILED`,
so the installer can tell "not installed" from "wrong password" from "schema problem".

**Two other code spaces exist above 16, and they are not the same list.** Each belongs to one
program; they never appear together.

| Owner | Codes |
|---|---|
| `app/installer_bridge.py` (what the installer calls) | 20 `POSTGRES_STOPPED`, 21 `NO_ADMIN_PASSWORD`, 22 `UNEXPECTED`, 23 `POSTGRES_WRONG_VERSION`, 24 `HANDOFF_FAILED` |
| `scripts\provision_postgres.ps1` | 20 `PYTHON_NOT_FOUND` — the developer's Python environment is missing |

They start at 20 deliberately, above the `Status` range, so that adding a new `Status` value
can never collide with one of them.

Detection is protocol-level: `probe_port` sends a PostgreSQL `SSLRequest` packet and requires
the one-byte `S`/`N` reply. A web server or any other program on 5432 is therefore reported
as `PORT_OCCUPIED_BY_OTHER`, not mistaken for a broken PostgreSQL.

---

## Password rotation

| Situation | Behaviour |
|---|---|
| Role missing | generate, create, store |
| Role exists, stored credential works | **retain** — nothing changes |
| Role exists, stored credential missing or wrong | rotate `ALTER ROLE ... PASSWORD`, store |
| `-Rotate` passed | always rotate |

Rotation never touches data. To rotate deliberately:

```powershell
.\scripts\provision_postgres.ps1 -Rotate
```

---

## Existing installations

| Case | Handling |
|---|---|
| **A. PostgreSQL absent** | exit 10; nothing changed |
| **B. Installed, database absent** | create role + database, migrate to head |
| **C. Database exists under the old `postgres` credential** | verify, create the role, transfer ownership of the database, schema and every table/view/sequence/enum, keep all data, store new credentials |
| **D. Already provisioned** | verify, `upgrade head`, no destructive change |
| **E. Port held by another service** | exit 11 with a clear message |
| **F. Non-default port** | `-Port N`; persisted with the credential |

Ownership transfer enumerates objects explicitly instead of using `REASSIGN OWNED BY`, which
also re-owns objects **outside** the target database — including other databases owned by the
same role. Sequences belonging to a `serial`/identity column are skipped: PostgreSQL refuses
to re-own them separately, and they follow their table anyway.

An adopted database that has the baseline schema but no `alembic_version` row is **stamped
first** (only after verification passes), then upgraded — otherwise the baseline migration
would fail on "relation already exists".

---

## Converting THIS development database (not done automatically)

The real `linkedin_auto_apply` is still owned by the `postgres` superuser, and the dedicated
role has deliberately **not** been created. Phase 2B only inspected it read-only.

When you want to convert it:

```powershell
# 1. Back up first. This is the only irreversible step.
pg_dump -U postgres -F c -f linkedin_auto_apply_before_role_change.dump linkedin_auto_apply

# 2. Confirm the schema still matches the baseline (read-only)
.venv\Scripts\python.exe -m app.schema_verification

# 3. Convert: creates ai_job_automation_app, transfers ownership, stores the credential,
#    runs upgrade head, verifies. Prompts for the postgres password.
.\scripts\provision_postgres.ps1

# 4. Confirm the app now connects as the dedicated role
.venv\Scripts\python.exe -m app.provisioning --status --json

# 5. Remove DATABASE_URL from .env so the secure store is used
#    (precedence step 1 otherwise keeps overriding step 2)

# 6. Start the application
.\start_app.ps1
```

Step 5 matters: while `DATABASE_URL` is present in `.env` it wins over the secure store, so
the app would keep connecting as `postgres` and the conversion would look like it had no
effect.

**Rollback** — restore the dump, or simply put `DATABASE_URL` back in `.env`: the old
superuser credential still works, since provisioning never revokes it.

---

## Failure recovery

| Symptom | Cause | Fix |
|---|---|---|
| "No database credential is configured" | no `DATABASE_URL`, no stored credential | run provisioning |
| "Windows could not read the stored credential" | different Windows account, restored profile, or another computer | re-run provisioning (rotates the password; data untouched) |
| Exit 10 | PostgreSQL not installed or its service is stopped | `Start-Service postgresql-x64-16`, or install PostgreSQL 16 by hand |
| Exit 11 | another program on 5432 | stop it, or use `-Port` |
| Exit 12 | wrong administrator password | re-run and retype |
| Exit 14/15 | migration or schema problem | see `docs/DATABASE_MIGRATIONS.md`; the database is not modified destructively |

Provisioning contains **no** `DROP`, `TRUNCATE` or `DELETE` — enforced by a test that parses
the module and inspects every executable SQL string. Re-running it can never destroy data.

---

## Upgrades and uninstall

**Application upgrades** keep everything. The credential lives in
`%LOCALAPPDATA%\AIJobAutomation\config\` and the database lives in PostgreSQL's own data
directory; neither is inside the install folder, so replacing the program files cannot
disturb them.

The supported upgrade is to run the **newer installer over the existing installation** —
users must **not** uninstall first. Setup asks the application whether it is already
provisioned (`--installer status`); when it is, provisioning is skipped entirely, the wizard
reports *"The existing database was kept"*, and the administrator password typed on the page
is never used. New schema changes arrive as new Alembic migrations. Preserved across an
upgrade: the database and all application history, uploaded resumes, LinkedIn and Dice
sessions, the DPAPI credential, and everything under `%LOCALAPPDATA%\AIJobAutomation`.

**Uninstall leaves the database and the credential in place**, because removing them would
destroy the user's entire application history. The uninstaller removes the program directory
and the shortcuts and nothing else — it does not touch `%LOCALAPPDATA%`, the database, the
role, or PostgreSQL itself, which other software may be using. Reinstalling later adopts the
existing database unchanged.

Deleting the data is a separate, explicit, manual act, in this order: the
`linkedin_auto_apply` database, the `ai_job_automation_app` role, then
`%LOCALAPPDATA%\AIJobAutomation\`. The exact commands are in
[WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md#9-optional-complete-cleanup).

---

## Development override

```dotenv
# .env
DATABASE_URL=postgresql+psycopg://postgres:yourpassword@localhost:5432/linkedin_auto_apply
```

This takes precedence over everything and needs no provisioning. Alembic uses the same value,
so migrations always target the same database the app opens.

To test the secure-store path in development, remove `DATABASE_URL` and run provisioning.

---

## Testing

```powershell
.venv\Scripts\python.exe -m unittest tests.test_secure_credentials tests.test_postgres_provisioning
```

Every database created by these tests is named `ai_job_migtest_*` and every role
`ai_job_testrole_*`; prefix guards in `tests/migration_db.py` raise `UnsafeDatabaseName` for
anything else, so the real database and the real role can never be created, altered or
dropped by a test. Credential tests redirect the store to a temporary directory, so the
developer's own stored credential is never read or overwritten.

---

## See also

- [WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md) — installing, upgrading and removing the
  product; the manual PostgreSQL 16 prerequisite; the masked password page.
- [WINDOWS_BUILD.md](WINDOWS_BUILD.md) — the packaged application this provisioning ships in.
- [DATABASE_MIGRATIONS.md](DATABASE_MIGRATIONS.md) — the migrations `upgrade head` applies.
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — what shipped in each version.
