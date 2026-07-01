# Search Batch Implementation

Date: June 25, 2026

## Implemented

- Every Find Matches execution creates a separate `job_search_batches` record.
- Every matched job stores `search_batch_id` and `search_timestamp`.
- The same LinkedIn job URL can exist in multiple batches for the same resume.
- Previous batches and jobs are preserved.
- Legacy jobs without a batch are preserved.
- `Apply to top 10` uses the latest batch for the active resume by default.
- The API accepts an optional `search_batch_id` to apply from a selected historical batch.
- Dashboard counters, matched jobs, and application history follow the selected batch.
- The dashboard defaults to the newest batch and provides a selector for older batches.

## API

- `GET /jobs/search-batches?resume_id=<id>`
- `GET /jobs?resume_id=<id>&search_batch_id=<batch_id>`
- `GET /dashboard/summary?resume_id=<id>&search_batch_id=<batch_id>`
- `GET /applications?resume_id=<id>&search_batch_id=<batch_id>`
- `POST /applications/apply-top` accepts optional `search_batch_id`

## Verification

- PostgreSQL migration: PASS
- Repeated searches with the same job URL create separate rows: PASS
- Latest-batch job listing: PASS
- Latest-batch dashboard summary: PASS
- Apply Top 10 defaults to the latest batch: PASS
- Previous-batch selector changes jobs and counters: PASS
- Existing search, matching, and application tests: PASS
- Full automated suite: 17 of 17 passed

No LinkedIn application was submitted during verification.
