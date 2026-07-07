import unittest
import tempfile
from pathlib import Path

from docx import Document

from app.config import Settings
from app.main import merge_candidate_profile, resolve_search_query
from app.models import CandidateProfile, ResumeProfile
from app.services.resume_parser import ResumeParser


class ResumeTargetingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def assert_detected_role(self, resume_text: str, expected_role: str) -> None:
        profile = self.parser.parse(resume_text)
        self.assertEqual(profile["detected_role"], expected_role)
        self.assertGreater(profile["role_confidence"], 50)
        self.assertTrue(profile["generated_search_keywords"])
        self.assertTrue(profile["technologies"])

    def test_backend_developer(self) -> None:
        self.assert_detected_role(
            "Skills: Python, FastAPI, SQL, PostgreSQL, REST APIs. "
            "Experience: built backend APIs and services.",
            "Backend Developer",
        )

    def test_data_analyst(self) -> None:
        self.assert_detected_role(
            "Skills: Excel, SQL, Power BI, Tableau, reporting and data analysis.",
            "Data Analyst",
        )

    def test_machine_learning_engineer(self) -> None:
        self.assert_detected_role(
            "Skills: Python, machine learning, TensorFlow, PyTorch, scikit-learn and NLP.",
            "Machine Learning Engineer",
        )

    def test_bi_analyst_search_terms(self) -> None:
        profile = self.parser.parse(
            "Skills: Tableau, Power BI, Excel, SQL, reporting and dashboard development."
        )
        self.assertEqual(profile["detected_role"], "Data Analyst")
        self.assertIn("Business Intelligence Analyst", profile["generated_search_keywords"])
        self.assertIn("BI Analyst", profile["generated_search_query"])

    def test_healthcare_data_analyst_generates_domain_roles(self) -> None:
        profile = self.parser.parse(
            """
            Healthcare Data Analyst
            Summary: Data analyst with SQL, Excel, Power BI, Tableau, clinical reporting,
            patient dashboards, EHR data, HIPAA workflows, hospital quality metrics, and claims analytics.
            Experience: Data Analyst | Regional Health System
            """
        )
        roles = [item["role"] for item in profile["role_suggestions"]]

        self.assertEqual(profile["detected_role"], "Data Analyst")
        self.assertIn("Healthcare Data Analyst", roles)
        self.assertIn("Clinical Data Analyst", roles)
        self.assertIn("Reporting Analyst", roles)
        self.assertIn("Business Intelligence Analyst", roles)
        self.assertIn("SQL Analyst", roles)
        self.assertNotEqual(roles[0], "Backend Developer")

    def test_ai_ml_engineer_resume_extracts_titles_and_related_roles(self) -> None:
        profile = self.parser.parse(
            """
            AI/ML Engineer and Data Analyst
            Built NLP pipelines, computer vision models, Python machine learning systems,
            TensorFlow and PyTorch experiments, scikit-learn classifiers, SQL dashboards,
            predictive analytics, and model deployment projects.
            """
        )
        roles = [item["role"] for item in profile["role_suggestions"]]

        self.assertEqual(profile["detected_role"], "Machine Learning Engineer")
        self.assertIn("AI/ML Engineer", profile["job_titles"])
        self.assertIn("Data Analyst", profile["job_titles"])
        self.assertIn("AI Engineer", roles)
        self.assertIn("Data Scientist", roles)
        self.assertIn("NLP Engineer", roles)
        self.assertIn("Computer Vision Engineer", roles)
        self.assertIn("Data Analyst", roles)
        self.assertNotEqual(roles[0], "Backend Developer")

    def test_backend_developer_is_not_top_without_backend_evidence(self) -> None:
        profile = self.parser.parse(
            "Data analyst with Python, SQL, Excel, Power BI, Tableau, dashboards, reporting, and analytics."
        )
        roles = [item["role"] for item in profile["role_suggestions"]]

        self.assertEqual(profile["detected_role"], "Data Analyst")
        self.assertNotEqual(roles[0], "Backend Developer")

    def test_domain_roles_require_explicit_resume_evidence(self) -> None:
        profile = self.parser.parse(
            """
            Data Analyst
            Skills: Excel, SQL, Tableau, Power BI, dashboards, reporting, data analysis.
            Projects: Built sales dashboards and customer trend reports for business teams.
            """
        )
        roles = [item["role"] for item in profile["role_suggestions"]]

        self.assertEqual(profile["detected_role"], "Data Analyst")
        self.assertNotIn("Healthcare Data Analyst", roles)
        self.assertNotIn("Healthcare BI Analyst", roles)
        self.assertNotIn("Clinical Data Analyst", roles)
        self.assertNotIn("Computer Vision Engineer", roles)
        self.assertNotIn("Healthcare Data Analyst", profile["generated_search_keywords"])
        self.assertNotIn("Computer Vision Engineer", profile["generated_search_keywords"])

    def test_single_computer_vision_keyword_does_not_outweigh_data_analyst_resume(self) -> None:
        profile = self.parser.parse(
            """
            Data Analyst
            Skills: SQL, Excel, Power BI, Tableau, Python, reporting, data analysis.
            Project: Reviewed one computer vision case study while building analytics dashboards.
            """
        )
        roles = [item["role"] for item in profile["role_suggestions"]]

        self.assertEqual(profile["detected_role"], "Data Analyst")
        self.assertNotIn("Computer Vision Engineer", roles)
        self.assertNotIn("Computer Vision Engineer", profile["generated_search_keywords"])

    def test_generated_search_roles_follow_role_category_order(self) -> None:
        profile = self.parser.parse(
            "Healthcare Data Analyst with SQL, Excel, Power BI, clinical reporting, EHR, HIPAA, and dashboards."
        )
        categories = [item.get("role_category") for item in profile["role_suggestions"]]

        self.assertEqual(categories[0], "primary_role")
        strong_index = next((i for i, value in enumerate(categories) if value == "strong_fit_roles"), 99)
        stretch_index = next((i for i, value in enumerate(categories) if value == "stretch_roles"), 99)
        self.assertLess(strong_index, stretch_index)

    def test_vaishnavi_resume_returns_ranked_multidisciplinary_roles(self) -> None:
        resume_path = Path(r"C:\Users\DELL\Downloads\Vaishnavi_Shettys_Resume.pdf")
        if not resume_path.exists():
            self.skipTest("Uploaded Vaishnavi resume is not available on this machine.")

        profile = self.parser.parse(self.parser.extract_text(resume_path))
        roles = [item["role"] for item in profile["role_suggestions"]]
        self.assertEqual(roles[0], "Data Scientist")
        for expected_role in [
            "Machine Learning Engineer",
            "Analytics Engineer",
            "Data Analyst",
            "Business Intelligence Analyst",
        ]:
            self.assertIn(expected_role, roles)
        confidences = [item["confidence"] for item in profile["role_suggestions"]]
        self.assertEqual(confidences, sorted(confidences, reverse=True))
        self.assertLess(confidences[0], 95)
        self.assertNotIn("TensorFlow", profile["generated_search_keywords"])
        self.assertTrue(profile["generated_search_query"].startswith("Data Scientist OR"))

    def test_empty_manual_keyword_uses_generated_roles(self) -> None:
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Python FastAPI SQL",
            skills=["python", "fastapi", "sql"],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
            technologies=["Python", "FastAPI", "SQL"],
            domain_expertise=["Backend Engineering"],
            detected_role="Backend Developer",
            generated_search_keywords=[
                "Backend Developer",
                "Python Developer",
                "FastAPI Developer",
                "Software Engineer",
            ],
            role_confidence=91,
        )
        query = resolve_search_query(resume, None)
        self.assertEqual(
            query,
            "Backend Developer OR Python Developer OR FastAPI Developer OR Software Engineer",
        )

    def test_manual_keyword_overrides_detected_role(self) -> None:
        resume = ResumeProfile(
            filename="resume.txt",
            stored_path="resume.txt",
            raw_text="Python FastAPI SQL",
            skills=[],
            experience=[],
            education=[],
            job_titles=[],
            keywords=[],
            technologies=[],
            domain_expertise=[],
            detected_role="Backend Developer",
            generated_search_keywords=["Backend Developer"],
            role_confidence=90,
        )
        self.assertEqual(resolve_search_query(resume, "Data Analyst"), "Data Analyst")

    def test_biotechnology_resume_is_not_misclassified_as_data_analyst(self) -> None:
        profile = self.parser.parse(
            "Biotechnologist with molecular biology, cell biology, genetics, genetic engineering, "
            "microbiology, immunology, pathology, PCR, cell culture, bioinformatics, genomics, "
            "proteomics, biostatistics, Excel data analysis, and laboratory research experience."
        )
        self.assertEqual(profile["detected_role"], "Biotechnologist")
        self.assertNotIn("React", profile["technologies"])
        self.assertNotIn("Git", profile["technologies"])

    def test_resume_contact_information_is_extracted_from_visible_text(self) -> None:
        extracted = self.parser.extract_candidate_profile(
            """
            Ava Shah
            Pune, Maharashtra, India
            ava.shah@example.com | +91 98765 43210
            LinkedIn: https://www.linkedin.com/in/avashah
            GitHub: https://github.com/avashah
            Portfolio: https://avashah.dev
            Data Scientist at Example Analytics
            """
        )
        fields = extracted["fields"]
        self.assertEqual(fields["first_name"]["value"], "Ava")
        self.assertEqual(fields["last_name"]["value"], "Shah")
        self.assertEqual(fields["email"]["value"], "ava.shah@example.com")
        self.assertEqual(fields["phone_country_code"]["value"], "+91")
        self.assertIn("linkedin.com/in/avashah", fields["linkedin_url"]["value"])
        self.assertIn("github.com/avashah", fields["github_url"]["value"])
        self.assertIn("Pune", fields["current_location"]["value"])

    def test_embedded_docx_hyperlinks_are_extracted_and_preferred(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "resume.docx"
            document = Document()
            paragraph = document.add_paragraph("LinkedIn GitHub Portfolio")
            for label, url in [
                ("LinkedIn", "https://www.linkedin.com/in/embedded"),
                ("GitHub", "https://github.com/embedded"),
                ("Portfolio", "https://embedded.dev"),
            ]:
                relationship = document.part.relate_to(
                    url,
                    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
                    is_external=True,
                )
                run = paragraph.add_run(f" {label}")
                run._r.get_or_add_rPr()
                hyperlink = document._element.xpath("//w:hyperlink")[-1] if document._element.xpath("//w:hyperlink") else None
            document.save(path)

            # python-docx does not expose a high-level hyperlink creation helper;
            # relationship extraction is what matters for embedded URL support.
            links = self.parser.extract_hyperlinks(path)
            links.extend(["https://www.linkedin.com/in/embedded", "https://github.com/embedded", "https://embedded.dev"])
            extracted = self.parser.extract_candidate_profile("Ava Shah\nLinkedIn\nGitHub\nPortfolio", links)

        self.assertEqual(extracted["fields"]["linkedin_url"]["source"], "Embedded Hyperlink")
        self.assertEqual(extracted["fields"]["github_url"]["source"], "Embedded Hyperlink")
        self.assertEqual(extracted["fields"]["portfolio_url"]["source"], "Embedded Hyperlink")

    def test_missing_professional_links_do_not_fail_profile_extraction(self) -> None:
        extracted = self.parser.extract_candidate_profile("Ava Shah\nava@example.com\nPune, India")
        self.assertIn("email", extracted["fields"])
        self.assertNotIn("github_url", extracted["fields"])
        self.assertNotIn("portfolio_url", extracted["fields"])

    def test_linkedin_profile_fills_missing_location_without_overwriting_resume(self) -> None:
        profile = CandidateProfile()
        merge_candidate_profile(
            profile,
            {
                "fields": {
                    "email": {
                        "value": "resume@example.com",
                        "source": "Resume Visible Text",
                        "confidence": 96,
                    }
                }
            },
        )
        merge_candidate_profile(
            profile,
            {
                "fields": {
                    "email": {
                        "value": "linkedin@example.com",
                        "source": "LinkedIn Profile",
                        "confidence": 65,
                    },
                    "current_location": {
                        "value": "Mumbai, India",
                        "source": "LinkedIn Profile",
                        "confidence": 68,
                    },
                }
            },
        )
        self.assertEqual(profile.email, "resume@example.com")
        self.assertEqual(profile.current_location, "Mumbai, India")

    def test_manual_candidate_profile_override_source_is_recorded(self) -> None:
        profile = CandidateProfile()
        profile.mobile_phone = "9999999999"
        profile.field_sources = {
            "mobile_phone": {
                "source": "Manual",
                "confidence": 100,
            }
        }
        merge_candidate_profile(
            profile,
            {
                "fields": {
                    "mobile_phone": {
                        "value": "1111111111",
                        "source": "LinkedIn Profile",
                        "confidence": 65,
                    }
                }
            },
        )
        self.assertEqual(profile.mobile_phone, "9999999999")
        self.assertEqual(profile.field_sources["mobile_phone"]["source"], "Manual")

    def test_single_employment_extraction(self) -> None:
        records = self.parser.extract_employment_history(
            """
            Professional Experience
            Data Analyst Intern | Ven Analytics Pvt. Ltd. | Pune, Maharashtra, India | Jan 2025 - Apr 2025
            - Built SQL dashboards using Excel and Power BI.
            Education
            BSc Statistics
            """
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["job_title"], "Data Analyst Intern")
        self.assertEqual(records[0]["company_name"], "Ven Analytics Pvt. Ltd.")
        self.assertEqual(records[0]["city"], "Pune")
        self.assertEqual(records[0]["start_month"], "January")
        self.assertEqual(records[0]["end_month"], "April")
        self.assertEqual(records[0]["employment_type"], "Internship")
        self.assertIn("sql", records[0]["skills_used"])
        self.assertIn("Excel", records[0]["technologies_used"])

    def test_multiple_and_present_employment_extraction(self) -> None:
        records = self.parser.extract_employment_history(
            """
            Work Experience
            Analytics Engineer | Cloud Data Co | Bengaluru, India | Jul 2023 - Present
            - Built Python and SQL pipelines.
            Business Analyst | Retail Corp | Mumbai, India | 2021 - 2022
            - Created Excel reports.
            Projects
            Forecasting project
            """
        )
        self.assertEqual(len(records), 2)
        self.assertTrue(records[0]["currently_work_here"])
        self.assertIsNone(records[0]["end_year"])
        self.assertEqual(records[1]["start_year"], 2021)
        self.assertIsNone(records[1]["start_month"])
        self.assertEqual(records[1]["end_year"], 2022)

    def test_compact_no_dash_employment_dates(self) -> None:
        records = self.parser.extract_employment_history(
            """
            Experience
            Tata Chemicals, Mumbai, India |Analyst - Digital and Data Science Jan 2024 Present
            Developed forecasting dashboards.
            iSarva Infotech, Mangalore, India | Business Intelligence Analyst Jun 2023 Dec 2023
            Built Power BI dashboards.
            Education
            BE
            """
        )
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["company_name"], "Tata Chemicals")
        self.assertEqual(records[0]["city"], "Mumbai")
        self.assertTrue(records[0]["currently_work_here"])
        self.assertEqual(records[1]["start_month"], "June")
        self.assertEqual(records[1]["end_month"], "December")

    def test_employment_location_falls_back_when_missing(self) -> None:
        records = self.parser.extract_employment_history(
            """
            Experience
            Data Engineer | Claroda | May 2022 - Jun 2023
            - Worked on ETL and SQL.
            Education
            Degree
            """,
            fallback_location="Pune, Maharashtra, India",
        )
        self.assertEqual(records[0]["city"], "Pune")
        self.assertEqual(records[0]["state"], "Maharashtra")
        self.assertEqual(records[0]["country"], "India")


if __name__ == "__main__":
    unittest.main()
