"""Top-five search-role selection (1.1.0).

Resume analysis used to produce twelve ranked roles, and the Find 20/50/100 preset decided how
many of them (4/8/12) were searched -- so the preset changed WHAT was searched, and the larger
presets reached down into weak "Related"/"Stretch" tiers. Analysis now produces the detected
primary role plus up to four genuinely strong related roles, and every preset searches that same
ordered list.

These tests prove the contract end to end:

    ranking -> normalize -> filter (family / seniority / evidence / duplicates) -> limit to five

with the limit applied LAST, the primary role exempt from every rule, and nothing padded to reach
five. They also pin the realistic behaviour of seven professional families and prove that
technology-specific titles appear only when the resume's own languages, frameworks and projects
support them.
"""

import unittest
from pathlib import Path

from app.config import Settings
from app.models import ResumeProfile
from app.services.find_matches import FindMatchesEngine
from app.services.resume_parser import ResumeParser, apply_role_profile_compat
from app.services.role_ranking import CATEGORY_LAYOUT, TOTAL_ROLES
from app.services.role_selection import (
    MAX_SEARCH_ROLES,
    RoleCandidate,
    normalize_title,
    select_role_dicts,
    select_roles,
    selection_score,
    title_similarity,
)

MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")
FIND_MATCHES_SOURCE = Path("app/services/find_matches.py").read_text(encoding="utf-8", errors="replace")


# --- Realistic resumes, one per professional family ---------------------------------------------

FINANCIAL_ANALYST_RESUME = (
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
    "EDUCATION\nBachelor of Science in Finance, MIT, 2019\n"
    "SKILLS\nExcel, SQL, Power BI, Tableau, Python\n"
)

BUSINESS_ANALYST_RESUME = (
    "Priya Nair\nBusiness Analyst\n"
    "EXPERIENCE\n"
    "Northwind Systems | Chicago, IL | Mar 2021 - Present\n"
    "Business Analyst\n"
    "- Gathered business requirements and wrote user stories for the claims platform\n"
    "- Facilitated UAT, functional specification reviews and stakeholder workshops\n"
    "- Mapped business process workflows and ran gap analysis for process improvement\n"
    "Vertex Retail | 2018 - 2021\n"
    "Junior Business Analyst\n"
    "- Documented requirements and use cases; supported SDLC delivery\n"
    "SKILLS\nJIRA, Confluence, SQL, Visio, BPMN\n"
)

AI_ML_ENGINEER_RESUME = (
    "Alex Ng\nMachine Learning Engineer\n"
    "EXPERIENCE\n"
    "AI Labs | Remote | Feb 2021 - Present\n"
    "Machine Learning Engineer\n"
    "- Model training and evaluation of deep learning neural networks in PyTorch\n"
    "- Feature engineering, model deployment and inference pipelines\n"
    "- Built NLP text classification and computer vision object detection models\n"
    "SKILLS\nPython, PyTorch, TensorFlow, scikit-learn, pandas\n"
)

NETWORK_ENGINEER_RESUME = (
    "Omar Haddad\nNetwork Engineer\n"
    "EXPERIENCE\n"
    "LinkGrid Telecom | Dallas, TX | Feb 2021 - Present\n"
    "Network Engineer\n"
    "- Routing and switching configuration across LAN and WAN; BGP and OSPF\n"
    "- Network monitoring and uptime for the NOC; TCP/IP troubleshooting\n"
    "- Firewall and VPN administration for network security\n"
    "Cabletown | 2018 - 2021\n"
    "Network Administrator\n"
    "- Network monitoring, connectivity troubleshooting and network support\n"
    "SKILLS\nCisco IOS, BGP, OSPF, Wireshark, SolarWinds\n"
)

HR_COORDINATOR_RESUME = (
    "Grace Owens\nHR Coordinator\n"
    "EXPERIENCE\n"
    "BrightPath Group | Denver, CO | Jan 2022 - Present\n"
    "HR Coordinator\n"
    "- Onboarding coordination, benefits administration and payroll support\n"
    "- Maintained HRIS records in Workday and produced HR reporting\n"
    "- Scheduled interviews and supported recruitment and sourcing\n"
    "SKILLS\nWorkday, Excel, Greenhouse\n"
)

# Data Analysis, case 1: a reporting / BI resume with no modelling work at all.
REPORTING_DATA_ANALYST_RESUME = (
    "Dana Kim\nData Analyst\n"
    "WORK EXPERIENCE\n"
    "Insight Co | Pune | Jan 2021 - Present\n"
    "Data Analyst\n"
    "- Built dashboards and reporting in Power BI and Tableau\n"
    "- Ad hoc analysis, KPI and metrics, data-driven insights for stakeholders\n"
    "- SQL queries and Python pandas for trend analysis\n"
    "SKILLS\nSQL, Excel, Power BI, Tableau, Python, Pandas\n"
)

