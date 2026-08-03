# LinkedIn AI Job Matcher

A FastAPI and Streamlit application that parses an existing resume, finds LinkedIn Easy Apply jobs, scores them against the candidate profile, ranks the best opportunities, submits compatible Easy Apply forms, and records application history in PostgreSQL.

The application never rewrites or tailors the uploaded resume. Browser automation uses the original uploaded file.

## Features

- PDF, DOCX, and text resume parsing
- Automatic target-role detection with confidence scoring
- Resume-derived LinkedIn search keywords, technologies, and domain expertise
- Optional role override; no manual keyword is required
- OpenAI structured extraction and match scoring
- Local extraction and TF-IDF scoring fallback
- LinkedIn authenticated browser session storage
- Easy Apply-first job search and ranking
- Top-10 application workflow with dry-run mode
- Conservative form handling that skips unknown required questions
- PostgreSQL tracking for jobs, status, attempts, dates, URLs, and logs
- FastAPI backend and Streamlit operations dashboard
- Retry handling for recoverable Playwright failures

## Setup

1. Create and activate a Python 3.11+ virtual environment.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   playwright install chromium
   ```

3. Copy `.env.example` to `.env`. Add `OPENAI_API_KEY` to enable LLM parsing and scoring.
4. Start PostgreSQL:

   ```powershell
   docker compose up -d postgres
   ```

5. Start the API:

   ```powershell
   uvicorn app.main:app --reload
   ```

6. In a second terminal, start the dashboard:

   ```powershell
   streamlit run dashboard.py
   ```

Open `http://localhost:8501`.

### One-command local startup (recommended)

`start_app.ps1` is the primary local workflow. It starts FastAPI and the **React
dashboard** (the primary UI) and opens the browser to React. Streamlit is kept as an
optional fallback and is not started unless you ask for it.

```powershell
.\start_app.ps1                 # FastAPI + React (primary); opens http://127.0.0.1:5173
.\start_app.ps1 -WithStreamlit  # also start the Streamlit fallback on :8501
.\start_app.ps1 -NoBrowser      # start without opening a browser
.\stop_app.ps1                  # stop ONLY this project's FastAPI, React/Vite and Streamlit
```

Local URLs:

- React dashboard (primary): `http://127.0.0.1:5173`
- FastAPI backend / health: `http://127.0.0.1:8000` (`/health`)
- Streamlit fallback (optional): `http://127.0.0.1:8501`

React talks to FastAPI through the Vite dev proxy (`/api` → `http://localhost:8000`), so
no CORS configuration is required. Streamlit remains fully supported as a fallback — run
`streamlit run dashboard.py` directly, or start it alongside React with `-WithStreamlit`.
`dashboard.py` is unchanged.

## First Run

1. Upload and analyze a resume.
2. Click **Connect LinkedIn**. A browser opens for manual sign-in; the session is stored locally after LinkedIn reaches the feed.
3. Review the detected role and generated search keywords.
4. Search for matches without selecting a role, or choose an optional manual override.
5. The search requests LinkedIn's Easy Apply filter and ranks the top 10.
6. Keep **Dry run** enabled for the first pass.
7. Review results, then disable dry run to submit eligible Easy Apply applications.

## Automation Behavior

LinkedIn changes its interface frequently, so selectors may need maintenance. The automation:

- uses the authenticated session in `storage/linkedin_state.json`;
- uploads only the original resume file;
- does not invent answers to required screening questions;
- skips forms it cannot safely complete;
- retries transient Playwright failures up to three times;
- defaults to `AUTO_APPLY_DRY_RUN=false`, so an apply run submits for real unless the Dry run
  toggle is switched on (ON still fills everything and stops before the final Submit);
- caps each run using `MAX_APPLICATIONS_PER_RUN`;
- can be stopped mid-batch with **Stop Automation**: the run ends at its next safe checkpoint,
  never mid-action, and no further job is submitted.

Use automation at a measured rate and review LinkedIn's current terms and account restrictions before enabling live submission.

## API

- `POST /resumes`
- `GET /resumes/latest`
- `POST /linkedin/connect`
- `GET /linkedin/status`
- `POST /linkedin/disconnect`
- `POST /jobs/search`
- `GET /jobs`
- `POST /applications/apply-top`
- `POST /applications/apply-selected`
- `GET /jobs/search-batches?resume_id=<id>`
- `GET /applications`
- `GET /dashboard/summary`
- `GET /health`
