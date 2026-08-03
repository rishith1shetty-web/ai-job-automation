"""Regression tests for the Dice Find Matches card-scan limitation.

Root cause (proven from a real authenticated run + loaded HTML):
    result_card_containers_found: ~112   (INFLATED: [data-testid*='job-card'] substring-matches
                                          nested per-card sub-elements -- job-card-company-name,
                                          job-card-save-btn, the hrefless job-search-job-card-link)
    unique real job cards:          34   (data-testid='job-card' == data-job-guid == data-id)
    cards_scanned_by_extractor:      7   <-- only the first `limit` cards were ever inspected

The extractor capped the DOM scan at `limit` (== FindMatchesEngine.collection_plan's
limit_per_query == 7 for Find 20). Because Dice's list is Easy Apply-filtered client-side and
most scanned cards are dropped, capping the scan at the return target starved the pipeline
(7 scanned -> ~2 kept), so Find 20/50/100 could never reach their requested totals.

Fix (Dice-only): the scan cap (`_dice_scan_limit`) is decoupled from and larger than the
per-query eligible-return target; nested sub-element matches are collapsed onto their real
job-card (canonicalOf + data-job-guid/data-id dedup) so one job is never scanned as several;
scanning stops early once enough eligible jobs are collected. Easy Apply classification and
LinkedIn are untouched.
"""

import re
import unittest
from pathlib import Path

from app.config import Settings
from app.services.find_matches import FindMatchesEngine, SearchVariation
from app.services.portals.dice_portal import DicePortalAdapter

DICE_SOURCE = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
LINKEDIN_SOURCE = Path("app/services/linkedin.py").read_text(encoding="utf-8", errors="replace")
LINKEDIN_PORTAL_SOURCE = Path("app/services/portals/linkedin_portal.py").read_text(
    encoding="utf-8", errors="replace"
)


def _variation() -> SearchVariation:
    return SearchVariation(
        query="Data Analyst",
        location="United States",
        role="Data Analyst",
        expanded_role="Data Analyst",
        pass_name="primary",
    )


def _card(i: int, easy: bool) -> dict:
    # A real Dice card carries no /job-detail/ href of its own; its corroborated
    # data-job-guid is what yields the canonical job URL.
    return {
        "job_guid": f"{i:08d}-2222-4333-8444-555555555555",
        "guid_corroborated": True,
        "title": f"Data Analyst {i}",
        "company": f"Company {i}",
        "location": "Remote",
        "job_url": "",
        "apply_url": "",
        # Easy Apply is decided ONLY by the CTA badge text -- unchanged classification path.
        "apply_cta": "Easy Apply" if easy else "",
        "posted_date": "",
        "description": f"Data Analyst {i}\nCompany {i}\nRemote",
    }


class FakeEvaluatePage:
    """Stands in for a Playwright page. `evaluate` returns preset card dicts, honouring the
    scanLimit the extractor passes (as the real JS does) so scan-cap behaviour is observable."""

    def __init__(self, cards: list[dict]):
        self._cards = cards
        self.last_arg: dict | None = None

    def evaluate(self, script, arg=None):
        self.last_arg = arg
        scan_limit = (arg or {}).get("scanLimit", len(self._cards))
        return self._cards[:scan_limit]


class DiceScanLimitAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    # 1. Find 20 is not limited to 7 cards.
    def test_find20_scans_past_seven_cards_to_find_eligible(self) -> None:
        # Sparse eligibility: every 4th card is Easy Apply. Return target 7 (Find 20's
        # per-query share). The extractor must inspect well past 7 cards to collect 7 eligible.
        cards = [_card(i, easy=((i + 1) % 4 == 0)) for i in range(40)]
        page = FakeEvaluatePage(cards)
        diag: dict = {}
        jobs = self.adapter._extract_jobs_from_dom(page, _variation(), limit=7, diagnostics=diag)

        self.assertEqual(page.last_arg["scanLimit"], self.adapter._dice_scan_limit(7))
        self.assertGreater(diag["cards_scanned_by_extractor"], 7)
        self.assertEqual(diag["cumulative_eligible_jobs"], 7)
        self.assertEqual(diag["scan_stop_reason"], "eligible_target_reached")
        self.assertGreaterEqual(sum(1 for j in jobs if j.get("easy_apply")), 7)

    # 2. Find 50 scans more cards than Find 20 when needed.
    def test_find50_scan_target_exceeds_find20(self) -> None:
        # Per-query return targets from collection_plan: Find 20 -> 7, Find 50 -> 9.
        find20_per_query = self.engine.collection_plan(20, 24)["limit_per_query"]
        find50_per_query = self.engine.collection_plan(50, 24)["limit_per_query"]
        self.assertLess(find20_per_query, find50_per_query)
        self.assertGreater(
            self.adapter._dice_scan_limit(find50_per_query),
            self.adapter._dice_scan_limit(find20_per_query),
        )
        # And the larger scan target is actually applied by the extractor.
        page = FakeEvaluatePage([_card(i, easy=False) for i in range(80)])
        self.adapter._extract_jobs_from_dom(page, _variation(), limit=find50_per_query, diagnostics={})
        self.assertEqual(page.last_arg["scanLimit"], self.adapter._dice_scan_limit(find50_per_query))

    # 3. Find 100 can continue across queries when enough jobs exist.
    def test_find100_allocates_enough_queries_to_reach_target(self) -> None:
        plan20 = self.engine.collection_plan(20, 24)
        plan100 = self.engine.collection_plan(100, 24)
        # Both presets search the SAME top-five roles; the larger request goes deeper per query
        # instead of adding roles (it used to add them, which changed what was searched).
        self.assertEqual(plan100["selected_variation_count"], plan20["selected_variation_count"])
        self.assertGreater(plan100["limit_per_query"], plan20["limit_per_query"])
        # Total per-query capacity across the selected queries must still be able to reach 100.
        capacity = plan100["selected_variation_count"] * plan100["limit_per_query"]
        self.assertGreaterEqual(capacity, 100)

    # 4. Duplicate DOM representations are not double-counted.
    def test_extractor_collapses_nested_duplicate_representations(self) -> None:
        # The over-broad [data-testid*='job-card'] selector also matches per-card children; the
        # JS must collapse them onto the real card (closest) and dedup by the stable job identity.
        self.assertIn("canonicalOf", DICE_SOURCE)
        self.assertIn("seenKeys", DICE_SOURCE)
        self.assertRegex(DICE_SOURCE, r"closest\(\s*\"\[data-testid='job-card'\]")
        self.assertIn("data-job-guid", DICE_SOURCE)
        self.assertIn("data-id", DICE_SOURCE)

    # 5. Scanning stops after enough unique eligible jobs are collected.
    def test_scan_stops_once_enough_eligible_collected(self) -> None:
        cards = [_card(i, easy=True) for i in range(40)]  # every card eligible
        page = FakeEvaluatePage(cards)
        diag: dict = {}
        self.adapter._extract_jobs_from_dom(page, _variation(), limit=7, diagnostics=diag)
        self.assertEqual(diag["cards_scanned_by_extractor"], 7)
        self.assertEqual(diag["cumulative_eligible_jobs"], 7)
        self.assertEqual(diag["scan_stop_reason"], "eligible_target_reached")

    def test_scan_is_capped_and_reports_exhausted_when_eligibles_are_scarce(self) -> None:
        # 100 cards, none eligible, return target 7 -> scan stops at the scan cap (not 7, not 100),
        # and reports why it stopped so a starved page is diagnosable.
        cards = [_card(i, easy=False) for i in range(100)]
        page = FakeEvaluatePage(cards)
        diag: dict = {}
        self.adapter._extract_jobs_from_dom(page, _variation(), limit=7, diagnostics=diag)
        self.assertEqual(diag["cards_scanned_by_extractor"], self.adapter._dice_scan_limit(7))
        self.assertEqual(diag["cumulative_eligible_jobs"], 0)
        self.assertEqual(diag["scan_stop_reason"], "cards_exhausted")

    def test_scan_diagnostics_keys_present(self) -> None:
        page = FakeEvaluatePage([_card(i, easy=True) for i in range(10)])
        diag: dict = {}
        self.adapter._extract_jobs_from_dom(page, _variation(), limit=5, diagnostics=diag)
        for key in (
            "per_query_scan_target",
            "per_query_return_target",
            "unique_candidate_cards",
            "cards_scanned_by_extractor",
            "scan_stop_reason",
            "cumulative_eligible_jobs",
        ):
            self.assertIn(key, diag)

    # 6. Easy Apply detection remains unchanged.
    def test_easy_apply_classification_unchanged(self) -> None:
        is_easy, reason = self.adapter._classify_dice_apply(
            card_text="Data Analyst\nAcme", cta_text="Easy Apply", apply_url=""
        )
        self.assertTrue(is_easy)
        self.assertTrue(reason.startswith("native_cta"))
        is_easy, reason = self.adapter._classify_dice_apply(
            card_text="Data Analyst\nAcme", cta_text="", apply_url=""
        )
        self.assertFalse(is_easy)
        self.assertEqual(reason, "no_easy_apply_signal")

    # 7. LinkedIn remains unchanged.
    def test_scan_allocation_is_dice_only(self) -> None:
        # The shared collection_plan (which LinkedIn also consumes) keeps its exact values.
        self.assertEqual(self.engine.collection_plan(20, 24), {"selected_variation_count": 5, "limit_per_query": 6})
        # The new scan machinery lives only in the Dice portal.
        self.assertIn("_dice_scan_limit", DICE_SOURCE)
        for source in (LINKEDIN_SOURCE, LINKEDIN_PORTAL_SOURCE):
            self.assertNotIn("_dice_scan_limit", source)
            self.assertNotIn("DICE_SCAN", source)
            self.assertNotIn("scanLimit", source)


if __name__ == "__main__":
    unittest.main()
