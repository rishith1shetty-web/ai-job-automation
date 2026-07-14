"""Dice live apply: dry_run=False must really submit; dry_run=True must never submit.

Root cause this pins: Dice had NO live submit path at all. `apply_to_job` opened with a hard
`if not dry_run: return NEEDS_REVIEW("dice_live_apply_disabled")` guard, so dry_run=False was
discarded before any form work happened, and the submit-ready branch unconditionally returned
DRY_RUN_READY.

NOTHING HERE PERFORMS A REAL SUBMISSION. Every page is a fake; `FakePage.submitted` records
whether the adapter *would* have clicked Dice's Submit control.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.models import ApplicationStatus, JobOpportunity, ResumeProfile
from app.services.portals.dice_portal import DicePortalAdapter


class FakeLocator:
    def __init__(self, page, selector: str, present: bool, enabled: bool = True) -> None:
        self._page = page
        self._selector = selector
        self._present = present
        self._enabled = enabled
        self.first = self

    def count(self) -> int:
        return 1 if self._present else 0

    def is_enabled(self) -> bool:
        return self._enabled

    def click(self, **_kw) -> None:
        if self._page.submit_raises:
            raise RuntimeError("detached from DOM")
        self._page.submitted = True
        self._page.body = self._page.body_after_submit


class FakePage:
    """A Dice apply page sitting at the submit step."""

    def __init__(
        self,
        submit_present: bool = True,
        submit_enabled: bool = True,
        body_after_submit: str = "Your application was submitted.",
        submit_raises: bool = False,
    ) -> None:
        self.submit_present = submit_present
        self.submit_enabled = submit_enabled
        self.body = "Review your application"
        self.body_after_submit = body_after_submit
        self.submit_raises = submit_raises
        self.submitted = False          # <- did the adapter click Dice's real Submit?
        self.url = "https://dice.com/apply/123"

    def locator(self, selector: str):
        is_submit = any(token in selector for token in ("submit", "Submit"))
        return FakeLocator(self, selector, self.submit_present and is_submit, self.submit_enabled)

    def inner_text(self, _selector: str) -> str:
        return self.body

    def screenshot(self, **_kw) -> None:
        pass

    def content(self) -> str:
        return f"<html>{self.body}</html>"


class DiceLiveApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.debug_dir = Path(self._tmp.name)
        self.adapter._active_debug_dir = self.debug_dir
        self.job = JobOpportunity(
            id=1, resume_id=1, portal="dice", title="Data Analyst", company="Acme",
            location="Remote", job_url="https://dice.com/j/1", description="",
            status=ApplicationStatus.MATCHED,
        )
        self.resume = ResumeProfile(id=1, portal="dice", filename="r.pdf", stored_path="", raw_text="x")
        # a fully answerable native form: every safety gate above the submit fork passes
        self.form = {"apply_type": "native", "submit_present": True, "fields": []}

    def _plan(self, page, *, dry_run: bool):
        return self.adapter._plan_dry_run(
            page, self.form, self.job, self.resume, None, {}, self.debug_dir, [], dry_run=dry_run,
        )

    # -- 8. dry run still never submits -------------------------------------------------
    def test_8_dry_run_reaches_submit_ready_and_never_submits(self) -> None:
        page = FakePage()
        result = self._plan(page, dry_run=True)

        self.assertFalse(page.submitted)                        # THE invariant
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(result.current_step, "dice_dry_run_ready")
        self.assertIn("NOT submitted", result.message)

    def test_omitted_dry_run_flag_defaults_to_safe(self) -> None:
        """An omitted flag must never mean 'submit'."""
        page = FakePage()
        result = self.adapter._plan_dry_run(
            page, self.form, self.job, self.resume, None, {}, self.debug_dir, [],
        )
        self.assertFalse(page.submitted)
        self.assertEqual(result.status, ApplicationStatus.DRY_RUN_READY)

    # -- 7 + 9. live mode submits and only then reports APPLIED --------------------------
    def test_7_live_mode_does_not_return_dry_run_ready_at_submit_ready(self) -> None:
        page = FakePage()
        result = self._plan(page, dry_run=False)

        self.assertNotEqual(result.status, ApplicationStatus.DRY_RUN_READY)
        self.assertNotEqual(result.current_step, "dice_dry_run_ready")

    def test_9_live_mode_returns_applied_only_after_confirmation(self) -> None:
        page = FakePage(body_after_submit="Your application was submitted.")
        result = self._plan(page, dry_run=False)

        self.assertTrue(page.submitted)                         # it really clicked Submit
        self.assertEqual(result.status, ApplicationStatus.APPLIED)
        self.assertEqual(result.current_step, "dice_submitted")

    # -- 10. an unconfirmed submit must never be reported as APPLIED ----------------------
    def test_10_unconfirmed_submission_is_not_reported_as_applied(self) -> None:
        page = FakePage(body_after_submit="Something went wrong.")
        self.adapter._submission_confirmed = lambda _page, timeout_ms=0: False   # no waiting
        result = self._plan(page, dry_run=False)

        self.assertTrue(page.submitted)
        self.assertNotEqual(result.status, ApplicationStatus.APPLIED)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_submit_unconfirmed")
        self.assertIn("manually", result.message)

    def test_missing_submit_control_is_reviewed_not_applied(self) -> None:
        page = FakePage(submit_present=False)
        result = self._plan(page, dry_run=False)

        self.assertFalse(page.submitted)
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)
        self.assertEqual(result.current_step, "dice_submit_not_found")

    def test_submit_click_failure_is_failed_not_applied(self) -> None:
        page = FakePage(submit_raises=True)
        result = self._plan(page, dry_run=False)

        self.assertFalse(page.submitted)
        self.assertEqual(result.status, ApplicationStatus.FAILED)
        self.assertEqual(result.current_step, "dice_submit_failed")

    # -- safety gates still short-circuit BEFORE the submit fork --------------------------
    def test_live_mode_never_submits_an_unsupported_form(self) -> None:
        page = FakePage()
        form = {"apply_type": "external", "submit_present": False, "fields": []}
        result = self.adapter._plan_dry_run(
            page, form, self.job, self.resume, None, {}, self.debug_dir, [], dry_run=False,
        )
        self.assertFalse(page.submitted)                        # never reached submit
        self.assertEqual(result.status, ApplicationStatus.UNSUPPORTED_FORM)

    def test_live_mode_never_submits_when_the_review_step_is_incomplete(self) -> None:
        page = FakePage()
        self.adapter._review_missing_required = ["Phone number"]
        result = self._plan(page, dry_run=False)

        self.assertFalse(page.submitted)                        # safety gate held
        self.assertEqual(result.status, ApplicationStatus.NEEDS_REVIEW)

    # -- confirmation matcher ---------------------------------------------------------------
    def test_confirmation_requires_positive_proof(self) -> None:
        matches = self.adapter.SUBMIT_CONFIRMATION_RE.search
        self.assertTrue(matches("Your application was submitted"))
        self.assertTrue(matches("Application sent"))
        self.assertTrue(matches("Thanks for applying!"))
        self.assertTrue(matches("You have successfully applied"))
        # silence / an error page is NOT confirmation
        self.assertFalse(matches("Review your application"))
        self.assertFalse(matches("Something went wrong"))
        self.assertFalse(matches(""))


if __name__ == "__main__":
    unittest.main()
