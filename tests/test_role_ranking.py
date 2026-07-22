"""Phase 3: deterministic top-12 role ranking + evidence-based confidence.

Roles are built from the Phase-1 structured resume and the Phase-2 domain profile via the
self-contained ``app.services.role_ranking`` layer. The dominant properties under test: exactly 12
unique roles in 1/4/4/3 categories drawn mainly from the primary domain; specialised roles gated on
real professional/project evidence; unsupported cross-domain roles excluded; rare professions handled
by a provisional fallback instead of being forced into a supported domain; deterministic output.
"""

import unittest

from app.config import Settings
from app.services.domain_detection import DomainEvidence, ExperienceEvidence
from app.services.resume_parser import ResumeParser
from app.services.role_ranking import (
    ROLE_TAXONOMY,
    RoleDef,
    _score_role,
    build_role_profile,
)
from tests.test_domain_detection import (
    CLOUD_RESUME,
    CYBER_RESUME,
    DATA_RESUME,
    FINANCE_RESUME,
    FRESHER_RESUME,
    IT_SUPPORT_RESUME,
    ML_RESUME,
    SOFTWARE_RESUME,
)

SUPPORTED_KEYS = {
    "taxonomy_status", "specialization", "primary_role", "primary_role_confidence", "roles",
    "suppressed_roles", "unmatched_terms", "suggested_taxonomy_additions",
}
CATEGORY_COUNTS = {"Primary Role": 1, "Strong Fit": 4, "Related": 4, "Stretch": 3}

FINANCE_EXCLUDED = {
    "Data Scientist", "Machine Learning Engineer", "ML Engineer", "Data Engineer",
    "Analytics Engineer", "Python Developer", "Software Engineer", "Backend Developer",
}

# Rare / unsupported-domain resumes (professions outside the 19-domain taxonomy).
ACTUARY_RESUME = (
    "Pat Roe\nActuary\nEXPERIENCE\n"
    "AssureCo | Hartford, CT | Jan 2020 - Present\nActuary\n"
    "- Actuarial pricing, reserving and loss modeling for insurance products\n"
    "- Mortality and morbidity assumptions; solvency and premium adequacy analysis\n"
    "Prior Insurance | 2017 - 2020\nActuarial Analyst\n"
    "- Reserving triangles and experience studies\n"
    "SKILLS\nExcel, SQL, R\n"
)
PARALEGAL_RESUME = (
    "Sam Fox\nParalegal\nEXPERIENCE\n"
    "Legal LLP | Chicago, IL | Mar 2019 - Present\nParalegal\n"
    "- Legal research, drafting pleadings, managing case files and court filings\n"
    "- Litigation support, discovery, deposition preparation and contract review\n"
    "SKILLS\nMicrosoft Office, Westlaw\n"
)
MECHANICAL_RESUME = (
    "Lee Ono\nMechanical Engineer\nEXPERIENCE\n"
    "MakeWorks | Detroit, MI | Jan 2019 - Present\nMechanical Engineer\n"
    "- Designed mechanical components and assemblies in SolidWorks and AutoCAD\n"
    "- Thermodynamics, tolerance analysis, GD&T, manufacturing and prototyping\n"
    "SKILLS\nSolidWorks, AutoCAD, MATLAB\n"
)
UX_RESUME = (
    "Dev Ux\nUX Designer\nEXPERIENCE\n"
    "PixelCo | Remote | Feb 2020 - Present\nUX Designer\n"
    "- User research, wireframes, prototypes and usability testing\n"
    "- Interaction design and design systems in Figma\n"
    "SKILLS\nFigma, Sketch, Adobe XD\n"
)
SALES_RESUME = (
    "Chris Sal\nSales Executive\nEXPERIENCE\n"
    "SellCorp | NYC | Jan 2018 - Present\nSales Executive\n"
    "- Managed sales pipeline, quota attainment and client negotiations\n"
    "- Prospecting, account management and closing enterprise deals\n"
    "SKILLS\nSalesforce, HubSpot\n"
)

SUPPORTED_DOMAINS = {
    "Finance / Financial Analysis", "Investment and Portfolio Management", "Accounting",
    "Software Engineering", "Web Development", "Data Analytics", "Data Engineering",
    "AI / Machine Learning", "Cloud / DevOps", "Cybersecurity", "IT Support", "Networking",
    "Business Analysis", "Product Management", "Project Management", "Operations", "Marketing",
    "Human Resources", "Healthcare Technology",
}


def _profile(text: str) -> dict:
    return ResumeParser(Settings(openai_api_key=None)).parse(text)["role_profile"]


def _titles(role_profile: dict) -> list[str]:
    return [role["title"] for role in role_profile["roles"]]


