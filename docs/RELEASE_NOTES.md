# Rising Path Recruiters — release notes

## 1.3.0

A matching-quality release. **Install it over 1.2.0 — do not uninstall first.** The database
schema is unchanged from 1.2.0, so there is no migration to run and nothing to convert; every
existing resume, matched job, application record, profile, employment-history entry, screening
answer, portal session and stored credential is carried over untouched.

**Installer SHA256**

```
84A5F6DE0D01105A6BE1CCBFDA9E9EE83E0FD3164AD50BA9432B7AFF0C70F4A5
```

### What is new

- **Senior-level roles are excluded from Find Matches**, for LinkedIn and Dice and in every
  domain. Senior, Sr., Lead, Principal, Staff (on a technical ladder), Architect, engineering
  management and executive titles are left out; junior, entry-level, associate and ordinary
  individual-contributor roles are kept. The decision is made from the job title — a
  description that merely mentions senior colleagues or leadership never excludes a job.
  Excluded jobs are rejected before the requested total is shared out, so they never take a
  place in Find 20, 50 or 100.
- **Dice jobs need a Dice Job Match Score of 60% or higher.** Find Matches reads the genuine
  score from each Dice job's own page and returns a Dice job only when that score is verified at
  60% or above. A missing, still-loading or unreadable score never qualifies. The search reads
  further Dice results when too few jobs qualify, opening at most 150 job pages per run; when
  fewer jobs than requested qualify, the finished search says how many were found, how many
  were requested and whether the 150-page limit was reached. The verified score is saved with
  each job. LinkedIn jobs have no Dice score and are not affected.
- **Find Matches progress is visible while Dice scores are read**, with the number of job pages
  inspected and jobs qualifying so far.
- **The Find buttons explain themselves when no portal is active.** After the application
  starts, no portal is active until one is chosen and connected in the sidebar; Find 20, 50, 100
  and Find custom are now disabled with a message saying so, instead of appearing to do
  nothing.

### Unchanged from 1.2.0

- Easy Apply eligibility, ranking and freshness, duplicate handling, the already-applied
  exclusion, Continue Automation and Stop Automation.
- LinkedIn and Dice connection, search, screening, resume handling and status mapping, apart
  from the Find Matches filters above.
- The local PostgreSQL deployment model: database `linkedin_auto_apply`, role
  `ai_job_automation_app`, port 5432, PostgreSQL 16 installed by hand beforehand.
- Installation directory, Start-menu group, `AppId`, executable name and the
  `%LOCALAPPDATA%\AIJobAutomation` data layout.

### Upgrading from 1.2.0

Close the application, run `RisingPathRecruiters-Setup-1.3.0.exe` as administrator, and accept
the same installation directory. Setup detects the existing database and keeps it — it does not
recreate the role, rotate the credential or drop anything. Nothing under
`%LOCALAPPDATA%\AIJobAutomation` is read or written by the installer, and the uninstaller still
removes only the program directory.

### Known limitations

Unchanged from 1.2.0: PostgreSQL 16 installed by hand, manual updates, no code signing,
`localhost:5432` only, Windows x64 only.

The Dice score check opens each candidate job's page, so a Dice search can take several
minutes longer, and Find 50 or Find 100 will often stop at the 150-page limit with fewer jobs
than requested. The score is Dice's own, calculated from the Dice account's Ideal Job profile.

## 1.2.0

A recovery release. **Install it over 1.1.0 or 1.0.0 — do not uninstall first.** The database
schema is unchanged from both, so there is no migration to run and nothing to convert; every
existing resume, matched job, application record, profile, employment-history entry, screening
answer, portal session and stored credential is carried over untouched.

**Installer SHA256**

```
D1F650B396B6C888D7600E0563D272AE78B6AFAFECBB2AC8CDE9FCCFD41DDA88
```

### What is new

- **Continue Automation.** An apply run that ends before processing every eligible job can be
  continued from the exact job it stopped at. The button replaces Apply Jobs whenever an
  unfinished batch exists, and returns to Apply Jobs once the batch is done.
- **The same batch is resumed, never a new one.** No search is re-run, no job list is
  regenerated, and nothing already processed is revisited. The original ordering, allocation,
  duplicate ownership, priorities, counters, progress and timestamps are all preserved, because
  they are read back from what was stored rather than recalculated.
- **Every recoverable interruption stays resumable** — Stop Automation, a portal or browser
  failure, a network drop, the application being closed mid-run, a machine going away, and a
  defect in the automation itself. A batch stops being resumable only when it completes, when
  no eligible job is left, when its resume is gone, or when a new Find Matches supersedes it.
