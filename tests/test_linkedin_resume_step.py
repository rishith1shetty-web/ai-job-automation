"""LinkedIn Easy Apply resume step: ALWAYS upload the active analyzed resume.

Business rule: every application uploads the active analyzed resume and selects the card that
upload creates. Resumes already saved on the LinkedIn account are ignored entirely -- whether they
differ from the active resume, match it, are already selected, or there are several.

THE BUG THIS PINS (found in a real dry run, outputs/resume_upload_debug/20260714T163928025222):
the resume WAS uploaded and WAS selected, but LinkedIn's card control is a TOGGLE whose label
flips from "Select resume X.pdf" to "DEselect resume X.pdf" once chosen. The filename matcher did
not strip "de", so the selected card's name never matched, verification failed,
mismatch_detected was set, and the caller returned NEEDS_REVIEW -- closing the browser on a
perfectly good upload and moving to the next job.

Because a duplicate upload produces two cards with the SAME filename, the new card is identified
by a pre/post DOM diff (existing controls are marked before the upload), never by name.

No real browser and no submission: the modal is a fake that dispatches on the adapter's REAL JS.
"""

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.services.linkedin import LinkedInAutomation

ACTIVE = "Rishith_Shetty_Resume (1).pdf"
OTHER = "Jhanvi Patel New Data Analyst (2).pdf"


class FakeFileInput:
    def __init__(self, modal) -> None:
        self._modal = modal

    def count(self) -> int:
        return 1 if self._modal.has_file_input else 0

    def nth(self, _index):
        return self

    def set_input_files(self, payload) -> None:
        self._modal.uploads.append(payload)
        self._modal.on_upload()


class FakeTextLocator:
    def __init__(self, texts) -> None:
        self._texts = list(texts)
        self.first = self

    def count(self) -> int:
        return len(self._texts)

    def all_inner_texts(self):
        return list(self._texts)


class FakeNavButton:
    def __init__(self, modal) -> None:
        self._modal = modal
        self.first = self

    def count(self) -> int:
        return 1

    def nth(self, _index):
        return self

    def is_enabled(self) -> bool:
        return self._modal.next_enabled

    def is_visible(self) -> bool:
        return True

    def inner_text(self, **_kw) -> str:
        return "Next"

    def all_inner_texts(self):
        return ["Next"]


class FakeResumeModal:
    """A LinkedIn resume step.

    A card = {"filename", "selected", "marked"}. `marked` mirrors the real data-rp-preexisting
    attribute the adapter stamps on pre-existing controls.
    """

    def __init__(
        self,
        cards=(),
        has_file_input=True,
        busy_ticks=0,
        validation=(),
        next_enabled=True,
        select_on_upload=True,
        appears_after_upload=True,
        highlight_only=False,
    ) -> None:
        self.cards = [{"filename": c["filename"], "selected": c.get("selected", False),
                       "marked": False} for c in cards]
        self.has_file_input = has_file_input
        self.busy_ticks = busy_ticks
        self.validation = list(validation)
        self.next_enabled = next_enabled
        self.select_on_upload = select_on_upload
        self.appears_after_upload = appears_after_upload
        self.highlight_only = highlight_only     # card looks chosen but is not really checked
        self.uploads: list = []
        self.clicked_new = 0
        self.busy_reads = 0

    def on_upload(self) -> None:
        """What LinkedIn does with the file."""
        if not self.appears_after_upload:
            return                                # LinkedIn never rendered it
        selected = self.select_on_upload and not self.highlight_only
        if self.select_on_upload:                 # LinkedIn deselects the previous card
            for card in self.cards:
                card["selected"] = False
        self.cards.append({"filename": Path(self.uploads[-1]["name"]).name,
                           "selected": selected, "marked": False})

    # -- Playwright surface -----------------------------------------------------------------
    def evaluate(self, script: str, _arg=None):
        automation = LinkedInAutomation(Settings(openai_api_key=None))
        if script == automation._RESUME_BUSY_JS:
            self.busy_reads += 1
            if self.busy_ticks < 0:
                return True                       # a spinner that never clears
            if self.busy_ticks > 0:
                self.busy_ticks -= 1
                return True
            return False
        if script == automation._MARK_RESUME_CARDS_JS:
            for card in self.cards:
                card["marked"] = True
            return len(self.cards)
        if script == automation._RESUME_CARDS_JS:
            if self.busy_ticks != 0:
                return []                         # mid-render: not readable yet
            return [
                # LinkedIn's control label is a TOGGLE -- this is the real DOM shape.
                {"filename": f"{'Deselect' if c['selected'] else 'Select'} resume {c['filename']}",
                 "selected": c["selected"],
                 "is_new": not c["marked"]}
                for c in self.cards
            ]
        if script == automation._SELECT_NEW_RESUME_CARD_JS:
            for card in self.cards:
                if not card["marked"]:
                    self.clicked_new += 1
                    for other in self.cards:
                        other["selected"] = False
                    card["selected"] = not self.highlight_only
                    return True
            return False
        raise AssertionError(f"unexpected script: {script[:40]}")

    def locator(self, selector: str):
        if selector == "input[type='file']":
            return FakeFileInput(self)
        if "error" in selector or "alert" in selector:
            return FakeTextLocator(self.validation)
        return FakeNavButton(self)

    def get_by_role(self, _role, name=None):
        return FakeNavButton(self)

    def inner_text(self, **_kw) -> str:
        return "\n".join([c["filename"] for c in self.cards] + self.validation)

    def all_inner_texts(self):
        return list(self.validation)


class LinkedInResumeStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.automation = LinkedInAutomation(Settings(openai_api_key=None))
        # Real bounded waits, just short ones: the failure cases must not sit through 15s.
        self.automation.RESUME_READY_TIMEOUT_MS = 300
        self.automation.RESUME_CARD_TIMEOUT_MS = 300
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.resume_path = Path(self._tmp.name) / "stored_copy.pdf"
        self.resume_path.write_bytes(b"%PDF-1.4 fake")

    def _diagnostics(self) -> dict:
        return {
            "resume_original_filename": ACTIVE,
            "upload_started": False, "upload_completed": False,
            "upload_success": False, "mismatch_detected": False, "reason_if_failed": None,
        }

    def _run(self, modal) -> tuple[bool, dict]:
        diagnostics = self._diagnostics()
        uploaded = self.automation._upload_resume_if_needed(modal, self.resume_path, diagnostics, None)
        return uploaded, diagnostics

    def _selected(self, modal) -> list[str]:
        return [c["filename"] for c in modal.cards if c["selected"]]

    # -- THE REGRESSION -----------------------------------------------------------------------
    def test_deselect_label_on_the_selected_card_is_not_a_mismatch(self) -> None:
        """The exact real-world failure: LinkedIn labels the CHOSEN card 'Deselect resume X.pdf'."""
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertTrue(diagnostics["upload_success"])
        self.assertFalse(diagnostics["mismatch_detected"])       # <- was True; closed the browser
        self.assertIsNone(diagnostics["reason_if_failed"])
        self.assertTrue(self.automation._resume_names_match(ACTIVE, f"Deselect resume {ACTIVE}"))

    def test_rerender_wiping_the_dom_markers_does_not_fail_verification(self) -> None:
        """THE REAL-RUN FAILURE (cards before=1, after=2, selected='Deselect resume <active>',
        green radio, Next enabled -- yet verification returned False).

        LinkedIn re-renders the resume list after an upload and REPLACES the DOM nodes, which
        wipes the data-rp-preexisting markers off the surviving cards. Every card then reports
        is_new=True, so the old `exactly_one_new_card` gate went False and rejected a resume
        LinkedIn had already accepted. The marker still TARGETS the click; it no longer JUDGES.
        """
        class RerenderModal(FakeResumeModal):
            def on_upload(self) -> None:
                for card in self.cards:
                    card["selected"] = False
                    card["marked"] = False       # React replaced the nodes: markers are gone
                self.cards.append({"filename": ACTIVE, "selected": True, "marked": False})

        modal = RerenderModal(cards=[{"filename": OTHER, "selected": True}])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        verification = diagnostics["resume_verification"]
        self.assertEqual(verification["card_count"], 2)
        self.assertFalse(verification["exactly_one_new_card"])    # markers gone: BOTH look new
        self.assertTrue(verification["selected_filename_matches_active_resume"])
        self.assertTrue(verification["next_enabled"])
        self.assertTrue(verification["no_validation_error"])
        self.assertFalse(diagnostics["mismatch_detected"])        # <- was True; closed the browser
        self.assertTrue(diagnostics["upload_success"])

    # -- 1. empty resume step ------------------------------------------------------------------
    def test_1_empty_step_uploads_the_active_resume_and_verifies(self) -> None:
        modal = FakeResumeModal(cards=[])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertEqual(len(modal.uploads), 1)
        self.assertEqual(modal.uploads[0]["name"], ACTIVE)
        self.assertEqual(self._selected(modal), [ACTIVE])
        self.assertFalse(diagnostics["mismatch_detected"])
        self.assertEqual(diagnostics["resume_cards_before_upload"], 0)

    # -- 2. a different resume exists ----------------------------------------------------------
    def test_2_different_saved_resume_is_ignored_and_active_is_uploaded(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertEqual(self._selected(modal), [ACTIVE])        # the OTHER got deselected
        self.assertFalse(diagnostics["mismatch_detected"])

    # -- 3 + 6. the SAME resume already exists -> upload again anyway --------------------------
    def test_3_same_filename_already_saved_is_still_uploaded_again(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": ACTIVE, "selected": True}])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)                                 # NOT skipped
        self.assertEqual(len(modal.uploads), 1)
        self.assertEqual(len(modal.cards), 2)                     # two cards, identical names
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_6_duplicate_filenames_select_the_NEW_card_not_the_old_one(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": ACTIVE, "selected": True}])
        self._run(modal)

        old_card, new_card = modal.cards[0], modal.cards[1]
        self.assertEqual(old_card["filename"], new_card["filename"])   # indistinguishable by name
        self.assertFalse(old_card["selected"])                          # the OLD one is not chosen
        self.assertTrue(new_card["selected"])                           # the NEW one is
        self.assertTrue(new_card["marked"] is False)

    # -- 4. several saved resumes --------------------------------------------------------------
    def test_4_multiple_saved_resumes_are_all_ignored(self) -> None:
        modal = FakeResumeModal(cards=[
            {"filename": OTHER, "selected": True},
            {"filename": ACTIVE, "selected": False},
            {"filename": "Old_Resume.docx", "selected": False},
        ])
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertEqual(len(modal.cards), 4)
        self.assertEqual(self._selected(modal), [ACTIVE])
        self.assertTrue(modal.cards[3]["selected"])              # the newly uploaded one
        self.assertEqual(diagnostics["resume_cards_before_upload"], 3)
        self.assertEqual(diagnostics["resume_cards_after_upload"], 4)
        self.assertFalse(diagnostics["mismatch_detected"])

    # -- 5. pre/post DOM comparison -------------------------------------------------------------
    def test_5_new_card_is_identified_by_the_pre_post_dom_diff(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": ACTIVE, "selected": False}])
        self._run(modal)

        self.assertTrue(modal.cards[0]["marked"])                # existing card was stamped
        self.assertFalse(modal.cards[1]["marked"])               # the new one was not
        self.assertEqual(len(self.automation._new_resume_cards(modal)), 1)

    # -- 7 + 8. waiting --------------------------------------------------------------------------
    def test_7_spinner_is_awaited(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}], busy_ticks=3)
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertGreater(modal.busy_reads, 1)                  # it polled, it did not sleep
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_8_dom_rerender_after_upload_is_handled(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}], busy_ticks=2)
        _, diagnostics = self._run(modal)
        # The card list was unreadable mid-render; the adapter re-read it afterwards.
        self.assertEqual(diagnostics["resume_cards_after_upload"], 2)
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_next_is_not_trusted_while_linkedin_is_still_busy(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": ACTIVE, "selected": True}], busy_ticks=-1)
        self.assertFalse(self.automation._wait_for_resume_ready(modal, timeout_ms=200))
        self.assertFalse(self.automation._resume_selection_verified(modal, ACTIVE))

    # -- 9 + 10. selection is REAL state, not styling ---------------------------------------------
    def test_9_new_card_must_be_truly_checked(self) -> None:
        modal = FakeResumeModal(cards=[])
        self._run(modal)
        self.assertTrue(modal.cards[0]["selected"])
        self.assertTrue(self.automation._resume_selection_verified(modal, ACTIVE))

    def test_10_visual_highlight_without_checked_state_is_rejected(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}], highlight_only=True)
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertTrue(modal.clicked_new)                       # it tried to select it
        self.assertFalse(diagnostics["upload_success"])          # but never claimed success
        self.assertTrue(diagnostics["mismatch_detected"])

    # -- 11 + 12 + 13. Next gating -----------------------------------------------------------------
    def test_11_verification_passes_only_when_everything_is_clean(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}])
        self._run(modal)
        self.assertTrue(self.automation._resume_selection_verified(modal, ACTIVE))

    def test_12_disabled_next_does_not_count_as_success(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}], next_enabled=False)
        _, diagnostics = self._run(modal)
        self.assertTrue(diagnostics["mismatch_detected"])
        self.assertFalse(self.automation._resume_selection_verified(modal, ACTIVE))

    def test_13_resume_validation_error_does_not_count_as_success(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}],
                                validation=["Please attach a resume"])
        _, diagnostics = self._run(modal)
        self.assertTrue(diagnostics["mismatch_detected"])

    # -- 14. genuine failure ------------------------------------------------------------------------
    def test_14_upload_linkedin_never_renders_is_flagged_for_review(self) -> None:
        modal = FakeResumeModal(cards=[{"filename": OTHER, "selected": True}],
                                appears_after_upload=False)
        uploaded, diagnostics = self._run(modal)

        self.assertTrue(uploaded)
        self.assertFalse(diagnostics["upload_success"])
        self.assertTrue(diagnostics["mismatch_detected"])        # -> caller returns NEEDS_REVIEW
        # The reason must NAME the failing condition, never say "could not confirm".
        self.assertIn("selected_filename_matches_active_resume=False",
                      diagnostics["reason_if_failed"])
        self.assertFalse(
            diagnostics["resume_verification"]["selected_filename_matches_active_resume"])

    def test_our_resume_selected_alongside_a_stale_card_is_still_a_success(self) -> None:
        """DELIBERATE relaxation: verification must not be stricter than LinkedIn.

        If our resume IS the selected radio and Next is enabled, LinkedIn considers the step
        done -- so we do too, even if a stale card still reports itself selected after the
        re-render. `exactly_one_selected` is recorded for diagnosis but must never veto.
        """
        class StickySelectionModal(FakeResumeModal):
            def on_upload(self) -> None:                 # old card stays selected too
                self.cards.append({"filename": ACTIVE, "selected": True, "marked": False})

        modal = StickySelectionModal(cards=[{"filename": OTHER, "selected": True}])
        _, diagnostics = self._run(modal)

        self.assertEqual(len([c for c in modal.cards if c["selected"]]), 2)
        self.assertFalse(diagnostics["mismatch_detected"])          # accepted
        verification = diagnostics["resume_verification"]
        self.assertFalse(verification["exactly_one_selected"])      # recorded...
        self.assertTrue(verification["selected_filename_matches_active_resume"])  # ...but not a gate

    def test_a_different_resume_being_the_selected_one_is_still_rejected(self) -> None:
        """The relaxation must not let the WRONG resume through."""
        class WrongSelectionModal(FakeResumeModal):
            def on_upload(self) -> None:                 # new card added but OTHER stays selected
                self.cards.append({"filename": ACTIVE, "selected": False, "marked": False})

            def evaluate(self, script, _arg=None):
                automation = LinkedInAutomation(Settings(openai_api_key=None))
                if script == automation._SELECT_NEW_RESUME_CARD_JS:
                    self.clicked_new += 1                # clicking it does not take effect
                    return True
                return super().evaluate(script, _arg)

        modal = WrongSelectionModal(cards=[{"filename": OTHER, "selected": True}])
        _, diagnostics = self._run(modal)

        self.assertTrue(diagnostics["mismatch_detected"])
        self.assertIn("selected_filename_matches_active_resume=False",
                      diagnostics["reason_if_failed"])

    # -- backward compatibility -----------------------------------------------------------------------
    def test_unreadable_card_list_is_no_stricter_than_before(self) -> None:
        class UploadOnlyModal(FakeResumeModal):
            def evaluate(self, script, _arg=None):
                raise RuntimeError("no card list on this step")

        modal = UploadOnlyModal(cards=[])
        uploaded, diagnostics = self._run(modal)
        self.assertTrue(uploaded)
        self.assertTrue(diagnostics["upload_success"])
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_no_file_input_is_not_an_upload_and_not_a_mismatch(self) -> None:
        modal = FakeResumeModal(cards=[], has_file_input=False)
        uploaded, diagnostics = self._run(modal)
        self.assertFalse(uploaded)
        self.assertEqual(modal.uploads, [])
        self.assertFalse(diagnostics["mismatch_detected"])

    def test_idempotent_second_call_does_not_upload_twice(self) -> None:
        modal = FakeResumeModal(cards=[])
        diagnostics = self._diagnostics()
        diagnostics["upload_success"] = True
        uploaded = self.automation._upload_resume_if_needed(modal, self.resume_path, diagnostics, None)
        self.assertFalse(uploaded)
        self.assertEqual(modal.uploads, [])

    # -- filename normalisation -------------------------------------------------------------------------
    def test_toggle_labels_are_stripped_to_the_bare_filename(self) -> None:
        match = self.automation._resume_names_match
        self.assertTrue(match(ACTIVE, f"Select resume {ACTIVE}"))
        self.assertTrue(match(ACTIVE, f"Deselect resume {ACTIVE}"))   # the bug
        self.assertTrue(match(ACTIVE, ACTIVE.upper()))
        self.assertFalse(match(ACTIVE, OTHER))
        self.assertFalse(match(ACTIVE, None))


if __name__ == "__main__":
    unittest.main()
