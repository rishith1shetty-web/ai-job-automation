# Runtime Verification Report

Date: June 23, 2026

## Environment

- Python 3.12.10 installed
- PostgreSQL 16 installed and running on port 5432
- Playwright Chromium 131.0.6778.33 installed
- Project dependencies installed in `.venv`
- FastAPI running at `http://127.0.0.1:8000`
- Streamlit running at `http://127.0.0.1:8501`

## Results

| Feature | Status | Evidence |
|---|---|---|
| Resume upload and parsing | PASS | `resume-upload-test.log`; extracted Python, FastAPI, PostgreSQL, experience, education, and job titles |
| LinkedIn login | BLOCKED | Browser opened, but sign-in was not completed before the 180-second timeout; no authenticated state is being claimed |
| LinkedIn job search | PASS | `linkedin-search-test.log` and `linkedin-api-search-test.log`; live LinkedIn jobs were parsed and persisted |
| Resume-to-job scoring | PASS | `match-scoring-test.log`; relevant Python role ranked above unrelated retail role |
| Easy Apply dry run | PASS | `dry-run-test.log`; two jobs recorded as skipped without submission |
| PostgreSQL connectivity and CRUD | PASS | Schema created; create/read/update/delete test passed; final counts are 1 resume, 4 jobs, 2 application records |
| Streamlit dashboard | PASS | HTTP 200 and visual checks of matched jobs and application history |
| Playwright startup | PASS | Chromium launched, rendered a test page, and produced `playwright-startup.png` |
| Dependency integrity | PASS | `pip check` reported no broken requirements |
| Python compilation | PASS | All application, dashboard, and script modules compiled |

## Runtime Fixes

1. Fixed `scripts/init_db.py` so it can import the application when run directly.
2. Added the missing `API_BASE_URL` setting accepted by `.env.example`.
3. Reworked LinkedIn search to navigate directly to collected job URLs, avoiding sign-in modal overlays that intercepted result-card clicks.

## Screenshots

- `streamlit-dashboard.png`
- `streamlit-history.png`
- `playwright-startup.png`

## Remaining Issue

LinkedIn authentication requires the account owner to complete login and any challenge or CAPTCHA in the visible Chromium window. Run the connection step from the dashboard, finish sign-in, and wait until LinkedIn reaches the feed. The application will then save `storage/linkedin_state.json`.

No live job application was submitted. All application automation verification used dry-run mode.

## Start Commands

From the project directory in PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
python scripts\init_db.py
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

In a second PowerShell window:

```powershell
cd "C:\Users\DELL\Documents\Codex\2026-06-23\build-a-python-application-that-allows"
.\.venv\Scripts\Activate.ps1
python -m streamlit run dashboard.py --server.address 127.0.0.1 --server.port 8501
```

Open `http://127.0.0.1:8501`.
