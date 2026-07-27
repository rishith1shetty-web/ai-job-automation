"""Dice-only two-pass freshness retrieval (last-24h first, relevance fallback).

Pass A queries Dice's native last-24h facet -- confirmed live from a captured authenticated
search page's `postedDate` radios (NO_PREFERENCE|ONE|THREE|SEVEN, ONE == last 24 hours), using
Dice's `filters.<facetName>=` URL convention (corroborated live by filters.workplaceTypes and
filters.clientBrandNameFilter) -- across a BOUNDED number of pages, without early-stopping on
the eligible quota. Pass B is the existing relevance search, run only when Pass A supplies too
few eligible jobs. Both passes merge on canonical Dice identity, fresh copy winning.
"""

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote_plus

from app.config import Settings
from app.services.find_matches import SearchVariation
from app.services.portals.dice_portal import DicePortalAdapter

DICE_SOURCE = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
FIND_MATCHES_SOURCE = Path("app/services/find_matches.py").read_text(encoding="utf-8", errors="replace")


def _variation() -> SearchVariation:
    return SearchVariation(
        query="Data Analyst",
        location="Remote",
        role="Data Analyst",
        expanded_role="Data Analyst",
        pass_name="primary",
        search_role="Data Analyst",
        role_priority="primary",
    )


def _card(index: int, *, easy: bool = True, posted_iso: str = "", posted_date: str = "") -> dict:
    return {
        "job_guid": f"{index:08d}-2222-4333-8444-555555555555",
        "guid_corroborated": True,
        "title": f"Data Analyst {index}",
        "company": f"Company {index}",
        "location": "Remote",
        "job_url": "",
        "apply_url": "",
        "apply_cta": "Easy Apply" if easy else "Apply",
        "posted_iso": posted_iso,
        "posted_badge": posted_date,
        "posted_date": "",
        "description": f"Data Analyst {index}\nCompany {index}\nRemote",
    }


def _iso_ago(**kwargs) -> str:
    return (datetime.utcnow() - timedelta(**kwargs)).strftime("%Y-%m-%dT%H:%M:%SZ")


class _FakeLocator:
    def __init__(self, body_text: str = "") -> None:
        self.body_text = body_text

    @property
    def first(self):
        return self

    def count(self):
        return 0

    def inner_text(self, timeout: int = 0):
        return self.body_text


class FakeTwoPassPage:
    """Serves DIFFERENT cards for the fresh (last-24h) URL and the relevance URL, and records
    every navigation so pass ordering is observable."""

    def __init__(self, fresh_pages=None, relevance=None, no_results_from_fresh_page=None,
                 no_results_on_relevance=False, alias_results=None, sticky_posted_filter=False):
        self.fresh_pages = fresh_pages or []
        self.relevance = relevance or []
        # 1-based fresh page number that renders Dice's "No results found" end-of-results page.
        self.no_results_from_fresh_page = no_results_from_fresh_page
        self.no_results_on_relevance = no_results_on_relevance
        # query (lowercased) -> cards, for the bounded same-role alias retry.
        self.alias_results = alias_results or {}
        # Simulates Dice re-applying a persisted Today selection to an unfiltered request.
        self.sticky_posted_filter = sticky_posted_filter
        self.url = "about:blank"
        self.goto_urls: list[str] = []

    def goto(self, url: str, *args, **kwargs):
        self.goto_urls.append(url)
        if self.sticky_posted_filter and "filters.postedDate=" not in url:
            # Dice rewrites the URL after navigation; the rendered page keeps the old facet.
            url = f"{url}&filters.postedDate=ONE"
        self.url = url

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms: int):
        return None

    def screenshot(self, path: str, full_page: bool = True):
        Path(path).write_bytes(b"fake")

    def content(self):
        return "<html><body></body></html>"

    def title(self):
        return "Dice Jobs"

    def _query(self) -> str:
        for part in self.url.split("?", 1)[-1].split("&"):
            if part.startswith("q="):
                # unquote_plus, so an encoded "FP%26A+Analyst" reads back as "fp&a analyst".
                return unquote_plus(part.split("=", 1)[1]).lower()
        return ""

    def _page_number(self) -> int:
        for part in self.url.split("&"):
            if part.startswith("page="):
                return int(part.split("=", 1)[1])
        return 1

    def _is_fresh_url(self) -> bool:
        return "filters.postedDate=ONE" in self.url

    def _current_cards(self) -> list[dict]:
        if self._is_fresh_url():
            index = self._page_number() - 1
            return self.fresh_pages[index] if 0 <= index < len(self.fresh_pages) else []
        alias = self.alias_results.get(self._query())
        if alias is not None:
            return alias
        return self.relevance

    def _shows_no_results(self) -> bool:
        if self._is_fresh_url():
            return self.no_results_from_fresh_page is not None and \
                self._page_number() >= self.no_results_from_fresh_page
        if self.alias_results.get(self._query()):
            return False
        return self.no_results_on_relevance

    def evaluate(self, script, args=None):
        if not isinstance(args, dict):
            return 0
        if self._shows_no_results():
            return []
        return self._current_cards()[: args.get("scanLimit", 9999)]

    def locator(self, selector: str):
        return _FakeLocator("No results found" if self._shows_no_results() else "")

    @property
    def fresh_gotos(self) -> list[str]:
        return [url for url in self.goto_urls if "filters.postedDate=ONE" in url]

    @property
    def relevance_gotos(self) -> list[str]:
        return [url for url in self.goto_urls if "filters.postedDate=ONE" not in url]


class DiceFreshSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.debug_dir = Path(tempfile.mkdtemp(prefix="dice-fresh-"))
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def tearDown(self) -> None:
        shutil.rmtree(self.debug_dir, ignore_errors=True)

    def _search(self, page, limit: int, search_index: int = 1) -> list[dict]:
        return self.adapter.search_jobs(
            page, _variation(), limit=limit, debug_dir=self.debug_dir, search_index=search_index
        )

    def _diagnostics(self, search_index: int = 1) -> dict:
        return json.loads(
            (self.debug_dir / f"search_{search_index}_diagnostics.json").read_text(encoding="utf-8")
        )

    def test_fresh_pass_runs_before_relevance_fallback(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[_card(1, posted_iso=_iso_ago(minutes=20))]],
            relevance=[_card(50, posted_date="5 days ago")],
        )

        self._search(page, limit=5)

        self.assertIn("filters.postedDate=ONE", page.goto_urls[0])
        first_relevance = next(i for i, u in enumerate(page.goto_urls) if "filters.postedDate=" not in u)
        self.assertGreater(first_relevance, 0)

    def test_fallback_runs_only_when_fresh_supply_is_insufficient(self) -> None:
        # Sufficient fresh supply -> no relevance pass at all.
        enough = FakeTwoPassPage(
            fresh_pages=[[_card(i, posted_iso=_iso_ago(minutes=10 * i)) for i in range(1, 6)]],
            relevance=[_card(90, posted_date="9 days ago")],
        )
        jobs = self._search(enough, limit=5, search_index=1)
        self.assertEqual(len(jobs), 5)
        self.assertEqual(enough.relevance_gotos, [])
        self.assertFalse(self._diagnostics(1)["fallback_pass_ran"])

        # Insufficient fresh supply -> relevance pass fills the remainder, fresh job still leads.
        short = FakeTwoPassPage(
            fresh_pages=[[_card(1, posted_iso=_iso_ago(minutes=30))]],
            relevance=[_card(50 + i, posted_date="5 days ago") for i in range(4)],
        )
        jobs = self._search(short, limit=5, search_index=2)
        self.assertTrue(short.relevance_gotos)
        self.assertTrue(self._diagnostics(2)["fallback_pass_ran"])
        self.assertEqual(jobs[0]["title"], "Data Analyst 1")
        self.assertEqual(len(jobs), 5)

    def test_fresh_pass_is_bounded_by_page_limit(self) -> None:
        # Every page yields NEW unique cards, so only the page cap can stop the loop.
        page = FakeTwoPassPage(
            fresh_pages=[
                [_card(p * 100 + i, posted_iso=_iso_ago(minutes=i)) for i in range(3)]
                for p in range(1, 9)
            ]
        )

        self._search(page, limit=50)

        self.assertLessEqual(len(page.fresh_gotos), self.adapter.FRESH_MAX_PAGES)
        self.assertLessEqual(self.adapter.FRESH_MAX_PAGES, 3)
        self.assertEqual(self._diagnostics()["fresh_pass_stop_reason"], "max_pages")
        self.assertNotIn("while True", DICE_SOURCE)

    def test_no_results_on_a_later_fresh_page_keeps_the_jobs_already_collected(self) -> None:
        """The live defect: pages 1-2 returned real Today jobs, page 3 was Dice's end-of-results
        page, and the whole variation returned zero. 62 real cards were discarded."""
        page = FakeTwoPassPage(
            fresh_pages=[
                [_card(i, posted_iso=_iso_ago(minutes=i)) for i in range(1, 35)],
                [_card(100 + i, posted_iso=_iso_ago(minutes=i)) for i in range(1, 29)],
            ],
            no_results_from_fresh_page=3,
            relevance=[_card(500, posted_date="8 days ago")],
        )

        jobs = self._search(page, limit=22)

        self.assertEqual(len(jobs), 22, "jobs collected before the end-of-results page were lost")
        diag = self._diagnostics()
        self.assertIsNone(diag["login_or_challenge_detected"])
        self.assertEqual(diag["fresh_pass_stop_reason"], "no_more_fresh_results")

    def test_zero_fresh_cards_triggers_the_older_relevance_fallback(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            relevance=[_card(200 + i, posted_date="6 days ago") for i in range(5)],
        )

        jobs = self._search(page, limit=5)

        self.assertTrue(self._diagnostics()["fallback_pass_ran"])
        self.assertEqual(len(jobs), 5)

    def test_no_results_found_page_triggers_fallback_instead_of_ending_the_variation(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[_card(300 + i, posted_date="10 days ago") for i in range(4)],
        )

        jobs = self._search(page, limit=6)

        diag = self._diagnostics()
        self.assertIsNone(diag["login_or_challenge_detected"], "end-of-results is not a blocker")
        self.assertTrue(diag["fallback_pass_ran"])
        self.assertTrue(page.relevance_gotos)
        self.assertEqual(len(jobs), 4, "older jobs must be accepted when nothing was posted today")

    def test_fallback_url_never_carries_the_posted_date_filter(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[_card(400, posted_date="3 days ago")],
        )

        self._search(page, limit=5)

        self.assertTrue(page.relevance_gotos)
        for url in page.relevance_gotos:
            self.assertNotIn("filters.postedDate=ONE", url)

    def test_a_sticky_today_filter_is_cleared_before_counting_fallback_results(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[_card(410 + i, posted_date="4 days ago") for i in range(3)],
            sticky_posted_filter=True,
        )

        jobs = self._search(page, limit=5)

        diag = self._diagnostics()
        self.assertTrue(diag["fallback_posted_filter_cleared"])
        self.assertTrue(any("NO_PREFERENCE" in url for url in page.goto_urls))
        self.assertEqual(len(jobs), 3)

    def test_fallback_may_return_older_jobs(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[_card(600, posted_date="30 days ago")],
        )

        jobs = self._search(page, limit=5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(self._diagnostics()["jobs_within_24h"], 0)

    def test_easy_apply_is_still_mandatory_in_the_fallback(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[
                _card(700, easy=False, posted_date="5 days ago"),
                _card(701, easy=True, posted_date="5 days ago"),
                _card(702, easy=False, posted_date="5 days ago"),
            ],
        )

        jobs = self._search(page, limit=5)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Data Analyst 701")
        self.assertTrue(all(job["easy_apply"] for job in jobs))

    def test_a_zero_variation_does_not_stop_later_variations(self) -> None:
        empty = FakeTwoPassPage(fresh_pages=[[]], no_results_from_fresh_page=1,
                                no_results_on_relevance=True)
        self.assertEqual(self._search(empty, limit=5, search_index=1), [])

        productive = FakeTwoPassPage(
            fresh_pages=[[_card(800 + i, posted_iso=_iso_ago(minutes=5)) for i in range(3)]]
        )
        self.assertEqual(len(self._search(productive, limit=5, search_index=2)), 3)

    def test_a_genuine_blocker_keeps_what_was_already_collected(self) -> None:
        class BlockedPage(FakeTwoPassPage):
            def locator(self, selector: str):
                # Page 2 of the fresh pass hits a captcha wall.
                if self._page_number() >= 2:
                    return _FakeLocator("Please verify you are human")
                return _FakeLocator("")

        page = BlockedPage(
            fresh_pages=[[_card(900 + i, posted_iso=_iso_ago(minutes=i)) for i in range(1, 5)]] * 3
        )

        jobs = self._search(page, limit=10)

        self.assertEqual(self._diagnostics()["login_or_challenge_detected"], "captcha_or_blocked")
        self.assertEqual(len(jobs), 4, "a captcha must not erase jobs already extracted")

    def test_bounded_same_role_alias_retry_only_when_everything_else_is_empty(self) -> None:
        variation = SearchVariation(
            query="Financial Planning and Analysis Analyst", location="Remote",
            role="FP&A Analyst", expanded_role="FP&A Analyst", pass_name="primary",
            search_role="FP&A Analyst", role_priority="primary",
            synonyms=("FP&A Analyst", "Financial Planning Analyst"),
        )
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            no_results_on_relevance=True,
            alias_results={"fp&a analyst": [_card(950 + i, posted_date="7 days ago") for i in range(3)]},
        )

        jobs = self.adapter.search_jobs(page, variation, limit=5, debug_dir=self.debug_dir, search_index=1)

        diag = self._diagnostics()
        self.assertEqual(diag["alias_queries_tried"], ["FP&A Analyst"])
        self.assertEqual(len(jobs), 3)
        self.assertLessEqual(len(diag["alias_queries_tried"]), self.adapter.MAX_QUERY_ALIASES)

    def test_alias_retry_never_runs_when_the_title_already_found_jobs(self) -> None:
        variation = SearchVariation(
            query="Financial Planning and Analysis Analyst", location="Remote",
            role="FP&A Analyst", expanded_role="FP&A Analyst", pass_name="primary",
            search_role="FP&A Analyst", role_priority="primary",
            synonyms=("FP&A Analyst", "Financial Planning Analyst"),
        )
        page = FakeTwoPassPage(
            fresh_pages=[[_card(960 + i, posted_iso=_iso_ago(minutes=i)) for i in range(1, 4)]],
            no_results_from_fresh_page=2,
            alias_results={"fp&a analyst": [_card(970, posted_date="7 days ago")]},
        )

        self.adapter.search_jobs(page, variation, limit=3, debug_dir=self.debug_dir, search_index=1)

        self.assertEqual(self._diagnostics()["alias_queries_tried"], [])

    def test_alias_retry_is_bounded_and_uses_only_registered_synonyms(self) -> None:
        variation = SearchVariation(
            query="Nonexistent Role Title", location="Remote", role="R", expanded_role="R",
            pass_name="primary", search_role="R", role_priority="primary",
            synonyms=("Alias One", "Alias Two", "Alias Three", "Alias Four"),
        )
        page = FakeTwoPassPage(fresh_pages=[[]], no_results_from_fresh_page=1,
                               no_results_on_relevance=True)

        self.adapter.search_jobs(page, variation, limit=5, debug_dir=self.debug_dir, search_index=1)

        tried = self._diagnostics()["alias_queries_tried"]
        self.assertEqual(tried, ["Alias One", "Alias Two"])
        self.assertLessEqual(len(tried), self.adapter.MAX_QUERY_ALIASES)

    def test_no_alias_synonyms_means_no_extra_queries(self) -> None:
        page = FakeTwoPassPage(fresh_pages=[[]], no_results_from_fresh_page=1,
                               no_results_on_relevance=True)
        self._search(page, limit=5)
        self.assertEqual(self._diagnostics()["alias_queries_tried"], [])

    def test_progress_event_reports_fresh_and_fallback_separately(self) -> None:
        page = FakeTwoPassPage(
            fresh_pages=[[]],
            no_results_from_fresh_page=1,
            relevance=[_card(980 + i, posted_date="5 days ago") for i in range(2)],
        )
        self._search(page, limit=5)
        breakdown = self.adapter._search_pass_breakdown()
        self.assertEqual(breakdown["fresh_eligible"], 0)
        self.assertTrue(breakdown["fallback_pass_ran"])
        self.assertEqual(breakdown["fallback_eligible"], 2)

    def test_grouped_final_ordering_is_unchanged(self) -> None:
        # Ordering lives in the shared engine/queries, never in the Dice adapter's search passes.
        fresh_pass_source = DICE_SOURCE.split("def _run_fresh_pass")[1].split("def _run_relevance_pass")[0]
        for forbidden in ("freshness_score", "priority_score", "match_score", "variation_index"):
            self.assertNotIn(forbidden, fresh_pass_source)
        self.assertIn("freshness_score * 0.10", FIND_MATCHES_SOURCE)
        self.assertNotIn("filters.postedDate", FIND_MATCHES_SOURCE)


if __name__ == "__main__":
    unittest.main()
