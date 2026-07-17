"""POST /session/reset -- start a fresh dashboard session without restarting the backend.

The endpoint resets ONLY the in-memory RuntimeSession (the same clean state a process gets on
startup). It must never touch persistent data (resumes, batches, jobs, application history) or
saved portal storage-state files, and must not call LinkedIn/Dice automation or the explicit
disconnect functions (those delete storage-state files). It is idempotent.
"""

import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select

from app.database import SessionLocal, init_db
from app.main import (
    _create_search_batch,
    list_applications,
    list_jobs,
    list_search_batches,
    reset_session,
    set_active_portal,
    settings,
)
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobOpportunity,
    JobSearchBatch,
    ResumeProfile,
)
from app.services.runtime_session import get_runtime_session, reset_runtime_session


def _resume(portal: str = "dice") -> ResumeProfile:
    return ResumeProfile(
        portal=portal,
        filename=f"{portal}-resume.txt",
        stored_path=f"{portal}-resume.txt",
        raw_text="Python SQL data analyst",
        skills=["Python", "SQL"],
        experience=[],
        education=[],
        job_titles=["Data Analyst"],
        keywords=["analytics"],
        technologies=["Python", "SQL"],
        domain_expertise=["Data Analytics"],
        detected_role="Data Analyst",
        generated_search_keywords=["Data Analyst"],
        generated_search_query="Data Analyst",
        role_confidence=85,
    )


def _dirty_session() -> None:
    """Put the runtime session into a fully populated 'work in progress' state."""
    session = get_runtime_session()
    session.mark_connected("dice")
    session.active_resume_id = 4242
    session.active_search_batch_id = "batch-xyz"
    session.current_matches = [{"job_id": 1}, {"job_id": 2}]
    session.current_apply_queue = [{"job_id": 1}]
    session.current_progress = {"total": 10, "completed": 4, "applied": 2,
                                "failed": 1, "skipped": 1, "needs_review": 0}
    session.current_run_status = "running"
    session.current_run_error = "some stale error"


class SessionResetEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def tearDown(self) -> None:
        reset_runtime_session()

    # 1 + 4 + 5 + 6. A dirty session becomes fully clean.
    def test_reset_returns_a_clean_runtime_session(self) -> None:
        _dirty_session()
        payload = reset_session()
        self.assertIsNone(payload["active_portal"])
        self.assertIsNone(payload["active_resume_id"])
        self.assertIsNone(payload["active_search_batch_id"])
        self.assertEqual(payload["current_matches"], [])
        self.assertEqual(payload["current_apply_queue"], [])
        self.assertEqual(payload["current_run_status"], "idle")
        self.assertIsNone(payload["current_run_error"])
        self.assertEqual(
            payload["current_progress"],
            {"total": 0, "completed": 0, "applied": 0, "failed": 0, "skipped": 0, "needs_review": 0},
        )

    # 3. Both portal connected flags become false.
    def test_both_portal_flags_become_false(self) -> None:
        _dirty_session()
        payload = reset_session()
        self.assertFalse(payload["linkedin_connected"])
        self.assertFalse(payload["dice_connected"])

    # 2. Calling it twice is safe and returns the same clean state.
    def test_reset_is_idempotent(self) -> None:
        _dirty_session()
        first = reset_session()
        second = reset_session()
        self.assertEqual(first, second)
        self.assertIsNone(second["active_portal"])
        self.assertEqual(second["current_run_status"], "idle")

    # 7 + 9. No persistent rows are deleted or modified.
    def test_persistent_row_counts_are_unchanged(self) -> None:
        with SessionLocal() as db:
            resume = _resume()
            db.add(resume)
            db.commit()
            db.refresh(resume)
            batch = JobSearchBatch(id=str(uuid4()), resume_id=resume.id,
                                   search_query="Data Analyst", location="Remote", portal="dice")
            db.add(batch)
            db.commit()
            job = JobOpportunity(
                resume_id=resume.id, search_batch_id=batch.id, portal="dice",
                title="Data Analyst", company="Acme", location="Remote",
                job_url="https://www.dice.com/job-detail/00000001-2222-4333-8444-555555555555",
                description="SQL", easy_apply=True,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            applied = ApplicationRecord(
                job_id=job.id, portal="dice",
                status=ApplicationStatus.APPLIED, current_step="submitted",
            )
            db.add(applied)
            db.commit()

            before = {
                "resumes": db.scalar(select(func.count()).select_from(ResumeProfile)),
                "batches": db.scalar(select(func.count()).select_from(JobSearchBatch)),
                "jobs": db.scalar(select(func.count()).select_from(JobOpportunity)),
                "applications": db.scalar(select(func.count()).select_from(ApplicationRecord)),
            }
            try:
                _dirty_session()
                reset_session()
                after = {
                    "resumes": db.scalar(select(func.count()).select_from(ResumeProfile)),
                    "batches": db.scalar(select(func.count()).select_from(JobSearchBatch)),
                    "jobs": db.scalar(select(func.count()).select_from(JobOpportunity)),
                    "applications": db.scalar(select(func.count()).select_from(ApplicationRecord)),
                }
                self.assertEqual(before, after)

                # 8. Search batches and jobs remain readable through historical endpoints.
                set_active_portal(db, "dice")
                batches = list_search_batches(resume_id=resume.id, portal="dice", db=db)
                self.assertTrue(any(b["id"] == batch.id for b in batches))
                jobs = list_jobs(resume_id=resume.id, portal="dice", db=db)
                self.assertTrue(any(j.id == job.id for j in jobs))

                # 9. The APPLIED record is still present.
                applications = list_applications(db=db)
                self.assertTrue(
                    any(a["job_id"] == job.id and a["status"] == ApplicationStatus.APPLIED
                        for a in applications)
                )
            finally:
                db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id == job.id))
                db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                db.delete(resume)
                db.commit()

    # 10. LinkedIn and Dice storage-state files are left exactly as they were.
    def test_storage_state_files_are_untouched(self) -> None:
        paths = [settings.linkedin_storage_state, settings.dice_storage_state]
        created: list[Path] = []
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text('{"cookies": []}', encoding="utf-8")
                created.append(path)
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        try:
            _dirty_session()
            reset_session()
            for p in paths:
                self.assertTrue(p.exists(), f"{p} must not be unlinked")
                content, mtime = before[p]
                self.assertEqual(p.read_bytes(), content, f"{p} content changed")
                self.assertEqual(p.stat().st_mtime_ns, mtime, f"{p} was rewritten")
        finally:
            for p in created:
                p.unlink(missing_ok=True)

    # 11. No explicit portal disconnect / automation is invoked.
    def test_reset_calls_no_disconnect_or_automation(self) -> None:
        _dirty_session()
        with patch("app.main.LinkedInAutomation") as linkedin_cls, patch(
            "app.main.dice_adapter"
        ) as dice_factory, patch(
            "app.services.runtime_session.RuntimeSession.mark_disconnected"
        ) as mark_disconnected:
            reset_session()
            linkedin_cls.assert_not_called()
            dice_factory.assert_not_called()
            mark_disconnected.assert_not_called()

    # Reset does not require (or consult) the DB or an active portal to run.
    def test_reset_needs_no_active_portal(self) -> None:
        reset_runtime_session()
        payload = reset_session()  # no dirty state, no portal selected
        self.assertIsNone(payload["active_portal"])
        self.assertEqual(payload["current_run_status"], "idle")


if __name__ == "__main__":
    unittest.main()
