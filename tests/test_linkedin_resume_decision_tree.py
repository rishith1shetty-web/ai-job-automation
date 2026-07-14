"""LinkedIn Easy Apply resume step: the CASE 1 / CASE 2 / CASE 3 decision tree.

LinkedIn only remembers resumes it has already seen. So when the user analyses Resume B on an
account that has only ever uploaded Resume A, the step legitimately lists Resume A alone. That
is NOT a mismatch -- it means Resume B is new here and must be uploaded once.

    CASE 1  active resume already saved      -> select it, verify, NEVER upload a duplicate
    CASE 2  active resume not saved yet      -> upload it, wait, re-read, select, verify
    CASE 3  upload/verification really fails -> manual assist (browser + modal stay open)

The fake modal dispatches on the adapter's REAL JS constants, so production selection and
verification logic is exercised -- only the browser is faked.
"""

import inspect
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.services import linkedin as linkedin_module
from app.services.linkedin import LinkedInAutomation
from app.services.manual_assist import ManualAssistCoordinator, ManualDecision, ManualOutcome

ACTIVE = "Resume_B.pdf"          # the newly analysed resume
SAVED_A = "Resume_A.pdf"         # what LinkedIn already remembers from an earlier run


@dataclass
class FakeJob:
    id: int = 7
    title: str = "Data Analyst"
    company: str = "Acme"
    job_url: str = "https://linkedin.com/jobs/view/7"


def _base(name) -> str:
    return Path(str(name or "")).name.strip().lower()


class FakePage:
    def __init__(self) -> None:
        self.waits = 0

    def wait_for_timeout(self, _ms) -> None:
        self.waits += 1


class FakeInputs:
    """LinkedIn's file input. `fail=True` models LinkedIn rejecting the file."""

    def __init__(self, modal, present: int = 1, fail: bool = False) -> None:
        self.modal = modal
        self.present = present
        self.fail = fail
        self.uploaded: list = []

    def count(self) -> int:
        return self.present

    def nth(self, _i):
        return self

    def set_input_files(self, payload) -> None:
        if self.fail:
            raise RuntimeError("LinkedIn rejected the file")
        self.uploaded.append(payload)
        self.modal.on_upload()


class FakeModal:
    """A resume step. Models LinkedIn's real post-upload behaviour: a processing delay, then a
    re-render in which the new resume card appears (and is usually, but not always, auto-selected).
    """

    def __init__(
        self,
        cards,
        *,
        upload_filename: str | None = ACTIVE,
        upload_appears: bool = True,
        upload_auto_selects: bool = True,
        processing_reads: int = 1,
        busy_ticks: int = 0,
        inputs_present: int = 1,
        upload_fails: bool = False,
    ) -> None:
        self.cards = [dict(c) for c in cards]
        self.upload_filename = upload_filename
        self.upload_appears = upload_appears
        self.upload_auto_selects = upload_auto_selects
        self.processing_reads = processing_reads
        self.busy_ticks = busy_ticks
        self.clicked: list[str] = []
        self.card_reads = 0
        self.page = FakePage()
        self.inputs = FakeInputs(self, present=inputs_present, fail=upload_fails)
        self._uploaded = False
        self._reads_left = 0

    # -- the browser -------------------------------------------------------------------
    def locator(self, selector):
        assert selector == "input[type='file']", selector
        return self.inputs

    def on_upload(self) -> None:
        self._uploaded = True
        self._reads_left = self.processing_reads
        self.busy_ticks = max(self.busy_ticks, 1)     # LinkedIn shows a spinner while processing

    def _maybe_land_upload(self) -> None:
        if not self._uploaded or not self.upload_appears:
            return
        if self._reads_left > 0:
            self._reads_left -= 1
            return
        if any(_base(c["filename"]) == _base(self.upload_filename) for c in self.cards):
            return
        if self.upload_auto_selects:
            for card in self.cards:
                card["selected"] = False
        self.cards.append(
            {"filename": self.upload_filename, "selected": self.upload_auto_selects}
        )

    def evaluate(self, script, arg=None):
        if script == LinkedInAutomation._RESUME_BUSY_JS:
            if self.busy_ticks < 0:
                return True                            # spinner never clears
            if self.busy_ticks > 0:
                self.busy_ticks -= 1
                return True
            return False
        if script == LinkedInAutomation._RESUME_CARDS_JS:
            self.card_reads += 1
            self._maybe_land_upload()
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


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.auto = LinkedInAutomation(get_settings())
        self.validation: list[str] = []
        self.nav = {"locator": object(), "enabled": True, "text": "Next"}
        self.auto._validation_messages = lambda modal: list(self.validation)
        self.auto._find_navigation_button = lambda modal: dict(self.nav)
        # Never touch disk in these tests.
        self.auto._save_resume_upload_diagnostics = lambda *_a, **_k: None
        self.auto._resume_upload_payload = lambda path, diagnostics=None: str(path)
        # Same bounded-poll logic, just a short budget so the "upload never lands" case does not
        # spend LinkedIn's full real-world processing allowance in the test suite.
        self.auto.resume_upload_timeout_ms = 300

    def _run(self, modal, diagnostics=None):
        diagnostics = diagnostics if diagnostics is not None else {
            "resume_original_filename": ACTIVE
        }
        uploaded = self.auto._upload_resume_if_needed(
            modal, Path("/storage/resume_b.pdf"), diagnostics=diagnostics
        )
        return uploaded, diagnostics


