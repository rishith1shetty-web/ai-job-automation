import unittest

import json

import pandas as pd

from app.services.dashboard_tables import build_unique_display_frame, safe_dataframe_for_streamlit


class DashboardTableFormattingTests(unittest.TestCase):
    def test_duplicate_source_columns_do_not_create_duplicate_display_names(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "title": "Data Analyst",
                    "company": "Example",
                    "form_provider": "LinkedIn",
                    "job_url": "https://www.linkedin.com/jobs/view/123",
                }
            ]
        )
        display = build_unique_display_frame(
            frame,
            ["title", "company", "form_provider", "job_url", "form_provider", "job_url"],
            {
                "title": "Title",
                "company": "Company",
                "form_provider": "Form Provider",
                "job_url": "Job URL",
            },
        )

        self.assertEqual(list(display.columns), ["Title", "Company", "Form Provider", "Job URL"])
        self.assertEqual(len(display.columns), len(set(display.columns)))
        self.assertIn("Job URL", display.columns)

    def test_display_name_collision_is_renamed_clearly(self) -> None:
        frame = pd.DataFrame([{"url": "https://example.com", "job_url": "https://linkedin.com/jobs/view/1"}])
        display = build_unique_display_frame(
            frame,
            ["url", "job_url"],
            {"url": "Job URL", "job_url": "Job URL"},
        )

        self.assertEqual(list(display.columns), ["Job URL", "Job URL (2)"])
        self.assertEqual(len(display.columns), len(set(display.columns)))

    def test_safe_dataframe_serializes_mixed_object_columns(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "job_title": "Data Analyst",
                    "evidence_sources": [{"question": "SQL", "answer": "1"}],
                    "screening_answers": ["Yes", "No"],
                    "metadata": {"provider": "Dice"},
                    "evidence": {"skills": ["SQL"]},
                    "reasoning": {"source": "resume"},
                    "plain_text": "ok",
                    "score": 85,
                },
                {
                    "job_title": "Data Engineer",
                    "evidence_sources": "manual note",
                    "screening_answers": None,
                    "metadata": {"provider": "LinkedIn"},
                    "evidence": set(["Python"]),
                    "reasoning": "already text",
                    "plain_text": "still ok",
                    "score": 90,
                },
            ]
        )

        safe = safe_dataframe_for_streamlit(frame)

        self.assertEqual(safe["score"].dtype, frame["score"].dtype)
        for column in ["evidence_sources", "screening_answers", "metadata", "evidence", "reasoning"]:
            self.assertTrue(all(isinstance(value, str) for value in safe[column]))
        self.assertEqual(json.loads(safe.loc[0, "metadata"]), {"provider": "Dice"})
        self.assertEqual(safe.loc[1, "screening_answers"], "")


if __name__ == "__main__":
    unittest.main()
