"""Portal-independent pause/resume lifecycle.

These drive the real coordinator across two threads -- an "automation thread" (which owns a
fake page and blocks inside the apply) and the "API thread" (which only signals). That is
exactly the production threading model, so the tests fail if the pause ever stops blocking,
resumes without re-validating, or lets the automation thread touch the page from elsewhere.
"""

import threading
import unittest
from dataclasses import dataclass

from app.services.manual_assist import (
    ManualAssistCoordinator,
    ManualAssistMixin,
    ManualDecision,
    ManualOutcome,
)


@dataclass
class FakeJob:
    id: int = 42
    title: str = "Data Analyst"
    company: str = "Acme"
    job_url: str = "https://linkedin.com/jobs/view/42"


class FakePage:
    """Stands in for the live Playwright page. `closed` proves the browser was never torn
    down while paused; `answered` is the field the user completes by hand."""

    def __init__(self) -> None:
        self.closed = False
        self.answered = False

    def close(self) -> None:
        self.closed = True


class FakePortal(ManualAssistMixin):
    """A portal adapter that reaches a field it cannot fill, exactly like LinkedIn/Dice."""

    portal_name = "linkedin"

    def __init__(self, coordinator, page: FakePage) -> None:
        self.attach_manual_assist(coordinator, batch_id="batch-1")
        self.page = page
        self.events: list[tuple[str, str]] = []
        self.finished = False

    def apply(self, dry_run: bool = True) -> ManualOutcome:
        outcome = self.pause_for_manual_input(
            portal=self.portal_name,
            job=FakeJob(),
            dry_run=dry_run,
            current_step="screening_questions",
            question="Years of experience with Kubernetes?",
            reason="Automation could not select an answer for this question.",
            validate=lambda: self.page.answered,   # re-checked on the automation thread
            on_event=lambda event, detail: self.events.append((event, detail)),
        )
        if outcome is ManualOutcome.COMPLETED:
            self.finished = True          # automation resumed and finished the application
        return outcome


class ManualAssistCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = ManualAssistCoordinator()
        self.page = FakePage()
        self.portal = FakePortal(self.coordinator, self.page)
        self.outcome: ManualOutcome | None = None

    def _start_automation(self, dry_run: bool = True) -> threading.Thread:
        def run() -> None:
            self.outcome = self.portal.apply(dry_run=dry_run)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._await_pause()
        return thread

    def _await_pause(self, after: int = 0) -> dict:
        """Block until a pause NEWER than `after` is published (see ManualPauseState.sequence).
        Without the generation check this would race the pause we just released."""
        for _ in range(500):
            state = self.coordinator.snapshot()
            if state and state["sequence"] > after:
                return state
            threading.Event().wait(0.01)
        self.fail(f"automation never paused (after sequence {after})")

    # --- pausing -----------------------------------------------------------------------
    def test_automation_pauses_and_stays_paused_with_the_page_alive(self) -> None:
        thread = self._start_automation()

        # The automation thread is parked inside the apply: still alive, page still open.
        self.assertTrue(thread.is_alive())
        self.assertTrue(self.coordinator.is_paused)
        self.assertFalse(self.page.closed)
        self.assertFalse(self.portal.finished)   # it did NOT move on

        state = self.coordinator.snapshot()
        self.assertEqual(state["portal"], "linkedin")
        self.assertEqual(state["job_id"], 42)
        self.assertEqual(state["company"], "Acme")
        self.assertEqual(state["question"], "Years of experience with Kubernetes?")
        self.assertEqual(state["current_step"], "screening_questions")
        self.assertEqual(state["batch_id"], "batch-1")
        self.assertTrue(state["dry_run"])
        self.assertIn("could not select an answer", state["reason"])

        self.coordinator.resolve(ManualDecision.SKIP)   # release the thread
        thread.join(timeout=2)

    def test_pause_payload_reports_live_apply(self) -> None:
        thread = self._start_automation(dry_run=False)
        self.assertFalse(self.coordinator.snapshot()["dry_run"])
        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=2)

    # --- resuming ----------------------------------------------------------------------
    def test_continue_after_user_completes_the_field_resumes_automation(self) -> None:
        thread = self._start_automation()

        self.page.answered = True                       # user answers it in the browser
        self.assertTrue(self.coordinator.resolve(ManualDecision.CONTINUE))
        thread.join(timeout=2)

        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)
        self.assertTrue(self.portal.finished)           # resumed from the exact same point
        self.assertFalse(self.page.closed)              # never reopened / never torn down
        self.assertFalse(self.coordinator.is_paused)
        self.assertIn(("manual_input_completed", "Years of experience with Kubernetes?"),
                      self.portal.events)

    def test_continue_without_completing_the_field_re_pauses_and_never_continues(self) -> None:
        thread = self._start_automation()
        first = self.coordinator.snapshot()["sequence"]

        # User presses Continue but did NOT actually answer the question.
        self.coordinator.resolve(ManualDecision.CONTINUE)
        state = self._await_pause(after=first)           # pauses AGAIN rather than proceeding

        self.assertTrue(thread.is_alive())
        self.assertFalse(self.portal.finished)           # automation did NOT silently continue
        self.assertFalse(self.page.closed)
        self.assertEqual(state["attempt"], 2)
        self.assertIn("still looks incomplete", state["reason"])

        # Now they really answer it.
        self.page.answered = True
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=2)
        self.assertEqual(self.outcome, ManualOutcome.COMPLETED)
        self.assertTrue(self.portal.finished)

    def test_validation_error_re_pauses_instead_of_crashing(self) -> None:
        def exploding_validate() -> bool:
            raise RuntimeError("page detached")

        portal = FakePortal(self.coordinator, self.page)
        outcomes: list[ManualOutcome] = []

        def run() -> None:
            outcomes.append(
                portal.pause_for_manual_input(
                    portal="dice", job=FakeJob(), dry_run=True,
                    current_step="screening", question="Q", reason="cannot answer",
                    validate=exploding_validate,
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        first = self._await_pause()["sequence"]
        self.coordinator.resolve(ManualDecision.CONTINUE)
        state = self._await_pause(after=first)           # re-paused, did not crash the batch

        self.assertIn("Could not verify the answer", state["reason"])
        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=2)
        self.assertEqual(outcomes, [ManualOutcome.SKIPPED])

    # --- skipping ----------------------------------------------------------------------
    def test_skip_this_job_releases_automation_with_skipped(self) -> None:
        thread = self._start_automation()
        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=2)

        self.assertEqual(self.outcome, ManualOutcome.SKIPPED)
        self.assertFalse(self.portal.finished)
        self.assertFalse(self.coordinator.is_paused)
        self.assertIn(("manual_input_skipped", "Years of experience with Kubernetes?"),
                      self.portal.events)

    # --- guards ------------------------------------------------------------------------
    def test_resolve_is_a_noop_when_nothing_is_paused(self) -> None:
        self.assertFalse(self.coordinator.resolve(ManualDecision.CONTINUE))
        self.assertIsNone(self.coordinator.snapshot())

    def test_clear_releases_a_waiting_automation_as_skip(self) -> None:
        thread = self._start_automation()
        self.coordinator.clear()
        thread.join(timeout=2)
        self.assertEqual(self.outcome, ManualOutcome.SKIPPED)
        self.assertFalse(self.coordinator.is_paused)

    def test_no_coordinator_preserves_existing_behaviour(self) -> None:
        # This is what keeps every existing flow (apply-selected, current tests) unchanged:
        # with no coordinator attached the portal falls straight through to its old path.
        portal = FakePortal(self.coordinator, self.page)
        portal.attach_manual_assist(None)
        self.assertEqual(portal.apply(), ManualOutcome.UNAVAILABLE)
        self.assertFalse(portal.finished)
        self.assertFalse(self.coordinator.is_paused)


