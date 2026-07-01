# Ranked Role Detection Verification Report

Date: June 24, 2026

## Changes Made

- Replaced single-role detection with five ranked role suggestions.
- Added calibrated confidence scores that account for ambiguity between disciplines.
- Added source-aware evidence from job titles, experience, projects, education, skills, and technologies.
- Added chronological title weighting so the current title is stronger than older positions.
- Added dedicated Data Scientist, Machine Learning Engineer, Analytics Engineer, Data Analyst, and Business Intelligence Analyst profiles.
- Prevented technologies such as TensorFlow and Power BI from becoming generated job-title keywords.
- Added a generated LinkedIn Boolean query using all five suggested roles.
- Added database fields for role suggestions and generated search queries.
- Updated FastAPI responses and automatic search-query resolution.
- Updated Streamlit with a top-five role table, confidence bars, generated query, individual role selection, all-role search, and manual override.

## Vaishnavi Resume Result

1. Data Scientist: 91.9%
2. Machine Learning Engineer: 69.6%
3. Analytics Engineer: 67.5%
4. Data Analyst: 58.3%
5. Business Intelligence Analyst: 57.3%

Generated query:

`Data Scientist OR Machine Learning Engineer OR Analytics Engineer OR Data Analyst OR BI Analyst`

## Test Results

- Python compilation: PASS
- Full unit test suite: PASS, 9 of 9 tests
- Vaishnavi PDF extraction: PASS
- Expected five-role ordering: PASS
- Confidence below 95% for multidisciplinary resume: PASS
- Technologies excluded from role keywords: PASS
- Automatic all-role query generation: PASS
- Manual role override resolution: PASS
- PostgreSQL migration and persistence: PASS
- FastAPI resume upload with real PDF: PASS
- Streamlit startup: PASS
- Top-five role table rendering: PASS
- Suggested-role selector options: PASS
- Manual override input rendering: PASS

## Runtime

- Dashboard: http://127.0.0.1:8501
- API health: http://127.0.0.1:8000/health
- Screenshot: `outputs/role-ranking-dashboard.png`

## Remaining Notes

- The ranked-role system is heuristic when no OpenAI API key is configured. It is deterministic and tested, but unusual titles may still require future role-profile additions.
- No live LinkedIn job search or application submission was triggered during this verification.
