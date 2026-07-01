import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.reasoning import AIReasoningEngine
from app.services.resume_intelligence import ResumeIntelligenceEngine


def resume() -> ResumeProfile:
    return ResumeProfile(
        filename="candidate.pdf",
        stored_path="candidate.pdf",
        raw_text=(
            "Data Analyst with SQL, Python, Power BI, Excel, dashboards, KPIs, "
            "healthcare analytics, and stakeholder presentations. Built a Power BI "
            "dashboard project using patient records and SQL reporting."
        ),
        skills=["SQL", "Python", "Power BI", "Excel"],
        technologies=["Pandas"],
        domain_expertise=["Healthcare Analytics"],
        experience=[
            {
                "job_title": "Data Analyst",
                "description": "Built dashboards and SQL reports for healthcare stakeholders.",
                "skills_used": ["SQL", "Power BI", "Excel"],
            }
        ],
        education=[{"degree": "Bachelor of Engineering"}],
        job_titles=["Data Analyst"],
        keywords=["dashboard", "analytics"],
    )


def job() -> JobOpportunity:
    return JobOpportunity(
        id=1,
        resume_id=1,
        title="Data Analyst",
        company="Health Insights",
        location="India",
        job_url="https://example.com/job",
        description=(
            "We need a Data Analyst for healthcare analytics, SQL reporting, "
            "Power BI dashboards, Excel, stakeholder communication, and AWS."
        ),
        match_score=72,
        priority_score=80,
    )


class ResumeIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(openai_api_key=None)
        self.engine = ResumeIntelligenceEngine(self.settings, AIReasoningEngine(self.settings))
        self.resume = resume()
        self.job = job()

    def test_explainable_ranking_generates_match_components(self) -> None:
        result = self.engine.analyze_job(self.resume, self.job)

        self.assertGreater(result.overall_match, 0)
        self.assertGreater(result.technical_match, 0)
        self.assertGreater(result.semantic_match, 0)
        self.assertIn("sql", result.matched_skills)
        self.assertIn("aws", result.missing_skills)

    def test_match_explanation_contains_strengths_weaknesses_and_recommendations(self) -> None:
        result = self.engine.analyze_job(self.resume, self.job)

        self.assertTrue(result.strengths)
        self.assertTrue(result.weaknesses)
        self.assertTrue(result.recommendations)
        self.assertIn("overall match", result.overall_summary.lower())

    def test_cover_letter_is_company_and_role_aware(self) -> None:
        result = self.engine.analyze_job(
            self.resume,
            self.job,
            CandidateProfile(first_name="Jhanvi", last_name="Patel"),
        )

        self.assertIn("Health Insights", result.cover_letter)
        self.assertIn("Data Analyst", result.cover_letter)
        self.assertIn("Jhanvi Patel", result.cover_letter)

    def test_generated_answer_is_grounded_and_role_specific(self) -> None:
        answer = self.engine.generate_answer(
            "Describe your Power BI experience.",
            self.resume,
            self.job,
        )

        self.assertIn("power bi", answer["answer"].lower())
        self.assertGreater(answer["grounding_score"], 40)
        self.assertIn("Resume", answer["source_list"])

    def test_resume_suggestions_explain_why_they_help(self) -> None:
        result = self.engine.analyze_job(self.resume, self.job)

        self.assertTrue(result.resume_suggestions)
        self.assertTrue(all(item.get("why_it_helps") for item in result.resume_suggestions))

    def test_no_hallucinated_experience_when_evidence_is_weak(self) -> None:
        weak_resume = ResumeProfile(
            filename="weak.txt",
            stored_path="weak.txt",
            raw_text="Customer support background.",
            skills=[],
            technologies=[],
            domain_expertise=[],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
        )

        answer = self.engine.generate_answer("Describe your Kubernetes experience.", weak_resume, self.job)

        self.assertIn("need more candidate-specific evidence", answer["answer"])
        self.assertEqual(answer["confidence"], "Low")

    def test_apply_to_job_stores_intelligence_fields(self) -> None:
        result = self.engine.analyze_job(self.resume, self.job)
        self.engine.apply_to_job(self.job, result)

        self.assertEqual(self.job.confidence_score, result.confidence)
        self.assertTrue(self.job.overall_match_summary)
        self.assertTrue(self.job.generated_cover_letter)
        self.assertTrue(self.job.resume_improvement_suggestions)

    def test_debug_files_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(openai_api_key=None, resume_intelligence_debug_dir=Path(temporary))
            engine = ResumeIntelligenceEngine(settings)
            result = engine.analyze_job(self.resume, self.job)
            debug_dir = engine.create_debug_dir()
            engine.write_debug(
                debug_dir,
                [
                    {
                        "job": {"title": self.job.title, "company": self.job.company},
                        "result": result.__dict__,
                    }
                ],
            )

            self.assertTrue((debug_dir / "ranking_analysis.json").exists())
            self.assertTrue((debug_dir / "cover_letter.json").exists())
            self.assertTrue((debug_dir / "resume_suggestions.json").exists())


if __name__ == "__main__":
    unittest.main()
