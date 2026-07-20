"""Freshness-aware Find Matches ranking (LinkedIn + Dice).

Every manual Find Matches run performs a fresh portal search; the portal's own posted time is
normalized (one reference timestamp per run) into job_description_metadata.posted_at and a
deterministic freshness_score. Relevance/eligibility decide whether a job ENTERS the matched
set; the completed dashboard is then ordered GLOBALLY by posting time -- newest confirmed
first, unknown last -- derived from posted_at, never created_at / discovery / variation order.

Reappearance + Live-APPLIED exclusion are pinned by tests/test_previous_batch_suppression.py
and tests/test_automatic_search_workflow.py and are unchanged here.
"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import (
    _create_search_batch,
    _run_find_matches_batch,
    list_jobs,
    set_active_portal,
    settings,
)
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.schemas import SearchRequest
from app.services.find_matches import FindMatchesEngine, SearchVariation
from app.services.linkedin import LinkedInAutomation
from app.services.portals.dice_portal import DicePortalAdapter
from app.services.posting_time import UNKNOWN_FRESHNESS, freshness_score_for, parse_posted_text

REF = datetime(2026, 7, 17, 14, 30, 0)


def _variation() -> SearchVariation:
    return SearchVariation(query="Data Analyst", location="United States", role="Data Analyst",
                           expanded_role="Data Analyst", pass_name="primary")


def _resume(portal: str = "linkedin") -> ResumeProfile:
    return ResumeProfile(
        portal=portal,
        filename="freshness.txt",
        stored_path="freshness.txt",
        raw_text="Python SQL data analyst",
        skills=["Python", "SQL"],
        experience=[],
        education=[],
        job_titles=["Data Analyst"],
        keywords=["analytics"],
        technologies=["Python", "SQL"],
        domain_expertise=["Data Analytics"],
        detected_role="Data Analyst",
        generated_search_keywords=["Data Analyst"],
        generated_search_query="Data Analyst",
        role_confidence=90,
    )


class EnrichmentNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _raw(self, posted: str | None, **over) -> dict:
        raw = {
            "portal": "dice",
            "title": "Data Analyst",
            "company": "Acme",
            "location": "Remote",
            "description": "Python SQL dashboards",
            "job_url": "https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2",
            "easy_apply": True,
            "job_description_metadata": {"posted_date": posted} if posted is not None else {},
        }
        raw.update(over)
        return raw

    def test_posted_text_normalizes_to_posted_at_and_freshness(self) -> None:
        seven = self.engine.enrich_raw_job(self._raw("7 minutes ago"), _variation(), reference_time=REF)
        three_days = self.engine.enrich_raw_job(self._raw("3 days ago"), _variation(), reference_time=REF)
        self.assertEqual(seven["job_description_metadata"]["posted_at"],
                         (REF - timedelta(minutes=7)).isoformat())
        self.assertEqual(three_days["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=3)).isoformat())
        self.assertGreater(seven["freshness_score"], three_days["freshness_score"])

    def test_linkedin_style_metadata_is_normalized_too(self) -> None:
        raw = self._raw("Reposted 2 weeks ago", portal="linkedin",
                        job_url="https://www.linkedin.com/jobs/view/12345")
        enriched = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)
        self.assertEqual(enriched["job_description_metadata"]["posted_at"],
                         (REF - timedelta(weeks=2)).isoformat())

    def test_phrase_is_rescued_from_card_text_when_no_dedicated_field(self) -> None:
        raw = self._raw(None, description="Data Analyst\nAcme\nRemote\nPosted 2 days ago")
        enriched = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)
        self.assertEqual(enriched["job_description_metadata"]["posted_at"],
                         (REF - timedelta(days=2)).isoformat())

    def test_unknown_and_malformed_stay_unknown_without_crashing(self) -> None:
        for posted in (None, "", "garbled nonsense", "13 fortnights ago"):
            with self.subTest(posted=posted):
                enriched = self.engine.enrich_raw_job(self._raw(posted), _variation(), reference_time=REF)
                self.assertIsNone(enriched["job_description_metadata"]["posted_at"])
                self.assertEqual(enriched["freshness_score"], UNKNOWN_FRESHNESS)

    def test_normalization_is_deterministic_for_one_reference_time(self) -> None:
        a = self.engine.enrich_raw_job(self._raw("45 minutes ago"), _variation(), reference_time=REF)
        b = self.engine.enrich_raw_job(self._raw("45 minutes ago"), _variation(), reference_time=REF)
        self.assertEqual(a["freshness_score"], b["freshness_score"])
        self.assertEqual(a["job_description_metadata"]["posted_at"],
                         b["job_description_metadata"]["posted_at"])

    def test_enriched_dict_constructs_a_job_opportunity(self) -> None:
        # Raw posting text lives in metadata only -- no stray top-level kwargs.
        raw = self._raw(None)
        raw["posted_date"] = "7 minutes ago"  # adapter-style top-level field must be folded in
        enriched = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)
        self.assertNotIn("posted_date", enriched)
        self.assertEqual(enriched["job_description_metadata"]["posted_date"], "7 minutes ago")
        job = JobOpportunity(resume_id=1, **enriched)
        self.assertEqual(job.title, "Data Analyst")


class DicePostedDisplayTests(unittest.TestCase):
    """The visible 'Posted' value (posted_display) is derived from the authoritative posted_at
    for Dice -- so a raw ISO timestamp or a stale coarse card badge is never shown, and the
    label stays consistent with the newest-to-oldest ordering. Sorting is unaffected; LinkedIn's
    own relative text is preserved verbatim."""

    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _dice(self, *, posted_date: str = "", posted_iso: str = "") -> dict:
        raw = {
            "portal": "dice",
            "title": "Data Analyst",
            "company": "Acme",
            "location": "Remote",
            "description": "Python SQL dashboards",
            "job_url": "https://www.dice.com/job-detail/be2352e2-6429-495f-b28d-71f9bd9a60b2",
            "easy_apply": True,
            "job_description_metadata": {"posted_date": posted_date, "posted_iso": posted_iso},
        }
        return self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)

    def test_exact_iso_payload_displays_relative_never_raw_iso(self) -> None:
        # 7 minutes before REF, no badge -> "7 minutes ago", not "2026-07-17T14:23:00Z".
        meta = self._dice(posted_iso="2026-07-17T14:23:00Z")["job_description_metadata"]
        self.assertEqual(meta["posted_display"], "7 minutes ago")
        self.assertNotIn("T", meta["posted_display"])
        self.assertNotIn("2026", meta["posted_display"])

    def test_hours_and_days_display(self) -> None:
        two_hours = self._dice(posted_iso="2026-07-17T12:30:00Z")["job_description_metadata"]
        self.assertEqual(two_hours["posted_display"], "2 hours ago")
        two_days = self._dice(posted_iso="2026-07-15T09:00:00Z")["job_description_metadata"]
        self.assertEqual(two_days["posted_display"], "2 days ago")
        yesterday = self._dice(posted_iso="2026-07-16T20:00:00Z")["job_description_metadata"]
        self.assertEqual(yesterday["posted_display"], "Yesterday")

    def test_stale_coarse_badge_is_ignored_when_exact_timestamp_exists(self) -> None:
        # Dice's card badge says "Today" but the payload postedDate is 2 days old: the display
        # follows the exact timestamp, and posted_date keeps the raw badge for diagnostics.
        meta = self._dice(posted_date="Today", posted_iso="2026-07-15T09:00:00Z")["job_description_metadata"]
        self.assertEqual(meta["posted_display"], "2 days ago")
        self.assertEqual(meta["posted_date"], "Today")

    def test_unknown_posted_display_is_empty(self) -> None:
        meta = self._dice()["job_description_metadata"]
        self.assertIsNone(meta["posted_at"])
        self.assertEqual(meta["posted_display"], "")

    def test_display_does_not_change_sort_values(self) -> None:
        enriched = self._dice(posted_iso="2026-07-17T14:23:00Z")
        meta = enriched["job_description_metadata"]
        # posted_at and freshness_score come from the exact timestamp, independent of display.
        self.assertEqual(meta["posted_at"], (REF - timedelta(minutes=7)).isoformat())
        self.assertEqual(enriched["freshness_score"],
                         freshness_score_for(REF - timedelta(minutes=7), REF))

    def test_linkedin_display_preserves_raw_relative_text(self) -> None:
        raw = {
            "portal": "linkedin",
            "title": "Data Analyst",
            "company": "Acme",
            "location": "Remote",
            "description": "Python SQL",
            "job_url": "https://www.linkedin.com/jobs/view/12345",
            "easy_apply": True,
            "job_description_metadata": {"posted_date": "Reposted 2 weeks ago"},
        }
        meta = self.engine.enrich_raw_job(raw, _variation(), reference_time=REF)["job_description_metadata"]
        self.assertEqual(meta["posted_display"], "Reposted 2 weeks ago")


class FreshnessOrderingSqlTests(unittest.TestCase):
    """The completed-batch ordering (list_jobs -> _job_apply_ordering) is GLOBAL posting-time
    order: relevance/eligibility decide inclusion (collection, unchanged); posted_at decides
    position. Newest confirmed first, unknown last, across every search variation -- with no
    influence from variation order, insertion order, or created_at."""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = _resume("dice")
        self.db.add(self.resume)
        self.db.commit()
        self.db.refresh(self.resume)
        self.batch = JobSearchBatch(id=f"fresh-{self.resume.id}", resume_id=self.resume.id,
                                    search_query="Data Analyst", location="Remote", portal="dice")
        self.db.add(self.batch)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == self.resume.id))
        self.db.delete(self.resume)
        self.db.commit()
        self.db.close()

    def _job(self, guid_tail: int, *, match: float, posted: str | None = None,
             created_at: datetime | None = None, variation: str = "Data Analyst") -> JobOpportunity:
        # freshness_score is derived through the REAL parser exactly as enrichment does, so
        # these tests exercise the true posted_at -> ordering path, not hand-picked scores.
        posted_at = parse_posted_text(posted, REF) if posted else None
        job = JobOpportunity(
            resume_id=self.resume.id,
            search_batch_id=self.batch.id,
            portal="dice",
            title=f"Job {guid_tail}",
            company="Acme",
            location="Remote",
            job_url=f"https://www.dice.com/job-detail/{guid_tail:08d}-2222-4333-8444-555555555555",
            description="SQL",
            easy_apply=True,
            match_score=match,
            freshness_score=freshness_score_for(posted_at, REF),
            completion_likelihood_score=90,
            application_friction_score=10,
            priority_score=50,
            search_query_used=variation,
            job_description_metadata={
                "posted_date": posted or "",
                "posted_at": posted_at.isoformat() if posted_at else None,
            },
        )
        if created_at is not None:
            job.created_at = created_at
        self.db.add(job)
        self.db.commit()
        self.db.refresh(job)
        return job

    def _ordered_titles(self) -> list[str]:
        rows = list_jobs(resume_id=self.resume.id, search_batch_id=self.batch.id,
                         portal="dice", db=self.db)
        return [row.title for row in rows]

    def test_two_hours_outranks_one_day_despite_higher_match(self) -> None:
        # The live-validation defect: relevance layers must not pin an older job on top.
        self._job(1, match=27.9, posted="1 day ago")
        self._job(2, match=26.5, posted="2 hours ago")
        self.assertEqual(self._ordered_titles(), ["Job 2", "Job 1"])

    def test_variation_and_insertion_order_have_no_influence(self) -> None:
        # The spec's exact example: A (var 1/15, 1 day, best match) inserted FIRST, C (var
        # 10/15, 2 minutes, weakest match) inserted LAST -> final order is C, B, A purely by
        # posting time, interleaving variations.
        self._job(1, match=27.9, posted="1 day ago", variation="Data Analyst 1/15")
        self._job(2, match=26.5, posted="2 hours ago", variation="Reporting Analyst 2/15")
        self._job(3, match=22.0, posted="2 minutes ago", variation="Data Scientist 10/15")
        self.assertEqual(self._ordered_titles(), ["Job 3", "Job 2", "Job 1"])

    def test_full_posting_ladder_from_scrambled_insertion(self) -> None:
        # Inserted deliberately out of order; every confirmed step of the ladder must come
        # out newest-to-oldest with unknown last, regardless of insertion sequence.
        ladder = ["7 minutes ago", "12 minutes ago", "1 hour ago", "yesterday",
                  "3 days ago", "1 week ago", None]
        for scrambled_position, ladder_index in enumerate([3, 0, 6, 2, 5, 1, 4]):
            self._job(ladder_index + 1, match=50, posted=ladder[ladder_index])
        self.assertEqual(self._ordered_titles(),
                         [f"Job {i}" for i in range(1, len(ladder) + 1)])

    def test_created_at_has_no_influence_when_posted_at_is_known(self) -> None:
        # The fresher-POSTED job has the OLDER database row -- it must still rank first.
        old_row_time = datetime.utcnow() - timedelta(days=10)
        self._job(1, match=77, posted="12 minutes ago", created_at=old_row_time)
        self._job(2, match=77, posted="1 week ago", created_at=datetime.utcnow())
        self.assertEqual(self._ordered_titles(), ["Job 1", "Job 2"])

    def test_all_confirmed_dates_rank_above_unknown(self) -> None:
        self._job(1, match=95)                                # unknown, best match
        self._job(2, match=40, posted="2019-03-01")           # ancient but confirmed
        self.assertEqual(self._ordered_titles(), ["Job 2", "Job 1"])

    def test_equal_posted_at_breaks_on_match_then_id(self) -> None:
        # Secondary ordering is deterministic and only applies WITHIN one posted_at value.
        self._job(1, match=60, posted="3 days ago")
        self._job(2, match=80, posted="3 days ago")
        first = self._job(3, match=60, posted="2019-03-01")
        second = self._job(4, match=60, posted="2019-03-01")
        self.assertEqual(self._ordered_titles(), ["Job 2", "Job 1", "Job 3", "Job 4"])
        self.assertLess(first.id, second.id)

    def test_ordering_only_permutes_the_included_set(self) -> None:
        # Posting time never ADDS a job: the response is exactly the batch's accepted rows.
        # Inclusion (relevance/eligibility at collection) is pinned by the collection tests.
        self._job(1, match=30, posted="2 minutes ago")
        self._job(2, match=90, posted="1 week ago")
        rows = list_jobs(resume_id=self.resume.id, search_batch_id=self.batch.id,
                         portal="dice", db=self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row.search_batch_id for row in rows}, {self.batch.id})

    def test_match_similarity_band_is_gone(self) -> None:
        import app.main as main_module

        self.assertFalse(hasattr(main_module, "MATCH_SIMILARITY_BAND"))


class _FreshSearchFakeLinkedIn:
    """search_jobs returns portal jobs WITH raw posted text, as the adapter now does."""

    jobs: list[dict] = []

    def __init__(self, _settings) -> None:
        pass

    def search_jobs(self, _resume, _location, _keywords, _limit):
        return [dict(job) for job in self.__class__.jobs]


class FreshSearchPipelineTests(unittest.TestCase):
    """Full Find Matches run: posted text survives enrichment, persistence AND the reasoning
    metadata pass (merge, not replace), with one reference timestamp for the whole run."""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def test_posted_metadata_survives_the_full_pipeline(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    set_active_portal(db, "linkedin")
                    resume = _resume("linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    _FreshSearchFakeLinkedIn.jobs = [
                        {
                            "title": "Fresh Analyst",
                            "company": "Acme",
                            "location": "Remote",
                            "description": "Python SQL dashboards",
                            "job_url": "https://www.linkedin.com/jobs/view/910000001",
                            "easy_apply": True,
                            "job_description_metadata": {"posted_date": "7 minutes ago"},
                        },
                        {
                            "title": "Older Analyst",
                            "company": "Acme",
                            "location": "Remote",
                            "description": "Python SQL dashboards",
                            "job_url": "https://www.linkedin.com/jobs/view/910000002",
                            "easy_apply": True,
                            "job_description_metadata": {"posted_date": "3 days ago"},
                        },
                    ]
                    try:
                        request = SearchRequest(resume_id=resume.id, location="Remote",
                                                keywords=None, limit=10)
                        with patch("app.main.LinkedInAutomation", _FreshSearchFakeLinkedIn):
                            batch = _create_search_batch(request, db)
                            _run_find_matches_batch(batch.id)
                        rows = {
                            row.title: row
                            for row in db.scalars(
                                select(JobOpportunity).where(JobOpportunity.search_batch_id == batch.id)
                            ).all()
                        }
                        self.assertEqual(set(rows), {"Fresh Analyst", "Older Analyst"})
                        fresh, older = rows["Fresh Analyst"], rows["Older Analyst"]
                        for row in (fresh, older):
                            meta = row.job_description_metadata or {}
                            # Portal posting data survived the reasoning metadata pass...
                            self.assertTrue(meta.get("posted_at"), f"posted_at lost on {row.title}")
                            self.assertTrue(meta.get("posted_date"))
                            # ...and the reasoning keys are present too (merge, not replace).
                            self.assertIn("required_skills", meta)
                        # One run-wide reference time (the batch timestamp) -> 7 minutes is
                        # strictly fresher than 3 days, reflected in the persisted scores.
                        self.assertGreater(fresh.freshness_score, older.freshness_score)
                        expected_fresh_at = (batch.created_at - timedelta(minutes=7)).isoformat()
                        self.assertEqual(fresh.job_description_metadata["posted_at"], expected_fresh_at)
                    finally:
                        db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()


class DicePostedCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def test_card_posted_text_reaches_metadata(self) -> None:
        job = self.adapter.job_from_card_data(
            {
                "job_guid": "be2352e2-6429-495f-b28d-71f9bd9a60b2",
                "guid_corroborated": True,
                "title": "Data Analyst",
                "company": "Acme",
                "location": "Remote",
                "job_url": "",
                "apply_url": "",
                "apply_cta": "Easy Apply",
                "posted_date": "Posted 7 minutes ago",
                "description": "Data Analyst\nAcme\nRemote",
            },
            _variation(),
        )
        self.assertEqual(job["job_description_metadata"]["posted_date"], "Posted 7 minutes ago")

    def test_card_scanner_vocabulary_covers_minutes_and_yesterday(self) -> None:
        # The in-page scanner must recognise the full Dice vocabulary -- a job posted minutes
        # ago must not be read as having no posting date.
        source = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
        self.assertIn("min(?:ute)?s?", source)
        self.assertIn("yesterday", source)
        self.assertIn("moments?", source)


class _PostedFakeLocator:
    def __init__(self, text: str):
        self._text = text

    def count(self) -> int:
        return 1 if self._text else 0

    @property
    def first(self):
        return self

    def inner_text(self, timeout=None) -> str:
        return self._text


class _PostedFakePage:
    """Job-detail page exposing posted text under a given selector only."""

    def __init__(self, texts_by_selector: dict[str, str]):
        self._texts = texts_by_selector

    def locator(self, selector: str):
        return _PostedFakeLocator(self._texts.get(selector, ""))


class LinkedInPostedCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.automation = LinkedInAutomation(Settings(openai_api_key=None))

    def test_dedicated_posted_selector_wins(self) -> None:
        page = _PostedFakePage({".jobs-unified-top-card__posted-date": "2 hours ago"})
        self.assertEqual(self.automation._posted_phrase(page, ""), "2 hours ago")

    def test_phrase_extracted_from_top_card_description_fallback(self) -> None:
        page = _PostedFakePage({})
        place = "Acme Health · New York, NY · Reposted 2 weeks ago · Over 100 applicants"
        self.assertEqual(self.automation._posted_phrase(page, place).lower(), "reposted 2 weeks ago")

    def test_no_posted_text_stays_unknown(self) -> None:
        page = _PostedFakePage({})
        self.assertEqual(self.automation._posted_phrase(page, "Remote · Full-time"), "")

    def test_search_paths_capture_posted_metadata(self) -> None:
        # Both LinkedIn search paths hand the phrase to the shared normalization pass.
        source = Path("app/services/linkedin.py").read_text(encoding="utf-8", errors="replace")
        self.assertEqual(source.count('"job_description_metadata": {"posted_date": self._posted_phrase(page, place)}'), 2)


if __name__ == "__main__":
    unittest.main()
