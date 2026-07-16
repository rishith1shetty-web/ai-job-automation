"""Fresh-session dashboard startup.

Every backend start must begin with a clean ACTIVE session — no connected portal, no active
portal, no active resume, no matched jobs, no selected batch, zeroed counters, idle run
status — while every piece of PERSISTENT data (resumes, analysis, candidate profile,
employment history, screening answers, search batches, matched jobs, application history,
successful live APPLIED jobs) stays in the database and saved portal storage-state files stay
on disk.

Previously the dashboard restored the last session because:
  * /portal/active reported connected from storage-state FILE EXISTENCE (and deleted the
    inactive portal's saved session as a side effect of a GET),
  * /resumes/latest returned the newest resume row in the DB as "the active resume",
  * /dashboard/summary adopted the newest search batch when none was given,
  * the React batch bar auto-selected batches[0].

The user reaches their data again by explicitly connecting a portal and re-uploading +
analysing the resume, which reattaches to the existing fingerprint family.
"""

import unittest
from unittest.mock import patch
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.database import SessionLocal, init_db
from app.main import (
    _empty_dashboard_summary,
    dashboard_summary,
    dice_status,
    get_active_portal_status,
    latest_resume,
    linkedin_status,
    list_search_batches,
    set_active_portal,
    startup,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
)
from app.services.resume_identity import compute_resume_fingerprint
from app.services.runtime_session import (
    RUN_STATUS_IDLE,
    get_runtime_session,
    reset_runtime_session,
)

from tests.test_resume_identity_history import RESUME_A_TEXT, _DBTest


class FreshSessionStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        reset_runtime_session()

    def tearDown(self) -> None:
        reset_runtime_session()

    # 1. After backend startup, no active portal exists.
    def test_startup_leaves_no_active_portal(self) -> None:
        # Simulate a previous session having connected Dice, then a restart.
        get_runtime_session().mark_connected("dice")
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_active_portal_status()["active_portal"])

    # 2. LinkedIn and Dice both report disconnected.
    def test_both_portals_report_disconnected_after_startup(self) -> None:
        with patch("app.main.init_db"):
            startup()
        status = get_active_portal_status()
        self.assertFalse(status["linkedin_connected"])
        self.assertFalse(status["dice_connected"])
        self.assertFalse(linkedin_status()["connected"])
        self.assertFalse(dice_status()["connected"])

    def test_persisted_portal_preference_does_not_reconnect(self) -> None:
        # The DB still remembers a portal preference; that must not mark it connected.
        with SessionLocal() as db:
            set_active_portal(db, "dice")
        with patch("app.main.init_db"):
            startup()
        status = get_active_portal_status()
        self.assertIsNone(status["active_portal"])
        self.assertFalse(status["dice_connected"])

    # 3. No active resume is selected.
    def test_no_active_resume_after_startup(self) -> None:
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_runtime_session().active_resume_id)
        with SessionLocal() as db:
            with self.assertRaises(HTTPException) as ctx:
                latest_resume(db=db)
        self.assertEqual(ctx.exception.status_code, 404)

    def test_latest_resume_never_returns_newest_db_row(self) -> None:
        # THE regression: rows exist in the DB, but none is active in a fresh session.
        with SessionLocal() as db:
            existing = db.scalar(select(ResumeProfile).limit(1))
            if existing is None:
                self.skipTest("no resume rows available in the local database")
            reset_runtime_session()
            with self.assertRaises(HTTPException):
                latest_resume(db=db)

    def test_analysing_a_resume_activates_it_for_the_session(self) -> None:
        session = get_runtime_session()
        session.activate_resume(4242)
        self.assertEqual(session.active_resume_id, 4242)
        # ...and re-analysing clears any previous session's batch/matches so old Find
        # Matches results never reappear.
        session.active_search_batch_id = "old-batch"
        session.current_matches = [{"id": 1}]
        session.activate_resume(4243)
        self.assertIsNone(session.active_search_batch_id)
        self.assertEqual(session.current_matches, [])

    # 4-6. Persistent data survives: resumes, application history, search batches.
    def test_historical_data_is_not_deleted_by_startup(self) -> None:
        with SessionLocal() as db:
            before = (
                len(list(db.scalars(select(ResumeProfile)).all())),
                len(list(db.scalars(select(ApplicationRecord)).all())),
                len(list(db.scalars(select(JobSearchBatch)).all())),
                len(list(db.scalars(select(JobOpportunity)).all())),
            )
        with patch("app.main.init_db"):
            startup()
        with SessionLocal() as db:
            after = (
                len(list(db.scalars(select(ResumeProfile)).all())),
                len(list(db.scalars(select(ApplicationRecord)).all())),
                len(list(db.scalars(select(JobSearchBatch)).all())),
                len(list(db.scalars(select(JobOpportunity)).all())),
            )
        self.assertEqual(before, after)

    # 7. Previous matched jobs are not automatically displayed.
    def test_summary_is_zeroed_without_an_active_resume_or_batch(self) -> None:
        with SessionLocal() as db:
            self.assertEqual(dashboard_summary(portal="dice", db=db), _empty_dashboard_summary())
            # A resume with no explicitly selected batch must NOT adopt the newest batch.
            self.assertEqual(
                dashboard_summary(resume_id=1, portal="dice", db=db),
                _empty_dashboard_summary(),
            )

    def test_no_batch_is_active_after_startup(self) -> None:
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_runtime_session().active_search_batch_id)

    def test_counters_and_run_status_start_clean(self) -> None:
        session = get_runtime_session()
        session.current_progress = {"total": 9, "completed": 9}
        session.current_run_status = "running"
        session.current_run_error = "boom"
        session.current_apply_queue = [{"job": 1}]
        with patch("app.main.init_db"):
            startup()
        session = get_runtime_session()
        self.assertEqual(session.current_run_status, RUN_STATUS_IDLE)
        self.assertIsNone(session.current_run_error)
        self.assertEqual(session.current_apply_queue, [])
        self.assertEqual(session.current_matches, [])
        self.assertTrue(all(v == 0 for v in session.current_progress.values()))

    # 8. Restarting the backend resets runtime state again.
    def test_restart_resets_again(self) -> None:
        session = get_runtime_session()
        session.mark_connected("dice")
        session.activate_resume(77)
        session.active_search_batch_id = "batch-1"
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_runtime_session().active_portal)
        self.assertIsNone(get_runtime_session().active_resume_id)
        # ...and a second restart is just as clean.
        get_runtime_session().mark_connected("linkedin")
        with patch("app.main.init_db"):
            startup()
        self.assertIsNone(get_runtime_session().active_portal)
        self.assertFalse(get_runtime_session().linkedin_connected)

    # 9. Saved portal storage-state files are not deleted.
    def test_startup_and_status_never_delete_portal_storage_state(self) -> None:
        calls = {"dice_disconnect": 0, "linkedin_disconnect": 0}

        class _Dice:
            def disconnect(self):
                calls["dice_disconnect"] += 1

            def is_connected(self):
                return True

        class _LinkedIn:
            def __init__(self, *_a, **_k):
                pass

            def disconnect_account(self):
                calls["linkedin_disconnect"] += 1

            def has_authenticated_session(self):
                return True

        with patch("app.main.dice_adapter", return_value=_Dice()), patch(
            "app.main.LinkedInAutomation", _LinkedIn
        ):
            with patch("app.main.init_db"):
                startup()
            get_active_portal_status()
            linkedin_status()
            dice_status()

        self.assertEqual(calls["dice_disconnect"], 0)
        self.assertEqual(calls["linkedin_disconnect"], 0)

    # 10. Clicking Connect may reuse an existing valid saved session.
    def test_explicit_connect_marks_session_connected(self) -> None:
        session = get_runtime_session()
        self.assertFalse(session.dice_connected)
        session.mark_connected("dice")  # what /dice/connect does after a verified login
        self.assertTrue(session.dice_connected)
        self.assertEqual(session.active_portal, "dice")
        self.assertTrue(dice_status()["connected"])
        # Single active portal: connecting one clears the other.
        session.mark_connected("linkedin")
        self.assertFalse(session.dice_connected)
        self.assertTrue(session.linkedin_connected)
        self.assertEqual(session.active_portal, "linkedin")

    def test_disconnect_clears_only_that_portal(self) -> None:
        session = get_runtime_session()
        session.mark_connected("dice")
        session.mark_disconnected("dice")
        self.assertFalse(session.dice_connected)
        self.assertIsNone(session.active_portal)


