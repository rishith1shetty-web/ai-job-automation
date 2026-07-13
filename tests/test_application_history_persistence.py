"""Application History is PERMANENT history.

Covers the five regression scenarios for history persistence and retrieval:

  1. Resume A -> LinkedIn -> Dry Run (10 successful) -> re-upload Resume A -> 10 visible.
  2. Resume A -> LinkedIn -> Live Apply (5)          -> re-upload Resume A -> 5 visible.
  3. Resume A -> LinkedIn (10) + Dice (7) -> LinkedIn history shows 10 only.
  4. Resume A -> Dice history shows 7 only.
  5. Resume B -> history empty.

plus: a successful dry run (DRY_RUN_READY) appears, unsuccessful statuses never appear,
history survives new search batches, no duplicate rows per job, and the legacy backfill
promotes dry runs that were stored as SKIPPED before the fix.

DB-backed tests run inside a transaction rolled back in tearDown, so nothing persists.
"""

import unittest
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.database import engine, init_db
from app.main import _record_application_outcome, list_applications
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
)
from app.services.application_outcome import (
    ATTEMPT_SPECIFIC_FIELDS,
    backfill_dry_run_ready_records,
)
from app.services.resume_identity import compute_resume_fingerprint

RESUME_A_TEXT = "Jane Doe\nData Analyst\nSkills: SQL, Python, Tableau\nExperience: 5 years"
RESUME_B_TEXT = "John Smith\nBackend Engineer\nSkills: Go, Kubernetes\nExperience: 8 years"

# Every status that must NEVER appear in Application History.
UNSUCCESSFUL_STATUSES = [
    ApplicationStatus.QUEUED,
    ApplicationStatus.MATCHED,
    ApplicationStatus.FAILED,
    ApplicationStatus.SKIPPED,
    ApplicationStatus.NEEDS_REVIEW,
    ApplicationStatus.UNSUPPORTED_FORM,
]