class TaxonomySelfSufficiencyTests(unittest.TestCase):
    def test_all_19_domains_present_with_at_least_12_roles(self) -> None:
        self.assertEqual(set(ROLE_TAXONOMY), SUPPORTED_DOMAINS)
        for domain, roles in ROLE_TAXONOMY.items():
            self.assertGreaterEqual(len(roles), 12, f"{domain} has fewer than 12 canonical roles")

    def test_no_duplicate_role_titles_within_a_domain(self) -> None:
        for domain, roles in ROLE_TAXONOMY.items():
            titles = [role.title for role in roles]
            self.assertEqual(len(titles), len(set(titles)), f"{domain} has duplicate role titles")


class Structure12Tests(unittest.TestCase):
    """Behaviors 1, 2, 12, 14 and the output contract."""

    def setUp(self) -> None:
        self.profile = _profile(FINANCE_RESUME)

    def test_exactly_twelve_unique_roles(self) -> None:
        titles = _titles(self.profile)
        self.assertEqual(len(titles), 12)
        self.assertEqual(len(titles), len(set(titles)))  # no duplicates

    def test_category_counts_are_one_four_four_three(self) -> None:
        counts: dict[str, int] = {}
        for role in self.profile["roles"]:
            counts[role["category"]] = counts.get(role["category"], 0) + 1
        self.assertEqual(counts, CATEGORY_COUNTS)

    def test_primary_and_strong_fit_come_from_the_primary_domain(self) -> None:
        primary_domain = "Finance / Financial Analysis"
        top_five = [r for r in self.profile["roles"] if r["category"] in {"Primary Role", "Strong Fit"}]
        self.assertEqual(len(top_five), 5)
        for role in top_five:
            self.assertEqual(role["domain"], primary_domain)

    def test_output_contract_keys_present(self) -> None:
        self.assertEqual(set(self.profile), SUPPORTED_KEYS)
        self.assertEqual(self.profile["taxonomy_status"], "supported")
        for role in self.profile["roles"]:
            self.assertEqual(
                set(role),
                {"rank", "title", "category", "confidence", "domain", "job_family",
                 "seniority_fit", "evidence", "missing_evidence", "penalties"},
            )


class FinanceRankingTests(unittest.TestCase):
    """Behaviors 3, 4, 5."""

    def setUp(self) -> None:
        self.profile = _profile(FINANCE_RESUME)

    def test_financial_analyst_ranks_first(self) -> None:
        self.assertEqual(self.profile["primary_role"], "Financial Analyst")
        self.assertEqual(self.profile["roles"][0]["title"], "Financial Analyst")
        self.assertGreaterEqual(self.profile["primary_role_confidence"], 90.0)

    def test_top_five_are_finance_centered(self) -> None:
        top_five = _titles(self.profile)[:5]
        for title in top_five:
            self.assertTrue(
                any(word in title for word in ("Financial", "Finance", "FP&A", "Treasury", "Investment", "Valuation", "Portfolio", "Revenue")),
                f"{title} is not finance-centered",
            )

    def test_unsupported_technical_roles_are_excluded(self) -> None:
        self.assertFalse(FINANCE_EXCLUDED & set(_titles(self.profile)))

    def test_confidence_is_monotonic_non_increasing(self) -> None:
        confidences = [role["confidence"] for role in self.profile["roles"]]
        self.assertEqual(confidences, sorted(confidences, reverse=True))


class MultiDomainTests(unittest.TestCase):
    """Behaviors 6, 7, 8, 9 across the major domains."""

    def _assert_primary(self, text: str, expected_domain: str, expected_role_hint: tuple[str, ...]) -> dict:
        profile = _profile(text)
        self.assertEqual(len(profile["roles"]), 12)
        self.assertEqual(profile["roles"][0]["domain"], expected_domain)
        self.assertTrue(any(hint in profile["primary_role"] for hint in expected_role_hint))
        return profile

    def test_software_resume_produces_software_roles(self) -> None:
        self._assert_primary(SOFTWARE_RESUME, "Software Engineering", ("Developer", "Engineer"))

    def test_it_support_resume_produces_support_roles(self) -> None:
        self._assert_primary(IT_SUPPORT_RESUME, "IT Support", ("Support", "Help Desk", "Desktop"))

    def test_data_resume_suppresses_ml_and_data_engineering_roles(self) -> None:
        profile = self._assert_primary(DATA_RESUME, "Data Analytics", ("Data Analyst", "Analyst"))
        titles = set(_titles(profile))
        self.assertFalse({"Data Scientist", "Machine Learning Engineer", "ML Engineer", "Data Engineer", "ETL Developer"} & titles)

    def test_ml_resume_produces_ml_roles(self) -> None:
        self._assert_primary(ML_RESUME, "AI / Machine Learning", ("Machine Learning", "ML", "AI"))

    def test_cloud_resume_produces_cloud_roles(self) -> None:
        self._assert_primary(CLOUD_RESUME, "Cloud / DevOps", ("DevOps", "Cloud", "Infrastructure"))

    def test_cyber_resume_produces_security_roles(self) -> None:
        self._assert_primary(CYBER_RESUME, "Cybersecurity", ("Security", "Cybersecurity", "SOC"))

    def test_fresher_gets_realistic_entry_level_roles(self) -> None:
        profile = _profile(FRESHER_RESUME)
        self.assertEqual(len(profile["roles"]), 12)
        self.assertEqual(profile["roles"][0]["domain"], "Software Engineering")
        # Project-only evidence -> honest, lower primary confidence and entry-level fit.
        self.assertLessEqual(profile["primary_role_confidence"], 75.0)
        self.assertIn("Entry-level", profile["roles"][0]["seniority_fit"])


