# Automatic Job Search Workflow Report

Date: June 24, 2026

## Changes Made

- Added extraction and persistence for technologies and domain expertise.
- Added deterministic target-role inference with a confidence score.
- Added generated role and technology search keywords.
- Added role profiles for backend, data analysis, machine learning, data engineering, frontend, full stack, DevOps, project management, and product management.
- Changed job search so an empty keyword uses the detected role and generated role variants.
- Kept manual role selection as an optional override.
- Added a visible detected-profile section to Streamlit.
- Added database migration statements for existing PostgreSQL installations.
- Added profile details for technologies, domains, and generated search terms.

## Files Modified

- `app/database.py`
- `app/main.py`
- `app/models.py`
- `app/schemas.py`
- `app/services/linkedin.py`
- `app/services/resume_parser.py`
- `dashboard.py`
- `README.md`

## Files Added

- `tests/test_resume_targeting.py`
- `tests/test_automatic_search_workflow.py`
- `outputs/automatic-role-workflow.png`
- `outputs/automatic-profile-details.png`

## Test Results

| Test | Result |
|---|---|
| PostgreSQL schema migration | PASS |
| Python/FastAPI compilation | PASS |
| Backend Developer inference | PASS |
| Data Analyst inference | PASS |
| Machine Learning Engineer inference | PASS |
| BI Analyst keyword generation | PASS |
| Empty manual keyword query generation | PASS |
| Manual role override precedence | PASS |
| Search handler with `keywords=None` | PASS |
| Resume upload and automatic profile API response | PASS |
| Streamlit detected-role display | PASS |
| Streamlit default “Auto-detect from resume” option | PASS |
| Live LinkedIn search during this test run | BLOCKED: LinkedIn session was disconnected |

Seven automated tests passed. The controlled search-handler integration test confirmed that no manual keyword is required and that the generated query was:

`Backend Developer OR Python Developer OR FastAPI Developer OR Software Engineer`

The API upload test detected:

- Role: Backend Developer
- Confidence: 98%
- Technologies: AWS, CI/CD, Docker, FastAPI, Git, PostgreSQL, React, SQL
- Domains: Backend Engineering, API Development

## Updated Workflow

1. Upload a resume.
2. Click **Analyze resume**.
3. Review the detected role, confidence, and generated terms.
4. Leave **Role override** on **Auto-detect from resume**.
5. Click **Find matches**.

The user only selects a role when they intentionally want to override the detected role.

## Screenshots

- `automatic-role-workflow.png`
- `automatic-profile-details.png`

## Remaining External Requirement

LinkedIn must be connected before a live search can run. The automatic role and query flow is fully tested, but the live LinkedIn retrieval test in this run stopped at the expected `409 Connect LinkedIn before searching for jobs` response because the saved account session was disconnected.
