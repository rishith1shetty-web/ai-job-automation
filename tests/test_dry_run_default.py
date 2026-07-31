"""Dry run starts OFF.

Only the DEFAULT changed. The toggle keeps its name and its meaning -- ON is still a dry run
that blocks final submission, OFF is still the existing normal/live behaviour -- and an explicit
`dry_run` on a request still wins over the default at every entry point. There is no new toggle
and no change to the safety flow; these tests pin where the value STARTS, and that everything
downstream of it is untouched.

Nothing here opens a browser or submits an application: the adapter is a recording double.
"""

import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import _create_apply_top_batch, apply_selected_job, set_active_portal, settings
from app.models import ApplicationRecord, ApplicationStatus, JobOpportunity, ResumeProfile
from app.schemas import ApplyRequest, ApplySelectedRequest
from app.services.linkedin import AutomationResult
from app.services.resume_identity import compute_resume_fingerprint

MATCHED_JOBS_PAGE = Path("frontend/src/pages/MatchedJobsPage.tsx").read_text(
    encoding="utf-8", errors="replace"
)
RESUME_TEXT = "Jordan Vale\nData Analyst"


class RecordingAdapter:
    """Records the dry_run it was handed. Never opens a browser, never submits."""

    seen: list[bool] = []

    def __init__(self, _settings=None) -> None:
        pass

    def apply_to_job(self, job, resume, candidate_profile, *, dry_run, capture_evidence,
                     screening_context=None, cancellation=None) -> AutomationResult:
        RecordingAdapter.seen.append(dry_run)
        if dry_run:
            return AutomationResult(ApplicationStatus.DRY_RUN_READY, None, [], "dice_dry_run_ready", None)
        return AutomationResult(ApplicationStatus.APPLIED, None, [], "dice_submitted", None)


class BackendDefaultTests(unittest.TestCase):
    """1-3: the value an apply run starts from, on startup and after a restart."""

    def test_the_code_default_is_off(self) -> None:
        # The declared field default, independent of any machine's .env file.
        self.assertIs(Settings.model_fields["auto_apply_dry_run"].default, False)

    def test_a_freshly_constructed_settings_object_resolves_to_off(self) -> None:
        # A restart builds Settings from scratch; it must come up OFF, not carry ON forward.
        self.assertFalse(Settings().auto_apply_dry_run)
        self.assertFalse(settings.auto_apply_dry_run)

    def test_the_shipped_env_template_does_not_switch_it_back_on(self) -> None:
        # A stale AUTO_APPLY_DRY_RUN=true in an env file would silently restore the old default.
        for name in (".env", ".env.example"):
            path = Path(name)
            if not path.is_file():
                continue
            with self.subTest(env_file=name):
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip().upper().startswith("AUTO_APPLY_DRY_RUN"):
                        self.assertEqual(line.split("=", 1)[1].strip().lower(), "false")

    def test_an_explicit_value_still_overrides_the_default(self) -> None:
        # The default is only a fallback; it must never mask what the user actually chose.
        self.assertTrue(Settings(auto_apply_dry_run=True).auto_apply_dry_run)


class FrontendDefaultTests(unittest.TestCase):
    """1-2: the toggle starts OFF, is not renamed, and is not persisted anywhere."""

    def test_the_toggle_state_is_initialized_off(self) -> None:
        self.assertIn("useState(false)", MATCHED_JOBS_PAGE.split("dryRun, setDryRun")[1][:60])

    def test_the_toggle_is_not_renamed_or_inverted(self) -> None:
        self.assertIn('label="Dry run"', MATCHED_JOBS_PAGE)
        self.assertIn("checked={dryRun}", MATCHED_JOBS_PAGE)
        self.assertIn("onCheckedChange={setDryRun}", MATCHED_JOBS_PAGE)
        # No second, inverted control was introduced alongside it.
        self.assertNotIn("liveApply", MATCHED_JOBS_PAGE)
        self.assertNotIn("!dryRun}", MATCHED_JOBS_PAGE)

    def test_the_toggle_is_not_persisted_across_restarts(self) -> None:
        # Plain component state: nothing can restore ON after an app start or a New Session.
        self.assertNotIn("localStorage", MATCHED_JOBS_PAGE)
        self.assertNotIn("sessionStorage", MATCHED_JOBS_PAGE)
        self.assertIn("dry_run: dryRun", MATCHED_JOBS_PAGE)


class RequestDefaultTests(unittest.TestCase):
    """4-7: what each apply entry point actually forwards."""

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

    def test_an_unspecified_apply_selected_runs_live(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            apply_selected_job(ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id), self.db)
        self.assertEqual(RecordingAdapter.seen, [False])

    def test_the_user_can_still_turn_dry_run_on_for_one_apply(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            record = apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id, dry_run=True), self.db
            )
        self.assertEqual(RecordingAdapter.seen, [True])
        # ON still stops at the submit-ready stage instead of submitting.
        self.assertEqual(record.status, ApplicationStatus.DRY_RUN_READY)

    def test_dry_run_off_still_reaches_the_live_submit_path(self) -> None:
        with patch("app.main.dice_adapter", RecordingAdapter):
            record = apply_selected_job(
                ApplySelectedRequest(resume_id=self.resume.id, job_id=self.job_id, dry_run=False), self.db
            )
        self.assertEqual(RecordingAdapter.seen, [False])
        self.assertEqual(record.status, ApplicationStatus.APPLIED)

    def test_a_batch_started_without_a_value_is_recorded_as_a_live_batch(self) -> None:
        # The batch stores the dry_run it will run with, read at start; nothing later re-defaults it.
        batch = _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=1, portal="dice"), self.db
        )
        try:
            self.assertFalse(batch.dry_run)
            self.assertIn("Queued Apply Top", batch.latest_status_message or "")
            self.assertNotIn("dry run", (batch.latest_status_message or "").lower())
        finally:
            from app.models import ApplyTopBatch

            self.db.delete(self.db.get(ApplyTopBatch, batch.id))
            self.db.commit()

    def test_a_batch_started_with_dry_run_on_keeps_it_on(self) -> None:
        batch = _create_apply_top_batch(
            ApplyRequest(resume_id=self.resume.id, limit=1, portal="dice", dry_run=True), self.db
        )
        try:
            self.assertTrue(batch.dry_run)
        finally:
            from app.models import ApplyTopBatch

            self.db.delete(self.db.get(ApplyTopBatch, batch.id))
            self.db.commit()


if __name__ == "__main__":
    unittest.main()
