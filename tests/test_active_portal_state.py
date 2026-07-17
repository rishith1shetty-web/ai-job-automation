import unittest
import tempfile
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from sqlalchemy import delete, func, select

from app.database import SessionLocal, init_db
from app.main import (
    _create_search_batch,
    apply_selected_job,
    connect_dice,
    connect_linkedin,
    get_active_portal,
    get_active_portal_status,
    set_active_portal,
    settings,
    upload_resume,
)
from app.models import (
    ApplicationRecord,
    CandidateEmploymentHistory,
    CandidateProfile,
    JobOpportunity,
    JobSearchBatch,
    PortalRuntimeState,
    ResumeProfile,
)
from app.schemas import ApplySelectedRequest, SearchRequest
from app.services.runtime_session import reset_runtime_session


class ActivePortalStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def tearDown(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")

    def test_only_one_active_portal_state_row_exists(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")
            set_active_portal(db, "dice")

            count = db.scalar(select(func.count()).select_from(PortalRuntimeState))

            self.assertEqual(count, 1)
            self.assertEqual(get_active_portal(db), "dice")

    def test_active_status_reports_session_and_never_deletes_a_saved_session(self) -> None:
        # GET /portal/active now reports the ACTIVE SESSION, not the persisted preference and
        # not "a storage-state file exists". A fresh session shows both portals disconnected
        # with no active portal, even though the DB still records a portal preference and a
        # saved storage-state file may exist. It is read-only: polling it must never delete a
        # saved portal session.
        fake_dice = _FakeDiceAdapter(connected=True)
        reset_runtime_session()
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")
        with patch("app.main.dice_adapter", return_value=fake_dice), patch(
            "app.main.LinkedInAutomation",
            _FakeLinkedInAutomation,
        ):
            status = get_active_portal_status()

        self.assertIsNone(status["active_portal"])
        self.assertFalse(status["linkedin_connected"])
        self.assertFalse(status["dice_connected"])
        self.assertEqual(fake_dice.disconnect_calls, 0)

    def test_connecting_dice_disconnects_linkedin_and_sets_active_portal(self) -> None:
        fake_dice = _FakeDiceAdapter(connected=True)
        _FakeLinkedInAutomation.disconnect_calls = 0

        with patch("app.main.dice_adapter", return_value=fake_dice), patch(
            "app.main.LinkedInAutomation",
            _FakeLinkedInAutomation,
        ):
            result = connect_dice()

        with SessionLocal() as db:
            active = get_active_portal(db)

        self.assertEqual(result["status"], "connected")
        self.assertEqual(active, "dice")
        self.assertEqual(_FakeLinkedInAutomation.disconnect_calls, 1)

    def test_connecting_linkedin_disconnects_dice_and_sets_active_portal(self) -> None:
        fake_dice = _FakeDiceAdapter(connected=True)
        _FakeLinkedInAutomation.connect_calls = 0

        with patch("app.main.dice_adapter", return_value=fake_dice), patch(
            "app.main.LinkedInAutomation",
            _FakeLinkedInAutomation,
        ):
            result = connect_linkedin()

        with SessionLocal() as db:
            active = get_active_portal(db)

        self.assertEqual(result["status"], "connected")
        self.assertEqual(active, "linkedin")
        self.assertEqual(fake_dice.disconnect_calls, 1)
        self.assertEqual(_FakeLinkedInAutomation.connect_calls, 1)

    def test_find_matches_rejects_inactive_portal_resume(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "dice")
            resume = _resume(portal="linkedin")
            db.add(resume)
            db.commit()
            db.refresh(resume)
            try:
                with self.assertRaises(HTTPException) as error:
                    _create_search_batch(SearchRequest(resume_id=resume.id, location="Remote"), db)
                self.assertEqual(error.exception.status_code, 409)
                self.assertIn("condition=resume_portal_mismatch", error.exception.detail)
                self.assertIn("Analyze/upload the resume again while Dice is the active portal", error.exception.detail)
            finally:
                db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                db.delete(resume)
                db.commit()

    def test_find_matches_conflict_logs_resume_portal_mismatch_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(settings, "find_matches_batch_debug_dir", Path(temp_dir)):
                with SessionLocal() as db:
                    set_active_portal(db, "dice")
                    resume = _resume(portal="linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with self.assertRaises(HTTPException):
                            _create_search_batch(SearchRequest(resume_id=resume.id, location="Remote"), db)

                        files = list(Path(temp_dir).glob("*/search_conflict/search_conflict.json"))
                        self.assertEqual(len(files), 1)
                        payload = json.loads(files[0].read_text(encoding="utf-8"))
                        self.assertEqual(payload["condition"], "resume_portal_mismatch")
                        self.assertEqual(payload["active_portal"], "dice")
                        self.assertEqual(payload["resolved_request_portal"], "dice")
                        self.assertEqual(payload["resume_portal"], "linkedin")
                        self.assertIn("dice_connected", payload["session_status"])
                    finally:
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()

    def test_find_matches_conflict_reports_unverified_dice_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(settings, "find_matches_batch_debug_dir", Path(temp_dir)), patch(
                "app.main.dice_adapter",
                return_value=_FakeDiceAdapter(connected=False),
            ):
                with SessionLocal() as db:
                    set_active_portal(db, "dice")
                    resume = _resume(portal="dice")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with self.assertRaises(HTTPException) as error:
                            _create_search_batch(SearchRequest(resume_id=resume.id, location="Remote"), db)

                        self.assertEqual(error.exception.status_code, 409)
                        self.assertIn("condition=dice_session_missing_or_unverified", error.exception.detail)
                        self.assertIn("saved Dice session is missing or not verified", error.exception.detail)
                        files = list(Path(temp_dir).glob("*/search_conflict/search_conflict.json"))
                        payload = json.loads(files[0].read_text(encoding="utf-8"))
                        self.assertEqual(payload["condition"], "dice_session_missing_or_unverified")
                        self.assertEqual(payload["session_status"]["dice_connected"], False)
                    finally:
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()

    def test_apply_selected_rejects_inactive_portal_job(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")
            resume = _resume(portal="linkedin")
            db.add(resume)
            db.commit()
            db.refresh(resume)
            job = JobOpportunity(
                resume_id=resume.id,
                portal="dice",
                title="Data Analyst",
                company="Dice Co",
                location="Remote",
                job_url="https://www.dice.com/job-detail/00000007-2222-4333-8444-555555555555",
                description="SQL Python",
                easy_apply=True,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            try:
                with self.assertRaises(HTTPException) as error:
                    apply_selected_job(
                        ApplySelectedRequest(resume_id=resume.id, job_id=job.id, dry_run=True),
                        db,
                    )
                self.assertEqual(error.exception.status_code, 409)
                self.assertIn("Dice", error.exception.detail)
            finally:
                db.execute(delete(ApplicationRecord).where(ApplicationRecord.job_id == job.id))
                db.delete(job)
                db.delete(resume)
                db.commit()

    def test_resume_analysis_belongs_to_active_portal(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "dice")
            upload = UploadFile(
                filename="active-portal-resume.txt",
                file=BytesIO(b"Data analyst with Python SQL Excel dashboards"),
            )
            resume = upload_resume(upload, db)
            try:
                self.assertEqual(resume.portal, "dice")
                candidate = db.scalar(
                    select(CandidateProfile)
                    .where(CandidateProfile.portal == "dice")
                    .order_by(CandidateProfile.id.desc())
                    .limit(1)
                )
                self.assertIsNotNone(candidate)
            finally:
                db.execute(delete(CandidateEmploymentHistory).where(CandidateEmploymentHistory.resume_id == resume.id))
                db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                db.delete(resume)
                db.commit()


def _resume(portal: str) -> ResumeProfile:
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


class _FakeLinkedInAutomation:
    connect_calls = 0
    disconnect_calls = 0

    def __init__(self, _settings) -> None:
        pass

    def connect_account(self) -> None:
        self.__class__.connect_calls += 1

    def disconnect_account(self) -> None:
        self.__class__.disconnect_calls += 1

    def has_authenticated_session(self) -> bool:
        return True


class _FakeDiceAdapter:
    def __init__(self, connected: bool = False) -> None:
        self.connected = connected
        self.disconnect_calls = 0

    def connect(self) -> dict:
        return {"status": "connected"} if self.connected else {"status": "not_connected"}

    def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


if __name__ == "__main__":
    unittest.main()
