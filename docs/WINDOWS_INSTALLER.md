# Windows installer

How the setup program is built, what it does on a user's computer, and what it deliberately
does not do.

The person receiving it gets **one file**. They do not install Python, Node.js, npm, Vite,
Playwright or Chromium; they do not download a browser; they do not install or configure
PostgreSQL, open pgAdmin, create a role or a database, edit `.env`, run Alembic, run any
PowerShell command, or copy anything by hand.

This document covers the installer. The application package it wraps is documented in
[WINDOWS_BUILD.md](WINDOWS_BUILD.md).

---

## Build prerequisites

| Requirement | Why | Check |
|---|---|---|
| Everything in [WINDOWS_BUILD.md](WINDOWS_BUILD.md) | The installer wraps `dist\RisingPathRecruiters`, which must exist and have passed its secrets scan. | `dist\RisingPathRecruiters\RisingPathRecruiters.exe --selftest` |
| **Inno Setup 6** | Compiles the setup program. | `"%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" /?` |
| The pinned PostgreSQL installer | Embedded so a machine with no database server can still be set up offline. | `scripts\prepare_installer_prerequisites.ps1 -Offline` |
| ~3 GB free disk | Payload + prerequisite + compression working set. | — |

### Inno Setup is not installed automatically

`scripts\build_installer.ps1` looks for it and **stops with exit code 3** if it is absent —
it never downloads or installs a build tool on your behalf. Install it from the official
site, <https://jrsoftware.org/isdl.php>, then run the build again. Everything else (the
`.iss` source, the payload, the verified prerequisite, the whole test suite) works and is
validated without it.

## PostgreSQL prerequisite preparation

```powershell
# First time on a build machine (downloads ~334 MB from the official source):
powershell -ExecutionPolicy Bypass -File scripts\prepare_installer_prerequisites.ps1

# Afterwards, and in CI: verify what is already there, download nothing.
powershell -ExecutionPolicy Bypass -File scripts\prepare_installer_prerequisites.ps1 -Offline
```

The version, URL, expected SHA-256 and expected publisher all come from
[`installer/prerequisites.json`](../installer/prerequisites.json). Two independent checks
must pass:

* **SHA-256** — proves the file is byte-identical to the one that was reviewed when the pin
  was established;
* **Authenticode** — proves EDB published it, regardless of how it arrived. This is the
  stronger check: a checksum only ever confirms a decision someone already made.

The script **never runs** the installer it downloads.

| Exit | Meaning |
|---|---|
| 0 | verified and present |
| 2 | manifest missing, or no checksum pinned |
| 3 | download failed (or `-Offline` with no local copy) |
| 4 | **checksum mismatch** — not the file that was pinned |
| 5 | signature missing, invalid, or from another publisher |

### Changing the pinned version

Update `version`, `major`, `filename` and `url` in the manifest, set `sha256` to `null`, then:

```powershell
scripts\prepare_installer_prerequisites.ps1 -Pin
```

It prints the checksum and signer for you to paste in. It deliberately does **not** write
them itself — a script that updates the value it verifies against verifies nothing.

## Build command

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

| Flag | Effect |
|---|---|
| `-SkipPostgres` | Build without embedding the prerequisite. Far smaller; cannot set up a machine that has no PostgreSQL. |
| `-IsccPath <path>` | Use a specific `ISCC.exe`. |

| Exit | Meaning |
|---|---|
| 0 | built and verified |
| 2 | payload or version source missing |
| 3 | **Inno Setup is not installed** — nothing was built |
| 4 | the payload failed the secrets scan |
| 5 | the pinned prerequisite failed verification |
| 6 | ISCC failed |
| 7 | the produced installer failed verification |

The secrets scan is re-run here rather than trusted from the packaging step: this script may
be pointed at a payload built days ago, and the installer publishes whatever that payload
contains.

## Installer output

```
release\RisingPathRecruiters-Setup-<version>.exe
```

Deterministic: the same version always produces the same filename. Version, publisher, AppId
and product name all come from [`app/version.py`](../app/version.py) — the one place any of
them is written down.

Two companion files are written beside it, for auditing rather than distribution:

| File | What it is |
|---|---|
| `…-Setup-<version>.build.log` | ISCC's full compile output |
| `…-Setup-<version>.files.txt` | every file embedded, extracted from that log |