class SpecializedGateTests(unittest.TestCase):
    """Behavior 11: tools/certifications alone cannot create a specialist role."""

    def _evidence(self, *, skills, responsibilities="") -> DomainEvidence:
        exp = [ExperienceEvidence("Analyst", responsibilities, 24, True, 0)] if responsibilities else []
        return DomainEvidence(titles=["Analyst"], experiences=exp, skills=skills, technologies=[],
                              education="", projects="", raw_text=" ".join(skills) + " " + responsibilities)

    def test_specialized_role_with_only_tool_evidence_is_gated_out(self) -> None:
        role = RoleDef("Data Engineer", "ETL / Pipeline Engineering", ("etl", "data pipeline", "spark"), specialized=True)
        evidence = self._evidence(skills=["Spark", "SQL"])  # tool present, no professional/project evidence
        self.assertIsNone(_score_role(role, "Data Engineering", True, evidence, set(), "Mid-level", 0))

    def test_specialized_role_with_professional_evidence_survives(self) -> None:
        role = RoleDef("Data Engineer", "ETL / Pipeline Engineering", ("etl", "data pipeline", "spark"), specialized=True)
        evidence = self._evidence(skills=["Spark"], responsibilities="built etl data pipeline jobs")
        scored = _score_role(role, "Data Engineering", True, evidence, set(), "Mid-level", 0)
        self.assertIsNotNone(scored)

    def test_non_specialized_role_survives_without_specialist_evidence(self) -> None:
        role = RoleDef("Reporting Analyst", "Reporting Analytics", ("reporting", "dashboard"))
        evidence = self._evidence(skills=["Excel"])
        self.assertIsNotNone(_score_role(role, "Data Analytics", True, evidence, set(), "Mid-level", 0))


class ProvisionalFallbackTests(unittest.TestCase):
    """Rare / unsupported-domain fallback: never force a supported domain."""

    def _assert_provisional(self, text: str, expected_role_hint: str) -> dict:
        profile = _profile(text)
        self.assertIn(profile["taxonomy_status"], {"provisional", "unsupported_domain"})
        self.assertIn(expected_role_hint, profile["primary_role"])
        # No returned role may claim a supported domain.
        for role in profile["roles"]:
            self.assertNotIn(role["domain"], SUPPORTED_DOMAINS)
        # Fewer than 12 is allowed; weak invented roles are not.
        self.assertLessEqual(len(profile["roles"]), 12)
        self.assertTrue(profile["unmatched_terms"])
        return profile

    def test_actuary_is_not_forced_into_a_supported_domain(self) -> None:
        self._assert_provisional(ACTUARY_RESUME, "Actuary")

    def test_paralegal_is_not_forced_into_a_supported_domain(self) -> None:
        self._assert_provisional(PARALEGAL_RESUME, "Paralegal")

    def test_mechanical_engineer_is_not_forced_into_software(self) -> None:
        profile = self._assert_provisional(MECHANICAL_RESUME, "Mechanical Engineer")
        self.assertNotEqual(profile["roles"][0]["domain"], "Software Engineering")

    def test_ux_designer_is_not_forced_into_a_supported_domain(self) -> None:
        self._assert_provisional(UX_RESUME, "UX Designer")

    def test_sales_executive_is_not_forced_into_a_supported_domain(self) -> None:
        self._assert_provisional(SALES_RESUME, "Sales Executive")

    def test_provisional_records_suggested_taxonomy_additions(self) -> None:
        profile = _profile(MECHANICAL_RESUME)
        self.assertIn("Mechanical Engineer", profile["suggested_taxonomy_additions"])


class DeterminismTests(unittest.TestCase):
    """Behavior 13: identical input -> identical role profile."""

    def test_repeated_ranking_is_identical(self) -> None:
        parser = ResumeParser(Settings(openai_api_key=None))
        first = parser.parse(FINANCE_RESUME)["role_profile"]
        second = parser.parse(FINANCE_RESUME)["role_profile"]
        self.assertEqual(first, second)

    def test_provisional_ranking_is_deterministic(self) -> None:
        parser = ResumeParser(Settings(openai_api_key=None))
        self.assertEqual(
            parser.parse(ACTUARY_RESUME)["role_profile"],
            parser.parse(ACTUARY_RESUME)["role_profile"],
        )


if __name__ == "__main__":
    unittest.main()
