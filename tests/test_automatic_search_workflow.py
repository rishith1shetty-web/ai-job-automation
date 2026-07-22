import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks
from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.main import _create_search_batch, _run_find_matches_batch, search_jobs, set_active_portal, settings
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.schemas import SearchRequest
from app.services.find_matches import per_variation_limits


class FakeLinkedInAutomation:
    received_queries: list[str] = []

    def __init__(self, _settings) -> None:
        pass

    def search_jobs(self, _resume, _location, keywords, _limit):
        self.__class__.received_queries.append(keywords)
        return [
            {
                "title": "Backend Developer",
                "company": "Example Company",
                "location": "Remote",
                "description": "Python FastAPI SQL REST APIs",
                "job_url": "https://example.com/jobs/automatic-search-test",
                "easy_apply": True,
            }
        ]


class FakeManyLinkedInAutomation:
    received_collection_plan: list[tuple[int, int]] = []

    def __init__(self, _settings) -> None:
        pass

    def search_jobs_many(self, _resume, variations, limit_per_query, collect_card_diagnostics=False):
        # limit_per_query is now a per-variation budget (uniform int or weighted list); mirror
        # the real adapter contract so the top variations can inspect a larger candidate budget.
        variations = list(variations)
        limits = per_variation_limits(limit_per_query, len(variations))
        self.__class__.received_collection_plan.append((len(variations), limits))
        results = []
        counter = 0
        for index, variation in enumerate(variations):
            raw_jobs = []
            for _index in range(limits[index]):
                counter += 1
                raw_jobs.append(
                    {
                        "title": f"Backend Developer {counter}",
                        "company": f"Example Company {counter}",
                        "location": "Remote",
                        "description": "Python FastAPI SQL REST APIs",
                        "job_url": f"https://www.linkedin.com/jobs/view/{900000000 + counter}",
                        "easy_apply": True,
                    }
                )
            results.append((variation, raw_jobs))
        return results


class FakeDedupLinkedIn:
    """Returns the SAME canonical job under two variations (positions 2 and 5) plus a
    non-Easy-Apply external job under variation 3, to exercise earliest-variation ownership
    and the Easy-Apply-only gate."""

    SHARED_URL = "https://www.linkedin.com/jobs/view/777000777"
    EXTERNAL_URL = "https://boards.greenhouse.io/acme/jobs/999"

    def __init__(self, _settings) -> None:
        pass

    def search_jobs_many(self, _resume, variations, limit_per_query, collect_card_diagnostics=False):
        variations = list(variations)
        shared = {"title": "Shared Role", "company": "Shared Co", "location": "Remote",
                  "description": "Python FastAPI SQL", "job_url": self.SHARED_URL, "easy_apply": True}
        external = {"title": "External Role", "company": "Ext Co", "location": "Remote",
                    "description": "Python", "job_url": self.EXTERNAL_URL, "easy_apply": False}
        results = []
        for i, variation in enumerate(variations):
            raw: list[dict] = []
            if i in (1, 4):            # variation_index 2 and 5 both discover the shared job
                raw.append(dict(shared))
            if i == 2:                 # variation_index 3 discovers a non-Easy-Apply external job
                raw.append(dict(external))
            results.append((variation, raw))
        return results


class AutomaticSearchWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()
        with SessionLocal() as db:
            set_active_portal(db, "linkedin")

    def test_cross_variation_dedup_owns_earliest_and_keeps_easy_apply_only(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    resume = ResumeProfile(
                        filename="dedup-resume.txt", stored_path="dedup-resume.txt",
                        raw_text="Python FastAPI SQL REST APIs backend developer",
                        skills=["python", "fastapi", "sql"], experience=[], education=[],
                        job_titles=[], keywords=["python", "fastapi", "sql"],
                        technologies=["Python", "FastAPI", "SQL"],
                        domain_expertise=["Backend Engineering"], detected_role="Backend Developer",
                        generated_search_keywords=["Backend Developer", "Python Developer",
                                                   "FastAPI Developer", "Software Engineer"],
                        role_confidence=95,
                    )
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)
                    # limit 50 -> >=8 variations selected, so positions 2 and 5 both run.
                    request = SearchRequest(resume_id=resume.id, location="United States",
                                            keywords=None, limit=50)
                    try:
                        with patch("app.main.LinkedInAutomation", FakeDedupLinkedIn):
                            batch = _create_search_batch(request, db)
                            _run_find_matches_batch(batch.id)
                            jobs = list(db.scalars(
                                select(JobOpportunity).where(JobOpportunity.search_batch_id == batch.id)
                            ).all())
                        shared = [j for j in jobs if j.job_url == FakeDedupLinkedIn.SHARED_URL]
                        # 13: the canonical job found under two variations appears exactly once.
                        self.assertEqual(len(shared), 1)
                        # 14: it is owned by the EARLIEST (highest-priority) variation -- index 2.
                        self.assertEqual(shared[0].job_description_metadata.get("variation_index"), 2)
                        # Easy-Apply-only: the external, non-Easy-Apply job never entered the batch,
                        # and every persisted job is Easy Apply.
                        self.assertFalse(any(j.job_url == FakeDedupLinkedIn.EXTERNAL_URL for j in jobs))
                        self.assertTrue(all(j.easy_apply for j in jobs))
                    finally:
                        db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()

    def test_find_matches_endpoint_returns_background_batch_immediately(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    resume = ResumeProfile(
                        filename="background-resume.txt",
                        stored_path="background-resume.txt",
                        raw_text="Python FastAPI SQL REST APIs",
                        skills=["python", "fastapi", "sql"],
                        experience=[],
                        education=[],
                        job_titles=[],
                        keywords=["python", "fastapi", "sql"],
                        technologies=["Python", "FastAPI", "SQL"],
                        domain_expertise=["Backend Engineering"],
                        detected_role="Backend Developer",
                        generated_search_keywords=["Backend Developer"],
                        role_confidence=90,
                    )
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)

                    background_tasks = BackgroundTasks()
                    response = search_jobs(
                        SearchRequest(resume_id=resume.id, location="United States", limit=20),
                        background_tasks,
                        db,
                    )

                    self.assertEqual(response["status"], "queued")
                    self.assertEqual(response["requested_limit"], 20)
                    self.assertEqual(response["jobs_collected"], 0)
                    self.assertEqual(len(background_tasks.tasks), 1)

                    db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                    db.delete(resume)
                    db.commit()

    def test_dice_portal_search_batch_is_created(self) -> None:
        with SessionLocal() as db:
            resume = ResumeProfile(
                portal="dice",
                filename="dice-resume.txt",
                stored_path="dice-resume.txt",
                raw_text="Python SQL data analyst",
                skills=["python", "sql"],
                experience=[],
                education=[],
                job_titles=[],
                keywords=["python", "sql"],
                technologies=["Python", "SQL"],
                domain_expertise=["Data Analytics"],
                detected_role="Data Analyst",
                generated_search_keywords=["Data Analyst"],
                role_confidence=80,
            )
            db.add(resume)
            db.commit()
            db.refresh(resume)
            try:
                with patch("app.main.dice_adapter") as fake_dice_adapter:
                    fake_dice_adapter.return_value.is_connected.return_value = True
                    set_active_portal(db, "dice")
                    batch = _create_search_batch(
                        SearchRequest(resume_id=resume.id, location="Remote", limit=20, portal="dice"),
                        db,
                    )

                self.assertEqual(batch.portal, "dice")
                self.assertEqual(batch.requested_limit, 20)
            finally:
                db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                db.delete(resume)
                db.commit()

    def test_find_matches_without_manual_keyword(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    resume = ResumeProfile(
                        filename="resume.txt",
                        stored_path="resume.txt",
                        raw_text="Python FastAPI SQL REST APIs",
                        skills=["python", "fastapi", "sql"],
                        experience=[],
                        education=[],
                        job_titles=[],
                        keywords=["python", "fastapi", "sql"],
                        technologies=["Python", "FastAPI", "SQL", "REST APIs"],
                        domain_expertise=["Backend Engineering", "API Development"],
                        detected_role="Backend Developer",
                        generated_search_keywords=[
                            "Backend Developer",
                            "Python Developer",
                            "FastAPI Developer",
                            "Software Engineer",
                        ],
                        role_confidence=95,
                    )
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)

                    request = SearchRequest(
                        resume_id=resume.id,
                        location="United States",
                        keywords=None,
                        limit=10,
                    )
                    with patch("app.main.LinkedInAutomation", FakeLinkedInAutomation):
                        FakeLinkedInAutomation.received_queries = []
                        batch = _create_search_batch(request, db)
                        _run_find_matches_batch(batch.id)
                        db.refresh(batch)
                        jobs = list(
                            db.scalars(
                                select(JobOpportunity).where(
                                    JobOpportunity.search_batch_id == batch.id
                                )
                            ).all()
                        )

                    self.assertEqual(len(jobs), 1)
                    self.assertEqual(batch.status, "completed")
                    self.assertEqual(batch.jobs_collected, 1)
                    self.assertEqual(batch.easy_apply_count, 1)
                    self.assertIn("Backend Developer", FakeLinkedInAutomation.received_queries)
                    self.assertGreater(jobs[0].match_score, 0)
                    self.assertGreater(jobs[0].priority_score, 0)
                    self.assertEqual(jobs[0].complexity_level, "Simple")
                    self.assertEqual(jobs[0].search_query_used, "Backend Developer")
                    first_batch_id = jobs[0].search_batch_id
                    self.assertIsNotNone(first_batch_id)
                    self.assertIsNotNone(jobs[0].search_timestamp)
                    # The fake returns the same job for every search variation, so the extras
                    # are removed as in-batch duplicates. A re-run must remove exactly the
                    # same number -- no additional cross-batch suppression.
                    first_duplicates_removed = batch.duplicates_removed

                    with patch("app.main.LinkedInAutomation", FakeLinkedInAutomation):
                        second_batch = _create_search_batch(request, db)
                        _run_find_matches_batch(second_batch.id)
                        db.refresh(second_batch)
                        second_jobs = list(
                            db.scalars(
                                select(JobOpportunity).where(
                                    JobOpportunity.search_batch_id == second_batch.id
                                )
                            ).all()
                        )

                    # The job was only MATCHED in the first batch -- never applied -- so it
                    # must stay searchable and be collected again. Merely appearing in an
                    # earlier Find Matches batch never excludes a job; only a confirmed live
                    # APPLIED outcome does (see tests/test_previous_batch_suppression.py).
                    self.assertEqual(len(second_jobs), 1)
                    self.assertEqual(second_batch.status, "completed")
                    self.assertEqual(second_batch.duplicates_removed, first_duplicates_removed)
                    stored_versions = list(
                        db.scalars(
                            select(JobOpportunity).where(
                                JobOpportunity.resume_id == resume.id,
                                JobOpportunity.job_url
                                == "https://example.com/jobs/automatic-search-test",
                            )
                        ).all()
                    )
                    # One row per batch: the same job legitimately reappears in the new batch
                    # (uniqueness is scoped to job_url + resume + search_batch_id).
                    self.assertEqual(len(stored_versions), 2)
                    self.assertEqual(
                        {row.search_batch_id for row in stored_versions},
                        {first_batch_id, second_batch.id},
                    )

                    db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                    db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                    db.delete(resume)
                    db.commit()

    def test_find_matches_supports_limit_100(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".json") as state_file:
            with patch.object(settings, "linkedin_storage_state", Path(state_file.name)):
                with SessionLocal() as db:
                    resume = ResumeProfile(
                        filename="volume-resume.txt",
                        stored_path="volume-resume.txt",
                        raw_text="Python FastAPI SQL REST APIs backend developer",
                        skills=["python", "fastapi", "sql"],
                        experience=[],
                        education=[],
                        job_titles=[],
                        keywords=["python", "fastapi", "sql"],
                        technologies=["Python", "FastAPI", "SQL", "REST APIs"],
                        domain_expertise=["Backend Engineering", "API Development"],
                        detected_role="Backend Developer",
                        generated_search_keywords=[
                            "Backend Developer",
                            "Python Developer",
                            "FastAPI Developer",
                            "Software Engineer",
                        ],
                        role_confidence=95,
                    )
                    db.add(resume)
                    db.commit()
                    db.refresh(resume)

                    request = SearchRequest(
                        resume_id=resume.id,
                        location="United States",
                        keywords=None,
                        limit=100,
                    )
                    try:
                        with patch("app.main.LinkedInAutomation", FakeManyLinkedInAutomation):
                            FakeManyLinkedInAutomation.received_collection_plan = []
                            batch = _create_search_batch(request, db)
                            _run_find_matches_batch(batch.id)
                            db.refresh(batch)
                            jobs = list(
                                db.scalars(
                                    select(JobOpportunity).where(
                                        JobOpportunity.search_batch_id == batch.id
                                    )
                                ).all()
                            )

                        self.assertEqual(len(jobs), 100)
                        self.assertEqual(batch.status, "completed")
                        self.assertEqual(batch.jobs_collected, 100)
                        self.assertTrue(all(job.easy_apply for job in jobs))
                        selected_variations, limits = FakeManyLinkedInAutomation.received_collection_plan[0]
                        # Phase 4: the plan is one narrow variation per ranked role (up to 12), so a
                        # supported-domain resume selects its full set of role variations.
                        self.assertGreaterEqual(selected_variations, 12)
                        self.assertGreaterEqual(min(limits), 8)          # base per-query budget preserved
                        self.assertGreater(limits[0], limits[-1])        # top-five inspect a larger budget
                        self.assertEqual(len({job.job_url for job in jobs}), 100)
                    finally:
                        db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                        db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                        db.delete(resume)
                        db.commit()

    def test_find_matches_custom_limit_caps_at_100(self) -> None:
        engine = __import__("app.services.find_matches", fromlist=["FindMatchesEngine"]).FindMatchesEngine(settings)
        self.assertEqual(engine.target_count(150), 100)


if __name__ == "__main__":
    unittest.main()
