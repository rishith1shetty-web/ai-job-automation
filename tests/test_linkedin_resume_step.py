"""LinkedIn Easy Apply resume step: select the existing matching resume instead of
uploading a duplicate, and never click Next until the RIGHT resume is verifiably selected.

The fake modal dispatches on the adapter's real JS constants, so the production selection /
verification logic is exercised — only the browser is faked.
"""

import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services.linkedin import LinkedInAutomation
from app.services.manual_assist import (
    ManualAssistCoordinator,
    ManualDecision,
    ManualOutcome,
)

EXPECTED = "Jane_Doe_Resume.pdf"
OTHER = "Old_Resume_2019.pdf"


@dataclass
class FakeJob:
    id: int = 5
    title: str = "Data Analyst"
    company: str = "Acme"
    job_url: str = "https://linkedin.com/jobs/view/5"


def _base(name) -> str:
    return Path(str(name or "")).name.strip().lower()


class FakePage:
    def __init__(self) -> None:
        self.waits = 0

    def wait_for_timeout(self, _ms) -> None:
        self.waits += 1


class FakeModal:
    """Implements only what the resume-step helpers evaluate against the live DOM."""

    def __init__(self, cards, busy_ticks: int = 0) -> None:
        self.cards = [dict(c) for c in cards]
        self.busy_ticks = busy_ticks
        self.clicked: list[str] = []
        self.card_reads = 0
        self.open = True
        self.page = FakePage()

    def evaluate(self, script, arg=None):
        if script == LinkedInAutomation._RESUME_BUSY_JS:
            if self.busy_ticks < 0:
                return True                      # spinner never clears
            if self.busy_ticks > 0:
                self.busy_ticks -= 1
                return True
            return False
        if script == LinkedInAutomation._RESUME_CARDS_JS:
            self.card_reads += 1
            # Cards are only readable once the spinner has cleared, as in the real DOM.
            return [dict(c) for c in self.cards]
        if script == LinkedInAutomation._CLICK_RESUME_CARD_JS:
            for card in self.cards:
                if _base(card["filename"]) == _base(arg):
                    for other in self.cards:
                        other["selected"] = False
                    card["selected"] = True
                    self.clicked.append(card["filename"])
                    return True
            return False
        raise AssertionError(f"unexpected script: {script[:40]}")


class DeadModal:
    open = False

    def __init__(self) -> None:
        self.page = FakePage()

    def evaluate(self, *_a, **_k):
        raise RuntimeError("page closed")


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.auto = LinkedInAutomation(get_settings())
        # Stub the two collaborators the verifier consults, so tests control them precisely.
        self.validation: list[str] = []
        self.nav = {"locator": object(), "enabled": True, "text": "Next"}
        self.auto._validation_messages = lambda modal: list(self.validation)
        self.auto._find_navigation_button = lambda modal: dict(self.nav)


