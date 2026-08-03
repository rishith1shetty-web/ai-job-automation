"""Stop Automation: graceful, backend-enforced cancellation of the active apply batch.

The contract:

    * ONE apply batch is active per process; a second cannot start while it runs OR stops;
    * that batch owns ONE cancellation token, shared between the HTTP thread that services Stop
      and the worker thread driving the browser;
    * the worker checks the token at named CHECKPOINTS between browser actions -- including one
      immediately before the final Submit click, which is what makes "no further job may be
      submitted" true rather than merely intended;
    * a stopped job is STOPPED, never failed: no ApplicationRecord, no status change, still
      eligible for a later batch;
    * whatever already completed keeps its result, and the batch keeps its summary.

No browser and no real submission anywhere in this file: the adapters are recording doubles.
"""

import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete

from app.database import SessionLocal, init_db
from app.main import (
    _create_apply_top_batch,
    _process_application,
    _run_apply_top_batch,
    set_active_portal,
    start_apply_top_batch,
    stop_apply_top_batch,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplyTopBatch,
    JobOpportunity,
    ResumeProfile,
)
from app.schemas import ApplyRequest
from app.services.automation_control import (
    AUTOMATION_IDLE,
    AUTOMATION_RUNNING,
    AUTOMATION_STOPPING,
    BATCH_STATUS_STOPPED,
    BATCH_STATUS_STOPPING,
    JOB_RESULT_STOPPED,
    AutomationCancelled,
    AutomationController,
    BatchAlreadyActive,
    CancellationToken,
    check_cancelled,
    get_automation_controller,
    reset_automation_controller,
)
from app.services.linkedin import AutomationResult
from app.services.resume_identity import compute_resume_fingerprint

LINKEDIN_SOURCE = Path("app/services/linkedin.py").read_text(encoding="utf-8", errors="replace")
DICE_SOURCE = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")
RESUME_TEXT = "Sam Halloran\nData Analyst"


# --- 1. the token and the controller -------------------------------------------------------


class CancellationTokenTests(unittest.TestCase):
    def test_a_fresh_token_never_cancels(self) -> None:
        token = CancellationToken(batch_id="b")
        self.assertFalse(token.cancelled)
        token.check("before_submit")  # must not raise

    def test_check_raises_once_cancelled_and_names_the_checkpoint(self) -> None:
        token = CancellationToken(batch_id="b")
        token.cancel()
        with self.assertRaises(AutomationCancelled) as caught:
            token.check("before_submit")
        self.assertEqual(caught.exception.checkpoint, "before_submit")
        self.assertEqual(caught.exception.batch_id, "b")

    def test_cancel_is_idempotent(self) -> None:
        token = CancellationToken(batch_id="b")
        self.assertTrue(token.cancel())
        self.assertFalse(token.cancel())
        self.assertIsNotNone(token.requested_at)

    def test_a_missing_token_never_cancels(self) -> None:
        check_cancelled(None, "before_submit")  # single-job applies pass None

    def test_the_token_is_safe_across_threads(self) -> None:
        # The HTTP thread sets it while the worker thread polls it.
        token = CancellationToken(batch_id="b")
        seen: list[bool] = []
        stop = threading.Event()

        def worker() -> None:
            while not stop.is_set():
                if token.cancelled:
                    seen.append(True)
                    return

        thread = threading.Thread(target=worker)
        thread.start()
        token.cancel()
        thread.join(timeout=5)
        stop.set()
        self.assertEqual(seen, [True])


class AutomationControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.controller = AutomationController()

    def test_a_fresh_controller_is_idle(self) -> None:
        self.assertEqual(self.controller.state, AUTOMATION_IDLE)
        self.assertIsNone(self.controller.active_batch_id)
        self.assertFalse(self.controller.is_active())

    def test_starting_claims_the_only_slot(self) -> None:
        self.controller.start("batch-1")
        self.assertEqual(self.controller.state, AUTOMATION_RUNNING)
        self.assertEqual(self.controller.active_batch_id, "batch-1")

    def test_a_second_batch_cannot_start_while_one_is_running(self) -> None:
        self.controller.start("batch-1")
        with self.assertRaises(BatchAlreadyActive) as caught:
            self.controller.start("batch-2")
        self.assertEqual(caught.exception.batch_id, "batch-1")
        self.assertEqual(caught.exception.state, AUTOMATION_RUNNING)

    def test_a_second_batch_cannot_start_while_one_is_stopping(self) -> None:
        self.controller.start("batch-1")
        self.controller.request_stop("batch-1")
        self.assertEqual(self.controller.state, AUTOMATION_STOPPING)
        with self.assertRaises(BatchAlreadyActive) as caught:
            self.controller.start("batch-2")
        self.assertEqual(caught.exception.state, AUTOMATION_STOPPING)

    def test_the_slot_is_released_when_the_batch_finishes(self) -> None:
        self.controller.start("batch-1")
        self.controller.finish("batch-1")
        self.assertEqual(self.controller.state, AUTOMATION_IDLE)
        self.controller.start("batch-2")  # a new batch may now start

    def test_a_stale_finish_cannot_release_a_newer_batch(self) -> None:
        self.controller.start("batch-1")
        self.controller.finish("batch-old")
        self.assertEqual(self.controller.active_batch_id, "batch-1")

    def test_a_stop_aimed_at_another_batch_is_ignored(self) -> None:
        self.controller.start("batch-1")
        self.assertFalse(self.controller.request_stop("batch-other"))
        self.assertFalse(self.controller.is_cancelled("batch-1"))

    def test_stopping_nothing_is_not_an_error(self) -> None:
        self.assertFalse(self.controller.request_stop())

    def test_reset_clears_a_stale_claim(self) -> None:
        self.controller.start("batch-1")
        self.controller.reset()
        self.assertEqual(self.controller.state, AUTOMATION_IDLE)

    def test_the_snapshot_reports_state_for_the_dashboard(self) -> None:
        self.assertEqual(self.controller.snapshot()["state"], AUTOMATION_IDLE)
        self.controller.start("batch-1")
        snap = self.controller.snapshot()
        self.assertEqual((snap["state"], snap["active_batch_id"]), (AUTOMATION_RUNNING, "batch-1"))
        self.assertFalse(snap["cancellation_requested"])
        self.controller.request_stop("batch-1")
        snap = self.controller.snapshot()
        self.assertEqual(snap["state"], AUTOMATION_STOPPING)
        self.assertTrue(snap["cancellation_requested"])


# --- 2. the checkpoints actually exist, in both portals -------------------------------------


class CheckpointCoverageTests(unittest.TestCase):
    """The same mechanism and the same named checkpoints on LinkedIn and on Dice."""

    REQUIRED = ("before_job", "job_opened", "before_continue", "before_submit")

    def test_linkedin_checks_every_required_checkpoint(self) -> None:
        for name in (*self.REQUIRED, "before_form_page"):
            with self.subTest(checkpoint=name):
                self.assertIn(f'self._checkpoint("{name}")', LINKEDIN_SOURCE)

    def test_dice_checks_every_required_checkpoint(self) -> None:
        for name in (*self.REQUIRED, "before_fill"):
            with self.subTest(checkpoint=name):
                self.assertIn(f'self._checkpoint("{name}")', DICE_SOURCE)

    def test_both_portals_use_the_one_shared_mechanism(self) -> None:
        for source in (LINKEDIN_SOURCE, DICE_SOURCE):
            self.assertIn("from app.services.automation_control import", source)
            self.assertIn("check_cancelled(self._cancellation", source)
            self.assertIn("cancellation: CancellationToken | None = None", source)

    def test_the_submit_checkpoint_immediately_precedes_the_submit_click(self) -> None:
        # Not merely present -- present at the last possible moment. Anything between the
        # checkpoint and the click is a window in which a stop would be ignored.
        linkedin = LINKEDIN_SOURCE.index('self._checkpoint("before_submit")')
        self.assertLess(linkedin, LINKEDIN_SOURCE.index("submit.first.click()"))
        between = LINKEDIN_SOURCE[linkedin:LINKEDIN_SOURCE.index("submit.first.click()")]
        self.assertEqual(between.count("\n"), 1, f"code between checkpoint and submit: {between!r}")

        dice = DICE_SOURCE.index('self._checkpoint("before_submit")')
        self.assertLess(dice, DICE_SOURCE.index("submit.click()"))
        between = DICE_SOURCE[dice:DICE_SOURCE.index("submit.click()")]
        self.assertLessEqual(between.count("\n"), 3, f"code between checkpoint and submit: {between!r}")

    def test_a_stopped_application_is_never_recorded_as_failed(self) -> None:
        # _process_application catches broad exceptions to write a FAILED record; the
        # cancellation must be re-raised BEFORE that handler is reached.
        cancel_at = MAIN_SOURCE.index("except AutomationCancelled:")
        failed_at = MAIN_SOURCE.index('logger.exception("Application failed for job %s", job.id)')
        self.assertLess(cancel_at, failed_at)


