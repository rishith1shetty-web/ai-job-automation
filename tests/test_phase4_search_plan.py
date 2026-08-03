"""Phase 4: the Phase-3 role_profile drives the shared LinkedIn/Dice Find Matches search plan.

These tests prove the ordered role -> variation mapping (variation 1 = the detected primary role,
2-5 = the strongest related roles), one narrow query per role (never a broad OR), the same ordered
plan for both portals, the preserved weighted allocation, persistence of domain_profile /
role_profile across a DB round-trip, and the old-analysis regeneration/legacy fallback.
"""

import unittest

from sqlalchemy import delete

from app.config import Settings
from app.database import SessionLocal, init_db
from app.main import latest_resume
from app.models import ResumeProfile
from app.services.find_matches import FindMatchesEngine
from app.services.role_selection import MAX_SEARCH_ROLES
from app.services.resume_parser import ResumeParser, apply_role_profile_compat
from app.services.runtime_session import get_runtime_session, reset_runtime_session
from tests.test_domain_detection import (
    DATA_RESUME,
    FINANCE_RESUME,
    IT_SUPPORT_RESUME,
    SOFTWARE_RESUME,
)
from tests.test_role_ranking import ACTUARY_RESUME

FINANCE_EXCLUDED = {
    "Data Scientist", "Machine Learning Engineer", "ML Engineer", "Data Engineer",
    "Analytics Engineer", "Python Developer", "Software Engineer",
}


def _analyzed_resume(text: str, *, persist_role_profile: bool = True) -> ResumeProfile:
    """Parse a resume and build the ORM row exactly as the analyze endpoint would."""
    parser = ResumeParser(Settings(openai_api_key=None))
    profile = parser.parse(text)
    apply_role_profile_compat(profile)
    return ResumeProfile(
        portal="linkedin", filename="r.txt", stored_path="r.txt", raw_text=text,
        skills=profile["skills"], experience=profile["experience"], education=profile["education"],
        job_titles=profile["job_titles"], keywords=profile["keywords"], technologies=profile["technologies"],
        domain_expertise=profile["domain_expertise"], detected_role=profile["detected_role"],
        role_suggestions=profile["role_suggestions"], generated_search_keywords=profile["generated_search_keywords"],
        generated_search_query=profile["generated_search_query"], role_confidence=profile["role_confidence"],
        domain_profile=profile["domain_profile"],
        role_profile=profile["role_profile"] if persist_role_profile else {},
    )


def _engine() -> FindMatchesEngine:
    return FindMatchesEngine(Settings(openai_api_key=None))


def _search_limits(engine: FindMatchesEngine, count: int) -> list[int]:
    """Reproduce the weighted candidate budget the batch runner computes (the top band doubles)."""
    base = 6
    return [base * engine.TOP_VARIATION_WEIGHT if i < engine.TOP_VARIATION_COUNT else base for i in range(count)]


class SearchPlanMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = _engine()
        self.resume = _analyzed_resume(FINANCE_RESUME)
        self.variations, self.plan = self.engine.build_search_plan(self.resume, "Remote", None)

    def test_role_profile_drives_the_plan(self) -> None:
        self.assertEqual(self.plan["role_source"], "role_profile")
        self.assertEqual(len(self.variations), MAX_SEARCH_ROLES)

    def test_variation_one_is_primary_role(self) -> None:
        self.assertEqual(self.variations[0].variation_index, 1)
        self.assertEqual(self.variations[0].role_priority, "primary")
        self.assertEqual(self.variations[0].role, "Financial Analyst")

    def test_variations_two_to_five_are_strong_fit(self) -> None:
        for v in self.variations[1:5]:
            self.assertEqual(v.role_priority, "strong_fit")
            self.assertEqual(v.role_category, "Strong Fit")

    def test_no_related_or_stretch_tier_is_searched(self) -> None:
        # Those tiers existed only to fill a twelve-slot layout; they are not searched at all now.
        self.assertFalse({v.role_priority for v in self.variations} & {"related", "stretch"})

    def test_variation_indices_are_deterministic_and_one_based(self) -> None:
        self.assertEqual(
            [v.variation_index for v in self.variations], list(range(1, MAX_SEARCH_ROLES + 1))
        )
        again, _ = self.engine.build_search_plan(_analyzed_resume(FINANCE_RESUME), "Remote", None)
        self.assertEqual([v.role for v in self.variations], [v.role for v in again])

    def test_each_variation_carries_full_role_metadata(self) -> None:
        for v in self.variations:
            self.assertTrue(v.query and v.role and v.role_category and v.domain and v.job_family)
            self.assertGreater(v.role_rank, 0)
            self.assertGreaterEqual(v.role_confidence, 0)

    def test_no_variation_is_a_broad_or_query(self) -> None:
        for v in self.variations:
            self.assertNotIn(" OR ", v.query)
        # display label is a single role, not an OR list.
        self.assertNotIn(" OR ", self.plan["display_query"])

    def test_controlled_synonym_expands_one_role_not_many(self) -> None:
        fp_a = next(v for v in self.variations if v.role == "FP&A Analyst")
        self.assertEqual(fp_a.query, "Financial Planning and Analysis Analyst")
        self.assertIn("FP&A Analyst", fp_a.synonyms)
        self.assertNotIn(" OR ", fp_a.query)


