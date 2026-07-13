"""Resume-identity fingerprinting, portal-scoped application history, and successful-job
exclusion from Find Matches.

DB-backed tests run inside a transaction that is rolled back in tearDown, so they never
persist data to the local Postgres database (same engine the app uses).
"""

import unittest

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import engine, init_db
from app.main import list_applications
from app.models import ApplicationRecord, ApplicationStatus, JobOpportunity, ResumeProfile
from app.services.application_outcome import (
    SUCCESS_APPLICATION_STATUSES,
    is_successful_application,
)
from app.services.find_matches import FindMatchesEngine
from app.services.resume_identity import (
    backfill_resume_fingerprints,
    compute_resume_fingerprint,
    normalize_resume_text,
    resume_fingerprint_family_ids,
    successful_jobs_for_fingerprint,
)

RESUME_A_TEXT = "Jane Doe\nData Analyst\nSkills: SQL, Python, Tableau\nExperience: 5 years"


# --------------------------------------------------------------------------------------
# 1-5. Resume fingerprint (pure, no DB)
# --------------------------------------------------------------------------------------
class ResumeFingerprintTests(unittest.TestCase):
    def test_same_content_same_filename_same_fingerprint(self) -> None:
        # Filename is not part of the fingerprint at all — only parsed content is.
        self.assertEqual(compute_resume_fingerprint(RESUME_A_TEXT), compute_resume_fingerprint(RESUME_A_TEXT))

    def test_same_content_different_filename_same_fingerprint(self) -> None:
        # There is no filename input; identical content always hashes identically.
        a = compute_resume_fingerprint(RESUME_A_TEXT)
        b = compute_resume_fingerprint(str(RESUME_A_TEXT))
        self.assertEqual(a, b)

    def test_different_content_different_fingerprint(self) -> None:
        other = RESUME_A_TEXT.replace("Data Analyst", "Machine Learning Engineer")
        self.assertNotEqual(compute_resume_fingerprint(RESUME_A_TEXT), compute_resume_fingerprint(other))

    def test_whitespace_and_line_endings_only_same_fingerprint(self) -> None:
        noisy = "  Jane Doe\r\nData   Analyst\r\nSkills: SQL, Python, Tableau\n\n  Experience: 5 years   "
        clean = "Jane Doe\nData Analyst\nSkills: SQL, Python, Tableau\nExperience: 5 years"
        self.assertEqual(compute_resume_fingerprint(noisy), compute_resume_fingerprint(clean))

    def test_meaningful_change_changes_fingerprint(self) -> None:
        edited = RESUME_A_TEXT + "\nCertifications: AWS Solutions Architect"
        self.assertNotEqual(compute_resume_fingerprint(RESUME_A_TEXT), compute_resume_fingerprint(edited))

    def test_empty_or_unusable_text_has_no_fingerprint(self) -> None:
        self.assertIsNone(compute_resume_fingerprint(""))
        self.assertIsNone(compute_resume_fingerprint("   \n\r\n  "))
        self.assertIsNone(compute_resume_fingerprint(None))

    def test_case_insensitive_normalization(self) -> None:
        self.assertEqual(normalize_resume_text("Hello World"), normalize_resume_text("hello   world"))


# --------------------------------------------------------------------------------------
# DB-backed base: transaction rollback so nothing persists.
# --------------------------------------------------------------------------------------
class _DBTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        init_db()
        cls.engine = FindMatchesEngine(get_settings())

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
    def _resume(self, fingerprint, portal, raw_text=RESUME_A_TEXT) -> ResumeProfile:
        resume = ResumeProfile(
            portal=portal,
            filename="resume.pdf",
            stored_path="",
            raw_text=raw_text,
            resume_fingerprint=fingerprint,
        )
        self.db.add(resume)
        self.db.flush()
        return resume

    def _job(self, resume, portal, url, title="Analyst", company="Acme") -> JobOpportunity:
        job = JobOpportunity(
            resume_id=resume.id, portal=portal, title=title, company=company,
            location="Remote", job_url=url, description="",
        )
        self.db.add(job)
        self.db.flush()
        return job

    def _app(self, job, status) -> ApplicationRecord:
        record = ApplicationRecord(job_id=job.id, portal=job.portal, status=status)
        self.db.add(record)
        self.db.flush()
        return record

    def _keys_for(self, job) -> set:
        return {
            k
            for k in self.engine.duplicate_key(
                {"job_url": job.job_url, "title": job.title, "company": job.company, "location": job.location}
            )
            if k
        }


