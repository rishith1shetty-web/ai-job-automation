import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.models import JobOpportunity, ResumeProfile
from app.services.find_matches import FindMatchesEngine


class FindMatchesEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            openai_api_key=None,
            find_matches_debug_dir=Path(tempfile.mkdtemp()) / "find_matches_debug",
        )
        self.engine = FindMatchesEngine(settings)
        self.resume = ResumeProfile(
            id=1,
            filename="resume.pdf",
            stored_path="resume.pdf",
            raw_text=(
                "Data analyst with Python, SQL, Power BI, Excel, Tableau, machine learning, "
                "FastAPI, MongoDB, dashboards, reporting, and analytics projects."
            ),
            skills=["Python", "SQL", "Power BI", "Excel", "Machine Learning"],
            technologies=["FastAPI", "MongoDB", "Tableau"],
            experience=[{"title": "Data Analyst Intern", "description": "Built SQL dashboards"}],
            education=[{"degree": "B.Tech"}],
            job_titles=[],
            keywords=["analytics", "reporting"],
            domain_expertise=["Data Analytics"],
            detected_role="Data Analyst",
            role_suggestions=[
                {"role": "Data Scientist", "confidence": 78},
                {"role": "Data Analyst", "confidence": 76},
            ],
            generated_search_keywords=["Data Scientist", "Data Analyst", "BI Analyst"],
            generated_search_query="Data Scientist OR Data Analyst OR BI Analyst",
            role_confidence=76,
        )

    def test_role_detection_returns_multiple_ranked_roles(self) -> None:
        roles = self.engine.detect_roles(self.resume)
        role_names = [item["role"] for item in roles]
        self.assertIn("Data Scientist", role_names)
        self.assertIn("Data Analyst", role_names)
        self.assertGreaterEqual(len(roles), 3)
        self.assertTrue(all(item["confidence"] <= 92 for item in roles))

    def test_role_skill_and_location_expansion_are_bounded(self) -> None:
        roles = self.engine.expand_roles(self.engine.detect_roles(self.resume))
        skills = self.engine.expand_skills(self.resume)
        locations = self.engine.expand_locations("Mumbai")
        self.assertLessEqual(len(roles), 12)
        self.assertTrue(any(item["role"] == "Junior Data Analyst" for item in roles))
        self.assertTrue(any(item["skill"] == "SQL" for item in skills))
        self.assertEqual([item["location"] for item in locations[:4]], ["Mumbai", "Navi Mumbai", "Thane", "Pune"])

    def test_multi_pass_search_plan_stays_fast(self) -> None:
        queries, plan = self.engine.build_search_plan(self.resume, "Mumbai", None)
        self.assertLessEqual(len(queries), 18)
        self.assertEqual(queries[0].pass_name, "primary_role_primary_location")
        self.assertIn("Data Scientist", plan["display_query"])
        self.assertTrue(any(query.location == "Remote" for query in queries))

    def test_duplicate_key_uses_url_job_id_and_title_company_location(self) -> None:
        raw = {
            "title": "Data Analyst",
            "company": "Example",
            "location": "Mumbai",
            "job_url": "https://www.linkedin.com/jobs/view/1234567890/?trackingId=x",
        }
        keys = self.engine.duplicate_key(raw)
        self.assertIn("1234567890", keys)
        self.assertIn("data analyst|example|mumbai", keys)

    def test_complexity_and_freshness_scoring(self) -> None:
        simple = self.engine.estimate_complexity("Data Analyst", "Apply today. Resume required.")
        complex_job = self.engine.estimate_complexity(
            "Data Analyst",
            "Visa sponsorship, salary expectation, cover letter, employment history, years of SQL required.",
        )
        self.assertEqual(simple["complexity_level"], "Simple")
        self.assertEqual(complex_job["complexity_level"], "Complex")
        self.assertGreater(self.engine.estimate_freshness("posted today")["score"], self.engine.estimate_freshness("1 month ago")["score"])

    def test_priority_queue_prefers_simple_easy_apply_jobs(self) -> None:
        simple = JobOpportunity(
            resume_id=1,
            title="Simple Data Analyst",
            company="A",
            location="Remote",
            job_url="https://example.com/simple",
            description="SQL",
            easy_apply=True,
            match_score=70,
            complexity_score=95,
            complexity_level="Simple",
            freshness_score=90,
        )
        complex_job = JobOpportunity(
            resume_id=1,
            title="Complex Data Scientist",
            company="B",
            location="Remote",
            job_url="https://example.com/complex",
            description="SQL",
            easy_apply=True,
            match_score=80,
            complexity_score=35,
            complexity_level="Complex",
            freshness_score=50,
        )
        ranked = self.engine.apply_priority_scores([complex_job, simple])
        self.assertEqual(ranked[0].title, "Simple Data Analyst")
        self.assertGreater(ranked[0].priority_score, ranked[1].priority_score)

    def test_target_job_limit_is_15_to_20(self) -> None:
        self.assertEqual(self.engine.target_count(5), 15)
        self.assertEqual(self.engine.target_count(18), 18)
        self.assertEqual(self.engine.target_count(100), 20)


if __name__ == "__main__":
    unittest.main()
