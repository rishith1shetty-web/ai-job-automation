"""The Dice lifecycle bug: a batch left claiming Running after its worker has gone.

Reported sequence:

    start a Dice apply batch -> jobs process -> the worker stops unexpectedly ->
    the dashboard still shows "Running, processing X/Y" -> the user presses Stop Automation ->
    "Stop Automation failed: Apply batch is not the active automation run."

Three separate defects produce that, and each has its own test here:

  1. THE RECOVERY PATH SHARES THE WORKER'S BROKEN SESSION. _run_apply_top_batch owns one
     Session. A database error inside the job loop puts it into PendingRollbackError, and every
     later statement on it raises -- including the ones in the batch-level `except` and in the
     `finally` that exist to record a final state. The status is never written, so the row stays
     "running" while `controller.finish()` still releases the slot. Database and controller now
     disagree permanently.

  2. STOP NEVER RECONCILES. stop_apply_top_batch asks the controller whether the batch is
     active, and reports "not the active automation run" when it is not -- which is precisely
     the situation where the batch needs adopting, not rejecting. It also does not recognise
     `interrupted` as an already-finished state.

  3. THE ORPHAN GRACE WINDOW BLOCKS AN EXPLICIT REQUEST. _reconcile_batch_state ignores a batch
     younger than 30s so the create-then-claim window is never mistaken for a crash. That is
     right for background polling and wrong for a Stop the user just pressed on a batch that
     has demonstrably started.

Nothing here opens a browser or submits an application: the adapters are doubles.
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal, init_db
from app.main import (
    CONSECUTIVE_PORTAL_FAILURE_LIMIT,
    _portal_health,
    _apply_top_jobs_statement,
    _apply_top_remaining_jobs,
    _create_apply_top_batch,
    _reconcile_batch_state,
    _resume_blocker,
    _run_apply_top_batch,
    get_apply_top_batch,
    get_latest_apply_top_batch,
    resume_apply_top_batch,
    set_active_portal,
    stop_apply_top_batch,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    ApplyTopBatch,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
)
from app.schemas import ApplyRequest, ApplyTopBatchOut
from app.services.automation_control import (
    ACTIVE_BATCH_STATUSES,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_INTERRUPTED,
    BATCH_STATUS_STOPPED,
    RESUMABLE_BATCH_STATUSES,
    check_cancelled,
    get_automation_controller,
    reset_automation_controller,
)
from app.services.linkedin import AutomationResult
from app.services.resume_identity import compute_resume_fingerprint

RESUME_TEXT = "Dev Kulkarni\nData Analyst"


class DiceAdapterDouble:
    """Applies each job. `explode_after` raises the way a Dice adapter failure would."""

    applied: list[int] = []
    explode_after: int | None = None
    explode_with: type[BaseException] = RuntimeError
    stop_after: int | None = None
    controller = None
    batch_id = ""

    def __init__(self, _settings=None) -> None:
        pass

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None, cancellation=None) -> AutomationResult:
        done = len(DiceAdapterDouble.applied)
        if DiceAdapterDouble.stop_after is not None and done >= DiceAdapterDouble.stop_after:
            DiceAdapterDouble.controller.request_stop(DiceAdapterDouble.batch_id)
            check_cancelled(cancellation, "before_submit")
        if DiceAdapterDouble.explode_after is not None and done >= DiceAdapterDouble.explode_after:
            raise DiceAdapterDouble.explode_with("the Dice browser went away")
        DiceAdapterDouble.applied.append(job.id)
        return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)


class DiceLifecycleTestCase(unittest.TestCase):
    JOB_COUNT = 6

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_automation_controller()
        DiceAdapterDouble.applied = []
        DiceAdapterDouble.explode_after = None
        DiceAdapterDouble.explode_with = RuntimeError
        DiceAdapterDouble.stop_after = None
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = ResumeProfile(
            portal="dice", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.search = JobSearchBatch(
            id=str(uuid4()), resume_id=self.resume.id, search_query="data analyst",
            location="Remote", portal="dice", status="completed",
        )
        self.db.add(self.search)
        self.db.commit()
        self.jobs = []
        for index in range(self.JOB_COUNT):
            job = JobOpportunity(
                resume_id=self.resume.id, portal="dice", search_batch_id=self.search.id,
                title=f"Data Analyst {index}", company=f"Acme {index}", location="Remote",
                job_url=f"https://dice/{uuid4().hex}", description="",
                status=ApplicationStatus.MATCHED,
            )
            self.db.add(job)
            self.jobs.append(job)
        self.db.commit()
        self.job_ids = [job.id for job in self.jobs]

    def tearDown(self) -> None:
        self.db.rollback()
        self.db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id.in_(self.job_ids)))
        self.db.execute(delete(ApplyTopBatch).where(ApplyTopBatch.resume_id == self.resume.id))
        self.db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id))
        self.db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == self.resume.id))
        self.db.delete(self.resume)
        set_active_portal(self.db, "linkedin")
        self.db.commit()
        self.db.close()
        reset_automation_controller()

    # -- helpers ---------------------------------------------------------------------------

    def _planned_order(self) -> list[int]:
        return [j.id for j in self.db.scalars(
            _apply_top_jobs_statement(self.resume.id, self.search.id, portal="dice")).all()]

    def _batch(self, limit: int = 10) -> ApplyTopBatch:
        return _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=limit, dry_run=False,
                         portal="dice", search_batch_id=self.search.id),
            self.db,
        )

    def _run(self, batch: ApplyTopBatch, resuming: bool = False) -> ApplyTopBatch:
        controller = get_automation_controller()
        controller.start(batch.id)
        DiceAdapterDouble.controller = controller
        DiceAdapterDouble.batch_id = batch.id
        with patch("app.main.dice_adapter", DiceAdapterDouble):
            _run_apply_top_batch(batch.id, resuming)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def _reread(self, batch_id: str) -> ApplyTopBatch:
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch_id)

    def _age_the_batch(self, batch_id: str, minutes: int = 5) -> None:
        """Backdate updated_at so the orphan grace window is not what is under test."""
        self.db.query(ApplyTopBatch).filter(ApplyTopBatch.id == batch_id).update(
            {"updated_at": datetime.utcnow() - timedelta(minutes=minutes)})
        self.db.commit()
        self.db.expire_all()


# ======================================================================================
# Defect 1 -- the worker's recovery path runs on its own broken session
# ======================================================================================

class BrokenSessionRecoveryTests(DiceLifecycleTestCase):
    def test_1_a_database_failure_mid_batch_still_records_a_final_state(self) -> None:
        """The reported bug, reproduced at its source.

        A Dice job fails, and recording that failure hits a database error which leaves the
        worker's Session unusable. Every later statement on that Session raises -- including the
        ones meant to finalise the batch. The batch must still not be left claiming to run."""
        batch = self._batch()
        original_append = None
        from app import main as main_module
        original_append = main_module._append_apply_top_result

        calls = {"n": 0}

        def break_the_session(db, batch_id, result):
            calls["n"] += 1
            if calls["n"] == 2:
                # Poison the session for real: a failed statement that is NOT rolled back, so
                # every later statement on it raises -- exactly what the worker's recovery code
                # used to inherit. (Rolling back here would repair it and prove nothing.)
                try:
                    db.execute(text("SELECT 1/0"))
                except Exception:
                    pass
                raise SQLAlchemyError("simulated failure with a poisoned session")
            return original_append(db, batch_id, result)

        with patch.object(main_module, "_append_apply_top_result", side_effect=break_the_session):
            finished = self._run(batch)

        self.assertNotIn(finished.status, ACTIVE_BATCH_STATUSES,
                         f"batch was left claiming to be {finished.status} with no worker")
        self.assertIn(finished.status, RESUMABLE_BATCH_STATUSES)

    def test_2_controller_and_database_agree_after_a_broken_session(self) -> None:
        batch = self._batch()
        from app import main as main_module
        original_append = main_module._append_apply_top_result
        calls = {"n": 0}

        def break_the_session(db, batch_id, result):
            calls["n"] += 1
            if calls["n"] == 2:
                # Poison the session for real: a failed statement that is NOT rolled back, so
                # every later statement on it raises -- exactly what the worker's recovery code
                # used to inherit. (Rolling back here would repair it and prove nothing.)
                try:
                    db.execute(text("SELECT 1/0"))
                except Exception:
                    pass
                raise SQLAlchemyError("simulated failure with a poisoned session")
            return original_append(db, batch_id, result)

        with patch.object(main_module, "_append_apply_top_result", side_effect=break_the_session):
            finished = self._run(batch)

        controller = get_automation_controller()
        self.assertIsNone(controller.active_batch_id, "the slot must always be released")
        # The two authorities must not disagree: an idle controller and a running row is the
        # state the user cannot escape from.
        self.assertFalse(
            controller.active_batch_id is None and finished.status in ACTIVE_BATCH_STATUSES,
            "controller is idle but the batch still claims to be active",
        )

    def test_3_progress_survives_the_broken_session(self) -> None:
        batch = self._batch()
        from app import main as main_module
        original_append = main_module._append_apply_top_result
        calls = {"n": 0}

        def break_the_session(db, batch_id, result):
            calls["n"] += 1
            if calls["n"] == 2:
                # Poison the session for real: a failed statement that is NOT rolled back, so
                # every later statement on it raises -- exactly what the worker's recovery code
                # used to inherit. (Rolling back here would repair it and prove nothing.)
                try:
                    db.execute(text("SELECT 1/0"))
                except Exception:
                    pass
                raise SQLAlchemyError("simulated failure with a poisoned session")
            return original_append(db, batch_id, result)

        with patch.object(main_module, "_append_apply_top_result", side_effect=break_the_session):
            finished = self._run(batch)

        self.assertGreaterEqual(finished.completed_count, 1, "the first job's result was lost")
        self.assertIsNone(_resume_blocker(self.db, finished))


# ======================================================================================
# Defect 2 + 3 -- Stop against a worker that has already died
# ======================================================================================

class StopAfterWorkerDiedTests(DiceLifecycleTestCase):
    def _orphan(self) -> ApplyTopBatch:
        """A batch the database still calls running, with no worker behind it -- the state the
        user is looking at when they press Stop."""
        batch = self._batch()
        batch.status = "running"
        batch.started_at = datetime.utcnow()
        batch.current_index = 3
        batch.total_count = self.JOB_COUNT
        batch.latest_status_message = "Processing 3/6: Data Analyst 2 at Acme 2"
        self.db.add(batch)
        self.db.commit()
        reset_automation_controller()      # the worker is gone; the slot was released
        return batch

    def test_4_stop_does_not_report_that_it_is_not_the_active_run(self) -> None:
        """The exact user-visible failure."""
        orphan = self._orphan()
        try:
            returned = stop_apply_top_batch(orphan.id, self.db)
        except HTTPException as exc:
            self.assertNotIn("not the active automation run", str(exc.detail),
                             "Stop still reports the misleading 'not the active run' error")
            returned = None
        if returned is not None:
            self.assertNotIn(returned.status, ACTIVE_BATCH_STATUSES)

    def test_5_stop_leaves_the_batch_resumable_not_stuck(self) -> None:
        orphan = self._orphan()
        try:
            stop_apply_top_batch(orphan.id, self.db)
        except HTTPException:
            pass
        after = self._reread(orphan.id)
        self.assertNotIn(after.status, ACTIVE_BATCH_STATUSES,
                         f"Stop left the batch {after.status} with no worker")
        self.assertIsNone(_resume_blocker(self.db, after))

    def test_6_stop_does_not_make_the_state_worse(self) -> None:
        """It must never push a dead batch into `stopping`, which is another active state."""
        orphan = self._orphan()
        try:
            stop_apply_top_batch(orphan.id, self.db)
        except HTTPException:
            pass
        after = self._reread(orphan.id)
        self.assertNotEqual(after.status, "stopping")

    def test_7_polling_recovers_the_batch_once_the_grace_window_passes(self) -> None:
        orphan = self._orphan()
        self._age_the_batch(orphan.id)
        polled = get_apply_top_batch(orphan.id, self.db)
        self.assertEqual(polled.status, BATCH_STATUS_INTERRUPTED)
        self.assertTrue(ApplyTopBatchOut.model_validate(polled).resumable)

    def test_8_stop_reconciles_immediately_without_waiting_for_the_grace_window(self) -> None:
        """A Stop the user just pressed is explicit; it must not wait 30 seconds to be honest
        about a worker that is already gone."""
        orphan = self._orphan()          # updated_at is seconds old on purpose
        try:
            stop_apply_top_batch(orphan.id, self.db)
        except HTTPException:
            pass
        after = self._reread(orphan.id)
        self.assertNotIn(after.status, ACTIVE_BATCH_STATUSES)

    def test_9_stopping_an_already_interrupted_batch_is_not_an_error(self) -> None:
        orphan = self._orphan()
        self._age_the_batch(orphan.id)
        get_apply_top_batch(orphan.id, self.db)          # reconciles it to interrupted
        returned = stop_apply_top_batch(orphan.id, self.db)
        self.assertEqual(returned.status, BATCH_STATUS_INTERRUPTED)

    def test_10_continue_is_offered_after_stop_on_a_dead_worker(self) -> None:
        orphan = self._orphan()
        try:
            stop_apply_top_batch(orphan.id, self.db)
        except HTTPException:
            pass
        latest = get_latest_apply_top_batch(self.resume.id, "dice", self.db)
        self.assertTrue(ApplyTopBatchOut.model_validate(latest).resumable)


# ======================================================================================
# The full reported sequence, end to end
# ======================================================================================

class ReportedSequenceTests(DiceLifecycleTestCase):
    # Enough jobs that the portal-loss breaker can trip AND still leave work behind: three
    # applied, three failed against the dead browser, two never touched.
    JOB_COUNT = 8

    def test_the_exact_reported_sequence_recovers_and_resumes(self) -> None:
        order = self._planned_order()

        # 1-2. the batch runs and applies to three jobs, then the Dice adapter dies
        batch = self._batch()
        DiceAdapterDouble.explode_after = 3
        crashed = self._run(batch)
        applied_first = list(DiceAdapterDouble.applied)
        self.assertEqual(applied_first, order[:3])

        # 3-4. the worker is gone. Whatever it recorded, the row must not claim to be active.
        self.assertNotIn(crashed.status, ACTIVE_BATCH_STATUSES)

        # 5-6. the user presses Stop; it must not fail with the misleading error
        try:
            stop_apply_top_batch(crashed.id, self.db)
        except HTTPException as exc:
            self.assertNotIn("not the active automation run", str(exc.detail))

        # 7. the batch is resumable
        after_stop = self._reread(crashed.id)
        self.assertIsNone(_resume_blocker(self.db, after_stop))
        self.assertTrue(ApplyTopBatchOut.model_validate(
            get_latest_apply_top_batch(self.resume.id, "dice", self.db)).resumable)

        # 8. Continue Automation resumes the SAME batch from the next unprocessed job
        reset_automation_controller()
        DiceAdapterDouble.explode_after = None
        DiceAdapterDouble.applied = []
        resumed = self._run(self._reread(crashed.id), resuming=True)

        self.assertEqual(resumed.id, batch.id, "a new batch was created")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(set(applied_first) & set(DiceAdapterDouble.applied), set(),
                         "a completed job was processed again")
        # Jobs 4-6 were genuinely attempted against the dead browser and recorded as failed, so
        # the first UNPROCESSED job is the seventh -- the breaker stopped the run there rather
        # than burning the rest of the queue.
        self.assertEqual(DiceAdapterDouble.applied[0], order[6],
                         "the resume did not start at the first untouched job")
        self.assertEqual(resumed.completed_count, self.JOB_COUNT)


# ======================================================================================
# The other three cases in the report
# ======================================================================================

class RemainingCasesTests(DiceLifecycleTestCase):
    def test_case2_stop_during_a_genuinely_running_dice_batch_still_works(self) -> None:
        batch = self._batch()
        DiceAdapterDouble.stop_after = 2
        stopped = self._run(batch)
        self.assertEqual(stopped.status, BATCH_STATUS_STOPPED)
        self.assertEqual(len(DiceAdapterDouble.applied), 2)
        self.assertIsNone(_resume_blocker(self.db, stopped))

    def test_case4_a_normally_completed_dice_batch_is_not_resumable(self) -> None:
        batch = self._batch()
        completed = self._run(batch)
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(_resume_blocker(self.db, completed))
        self.assertFalse(ApplyTopBatchOut.model_validate(
            get_latest_apply_top_batch(self.resume.id, "dice", self.db)).resumable)

    def test_a_dice_adapter_exception_leaves_the_batch_resumable(self) -> None:
        """A dead Dice browser must not consume the whole queue as individual failures."""
        batch = self._batch()
        DiceAdapterDouble.explode_after = 1
        crashed = self._run(batch)
        self.assertNotIn(crashed.status, ACTIVE_BATCH_STATUSES)
        self.assertIsNone(_resume_blocker(self.db, crashed))

    def test_a_base_exception_cannot_leave_the_batch_running(self) -> None:
        batch = self._batch()
        DiceAdapterDouble.explode_after = 2
        DiceAdapterDouble.explode_with = KeyboardInterrupt
        with self.assertRaises(BaseException):
            self._run(batch)
        after = self._reread(batch.id)
        self.assertNotIn(after.status, ACTIVE_BATCH_STATUSES)
        self.assertIsNone(get_automation_controller().active_batch_id)

    def test_a_lost_portal_stops_the_run_instead_of_consuming_every_job(self) -> None:
        """The second Dice defect: a dead browser used to fail every remaining job in seconds,
        leaving a 'completed' batch with nothing to continue."""
        self.JOB_COUNT = self.JOB_COUNT  # documented: 6 jobs, breaker limit 3
        batch = self._batch()
        DiceAdapterDouble.explode_after = 1          # 1 applied, then the browser is gone
        ended = self._run(batch)

        attempted = len(ended.job_results)
        self.assertEqual(attempted, 1 + CONSECUTIVE_PORTAL_FAILURE_LIMIT,
                         "the run did not stop at the failure limit")
        untouched = self.JOB_COUNT - attempted
        self.assertGreater(untouched, 0, "no jobs were left to continue")
        self.assertEqual(ended.status, BATCH_STATUS_FAILED)
        self.assertIsNone(_resume_blocker(self.db, ended))
        self.assertEqual(
            len(_apply_top_remaining_jobs(self.db, ended, self.resume)), untouched)

    def test_the_failure_counter_resets_on_any_success(self) -> None:
        """A healthy run with scattered failures must never trip the breaker."""
        from app import main as main_module
        batch = self._batch()
        outcomes = iter([
            ApplicationStatus.FAILED, ApplicationStatus.FAILED, ApplicationStatus.APPLIED,
            ApplicationStatus.FAILED, ApplicationStatus.FAILED, ApplicationStatus.APPLIED,
        ])

        class Alternating(DiceAdapterDouble):
            def apply_to_job(self, job, *args, **kwargs):
                status = next(outcomes)
                if status == ApplicationStatus.FAILED:
                    raise RuntimeError("this one job could not be applied to")
                DiceAdapterDouble.applied.append(job.id)
                return AutomationResult(status, None, [], "dice_submitted", None)

        controller = get_automation_controller()
        controller.start(batch.id)
        with patch("app.main.dice_adapter", Alternating):
            _run_apply_top_batch(batch.id)
        self.db.expire_all()
        ended = self.db.get(ApplyTopBatch, batch.id)

        self.assertEqual(ended.status, "completed",
                         "two failures separated by a success must not end the run")
        self.assertEqual(len(ended.job_results), self.JOB_COUNT)

    def test_a_running_batch_with_a_live_worker_is_never_reconciled_away(self) -> None:
        batch = self._batch()
        batch.status = "running"
        batch.started_at = datetime.utcnow()
        self.db.add(batch)
        self.db.commit()
        self._age_the_batch(batch.id)
        get_automation_controller().start(batch.id)       # a worker genuinely owns it
        untouched = _reconcile_batch_state(self.db, self._reread(batch.id))
        self.assertEqual(untouched.status, "running")


# ======================================================================================
# What the breaker counter actually measures
#
# It counts consecutive PORTAL failures, not "jobs that did not end in Applied". Classifying by
# job outcome alone was wrong in both directions, and both mistakes were live in the first
# version of the breaker: a hung browser times out on every job (and `timed_out` RESET the
# counter), and a Dice page that fails to open is reported as SKIPPED (which also reset it).
# ======================================================================================

class PortalHealthClassificationTests(unittest.TestCase):
    def test_a_successful_application_proves_the_portal_works(self) -> None:
        self.assertEqual(_portal_health("applied", "submitted", None, []), "healthy")

    def test_a_dry_run_that_reached_review_proves_the_portal_works(self) -> None:
        self.assertEqual(_portal_health("reached_review", "review", None, []), "healthy")

    def test_needs_review_proves_the_portal_works(self) -> None:
        """The form had to be reached and inspected to be judged unsupported."""
        self.assertEqual(_portal_health("needs_review", "unsupported_form", None, []), "healthy")

    def test_a_handled_timeout_inside_a_successful_apply_is_still_healthy(self) -> None:
        """The outcome decides: the page opened, the form was filled, it submitted."""
        log = [{"step": "wait_retry", "error": "Timeout 5000ms exceeded"}, {"step": "submitted"}]
        self.assertEqual(_portal_health("applied", "submitted", None, log), "healthy")

    def test_a_hard_failure_is_a_portal_failure(self) -> None:
        self.assertEqual(_portal_health("failed", "apply_failed", "boom", []), "failure")

    def test_a_timeout_is_a_portal_failure_not_a_neutral_outcome(self) -> None:
        """The hole that mattered most: a hung browser times out on EVERY job, so treating this
        as neutral reset the breaker exactly when it was needed."""
        self.assertEqual(_portal_health("timed_out", "timed_out", "Timeout 30000ms", []),
                         "failure")

    def test_a_skip_from_a_page_that_never_opened_is_a_portal_failure(self) -> None:
        """Dice reports a dead browser as SKIPPED through its identity gate. The adapter run log
        is what gives it away."""
        log = [{"step": "job_open_failed", "error": "net::ERR_CONNECTION_REFUSED"},
               {"step": "identity_mismatch"}]
        self.assertEqual(_portal_health("skipped", "JOB_IDENTITY_MISMATCH", None, log), "failure")

    def test_a_legitimate_data_skip_is_neutral_not_healthy(self) -> None:
        """URL_UNRESOLVED is decided without opening anything, so it proves nothing either way
        and must never clear a genuine failure streak."""
        log = [{"step": "url_unresolved", "status": "URL_UNRESOLVED"}]
        self.assertEqual(_portal_health("skipped", "URL_UNRESOLVED", None, log), "neutral")

    def test_a_browser_crash_is_a_portal_failure(self) -> None:
        self.assertEqual(
            _portal_health("skipped", "JOB_IDENTITY_MISMATCH",
                           "Target page, context or browser has been closed", []),
            "failure")

    def test_a_navigation_failure_is_a_portal_failure(self) -> None:
        self.assertEqual(
            _portal_health("skipped", "load_failed", "net::ERR_NAME_NOT_RESOLVED", []), "failure")

    def test_a_session_failure_is_a_portal_failure(self) -> None:
        log = [{"step": "session_verification_failed", "reason": "login_timeout"}]
        self.assertEqual(_portal_health("skipped", "session_check", None, log), "failure")


class BreakerEndToEndTests(DiceLifecycleTestCase):
    JOB_COUNT = 8

    def _run_with_outcomes(self, outcomes: list) -> ApplyTopBatch:
        """Drive the real worker with a scripted sequence of adapter behaviours."""
        batch = self._batch()
        script = iter(outcomes)

        class Scripted(DiceAdapterDouble):
            def apply_to_job(self, job, *args, **kwargs):
                behaviour = next(script)
                if behaviour == "timeout":
                    raise RuntimeError("Timeout 30000ms exceeded while navigating")
                if behaviour == "skip_dead_browser":
                    return AutomationResult(
                        ApplicationStatus.SKIPPED, "identity could not be verified",
                        [{"step": "job_open_failed", "error": "net::ERR_CONNECTION_REFUSED"}],
                        "JOB_IDENTITY_MISMATCH", None)
                if behaviour == "skip_legitimate":
                    return AutomationResult(
                        ApplicationStatus.SKIPPED, "no verified job-detail URL",
                        [{"step": "url_unresolved"}], "URL_UNRESOLVED", None)
                DiceAdapterDouble.applied.append(job.id)
                return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)

        controller = get_automation_controller()
        controller.start(batch.id)
        with patch("app.main.dice_adapter", Scripted):
            _run_apply_top_batch(batch.id)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def test_consecutive_timeouts_trip_the_breaker(self) -> None:
        ended = self._run_with_outcomes(["apply", "timeout", "timeout", "timeout", "apply"])
        self.assertEqual(ended.status, BATCH_STATUS_FAILED)
        self.assertEqual(len(ended.job_results), 4, "the run did not stop at the third timeout")
        self.assertIsNone(_resume_blocker(self.db, ended))

    def test_dead_browser_skips_do_not_stop_the_breaker_tripping(self) -> None:
        """Timeouts interleaved with the skips a dead browser produces must still trip it."""
        ended = self._run_with_outcomes(
            ["apply", "timeout", "skip_dead_browser", "timeout", "timeout", "apply"])
        self.assertEqual(ended.status, BATCH_STATUS_FAILED)
        self.assertIsNone(_resume_blocker(self.db, ended))

    def test_legitimate_skips_alone_never_trip_the_breaker(self) -> None:
        ended = self._run_with_outcomes(
            ["skip_legitimate"] * 4 + ["apply", "apply", "apply", "apply"])
        self.assertEqual(ended.status, "completed",
                         "legitimate skips must not be mistaken for a dead portal")

    def test_a_healthy_run_with_scattered_timeouts_still_completes(self) -> None:
        ended = self._run_with_outcomes(
            ["timeout", "timeout", "apply", "timeout", "timeout", "apply", "apply", "apply"])
        self.assertEqual(ended.status, "completed")
        self.assertEqual(len(ended.job_results), self.JOB_COUNT)


if __name__ == "__main__":
    unittest.main()
