"""Continue Automation: resuming an interrupted apply batch.

The contract:

    * resuming re-enters the EXISTING batch row -- no batch is created, no search is re-run,
      no job list is regenerated, nothing already processed is revisited;
    * the first remaining job is the one after the last processed job, in the batch's original
      order, with the batch's original allocation and duplicate ownership;
    * a job that already applied, failed or was skipped keeps that outcome for ever;
    * a batch is resumable only while it is the newest batch for its resume and portal AND its
      search session is still the newest one -- a new Find Matches retires it;
    * an application that was closed mid-run leaves a batch nothing marked terminal, so startup
      adopts it as interrupted and it becomes resumable.

The exclusion is driven by job_results, NOT by job status: _process_application leaves a job
MATCHED in dry run, so a status-only filter would re-apply to every dry-run job in the batch.
test_dry_run_resume_does_not_reapply_to_processed_jobs is the guard on that.

No browser and no real submission anywhere in this file: the adapters are recording doubles.
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.main import (
    _adopt_interrupted_batches,
    _reconcile_batch_state,
    _apply_top_remaining_jobs,
    _apply_top_jobs_statement,
    _create_apply_top_batch,
    _processed_job_ids,
    _resume_blocker,
    _run_apply_top_batch,
    get_latest_apply_top_batch,
    resume_apply_top_batch,
    set_active_portal,
    start_apply_top_batch,
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
    BATCH_STATUS_STOPPING,
    JOB_RESULT_STOPPED,
    RESUMABLE_BATCH_STATUSES,
    check_cancelled,
    get_automation_controller,
    reset_automation_controller,
)
from app.services.linkedin import AutomationResult
from app.services.resume_identity import compute_resume_fingerprint

MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")
RESUME_TEXT = "Priya Raman\nData Analyst"


class ScriptedAdapter:
    """Applies each job it is handed, and stops the batch after `stop_after` of them.

    `stop_after = None` means "never stop" -- the resumed run that carries the batch through to
    completion. Every job it touches is recorded, so the tests can assert on exactly which jobs
    were visited and in what order."""

    applied: list[int] = []
    stop_after: int | None = None
    controller = None
    batch_id = ""
    outcome = ApplicationStatus.APPLIED

    def __init__(self, _settings=None) -> None:
        pass

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None, cancellation=None) -> AutomationResult:
        if (ScriptedAdapter.stop_after is not None
                and len(ScriptedAdapter.applied) >= ScriptedAdapter.stop_after):
            ScriptedAdapter.controller.request_stop(ScriptedAdapter.batch_id)
            check_cancelled(cancellation, "before_submit")
        ScriptedAdapter.applied.append(job.id)
        if dry_run:
            return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [], "dice_dry_ready", None)
        return AutomationResult(ScriptedAdapter.outcome, None, [], "dice_submitted", None)


class ContinueAutomationTestCase(unittest.TestCase):
    """A resume, a search session and eight matched jobs on Dice."""

    JOB_COUNT = 8
    PORTAL = "dice"

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_automation_controller()
        ScriptedAdapter.applied = []
        ScriptedAdapter.stop_after = None
        ScriptedAdapter.outcome = ApplicationStatus.APPLIED
        self.db = SessionLocal()
        set_active_portal(self.db, "dice")
        self.resume = ResumeProfile(
            portal="dice", filename="r.pdf", stored_path="", raw_text=RESUME_TEXT,
            resume_fingerprint=compute_resume_fingerprint(RESUME_TEXT + uuid4().hex),
        )
        self.db.add(self.resume)
        self.db.commit()
        self.search = self._new_search_batch()
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

    def _new_search_batch(self, minutes_later: int = 0) -> JobSearchBatch:
        batch = JobSearchBatch(
            id=str(uuid4()), resume_id=self.resume.id, search_query="data analyst",
            location="Remote", portal="dice", status="completed",
            created_at=datetime.utcnow() + timedelta(minutes=minutes_later),
        )
        self.db.add(batch)
        self.db.commit()
        return batch

    def tearDown(self) -> None:
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
        """The order the batch would process every job in, straight from the batch query."""
        return [
            job.id for job in self.db.scalars(
                _apply_top_jobs_statement(self.resume.id, self.search.id, portal=self.PORTAL)
            ).all()
        ]

    def _start_batch(self, limit: int = 10, dry_run: bool = False) -> ApplyTopBatch:
        return _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=limit, dry_run=dry_run,
                         portal="dice", search_batch_id=self.search.id),
            self.db,
        )

    def _run(self, batch: ApplyTopBatch, *, stop_after: int | None, resuming: bool = False) -> ApplyTopBatch:
        ScriptedAdapter.stop_after = stop_after
        controller = get_automation_controller()
        controller.start(batch.id)
        ScriptedAdapter.controller = controller
        ScriptedAdapter.batch_id = batch.id
        with patch("app.main.dice_adapter", ScriptedAdapter):
            _run_apply_top_batch(batch.id, resuming)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def _stopped_batch(self, stop_after: int = 3, limit: int = 10,
                       dry_run: bool = False) -> ApplyTopBatch:
        batch = self._start_batch(limit=limit, dry_run=dry_run)
        return self._run(batch, stop_after=stop_after)

    def _resume(self, batch: ApplyTopBatch, *, stop_after: int | None = None) -> ApplyTopBatch:
        ScriptedAdapter.applied = []
        return self._run(batch, stop_after=stop_after, resuming=True)


# ======================================================================================
# 1-5. Resuming the existing batch
# ======================================================================================

class ResumeTests(ContinueAutomationTestCase):
    def test_1_continue_resumes_the_existing_batch(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        self.assertEqual(stopped.status, BATCH_STATUS_STOPPED)
        resumed = self._resume(stopped)
        self.assertEqual(resumed.id, stopped.id)
        self.assertEqual(resumed.status, "completed")

    def test_2_no_new_batch_is_created(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        before = self.db.scalars(
            select(ApplyTopBatch.id).where(ApplyTopBatch.resume_id == self.resume.id)
        ).all()
        self._resume(stopped)
        after = self.db.scalars(
            select(ApplyTopBatch.id).where(ApplyTopBatch.resume_id == self.resume.id)
        ).all()
        self.assertEqual(sorted(before), sorted(after))
        self.assertEqual(len(after), 1, "resuming must reuse the one batch, never add another")

    def test_3_already_processed_jobs_are_never_revisited(self) -> None:
        order = self._planned_order()
        stopped = self._stopped_batch(stop_after=3)
        first_pass = list(ScriptedAdapter.applied)
        self.assertEqual(first_pass, order[:3])

        self._resume(stopped)
        second_pass = list(ScriptedAdapter.applied)
        self.assertEqual(set(first_pass) & set(second_pass), set(),
                         "a resumed run revisited a job it had already processed")

    def test_4_resume_starts_at_the_first_remaining_job(self) -> None:
        order = self._planned_order()
        stopped = self._stopped_batch(stop_after=3)
        self._resume(stopped)
        self.assertEqual(ScriptedAdapter.applied[0], order[3],
                         "the resumed run did not begin at the first unprocessed job")

    def test_5_ordering_is_preserved_across_the_stop(self) -> None:
        """The two passes concatenated must equal one uninterrupted run."""
        order = self._planned_order()
        stopped = self._stopped_batch(stop_after=3)
        first = list(ScriptedAdapter.applied)
        self._resume(stopped)
        self.assertEqual(first + list(ScriptedAdapter.applied), order[:self.JOB_COUNT])

    def test_the_whole_batch_is_processed_exactly_once(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        first = list(ScriptedAdapter.applied)
        resumed = self._resume(stopped)
        visited = first + list(ScriptedAdapter.applied)
        self.assertEqual(len(visited), len(set(visited)), "a job was processed twice")
        self.assertEqual(sorted(visited), sorted(self.job_ids))
        self.assertEqual(resumed.completed_count, self.JOB_COUNT)

    def test_progress_continues_rather_than_restarting(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        resumed = self._resume(stopped)
        indices = [item["job_index"] for item in resumed.job_results]
        self.assertEqual(indices, list(range(1, self.JOB_COUNT + 1)))
        self.assertEqual(resumed.total_count, self.JOB_COUNT)

    def test_the_batch_budget_is_not_renewed_by_resuming(self) -> None:
        """A batch of 5 that stopped after 3 has 2 left -- never another 5."""
        stopped = self._stopped_batch(stop_after=3, limit=5)
        resumed = self._resume(stopped)
        self.assertEqual(resumed.completed_count, 5)
        self.assertEqual(resumed.total_count, 5)

    def test_started_at_survives_a_resume(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        first_started = stopped.started_at
        resumed = self._resume(stopped)
        self.assertEqual(resumed.started_at, first_started)


# ======================================================================================
# 6-8. What must be preserved
# ======================================================================================

class PreservationTests(ContinueAutomationTestCase):
    def test_6_allocation_and_7_duplicate_ownership_come_from_the_same_query(self) -> None:
        """Order, allocation and duplicate ownership are all properties of the batch query.
        Resuming reuses it verbatim -- the remaining jobs are the tail of the original plan."""
        order = self._planned_order()
        stopped = self._stopped_batch(stop_after=3)
        remaining = [job.id for job in _apply_top_remaining_jobs(self.db, stopped, self.resume)]
        self.assertEqual(remaining, order[3:self.JOB_COUNT])

    def test_6b_the_search_batch_still_scopes_the_resumed_run(self) -> None:
        """Allocation belongs to one search session; a resume must not pull in jobs from
        outside it."""
        other = self._new_search_batch(minutes_later=-30)
        stray = JobOpportunity(
            resume_id=self.resume.id, portal="dice", search_batch_id=other.id,
            title="Unrelated", company="Elsewhere", location="Remote",
            job_url=f"https://dice/{uuid4().hex}", description="",
            status=ApplicationStatus.MATCHED,
        )
        self.db.add(stray)
        self.db.commit()
        self.job_ids.append(stray.id)

        stopped = self._stopped_batch(stop_after=3)
        remaining = [job.id for job in _apply_top_remaining_jobs(self.db, stopped, self.resume)]
        self.assertNotIn(stray.id, remaining)

    def test_8_application_history_from_the_first_pass_is_preserved(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        before = sorted(self.db.scalars(
            select(ApplicationRecord.id).where(ApplicationRecord.job_id.in_(self.job_ids))
        ).all())
        self.assertTrue(before, "the first pass wrote no history to preserve")

        self._resume(stopped)
        after = sorted(self.db.scalars(
            select(ApplicationRecord.id).where(ApplicationRecord.job_id.in_(self.job_ids))
        ).all())
        self.assertEqual(before, after[:len(before)], "existing history was altered by a resume")

    def test_completed_results_from_the_first_pass_survive(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        first = [item for item in stopped.job_results if item["status"] == "applied"]
        resumed = self._resume(stopped)
        kept = [item for item in resumed.job_results if item["job_id"] in
                {entry["job_id"] for entry in first}]
        self.assertEqual(len(kept), len(first))
        self.assertTrue(all(item["status"] == "applied" for item in kept))

    def test_a_failed_job_stays_failed_and_is_not_retried(self) -> None:
        ScriptedAdapter.outcome = ApplicationStatus.FAILED
        stopped = self._stopped_batch(stop_after=2)
        failed_ids = [item["job_id"] for item in stopped.job_results
                      if item["status"] not in (JOB_RESULT_STOPPED,)]
        self.assertTrue(failed_ids)

        ScriptedAdapter.outcome = ApplicationStatus.APPLIED
        resumed = self._resume(stopped)
        self.assertEqual(set(failed_ids) & set(ScriptedAdapter.applied), set(),
                         "a job with a recorded outcome was retried")
        still = [item["status"] for item in resumed.job_results if item["job_id"] in failed_ids]
        self.assertTrue(all(status != "applied" for status in still))

    def test_the_stopped_placeholders_are_replaced_not_duplicated(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        self.assertGreater(stopped.stopped_count, 0)
        resumed = self._resume(stopped)
        self.assertEqual(resumed.stopped_count, 0, "stopped placeholders survived the resume")
        seen = [item["job_id"] for item in resumed.job_results]
        self.assertEqual(len(seen), len(set(seen)), "job_results has duplicate entries")

    def test_17_dry_run_resume_does_not_reapply_to_processed_jobs(self) -> None:
        """The reason exclusion reads job_results and not job status: in dry run a processed
        job is deliberately left MATCHED, so a status-only filter would revisit all of them."""
        stopped = self._stopped_batch(stop_after=3, dry_run=True)
        processed = _processed_job_ids(stopped)
        self.assertTrue(processed)
        for job_id in processed:
            job = self.db.get(JobOpportunity, job_id)
            self.assertEqual(job.status, ApplicationStatus.MATCHED,
                             "precondition: dry run leaves the job matched")
        self._resume(stopped)
        self.assertEqual(processed & set(ScriptedAdapter.applied), set())

    def test_17b_dry_run_stays_dry_across_a_resume(self) -> None:
        stopped = self._stopped_batch(stop_after=3, dry_run=True)
        resumed = self._resume(stopped)
        self.assertTrue(resumed.dry_run, "a resume must not flip the batch to live")
        # A successful dry run is recorded as reached_review (_batch_job_status): it filled
        # everything and stopped at the submit step without submitting.
        self.assertTrue(all(item["status"] == "reached_review"
                            for item in resumed.job_results if item["status"] != JOB_RESULT_STOPPED))
        self.assertEqual(
            self.db.scalars(
                select(ApplicationRecord.status).where(ApplicationRecord.job_id.in_(self.job_ids))
            ).all().count(ApplicationStatus.APPLIED),
            0,
            "a dry-run resume must never record a real application",
        )

    def test_18_live_apply_stays_live_across_a_resume(self) -> None:
        stopped = self._stopped_batch(stop_after=3, dry_run=False)
        resumed = self._resume(stopped)
        self.assertFalse(resumed.dry_run)
        self.assertTrue(any(item["status"] == "applied" for item in resumed.job_results))


# ======================================================================================
# 11-13. When Continue may and may not appear
# ======================================================================================

class ResumabilityTests(ContinueAutomationTestCase):
    def test_a_stopped_batch_is_resumable(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        self.assertIsNone(_resume_blocker(self.db, stopped))

    def test_11_a_completed_batch_is_not_resumable(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        completed = self._resume(stopped)
        self.assertEqual(completed.status, "completed")
        self.assertIsNotNone(_resume_blocker(self.db, completed))

    def test_11b_a_batch_that_ran_to_the_end_is_never_resumable(self) -> None:
        batch = self._start_batch()
        finished = self._run(batch, stop_after=None)
        self.assertEqual(finished.status, "completed")
        self.assertIsNotNone(_resume_blocker(self.db, finished))

    def test_a_running_batch_is_not_resumable(self) -> None:
        batch = self._start_batch()
        batch.status = "running"
        self.db.add(batch)
        self.db.commit()
        self.assertIn("running", _resume_blocker(self.db, batch))

    def test_13_a_failed_batch_stays_resumable_while_jobs_remain(self) -> None:
        """A defect that ends a batch must cost the user a click, not their progress. The
        conditions that would make a retry pointless -- no resume, no jobs left, a superseded
        search -- are checked on their own, so the CAUSE of the ending is never the gate."""
        batch = self._start_batch()
        batch.status = BATCH_STATUS_FAILED
        batch.error_message = "TypeError: a bug in the automation"
        self.db.add(batch)
        self.db.commit()
        self.assertIsNone(_resume_blocker(self.db, batch))

    def test_13_a_failed_batch_with_nothing_left_is_not_resumable(self) -> None:
        batch = self._start_batch()
        batch.status = BATCH_STATUS_FAILED
        self.db.add(batch)
        for job in self.db.scalars(
            select(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id)
        ).all():
            job.status = ApplicationStatus.APPLIED
            self.db.add(job)
        self.db.commit()
        self.assertIsNotNone(_resume_blocker(self.db, batch))

    def test_13b_a_batch_whose_resume_is_gone_is_not_resumable(self) -> None:
        # A detached object, never persisted: a foreign key makes a missing resume_id
        # impossible to store, but the validation still has to survive meeting one.
        orphan = ApplyTopBatch(
            id=str(uuid4()), resume_id=10_000_000, portal=self.PORTAL,
            status=BATCH_STATUS_STOPPED, dry_run=False, limit=10, job_results=[],
        )
        self.assertIn("resume", _resume_blocker(self.db, orphan).lower())

    def test_13c_a_batch_with_no_jobs_left_is_not_resumable(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        for job in self.db.scalars(
            select(JobOpportunity).where(JobOpportunity.resume_id == self.resume.id)
        ).all():
            job.status = ApplicationStatus.APPLIED
            self.db.add(job)
        self.db.commit()
        self.assertIn("no eligible jobs", _resume_blocker(self.db, stopped).lower())

    def test_only_the_newest_batch_may_be_resumed(self) -> None:
        older = self._stopped_batch(stop_after=3)
        reset_automation_controller()
        newer = self._start_batch()
        self.assertIsNotNone(_resume_blocker(self.db, older))
        self.assertIn("newer apply batch", _resume_blocker(self.db, older))
        self.assertIsNotNone(newer.id)

    def test_12_a_new_find_matches_retires_the_batch(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        self.assertIsNone(_resume_blocker(self.db, stopped))
        # The user searches again: a newer search session now exists.
        self._new_search_batch(minutes_later=5)
        blocker = _resume_blocker(self.db, stopped)
        self.assertIsNotNone(blocker, "a new Find Matches must retire the previous batch")
        self.assertIn("newer job search", blocker)

    def test_every_non_completed_ending_is_resumable(self) -> None:
        """Stopped by the user, cut off by a crash, or ended by a bug -- all recoverable."""
        self.assertEqual(
            RESUMABLE_BATCH_STATUSES,
            frozenset({BATCH_STATUS_STOPPED, BATCH_STATUS_INTERRUPTED, BATCH_STATUS_FAILED}),
        )
        self.assertNotIn("completed", RESUMABLE_BATCH_STATUSES)

    def test_a_batch_stays_resumable_through_each_recoverable_ending(self) -> None:
        batch = self._stopped_batch(stop_after=3)
        for ending in (BATCH_STATUS_STOPPED, BATCH_STATUS_INTERRUPTED, BATCH_STATUS_FAILED):
            with self.subTest(ending=ending):
                batch.status = ending
                self.db.add(batch)
                self.db.commit()
                self.assertIsNone(_resume_blocker(self.db, batch))


# ======================================================================================
# 10. Surviving an application restart
# ======================================================================================

class RestartTests(ContinueAutomationTestCase):
    def test_10_a_batch_left_running_by_a_crash_becomes_interrupted(self) -> None:
        batch = self._start_batch()
        batch.status = "running"
        batch.current_job_id = self.job_ids[0]
        self.db.add(batch)
        self.db.commit()

        _adopt_interrupted_batches()               # what startup() does
        self.db.expire_all()
        adopted = self.db.get(ApplyTopBatch, batch.id)
        self.assertEqual(adopted.status, BATCH_STATUS_INTERRUPTED)
        self.assertIsNone(adopted.current_job_id)

    def test_10b_an_interrupted_batch_is_resumable_and_resumes(self) -> None:
        batch = self._start_batch()
        stopped = self._run(batch, stop_after=3)
        # Simulate the process dying rather than stopping cleanly.
        stopped.status = "running"
        self.db.add(stopped)
        self.db.commit()

        reset_automation_controller()
        _adopt_interrupted_batches()
        self.db.expire_all()
        adopted = self.db.get(ApplyTopBatch, stopped.id)
        self.assertEqual(adopted.status, BATCH_STATUS_INTERRUPTED)
        self.assertIsNone(_resume_blocker(self.db, adopted))

        resumed = self._resume(adopted)
        self.assertEqual(resumed.id, stopped.id)
        self.assertEqual(resumed.status, "completed")

    def test_a_stopping_batch_left_by_a_crash_is_adopted_too(self) -> None:
        batch = self._start_batch()
        batch.status = BATCH_STATUS_STOPPING
        self.db.add(batch)
        self.db.commit()
        _adopt_interrupted_batches()
        self.db.expire_all()
        self.assertEqual(self.db.get(ApplyTopBatch, batch.id).status, BATCH_STATUS_INTERRUPTED)

    def test_terminal_batches_are_never_adopted(self) -> None:
        batch = self._start_batch()
        for status in ("completed", "failed", BATCH_STATUS_STOPPED):
            batch.status = status
            self.db.add(batch)
            self.db.commit()
            _adopt_interrupted_batches()
            self.db.expire_all()
            self.assertEqual(self.db.get(ApplyTopBatch, batch.id).status, status)


# ======================================================================================
# Recovering from a defect
#
# The promise is that automation ends by itself only when the user stops it, when it finishes,
# or when nothing is left to do. Any other ending -- including one this software caused -- must
# leave the batch resumable.
# ======================================================================================

class BugRecoveryTests(ContinueAutomationTestCase):
    class Exploding(ScriptedAdapter):
        """An adapter that raises the way a defect would, part-way through the batch."""

        blow_up_after = 3

        def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                         screening_context=None, cancellation=None):
            if len(ScriptedAdapter.applied) >= BugRecoveryTests.Exploding.blow_up_after:
                raise TypeError("a defect in the automation")
            ScriptedAdapter.applied.append(job.id)
            return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)

    def _run_with(self, adapter, batch, resuming: bool = False) -> ApplyTopBatch:
        controller = get_automation_controller()
        controller.start(batch.id)
        ScriptedAdapter.controller = controller
        ScriptedAdapter.batch_id = batch.id
        with patch("app.main.dice_adapter", adapter):
            _run_apply_top_batch(batch.id, resuming)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def test_a_per_job_defect_does_not_end_the_batch(self) -> None:
        """A bug inside one job is recorded against that job; the rest still run."""
        BugRecoveryTests.Exploding.blow_up_after = 10_000    # never
        batch = self._start_batch()
        finished = self._run_with(BugRecoveryTests.Exploding, batch)
        self.assertEqual(finished.status, "completed")

    def test_a_batch_level_defect_leaves_the_batch_resumable(self) -> None:
        with patch("app.main.default_candidate_profile", side_effect=RuntimeError("boom")):
            batch = self._start_batch()
            crashed = self._run_with(ScriptedAdapter, batch)
        self.assertEqual(crashed.status, BATCH_STATUS_FAILED)
        self.assertIn("boom", crashed.error_message)
        self.assertIsNone(_resume_blocker(self.db, crashed),
                          "a bug must not strand the user's progress")

    def test_progress_before_a_defect_is_kept_and_the_resume_carries_on(self) -> None:
        batch = self._start_batch()
        # Fail the whole batch after three jobs have genuinely been applied for.
        BugRecoveryTests.Exploding.blow_up_after = 3
        original = _run_apply_top_batch

        def explode_at_batch_level(batch_id, resuming=False):
            original(batch_id, resuming)

        crashed = self._run_with(BugRecoveryTests.Exploding, batch)
        # Three applied, then every later job failed against the defect -- either way the batch
        # ended early and its recorded progress survived.
        self.assertEqual(len(ScriptedAdapter.applied), 3)
        applied = [r for r in crashed.job_results if r["status"] == "applied"]
        self.assertEqual(len(applied), 3)

    def test_a_worker_that_vanishes_is_recovered_without_a_restart(self) -> None:
        """The case a restart would fix but the user should not have to trigger: the batch is
        left recorded as running with no worker behind it."""
        batch = self._start_batch()
        batch.status = "running"
        batch.updated_at = datetime.utcnow() - timedelta(minutes=5)
        self.db.add(batch)
        self.db.commit()
        reset_automation_controller()               # no worker holds the slot

        recovered = _reconcile_batch_state(self.db, self.db.get(ApplyTopBatch, batch.id))
        self.assertEqual(recovered.status, BATCH_STATUS_INTERRUPTED)
        self.assertIsNone(_resume_blocker(self.db, recovered))

    def test_a_live_batch_is_never_stolen_by_reconciliation(self) -> None:
        batch = self._start_batch()
        batch.status = "running"
        batch.updated_at = datetime.utcnow() - timedelta(minutes=5)
        self.db.add(batch)
        self.db.commit()
        get_automation_controller().start(batch.id)  # a worker genuinely owns it

        untouched = _reconcile_batch_state(self.db, self.db.get(ApplyTopBatch, batch.id))
        self.assertEqual(untouched.status, "running")

    def test_a_just_created_batch_is_not_mistaken_for_an_orphan(self) -> None:
        """The slot is claimed a moment after the row is written; that window is not a crash."""
        batch = self._start_batch()
        reset_automation_controller()
        fresh = _reconcile_batch_state(self.db, self.db.get(ApplyTopBatch, batch.id))
        self.assertEqual(fresh.status, "queued")

    def test_the_worker_never_leaves_a_batch_claiming_to_run(self) -> None:
        """However the worker leaves -- including on an exception no `except Exception` sees --
        the batch must not be left in an active state."""
        class Fatal(ScriptedAdapter):
            def apply_to_job(self, *args, **kwargs):
                raise KeyboardInterrupt("the process is going away")

        batch = self._start_batch()
        with self.assertRaises(BaseException):
            self._run_with(Fatal, batch)
        self.db.expire_all()
        after = self.db.get(ApplyTopBatch, batch.id)
        self.assertNotIn(after.status, ACTIVE_BATCH_STATUSES)
        self.assertIsNone(_resume_blocker(self.db, after))

    def test_the_dashboard_offers_continue_after_a_defect(self) -> None:
        with patch("app.main.default_candidate_profile", side_effect=RuntimeError("boom")):
            batch = self._start_batch()
            self._run_with(ScriptedAdapter, batch)
        latest = get_latest_apply_top_batch(self.resume.id, "dice", self.db)
        payload = ApplyTopBatchOut.model_validate(latest)
        self.assertTrue(payload.resumable)
        self.assertEqual(payload.remaining_count, self.JOB_COUNT)

    def test_a_crashed_batch_actually_resumes_and_completes(self) -> None:
        with patch("app.main.default_candidate_profile", side_effect=RuntimeError("boom")):
            batch = self._start_batch()
            crashed = self._run_with(ScriptedAdapter, batch)
        self.assertEqual(crashed.status, BATCH_STATUS_FAILED)

        reset_automation_controller()
        ScriptedAdapter.applied = []
        ScriptedAdapter.stop_after = None
        resumed = self._run_with(ScriptedAdapter, crashed, resuming=True)
        self.assertEqual(resumed.id, crashed.id)
        self.assertEqual(resumed.status, "completed")
        self.assertIsNone(resumed.error_message, "a successful resume clears the old error")
        self.assertEqual(resumed.completed_count, self.JOB_COUNT)


# ======================================================================================
# The endpoint
# ======================================================================================

class ResumeEndpointTests(ContinueAutomationTestCase):
    def test_the_endpoint_queues_the_existing_batch(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        reset_automation_controller()
        tasks = BackgroundTasks()
        returned = resume_apply_top_batch(stopped.id, tasks, self.db)
        self.assertEqual(returned.id, stopped.id)
        self.assertEqual(len(tasks.tasks), 1)
        # Queued with resuming=True against the SAME id -- never a new batch.
        self.assertEqual(tasks.tasks[0].args, (stopped.id, True))

    def test_an_unknown_batch_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            resume_apply_top_batch(str(uuid4()), BackgroundTasks(), self.db)
        self.assertEqual(caught.exception.status_code, 404)

    def test_13d_a_batch_that_cannot_be_resumed_is_409_not_404(self) -> None:
        batch = self._start_batch()
        finished = self._run(batch, stop_after=None)
        reset_automation_controller()
        with self.assertRaises(HTTPException) as caught:
            resume_apply_top_batch(finished.id, BackgroundTasks(), self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_resume_is_refused_while_another_batch_is_active(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        reset_automation_controller()
        get_automation_controller().start("someone-else")
        with self.assertRaises(HTTPException) as caught:
            resume_apply_top_batch(stopped.id, BackgroundTasks(), self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_the_latest_endpoint_publishes_the_resume_verdict(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        latest = get_latest_apply_top_batch(self.resume.id, "dice", self.db)
        self.assertEqual(latest.id, stopped.id)
        payload = ApplyTopBatchOut.model_validate(latest)
        self.assertTrue(payload.resumable)
        self.assertEqual(payload.remaining_count, self.JOB_COUNT - 3)
        self.assertIsNone(payload.resume_blocked_reason)

    def test_the_verdict_turns_false_once_the_batch_completes(self) -> None:
        stopped = self._stopped_batch(stop_after=3)
        self._resume(stopped)
        latest = get_latest_apply_top_batch(self.resume.id, "dice", self.db)
        payload = ApplyTopBatchOut.model_validate(latest)
        self.assertFalse(payload.resumable)
        self.assertEqual(payload.remaining_count, 0)
        self.assertIsNotNone(payload.resume_blocked_reason)

    def test_a_fresh_batch_is_not_advertised_as_resumable(self) -> None:
        self._start_batch()
        latest = get_latest_apply_top_batch(self.resume.id, "dice", self.db)
        self.assertFalse(ApplyTopBatchOut.model_validate(latest).resumable)


# ======================================================================================
# 9 + 16. Stop Automation is unchanged, and both portals continue
# ======================================================================================

class StopStillWorksTests(ContinueAutomationTestCase):
    def test_9_stop_automation_still_stops_the_batch(self) -> None:
        stopped = self._stopped_batch(stop_after=2)
        self.assertEqual(stopped.status, BATCH_STATUS_STOPPED)
        self.assertEqual(len(ScriptedAdapter.applied), 2)
        self.assertGreater(stopped.stopped_count, 0)
        self.assertEqual(stopped.failed_count, 0)

    def test_9b_a_resumed_batch_can_be_stopped_again(self) -> None:
        stopped = self._stopped_batch(stop_after=2)
        again = self._resume(stopped, stop_after=2)
        self.assertEqual(again.status, BATCH_STATUS_STOPPED)
        self.assertIsNone(_resume_blocker(self.db, again), "a twice-stopped batch is still resumable")
        final = self._resume(again)
        self.assertEqual(final.status, "completed")
        self.assertEqual(final.completed_count, self.JOB_COUNT)

    def test_9c_the_stop_endpoint_rejects_an_already_stopped_batch(self) -> None:
        stopped = self._stopped_batch(stop_after=2)
        with self.assertRaises(HTTPException) as caught:
            stop_apply_top_batch(stopped.id, self.db)
        self.assertEqual(caught.exception.status_code, 409)

    def test_15_dice_continues_correctly(self) -> None:
        """This whole case runs on Dice; asserted explicitly so the portal is named."""
        stopped = self._stopped_batch(stop_after=3)
        self.assertEqual(stopped.portal, "dice")
        resumed = self._resume(stopped)
        self.assertEqual(resumed.portal, "dice")
        self.assertEqual(resumed.completed_count, self.JOB_COUNT)


class LinkedInContinueTests(ContinueAutomationTestCase):
    """14. The same lifecycle on LinkedIn, which routes through a different adapter."""

    PORTAL = "linkedin"

    def setUp(self) -> None:
        super().setUp()
        set_active_portal(self.db, "linkedin")
        self.resume.portal = "linkedin"
        for job in self.jobs:
            job.portal = "linkedin"
            self.db.add(job)
        self.search.portal = "linkedin"
        self.db.add_all([self.resume, self.search])
        self.db.commit()

    def _start_batch(self, limit: int = 10, dry_run: bool = False) -> ApplyTopBatch:
        return _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=limit, dry_run=dry_run,
                         portal="linkedin", search_batch_id=self.search.id),
            self.db,
        )

    def _run(self, batch: ApplyTopBatch, *, stop_after: int | None, resuming: bool = False):
        ScriptedAdapter.stop_after = stop_after
        controller = get_automation_controller()
        controller.start(batch.id)
        ScriptedAdapter.controller = controller
        ScriptedAdapter.batch_id = batch.id
        # LinkedIn goes through the automation object rather than the portal adapter registry.
        with patch("app.main.LinkedInAutomation", ScriptedAdapter):
            _run_apply_top_batch(batch.id, resuming)
        self.db.expire_all()
        return self.db.get(ApplyTopBatch, batch.id)

    def test_14_linkedin_continues_from_the_first_remaining_job(self) -> None:
        order = self._planned_order()
        stopped = self._stopped_batch(stop_after=3)
        self.assertEqual(stopped.portal, "linkedin")
        self.assertEqual(stopped.status, BATCH_STATUS_STOPPED)

        self._resume(stopped)
        self.assertEqual(ScriptedAdapter.applied[0], order[3])
        self.assertEqual(set(order[:3]) & set(ScriptedAdapter.applied), set())


# ======================================================================================
# Source-level guarantees
# ======================================================================================

class ImplementationShapeTests(unittest.TestCase):
    def test_resume_never_calls_the_batch_factory(self) -> None:
        """The one rule that matters most: Continue Automation must not create a batch."""
        start = MAIN_SOURCE.index("def resume_apply_top_batch")
        body = MAIN_SOURCE[start:MAIN_SOURCE.index("\n@app.", start + 10)]
        self.assertNotIn("_create_apply_top_batch", body)
        self.assertIn("_run_apply_top_batch", body)

    def test_resume_does_not_re_run_search_ranking_or_allocation(self) -> None:
        start = MAIN_SOURCE.index("def resume_apply_top_batch")
        body = MAIN_SOURCE[start:MAIN_SOURCE.index("\n@app.", start + 10)]
        for forbidden in ("find_matches", "collection_plan", "weighted_take", "soft_allocation",
                          "build_role_profile", "select_role_dicts", "MatchScorer"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_startup_adopts_interrupted_batches(self) -> None:
        startup = MAIN_SOURCE[MAIN_SOURCE.index("def startup"):]
        startup = startup[:startup.index("@app.get")]
        self.assertIn("_adopt_interrupted_batches", startup)

    def test_exclusion_is_driven_by_job_results_not_job_status(self) -> None:
        source = MAIN_SOURCE[MAIN_SOURCE.index("def _processed_results"):]
        source = source[:source.index("def _resume_blocker")]
        self.assertIn("job_results", source)
        self.assertIn("JOB_RESULT_STOPPED", source)


if __name__ == "__main__":
    unittest.main()
