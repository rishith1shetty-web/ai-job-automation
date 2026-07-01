import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import delete, select

from app.database import SessionLocal, init_db
from app.main import search_jobs, settings
from app.models import JobOpportunity, JobSearchBatch, ResumeProfile
from app.schemas import SearchRequest


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


class AutomaticSearchWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        init_db()

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
                        jobs = search_jobs(request, db)

                    self.assertEqual(len(jobs), 1)
                    self.assertIn("Backend Developer", FakeLinkedInAutomation.received_queries)
                    self.assertGreater(jobs[0].match_score, 0)
                    self.assertGreater(jobs[0].priority_score, 0)
                    self.assertEqual(jobs[0].complexity_level, "Simple")
                    self.assertEqual(jobs[0].search_query_used, "Backend Developer")
                    first_batch_id = jobs[0].search_batch_id
                    self.assertIsNotNone(first_batch_id)
                    self.assertIsNotNone(jobs[0].search_timestamp)

                    with patch("app.main.LinkedInAutomation", FakeLinkedInAutomation):
                        second_jobs = search_jobs(request, db)

                    self.assertEqual(len(second_jobs), 0)
                    stored_versions = list(
                        db.scalars(
                            select(JobOpportunity).where(
                                JobOpportunity.resume_id == resume.id,
                                JobOpportunity.job_url
                                == "https://example.com/jobs/automatic-search-test",
                            )
                        ).all()
                    )
                    self.assertEqual(len(stored_versions), 1)

                    db.execute(delete(JobOpportunity).where(JobOpportunity.resume_id == resume.id))
                    db.execute(delete(JobSearchBatch).where(JobSearchBatch.resume_id == resume.id))
                    db.delete(resume)
                    db.commit()


if __name__ == "__main__":
    unittest.main()