class SharedPlanAndAllocationTests(unittest.TestCase):
    def test_linkedin_and_dice_receive_the_same_ordered_plan(self) -> None:
        engine = _engine()
        resume = _analyzed_resume(FINANCE_RESUME)
        # build_search_plan is portal-independent: both portals consume this one ordered plan.
        first, _ = engine.build_search_plan(resume, "Remote", None)
        second, _ = engine.build_search_plan(resume, "Remote", None)
        self.assertEqual(
            [(v.variation_index, v.role, v.role_priority) for v in first],
            [(v.variation_index, v.role, v.role_priority) for v in second],
        )

    def test_the_primary_role_keeps_a_greater_search_allocation(self) -> None:
        engine = _engine()
        variations, _ = engine.build_search_plan(_analyzed_resume(FINANCE_RESUME), "Remote", None)
        limits = _search_limits(engine, len(variations))
        self.assertEqual(len(limits), MAX_SEARCH_ROLES)
        self.assertTrue(all(limits[0] > later for later in limits[1:]))


class DomainScopedRolesTests(unittest.TestCase):
    def _roles(self, text: str) -> list[str]:
        variations, _ = _engine().build_search_plan(_analyzed_resume(text), "Remote", None)
        return [v.role for v in variations]

    def test_finance_searches_finance_roles_not_data_or_ml(self) -> None:
        roles = self._roles(FINANCE_RESUME)
        self.assertEqual(roles[0], "Financial Analyst")
        self.assertFalse(FINANCE_EXCLUDED & set(roles))

    def test_software_resume_searches_software_roles(self) -> None:
        variations, _ = _engine().build_search_plan(_analyzed_resume(SOFTWARE_RESUME), "Remote", None)
        self.assertTrue(all(v.domain == "Software Engineering" for v in variations))

    def test_it_support_resume_searches_support_roles(self) -> None:
        variations, _ = _engine().build_search_plan(_analyzed_resume(IT_SUPPORT_RESUME), "Remote", None)
        self.assertTrue(all(v.domain == "IT Support" for v in variations))

    def test_data_resume_does_not_search_ml_or_data_engineering(self) -> None:
        roles = set(self._roles(DATA_RESUME))
        self.assertFalse({"Data Scientist", "Machine Learning Engineer", "Data Engineer", "ETL Developer"} & roles)

    def test_rare_domain_uses_only_credible_provisional_roles(self) -> None:
        variations, plan = _engine().build_search_plan(_analyzed_resume(ACTUARY_RESUME), "Remote", None)
        self.assertLessEqual(len(variations), MAX_SEARCH_ROLES)  # not padded to five
        self.assertGreaterEqual(len(variations), 1)
        self.assertEqual(variations[0].role, "Actuary")
        for v in variations:  # never forced into a supported domain
            self.assertIn("Provisional", v.domain)


