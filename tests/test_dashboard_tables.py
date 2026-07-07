import unittest

import pandas as pd

from app.services.dashboard_tables import build_unique_display_frame


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


if __name__ == "__main__":
    unittest.main()
