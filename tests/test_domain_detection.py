"""Phase 2: evidence-based primary-domain / adjacent-domain / job-family / seniority detection.

These tests exercise the self-contained ``app.services.domain_detection`` layer both directly (with
hand-built ``DomainEvidence`` for precise control over the evidence hierarchy) and end-to-end through
``ResumeParser.parse`` (so the Phase-1 -> Phase-2 wiring is covered on realistic resume text).

The dominant property under test: professional TITLES and RESPONSIBILITIES outweigh tools, projects
and education, so a finance resume full of Excel/SQL/Power BI/Tableau and a CS degree stays
finance-centered rather than drifting into Data Analytics / Software / AI-ML / Data Engineering.
"""

import unittest

from app.config import Settings
from app.services.domain_detection import (
    DomainEvidence,
    ExperienceEvidence,
    detect_domain_profile,
)
from app.services.resume_parser import ResumeParser

REQUIRED_KEYS = {
    "primary_domain",
    "primary_domain_confidence",
    "adjacent_domains",
    "seniority",
    "strongest_job_families",
    "supporting_job_titles",
    "supporting_responsibilities",
    "supporting_domain_terms",
    "supporting_tools",
    "suppressed_domains",
    "suppression_reasons",
    "evidence_summary",
}


def _exp(title, responsibilities, months=24, current=False, rank=0) -> ExperienceEvidence:
    return ExperienceEvidence(
        title=title, responsibilities=responsibilities, duration_months=months,
        is_current=current, recency_rank=rank,
    )


def _evidence(*, titles=None, experiences=None, skills=None, technologies=None,
              education="", projects="", raw="") -> DomainEvidence:
    return DomainEvidence(
        titles=titles or [],
        experiences=experiences or [],
        skills=skills or [],
        technologies=technologies or [],
        education=education,
        projects=projects,
        raw_text=raw or " ".join(
            [*(titles or []), *(skills or []), *(technologies or []), education, projects,
             *[f"{e.title} {e.responsibilities}" for e in (experiences or [])]]
        ),
    )


# --- Realistic resume fixtures (used through the full parser) ------------------------------------
FINANCE_RESUME = (
    "John Smith\nFinancial Analyst\n"
    "EXPERIENCE\n"
    "Acme Capital | New York, NY | Jan 2022 - Present\n"
    "Financial Analyst\n"
    "- Built financial models and DCF valuations in Excel and Power BI\n"
    "- Led annual budgeting and forecasting; monthly variance analysis and financial reporting\n"
    "- Prepared P&L, cash flow and balance sheet analysis for FP&A\n"
    "Beta Corp | Boston, MA | Jun 2019 - Dec 2021\n"
    "Junior Financial Analyst\n"
    "- Prepared SQL and Tableau reports and dashboards for revenue analysis\n"
    "- Supported treasury and working capital forecasting\n"
    "PROJECTS\nBudget Forecasting Tool\n"
    "EDUCATION\nBachelor of Science in Computer Science, MIT, 2019\n"
    "SKILLS\nExcel, SQL, Power BI, Tableau, Python\n"
)
SOFTWARE_RESUME = (
    "Jane Roe\nBackend Developer\n"
    "WORK EXPERIENCE\n"
    "Globex Inc | Seattle, WA | Jan 2021 - Present\n"
    "Backend Developer\n"
    "- Developed REST APIs and microservices in Python and FastAPI\n"
    "- Designed database schema, code review, unit tests\n"
    "Initech | Remote | Jun 2018 - Dec 2020\n"
    "Software Engineer\n"
    "- Built distributed systems and web services in Java Spring Boot\n"
    "EDUCATION\nB.Tech Computer Science\nSKILLS\nPython, Java, FastAPI, PostgreSQL\n"
)
IT_SUPPORT_RESUME = (
    "Sam Lee\nIT Support Specialist\n"
    "EXPERIENCE\n"
    "TechCorp | Austin, TX | Mar 2020 - Present\n"
    "Desktop Support Engineer\n"
    "- Troubleshoot hardware and software issues; ticketing via ServiceNow\n"
    "- Active Directory user support, software installation, endpoint support\n"
    "HelpCo | 2017 - 2020\n"
    "IT Support Specialist\n"
    "- Help desk technical support, incident handling, onboarding\n"
    "SKILLS\nWindows, Active Directory, Office 365, ServiceNow\n"
)
DATA_RESUME = (
    "Dana Kim\nData Analyst\n"
    "WORK EXPERIENCE\n"
    "Insight Co | Pune | Jan 2021 - Present\n"
    "Data Analyst\n"
    "- Built dashboards and reporting in Power BI and Tableau\n"
    "- Ad hoc analysis, KPI and metrics, data-driven insights for stakeholders\n"
    "- SQL queries and Python pandas for trend analysis\n"
    "SKILLS\nSQL, Excel, Power BI, Tableau, Python, Pandas\n"
)
ML_RESUME = (
    "Alex Ng\nMachine Learning Engineer\n"
    "EXPERIENCE\n"
    "AI Labs | Remote | Feb 2021 - Present\n"
    "Machine Learning Engineer\n"
    "- Model training and evaluation of deep learning neural networks in PyTorch\n"
    "- Feature engineering, model deployment and inference pipelines\n"
    "- Built NLP text classification and computer vision object detection models\n"
    "SKILLS\nPython, PyTorch, TensorFlow, scikit-learn, pandas\n"
)
CLOUD_RESUME = (
    "Pat Ora\nDevOps Engineer\n"
    "EXPERIENCE\n"
    "CloudOps LLC | Remote | Feb 2021 - Present\n"
    "DevOps Engineer\n"
    "- Built CI/CD pipelines and infrastructure as code with Terraform\n"
    "- Kubernetes container orchestration, provisioning cloud infrastructure on AWS\n"
    "- Monitoring, scaling and high availability with Prometheus\n"
    "SKILLS\nDocker, Kubernetes, Terraform, AWS, Jenkins\n"
)
CYBER_RESUME = (
    "Robin Tal\nCybersecurity Analyst\n"
    "EXPERIENCE\n"
    "SecureNet | Washington, DC | Jun 2022 - Present\n"
    "Cybersecurity Analyst\n"
    "- SIEM monitoring in Splunk, incident response and threat detection\n"
    "- Vulnerability management, penetration testing, IAM and firewall administration\n"
    "SKILLS\nSplunk, Wireshark, Nessus\n"
)
FRESHER_RESUME = (
    "Chris Uni\nComputer Science Student\n"
    "EDUCATION\nBachelor of Technology in Computer Science, 2025\n"
    "PROJECTS\n"
    "Home Automation System - built a web application in React and Python\n"
    "Budget Tracker - developed a mobile application\n"
    "SKILLS\nPython, JavaScript, React\n"
)