class OldAnalysisFallbackTests(unittest.TestCase):
    def test_regenerates_role_profile_from_stored_resume_text(self) -> None:
        # An old row with no persisted role_profile but with raw_text -> regenerate.
        resume = _analyzed_resume(FINANCE_RESUME, persist_role_profile=False)
        variations, plan = _engine().build_search_plan(resume, "Remote", None)
        self.assertEqual(plan["role_source"], "regenerated")
        self.assertEqual(variations[0].role, "Financial Analyst")

    def test_falls_back_to_legacy_fields_when_regeneration_impossible(self) -> None:
        # No role_profile and no raw_text -> legacy ROLE_PROFILES path still produces a plan.
        resume = ResumeProfile(
            portal="linkedin", filename="legacy.txt", stored_path="legacy.txt", raw_text="",
            skills=["SQL", "Excel"], experience=[], education=[], job_titles=["Data Analyst"],
            keywords=[], technologies=["SQL"], domain_expertise=["Data Analytics"],
            detected_role="Data Analyst", role_suggestions=[{"role": "Data Analyst", "confidence": 80, "role_category": "primary_role"}],
            generated_search_keywords=["Data Analyst"], generated_search_query="Data Analyst", role_confidence=80,
            domain_profile={}, role_profile={},
        )
        variations, plan = _engine().build_search_plan(resume, "Remote", None)
        self.assertEqual(plan["role_source"], "legacy_fallback")
        self.assertTrue(variations)

    def test_manual_keywords_use_legacy_override_path(self) -> None:
        resume = _analyzed_resume(FINANCE_RESUME)
        variations, plan = _engine().build_search_plan(resume, "Remote", "Investment Banking Analyst")
        self.assertEqual(plan["role_source"], "manual_override")
        self.assertTrue(variations)


class CompatMappingTests(unittest.TestCase):
    """The visible fields the dashboard reads are mapped from role_profile (behavior 22)."""

    def setUp(self) -> None:
        parser = ResumeParser(Settings(openai_api_key=None))
        self.profile = parser.parse(FINANCE_RESUME)
        apply_role_profile_compat(self.profile)

    def test_detected_role_and_confidence_come_from_role_profile(self) -> None:
        self.assertEqual(self.profile["detected_role"], "Financial Analyst")
        self.assertGreaterEqual(self.profile["role_confidence"], 90.0)

    def test_role_suggestions_have_five_entries_in_two_groups(self) -> None:
        suggestions = self.profile["role_suggestions"]
        self.assertEqual(len(suggestions), MAX_SEARCH_ROLES)
        groups = {s["role_category"] for s in suggestions}
        self.assertEqual(groups, {"primary_role", "strong_fit_roles"})

    def test_domain_expertise_is_primary_plus_adjacent(self) -> None:
        self.assertEqual(self.profile["domain_expertise"][0], "Finance / Financial Analysis")

    def test_generated_query_is_primary_role_not_broad_or(self) -> None:
        self.assertEqual(self.profile["generated_search_query"], "Financial Analyst")
        self.assertNotIn(" OR ", self.profile["generated_search_query"])


class PersistenceTests(unittest.TestCase):
    """domain_profile and role_profile survive a DB round-trip (persistence + API reload/refresh)."""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()

    def tearDown(self) -> None:
        if getattr(self, "resume_id", None) is not None:
            self.db.execute(delete(ResumeProfile).where(ResumeProfile.id == self.resume_id))
            self.db.commit()
        self.db.close()

    def test_role_profile_survives_save_and_reload(self) -> None:
        resume = _analyzed_resume(FINANCE_RESUME)
        self.db.add(resume)
        self.db.commit()
        self.resume_id = resume.id
        self.db.expire_all()  # force a fresh read, as a page refresh / API reload would

        reloaded = self.db.get(ResumeProfile, self.resume_id)
        self.assertEqual(reloaded.role_profile["primary_role"], "Financial Analyst")
        self.assertEqual(len(reloaded.role_profile["roles"]), MAX_SEARCH_ROLES)
        self.assertEqual(reloaded.domain_profile["primary_domain"], "Finance / Financial Analysis")

        # Find Matches after the reload uses the same five roles, no re-upload needed.
        variations, plan = _engine().build_search_plan(reloaded, "Remote", None)
        self.assertEqual(plan["role_source"], "role_profile")
        self.assertEqual([v.role for v in variations][0], "Financial Analyst")

    def test_legacy_row_without_profiles_is_readable(self) -> None:
        # Simulate an old row: empty JSON profiles (the column default) must read back cleanly.
        resume = ResumeProfile(
            portal="linkedin", filename="old.txt", stored_path="old.txt", raw_text="",
            skills=[], experience=[], education=[], job_titles=[], keywords=[], technologies=[],
            domain_expertise=[], detected_role="Data Analyst", role_suggestions=[],
            generated_search_keywords=[], generated_search_query="Data Analyst", role_confidence=70,
        )
        self.db.add(resume)
        self.db.commit()
        self.resume_id = resume.id
        self.db.expire_all()
        reloaded = self.db.get(ResumeProfile, self.resume_id)
        self.assertEqual(reloaded.role_profile, {})
        self.assertEqual(reloaded.domain_profile, {})


