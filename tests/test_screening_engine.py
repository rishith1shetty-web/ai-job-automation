"""Phase 2 tests: portal-agnostic ScreeningAnswerEngine.

These exercise the shared engine directly (no Playwright / no DOM / no portal),
proving it is reusable by LinkedIn, Dice, and future portals while preserving
the existing decision pipeline and safe-refusal behaviour.
"""

import unittest

from app.config import Settings
from app.models import CandidateProfile, JobOpportunity, ResumeProfile
from app.services.screening_engine import (
    ScreeningAnswerEngine,
    ScreeningDecision,
    ScreeningQuestion,
)


def make_resume(**overrides) -> ResumeProfile:
    data = dict(
        filename="resume.txt",
        stored_path="resume.txt",
        raw_text="Data analyst with SQL and Excel.",
        skills=["SQL", "Excel"],
        technologies=[],
        education=[],
        experience=[],
        job_titles=[],
        keywords=[],
    )
    data.update(overrides)
    return ResumeProfile(**data)


def make_job() -> JobOpportunity:
    return JobOpportunity(
        resume_id=1,
        title="Analyst",
        company="Example",
        location="United States",
        job_url="https://example.com",
    )


class ScreeningAnswerEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = ScreeningAnswerEngine(Settings(openai_api_key=None))
        self.resume = make_resume()
        self.job = make_job()

    def _decide(self, question, category=None, *, options, field_type="radio", field_kind="radio", context, candidate_profile=None):
        return self.engine.decide(
            ScreeningQuestion(
                text=question,
                category=category,
                field_type=field_type,
                field_kind=field_kind,
                options=options,
                portal="generic",
            ),
            resume=self.resume,
            job=self.job,
            candidate_profile=candidate_profile,
            context=context,
        )

    # ---- Independence / interface ----
    def test_engine_has_no_playwright_or_portal_dependency(self) -> None:
        import app.services.screening_engine as mod

        text = open(mod.__file__, encoding="utf-8").read()
        import_lines = [
            line for line in text.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        ]
        # No imports from Playwright, portal adapters, or the LinkedIn module.
        for line in import_lines:
            lowered = line.lower()
            self.assertNotIn("playwright", lowered)
            self.assertNotIn("linkedin", lowered)
            self.assertNotIn("portals", lowered)
        # No DOM/browser interaction calls (structured data only).
        for dom_call in (".evaluate(", "page.", ".query_selector", ".locator(", "field.evaluate"):
            self.assertNotIn(dom_call, text, dom_call)

    # ---- Saved answers (exact/normalized) ----
    def test_saved_answer_is_reused(self) -> None:
        question = "Are you willing to relocate?"
        saved = {
            "normalized_question": self.engine.normalize_question(question),
            "answer": "Yes",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "category": "work_preference",
            "evidence": {"source": "candidate_saved"},
        }
        decision = self._decide(
            question,
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": [saved]},
        )
        self.assertIsInstance(decision, ScreeningDecision)
        self.assertFalse(decision.needs_review)
        self.assertEqual(decision.answer, "Yes")
        self.assertEqual(decision.decision_stage, "saved_answer")

    def test_fuzzy_saved_answer_matches_via_normalization(self) -> None:
        # Saved under the normalized alias; asked with different phrasing.
        saved = {
            "normalized_question": "work sponsorship",
            "answer": "No",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "category": "authorization",
            "evidence": {},
        }
        decision = self._decide(
            "Do you require sponsorship?",
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": [saved]},
        )
        self.assertFalse(decision.needs_review)
        self.assertEqual(decision.answer, "No")

    # ---- Confidence scoring ----
    def test_confidence_scoring_present(self) -> None:
        saved = {
            "normalized_question": self.engine.normalize_question("Are you open to relocation?"),
            "answer": "Yes",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "category": "work_preference",
            "evidence": {},
        }
        decision = self._decide(
            "Are you open to relocation?",
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": [saved]},
        )
        self.assertEqual(decision.confidence, "High")

    # ---- Safe legal refusal -> needs_review ----
    def test_legal_visa_question_without_saved_answer_needs_review(self) -> None:
        question = "Will you now, or in the future, require sponsorship for employment visa status?"
        decision = self._decide(
            question,
            options=["Yes", "No"],
            context={"mode": "aggressive", "saved_answers": []},
        )
        self.assertTrue(decision.needs_review)
        self.assertIsNone(decision.answer)
        self.assertEqual(decision.category, "authorization")

    def test_authorization_category_is_refused_even_in_aggressive_mode(self) -> None:
        # Authorization/legal never guessed: safe-refusal short-circuits the pipeline.
        decision = self._decide(
            "Are you legally authorized to work in the United States?",
            options=["Yes", "No"],
            context={"mode": "aggressive", "saved_answers": []},
        )
        self.assertTrue(decision.needs_review)

    # ---- Non-legal unknown -> AI-generated conservative answer (both modes) ----
    def test_unknown_nonlegal_yes_no_is_ai_answered(self) -> None:
        empty_resume = make_resume(raw_text="Data analyst.", skills=[], technologies=[])
        decision = self.engine.decide(
            ScreeningQuestion(
                text="Do you have E-Commerce experience?",
                field_type="select-one",
                field_kind="select",
                options=["Yes", "No"],
            ),
            resume=empty_resume,
            job=self.job,
            candidate_profile=None,
            context={"mode": "safe", "saved_answers": []},
        )
        # No e-commerce evidence -> honest AI-generated "No" (field is not left blank).
        self.assertFalse(decision.needs_review)
        self.assertEqual(decision.answer, "No")
        self.assertEqual(decision.decision_stage, "coverage")

    def test_legal_unknown_still_needs_review_in_safe_mode(self) -> None:
        # Legal/authorization is never fabricated even though non-legal is answered.
        decision = self.engine.decide(
            ScreeningQuestion(
                text="Are you authorized to work in the United States?",
                field_type="radio",
                field_kind="radio",
                options=["Yes", "No"],
            ),
            resume=make_resume(),
            job=self.job,
            candidate_profile=None,
            context={"mode": "safe", "saved_answers": []},
        )
        self.assertTrue(decision.needs_review)
        self.assertIsNone(decision.answer)

    # ---- Resume-evidence answer (skill) ----
    def test_skill_question_uses_resume_evidence(self) -> None:
        resume = make_resume(raw_text="Python and SQL developer.", skills=["SQL"], technologies=["Python"])
        decision = self.engine.decide(
            ScreeningQuestion(
                text="Do you have SQL experience?",
                category="skill",
                field_type="select-one",
                field_kind="select",
                options=["Yes", "No"],
            ),
            resume=resume,
            job=self.job,
            candidate_profile=None,
            context={"mode": "safe", "saved_answers": []},
        )
        self.assertFalse(decision.needs_review)
        self.assertEqual(decision.answer, "Yes")

    # ---- Categorisation ----
    def test_categorize_maps_expected_types(self) -> None:
        cases = {
            "How many years of experience do you have with SQL?": "skill_years",
            "Are you authorized to work in the United States?": "authorization",
            "Are you willing to relocate?": "work_preference",
            "What is your expected salary?": "salary",
            "Have you completed a Bachelor's Degree?": "education",
        }
        for question, expected in cases.items():
            self.assertEqual(self.engine.categorize(question), expected, question)

    # ---- Structured metadata for logging ----
    def test_decision_exposes_structured_metadata(self) -> None:
        saved = {
            "normalized_question": self.engine.normalize_question("Are you willing to relocate?"),
            "answer": "Yes",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "category": "work_preference",
            "evidence": {"source": "candidate_saved"},
        }
        decision = self._decide(
            "Are you willing to relocate?",
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": [saved]},
        )
        meta = decision.to_metadata()
        for key in ("answer", "confidence", "answer_source", "reasoning", "category", "matched_profile_field", "matched_resume_evidence", "decision_pipeline"):
            self.assertIn(key, meta)
        self.assertEqual(meta["answer_source"], "Candidate Knowledge Base")

    def test_to_event_preserves_legacy_shape(self) -> None:
        saved = {
            "normalized_question": self.engine.normalize_question("Are you willing to relocate?"),
            "answer": "Yes",
            "source": "Candidate Knowledge Base",
            "confidence": "High",
            "category": "work_preference",
            "evidence": {},
        }
        decision = self._decide(
            "Are you willing to relocate?",
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": [saved]},
        )
        event = decision.to_event()
        # Legacy consumers rely on these keys.
        for key in ("answer", "selected_answer", "source", "confidence", "reason", "evidence", "decision_stage"):
            self.assertIn(key, event)
        self.assertEqual(event["selected_answer"], "Yes")

    def test_needs_review_decision_returns_none_event(self) -> None:
        decision = self._decide(
            "Will you require visa sponsorship?",
            options=["Yes", "No"],
            context={"mode": "safe", "saved_answers": []},
        )
        self.assertTrue(decision.needs_review)
        self.assertIsNone(decision.to_event())


if __name__ == "__main__":
    unittest.main()
