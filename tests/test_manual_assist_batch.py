"""Manual assist wired into the Apply Top batch + the HTTP handlers.

The automation is faked, but the THREADING is real: the batch runs on its own thread and
blocks inside the paused job exactly as in production, while the test acts as the API
thread. That is what makes "the next job must not start while paused" a real assertion.
"""

import threading
import unittest
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.database import engine, init_db, SessionLocal
from app.main import (
    _run_apply_top_batch,
    continue_manual_assist,
    get_manual_assist_state,
    skip_manual_assist,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplyTopBatch,
    CandidateProfile,
    JobOpportunity,
    ResumeProfile,
)
from app.schemas import ManualAssistRequest
from app.services.linkedin import AutomationResult
from app.services.manual_assist import ManualOutcome, manual_assist_coordinator
from app.services.resume_identity import compute_resume_fingerprint

RESUME_TEXT = "Jane Doe\nData Analyst\nSkills: SQL"


class PausingAutomation:
    """Job 1 needs manual help; every other job succeeds immediately. Records the exact
    order jobs were started so we can prove job 2 never begins while job 1 is paused."""

    started: list[int] = []

    def __init__(self, _settings=None) -> None:
        self.manual_assist = None
        self.manual_assist_batch_id = None

    def attach_manual_assist(self, coordinator, batch_id=None) -> None:
        self.manual_assist = coordinator
        self.manual_assist_batch_id = batch_id

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None) -> AutomationResult:
        PausingAutomation.started.append(job.id)
        if job.id == PausingAutomation.pause_job_id:
            from app.services.manual_assist import ManualAssistMixin
            outcome = ManualAssistMixin.pause_for_manual_input(
                self,
                portal="linkedin",
                job=job,
                dry_run=dry_run,
                current_step="manual_input_required",
                question="Years of Kubernetes experience?",
                reason="Automation could not answer this screening question.",
                validate=lambda: PausingAutomation.answered,
            )
            if outcome is ManualOutcome.SKIPPED:
                return AutomationResult(ApplicationStatus.SKIPPED, "Skipped by user.", [],
                                        "skipped_by_user", None)
            if outcome is ManualOutcome.COMPLETED:
                return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [],
                                        "dry_run_complete", None)
        return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [], "dry_run_complete", None)


class ManualAssistBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        PausingAutomation.started = []
        PausingAutomation.answered = False
        manual_assist_coordinator.clear()
        manual_assist_coordinator.set_listeners(None, None)

        self.db = SessionLocal()
        self.resume = ResumeProfile(
            portal="linkedin", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.jobs = []
        for i in range(3):
            job = JobOpportunity(
                resume_id=self.resume.id, portal="linkedin", title=f"Job {i}", company="Acme",
                location="Remote", job_url=f"https://li/manual/{uuid4().hex}", description="",
                easy_apply=True, status=ApplicationStatus.MATCHED,
            )
            self.db.add(job)
            self.jobs.append(job)
        self.db.commit()
        PausingAutomation.pause_job_id = self.jobs[0].id

        self.batch = ApplyTopBatch(
            id=str(uuid4()), resume_id=self.resume.id, portal="linkedin",
            status="queued", dry_run=True, limit=10,
        )
        self.db.add(self.batch)
        self.db.commit()
        self.batch_id = self.batch.id

    def tearDown(self) -> None:
        manual_assist_coordinator.clear()
        manual_assist_coordinator.set_listeners(None, None)
        self.db.query(ApplicationRecord).filter(
            ApplicationRecord.job_id.in_([j.id for j in self.jobs])
        ).delete(synchronize_session=False)
        for job in self.jobs:
            self.db.delete(job)
        self.db.delete(self.db.get(ApplyTopBatch, self.batch_id))
        self.db.delete(self.resume)
        self.db.commit()
        self.db.close()

    # -- helpers ------------------------------------------------------------------------
    def _start_batch(self) -> threading.Thread:
        from unittest.mock import patch

        def run() -> None:
            with patch("app.main.LinkedInAutomation", PausingAutomation):
                _run_apply_top_batch(self.batch_id)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await_pause()
        return thread

    def _await_pause(self, after: int = 0) -> dict:
        # Generous budget: the batch thread does real DB work before it reaches the pause, and
        # under a loaded full-suite run that can take several seconds.
        for _ in range(2000):
            state = manual_assist_coordinator.snapshot()
            if state and state["sequence"] > after:
                return state
            threading.Event().wait(0.01)
        self.fail("batch never paused")

    def _batch_row(self) -> ApplyTopBatch:
        with SessionLocal() as db:
            return db.get(ApplyTopBatch, self.batch_id)

    def _records(self) -> list[ApplicationRecord]:
        with SessionLocal() as db:
            return list(
                db.query(ApplicationRecord)
                .filter(ApplicationRecord.job_id.in_([j.id for j in self.jobs]))
                .all()
            )

    # -- 7, 8, 13: pause blocks the batch ----------------------------------------------
    def test_batch_pauses_and_does_not_start_the_next_job(self) -> None:
        thread = self._start_batch()

        self.assertTrue(thread.is_alive())
        self.assertEqual(PausingAutomation.started, [self.jobs[0].id])  # job 2 NOT started
        self.assertEqual(self._batch_row().status, "paused")            # non-terminal status

        # The HTTP handler sees the pause without ever touching Playwright.
        state = get_manual_assist_state()
        self.assertTrue(state["active"])
        self.assertEqual(state["batch_id"], self.batch_id)
        self.assertEqual(state["portal"], "linkedin")
        self.assertEqual(state["job_id"], self.jobs[0].id)
        self.assertEqual(state["company"], "Acme")
        self.assertEqual(state["question"], "Years of Kubernetes experience?")
        self.assertEqual(state["current_step"], "manual_input_required")
        self.assertTrue(state["dry_run"])
        self.assertIn("paused_at", state)

        # No record is written for the pause itself -- pausing is not an outcome.
        self.assertEqual(self._records(), [])

        skip_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

    # -- 9: continue resumes -------------------------------------------------------------
    def test_continue_resumes_the_batch_and_finishes_every_job(self) -> None:
        thread = self._start_batch()
        PausingAutomation.answered = True                      # user answers in the browser
        continue_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

        self.assertEqual(PausingAutomation.started, [j.id for j in self.jobs])  # 2 and 3 ran
        batch = self._batch_row()
        self.assertEqual(batch.status, "completed")
        records = {r.job_id: r for r in self._records()}
        self.assertEqual(len(records), 3)                       # one record per job
        self.assertEqual(records[self.jobs[0].id].status, ApplicationStatus.DRY_RUN_READY)
        self.assertFalse(manual_assist_coordinator.is_paused)   # coordinator cleared

    # -- failed re-validation re-pauses --------------------------------------------------
    def test_continue_without_answering_re_pauses_and_batch_stays_paused(self) -> None:
        thread = self._start_batch()
        first = manual_assist_coordinator.snapshot()["sequence"]

        continue_manual_assist(ManualAssistRequest(batch_id=self.batch_id))  # not answered
        state = self._await_pause(after=first)

        self.assertEqual(state["attempt"], 2)
        self.assertIn("still looks incomplete", state["reason"])
        self.assertEqual(PausingAutomation.started, [self.jobs[0].id])   # still no job 2
        self.assertTrue(thread.is_alive())

        PausingAutomation.answered = True
        continue_manual_assist(ManualAssistRequest(batch_id=self.batch_id, sequence=state["sequence"]))
        thread.join(timeout=30)
        self.assertEqual(self._batch_row().status, "completed")

    # -- 10: skip -------------------------------------------------------------------------
    def test_skip_marks_only_that_job_skipped_and_batch_continues(self) -> None:
        thread = self._start_batch()
        skip_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

        self.assertEqual(PausingAutomation.started, [j.id for j in self.jobs])  # batch continued
        records = {r.job_id: r for r in self._records()}
        self.assertEqual(len(records), 3)
        self.assertEqual(records[self.jobs[0].id].status, ApplicationStatus.SKIPPED)   # not FAILED
        self.assertEqual(records[self.jobs[1].id].status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(self._batch_row().status, "completed")

    # -- 11: coordinator cleanup ----------------------------------------------------------
    def test_coordinator_is_cleared_after_the_batch(self) -> None:
        thread = self._start_batch()
        skip_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

        self.assertFalse(manual_assist_coordinator.is_paused)
        self.assertIsNone(manual_assist_coordinator.snapshot())
        self.assertEqual(get_manual_assist_state(), {"active": False})

    # -- 12: guards ------------------------------------------------------------------------
    def test_stale_sequence_cannot_resolve_a_newer_pause(self) -> None:
        thread = self._start_batch()
        state = manual_assist_coordinator.snapshot()

        with self.assertRaises(HTTPException) as ctx:
            continue_manual_assist(
                ManualAssistRequest(batch_id=self.batch_id, sequence=state["sequence"] - 1)
            )
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(manual_assist_coordinator.is_paused)     # still paused

        skip_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

    def test_wrong_batch_cannot_resolve_the_pause(self) -> None:
        thread = self._start_batch()
        with self.assertRaises(HTTPException) as ctx:
            skip_manual_assist(ManualAssistRequest(batch_id="some-other-batch"))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(manual_assist_coordinator.is_paused)

        skip_manual_assist(ManualAssistRequest(batch_id=self.batch_id))
        thread.join(timeout=30)

    def test_resolving_when_nothing_is_paused_is_a_clean_409(self) -> None:
        self.assertEqual(get_manual_assist_state(), {"active": False})
        with self.assertRaises(HTTPException) as ctx:
            continue_manual_assist(ManualAssistRequest())
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
