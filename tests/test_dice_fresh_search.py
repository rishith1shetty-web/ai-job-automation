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
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def inner_text(self, timeout: int = 0):
        return ""


class FakeTwoPassPage:
    """Serves DIFFERENT cards for the fresh (last-24h) URL and the relevance URL, and records
    every navigation so pass ordering is observable."""

    def __init__(self, fresh_pages=None, relevance=None):
        self.fresh_pages = fresh_pages or []
        self.relevance = relevance or []
        self.url = "about:blank"
        self.goto_urls: list[str] = []

    def goto(self, url: str, *args, **kwargs):
        self.url = url
        self.goto_urls.append(url)

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

    def _current_cards(self) -> list[dict]:
        if "filters.postedDate=" in self.url:
            page_number = 1
            for part in self.url.split("&"):
                if part.startswith("page="):
                    page_number = int(part.split("=", 1)[1])
            index = page_number - 1
            return self.fresh_pages[index] if 0 <= index < len(self.fresh_pages) else []
        return self.relevance

    def evaluate(self, script, args=None):
        if not isinstance(args, dict):
            return 0
        return self._current_cards()[: args.get("scanLimit", 9999)]

    def locator(self, selector: str):
        return _FakeLocator()

    @property
    def fresh_gotos(self) -> list[str]:
        return [url for url in self.goto_urls if "filters.postedDate=" in url]

    @property
    def relevance_gotos(self) -> list[str]:
        return [url for url in self.goto_urls if "filters.postedDate=" not in url]


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

    def test_grouped_final_ordering_is_unchanged(self) -> None:
        # Ordering lives in the shared engine/queries, never in the Dice adapter's search passes.
        fresh_pass_source = DICE_SOURCE.split("def _run_fresh_pass")[1].split("def _run_relevance_pass")[0]
        for forbidden in ("freshness_score", "priority_score", "match_score", "variation_index"):
            self.assertNotIn(forbidden, fresh_pass_source)
        self.assertIn("freshness_score * 0.10", FIND_MATCHES_SOURCE)
        self.assertNotIn("filters.postedDate", FIND_MATCHES_SOURCE)


if __name__ == "__main__":
    unittest.main()
