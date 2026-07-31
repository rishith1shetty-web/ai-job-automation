"""dry_run must survive every backend layer, for every apply entry point.

The Dice live-apply bug was traced by proving the flag at each layer rather than reading code.
These tests keep it proven: if any endpoint ever re-defaults `false` back to `true`, or
`_process_application` stops forwarding it to the portal adapter, one of these fails.

No browser and no real submission: the adapters are recording doubles.
"""

import unittest
from unittest.mock import patch
from uuid import uuid4

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _process_application, apply_selected_job, apply_top_jobs, set_active_portal
from app.models import (
    ApplicationRecord,
    ApplicationStatus,
    JobOpportunity,
    ResumeProfile,
)
from app.schemas import ApplyRequest, ApplySelectedRequest
from app.services.linkedin import AutomationResult
from app.services.resume_identity import compute_resume_fingerprint

RESUME_TEXT = "Jane Doe\nData Analyst"


class RecordingAdapter:
    """Records the dry_run it was handed. Never opens a browser, never submits."""

    seen: list[bool] = []

    def __init__(self, _settings=None) -> None:
        pass

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None, cancellation=None) -> AutomationResult:
        RecordingAdapter.seen.append(dry_run)
        # live mode reports a real submission; dry mode reports submit-ready
        if dry_run:
            return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [], "dice_dry_run_ready", None)
        return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)


class DryRunPropagationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        RecordingAdapter.seen = []
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
        self.job_id = self.job.id

    def tearDown(self) -> None:
        self.db.query(ApplicationRecord).filter(
            ApplicationRecord.job_id == self.job_id
        ).delete(synchronize_session=False)
        self.db.delete(self.db.get(JobOpportunity, self.job_id))
        self.db.delete(self.resume)
        set_active_portal(self.db, "linkedin")
        self.db.commit()
        self.db.close()

    # -- 5 + 6: _process_application -> Dice adapter -------------------------------------
    def test_5_process_application_passes_false_to_the_dice_adapter(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            record = _process_application(
                self.db, None, self.job, self.resume, None, False, capture_evidence=False,
            )
        self.assertEqual(RecordingAdapter.seen, [False])          # not re-defaulted to True
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_6_process_application_passes_true_to_the_dice_adapter(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            record = _process_application(
                self.db, None, self.job, self.resume, None, True, capture_evidence=False,
            )
        self.assertEqual(RecordingAdapter.seen, [True])
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)

    # -- 3: apply-selected ----------------------------------------------------------------
    def test_3_apply_selected_preserves_false(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id, dry_run=False),
                self.db,
            )
        self.assertEqual(RecordingAdapter.seen, [False])

    def test_apply_selected_preserves_true(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id, dry_run=True),
                self.db,
            )
        self.assertEqual(RecordingAdapter.seen, [True])

    def test_apply_selected_omitting_dry_run_falls_back_to_the_configured_default(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id),
                self.db,
            )
        self.assertEqual(RecordingAdapter.seen, [Settings().auto_apply_dry_run])
        # Dry run now starts OFF, so an unspecified request performs a normal live application.
        # Pinned in tests/test_dry_run_default.py; asserted here as the value actually forwarded.
        self.assertFalse(Settings().auto_apply_dry_run, "dry run must default to OFF")

    # -- 4: the legacy synchronous apply-top ------------------------------------------------
    def test_4_apply_top_preserves_false(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_top_jobs(
                ApplyRequest(resume_id=self.resume.id, limit=5, dry_run=False, portal="dice"),
                self.db,
            )
        self.assertTrue(RecordingAdapter.seen, "apply-top applied to no jobs")
        self.assertEqual(set(RecordingAdapter.seen), {False})

    def test_apply_top_preserves_true(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_top_jobs(
                ApplyRequest(resume_id=self.resume.id, limit=5, dry_run=True, portal="dice"),
                self.db,
            )
        self.assertEqual(set(RecordingAdapter.seen), {True})

    # -- 11: LinkedIn is untouched ------------------------------------------------------------
    def test_11_linkedin_still_receives_the_same_flag(self) -> None:
        self.job.portal = "linkedin"
        self.resume.portal = "linkedin"
        set_active_portal(self.db, "linkedin")
        self.db.commit()

        automation = RecordingAdapter()
        _process_application(
            self.db, automation, self.job, self.resume, None, False, capture_evidence=False,
        )
        self.assertEqual(RecordingAdapter.seen, [False])   # routed to LinkedIn, flag intact


if __name__ == "__main__":
    unittest.main()
