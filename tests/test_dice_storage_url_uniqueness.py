"""What may be PERSISTED as a job_url.

History of this file: it used to assert that a shared search-results URL was made unique by
appending a stable ``#job-<hash>`` fragment, so distinct jobs would not collide on
UNIQUE (job_url, resume_id, search_batch_id) and roll back the whole batch insert. That
unblocked persistence but manufactured PSEUDO-URLs: the fragment means nothing to Dice, so
every such row still opened the same search-results page and Apply applied to whichever job
was selected there. Live dry-run artifacts proved it -- several dashboard rows sharing one
search URL all reached the same wizard.

The rule now: a portal resolves a real per-job URL or reports the job URL_UNRESOLVED and drops
it (see tests/test_dice_job_identity.py). Nothing manufactures identity, so storage_job_url
persists the portal's URL verbatim -- there is no shared URL left for it to disambiguate.
"""

import unittest

from app.config import Settings
from app.services.find_matches import FindMatchesEngine

DICE_SEARCH_URL = (
    "https://www.dice.com/jobs?q=Healthcare+Data+Analyst&location=United+States"
    "&radius=30&radiusUnit=mi&page=1&pageSize=20&easyApply=true"
)
CANONICAL_URL = "https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2"


class StorageJobUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_no_pseudo_url_fragment_is_ever_manufactured(self) -> None:
        for url in (DICE_SEARCH_URL, CANONICAL_URL, "https://www.linkedin.com/jobs/view/12345", ""):
            with self.subTest(url=url):
                stored = self.engine.storage_job_url(
                    {"job_url": url, "title": "Data Analyst", "company": "Acme", "location": "US"}
                )
                self.assertNotIn("#job-", stored)
                self.assertEqual(stored, url)

    def test_storage_no_longer_disambiguates_a_shared_url(self) -> None:
        # Two distinct jobs that somehow share a URL are NOT made unique here any more --
        # resolving them (or dropping them as URL_UNRESOLVED) is the portal's job, before
        # persistence, so no such pair should reach this point at all.
        a = self.engine.storage_job_url({"job_url": DICE_SEARCH_URL, "title": "A", "company": "X", "location": "US"})
        b = self.engine.storage_job_url({"job_url": DICE_SEARCH_URL, "title": "B", "company": "Y", "location": "US"})
        self.assertEqual(a, b)

    def test_dedup_identity_still_distinguishes_such_jobs(self) -> None:
        # The dedup key stays content-based for a non-identifying URL, so two jobs sharing one
        # are still recognised as different jobs (that earlier fix is unchanged).
        a = self.engine.duplicate_key({"job_url": DICE_SEARCH_URL, "title": "A", "company": "X", "location": "US"})
        b = self.engine.duplicate_key({"job_url": DICE_SEARCH_URL, "title": "B", "company": "Y", "location": "US"})
        self.assertEqual(set(a) & set(b), set())

    def test_real_per_job_urls_are_persisted_verbatim(self) -> None:
        for url in (CANONICAL_URL, "https://www.linkedin.com/jobs/view/12345"):
            with self.subTest(url=url):
                job = {"job_url": url, "title": "Data Analyst", "company": "Acme", "location": "US"}
                self.assertEqual(self.engine.storage_job_url(job), url)


if __name__ == "__main__":
    unittest.main()
