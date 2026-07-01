# Easy Apply Detection Improvements

Date: June 25, 2026

## Changes

- The opened LinkedIn job page is now authoritative; the workflow no longer skips solely because the search result was not marked Easy Apply.
- Waits for DOM content, network idle when available, and visible job-page content before detection.
- Captures `job_page.png` before searching for application controls.
- Searches for:
  - Easy Apply role button
  - Easy Apply aria label
  - LinkedIn in-apply control markers
  - Apply now
  - Apply
  - Submit application
- Only LinkedIn-specific Easy Apply evidence is clicked.
- Generic Apply controls are diagnosed but are not treated as Easy Apply automatically.
- Detects external redirects and external employer application links.
- Records all visible button texts.
- Records selector counts and page-readiness results.
- Saves `page.html` whenever Easy Apply is not confirmed.
- Writes a structured `diagnostic.json` for every attempted job.

## Output

Each run and job creates:

`outputs/easy_apply_debug/<timestamp>/<linkedin_job_id>/`

Files:

- `01_job_card_click.png`
- `02_job_details_page_loaded.png`
- `03_before_easy_apply_detection.png`
- `04_after_easy_apply_detection.png`
- `diagnostic.json`
- `page.html` when detection fails

Diagnostics include card active state, opening mode, navigation titles and URLs,
search/application phase results, exact selector counts, and
`EASY_APPLY_MISMATCH`.

## Verification

- Confirmed Easy Apply selector test: PASS
- External application detection test: PASS
- Existing single-job workflow tests: PASS
- Existing bulk application tests: PASS
- Full test suite: PASS, 16 of 16

No real LinkedIn application was submitted during verification.