The file list is what the build checks to prove nothing outside the packaged payload got in.
Measured for 1.0.0:

| | |
|---|---|
| Application payload (uncompressed) | 922.1 MB |
| PostgreSQL prerequisite | 333.8 MB |
| **Total embedded** | **1,255.9 MB** |
| **Installer (compressed)** | **625.9 MB** (49.8 %) |
| Files embedded | 3,293 (3,292 payload + 1 prerequisite) |
| Compile time | ~4 minutes |

Disk needed during installation: roughly **2.2 GB** — the 626 MB installer, plus ~922 MB
expanded into Program Files, plus ~334 MB for the PostgreSQL installer unpacked to the
temporary directory (only on a machine that has no PostgreSQL), plus whatever PostgreSQL
itself then uses. On a machine that already has a compatible server, budget ~1.6 GB.

Installed layout:

```
%ProgramFiles%\Rising Path Recruiters\
├── RisingPathRecruiters.exe
└── _internal\...                 (frozen runtime, React build, Alembic, Chromium)
```

Nothing is ever written there at runtime.

## New-machine installation flow

1. **Close-check** — if the application is already running, the user is asked to close it.
   Nothing is force-killed, and no unrelated `python.exe`, `chrome.exe` or `postgres.exe`
   process is touched.
2. **Files** — the packaged application is copied to Program Files.
3. **PostgreSQL detection** (read-only, as the original user) decides among five outcomes:

   | Situation | What happens |
   |---|---|
   | Compatible server running | **Used as it is.** Not reinstalled, not upgraded. The 334 MB prerequisite is never even extracted. |
   | Compatible server on a non-default port | Used on that port; the port is recorded in the stored credential. |
   | Installed but stopped | Reported with instructions to start the service. |
   | Port 5432 held by something else | Reported as a conflict. **PostgreSQL is not installed over it.** |
   | Nothing installed | The pinned installer runs (below). |

4. **PostgreSQL installation**, only in the last case. A strong temporary administrator
   password is generated, written to an ACL-restricted option file, and passed to the
   official installer via `--optionfile` — never on a command line. pgAdmin and Stack Builder
   are disabled; the command-line tools are kept for recovery.
5. **Provisioning** (as the original user) — creates or adopts `ai_job_automation_app` and
   `linkedin_auto_apply`, runs `alembic upgrade head`, verifies the schema, and stores the
   credential with DPAPI. The temporary option file is destroyed here, whether provisioning
   succeeded or failed.
6. **Shortcuts** — Start menu always; desktop only if the user ticked the box.

### The elevation boundary

This is the part most worth understanding.

The installer runs **elevated**, because writing to Program Files and installing a Windows
service both require it. But the database credential is protected with DPAPI in
**current-user** scope, and "current user" for an elevated installer is whichever account
approved the UAC prompt — frequently *not* the person who will use the application. A
credential written there would be undecryptable by the real user, and the application would
ask to be set up again on every start.

The split follows what each step *genuinely* needs:

| Step | Context | Why |
|---|---|---|
| Detection | original user | reads only |
| Prepare option file | original user | writes into that user's own data directory |
| Install PostgreSQL | **elevated** | installing a service needs administrator rights |
| Provision + store credential | original user | DPAPI must belong to the account that runs the app |

The key observation: **provisioning needs no Windows elevation at all.** Creating a role and
a database requires the *PostgreSQL* administrator password, which has nothing to do with
UAC. That lets the credential step run through `ExecAsOriginalUser`, and removes any need to
hand a secret from an elevated process back down to an unelevated one.

DPAPI is **not** weakened to machine scope to make installation simpler. Machine scope would
let every account on the computer decrypt the credential, destroying the per-user isolation
established in Phase 1A.

### Secret handling

* The PostgreSQL administrator password is **generated**, never chosen by a person, and never
  stored permanently.
* It never appears in a command line. On Windows any user can read the full command line of
  any process; the option file and the process environment cannot be read by other standard
  users.
* The option file is created empty, ACL-restricted **first**, and only then filled — writing
  the secret and tightening permissions afterwards would leave a readable window.
* It is overwritten and deleted immediately after provisioning, on both the success and the
  failure path.
* After provisioning, the application uses **only** `ai_job_automation_app`, a role with
  `NOSUPERUSER NOCREATEDB NOCREATEROLE`.
