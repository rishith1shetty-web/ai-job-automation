"""Regression: manual assist must work on the apply-selected path, not only in the Apply Top
batch runner.

The reported bug: a LinkedIn resume mismatch closed the browser and moved on. Root cause was
NOT in the LinkedIn resume code -- it was that `apply_selected_job` never attached the
coordinator, so `pause_for_manual_input` short-circuited to UNAVAILABLE and the caller fell
through to its pre-existing needs-review exit, unwinding the browser context.

The threading here is real: the endpoint runs on its own thread and BLOCKS inside the paused
job (exactly as it does under FastAPI's threadpool), while the test acts as the API thread.
"""

import threading
import unittest
from uuid import uuid4

from app.database import SessionLocal, init_db
from app.main import (
    apply_selected_job,
    get_manual_assist_state,
    continue_manual_assist,
    set_active_portal,
    skip_manual_assist,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobOpportunity,
    ResumeProfile,
)
from app.schemas import ApplySelectedRequest, ManualAssistRequest
from app.services.linkedin import AutomationResult
from app.services.manual_assist import ManualAssistMixin, ManualOutcome, manual_assist_coordinator
from app.services.resume_identity import compute_resume_fingerprint

RESUME_TEXT = "Jane Doe\nData Analyst\nSkills: SQL"


class ResumeMismatchAutomation(ManualAssistMixin):
    """Stands in for LinkedInAutomation at the moment the resume step cannot be verified.

    `browser_closed` records the `finally` that tears the browser down -- the very thing the
    user saw happen. If the pause works, it must not run until the user has decided.
    """

    resume_fixed = False
    browser_closed = False
    paused = False

    def __init__(self, _settings=None) -> None:
        self.manual_assist = None
        self.manual_assist_batch_id = None

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None) -> AutomationResult:
        try:  # the real code wraps this in `with self._browser_context()`
            outcome = self.pause_for_manual_input(
                portal="linkedin",
                job=job,
                dry_run=dry_run,
                current_step="manual_input_required",
                question="Select the resume 'r.pdf' on the LinkedIn resume step",
                reason="Automation could not confirm the correct resume is selected.",
                validate=lambda: ResumeMismatchAutomation.resume_fixed,
            )
            ResumeMismatchAutomation.paused = outcome is not ManualOutcome.UNAVAILABLE
            if outcome is ManualOutcome.SKIPPED:
                return AutomationResult(ApplicationStatus.SKIPPED, "Skipped by user.", [],
                                        "skipped_by_user", None)
            if outcome is ManualOutcome.COMPLETED:
                return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [],
                                        "dry_run_complete", None)
            # UNAVAILABLE -> the old behaviour: give up, and the browser closes on the way out.
            return AutomationResult(ApplicationStatus.NEEDS_REVIEW, "Resume upload mismatch detected.",
                                    [], "needs_review", None)
        finally:
            ResumeMismatchAutomation.browser_closed = True


class ManualAssistApplySelectedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        ResumeMismatchAutomation.resume_fixed = False
        ResumeMismatchAutomation.browser_closed = False
        ResumeMismatchAutomation.paused = False
        manual_assist_coordinator.clear()
        manual_assist_coordinator.set_listeners(None, None)

        self.db = SessionLocal()
        set_active_portal(self.db, "linkedin")
        self.resume = ResumeProfile(
            portal="linkedin", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.job = JobOpportunity(
            resume_id=self.resume.id, portal="linkedin", title="Data Analyst", company="Acme",
            location="Remote", job_url=f"https://li/sel/{uuid4().hex}", description="",
            easy_apply=True, status=ApplicationStatus.MATCHED,
        )
        self.db.add(self.job)
        self.db.commit()
        self.job_id = self.job.id

    def tearDown(self) -> None:
        manual_assist_coordinator.clear()
        manual_assist_coordinator.set_listeners(None, None)
        self.db.query(ApplicationRecord).filter(
            ApplicationRecord.job_id == self.job_id
        ).delete(synchronize_session=False)
        self.db.delete(self.db.get(JobOpportunity, self.job_id))
        self.db.delete(self.resume)
        self.db.commit()
        self.db.close()

    # -- helpers ------------------------------------------------------------------------
    def _start_apply(self) -> threading.Thread:
        from unittest.mock import patch

        def run() -> None:
            with patch("app.main.LinkedInAutomation", ResumeMismatchAutomation):
                with SessionLocal() as db:   # its own session: this is a different thread
                    try:
                        apply_selected_job(
                            ApplySelectedRequest(
                                resume_id=self.resume.id, job_id=self.job_id, dry_run=True
                            ),
                            db,
                        )
                    except Exception:   # pragma: no cover - surfaced via the assertions below
                        pass

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await_pause()
        return thread

    def _await_pause(self, after: int = 0) -> dict:
        for _ in range(2000):
            state = manual_assist_coordinator.snapshot()
            if state and state["sequence"] > after:
                return state
            threading.Event().wait(0.01)
        self.fail("apply-selected never paused (manual assist was not armed)")

    def _record(self) -> ApplicationRecord | None:
        with SessionLocal() as db:
            return db.query(ApplicationRecord).filter(
                ApplicationRecord.job_id == self.job_id
            ).one_or_none()

    # -- the regression ------------------------------------------------------------------
    def test_resume_mismatch_pauses_instead_of_closing_the_browser(self) -> None:
        thread = self._start_apply()

        # The automation thread is parked INSIDE apply_to_job: the browser teardown has not run.
        self.assertTrue(thread.is_alive())
        self.assertFalse(ResumeMismatchAutomation.browser_closed)
        self.assertIsNone(self._record())        # pausing is not an outcome; no record yet

        state = get_manual_assist_state()        # the HTTP thread sees it without touching the page
        self.assertTrue(state["active"])
        self.assertEqual(state["portal"], "linkedin")
        self.assertEqual(state["job_id"], self.job_id)
        self.assertEqual(state["company"], "Acme")
        self.assertEqual(state["current_step"], "manual_input_required")
        self.assertTrue(state["dry_run"])

        skip_manual_assist(ManualAssistRequest(batch_id=state["batch_id"]))
        thread.join(timeout=30)

    def test_continue_resumes_the_same_application_after_the_user_fixes_the_resume(self) -> None:
        thread = self._start_apply()
        ResumeMismatchAutomation.resume_fixed = True     # user selects the right resume in the browser
        continue_manual_assist(ManualAssistRequest(batch_id=manual_assist_coordinator.snapshot()["batch_id"]))
        thread.join(timeout=30)

        record = self._record()
        self.assertIsNotNone(record)
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)   # NOT needs_review
        self.assertTrue(ResumeMismatchAutomation.browser_closed)           # closed only at the end
        self.assertFalse(manual_assist_coordinator.is_paused)

    def test_continue_without_fixing_it_re_pauses_and_never_accepts_the_wrong_resume(self) -> None:
        thread = self._start_apply()
        first = manual_assist_coordinator.snapshot()

        continue_manual_assist(ManualAssistRequest(batch_id=first["batch_id"]))   # not fixed
        state = self._await_pause(after=first["sequence"])

        self.assertEqual(state["attempt"], 2)
        self.assertTrue(thread.is_alive())
        self.assertFalse(ResumeMismatchAutomation.browser_closed)
        self.assertIsNone(self._record())

        ResumeMismatchAutomation.resume_fixed = True
        continue_manual_assist(
            ManualAssistRequest(batch_id=state["batch_id"], sequence=state["sequence"])
        )
        thread.join(timeout=30)
        self.assertEqual(self._record().status, ApplicationStatus.DRY_RUN_READY)

    def test_skip_marks_only_this_job_skipped(self) -> None:
        thread = self._start_apply()
        skip_manual_assist(ManualAssistRequest(batch_id=manual_assist_coordinator.snapshot()["batch_id"]))
        thread.join(timeout=30)

        record = self._record()
        self.assertEqual(record.status, ApplicationStatus.SKIPPED)    # not FAILED, not NEEDS_REVIEW
        self.assertFalse(manual_assist_coordinator.is_paused)

    def test_coordinator_is_released_after_the_apply(self) -> None:
        thread = self._start_apply()
        skip_manual_assist(ManualAssistRequest(batch_id=manual_assist_coordinator.snapshot()["batch_id"]))
        thread.join(timeout=30)

        self.assertIsNone(manual_assist_coordinator.snapshot())
        self.assertEqual(get_manual_assist_state(), {"active": False})

    def test_every_apply_entry_point_arms_manual_assist(self) -> None:
        """The bug was a whole apply entry point silently running without a coordinator. Guard
        it structurally: if a future endpoint calls _process_application without manual_assist,
        its in-form pauses would be dead and the browser would close on the user again."""
        from pathlib import Path

        source = Path("app/main.py").read_text(encoding="utf-8")
        call_sites = source.count("_process_application(\n")
        armed = source.count("manual_assist=manual_assist_coordinator")
        self.assertEqual(
            call_sites,
            armed,
            f"{call_sites} _process_application call sites but only {armed} pass "
            "manual_assist=manual_assist_coordinator",
        )


if __name__ == "__main__":
    unittest.main()
