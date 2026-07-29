# Rising Path Recruiters — Windows installation guide

Version 1.0.0

This is the complete, supported process for installing, upgrading, running and removing
Rising Path Recruiters on Windows.

**One thing is installed by hand: PostgreSQL 16.** The Rising Path Recruiters installer does
not download, bundle or install a database server. It checks that a suitable one is already
there, asks once for its administrator password, and then creates everything the application
needs — its own role, its own database, its schema and its stored credential — without any
further input.

That split is deliberate. An earlier version embedded the official PostgreSQL installer and
ran it unattended; on a clean machine it failed in a way that could not be diagnosed from the
logs it produced, and it made the setup file more than twice as large. Installing PostgreSQL
by hand takes about three minutes, is a step the person doing it can see and correct, and
leaves them in control of a database server that other software on the computer may also use.

The application package this installer wraps is documented in
[WINDOWS_BUILD.md](WINDOWS_BUILD.md); the provisioning design in
[POSTGRES_PROVISIONING.md](POSTGRES_PROVISIONING.md).

---

## Contents

1. [System requirements](#1-system-requirements)
2. [Before you start: install PostgreSQL 16](#2-before-you-start-install-postgresql-16)
3. [Verify the download](#3-verify-the-download)
4. [Fresh installation](#4-fresh-installation)
5. [Starting the application](#5-starting-the-application)
6. [Upgrading to a newer version](#6-upgrading-to-a-newer-version)
7. [The update model](#7-the-update-model)
8. [Uninstalling and keeping your data](#8-uninstalling-and-keeping-your-data)
9. [Optional: complete cleanup](#9-optional-complete-cleanup)
10. [Where your data and logs live](#10-where-your-data-and-logs-live)
11. [Common errors](#11-common-errors)
12. [PostgreSQL service troubleshooting](#12-postgresql-service-troubleshooting)
13. [Port 5432 conflicts](#13-port-5432-conflicts)
14. [For maintainers: building the installer](#14-for-maintainers-building-the-installer)

---

## 1. System requirements

| | |
|---|---|
| Operating system | Windows 10 (1809 or later) or Windows 11, **64-bit** |
| Processor | x64 (`x64compatible`; ARM64 under emulation is untested) |
| Disk space | ~1.0 GB for the application, plus PostgreSQL and your own data |
| Memory | 4 GB minimum; 8 GB recommended, because the application drives a real browser |
| Privileges | A **local administrator** account to run the installer |
| Database | **PostgreSQL 16** (or newer), installed first — see section 2 |
| Network | Internet access at run time, for the job portals |

You do **not** need Python, Node.js, npm, Playwright or a separate Chromium. All of them are
inside the setup file.

**Sign in as the account that will use the application before you run setup.** The database
credential is protected with Windows DPAPI in *current-user* scope, so it belongs to the
account that created it. If a different administrator approves the UAC prompt, setup detects
this and stops with an explanation rather than storing a credential you could not use.

---

## 2. Before you start: install PostgreSQL 16

1. Go to <https://www.postgresql.org/download/windows/> and follow the link to the
   EDB installer.
2. Download **PostgreSQL 16 for Windows x86-64**.
3. Run it as an administrator and accept the defaults, with two exceptions you must pay
   attention to:
   - **Port: leave it at `5432`.** Rising Path Recruiters looks there and nowhere else.
   - **Password for the `postgres` superuser: choose one and write it down.** The Rising Path
     Recruiters installer asks for this password once. Nobody can recover it for you — if it
     is lost, the only remedy is to reinstall PostgreSQL.
4. On the component page you may clear **Stack Builder**. Leave *PostgreSQL Server* and
   *Command Line Tools* selected — the command line tools include `pg_isready`, which setup
   uses to confirm the server is answering.
5. Let the installation finish.

### Confirm it worked

Open PowerShell and run:

```powershell
Get-Service -Name postgresql*
```

You should see a service such as `postgresql-x64-16` with status **Running**.

```powershell
& "$env:ProgramFiles\PostgreSQL\16\bin\pg_isready.exe" -h localhost -p 5432
```

You should see `localhost:5432 - accepting connections`.

If either of those fails, fix it before running the Rising Path Recruiters installer —
see [section 12](#12-postgresql-service-troubleshooting).

---

## 3. Verify the download

The setup file is not code-signed yet, so Windows SmartScreen will warn you the first time
you run it. Check the checksum before you do.

```powershell
Get-FileHash .\RisingPathRecruiters-Setup-1.0.0.exe -Algorithm SHA256
```

Expected for version 1.0.0:

```
44300484BDC432FFC600CE2983B5855F21F1C7760D288552D0AF5E9DF0E96D83
```

`SHA256.txt` in the release folder carries the same value. **If it does not match, do not run
the file.**

At the SmartScreen prompt choose **More info → Run anyway**. See
[section 14](#14-for-maintainers-building-the-installer) for why the file is unsigned.

---

## 4. Fresh installation

1. Right-click `RisingPathRecruiters-Setup-1.0.0.exe` and choose **Run as administrator**.

2. **Setup checks PostgreSQL before it installs anything.** It verifies, in order, that a
   PostgreSQL 16 (or newer) installation is registered, that its Windows service is running,
   and that `localhost:5432` is accepting connections. If any of those is not true it tells
   you exactly what to do and stops — **nothing is written to your computer**, so you can fix
   the problem and run setup again with nothing to undo.

3. Choose the installation folder. The default is
   `C:\Program Files\Rising Path Recruiters`.

4. **The PostgreSQL administrator password page.** Enter the password you chose in section 2.

   | Field | Value |
   |---|---|
   | User name | `postgres` (fixed) |
   | Server | `localhost` (fixed) |
   | Port | `5432` (fixed) |
   | Database | `postgres` (fixed) |

   The field is masked. The password is used once, to create the application's own
   low-privilege account, and is **never stored**.

5. Optionally tick **Create a desktop shortcut**, then click Install.

6. After the files are copied, setup completes the database automatically:

   - creates the role `ai_job_automation_app` with a **separate, randomly generated**
     password (not the one you typed);
   - creates the database `linkedin_auto_apply` owned by that role;
   - applies least-privilege permissions;
   - runs all Alembic migrations;
   - verifies the resulting schema;
   - stores **only** the `ai_job_automation_app` credential, protected with Windows DPAPI for
     your Windows account.

   If the password is not accepted, setup asks again — up to five times — so a typo is a
   correctable mistake. If you give up, or if provisioning fails, setup **rolls the
   installation back** and tells you which stage failed and where the log is. You are never
   left with a half-configured program.

7. Click Finish. The wizard reports *"The application database is ready."*

### What the administrator password is, and is not, used for

- It **is** used once, in memory, to connect as `postgres` and create the role and database.
- It is **not** written to disk in plain text, put on any command line, placed in an
  environment variable, printed in a log or shown in a message box. It crosses from the
  elevated wizard to the unelevated provisioning step as DPAPI **ciphertext** in a file that
  is access-restricted to your account and destroyed afterwards.
- It is **not** kept. The application never knows it and never needs it again.

---

## 5. Starting the application

Use the **Start menu → Rising Path Recruiters** shortcut, or the desktop shortcut if you
created one.

The application is a windowed program with no console. It starts a local web server bound to
`127.0.0.1` only — never to your network — and opens your default browser at
<http://127.0.0.1:8000> once it is ready.

- Starting it a second time brings the existing window forward instead of starting another
  copy.
- Closing the browser tab does not stop it; use the application's own exit, or run
  `"C:\Program Files\Rising Path Recruiters\RisingPathRecruiters.exe" --shutdown`.

### Diagnostic switches

| Command | Purpose |
|---|---|
| `RisingPathRecruiters.exe --check` | Report configuration, database and schema state, then exit |
| `RisingPathRecruiters.exe --shutdown` | Ask a running copy to stop cleanly |
| `RisingPathRecruiters.exe --no-browser` | Start without opening a browser |
| `RisingPathRecruiters.exe --selftest` | Run the packaged self-test |

Anything else on the command line is ignored and the application starts normally.

---

## 6. Upgrading to a newer version

**Do not uninstall first.** Installing over the top is the supported path and is what
preserves your data.

1. Close Rising Path Recruiters. (If you forget, setup asks it to close cleanly and waits.)
2. Verify the new file's SHA256 against the `SHA256.txt` shipped with it.
3. Run the new installer as an administrator.
4. Setup checks PostgreSQL exactly as it does for a fresh install.
5. The password page still appears. **On an upgrade the password is not used** — setup first
   asks the application whether the database is already provisioned, finds that it is, and
   reports *"The existing database was kept."* Enter your `postgres` password anyway; the
   page asks unconditionally.
6. Finish, then start the application.

### What an upgrade preserves

Everything. The installer replaces the program files in `C:\Program Files\Rising Path
Recruiters` and touches nothing else:

- the `linkedin_auto_apply` database and all application history;
- uploaded resumes and their analysis;
- LinkedIn and Dice sessions, so you do not sign in again;
- the DPAPI credential;
- everything under `%LOCALAPPDATA%\AIJobAutomation`;
- your PostgreSQL installation, its port, and its `postgres` password.

Future schema changes ship as **new Alembic migrations**, which the installer applies during
provisioning. Migrations are additive and backward compatible; application history is
immutable. See [DATABASE_MIGRATIONS.md](DATABASE_MIGRATIONS.md).

---

## 7. The update model

Rising Path Recruiters uses a **manual update model**. There is no automatic updater, no
background update check and no telemetry. Nothing contacts a server to look for a new
version, and nothing about your computer is reported anywhere.

When a new version is released:

1. You receive the new installer and its `SHA256.txt`.
2. You close the application.
3. You run the new installer over the existing installation.
4. You do **not** uninstall first.

That is the whole process. It is deliberate: an application that drives your LinkedIn and
Dice accounts should not change itself underneath you.

---

## 8. Uninstalling and keeping your data

**Settings → Apps → Rising Path Recruiters → Uninstall**, or the Start-menu shortcut.

Uninstall removes only:

- the program directory `C:\Program Files\Rising Path Recruiters`;
- the Start-menu and desktop shortcuts;
- the Add/Remove Programs entry.

Uninstall **does not** touch:

- `%LOCALAPPDATA%\AIJobAutomation` — resumes, sessions, logs, diagnostics;
- the `linkedin_auto_apply` database or your application history;
- the `ai_job_automation_app` role;
- the stored DPAPI credential;
- PostgreSQL itself, which other software may depend on.

Reinstalling later picks all of it up again: provisioning finds the existing database and
keeps it as it is.

---

## 9. Optional: complete cleanup

Only if you want everything gone. **This is irreversible — back up first if your application
history matters.**

```powershell
# 1. Uninstall the application first (section 8).

# 2. Application data: resumes, portal sessions, logs, diagnostics, the DPAPI credential.
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\AIJobAutomation"

# 3. The database and its role. Irreversible.
#    Run as a PostgreSQL administrator; you will be prompted for the postgres password.
psql -U postgres -c "DROP DATABASE linkedin_auto_apply;"
psql -U postgres -c "DROP ROLE ai_job_automation_app;"

# 4. PostgreSQL itself, ONLY if nothing else on this computer uses it:
#    Settings > Apps > PostgreSQL 16 > Uninstall
```

Step 3 is the only step that destroys data you cannot recreate.

---

## 10. Where your data and logs live

Everything is local to this computer and to your Windows account. Nothing is synchronised,
uploaded or shared.

| What | Where |
|---|---|
| Program files | `C:\Program Files\Rising Path Recruiters` |
| Application data root | `%LOCALAPPDATA%\AIJobAutomation` |
| Support log | `%LOCALAPPDATA%\AIJobAutomation\logs\launcher.log` |
| Database credential (DPAPI) | `%LOCALAPPDATA%\AIJobAutomation\config\database_credential.dat` |
| Resumes and uploads | `%LOCALAPPDATA%\AIJobAutomation\storage` |
| LinkedIn / Dice sessions | `%LOCALAPPDATA%\AIJobAutomation\storage\browser_sessions` |
| Screenshots, evidence, debug HTML | `%LOCALAPPDATA%\AIJobAutomation\outputs` |
| Runtime state (while running) | `%LOCALAPPDATA%\AIJobAutomation\work\runtime.json` |
| Setup result (non-secret) | `%LOCALAPPDATA%\AIJobAutomation\work\installer-result.json` |
| Database | PostgreSQL database `linkedin_auto_apply` |
| Setup log | `%TEMP%\Setup Log <date> #NNN.txt` |

`%LOCALAPPDATA%` is used deliberately rather than `%APPDATA%`: this data is specific to one
computer and must not follow a roaming profile onto another.

### The credential

The application never holds the `postgres` password. It connects as
`ai_job_automation_app`, a role that can log in and owns exactly one database — it is not a
superuser and cannot create roles or other databases. That password is random, generated
during installation, and stored only inside a DPAPI blob tied to your Windows account. No
other account on the computer can read it, and it is useless if copied elsewhere.

---

## 11. Common errors

### "PostgreSQL is not installed on this computer."

Setup found no PostgreSQL in the registry. Install PostgreSQL 16 (section 2) and run setup
again. Nothing was installed.

### "This computer has PostgreSQL *n*, but Rising Path Recruiters requires PostgreSQL 16 or newer."

An older major version is present. Install PostgreSQL 16 alongside or in place of it. Note
that setup reads the **registry**, so a server installed by some other means (Docker, a
zip-file copy, WSL) is not detected even if it is running.

### "PostgreSQL 16 is installed on this computer, but its Windows service is not running."

Start it — see [section 12](#12-postgresql-service-troubleshooting) — then run setup again.

### "PostgreSQL is running, but nothing is answering on localhost:5432."

The server is on another port, or something is blocking the loopback connection. See
[section 13](#13-port-5432-conflicts).

### "PostgreSQL did not accept the administrator password."

Wrong password for the `postgres` account. Setup lets you retry. It is the password you set
during the PostgreSQL installation, not your Windows password. If it is genuinely lost, the
practical remedy is to reinstall PostgreSQL — but if you have an existing
`linkedin_auto_apply` database you want to keep, back it up first with `pg_dump`.

### "The password could not be passed securely to the database step."

Setup was approved with a **different** administrator account from the one signed in. DPAPI
keys belong to an account, so the provisioning step could not read the password back. Sign in
as the account that will use the application and run setup again. Setup stops rather than
storing a credential you could never decrypt.

### "Windows could not protect the password on this computer."

DPAPI is unavailable for this account. This is unusual and normally means a damaged profile
or a very restrictive policy. Try a different administrator account on the same computer.

### The application will not start, or the browser shows nothing

Read `%LOCALAPPDATA%\AIJobAutomation\logs\launcher.log`. It records startup, the port,
readiness and any error — including anything raised by the web server. There is no console
window; the log is the place to look.

Then run:

```powershell
& "C:\Program Files\Rising Path Recruiters\RisingPathRecruiters.exe" --check
```

Exit code 0 means configuration, database and schema are all in order.

### The application says the database is not set up

The DPAPI credential is missing or belongs to another account — most often because setup was
run by a different administrator. Run the installer again while signed in as the account that
will use the application.

---

## 12. PostgreSQL service troubleshooting

```powershell
# Is it there, and what is its state?
Get-Service -Name postgresql*

# Start it (needs an elevated PowerShell).
Start-Service -Name postgresql-x64-16

# Make sure it starts with Windows.
Set-Service -Name postgresql-x64-16 -StartupType Automatic

# Is the server accepting connections?
& "$env:ProgramFiles\PostgreSQL\16\bin\pg_isready.exe" -h localhost -p 5432
```

If the service refuses to start, its own log says why:

```
C:\Program Files\PostgreSQL\16\data\log\
```

The usual causes are a port already in use (section 13), a data directory whose permissions
have been changed, or an unclean shutdown. None of them is caused or fixed by the Rising Path
Recruiters installer, which never starts, stops, reconfigures or removes PostgreSQL.

---

## 13. Port 5432 conflicts

Rising Path Recruiters requires PostgreSQL on **5432**. Find out what is there:

```powershell
netstat -ano -p TCP | Select-String "LISTENING" | Select-String ":5432"
# Take the PID from the last column:
Get-Process -Id <PID>
```

**If it is another PostgreSQL** (a second installation, or an older major version), decide
which one you want the application to use and give that one port 5432. Change the other in
its `postgresql.conf` and restart it.

**If it is something else entirely**, stop or reconfigure that program. Setup reports this as
*"Port 5432 is being used by a program that is not PostgreSQL"* and refuses to continue,
rather than guessing.

**If nothing is listening at all**, the service is stopped — section 12.

To change your PostgreSQL server's port:

```
C:\Program Files\PostgreSQL\16\data\postgresql.conf   ->   port = 5432
```

then restart the service. The application does not support any other port.

---

## 14. For maintainers: building the installer

Needed on the build machine only:

- Python 3.12 with the project virtual environment at `.venv`
- Node.js and npm, for the React build
- **Inno Setup 6** — <https://jrsoftware.org/isdl.php>; the build stops with a clear message
  if it is absent and installs nothing itself

```powershell
# 1. Package the application (PyInstaller onedir, ~920 MB).
powershell -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1

# 2. Compile the setup program (~5 minutes).
powershell -ExecutionPolicy Bypass -File .\scripts\build_installer.ps1
```

The result is `release\RisingPathRecruiters-Setup-<version>.exe`, together with a build log
and the list of every file embedded in it.

**No PostgreSQL prerequisite is downloaded, verified or embedded.** The build fails outright
if a `postgresql*.exe` ever appears in the embedded file list, and every embedded file must
come from the packaged payload — there are no exceptions to that rule.

Version, name, publisher, copyright and the installer filename all come from one place,
`app/version.py`, and are passed to Inno Setup as `/D` definitions. Change the version there
and nowhere else.

### Verifying a build

```powershell
# The packaged application, in a clean-room child process (30 checks).
powershell -ExecutionPolicy Bypass -File .\scripts\test_packaged_build.ps1 -DatabaseUrl "<url>"

# The installer and runtime, on a separate clean computer, one stage at a time.
powershell -ExecutionPolicy Bypass -File .\scripts\verify_clean_machine.ps1 -Stage preflight -Installer .\RisingPathRecruiters-Setup-1.0.0.exe
powershell -ExecutionPolicy Bypass -File .\scripts\verify_clean_machine.ps1 -Stage install-verify
powershell -ExecutionPolicy Bypass -File .\scripts\verify_clean_machine.ps1 -Stage runtime
powershell -ExecutionPolicy Bypass -File .\scripts\verify_clean_machine.ps1 -Stage postuninstall
```

The verifier's `preflight` stage expects PostgreSQL 16 to be **already installed** — that is
the supported starting state, not a contaminated machine.

### Code signing

The setup program is not signed, so SmartScreen warns on first run until it builds
reputation. Signing needs an OV or EV code-signing certificate and a change to the build
script; it is deliberately not part of this release.

### Known limitations

- **PostgreSQL 16 must be installed manually first.** This is the supported design, not an
  oversight.
- The setup program is not code-signed.
- There is no automatic updater; see [section 7](#7-the-update-model).
- Only `localhost:5432` is supported. A remote or non-default-port server is not.
- Windows x64 only.