# --- 3. the adapters honour a cancelled token -----------------------------------------------


class _StubJob:
    id = 1
    title = "Data Analyst"
    company = "Acme"
    job_url = "https://example.test/job/1"
    portal = "dice"
    easy_apply = True


class AdapterCancellationTests(unittest.TestCase):
    """Both adapters stop at their FIRST checkpoint, before any browser is opened."""

    def _cancelled_token(self) -> CancellationToken:
        token = CancellationToken(batch_id="batch-1")
        token.cancel()
        return token

    def test_linkedin_stops_before_opening_a_browser(self) -> None:
        from app.config import Settings
        from app.services.linkedin import LinkedInAutomation

        automation = LinkedInAutomation(Settings(openai_api_key=None))
        with patch.object(LinkedInAutomation, "_browser_context") as browser:
            with self.assertRaises(AutomationCancelled) as caught:
                automation.apply_to_job(
                    _StubJob(), None, None, dry_run=True, capture_evidence=False,
                    cancellation=self._cancelled_token(),
                )
        self.assertEqual(caught.exception.checkpoint, "linkedin:before_job")
        browser.assert_not_called()

    def test_dice_stops_before_opening_a_browser(self) -> None:
        from app.config import Settings
        from app.services.portals.dice_portal import DicePortalAdapter

        adapter = DicePortalAdapter(Settings(openai_api_key=None))
        with patch.object(DicePortalAdapter, "_apply_browser_context") as browser:
            with self.assertRaises(AutomationCancelled) as caught:
                adapter.apply_to_job(
                    _StubJob(), None, None, dry_run=True, capture_evidence=False,
                    cancellation=self._cancelled_token(),
                )
        self.assertEqual(caught.exception.checkpoint, "dice:before_job")
        browser.assert_not_called()

    def test_neither_adapter_cancels_without_a_token(self) -> None:
        # Every pre-existing caller passes no token and must be completely unaffected.
        from app.config import Settings
        from app.services.linkedin import LinkedInAutomation
        from app.services.portals.dice_portal import DicePortalAdapter

        LinkedInAutomation(Settings(openai_api_key=None))._checkpoint("before_submit")
        DicePortalAdapter(Settings(openai_api_key=None))._checkpoint("before_submit")


# --- 4. the batch runner --------------------------------------------------------------------


class StoppingAdapter:
    """Applies normally until `stop_after` jobs, then cancels the batch from inside the
    adapter -- exactly as a real Stop click lands while a job is being processed."""

    controller = None
    batch_id = ""
    stop_after = 1
    applied: list[int] = []
    checkpoint = "before_submit"

    def __init__(self, _settings=None) -> None:
        pass

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None, cancellation=None) -> AutomationResult:
        if len(StoppingAdapter.applied) >= StoppingAdapter.stop_after:
            # The user clicks Stop while this job is in flight...
            StoppingAdapter.controller.request_stop(StoppingAdapter.batch_id)
            # ...and the very next checkpoint refuses to submit.
            check_cancelled(cancellation, StoppingAdapter.checkpoint)
        StoppingAdapter.applied.append(job.id)
        return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)


class BatchStopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_automation_controller()
        StoppingAdapter.applied = []
        StoppingAdapter.stop_after = 1
        StoppingAdapter.checkpoint = "before_submit"
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = ResumeProfile(
            portal="dice", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.jobs = []
        for i in range(4):
            job = JobOpportunity(
                resume_id=self.resume.id, portal="dice", title=f"Data Analyst {i}",
                company=f"Acme {i}", location="Remote", job_url=f"https://dice/{uuid4().hex}",
                description="", status=ApplicationStatus.MATCHED,
            )
            self.db.add(job)
            self.jobs.append(job)
        self.db.commit()
        self.job_ids = [job.id for job in self.jobs]

    def tearDown(self) -> None:
        self.db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id.in_(self.job_ids)))
        self.db.execute(delete(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id))
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.delete(self.resume)
        set_active_portal(self.db, "linkedin")
        self.db.commit()
        self.db.close()
        reset_automation_controller()

    def _run_stopped_batch(self, stop_after: int = 1) -> ApplyTopBatch:
        StoppingAdapter.stop_after = stop_after
        batch = _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=10, dry_run=False, portal="dice"), self.db
        )
        controller = get_automation_controller()
        controller.start(batch.id)
        StoppingAdapter.controller = controller
        StoppingAdapter.batch_id = batch.id
        with patch("app.main.dice_adapter", StoppingAdapter):
            _run_apply_top_batch(batch.id)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def test_the_batch_ends_as_stopped_not_failed(self) -> None:
        batch = self._run_stopped_batch()
        self.assertEqual(batch.status, BATCH_STATUS_STOPPED)
        self.assertIsNone(batch.error_message)
        self.assertIsNotNone(batch.finished_at)

    def test_jobs_already_applied_keep_their_result(self) -> None:
        batch = self._run_stopped_batch(stop_after=2)
        applied = [r for r in batch.job_results if r["status"] == "applied"]
        self.assertEqual(len(applied), 2)
        self.assertEqual(StoppingAdapter.applied, self.job_ids[:2])

    def test_the_interrupted_and_pending_jobs_are_stopped_not_failed(self) -> None:
        batch = self._run_stopped_batch(stop_after=1)
        statuses = [r["status"] for r in batch.job_results]
        self.assertEqual(statuses[0], "applied")
        self.assertEqual(set(statuses[1:]), {JOB_RESULT_STOPPED})
        self.assertEqual(batch.failed_count, 0, "a stopped job must never count as failed")
        self.assertEqual(batch.stopped_count, 3)

    def test_the_interrupted_job_records_where_it_stopped(self) -> None:
        batch = self._run_stopped_batch(stop_after=1)
        interrupted = batch.job_results[1]
        self.assertEqual(interrupted["status"], JOB_RESULT_STOPPED)
        self.assertIn("before_submit", interrupted["stop_checkpoint"])
        self.assertEqual(interrupted["current_step"], "stopped_by_user")
        # The jobs after it never started.
        self.assertEqual(batch.job_results[2]["stop_checkpoint"], "not_started")

    def test_a_stopped_job_writes_no_application_history(self) -> None:
        batch = self._run_stopped_batch(stop_after=1)
        stopped_ids = [r["job_id"] for r in batch.job_results if r["status"] == JOB_RESULT_STOPPED]
        records = self.db.scalars(
            delete(ApplicationRecord).returning(ApplicationRecord.job_id)
            if False else __import__("sqlalchemy").select(ApplicationRecord).where(
                ApplicationRecord.job_id.in_(stopped_ids)
            )
        ).all()
        self.assertEqual(list(records), [], "a stopped job must not appear in Application History")

    def test_stopped_jobs_stay_eligible_for_a_later_batch(self) -> None:
        batch = self._run_stopped_batch(stop_after=1)
        stopped_ids = [r["job_id"] for r in batch.job_results if r["status"] == JOB_RESULT_STOPPED]
        for job_id in stopped_ids:
            with self.subTest(job=job_id):
                self.assertEqual(self.db.get(JobOpportunity, job_id).status, ApplicationStatus.MATCHED)

    def test_the_summary_reports_completed_stopped_skipped_and_failed(self) -> None:
        batch = self._run_stopped_batch(stop_after=2)
        self.assertIn("stopped by user", (batch.latest_status_message or "").lower())
        self.assertIn("2 stopped", batch.latest_status_message or "")
        self.assertEqual(batch.completed_count, len(batch.job_results))
        self.assertEqual(batch.stopped_count, 2)
        self.assertEqual(batch.failed_count, 0)
        self.assertEqual(batch.skipped_count, 0)

    def test_the_automation_slot_is_released_after_a_stop(self) -> None:
        self._run_stopped_batch()
        self.assertEqual(get_automation_controller().state, AUTOMATION_IDLE)

    def test_cancelling_before_the_first_job_stops_every_job(self) -> None:
        batch = _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=10, dry_run=False, portal="dice"), self.db
        )
        controller = get_automation_controller()
        controller.start(batch.id)
        controller.request_stop(batch.id)          # stop before the worker even starts
        StoppingAdapter.controller = controller
        StoppingAdapter.batch_id = batch.id
        with patch("app.main.dice_adapter", StoppingAdapter):
            _run_apply_top_batch(batch.id)
        self.db.expire_all()
        batch = self.db.get(ApplyTopBatch, batch.id)
        self.assertEqual(batch.status, BATCH_STATUS_STOPPED)
        self.assertEqual(StoppingAdapter.applied, [], "no job may run after a stop is requested")
        self.assertEqual(batch.stopped_count, len(self.job_ids))

    def test_the_batch_completes_normally_when_nothing_stops_it(self) -> None:
        StoppingAdapter.stop_after = 99
        batch = _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=10, dry_run=False, portal="dice"), self.db
        )
        controller = get_automation_controller()
        controller.start(batch.id)
        StoppingAdapter.controller = controller
        StoppingAdapter.batch_id = batch.id
        with patch("app.main.dice_adapter", StoppingAdapter):
            _run_apply_top_batch(batch.id)
        self.db.expire_all()
        batch = self.db.get(ApplyTopBatch, batch.id)
        self.assertEqual(batch.status, "completed")
        self.assertEqual(batch.stopped_count, 0)
        self.assertIn("Batch complete", batch.latest_status_message or "")