class SessionClearIsRuntimeOnlyTests(_DBTest):
    """Clearing the session must only drop the RUNTIME pointers -- never a database row.

    activate_resume()/reset() null out active_search_batch_id and empty current_matches /
    current_apply_queue. Those are in-memory pointers to what the dashboard is looking at.
    The search batches, matched jobs and application history they referred to are permanent
    records and must still be in the database, still complete, and still reachable.
    """

    def _seed(self):
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        batch = JobSearchBatch(
            id=str(uuid4()),
            resume_id=resume.id,
            search_query="Data Analyst",
            location="Remote",
            portal="dice",
        )
        self.db.add(batch)
        self.db.flush()
        job = self._job(resume, "dice", "https://www.dice.com/job-detail/guid-keep")
        job.search_batch_id = batch.id
        self.db.flush()
        record = self._app(job, ApplicationStatus.APPLIED)
        return resume, batch, job, record

    def _counts(self) -> tuple[int, int, int, int]:
        return (
            self.db.scalar(select(func.count()).select_from(ResumeProfile)),
            self.db.scalar(select(func.count()).select_from(JobSearchBatch)),
            self.db.scalar(select(func.count()).select_from(JobOpportunity)),
            self.db.scalar(select(func.count()).select_from(ApplicationRecord)),
        )

    def test_activate_resume_clears_only_runtime_pointers(self) -> None:
        resume, batch, job, record = self._seed()
        before = self._counts()

        session = get_runtime_session()
        session.activate_resume(resume.id)
        session.active_search_batch_id = batch.id
        session.current_matches = [{"job_id": job.id}]
        session.current_apply_queue = [{"job_id": job.id}]

        # Re-analysing the resume clears the runtime batch/matches...
        session.activate_resume(resume.id)
        self.assertIsNone(session.active_search_batch_id)
        self.assertEqual(session.current_matches, [])
        self.assertEqual(session.current_apply_queue, [])

        # ...and deletes nothing.
        self.assertEqual(self._counts(), before)
        self.assertIsNotNone(self.db.get(JobSearchBatch, batch.id))
        self.assertIsNotNone(self.db.get(JobOpportunity, job.id))
        self.assertIsNotNone(self.db.get(ApplicationRecord, record.id))

    def test_reset_session_deletes_no_rows(self) -> None:
        resume, batch, job, record = self._seed()
        before = self._counts()

        session = get_runtime_session()
        session.activate_resume(resume.id)
        session.active_search_batch_id = batch.id
        reset_runtime_session()

        self.assertIsNone(get_runtime_session().active_search_batch_id)
        self.assertEqual(self._counts(), before)
        # The batch the session pointed at is intact and still fully readable.
        kept = self.db.get(JobSearchBatch, batch.id)
        self.assertEqual(kept.search_query, "Data Analyst")
        self.assertEqual(kept.resume_id, resume.id)
        # Its job and the APPLIED history survive with it.
        self.assertEqual(self.db.get(JobOpportunity, job.id).search_batch_id, batch.id)
        self.assertEqual(self.db.get(ApplicationRecord, record.id).status, ApplicationStatus.APPLIED)

    def test_cleared_batch_is_still_reachable_afterwards(self) -> None:
        # The batch remains explicitly openable through the historical endpoint.
        resume, batch, job, _ = self._seed()
        get_runtime_session().activate_resume(resume.id)  # clears the runtime batch pointer
        rows = list_search_batches(resume_id=resume.id, portal="dice", db=self.db)
        self.assertIn(batch.id, [b["id"] if isinstance(b, dict) else b.id for b in rows])


if __name__ == "__main__":
    unittest.main()