# --------------------------------------------------------------------------------------
# 6-13. Portal-specific application history across re-uploads
# --------------------------------------------------------------------------------------
class ApplicationHistoryTests(_DBTest):
    def _build_resume_a_family(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a1 = self._resume(fp, "linkedin")            # first upload (LinkedIn)
        a_dice = self._resume(fp, "dice")            # same resume used on Dice
        # 3 LinkedIn records (stand-in for the 57) and 2 Dice records (stand-in for 44).
        for i in range(3):
            self._app(self._job(a1, "linkedin", f"https://linkedin.com/jobs/view/{100 + i}"), ApplicationStatus.APPLIED)
        for i in range(2):
            self._app(self._job(a_dice, "dice", f"https://dice.com/job/{200 + i}"), ApplicationStatus.APPLIED)
        a2 = self._resume(fp, "linkedin")            # re-upload later -> NEW id, same fingerprint
        return a1, a_dice, a2, fp

    def test_reupload_shares_fingerprint_family(self):
        a1, a_dice, a2, fp = self._build_resume_a_family()
        family = resume_fingerprint_family_ids(self.db, a2)
        self.assertEqual(set(family), {a1.id, a_dice.id, a2.id})

    def test_linkedin_history_for_reuploaded_resume_returns_only_linkedin(self):
        a1, a_dice, a2, fp = self._build_resume_a_family()
        rows = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r["job_portal"] == "linkedin" for r in rows))

    def test_dice_history_for_reuploaded_resume_returns_only_dice(self):
        a1, a_dice, a2, fp = self._build_resume_a_family()
        rows = list_applications(resume_id=a2.id, portal="dice", db=self.db)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["job_portal"] == "dice" for r in rows))

    def test_portal_filter_never_leaks_other_portal(self):
        a1, a_dice, a2, fp = self._build_resume_a_family()
        li = list_applications(resume_id=a2.id, portal="linkedin", db=self.db)
        di = list_applications(resume_id=a2.id, portal="dice", db=self.db)
        li_ids = {r["id"] for r in li}
        di_ids = {r["id"] for r in di}
        self.assertEqual(li_ids & di_ids, set())  # no record appears in both

    def test_resume_b_sees_neither_history_set(self):
        a1, a_dice, a2, fp = self._build_resume_a_family()
        b = self._resume(compute_resume_fingerprint("Totally different resume content for B"), "linkedin")
        self.assertEqual(list_applications(resume_id=b.id, portal="linkedin", db=self.db), [])
        self.assertEqual(list_applications(resume_id=b.id, portal="dice", db=self.db), [])

    def test_history_is_never_narrowed_to_a_search_batch(self):
        # History is permanent, not batch data. Both dashboard.py and React still send the
        # selected search_batch_id; narrowing by it is what made earlier runs disappear
        # after a new search, so the parameter must NOT filter the result.
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a1 = self._resume(fp, "linkedin")
        from app.models import JobSearchBatch
        from uuid import uuid4
        batch = JobSearchBatch(id=str(uuid4()), resume_id=a1.id, search_query="x", location="Remote", portal="linkedin")
        self.db.add(batch)
        self.db.flush()
        j_in = self._job(a1, "linkedin", "https://linkedin.com/jobs/view/900")
        j_in.search_batch_id = batch.id          # belongs to the new batch
        self.db.flush()
        self._app(j_in, ApplicationStatus.APPLIED)
        # An older job from a previous search (no/!= batch) must still be in history.
        self._app(self._job(a1, "linkedin", "https://linkedin.com/jobs/view/901"), ApplicationStatus.APPLIED)
        rows = list_applications(resume_id=a1.id, portal="linkedin", search_batch_id=batch.id, db=self.db)
        self.assertEqual(len(rows), 2)

    def test_no_resume_id_returns_all_for_portal_unchanged(self):
        # Regression: existing behaviour when only portal is supplied.
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a1 = self._resume(fp, "linkedin")
        self._app(self._job(a1, "linkedin", "https://linkedin.com/jobs/view/950"), ApplicationStatus.APPLIED)
        rows = list_applications(portal="linkedin", db=self.db)
        self.assertTrue(any(r["job_url"].endswith("/950") for r in rows))

    def test_legacy_resume_without_fingerprint_falls_back_to_self(self):
        a = self._resume(None, "linkedin")  # no fingerprint (legacy)
        self.assertEqual(resume_fingerprint_family_ids(self.db, a), [a.id])
        self._app(self._job(a, "linkedin", "https://linkedin.com/jobs/view/960"), ApplicationStatus.APPLIED)
        rows = list_applications(resume_id=a.id, portal="linkedin", db=self.db)
        self.assertEqual(len(rows), 1)