# --- 5. the HTTP surface --------------------------------------------------------------------


class StopEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_automation_controller()
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = ResumeProfile(
            portal="dice", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.job = JobOpportunity(
            resume_id=self.resume.id, portal="dice", title="Data Analyst", company="Acme",
            location="Remote", job_url=f"https://dice/{uuid4().hex}", description="",
            status=ApplicationStatus.MATCHED,
        )
        self.db.add(self.job)
        self.db.commit()

    def tearDown(self) -> None:
        self.db.execute(delete(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id))
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.delete(self.resume)
        set_active_portal(self.db, "linkedin")
        self.db.commit()
        self.db.close()
        reset_automation_controller()

    def _request(self) -> ApplyRequest:
        return ApplyRequest(resume_id=self.resume.id, limit=1, dry_run=True, portal="dice")

    def test_a_second_batch_is_refused_while_one_is_active(self) -> None:
        from fastapi import BackgroundTasks, HTTPException

        tasks = BackgroundTasks()
        first = start_apply_top_batch(self._request(), tasks, self.db)
        self.assertEqual(get_automation_controller().active_batch_id, first.id)
        with self.assertRaises(HTTPException) as caught:
            start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("already running", caught.exception.detail)

    def test_a_second_batch_is_refused_while_one_is_stopping(self) -> None:
        from fastapi import BackgroundTasks, HTTPException

        first = start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        stop_apply_top_batch(first.id, self.db)
        self.assertEqual(get_automation_controller().state, AUTOMATION_STOPPING)
        with self.assertRaises(HTTPException) as caught:
            start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("stopping", caught.exception.detail)

    def test_a_refused_second_batch_leaves_no_orphan_row(self) -> None:
        from fastapi import BackgroundTasks, HTTPException
        from sqlalchemy import func, select

        start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        before = self.db.scalar(
            select(func.count()).select_from(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id)
        )
        with self.assertRaises(HTTPException):
            start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        after = self.db.scalar(
            select(func.count()).select_from(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id)
        )
        self.assertEqual(before, after)

    def test_stopping_marks_the_batch_stopping_and_raises_the_flag(self) -> None:
        from fastapi import BackgroundTasks

        batch = start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        stopped = stop_apply_top_batch(batch.id, self.db)
        self.assertEqual(stopped.status, BATCH_STATUS_STOPPING)
        self.assertIn("no further job", stopped.latest_status_message or "")
        self.assertTrue(get_automation_controller().is_cancelled(batch.id))

    def test_stopping_twice_is_not_an_error(self) -> None:
        from fastapi import BackgroundTasks

        batch = start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        stop_apply_top_batch(batch.id, self.db)
        again = stop_apply_top_batch(batch.id, self.db)
        self.assertEqual(again.status, BATCH_STATUS_STOPPING)

    def test_stopping_an_unknown_batch_is_a_404(self) -> None:
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as caught:
            stop_apply_top_batch("no-such-batch", self.db)
        self.assertEqual(caught.exception.status_code, 404)

    def test_stopping_a_finished_batch_is_a_409(self) -> None:
        from fastapi import BackgroundTasks, HTTPException

        batch = start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        batch.status = "completed"
        self.db.add(batch)
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            stop_apply_top_batch(batch.id, self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_the_automation_state_endpoint_reports_the_backend_truth(self) -> None:
        from fastapi import BackgroundTasks

        from app.main import get_automation_state

        self.assertEqual(get_automation_state()["state"], AUTOMATION_IDLE)
        batch = start_apply_top_batch(self._request(), BackgroundTasks(), self.db)
        state = get_automation_state()
        self.assertEqual(state["state"], AUTOMATION_RUNNING)
        self.assertEqual(state["active_batch_id"], batch.id)
        self.assertFalse(state["cancellation_requested"])
        stop_apply_top_batch(batch.id, self.db)
        state = get_automation_state()
        self.assertEqual(state["state"], AUTOMATION_STOPPING)
        self.assertTrue(state["cancellation_requested"])


# --- 6. what a stop must NOT touch ----------------------------------------------------------


class PreservedBehaviourTests(unittest.TestCase):
    def test_the_portal_session_files_are_never_deleted_by_stopping(self) -> None:
        # Stop raises a flag and lets the worker unwind. It must not disconnect a portal or
        # remove a saved storage-state file, so the user stays logged in.
        stop_block = MAIN_SOURCE.split("def stop_apply_top_batch(")[1].split("\n@app.")[0]
        for forbidden in ("unlink", "disconnect", "storage_state", "close()", "kill"):
            self.assertNotIn(forbidden, stop_block)

    def test_stopping_never_force_kills_the_browser(self) -> None:
        # The only teardown is the adapter's own context manager, which already runs in a
        # `finally` on the way out -- so only that apply's temporary page/context closes.
        for source in (LINKEDIN_SOURCE, DICE_SOURCE):
            self.assertNotIn("browser.kill", source)
            self.assertNotIn("process.terminate", source)

    def test_a_single_job_apply_is_unaffected(self) -> None:
        # apply-selected passes no token: nothing about that path can cancel.
        block = MAIN_SOURCE.split("def apply_selected_job(")[1][:3000]
        self.assertNotIn("cancellation=", block)

    def test_the_startup_hook_clears_a_stale_claim(self) -> None:
        self.assertIn("reset_automation_controller()", MAIN_SOURCE)
        startup = MAIN_SOURCE.split("def startup()")[1][:900]
        self.assertIn("reset_automation_controller()", startup)

    def test_stopped_batches_do_not_change_the_application_status_taxonomy(self) -> None:
        # "stopped" lives in job_results (JSONB) and in the batch's own string status. No new
        # ApplicationStatus member, so no PostgreSQL enum migration.
        self.assertNotIn("STOPPED", ApplicationStatus.__members__)
        self.assertNotIn("CANCELLED", ApplicationStatus.__members__)


if __name__ == "__main__":
    unittest.main()
