"""The six in-form LinkedIn pause paths.

Each test drives a REAL LinkedInAutomation across two threads (automation thread blocks
inside the pause; test thread acts as the API), using the same re-validation predicate the
production code passes at that site. The DOM is faked, the predicates are not.
"""

import threading
import unittest
from dataclasses import dataclass

from app.config import get_settings
from app.models import ApplicationStatus
from app.services.linkedin import LinkedInAutomation
from app.services.manual_assist import (
    ManualAssistCoordinator,
    ManualDecision,
    ManualOutcome,
)


@dataclass
class FakeJob:
    id: int = 7
    title: str = "Data Analyst"
    company: str = "Acme"
    job_url: str = "https://linkedin.com/jobs/view/7"


class _Modal:
    """A stand-in for the live Easy Apply modal. `open` proves it is never torn down."""

    def __init__(self) -> None:
        self.open = True


class _NavButton(dict):
    pass


class LinkedInManualAssistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = ManualAssistCoordinator()
        self.auto = LinkedInAutomation(get_settings())
        self.auto.attach_manual_assist(self.coordinator, batch_id="batch-1")
        self.modal = _Modal()
        self.job = FakeJob()
        self.run_log: list[dict] = []
        self.outcome: ManualOutcome | None = None

    # -- harness --------------------------------------------------------------------------
    def _pause_on(self, validate) -> threading.Thread:
        def run() -> None:
            self.outcome = self.auto._request_manual_input(
                job=self.job,
                dry_run=True,
                question="Q",
                reason="Automation could not complete this field.",
                validate=validate,
                run_log=self.run_log,
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await_pause()
        return thread

    def _await_pause(self, after: int = 0) -> dict:
        for _ in range(500):
            state = self.coordinator.snapshot()
            if state and state["sequence"] > after:
                return state
            threading.Event().wait(0.01)
        self.fail(f"automation never paused (after {after})")

    def _resume_and_expect_completed(self, thread) -> None:
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=3)
        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)
        self.assertTrue(self.modal.open)          # browser/modal never closed

    # -- 1. repeated interaction failure ---------------------------------------------------
    def test_repeated_control_failure_pauses_then_resumes_when_field_resolved(self) -> None:
        unresolved = ["How many years of Kubernetes?"]
        self.auto._form_required_fields = lambda modal, unresolved_only=False: list(unresolved)

        thread = self._pause_on(
            lambda: not self.auto._form_required_fields(self.modal, unresolved_only=True)
        )
        self.assertTrue(thread.is_alive())        # batch parked here
        self.assertTrue(self.modal.open)

        unresolved.clear()                        # user selects the radio by hand
        self._resume_and_expect_completed(thread)

    # -- 2. contact fields -----------------------------------------------------------------
    def test_missing_contact_fields_pause_then_resume_after_manual_fill(self) -> None:
        missing = ["Mobile phone"]
        self.auto._fill_contact_fields = lambda *a, **k: list(missing)

        thread = self._pause_on(
            lambda: not self.auto._fill_contact_fields(self.modal, None, None, None, None, [])
        )
        missing.clear()                           # user types the phone number
        self._resume_and_expect_completed(thread)

    # -- 3. employment fields --------------------------------------------------------------
    def test_missing_employment_fields_pause_then_resume_after_manual_fill(self) -> None:
        missing = ["Employer name"]
        self.auto._fill_employment_history_fields = lambda *a, **k: list(missing)

        thread = self._pause_on(
            lambda: not self.auto._fill_employment_history_fields(
                self.modal, [], None, None, "safe", None, []
            )
        )
        missing.clear()
        self._resume_and_expect_completed(thread)

    # -- 4. inline validation --------------------------------------------------------------
    def test_inline_validation_pauses_then_clears_after_manual_correction(self) -> None:
        errors = ["Enter a valid number"]
        self.auto._validation_messages = lambda modal: list(errors)

        thread = self._pause_on(lambda: not self.auto._validation_messages(self.modal))
        errors.clear()                            # user corrects the value
        self._resume_and_expect_completed(thread)

    # -- 5. unresolved required fields after navigation ------------------------------------
    def test_navigation_unresolved_fields_pause_then_resume(self) -> None:
        unresolved = ["Notice period"]
        self.auto._form_required_fields = lambda modal, unresolved_only=False: list(unresolved)

        thread = self._pause_on(
            lambda: not self.auto._form_required_fields(self.modal, unresolved_only=True)
        )
        unresolved.clear()
        self._resume_and_expect_completed(thread)

    # -- 6. disabled Next/Submit -----------------------------------------------------------
    def test_form_stop_cleared_requires_enabled_button_and_no_unresolved_fields(self) -> None:
        nav = _NavButton({"locator": object(), "enabled": False, "text": "Next"})
        unresolved: list[str] = []
        self.auto._find_navigation_button = lambda modal: nav
        self.auto._form_required_fields = lambda modal, unresolved_only=False: list(unresolved)

        self.assertFalse(self.auto._form_stop_cleared(self.modal))   # button disabled
        nav["enabled"] = True
        self.assertTrue(self.auto._form_stop_cleared(self.modal))    # now enabled, nothing unresolved

        unresolved.append("Notice period")
        self.assertFalse(self.auto._form_stop_cleared(self.modal))   # enabled but field unresolved

    def test_disabled_next_button_pauses_then_resumes_when_enabled(self) -> None:
        nav = _NavButton({"locator": object(), "enabled": False, "text": "Next"})
        self.auto._find_navigation_button = lambda modal: nav
        self.auto._form_required_fields = lambda modal, unresolved_only=False: []

        thread = self._pause_on(lambda: self.auto._form_stop_cleared(self.modal))
        self.assertTrue(thread.is_alive())
        nav["enabled"] = True                     # user completes the form; Next lights up
        self._resume_and_expect_completed(thread)

    # -- 8. failed re-validation re-pauses --------------------------------------------------
    def test_continue_without_fixing_re_pauses_with_new_reason_and_sequence(self) -> None:
        errors = ["Enter a valid number"]
        self.auto._validation_messages = lambda modal: list(errors)

        thread = self._pause_on(lambda: not self.auto._validation_messages(self.modal))
        first = self.coordinator.snapshot()["sequence"]

        self.coordinator.resolve(ManualDecision.CONTINUE)   # user did NOT fix it
        state = self._await_pause(after=first)

        self.assertGreater(state["sequence"], first)
        self.assertEqual(state["attempt"], 2)
        self.assertIn("still looks incomplete", state["reason"])
        self.assertTrue(thread.is_alive())                  # never silently continued
        self.assertTrue(self.modal.open)

        errors.clear()
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=3)
        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)

    # -- 9. skip ----------------------------------------------------------------------------
    def test_skip_returns_skipped_outcome_and_result(self) -> None:
        self.auto._validation_messages = lambda modal: ["still broken"]
        thread = self._pause_on(lambda: not self.auto._validation_messages(self.modal))

        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=3)
        self.assertEqual(self.outcome, ManualOutcome.SKIPPED)

        # The result the adapter returns for a user skip: SKIPPED, never FAILED/NEEDS_REVIEW.
        self.auto._persist_run_log = lambda *a, **k: None
        result = self.auto._skipped_by_user(self.job, True, [], None, [], [])
        self.assertEqual(result.status, ApplicationStatus.SKIPPED)
        self.assertEqual(result.current_step, "skipped_by_user")

    # -- 12. no coordinator -----------------------------------------------------------------
    def test_without_a_coordinator_every_path_keeps_existing_behaviour(self) -> None:
        self.auto.attach_manual_assist(None)
        outcome = self.auto._request_manual_input(
            job=self.job, dry_run=True, question="Q", reason="r",
            validate=lambda: False, run_log=self.run_log,
        )
        self.assertEqual(outcome, ManualOutcome.UNAVAILABLE)
        self.assertFalse(self.coordinator.is_paused)


