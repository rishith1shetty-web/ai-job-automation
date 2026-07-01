# Single Selected Job Application Report

Date: June 24, 2026

## Implemented

- Added a single-select radio control for matched jobs.
- Added an `Apply Selected Job` button.
- Kept the existing `Apply to top 10` workflow.
- Added `POST /applications/apply-selected`.
- Validates that the selected job belongs to the active resume.
- Processes only the selected job ID.
- Uses the same Easy Apply automation as the bulk workflow.
- Respects the shared Dry Run toggle.
- Dry Run now opens the workflow and stops before clicking Submit.
- Added `needs_review` status for unanswered required questions and uncertain completion.
- Added database fields for Dry Run, current step, and evidence path.
- Added a dashboard result panel for selected job, status, last step, error, and evidence location.

## Evidence

Each selected-job run creates:

`outputs/live_test/<timestamp>_job_<id>_<title>/`

Contents:

- `application_log.json`

Screenshots are saved under:

`outputs/live_test/screenshots/<timestamp>_job_<id>_<title>/`

- `01_job_page.png`
- `02_easy_apply_modal.png`
- `03_resume_upload.png`
- `04_review_step.png`
- `05_submission_confirmation.png` for live submissions
- `failure_*.png` when a failure or review condition occurs

Logged workflow events include:

- Job opened
- Easy Apply clicked
- Resume uploaded
- Form page processed
- Review page reached
- Submit clicked
- Confirmation received
- Failure or needs-review reason

## Verification

- Python compilation: PASS
- Database migration: PASS
- FastAPI startup and health endpoint: PASS
- Streamlit startup: PASS
- Single-job selection UI: PASS
- Exactly one selected job processed: PASS
- Bulk ranked processing unchanged: PASS
- Dry-run evidence log creation: PASS
- Dry-run screenshots through review: PASS
- Automated tests: PASS, 12 of 12

## Safety

- Dry Run remains enabled by default.
- A dry run never clicks `Submit application`.
- Required unanswered fields stop the workflow with `needs_review`.
- Non-Easy Apply jobs are skipped and logged.
- A real LinkedIn submission was not performed during implementation verification.

## Start Commands

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\start_app.ps1
```

Dashboard:

`http://127.0.0.1:8501`

API:

`http://127.0.0.1:8000`

Run tests:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```
