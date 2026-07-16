"""Regression tests: appearing in a PREVIOUS Find Matches batch must never exclude a job.

Proven from the real Dice Find 100 run 20260716T152746174043:
    129 raw -> 2 removed as true in-batch duplicates -> 127 unique
    -> 98 removed by the status-blind `previous_keys` filter -> 29 final.
All 98 traced to prior batch 54abb56b, and every prior row for that resume was status
MATCHED (136/136) -- never applied. The APPLIED exclusion removed 0.

Final business rule: a job may be excluded across batches ONLY on a confirmed successful
live APPLIED outcome for the same portal + canonical identity + resume fingerprint family.
MATCHED / DRY_RUN_READY / FAILED / NEEDS_REVIEW / SKIPPED / UNSUPPORTED_FORM stay searchable.
(TIMED_OUT is not a defined ApplicationStatus, so it cannot exclude anything.)

Fix: the `previous_keys` set and its "Already present in previous batch for this resume"
branch are removed from main.py. Cross-batch exclusion now comes only from
excluded_success_keys_by_portal, which asks the existing successful_jobs_for_fingerprint for
APPLIED only. In-batch duplicate suppression (seen_keys) is unchanged.
"""

import unittest
from pathlib import Path

from app.models import ApplicationStatus
from app.services.resume_identity import compute_resume_fingerprint, successful_jobs_for_fingerprint

from tests.test_resume_identity_history import RESUME_A_TEXT, _DBTest

MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")