* No installer message, log line or dialog contains a password or a connection URL.

## Existing-machine upgrade flow

Installing a newer version over an older one:

| Behaviour | Guaranteed by |
|---|---|
| Recognised as the same application | a stable `AppId` that never changes |
| Application binaries replaced | `[Files] ... ignoreversion` |
| `%LOCALAPPDATA%\AIJobAutomation` untouched | the installer writes only to `{app}` |
| PostgreSQL untouched | detection reports "compatible" and the prerequisite is never extracted |
| Role and database not recreated | the `status` action short-circuits provisioning |
| Existing credential kept | provisioning retains a stored password that still authenticates |
| Schema brought up to date | `alembic upgrade head` runs on next start regardless |
| Resumes, jobs, history, sessions preserved | none of them live in the program directory |
| No duplicate instance | the running copy must be closed first |

### Upgrading a development machine

A developer's computer is the awkward case: PostgreSQL exists, `linkedin_auto_apply` exists,
it is already stamped at `ed2a513cd149`, the application role may not exist, and a local
`.env` may still carry a `DATABASE_URL`.

* The installer **never reads or bundles the source checkout's `.env`.** An installed
  application resolves its credential from the secure store only.
* Provisioning **adopts** the existing database rather than recreating it: it verifies the
  schema matches the baseline before stamping, transfers ownership of the objects in `public`
  to the application role, and preserves every row.
* Ownership transfer enumerates objects explicitly instead of using `REASSIGN OWNED BY`,
  which would also re-own objects in *other* databases.
* `postgres` access is **not revoked**. The conversion adds a role; it does not take one away.
* There is no `DROP` anywhere in the provisioning or installation path.

**Rollback.** Because nothing is dropped and `postgres` retains access, reverting is:
```sql
ALTER DATABASE linkedin_auto_apply OWNER TO postgres;
```
plus deleting `%LOCALAPPDATA%\AIJobAutomation\config\database_credential.dat`. The data is
untouched throughout.

## What uninstall does

**Removes:** the Program Files directory, the shortcuts, and the Add/Remove Programs entry.

**Preserves — always, silently, by design:**

* the PostgreSQL installation (other software may depend on it)
* the `linkedin_auto_apply` database and every row in it
* the `ai_job_automation_app` role
* all of `%LOCALAPPDATA%\AIJobAutomation`: resumes, uploads, job and application history,
  LinkedIn/Dice sessions, browser profiles, screenshots, diagnostics, logs
* the stored DPAPI credential

There is no `DROP DATABASE`, `DROP ROLE`, `dropdb` or `dropuser` anywhere in the installer,
and a test asserts their absence. Uninstalling an application is not consent to destroy the
data it produced.

### Advanced: removing everything

Deliberately manual, deliberately not in the uninstaller:

```powershell
# 1. Application data, credential, sessions, resumes, diagnostics.
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\AIJobAutomation"

# 2. The database and its role. Irreversible. Back up first if you may want the history.
#    Run as a PostgreSQL administrator:
#      psql -U postgres -c "DROP DATABASE linkedin_auto_apply;"
#      psql -U postgres -c "DROP ROLE ai_job_automation_app;"

# 3. PostgreSQL itself, only if nothing else on the computer uses it:
#    Settings > Apps > PostgreSQL <version> > Uninstall
```

## PostgreSQL failure recovery

| Symptom | Cause | Fix |
|---|---|---|
| "Port 5432 is being used by a program that is not PostgreSQL" | Another service holds the port. | Stop it, or install PostgreSQL on another port and re-run setup. Detection uses a protocol-level probe, so this is a fact, not a guess. |
| "PostgreSQL is installed but is not running" | The service is stopped. | Start `postgresql-x64-NN` in Services, then re-run setup. |
| "The PostgreSQL administrator password was not accepted" | An existing server whose password we do not have. | The application is installed; start it and complete setup, or re-run with the right password. |
| "The database credential could not be stored securely" | DPAPI failed for that account. | Start the application and finish setup as the user who will run it. A credential from another account or computer can never be decrypted — that is the design. |
| "The database could not be brought up to date" | A migration failed. | **No data was changed.** See `%LOCALAPPDATA%\AIJobAutomation\logs\launcher.log`. |
| PostgreSQL installation itself failed | Policy, antivirus, or disk. | Install PostgreSQL yourself, then start the application to finish setup. |