class _HistoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.connection = engine.connect()
        self.trans = self.connection.begin()
        self.db = Session(bind=self.connection)

    def tearDown(self) -> None:
        self.db.close()
        if self.trans.is_active:
            self.trans.rollback()
        self.connection.close()

    # helpers -------------------------------------------------------------------------
    def _resume(self, raw_text, portal) -> ResumeProfile:
        """An upload of a resume. Re-uploading the same text yields a NEW row with a new
        id but the SAME fingerprint — exactly what happens in production."""
        resume = ResumeProfile(
            portal=portal,
            filename="resume.pdf",
            stored_path="",
            raw_text=raw_text,
            resume_fingerprint=compute_resume_fingerprint(raw_text),
        )
        self.db.add(resume)
        self.db.flush()
        return resume

    def _batch(self, resume, portal) -> JobSearchBatch:
        batch = JobSearchBatch(
            id=str(uuid4()), resume_id=resume.id, search_query="q", location="Remote", portal=portal
        )
        self.db.add(batch)
        self.db.flush()
        return batch

    def _job(self, resume, portal, url, batch=None) -> JobOpportunity:
        job = JobOpportunity(
            resume_id=resume.id, portal=portal, title="Analyst", company="Acme",
            location="Remote", job_url=url, description="",
            search_batch_id=batch.id if batch else None,
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _app(self, job, status, current_step=None) -> ApplicationRecord:
        record = ApplicationRecord(
            job_id=job.id, portal=job.portal, status=status, current_step=current_step
        )
        self.db.add(record)
        self.db.flush()
        return record

    def _run(self, resume, portal, count, status, batch=None, offset=0):
        """Simulate an apply run: `count` jobs finishing with `status`."""
        for i in range(count):
            job = self._job(resume, portal, f"https://{portal}.com/jobs/{offset + i}", batch)
            self._app(job, status)


class HistorySurvivesResumeReupload(_HistoryTest):
    def test_scenario_1_dry_run_history_survives_reupload(self) -> None:
        a1 = self._resume(RESUME_A_TEXT, "linkedin")
        self._run(a1, "linkedin", 10, ApplicationStatus.DRY_RUN_READY)

        a2 = self._resume(RESUME_A_TEXT, "linkedin")  # re-upload -> new id, same fingerprint
        rows = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["status"] == ApplicationStatus.DRY_RUN_READY for r in rows))

    def test_scenario_2_live_apply_history_survives_reupload(self) -> None:
        a1 = self._resume(RESUME_A_TEXT, "linkedin")
        self._run(a1, "linkedin", 5, ApplicationStatus.APPLIED)

        a2 = self._resume(RESUME_A_TEXT, "linkedin")
        rows = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 5)

    def test_scenario_3_linkedin_history_excludes_dice(self) -> None:
        a_li = self._resume(RESUME_A_TEXT, "linkedin")
        a_di = self._resume(RESUME_A_TEXT, "dice")
        self._run(a_li, "linkedin", 10, ApplicationStatus.DRY_RUN_READY)
        self._run(a_di, "dice", 7, ApplicationStatus.DRY_RUN_READY, offset=500)

        a2 = self._resume(RESUME_A_TEXT, "linkedin")
        rows = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(r["job_portal"] == "linkedin" for r in rows))

    def test_scenario_4_dice_history_excludes_linkedin(self) -> None:
        a_li = self._resume(RESUME_A_TEXT, "linkedin")
        a_di = self._resume(RESUME_A_TEXT, "dice")
        self._run(a_li, "linkedin", 10, ApplicationStatus.DRY_RUN_READY)
        self._run(a_di, "dice", 7, ApplicationStatus.DRY_RUN_READY, offset=500)

        rows = list_applications(resume_id=a_di.id, portal="dice", db=self.db)
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(r["job_portal"] == "dice" for r in rows))

    def test_scenario_5_resume_b_history_is_empty(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        self._run(a, "linkedin", 10, ApplicationStatus.DRY_RUN_READY)

        b = self._resume(RESUME_B_TEXT, "linkedin")
        self.assertEqual(list_applications(resume_id=b.id, portal="linkedin", db=self.db), [])
        self.assertEqual(list_applications(resume_id=b.id, portal="dice", db=self.db), [])


class HistoryStatusRules(_HistoryTest):
    def test_dry_run_ready_appears_in_history(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        self._app(self._job(a, "linkedin", "https://li/1"), ApplicationStatus.DRY_RUN_READY)
        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], ApplicationStatus.DRY_RUN_READY)

    def test_applied_appears_in_history(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        self._app(self._job(a, "linkedin", "https://li/2"), ApplicationStatus.APPLIED)
        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], ApplicationStatus.APPLIED)

    def test_unsuccessful_statuses_never_appear(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        for i, status in enumerate(UNSUCCESSFUL_STATUSES):
            self._app(self._job(a, "linkedin", f"https://li/bad/{i}"), status)
        self.assertEqual(list_applications(resume_id=a.id, portal="linkedin", db=self.db), [])

    def test_only_successful_rows_survive_a_mixed_run(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        self._app(self._job(a, "linkedin", "https://li/ok1"), ApplicationStatus.APPLIED)
        self._app(self._job(a, "linkedin", "https://li/ok2"), ApplicationStatus.DRY_RUN_READY)
        self._app(self._job(a, "linkedin", "https://li/bad1"), ApplicationStatus.FAILED)
        self._app(self._job(a, "linkedin", "https://li/bad2"), ApplicationStatus.NEEDS_REVIEW)
        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {r["status"] for r in rows},
            {ApplicationStatus.APPLIED, ApplicationStatus.DRY_RUN_READY},
        )


class HistorySurvivesNewSearchBatches(_HistoryTest):
    def test_history_not_scoped_to_the_selected_search_batch(self) -> None:
        # Saturday's run belongs to batch 1; today a new search creates batch 2 and the UI
        # sends batch 2's id. Saturday's jobs must still be in history.
        a = self._resume(RESUME_A_TEXT, "linkedin")
        old_batch = self._batch(a, "linkedin")
        self._run(a, "linkedin", 10, ApplicationStatus.DRY_RUN_READY, batch=old_batch)

        a2 = self._resume(RESUME_A_TEXT, "linkedin")
        new_batch = self._batch(a2, "linkedin")

        rows = list_applications(
            resume_id=a2.id, portal="linkedin", search_batch_id=new_batch.id, db=self.db
        )
        self.assertEqual(len(rows), 10)


class HistoryHasNoDuplicates(_HistoryTest):
    def test_one_history_row_per_job_even_with_legacy_duplicates(self) -> None:
        # Legacy data: the same job was written twice (dry run, then live apply).
        a = self._resume(RESUME_A_TEXT, "linkedin")
        job = self._job(a, "linkedin", "https://li/dupe")
        self._app(job, ApplicationStatus.DRY_RUN_READY)
        self._app(job, ApplicationStatus.APPLIED)

        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["job_id"], job.id)
        # Newest record wins.
        self.assertEqual(rows[0]["status"], ApplicationStatus.APPLIED)


