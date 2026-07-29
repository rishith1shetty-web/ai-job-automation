# Rising Path Recruiters — release notes

## 1.0.0

First general release. A single Windows setup file installs the complete application: no
Python, Node.js, npm, Playwright or Chromium download, and no manual role, database, `.env`
or Alembic step.

**Installer SHA256**

```
44300484BDC432FFC600CE2983B5855F21F1C7760D288552D0AF5E9DF0E96D83
```

### What it is

- **A Windows desktop application.** A windowed (GUI subsystem) program with no console
  window. It starts a local web server bound to `127.0.0.1` only and opens your browser to
  it; nothing is exposed to the network.
- **Local-only, per-computer data.** Everything lives on the machine it was installed on,
  under `%LOCALAPPDATA%\AIJobAutomation` and in a local PostgreSQL database. Nothing is
  synchronised, uploaded, shared or reported anywhere. There is no telemetry.

### Features

- **LinkedIn Easy Apply and Dice**, implemented as independent portal adapters over a shared
  `BasePortal` interface. A change to one cannot affect the other.
- **Resume upload and analysis.** The analysed resume is the source of truth; uploaded
  resumes are never rewritten or modified.
- **Job search, ranking and matching** against the currently analysed resume.
- **Dry run and live run.** Dry run fills every field, uploads the resume, answers screening
  questions and stops at submit-ready without ever clicking Submit. Live run submits only
  after every validation has passed and a positive confirmation has been detected — an
  application is never reported as submitted unless it was.
- **Application history**, immutable, with per-step logging, screenshots and evidence paths.
- **Packaged React frontend and bundled Chromium.** The interface and the browser the
  automation drives are both inside the installation; nothing is downloaded at first run.

### Database and security

- **PostgreSQL 16 or newer is required and is installed by you, before setup runs.** See the
  known limitation below.
- The installer verifies PostgreSQL **before installing anything**: correct major version, a
  Windows service that exists and is running, and `localhost:5432` accepting connections. If
  any check fails it stops and explains what to do, leaving your computer untouched.
- It then asks once, on a masked page, for the `postgres` administrator password and does the
  rest automatically: creates the least-privilege role `ai_job_automation_app`, creates the
  database `linkedin_auto_apply` owned by it, applies permissions, runs all Alembic
  migrations and verifies the resulting schema.
- **The administrator password is never stored.** It is used once, in memory. It never
  appears in a command line, an environment variable, a log, a message box, PowerShell
  history or a plain-text file — it crosses the elevation boundary as DPAPI ciphertext in an
  access-restricted file that is destroyed afterwards.
- **Only the application's own credential is kept**, protected with Windows DPAPI in
  current-user scope. It is readable by your Windows account and no other, and useless if
  copied to another computer.
- If provisioning fails, or the password is wrong and you cancel, setup **rolls the
  installation back** rather than leaving a half-configured program behind.

### Known limitations

- **PostgreSQL 16 must be installed manually before running the installer.** This is the
  supported design. The installer does not bundle, download or install a database server;
  it verifies the one you installed and provisions inside it. Keep the default port 5432 and
  remember the `postgres` password you choose — the installer asks for it once.
- **Updates are manual.** There is no automatic updater and no background update check. To
  move to a newer version you receive the new installer, close the application, and run the
  new installer **over** the existing installation — do not uninstall first. Your database,
  resumes, portal sessions, application history, DPAPI credential and everything under
  `%LOCALAPPDATA%` are preserved, and any new schema changes arrive as new Alembic
  migrations.
- The setup program is **not code-signed**, so SmartScreen warns on first run. Verify the
  SHA256 above, then choose *More info → Run anyway*.
- Only `localhost:5432` is supported; a remote server or a non-default port is not.
- Windows x64 only.

### Verified before release

Installed and exercised end to end on a separate clean Windows computer with PostgreSQL 16
installed by hand: clean-machine installation, install verification, runtime verification,
LinkedIn, Dice, resume upload, matching, dry run, live run, application history, restart,
session persistence, and upgrade over an existing installation.

Full documentation: [WINDOWS_INSTALLER.md](WINDOWS_INSTALLER.md).