# ======================================================================================
# CASE 1 -- the active resume is already saved on LinkedIn
# ======================================================================================
class Case1ReuseTests(_Base):
    def test_1_existing_matching_resume_is_reused(self) -> None:
        modal = FakeModal([{"filename": ACTIVE, "selected": False}])
        uploaded, diagnostics = self._run(modal)

        self.assertFalse(uploaded)
        self.assertEqual(modal.inputs.uploaded, [])          # nothing uploaded
        self.assertEqual(modal.clicked, [ACTIVE])            # it was selected
        self.assertTrue(diagnostics["existing_resume_selected"])
        self.assertTrue(diagnostics["upload_success"])
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_6_no_duplicate_upload_when_the_resume_already_exists(self) -> None:
        modal = FakeModal([
            {"filename": SAVED_A, "selected": True},
            {"filename": ACTIVE, "selected": False},
        ])
        uploaded, _ = self._run(modal)

        self.assertFalse(uploaded)
        self.assertEqual(modal.inputs.uploaded, [])          # THE duplicate-upload guard
        self.assertEqual(len([c for c in modal.cards if _base(c["filename"]) == _base(ACTIVE)]), 1)

    def test_5_with_two_saved_resumes_the_correct_one_is_selected(self) -> None:
        modal = FakeModal([
            {"filename": SAVED_A, "selected": True},         # LinkedIn's stale default
            {"filename": ACTIVE, "selected": False},
        ])
        _, diagnostics = self._run(modal)

        self.assertEqual(modal.clicked, [ACTIVE])            # the old resume is never clicked
        selected = [c["filename"] for c in modal.cards if c["selected"]]
        self.assertEqual(selected, [ACTIVE])
        self.assertEqual(diagnostics["linkedin_filename_detected_after_upload"], ACTIVE)

    def test_14_already_selected_correct_resume_is_left_alone(self) -> None:
        modal = FakeModal([
            {"filename": SAVED_A, "selected": False},
            {"filename": ACTIVE, "selected": True},
        ])
        uploaded, diagnostics = self._run(modal)

        self.assertFalse(uploaded)
        self.assertEqual(modal.clicked, [])                  # no needless re-click
        self.assertEqual(modal.inputs.uploaded, [])
        self.assertTrue(diagnostics["upload_success"])