- **Interrupted runs are recovered without a restart.** A batch left behind by a worker that
  vanished is adopted automatically, so the Continue button appears on the next refresh rather
  than only after the application is restarted.
- **Already-finished jobs keep their outcome for ever.** Applied stays applied, failed stays
  failed, skipped stays skipped; only jobs that never ran are picked up again.

### Unchanged from 1.1.0

- LinkedIn and Dice automation, Easy Apply handling, screening, resume handling and status
  mapping.
- Stop Automation, which is extended by this release rather than replaced.
- Dry run still starts OFF, and a resumed run keeps the mode its batch began with.
- The top-five search roles, Find 20/50/100 behaviour, and the two-tab navigation.
- The local PostgreSQL deployment model: database `linkedin_auto_apply`, role
  `ai_job_automation_app`, port 5432, PostgreSQL 16 installed by hand beforehand.
- Installation directory, Start-menu group, `AppId`, executable name and the
  `%LOCALAPPDATA%\AIJobAutomation` data layout.

### Upgrading from 1.1.0

Close the application, run `RisingPathRecruiters-Setup-1.2.0.exe` as administrator, and accept
the same installation directory. Setup detects the existing database and keeps it — it does not
recreate the role, rotate the credential or drop anything. Nothing under
`%LOCALAPPDATA%\AIJobAutomation` is read or written by the installer, and the uninstaller still
removes only the program directory.

### Known limitations

Unchanged from 1.1.0: PostgreSQL 16 installed by hand, manual updates, no code signing,
`localhost:5432` only, Windows x64 only.

A batch that fails immediately every time will keep offering Continue Automation. That is
deliberate — the error is shown in full each time and the user decides when to stop; there is
no automatic retry loop.

## 1.1.0

A feature and usability release. **Install it over 1.0.0 — do not uninstall first.** The
database schema is unchanged from 1.0.0, so there is no migration to run and nothing to
convert; every existing resume, matched job, application record, profile, employment-history
entry, screening answer, portal session and stored credential is carried over untouched.

**Installer SHA256**

```
5E5A6A8CE9191A1E59F610CB994B2F69EB30AAC1042918DB48FC5F2D4D7DD889
```

### What is new

- **At most five search roles, chosen by evidence.** Resume analysis now produces one
  detected primary role plus up to four genuinely close strong-fit roles, instead of a
  twelve-role list. Roles are filtered first — by professional family, by seniority, and by
  whether anything in the resume actually supports them — and only then limited to five, so
  the list never contains unrelated filler. The primary role is always rank 1.
- **Find 20, Find 50 and Find 100 all search the same five roles.** The preset now controls
  only how many jobs come back, not which roles are searched, so a wider search cannot drift
  into a different profession.
- **Dry run now starts OFF.** The toggle is unchanged in name and meaning — switching it on
  still fills every field and stops before the final Submit. Only its starting value changed,
  so an apply run performs a real application unless you deliberately ask for a dry run.
- **Stop Automation.** A running batch can be stopped from the dashboard. The run ends at its
  next safe checkpoint — never in the middle of a browser action — and no further job is
  submitted. Each portal is checked immediately before its Submit button is clicked, so a
  stop that arrives late still prevents that submission. Jobs that were never reached stay
  matched and can be applied to later; completed applications and history are untouched.
- **Job Application Platform branding**, replacing the previous product wording in the
  sidebar, header, welcome banner and browser tab title.
- **Two-tab navigation** — Matched jobs and Application history. The other sections keep
  their pages and URLs but no longer appear in the navigation bar.
- **Empty role groups are hidden.** Related and Stretch groups are no longer rendered as
  empty headings when the five-role list leaves them unused.

### Unchanged from 1.0.0

- LinkedIn and Dice automation, including search, pagination, screening, resume handling and
  status mapping. The only additions are the cancellation checkpoints.
- The local PostgreSQL deployment model: database `linkedin_auto_apply`, role
  `ai_job_automation_app`, port 5432, PostgreSQL 16 installed by hand beforehand.
- Installation directory, Start-menu group, `AppId`, executable name and the
  `%LOCALAPPDATA%\AIJobAutomation` data layout.
- The status vocabulary. No new `ApplicationStatus` value was introduced.

### Upgrading from 1.0.0

Close the application, run `RisingPathRecruiters-Setup-1.1.0.exe` as administrator, and
accept the same installation directory. Setup detects the existing database and keeps it —
it does not recreate the role, rotate the credential or drop anything. Nothing under
`%LOCALAPPDATA%\AIJobAutomation` is read or written by the installer, and the uninstaller
still removes only the program directory.

### Known limitations

Unchanged from 1.0.0: PostgreSQL 16 installed by hand, manual updates, no code signing,
`localhost:5432` only, Windows x64 only.

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