# --------------------------------------------------------------------------------------
# 14-27. Successful-job exclusion (portal + fingerprint scoped)
# --------------------------------------------------------------------------------------
class SuccessfulJobExclusionTests(_DBTest):
    def test_success_status_set_is_exactly_applied_and_dry_run_ready(self):
        self.assertEqual(SUCCESS_APPLICATION_STATUSES, frozenset({ApplicationStatus.APPLIED, ApplicationStatus.DRY_RUN_READY}))
        self.assertTrue(is_successful_application(ApplicationStatus.APPLIED))
        self.assertTrue(is_successful_application("dry_run_ready"))
        for retryable in ("failed", "skipped", "needs_review", "unsupported_form", "queued", "matched"):
            self.assertFalse(is_successful_application(retryable))

    def test_successful_linkedin_job_excluded_for_reuploaded_resume(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a1 = self._resume(fp, "linkedin")
        job = self._job(a1, "linkedin", "https://linkedin.com/jobs/view/123")
        self._app(job, ApplicationStatus.APPLIED)
        a2 = self._resume(fp, "linkedin")  # re-upload
        excluded = successful_jobs_for_fingerprint(self.db, a2, "linkedin")
        self.assertEqual([j.id for j in excluded], [job.id])

    def test_linkedin_success_does_not_exclude_dice(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a1 = self._resume(fp, "linkedin")
        self._app(self._job(a1, "linkedin", "https://linkedin.com/jobs/view/123"), ApplicationStatus.APPLIED)
        self.assertEqual(successful_jobs_for_fingerprint(self.db, a1, "dice"), [])

    def test_successful_dice_job_excluded_and_does_not_exclude_linkedin(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a_dice = self._resume(fp, "dice")
        self._app(self._job(a_dice, "dice", "https://dice.com/job/456"), ApplicationStatus.APPLIED)
        self.assertEqual(len(successful_jobs_for_fingerprint(self.db, a_dice, "dice")), 1)
        self.assertEqual(successful_jobs_for_fingerprint(self.db, a_dice, "linkedin"), [])

    def test_resume_b_can_still_see_resume_a_completed_job(self):
        fp_a = compute_resume_fingerprint(RESUME_A_TEXT)
        a = self._resume(fp_a, "linkedin")
        self._app(self._job(a, "linkedin", "https://linkedin.com/jobs/view/123"), ApplicationStatus.APPLIED)
        b = self._resume(compute_resume_fingerprint("Different resume B content"), "linkedin")
        self.assertEqual(successful_jobs_for_fingerprint(self.db, b, "linkedin"), [])

    def test_unsuccessful_statuses_remain_eligible(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a = self._resume(fp, "linkedin")
        for i, status in enumerate([
            ApplicationStatus.FAILED, ApplicationStatus.SKIPPED,
            ApplicationStatus.NEEDS_REVIEW, ApplicationStatus.UNSUPPORTED_FORM,
            ApplicationStatus.QUEUED, ApplicationStatus.MATCHED,
        ]):
            self._app(self._job(a, "linkedin", f"https://linkedin.com/jobs/view/{300 + i}"), status)
        self.assertEqual(successful_jobs_for_fingerprint(self.db, a, "linkedin"), [])

    def test_dry_run_ready_is_excluded(self):
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a = self._resume(fp, "linkedin")
        self._app(self._job(a, "linkedin", "https://linkedin.com/jobs/view/321"), ApplicationStatus.DRY_RUN_READY)
        self.assertEqual(len(successful_jobs_for_fingerprint(self.db, a, "linkedin")), 1)

    def test_tracking_url_variance_matches_canonical_identity(self):
        # 26: the same job returned later with tracking params is recognised via the
        # existing canonical job identity (engine.duplicate_key).
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a = self._resume(fp, "linkedin")
        job = self._job(a, "linkedin", "https://linkedin.com/jobs/view/123")
        self._app(job, ApplicationStatus.APPLIED)
        excluded_keys = set()
        for j in successful_jobs_for_fingerprint(self.db, a, "linkedin"):
            excluded_keys |= self._keys_for(j)
        raw_with_tracking = {
            "job_url": "https://linkedin.com/jobs/view/123?utm_source=email&refId=xyz&trk=abc",
            "title": "Analyst", "company": "Acme", "location": "Remote",
        }
        raw_keys = [k for k in self.engine.duplicate_key(raw_with_tracking) if k]
        self.assertTrue(any(k in excluded_keys for k in raw_keys))

    def test_search_loop_decision_excludes_only_successful(self):
        # 27: a raw job identical to a prior SUCCESS is skipped; a raw job identical to a
        # prior FAILED job is kept (mirrors the main.py search-loop decision). Each job is
        # a distinct listing (its own title/company/url) as in real results.
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        a = self._resume(fp, "linkedin")
        success_job = self._job(a, "linkedin", "https://linkedin.com/jobs/view/111", title="Data Analyst", company="Acme")
        self._app(success_job, ApplicationStatus.APPLIED)
        failed_job = self._job(a, "linkedin", "https://linkedin.com/jobs/view/222", title="ML Engineer", company="Globex")
        self._app(failed_job, ApplicationStatus.FAILED)

        excluded_keys = set()
        for j in successful_jobs_for_fingerprint(self.db, a, "linkedin"):
            excluded_keys |= self._keys_for(j)

        def is_excluded(url, title, company):
            keys = [k for k in self.engine.duplicate_key({"job_url": url, "title": title, "company": company, "location": "Remote"}) if k]
            return any(k in excluded_keys for k in keys)

        self.assertTrue(is_excluded("https://linkedin.com/jobs/view/111", "Data Analyst", "Acme"))   # prior success
        self.assertFalse(is_excluded("https://linkedin.com/jobs/view/222", "ML Engineer", "Globex"))  # prior failure retryable
        self.assertFalse(is_excluded("https://linkedin.com/jobs/view/999", "UX Designer", "Initech"))  # brand new


# --------------------------------------------------------------------------------------
# 28-31. Backfill / migration
# --------------------------------------------------------------------------------------
class BackfillTests(_DBTest):
    def test_backfill_assigns_deterministic_fingerprints(self):
        r = self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        summary = backfill_resume_fingerprints(self.db)
        self.db.refresh(r)
        self.assertEqual(r.resume_fingerprint, compute_resume_fingerprint(RESUME_A_TEXT))
        self.assertGreaterEqual(summary["updated"], 1)

    def test_backfill_is_idempotent(self):
        r = self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        backfill_resume_fingerprints(self.db)
        self.db.refresh(r)
        first = r.resume_fingerprint
        second_summary = backfill_resume_fingerprints(self.db)
        self.db.refresh(r)
        self.assertEqual(r.resume_fingerprint, first)  # unchanged
        # This resume is no longer counted as updated on the second run.
        self.assertNotIn(r.id, second_summary.get("skipped_ids", []))

    def test_backfill_preserves_application_records(self):
        r = self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        rec = self._app(self._job(r, "linkedin", "https://linkedin.com/jobs/view/770"), ApplicationStatus.APPLIED)
        rec_id = rec.id
        backfill_resume_fingerprints(self.db)
        self.assertIsNotNone(self.db.get(ApplicationRecord, rec_id))

    def test_unfingerprintable_legacy_row_left_unchanged(self):
        r = self._resume(None, "linkedin", raw_text="   ")  # no usable content
        summary = backfill_resume_fingerprints(self.db)
        self.db.refresh(r)
        self.assertIsNone(r.resume_fingerprint)
        self.assertIn(r.id, summary["skipped_ids"])

    def test_existing_fingerprints_are_never_recomputed_or_overwritten(self):
        # A row whose stored fingerprint does NOT match its text (hand-set / legacy) must be
        # left exactly as-is: the backfill only ever fills NULLs, it never "corrects" rows.
        r = self._resume("deadbeef" * 8, "linkedin", raw_text=RESUME_A_TEXT)
        summary = backfill_resume_fingerprints(self.db)
        self.db.refresh(r)
        self.assertEqual(r.resume_fingerprint, "deadbeef" * 8)
        self.assertGreaterEqual(summary["already_set"], 1)

    def test_running_twice_does_not_change_correct_rows(self):
        done = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "linkedin")
        pending = self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        first = backfill_resume_fingerprints(self.db)
        second = backfill_resume_fingerprints(self.db)
        self.db.refresh(done)
        self.db.refresh(pending)
        self.assertEqual(done.resume_fingerprint, compute_resume_fingerprint(RESUME_A_TEXT))
        self.assertEqual(pending.resume_fingerprint, compute_resume_fingerprint(RESUME_A_TEXT))
        self.assertGreaterEqual(first["updated"], 1)
        self.assertEqual(second["updated"], 0)  # nothing left to do


class BackfillEfficiencyTests(_DBTest):
    """The backfill runs on every startup: it must not load rows that are already done."""

    def _capture_sql(self, fn):
        statements: list[str] = []

        def before(_conn, _cursor, statement, _params, _context, _executemany):
            statements.append(statement)

        event.listen(self.connection, "before_cursor_execute", before)
        try:
            result = fn()
        finally:
            event.remove(self.connection, "before_cursor_execute", before)
        return result, statements

    def test_only_null_fingerprint_rows_are_selected(self):
        for _ in range(20):
            self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "linkedin")
        self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        self.db.flush()

        summary, statements = self._capture_sql(lambda: backfill_resume_fingerprints(self.db))

        # The row-loading SELECT must carry the IS NULL predicate — i.e. the filter is in
        # SQL, not in Python. A bare "SELECT ... FROM resume_profiles" would load everything.
        row_selects = [
            sql for sql in statements
            if "FROM resume_profiles" in sql and "count(" not in sql.lower()
        ]
        self.assertTrue(row_selects, "expected a SELECT against resume_profiles")
        self.assertTrue(
            all("IS NULL" in sql.upper() for sql in row_selects),
            f"completed rows would be loaded: {row_selects}",
        )
        self.assertEqual(summary["updated"], 1)
        self.assertGreaterEqual(summary["already_set"], 20)

    def test_query_count_does_not_grow_with_completed_rows(self):
        self._resume(None, "linkedin", raw_text=RESUME_A_TEXT)
        self.db.flush()
        _, few = self._capture_sql(lambda: backfill_resume_fingerprints(self.db))

        for _ in range(25):
            self._resume(compute_resume_fingerprint(RESUME_A_TEXT + "x"), "linkedin")
        self._resume(None, "linkedin", raw_text=RESUME_A_TEXT + "y")
        self.db.flush()
        _, many = self._capture_sql(lambda: backfill_resume_fingerprints(self.db))

        # 25 extra already-fingerprinted rows must not cost extra statements.
        self.assertEqual(len(many), len(few))

    def test_fully_backfilled_database_is_a_no_op(self):
        for _ in range(5):
            self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "linkedin")
        self.db.flush()
        summary, statements = self._capture_sql(lambda: backfill_resume_fingerprints(self.db))
        self.assertEqual(summary["updated"], 0)
        self.assertEqual(summary["skipped_ids"], [])
        # Nothing to write: no UPDATE/COMMIT work is issued.
        self.assertFalse([sql for sql in statements if sql.strip().upper().startswith("UPDATE")])


if __name__ == "__main__":
    unittest.main()