class AutomaticSelectionTests(_Base):
    def test_1_matching_existing_resume_is_selected_automatically(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        self.assertTrue(self.auto._select_existing_resume(modal, EXPECTED))
        self.assertEqual(modal.clicked, [EXPECTED])

    def test_5_with_two_resumes_the_correct_one_is_selected_and_the_wrong_one_ignored(self) -> None:
        modal = FakeModal([
            {"filename": OTHER, "selected": False},
            {"filename": EXPECTED, "selected": False},
        ])
        self.assertTrue(self.auto._select_existing_resume(modal, EXPECTED))
        self.assertEqual(modal.clicked, [EXPECTED])       # never touched the old resume
        selected = [c["filename"] for c in modal.cards if c["selected"]]
        self.assertEqual(selected, [EXPECTED])

    def test_already_selected_correct_resume_is_left_alone(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": True}])
        self.assertTrue(self.auto._select_existing_resume(modal, EXPECTED))
        self.assertEqual(modal.clicked, [])               # no needless re-click

    def test_2_spinner_is_awaited_before_the_selection_is_trusted(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}], busy_ticks=3)
        self.assertTrue(self.auto._select_existing_resume(modal, EXPECTED))
        self.assertGreater(modal.page.waits, 0)           # it polled instead of sleeping blindly

    def test_2b_next_is_not_trusted_while_linkedin_is_still_busy(self) -> None:
        # Spinner never clears within the bounded wait -> we must NOT advance.
        modal = FakeModal([{"filename": EXPECTED, "selected": True}], busy_ticks=-1)
        self.assertFalse(self.auto._wait_for_resume_ready(modal, timeout_ms=120))
        self.assertGreater(modal.page.waits, 0)          # bounded polling, not a blind sleep

    def test_3_cards_are_re_read_after_the_post_click_rerender(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        self.auto._select_existing_resume(modal, EXPECTED)
        # Read before the click, and re-resolved again after the re-render.
        self.assertGreaterEqual(modal.card_reads, 2)

    def test_4_exact_filename_match_is_required(self) -> None:
        modal = FakeModal([{"filename": "Jane_Doe_Resume (1).pdf", "selected": False}])
        self.assertFalse(self.auto._select_existing_resume(modal, EXPECTED))
        self.assertEqual(modal.clicked, [])               # no near-miss card is ever clicked


class VerificationTests(_Base):
    def test_6_highlight_without_a_real_selection_is_rejected(self) -> None:
        # The card is visible (and would look highlighted) but nothing is actually checked.
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        self.assertFalse(self.auto._resume_selection_verified(modal, EXPECTED))

    def test_a_different_selected_resume_is_rejected(self) -> None:
        modal = FakeModal([
            {"filename": EXPECTED, "selected": False},
            {"filename": OTHER, "selected": True},
        ])
        self.assertFalse(self.auto._resume_selection_verified(modal, EXPECTED))

    def test_correct_selected_resume_is_accepted(self) -> None:
        modal = FakeModal([
            {"filename": OTHER, "selected": False},
            {"filename": EXPECTED, "selected": True},
        ])
        self.assertTrue(self.auto._resume_selection_verified(modal, EXPECTED))

    def test_resume_validation_error_blocks_verification(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": True}])
        self.validation = ["Please attach a resume document"]
        self.assertFalse(self.auto._resume_selection_verified(modal, EXPECTED))

    def test_disabled_next_blocks_verification(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": True}])
        self.nav = {"locator": object(), "enabled": False, "text": "Next"}
        self.assertFalse(self.auto._resume_selection_verified(modal, EXPECTED))

    def test_unreadable_modal_is_never_treated_as_success(self) -> None:
        self.assertFalse(self.auto._resume_selection_verified(DeadModal(), EXPECTED))
        self.assertFalse(self.auto._select_existing_resume(DeadModal(), EXPECTED))

    def test_ambiguous_multi_selection_is_rejected(self) -> None:
        modal = FakeModal([
            {"filename": EXPECTED, "selected": True},
            {"filename": OTHER, "selected": True},
        ])
        self.assertFalse(self.auto._resume_selection_verified(modal, EXPECTED))


class NoDuplicateUploadTests(_Base):
    class _Inputs:
        def __init__(self) -> None:
            self.uploaded: list = []

        def count(self) -> int:
            return 1

        def nth(self, _i):
            return self

        def set_input_files(self, payload) -> None:
            self.uploaded.append(payload)

    def test_13_matching_card_present_means_no_duplicate_upload(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        inputs = self._Inputs()
        modal.locator = lambda _sel: inputs
        modal.inner_text = lambda timeout=None: f"{EXPECTED}\n{OTHER}"

        diagnostics = {"resume_original_filename": EXPECTED}
        uploaded = self.auto._upload_resume_if_needed(
            modal, Path("resume.pdf"), diagnostics=diagnostics
        )

        self.assertFalse(uploaded)
        self.assertEqual(inputs.uploaded, [])             # nothing re-uploaded
        self.assertTrue(diagnostics["existing_resume_selected"])
        self.assertFalse(diagnostics["mismatch_detected"])
        self.assertTrue(diagnostics["upload_success"])


class ResumeStepManualAssistTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.coordinator = ManualAssistCoordinator()
        self.auto.attach_manual_assist(self.coordinator, batch_id="batch-1")
        self.outcome: ManualOutcome | None = None

    def _pause(self, modal) -> threading.Thread:
        def run() -> None:
            self.outcome = self.auto._resume_step_manual_assist(
                modal, FakeJob(), True,
                {"resume_original_filename": EXPECTED,
                 "reason_if_failed": "could not confirm selection"},
                [],
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await(0)
        return thread

    def _await(self, after: int) -> dict:
        for _ in range(500):
            state = self.coordinator.snapshot()
            if state and state["sequence"] > after:
                return state
            threading.Event().wait(0.01)
        self.fail("never paused")

    def test_7_8_unverifiable_selection_pauses_with_the_modal_still_open(self) -> None:
        modal = FakeModal([
            {"filename": EXPECTED, "selected": False},
            {"filename": OTHER, "selected": False},
        ])
        thread = self._pause(modal)

        self.assertTrue(thread.is_alive())                # batch parked, browser NOT closed
        self.assertTrue(modal.open)
        state = self.coordinator.snapshot()
        self.assertIn(EXPECTED, state["question"])
        self.assertIn(EXPECTED, state["reason"])

        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=3)

    def test_9_user_selects_the_correct_resume_and_continue_resumes(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        thread = self._pause(modal)

        modal.cards[0]["selected"] = True                 # user clicks the right card by hand
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=3)

        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)
        self.assertTrue(modal.open)

    def test_10_user_selects_the_wrong_resume_and_continue_re_pauses(self) -> None:
        modal = FakeModal([
            {"filename": EXPECTED, "selected": False},
            {"filename": OTHER, "selected": False},
        ])
        thread = self._pause(modal)
        first = self.coordinator.snapshot()["sequence"]

        modal.cards[1]["selected"] = True                 # WRONG resume selected
        self.coordinator.resolve(ManualDecision.CONTINUE)
        state = self._await(first)                        # identity safety: re-pauses

        self.assertEqual(state["attempt"], 2)
        self.assertTrue(thread.is_alive())
        self.assertIsNone(self.outcome)                   # never resumed with the wrong resume

        modal.cards[1]["selected"] = False
        modal.cards[0]["selected"] = True                 # now the right one
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=3)
        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)

    def test_11_skip_is_available_while_paused(self) -> None:
        modal = FakeModal([{"filename": EXPECTED, "selected": False}])
        thread = self._pause(modal)
        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=3)
        self.assertEqual(self.outcome, ManualOutcome.SKIPPED)

    def test_12_no_resume_step_on_screen_keeps_the_existing_needs_review_exit(self) -> None:
        # Pre-flight / no modal / not on the resume step -> nothing a human can click here,
        # so the adapter falls through to its existing behaviour unchanged.
        self.assertEqual(
            self.auto._resume_step_manual_assist(
                FakeModal([]), FakeJob(), True, {"resume_original_filename": EXPECTED}, []
            ),
            ManualOutcome.UNAVAILABLE,
        )
        self.assertEqual(
            self.auto._resume_step_manual_assist(
                FakeModal([{"filename": EXPECTED, "selected": False}]), FakeJob(), True, {}, []
            ),
            ManualOutcome.UNAVAILABLE,                    # no expected filename known
        )
        self.assertFalse(self.coordinator.is_paused)


if __name__ == "__main__":
    unittest.main()
