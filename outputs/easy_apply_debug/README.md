# Easy Apply Diagnostics

Every search or application run creates folders using:

`outputs/easy_apply_debug/<timestamp>/<linkedin_job_id>/`

Each folder contains:

- `01_job_card_click.png`
- `02_job_details_page_loaded.png`
- `03_before_easy_apply_detection.png`
- `04_after_easy_apply_detection.png`
- `diagnostic.json`: job URL, final URL, page readiness, visible button texts, selector counts, external links, classification, and failure reason.
- `page.html`: saved only when LinkedIn Easy Apply cannot be confirmed.

The JSON also records:

- LinkedIn job ID and URL
- Page title and current URL after navigation
- Whether the card stayed active
- In-place panel, new-tab, or external-site opening mode
- Search-phase and application-phase Easy Apply results
- Exact requested Playwright selector counts
- `EASY_APPLY_MISMATCH`

Classifications:

- `linkedin_easy_apply`: a LinkedIn-specific Easy Apply control was confirmed.
- `external_redirect`: the job URL redirected away from LinkedIn.
- `external_application`: an external employer application link was found.
- `unverified_apply_button`: a generic Apply or Apply now control was present but was not safely identifiable as LinkedIn Easy Apply.
- `not_easy_apply`: neither the search record nor opened page indicated Easy Apply.
- `easy_apply_control_missing`: the search record claimed Easy Apply but no supported control appeared after loading.
