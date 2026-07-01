# Dry-Run Failure Analysis and Fix

Date: June 25, 2026

## Observed Causes

- The global `Next|Review` locator selected LinkedIn's background job carousel button.
- The Easy Apply modal was not awaited before inspecting form controls.
- Form actions were not scoped to the Easy Apply dialog.
- Required-question messages did not identify the affected fields.
- Bulk dry runs did not retain evidence paths.
- The dashboard combined needs-review, failed, and skipped records into one counter.
- One LinkedIn job loaded only the generic jobs shell and required a retry.

## Fixes

- Wait for the Easy Apply modal after clicking.
- Scope upload, required-field, Next, Review, and Submit locators to the modal.
- Use exact button names for Next, Review, and Submit application.
- Retry a blocked modal button click with a short forced click only inside the modal.
- Wait for modal content changes between form pages.
- Record the labels of unanswered required fields.
- Save `form_diagnostic.json` and `form_modal.html` at blocker/failure points.
- Preserve evidence paths for every bulk dry-run application.
- Reload and redetect once when a search-marked Easy Apply job opens without a control.
- Convert unexpected automation exceptions into evidence-backed failed records.
- Separate Needs review, Failed, and Skipped dashboard counts.

## Verification

- Full automated suite: 18 of 18 passed.
- Background carousel Next button regression: PASS.
- Modal-scoped dry-run path through review: PASS.
- Evidence creation for bulk dry runs: PASS.

Existing historical records are unchanged. Run a new dry run to evaluate the corrected workflow.
