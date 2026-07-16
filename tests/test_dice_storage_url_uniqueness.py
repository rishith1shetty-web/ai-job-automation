"""Regression test for the Dice Find Matches DB-persistence 20-to-0 collapse.

Proven from the post-dedup-fix run 20260715T214440286759:
    variation 1 -> raw_count 7, unique_added 7, duplicates_removed 0  (in-memory dedup fixed),
    but the batch insert crashed with:
      psycopg.errors.UniqueViolation: duplicate key value violates unique constraint
      "uq_job_url_resume_batch"
      DETAIL: Key (job_url, resume_id, search_batch_id)=(https://www.dice.com/jobs?..., 21309, ...)
    -> the whole flush rolled back -> 0 jobs persisted -> final result 0.

Root cause: job_opportunities enforces UNIQUE (job_url, resume_id, search_batch_id). Every
distinct Dice job in the batch shared the search-results FALLBACK URL, so the 2nd..Nth rows
collided. In-memory dedup keys on content (fixed earlier); the DB keys on job_url (this fix).

Fix: FindMatchesEngine.storage_job_url disambiguates a shared fallback URL with a stable
per-job fragment (#job-<hash of title|company|location>) so distinct jobs persist. Real
per-job URLs are returned unchanged, and the fragment is a navigation no-op.
"""

import unittest

from app.config import Settings
from app.services.find_matches import FindMatchesEngine

DICE_FALLBACK_URL = (
    "https://www.dice.com/jobs?q=Healthcare+Data+Analyst&location=United+States"
    "&radius=30&radiusUnit=mi&page=1&pageSize=20&easyApply=true"
)


class DiceStorageUrlUniquenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _fallback_jobs(self, n: int) -> list[dict]:
        return [
            {
                "job_url": DICE_FALLBACK_URL,
                "title": f"Healthcare Data Analyst {i}",
                "company": f"Employer {i}",
                "location": "United States",
            }
            for i in range(n)
        ]

    def test_distinct_fallback_jobs_get_distinct_storage_urls(self) -> None:
        # THE regression: 7 distinct jobs sharing the fallback URL must yield 7 distinct
        # storage URLs, so none collide on uq_job_url_resume_batch within one batch.
        urls = [self.engine.storage_job_url(job) for job in self._fallback_jobs(7)]
        self.assertEqual(len(set(urls)), 7)

    def test_storage_url_is_stable_for_same_job(self) -> None:
        job = {"job_url": DICE_FALLBACK_URL, "title": "Data Analyst", "company": "Acme", "location": "US"}
        self.assertEqual(self.engine.storage_job_url(dict(job)), self.engine.storage_job_url(dict(job)))

    def test_fragment_does_not_change_dedup_identity(self) -> None:
        # A persisted (disambiguated) URL must still map to the same duplicate_key as the raw
        # job, so cross-batch dedup keeps recognising the same job.
        raw = {"job_url": DICE_FALLBACK_URL, "title": "Data Analyst", "company": "Acme", "location": "US"}
        stored = {**raw, "job_url": self.engine.storage_job_url(raw)}
        self.assertEqual(self.engine.duplicate_key(raw), self.engine.duplicate_key(stored))

    def test_real_dice_job_detail_url_unchanged(self) -> None:
        url = "https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2"
        job = {"job_url": url, "title": "Data Analyst", "company": "Acme", "location": "US"}
        self.assertEqual(self.engine.storage_job_url(job), url)

    def test_linkedin_url_unchanged(self) -> None:
        url = "https://www.linkedin.com/jobs/view/12345"
        job = {"job_url": url, "title": "Engineer", "company": "Acme", "location": "US"}
        self.assertEqual(self.engine.storage_job_url(job), url)


if __name__ == "__main__":
    unittest.main()