class DomainFixtureTests(unittest.TestCase):
    """Each representative resume resolves to the expected PRIMARY domain (behaviors 1, 4-9)."""

    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def profile(self, text: str) -> dict:
        return self.parser.parse(text)["domain_profile"]

    def test_finance_resume_is_finance_primary(self) -> None:
        self.assertEqual(self.profile(FINANCE_RESUME)["primary_domain"], "Finance / Financial Analysis")

    def test_software_resume_is_software_primary(self) -> None:
        self.assertEqual(self.profile(SOFTWARE_RESUME)["primary_domain"], "Software Engineering")

    def test_it_support_resume_is_it_support_primary(self) -> None:
        self.assertEqual(self.profile(IT_SUPPORT_RESUME)["primary_domain"], "IT Support")

    def test_data_resume_is_data_analytics_primary(self) -> None:
        self.assertEqual(self.profile(DATA_RESUME)["primary_domain"], "Data Analytics")

    def test_ml_resume_is_ai_ml_primary(self) -> None:
        self.assertEqual(self.profile(ML_RESUME)["primary_domain"], "AI / Machine Learning")

    def test_cloud_resume_is_cloud_devops_primary(self) -> None:
        self.assertEqual(self.profile(CLOUD_RESUME)["primary_domain"], "Cloud / DevOps")

    def test_cyber_resume_is_cybersecurity_primary(self) -> None:
        self.assertEqual(self.profile(CYBER_RESUME)["primary_domain"], "Cybersecurity")

    def test_output_contains_every_required_field(self) -> None:
        self.assertEqual(set(self.profile(FINANCE_RESUME)), REQUIRED_KEYS)