class LegacyDryRunBackfill(_HistoryTest):
    def test_backfill_promotes_skipped_dry_runs_to_dry_run_ready(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        legacy = self._app(
            self._job(a, "linkedin", "https://li/legacy"),
            ApplicationStatus.SKIPPED,
            current_step="dry_run_complete",
        )
        # Before the backfill the successful dry run is invisible in history.
        self.assertEqual(list_applications(resume_id=a.id, portal="linkedin", db=self.db), [])

        corrected = backfill_dry_run_ready_records(self.db)
        self.assertEqual(corrected, 1)
        self.assertEqual(legacy.status, ApplicationStatus.DRY_RUN_READY)

        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)

    def test_backfill_leaves_genuinely_skipped_jobs_alone(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        genuine = self._app(
            self._job(a, "linkedin", "https://li/genuine-skip"),
            ApplicationStatus.SKIPPED,
            current_step="already_applied",
        )
        self.assertEqual(backfill_dry_run_ready_records(self.db), 0)
        self.assertEqual(genuine.status, ApplicationStatus.SKIPPED)
        self.assertEqual(list_applications(resume_id=a.id, portal="linkedin", db=self.db), [])

    def test_backfill_is_idempotent(self) -> None:
        a = self._resume(RESUME_A_TEXT, "linkedin")
        self._app(
            self._job(a, "linkedin", "https://li/legacy2"),
            ApplicationStatus.SKIPPED,
            current_step="dry_run_complete",
        )
        self.assertEqual(backfill_dry_run_ready_records(self.db), 1)
        self.assertEqual(backfill_dry_run_ready_records(self.db), 0)  # re-run is a no-op
        self.assertEqual(len(list_applications(resume_id=a.id, portal="linkedin", db=self.db)), 1)


class OutcomePrecedence(_HistoryTest):
    """A recorded success is authoritative: a later failed attempt on the same persisted job
    must never erase it. Exercises the real persistence path (_record_application_outcome)."""

    def _attempt(self, job, status, evidence="outputs/run", error=None) -> ApplicationRecord:
        record = _record_application_outcome(
            self.db,
            job,
            portal=job.portal,
            status=status,
            dry_run=status is not ApplicationStatus.APPLIED,
            current_step="step",
            evidence_path=evidence,
            error_message=error,
            run_log=[],
        )
        self.db.add(record)
        self.db.flush()
        return record

    def _rows(self, resume, portal="linkedin"):
        return list_applications(resume_id=resume.id, portal=portal, db=self.db)

    def _setup(self):
        resume = self._resume(RESUME_A_TEXT, "linkedin")
        return resume, self._job(resume, "linkedin", "https://li/job-x")

    # --- a success is never downgraded -------------------------------------------------
    def test_1_dry_run_ready_then_failed_keeps_dry_run_ready(self) -> None:
        resume, job = self._setup()
        self._attempt(job, ApplicationStatus.DRY_RUN_READY, evidence="outputs/success")
        self._attempt(job, ApplicationStatus.FAILED, evidence="outputs/failure", error="boom")

        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(record.attempt_count, 2)          # the failure is still counted
        self.assertEqual(record.evidence_path, "outputs/success")  # success evidence intact
        self.assertEqual(len(self._rows(resume)), 1)       # still in Application History

    def test_2_applied_then_failed_keeps_applied(self) -> None:
        resume, job = self._setup()
        self._attempt(job, ApplicationStatus.APPLIED)
        self._attempt(job, ApplicationStatus.FAILED, error="boom")
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.APPLIED)
        self.assertEqual(len(self._rows(resume)), 1)

    def test_3_dry_run_ready_then_needs_review_keeps_dry_run_ready(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.DRY_RUN_READY)
        self._attempt(job, ApplicationStatus.NEEDS_REVIEW)
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)

    def test_4_applied_then_skipped_keeps_applied(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.APPLIED)
        self._attempt(job, ApplicationStatus.SKIPPED)
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_4b_success_survives_every_unsuccessful_status(self) -> None:
        for status in UNSUCCESSFUL_STATUSES:
            with self.subTest(status=status):
                _, job = self._setup()
                self._attempt(job, ApplicationStatus.DRY_RUN_READY)
                self._attempt(job, status)
                record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
                self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)

    # --- upgrades still happen ---------------------------------------------------------
    def test_5_failed_then_dry_run_ready_upgrades(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.FAILED)
        self._attempt(job, ApplicationStatus.DRY_RUN_READY, evidence="outputs/success")
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(record.evidence_path, "outputs/success")

    def test_6_failed_then_applied_upgrades(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.FAILED)
        self._attempt(job, ApplicationStatus.APPLIED)
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_7_dry_run_ready_then_applied_upgrades(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.DRY_RUN_READY)
        self._attempt(job, ApplicationStatus.APPLIED)
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.APPLIED)
        self.assertEqual(record.attempt_count, 2)

    def test_8_applied_then_dry_run_ready_keeps_applied(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.APPLIED)
        self._attempt(job, ApplicationStatus.DRY_RUN_READY)
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_unsuccessful_row_still_refreshes_on_a_later_unsuccessful_attempt(self) -> None:
        _, job = self._setup()
        self._attempt(job, ApplicationStatus.SKIPPED, error="first")
        self._attempt(job, ApplicationStatus.FAILED, error="second")
        record = self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()
        self.assertEqual(record.status, ApplicationStatus.FAILED)
        self.assertEqual(record.error_message, "second")

    # --- structural guarantees ---------------------------------------------------------
    def test_9_no_duplicate_rows_are_created(self) -> None:
        _, job = self._setup()
        for status in (
            ApplicationStatus.DRY_RUN_READY,
            ApplicationStatus.FAILED,
            ApplicationStatus.APPLIED,
            ApplicationStatus.SKIPPED,
        ):
            self._attempt(job, status)
        rows = self.db.query(ApplicationRecord).filter_by(job_id=job.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, ApplicationStatus.APPLIED)
        self.assertEqual(rows[0].attempt_count, 4)

    def test_11_portal_isolation_unchanged_by_precedence(self) -> None:
        a_li = self._resume(RESUME_A_TEXT, "linkedin")
        a_di = self._resume(RESUME_A_TEXT, "dice")
        li_job = self._job(a_li, "linkedin", "https://li/p")
        di_job = self._job(a_di, "dice", "https://di/p")
        self._attempt(li_job, ApplicationStatus.DRY_RUN_READY)
        self._attempt(di_job, ApplicationStatus.DRY_RUN_READY)
        # A LinkedIn failure must not touch the Dice record (separate job, separate row).
        self._attempt(li_job, ApplicationStatus.FAILED)
        self.assertEqual(len(self._rows(a_li, "linkedin")), 1)
        self.assertEqual(len(self._rows(a_di, "dice")), 1)

    def test_12_fingerprint_family_history_unchanged_by_precedence(self) -> None:
        a1 = self._resume(RESUME_A_TEXT, "linkedin")
        job = self._job(a1, "linkedin", "https://li/fam")
        self._attempt(job, ApplicationStatus.DRY_RUN_READY)
        self._attempt(job, ApplicationStatus.FAILED)
        a2 = self._resume(RESUME_A_TEXT, "linkedin")  # re-upload -> new id, same fingerprint
        self.assertEqual(len(self._rows(a2)), 1)


