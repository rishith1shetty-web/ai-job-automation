import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.models import ApplicationStatus, JobOpportunity, ResumeProfile
from app.services.find_matches import FindMatchesEngine, SearchVariation
from app.services.matcher import MatchScorer
from app.services.portals.dice_portal import DicePortalAdapter


class PortalAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            openai_api_key=None,
            dice_storage_state=Path(tempfile.mkdtemp()) / "dice_state.json",
            dice_debug_dir=Path(tempfile.mkdtemp()) / "dice_debug",
            find_matches_debug_dir=Path(tempfile.mkdtemp()) / "find_matches_debug",
        )
        self.resume = ResumeProfile(
            id=1,
            filename="resume.pdf",
            stored_path="resume.pdf",
            raw_text="Data analyst with Python SQL Excel Power BI reporting dashboards",
            skills=["Python", "SQL", "Excel", "Power BI"],
            experience=[],
            education=[],
            job_titles=["Data Analyst"],
            keywords=["reporting", "analytics"],
            technologies=["Python", "SQL"],
            domain_expertise=["Data Analytics"],
            detected_role="Data Analyst",
            role_suggestions=[{"role": "Data Analyst", "confidence": 86}],
            generated_search_keywords=["Data Analyst"],
            generated_search_query="Data Analyst",
            role_confidence=86,
        )

    def test_dice_search_result_parsing(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        variation = SearchVariation(
            query="Data Analyst",
            location="Remote",
            role="Data Analyst",
            expanded_role="Data Analyst",
            pass_name="primary",
            search_role="Data Analyst",
            role_priority="primary",
            role_confidence=86,
        )

        job = adapter.job_from_card_text(
            "Data Analyst\nCompany\nDice Analytics\nRemote\nQuick Apply\nPython SQL dashboards",
            "https://www.dice.com/job-detail/123",
            variation,
        )

        self.assertEqual(job["portal"], "dice")
        self.assertEqual(job["title"], "Data Analyst")
        self.assertEqual(job["company"], "Dice Analytics")
        self.assertTrue(job["easy_apply"])
        self.assertEqual(job["form_provider"], "dice")

    def test_dice_search_url_generation(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        variation = SearchVariation(
            query="Data Analyst",
            location="New York, NY",
            role="Data Analyst",
            expanded_role="Data Analyst",
            pass_name="primary",
        )

        url = adapter.search_url(variation)

        self.assertIn("https://www.dice.com/jobs", url)
        self.assertIn("q=Data+Analyst", url)
        self.assertIn("location=New+York%2C+NY", url)
        self.assertIn("pageSize=20", url)

    def test_dice_search_returns_collected_jobs_from_sample_html(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        variation = SearchVariation(
            query="Data Analyst",
            location="Remote",
            role="Data Analyst",
            expanded_role="Data Analyst",
            pass_name="primary",
            search_role="Data Analyst",
            role_priority="primary",
        )
        page = _FakeDiceSearchPage(
            jobs=[
                {
                    "title": "Data Analyst",
                    "company": "Dice Analytics",
                    "location": "Remote",
                    "job_url": "/job-detail/abc123",
                    "apply_url": "/job-detail/abc123?apply=true",
                    "posted_date": "2 days ago",
                    "description": "Data Analyst\nDice Analytics\nRemote\nQuick Apply\nPython SQL Excel",
                    "easy_apply": True,
                }
            ]
        )

        jobs = adapter.search_jobs(page, variation, limit=5, debug_dir=self.settings.dice_debug_dir, search_index=1)

        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["portal"], "dice")
        self.assertEqual(jobs[0]["title"], "Data Analyst")
        self.assertEqual(jobs[0]["company"], "Dice Analytics")
        self.assertEqual(jobs[0]["job_url"], "https://www.dice.com/job-detail/abc123")
        self.assertTrue(jobs[0]["easy_apply"])
        self.assertEqual(jobs[0]["job_description_metadata"]["posted_date"], "2 days ago")

    def _easy_apply_variation(self) -> SearchVariation:
        return SearchVariation(
            query="Data Analyst",
            location="Remote",
            role="Data Analyst",
            expanded_role="Data Analyst",
            pass_name="primary",
            search_role="Data Analyst",
            role_priority="primary",
        )

    def test_dice_easy_apply_cta_is_kept(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Data Analyst",
                "company": "Native Co",
                "location": "Remote",
                "job_url": "/job-detail/1",
                "apply_url": "/job-detail/1",
                "apply_cta": "Easy apply",
                "description": "Data Analyst\nNative Co\nRemote",
            },
            self._easy_apply_variation(),
        )
        self.assertTrue(job["easy_apply"])
        self.assertTrue(job["job_description_metadata"]["apply_classification"].startswith("native_cta"))

    def test_dice_plain_apply_cta_is_filtered(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Data Analyst",
                "company": "Plain Co",
                "location": "Remote",
                "job_url": "/job-detail/2",
                "apply_url": "/job-detail/2",
                "apply_cta": "Apply",
                "description": "Data Analyst\nPlain Co\nRemote",
            },
            self._easy_apply_variation(),
        )
        self.assertFalse(job["easy_apply"])
        self.assertTrue(job["job_description_metadata"]["apply_classification"].startswith("non_native_cta"))

    def test_dice_apply_now_cta_is_filtered(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Data Analyst",
                "company": "Now Co",
                "location": "Remote",
                "job_url": "/job-detail/3",
                "apply_url": "/job-detail/3",
                "apply_cta": "Apply Now",
                "description": "Data Analyst\nNow Co\nRemote",
            },
            self._easy_apply_variation(),
        )
        self.assertFalse(job["easy_apply"])

    def test_dice_external_ats_apply_url_is_filtered(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Data Analyst",
                "company": "Workday Co",
                "location": "Remote",
                "job_url": "/job-detail/4",
                # Even with an "Easy apply"-looking chip, an external ATS host wins.
                "apply_url": "https://company.wd1.myworkdayjobs.com/apply/123",
                "apply_cta": "Easy apply",
                "description": "Data Analyst\nWorkday Co\nRemote",
            },
            self._easy_apply_variation(),
        )
        self.assertFalse(job["easy_apply"])
        self.assertEqual(job["job_description_metadata"]["apply_classification"], "external_ats:workday")

    def test_dice_external_cta_phrase_is_filtered(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Data Analyst",
                "company": "Site Co",
                "location": "Remote",
                "job_url": "/job-detail/5",
                "apply_url": "/job-detail/5",
                "apply_cta": "Apply on company site",
                "description": "Data Analyst\nSite Co\nRemote",
            },
            self._easy_apply_variation(),
        )
        self.assertFalse(job["easy_apply"])
        self.assertEqual(job["job_description_metadata"]["apply_classification"], "external_cta")

    def test_dice_description_mentioning_ats_tool_is_not_falsely_filtered(self) -> None:
        # A native Easy Apply job whose DESCRIPTION merely names an ATS tool must
        # still be kept -- external detection only inspects the apply URL/CTA.
        adapter = DicePortalAdapter(self.settings)
        job = adapter.job_from_card_data(
            {
                "title": "Integrations Engineer",
                "company": "Native Co",
                "location": "Remote",
                "job_url": "/job-detail/6",
                "apply_url": "/job-detail/6",
                "apply_cta": "Easy apply",
                "description": "Integrations Engineer\nNative Co\nBuild Workday and Greenhouse integrations.",
            },
            self._easy_apply_variation(),
        )
        self.assertTrue(job["easy_apply"])

    def test_dice_mixed_search_returns_only_easy_apply_with_reasons(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        page = _FakeDiceSearchPage(
            jobs=[
                {
                    "title": "Easy One", "company": "Native Co", "location": "Remote",
                    "job_url": "/job-detail/1", "apply_url": "/job-detail/1",
                    "apply_cta": "Easy apply", "description": "Easy One\nNative Co\nRemote",
                },
                {
                    "title": "Plain Two", "company": "Plain Co", "location": "Remote",
                    "job_url": "/job-detail/2", "apply_url": "/job-detail/2",
                    "apply_cta": "Apply", "description": "Plain Two\nPlain Co\nRemote",
                },
                {
                    "title": "Now Three", "company": "Now Co", "location": "Remote",
                    "job_url": "/job-detail/3", "apply_url": "/job-detail/3",
                    "apply_cta": "Apply Now", "description": "Now Three\nNow Co\nRemote",
                },
                {
                    "title": "Workday Four", "company": "WD Co", "location": "Remote",
                    "job_url": "/job-detail/4",
                    "apply_url": "https://company.wd1.myworkdayjobs.com/apply/9",
                    "apply_cta": "Apply", "description": "Workday Four\nWD Co\nRemote",
                },
            ]
        )

        jobs = adapter.search_jobs(page, self._easy_apply_variation(), limit=10, debug_dir=self.settings.dice_debug_dir, search_index=7)

        self.assertEqual([job["title"] for job in jobs], ["Easy One"])
        self.assertTrue(all(job["easy_apply"] for job in jobs))

        filter_file = self.settings.dice_debug_dir / "search_7_easy_apply_filter.json"
        self.assertTrue(filter_file.exists())
        report = json.loads(filter_file.read_text(encoding="utf-8"))
        self.assertEqual(report["total_jobs_found"], 4)
        self.assertEqual(report["easy_apply_kept"], 1)
        self.assertEqual(report["external_or_apply_filtered"], 3)
        reasons = {row["title"]: row["reason"] for row in report["excluded_jobs"]}
        self.assertTrue(reasons["Plain Two"].startswith("non_native_cta"))
        self.assertTrue(reasons["Now Three"].startswith("non_native_cta"))
        self.assertEqual(reasons["Workday Four"], "external_ats:workday")

    def test_dice_zero_result_search_logs_clear_reason(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        variation = SearchVariation(
            query="Data Analyst",
            location="Remote",
            role="Data Analyst",
            expanded_role="Data Analyst",
            pass_name="primary",
        )
        page = _FakeDiceSearchPage(jobs=[])

        jobs = adapter.search_jobs(page, variation, limit=5, debug_dir=self.settings.dice_debug_dir, search_index=2)

        self.assertEqual(jobs, [])
        reason_file = self.settings.dice_debug_dir / "search_2_zero_result.json"
        self.assertTrue(reason_file.exists())
        reason = json.loads(reason_file.read_text(encoding="utf-8"))
        self.assertEqual(reason["reason"], "no_job_cards_found")
        self.assertIn("selectors_tried", reason)

    def test_dice_job_scoring_uses_existing_scorer(self) -> None:
        job = JobOpportunity(
            resume_id=1,
            portal="dice",
            title="Data Analyst",
            company="Dice Analytics",
            location="Remote",
            job_url="https://www.dice.com/job-detail/123",
            description="Python SQL Excel Power BI dashboards",
            easy_apply=True,
        )

        scored = MatchScorer(self.settings).score_jobs(self.resume, [job])[0]

        self.assertEqual(scored.portal, "dice")
        self.assertGreater(scored.match_score, 0)

    def test_mixed_linkedin_and_dice_ranking_preserves_portal(self) -> None:
        linkedin = JobOpportunity(
            resume_id=1,
            portal="linkedin",
            title="Data Analyst",
            company="LinkedIn Co",
            location="Remote",
            job_url="https://www.linkedin.com/jobs/view/1",
            description="Python SQL",
            easy_apply=True,
            match_score=70,
            completion_likelihood_score=90,
            application_friction_score=10,
        )
        dice = JobOpportunity(
            resume_id=1,
            portal="dice",
            title="Data Analyst",
            company="Dice Co",
            location="Remote",
            job_url="https://www.dice.com/job-detail/1",
            description="Python SQL Excel Power BI",
            easy_apply=True,
            match_score=82,
            completion_likelihood_score=80,
            application_friction_score=20,
        )

        ranked = FindMatchesEngine(self.settings).apply_priority_scores([linkedin, dice])

        self.assertEqual({job.portal for job in ranked}, {"linkedin", "dice"})
        self.assertTrue(all(job.priority_score >= 0 for job in ranked))

    def test_dice_apply_without_native_form_is_unsupported_form(self) -> None:
        from contextlib import contextmanager

        adapter = DicePortalAdapter(self.settings)
        job = JobOpportunity(
            id=99,
            resume_id=1,
            portal="dice",
            title="Data Analyst",
            company="Dice Co",
            location="Remote",
            job_url="https://www.dice.com/job-detail/99",
            description="Python SQL",
            easy_apply=True,
        )

        class _FakePage:
            def goto(self, *a, **k):
                return None

            def screenshot(self, *a, **k):
                return None

            def wait_for_timeout(self, *a, **k):
                return None

        @contextmanager
        def _fake_ctx():
            class _Ctx:
                def new_page(self):
                    return _FakePage()

            yield _Ctx()

        # No native Dice apply form -> classified as unsupported_form (dry-run, no submit).
        with patch.object(adapter, "_apply_browser_context", _fake_ctx), patch.object(
            adapter, "_detect_apply_form", return_value={"apply_type": "none", "fields": [], "submit_present": False}
        ):
            result = adapter.apply_to_job(job, self.resume, None, dry_run=True, capture_evidence=True)

        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)
        self.assertEqual(result.current_step, "dice_apply_unsupported_form")
        self.assertTrue(result.evidence_path)

    def test_existing_saved_dice_session_requires_authenticated_cookie(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        adapter.storage_state.write_text(
            json.dumps({"cookies": [{"domain": ".dice.com", "name": "consent", "value": "yes"}], "origins": []}),
            encoding="utf-8",
        )

        self.assertFalse(adapter.is_connected())

        adapter.storage_state.write_text(
            json.dumps({"cookies": [{"domain": ".dice.com", "name": "session_token", "value": "abc"}], "origins": []}),
            encoding="utf-8",
        )

        self.assertTrue(adapter.is_connected())

    def test_dice_connect_successful_manual_login_saves_session(self) -> None:
        adapter = DicePortalAdapter(self.settings)

        with patch("app.services.portals.dice_portal.sync_playwright", return_value=_FakePlaywrightManager("success")):
            result = adapter.connect()

        self.assertEqual(result["status"], "connected")
        self.assertTrue(adapter.storage_state.exists())
        self.assertTrue(adapter.is_connected())

    def test_dice_connect_browser_closed_before_login_does_not_save_session(self) -> None:
        adapter = DicePortalAdapter(self.settings)

        with patch("app.services.portals.dice_portal.sync_playwright", return_value=_FakePlaywrightManager("closed")):
            result = adapter.connect()

        self.assertEqual(result["status"], "not_connected")
        self.assertFalse(adapter.storage_state.exists())

    def test_dice_connect_invalid_login_does_not_save_session(self) -> None:
        adapter = DicePortalAdapter(self.settings)
        adapter.login_timeout_seconds = 0

        with patch("app.services.portals.dice_portal.sync_playwright", return_value=_FakePlaywrightManager("invalid")):
            result = adapter.connect()

        self.assertEqual(result["status"], "not_connected")
        self.assertFalse(adapter.storage_state.exists())


class _FakePlaywrightManager:
    def __init__(self, mode: str):
        self.chromium = _FakeChromium(mode)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeChromium:
    def __init__(self, mode: str):
        self.mode = mode

    def launch(self, **kwargs):
        return _FakeBrowser(self.mode)


class _FakeBrowser:
    def __init__(self, mode: str):
        self.mode = mode
        self.connected = True

    def new_context(self):
        return _FakeContext(self.mode)

    def is_connected(self):
        return self.connected

    def close(self):
        self.connected = False


class _FakeContext:
    def __init__(self, mode: str):
        self.mode = mode

    def new_page(self):
        return _FakePage(self.mode)

    def storage_state(self, path: str | None = None):
        state = {
            "cookies": (
                [{"domain": ".dice.com", "name": "session_token", "value": "abc"}]
                if self.mode == "success"
                else [{"domain": ".dice.com", "name": "consent", "value": "yes"}]
            ),
            "origins": [],
        }
        if path:
            Path(path).write_text(json.dumps(state), encoding="utf-8")
        return state


class _FakePage:
    def __init__(self, mode: str):
        self.mode = mode
        self.url = "https://www.dice.com/dashboard" if mode == "success" else "https://www.dice.com/dashboard/login"

    def goto(self, *args, **kwargs):
        return None

    def is_closed(self):
        return self.mode == "closed"

    def wait_for_timeout(self, ms: int):
        return None

    def locator(self, selector: str):
        return _FakeLocator()


class _FakeLocator:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def inner_text(self, timeout: int = 0):
        return ""


class _FakeDiceSearchPage:
    def __init__(self, jobs: list[dict]):
        self.jobs = jobs
        self.url = "about:blank"

    def goto(self, url: str, *args, **kwargs):
        self.url = url

    def wait_for_load_state(self, *args, **kwargs):
        return None

    def wait_for_timeout(self, ms: int):
        return None

    def screenshot(self, path: str, full_page: bool = True):
        Path(path).write_bytes(b"fake screenshot")

    def content(self):
        return "<html><body><dhi-search-card>Data Analyst</dhi-search-card></body></html>"

    def evaluate(self, script: str, args: dict):
        return self.jobs

    def locator(self, selector: str):
        return _FakeLocator()


if __name__ == "__main__":
    unittest.main()
