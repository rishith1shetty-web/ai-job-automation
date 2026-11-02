# Rising Path Recruiters

Resume-driven job search and application automation for LinkedIn and Dice, built as a local
full-stack application with FastAPI, React, PostgreSQL and Playwright.

## Project Overview

Rising Path Recruiters takes a candidate's resume, works out which roles it supports, searches
LinkedIn and Dice for matching Easy Apply jobs, filters and ranks the results, and fills in the
application forms in a real browser session. Every search, match and application attempt is
stored in PostgreSQL, so the application history survives restarts and upgrades.

The application runs entirely on the user's Windows computer. The backend listens on
`127.0.0.1` only, portal sessions and resumes stay in local storage, and the database credential
is protected with Windows DPAPI. It is distributed as a Windows installer, and it can also be
run from source for development.

A few rules shape the design:

- The uploaded resume is the source of truth. It is never rewritten, tailored or modified;
  automation always uploads the original file.
- Each job portal is implemented as an independent adapter, so a change to one portal cannot
  affect another.
- A dry-run mode fills every field and stops before the final submit, and a live submission is
  recorded as applied only after the portal confirms it.

## Key Features

**Resume analysis**

- Parses PDF, DOCX and plain-text resumes.
- Detects the candidate's domain, seniority and job family from the resume's own evidence.
- Produces a ranked list of at most five target roles: one primary role and up to four closely
  related roles. Unrelated roles are filtered out before the list is limited.
- A manual role override is available for any search.

**Job search (Find Matches)**

- Searches LinkedIn and Dice for Easy Apply jobs. One portal is active at a time.
- Find 20, Find 50, Find 100 or a custom count (up to 100). Every preset searches the same
  top-five roles; the preset changes only how many jobs are returned.
- On Dice, jobs posted in the last 24 hours are searched first, with a relevance search as a
  fallback when there are too few.
- Posting times are normalised against a single reference time per search, so results can be
  ordered from newest to oldest.
- Searches run as background batches with live progress in the dashboard.

**Filtering, applied before the requested result count is allocated**

- **Easy Apply only.** External application sites are never added to the results.
- **Senior-level exclusion.** Senior, Lead, Principal, Staff (technical ladder), Architect,
  engineering-management and executive titles are excluded in every domain. The decision is
  made from the job title; descriptions that only mention senior colleagues or leadership do
  not exclude a job, and entry-level titles such as Junior or Associate are kept.
- **Dice Job Match Score.** A Dice job is returned only when the Dice Job Match Score on its
  job-detail page is verified at 60% or higher. Missing, pending or unreadable scores never
  qualify. When too few jobs qualify, the search reads further Dice results, opening at most 150
  job pages per run, and reports any shortfall. Both values are configurable. LinkedIn has no
  equivalent score and is not affected.
- **Duplicate handling.** A job found by more than one search role is kept once, under the
  highest-priority role.
- **Previously applied jobs.** A job already applied to with the same resume on the same portal
  is not offered again, even after the resume is uploaded again.

**Ranking**

- Match score from TF-IDF similarity between the resume and the job, or from an OpenAI model
  when an API key is configured.
- Estimated application complexity and completion likelihood, based on the form provider and the
  screening questions the job is likely to ask.
- Results stay grouped by search role in priority order.

**Application automation**

- Apply to a single selected job, the top 10, or every eligible job in a search batch (up to
  100).
- Answers screening questions from the candidate profile, employment history and a saved
  knowledge base of answers. Questions without a confident answer are marked for review instead
  of being guessed.
- **Stop Automation** ends a running batch at its next safe checkpoint, never in the middle of a
  browser action. **Continue Automation** resumes an interrupted batch from the job it stopped
  at, without searching again.
- Screenshots, debug HTML and step logs are saved for every attempt.
- Application statuses: matched, queued, applied, dry run ready, needs review, skipped, failed
  and unsupported form.

**Dashboard**

- React interface with pages for matched jobs, application history, candidate profile,
  employment history, screening knowledge and diagnostics.
- Matched jobs can be exported to CSV.

## Technology Stack

| Area | Technologies |
|---|---|
| Backend | Python 3.12, FastAPI, Uvicorn, Pydantic Settings |
| Database | PostgreSQL 16, SQLAlchemy 2, Alembic, psycopg 3 |
| Browser automation | Playwright for Python, Chromium |
| Resume parsing and matching | pdfplumber, python-docx, scikit-learn (TF-IDF), optional OpenAI API |
| Frontend | React 18, TypeScript, Vite 5, TanStack Query 5, React Router 6, Tailwind CSS 3 |
| Testing | Python `unittest`; Vitest, Testing Library and MSW for the frontend |
| Packaging | PyInstaller (one-folder build), Inno Setup 6 |
| Platform | Windows 10 and 11, 64-bit |

A Streamlit dashboard (`dashboard.py`) is kept as an optional fallback interface.

## Installation and Setup

### Windows installer

The installer bundles the backend, the built React interface and Chromium, so the target
computer needs no Python, Node.js or browser download.

1. Install **PostgreSQL 16 for Windows x86-64** first, keeping the default port `5432`, and note
   the `postgres` administrator password you choose.