# Data Analysis, case 2: the same job title, but genuine statistical / ML work that justifies a
# specialized substitution.
ADVANCED_DATA_ANALYST_RESUME = (
    "Ravi Menon\nData Analyst\n"
    "EXPERIENCE\n"
    "Quantum Retail | Bengaluru | Jan 2021 - Present\n"
    "Data Analyst\n"
    "- Built predictive models and statistical modeling for demand forecasting\n"
    "- Machine learning experimentation and model evaluation in Python\n"
    "- SQL analysis, dashboards and reporting for stakeholders\n"
    "SKILLS\nPython, SQL, scikit-learn, Power BI\n"
)

# Software Development, case 1: general, technology-neutral.
GENERAL_SOFTWARE_RESUME = (
    "Ana Silva\nSoftware Developer\n"
    "WORK EXPERIENCE\n"
    "Meridian Software | Toronto, ON | Feb 2021 - Present\n"
    "Software Developer\n"
    "- Software development of business applications; feature development and code review\n"
    "- Application development across the SDLC using object-oriented design and algorithms\n"
    "Halcyon Systems | 2018 - 2021\n"
    "Junior Software Developer\n"
    "- Software application maintenance, defect fixes and unit tests\n"
    "EDUCATION\nBSc Computer Science\nSKILLS\nJava, C#, Git, Jira\n"
)

# Software Development, case 2: technology-specific (frontend / React).
FRONTEND_DEVELOPER_RESUME = (
    "Noor Aziz\nFrontend Developer\n"
    "WORK EXPERIENCE\n"
    "Lumen Interactive | Remote | Mar 2021 - Present\n"
    "Frontend Developer\n"
    "- Built responsive user interface components in React with Redux and hooks\n"
    "- Web application development in TypeScript; CSS design system and accessibility work\n"
    "Pixel Forge | 2018 - 2021\n"
    "Web Developer\n"
    "- Built web pages in HTML, CSS and JavaScript for marketing sites\n"
    "SKILLS\nReact, TypeScript, JavaScript, HTML, CSS, Redux\n"
)