# ======================================================================================
# CASE 2 -- the active resume has never been uploaded to this LinkedIn account
# ======================================================================================
class Case2AutomaticUploadTests(_Base):
    def test_2_missing_resume_is_uploaded_automatically(self) -> None:
        # THE REPORTED BUG: LinkedIn remembers only Resume A. That is not a mismatch.
        modal = FakeModal([{"filename": SAVED_A, "selected": True}])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertEqual(len(modal.inputs.uploaded), 1)      # uploaded exactly once
        self.assertFalse(diagnostics["mismatch_detected"])   # NOT a mismatch, NOT needs_review
        self.assertTrue(diagnostics["upload_started"])

    def test_3_successful_upload_is_verified_and_continues(self) -> None:
        modal = FakeModal([{"filename": SAVED_A, "selected": True}])
        _, diagnostics = self._run(modal)

        self.assertTrue(diagnostics["upload_success"])
        self.assertTrue(diagnostics["upload_completed"])
        self.assertIsNone(diagnostics["reason_if_failed"])
        self.assertEqual(diagnostics["linkedin_filename_detected_after_upload"], ACTIVE)
        selected = [c["filename"] for c in modal.cards if c["selected"]]
        self.assertEqual(selected, [ACTIVE])                 # the NEW resume is the selected one

    def test_7_the_wrong_saved_resume_is_never_selected_instead(self) -> None:
        modal = FakeModal([{"filename": SAVED_A, "selected": True}])
        self._run(modal)
        self.assertNotIn(SAVED_A, modal.clicked)             # never falls back to Resume A

    def test_8_the_spinner_is_awaited_after_the_upload(self) -> None:
        modal = FakeModal([{"filename": SAVED_A, "selected": True}], busy_ticks=3)
        _, diagnostics = self._run(modal)

        self.assertGreater(modal.page.waits, 0)              # polled, never a blind sleep
        self.assertTrue(diagnostics["upload_success"])

    def test_9_the_dom_is_re_read_after_the_post_upload_rerender(self) -> None:
        # The new card only appears on a LATER read -- exactly like LinkedIn's async re-render.
        modal = FakeModal([{"filename": SAVED_A, "selected": True}], processing_reads=3)
        _, diagnostics = self._run(modal)

        self.assertGreater(modal.card_reads, 3)              # it kept re-reading until it landed
        self.assertTrue(diagnostics["upload_success"])

    def test_upload_that_linkedin_does_not_auto_select_is_selected_by_us(self) -> None:
        modal = FakeModal(
            [{"filename": SAVED_A, "selected": True}], upload_auto_selects=False
        )
        _, diagnostics = self._run(modal)

        self.assertEqual(modal.clicked, [ACTIVE])            # we selected the freshly uploaded one
        self.assertTrue(diagnostics["upload_success"])


