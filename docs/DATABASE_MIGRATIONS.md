# Database migrations (Alembic)

The application uses PostgreSQL with SQLAlchemy models. Schema changes are managed by
Alembic, in `alembic/versions/`.

**Current baseline revision: `ed2a513cd149`** — *Baseline: the complete current schema.*

The baseline represents exactly the schema `app/database.py:init_db()` has always produced,
so a freshly migrated database and an existing installation are structurally identical. That
equivalence is enforced by a test (`BaselineParityTests.test_migrated_schema_equals_init_db_schema`),
which builds one database each way and compares every table, column, index, constraint,
foreign key and enum value.

## Where the database URL comes from

`alembic.ini` contains **no** `sqlalchemy.url` and no password. `alembic/env.py` resolves the
URL at runtime, in this order:

1. `ALEMBIC_DATABASE_URL` — an explicit override (used by the migration tests)
2. a URL passed programmatically via `app.schema_verification.alembic_config(url)`
3. `app.config.get_settings().database_url` — the application's own setting (`DATABASE_URL`)

Never write a URL into `alembic.ini`; it is committed to source control.

---

## Fresh installation

An empty PostgreSQL database, no existing data.

```powershell
# 1. Create the database (once)
psql -U postgres -c "CREATE DATABASE linkedin_auto_apply"

# 2. Build the schema
.venv\Scripts\python.exe -m alembic upgrade head

# 3. Confirm
.venv\Scripts\python.exe -m app.schema_verification

# 4. Start the application
.\start_app.ps1
```

`upgrade head` creates all 12 tables, the `applicationstatus` enum, every index and
constraint, and inserts the `portal_runtime_state` singleton row.

---

## Existing installation (adoption)

A database that already contains real resumes, jobs and application history. It must be
**adopted**, not rebuilt: `stamp` records that it already sits at the baseline.

```powershell
# 1. BACK UP FIRST — this is the only step that cannot be undone by re-running it.
pg_dump -U postgres -F c -f linkedin_auto_apply_backup.dump linkedin_auto_apply

# 2. Verify the live schema really matches the baseline (read-only, changes nothing)
.venv\Scripts\python.exe -m app.schema_verification

# 3. Only if step 2 printed "schema MATCHES the baseline": stamp it
.venv\Scripts\python.exe -m app.schema_verification --stamp

# 4. Apply anything newer than the baseline (a no-op today)
.venv\Scripts\python.exe -m alembic upgrade head

# 5. Start the application
.\start_app.ps1
```

Steps 2 and 3 can be combined: `python -m app.schema_verification --stamp --upgrade`.

### Stamping is never blind

`stamp_baseline()` runs the verification first and raises `SchemaMismatch` if anything is
missing, so a database that does not match cannot be stamped by accident. Stamping a
mismatched database is the one genuinely dangerous mistake in Alembic adoption: every later
migration would then run against a schema it was not written for.

If verification fails, it prints exactly what is missing. Do not stamp. Let the application
start once (`init_db()` still repairs older schemas — see below), then verify again.

### What is verified

| Checked | Not checked |
|---|---|
| all 12 required tables | extra tables/columns/indexes (reported as notes) |
| all 8 `applicationstatus` enum values | column ordering |
| 80+ critical columns | `server_default` values (see below) |
| 24 key indexes | comments, storage parameters, statistics |
| 4 unique constraints | row data |
| 12 foreign keys by shape + `fk_job_opportunities_search_batch` by name | |

**Why `server_default` is not compared.** The ORM declares Python-side defaults, so
`create_all` never emitted DDL defaults. But `init_db()`'s `ALTER TABLE ... ADD COLUMN ...
DEFAULT ...` statements *did* attach real DDL defaults to columns added later. A database's
defaults therefore depend on how old it is, while runtime behaviour is identical either way.
Comparing them would reject perfectly healthy databases.

---

## Writing a new migration

```powershell
.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe the change"
```

**Always review the generated file before committing it.** Autogenerate was wrong in three
ways when the baseline was produced, and the same traps apply to every future revision:

1. **Enums.** It emits an inline `sa.Enum(...)` per column, so the second one fails with
   *type already exists*. Create the type once and reference it with `create_type=False`.
   To add a value to an existing enum, use `ALTER TYPE ... ADD VALUE`, and be aware that
   PostgreSQL cannot use a newly added enum value in the same transaction that added it —
   put the `ADD VALUE` in its own migration, separate from any migration that writes rows
   using it.
2. **Raw SQL objects.** Anything created by `init_db()`'s raw statements is invisible to
   autogenerate's model comparison. Check the list at the end of the baseline's `upgrade()`.
3. **Server defaults.** Autogenerate will not add them, and mostly should not. Add one
   explicitly only when the database genuinely needs it (e.g. a NOT NULL column added to a
   table that already has rows).

Also confirm JSONB columns render as `postgresql.JSONB(...)` and not `sa.JSON`.

---

## Current state of `init_db()` (Phase 2A)

`init_db()` **still runs at application startup** and is unchanged. Alembic is purely
additive for now: the two produce the same schema, and both are idempotent, so running them
together is safe (proved by `IdempotencyTests.test_init_db_still_works_on_a_migrated_database`).

`init_db()` currently performs:

- `Base.metadata.create_all()`
- ~110 idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`,
  `CREATE TABLE IF NOT EXISTS` and `ALTER TYPE ... ADD VALUE IF NOT EXISTS` statements
- two idempotent data backfills (`backfill_resume_fingerprints`,
  `backfill_dry_run_ready_records`) — these are **data**, not schema, and will remain

**Removal criteria.** The raw schema statements may be removed only after all of:

1. a fresh database created solely by `alembic upgrade head` runs the application end to end;
2. an existing installation has been verified, stamped, upgraded and run end to end;
3. both flows have been exercised on a machine other than the development machine;
4. at least one real migration has been authored and applied on top of the baseline.

Until then, deleting them risks leaving an older database unrepaired. The data backfills stay
regardless.

---

## Migration testing

Tests live in `tests/test_alembic_migrations.py` and use disposable databases created by
`tests/migration_db.py`. Every such database is named `ai_job_migtest_<random>`, and
`migration_db._guard()` refuses to create or drop anything without that prefix — so the tests
cannot touch `linkedin_auto_apply` even by mistake. Each database is dropped when its test
finishes.

```powershell
.venv\Scripts\python.exe -m unittest tests.test_alembic_migrations
```

Tests are skipped automatically when PostgreSQL is unreachable.