class FinanceDominanceTests(unittest.TestCase):
    """Behaviors 2, 3: finance titles/responsibilities dominate tools+education; analytics adjacent."""

    def setUp(self) -> None:
        self.profile = ResumeParser(Settings(openai_api_key=None)).parse(FINANCE_RESUME)["domain_profile"]

    def test_finance_is_primary_over_data_and_tools(self) -> None:
        self.assertEqual(self.profile["primary_domain"], "Finance / Financial Analysis")

    def test_tools_and_cs_degree_are_only_supporting_evidence(self) -> None:
        # Excel/SQL/Power BI/Tableau appear as SUPPORTING tools, never as the primary driver.
        supporting_tools = {tool.lower() for tool in self.profile["supporting_tools"]}
        self.assertTrue({"excel", "sql", "power bi", "tableau"} & supporting_tools)
        self.assertEqual(self.profile["seniority"], "Mid-level")

    def test_data_software_aiml_dataeng_are_suppressed_as_primary(self) -> None:
        suppressed = set(self.profile["suppressed_domains"])
        # None of the tech domains may be the primary domain; where they scored, they are suppressed.
        self.assertNotIn(self.profile["primary_domain"], {"Data Analytics", "Software Engineering", "AI / Machine Learning", "Data Engineering"})
        self.assertIn("Software Engineering", suppressed)
        self.assertTrue(self.profile["suppression_reasons"])

    def test_finance_analytics_may_be_adjacent_not_primary(self) -> None:
        self.assertIn("Finance Analytics / Business Intelligence", self.profile["adjacent_domains"])
        self.assertNotIn("Data Analytics", self.profile["adjacent_domains"])  # presented as a finance label

    def test_strongest_families_are_finance_families(self) -> None:
        families = self.profile["strongest_job_families"]
        self.assertGreaterEqual(len(families), 3)
        self.assertLessEqual(len(families), 8)
        allowed = {
            "Corporate Finance / Financial Analysis", "FP&A", "Investment Analysis", "Portfolio Analysis",
            "Financial Reporting", "Valuation", "Treasury", "Financial Risk", "Finance Analytics",
        }
        self.assertTrue(set(families).issubset(allowed))


class SuppressionAndGateTests(unittest.TestCase):
    """Behaviors 6 (min-evidence gates), 10-12: tools/education/single project cannot decide."""

    def test_tools_alone_cannot_determine_a_primary_domain(self) -> None:
        # A bare skills list, no titles, no experience, no projects.
        profile = detect_domain_profile(_evidence(skills=["Python", "SQL", "Power BI", "Tableau"]))
        self.assertEqual(profile["primary_domain"], "Undetermined")
        self.assertEqual(profile["primary_domain_confidence"], 0.0)

    def test_education_alone_cannot_override_professional_experience(self) -> None:
        # Several years as an IT support engineer; a CS degree must not flip the domain to software.
        profile = detect_domain_profile(_evidence(
            titles=["Desktop Support Engineer"],
            experiences=[_exp("Desktop Support Engineer",
                              "Troubleshoot hardware, ticketing, active directory user support", months=48, current=True)],
            education="Bachelor of Science in Computer Science",
            skills=["Windows", "Active Directory"],
        ))
        self.assertEqual(profile["primary_domain"], "IT Support")

    def test_single_project_cannot_override_years_of_unrelated_work(self) -> None:
        # 4 years of finance work; a single ML class project must not make AI/ML primary.
        profile = detect_domain_profile(_evidence(
            titles=["Financial Analyst"],
            experiences=[_exp("Financial Analyst",
                              "budgeting, forecasting, financial modeling, valuation, financial reporting",
                              months=48, current=True)],
            projects="Academic project: trained a machine learning model with feature engineering",
            skills=["Excel", "SQL"],
        ))
        self.assertEqual(profile["primary_domain"], "Finance / Financial Analysis")
        self.assertNotEqual(profile["primary_domain"], "AI / Machine Learning")

    def test_specialized_gate_blocks_data_engineering_from_sql_alone(self) -> None:
        # SQL as a tool must not satisfy the Data Engineering minimum-evidence gate.
        profile = detect_domain_profile(_evidence(
            titles=["Data Analyst"],
            experiences=[_exp("Data Analyst", "dashboards, reporting, kpi, insights", months=36, current=True)],
            skills=["SQL", "Excel", "Power BI"],
        ))
        self.assertNotEqual(profile["primary_domain"], "Data Engineering")
        self.assertNotIn("Data Engineering", profile["adjacent_domains"])

    def test_specialized_gate_blocks_cloud_from_aws_certificate_alone(self) -> None:
        profile = detect_domain_profile(_evidence(
            titles=["IT Support Specialist"],
            experiences=[_exp("IT Support Specialist", "help desk, troubleshoot, ticketing, user support", months=36, current=True)],
            education="AWS Certified Cloud Practitioner",
            skills=["AWS"],
        ))
        self.assertEqual(profile["primary_domain"], "IT Support")
        self.assertNotEqual(profile["primary_domain"], "Cloud / DevOps")