class StaleAttemptFieldCleanup(_HistoryTest):
    """A superseding attempt must not inherit the previous attempt's evidence/reasoning.
    A protected success must not be touched at all."""

    #: A full "rich" attempt: every attempt-specific column populated.
    RICH = dict(
        current_step="review_page_reached",
        evidence_path="outputs/old_run",
        resume_filename_used="old_resume.pdf",
        resume_upload_status="uploaded",
        resume_upload_evidence_path="outputs/old_run/upload",
        reasoning_type="llm",
        reasoning_summary="old reasoning",
        confidence_score="0.82",
        evidence_sources=[{"source": "old"}],
        error_message="old error",
        run_log=[{"event": "old"}],
    )

    def _attempt(self, job, status, **fields) -> ApplicationRecord:
        record = _record_application_outcome(
            self.db, job, portal=job.portal, status=status, dry_run=True, **fields
        )
        self.db.add(record)
        self.db.flush()
        return record

    def _row(self, job) -> ApplicationRecord:
        return self.db.query(ApplicationRecord).filter_by(job_id=job.id).one()

    def _job_for(self, url="https://li/stale"):
        resume = self._resume(RESUME_A_TEXT, "linkedin")
        return resume, self._job(resume, "linkedin", url)

    def _assert_no_stale_evidence(self, record) -> None:
        self.assertIsNone(record.evidence_path)
        self.assertIsNone(record.reasoning_type)
        self.assertIsNone(record.reasoning_summary)
        self.assertIsNone(record.confidence_score)
        self.assertIsNone(record.resume_filename_used)
        self.assertIsNone(record.resume_upload_status)
        self.assertIsNone(record.resume_upload_evidence_path)
        self.assertEqual(record.evidence_sources, [])

    # --- the reported bug --------------------------------------------------------------
    def test_1_failed_then_bare_failed_clears_stale_evidence(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
        # Attempt 2: the exception path supplies ONLY these fields.
        self._attempt(
            job,
            ApplicationStatus.FAILED,
            current_step="automation_failed",
            error_message="new browser timeout",
            run_log=[],
        )
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.FAILED)
        self._assert_no_stale_evidence(record)          # nothing from attempt 1 survives
        self.assertEqual(record.current_step, "automation_failed")
        self.assertEqual(record.error_message, "new browser timeout")
        self.assertEqual(record.run_log, [])
        self.assertEqual(record.attempt_count, 2)

    def test_2_skipped_metadata_cleared_when_replaced_by_failed(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.SKIPPED, **self.RICH)
        self._attempt(job, ApplicationStatus.FAILED, current_step="automation_failed",
                      error_message="crashed", run_log=[])
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.FAILED)
        self._assert_no_stale_evidence(record)

    def test_3_needs_review_to_unsupported_form_drops_old_evidence(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.NEEDS_REVIEW, **self.RICH)
        self._attempt(job, ApplicationStatus.UNSUPPORTED_FORM, current_step="unsupported_form")
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.UNSUPPORTED_FORM)
        self._assert_no_stale_evidence(record)
        self.assertIsNone(record.error_message)
        self.assertEqual(record.run_log, [])

    def test_4_new_attempt_with_full_evidence_is_stored(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
        self._attempt(
            job, ApplicationStatus.FAILED,
            current_step="new_step", evidence_path="outputs/new_run",
            reasoning_summary="new reasoning", confidence_score="0.44",
            evidence_sources=[{"source": "new"}], error_message="new error",
            run_log=[{"event": "new"}],
        )
        record = self._row(job)
        self.assertEqual(record.evidence_path, "outputs/new_run")
        self.assertEqual(record.reasoning_summary, "new reasoning")
        self.assertEqual(record.confidence_score, "0.44")
        self.assertEqual(record.evidence_sources, [{"source": "new"}])
        self.assertEqual(record.run_log, [{"event": "new"}])
        # Not supplied by the new attempt -> neutral, not inherited.
        self.assertIsNone(record.reasoning_type)
        self.assertIsNone(record.resume_upload_status)

    # --- upgrades write the full successful result --------------------------------------
    def test_5_failed_upgrades_to_dry_run_ready_with_success_fields(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
        self._attempt(job, ApplicationStatus.DRY_RUN_READY, current_step="dry_run_complete",
                      evidence_path="outputs/success", run_log=[{"event": "ok"}])
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(record.evidence_path, "outputs/success")
        self.assertIsNone(record.error_message)      # the old failure's error is gone
        self.assertIsNone(record.reasoning_summary)  # old reasoning did not leak in

    def test_6_failed_upgrades_to_applied_with_success_fields(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
        self._attempt(job, ApplicationStatus.APPLIED, current_step="confirmation_received",
                      evidence_path="outputs/applied")
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.APPLIED)
        self.assertEqual(record.evidence_path, "outputs/applied")
        self.assertIsNone(record.error_message)

    # --- successes stay fully protected --------------------------------------------------
    def test_7_dry_run_ready_then_failed_preserves_success_and_its_evidence(self) -> None:
        resume, job = self._job_for()
        self._attempt(job, ApplicationStatus.DRY_RUN_READY, current_step="dry_run_complete",
                      evidence_path="outputs/success", reasoning_summary="good reasoning",
                      confidence_score="0.91", evidence_sources=[{"source": "ok"}])
        self._attempt(job, ApplicationStatus.FAILED, current_step="automation_failed",
                      error_message="timeout", run_log=[])
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(record.evidence_path, "outputs/success")   # NOT cleared
        self.assertEqual(record.reasoning_summary, "good reasoning")
        self.assertEqual(record.confidence_score, "0.91")
        self.assertEqual(record.evidence_sources, [{"source": "ok"}])
        self.assertEqual(record.current_step, "dry_run_complete")
        self.assertEqual(record.attempt_count, 2)
        self.assertEqual(len(list_applications(resume_id=resume.id, portal="linkedin", db=self.db)), 1)

    def test_8_applied_then_failed_preserves_success_and_its_evidence(self) -> None:
        resume, job = self._job_for()
        self._attempt(job, ApplicationStatus.APPLIED, current_step="confirmation_received",
                      evidence_path="outputs/applied")
        self._attempt(job, ApplicationStatus.FAILED, current_step="automation_failed",
                      error_message="timeout")
        record = self._row(job)
        self.assertEqual(record.status, ApplicationStatus.APPLIED)
        self.assertEqual(record.evidence_path, "outputs/applied")
        self.assertIsNone(record.error_message)
        self.assertEqual(record.attempt_count, 2)
        self.assertEqual(len(list_applications(resume_id=resume.id, portal="linkedin", db=self.db)), 1)

    # --- structural ----------------------------------------------------------------------
    def test_9_no_duplicate_rows(self) -> None:
        _, job = self._job_for()
        for status in (ApplicationStatus.FAILED, ApplicationStatus.SKIPPED,
                       ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.FAILED):
            self._attempt(job, status, **self.RICH)
        rows = self.db.query(ApplicationRecord).filter_by(job_id=job.id).all()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].attempt_count, 4)

    def test_10_neutral_values_are_type_correct(self) -> None:
        _, job = self._job_for()
        self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
        self._attempt(job, ApplicationStatus.FAILED, error_message="only this")
        record = self._row(job)
        # JSONB list columns stay lists (never None); nullable scalars become None.
        self.assertIsInstance(record.evidence_sources, list)
        self.assertIsInstance(record.run_log, list)
        self.assertEqual(record.evidence_sources, [])
        self.assertEqual(record.run_log, [])
        self.assertIsNone(record.evidence_path)
        self.assertIsNone(record.confidence_score)

    def test_10b_reset_lists_are_not_shared_between_records(self) -> None:
        _, job_a = self._job_for("https://li/stale-a")
        _, job_b = self._job_for("https://li/stale-b")
        for job in (job_a, job_b):
            self._attempt(job, ApplicationStatus.FAILED, **self.RICH)
            self._attempt(job, ApplicationStatus.FAILED, error_message="reset")
        rec_a, rec_b = self._row(job_a), self._row(job_b)
        rec_a.run_log.append({"event": "mutated"})
        self.assertEqual(rec_b.run_log, [])   # no shared mutable default
        self.assertIsNot(rec_a.evidence_sources, rec_b.evidence_sources)

    def test_attempt_specific_fields_all_exist_on_the_model(self) -> None:
        # Guards against the reset list drifting away from the actual columns.
        columns = set(ApplicationRecord.__table__.columns.keys())
        self.assertTrue(ATTEMPT_SPECIFIC_FIELDS <= columns, ATTEMPT_SPECIFIC_FIELDS - columns)
        # Identity/audit columns must never be in the reset list.
        for protected in ("id", "job_id", "portal", "status", "attempt_count",
                          "application_date", "dry_run"):
            self.assertNotIn(protected, ATTEMPT_SPECIFIC_FIELDS)

    def test_12_portal_isolation_and_family_unchanged(self) -> None:
        a_li = self._resume(RESUME_A_TEXT, "linkedin")
        a_di = self._resume(RESUME_A_TEXT, "dice")
        li_job = self._job(a_li, "linkedin", "https://li/iso")
        di_job = self._job(a_di, "dice", "https://di/iso")
        self._attempt(li_job, ApplicationStatus.APPLIED, evidence_path="outputs/li")
        self._attempt(di_job, ApplicationStatus.DRY_RUN_READY, evidence_path="outputs/di")
        self._attempt(li_job, ApplicationStatus.FAILED, error_message="boom")

        a2 = self._resume(RESUME_A_TEXT, "linkedin")  # re-upload: same fingerprint family
        li = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        di = list_applications(resume_id=a_di.id, portal="dice", db=self.db)
        self.assertEqual(len(li), 1)
        self.assertEqual(len(di), 1)
        self.assertEqual(li[0]["evidence_path"], "outputs/li")
        self.assertEqual(di[0]["evidence_path"], "outputs/di")