if __name__ == "__main__":
    unittest.main()


class NonFormExitsMustNotPauseTests(unittest.TestCase):
    """The excluded exits must keep their existing skip/fail behaviour. This asserts it
    structurally: no pause call may appear inside those blocks, so a later edit that wires one
    of them by accident fails here."""

    EXCLUDED_STEPS = [
        "easy_apply_modal_unavailable",   # no usable live form
        "easy_apply_modal_closed",        # form is gone
        "confirmation_not_detected",      # post-submit
    ]

    @classmethod
    def setUpClass(cls) -> None:
        import inspect

        from app.services import linkedin

        cls.source = inspect.getsource(linkedin.LinkedInAutomation.apply_to_job).splitlines()

    def _pause_lines(self) -> list[int]:
        return [
            i for i, line in enumerate(self.source)
            if "_request_manual_input(" in line or "pause_for_manual_input(" in line
        ]

    def test_excluded_steps_have_no_pause_in_their_block(self) -> None:
        pauses = self._pause_lines()
        for step in self.EXCLUDED_STEPS:
            marks = [i for i, line in enumerate(self.source) if f'"{step}"' in line]
            self.assertTrue(marks, f"{step} no longer exists in apply_to_job")
            for mark in marks:
                # No pause may be introduced in the ~30 lines that make up that exit block.
                near = [p for p in pauses if mark <= p <= mark + 30]
                self.assertEqual(near, [], f"{step} must not pause for manual input")

    def test_step_limit_remains_unwired(self) -> None:
        # Still excluded: a runaway-loop guard has no single field to re-validate, so a pause
        # there would have no meaningful predicate.
        pauses = self._pause_lines()
        marks = [i for i, line in enumerate(self.source) if "_step_limit_reason(" in line]
        self.assertTrue(marks, "_step_limit_reason no longer exists")
        for mark in marks:
            self.assertEqual(
                [p for p in pauses if mark <= p <= mark + 20], [],
                "the step-limit guard is intentionally not wired to manual assist",
            )

    def test_resume_step_mismatch_is_now_wired(self) -> None:
        # The resume step IS manually resolvable (select the right existing resume), so both
        # in-modal mismatch exits now go through the shared lifecycle first.
        gates = [
            i for i, line in enumerate(self.source)
            if "_resume_step_manual_assist(" in line
        ]
        self.assertEqual(len(gates), 2)
        for mark in [i for i, line in enumerate(self.source)
                     if 'Resume upload mismatch detected.' in line]:
            # A gate precedes each needs-review exit.
            self.assertTrue([g for g in gates if mark - 20 <= g <= mark])

    def test_exactly_nine_in_form_pause_sites_are_wired(self) -> None:
        # 7 field/validation gates + the 2 resume-step gates.
        self.assertEqual(len(self._pause_lines()) + 2, 9)