2. Run `RisingPathRecruiters-Setup-<version>.exe` as an administrator, signed in as the Windows
   account that will use the application.
3. Enter the `postgres` password when asked. Setup uses it once to create the application's own
   database role and database, and does not store it.

Upgrades are installed over the existing version without uninstalling; the database and all
user data are kept. Installer files are not stored in this repository. The complete guide,
including checksum verification and troubleshooting, is in
[docs/WINDOWS_INSTALLER.md](docs/WINDOWS_INSTALLER.md).

### Local development

Prerequisites: Windows, Python 3.12, Node.js with npm, and PostgreSQL 16 running on
`localhost:5432`.

Run these from the repository root.

1. Create the virtual environment and install the backend dependencies:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Install Chromium for Playwright. `start_app.ps1` runs the backend with
   `PLAYWRIGHT_BROWSERS_PATH` set to `storage\ms-playwright`, so install the browser there:

   ```powershell
   $env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\storage\ms-playwright"
   .\.venv\Scripts\python.exe -m playwright install chromium
   ```

3. Install the frontend dependencies:

   ```powershell
   cd frontend
   npm ci
   cd ..
   ```

4. Set up the database. The provisioning script prompts for the `postgres` administrator
   password, creates the `ai_job_automation_app` role and the `linkedin_auto_apply` database,
   runs the Alembic migrations, verifies the schema and stores the role's credential with
   Windows DPAPI:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\provision_postgres.ps1
   ```

   Alternatively, copy `.env.example` to `.env`, set `DATABASE_URL` to an existing database, and
   run the migrations with `.\.venv\Scripts\python.exe -m alembic upgrade head`. A
   `DATABASE_URL` takes precedence over the stored credential.

   The installed application and a development checkout on the same computer share the same
   database role but keep its password separately. Provisioning one of them when it has no
   stored credential changes the role's password, so the other copy then needs its credential
   refreshed. See [docs/POSTGRES_PROVISIONING.md](docs/POSTGRES_PROVISIONING.md).

5. Optionally add `OPENAI_API_KEY` to `.env` to enable OpenAI-based resume extraction and match
   scoring. Without it, the local parser and TF-IDF scoring are used.

6. Start the application:

   ```powershell
   .\start_app.ps1
   ```

   This starts FastAPI on `http://127.0.0.1:8000` and the React development server on
   `http://127.0.0.1:5173`, then opens the browser. Useful options:

   ```powershell
   .\start_app.ps1 -NoBrowser      # do not open a browser
   .\start_app.ps1 -WithStreamlit  # also start the Streamlit fallback on port 8501
   .\start_app.ps1 -Packaged       # serve the built frontend from FastAPI on port 8000
   .\stop_app.ps1                  # stop this project's servers only
   ```

### Running the tests

The backend tests read `DATABASE_URL` and create and delete their own rows, so point them at a
disposable database rather than one holding real data:

```powershell
$env:DATABASE_URL = "postgresql+psycopg://<user>:<password>@localhost:5432/<test_database>"
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Frontend tests, type checking and the production build:

```powershell
cd frontend
npm test
npm run typecheck
npm run build
```

### Building the Windows installer

Building needs the development environment above, PyInstaller and Inno Setup 6.3 or newer. Use
PyInstaller 6.15.0: later releases store namespace packages in a form that the build's archive
integrity check rejects.

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller==6.15.0
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
```

`build_windows.ps1` builds the React bundle, stages Chromium, runs PyInstaller, reads back
every entry in the packaged archive and scans the output for secrets and user data. Stop the
development servers first: the script reinstalls `frontend\node_modules`, which fails while the
Vite server has files open. `build_installer.ps1` compiles
`release\RisingPathRecruiters-Setup-<version>.exe`. The version comes from `app/version.py`
alone. See [docs/WINDOWS_BUILD.md](docs/WINDOWS_BUILD.md).

## Usage Instructions

1. **Choose and connect a portal.** No portal is active when the application starts. In the
   sidebar, switch to LinkedIn or Dice and click **Connect LinkedIn** or **Connect Dice**. A
   browser window opens for you to sign in yourself; the session is then saved locally and
   reused.
2. **Upload a resume** (PDF, DOCX or TXT). The dashboard shows the detected primary role, the
   domain expertise and the ranked target roles.
3. **Find matches.** On the Matched jobs page, choose whether to search all suggested roles, a
   single role or a manual override, choose a location, and click **Find 20**, **Find 50**,
   **Find 100** or **Find custom**. Progress appears while the search runs. If fewer jobs
   qualify than requested, the completed search says how many were found and why it stopped.
4. **Review the results.** Jobs show their portal, posting age, apply priority and status, and
   can be exported with **Export matched jobs CSV**.
5. **Apply.** Use **Apply Selected Job**, **Apply Top 10** or **Apply All**. The **Dry run**
   toggle starts off, so an apply run submits for real unless dry run is switched on. A
   running batch can be stopped with **Stop Automation** and later resumed with
   **Continue Automation**.
6. **Track outcomes** on the Application history page. Fill in the Profile, Employment history
   and Screening knowledge pages to improve the answers given to screening questions.

