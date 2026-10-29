"""Senior-level exclusion through the real Find Matches pipeline (_search_jobs_sync), for BOTH
portals. Uses the database like test_automatic_search_workflow.py; the portals are fakes, so no
live LinkedIn or Dice access is needed.

The adapters return a mix of senior and acceptable jobs. Find Matches must persist only the
acceptable ones, reject the senior ones BEFORE the weighted allocation (so Find 20 still returns
20 when 20 eligible jobs exist), and leave manual overrides, duplicate ownership and the
Easy-Apply-only gate exactly as they were.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.main import _create_search_batch, _run_find_matches_batch, set_active_portal, settings
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.schemas import SearchRequest
from app.services.find_matches import per_variation_limits
from app.services.seniority_filter import senior_level_reason

SENIOR_TITLES = ("Senior DevOps Engineer", "Lead Cloud Engineer", "Principal Platform Engineer",
                 "Staff Site Reliability Engineer", "DevOps Architect", "Engineering Manager")


def _mixed_jobs(variation_number: int, count: int, url_prefix: str) -> list[dict]:
    """`count` raw jobs where every third one is senior-level."""
    jobs = []
    for position in range(count):
        senior = position % 3 == 0
        title = (SENIOR_TITLES[position % len(SENIOR_TITLES)] if senior
                 else f"DevOps Engineer {variation_number}-{position}")
        jobs.append(
            {
                "title": title,
                "company": f"Company {variation_number}-{position}",
                "location": "Remote",
                "description": "Terraform Kubernetes AWS CI/CD. You will partner with senior stakeholders.",
                "job_url": f"{url_prefix}{variation_number * 1000 + position}",
                "easy_apply": True,
            }
        )
    return jobs


class FakeMixedLinkedIn:
    received_queries: list[str] = []

    def __init__(self, _settings) -> None:
        pass

    def search_jobs_many(self, _resume, variations, limit_per_query, collect_card_diagnostics=False, **_kwargs):
        variations = list(variations)
        limits = per_variation_limits(limit_per_query, len(variations))
        self.__class__.received_queries = [variation.query for variation in variations]
        return [
            (variation, _mixed_jobs(index + 1, limits[index], "https://www.linkedin.com/jobs/view/"))
            for index, variation in enumerate(variations)
        ]


class FakeMixedDice:
    def is_connected(self) -> bool:
        return True

    def search_jobs_many(self, _resume, variations, limit_per_query, progress_callback=None, global_target=None):
        variations = list(variations)
        limits = per_variation_limits(limit_per_query, len(variations))
        return [
            (variation, _mixed_jobs(index + 1, limits[index],
                                    "https://www.dice.com/job-detail/00000000-0000-4000-8000-"))
            for index, variation in enumerate(variations)
        ]


class FakeDuplicateLinkedIn:
    """The same acceptable job under variations 1 and 2, and a senior job under both too."""

    def __init__(self, _settings) -> None:
        pass

    def search_jobs_many(self, _resume, variations, limit_per_query, collect_card_diagnostics=False, **_kwargs):
        shared = {"title": "Cloud Engineer", "company": "Shared Co", "location": "Remote",
                  "description": "AWS", "job_url": "https://www.linkedin.com/jobs/view/555000555",
                  "easy_apply": True}
        senior = {"title": "Senior Cloud Engineer", "company": "Senior Co", "location": "Remote",
                  "description": "AWS", "job_url": "https://www.linkedin.com/jobs/view/555000556",
                  "easy_apply": True}
        return [(variation, [dict(senior), dict(shared)]) for variation in list(variations)[:2]]


def _resume(portal: str, **overrides) -> ResumeProfile:
    fields = dict(
        portal=portal,
        filename=f"senior-filter-{portal}.txt",
        stored_path=f"senior-filter-{portal}.txt",
        raw_text="DevOps engineer Terraform Kubernetes AWS CI/CD Docker Linux",
        skills=["terraform", "kubernetes", "aws"],
        experience=[],
        education=[],
        job_titles=[],
        keywords=["terraform", "kubernetes", "aws"],
        technologies=["Terraform", "Kubernetes", "AWS"],
        domain_expertise=["DevOps"],
        detected_role="DevOps Engineer",
        generated_search_keywords=["DevOps Engineer", "Cloud Engineer"],
        role_confidence=90,
    )
    fields.update(overrides)
    return ResumeProfile(**fields)


class SeniorExclusionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()

    def _run(self, db, resume: ResumeProfile, request: SearchRequest) -> tuple[JobSearchBatch, list[JobOpportunity]]:
        batch = _create_search_batch(request, db)
        _run_find_matches_batch(batch.id)
        db.refresh(batch)
        jobs = list(db.scalars(select(JobOpportunity).where(JobOpportunity.search_batch_id == batch.id)).all())
        return batch, jobs

    def _cleanup(self, db, resume: ResumeProfile) -> None:
        db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
        db.delete(resume)
        db.commit()

    def _assert_no_senior_jobs(self, jobs: list[JobOpportunity]) -> None:
        senior = [job.title for job in jobs if senior_level_reason(job.title, job.description)]
        self.assertEqual(senior, [], "a senior-level job entered the batch")

    # 17/18. LinkedIn: seniors are rejected before the allocation, so Find 20 still returns 20.
    def test_linkedin_find20_returns_twenty_eligible_jobs_and_no_senior_roles(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    set_active_portal(db, "linkedin")
                    resume = _resume("linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with patch("app.main.LinkedInAutomation", FakeMixedLinkedIn):
                            batch, jobs = self._run(db, resume, SearchRequest(
                                resume_id=resume.id, location="Remote", keywords=None, limit=20))
                        self.assertEqual(batch.status, "completed")
                        self.assertEqual(len(jobs), 20)
                        self.assertEqual(batch.jobs_collected, 20)
                        self._assert_no_senior_jobs(jobs)
                        self.assertTrue(all(job.easy_apply for job in jobs))
                    finally:
                        self._cleanup(db, resume)

    # 17. Dice: the same gate applies to the other portal.
    def test_dice_find_matches_excludes_senior_roles(self) -> None:
        with SessionLocal() as db:
            set_active_portal(db, "dice")
            resume = _resume("dice")
            db.add(resume)
            db.commit()
            db.refresh(resume)
            try:
                with patch("app.main.dice_adapter", return_value=FakeMixedDice()):
                    batch, jobs = self._run(db, resume, SearchRequest(
                        resume_id=resume.id, location="Remote", keywords=None, limit=20, portal="dice"))
                self.assertEqual(batch.status, "completed")
                self.assertEqual(len(jobs), 20)
                self._assert_no_senior_jobs(jobs)
                self.assertTrue(all(job.portal == "dice" for job in jobs))
            finally:
                self._cleanup(db, resume)
                set_active_portal(db, "linkedin")

    # 20. A manual role override is searched verbatim and still filtered.
    def test_manual_keyword_override_is_searched_and_filtered(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    set_active_portal(db, "linkedin")
                    resume = _resume("linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with patch("app.main.LinkedInAutomation", FakeMixedLinkedIn):
                            _batch, jobs = self._run(db, resume, SearchRequest(
                                resume_id=resume.id, location="Remote", keywords="Platform Engineer", limit=20))
                        self.assertEqual(FakeMixedLinkedIn.received_queries[0], "Platform Engineer")
                        self.assertTrue(jobs)
                        self._assert_no_senior_jobs(jobs)
                    finally:
                        self._cleanup(db, resume)

    # 20. Duplicate ownership is unchanged; a senior duplicate is excluded under every variation.
    def test_duplicate_ownership_is_unchanged(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    set_active_portal(db, "linkedin")
                    resume = _resume("linkedin")
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    try:
                        with patch("app.main.LinkedInAutomation", FakeDuplicateLinkedIn):
                            _batch, jobs = self._run(db, resume, SearchRequest(
                                resume_id=resume.id, location="Remote", keywords=None, limit=20))
                        self.assertEqual([job.title for job in jobs], ["Cloud Engineer"])
                        self.assertEqual(jobs[0].job_description_metadata.get("variation_index"), 1)
                    finally:
                        self._cleanup(db, resume)


if __name__ == "__main__":
    unittest.main()