class ReanalyzeReplacesLegacyOutputTests(unittest.TestCase):
    """Re-analyzing an already-uploaded resume must STORE the new profiles and the refreshed
    dashboard (GET /resumes/latest) must show AT MOST five roles in 1 Primary / 4 Strong Fit
    categories -- never the old twelve-role layout and never the old eight-role tech-biased
    output."""

    @classmethod
    def setUpClass(cls) -> None:
        init_db()

    def setUp(self) -> None:
        self.db = SessionLocal()
        self.ids: list[int] = []

    def tearDown(self) -> None:
        for rid in self.ids:
            self.db.execute(delete(ResumeProfile).where(ResumeProfile.id == rid))
        self.db.commit()
        self.db.close()
        reset_runtime_session()

    def test_reanalysis_stores_new_profiles_and_latest_shows_five_roles(self) -> None:
        # 1. A stale pre-Phase-4 row for this resume: empty profiles + the old eight-role output.
        legacy = ResumeProfile(
            portal="linkedin", filename="finance.txt", stored_path="finance.txt", raw_text=FINANCE_RESUME,
            skills=[], experience=[], education=[], job_titles=[], keywords=[], technologies=[],
            domain_expertise=["Data Analytics"], detected_role="Data Analyst",
            role_suggestions=[{"role": f"Legacy Role {i}", "confidence": 70, "role_category": "strong_fit_roles"} for i in range(8)],
            generated_search_keywords=["Data Analyst"], generated_search_query="Data Analyst OR BI Analyst",
            role_confidence=76, domain_profile={}, role_profile={},
        )
        self.db.add(legacy)
        self.db.commit()
        self.ids.append(legacy.id)

        # 2. Re-analyze the SAME resume exactly as the analyze endpoint does, then activate it.
        resume = _analyzed_resume(FINANCE_RESUME)
        self.db.add(resume)
        self.db.commit()
        self.ids.append(resume.id)
        get_runtime_session().activate_resume(resume.id)

        # 3. The dashboard's latest-resume source returns the NEW row with the new profiles.
        latest = latest_resume(self.db)
        self.assertEqual(latest.id, resume.id)
        self.assertEqual(latest.detected_role, "Financial Analyst")
        self.assertEqual(latest.role_profile["primary_role"], "Financial Analyst")
        self.assertEqual(latest.domain_profile["primary_domain"], "Finance / Financial Analysis")

        # Five roles, not the old 8 (nor the interim 12), grouped exactly 1 primary / 4 strong fit.
        suggestions = latest.role_suggestions
        self.assertEqual(len(suggestions), MAX_SEARCH_ROLES)
        categories = [s["role_category"] for s in suggestions]
        self.assertEqual(
            (
                categories.count("primary_role"),
                categories.count("strong_fit_roles"),
                categories.count("related_roles"),
                categories.count("stretch_roles"),
            ),
            (1, 4, 0, 0),
        )
        # The old tech-biased list is gone.
        self.assertNotIn("Legacy Role 0", [s["role"] for s in suggestions])


if __name__ == "__main__":
    unittest.main()
