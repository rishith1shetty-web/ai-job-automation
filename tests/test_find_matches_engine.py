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
        self.assertLessEqual(len(queries), 24)
        self.assertEqual(queries[0].pass_name, "primary_role_primary_location")
        # Phase 4: the plan is role_profile-driven -- one narrow query per ranked role, and the
        # display label is the single primary role (never a broad OR of unrelated roles).
        self.assertEqual(plan["role_source"], "regenerated")
        self.assertEqual(plan["display_query"], queries[0].role)
        self.assertNotIn(" OR ", plan["display_query"])

    def test_all_detected_roles_generate_search_variations_in_priority_order(self) -> None:
        self.resume.raw_text = ""
        self.resume.skills = []
        self.resume.technologies = []
        self.resume.keywords = []
        self.resume.domain_expertise = []
        self.resume.experience = []
        self.resume.detected_role = None
        self.resume.role_suggestions = [
            {"role": "Data Scientist", "confidence": 86},
            {"role": "Machine Learning Engineer", "confidence": 82},
            {"role": "Analytics Engineer", "confidence": 72},
            {"role": "Business Intelligence Analyst", "confidence": 58},
        ]

        queries, plan = self.engine.build_search_plan(self.resume, "United States", None)
        first_role_queries = [query for query in queries if query.pass_name.endswith("_role_primary_location")]

        self.assertEqual(first_role_queries[0].query, "Data Scientist")
        self.assertEqual(first_role_queries[0].role_priority, "primary")
        self.assertEqual(first_role_queries[1].query, "Machine Learning Engineer")
        self.assertEqual(first_role_queries[1].role_priority, "strong_fit")
        self.assertTrue(any(query.query == "Analytics Engineer" and query.role_priority == "strong_fit" for query in first_role_queries))
        self.assertTrue(
            any(query.query == "Business Intelligence Analyst" and query.role_priority == "stretch" for query in first_role_queries)
        )
        self.assertEqual([item["role"] for item in plan["role_groups"][:4]], [
            "Data Scientist",
            "Machine Learning Engineer",
            "Analytics Engineer",
            "Business Intelligence Analyst",
        ])

    def test_or_query_is_fallback_not_primary_search(self) -> None:
        queries, plan = self.engine.build_search_plan(self.resume, "United States", None)

        # Phase 4: every variation searches ONE canonical role and the display label is a single
        # role -- a broad "A OR B OR C" query is never produced for either portal.
        self.assertNotIn(" OR ", queries[0].query)
        self.assertNotIn(" OR ", plan["display_query"])
        self.assertFalse(any(" OR " in query.query for query in queries))

    def test_related_role_jobs_are_included_with_source_metadata(self) -> None:
        variation = next(
            query
            for query in self.engine.build_search_plan(self.resume, "United States", None)[0]
            if query.role_priority in {"related", "stretch"}
        )

        enriched = self.engine.enrich_raw_job(
            {
                "title": "Analytics Engineer",
                "company": "Example",
                "location": "Remote",
                "description": "SQL analytics pipelines",
                "job_url": "https://www.linkedin.com/jobs/view/999",
                "easy_apply": True,
            },
            variation,
        )

        self.assertEqual(enriched["search_query_used"], variation.query)
        self.assertEqual(enriched["search_role"], variation.search_role)
        self.assertEqual(enriched["role_priority"], variation.role_priority)

    def test_dice_enrichment_preserves_dice_form_provider_label(self) -> None:
        variation = self.engine.build_search_plan(self.resume, "Remote", None)[0][0]

        enriched = self.engine.enrich_raw_job(
            {
                "portal": "dice",
                "title": "Data Analyst",
                "company": "Dice Analytics",
                "location": "Remote",
                "description": "Quick Apply. Python SQL Excel.",
                "job_url": "https://www.dice.com/job-detail/123",
                "easy_apply": True,
                "form_provider": "dice",
            },
            variation,
        )

        self.assertEqual(enriched["form_provider"], "Dice Easy Apply")
        self.assertNotEqual(enriched["form_provider"], "LinkedIn Easy Apply")

    def test_top_ranking_favors_stronger_role_matches_when_other_scores_are_close(self) -> None:
        primary = JobOpportunity(
            resume_id=1,
            title="Primary Role Job",
            company="A",
            location="Remote",
            job_url="https://example.com/primary",
            description="SQL",
            easy_apply=True,
            match_score=75,
            complexity_score=90,
            completion_likelihood_score=90,
            freshness_score=75,
            role_priority="primary",
        )
        stretch = JobOpportunity(
            resume_id=1,
            title="Stretch Role Job",
            company="B",
            location="Remote",
            job_url="https://example.com/stretch",
            description="SQL",
            easy_apply=True,
            match_score=76,
            complexity_score=90,
            completion_likelihood_score=90,
            freshness_score=75,
            role_priority="stretch",
        )

        ranked = self.engine.apply_priority_scores([stretch, primary])

        self.assertEqual(ranked[0], primary)

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

    def test_fewer_screening_questions_rank_higher_when_match_is_similar(self) -> None:
        simple_data = self.engine.estimate_complexity("Data Analyst", "Easy Apply. SQL and Excel.")
        complex_data = self.engine.estimate_complexity(
            "Data Analyst",
            "Easy Apply. Required screening questions: years of SQL, years of Excel, explain why, salary, background check.",
        )
        simple = JobOpportunity(
            resume_id=1,
            title="Simple Analyst",
            company="A",
            location="Remote",
            job_url="https://www.linkedin.com/jobs/view/1",
            description="SQL",
            easy_apply=True,
            match_score=78,
            freshness_score=70,
            **simple_data,
        )
        complex_job = JobOpportunity(
            resume_id=1,
            title="Screening Heavy Analyst",
            company="B",
            location="Remote",
            job_url="https://www.linkedin.com/jobs/view/2",
            description="SQL",
            easy_apply=True,
            match_score=79,
            freshness_score=70,
            **complex_data,
        )

        ranked = self.engine.apply_priority_scores([complex_job, simple])

        self.assertEqual(ranked[0].title, "Simple Analyst")
        self.assertLess(ranked[0].application_friction_score, ranked[1].application_friction_score)

    def test_complex_form_provider_ranks_lower(self) -> None:
        linked = self.engine.estimate_complexity("Data Analyst", "Easy Apply. Resume required.", "https://www.linkedin.com/jobs/view/3", True)
        workable = self.engine.estimate_complexity("Data Analyst", "Apply via Workable with custom questions.", "https://apply.workable.com/acme/j/123", False)

        self.assertEqual(workable["form_provider"], "Workable")
        self.assertGreater(workable["application_friction_score"], linked["application_friction_score"])
        self.assertLess(workable["completion_likelihood_score"], linked["completion_likelihood_score"])

    def test_cover_letter_required_job_ranks_lower(self) -> None:
        simple = self.engine.estimate_complexity("Data Analyst", "Easy Apply. Resume required.")
        cover = self.engine.estimate_complexity("Data Analyst", "Easy Apply. Resume and cover letter required. Explain why you want this role.")

        self.assertGreater(cover["application_friction_score"], simple["application_friction_score"])
        self.assertIn("cover letter may be required", cover["friction_reasons"])

    def test_unknown_provider_custom_form_ranks_lower(self) -> None:
        native = self.engine.estimate_complexity("Data Analyst", "Easy Apply. Resume required.", "https://www.linkedin.com/jobs/view/4", True)
        custom = self.engine.estimate_complexity("Data Analyst", "Custom application form.", "https://jobs.example.com/apply/4", False)

        self.assertEqual(custom["form_provider"], "Custom/Unknown")
        self.assertGreater(custom["external_form_risk"], native["external_form_risk"])
        self.assertLess(custom["completion_likelihood_score"], native["completion_likelihood_score"])

    def test_high_match_high_friction_does_not_always_beat_medium_match_low_friction(self) -> None:
        low_friction = self.engine.estimate_complexity("Data Analyst", "Easy Apply. Resume required.")
        high_friction = self.engine.estimate_complexity(
            "Data Scientist",
            "Greenhouse application. Cover letter, LinkedIn profile URL, salary, visa sponsorship, background check, "
            "employment history, essay questions, and custom required fields.",
            "https://boards.greenhouse.io/acme/jobs/55",
            False,
        )
        medium_match = JobOpportunity(
            resume_id=1,
            title="Medium Match Simple",
            company="A",
            location="Remote",
            job_url="https://www.linkedin.com/jobs/view/5",
            description="SQL",
            easy_apply=True,
            match_score=72,
            freshness_score=70,
            **low_friction,
        )
        high_match = JobOpportunity(
            resume_id=1,
            title="High Match Complex",
            company="B",
            location="Remote",
            job_url="https://boards.greenhouse.io/acme/jobs/55",
            description="SQL",
            easy_apply=False,
            match_score=95,
            freshness_score=70,
            **high_friction,
        )

        ranked = self.engine.apply_priority_scores([high_match, medium_match])

        self.assertEqual(ranked[0].title, "Medium Match Simple")
        self.assertGreater(ranked[0].priority_score, ranked[1].priority_score)

    def test_target_job_limit_supports_configurable_100_default(self) -> None:
        self.assertEqual(self.engine.target_count(0), 100)
        self.assertEqual(self.engine.target_count(20), 20)
        self.assertEqual(self.engine.target_count(50), 50)
        self.assertEqual(self.engine.target_count(100), 100)
        self.assertEqual(self.engine.target_count(150), 100)

    def test_collection_plan_scales_for_100_results(self) -> None:
        plan = self.engine.collection_plan(100, 18)

        self.assertGreaterEqual(plan["selected_variation_count"], 14)
        self.assertGreaterEqual(plan["limit_per_query"], 8)

    def test_friction_affects_ranking_but_does_not_remove_jobs(self) -> None:
        simple = JobOpportunity(
            resume_id=1,
            title="Simple Easy Apply",
            company="A",
            location="Remote",
            job_url="https://www.linkedin.com/jobs/view/simple",
            description="SQL analyst",
            easy_apply=True,
            match_score=75,
            complexity_score=90,
            completion_likelihood_score=92,
            application_friction_score=5,
            estimated_steps=2,
            freshness_score=70,
        )
        complex_job = JobOpportunity(
            resume_id=1,
            title="Complex External Form",
            company="B",
            location="Remote",
            job_url="https://boards.greenhouse.io/example/jobs/complex",
            description="SQL analyst",
            easy_apply=False,
            match_score=95,
            complexity_score=35,
            completion_likelihood_score=30,
            application_friction_score=85,
            estimated_steps=8,
            freshness_score=70,
        )

        ranked = self.engine.apply_priority_scores([complex_job, simple])

        self.assertEqual(len(ranked), 2)
        self.assertIn(complex_job, ranked)
        self.assertEqual(ranked[0], simple)


if __name__ == "__main__":
    unittest.main()
