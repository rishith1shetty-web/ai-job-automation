"""Regression test for the Dice Find Matches zero-result bug.

Verified from real authenticated diagnostics (search_1_diagnostics.json):
    result_card_containers_found: 112
    cards_scanned_by_extractor:   7
    parsed_jobs:                  7
    easy_apply_identified:        0   <-- first stage where the count becomes zero

Root cause proven from search_1_loaded.html: Dice renders the native badge as
    <div class="... sm:hidden" aria-labelledby="easyApply-label">
        <p id="easyApply-label">Easy Apply</p>
    </div>
The container is `sm:hidden` (display:none on a desktop headless browser), so it is
excluded from card.innerText, and the old apply-CTA selectors (class/button/data-cy based)
never matched the id/aria-labelledby badge. So apply_cta was "" and the card text held no
"Easy Apply", making _classify_dice_apply return no_easy_apply_signal for every card, which
the Easy Apply filter then dropped -> 0 results.

Fix: the DOM extractor's apply-CTA capture now matches the stable id / aria-labelledby badge
markers and reads textContent (which works even when the element is display:none).
"""

import re
import unittest
from pathlib import Path

from app.config import Settings
from app.services.portals.dice_portal import DicePortalAdapter

EXTRACTOR_SOURCE = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")


class DiceEasyApplyBadgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = DicePortalAdapter(Settings(openai_api_key=None))

    def test_badge_text_is_classified_as_native_easy_apply(self) -> None:
        # This is exactly what the fixed extractor now feeds classify for a genuine Easy Apply
        # card (the badge's textContent), even though the badge was sm:hidden in the DOM.
        is_easy, reason = self.adapter._classify_dice_apply(
            card_text="Senior Healthcare Data Analyst\nE-Solutions, Inc.\nRemote",
            cta_text="Easy Apply",
            apply_url="",
        )
        self.assertTrue(is_easy)
        self.assertTrue(reason.startswith("native_cta"))

    def test_quick_apply_badge_is_native(self) -> None:
        is_easy, _ = self.adapter._classify_dice_apply(
            card_text="Data Analyst\nAcme", cta_text="Quick Apply", apply_url=""
        )
        self.assertTrue(is_easy)

    def test_card_without_badge_is_not_easy_apply(self) -> None:
        # A non-Easy-Apply card (no badge element -> empty cta, no badge text in card text)
        # must still be classified as not easy apply, so the fix does not create false positives.
        is_easy, reason = self.adapter._classify_dice_apply(
            card_text="Principal Data Analyst\nCapital One\nRichmond, Virginia",
            cta_text="",
            apply_url="",
        )
        self.assertFalse(is_easy)
        self.assertEqual(reason, "no_easy_apply_signal")

    def test_extractor_matches_the_id_and_aria_labelledby_badge(self) -> None:
        # Guard the fix: the apply-CTA selector must target the stable easyApply / quickApply
        # id and aria-labelledby markers, not only the stale class/button selectors.
        self.assertRegex(EXTRACTOR_SOURCE, r"\[id\*='easyApply' i\]")
        self.assertRegex(EXTRACTOR_SOURCE, r"\[aria-labelledby\*='easyApply' i\]")
        self.assertIn("quickApply", EXTRACTOR_SOURCE)

    def test_extractor_reads_textcontent_for_hidden_badge(self) -> None:
        # textContent must be read (it works for display:none elements); it must come before
        # innerText so the sm:hidden badge is not lost.
        self.assertIn("el.textContent || el.innerText", EXTRACTOR_SOURCE)


if __name__ == "__main__":
    unittest.main()
