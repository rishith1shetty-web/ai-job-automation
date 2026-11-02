"""Dice-only Find Matches gate: a Dice job is returned only when its genuine Dice Job Match Score,
read from its job-detail page, is verified at or above settings.dice_min_match_score (60).

Dice shows the score in a "Fitment" card ("Your Dice Job Match score is 18%.") that loads after
the page ("Crunching numbers..." first). Search results carry it only for the one job open in the
side panel, so each candidate's detail page is opened -- at most settings.dice_match_score_max_pages
(150) per run. Missing, pending, invalid or unreadable scores never qualify. LinkedIn has no such
score and is unaffected.

No live Dice access: the detail pages and search pages are fakes. The pipeline tests use the
database like test_automatic_search_workflow.py.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _create_search_batch, _run_find_matches_batch, set_active_portal, settings
from app.models import ApplicationRecord, ApplicationStatus, JobOpportunity, JobSearchBatch
from app.schemas import SearchRequest
from app.services.find_matches import per_variation_limits
from app.services.portals.dice_portal import (
    MATCH_SCORE_BLOCKED,
    MATCH_SCORE_INVALID,
    MATCH_SCORE_NO_URL,
    MATCH_SCORE_NOT_SHOWN,
    MATCH_SCORE_PENDING,
    MATCH_SCORE_UNREADABLE,
    MATCH_SCORE_VERIFIED,
    DiceMatchScoreRun,
    DicePortalAdapter,
    dice_match_score_rejection,
    parse_dice_match_score,
)
from app.services.seniority_filter import senior_level_reason
from tests.test_dice_fresh_search import FakeTwoPassPage, _card, _variation
from tests.test_senior_role_pipeline import FakeMixedLinkedIn, _resume

CARD_TEXT = "Fitment\nDice Job Match Score™\nYour Dice Job Match score is {}%. Here's where it meets based on your Ideal Job."
DICE_URL = "https://www.dice.com/job-detail/{:08d}-2222-4333-8444-555555555555"


def _scored(score: int | None, status: str = MATCH_SCORE_VERIFIED) -> dict:
    return {"job_description_metadata": {"dice_match_score": score, "dice_match_score_status": status}}


class ScoreParsingTests(unittest.TestCase):
    def test_threshold_boundaries(self) -> None:
        for score, qualifies in ((59, False), (60, True), (61, True), (0, False), (100, True)):
            with self.subTest(score=score):
                parsed, status = parse_dice_match_score(CARD_TEXT.format(score))
                self.assertEqual((parsed, status), (score, MATCH_SCORE_VERIFIED))
                self.assertEqual(dice_match_score_rejection(_scored(parsed, status), 60) is None, qualifies)

    def test_the_screenshot_wording_reads_18_percent_and_is_rejected(self) -> None:
        score, status = parse_dice_match_score("Your Dice Job Match score is 18%. Here's where it meets based on your Ideal Job.")
        self.assertEqual((score, status), (18, MATCH_SCORE_VERIFIED))
        self.assertEqual(dice_match_score_rejection(_scored(score), 60), "Dice Job Match Score 18% is below the 60% minimum")

    def test_invalid_numbers_are_not_scores(self) -> None:
        for text in ("Your Dice Job Match score is %.", "Your Dice Job Match score is 150%.",
                     "Your Dice Job Match score is 18.5%.", "Your Dice Job Match score is -5%.",
                     "Your Dice Job Match score is abc%."):
            with self.subTest(text=text):
                self.assertEqual(parse_dice_match_score(text), (None, MATCH_SCORE_INVALID))

    def test_pending_unreadable_and_missing_cards(self) -> None:
        self.assertEqual(parse_dice_match_score("Fitment Dice Job Match Score \U0001f522 Crunching numbers..."),
                         (None, MATCH_SCORE_PENDING))
        self.assertEqual(parse_dice_match_score("Fitment Dice Job Match Score™ Learn more"),
                         (None, MATCH_SCORE_UNREADABLE))
        self.assertEqual(parse_dice_match_score("Job Details Skills Docker Python"), (None, MATCH_SCORE_NOT_SHOWN))
        self.assertEqual(parse_dice_match_score(None), (None, MATCH_SCORE_NOT_SHOWN))

    def test_nothing_but_a_verified_whole_number_qualifies(self) -> None:
        not_qualifying = (
            {},                                              # never retrieved
            {"job_description_metadata": {}},                # missing
            _scored(None, MATCH_SCORE_PENDING),
            _scored(None, MATCH_SCORE_INVALID),
            _scored(None, MATCH_SCORE_UNREADABLE),
            _scored(None, MATCH_SCORE_NOT_SHOWN),
            _scored(None, MATCH_SCORE_BLOCKED),
            _scored(75, MATCH_SCORE_PENDING),                # a number without verification
            _scored("75"),                                   # not an int
            _scored(True),                                   # bool is not a score
            _scored(150),                                    # out of range
        )
        for job in not_qualifying:
            with self.subTest(job=job):
                self.assertIsNotNone(dice_match_score_rejection(job, 60))

    def test_the_threshold_and_cap_are_configurable_settings(self) -> None:
        defaults = Settings(openai_api_key=None)
        self.assertEqual(defaults.dice_min_match_score, 60)
        self.assertEqual(defaults.dice_match_score_max_pages, 150)


class _FakeBody:
    def __init__(self, tab) -> None:
        self.tab = tab

    def inner_text(self, timeout: int | None = None) -> str:
        texts = self.tab.texts
        return texts.pop(0) if len(texts) > 1 else texts[0]


class FakeDetailTab:
    """A job-detail tab whose body text changes over successive reads."""

    def __init__(self, texts: list[str], redirect_to: str | None = None) -> None:
        self.texts = list(texts)
        self.redirect_to = redirect_to
        self.url = "about:blank"
        self.opened: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.opened.append(url)
        self.url = self.redirect_to or url

    def locator(self, _selector: str) -> _FakeBody:
        return _FakeBody(self)

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def route(self, *_args) -> None:
        return None


class _SearchPageWithContext:
    def __init__(self, tab: FakeDetailTab) -> None:
        self.context = type("Context", (), {"new_page": lambda _self: tab})()


class DetailPageReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.run = DiceMatchScoreRun(minimum=60, cap=150)

    def _read(self, tab: FakeDetailTab, url: str = DICE_URL.format(1)):
        return self.adapter._read_dice_match_score(_SearchPageWithContext(tab), self.run, {"job_url": url})

    def test_waits_through_crunching_numbers_for_the_score(self) -> None:
        tab = FakeDetailTab(["Dice Job Match Score Crunching numbers...", "Dice Job Match Score Crunching numbers...",
                             CARD_TEXT.format(72)])
        self.assertEqual(self._read(tab), (72, MATCH_SCORE_VERIFIED))
        self.assertEqual(self.run.pages_inspected, 1)

    def test_a_score_that_never_arrives_is_pending_not_assumed(self) -> None:
        tab = FakeDetailTab(["Dice Job Match Score Crunching numbers..."])
        with patch.object(DicePortalAdapter, "MATCH_SCORE_WAIT_SECONDS", 0):
            self.assertEqual(self._read(tab), (None, MATCH_SCORE_PENDING))

    def test_a_sign_in_redirect_stops_the_run(self) -> None:
        tab = FakeDetailTab([""], redirect_to="https://www.dice.com/dashboard/login")
        self.assertEqual(self._read(tab), (None, MATCH_SCORE_BLOCKED))
        self.assertTrue(self.run.blocked)

    def test_a_job_without_a_detail_url_is_not_opened_or_counted(self) -> None:
        tab = FakeDetailTab([CARD_TEXT.format(90)])
        self.assertEqual(self._read(tab, url="https://www.dice.com/jobs?q=x"), (None, MATCH_SCORE_NO_URL))
        self.assertEqual((tab.opened, self.run.pages_inspected), ([], 0))


def _install_scores(adapter: DicePortalAdapter, scores: dict[int, object], opened: list[int]) -> None:
    """Replace the detail-page reader: card i scores scores[i] (an int is verified; a tuple is
    (score, status); absent means the card never shows)."""

    def fake_read(_page, run, job):
        run.pages_inspected += 1
        index = int(job["job_url"].rsplit("/", 1)[1][:8])
        opened.append(index)
        value = scores.get(index)
        if isinstance(value, tuple):
            return value
        return (value, MATCH_SCORE_VERIFIED) if value is not None else (None, MATCH_SCORE_NOT_SHOWN)

    adapter._read_dice_match_score = fake_read


def _titled(card: dict, title: str) -> dict:
    return {**card, "title": title, "description": f"{title}\n{card['company']}\nRemote"}


class QualificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.debug_dir = Path(tempfile.mkdtemp(prefix="dice-score-"))
        self.opened: list[int] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.debug_dir, ignore_errors=True)

    def _jobs(self, indexes, **card_overrides) -> list[dict]:
        return [self.adapter.job_from_card_data({**_card(i), **card_overrides}, _variation()) for i in indexes]

    def _qualify(self, jobs, run, target, allowance=None, next_page=None, page=None):
        return self.adapter._qualify_by_match_score(
            page, _variation(), jobs, run, target, run.cap if allowance is None else allowance,
            5, 30, self.debug_dir, 1, {}, next_relevance_page=next_page,
        )

    def test_continues_past_rejected_jobs_until_the_target_qualifies(self) -> None:
        _install_scores(self.adapter, {1: 40, 2: 59, 3: (None, MATCH_SCORE_PENDING), 4: 61, 5: 80, 6: 60, 7: 99},
                        self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        kept = self._qualify(self._jobs(range(1, 8)), run, target=3)

        self.assertEqual([job["title"] for job in kept], ["Data Analyst 4", "Data Analyst 5", "Data Analyst 6"])
        self.assertEqual(self.opened, [1, 2, 3, 4, 5, 6], "job 7 is never opened once the target is met")
        self.assertEqual(run.pages_inspected, 6)

    def test_the_score_is_stored_on_the_job(self) -> None:
        _install_scores(self.adapter, {1: 85}, self.opened)
        kept = self._qualify(self._jobs([1]), DiceMatchScoreRun(minimum=60, cap=150), target=1)
        metadata = kept[0]["job_description_metadata"]
        self.assertEqual((metadata["dice_match_score"], metadata["dice_match_score_status"]), (85, MATCH_SCORE_VERIFIED))
        self.assertTrue(metadata["dice_match_score_checked_at"])

    def test_the_run_cap_stops_page_opens(self) -> None:
        _install_scores(self.adapter, {i: 10 for i in range(1, 20)}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=4)
        kept = self._qualify(self._jobs(range(1, 20)), run, target=5)
        self.assertEqual((kept, run.pages_inspected, run.cap_reached), ([], 4, True))

    def test_a_variation_cannot_exceed_its_page_allowance(self) -> None:
        _install_scores(self.adapter, {i: 10 for i in range(1, 20)}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        self._qualify(self._jobs(range(1, 20)), run, target=5, allowance=3)
        self.assertEqual(run.pages_inspected, 3)

    def test_applied_jobs_are_skipped_without_opening_or_counting(self) -> None:
        _install_scores(self.adapter, {1: 90, 2: 90, 3: 90}, self.opened)
        applied_url = DICE_URL.format(2)
        run = DiceMatchScoreRun(minimum=60, cap=150, exclude_job=lambda job: job["job_url"] == applied_url)
        kept = self._qualify(self._jobs([1, 2, 3]), run, target=5)
        self.assertEqual([job["job_url"] for job in kept], [DICE_URL.format(1), DICE_URL.format(3)])
        self.assertEqual((self.opened, run.pages_inspected), ([1, 3], 2))

    def test_a_job_is_never_opened_twice_in_a_run(self) -> None:
        _install_scores(self.adapter, {1: 30, 2: 90}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        first = self._qualify(self._jobs([1, 2]), run, target=5)
        # A later variation finds the same two jobs: job 1 was rejected (cached), job 2 already
        # qualified for the earlier variation (a duplicate) -- neither is opened or counted again.
        second = self._qualify(self._jobs([1, 2]), run, target=5)
        self.assertEqual((len(first), second, self.opened, run.pages_inspected), (1, [], [1, 2], 2))

    def test_reads_further_result_pages_when_too_few_qualify(self) -> None:
        # The fresh pass alone meets the pre-score quota (so the relevance fallback never runs),
        # but none of its jobs qualifies: relevance page 1 is read and supplies the jobs.
        fresh = [_card(i) for i in range(1, 6)]
        relevance = [_card(i) for i in range(20, 26)]
        page = FakeTwoPassPage(fresh_pages=[fresh], relevance=relevance)
        _install_scores(self.adapter, {**{i: 20 for i in range(1, 6)}, **{i: 75 for i in range(20, 26)}}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)

        jobs = self.adapter.search_jobs(page, _variation(), limit=5, debug_dir=self.debug_dir, search_index=1,
                                        match_run=run, match_target=4)

        self.assertEqual([job["title"] for job in jobs], [f"Data Analyst {i}" for i in range(20, 24)])
        self.assertTrue(any("page=1" in url and "filters.postedDate" not in url for url in page.goto_urls))
        self.assertTrue(all(dice_match_score_rejection(job, 60) is None for job in jobs))

    def test_a_repeated_result_page_ends_the_deeper_search(self) -> None:
        # Dice serves the same relevance page for every page number: one extra page, then stop.
        page = FakeTwoPassPage(fresh_pages=[[_card(i) for i in range(1, 6)]], relevance=[_card(40)])
        _install_scores(self.adapter, {}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        jobs = self.adapter.search_jobs(page, _variation(), limit=5, debug_dir=self.debug_dir, search_index=1,
                                        match_run=run, match_target=5)
        self.assertEqual(jobs, [])
        self.assertEqual(sum(1 for url in page.goto_urls if "filters.postedDate" not in url), 2)

    def test_senior_and_non_easy_apply_jobs_are_never_opened(self) -> None:
        cards = [_card(1), _titled(_card(2), "Senior Data Analyst"), _card(3, easy=False), _card(4)]
        page = FakeTwoPassPage(fresh_pages=[cards], relevance=[])
        _install_scores(self.adapter, {1: 70, 2: 99, 3: 99, 4: 65}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        jobs = self.adapter.search_jobs(page, _variation(), limit=5, debug_dir=self.debug_dir, search_index=1,
                                        match_run=run, match_target=5)
        self.assertEqual(self.opened, [1, 4])
        self.assertEqual([job["title"] for job in jobs], ["Data Analyst 1", "Data Analyst 4"])
        self.assertIsNone(senior_level_reason(jobs[0]["title"]))

    def test_a_blocked_search_session_opens_nothing_and_returns_nothing(self) -> None:
        class BlockedPage(FakeTwoPassPage):
            def goto(self, url, *args, **kwargs):
                super().goto(url, *args, **kwargs)
                self.url = "https://www.dice.com/dashboard/login"

        _install_scores(self.adapter, {1: 99}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150)
        jobs = self.adapter.search_jobs(BlockedPage(fresh_pages=[[_card(1)]]), _variation(), limit=5,
                                        debug_dir=self.debug_dir, search_index=1, match_run=run, match_target=5)
        self.assertEqual((jobs, self.opened, run.blocked), ([], [], True))

    def test_without_a_match_run_search_jobs_is_unchanged(self) -> None:
        page = FakeTwoPassPage(fresh_pages=[[_card(i) for i in range(1, 4)]], relevance=[])
        _install_scores(self.adapter, {}, self.opened)
        jobs = self.adapter.search_jobs(page, _variation(), limit=3, debug_dir=self.debug_dir, search_index=1)
        self.assertEqual((len(jobs), self.opened), (3, []))

    def test_progress_reports_pages_and_qualifying_counts(self) -> None:
        messages: list[str] = []
        _install_scores(self.adapter, {1: 30, 2: 90}, self.opened)
        run = DiceMatchScoreRun(minimum=60, cap=150, report=messages.append, total_variations=3)
        self._qualify(self._jobs([1, 2]), run, target=5)
        self.assertEqual(len(messages), 2)
        self.assertIn("1/3", messages[-1])
        self.assertIn("2/150 job pages inspected, 1 qualifying (>= 60%)", messages[-1])


class RunBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def test_the_page_cap_is_split_by_search_weight_and_sums_to_the_cap(self) -> None:
        shares = self.adapter._match_score_page_shares([12, 6, 6, 6, 6], 150)
        self.assertEqual(sum(shares), 150)
        self.assertEqual(shares[0], 50)            # the top role carries twice the weight
        self.assertEqual(shares[1:], [25, 25, 25, 25])

    def test_qualifying_targets_follow_the_find_matches_allocation(self) -> None:
        for requested in (20, 50, 100):
            with self.subTest(requested=requested):
                limits = [requested * 2, requested, requested, requested, requested]
                self.assertEqual(sum(self.adapter._match_score_targets(limits, requested)), requested)


class FakeScoredDice:
    """A Dice adapter whose jobs carry (or lack) a Dice Job Match Score, as the real adapter
    stamps it. It records what main.py hands it."""

    jobs_per_variation: list[list[dict]] = []
    summary = {"minimum": 60, "cap": 150, "pages_inspected": 40, "cap_reached": False, "qualified": 0,
               "session_blocked": False}
    exclude_job = None
    status_seen: list[str] = []

    def __init__(self) -> None:
        self.last_match_score_run = type("Run", (), {"summary": lambda _self: dict(FakeScoredDice.summary)})()

    def is_connected(self) -> bool:
        return True

    def search_jobs_many(self, _resume, variations, limit_per_query, progress_callback=None, global_target=None,
                         exclude_job=None, status_callback=None):
        FakeScoredDice.exclude_job = exclude_job
        if status_callback is not None:
            status_callback("Dice search 1/1: DevOps Engineer -- checking Dice Job Match Scores: 3/150 job pages "
                            "inspected, 1 qualifying (>= 60%).")
            with SessionLocal() as other:
                latest = other.scalars(select(JobSearchBatch).order_by(JobSearchBatch.created_at.desc())).first()
                FakeScoredDice.status_seen.append(latest.latest_status_message if latest else "")
        variations = list(variations)
        return [(variation, [dict(job) for job in (self.jobs_per_variation[i] if i < len(self.jobs_per_variation) else [])])
                for i, variation in enumerate(variations)]


def _dice_job(index: int, score, status: str = MATCH_SCORE_VERIFIED, title: str | None = None) -> dict:
    return {
        "title": title or f"DevOps Engineer {index}",
        "company": f"Company {index}",
        "location": "Remote",
        "description": "Terraform Kubernetes AWS",
        "job_url": DICE_URL.format(index),
        "easy_apply": True,
        "job_description_metadata": {"dice_match_score": score, "dice_match_score_status": status},
    }


class DiceScorePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        FakeScoredDice.status_seen = []
        FakeScoredDice.summary = {**FakeScoredDice.summary, "cap_reached": False, "pages_inspected": 40}

    def _run_dice(self, db, resume, limit: int = 20):
        batch = _create_search_batch(
            SearchRequest(resume_id=resume.id, location="Remote", keywords=None, limit=limit, portal="dice"), db
        )
        with patch("app.main.dice_adapter", return_value=FakeScoredDice()):
            _run_find_matches_batch(batch.id)
        db.refresh(batch)
        jobs = list(db.scalars(select(JobOpportunity).where(JobOpportunity.search_batch_id == batch.id)).all())
        return batch, jobs

    def _with_dice_resume(self, body) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "dice")
            resume = _resume("dice")
            db.add(resume)
            db.commit()
            db.refresh(resume)
            try:
                body(db, resume)
            finally:
                job_ids = select(JobOpportunity.id).where(JobOpportunity.resume_id == resume.id)
                db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id.in_(job_ids)))
                db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                db.delete(resume)
                db.commit()
                set_active_portal(db, "linkedin")

    def test_find20_returns_only_verified_jobs_at_or_above_60(self) -> None:
        # Every variation offers qualifying jobs interleaved with every kind of non-qualifying one.
        rejects = [(59, MATCH_SCORE_VERIFIED), (None, MATCH_SCORE_PENDING), (None, MATCH_SCORE_INVALID),
                   (None, MATCH_SCORE_NOT_SHOWN), (75, MATCH_SCORE_PENDING), ("90", MATCH_SCORE_VERIFIED)]
        # All supply sits in the first variation (the plan may hold fewer than five roles): 24
        # qualifying jobs (60-83%) interleaved with 24 that must never qualify.
        FakeScoredDice.jobs_per_variation = [[
            _dice_job(i + 1, 60 + i // 2) if i % 2 == 0 else _dice_job(i + 1, *rejects[(i // 2) % len(rejects)])
            for i in range(48)
        ]]

        def body(db, resume):
            batch, jobs = self._run_dice(db, resume, limit=20)
            self.assertEqual(batch.status, "completed")
            self.assertEqual(len(jobs), 20)
            for job in jobs:
                metadata = job.job_description_metadata
                self.assertEqual(metadata["dice_match_score_status"], MATCH_SCORE_VERIFIED)
                self.assertIsInstance(metadata["dice_match_score"], int)
                self.assertGreaterEqual(metadata["dice_match_score"], 60)
            self.assertTrue(all(job.easy_apply for job in jobs))
            self.assertEqual(len({job.job_url for job in jobs}), 20)

        self._with_dice_resume(body)

    def test_senior_jobs_are_still_excluded_even_with_a_high_score(self) -> None:
        FakeScoredDice.jobs_per_variation = [[_dice_job(1, 95, title="Senior DevOps Engineer"), _dice_job(2, 70)]]

        def body(db, resume):
            _batch, jobs = self._run_dice(db, resume, limit=20)
            self.assertEqual([job.title for job in jobs], ["DevOps Engineer 2"])

        self._with_dice_resume(body)

    def test_a_shortfall_at_the_cap_is_reported_with_counts_and_the_cap(self) -> None:
        FakeScoredDice.jobs_per_variation = [[_dice_job(i, 80) for i in range(1, 6)]]
        FakeScoredDice.summary = {**FakeScoredDice.summary, "cap_reached": True, "pages_inspected": 150}

        def body(db, resume):
            batch, jobs = self._run_dice(db, resume, limit=20)
            self.assertEqual(len(jobs), 5)
            message = batch.latest_status_message
            self.assertIn("Found 5 jobs out of requested 20", message)
            self.assertIn("at least 60%", message)
            self.assertIn("150 job-detail page cap was reached", message)

        self._with_dice_resume(body)

    def test_progress_is_visible_on_the_batch_while_scores_are_read(self) -> None:
        FakeScoredDice.jobs_per_variation = [[_dice_job(1, 80)]]

        def body(db, resume):
            batch, _jobs = self._run_dice(db, resume, limit=20)
            self.assertIn("checking Dice Job Match Scores: 3/150", FakeScoredDice.status_seen[0])
            self.assertNotIn("cap was reached", batch.latest_status_message)
            self.assertIn("40 of 150 job-detail pages inspected", batch.latest_status_message)

        self._with_dice_resume(body)

    def test_already_applied_jobs_are_excluded_and_never_scored(self) -> None:
        def body(db, resume):
            applied = JobOpportunity(resume_id=resume.id, portal="dice", title="DevOps Engineer 1",
                                     company="Company 1", location="Remote", job_url=DICE_URL.format(1),
                                     description="", easy_apply=True, match_score=80)
            db.add(applied)
            db.flush()
            db.add(ApplicationRecord(job_id=applied.id, portal="dice", status=ApplicationStatus.APPLIED, dry_run=False))
            db.commit()
            FakeScoredDice.jobs_per_variation = [[_dice_job(1, 95), _dice_job(2, 70)]]

            _batch, jobs = self._run_dice(db, resume, limit=20)

            self.assertEqual([job.job_url for job in jobs], [DICE_URL.format(2)])
            # main.py handed the adapter the same applied-job identity check, so the deeper Dice
            # search never spends a page on an applied job or counts it as qualifying.
            self.assertTrue(FakeScoredDice.exclude_job(_dice_job(1, None)))
            self.assertFalse(FakeScoredDice.exclude_job(_dice_job(2, None)))

        self._with_dice_resume(body)

    def test_linkedin_jobs_have_no_dice_score_and_are_unaffected(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    set_active_portal(db, "linkedin")
                    resume = _resume("linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with patch("app.main.LinkedInAutomation", FakeMixedLinkedIn):
                            batch = _create_search_batch(SearchRequest(
                                resume_id=resume.id, location="Remote", keywords=None, limit=20), db)
                            _run_find_matches_batch(batch.id)
                        db.refresh(batch)
                        jobs = list(db.scalars(
                            select(JobOpportunity).where(JobOpportunity.search_batch_id == batch.id)).all())
                        self.assertEqual(len(jobs), 20)
                        self.assertFalse(any("dice_match_score" in (job.job_description_metadata or {}) for job in jobs))
                        self.assertNotIn("Dice Job Match Score", batch.latest_status_message)
                    finally:
                        db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()


if __name__ == "__main__":
    unittest.main()
