"""Phase 1: robust, domain-agnostic professional job-title extraction.

Titles come primarily from the parsed WORK-EXPERIENCE STRUCTURE (any domain), with the old
tech-title whitelist kept only as a supplement/fallback. Seniority/role abbreviations are
normalized; projects, degrees, certifications, tools, employers, locations and section headings
are never treated as titles.
"""

import unittest

from app.config import Settings
from app.services.resume_parser import ResumeParser


class TitleExtractionAcrossDomainsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def titles(self, text: str) -> list[str]:
        return self.parser._extract_job_titles(text)

    # 1. The reported failure: a Financial Analyst resume must not report zero titles.
    def test_financial_analyst_and_junior_are_extracted_and_normalized(self) -> None:
        text = (
            "Experience\n"
            "Acme Capital | New York, NY | Jan 2022 - Present\n"
            "Financial Analyst\n"
            "- Built financial models in Excel and Power BI\n"
            "Beta Corp | Boston, MA | Jun 2019 - Dec 2021\n"
            "Jr. Financial Analyst\n"
            "- Prepared SQL reports\n"
            "Education\n"
            "Bachelor of Science in Finance\n"
        )
        titles = self.titles(text)
        self.assertIn("Financial Analyst", titles)
        self.assertIn("Junior Financial Analyst", titles)          # Jr. -> Junior
        self.assertNotIn("Jr. Financial Analyst", titles)

    def test_software_engineer_and_backend_developer(self) -> None:
        text = (
            "Work Experience\n"
            "Globex Inc | Seattle, WA | Jan 2021 - Present\n"
            "Backend Developer\n"
            "- Python microservices\n"
            "Initech | Remote | 2018 - 2020\n"
            "Software Engineer\n"
            "- Java services\n"
        )
        titles = self.titles(text)
        self.assertIn("Backend Developer", titles)
        self.assertIn("Software Engineer", titles)

    def test_it_support_titles(self) -> None:
        text = (
            "Experience\n"
            "Desktop Support Engineer | TechCorp | Austin, TX | Mar 2020 - Present\n"
            "- Windows, Active Directory\n"
            "IT Support Specialist | HelpCo | 2017 - 2020\n"
            "- Ticketing\n"
        )
        titles = self.titles(text)
        self.assertIn("Desktop Support Engineer", titles)
        self.assertIn("IT Support Specialist", titles)

    def test_cloud_and_devops_titles(self) -> None:
        text = (
            "Professional Experience\n"
            "CloudOps LLC | Remote | Feb 2021 - Present\n"
            "Cloud Engineer\n"
            "- AWS, Terraform\n"
            "Pipeline Inc | NYC | 2019 - 2021\n"
            "DevOps Engineer\n"
            "- CI/CD, Kubernetes\n"
        )
        titles = self.titles(text)
        self.assertIn("Cloud Engineer", titles)
        self.assertIn("DevOps Engineer", titles)

    def test_cybersecurity_analyst_title(self) -> None:
        text = (
            "Experience\n"
            "SecureNet | Washington, DC | Jun 2022 - Present\n"
            "Cybersecurity Analyst\n"
            "- SIEM, incident response\n"
        )
        self.assertIn("Cybersecurity Analyst", self.titles(text))

    def test_data_analyst_title(self) -> None:
        text = (
            "Work Experience\n"
            "Data Analyst Intern | Ven Analytics Pvt. Ltd. | Pune, India | Jan 2025 - Apr 2025\n"
            "- SQL dashboards\n"
        )
        self.assertIn("Data Analyst Intern", self.titles(text))

    def test_seniority_abbreviations_normalize(self) -> None:
        self.assertEqual(self.parser._normalize_job_title("Jr. Financial Analyst"), "Junior Financial Analyst")
        self.assertEqual(self.parser._normalize_job_title("Sr Financial Analyst"), "Senior Financial Analyst")
        self.assertEqual(self.parser._normalize_job_title("Software Eng."), "Software Engineer")
        # Full titles are left untouched (no meaningful distinction lost).
        self.assertEqual(self.parser._normalize_job_title("Software Engineer"), "Software Engineer")
        self.assertEqual(self.parser._normalize_job_title("AI/ML Engineer"), "AI/ML Engineer")


class EmploymentStructureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def test_current_versus_previous_roles_are_distinguished(self) -> None:
        text = (
            "Experience\n"
            "Acme Capital | New York, NY | Jan 2022 - Present\n"
            "Financial Analyst\n"
            "- models\n"
            "Beta Corp | Boston, MA | Jun 2019 - Dec 2021\n"
            "Junior Financial Analyst\n"
            "- reports\n"
        )
        records = self.parser.extract_employment_history(text)
        self.assertEqual(len(records), 2)
        current = [r for r in records if r["currently_work_here"]]
        previous = [r for r in records if not r["currently_work_here"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["job_title"], "Financial Analyst")
        self.assertEqual(previous[0]["job_title"], "Junior Financial Analyst")

    def test_dates_company_location_and_duration_are_captured(self) -> None:
        text = (
            "Experience\n"
            "Acme Capital | New York, NY | Jan 2022 - Present\n"
            "Financial Analyst\n"
            "- models\n"
        )
        record = self.parser.extract_employment_history(text)[0]
        self.assertEqual(record["job_title"], "Financial Analyst")
        self.assertEqual(record["company_name"], "Acme Capital")
        self.assertEqual(record["city"], "New York")
        self.assertEqual(record["start_month"], "January")
        self.assertEqual(record["start_year"], 2022)
        self.assertTrue(record["currently_work_here"])
        self.assertIsNotNone(record["duration_months"])
        self.assertGreater(record["duration_months"], 0)


class NonTitleRejectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def test_a_resume_full_of_distractors_yields_only_the_real_title(self) -> None:
        text = (
            "Jane Doe\n"
            "Cloud Engineer\n"
            "EXPERIENCE\n"
            "Nimbus Cloud Solutions | Denver, CO | Jan 2022 - Present\n"
            "Cloud Engineer\n"
            "- Project: Migration Accelerator\n"
            "- Built AWS Terraform pipelines\n"
            "PROJECTS\n"
            "Budget Forecasting Tool\n"
            "EDUCATION\n"
            "Bachelor of Technology in Computer Science, MIT, 2018\n"
            "CERTIFICATIONS\n"
            "AWS Certified Solutions Architect\n"
            "CFA Level 1\n"
            "TECHNICAL SKILLS\n"
            "Python, SQL, Power BI, Tableau, Kubernetes\n"
        )
        titles = self.parser._extract_job_titles(text)
        self.assertEqual(titles, ["Cloud Engineer"])

    def test_none_of_these_are_recognised_as_job_titles(self) -> None:
        for value in (
            "Budget Forecasting Tool",                       # project
            "Home Automation System",                        # project
            "Bachelor of Technology in Computer Science",    # degree
            "Master of Science in Data Science",             # degree
            "AWS Certified Solutions Architect",             # certification
            "CFA Level 1",                                    # certification
            "Python", "SQL", "Power BI", "Tableau",          # tools
            "Nimbus Cloud Solutions",                        # employer
            "Denver, CO",                                    # location
            "Projects", "Education", "Certifications", "Technical Skills",  # headings
        ):
            with self.subTest(value=value):
                self.assertFalse(self.parser._looks_like_job_title(value))


NETWORK_RESUME = (
    "Rahul Nair\n"
    "Network Engineer\n"
    "\n"
    "PROFESSIONAL SUMMARY\n"
    "Network engineer with 5 years of experience in enterprise routing and switching,\n"
    "LAN/WAN design, firewall administration and network troubleshooting.\n"
    "\n"
    "PROFESSIONAL EXPERIENCE\n"
    "TechNova Systems | Pune, India\n"
    "Network Engineer\n"
    "Jan 2022 - Present\n"
    "- Configured and maintained Cisco routers and switches across a multi-site WAN\n"
    "- Managed BGP and OSPF routing, VLANs and network segmentation\n"
    "- Administered Palo Alto firewalls and VPN tunnels\n"
    "\n"
    "GlobalLink Networks | Mumbai, India\n"
    "Junior Network Engineer\n"
    "Jun 2020 - Dec 2021\n"
    "- Monitored network performance using SolarWinds and Wireshark\n"
    "- Supported LAN/WAN infrastructure and resolved connectivity incidents\n"
    "\n"
    "BrightEdge IT | Mumbai, India\n"
    "Network Support Engineer Intern\n"
    "Jan 2020 - May 2020\n"
    "- Assisted with switch configuration and structured cabling\n"
    "\n"
    "TECHNICAL SKILLS\n"
    "Cisco, BGP, OSPF, VLAN, Palo Alto, VPN, Wireshark, SolarWinds, TCP/IP\n"
    "\n"
    "EDUCATION\n"
    "B.E. Electronics and Telecommunication\n"
)


class SectionHeadingNeverBecomesTitleTests(unittest.TestCase):
    """Regression: a resume whose entry header spans two lines ('Company | Location' then the
    title) used to have the header COLLAPSED into one string, gluing the location onto the title
    ("Pune, India Network Engineer") -- and the same collapse could turn a "Professional Summary"
    heading plus its first sentence into a bogus title. Headings are also rejected outright."""

    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def test_network_resume_yields_exactly_the_three_real_titles(self) -> None:
        self.assertEqual(
            self.parser._extract_job_titles(NETWORK_RESUME),
            ["Network Engineer", "Junior Network Engineer", "Network Support Engineer Intern"],
        )

    def test_location_is_never_glued_onto_the_title(self) -> None:
        for title in self.parser._extract_job_titles(NETWORK_RESUME):
            self.assertNotIn("Pune", title)
            self.assertNotIn("Mumbai", title)
            self.assertNotIn("India", title)

    def test_company_and_location_are_still_captured_separately(self) -> None:
        records = self.parser.extract_employment_history(NETWORK_RESUME)
        self.assertEqual(
            [(r["job_title"], r["company_name"], r["city"]) for r in records],
            [
                ("Network Engineer", "TechNova Systems", "Pune"),
                ("Junior Network Engineer", "GlobalLink Networks", "Mumbai"),
                ("Network Support Engineer Intern", "BrightEdge IT", "Mumbai"),
            ],
        )

    def test_current_and_previous_roles_are_distinguished(self) -> None:
        records = self.parser.extract_employment_history(NETWORK_RESUME)
        self.assertTrue(records[0]["currently_work_here"])
        self.assertFalse(records[1]["currently_work_here"])
        self.assertFalse(records[2]["currently_work_here"])

    def test_section_headings_are_never_job_titles(self) -> None:
        for heading in (
            "Professional Summary", "PROFESSIONAL SUMMARY", "Summary", "Profile",
            "Career Summary", "Technical Skills", "Professional Experience", "Experience",
            "Education", "Certifications", "Projects", "Academic Projects",
            "Work History", "Core Competencies", "Areas of Expertise", "Declaration",
        ):
            with self.subTest(heading=heading):
                self.assertTrue(self.parser._is_non_title(heading))
                self.assertFalse(self.parser._looks_like_job_title(heading))

    def test_headings_with_trailing_punctuation_are_still_rejected(self) -> None:
        for heading in ("Professional Summary:", "Academic Projects  ", "- Career Summary"):
            with self.subTest(heading=heading):
                self.assertTrue(self.parser._is_non_title(heading))

    def test_valid_titles_containing_heading_words_are_not_blocked(self) -> None:
        # Exact-line matching only: a real title that merely CONTAINS a heading word survives.
        for title in (
            "Summary Analyst", "Projects Engineer", "Education Program Manager",
            "Profile Analyst", "Experience Designer", "Skills Development Specialist",
        ):
            with self.subTest(title=title):
                self.assertFalse(self.parser._is_non_title(title))
                self.assertTrue(self.parser._looks_like_job_title(title))

    def test_structurally_valid_summary_analyst_is_extracted(self) -> None:
        text = (
            "PROFESSIONAL SUMMARY\n"
            "Analyst with reporting experience.\n"
            "EXPERIENCE\n"
            "Ledger Corp | Austin, TX\n"
            "Summary Analyst\n"
            "Feb 2021 - Present\n"
            "- Produced executive summary reporting packs\n"
        )
        self.assertIn("Summary Analyst", self.parser._extract_job_titles(text))


class HeaderLayoutRegressionTests(unittest.TestCase):
    """The supported entry-header layouts must all keep working after the line-wise fix."""

    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))

    def _first(self, text: str) -> tuple:
        record = self.parser.extract_employment_history(text)[0]
        return record["job_title"], record["company_name"], record["city"]

    def test_company_pipe_location_then_title_then_dates(self) -> None:
        text = (
            "EXPERIENCE\nTechNova Systems | Pune, India\nNetwork Engineer\nJan 2022 - Present\n"
            "- Managed BGP and OSPF routing\n"
        )
        self.assertEqual(self._first(text), ("Network Engineer", "TechNova Systems", "Pune"))

    def test_title_then_company_pipe_location(self) -> None:
        text = (
            "EXPERIENCE\nNetwork Engineer\nTechNova Systems | Pune, India\nJan 2022 - Present\n"
            "- Managed BGP and OSPF routing\n"
        )
        self.assertEqual(self._first(text), ("Network Engineer", "TechNova Systems", "Pune"))

    def test_single_line_company_pipe_location_pipe_dates_then_title(self) -> None:
        text = (
            "EXPERIENCE\nAcme Capital | New York, NY | Jan 2022 - Present\nFinancial Analyst\n"
            "- Built financial models\n"
        )
        self.assertEqual(self._first(text), ("Financial Analyst", "Acme Capital", "New York"))

    def test_single_line_title_pipe_company(self) -> None:
        text = (
            "EXPERIENCE\nNetwork Engineer | TechNova Systems\nJan 2022 - Present\n"
            "- Managed BGP and OSPF routing\n"
        )
        title, company, _ = self._first(text)
        self.assertEqual((title, company), ("Network Engineer", "TechNova Systems"))