class PreviousBatchSuppressionTests(_DBTest):
    """Mirrors the main.py collection loop: in-batch seen_keys + APPLIED-only exclusion."""

    def _excluded_keys(self, resume, portal) -> set:
        # Exactly what main.py builds now: APPLIED only, portal-scoped, fingerprint family.
        keys: set = set()
        for job in successful_jobs_for_fingerprint(
            self.db, resume, portal, statuses=(ApplicationStatus.APPLIED,)
        ):
            keys |= self._keys_for(job)
        return keys

    def _select(self, raw_jobs, excluded_keys):
        """The main.py decision: in-batch duplicate, else APPLIED-excluded, else keep."""
        seen: set = set()
        kept, removed = [], []
        for raw in raw_jobs:
            keys = [k for k in self.engine.duplicate_key(raw) if k]
            if any(k in seen for k in keys):
                removed.append((raw, "Duplicate in current search batch"))
                continue
            if any(k in excluded_keys for k in keys):
                removed.append((raw, "Already successfully applied for this resume and portal"))
                continue
            seen.update(keys)
            kept.append(raw)
        return kept, removed

    def _raw(self, job) -> dict:
        return {
            "job_url": job.job_url,
            "title": job.title,
            "company": job.company,
            "location": job.location,
        }

    # 1. A MATCHED job from a previous batch is returned again.  <-- THE 98-job regression
    def test_matched_job_from_previous_batch_is_returned_again(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        prior = self._job(resume, "dice", "https://www.dice.com/jobs?q=x", title="Data Analyst", company="Acme")
        # MATCHED = merely found in an earlier batch; no ApplicationRecord at all.
        self.assertEqual(prior.status, ApplicationStatus.MATCHED)
        kept, removed = self._select([self._raw(prior)], self._excluded_keys(resume, "dice"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed, [])

    # 2. A DRY_RUN_READY job from a previous batch is returned again.
    def test_dry_run_ready_job_from_previous_batch_is_returned_again(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        prior = self._job(resume, "dice", "https://www.dice.com/job-detail/guid-dry")
        self._app(prior, ApplicationStatus.DRY_RUN_READY)
        kept, _ = self._select([self._raw(prior)], self._excluded_keys(resume, "dice"))
        self.assertEqual(len(kept), 1, "a submit-ready dry run is not a submission")

    # 3. FAILED, NEEDS_REVIEW, SKIPPED, UNSUPPORTED_FORM remain searchable.
    def test_non_applied_outcomes_remain_searchable(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        for i, status in enumerate(
            (
                ApplicationStatus.FAILED,
                ApplicationStatus.NEEDS_REVIEW,
                ApplicationStatus.SKIPPED,
                ApplicationStatus.UNSUPPORTED_FORM,
                ApplicationStatus.QUEUED,
            )
        ):
            with self.subTest(status=status):
                prior = self._job(resume, "dice", f"https://www.dice.com/job-detail/guid-{i}")
                self._app(prior, status)
                kept, _ = self._select([self._raw(prior)], self._excluded_keys(resume, "dice"))
                self.assertEqual(len(kept), 1)

    # 4. A confirmed successful Live APPLIED job is excluded.
    def test_applied_job_is_excluded(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        prior = self._job(resume, "dice", "https://www.dice.com/job-detail/guid-applied")
        self._app(prior, ApplicationStatus.APPLIED)
        kept, removed = self._select([self._raw(prior)], self._excluded_keys(resume, "dice"))
        self.assertEqual(kept, [])
        self.assertEqual(removed[0][1], "Already successfully applied for this resume and portal")

    def test_applied_exclusion_survives_resume_reupload(self) -> None:
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        first = self._resume(fp, "dice")
        prior = self._job(first, "dice", "https://www.dice.com/job-detail/guid-family")
        self._app(prior, ApplicationStatus.APPLIED)
        reupload = self._resume(fp, "dice")  # new id, same fingerprint family
        kept, _ = self._select([self._raw(prior)], self._excluded_keys(reupload, "dice"))
        self.assertEqual(kept, [])

    # 5. A duplicate repeated within the same current batch is still removed.
    def test_in_batch_duplicate_still_removed(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        job = self._job(resume, "dice", "https://www.dice.com/jobs?q=x", title="BI Analyst", company="Acme")
        kept, removed = self._select([self._raw(job), self._raw(job)], self._excluded_keys(resume, "dice"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed[0][1], "Duplicate in current search batch")

    # 6. Different search variations returning the same job still collapse to one.
    def test_same_job_from_different_variations_collapses(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        job = self._job(resume, "dice", "https://www.dice.com/jobs?q=a", title="Power BI Dev", company="System One")
        # Same job surfaced by two different queries (fallback URLs differ by query string).
        via_a = {**self._raw(job), "job_url": "https://www.dice.com/jobs?q=BI+Analyst"}
        via_b = {**self._raw(job), "job_url": "https://www.dice.com/jobs?q=Dashboard+Developer"}
        kept, removed = self._select([via_a, via_b], self._excluded_keys(resume, "dice"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(removed[0][1], "Duplicate in current search batch")

    def test_distinct_jobs_from_previous_batch_all_return(self) -> None:
        # The real shape of the bug: many distinct prior-batch MATCHED jobs, all must return.
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        priors = [
            self._job(resume, "dice", "https://www.dice.com/jobs?q=x", title=f"Analyst {i}", company=f"Co {i}")
            for i in range(10)
        ]
        kept, removed = self._select([self._raw(j) for j in priors], self._excluded_keys(resume, "dice"))
        self.assertEqual(len(kept), 10)
        self.assertEqual(removed, [])

    # 7. LinkedIn behaviour unchanged.
    def test_linkedin_unchanged(self) -> None:
        fp = compute_resume_fingerprint(RESUME_A_TEXT)
        resume = self._resume(fp, "linkedin")
        # A prior MATCHED LinkedIn job returns again...
        matched = self._job(resume, "linkedin", "https://linkedin.com/jobs/view/111")
        kept, _ = self._select([self._raw(matched)], self._excluded_keys(resume, "linkedin"))
        self.assertEqual(len(kept), 1)
        # ...an APPLIED LinkedIn job is excluded...
        applied = self._job(resume, "linkedin", "https://linkedin.com/jobs/view/222")
        self._app(applied, ApplicationStatus.APPLIED)
        kept, _ = self._select([self._raw(applied)], self._excluded_keys(resume, "linkedin"))
        self.assertEqual(kept, [])
        # ...and a LinkedIn success never excludes on Dice (portal isolation).
        self.assertEqual(self._excluded_keys(resume, "dice"), set())

    # 8. Existing successful_jobs_for_fingerprint behaviour is unchanged by default.
    def test_successful_jobs_for_fingerprint_default_behaviour_unchanged(self) -> None:
        resume = self._resume(compute_resume_fingerprint(RESUME_A_TEXT), "dice")
        applied = self._job(resume, "dice", "https://www.dice.com/job-detail/g-applied")
        self._app(applied, ApplicationStatus.APPLIED)
        dry = self._job(resume, "dice", "https://www.dice.com/job-detail/g-dry")
        self._app(dry, ApplicationStatus.DRY_RUN_READY)
        # Default (no statuses arg) still means APPLIED + DRY_RUN_READY, as before.
        default_ids = {j.id for j in successful_jobs_for_fingerprint(self.db, resume, "dice")}
        self.assertEqual(default_ids, {applied.id, dry.id})
        # Find Matches opts into APPLIED-only explicitly.
        applied_only = {
            j.id
            for j in successful_jobs_for_fingerprint(
                self.db, resume, "dice", statuses=(ApplicationStatus.APPLIED,)
            )
        }
        self.assertEqual(applied_only, {applied.id})


class PreviousBatchRuleRemovedTests(unittest.TestCase):
    def test_status_blind_previous_batch_rule_is_gone(self) -> None:
        # Guard the fix: the status-blind filter and its reason string must not come back.
        self.assertNotIn("previous_keys", MAIN_SOURCE)
        self.assertNotIn("Already present in previous batch for this resume", MAIN_SOURCE)

    def test_remaining_exclusions_are_in_batch_dupes_and_applied_only(self) -> None:
        self.assertIn("Duplicate in current search batch", MAIN_SOURCE)
        self.assertIn("Already successfully applied for this resume and portal", MAIN_SOURCE)
        self.assertIn("statuses=(ApplicationStatus.APPLIED,)", MAIN_SOURCE)


if __name__ == "__main__":
    unittest.main()
