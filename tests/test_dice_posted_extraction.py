"""Dice posting-date extraction: each job's OWN "Posted <age>" phrase, never "Updated",
never Similar-Jobs ages, never prose day-words.

Live-validation defect: the old extraction regex-scanned whole card text with an OPTIONAL
"posted" prefix, so "Updated 1 day ago" matched as "1 day ago" and prose like "apply today"
became a false "Today" -- which is why the Dice dashboard showed repeated wrong values and
the batch was not truly newest-to-oldest.

The rule now (DicePortalAdapter.extract_dice_posted_phrase):
  1. a Posted/Reposted-prefixed phrase in any source is authoritative;
  2. otherwise only STRONG bare forms ("just now", "N units ago") not preceded by "Updated";
  3. bare "today"/"yesterday" without the prefix are rejected; nothing matched -> Unknown.
The engine's loose text rescue no longer runs when the adapter supplied a posted_date field.
"""

import unittest
from datetime import datetime, timedelta

from app.config import Settings
from app.services.find_matches import FindMatchesEngine, SearchVariation
from app.services.portals.dice_portal import DicePortalAdapter
from app.services.posting_time import UNKNOWN_FRESHNESS

REF = datetime(2026, 7, 17, 14, 30, 0)
GUID_A = "be2352e2-6429-495f-b28d-71f9bd9a60b2"
GUID_B = "50ef8f4f-2179-4b7a-8c2a-1ad0c22fbf74"
DETAIL_HEADER = "Hybrid in Troy, MI, US · Posted 2 days ago · Updated 1 day ago"


def _variation() -> SearchVariation:
    return SearchVariation(query="Data Analyst", location="United States", role="Data Analyst",
                           expanded_role="Data Analyst", pass_name="primary")


def _card(guid: str, posted: str, title: str = "Data Analyst", description: str = "") -> dict:
    return {
        "job_guid": guid,
        "guid_corroborated": True,
        "title": title,
        "company": "Acme",
        "location": "Remote",
        "job_url": "",
        "apply_url": "",
        "apply_cta": "Easy Apply",
        "posted_date": posted,
        "description": description or f"{title}\nAcme\nRemote",
    }


class DicePostedPhraseExtractorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    # 1. The exact live example: Posted wins, Updated is cut off.
    def test_posted_and_updated_header_extracts_only_posted(self) -> None:
        self.assertEqual(self.adapter.extract_dice_posted_phrase(DETAIL_HEADER), "Posted 2 days ago")

    # 2. Updated alone never becomes a posting age.
    def test_updated_alone_is_rejected(self) -> None:
        for text in ("Updated 1 day ago", "Updated 10 minutes ago", "Updated today"):
            with self.subTest(text=text):
                self.assertEqual(self.adapter.extract_dice_posted_phrase(text), "")

    # 3 + 4. Similar-Jobs style ages never contaminate the main job's value.
    def test_similar_jobs_ages_do_not_contaminate(self) -> None:
        similar = "Similar Jobs\nData Engineer\nToday\nAnalyst II\nYesterday\nBI Lead\n5 days ago"
        # Main header present -> it wins even when the noisy text comes first.
        self.assertEqual(
            self.adapter.extract_dice_posted_phrase(similar + "\n" + DETAIL_HEADER),
            "Posted 2 days ago",
        )
        self.assertEqual(
            self.adapter.extract_dice_posted_phrase("", similar + "\n" + DETAIL_HEADER),
            "Posted 2 days ago",
        )

    # Bare prose day-words are the proven false-"Today" source -> rejected.
    def test_bare_prose_today_is_rejected(self) -> None:
        for text in ("Apply today and join our team", "today", "Yesterday's tools, tomorrow's ideas"):
            with self.subTest(text=text):
                self.assertEqual(self.adapter.extract_dice_posted_phrase(text), "")
        # With the Posted prefix the same words are trusted.
        self.assertEqual(self.adapter.extract_dice_posted_phrase("Posted Today"), "Posted Today")
        self.assertEqual(self.adapter.extract_dice_posted_phrase("Posted yesterday"), "Posted yesterday")
        self.assertEqual(self.adapter.extract_dice_posted_phrase("Reposted 2 weeks ago"), "Reposted 2 weeks ago")

    # Strong bare forms still work (many Dice cards omit the "Posted" word).
    def test_strong_bare_forms_are_accepted(self) -> None:
        self.assertEqual(self.adapter.extract_dice_posted_phrase("7 minutes ago"), "7 minutes ago")
        self.assertEqual(self.adapter.extract_dice_posted_phrase("just now"), "just now")
        self.assertEqual(self.adapter.extract_dice_posted_phrase("30+ days ago"), "30+ days ago")

    # 8. A precise Posted value is never overwritten by a weaker bare value elsewhere.
    def test_prefixed_value_beats_bare_value_in_any_source(self) -> None:
        self.assertEqual(
            self.adapter.extract_dice_posted_phrase("3 days ago", "Posted 2 minutes ago"),
            "Posted 2 minutes ago",
        )

    # 7. A reliable later source fills in when the first source has nothing valid.
    def test_later_source_fills_absent_or_invalid_first_source(self) -> None:
        self.assertEqual(
            self.adapter.extract_dice_posted_phrase("", "Posted 2 days ago"), "Posted 2 days ago"
        )
        self.assertEqual(
            self.adapter.extract_dice_posted_phrase("Updated 1 day ago", "Posted 2 days ago"),
            "Posted 2 days ago",
        )

    # 9. Nothing reliable -> Unknown.
    def test_missing_values_stay_unknown(self) -> None:
        self.assertEqual(self.adapter.extract_dice_posted_phrase("", "  ", "great benefits"), "")


class DiceCardPostedCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    # 1 (metadata path) -- the posted element's contextual text yields only the Posted phrase.
    def test_card_metadata_keeps_posted_not_updated(self) -> None:
        job = self.adapter.job_from_card_data(
            _card(GUID_A, "Posted 2 days ago · Updated 1 day ago"), _variation()
        )
        self.assertEqual(job["job_description_metadata"]["posted_date"], "Posted 2 days ago")

    # 5. Each canonical GUID keeps ITS OWN card's phrase.
    def test_extraction_is_tied_to_each_cards_guid(self) -> None:
        class P:
            def evaluate(self, _script, _arg=None):
                return [
                    _card(GUID_A, "Posted 2 days ago · Updated 1 day ago", title="Job A"),
                    _card(GUID_B, "Posted 4 hours ago", title="Job B"),
                ]

        jobs = self.adapter._extract_jobs_from_dom(P(), _variation(), limit=10, diagnostics={})
        by_guid = {job["job_description_metadata"]["job_guid"]: job for job in jobs}
        self.assertEqual(by_guid[GUID_A]["job_description_metadata"]["posted_date"], "Posted 2 days ago")
        self.assertEqual(by_guid[GUID_B]["job_description_metadata"]["posted_date"], "Posted 4 hours ago")

    # 2 (normalization path) + 12. Updated never becomes posted_at, and ranking freshness
    # follows Posted: A posted 2d/updated 10min vs B posted 1d/updated 5d -> B is fresher.
    def test_updated_never_reaches_posted_at_or_ranking(self) -> None:
        job_a = self.engine.enrich_raw_job(
            self.adapter.job_from_card_data(
                _card(GUID_A, "Posted 2 days ago · Updated 10 minutes ago", title="Job A"),
                _variation(),
            ),
            _variation(),
            reference_time=REF,
        )
        job_b = self.engine.enrich_raw_job(
            self.adapter.job_from_card_data(
                _card(GUID_B, "Posted 1 day ago · Updated 5 days ago", title="Job B"),
                _variation(),
            ),
            _variation(),
            reference_time=REF,
        )
        self.assertEqual(job_a["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=2)).isoformat())
        self.assertEqual(job_b["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=1)).isoformat())
        # B (posted 1 day) outranks A (posted 2 days) although A was UPDATED 10 minutes ago.
        self.assertGreater(job_b["freshness_score"], job_a["freshness_score"])

    # The engine no longer free-scans Dice text: an adapter Unknown stays Unknown even when
    # the description contains prose day-words.
    def test_engine_rescue_is_skipped_when_adapter_supplied_the_field(self) -> None:
        raw = self.adapter.job_from_card_data(
            _card(GUID_A, "", description="Data Analyst\nAcme\nApply today and join us"),
            _variation(),
        )
        enriched = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)
        self.assertEqual(enriched["job_description_metadata"]["posted_date"], "")
        self.assertIsNone(enriched["job_description_metadata"]["posted_at"])
        self.assertEqual(enriched["freshness_score"], UNKNOWN_FRESHNESS)

    # ...while a raw job that never attempted extraction still gets the legacy rescue.
    def test_engine_rescue_still_runs_without_an_adapter_verdict(self) -> None:
        raw = {"portal": "dice", "title": "T", "company": "C", "location": "Remote",
               "description": "T\nC\nPosted 2 days ago", "job_url": "", "easy_apply": True}
        enriched = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)
        self.assertEqual(enriched["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=2)).isoformat())

    # The in-page scanner harvests exact per-guid postedDate timestamps from the embedded
    # payload (never modifiedDate -- the "Updated" field) plus the card's own whole-node age
    # badge. LinkedIn is untouched.
    def test_scanner_scope_and_linkedin_isolation(self) -> None:
        from pathlib import Path

        dice_source = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn("postedByGuid", dice_source)
        self.assertIn("postedDate", dice_source)
        self.assertIn("posted_badge", dice_source)
        self.assertNotIn("modifiedDate' +", dice_source)  # never harvested as a posting time
        self.assertNotIn("postedMatch", dice_source)
        linkedin_source = Path("app/services/linkedin.py").read_text(encoding="utf-8", errors="replace")
        self.assertNotIn("extract_dice_posted_phrase", linkedin_source)
        self.assertNotIn("postedByGuid", linkedin_source)


class DiceExactTimestampTests(unittest.TestCase):
    """The authoritative source proven from the live authenticated session: Dice's embedded
    search payload carries an exact per-guid postedDate ISO timestamp (modifiedDate is the
    separate 'Updated' field and is never read). The card's visible badge (often a coarse
    'Today') is kept for DISPLAY; the exact timestamp drives posted_at and ranking."""

    FIXTURE = "tests/fixtures/dice_search_payload_sample.html"
    # Real captured pair from card a9fe86a5... in the live run: badge said "Today",
    # payload postedDate was exact.
    REAL_ISO = "2026-07-17T10:16:41Z"

    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _job(self, *, iso: str = "", badge: str = "", posted: str = "") -> dict:
        card = _card(GUID_A, posted)
        card["posted_iso"] = iso
        card["posted_badge"] = badge
        raw = self.adapter.job_from_card_data(card, _variation())
        return self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)

    # 1 + 6. The real captured structure yields a posted value with full precision.
    def test_real_fixture_binds_exact_posted_date_to_each_guid(self) -> None:
        import re
        from pathlib import Path

        fixture = Path(self.FIXTURE).read_text(encoding="utf-8", errors="replace")
        bs = "\\\\"
        guid_re = re.compile(bs + '"guid' + bs + '":' + bs + '"([0-9a-f-]{36})' + bs + '"')
        posted_re = re.compile(bs + '"postedDate' + bs + '":' + bs + '"([0-9T:.Z-]+)')
        hits = list(guid_re.finditer(fixture))
        self.assertGreaterEqual(len(hits), 3, "fixture must keep the real payload segments")
        for i, m in enumerate(hits):
            # Mirror the production segment window (guidHits[i].start + 3000) so the last
            # job's postedDate -- which sits ~690 chars past its guid -- is fully in range.
            seg = fixture[m.start(): hits[i + 1].start() if i + 1 < len(hits) else m.start() + 3000]
            posted = posted_re.search(seg)
            self.assertIsNotNone(posted, f"guid {m.group(1)} lost its postedDate")
            # The captured timestamps normalize with full (second-level) precision.
            parsed = self.engine.enrich_raw_job(
                {"portal": "dice", "title": "T", "company": "C", "location": "US",
                 "description": "T", "job_url": "", "easy_apply": True,
                 "job_description_metadata": {"posted_date": "", "posted_iso": posted.group(1)}},
                _variation(), reference_time=REF,
            )
            self.assertIsNotNone(parsed["job_description_metadata"]["posted_at"])
            self.assertNotEqual(parsed["job_description_metadata"]["posted_at"][-8:], "00:00:00")

    # The card's visible whole-node badge exists in the real card fragment.
    def test_real_card_fragment_contains_whole_node_badge(self) -> None:
        from pathlib import Path

        fixture = Path(self.FIXTURE).read_text(encoding="utf-8", errors="replace")
        self.assertIn('data-job-guid="a9fe86a5-635a-4661-8ed7-a0e768e71cc2"', fixture)
        self.assertIn(">Today<", fixture)

    # Exact timestamp drives posted_at; the coarse badge only drives display.
    def test_exact_iso_outranks_the_coarse_badge_for_normalization(self) -> None:
        enriched = self._job(iso=self.REAL_ISO, badge="Today")
        meta = enriched["job_description_metadata"]
        self.assertEqual(meta["posted_date"], "Today")            # portal's visible text
        self.assertEqual(meta["posted_at"], "2026-07-17T10:16:41")  # exact, not midnight
        # Two same-day jobs with different exact times order correctly (impossible from
        # badge text alone -- both would just say "Today").
        later = self._job(iso="2026-07-17T13:49:18Z", badge="Today")
        self.assertGreater(later["freshness_score"], enriched["freshness_score"])

    def test_badge_alone_still_normalizes_when_payload_is_missing(self) -> None:
        enriched = self._job(badge="Yesterday")
        self.assertEqual(enriched["job_description_metadata"]["posted_date"], "Yesterday")
        self.assertEqual(enriched["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=1)).replace(hour=0, minute=0).isoformat())

    def test_invalid_iso_falls_back_to_display_text(self) -> None:
        enriched = self._job(iso="not-a-timestamp", badge="2 days ago")
        self.assertEqual(enriched["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=2)).isoformat())

    def test_nothing_reliable_stays_unknown(self) -> None:
        enriched = self._job()
        self.assertIsNone(enriched["job_description_metadata"]["posted_at"])
        self.assertEqual(enriched["freshness_score"], UNKNOWN_FRESHNESS)

    def test_extractor_diagnostics_record_source_per_guid(self) -> None:
        class P:
            def evaluate(self, _script, _arg=None):
                a = _card(GUID_A, ""); a["posted_iso"] = "2026-07-17T10:16:41Z"; a["posted_badge"] = "Today"
                b = _card(GUID_B, ""); b["posted_iso"] = ""; b["posted_badge"] = ""
                return [a, b]

        diag: dict = {}
        self.adapter._extract_jobs_from_dom(P(), _variation(), limit=10, diagnostics=diag)
        by_guid = {entry["guid"]: entry for entry in diag["posted_extraction"]}
        self.assertEqual(by_guid[GUID_A]["source"], "payload_postedDate")
        self.assertEqual(by_guid[GUID_A]["posted_iso"], "2026-07-17T10:16:41Z")
        self.assertEqual(by_guid[GUID_B]["source"], "none")
        self.assertTrue(by_guid[GUID_B]["rejection_reason"])


if __name__ == "__main__":
    unittest.main()
