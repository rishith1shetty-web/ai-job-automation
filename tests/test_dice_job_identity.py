"""Dice canonical job identity: the dashboard job must be the job that gets applied to.

Proven from live dry-run artifacts: every stored Dice job_url was the SHARED /jobs?q=...
search-results URL, so Apply opened a results list and applied to whichever job was selected
there -- several dashboard rows opened the same wizard, and the opened title/company did not
match the row the user picked.

Root cause: inside a Dice job card there is no /job-detail/ href (the card link is an hrefless
overlay), so URL extraction fell back to the search URL. The authoritative identity is the
card's data-job-guid, and Dice's own embedded detailsPageUrl proves the /job-detail/<guid>
route. So a guid becomes a canonical URL only when Dice's own page data corroborates it;
anything else is URL_UNRESOLVED and is never applied to.
"""

import unittest
from pathlib import Path

from app.config import Settings
from app.models import ApplicationStatus, JobOpportunity
from app.services.find_matches import FindMatchesEngine, SearchVariation
from app.services.portals.dice_portal import DicePortalAdapter

GUID_A = "be2352e2-6429-495f-b28d-71f9bd9a60b2"
GUID_B = "50ef8f4f-2179-4b7a-8c2a-1ad0c22fbf74"
SEARCH_URL = (
    "https://www.dice.com/jobs?q=Healthcare+Data+Analyst&location=United+States"
    "&radius=30&radiusUnit=mi&page=1&pageSize=20&easyApply=true"
)


def _variation() -> SearchVariation:
    return SearchVariation(query="Data Analyst", location="United States", role="Data Analyst",
                           expanded_role="Data Analyst", pass_name="primary")


def _card(guid: str, corroborated: bool, title: str = "Data Analyst", company: str = "Acme") -> dict:
    # A real Dice card: no /job-detail/ href of its own, identity carried by data-job-guid.
    return {
        "job_guid": guid,
        "guid_corroborated": corroborated,
        "title": title,
        "company": company,
        "location": "Remote",
        "job_url": "",
        "apply_url": "",
        "apply_cta": "Easy Apply",
        "posted_date": "",
        "description": f"{title}\n{company}\nRemote",
    }


class _Page:
    """A Dice job-detail page at `url`, exposing its own data-job-guid."""

    def __init__(self, url: str, guid: str = "", title: str = "Data Analyst - Dice.com"):
        self.url = url
        self._guid = guid
        self._title = title
        self.interactions: list[str] = []

    def title(self) -> str:
        return self._title

    def evaluate(self, *_a, **_k) -> str:
        return self._guid

    def get_by_role(self, *_a, **_k):
        self.interactions.append("APPLY_LOOKUP")  # must never happen on a mismatch
        raise AssertionError("apply control was searched after an identity failure")

    def fill(self, *_a, **_k):
        self.interactions.append("FILL")
        raise AssertionError("form was filled after an identity failure")


class DiceCanonicalUrlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    # 1. A corroborated data-job-guid produces the canonical URL.
    def test_corroborated_guid_becomes_canonical_url(self) -> None:
        job = self.adapter.job_from_card_data(_card(GUID_A, True), _variation())
        self.assertEqual(job["job_url"], f"https://www.dice.com/job-detail/{GUID_A}")
        self.assertEqual(job["job_description_metadata"]["url_status"], "CANONICAL")
        self.assertEqual(job["job_description_metadata"]["job_guid"], GUID_A)

    # 2. Two different GUIDs produce two different canonical URLs.
    def test_two_guids_produce_two_distinct_canonical_urls(self) -> None:
        a = self.adapter.job_from_card_data(_card(GUID_A, True, title="A"), _variation())
        b = self.adapter.job_from_card_data(_card(GUID_B, True, title="B"), _variation())
        self.assertNotEqual(a["job_url"], b["job_url"])
        self.assertTrue(self.adapter.is_canonical_job_url(a["job_url"]))
        self.assertTrue(self.adapter.is_canonical_job_url(b["job_url"]))

    # 3. An uncorroborated GUID becomes URL_UNRESOLVED -- never a guessed route.
    def test_uncorroborated_guid_is_url_unresolved(self) -> None:
        job = self.adapter.job_from_card_data(_card(GUID_A, False), _variation())
        self.assertEqual(job["job_url"], "")
        self.assertEqual(job["job_description_metadata"]["url_status"], "URL_UNRESOLVED")

    # 4 + 5. Search-result URLs are never persisted, #job-<hash> is never generated.
    def test_never_produces_a_search_url_or_pseudo_url(self) -> None:
        engine = FindMatchesEngine(Settings(openai_api_key=None))
        for corroborated in (True, False):
            job = self.adapter.job_from_card_data(_card(GUID_A, corroborated), _variation())
            self.assertNotIn("/jobs?q=", job["job_url"])
            self.assertNotIn("#job-", job["job_url"])
            self.assertNotIn("#job-", engine.storage_job_url(job))
        # storage never invents identity for a shared URL either -- it persists verbatim.
        self.assertEqual(
            engine.storage_job_url({"job_url": SEARCH_URL, "title": "A", "company": "X", "location": "US"}),
            SEARCH_URL,
        )

    def test_unresolved_jobs_are_dropped_from_find_matches(self) -> None:
        class P:
            def evaluate(self, _script, _arg=None):
                return [_card(GUID_A, True, title="Good"), _card(GUID_B, False, title="Unresolved")]

        diag: dict = {}
        jobs = self.adapter._extract_jobs_from_dom(P(), _variation(), limit=10, diagnostics=diag)
        self.assertEqual([j["title"] for j in jobs], ["Good"])
        self.assertEqual(diag["parsed_with_real_job_detail_url"], 1)
        self.assertEqual(diag["url_unresolved_dropped"], 1)
        self.assertEqual(diag["parsed_with_search_url_fallback"], 0)

    def test_canonical_url_recogniser(self) -> None:
        self.assertTrue(self.adapter.is_canonical_job_url(f"https://www.dice.com/job-detail/{GUID_A}"))
        for bad in (SEARCH_URL, f"{SEARCH_URL}#job-d2c4cff049b8",
                    "https://www.dice.com/job-detail/abc", "https://evil.example/job-detail/" + GUID_A, ""):
            self.assertFalse(self.adapter.is_canonical_job_url(bad), bad)


class DiceIdentityGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def _job(self, url: str, title: str = "Data Analyst", company: str = "Acme") -> JobOpportunity:
        return JobOpportunity(id=1, resume_id=1, portal="dice", title=title, company=company,
                              location="Remote", job_url=url, description="")

    # 6. Existing poisoned rows are blocked BEFORE navigation.
    def test_poisoned_row_is_blocked_before_navigation(self) -> None:
        for url in (SEARCH_URL, f"{SEARCH_URL}#job-d2c4cff049b8", ""):
            with self.subTest(url=url):
                opened: list[str] = []
                self.adapter._apply_browser_context = lambda: opened.append("BROWSER")
                result = self.adapter.apply_to_job(
                    self._job(url), resume=None, candidate_profile=None,
                    dry_run=True, capture_evidence=False,
                )
                self.assertEqual(result.current_step, "URL_UNRESOLVED")
                self.assertEqual(result.status, ApplicationStatus.SKIPPED)
                self.assertEqual(opened, [], "no browser/navigation for an unresolved row")
                self.assertIn("Find Matches again", result.message)

    # 7. A canonical GUID match lets the Apply flow continue.
    def test_matching_guid_passes_the_gate(self) -> None:
        page = _Page(f"https://www.dice.com/job-detail/{GUID_A}", guid=GUID_A)
        ok, detail = self.adapter._verify_job_identity(page, self._job(f"https://www.dice.com/job-detail/{GUID_A}"), [])
        self.assertTrue(ok)
        self.assertEqual(detail["reason"], "IDENTITY_VERIFIED")
        self.assertEqual(detail["expected_guid"], GUID_A)

    # 8 + 9. A GUID mismatch returns JOB_IDENTITY_MISMATCH and touches nothing.
    def test_guid_mismatch_is_rejected_with_no_click_or_fill(self) -> None:
        page = _Page(f"https://www.dice.com/job-detail/{GUID_B}", guid=GUID_B)
        log: list[dict] = []
        ok, detail = self.adapter._verify_job_identity(page, self._job(f"https://www.dice.com/job-detail/{GUID_A}"), log)
        self.assertFalse(ok)
        self.assertEqual(detail["reason"], "JOB_IDENTITY_MISMATCH")
        self.assertEqual(detail["expected_guid"], GUID_A)
        self.assertEqual(detail["actual_guid"], GUID_B)
        self.assertEqual(page.interactions, [], "no apply click or fill on a mismatch")
        entry = [e for e in log if e["step"] == "job_identity_check"][0]
        self.assertFalse(entry["identity_matched"])
        for key in ("expected_guid", "actual_guid", "expected_url", "actual_url",
                    "expected_title", "expected_company", "actual_page_title"):
            self.assertIn(key, entry)

    # 10. Harmless title/company formatting differences are tolerated when the GUID matches.
    def test_title_formatting_differences_are_tolerated_when_guid_matches(self) -> None:
        page = _Page(f"https://www.dice.com/job-detail/{GUID_A}", guid=GUID_A,
                     title="  SENIOR   data-analyst!!  (CDP) | Dice.com ")
        job = self._job(f"https://www.dice.com/job-detail/{GUID_A}", title="Senior Data Analyst (CDP)")
        ok, detail = self.adapter._verify_job_identity(page, job, [])
        self.assertTrue(ok, "a guid match must not be overridden by cosmetic title differences")
        self.assertTrue(detail["title_corroborated"])

    def test_normalizer_ignores_case_space_and_punctuation(self) -> None:
        normalize = self.adapter._normalize_identity
        self.assertEqual(normalize("Senior  Data-Analyst!! (CDP)"), normalize("senior data analyst cdp"))

    # A wrong-title page with the RIGHT guid still passes; title never overrides the guid.
    def test_title_mismatch_does_not_override_a_matching_guid(self) -> None:
        page = _Page(f"https://www.dice.com/job-detail/{GUID_A}", guid=GUID_A, title="Something Else Entirely")
        ok, detail = self.adapter._verify_job_identity(page, self._job(f"https://www.dice.com/job-detail/{GUID_A}"), [])
        self.assertTrue(ok)
        self.assertFalse(detail["title_corroborated"])

    # Navigation targets the job's own canonical URL, never the card-derived apply_url.
    def test_apply_navigates_to_the_stored_canonical_url(self) -> None:
        job = self._job(f"https://www.dice.com/job-detail/{GUID_A}")
        job.job_description_metadata = {"apply_url": SEARCH_URL}
        self.assertEqual(self.adapter._apply_target_url(job), f"https://www.dice.com/job-detail/{GUID_A}")

    # 11 + 12. Apply/Easy Apply detection and LinkedIn are untouched by this change.
    def test_apply_detection_and_linkedin_unchanged(self) -> None:
        is_easy, reason = self.adapter._classify_dice_apply(
            card_text="Data Analyst\nAcme", cta_text="Easy Apply", apply_url=""
        )
        self.assertTrue(is_easy)
        self.assertTrue(reason.startswith("native_cta"))
        for path in ("app/services/linkedin.py", "app/services/portals/linkedin_portal.py"):
            source = Path(path).read_text(encoding="utf-8", errors="replace")
            self.assertNotIn("is_canonical_job_url", source)
            self.assertNotIn("JOB_IDENTITY_MISMATCH", source)
            self.assertNotIn("job_guid", source)


if __name__ == "__main__":
    unittest.main()
