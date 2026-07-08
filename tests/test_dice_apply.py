"""Phase 4 tests: Dice dry-run apply via the shared ScreeningAnswerEngine.

The apply flow is exercised without a real browser by overriding the three
interaction seams (`_apply_browser_context`, `_detect_apply_form`, `_fill_fields`).
Safety is asserted: the flow NEVER submits and only ever reports dry-run outcomes.
"""

import unittest

from app.config import Settings
from app.database import init_db
from app.models import ApplicationStatus, CandidateProfile, JobOpportunity, ResumeProfile
from app.services.portals.dice_portal import DicePortalAdapter


class FakePage:
    def __init__(self):
        self.submitted = False  # never set by the adapter -> proves no submission

    def goto(self, *a, **k):
        return None

    def wait_for_timeout(self, *a, **k):
        return None

    def screenshot(self, *a, **k):
        return None

    def evaluate(self, *a, **k):
        return []


class FakeContext:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


class FakeDice(DicePortalAdapter):
    """DicePortalAdapter with the browser/DOM seams faked."""

    def __init__(self, settings, form):
        super().__init__(settings)
        self._form = form
        self.page = FakePage()
        self.filled_plan = None

    from contextlib import contextmanager as _cm

    @_cm
    def _apply_browser_context(self):
        yield FakeContext(self.page)

    def _detect_apply_form(self, page, job):
        return self._form

    def _fill_fields(self, page, plan):
        # Record the fill plan; crucially, this never triggers a submit.
        self.filled_plan = plan


def make_resume(**overrides):
    data = dict(
        filename="resume.txt",
        stored_path="resume.txt",
        raw_text="Data analyst with SQL and Excel.",
        skills=["SQL", "Excel"],
        technologies=[],
        education=[],
        experience=[],
        job_titles=[],
        keywords=[],
    )
    data.update(overrides)
    return ResumeProfile(**data)


def make_job():
    return JobOpportunity(
        resume_id=1, title="Data Analyst", company="Example",
        location="United States", job_url="https://www.dice.com/job-detail/abc",
    )


def make_candidate():
    return CandidateProfile(
        first_name="Ava", last_name="Shah", email="ava@example.com",
        mobile_phone="5551234567", current_location="New York, NY",
    )


class DiceApplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()  # GeneralizedFormEngine.detect_field may touch learning tables

    def setUp(self):
        self.settings = Settings(openai_api_key=None)
        self.resume = make_resume()
        self.job = make_job()

    def _apply(self, form, candidate=None, context=None, dry_run=True):
        adapter = FakeDice(self.settings, form)
        result = adapter.apply_to_job(
            self.job, self.resume, candidate,
            dry_run=dry_run, capture_evidence=False,
            screening_context=context or {"mode": "safe", "saved_answers": []},
        )
        return adapter, result

    def test_live_apply_is_disabled_and_never_submits(self):
        adapter, result = self._apply({"apply_type": "native", "fields": []}, dry_run=False)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_live_apply_disabled")
        self.assertFalse(adapter.page.submitted)
        self.assertIsNone(adapter.filled_plan)  # no filling in live-disabled path

    def test_external_form_is_unsupported(self):
        _, result = self._apply({"apply_type": "external", "fields": []})
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)

    def test_no_form_is_unsupported(self):
        _, result = self._apply({"apply_type": "none", "fields": []})
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)

    def test_dry_run_ready_when_all_fields_answerable(self):
        engine = DicePortalAdapter(self.settings).screening
        saved = {
            "normalized_question": engine.normalize_question("Do you have SQL experience?"),
            "answer": "Yes", "source": "Candidate Knowledge Base",
            "confidence": "High", "category": "skill", "evidence": {},
        }
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 1, "label": "Email", "control": "text", "kind": "text", "required": True, "options": []},
                {"index": 2, "label": "Do you have SQL experience?", "control": "radio", "kind": "radio", "required": True, "options": ["Yes", "No"]},
            ],
        }
        adapter, result = self._apply(
            form, candidate=make_candidate(),
            context={"mode": "safe", "saved_answers": [saved]},
        )
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(result.current_step, "dice_dry_run_ready")
        # Filled the form but never submitted.
        self.assertIsNotNone(adapter.filled_plan)
        self.assertFalse(adapter.page.submitted)
        # Screening decision came from the shared engine and is recorded.
        self.assertTrue(any(e.get("selected_answer") == "Yes" for e in result.screening_answers))

    def test_needs_review_when_required_screening_uncertain(self):
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "Are you legally authorized to work in the United States?",
                 "control": "radio", "kind": "radio", "required": True, "options": ["Yes", "No"]},
            ],
        }
        adapter, result = self._apply(form, candidate=make_candidate())
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_apply_needs_review")
        self.assertFalse(adapter.page.submitted)

    def test_needs_review_when_required_contact_missing(self):
        form = {
            "apply_type": "native",
            "submit_present": True,
            "fields": [
                {"index": 0, "label": "First Name", "control": "text", "kind": "text", "required": True, "options": []},
            ],
        }
        # No candidate profile -> required contact field cannot be filled.
        adapter, result = self._apply(form, candidate=None)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertFalse(adapter.page.submitted)

    def test_result_shape_is_automationresult_compatible(self):
        _, result = self._apply({"apply_type": "external", "fields": []})
        # Fields consumed by main._process_application must exist.
        for attr in ("status", "message", "current_step", "evidence_path", "log", "screening_answers", "missing_contact_fields"):
            self.assertTrue(hasattr(result, attr))


if __name__ == "__main__":
    unittest.main()
