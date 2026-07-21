"""Weighted top-five collection: search-effort budget + accepted-result soft allocation.

Two related but separate controls, both derived from ONE weighting (variations 1..5 -> weight 2,
6+ -> weight 1) applied to the user's Find 20/50/100/custom target:
  * candidate/search effort  -- per-variation search limits (top five inspect more);
  * accepted-result quota    -- weighted soft allocation with reallocation, capped at the target.
Relevance/eligibility are never weakened; a weak variation honestly returns fewer jobs.
"""

import unittest

from app.config import Settings
from app.services.find_matches import (
    FindMatchesEngine,
    SearchVariation,
    per_variation_limits,
)


def _variation(index: int) -> SearchVariation:
    return SearchVariation(query=f"Role {index}", location="US", role=f"Role {index}",
                           expanded_role=f"Role {index}", pass_name="p", variation_index=index)


class SoftAllocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_top_five_receive_a_greater_share_than_later_variations(self) -> None:
        alloc = self.engine.soft_allocation(100, 15)
        self.assertEqual(alloc[:5], [10, 10, 10, 10, 10])
        self.assertTrue(all(value == 5 for value in alloc[5:]))
        self.assertGreater(min(alloc[:5]), max(alloc[5:]))

    def test_total_never_exceeds_the_requested_limit(self) -> None:
        for target in (20, 50, 100, 37, 73):
            for n in (4, 8, 15, 24):
                alloc = self.engine.soft_allocation(target, n)
                self.assertLessEqual(sum(alloc), target, msg=f"target={target} n={n}")

    def test_every_limit_uses_the_same_proportional_weighting(self) -> None:
        # Find 20 / 50 / 100 all split by the same 2:1 weighting, not a hard-coded number.
        self.assertEqual(self.engine.soft_allocation(20, 4), [5, 5, 5, 5])          # all top-tier
        self.assertEqual(self.engine.soft_allocation(50, 8), [8, 8, 8, 8, 8, 4, 3, 3])
        top, rest = self.engine.soft_allocation(100, 15)[:5], self.engine.soft_allocation(100, 15)[5:]
        self.assertEqual(top, [10] * 5)
        self.assertEqual(rest, [5] * 10)

    def test_every_selected_variation_gets_at_least_one_slot(self) -> None:
        # Lower-priority variations are still represented (they are still searched).
        alloc = self.engine.soft_allocation(100, 20)
        self.assertTrue(all(value >= 1 for value in alloc))
        self.assertEqual(len(alloc), 20)


class WeightedTakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_one_prolific_variation_cannot_consume_the_whole_target(self) -> None:
        # Variation 1 has 100 eligible jobs but others also have supply: it is capped at its
        # soft quota, leaving room for the rest.
        take = self.engine.weighted_take([100] * 15, 100)
        self.assertEqual(take[0], 10)
        self.assertEqual(sum(take), 100)

    def test_unused_capacity_is_reallocated_to_higher_priority_first(self) -> None:
        # Variation 1 can only supply 2; its unused 8-slot budget goes to variation 2 (the next
        # highest priority with spare), not to a lower-priority one.
        take = self.engine.weighted_take([2] + [50] * 14, 100)
        self.assertEqual(take[0], 2)
        self.assertEqual(take[1], 18)          # 10 soft + 8 reallocated from variation 1
        self.assertGreater(take[1], take[2])   # higher priority filled before lower
        self.assertEqual(sum(take), 100)

    def test_total_taken_never_exceeds_the_target(self) -> None:
        for target in (20, 50, 100):
            self.assertLessEqual(sum(self.engine.weighted_take([50] * 15, target)), target)

    def test_returns_fewer_honestly_when_eligible_supply_is_short(self) -> None:
        # Only 3 eligible jobs exist across everything -> exactly 3 are taken, never padded to 100.
        take = self.engine.weighted_take([1, 1, 1], 100)
        self.assertEqual(take, [1, 1, 1])
        self.assertEqual(sum(take), 3)

    def test_never_takes_more_than_a_variation_actually_has(self) -> None:
        take = self.engine.weighted_take([3, 0, 7, 0, 0], 100)
        for taken, available in zip(take, [3, 0, 7, 0, 0]):
            self.assertLessEqual(taken, available)


class SearchEffortWeightingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_top_five_variations_get_a_larger_candidate_budget(self) -> None:
        base = 10
        limits = [
            base * self.engine.TOP_VARIATION_WEIGHT if i < self.engine.TOP_VARIATION_COUNT else base
            for i in range(8)
        ]
        self.assertEqual(limits, [20, 20, 20, 20, 20, 10, 10, 10])
        self.assertTrue(all(limits[i] > limits[5] for i in range(5)))  # top-5 strictly larger

    def test_per_variation_limits_accepts_int_or_list(self) -> None:
        self.assertEqual(per_variation_limits(5, 3), [5, 5, 5])          # uniform
        self.assertEqual(per_variation_limits([20, 20, 10, 10], 3), [20, 20, 10])  # truncate
        self.assertEqual(per_variation_limits([20, 10], 4), [20, 10, 10, 10])      # pad last


class VariationIndexMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = FindMatchesEngine(Settings(openai_api_key=None))

    def test_enrich_records_the_discovering_variation_index(self) -> None:
        raw = {"portal": "dice", "title": "T", "company": "C", "location": "US",
               "description": "d", "job_url": "", "easy_apply": True,
               "job_description_metadata": {"posted_date": "Today"}}
        enriched = self.engine.enrich_raw_job(raw, _variation(3))
        self.assertEqual(enriched["job_description_metadata"]["variation_index"], 3)

    def test_build_search_plan_assigns_ascending_one_based_indices(self) -> None:
        from app.models import ResumeProfile

        resume = ResumeProfile(
            portal="linkedin", filename="r.txt", stored_path="r.txt",
            raw_text="Python SQL data analyst", skills=["Python", "SQL"], experience=[],
            education=[], job_titles=["Data Analyst"], keywords=["analytics"],
            technologies=["Python", "SQL"], domain_expertise=["Data Analytics"],
            detected_role="Data Analyst", generated_search_keywords=["Data Analyst"],
            generated_search_query="Data Analyst", role_confidence=90,
        )
        variations, _ = self.engine.build_search_plan(resume, "United States", None)
        self.assertTrue(variations)
        self.assertEqual([v.variation_index for v in variations], list(range(1, len(variations) + 1)))


if __name__ == "__main__":
    unittest.main()
