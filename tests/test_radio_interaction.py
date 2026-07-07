"""Phase 1 tests: LinkedIn radio/checkbox reliability.

These tests exercise the trusted-first interaction wiring and the
verification-driven selection loop in ``LinkedInAutomation`` without a real
browser, by faking the Playwright ``Locator`` / ``ElementHandle`` surface that
the code touches.
"""

import unittest
from unittest.mock import MagicMock, patch

from app.config import Settings
from app.services.linkedin import LinkedInAutomation


class FakePage:
    def wait_for_timeout(self, _ms):
        return None


class FakeElement:
    """Minimal ElementHandle stand-in.

    A trusted ``click`` / ``check`` flips shared ``committed`` state, which the
    fake verify script then reports as an accepted selection.
    """

    def __init__(self, shared, *, target=None):
        self.shared = shared
        self._target = target  # element returned by evaluate_handle (click target)

    def scroll_into_view_if_needed(self, timeout=None):
        return None

    def click(self, force=False, timeout=None):
        self.shared["committed"] = True

    def check(self, force=False, timeout=None):
        self.shared["committed"] = True

    def evaluate_handle(self, _script):
        return FakeHandle(self._target if self._target is not None else self)


class FakeHandle:
    def __init__(self, element):
        self._element = element

    def as_element(self):
        return self._element


class FakeField:
    """Locator stand-in that routes the engine's scripts to fake behaviour."""

    def __init__(self, shared, *, matched_element, verify_never_commits=False):
        self.shared = shared
        self._matched_element = matched_element
        self._verify_never_commits = verify_never_commits

    def evaluate_handle(self, script, _arg=None):
        if "found[0]" in script:  # _choice_match_input_script
            return FakeHandle(self._matched_element)
        return FakeHandle(None)

    def evaluate(self, script, _arg=None):
        if "matches_target" in script:  # _choice_verify_script
            committed = self.shared.get("committed") and not self._verify_never_commits
            return {
                "checked": bool(committed),
                "selected_label": "Yes" if committed else "",
                "matches_target": bool(committed),
                "validation_messages": [] if committed else ["Please make a selection"],
                "selection_error": not committed,
                "accepted": bool(committed),
            }
        return {}


class RadioInteractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(openai_api_key=None)
        self.automation = LinkedInAutomation(self.settings)

    def test_native_trusted_click_selects_and_verifies(self) -> None:
        shared = {"committed": False}
        label = FakeElement(shared)  # visible label acts as the click target
        matched_input = FakeElement(shared, target=label)
        field = FakeField(shared, matched_element=matched_input)

        result = self.automation._click_choice_control_native(
            field, "Yes", ["Yes", "No"], page=FakePage()
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["interaction_path"], "playwright_native")
        self.assertEqual(result["click_strategy_used"], "label_click")
        self.assertTrue(result["react_state_committed"])
        self.assertEqual(len(result["attempts"]), 1)  # accepted on first trusted click

    def test_native_no_match_reports_failure(self) -> None:
        shared = {"committed": False}
        field = FakeField(shared, matched_element=None)

        result = self.automation._click_choice_control_native(field, "Yes", ["Yes", "No"])

        self.assertFalse(result["success"])
        self.assertIn("No radio/checkbox option matched", result["reason"])

    def test_native_uncommitted_state_is_not_accepted(self) -> None:
        # React never commits -> verification keeps failing -> all strategies tried,
        # success stays False so the caller can fall back.
        shared = {"committed": False}
        label = FakeElement(shared)
        matched_input = FakeElement(shared, target=label)
        field = FakeField(shared, matched_element=matched_input, verify_never_commits=True)

        result = self.automation._click_choice_control_native(
            field, "Yes", ["Yes", "No"], page=FakePage()
        )

        self.assertFalse(result["success"])
        self.assertEqual(len(result["attempts"]), 3)  # exhausts every trusted strategy
        self.assertIn("did not commit", result["reason"])

    def test_click_choice_control_prefers_native_and_skips_writer(self) -> None:
        native_result = {"success": True, "interaction_path": "playwright_native"}
        with patch.object(self.automation, "_click_choice_control_native", return_value=native_result) as native, patch.object(
            self.automation, "_click_choice_control_with_writer"
        ) as writer:
            result = self.automation._click_choice_control(object(), "Yes", ["Yes", "No"])

        native.assert_called_once()
        writer.assert_not_called()
        self.assertEqual(result["interaction_path"], "playwright_native")

    def test_click_choice_control_falls_back_to_writer(self) -> None:
        native_result = {"success": False, "reason": "not committed"}
        writer_result = {"success": True}
        with patch.object(self.automation, "_click_choice_control_native", return_value=native_result), patch.object(
            self.automation, "_click_choice_control_with_writer", return_value=writer_result
        ) as writer:
            result = self.automation._click_choice_control(object(), "Yes", ["Yes", "No"])

        writer.assert_called_once()
        self.assertEqual(result["interaction_path"], "js_writer_fallback")
        self.assertEqual(result["native_attempt"], native_result)

    def test_native_click_helper_uses_trusted_click(self) -> None:
        element = MagicMock()
        self.assertTrue(self.automation._native_click_element(element, force=False))
        element.scroll_into_view_if_needed.assert_called_once()
        element.click.assert_called_once_with(force=False, timeout=4000)

    def test_native_check_helper_uses_trusted_check(self) -> None:
        element = MagicMock()
        self.assertTrue(self.automation._native_check_element(element))
        element.check.assert_called_once_with(force=True, timeout=4000)


if __name__ == "__main__":
    unittest.main()