class NetworkResumeAnalysisTests(unittest.TestCase):
    """End-to-end: the corrected titles drive Networking domain detection and a network-focused
    role set (no unsupported Cloud/Data/Security padding)."""

    def setUp(self) -> None:
        self.parser = ResumeParser(Settings(openai_api_key=None))
        self.profile = self.parser.parse(NETWORK_RESUME)

    def test_detected_role_is_the_real_current_title(self) -> None:
        self.assertEqual(self.profile["detected_role"], "Network Engineer")
        self.assertNotIn("Summary", self.profile["detected_role"])

    def test_domain_is_networking(self) -> None:
        self.assertEqual(self.profile["domain_profile"]["primary_domain"], "Networking")

    def test_roles_are_network_focused_and_multiple(self) -> None:
        roles = [role["title"] for role in self.profile["role_profile"]["roles"]]
        self.assertGreater(len(roles), 1, "only one suggested role was produced")
        self.assertEqual(roles[0], "Network Engineer")
        self.assertEqual(len(roles), len(set(roles)), "duplicate roles were suggested")
        # Every suggested role is a networking role -- nothing unrelated was added to pad the list.
        self.assertTrue(all("Network" in title or "Infrastructure" in title or "NOC" in title
                            for title in roles), roles)

    def test_unsupported_roles_are_not_added_to_reach_twelve(self) -> None:
        roles = [role["title"] for role in self.profile["role_profile"]["roles"]]
        # The resume shows no cloud evidence, so the taxonomy's Cloud Network Engineer is withheld
        # rather than padded in. Honest under-fill is required, not a forced 12.
        self.assertNotIn("Cloud Network Engineer", roles)
        for unrelated in ("Data Analyst", "Software Engineer", "IT Support Specialist",
                          "Cybersecurity Analyst", "Cloud Engineer"):
            self.assertNotIn(unrelated, roles)


if __name__ == "__main__":
    unittest.main()