class HistoryQueryEfficiency(_HistoryTest):
    """GET /applications must not issue one SELECT per history row (N+1). The job relationship
    is eager-loaded from the join the filter already needs."""

    def _count_sql(self, fn):
        """Run `fn`, capturing every SQL statement actually executed on this connection."""
        statements: list[str] = []

        def before(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement)

        event.listen(self.connection, "before_cursor_execute", before)
        try:
            result = fn()
        finally:
            event.remove(self.connection, "before_cursor_execute", before)
        return result, statements

    def _seed(self, raw_text, count, offset) -> int:
        resume = self._resume(raw_text, "linkedin")
        self._run(resume, "linkedin", count, ApplicationStatus.DRY_RUN_READY, offset=offset)
        return resume.id

    def _history(self, resume_id):
        return lambda: list_applications(resume_id=resume_id, portal="linkedin", db=self.db)

    def test_10_11_query_count_does_not_grow_with_history_size(self) -> None:
        small_id = self._seed(RESUME_A_TEXT, 1, 0)      # 1 history row
        big_id = self._seed(RESUME_B_TEXT, 20, 1000)    # 20 history rows
        self.db.flush()

        # Expire everything so the relationship genuinely has to be loaded from the database
        # (otherwise the identity map would hide an N+1 and make this test vacuous).
        self.db.expire_all()
        rows_small, sql_small = self._count_sql(self._history(small_id))
        self.db.expire_all()
        rows_big, sql_big = self._count_sql(self._history(big_id))

        self.assertEqual(len(rows_small), 1)
        self.assertEqual(len(rows_big), 20)
        # Constant, not linear: 20x the rows must cost the same number of statements as 1 row.
        self.assertEqual(len(sql_big), len(sql_small))
        self.assertLessEqual(len(sql_big), 4)

    def test_12_no_lazy_load_while_serializing_job_fields(self) -> None:
        resume_id = self._seed(RESUME_A_TEXT, 5, 2000)
        self.db.flush()
        self.db.expire_all()

        rows, statements = self._count_sql(self._history(resume_id))

        # A lazy load would be a SELECT against job_opportunities alone (no join to
        # application_records). There must be none: the job came back with the main query.
        lazy_loads = [
            sql for sql in statements
            if "job_opportunities" in sql and "application_records" not in sql
        ]
        self.assertEqual(lazy_loads, [])
        # And the job fields are genuinely populated in the response.
        self.assertEqual(len(rows), 5)
        self.assertTrue(all(r["job_title"] == "Analyst" for r in rows))
        self.assertTrue(all(r["company"] == "Acme" for r in rows))
        self.assertTrue(all(r["job_url"] for r in rows))
        self.assertTrue(all(r["job_portal"] == "linkedin" for r in rows))

    def test_2_twenty_rows_return_fully_populated_response_values(self) -> None:
        # Eager loading must not change what the response contains: every one of the 20 rows
        # carries its own correct job fields (a broken eager load would repeat or blank them).
        resume = self._resume(RESUME_A_TEXT, "linkedin")
        jobs = [self._job(resume, "linkedin", f"https://li/bulk/{i}") for i in range(20)]
        for job in jobs:
            self._app(job, ApplicationStatus.APPLIED)
        self.db.flush()
        self.db.expire_all()

        rows = list_applications(resume_id=resume.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 20)
        # Each response row maps to a distinct job, with that job's own url.
        self.assertEqual({r["job_id"] for r in rows}, {j.id for j in jobs})
        by_job = {r["job_id"]: r for r in rows}
        for job in jobs:
            row = by_job[job.id]
            self.assertEqual(row["job_url"], job.job_url)
            self.assertEqual(row["job_title"], "Analyst")
            self.assertEqual(row["company"], "Acme")
            self.assertEqual(row["job_portal"], "linkedin")
            self.assertEqual(row["status"], ApplicationStatus.APPLIED)

    def test_1_response_values_are_unchanged(self) -> None:
        resume = self._resume(RESUME_A_TEXT, "linkedin")
        job = self._job(resume, "linkedin", "https://li/resp")
        record = self._app(job, ApplicationStatus.APPLIED, current_step="confirmation_received")
        self.db.flush()
        self.db.expire_all()

        rows = list_applications(resume_id=resume.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], record.id)
        self.assertEqual(row["job_id"], job.id)
        self.assertEqual(row["portal"], "linkedin")
        self.assertEqual(row["status"], ApplicationStatus.APPLIED)
        self.assertEqual(row["current_step"], "confirmation_received")
        self.assertEqual(row["job_title"], "Analyst")
        self.assertEqual(row["company"], "Acme")
        self.assertEqual(row["job_url"], "https://li/resp")
        self.assertEqual(row["job_portal"], "linkedin")

    def test_8_ordering_remains_newest_first(self) -> None:
        resume = self._resume(RESUME_A_TEXT, "linkedin")
        first = self._app(self._job(resume, "linkedin", "https://li/o1"), ApplicationStatus.APPLIED)
        second = self._app(self._job(resume, "linkedin", "https://li/o2"), ApplicationStatus.APPLIED)
        third = self._app(self._job(resume, "linkedin", "https://li/o3"), ApplicationStatus.APPLIED)
        self.db.flush()

        rows = list_applications(resume_id=resume.id, portal="linkedin", db=self.db)
        # Same application_date (same second) -> id desc breaks the tie: newest first.
        self.assertEqual([r["id"] for r in rows], [third.id, second.id, first.id])

    def test_3_4_portal_isolation_with_eager_loading(self) -> None:
        a_li = self._resume(RESUME_A_TEXT, "linkedin")
        a_di = self._resume(RESUME_A_TEXT, "dice")
        self._run(a_li, "linkedin", 3, ApplicationStatus.APPLIED)
        self._run(a_di, "dice", 2, ApplicationStatus.DRY_RUN_READY, offset=300)
        self.db.flush()
        self.db.expire_all()

        li = list_applications(resume_id=a_li.id, portal="linkedin", db=self.db)
        di = list_applications(resume_id=a_di.id, portal="dice", db=self.db)
        self.assertEqual(len(li), 3)
        self.assertEqual(len(di), 2)
        self.assertTrue(all(r["job_portal"] == "linkedin" for r in li))
        self.assertTrue(all(r["job_portal"] == "dice" for r in di))


if __name__ == "__main__":
    unittest.main()