if __name__ == "__main__":
    unittest.main()


class DiceFieldRevalidationTests(unittest.TestCase):
    """_fields_completed must read the LIVE DOM (the `completed` flag produced by
    _collect_fields_script), not re-run the decision pipeline -- which would still report the
    field unanswered after the user typed into it, trapping the batch in a re-pause loop."""

    def setUp(self) -> None:
        from app.config import get_settings
        from app.services.portals.dice_portal import DicePortalAdapter

        self.adapter = DicePortalAdapter(get_settings())

    class _Page:
        def __init__(self, fields):
            self.fields = fields
            self.evaluated = 0

        def evaluate(self, _script):
            self.evaluated += 1
            return self.fields

    def test_blocked_field_still_empty_is_not_completed(self) -> None:
        page = self._Page([{"label": "Years of Python?", "required": True, "completed": False}])
        self.assertFalse(self.adapter._fields_completed(page, ["Years of Python?"]))

    def test_blocked_field_answered_in_the_browser_is_completed(self) -> None:
        page = self._Page([{"label": "Years of Python?", "required": True, "completed": True}])
        self.assertTrue(self.adapter._fields_completed(page, ["Years of Python?"]))

    def test_all_blocked_fields_must_be_completed(self) -> None:
        page = self._Page([
            {"label": "A", "completed": True},
            {"label": "B", "completed": False},
        ])
        self.assertFalse(self.adapter._fields_completed(page, ["A", "B"]))
        page.fields[1]["completed"] = True
        self.assertTrue(self.adapter._fields_completed(page, ["A", "B"]))

    def test_field_that_vanished_from_the_form_counts_as_resolved(self) -> None:
        # User advanced the wizard past it -- there is nothing left to block on.
        page = self._Page([{"label": "Other", "completed": True}])
        self.assertTrue(self.adapter._fields_completed(page, ["Gone"]))

    def test_it_re_reads_the_dom_on_every_call(self) -> None:
        page = self._Page([{"label": "A", "completed": False}])
        self.assertFalse(self.adapter._fields_completed(page, ["A"]))
        page.fields[0]["completed"] = True                 # the user answers it
        self.assertTrue(self.adapter._fields_completed(page, ["A"]))
        self.assertEqual(page.evaluated, 2)                # fresh read each time

    def test_dead_page_is_not_completed(self) -> None:
        class Dead:
            def evaluate(self, _script):
                raise RuntimeError("page closed")

        self.assertFalse(self.adapter._fields_completed(Dead(), ["A"]))

    def test_no_labels_is_never_completed(self) -> None:
        self.assertFalse(self.adapter._fields_completed(self._Page([]), []))
