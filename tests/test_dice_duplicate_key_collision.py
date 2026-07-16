"""Regression test for the Dice Find Matches 20-to-0 downstream collapse.

Proven from the real run 20260715T204921339532:
    portal returned 20 native Easy Apply jobs (27 raw across 4 variations),
    but unique_jobs_found = 0 and duplicate_jobs_skipped = 27.

Root cause: FindMatchesEngine.duplicate_key emitted the shared Dice search-results FALLBACK
URL as identity. For every job whose per-job URL could not be resolved, job_url stripped to
"https://www.dice.com/jobs" and job_id to "jobs" -- identical for all 27 -- so the main.py
dedup loop (any(key in seen/previous_keys)) collapsed every distinct job onto the first.
Across the 27 rows there was exactly 1 distinct stripped URL / job_id but 26 distinct
(title, company): the jobs were real and different.

Fix: duplicate_key drops the non-identifying URL keys for listing/search URLs and falls back
to the (title, company, location) content key, which is distinct per job. Real per-job URLs
(LinkedIn /jobs/view/<id>, Dice /job-detail/<guid>) are unaffected.
"""

import unittest

from app.config import Settings
from app.services.find_matches import FindMatchesEngine

DICE_FALLBACK_URL = (
    "https://www.dice.com/jobs?q=Healthcare+Data+Analyst&location=United+States"
    "&radius=30&radiusUnit=mi&page=1&pageSize=20&easyApply=true"
)


def _dedupe(engine: FindMatchesEngine, jobs: list[dict]) -> tuple[list[dict], int]:
    """Mirror of the main.py dedup loop (seen_keys + any-key-in-seen)."""
    seen_keys: set[str] = set()
    kept: list[dict] = []
    removed = 0
    for raw in jobs:
        keys = [key for key in engine.duplicate_key(raw) if key]
        if any(key in seen_keys for key in keys):
            removed += 1
            continue
        seen_keys.update(keys)
        kept.append(raw)
    return kept, removed


class DiceDuplicateKeyCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _dice_fallback_jobs(self, n: int) -> list[dict]:
        # Distinct jobs that all share the Dice search-results fallback URL (the real failure).
        return [
            {
                "job_url": DICE_FALLBACK_URL,
                "title": f"Healthcare Data Analyst {i}",
                "company": f"Employer {i}",
                "location": "United States",
                "easy_apply": True,
            }
            for i in range(n)
        ]

    def test_distinct_dice_fallback_jobs_do_not_collapse(self) -> None:
        # THE regression: 20 distinct jobs sharing the fallback URL must all survive dedup.
        jobs = self._dice_fallback_jobs(20)
        kept, removed = _dedupe(self.engine, jobs)
        self.assertEqual(len(kept), 20)
        self.assertEqual(removed, 0)

    def test_fallback_url_is_not_used_as_identity(self) -> None:
        a = self.engine.duplicate_key(
            {"job_url": DICE_FALLBACK_URL, "title": "A", "company": "X", "location": "US"}
        )
        b = self.engine.duplicate_key(
            {"job_url": DICE_FALLBACK_URL, "title": "B", "company": "Y", "location": "US"}
        )
        # No shared key between two different jobs that only share the fallback URL.
        self.assertEqual(set(a) & set(b), set())
        # And the degenerate tokens are never emitted as keys.
        self.assertNotIn("https://www.dice.com/jobs", set(a) | set(b))
        self.assertNotIn("jobs", set(a) | set(b))

    def test_same_job_still_deduplicates(self) -> None:
        # Two copies of the SAME job (same title/company/location) must still collapse.
        same = {"job_url": DICE_FALLBACK_URL, "title": "Data Analyst", "company": "Acme", "location": "US"}
        kept, removed = _dedupe(self.engine, [dict(same), dict(same)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, 1)

    def test_real_dice_job_detail_url_keeps_full_identity(self) -> None:
        key = self.engine.duplicate_key(
            {
                "job_url": "https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2",
                "title": "Data Analyst",
                "company": "Acme",
                "location": "US",
            }
        )
        self.assertIn("https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2", key)
        self.assertIn("be2352e2-6429-495f-b28d-71f9bd9a60b2", key)

    def test_linkedin_identity_unchanged(self) -> None:
        # Distinct LinkedIn jobs keep distinct id-based identity...
        a = self.engine.duplicate_key(
            {"job_url": "https://www.linkedin.com/jobs/view/111", "title": "A", "company": "X", "location": "US"}
        )
        b = self.engine.duplicate_key(
            {"job_url": "https://www.linkedin.com/jobs/view/222", "title": "B", "company": "Y", "location": "US"}
        )
        self.assertIn("111", a)
        self.assertIn("222", b)
        self.assertEqual(set(a) & set(b), set())
        # ...and two representations of the same LinkedIn job still collapse on the view id.
        c = self.engine.duplicate_key(
            {"job_url": "https://www.linkedin.com/jobs/view/333?trk=abc", "title": "Eng", "company": "Z", "location": "US"}
        )
        d = self.engine.duplicate_key(
            {"job_url": "https://www.linkedin.com/jobs/view/333", "title": "Eng", "company": "Z", "location": "US"}
        )
        self.assertTrue(set(c) & set(d))


if __name__ == "__main__":
    unittest.main()
