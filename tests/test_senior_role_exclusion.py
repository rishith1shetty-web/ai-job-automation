"""Senior-level role exclusion for Find Matches: the shared title policy and the portal adapters.

Find Matches serves junior, entry-level, associate and ordinary individual-contributor roles, in
every domain. Senior, Lead, Principal, Staff (technical ladder), Architect, engineering-management
and executive titles are excluded -- decided from the job title, and from a description only when
it states the level explicitly.

These tests need no database and no live portal: the adapters run against fake pages. The
end-to-end pipeline (both portals through _search_jobs_sync) is in test_senior_role_pipeline.py.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.services.linkedin import LinkedInAutomation
from app.services.portals.dice_portal import DicePortalAdapter
from app.services.seniority_filter import (
    is_senior_level_job,
    normalize_title,
    senior_level_job_reason,
    senior_level_reason,
)
from tests.test_dice_card_scan_allocation import FakeEvaluatePage
from tests.test_dice_fresh_search import FakeTwoPassPage, _card, _iso_ago, _variation

EXCLUDED_TITLES = (
    # The required examples.
    "Senior DevOps Engineer",
    "Senior DevSecOps Engineer",
    "Sr. Software Engineer",
    "Lead Data Analyst",
    "Principal Cloud Engineer",
    "Staff Backend Engineer",
    "Solutions Architect",
    "DevOps Architect",
    "Senior Engineering Manager",
    "Senior Technical Program Manager",
    # The same policy in other domains and word orders.
    "Senior Accountant",
    "Senior Product Manager",
    "Tech Lead",
    "Technical Lead - Platform",
    "Team Lead, Customer Support",
    "Lead Engineer",
    "Staff Engineer",
    "Staff Data Scientist",
    "Distinguished Engineer",
    "Software Architect",
    "Cloud Architect",
    "Enterprise Architect",
    "Engineering Manager",
    "Software Engineering Manager",
    "Software Development Manager",
    "Development Manager",
    "Technical Manager",
    "IT Manager",
    "Manager, Software Engineering",
    "Manager of Engineering",
    "Director of Engineering",
    "Head of Data",
    "VP of Engineering",
    "Chief Technology Officer",
)

RETAINED_TITLES = (
    # The required examples.
    "Junior DevOps Engineer",
    "Entry-Level Software Engineer",
    "Associate Software Engineer",
    "Software Engineer",
    "Backend Engineer",
    "QA Engineer",
    "Data Analyst",
    "Cloud Engineer",
    "Security Analyst",
    "Support Engineer",
    # Ordinary managers are not technical/engineering management.
    "Technical Program Manager",
    "Product Manager",
    "Project Manager",
    "IT Project Manager",
    "Business Development Manager",
    "Account Manager",
    # "Staff" is a level only on a technical ladder.
    "Staff Accountant",
    "Staff Nurse",
    "Member of Technical Staff",
    "Staffing Coordinator",
    # Word fragments and non-level senses.
    "Leadership Development Associate",
    "Lead Generation Specialist",
    "Senior Living Caregiver",
    "Cloud Architecture Engineer",
    "Directory Services Engineer",
    # An explicit entry level wins: these postings are open to a junior candidate.
    "Senior-Junior Engineer",
    "Junior/Senior Developer",
    "Junior Solutions Architect",
    "Associate Solutions Architect",
)


class SeniorTitlePolicyTests(unittest.TestCase):
    # 1-8. Senior-level titles are excluded, in every domain.
    def test_senior_level_titles_are_excluded(self) -> None:
        for title in EXCLUDED_TITLES:
            with self.subTest(title=title):
                self.assertIsNotNone(senior_level_reason(title), title)

    # 9-14. Junior, entry-level, associate and ordinary titles are retained.
    def test_acceptable_titles_are_retained(self) -> None:
        for title in RETAINED_TITLES:
            with self.subTest(title=title):
                self.assertIsNone(senior_level_reason(title), title)

    def test_each_required_example_reports_why(self) -> None:
        expected = {
            "Senior DevOps Engineer": "senior title",
            "Sr. Software Engineer": "senior title",
            "Lead Data Analyst": "lead title",
            "Principal Cloud Engineer": "principal title",
            "Staff Backend Engineer": "staff title",
            "Solutions Architect": "architect title",
            "DevOps Architect": "architect title",
            "Senior Engineering Manager": "senior title",
        }
        for title, reason in expected.items():
            with self.subTest(title=title):
                self.assertEqual(senior_level_reason(title), reason)

    # 15. Unrelated words never trigger an exclusion.
    def test_description_mentions_of_seniority_do_not_exclude(self) -> None:
        descriptions = (
            "You will partner with senior stakeholders and learn from our senior engineers.",
            "Strong leadership and communication skills. Mentor support staff.",
            "Experience with senior-level tooling such as Terraform and Kubernetes.",
            "Seniority level\nMid-Senior level\nEmployment type\nFull-time",
            "Reports to the Lead Architect and the Engineering Manager.",
        )
        for title in ("Data Analyst", "Associate Software Engineer", "Junior DevOps Engineer"):
            for description in descriptions:
                with self.subTest(title=title, description=description[:30]):
                    self.assertIsNone(senior_level_reason(title, description))

    def test_staff_and_lead_are_whole_words_only(self) -> None:
        for title in ("Staffing Specialist", "Overstaffed Shift Coordinator", "Leadership Program Analyst",
                      "Mislead Detection Analyst", "Leading Edge Software Engineer"):
            with self.subTest(title=title):
                self.assertIsNone(senior_level_reason(title))

    def test_an_explicit_description_level_statement_excludes(self) -> None:
        self.assertEqual(
            senior_level_reason("Data Analyst", "This is a senior-level position on our team."),
            "description states a senior-level role",
        )
        self.assertIsNotNone(senior_level_reason("Program Manager", "Experience level: Director"))

    def test_an_entry_level_title_outranks_the_description(self) -> None:
        # Associate/Junior positions are never rejected because of their description.
        self.assertIsNone(senior_level_reason("Junior Data Analyst", "This is a senior level role."))

    # 16. Case, punctuation, abbreviations and whitespace.
    def test_case_punctuation_abbreviation_and_whitespace_variants(self) -> None:
        for title in ("SENIOR DEVOPS ENGINEER", "senior devops engineer", "Sr DevOps Engineer",
                      "SR. DevOps Engineer", "Sr.DevOps Engineer", "Snr DevOps Engineer",
                      "  Senior   DevOps\tEngineer ", "Senior-DevOps-Engineer", "Senior DevOps Engineer (Remote)",
                      "DevOps Engineer - Senior", "Eng Manager", "Engineering Mgr", "LEAD, DATA ANALYTICS",
                      "Principal/Staff Engineer", "Staff Software Engineer, Backend"):
            with self.subTest(title=title):
                self.assertIsNotNone(senior_level_reason(title))
        for title in ("JUNIOR DEVOPS ENGINEER", "Jr. DevOps Engineer", "Jr DevOps Engineer",
                      "entry level software engineer", "Entry-level  Software  Engineer"):
            with self.subTest(title=title):
                self.assertIsNone(senior_level_reason(title))

    def test_normalization(self) -> None:
        self.assertEqual(normalize_title("Sr.  DevOps-Engineer!!"), "senior devops engineer")
        self.assertEqual(normalize_title("Jr. C++ Developer"), "junior c++ developer")
        self.assertEqual(normalize_title(None), "")

    def test_empty_or_missing_titles_are_never_excluded(self) -> None:
        for title in ("", None, "   ", "---"):
            with self.subTest(title=title):
                self.assertIsNone(senior_level_reason(title))

    def test_numbered_levels_are_not_treated_as_senior(self) -> None:
        # Documented decision: level numerals mean different things at different employers
        # ("Engineer II" is entry-level at some, mid at others), so they are not a signal.
        for title in ("Software Engineer II", "Software Engineer III", "Data Analyst 2"):
            with self.subTest(title=title):
                self.assertIsNone(senior_level_reason(title))

    def test_job_dict_helpers_read_title_and_description(self) -> None:
        self.assertTrue(is_senior_level_job({"title": "Lead Data Analyst", "description": ""}))
        self.assertFalse(is_senior_level_job({"title": "Data Analyst", "description": "senior stakeholders"}))
        self.assertEqual(
            senior_level_job_reason({"title": "Data Analyst", "description": "This is a senior role."}),
            "description states a senior-level role",
        )
        self.assertIsNone(senior_level_job_reason({}))


def _titled(card: dict, title: str) -> dict:
    return {**card, "title": title, "description": f"{title}\n{card['company']}\nRemote"}


class DiceSeniorExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self.debug_dir = Path(tempfile.mkdtemp(prefix="dice-senior-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.debug_dir, ignore_errors=True)

    # 18. A senior Easy Apply card does not count toward the per-query target, so the bounded
    # scan keeps going and finds a replacement instead of stopping short.
    def test_senior_cards_do_not_fill_the_extractor_target(self) -> None:
        cards = []
        for i in range(30):
            card = _card(i)
            cards.append(_titled(card, f"Senior Data Analyst {i}") if i % 2 == 0 else card)
        page = FakeEvaluatePage(cards)
        diag: dict = {}
        jobs = self.adapter._extract_jobs_from_dom(page, _variation(), limit=5, diagnostics=diag)

        self.assertEqual(diag["scan_stop_reason"], "eligible_target_reached")
        self.assertEqual(diag["cumulative_eligible_jobs"], 5)
        # Ten cards were needed to find five non-senior Easy Apply jobs, not five.
        self.assertEqual(diag["cards_scanned_by_extractor"], 10)
        self.assertEqual(sum(1 for job in jobs if not is_senior_level_job(job)), 5)

    def test_filter_drops_senior_jobs_keeps_order_and_records_why(self) -> None:
        jobs = [
            self.adapter.job_from_card_data(_card(1), _variation()),
            self.adapter.job_from_card_data(_titled(_card(2), "Principal Data Analyst"), _variation()),
            self.adapter.job_from_card_data(_card(3), _variation()),
            self.adapter.job_from_card_data(_titled(_card(4), "Data Architect"), _variation()),
            self.adapter.job_from_card_data(_card(5, easy=False), _variation()),
            self.adapter.job_from_card_data(_card(6), _variation()),
        ]
        kept = self.adapter._filter_easy_apply_only(jobs, self.debug_dir, 1)

        # The surviving jobs keep the order they arrived in (fresh first), so newest-first
        # ordering downstream is untouched.
        self.assertEqual([job["title"] for job in kept], ["Data Analyst 1", "Data Analyst 3", "Data Analyst 6"])
        report = json.loads((self.debug_dir / "search_1_easy_apply_filter.json").read_text(encoding="utf-8"))
        self.assertEqual(report["easy_apply_kept"], 3)
        self.assertEqual(report["external_or_apply_filtered"], 1)
        self.assertEqual(report["senior_level_filtered"], 2)
        self.assertEqual(
            [entry["reason"] for entry in report["senior_level_excluded_jobs"]],
            ["senior_level_role: principal title", "senior_level_role: architect title"],
        )

    def test_a_senior_only_fresh_pass_still_runs_the_relevance_fallback(self) -> None:
        # Pass A found five Easy Apply jobs, but every one is senior. Counting them as eligible
        # would skip the fallback and return nothing; the fallback must run and supply juniors.
        fresh = [_titled(_card(i, posted_iso=_iso_ago(minutes=10 * i)), f"Senior Data Analyst {i}")
                 for i in range(1, 6)]
        relevance = [_card(i, posted_date="2 days ago") for i in range(20, 25)]
        page = FakeTwoPassPage(fresh_pages=[fresh], relevance=relevance)

        jobs = self.adapter.search_jobs(page, _variation(), limit=5, debug_dir=self.debug_dir, search_index=1)

        self.assertTrue(page.relevance_gotos, "the relevance fallback must run")
        self.assertEqual(len(jobs), 5)
        self.assertFalse(any(is_senior_level_job(job) for job in jobs))
        diag = json.loads((self.debug_dir / "search_1_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diag["fresh_eligible_before_fallback"], 0)
        self.assertTrue(diag["fallback_pass_ran"])

    def test_fresh_juniors_still_rank_before_older_ones(self) -> None:
        # 19. Freshness: with seniors mixed in, the fresh pass's junior jobs still come first.
        fresh = [
            _card(1, posted_iso=_iso_ago(minutes=5)),
            _titled(_card(2, posted_iso=_iso_ago(minutes=6)), "Lead Data Analyst"),
            _card(3, posted_iso=_iso_ago(minutes=7)),
        ]
        relevance = [_card(i, posted_date="3 days ago") for i in range(30, 36)]
        page = FakeTwoPassPage(fresh_pages=[fresh], relevance=relevance)

        jobs = self.adapter.search_jobs(page, _variation(), limit=4, debug_dir=self.debug_dir, search_index=1)

        self.assertEqual([job["title"] for job in jobs[:2]], ["Data Analyst 1", "Data Analyst 3"])
        self.assertEqual(len(jobs), 4)
        self.assertNotIn("Lead Data Analyst", [job["title"] for job in jobs])

    def test_an_all_junior_search_is_unchanged(self) -> None:
        # Without senior cards the extractor behaves exactly as before.
        page = FakeEvaluatePage([_card(i) for i in range(20)])
        diag: dict = {}
        self.adapter._extract_jobs_from_dom(page, _variation(), limit=5, diagnostics=diag)
        self.assertEqual(diag["cards_scanned_by_extractor"], 5)
        self.assertEqual(diag["cumulative_eligible_jobs"], 5)


class _FakeLink:
    def __init__(self, href: str, title: str) -> None:
        self.href = href
        self.title = title

    @property
    def first(self):
        return self

    def count(self) -> int:
        return 1

    def get_attribute(self, name: str, timeout: int | None = None):
        return {"href": self.href, "aria-label": self.title}.get(name)

    def inner_text(self, timeout: int | None = None) -> str:
        return self.title


class _FakeCard:
    def __init__(self, job_id: int, title: str) -> None:
        self.link = _FakeLink(f"/jobs/view/{job_id}/?trk=search", title)

    def locator(self, _selector: str):
        return self.link

    def scroll_into_view_if_needed(self) -> None:
        return None


class _FakeCardList:
    def __init__(self, cards: list[_FakeCard]) -> None:
        self.cards = cards

    @property
    def first(self):
        return SimpleNamespace(wait_for=lambda **_kwargs: None)

    def count(self) -> int:
        return len(self.cards)

    def nth(self, index: int) -> _FakeCard:
        return self.cards[index]


class FakeLinkedInSearchPage:
    """A LinkedIn search-results page whose cards carry the given titles. Records every job
    page opened, so it is observable which cards used a slot of the limit."""

    def __init__(self, titles: list[str]) -> None:
        self.titles = {1000 + i: title for i, title in enumerate(titles)}
        self.cards = [_FakeCard(job_id, title) for job_id, title in self.titles.items()]
        self.url = "about:blank"
        self.opened_jobs: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.url = url
        if "/jobs/view/" in url:
            self.opened_jobs.append(url)

    def locator(self, selector: str):
        if "data-occludable-job-id" in selector:
            return _FakeCardList(self.cards)
        return SimpleNamespace(first=SimpleNamespace(wait_for=lambda **_kwargs: None))

    def wait_for_timeout(self, _ms: int) -> None:
        return None

    def title(self) -> str:
        return ""

    def current_title(self) -> str:
        job_id = int(self.url.rstrip("/").rsplit("/", 1)[-1])
        return self.titles[job_id]


class LinkedInSeniorExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.automation = LinkedInAutomation(Settings(openai_api_key=None))

    def _search(self, titles: list[str], limit: int) -> tuple[list[dict], FakeLinkedInSearchPage]:
        page = FakeLinkedInSearchPage(titles)

        def first_text(_page, selectors):
            if ".jobs-unified-top-card__job-title" in selectors:
                return page.current_title()
            if ".jobs-unified-top-card__company-name" in selectors:
                return "Example Co"
            return ""

        self.automation._first_text = first_text
        self.automation._detect_easy_apply = lambda *_args: (object(), {"reason": "easy_apply_found"})
        self.automation._posted_phrase = lambda *_args: "1 hour ago"
        resume = SimpleNamespace(id=1, detected_role="", job_titles=[], skills=[], keywords=[])
        jobs = self.automation._search_jobs_on_page(
            None, page, resume, "Remote", "DevOps Engineer", limit, False, "test"
        )
        return jobs, page

    # 17/18. LinkedIn skips a senior card without using a slot: the next card replaces it.
    def test_senior_cards_are_skipped_without_using_a_slot(self) -> None:
        titles = ["Senior DevOps Engineer", "Junior DevOps Engineer", "DevOps Architect",
                  "Cloud Engineer", "Staff DevOps Engineer", "DevOps Engineer", "Site Reliability Engineer"]
        jobs, page = self._search(titles, limit=3)

        self.assertEqual([job["title"] for job in jobs],
                         ["Junior DevOps Engineer", "Cloud Engineer", "DevOps Engineer"])
        self.assertEqual(len(page.opened_jobs), 3, "a senior job page is never opened")

    def test_without_senior_cards_the_first_limit_cards_are_used_as_before(self) -> None:
        titles = ["DevOps Engineer", "Cloud Engineer", "Junior DevOps Engineer", "Support Engineer"]
        jobs, _page = self._search(titles, limit=2)
        self.assertEqual([job["title"] for job in jobs], ["DevOps Engineer", "Cloud Engineer"])

    def test_a_page_of_only_senior_cards_returns_nothing_rather_than_padding(self) -> None:
        jobs, page = self._search(["Senior DevOps Engineer", "Lead DevOps Engineer"], limit=5)
        self.assertEqual(jobs, [])
        self.assertEqual(page.opened_jobs, [])

    def test_an_unreadable_card_title_is_not_treated_as_senior(self) -> None:
        class Unreadable:
            def get_attribute(self, *_args, **_kwargs):
                raise RuntimeError("detached")

        self.assertFalse(self.automation._card_is_senior_level(Unreadable()))

    def test_the_card_title_comes_from_the_job_link(self) -> None:
        self.assertTrue(self.automation._card_is_senior_level(_FakeLink("/jobs/view/1/", "Principal Cloud Engineer")))
        self.assertFalse(self.automation._card_is_senior_level(
            _FakeLink("/jobs/view/1/", "Associate Software Engineer\nAssociate Software Engineer with verification")
        ))


class PortalIndependenceTests(unittest.TestCase):
    def test_the_policy_module_depends_on_no_portal(self) -> None:
        source = Path("app/services/seniority_filter.py").read_text(encoding="utf-8")
        for forbidden in ("linkedin", "dice", "playwright", "sqlalchemy", "app.models", "app.database"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(f"import {forbidden}", source.lower())
                self.assertNotIn(f"from {forbidden}", source.lower())
                self.assertNotIn(f"from app.services.{forbidden}", source.lower())


if __name__ == "__main__":
    unittest.main()
