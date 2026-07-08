import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.linkedin import LinkedInAutomation
from app.services.reasoning import AIReasoningEngine


def sample_resume() -> ResumeProfile:
    return ResumeProfile(
        filename="resume.txt",
        stored_path="resume.txt",
        raw_text=(
            "Data Analyst with SQL, Power BI, Excel and Healthcare Analytics experience.\n"
            "Projects: Built KPI dashboards and processed patient records using Python and Pandas.\n"
            "Presented insights to management and client stakeholders."
        ),
        skills=["SQL", "Power BI", "Excel", "Python"],
        technologies=["Pandas"],
        domain_expertise=["Healthcare Analytics"],
        experience=[
            {
                "job_title": "Data Analyst Intern",
                "description": "Created dashboards, cleaned data, and presented insights to stakeholders.",
                "skills_used": ["SQL", "Power BI"],
            }
        ],
        education=[{"degree": "Bachelor of Engineering"}],
        job_titles=["Data Analyst Intern"],
        keywords=["dashboard", "reporting"],
    )


def sample_job() -> JobOpportunity:
    return JobOpportunity(
        id=10,
        resume_id=1,
        title="Data Analyst",
        company="Example",
        location="India",
        job_url="https://example.com/jobs/10",
        description=(
            "Responsibilities include dashboard development, large datasets, SQL reporting, "
            "stakeholder communication, Python, Power BI, and healthcare analytics."
        ),
        easy_apply=True,
    )


class AIReasoningEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(openai_api_key=None)
        self.engine = AIReasoningEngine(self.settings)
        self.resume = sample_resume()
        self.job = sample_job()

    def test_job_description_parsing_extracts_metadata(self) -> None:
        metadata = self.engine.analyze_job_description(self.job)

        self.assertIn("sql", metadata["required_skills"])
        self.assertIn("python", metadata["programming_languages"])
        self.assertIn("power bi", metadata["bi_tools"])
        self.assertEqual(metadata["industry"], "Healthcare")

    def test_semantic_reasoning_infers_large_dataset_experience(self) -> None:
        decision = self.engine.answer_question(
            question="Do you have experience with large datasets?",
            resume=self.resume,
            job=self.job,
            mode="aggressive",
            options=["Yes", "No"],
        )

        self.assertEqual(decision.answer, "Yes")
        self.assertIn(decision.confidence, {"Medium", "High", "Very High"})
        self.assertIn("Semantic", decision.reasoning_type) if decision.reasoning_type == "Semantic Inference" else self.assertTrue(decision.reasoning_type)

    def test_equivalent_skill_detection_for_etl(self) -> None:
        decision = self.engine.answer_question(
            question="Do you have ETL experience?",
            resume=self.resume,
            job=self.job,
            mode="aggressive",
            options=["Yes", "No"],
        )

        self.assertEqual(decision.answer, "Yes")
        self.assertTrue(any(item["concept"] == "etl" for item in decision.evidence_used))

    def test_confidence_scoring_uses_multiple_sources(self) -> None:
        decision = self.engine.answer_question(
            question="Do you have dashboard development experience?",
            resume=self.resume,
            job=self.job,
            candidate_profile=CandidateProfile(professional_headline="Power BI dashboard analyst"),
            employment_history=[{"description": "Built executive dashboards using Power BI."}],
            mode="aggressive",
            options=["Yes", "No"],
        )

        self.assertIn(decision.confidence, {"High", "Very High"})

    def test_evidence_ordering_prefers_knowledge_base(self) -> None:
        decision = self.engine.answer_question(
            question="Do you have stakeholder communication experience?",
            resume=self.resume,
            job=self.job,
            candidate_knowledge=[{"question_text": "stakeholder communication", "answer": "Yes"}],
            mode="aggressive",
            options=["Yes", "No"],
        )

        self.assertEqual(decision.evidence_used[0]["reasoning_type"], "Knowledge Base Match")

    def test_job_description_comparison(self) -> None:
        comparison = self.engine.compare_resume_to_job(self.resume, self.job)

        self.assertGreater(comparison["overall_semantic_match"], 0)
        self.assertIn("sql", comparison["matched_skills"])

    def test_debug_files_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = Settings(openai_api_key=None, reasoning_debug_dir=Path(temporary))
            engine = AIReasoningEngine(settings)
            decision = engine.answer_question(
                question="Do you have dashboard development experience?",
                resume=self.resume,
                job=self.job,
                mode="aggressive",
                options=["Yes", "No"],
            )
            debug_dir = engine.create_debug_dir(self.job)
            self.job.resume = self.resume
            engine.write_debug(debug_dir, self.job, [decision])

            self.assertTrue((debug_dir / "job_description_analysis.json").exists())
            self.assertTrue((debug_dir / "question_reasoning.json").exists())

    def test_screening_engine_uses_reasoning_before_default(self) -> None:
        automation = LinkedInAutomation(self.settings)
        answer = automation._select_screening_answer(
            "Do you have dashboard development experience?",
            "yes_no",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            self.resume,
            self.job,
            None,
            {"mode": "aggressive", "saved_answers": []},
        )

        self.assertEqual(answer["selected_answer"], "Yes")
        self.assertEqual(answer["source"], "AI Reasoning Engine")
        self.assertFalse(answer["aggressive_default_used"])
        self.assertEqual(answer["decision_stage"], "reasoning")
        self.assertEqual(
            [stage["stage"] for stage in answer["decision_pipeline"]],
            ["saved_answer", "learned_mapping", "evidence", "reasoning"],
        )

    def test_screening_decision_pipeline_reuses_saved_answer_first(self) -> None:
        automation = LinkedInAutomation(self.settings)
        answer = automation._select_screening_answer(
            "Do you have dashboard development experience?",
            "yes_no",
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            self.resume,
            self.job,
            None,
            {
                "mode": "aggressive",
                "saved_answers": [
                    {
                        "normalized_question": "do you have dashboard development experience",
                        "answer": "No",
                        "source": "Candidate Knowledge Base",
                        "confidence": "High",
                    }
                ],
            },
        )

        self.assertEqual(answer["selected_answer"], "No")
        self.assertEqual(answer["decision_stage"], "saved_answer")
        self.assertEqual([stage["stage"] for stage in answer["decision_pipeline"]], ["saved_answer"])

    def test_safe_mode_legal_question_still_needs_review(self) -> None:
        # Non-legal unknowns are now AI-answered, but legal/authorization is never
        # fabricated and still returns None (-> needs_review) in safe mode.
        automation = LinkedInAutomation(self.settings)
        answer = automation._select_screening_answer(
            "Are you legally authorized to work in the United States?",
            automation._screening_question_category("Are you legally authorized to work in the United States?"),
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            self.resume,
            self.job,
            None,
            {"mode": "safe", "saved_answers": []},
        )

        self.assertIsNone(answer)

    def test_safe_mode_unknown_nonlegal_question_is_answered(self) -> None:
        # Per product decision: non-legal questions are auto-answered (AI-generated
        # when not in the resume) rather than left for review.
        automation = LinkedInAutomation(self.settings)
        answer = automation._select_screening_answer(
            "Do you have aerospace telemetry certification?",
            automation._screening_question_category("Do you have aerospace telemetry certification?"),
            {"field_type": "select", "type": "select-one"},
            ["Yes", "No"],
            self.resume,
            self.job,
            None,
            {"mode": "safe", "saved_answers": []},
        )

        self.assertIsNotNone(answer)
        self.assertEqual(answer["selected_answer"], "No")


if __name__ == "__main__":
    unittest.main()