Automated applications are real submissions made through your accounts. Use the application at
a measured rate and in line with each portal's terms of use.

## Project Architecture

```text
app/                     FastAPI backend
  main.py                API routes, Find Matches pipeline, apply batches
  models.py, database.py SQLAlchemy models and database engine
  services/
    portals/             Portal adapters: base.py (interface), dice_portal.py, linkedin_portal.py
    linkedin.py          LinkedIn search and Easy Apply automation
    find_matches.py      Search plan, role variations, result allocation
    seniority_filter.py  Shared senior-level role exclusion
    resume_parser.py     Resume text extraction and analysis
    domain_detection.py, role_ranking.py, role_selection.py
    matcher.py           Resume-to-job match scoring
    screening_engine.py, generalized_form_engine.py
  windows_launcher.py    Entry point of the packaged application
alembic/                 Database migrations
frontend/                React and TypeScript dashboard (Vite)
installer/               Inno Setup script and installer metadata
scripts/                 Build, packaging, provisioning and verification scripts
tests/                   Backend test suite
docs/                    Installation, build, database and release documentation
start_app.ps1, stop_app.ps1
```

**Portal adapters.** Each portal implements the `PortalAdapter` interface in
`app/services/portals/base.py`: connect, disconnect, search and apply. Each adapter owns its
selectors, search logic, Easy Apply detection, screening handling and status mapping. Shared
logic such as role planning, seniority filtering, posting-time parsing, duplicate identity and
ranking lives in portal-independent modules, so a new portal can be added without changing the
existing ones.

**Find Matches pipeline.** A search request creates a batch and runs in a background task:

1. Build the search plan: one search per target role, at most five, in priority order.
2. The active portal's adapter searches each role within a bounded budget. Dice also verifies
   the Dice Job Match Score of each eligible candidate.
3. A shared gate drops duplicates, previously applied jobs, non-Easy-Apply jobs, senior-level
   roles and Dice jobs below the score threshold.
4. A weighted allocation shares the requested total across roles, giving the primary role the
   largest share and reassigning unused places to roles that have more eligible jobs.
5. Jobs are enriched with posting time, complexity and match scores, ranked and saved.

The dashboard polls the batch for progress. Apply runs use the same batch-and-poll model,
which is what lets them be stopped and resumed.

**Frontend.** The React application talks to the API through TanStack Query hooks in
`frontend/src/api`. In development, Vite proxies `/api` to FastAPI on port 8000. In the
packaged application, FastAPI serves the built frontend itself and no Node.js is needed.

## Database and Security

**Database.** PostgreSQL stores resume profiles, job opportunities, search batches, application
records, apply batches, the candidate profile and employment history, screening answers and
settings, and form-field learning data. The schema is managed with Alembic; the packaged
application applies any pending migrations and verifies the resulting schema each time it
starts. Upgrades keep the existing database, and schema changes are shipped as additive
migrations.

**Database credentials**

- The application connects as a dedicated least-privilege role, `ai_job_automation_app`, which
  owns only the application database and is not a superuser.
- The role's password is generated at random during provisioning and stored only as a Windows
  DPAPI blob scoped to the current user. It cannot be read by another Windows account or on
  another computer.
- The PostgreSQL administrator password is used once during setup to create the role and the
  database. It is never written to disk in plain text, placed on a command line or stored.
- Credentials are resolved in a fixed order: `DATABASE_URL` (development and tests), then the
  DPAPI-protected store. A development-only fallback stays off unless it is enabled
  explicitly.

**Local-only operation**

- The API binds to `127.0.0.1` and is not reachable from the network.
- Installed copies keep all user data under `%LOCALAPPDATA%\AIJobAutomation`, never in the
  roaming profile. A development checkout keeps it in the project's `storage/`, `outputs/` and
  `work/` folders.
- LinkedIn and Dice sessions are stored as local browser storage-state files. Passwords for
  the job portals are never stored; sign-in is always done by the user in the browser.

**Build safeguards**

- `.gitignore` excludes `.env`, `storage/`, `outputs/`, database dumps and key files.
- The build scans the packaged output and fails if it finds environment files, credentials,
  portal sessions, uploaded resumes or database dumps.

## Future Improvements

- Code signing for the Windows installer, which is currently unsigned.
- Additional job portals, such as Indeed, ZipRecruiter and Glassdoor, through the existing
  portal adapter interface.
- A calibrated, portal-independent resume-fit threshold, so that LinkedIn results can have a
  minimum match quality comparable to the Dice Job Match Score filter.
- Support for PostgreSQL servers other than `localhost:5432`.

## Contributing

1. Create a branch from `main`.
2. Keep each change limited to one concern. Portal-specific code belongs in that portal's
   adapter, and shared modules must not depend on a particular portal.
3. Add or update tests, then run the backend suite, the frontend tests, the type check and the
   production build before opening a pull request.
4. Never commit `.env` files, credentials, portal sessions, resumes or database dumps.

## License

No license has been specified for this project yet. A `LICENSE` file needs to be added to define
the terms of use.

## Contact

Contact details have not been added yet.