Every one of these leaves the application **installed and startable**; the database step can
always be completed later.

## Clean-VM test checklist

On a Windows 10/11 x64 VM with no Python, Node.js or PostgreSQL:

**Install**
- [ ] Only the setup `.exe` is copied to the VM
- [ ] SmartScreen warns (expected — not signed); "More info" → "Run anyway"
- [ ] UAC prompts once
- [ ] The directory page offers `C:\Program Files\Rising Path Recruiters`
- [ ] The desktop-shortcut box is **unticked** by default
- [ ] PostgreSQL installs without a single prompt
- [ ] No password appears in any dialog, log or window title
- [ ] Installation finishes without an error dialog

**Verify**
- [ ] Start-menu shortcut launches the app; browser opens **after** the interface answers
- [ ] `%LOCALAPPDATA%\AIJobAutomation\config\database_credential.dat` exists
- [ ] It belongs to the *logged-in* user, not an admin account
- [ ] `%APPDATA%` (Roaming) has no `AIJobAutomation` folder
- [ ] `Get-Service postgresql*` is Running
- [ ] `psql -U ai_job_automation_app -d linkedin_auto_apply -c "\dt"` lists the tables
- [ ] `%LOCALAPPDATA%\AIJobAutomation\work\postgres-setup.options` **does not exist**
- [ ] Nothing was written into Program Files after installation

**Upgrade**
- [ ] Bump `VERSION` in `app/version.py`, rebuild, run the new installer
- [ ] It replaces rather than installing a second copy
- [ ] PostgreSQL is not reinstalled
- [ ] Resumes, jobs and application history survive
- [ ] The LinkedIn/Dice session is still signed in

**Uninstall**
- [ ] Program Files directory and shortcuts are gone
- [ ] `%LOCALAPPDATA%\AIJobAutomation` is **still there**
- [ ] The database, its rows and the role are **still there**
- [ ] PostgreSQL is **still installed**

## Testing without touching this computer

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test_installer.ps1
.venv\Scripts\python.exe -m unittest tests.test_installer
```

Both are non-mutating. They never install PostgreSQL, never run the prerequisite installer,
never touch the real database and never overwrite the stored credential. Destructive paths
are covered with mocks and disposable databases.

## Code signing and SmartScreen

The installer is **not signed**. On a computer that has not seen it before, SmartScreen shows
"Windows protected your PC" and the user must choose "More info" → "Run anyway".

This is expected at this stage and is not a defect. Signing needs an EV or OV code-signing
certificate and a signing step for both `RisingPathRecruiters.exe` and the setup program;
it is a separate phase.

## Known limitations

* **Not code-signed** (above).
* **No auto-update.** A new version is a new installer run.
* **Not yet run on a clean VM.** Every check in this document has been validated by tests and
  by static analysis; the end-to-end install has not been exercised on a machine without a
  development environment.
* **The `[Code]` section compiles but has never been *executed*.** ISCC 6.7.3 parses it and
  resolves every function it calls (`ExecAsOriginalUser`, `ExtractTemporaryFile`, …), and
  `tests/test_installer.py` recompiles it on every run against a stub payload. What no test
  covers is the Pascal actually *running*: that needs a real installation on a clean machine.
* **The PostgreSQL unattended options are documented, not empirically confirmed.** They are
  EDB's published parameters; running the installer (even `--help`) was out of scope.
  `app.postgres_install.help_command()` exists for that check when it is authorised.
* **`ExecAsOriginalUser` behaviour when the installer is launched by an administrator
  account that is *also* the interactive user** collapses to the same account — correct, but
  it means the elevation split is untested for the separate-admin case.
* **One PostgreSQL major version is pinned** (16). A machine with a supported older major
  (≥13) is used as-is; nothing upgrades it.
* **Per-user credential.** Each Windows account that runs the application provisions its own.
* **626 MB installer.** Dominated by Chromium and the embedded PostgreSQL prerequisite. The
  largest safe reduction available is `pyarrow` (78 MB in the payload), which nothing in the
  application imports — it arrives transitively. See `docs/WINDOWS_BUILD.md`.
* **Inno Setup's compiler reports "Non-commercial use only"** on this machine. That is a
  property of the Inno Setup installation, not of this script, but it must be resolved before
  the installer is distributed commercially.
* **x64 only.** No 32-bit or ARM64 build.