# A backend resume, used to prove frontend titles are NOT hardcoded for software resumes.
BACKEND_DEVELOPER_RESUME = (
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

FAMILY_RESUMES = {
    "Financial Analyst": FINANCIAL_ANALYST_RESUME,
    "Business Analyst": BUSINESS_ANALYST_RESUME,
    "AI/ML Engineer": AI_ML_ENGINEER_RESUME,
    "Network Engineer": NETWORK_ENGINEER_RESUME,
    "HR Coordinator": HR_COORDINATOR_RESUME,
    "Data Analyst (reporting)": REPORTING_DATA_ANALYST_RESUME,
    "Data Analyst (advanced)": ADVANCED_DATA_ANALYST_RESUME,
    "Software Developer (general)": GENERAL_SOFTWARE_RESUME,
    "Software Developer (frontend)": FRONTEND_DEVELOPER_RESUME,
}


def _settings() -> Settings:
    return Settings(openai_api_key=None)


def _engine() -> FindMatchesEngine:
    return FindMatchesEngine(_settings())


def _role_profile(text: str) -> dict:
    return ResumeParser(_settings()).parse(text)["role_profile"]


def _titles(text: str) -> list[str]:
    return [role["title"] for role in _role_profile(text)["roles"]]


def _analyzed_resume(text: str) -> ResumeProfile:
    """Build the ORM row exactly as the analyze endpoint would."""
    profile = ResumeParser(_settings()).parse(text)
    apply_role_profile_compat(profile)
    return ResumeProfile(
        id=1, portal="linkedin", filename="r.txt", stored_path="r.txt", raw_text=text,
        skills=profile["skills"], experience=profile["experience"], education=profile["education"],
        job_titles=profile["job_titles"], keywords=profile["keywords"],
        technologies=profile["technologies"], domain_expertise=profile["domain_expertise"],
        detected_role=profile["detected_role"], role_suggestions=profile["role_suggestions"],
        generated_search_keywords=profile["generated_search_keywords"],
        generated_search_query=profile["generated_search_query"],
        role_confidence=profile["role_confidence"],
        domain_profile=profile["domain_profile"], role_profile=profile["role_profile"],
    )


def _candidate(title: str, index: int, **kwargs) -> RoleCandidate:
    return RoleCandidate(title=title, order_index=index, **kwargs)


# --- 1-4, 6-9: the selection contract ------------------------------------------------------------


class SelectionContractTests(unittest.TestCase):
    """The rules themselves, exercised directly on ranked candidates."""

    def test_the_constant_is_five_and_the_layout_agrees(self) -> None:
        self.assertEqual(MAX_SEARCH_ROLES, 5)
        self.assertEqual(TOTAL_ROLES, MAX_SEARCH_ROLES)
        self.assertEqual([count for _label, count in CATEGORY_LAYOUT], [1, 4])

    def test_at_most_five_roles_are_selected(self) -> None:
        candidates = [_candidate(f"Analyst {i}", i, job_family="F", confidence=90 - i) for i in range(20)]
        selection = select_roles(candidates)
        self.assertEqual(len(selection.selected), MAX_SEARCH_ROLES)

    def test_the_primary_role_is_always_first(self) -> None:
        # Every later candidate outscores it, and it is still rank 1.
        candidates = [_candidate("Weak Primary", 0, confidence=10.0)] + [
            _candidate(f"Strong Role {i}", i, confidence=99.0) for i in range(1, 8)
        ]
        selection = select_roles(candidates)
        self.assertEqual(selection.selected[0].title, "Weak Primary")

    def test_the_primary_role_survives_every_filter(self) -> None:
        # A primary that violates the family, seniority AND evidence rules at once is still kept.
        candidates = [
            _candidate("Impossible Primary", 0, in_primary_family=False,
                       seniority_compatible=False, has_supporting_evidence=False),
            _candidate("Clean Role", 1, confidence=80.0),
        ]
        selection = select_roles(candidates)
        self.assertEqual([c.title for c in selection.selected], ["Impossible Primary", "Clean Role"])

    def test_the_primary_role_survives_deduplication(self) -> None:
        # A higher-confidence near-duplicate cannot displace the primary; it is dropped instead.
        candidates = [
            _candidate("ML Engineer", 0, confidence=60.0),
            _candidate("Machine Learning Engineer", 1, confidence=99.0),
            _candidate("MLOps Engineer", 2, confidence=70.0),
        ]
        selection = select_roles(candidates)
        self.assertEqual([c.title for c in selection.selected], ["ML Engineer", "MLOps Engineer"])
        self.assertIn(
            "near-identical",
            next(r["reason"] for r in selection.rejected if r["title"] == "Machine Learning Engineer"),
        )

    def test_only_the_four_strongest_related_roles_are_selected(self) -> None:
        candidates = [_candidate("Primary", 0, job_family="F", confidence=99.0)] + [
            _candidate(f"Role {i}", i, job_family="F", confidence=float(90 - i * 5)) for i in range(1, 9)
        ]
        selection = select_roles(candidates)
        self.assertEqual(
            [c.title for c in selection.selected], ["Primary", "Role 1", "Role 2", "Role 3", "Role 4"]
        )
        for rejected in selection.rejected:
            self.assertIn("ranked below the top 5", rejected["reason"])

    def test_near_identical_titles_are_removed(self) -> None:
        pairs = [
            ("Financial Data Analyst", "Finance Data Analyst"),
            ("Machine Learning Engineer", "ML Engineer"),
            ("Human Resources Coordinator", "HR Coordinator"),
            ("Business Intelligence Analyst", "BI Analyst"),
            ("Senior Data Analyst", "Data Analyst"),
            ("Full Stack Developer", "Full-Stack Developer"),
        ]
        for left, right in pairs:
            with self.subTest(pair=(left, right)):
                self.assertEqual(normalize_title(left), normalize_title(right))
        # And distinct roles are NOT collapsed.
        self.assertNotEqual(normalize_title("Financial Analyst"), normalize_title("Finance BI Analyst"))
        self.assertNotEqual(normalize_title("Network Engineer"), normalize_title("Network Administrator"))
        self.assertNotEqual(normalize_title("Data Analyst"), normalize_title("Data Scientist"))

    def test_fewer_than_five_roles_are_allowed(self) -> None:
        selection = select_roles([_candidate("Only Role", 0)])
        self.assertEqual(len(selection.selected), 1)
        self.assertEqual(selection.rejected, [])

    def test_unrelated_filler_roles_are_excluded_rather_than_padding_to_five(self) -> None:
        candidates = [
            _candidate("Primary", 0, domain="D", job_family="F", confidence=99.0),
            _candidate("Supported Role", 1, domain="D", job_family="F", confidence=70.0),
            _candidate("Other Family Role", 2, domain="Elsewhere", in_primary_family=False, confidence=95.0),
            _candidate("Unevidenced Role", 3, domain="D", has_supporting_evidence=False, confidence=95.0),
        ]
        selection = select_roles(candidates)
        self.assertEqual([c.title for c in selection.selected], ["Primary", "Supported Role"])
        reasons = {r["title"]: r["reason"] for r in selection.rejected}
        self.assertIn("outside the primary professional family", reasons["Other Family Role"])
        self.assertIn("nothing in this resume supports", reasons["Unevidenced Role"])

    def test_seniority_incompatible_roles_are_excluded(self) -> None:
        candidates = [
            _candidate("Analyst", 0, job_family="F", confidence=90.0),
            _candidate("Head of Analytics", 1, job_family="F", confidence=99.0, seniority_compatible=False),
            _candidate("Senior Analyst", 2, job_family="F", confidence=60.0),
        ]
        selection = select_roles(candidates)
        self.assertNotIn("Head of Analytics", [c.title for c in selection.selected])
        self.assertIn(
            "seniority-incompatible",
            next(r["reason"] for r in selection.rejected if r["title"] == "Head of Analytics"),
        )

    def test_ordering_is_deterministic_when_scores_tie(self) -> None:
        # Identical confidence, identical family, identical title overlap -> incoming order decides.
        candidates = [_candidate("Primary Analyst", 0, job_family="F", confidence=90.0)] + [
            _candidate(f"Tied Role {i}", i, job_family="F", confidence=70.0) for i in range(1, 6)
        ]
        first = [c.title for c in select_roles(candidates).selected]
        for _ in range(5):
            self.assertEqual([c.title for c in select_roles(candidates).selected], first)
        self.assertEqual(first, ["Primary Analyst", "Tied Role 1", "Tied Role 2", "Tied Role 3", "Tied Role 4"])

    def test_family_compatibility_and_primary_similarity_dominate_the_ordering(self) -> None:
        primary = _candidate("Data Analyst", 0, job_family="Data Analysis", confidence=99.0)
        same_family = _candidate("Reporting Data Analyst", 1, job_family="Data Analysis", confidence=70.0)
        unrelated_title = _candidate("Compliance Officer", 2, job_family="Compliance", confidence=74.0)
        self.assertGreater(
            selection_score(primary, same_family), selection_score(primary, unrelated_title)
        )
        selected = [c.title for c in select_roles([primary, unrelated_title, same_family]).selected]
        self.assertEqual(selected, ["Data Analyst", "Reporting Data Analyst", "Compliance Officer"])

    def test_a_closer_family_band_is_never_outranked_by_the_broad_parent_domain(self) -> None:
        primary = _candidate("Healthcare Data Analyst", 0, job_family="Healthcare Analytics", confidence=99.0)
        specialization = _candidate("Clinical Data Analyst", 1, job_family="Healthcare Analytics",
                                    confidence=60.0, family_rank=0)
        broad = _candidate("Data Analyst", 2, job_family="Data Analysis", confidence=95.0, family_rank=1)
        selected = [c.title for c in select_roles([primary, broad, specialization]).selected]
        self.assertEqual(selected, ["Healthcare Data Analyst", "Clinical Data Analyst", "Data Analyst"])

    def test_title_similarity_is_bounded_and_symmetric(self) -> None:
        self.assertEqual(title_similarity("Data Analyst", "Data Analyst"), 1.0)
        self.assertEqual(title_similarity("Data Analyst", "Network Engineer"), 0.0)
        self.assertEqual(
            title_similarity("Data Analyst", "Reporting Analyst"),
            title_similarity("Reporting Analyst", "Data Analyst"),
        )

    def test_every_rejected_role_carries_a_reason(self) -> None:
        candidates = [_candidate(f"Role {i}", i, job_family="F", confidence=90 - i) for i in range(12)]
        selection = select_roles(candidates)
        self.assertEqual(len(selection.rejected), 12 - MAX_SEARCH_ROLES)
        for rejected in selection.rejected:
            self.assertTrue(rejected["reason"])
            self.assertTrue(rejected["title"])


# --- 1-9, applied to real resumes ----------------------------------------------------------------


class RealResumeSelectionTests(unittest.TestCase):
    def test_every_family_returns_at_most_five_unique_roles(self) -> None:
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                titles = _titles(text)
                self.assertGreaterEqual(len(titles), 1)
                self.assertLessEqual(len(titles), MAX_SEARCH_ROLES)
                self.assertEqual(len(titles), len(set(titles)), "duplicate titles were selected")
                self.assertEqual(
                    len({normalize_title(t) for t in titles}), len(titles), "near-duplicate titles were selected"
                )

    def test_every_family_keeps_its_detected_primary_role_at_rank_one(self) -> None:
        expected = {
            "Financial Analyst": "Financial Analyst",
            "Business Analyst": "Business Analyst",
            "AI/ML Engineer": "Machine Learning Engineer",
            "Network Engineer": "Network Engineer",
            "HR Coordinator": "HR Coordinator",
            "Data Analyst (reporting)": "Data Analyst",
            "Data Analyst (advanced)": "Data Analyst",
            "Software Developer (general)": "Software Engineer",
            "Software Developer (frontend)": "Frontend Developer",
        }
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                profile = _role_profile(text)
                self.assertEqual(profile["roles"][0]["title"], expected[label])
                self.assertEqual(profile["primary_role"], expected[label])
                self.assertEqual(profile["roles"][0]["category"], "Primary Role")
                self.assertEqual(profile["roles"][0]["rank"], 1)

    def test_every_selected_role_stays_inside_the_primary_professional_family(self) -> None:
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                roles = _role_profile(text)["roles"]
                domains = {role["domain"] for role in roles}
                self.assertEqual(len(domains), 1, f"{label} mixed role families: {domains}")

    def test_role_selection_is_deterministic_for_identical_input(self) -> None:
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                self.assertEqual(_role_profile(text), _role_profile(text))

    def test_ranks_are_contiguous_one_through_five(self) -> None:
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                roles = _role_profile(text)["roles"]
                self.assertEqual([r["rank"] for r in roles], list(range(1, len(roles) + 1)))
                self.assertEqual([r["category"] for r in roles[1:]], ["Strong Fit"] * (len(roles) - 1))


class FamilySpecificTests(unittest.TestCase):
    """Each family stays in its own lane, and substitutions follow the resume's own evidence."""

    def test_financial_analyst_searches_only_finance_roles(self) -> None:
        titles = _titles(FINANCIAL_ANALYST_RESUME)
        self.assertEqual(titles[0], "Financial Analyst")
        for title in titles:
            self.assertTrue(
                any(word in title for word in
                    ("Financial", "Finance", "FP&A", "Treasury", "Revenue", "Investment", "Valuation", "Portfolio")),
                f"{title} is not a finance role",
            )
        self.assertFalse(
            {"Data Scientist", "Software Engineer", "Machine Learning Engineer", "Data Engineer"} & set(titles)
        )

    def test_business_analyst_searches_only_business_analysis_roles(self) -> None:
        titles = _titles(BUSINESS_ANALYST_RESUME)
        self.assertEqual(titles[0], "Business Analyst")
        for title in titles:
            self.assertTrue(
                any(word in title for word in ("Analyst", "Owner")), f"{title} is not a business-analysis role"
            )
        self.assertFalse({"Software Engineer", "Data Scientist", "Product Manager"} & set(titles))

    def test_ai_ml_engineer_searches_only_ml_roles(self) -> None:
        titles = _titles(AI_ML_ENGINEER_RESUME)
        self.assertEqual(titles[0], "Machine Learning Engineer")
        for title in titles:
            self.assertTrue(
                any(word in title for word in
                    ("Machine Learning", "ML", "AI", "NLP", "Computer Vision", "Deep Learning", "Data Scien", "Applied")),
                f"{title} is not an AI/ML role",
            )
        # NLP and Computer Vision are substitutions the resume explicitly earns.
        self.assertIn("NLP Engineer", titles)
        self.assertIn("Computer Vision Engineer", titles)

    def test_network_engineer_searches_only_networking_roles(self) -> None:
        titles = _titles(NETWORK_ENGINEER_RESUME)
        self.assertEqual(titles[0], "Network Engineer")
        for title in titles:
            self.assertTrue(
                any(word in title for word in ("Network", "NOC", "Infrastructure")), f"{title} is not a networking role"
            )

    def test_cloud_network_engineer_needs_cloud_networking_evidence(self) -> None:
        # No VPC / cloud-networking work in the resume, so the specialised title is not searched.
        self.assertNotIn("Cloud Network Engineer", _titles(NETWORK_ENGINEER_RESUME))

    def test_hr_coordinator_searches_only_hr_roles(self) -> None:
        profile = _role_profile(HR_COORDINATOR_RESUME)
        self.assertEqual(profile["roles"][0]["title"], "HR Coordinator")
        self.assertEqual({r["domain"] for r in profile["roles"]}, {"Human Resources"})

    def test_hr_manager_is_excluded_for_a_coordinator_level_resume(self) -> None:
        # A manager-level role is a seniority stretch for a coordinator, so it is never searched.
        self.assertNotIn("HR Manager", _titles(HR_COORDINATOR_RESUME))

    def test_reporting_data_analyst_stays_on_reporting_and_bi_roles(self) -> None:
        titles = _titles(REPORTING_DATA_ANALYST_RESUME)
        self.assertEqual(titles[0], "Data Analyst")
        # None of the specialisations this resume does not support.
        self.assertFalse(
            {"Data Scientist", "Machine Learning Engineer", "Data Engineer", "Analytics Engineer",
             "Business Analyst", "Database Administrator"} & set(titles)
        )
        # And none of the unrelated verticals that merely exist in the taxonomy.
        self.assertFalse({"Marketing Analyst", "CRM Analyst", "Product Analyst"} & set(titles))

    def test_advanced_data_analyst_earns_a_data_science_substitution(self) -> None:
        titles = _titles(ADVANCED_DATA_ANALYST_RESUME)
        self.assertEqual(titles[0], "Data Analyst")
        self.assertTrue(
            any("Data Scien" in title or "Predictive" in title for title in titles[1:]),
            f"advanced analytics evidence produced no specialized substitution: {titles}",
        )
        # The same title with no modelling evidence gets none of those roles -- so the
        # substitution is driven by the resume, not by the job title.
        self.assertFalse(
            {title for title in titles if "Data Scien" in title or "Predictive" in title}
            & set(_titles(REPORTING_DATA_ANALYST_RESUME))
        )

    def test_general_software_resume_gets_no_technology_specific_titles(self) -> None:
        titles = _titles(GENERAL_SOFTWARE_RESUME)
        self.assertEqual(titles[0], "Software Engineer")
        for banned in ("Python Developer", "Java Developer", ".NET Developer", "React Developer",
                       "JavaScript Developer", "Frontend Developer", "Backend Developer", "Mobile Developer"):
            self.assertNotIn(banned, titles)
        # And none of the neighbouring professions.
        self.assertFalse(
            {"DevOps Engineer", "Data Engineer", "QA Automation Engineer", "Network Engineer",
             "Product Manager"} & set(titles)
        )

    def test_technology_specific_titles_appear_only_with_matching_evidence(self) -> None:
        frontend = _titles(FRONTEND_DEVELOPER_RESUME)
        backend = _titles(BACKEND_DEVELOPER_RESUME)
        general = _titles(GENERAL_SOFTWARE_RESUME)
        # The React/JavaScript resume earns React and JavaScript titles ...
        self.assertEqual(frontend[0], "Frontend Developer")
        self.assertIn("React Developer", frontend)
        self.assertIn("JavaScript Developer", frontend)
        # ... and no other software resume gets them, so they are not hardcoded.
        for other in (backend, general):
            self.assertNotIn("React Developer", other)
            self.assertNotIn("JavaScript Developer", other)
        # The backend resume earns backend/API titles the frontend resume does not.
        self.assertEqual(backend[0], "Backend Developer")
        self.assertIn("API Developer", backend)
        self.assertNotIn("API Developer", frontend)

    def test_a_narrow_resume_returns_fewer_roles_rather_than_filler(self) -> None:
        titles = _titles(GENERAL_SOFTWARE_RESUME)
        self.assertLess(len(titles), MAX_SEARCH_ROLES)
        self.assertGreaterEqual(len(titles), 1)


# --- 10-16: presets, portals, ordering and allocation --------------------------------------------


class PresetAndPortalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine()
        self.resume = _analyzed_resume(FINANCIAL_ANALYST_RESUME)
        self.variations, self.plan = self.engine.build_search_plan(self.resume, "Remote", None)

    def _selected(self, target: int) -> list[str]:
        count = self.engine.collection_plan(target, len(self.variations))["selected_variation_count"]
        return [variation.query for variation in self.variations[:count]]

    def test_find20_find50_and_find100_use_the_same_role_list(self) -> None:
        self.assertEqual(self._selected(20), self._selected(50))
        self.assertEqual(self._selected(50), self._selected(100))
        self.assertEqual(len(self._selected(20)), MAX_SEARCH_ROLES)

    def test_the_preset_changes_only_the_final_job_limit_and_the_per_query_depth(self) -> None:
        for target in (20, 50, 100):
            self.assertEqual(self.engine.target_count(target), target)
        plans = [self.engine.collection_plan(target, len(self.variations)) for target in (20, 50, 100)]
        self.assertEqual({plan["selected_variation_count"] for plan in plans}, {MAX_SEARCH_ROLES})
        self.assertEqual(
            [plan["limit_per_query"] for plan in plans],
            sorted(plan["limit_per_query"] for plan in plans),
        )

    def test_the_preset_no_longer_chooses_a_role_count(self) -> None:
        # The old rule was selected_variation_count = max(4, min(24, (target + 6) // 7)).
        self.assertNotIn("(target_count + 6) // 7", FIND_MATCHES_SOURCE)
        self.assertIn("min(variation_count, MAX_SEARCH_ROLES)", FIND_MATCHES_SOURCE)

    def test_the_plan_is_portal_independent(self) -> None:
        # The same resume produces the same ordered roles whichever portal it was uploaded for.
        linkedin_resume = _analyzed_resume(FINANCIAL_ANALYST_RESUME)
        dice_resume = _analyzed_resume(FINANCIAL_ANALYST_RESUME)
        dice_resume.portal = "dice"
        for_linkedin, _ = _engine().build_search_plan(linkedin_resume, "Remote", None)
        for_dice, _ = _engine().build_search_plan(dice_resume, "Remote", None)
        self.assertEqual(
            [(v.variation_index, v.query, v.role_priority) for v in for_linkedin],
            [(v.variation_index, v.query, v.role_priority) for v in for_dice],
        )

    def test_linkedin_and_dice_receive_the_same_ordered_list(self) -> None:
        # main.py builds the list ONCE and every adapter call site passes that same name -- there
        # is no per-portal copy, reorder or re-filter to diverge from.
        call_sites = MAIN_SOURCE.count("adapter.search_jobs_many(")
        self.assertGreaterEqual(call_sites, 2, "expected a LinkedIn and a Dice call site")
        passing_the_shared_list = sum(
            "selected_variations," in block.split(")")[0]
            for block in MAIN_SOURCE.split("adapter.search_jobs_many(")[1:]
        )
        self.assertEqual(passing_the_shared_list, call_sites)
        # The single-query LinkedIn fallback iterates the same list, in order.
        self.assertIn("for i, variation in enumerate(selected_variations):", MAIN_SOURCE)
        # And the list is assigned exactly once, before the portal loop.
        self.assertEqual(MAIN_SOURCE.count("selected_variations = "), 1)
        self.assertNotIn("selected_variations_for_dice", MAIN_SOURCE)
        self.assertNotIn("selected_variations_for_linkedin", MAIN_SOURCE)

    def test_no_duplicate_search_queries_are_created(self) -> None:
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                variations, _plan = _engine().build_search_plan(_analyzed_resume(text), "Remote", None)
                queries = [v.query for v in variations]
                self.assertEqual(len(queries), len(set(queries)))
                self.assertEqual(len({normalize_title(q) for q in queries}), len(queries))
                for query in queries:
                    self.assertNotIn(" OR ", query)

    def test_role_grouping_stays_rank_one_through_five(self) -> None:
        self.assertEqual(
            [v.variation_index for v in self.variations], list(range(1, len(self.variations) + 1))
        )
        self.assertEqual([v.role_rank for v in self.variations], [v.variation_index for v in self.variations])
        self.assertEqual(self.variations[0].role_priority, "primary")
        self.assertTrue(all(v.role_priority == "strong_fit" for v in self.variations[1:]))

    def test_weighted_allocation_still_prioritizes_higher_ranked_roles(self) -> None:
        take = self.engine.weighted_take([40] * MAX_SEARCH_ROLES, 100)
        self.assertEqual(sum(take), 100)
        self.assertGreater(take[0], max(take[1:]))
        # Search effort is weighted the same way.
        plan = self.engine.collection_plan(100, MAX_SEARCH_ROLES)
        limits = [
            plan["limit_per_query"] * self.engine.TOP_VARIATION_WEIGHT
            if i < self.engine.TOP_VARIATION_COUNT else plan["limit_per_query"]
            for i in range(MAX_SEARCH_ROLES)
        ]
        self.assertGreater(limits[0], limits[-1])

    def test_unused_capacity_is_reallocated_to_the_next_highest_rank(self) -> None:
        take = self.engine.weighted_take([1, 60, 60, 60, 60], 100)
        self.assertEqual(take[0], 1)                 # capped at what rank 1 actually has
        self.assertEqual(sum(take), 100)             # the shortfall is not lost
        self.assertGreater(take[1], take[2])         # it went to rank 2, not to a lower rank

    def test_short_supply_is_reported_honestly_and_never_padded(self) -> None:
        take = self.engine.weighted_take([2, 1, 0, 0, 0], 100)
        self.assertEqual(take, [2, 1, 0, 0, 0])


# --- 17-18: saved records ------------------------------------------------------------------------


class LegacyRecordTests(unittest.TestCase):
    """A record saved before 1.1.0 still holds twelve ranked roles."""

    def _legacy_twelve(self) -> list[dict]:
        return [
            {
                "rank": i + 1,
                "title": title,
                "category": category,
                "confidence": float(99 - i * 3),
                "domain": "Finance / Financial Analysis",
                "job_family": "Corporate Finance / Financial Analysis",
                "seniority_fit": "Mid-level (individual contributor match)",
                "evidence": ["professional responsibilities: financial analysis"],
                "missing_evidence": [],
                "penalties": [],
            }
            for i, (title, category) in enumerate([
                ("Financial Analyst", "Primary Role"),
                ("FP&A Analyst", "Strong Fit"),
                ("Financial Reporting Analyst", "Strong Fit"),
                ("Financial Data Analyst", "Strong Fit"),
                ("Treasury Analyst", "Strong Fit"),
                ("Revenue / Profitability Analyst", "Related"),
                ("Investment Analyst", "Related"),
                ("Business Finance Analyst", "Related"),
                ("Valuation Analyst", "Related"),
                ("Portfolio Analyst", "Stretch"),
                ("Investment Risk Analyst", "Stretch"),
                ("Finance BI Analyst", "Stretch"),
            ])
        ]

    def test_a_twelve_role_record_is_still_readable(self) -> None:
        stored = self._legacy_twelve()
        resume = ResumeProfile(
            id=7, portal="linkedin", filename="old.txt", stored_path="old.txt", raw_text="",
            role_profile={"taxonomy_status": "supported", "primary_role": "Financial Analyst",
                          "primary_role_confidence": 99.0, "roles": stored},
            domain_profile={"primary_domain": "Finance / Financial Analysis"},
        )
        variations, plan = _engine().build_search_plan(resume, "Remote", None)
        self.assertEqual(plan["role_source"], "role_profile")
        self.assertTrue(variations)
        # The record is unchanged on disk ...
        self.assertEqual(len(resume.role_profile["roles"]), 12)
        # ... and the search still uses at most five roles, primary first.
        self.assertLessEqual(len(variations), MAX_SEARCH_ROLES)
        self.assertEqual(variations[0].role, "Financial Analyst")
        self.assertEqual([v.variation_index for v in variations], list(range(1, len(variations) + 1)))

    def test_a_twelve_role_record_is_renumbered_for_the_search_plan(self) -> None:
        selected = select_role_dicts(self._legacy_twelve())
        self.assertEqual(len(selected), MAX_SEARCH_ROLES)
        self.assertEqual([role["rank"] for role in selected], [1, 2, 3, 4, 5])
        self.assertEqual([role["category"] for role in selected], ["Primary Role"] + ["Strong Fit"] * 4)
        self.assertEqual(selected[0]["title"], "Financial Analyst")

    def test_legacy_selection_drops_cross_family_and_seniority_stretch_roles(self) -> None:
        stored = self._legacy_twelve()
        stored[1] = {**stored[1], "title": "Software Engineer", "domain": "Software Engineering"}
        stored[2] = {**stored[2], "title": "Finance Manager",
                     "penalties": ["seniority Mid-level -> Manager (significant stretch up) (-15)"]}
        stored[3] = {**stored[3], "title": "Unsupported Analyst", "evidence": []}
        selected = [role["title"] for role in select_role_dicts(stored)]
        self.assertEqual(selected[0], "Financial Analyst")
        self.assertNotIn("Software Engineer", selected)
        self.assertNotIn("Finance Manager", selected)
        self.assertNotIn("Unsupported Analyst", selected)

    def test_reapplying_selection_to_a_saved_profile_changes_nothing(self) -> None:
        # The search plan re-runs selection on read (so legacy records are filtered too). For a
        # record saved by this version it must be a no-op, or the searched order would drift away
        # from the order the dashboard shows.
        for label, text in FAMILY_RESUMES.items():
            with self.subTest(resume=label):
                stored = _role_profile(text)["roles"]
                once = select_role_dicts(stored)
                self.assertEqual([r["title"] for r in once], [r["title"] for r in stored])
                self.assertEqual([r["title"] for r in select_role_dicts(once)], [r["title"] for r in once])

    def test_an_empty_or_missing_role_list_is_handled(self) -> None:
        self.assertEqual(select_role_dicts([]), [])
        self.assertEqual(select_role_dicts([{"title": "   "}]), [])

    def test_reanalysis_stores_at_most_five_roles(self) -> None:
        # The same resume text that used to produce twelve now persists five.
        resume = _analyzed_resume(FINANCIAL_ANALYST_RESUME)
        self.assertLessEqual(len(resume.role_profile["roles"]), MAX_SEARCH_ROLES)
        self.assertLessEqual(len(resume.role_suggestions), MAX_SEARCH_ROLES)
        self.assertEqual(resume.role_suggestions[0]["role_category"], "primary_role")
        self.assertEqual(resume.detected_role, "Financial Analyst")
        self.assertEqual(resume.generated_search_query, "Financial Analyst")

    def test_reanalysis_explains_which_credible_roles_were_not_searched(self) -> None:
        profile = _role_profile(FINANCIAL_ANALYST_RESUME)
        reasons = {entry["reason"] for entry in profile["suppressed_roles"]}
        self.assertTrue(profile["suppressed_roles"])
        self.assertTrue(any("ranked below the top 5" in reason for reason in reasons))


# --- 19-23: untouched behaviour ------------------------------------------------------------------


class UntouchedBehaviourTests(unittest.TestCase):
    """This feature is confined to role selection and the search plan."""

    PORTAL_SOURCES = (
        "app/services/linkedin.py",
        "app/services/portals/dice_portal.py",
        "app/services/portals/linkedin_portal.py",
        "app/services/portals/base.py",
    )

    def test_no_portal_adapter_knows_about_role_selection(self) -> None:
        for path in self.PORTAL_SOURCES:
            source = Path(path).read_text(encoding="utf-8", errors="replace")
            with self.subTest(source=path):
                self.assertNotIn("MAX_SEARCH_ROLES", source)
                self.assertNotIn("role_selection", source)
                self.assertNotIn("select_role_dicts", source)

    def test_dry_run_and_live_apply_modules_are_untouched_by_role_selection(self) -> None:
        for path in ("app/services/screening_engine.py", "app/services/application_outcome.py",
                     "app/services/generalized_form_engine.py"):
            source = Path(path).read_text(encoding="utf-8", errors="replace")
            with self.subTest(source=path):
                self.assertNotIn("MAX_SEARCH_ROLES", source)
                self.assertNotIn("role_selection", source)

    def test_role_selection_imports_nothing_portal_specific(self) -> None:
        source = Path("app/services/role_selection.py").read_text(encoding="utf-8", errors="replace")
        imports = [line.strip() for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertTrue(imports)
        for line in imports:
            self.assertNotIn("portal", line)
            self.assertNotIn("linkedin", line.lower())
            self.assertNotIn("dice", line.lower())
            self.assertNotIn("playwright", line.lower())
            # No app imports at all: selection depends on nothing but its own inputs.
            self.assertNotIn("app.", line)

    def test_the_shared_plan_still_feeds_canonical_dedup_and_ownership(self) -> None:
        # Earliest-variation ownership and the Easy-Apply gate still run over the same plan.
        self.assertIn("owned_by_variation", MAIN_SOURCE)
        self.assertIn("take_per_variation = engine.weighted_take(available, target_count)", MAIN_SOURCE)


if __name__ == "__main__":
    unittest.main()
