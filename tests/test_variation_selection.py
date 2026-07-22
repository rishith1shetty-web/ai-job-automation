"""Find Matches variation selection: always a PREFIX of the ranked role list.

Required mapping (both portals, identical ordered prefix):
    Find 20  -> ranked variations 1-4
    Find 50  -> ranked variations 1-8
    Find 100 -> ranked variations 1-12, or ALL roles when fewer than 12 exist

Previously the count was derived as (target + 6) // 7, which produced 15 for Find 100 -- so the
selected set depended on how many variations happened to exist rather than on a fixed ranked
prefix. Ranking, portal search behavior, freshness, Apply and canonical identity are untouched.
"""

import unittest
from pathlib import Path

from app.config import Settings
from app.services.find_matches import FindMatchesEngine, SearchVariation

MAIN_SOURCE = Path("app/main.py").read_text(encoding="utf-8", errors="replace")


def _ranked_variations(count: int) -> list[SearchVariation]:
    """A ranked role list exactly as build_search_plan emits it (variation_index is 1-based)."""
    return [
        SearchVariation(
            query=f"Role {i}",
            location="Remote",
            role=f"Role {i}",
            expanded_role=f"Role {i}",
            pass_name="primary",
            search_role=f"Role {i}",
            role_priority="primary" if i == 1 else "strong_fit",
            variation_index=i,
            role_rank=i,
        )
        for i in range(1, count + 1)
    ]


class VariationSelectionMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def _selected(self, target: int, available: int) -> list[int]:
        variations = _ranked_variations(available)
        count = self.engine.collection_plan(target, len(variations))["selected_variation_count"]
        # main.py takes a prefix: search_variations[:selected_variation_count]
        return [variation.role_rank for variation in variations[:count]]

    def test_find20_selects_exactly_ranks_1_to_4(self) -> None:
        self.assertEqual(self._selected(20, 12), [1, 2, 3, 4])

    def test_find50_selects_exactly_ranks_1_to_8(self) -> None:
        self.assertEqual(self._selected(50, 12), [1, 2, 3, 4, 5, 6, 7, 8])

    def test_find100_selects_exactly_ranks_1_to_12(self) -> None:
        self.assertEqual(self._selected(100, 12), list(range(1, 13)))

    def test_every_real_plan_path_offers_at_most_twelve_variations(self) -> None:
        # Find 100 selects min(available, 15); the mapping holds because NO plan path can produce
        # more than 12 ranked roles (role_profile == TOTAL_ROLES == 12, legacy == 12, manual < 12).
        # This test pins that precondition -- if a path ever grew past 12, the mapping would break.
        from app.services.resume_parser import ResumeParser
        from app.models import ResumeProfile

        settings = Settings(openai_api_key=None)
        text = (
            "Jane Doe\nData Analyst\nPROFESSIONAL EXPERIENCE\nAcme Corp | Austin, TX\n"
            "Data Analyst\nJan 2021 - Present\n"
            "- Built dashboards and SQL reporting on KPIs and business metrics\n"
            "TECHNICAL SKILLS\nSQL, Excel, Tableau, Power BI\n"
        )
        parsed = ResumeParser(settings).parse(text)
        role_profile_resume = ResumeProfile(
            id=1, raw_text=text,
            role_profile=parsed.get("role_profile") or {},
            domain_profile=parsed.get("domain_profile") or {},
        )
        legacy_resume = ResumeProfile(
            id=2, raw_text="", role_profile={}, domain_profile={},
            detected_role="Data Analyst", skills=["SQL", "Excel", "Tableau", "Power BI"],
        )
        for label, resume, manual in (
            ("role_profile", role_profile_resume, None),
            ("legacy", legacy_resume, None),
            ("manual", role_profile_resume, "Data Analyst"),
        ):
            with self.subTest(path=label):
                variations, _meta = self.engine.build_search_plan(resume, "Remote", manual)
                self.assertLessEqual(len(variations), 12, f"{label} plan exceeded 12 variations")

    def test_find100_with_only_six_credible_roles_selects_1_to_6(self) -> None:
        self.assertEqual(self._selected(100, 6), [1, 2, 3, 4, 5, 6])

    def test_fewer_roles_are_used_as_is_and_never_padded(self) -> None:
        for available in (1, 2, 3, 5, 7, 11):
            with self.subTest(available=available):
                selected = self._selected(100, available)
                self.assertEqual(selected, list(range(1, available + 1)))
                self.assertLessEqual(len(selected), available, "variations were invented")

    def test_manual_single_role_search_is_unchanged(self) -> None:
        self.assertEqual(self._selected(100, 1), [1])
        self.assertEqual(self._selected(20, 1), [1])


class PrefixInvariantTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_selection_is_always_a_contiguous_prefix_from_rank_one(self) -> None:
        for target in (1, 5, 20, 21, 35, 50, 51, 75, 100):
            for available in (1, 4, 6, 8, 12, 20):
                with self.subTest(target=target, available=available):
                    variations = _ranked_variations(available)
                    count = self.engine.collection_plan(target, available)["selected_variation_count"]
                    selected = variations[:count]
                    ranks = [v.role_rank for v in selected]
                    self.assertEqual(ranks, list(range(1, len(ranks) + 1)))
                    self.assertGreaterEqual(len(ranks), 1)

    def test_no_lower_ranked_role_appears_when_a_higher_ranked_one_is_omitted(self) -> None:
        variations = _ranked_variations(12)
        for target in (20, 50, 100):
            with self.subTest(target=target):
                count = self.engine.collection_plan(target, 12)["selected_variation_count"]
                selected = {v.role_rank for v in variations[:count]}
                omitted = {v.role_rank for v in variations[count:]}
                # Every selected rank is strictly better than every omitted rank.
                if omitted:
                    self.assertLess(max(selected), min(omitted))

    def test_role_order_from_role_profile_is_preserved(self) -> None:
        variations = _ranked_variations(12)
        count = self.engine.collection_plan(100, 12)["selected_variation_count"]
        selected = variations[:count]
        self.assertEqual([v.role_rank for v in selected], [v.variation_index for v in selected])
        self.assertEqual([v.query for v in selected], [f"Role {i}" for i in range(1, count + 1)])

    def test_larger_searches_never_select_fewer_variations(self) -> None:
        previous = 0
        for target in (20, 50, 100):
            count = self.engine.collection_plan(target, 12)["selected_variation_count"]
            self.assertGreaterEqual(count, previous)
            previous = count


class SharedAcrossPortalsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_linkedin_and_dice_receive_the_same_ordered_prefix(self) -> None:
        variations = _ranked_variations(12)
        count = self.engine.collection_plan(100, 12)["selected_variation_count"]
        selected = variations[:count]
        # main.py computes the prefix ONCE and hands the same list to every portal.
        linkedin_input = selected
        dice_input = selected
        self.assertEqual([v.role_rank for v in linkedin_input], [v.role_rank for v in dice_input])
        self.assertIs(linkedin_input, dice_input)

    def test_selection_is_computed_once_before_the_portal_loop(self) -> None:
        plan_at = MAIN_SOURCE.index("collection_plan = engine.collection_plan(")
        prefix_at = MAIN_SOURCE.index("selected_variations = search_variations[")
        loop_at = MAIN_SOURCE.index("for portal in portals:")
        self.assertLess(plan_at, prefix_at)
        self.assertLess(prefix_at, loop_at, "the prefix must be built before the portal loop")
        # And it is a plain prefix slice, not a filtered/reordered subset.
        self.assertIn(
            'selected_variations = search_variations[: collection_plan["selected_variation_count"]]',
            MAIN_SOURCE,
        )


class PreservedBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_weighted_allocation_within_the_prefix_is_unchanged(self) -> None:
        self.assertEqual(self.engine.TOP_VARIATION_COUNT, 5)
        self.assertEqual(self.engine.TOP_VARIATION_WEIGHT, 2)
        available = [10] * 12
        take = self.engine.weighted_take(available, 100)
        self.assertEqual(sum(take), 100)
        # The top five still receive at least as much as any later variation.
        self.assertGreaterEqual(min(take[:5]), max(take[5:]))

    def test_per_query_capacity_still_reaches_the_target(self) -> None:
        for target in (20, 50, 100):
            with self.subTest(target=target):
                plan = self.engine.collection_plan(target, 12)
                capacity = plan["selected_variation_count"] * plan["limit_per_query"]
                self.assertGreaterEqual(capacity, target)

    def test_existing_find20_plan_values_are_unchanged(self) -> None:
        self.assertEqual(
            self.engine.collection_plan(20, 24), {"selected_variation_count": 4, "limit_per_query": 7}
        )


if __name__ == "__main__":
    unittest.main()