# ======================================================================================
# CASE 3 -- only now may manual assist fire
# ======================================================================================
class Case3ManualAssistTests(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.coordinator = ManualAssistCoordinator()
        self.auto.attach_manual_assist(self.coordinator, batch_id="batch-1")

    def test_4_a_failed_upload_flags_the_step_for_manual_assist(self) -> None:
        modal = FakeModal([{"filename": SAVED_A, "selected": True}], upload_fails=True)
        uploaded, diagnostics = self._run(modal)

        self.assertFalse(uploaded)
        self.assertTrue(diagnostics["mismatch_detected"])    # -> caller pauses for manual assist
        self.assertFalse(diagnostics["upload_success"])
        self.assertIn("failed", diagnostics["reason_if_failed"].lower())

    def test_upload_that_never_lands_flags_the_step_for_manual_assist(self) -> None:
        # File accepted, but LinkedIn never renders a card for it (processing died).
        modal = FakeModal([{"filename": SAVED_A, "selected": True}], upload_appears=False)
        _, diagnostics = self._run(modal)

        self.assertTrue(diagnostics["mismatch_detected"])
        self.assertIn("did not confirm", diagnostics["reason_if_failed"])

    def test_10_another_saved_resume_alone_NEVER_triggers_manual_assist(self) -> None:
        """The whole point of the fix: Resume A being present is not a reason to interrupt."""
        modal = FakeModal([{"filename": SAVED_A, "selected": True}])
        _, diagnostics = self._run(modal)

        self.assertFalse(diagnostics["mismatch_detected"])
        # Nothing is waiting on a human.
        self.assertIsNone(self.coordinator.snapshot())
        self.assertFalse(self.coordinator.is_paused)

    def test_11_browser_stays_open_and_pauses_for_a_manually_resolvable_problem(self) -> None:
        # The resume is present but unverifiable (Next disabled) -> pause, do NOT close.
        modal = FakeModal([{"filename": ACTIVE, "selected": True}])
        self.nav = {"locator": object(), "enabled": False, "text": "Next"}
        _, diagnostics = self._run(modal)
        self.assertTrue(diagnostics["mismatch_detected"])
        self.assertEqual(modal.inputs.uploaded, [])          # still no duplicate

        outcome: list = []

        def run() -> None:
            outcome.append(
                self.auto._resume_step_manual_assist(modal, FakeJob(), True, diagnostics, [])
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        for _ in range(500):                                  # wait for the pause
            if self.coordinator.snapshot():
                break
            threading.Event().wait(0.01)

        state = self.coordinator.snapshot()
        self.assertIsNotNone(state, "resume step did not pause")
        self.assertTrue(thread.is_alive())                    # automation is PARKED, browser alive
        self.assertIn(ACTIVE, state["question"])

        self.nav = {"locator": object(), "enabled": True, "text": "Next"}   # user fixes it
        self.coordinator.resolve(ManualDecision.CONTINUE)
        thread.join(timeout=5)
        self.assertEqual(outcome, [ManualOutcome.COMPLETED])

    def test_skip_while_paused_is_reported_as_skipped(self) -> None:
        modal = FakeModal([{"filename": ACTIVE, "selected": True}])
        self.nav = {"locator": object(), "enabled": False, "text": "Next"}
        outcome: list = []

        def run() -> None:
            outcome.append(
                self.auto._resume_step_manual_assist(
                    modal, FakeJob(), True, {"resume_original_filename": ACTIVE}, []
                )
            )

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        for _ in range(500):
            if self.coordinator.snapshot():
                break
            threading.Event().wait(0.01)

        self.coordinator.resolve(ManualDecision.SKIP)
        thread.join(timeout=5)
        self.assertEqual(outcome, [ManualOutcome.SKIPPED])


# ======================================================================================
# Invariants that must survive this change
# ======================================================================================
class InvariantTests(_Base):
    def test_12_dry_run_still_never_clicks_submit(self) -> None:
        source = inspect.getsource(linkedin_module.LinkedInAutomation.apply_to_job)
        # The dry-run gate still returns at the submit/review stage, before any submit click.
        self.assertIn('"dry_run_stop" if dry_run else "submit_ready"', source)
        gate = source.index("if dry_run:\n", source.index("submit_available"))
        submit_click = source.find("submit.first.click()")
        self.assertTrue(submit_click == -1 or submit_click > gate)

    def test_12b_the_resume_step_never_interacts_with_submit(self) -> None:
        modal = FakeModal([{"filename": SAVED_A, "selected": True}])
        self._run(modal)
        # The only thing the resume step ever clicks is a resume card.
        self.assertTrue(all(_base(name) == _base(ACTIVE) for name in modal.clicked))

    def test_13_live_apply_takes_the_identical_resume_path(self) -> None:
        # The decision tree does not branch on dry_run at all -- it cannot behave differently
        # under Live Apply. Proven by the signature, and by an identical run.
        params = inspect.signature(LinkedInAutomation._upload_resume_if_needed).parameters
        self.assertNotIn("dry_run", params)

        dry = FakeModal([{"filename": SAVED_A, "selected": True}])
        live = FakeModal([{"filename": SAVED_A, "selected": True}])
        dry_uploaded, dry_diag = self._run(dry)
        live_uploaded, live_diag = self._run(live)

        self.assertEqual(dry_uploaded, live_uploaded)
        self.assertEqual(len(dry.inputs.uploaded), len(live.inputs.uploaded))
        self.assertEqual(dry_diag["upload_success"], live_diag["upload_success"])
        self.assertEqual(dry_diag["mismatch_detected"], live_diag["mismatch_detected"])


if __name__ == "__main__":
    unittest.main()