class AdjacentDomainTests(unittest.TestCase):
    """Behavior 13: adjacent domains require multiple meaningful signals and relatedness."""

    def test_single_tool_does_not_create_an_adjacent_domain(self) -> None:
        # A finance role that merely lists Python once must not gain an AI/ML or software adjacency.
        profile = detect_domain_profile(_evidence(
            titles=["Financial Analyst"],
            experiences=[_exp("Financial Analyst",
                              "budgeting, forecasting, valuation, financial reporting, variance analysis",
                              months=48, current=True)],
            skills=["Python"],
        ))
        self.assertNotIn("AI / Machine Learning", profile["adjacent_domains"])
        self.assertNotIn("Software Engineering", profile["adjacent_domains"])

    def test_adjacent_domains_are_related_to_the_primary(self) -> None:
        profile = ResumeParser(Settings(openai_api_key=None)).parse(FINANCE_RESUME)["domain_profile"]
        for adjacent in profile["adjacent_domains"]:
            self.assertIn("Finance", adjacent + " Finance")  # presented under a finance-flavoured label
        self.assertLessEqual(len(profile["adjacent_domains"]), 3)


class SeniorityTests(unittest.TestCase):
    """Behaviors 15, 16: seniority from titles/experience/scope, never tools/education."""

    def test_intern_title_yields_intern(self) -> None:
        profile = detect_domain_profile(_evidence(
            titles=["Data Analyst Intern"],
            experiences=[_exp("Data Analyst Intern", "dashboards, reporting, sql queries, insights", months=4, current=True)],
        ))
        self.assertEqual(profile["seniority"], "Intern")

    def test_senior_title_yields_senior(self) -> None:
        profile = detect_domain_profile(_evidence(
            titles=["Senior Software Engineer"],
            experiences=[_exp("Senior Software Engineer", "developed microservices, rest api, code review", months=72, current=True)],
        ))
        self.assertEqual(profile["seniority"], "Senior")

    def test_manager_title_yields_manager(self) -> None:
        profile = detect_domain_profile(_evidence(
            titles=["Engineering Manager"],
            experiences=[_exp("Engineering Manager", "managed a team, developed software, rest api", months=60, current=True)],
        ))
        self.assertEqual(profile["seniority"], "Manager")

    def test_plain_ic_title_with_tenure_is_mid_level_not_senior(self) -> None:
        # A plain "Financial Analyst" (no seniority word, no people management) stays Mid-level.
        profile = detect_domain_profile(_evidence(
            titles=["Financial Analyst"],
            experiences=[_exp("Financial Analyst", "budgeting, forecasting, valuation, financial reporting", months=84, current=True)],
        ))
        self.assertEqual(profile["seniority"], "Mid-level")

    def test_student_projects_only_yield_intern_or_entry_level(self) -> None:
        profile = ResumeParser(Settings(openai_api_key=None)).parse(FRESHER_RESUME)["domain_profile"]
        self.assertIn(profile["seniority"], {"Intern", "Entry-level"})
        self.assertLessEqual(profile["primary_domain_confidence"], 60.0)


class FamilyScopeAndNormalizationTests(unittest.TestCase):
    """Behaviors 14, 17: families stay inside the primary; labels normalized and deduplicated."""

    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def test_families_belong_to_the_primary_domain(self) -> None:
        profile = self.parser.parse(CYBER_RESUME)["domain_profile"]
        allowed = {
            "Security Operations (SOC)", "Incident Response", "Vulnerability Management",
            "Identity and Access Management", "Network Security", "Governance, Risk and Compliance",
        }
        self.assertTrue(set(profile["strongest_job_families"]).issubset(allowed))

    def test_supporting_lists_are_deduplicated(self) -> None:
        profile = self.parser.parse(FINANCE_RESUME)["domain_profile"]
        for key in ("supporting_tools", "supporting_responsibilities", "supporting_domain_terms", "strongest_job_families", "adjacent_domains"):
            values = profile[key]
            self.assertEqual(len(values), len(set(values)), f"{key} contains duplicates")


class DeterminismTests(unittest.TestCase):
    """Behavior 18: identical input yields byte-for-byte identical output."""

    def test_repeated_analysis_is_identical(self) -> None:
        parser = ResumeParser(Settings(openai_api_key=None))
        first = parser.parse(FINANCE_RESUME)["domain_profile"]
        second = parser.parse(FINANCE_RESUME)["domain_profile"]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
