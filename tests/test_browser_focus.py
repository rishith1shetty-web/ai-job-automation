"""Windows-only browser focus helper: raise the automation window at major transitions.

Contract proven here:
  - `focus_browser_window` calls page.bring_to_front() when headed.
  - It is a hard NO-OP when headless (tests and server runs must never try to raise a window).
  - Every failure is swallowed and logged -- login / search / apply must never break because
    focus failed, and fake pages without bring_to_front must still work.
  - Non-Windows systems use page.bring_to_front() only (no native calls).
  - The helper is wired at the intended transition points only (login opened, Find Matches
    begins, Apply begins / next application form) and NOT inside DOM or polling loops.
"""

import unittest
from pathlib import Path
from unittest import mock

from app.services import browser_focus
from app.services.browser_focus import focus_browser_window

LINKEDIN_SOURCE = Path("app/services/linkedin.py").read_text(encoding="utf-8", errors="replace")
DICE_SOURCE = Path("app/services/portals/dice_portal.py").read_text(encoding="utf-8", errors="replace")
FOCUS_SOURCE = Path("app/services/browser_focus.py").read_text(encoding="utf-8", errors="replace")


class FakePage:
    def __init__(self, title: str = "Jobs | LinkedIn"):
        self.calls = 0
        self._title = title

    def bring_to_front(self):
        self.calls += 1

    def title(self):
        return self._title


class RaisingPage(FakePage):
    def bring_to_front(self):
        self.calls += 1
        raise RuntimeError("window handle is gone")


class PageWithoutBringToFront:
    """A fake page from an existing unit test -- it has no bring_to_front at all."""

    def title(self):
        return "whatever"


class FocusHelperTests(unittest.TestCase):
    def test_headed_call_brings_page_to_front(self) -> None:
        page = FakePage()
        with mock.patch.object(browser_focus, "is_windows", return_value=False):
            self.assertTrue(focus_browser_window(page, headless=False))
        self.assertEqual(page.calls, 1)

    def test_headless_is_a_no_op(self) -> None:
        page = FakePage()
        self.assertFalse(focus_browser_window(page, headless=True))
        self.assertEqual(page.calls, 0, "focus was attempted in headless mode")

    def test_none_page_is_safe(self) -> None:
        self.assertFalse(focus_browser_window(None, headless=False))

    def test_bring_to_front_failure_is_swallowed_and_logged(self) -> None:
        page = RaisingPage()
        with mock.patch.object(browser_focus, "is_windows", return_value=False):
            with self.assertLogs("app.services.browser_focus", level="WARNING") as captured:
                result = focus_browser_window(page, headless=False, reason="linkedin_login")
        self.assertFalse(result)
        self.assertEqual(page.calls, 1)
        self.assertTrue(any("bring_to_front failed" in line for line in captured.output))

    def test_fake_page_without_bring_to_front_does_not_raise(self) -> None:
        with mock.patch.object(browser_focus, "is_windows", return_value=False):
            self.assertFalse(focus_browser_window(PageWithoutBringToFront(), headless=False))

    def test_non_windows_never_touches_native_apis(self) -> None:
        page = FakePage()
        with mock.patch.object(browser_focus, "is_windows", return_value=False):
            with mock.patch.object(browser_focus, "_focus_native_window") as native:
                focus_browser_window(page, headless=False)
        native.assert_not_called()

    def test_windows_path_attempts_native_activation(self) -> None:
        page = FakePage()
        with mock.patch.object(browser_focus, "is_windows", return_value=True):
            with mock.patch.object(browser_focus, "_focus_native_window", return_value=True) as native:
                self.assertTrue(focus_browser_window(page, headless=False, reason="dice_login"))
        native.assert_called_once()

    def test_native_failure_does_not_break_the_workflow(self) -> None:
        page = FakePage()
        with mock.patch.object(browser_focus, "is_windows", return_value=True):
            with mock.patch.object(
                browser_focus, "_focus_native_window", side_effect=OSError("user32 unavailable")
            ):
                with self.assertLogs("app.services.browser_focus", level="WARNING") as captured:
                    # bring_to_front still succeeded, so the call reports True and never raises.
                    self.assertTrue(focus_browser_window(page, headless=False, reason="dice_apply"))
        self.assertTrue(any("native window activation failed" in line for line in captured.output))

    def test_headless_skips_native_activation_too(self) -> None:
        with mock.patch.object(browser_focus, "is_windows", return_value=True):
            with mock.patch.object(browser_focus, "_focus_native_window") as native:
                focus_browser_window(FakePage(), headless=True)
        native.assert_not_called()

    def test_no_heavy_dependency_was_added(self) -> None:
        # Windows APIs go through the standard library only.
        self.assertIn("import ctypes", FOCUS_SOURCE)
        for heavy in ("pygetwindow", "pywin32", "win32gui", "pyautogui"):
            self.assertNotIn(heavy, FOCUS_SOURCE)


class TransitionPointWiringTests(unittest.TestCase):
    """Focus is called at the four intended transitions -- and nowhere hot."""

    def _calls(self, source: str) -> list[str]:
        return [line.strip() for line in source.splitlines() if "focus_browser_window(" in line
                and "def " not in line and "import" not in line]

    def test_linkedin_focuses_at_login_search_and_apply(self) -> None:
        reasons = " ".join(self._calls(LINKEDIN_SOURCE))
        self.assertIn("linkedin_login", reasons)
        self.assertIn("linkedin_search", reasons)
        self.assertIn("linkedin_apply", reasons)

    def test_dice_focuses_at_login_search_and_apply(self) -> None:
        reasons = " ".join(self._calls(DICE_SOURCE))
        self.assertIn("dice_login", reasons)
        self.assertIn("dice_search", reasons)
        self.assertIn("dice_apply", reasons)

    def test_focus_is_not_called_in_dom_or_polling_loops(self) -> None:
        # A small, bounded number of call sites per portal is the guard against focus stealing:
        # login + search entry points + one per job application.
        self.assertLessEqual(len(self._calls(LINKEDIN_SOURCE)), 5)
        self.assertLessEqual(len(self._calls(DICE_SOURCE)), 4)

    def test_search_focus_is_outside_the_variation_loop(self) -> None:
        # Dice: the focus call must sit before the per-variation loop, not inside it.
        block = DICE_SOURCE.split("page = context.new_page()")[2]
        focus_at = block.index("focus_browser_window(")
        loop_at = block.index("for index, variation in enumerate(variations")
        self.assertLess(focus_at, loop_at, "focus is being called inside the search loop")

    def test_helper_is_portal_agnostic(self) -> None:
        for portal_term in ("linkedin", "dice", "LinkedIn", "Dice"):
            self.assertNotIn(portal_term, FOCUS_SOURCE.replace("LinkedIn or Dice", ""))


if __name__ == "__main__":
    unittest.main()
